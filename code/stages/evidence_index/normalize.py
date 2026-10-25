"""Identifier normalisation, resource/system mapping, and state derivation.

Everything here is pure and deterministic so that the same evidence always yields
the same observations. Identifier matching downstream is exact-only, so the
normalisation rules here are the single place where formatting variance is removed.
"""
from __future__ import annotations

import re
from typing import Iterable

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
RESOURCE_PREFIX_SYSTEM = {
    "account": "account_service",
    "search": "search_service",
    "analytics": "analytics_platform",
    "support": "support_platform",
    "export": "export_service",
    "data_flow": "integration_service",
    "backup": "backup_service",
    "audit_record": "security_service",
    "tombstone": "identity_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
    "cache": "cache_service",
    "email_delivery": "email_delivery_service",
}
RESOURCE_RE = re.compile(
    r"(?<![\w])(" + "|".join(sorted(RESOURCE_PREFIX_SYSTEM, key=len, reverse=True)) + r")_(\d+)(?![\w])"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
USER_RE = re.compile(r"(?<![\w])user_\d+(?![\w])")
PHONE_RE = re.compile(r"\+\d[\d ()\-]{6,}\d")
ALIAS_RES = {
    "order": re.compile(r"\bORD-\d{6,}\b"),
    "ticket": re.compile(r"\bCS-\d{3,}\b"),
    "statement": re.compile(r"\bNST-\d{4}-\d{4}-\d{3,}\b"),
}

NULL_TOKENS = {"", "null", "none", "nil", "-", "—", "n/a", "no direct identifier"}

# Fields whose values are direct identifiers of a person (never a stable internal reference).
DIRECT_ID_FIELDS = {
    "email", "phone", "email_hash", "full_name", "display_name", "name",
    "requester_name", "requester_email", "requester_phone",
    "owner_email", "owner_phone", "customer_email", "uploaded_by",
    "subject_selector", "matched_value", "lookup_value",
}
# Stable internal references: permitted on retained/redacted records, but they still
# make an analytics record attributable.
STABLE_REF_FIELDS = {"customer_reference", "subject_reference", "user_id", "subject_ref"}
NAME_FIELDS = {"full_name", "display_name", "name", "requester_name"}
# Free-text fields scanned for embedded identifiers.
TEXT_FIELDS = {"message_text", "message_excerpt", "details", "document_text"}
# Fields never mined for identifiers (opaque integrity values etc.).
SKIP_ID_FIELDS = {"integrity_seal", "checksum", "archive_key", "object_key", "archive_path"}


def is_null(value) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    return str(value).strip().lower() in NULL_TOKENS


def norm_email(value: str) -> str:
    return value.strip().lower()


def norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return "+" + digits


def norm_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def system_for_resource(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    m = RESOURCE_RE.fullmatch(resource_id)
    return RESOURCE_PREFIX_SYSTEM[m.group(1)] if m else None


def find_resources(text: str) -> list[str]:
    return sorted({m.group(0) for m in RESOURCE_RE.finditer(text)})


def find_aliases(text: str) -> list[tuple[str, str]]:
    out = set()
    for kind, rx in ALIAS_RES.items():
        out.update((m.group(0), kind) for m in rx.finditer(text))
    return sorted(out)


def extract_identifiers(value, field: str | None = None) -> list[tuple[str, str]]:
    """Return normalised (identifier, kind) pairs found in one field value.

    kinds: email | email_hash | phone | user_ref | name
    """
    if is_null(value) or isinstance(value, (bool, int, float)):
        return []
    if field in SKIP_ID_FIELDS:
        return []
    text = str(value)
    found: set[tuple[str, str]] = set()
    for m in EMAIL_RE.finditer(text):
        found.add((norm_email(m.group(0)), "email"))
    for m in HASH_RE.finditer(text):
        found.add((m.group(0).lower(), "email_hash"))
    for m in USER_RE.finditer(text):
        found.add((m.group(0).lower(), "user_ref"))
    for m in PHONE_RE.finditer(text):
        phone = norm_phone(m.group(0))
        if len(phone) >= 9:
            found.add((phone, "phone"))
    if field in NAME_FIELDS and not EMAIL_RE.search(text):
        found.add((norm_name(text), "name"))
    return sorted(found)


def identifiers_from_fields(fields: dict) -> list[tuple[str, str, str]]:
    """(identifier, kind, field) for every identifying value in a flat field dict."""
    out = set()
    for key, value in fields.items():
        if key in TEXT_FIELDS or key in DIRECT_ID_FIELDS or key in STABLE_REF_FIELDS or key in LOOKUP_KEY_FIELDS \
                or key in ("correlation_key", "query"):
            for ident, kind in extract_identifiers(value, key):
                out.add((ident, kind, key))
    return sorted(out)


# Keys on log "started" lines that carry the subject being looked up.
LOOKUP_KEY_FIELDS = {"lookup_key", "subject_key", "subject_ref", "match_key", "owner_key", "scan_key"}


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------
COLLECTION_RECORD_TYPE = {
    "customer_profiles": "USER_PROFILE",
    "profile_lookups": "USER_PROFILE",
    "customer_profiles_primary": "USER_PROFILE",
    "customer_profiles_failover_replica": "USER_PROFILE",
    "customer_search_documents": "SEARCH_INDEX_ENTRY",
    "search_documents_primary": "SEARCH_INDEX_ENTRY",
    "index_queries": "SEARCH_INDEX_ENTRY",
    "customer_analytics_reports": "ANALYTICS_PROFILE",
    "customer_analytics_profiles": "ANALYTICS_PROFILE",
    "profiles": "ANALYTICS_PROFILE",
    "support_conversations": "SUPPORT_MESSAGE",
    "ticket_attachments": "SUPPORT_ATTACHMENT",
    "customer_export_jobs": "USER_EXPORT",
    "customer_exports": "USER_EXPORT",
    "exports": "USER_EXPORT",
    "connector_configurations": "DATA_FLOW",
    "connector_subscriptions": "DATA_FLOW",
    "connectors": "DATA_FLOW",
    "backup_catalog": "BACKUP_ARCHIVE",
    "backup_restore_entries": "BACKUP_ARCHIVE",
    "restore_entries": "BACKUP_ARCHIVE",
    "security_audit_events": "SECURITY_AUDIT_LOG",
    "security_audit_records": "SECURITY_AUDIT_LOG",
    "audit_records": "SECURITY_AUDIT_LOG",
    "deleted_customer_references": "ACCOUNT_TOMBSTONE",
    "retained_order_records": "LEGAL_HOLD_RECORD",
    "retention_register_row": "LEGAL_HOLD_RECORD",
    # Deliberately ambiguous (message vs attachment): left to adjudication.
    "support_records": None,
    "tickets": None,
}
PREFIX_RECORD_TYPE = {
    "account": "USER_PROFILE",
    "search": "SEARCH_INDEX_ENTRY",
    "analytics": "ANALYTICS_PROFILE",
    "export": "USER_EXPORT",
    "data_flow": "DATA_FLOW",
    "backup": "BACKUP_ARCHIVE",
    "audit_record": "SECURITY_AUDIT_LOG",
    "tombstone": "ACCOUNT_TOMBSTONE",
    "legal_hold": "LEGAL_HOLD_RECORD",
    "marketing": "MARKETING_PROFILE",
    "cache": "CACHE_ENTRY",
    "email_delivery": "EMAIL_DELIVERY_LOG",
    # support_ / object_ are ambiguous by prefix alone.
}
EVENT_RECORD_TYPE = [
    ("profile_lookup", "USER_PROFILE"),
    ("index_query", "SEARCH_INDEX_ENTRY"),
    ("analytics_", "ANALYTICS_PROFILE"),
    ("connector_", "DATA_FLOW"),
    ("restore_", "BACKUP_ARCHIVE"),
    ("export_", "USER_EXPORT"),
    ("audit_record", "SECURITY_AUDIT_LOG"),
    ("message_", "SUPPORT_MESSAGE"),
    ("attachment_", "SUPPORT_ATTACHMENT"),
]


def record_type_for(resource_id: str | None, collection: str | None = None, event: str | None = None,
                    explicit: str | None = None) -> tuple[str | None, str]:
    """Return (record_type, source) where source is explicit | collection | event | prefix | unknown."""
    if explicit and not is_null(explicit):
        return explicit.strip().upper(), "explicit"
    if collection is not None and collection in COLLECTION_RECORD_TYPE:
        rt = COLLECTION_RECORD_TYPE[collection]
        if rt:
            return rt, "collection"
    if event:
        for needle, rt in EVENT_RECORD_TYPE:
            if needle in event:
                return rt, "event"
    if resource_id:
        m = RESOURCE_RE.fullmatch(resource_id)
        if m and m.group(1) in PREFIX_RECORD_TYPE:
            return PREFIX_RECORD_TYPE[m.group(1)], "prefix"
    return None, "unknown"


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------
STATES = ("PRESENT", "ABSENT", "IDENTIFIABLE", "ANONYMIZED", "REDACTED",
          "ACTIVE", "DISABLED", "BLOCKED", "RETAINED", "UNKNOWN")
RESULT_STATE = {
    "FOUND": "PRESENT", "NOT_FOUND": "ABSENT", "IDENTIFIABLE": "IDENTIFIABLE",
    "ANONYMIZED": "ANONYMIZED", "REDACTED": "REDACTED", "ACTIVE": "ACTIVE",
    "DISABLED": "DISABLED", "BLOCKED": "BLOCKED",
}
DELETE_TYPES = {"USER_PROFILE", "SEARCH_INDEX_ENTRY", "USER_EXPORT", "SUPPORT_ATTACHMENT",
                "MARKETING_PROFILE", "CACHE_ENTRY", "SECONDARY_IDENTIFIER"}


def as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def exposure(fields: dict, record_type: str | None) -> str | None:
    """IDENTIFIABLE / de-identified state from identity fields, or None if no identity fields observed."""
    observed = False
    direct = False
    stable = False
    for key, value in fields.items():
        if key in DIRECT_ID_FIELDS:
            observed = True
            direct = direct or not is_null(value)
        elif key in STABLE_REF_FIELDS:
            observed = True
            stable = stable or not is_null(value)
        elif key in TEXT_FIELDS and not is_null(value):
            if EMAIL_RE.search(str(value)) or PHONE_RE.search(str(value)):
                observed = direct = True
            elif str(value).strip() == "[REDACTED]":
                observed = True
    if not observed:
        return None
    if record_type == "ANALYTICS_PROFILE":
        return "IDENTIFIABLE" if (direct or stable) else "ANONYMIZED"
    if direct:
        return "IDENTIFIABLE"
    return "REDACTED"


def derive_state(record_type: str | None, fields: dict, presence: str | None = None,
                 explicit_result: str | None = None) -> str:
    """Collapse what one observation says about a resource into a single state."""
    if explicit_result and not is_null(explicit_result):
        state = RESULT_STATE.get(explicit_result.strip().upper(), "UNKNOWN")
        if state == "PRESENT" and record_type in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
            return "RETAINED"
        return state
    if presence == "ABSENT":
        return "ABSENT"
    if record_type == "DATA_FLOW":
        enabled = as_bool(fields.get("enabled"))
        if enabled is not None:
            return "ACTIVE" if enabled else "DISABLED"
    if record_type == "BACKUP_ARCHIVE":
        allowed = as_bool(fields.get("restore_allowed"))
        if allowed is not None:
            return "ACTIVE" if allowed else "BLOCKED"
    if record_type in ("SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"):
        return "RETAINED" if presence != "UNKNOWN" else "UNKNOWN"
    if presence == "UNKNOWN":
        return "UNKNOWN"
    if record_type in DELETE_TYPES:
        return "PRESENT"
    exp = exposure(fields, record_type)
    if exp:
        return exp
    return presence or "PRESENT"


def flatten(record: dict, prefix: str = "") -> dict:
    """Flatten nested dicts (e.g. search `document`, account `profile`) one level into the parent."""
    out = {}
    for key, value in record.items():
        if isinstance(value, dict) and key in ("document", "profile"):
            out.update(flatten(value))
        elif isinstance(value, (dict, list)):
            out[prefix + key] = value
        else:
            out[prefix + key] = value
    return out


def unique(seq: Iterable) -> list:
    seen, out = set(), []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
