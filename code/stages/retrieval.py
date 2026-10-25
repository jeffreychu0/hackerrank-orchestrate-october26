"""Stage 3: per-case retrieval from cache/index.db (docs/pipeline.md section 3).

Builds a case bundle: the subject's identifiers, every resource provably linked to them,
and each resource's latest state at audit_at with citable evidence. Linkage rules
(docs/pipeline.md section 8):
  * resources enter only through a proven link - never by guessing from timing;
  * observations after audit_at may prove linkage only, and only for a resource that
    also has its own evidence at or before audit_at (later existence does not prove
    earlier existence); state and citations use obs_time <= audit_at and
    file_captured_at <= audit_at.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from stages.evidence_index.normalize import record_type_for, system_for_resource

# When several stores report the same resource at the same instant, any copy that
# still holds data wins over ABSENT (replica drift), and exposure wins over neutral presence.
STATE_PRIORITY = {"IDENTIFIABLE": 9, "ACTIVE": 8, "PRESENT": 7, "RETAINED": 6, "REDACTED": 5,
                  "ANONYMIZED": 4, "DISABLED": 3, "BLOCKED": 2, "ABSENT": 1, "UNKNOWN": 0}
LINK_PRIORITY = {"direct": 6, "query": 5, "trace": 4, "resource_link": 3, "catalog_claim": 2,
                 "post_audit_identifier": 1}
# States that say the same thing for citation purposes: the record still holds the person's data.
# (PRESENT = exists; IDENTIFIABLE = exists with identifiers visible.) Every other state is its own class.
STATE_CLASS = {"PRESENT": "holds_data", "IDENTIFIABLE": "holds_data"}


def _state_class(state: str | None) -> str | None:
    return STATE_CLASS.get(state, state)


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def resolve_subject(db: sqlite3.Connection, email: str) -> dict | None:
    row = db.execute("SELECT user_id, is_current, source_id FROM identities WHERE identifier=? AND id_kind='email' "
                     "ORDER BY is_current DESC LIMIT 1", (email.strip().lower(),)).fetchone()
    if not row:
        return None
    ids = db.execute("SELECT identifier, id_kind, is_current, source_id FROM identities WHERE user_id=? "
                     "AND id_kind <> 'name' ORDER BY id_kind, is_current DESC, identifier", (row["user_id"],)).fetchall()
    return {"user_id": row["user_id"], "request_email_is_current": bool(row["is_current"]),
            "identifiers": [dict(r) for r in ids]}


def _latest_state(db: sqlite3.Connection, resource_id: str, audit_at: str) -> dict:
    rows = db.execute("SELECT obs_id, file_id, file_captured_at, obs_time, store_role, derived_state, record_type, "
                      "record_type_source, system_id, container, fields_json, source_ref FROM observations "
                      "WHERE resource_id=? AND obs_time<=? ORDER BY obs_time, obs_id", (resource_id, audit_at)).fetchall()
    if not rows:
        return {"latest_state": None, "timeline": []}
    last_time = rows[-1]["obs_time"]
    at_last = [r for r in rows if r["obs_time"] == last_time]
    winner = max(at_last, key=lambda r: (STATE_PRIORITY.get(r["derived_state"], 0), r["obs_id"]))
    types = [(r["record_type"], r["record_type_source"]) for r in rows if r["record_type"]]
    explicit = [t for t, s in types if s in ("explicit", "collection", "event")]
    # Cite the most recent citable observation that supports the winning state (same state class).
    # The newest observation may sit in a file exported after audit_at (inner snapshot time <=
    # audit_at), while an earlier citable file shows the same condition, e.g. IDENTIFIABLE vs PRESENT.
    winner_class = _state_class(winner["derived_state"])
    supporting = [r for r in rows if r["file_captured_at"] <= audit_at and _state_class(r["derived_state"]) == winner_class]
    citable_time = supporting[-1]["obs_time"] if supporting else None
    citable = sorted({r["file_id"] for r in supporting if r["obs_time"] == citable_time})
    citable_rows = [r for r in rows if r["file_captured_at"] <= audit_at]
    conflict = bool(citable_rows) and _state_class(citable_rows[-1]["derived_state"]) != winner_class
    return {
        "citable_state_conflict": conflict,
        "latest_state": winner["derived_state"],
        "latest_time": last_time,
        "latest_store_roles": sorted({r["store_role"] for r in at_last}),
        "record_type": explicit[-1] if explicit else (types[-1][0] if types else None),
        "system_id": winner["system_id"],
        "citable": citable,
        "latest_fields": json.loads(winner["fields_json"]),
        "timeline": [{"t": r["obs_time"], "state": r["derived_state"], "file": r["file_id"],
                      "store": r["store_role"], "ref": r["source_ref"]} for r in rows],
    }


def _activity(db: sqlite3.Connection, resource_id: str, audit_at: str) -> list[dict]:
    return [dict(r) for r in db.execute(
        "SELECT activity_id, event_time, event_type, reported_result FROM activity WHERE resource_id=? AND event_time<=? "
        "ORDER BY event_time, seq", (resource_id, audit_at))]


def retrieve(db: sqlite3.Connection, case_id: str, subject: dict | None = None) -> dict:
    """Case bundle for case_id. `subject` may be supplied by stage 2 (needs user_id + identifiers
    as dicts with identifier/id_kind); otherwise it is resolved here from the request email."""
    req = db.execute("SELECT * FROM requests WHERE case_id=?", (case_id,)).fetchone()
    if not req:
        raise KeyError(f"unknown case_id {case_id}")
    audit = req["audit_at"]
    if subject is None:
        subject = resolve_subject(db, req["email_norm"])
    bundle = {"case_id": case_id, "email": req["email"], "requested_at": req["requested_at"],
              "reported_status": req["reported_status"], "audit_at": audit, "request_text": req["request_text"],
              "subject": subject, "resources": [], "case_flags": []}
    if not subject:
        bundle["case_flags"].append("SUBJECT_NOT_RESOLVED")
        return bundle

    links: dict[str, dict] = defaultdict(lambda: {"bases": set(), "evidence": set()})
    keys = [(i.get("identifier", i.get("value")), i.get("id_kind", i.get("kind"))) for i in subject["identifiers"]]
    for ident, kind in keys:
        for r in db.execute("SELECT o.resource_id, o.obs_time, o.file_id, oi.link_via FROM observation_identifiers oi "
                            "JOIN observations o USING(obs_id) WHERE oi.identifier=? AND oi.id_kind=? "
                            "AND o.resource_id IS NOT NULL", (ident, kind)):
            basis = r["link_via"] if r["obs_time"] <= audit else "post_audit_identifier"
            links[r["resource_id"]]["bases"].add(basis)
            if basis != "post_audit_identifier":
                links[r["resource_id"]]["evidence"].add(r["file_id"])
    # Catalog ownership claims are unverified; one counts only if the catalog recorded it while this
    # request was being handled (requested_at..audit_at). An older claim may be stale.
    for r in db.execute("SELECT resource_id, activity_id FROM activity WHERE linked_user_id=? "
                        "AND event_time>=? AND event_time<=?", (subject["user_id"], req["requested_at"], audit)):
        links[r["resource_id"]]["bases"].add("catalog_claim")
        links[r["resource_id"]]["evidence"].add(r["activity_id"])
    # One hop through resource links (repopulation, restore, search document -> source account).
    for rid in list(links):
        for r in db.execute("SELECT related_resource_id, relation, source FROM resource_links WHERE resource_id=? "
                            "AND (event_time IS NULL OR event_time<=?)", (rid, audit)):
            links[r["related_resource_id"]]["bases"].add("resource_link")
            links[r["related_resource_id"]]["evidence"].add(f"{r['source']}:{r['relation']}:{rid}")

    bundle["excluded"] = []
    for rid in sorted(links):
        info = links[rid]
        state = _latest_state(db, rid, audit)
        if info["bases"] == {"post_audit_identifier"} and state["latest_state"] is None:
            # Later evidence may say whose a record is, but not that it already existed at audit_at.
            bundle["excluded"].append({"resource_id": rid, "reason": "LINKED_ONLY_AFTER_AUDIT_WITHOUT_PRE_AUDIT_EVIDENCE"})
            continue
        flags = []
        bases = sorted(info["bases"], key=lambda b: -LINK_PRIORITY.get(b, 0))
        if bases == ["catalog_claim"]:
            flags.append("CATALOG_ONLY_LINK")
        if bases == ["post_audit_identifier"]:
            flags.append("LINKED_ONLY_AFTER_AUDIT")
        if state["latest_state"] is None:
            flags.append("NO_PRE_AUDIT_OBSERVATION")
        if state.get("record_type") is None and rid.startswith(("support_", "object_")):
            flags.append("CATEGORY_UNRESOLVED")
        if state["latest_state"] and not state["citable"]:
            flags.append("NOT_CITABLE")
        if state.get("citable_state_conflict"):
            flags.append("LATEST_CITABLE_STATE_DIFFERS")
        bundle["resources"].append({
            "resource_id": rid, "system_id": state.get("system_id") or system_for_resource(rid),
            "record_type": state.get("record_type") or record_type_for(rid)[0],
            "latest_state": state["latest_state"], "latest_time": state.get("latest_time"),
            "latest_store_roles": state.get("latest_store_roles", []), "citable": state.get("citable", []),
            "link_basis": bases, "link_evidence": sorted(info["evidence"]), "flags": flags,
            "activity_claims": _activity(db, rid, audit), "timeline": state["timeline"],
            "latest_fields": state.get("latest_fields"),
        })
    if not any(r["resource_id"].startswith("account_") for r in bundle["resources"]):
        bundle["case_flags"].append("NO_ACCOUNT_RESOURCE_LINKED")
    return bundle
