"""Stage 2: resolve who a deletion request is about.

Request email (current or historical) -> user_id -> every identifier the person has had, with
validity windows. Matching is exact on the normalised email. Names are kept for display only:
they are not unique in this dataset, so they are never used to link evidence.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class Identity:
    user_id: str | None
    request_email: str
    request_email_status: str       # current | historical | unknown
    name: str | None = None
    identifiers: list[dict] = field(default_factory=list)   # every current and historical identifier
    flags: list[str] = field(default_factory=list)
    related_cases: list[str] = field(default_factory=list)  # other cases about the same person

    def as_subject(self) -> dict:
        """The subject shape stage 3 retrieval expects."""
        return {"user_id": self.user_id, "request_email_is_current": self.request_email_status == "current",
                "identifiers": self.identifiers}


def resolve_identity(db: sqlite3.Connection, case: dict) -> Identity:
    email = case["email"].strip().lower()
    owners = db.execute(
        "SELECT DISTINCT user_id, is_current, valid_from, valid_to FROM identities "
        "WHERE identifier=? AND id_kind='email'", (email,)).fetchall()
    user_ids = sorted({r[0] for r in owners})
    if not user_ids:
        return Identity(None, email, "unknown", flags=["SUBJECT_NOT_RESOLVED"])
    ident = Identity(user_ids[0], email, "current" if any(r[1] for r in owners) else "historical")
    if len(user_ids) > 1:
        # An address that moved between people: refuse to pick one.
        ident.user_id = None
        ident.flags.append("AMBIGUOUS_SUBJECT:" + ",".join(user_ids))
        return ident

    rows = db.execute(
        "SELECT identifier, id_kind, is_current, valid_from, valid_to, source_id FROM identities "
        "WHERE user_id=? ORDER BY id_kind, is_current DESC, valid_to, identifier", (ident.user_id,)).fetchall()
    for value, kind, is_current, valid_from, valid_to, source in rows:
        if kind == "name":
            ident.name = value if is_current else ident.name
            continue
        ident.identifiers.append({"kind": kind, "value": value, "is_current": bool(is_current),
                                  "valid_from": valid_from, "valid_to": valid_to, "source": source})

    if ident.request_email_status == "historical":
        retired = max((r[3] for r in owners if r[3]), default=None)
        ident.flags.append("REQUEST_EMAIL_HISTORICAL")
        if retired and retired > case["requested_at"]:
            ident.flags.append("REQUEST_EMAIL_CHANGED_AFTER_REQUEST")
    if any(i["valid_from"] and i["valid_from"] > case["audit_at"] for i in ident.identifiers):
        ident.flags.append("IDENTIFIER_CHANGED_AFTER_AUDIT")
    return ident


def resolve_identities(db: sqlite3.Connection, cases: list[dict]) -> dict[str, Identity]:
    out = {c["case_id"]: resolve_identity(db, c) for c in cases}
    by_user: dict[str, list[str]] = {}
    for case_id, ident in out.items():
        if ident.user_id:
            by_user.setdefault(ident.user_id, []).append(case_id)
    for case_ids in by_user.values():
        if len(case_ids) > 1:
            for case_id in case_ids:
                out[case_id].related_cases = [c for c in case_ids if c != case_id]
                out[case_id].flags.append("REPEAT_SUBJECT")
    return out
