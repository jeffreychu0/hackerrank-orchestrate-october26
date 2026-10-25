"""Loaders for the CSV reference tables: identities, activity, rules, requests, manifest."""
from __future__ import annotations

import csv
import os
import re
from collections import defaultdict

from .model import EvidenceFile, Sink
from .normalize import norm_email, norm_name, norm_phone

KIND_BY_FIELD = {"email": "email", "phone": "phone", "email_hash": "email_hash"}
CATALOG_RE = re.compile(r"Data catalog linked (\S+) \(([^)]+)\) to (\S+) and (user_\d+)\.?$")
ATTACHMENT_RE = re.compile(
    r"Attachment catalog confirmed (\S+) \(([^)]+)\) belongs only to (\S+) and (user_\d+); no other subject is linked\.?$")
DOWNSTREAM_RE = re.compile(r"repopulated downstream resource (\w+)")


def read_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _norm(kind: str, value: str) -> str:
    if kind == "email":
        return norm_email(value)
    if kind == "phone":
        return norm_phone(value)
    if kind == "name":
        return norm_name(value)
    return value.strip().lower()


def load_manifest(dataset: str) -> list[EvidenceFile]:
    rows = read_csv(os.path.join(dataset, "evidence_files.csv"))
    files = [EvidenceFile(file_id=r["file_id"], path=r["file_path"],
                          abs_path=os.path.join(dataset, *r["file_path"].split("/")),
                          origin=r["origin"], captured_at=r["captured_at"]) for r in rows]
    return sorted(files, key=lambda f: int(f.file_id.rsplit("_", 1)[1]))


def load_identities(dataset: str) -> list[tuple]:
    """Rows of (identifier, id_kind, user_id, is_current, valid_from, valid_to, source_id)."""
    users = read_csv(os.path.join(dataset, "user_identities.csv"))
    audit = read_csv(os.path.join(dataset, "user_audit_log.csv"))
    history = defaultdict(list)
    for a in sorted(audit, key=lambda a: (a["event_time"], a["audit_id"])):
        history[(a["user_id"], a["field_name"])].append(a)
    rows = set()
    for u in users:
        uid = u["user_id"]
        rows.add((uid.lower(), "user_ref", uid, 1, None, None, "snapshot"))
        rows.add((_norm("name", u["name"]), "name", uid, 1, None, None, "snapshot"))
        for field, kind in KIND_BY_FIELD.items():
            changes = history.get((uid, field), [])
            since = changes[-1]["event_time"] if changes else None
            rows.add((_norm(kind, u[field]), kind, uid, 1, since, None, "snapshot"))
            prev_time = None
            for a in changes:
                rows.add((_norm(kind, a["old_value"]), kind, uid, 0, prev_time, a["event_time"], a["audit_id"]))
                prev_time = a["event_time"]
    return sorted(rows, key=lambda r: (r[2], r[1], r[0], r[6]))


def load_activity(dataset: str, sink: Sink) -> list[tuple]:
    rows = read_csv(os.path.join(dataset, "system_activity.csv"))
    out = []
    for seq, r in enumerate(rows, 1):
        details = r["details"]
        downstream = alias = linked_id = linked_user = None
        exclusive = 0
        m = DOWNSTREAM_RE.search(details)
        if m:
            downstream = m.group(1)
            relation = "restored_into" if r["event_type"] == "RESTORE_BACKUP" else "repopulated"
            sink.link(r["resource_id"], downstream, relation, r["activity_id"], r["event_time"])
        m = CATALOG_RE.search(details) or ATTACHMENT_RE.search(details)
        if m:
            _rid, alias, email, linked_user = m.groups()
            linked_id = norm_email(email)
            exclusive = 1 if details.startswith("Attachment catalog") else 0
            if alias != r["resource_id"]:
                kind = "statement" if alias.startswith("NST-") else ("file" if "." in alias else "reference")
                sink.alias(alias, kind, r["resource_id"], r["activity_id"])
            else:
                alias = None
        out.append((r["activity_id"], seq, r["event_time"], r["event_type"], r["source_system"],
                    r["target_system"], r["resource_id"], r["reported_result"], details, downstream, alias,
                    linked_id, linked_user, exclusive))
    return out


def load_rules(dataset: str) -> list[tuple]:
    return [(r["rule_id"], r["data_category"], r["condition"], r["required_action"])
            for r in read_csv(os.path.join(dataset, "deletion_rules.csv"))]


def load_requests(dataset: str) -> list[tuple]:
    out = []
    for name, is_sample in (("deletion_requests.csv", 0), ("sample_deletion_requests.csv", 1)):
        path = os.path.join(dataset, name)
        if not os.path.exists(path):
            continue
        for r in read_csv(path):
            out.append((r["case_id"], is_sample, r["email"], norm_email(r["email"]), r["requested_at"],
                        r["reported_status"], r["reported_at"], r["audit_at"], r["request_text"]))
    return out
