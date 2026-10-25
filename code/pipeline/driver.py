"""The driver: runs every stage in order for a set of cases (docs/pipeline.md section 1).

  0 build/confirm the evidence index      evidence_index.build
  1 classify intent                        intent_classifier.classify_intents
  2 resolve identity                       identity.resolve_identities
  3 retrieve the case bundle               retrieval.retrieve
  4 adjudicate -> state report             adjudicator (Strands agent on flagged cases; draft otherwise)
  5 validate the report and the row        validator
  6 generate status + steps                steps.generate_steps
  7 cross-case checks, write, evaluate     output
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from dataclasses import asdict

from lib import api, config, logger
from stages.adjudicator import adjudicate_cases, adjudication_reasons
from stages.evidence_index import build as build_index
from stages.identity import resolve_identities
from stages.intent_classifier import classify_intents
from stages.output import case_sort_key, cross_case_checks, evaluate, write_predictions
from stages.retrieval import connect, retrieve
from stages.state_report import build_fallback_report
from stages.steps import generate_steps
from stages.validator import validate_report, validate_row


class PipelineError(RuntimeError):
    """A condition that makes the run's answers untrustworthy; main.py exits non-zero on it."""


def _check_intents(intents: dict, allow_unclassified: bool) -> None:
    """Stage 1 gate: an unclassified request must never silently become a deletion audit."""
    from collections import Counter
    unclassified = {cid: r for cid, r in intents.items() if r.intent == "UNCLASSIFIED"}
    if not unclassified:
        return
    reasons = Counter(f for r in unclassified.values() for f in (r.flags or ["NO_REASON"]))
    ids = sorted(unclassified, key=case_sort_key)
    lines = [f"{len(unclassified)} of {len(intents)} requests could not be classified by Claude.",
             "Reasons: " + ", ".join(f"{k} x{v}" for k, v in reasons.most_common()),
             "Cases: " + ", ".join(ids[:15]) + (f" ... (+{len(ids) - 15} more)" if len(ids) > 15 else ""),
             "Fix: set ANTHROPIC_API_KEY (or .env) and re-run; successful verdicts are cached."]
    if allow_unclassified:
        lines.append("--allow-unclassified given: continuing, treating them as deletions; output.csv will NOT be written.")
        logger.error_banner("stage 1", "Unclassified requests (continuing by request)", lines)
        return
    logger.error_banner("stage 1", "Unclassified requests - run stopped", lines)
    raise PipelineError(f"{len(unclassified)} unclassified requests; see cache/run_log.jsonl")


def _output_gate(results: list[dict], expected_rows: int) -> list[str]:
    """Reasons the submission file must not be written (empty list = safe to write output.csv)."""
    problems = []
    unclassified = [r["case_id"] for r in results if r["intent"]["intent"] == "UNCLASSIFIED"]
    if unclassified:
        problems.append(f"{len(unclassified)} requests are unclassified (treated as deletions)")
    invalid = [r["case_id"] for r in results if r["validation_errors"]]
    if invalid:
        problems.append(f"{len(invalid)} cases failed stage-5 validation: {', '.join(invalid[:10])}")
    ids = [r["case_id"] for r in results]
    if len(ids) != expected_rows or len(set(ids)) != len(ids):
        problems.append(f"{len(set(ids))} unique rows, expected {expected_rows}")
    return problems


def read_cases(path: str, only: set[str] | None = None) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if only:
        rows = [r for r in rows if r["case_id"] in only]
    return sorted(rows, key=lambda r: case_sort_key(r["case_id"]))


def select_cases(samples: bool, case_ids: list[str] | None) -> tuple[list[dict], str]:
    """Return (cases, mode) where mode is full | samples | partial (decides where predictions go)."""
    if case_ids:
        only = set(case_ids)
        cases = read_cases(config.EVAL_REQUESTS, only) + read_cases(config.SAMPLE_REQUESTS, only)
        return sorted(cases, key=lambda r: case_sort_key(r["case_id"])), "partial"
    if samples:
        return read_cases(config.SAMPLE_REQUESTS), "samples"
    return read_cases(config.EVAL_REQUESTS), "full"


def run(samples: bool = False, case_ids: list[str] | None = None, use_model: bool = True, workers: int = 8,
        verbose: bool = False, allow_unclassified: bool = False, agent_mode: str = "flagged",
        agent_workers: int = 4) -> dict:
    started = time.perf_counter()
    api.load_dotenv()

    # Stage 0: rebuilt only when the dataset, media extraction, or builder code changed.
    build_index(config.DATASET_DIR, config.INDEX_PATH, config.MEDIA_EXTRACTIONS_PATH, log=lambda *_: None)
    cases, mode = select_cases(samples, case_ids)
    if not cases:
        raise PipelineError("no matching cases")
    logger.log_event("INFO", "run", "start", mode=mode, cases=len(cases), use_model=use_model,
                     allow_unclassified=allow_unclassified)

    # Stage 1 (stops the run if any request is unclassified, unless explicitly allowed)
    intents = classify_intents(cases, use_model=use_model, workers=workers)
    _check_intents(intents, allow_unclassified)

    # Stage 2 (deletion requests only; unclassified cases are treated as deletions and flagged)
    plain_db = sqlite3.connect(f"file:{config.INDEX_PATH}?mode=ro", uri=True)
    to_resolve = [c for c in cases if intents[c["case_id"]].intent != "NOT_DELETION"]
    identities = resolve_identities(plain_db, to_resolve)
    plain_db.close()

    # Stage 3 + deterministic draft report for every case.
    db = connect(config.INDEX_PATH)
    rules = [dict(r) for r in db.execute("SELECT * FROM deletion_rules")]
    work = []
    for case in cases:
        cid = case["case_id"]
        intent = intents[cid]
        verdict = "NOT_DELETION" if intent.intent == "NOT_DELETION" else "DELETION"
        case_flags = ["INTENT_UNCLASSIFIED_TREATED_AS_DELETION"] if intent.intent == "UNCLASSIFIED" else []
        ident = identities.get(cid)
        bundle = None
        if verdict == "DELETION":
            if ident and ident.user_id:
                bundle = retrieve(db, cid, subject=ident.as_subject())
                case_flags += bundle["case_flags"]
            else:
                bundle = {"case_id": cid, "audit_at": case["audit_at"], "resources": [], "excluded": []}
                case_flags.append("NO_SUBJECT")
        draft = build_fallback_report(bundle, verdict, rules) if bundle else \
            {"case_id": cid, "intent": verdict, "source": "fallback", "resources": [], "excluded": []}
        reasons = adjudication_reasons(bundle, draft) if bundle and agent_mode == "flagged" else \
            (["full adjudication requested"] if bundle and agent_mode == "all" and verdict == "DELETION" else [])
        work.append({"case": case, "intent": intent, "identity": ident, "bundle": bundle, "draft": draft,
                     "reasons": reasons, "flags": case_flags})
    db.close()

    # Stage 4: the Strands agent resolves flagged cases; everything else keeps the draft.
    flagged = [w for w in work if w["reasons"]]
    agent_out = adjudicate_cases(flagged, rules, use_model=use_model, workers=agent_workers) if flagged else {}
    unavailable = [cid for cid, m in agent_out.items() if m["report"] is None]
    if unavailable:
        reasons = sorted({f for m in agent_out.values() if m["report"] is None for f in m["flags"]})
        logger.error_banner("stage 4", "Agent did not adjudicate some flagged cases (draft report used)", [
            f"{len(unavailable)} of {len(flagged)} flagged cases: {', '.join(sorted(unavailable, key=case_sort_key)[:15])}",
            "Reasons: " + "; ".join(r[:120] for r in reasons[:6])])

    # Stages 5-6 for every case.
    results = []
    for w in work:
        cid, intent, bundle = w["case"]["case_id"], w["intent"], w["bundle"]
        meta = agent_out.get(cid)
        report = meta["report"] if meta and meta["report"] else w["draft"]
        case_flags = w["flags"] + ([f"AGENT:{f}" for f in meta["flags"]] if meta else [])
        errors = validate_report(report, bundle, rules) if bundle else []                 # stage 5
        status, steps, gen_flags = generate_steps(report)                                  # stage 6
        errors += validate_row(status, steps)
        results.append({"case_id": cid, "audit_at": w["case"]["audit_at"], "intent": asdict(intent),
                        "identity": asdict(w["identity"]) if w["identity"] else None, "report": report,
                        "report_source": report["source"] + (" (cached)" if meta and meta.get("source") == "cache" else ""),
                        "adjudication_reasons": w["reasons"],
                        "agent": {k: v for k, v in meta.items() if k != "report"} if meta else None,
                        "validation_errors": errors, "deletion_status": status, "steps": steps,
                        "case_flags": case_flags + gen_flags})
        if verbose:
            print(f"  {cid:<9} {intent.intent:<13} {status:<15} steps={len(steps):<3} {report['source']:<9} "
                  f"{','.join(case_flags + gen_flags + errors)[:110]}")

    # Stage 7
    for cid, flags in cross_case_checks(results).items():
        next(r for r in results if r["case_id"] == cid)["case_flags"].extend(flags)
    out_path = {"full": config.OUTPUT_PATH, "samples": config.SAMPLE_PREDICTIONS_PATH,
                "partial": config.PARTIAL_PREDICTIONS_PATH}[mode]
    problems = _output_gate(results, expected_rows=len(cases))
    if problems and mode == "full":
        # Never let an untrustworthy run produce the submission file; keep the rows for inspection.
        out_path = config.UNVERIFIED_OUTPUT_PATH
        logger.error_banner("stage 7", "output.csv NOT written", problems + [
            f"Rows were written to {os.path.relpath(out_path, config.REPO_ROOT)} for inspection only."])
    elif problems:
        logger.error_banner("stage 7", "Predictions have problems", problems)
    write_predictions(results, out_path)
    logger.write_jsonl(config.CASE_RESULTS_PATH, results)

    # Cases with the same request text share one IntentResult, i.e. one model call.
    unique_calls = {id(r): r for r in intents.values() if r.source == "model"}.values()
    agent_runs = [m for m in agent_out.values() if m.get("source") == "model"]
    tin = sum(c.input_tokens for c in unique_calls) + sum(m["input_tokens"] for m in agent_runs)
    tout = sum(c.output_tokens for c in unique_calls) + sum(m["output_tokens"] for m in agent_runs)
    cost = {"calls": len(unique_calls) + len(agent_runs), "input_tokens": tin, "output_tokens": tout,
            "usd": api.estimate_cost(api.DEFAULT_MODEL, tin, tout),
            "intent_calls": len(unique_calls), "agent_runs": len(agent_runs)}
    logger.print_run_summary(results, time.perf_counter() - started, cost)
    print(f"  wrote {os.path.relpath(out_path, config.REPO_ROOT)} ({len(results)} rows)")

    metrics = None
    if mode in ("samples", "partial") and any(r["case_id"] in _sample_ids() for r in results):
        metrics = evaluate(results, config.SAMPLE_GOLD)
        logger.print_evaluation(metrics)
        with open(config.SAMPLE_EVAL_PATH, "w", encoding="utf-8") as fh:
            json.dump(metrics, fh, indent=1, sort_keys=True)
    logger.log_event("ERROR" if problems else "INFO", "run", "end", mode=mode, cases=len(results),
                     output=os.path.relpath(out_path, config.REPO_ROOT), problems=problems,
                     statuses={s: sum(r["deletion_status"] == s for r in results)
                               for s in ("COMPLETE", "INCOMPLETE", "NOT_APPLICABLE")},
                     model_calls=cost["calls"], input_tokens=tin, output_tokens=tout)
    if problems and mode == "full":
        raise PipelineError("submission gate failed; output.csv not written (see cache/run_log.jsonl)")
    return {"results": results, "metrics": metrics, "output_path": out_path, "problems": problems}


def _sample_ids() -> set[str]:
    with open(config.SAMPLE_GOLD, encoding="utf-8", newline="") as fh:
        return {r["case_id"] for r in csv.DictReader(fh)}
