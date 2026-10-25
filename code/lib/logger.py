"""Logging: console progress, the per-call token usage log, JSONL artefacts, and run summaries."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import Counter

from lib.config import CACHE_DIR, RUN_LOG_PATH, USAGE_LOG_PATH

_USAGE_LOCK = threading.Lock()


def info(stage: str, message: str) -> None:
    print(f"[{stage}] {message}")


def log_event(level: str, stage: str, message: str, **details) -> None:
    """Append a run event to cache/run_log.jsonl (run start/end, errors, refusals to write output)."""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "stage": stage,
             "message": message, **details}
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(RUN_LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def error_banner(stage: str, title: str, lines: list[str]) -> None:
    """Print an unmissable error block to stderr and record it in the run log."""
    bar = "!" * 78
    print(f"\n{bar}\n!! ERROR [{stage}] {title}", file=sys.stderr)
    for line in lines:
        print(f"!!   {line}", file=sys.stderr)
    print(bar, file=sys.stderr)
    log_event("ERROR", stage, title, details=lines)


def log_usage(entry: dict) -> None:
    """Append one model call's token usage to cache/usage_log.jsonl (feeds evaluation/usage_report.md)."""
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
    with _USAGE_LOCK:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(USAGE_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")


def write_jsonl(path: str, records: list[dict]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _counts(counter: Counter) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(counter.items())) or "-"


def print_run_summary(results: list[dict], elapsed: float, cost: dict) -> None:
    """One block summarising every stage of a run. `results` are the per-case records from the pipeline."""
    print(f"\n== Run summary ({len(results)} cases, {elapsed:.1f}s)")
    print("  stage 1 intent:   " + _counts(Counter(r["intent"]["intent"] for r in results)))
    print("  stage 1 source:   " + _counts(Counter(r["intent"]["source"] for r in results)))
    intent_flags = Counter(f for r in results for f in r["intent"]["flags"])
    if intent_flags:
        print("  stage 1 flags:    " + _counts(intent_flags))
    if cost["calls"]:
        print(f"  model calls: {cost['calls']}  tokens in/out: {cost['input_tokens']}/{cost['output_tokens']}  "
              f"est. cost: ${cost['usd']:.4f}")
    idents = [r["identity"] for r in results if r.get("identity")]
    if idents:
        print("  stage 2 identity: " + _counts(Counter(i["request_email_status"] for i in idents)))
        id_flags = Counter(f.split(":")[0] for i in idents for f in i["flags"])
        if id_flags:
            print("  stage 2 flags:    " + _counts(id_flags))
    print("  stage 4 report:   " + _counts(Counter(r["report_source"] for r in results)))
    invalid = [r for r in results if r["validation_errors"]]
    print(f"  stage 5 valid:    {len(results) - len(invalid)}/{len(results)}")
    for r in invalid[:10]:
        print(f"      {r['case_id']}: {r['validation_errors'][:3]}")
    print("  stage 6 status:   " + _counts(Counter(r["deletion_status"] for r in results)))
    print(f"  stage 6 steps:    {sum(len(r['steps']) for r in results)} total")
    gen_flags = Counter(f for r in results for f in r["case_flags"])
    if gen_flags:
        print("  case flags:       " + _counts(gen_flags))


def print_evaluation(metrics: dict) -> None:
    print("\n== Evaluation against sample_output.csv")
    for key in ("cases", "status_accuracy", "step_precision", "step_recall", "step_f1", "field_accuracy",
                "sequence_accuracy"):
        value = metrics[key]
        print(f"  {key:<18} {value:.3f}" if isinstance(value, float) else f"  {key:<18} {value}")
    if metrics["status_confusion"]:
        print("  status confusion (gold -> predicted): " + ", ".join(
            f"{k}={v}" for k, v in sorted(metrics["status_confusion"].items())))
