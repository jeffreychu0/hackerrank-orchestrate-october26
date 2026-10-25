"""Stage 6: turn a validated state report into deletion_status + steps_json, deterministically.

The mapping and ordering rules come from docs/sample_case_analysis.md section 3 and hold for all
32 samples. No model writes status, operations, order, numbering, or evidence blanking.
"""
from __future__ import annotations

import json

# required end state -> (finding op, finding state, remediation op, remediation state, verify op, verify state)
VIOLATION_OPS = {
    "ABSENT": ("REPORT_REMAINING_RECORD", "PRESENT", "DELETE_RECORD", "ABSENT", "VERIFY_RECORD_ABSENT", "ABSENT"),
    "REDACTED": ("REPORT_EXPOSED_PII", "PRESENT", "REDACT_RECORD", "REDACTED", "VERIFY_RECORD_REDACTED", "REDACTED"),
    "ANONYMIZED": ("REPORT_EXPOSED_PII", "PRESENT", "ANONYMIZE_RECORD", "ANONYMIZED",
                   "VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    "DISABLED": ("REPORT_ACTIVE_DATA_FLOW", "ACTIVE", "DISABLE_DATA_FLOW", "DISABLED",
                 "VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    "BLOCKED": ("REPORT_RESTORE_RISK", "ACTIVE", "BLOCK_RESTORE", "BLOCKED", "VERIFY_RESTORE_BLOCKED", "BLOCKED"),
}
# required end state -> verification for a resource already in that state
VERIFY_OPS = {
    "ABSENT": ("VERIFY_RECORD_ABSENT", "ABSENT"),
    "RETAINED": ("VERIFY_RECORD_PRESENT", "PRESENT"),
    "REDACTED": ("VERIFY_RECORD_REDACTED", "REDACTED"),
    "ANONYMIZED": ("VERIFY_RECORD_ANONYMIZED", "ANONYMIZED"),
    "DISABLED": ("VERIFY_DATA_FLOW_DISABLED", "DISABLED"),
    "BLOCKED": ("VERIFY_RESTORE_BLOCKED", "BLOCKED"),
}
FINDING_ORDER = ["REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK"]
# Stop recreation first, then change records.
REMEDIATION_ORDER = ["DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD"]
VERIFICATION_ORDER = ["VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED",
                      "VERIFY_RECORD_ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED"]
STEP_FIELDS = ("step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id")


def _step(phase, res, operation, state, evidence=""):
    return {"phase": phase, "system_id": res["system_id"], "resource_id": res["resource_id"],
            "operation": operation, "state": state, "evidence_id": evidence or ""}


def _order(steps, op_order, account_first=False):
    def key(s):
        account = 0 if (account_first and s["system_id"] == "account_service") else 1
        return op_order.index(s["operation"]), account, s["system_id"], s["resource_id"]
    return sorted(steps, key=key)


def generate_steps(report: dict) -> tuple[str, list[dict], list[str]]:
    """Return (deletion_status, steps, flags) for one validated state report."""
    flags = []
    if report["intent"] == "NOT_DELETION":
        return "NOT_APPLICABLE", [], flags
    resources = report["resources"]
    violations = [r for r in resources if r["compliant"] is False and r["required_state"] in VIOLATION_OPS]
    for r in resources:
        if r["compliant"] is False and r["required_state"] == "RETAINED":
            flags.append(f"RETAINED_RECORD_MISSING:{r['resource_id']}")   # no allowed operation restores it

    if violations:
        status = "INCOMPLETE"
        findings, remediation, verification = [], [], []
        remediated = set()
        for r in violations:
            f_op, f_state, r_op, r_state, v_op, v_state = VIOLATION_OPS[r["required_state"]]
            findings.append(_step("FINDING", r, f_op, f_state, r["evidence_id"]))
            remediation.append(_step("REMEDIATION", r, r_op, r_state))
            verification.append(_step("VERIFICATION", r, v_op, v_state))
            remediated.add(r["resource_id"])
        # Post-remediation checks also confirm the account is gone and retained records still exist.
        for r in resources:
            if r["resource_id"] in remediated:
                continue
            if r["category"] == "USER_PROFILE" or r["required_state"] == "RETAINED":
                v_op, v_state = VERIFY_OPS[r["required_state"]]
                verification.append(_step("VERIFICATION", r, v_op, v_state))
        steps = (_order(findings, FINDING_ORDER) + _order(remediation, REMEDIATION_ORDER)
                 + _order(verification, VERIFICATION_ORDER, account_first=True))
    else:
        status = "COMPLETE"
        verification = []
        for r in resources:
            if r["compliant"] and r["evidence_id"]:
                v_op, v_state = VERIFY_OPS[r["required_state"]]
                verification.append(_step("VERIFICATION", r, v_op, v_state, r["evidence_id"]))
            elif r["compliant"] is None:
                flags.append(f"UNVERIFIABLE:{r['resource_id']}")
        steps = _order(verification, VERIFICATION_ORDER, account_first=True)
        if not steps:
            flags.append("COMPLETE_WITHOUT_VERIFIABLE_EVIDENCE")

    numbered = [{"step_no": i, **s} for i, s in enumerate(steps, 1)]
    return status, [{k: s[k] for k in STEP_FIELDS} for s in numbered], flags


def serialize_steps(steps: list[dict]) -> str:
    """Deterministic JSON: contract field order, no whitespace."""
    return json.dumps([{k: s[k] for k in STEP_FIELDS} for s in steps], ensure_ascii=False, separators=(",", ":"))
