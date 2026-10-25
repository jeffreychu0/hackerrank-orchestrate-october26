"""Stage 7: cross-case checks, writing the predictions CSV, and scoring against sample_output.csv."""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict

from stages.steps import STEP_FIELDS, serialize_steps


def case_sort_key(case_id: str):
    prefix, _, num = case_id.rpartition("_")
    return (prefix, int(num)) if num.isdigit() else (case_id, 0)


def cross_case_checks(results: list[dict]) -> dict[str, list[str]]:
    """Flag resources reported for more than one person (a sign of a lookalike or mislink)."""
    owners = defaultdict(set)
    for r in results:
        user = (r.get("identity") or {}).get("user_id")
        for res in r["report"]["resources"] if r.get("report") else []:
            if user:
                owners[res["resource_id"]].add(user)
    flags = defaultdict(list)
    for r in results:
        for res in r["report"]["resources"] if r.get("report") else []:
            if len(owners[res["resource_id"]]) > 1:
                flags[r["case_id"]].append(f"RESOURCE_SHARED_ACROSS_SUBJECTS:{res['resource_id']}")
    return flags


def write_predictions(results: list[dict], path: str) -> None:
    rows = sorted(results, key=lambda r: case_sort_key(r["case_id"]))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        for r in rows:
            writer.writerow([r["case_id"], r["deletion_status"], serialize_steps(r["steps"])])


def _step_key(s: dict) -> tuple:
    return tuple(s[k] for k in STEP_FIELDS if k != "step_no")


def evaluate(results: list[dict], gold_path: str) -> dict:
    """Case status accuracy; step precision/recall/F1 (multiset of steps ignoring step_no);
    field accuracy (position-aligned fields, padded to the longer list); exact sequence accuracy."""
    with open(gold_path, encoding="utf-8", newline="") as fh:
        gold = {r["case_id"]: r for r in csv.DictReader(fh)}
    scored = [r for r in results if r["case_id"] in gold]
    status_ok = tp = n_pred = n_gold = fields_ok = fields_total = seq_ok = 0
    confusion = Counter()
    per_case = {}
    for r in scored:
        g = gold[r["case_id"]]
        g_steps = json.loads(g["steps_json"])
        p_steps = r["steps"]
        status_ok += g["deletion_status"] == r["deletion_status"]
        if g["deletion_status"] != r["deletion_status"]:
            confusion[f"{g['deletion_status']}->{r['deletion_status']}"] += 1
        gc, pc = Counter(map(_step_key, g_steps)), Counter(map(_step_key, p_steps))
        hit = sum((gc & pc).values())
        tp, n_pred, n_gold = tp + hit, n_pred + len(p_steps), n_gold + len(g_steps)
        width = max(len(g_steps), len(p_steps))
        case_fields = 0
        for i in range(width):
            gs = g_steps[i] if i < len(g_steps) else {}
            ps = p_steps[i] if i < len(p_steps) else {}
            case_fields += sum(1 for k in STEP_FIELDS if k in gs and gs.get(k) == ps.get(k))
        fields_ok += case_fields
        fields_total += width * len(STEP_FIELDS)
        exact = [_step_key(s) for s in g_steps] == [_step_key(s) for s in p_steps] and \
            g["deletion_status"] == r["deletion_status"]
        seq_ok += exact
        per_case[r["case_id"]] = {"gold_status": g["deletion_status"], "pred_status": r["deletion_status"],
                                  "matched_steps": hit, "gold_steps": len(g_steps), "pred_steps": len(p_steps),
                                  "exact_sequence": exact}
    precision = tp / n_pred if n_pred else 0.0
    recall = tp / n_gold if n_gold else 0.0
    return {
        "cases": len(scored),
        "status_accuracy": status_ok / len(scored) if scored else 0.0,
        "step_precision": precision,
        "step_recall": recall,
        "step_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "field_accuracy": fields_ok / fields_total if fields_total else 0.0,
        "sequence_accuracy": seq_ok / len(scored) if scored else 0.0,
        "status_confusion": dict(confusion),
        "per_case": per_case,
    }
