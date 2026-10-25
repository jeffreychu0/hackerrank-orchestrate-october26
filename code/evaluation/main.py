"""Independent evaluation: an OpenAI single-call judge reviews the latest pipeline run, case by case.

    python code/main.py --samples              # 1. run the pipeline (writes cache/case_results.jsonl)
    python code/evaluation/main.py             # 2. judge every case in that run
    python code/evaluation/main.py --cases case_12,case_30 --model gpt-6.1-sol --effort low

On sample runs the judge is also scored against sample_output.csv, which shows how far its
verdicts on the unlabelled evaluation rows can be trusted. Needs OPENAI_API_KEY (environment or
.env). Verdicts are cached; token usage goes to cache/usage_log.jsonl.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # code/

from evaluation import judge  # noqa: E402
from evaluation.mutations import caught, make_mutants  # noqa: E402
from lib import api, config, logger  # noqa: E402
from stages.output import case_sort_key  # noqa: E402
from stages.retrieval import connect, retrieve  # noqa: E402
from stages.steps import STEP_FIELDS  # noqa: E402


def load_results(path: str, only: set[str] | None) -> list[dict]:
    if not os.path.exists(path):
        raise SystemExit(f"no {path}: run `python code/main.py` (or --samples) first")
    with open(path, encoding="utf-8") as fh:
        results = [json.loads(line) for line in fh]
    if only:
        results = [r for r in results if r["case_id"] in only]
    return sorted(results, key=lambda r: case_sort_key(r["case_id"]))


def build_payloads(items: list[tuple[str, dict]]) -> dict[str, dict]:
    """items: (key, result). The key is the case_id, or case_id#n:kind for a mutant."""
    db = connect(config.INDEX_PATH)
    payloads, bundles = {}, {}
    for key, r in items:
        cid = r["case_id"]
        case = dict(db.execute("SELECT * FROM requests WHERE case_id=?", (cid,)).fetchone())
        if cid not in bundles:
            bundles[cid] = retrieve(db, cid) if r["intent"]["intent"] != "NOT_DELETION" else None
        payloads[key] = judge.case_payload(r, case, bundles[cid])
    db.close()
    return payloads


def calibrate(judged: dict[str, dict], results: dict[str, dict]) -> dict | None:
    """Score the judge against gold on sample cases: does it disagree exactly where we are wrong?"""
    with open(config.SAMPLE_GOLD, encoding="utf-8", newline="") as fh:
        gold = {r["case_id"]: r for r in csv.DictReader(fh)}
    scored = [cid for cid in judged if cid in gold and judged[cid]["judgement"]]
    if not scored:
        return None
    key = lambda s: tuple(s[k] for k in STEP_FIELDS if k != "step_no")  # noqa: E731
    tp = fp = fn = tn = unsure = status_ok = 0
    missing_hits = missing_claims = gold_missing = 0
    for cid in scored:
        g_steps = json.loads(gold[cid]["steps_json"])
        g_keys = Counter(map(key, g_steps))
        verdicts = {v["step_no"]: v["verdict"] for v in judged[cid]["judgement"]["steps"]}
        for s in results[cid]["steps"]:
            wrong = g_keys[key(s)] == 0
            if g_keys[key(s)]:
                g_keys[key(s)] -= 1
            v = verdicts.get(s["step_no"], "unsure")
            if v == "unsure":
                unsure += 1
            elif v == "disagree":
                tp, fp = tp + wrong, fp + (not wrong)
            else:
                fn, tn = fn + wrong, tn + (not wrong)
        status_ok += judged[cid]["judgement"]["expected_status"] == gold[cid]["deletion_status"]
        # key = (phase, system_id, resource_id, operation, state, evidence_id): index 2 is the resource
        gold_missing_ids = {k[2] for k, n in g_keys.items() if n > 0}          # resource_ids we failed to output
        claimed = {m["resource_id"] for m in judged[cid]["judgement"]["missing_steps"]}
        gold_missing += len(gold_missing_ids)
        missing_claims += len(claimed)
        missing_hits += len(claimed & gold_missing_ids)
    return {
        "cases": len(scored),
        "judge_status_accuracy": status_ok / len(scored),
        "wrong_steps_flagged": f"{tp}/{tp + fn}",
        "disagreements_that_were_right": f"{tp}/{tp + fp}",
        "correct_steps_agreed": f"{tn}/{tn + fp}",
        "unsure_steps": unsure,
        "missing_step_claims_correct": f"{missing_hits}/{missing_claims}",
        "gold_missing_resources_found": f"{missing_hits}/{gold_missing}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", help="comma-separated case_ids (default: every case in the latest run)")
    parser.add_argument("--limit", type=int, help="judge only the first N cases")
    parser.add_argument("--model", default=judge.JUDGE_MODEL)
    parser.add_argument("--effort", default=judge.JUDGE_EFFORT, choices=["none", "minimal", "low", "medium", "high"])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--results", default=config.CASE_RESULTS_PATH, help="case results file to judge")
    parser.add_argument("--mutations", action="store_true",
                        help="judge mutated copies of the (sample) results to measure the judge's catch rate")
    parser.add_argument("--tag", help="suffix for the summary/results files, e.g. samples_sol")
    args = parser.parse_args(argv)
    api.load_dotenv()

    results = load_results(args.results, set(args.cases.split(",")) if args.cases else None)[: args.limit]
    targets = {}
    if args.mutations:
        mutants = make_mutants(results)
        items = [(f"{t['case_id']}#{i}:{t['kind']}", m) for i, (m, t) in enumerate(mutants)]
        targets = {key: t for (key, _m), (_mm, t) in zip(items, mutants)}
    else:
        items = [(r["case_id"], r) for r in results]
    payloads = build_payloads(items)
    cache = judge.load_cache()
    judged, pending = {}, []
    for cid, payload in payloads.items():
        key = judge.cache_key(payload, args.model, args.effort)
        if key in cache:
            judged[cid] = {**cache[key], "source": "cache"}
        else:
            pending.append((cid, payload, key))

    if pending and not os.environ.get("OPENAI_API_KEY"):
        logger.error_banner("evaluate", "OPENAI_API_KEY not set", [f"{len(pending)} cases need the judge."])
        return 2
    if pending:
        from openai import OpenAI
        client = OpenAI()
        logger.info("evaluate", f"judging {len(pending)} cases with {args.model} (effort {args.effort}) ...")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(judge.judge_case, client, p, args.model, args.effort): (cid, key)
                       for cid, p, key in pending}
            try:
                for future in as_completed(futures):
                    cid, key = futures[future]
                    out = {**future.result(), "source": "model"}
                    judged[cid] = out
                    if out["judgement"] is not None:
                        cache[key] = {k: v for k, v in out.items() if k != "source"}
            except judge.JudgeCredentialsError as exc:
                for f in futures:
                    f.cancel()
                logger.error_banner("evaluate", "OpenAI credentials rejected", [str(exc)])
                judge.save_cache(cache)
                return 2
        judge.save_cache(cache)

    # Report.
    by_id = {r["case_id"]: r for r in results}
    ok = {cid: j for cid, j in judged.items() if j["judgement"]}
    failed = sorted((cid for cid, j in judged.items() if not j["judgement"]), key=case_sort_key)
    step_verdicts = Counter(v["verdict"] for j in ok.values() for v in j["judgement"]["steps"])
    fresh = [j for j in judged.values() if j["source"] == "model"]
    tin, tcache, tout = (sum(j[k] for j in fresh) for k in ("input_tokens", "cached_tokens", "output_tokens"))
    summary = {
        "model": args.model, "effort": args.effort, "prompt_version": judge.PROMPT_VERSION,
        "cases_judged": len(ok), "cases_failed": failed,
        "case_verdicts": dict(Counter(j["judgement"]["case_verdict"] for j in ok.values())),
        "status_agreement": sum(j["judgement"]["status_verdict"] == "agree" for j in ok.values()) / max(len(ok), 1),
        "step_verdicts": dict(step_verdicts),
        "step_agreement": step_verdicts["agree"] / max(sum(step_verdicts.values()), 1),
        "missing_step_claims": sum(len(j["judgement"]["missing_steps"]) for j in ok.values()),
        "disputed_cases": sorted((cid for cid, j in ok.items() if j["judgement"]["case_verdict"] != "VALID"),
                                 key=case_sort_key),
        "usage": {"calls": len(fresh), "input_tokens": tin, "cached_input_tokens": tcache, "output_tokens": tout,
                  "est_cost_usd": round(judge.estimate_cost(args.model, tin, tcache, tout), 4)},
        "calibration_vs_gold": None if targets else calibrate(ok, by_id),
    }
    if targets:   # mutation test: how many planted errors did the judge catch, per error type?
        per_kind = {}
        for key, t in targets.items():
            if key not in ok:
                continue
            hit = caught(ok[key]["judgement"], t)
            k = per_kind.setdefault(t["kind"], [0, 0])
            k[0], k[1] = k[0] + hit, k[1] + 1
        summary["mutation_catch_rate"] = {k: f"{c}/{n}" for k, (c, n) in sorted(per_kind.items())}
        total_c, total_n = sum(c for c, _ in per_kind.values()), sum(n for _, n in per_kind.values())
        summary["mutation_catch_overall"] = total_c / max(total_n, 1)
    suffix = f"_{args.tag}" if args.tag else ""
    results_path = config.JUDGE_RESULTS_PATH.replace(".jsonl", f"{suffix}.jsonl")
    summary_path = config.JUDGE_SUMMARY_PATH.replace(".json", f"{suffix}.json")
    logger.write_jsonl(results_path, [{"key": cid, "target": targets.get(cid), **judged[cid]}
                                      for cid in sorted(judged, key=case_sort_key)])
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1, sort_keys=True)

    print(f"\n== Judge summary ({args.model}, effort {args.effort})")
    for k in ("cases_judged", "case_verdicts", "status_agreement", "step_verdicts", "step_agreement",
              "missing_step_claims", "usage"):
        v = summary[k]
        print(f"  {k:<20} {v:.3f}" if isinstance(v, float) else f"  {k:<20} {v}")
    if failed:
        print(f"  failed: {failed[:10]} -> " + "; ".join(sorted({f for c in failed for f in judged[c]['flags']}))[:200])
    for cid in summary["disputed_cases"][:15]:
        j = ok[cid]["judgement"]
        bad = [f"#{v['step_no']} {v['reason']}" for v in j["steps"] if v["verdict"] != "agree"][:2]
        miss = [f"missing {m['resource_id']} {m['operation']}" for m in j["missing_steps"]][:2]
        print(f"  {cid} {j['case_verdict']}: {j['summary'][:140]}")
        for line in bad + miss:
            print(f"      {line[:150]}")
    if summary["calibration_vs_gold"]:
        print("  calibration vs gold: " + json.dumps(summary["calibration_vs_gold"]))
    if targets:
        print(f"  mutation catch rate: {summary['mutation_catch_overall']:.3f}  {summary['mutation_catch_rate']}")
    logger.log_event("INFO", "evaluate", "judge run", **{k: summary[k] for k in ("model", "cases_judged", "usage")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
