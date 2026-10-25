"""Mutation test for the judge: plant one known error in a copy of a correct output row and see
whether the judge catches it. Combined with its verdicts on the unmodified rows (false alarms),
this measures the judge's sensitivity per error type, which agreement alone cannot show.
"""
from __future__ import annotations

import copy
import random

from stages.steps import STEP_FIELDS

# Op swaps that keep a valid operation/state pair, so only the substance is wrong.
OP_SWAP = {
    "DELETE_RECORD": ("ANONYMIZE_RECORD", "ANONYMIZED"), "ANONYMIZE_RECORD": ("DELETE_RECORD", "ABSENT"),
    "REDACT_RECORD": ("DELETE_RECORD", "ABSENT"), "DISABLE_DATA_FLOW": ("BLOCK_RESTORE", "BLOCKED"),
    "BLOCK_RESTORE": ("DISABLE_DATA_FLOW", "DISABLED"),
    "VERIFY_RECORD_ABSENT": ("VERIFY_RECORD_REDACTED", "REDACTED"),
    "VERIFY_RECORD_PRESENT": ("VERIFY_RECORD_ABSENT", "ABSENT"),
    "VERIFY_RECORD_ANONYMIZED": ("VERIFY_RECORD_ABSENT", "ABSENT"),
    "VERIFY_RECORD_REDACTED": ("VERIFY_RECORD_ABSENT", "ABSENT"),
    "REPORT_REMAINING_RECORD": ("REPORT_EXPOSED_PII", "PRESENT"), "REPORT_EXPOSED_PII": ("REPORT_REMAINING_RECORD", "PRESENT"),
}
KINDS = ["wrong_operation", "drop_resource", "flip_status", "foreign_resource", "wrong_evidence", "wrong_finding_type"]


def _renumber(steps):
    return [{**s, "step_no": i} for i, s in enumerate(steps, 1)]


def mutate(result: dict, kind: str, rng: random.Random, foreign_ids: list[str]) -> tuple[dict, dict] | None:
    """Return (mutated_result, target) or None when the mutation does not apply to this row."""
    r = copy.deepcopy(result)
    steps = r["steps"]
    if kind == "flip_status":
        if r["deletion_status"] == "NOT_APPLICABLE":
            return None
        r["deletion_status"] = "INCOMPLETE" if r["deletion_status"] == "COMPLETE" else "COMPLETE"
        return r, {"kind": kind, "status": True}
    if not steps:
        return None
    if kind == "wrong_operation":
        cands = [s for s in steps if s["phase"] in ("REMEDIATION", "VERIFICATION") and s["operation"] in OP_SWAP]
        if not cands:
            return None
        s = rng.choice(cands)
        s["operation"], s["state"] = OP_SWAP[s["operation"]]
        return r, {"kind": kind, "step_no": s["step_no"]}
    if kind == "wrong_finding_type":
        cands = [s for s in steps if s["operation"] in ("REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII")]
        if not cands:
            return None
        s = rng.choice(cands)
        s["operation"], s["state"] = OP_SWAP[s["operation"]]
        return r, {"kind": kind, "step_no": s["step_no"]}
    if kind == "drop_resource":
        rids = sorted({s["resource_id"] for s in steps})
        if len(rids) < 2:
            return None
        gone = rng.choice(rids)
        r["steps"] = _renumber([s for s in steps if s["resource_id"] != gone])
        return r, {"kind": kind, "resource_id": gone}
    if kind == "foreign_resource":
        s = rng.choice(steps)
        others = [f for f in foreign_ids if f.split("_")[0] == s["resource_id"].split("_")[0] and f != s["resource_id"]]
        if not others:
            return None
        s["resource_id"] = rng.choice(others)
        return r, {"kind": kind, "step_no": s["step_no"]}
    if kind == "wrong_evidence":
        cited = [s for s in steps if s["evidence_id"]]
        if not cited:
            return None
        s = rng.choice(cited)
        s["evidence_id"] = rng.choice([e for e in ("evidence_03", "evidence_50", "evidence_88", "evidence_41")
                                       if e != s["evidence_id"]])
        return r, {"kind": kind, "step_no": s["step_no"]}
    raise ValueError(kind)


def make_mutants(results: list[dict], per_case: int = 2, seed: int = 7) -> list[tuple[dict, dict]]:
    """Deterministic set of mutants: each case gets `per_case` different kinds, rotating through KINDS."""
    rng = random.Random(seed)
    foreign = sorted({s["resource_id"] for r in results for s in r["steps"]})
    out, k = [], 0
    for r in results:
        made = 0
        for attempt in range(len(KINDS)):
            kind = KINDS[(k + attempt) % len(KINDS)]
            m = mutate(r, kind, rng, foreign)
            if m:
                out.append((m[0], {**m[1], "case_id": r["case_id"]}))
                made += 1
                if made == per_case:
                    break
        k += per_case
    return out


def caught(judgement: dict, target: dict) -> bool:
    """Did the judge flag the planted error?"""
    if target.get("status"):
        return judgement["status_verdict"] == "disagree"
    if "resource_id" in target:
        return any(m["resource_id"] == target["resource_id"] for m in judgement["missing_steps"]) \
            or judgement["case_verdict"] == "INVALID"
    return any(v["step_no"] == target["step_no"] and v["verdict"] == "disagree" for v in judgement["steps"])


assert set(STEP_FIELDS) >= {"step_no", "resource_id", "operation", "state", "evidence_id"}
