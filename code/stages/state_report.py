"""The state report: the contract between adjudication (stage 4) and everything after it.

A state report says, for one case, what state each linked resource was in at audit_at and what
state the deletion policy requires. Stages 5-7 turn it into the output row deterministically.

    {"case_id": str, "intent": "DELETION" | "NOT_DELETION", "source": "agent" | "fallback",
     "resources": [{"resource_id", "system_id", "category", "observed_state", "required_state",
                    "compliant", "evidence_id", "link_basis", "rationale"}],
     "excluded": [{"resource_id", "reason"}]}

This module also builds the deterministic fallback report straight from a stage-3 bundle. It is
used until the stage-4 agent exists, and later whenever the agent's report fails validation.
"""
from __future__ import annotations

CATEGORIES = ("USER_PROFILE", "SECONDARY_IDENTIFIER", "SEARCH_INDEX_ENTRY", "CACHE_ENTRY", "SUPPORT_MESSAGE",
              "SUPPORT_ATTACHMENT", "ANALYTICS_PROFILE", "PRODUCT_TELEMETRY", "USER_EXPORT", "MARKETING_PROFILE",
              "EMAIL_DELIVERY_LOG", "SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD", "BACKUP_ARCHIVE",
              "ACCOUNT_TOMBSTONE", "DATA_FLOW")
OBSERVED_STATES = ("PRESENT", "ABSENT", "IDENTIFIABLE", "ANONYMIZED", "REDACTED", "ACTIVE", "DISABLED",
                   "BLOCKED", "RETAINED")
REQUIRED_STATES = ("ABSENT", "REDACTED", "ANONYMIZED", "RETAINED", "DISABLED", "BLOCKED")
LINK_BASES = ("direct", "query", "trace", "resource_link", "catalog_claim", "post_audit_identifier")

ACTION_TO_STATE = {"DELETE": "ABSENT", "REDACT": "REDACTED", "ANONYMIZE": "ANONYMIZED", "RETAIN": "RETAINED"}
# Observed states that satisfy each required state. A deleted copy satisfies redact/anonymize/disable/block
# (nothing identifying is left); a retained record must still exist.
SATISFIES = {
    "ABSENT": {"ABSENT"},
    "REDACTED": {"REDACTED", "ABSENT"},
    "ANONYMIZED": {"ANONYMIZED", "ABSENT"},
    "RETAINED": {"RETAINED", "PRESENT", "REDACTED", "ANONYMIZED"},
    "DISABLED": {"DISABLED", "ABSENT"},
    "BLOCKED": {"BLOCKED", "ABSENT"},
}


def required_state_map(rules: list[dict]) -> dict[str, str]:
    """data_category -> required end state, from deletion_rules.csv plus two non-rule categories."""
    out = {r["data_category"]: ACTION_TO_STATE[r["required_action"]] for r in rules}
    # A backup is retained, but compliance means its restore path is blocked.
    if out.get("BACKUP_ARCHIVE") == "RETAINED":
        out["BACKUP_ARCHIVE"] = "BLOCKED"
    out["DATA_FLOW"] = "DISABLED"   # active sync/import processes must be switched off
    return out


def is_compliant(required: str, observed: str | None) -> bool | None:
    if observed is None:
        return None
    return observed in SATISFIES[required]


def _fallback_category(resource: dict) -> str | None:
    category = resource.get("record_type")
    if category:
        return category
    rid = resource["resource_id"]
    if rid.startswith(("support_", "object_")):
        # Unresolved support record. If the evidence is the uploaded document itself, it is a file;
        # otherwise identity fields seen -> shared conversation, else a file.
        if any(t["store"] in ("document", "image") for t in resource.get("timeline", [])):
            return "SUPPORT_ATTACHMENT"
        return "SUPPORT_MESSAGE" if resource.get("latest_state") in ("IDENTIFIABLE", "REDACTED") else "SUPPORT_ATTACHMENT"
    return None


def build_fallback_report(bundle: dict, intent: str, rules: list[dict]) -> dict:
    """Deterministic state report from a stage-3 bundle: latest state per resource + policy lookup."""
    required = required_state_map(rules)
    report = {"case_id": bundle["case_id"], "intent": intent, "source": "fallback", "resources": [],
              "excluded": list(bundle.get("excluded", []))}
    if intent == "NOT_DELETION":
        return report
    for res in bundle["resources"]:
        rid = res["resource_id"]
        category = _fallback_category(res)
        observed = res["latest_state"]
        if category not in required:
            report["excluded"].append({"resource_id": rid, "reason": f"UNKNOWN_CATEGORY:{category}"})
            continue
        if observed is None and set(res["link_basis"]) <= {"catalog_claim"}:
            report["excluded"].append({"resource_id": rid, "reason": "CATALOG_CLAIM_WITHOUT_EVIDENCE"})
            continue
        need = required[category]
        compliant = is_compliant(need, observed)
        evidence = res["citable"][0] if res["citable"] else None
        if compliant is False and evidence is None:
            report["excluded"].append({"resource_id": rid, "reason": "VIOLATION_WITHOUT_CITABLE_EVIDENCE"})
            continue
        report["resources"].append({
            "resource_id": rid, "system_id": res["system_id"], "category": category,
            "observed_state": observed, "required_state": need, "compliant": compliant,
            "evidence_id": evidence, "link_basis": res["link_basis"][0] if res["link_basis"] else None,
            "rationale": f"latest pre-audit state {observed or 'unknown'}; policy requires {need}",
        })
    return report
