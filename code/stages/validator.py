"""Stage 5: validate a state report against its case bundle and the deletion rules, and validate
final output rows against the output contract. Every check returns a specific, actionable message
so an agent can be told exactly what to fix.
"""
from __future__ import annotations

import json

from stages.state_report import (CATEGORIES, LINK_BASES, OBSERVED_STATES, REQUIRED_STATES, is_compliant,
                          required_state_map)
from stages.steps import REMEDIATION_ORDER, STEP_FIELDS, VERIFICATION_ORDER, FINDING_ORDER

RESOURCE_FIELDS = {"resource_id", "system_id", "category", "observed_state", "required_state", "compliant",
                   "evidence_id", "link_basis", "rationale"}
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
PHASE_OPS = {"FINDING": set(FINDING_ORDER), "REMEDIATION": set(REMEDIATION_ORDER),
             "VERIFICATION": set(VERIFICATION_ORDER)}
PHASES = ["FINDING", "REMEDIATION", "VERIFICATION"]


def validate_report(report: dict, bundle: dict, rules: list[dict]) -> list[str]:
    errors = []
    if report.get("intent") not in ("DELETION", "NOT_DELETION"):
        return [f"intent must be DELETION or NOT_DELETION, got {report.get('intent')!r}"]
    if report["intent"] == "NOT_DELETION":
        return [] if not report.get("resources") else ["a NOT_DELETION report must list no resources"]

    required = required_state_map(rules)
    known = {r["resource_id"]: r for r in bundle["resources"]}
    audit_at = bundle["audit_at"]
    seen = set()
    for i, res in enumerate(report.get("resources", [])):
        rid = res.get("resource_id")
        where = f"resources[{i}] ({rid})"
        missing = RESOURCE_FIELDS - set(res)
        if missing:
            errors.append(f"{where}: missing fields {sorted(missing)}")
            continue
        if rid in seen:
            errors.append(f"{where}: listed twice")
        seen.add(rid)
        if rid not in known:
            errors.append(f"{where}: not in the case bundle; only linked resources may be reported")
            continue
        b = known[rid]
        if res["category"] not in CATEGORIES:
            errors.append(f"{where}: unknown category {res['category']!r}")
            continue
        if res["observed_state"] is not None and res["observed_state"] not in OBSERVED_STATES:
            errors.append(f"{where}: unknown observed_state {res['observed_state']!r}")
        if res["required_state"] not in REQUIRED_STATES or res["required_state"] != required.get(res["category"]):
            errors.append(f"{where}: required_state for {res['category']} must be {required.get(res['category'])}")
        expected = is_compliant(res["required_state"], res["observed_state"]) if res["required_state"] in REQUIRED_STATES else None
        if res["compliant"] != expected:
            errors.append(f"{where}: compliant={res['compliant']} contradicts observed {res['observed_state']} "
                          f"vs required {res['required_state']} (expected {expected})")
        if res["observed_state"] is not None and b["latest_state"] is not None and res["observed_state"] != b["latest_state"]:
            # Allowed only as an explicit adjudication; it must say why.
            if not res.get("rationale"):
                errors.append(f"{where}: observed_state differs from evidence ({b['latest_state']}) without a rationale")
        if res["link_basis"] not in LINK_BASES or res["link_basis"] not in b["link_basis"]:
            errors.append(f"{where}: link_basis {res['link_basis']!r} not supported by the bundle {b['link_basis']}")
        if b["link_basis"] == ["catalog_claim"] and b["latest_state"] is None:
            errors.append(f"{where}: catalog claim with no evidence observation cannot be reported")
        ev = res["evidence_id"]
        if ev is not None and ev not in b["citable"]:
            errors.append(f"{where}: evidence_id {ev} is not citable for this resource at audit_at {audit_at} "
                          f"(citable: {b['citable']})")
        if res["compliant"] is False and not ev:
            errors.append(f"{where}: a violation needs a citable evidence_id")
    for rid, b in known.items():
        if rid not in seen and not any(x["resource_id"] == rid for x in report.get("excluded", [])):
            errors.append(f"{rid}: linked in the bundle but neither reported nor excluded with a reason")
    return errors


def validate_row(status: str, steps: list[dict]) -> list[str]:
    """Output contract checks (AGENTS.md section 6.3/6.4)."""
    errors = []
    if status not in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE"):
        return [f"invalid deletion_status {status!r}"]
    try:
        json.loads(json.dumps(steps))
    except (TypeError, ValueError) as exc:
        return [f"steps are not JSON-serialisable: {exc}"]
    if status == "NOT_APPLICABLE":
        return [] if steps == [] else ["NOT_APPLICABLE must have steps_json=[]"]
    phase_idx = -1
    for n, s in enumerate(steps, 1):
        if set(s) != set(STEP_FIELDS):
            errors.append(f"step {n}: fields must be exactly {list(STEP_FIELDS)}")
            continue
        if s["step_no"] != n:
            errors.append(f"step {n}: step_no is {s['step_no']}, expected {n}")
        if s["phase"] not in PHASES:
            errors.append(f"step {n}: invalid phase {s['phase']}")
            continue
        if PHASES.index(s["phase"]) < phase_idx:
            errors.append(f"step {n}: phase {s['phase']} out of order")
        phase_idx = max(phase_idx, PHASES.index(s["phase"]))
        if s["operation"] not in PHASE_OPS[s["phase"]]:
            errors.append(f"step {n}: {s['operation']} is not a {s['phase']} operation")
        elif OP_STATE[s["operation"]] != s["state"]:
            errors.append(f"step {n}: {s['operation']} requires state {OP_STATE[s['operation']]}")
        if s["phase"] == "FINDING" and not s["evidence_id"]:
            errors.append(f"step {n}: findings must cite evidence")
        if status == "INCOMPLETE" and s["phase"] != "FINDING" and s["evidence_id"]:
            errors.append(f"step {n}: remediation/verification in an INCOMPLETE case must have empty evidence_id")
        if status == "COMPLETE" and (s["phase"] != "VERIFICATION" or not s["evidence_id"]):
            errors.append(f"step {n}: COMPLETE cases contain only evidence-backed verification steps")
    if status == "INCOMPLETE" and not any(s.get("phase") == "FINDING" for s in steps):
        errors.append("INCOMPLETE case has no findings")
    return errors
