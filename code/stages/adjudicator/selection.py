"""Which cases need the stage-4 agent.

The deterministic fallback report is correct whenever the evidence is unambiguous, so the agent
is only asked about cases where a judgement call is needed. Each reason names what to look at.
"""
from __future__ import annotations

# Bundle-level resource flags (from stages/retrieval.py) that need judgement.
JUDGEMENT_FLAGS = {
    "CATEGORY_UNRESOLVED": "data category unknown (e.g. support message vs attachment)",
    "LATEST_CITABLE_STATE_DIFFERS": "newest evidence disagrees with the newest citable evidence",
    "CATALOG_ONLY_LINK": "linked only by a data-catalog claim",
    "NOT_CITABLE": "state known but no citable evidence",
    # LINKED_ONLY_AFTER_AUDIT is deliberately absent: retrieval already applies the decided rule
    # (post-audit evidence may prove ownership of a resource that has its own pre-audit evidence).
}


def adjudication_reasons(bundle: dict, fallback_report: dict) -> list[str]:
    """Empty list = the fallback report can be used as-is."""
    if fallback_report["intent"] != "DELETION":
        return []
    reasons = []
    # Uncitable state only matters when the case would be COMPLETE (every verification must cite
    # evidence); violations without citable evidence are already excluded by the fallback below.
    fallback_complete = not any(r["compliant"] is False for r in fallback_report["resources"])
    for res in bundle["resources"]:
        rid = res["resource_id"]
        for flag in res["flags"]:
            if flag == "NOT_CITABLE" and not fallback_complete:
                continue
            if flag in JUDGEMENT_FLAGS:
                reasons.append(f"{rid}: {JUDGEMENT_FLAGS[flag]}")
        if res.get("record_type") == "ANALYTICS_PROFILE" and res["latest_state"] == "PRESENT":
            reasons.append(f"{rid}: analytics record present but neither identifiable nor anonymized - "
                           "check what it is and which identifier links it")
    accounts = [r["resource_id"] for r in bundle["resources"] if r.get("record_type") == "USER_PROFILE"]
    if len(accounts) > 1:
        reasons.append(f"several account records linked ({', '.join(accounts)}): confirm each belongs to the subject")
    for ex in fallback_report.get("excluded", []):
        if ex["reason"].startswith(("UNKNOWN_CATEGORY", "VIOLATION_WITHOUT_CITABLE_EVIDENCE")):
            reasons.append(f"{ex['resource_id']}: excluded by fallback ({ex['reason']})")
    return sorted(set(reasons))
