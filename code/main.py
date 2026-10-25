"""Ghost Data Still Exists - deletion-audit pipeline entry point.

    python code/main.py                      # all 518 cases -> output.csv at the repository root
    python code/main.py --samples            # the 32 public samples -> cache/sample_predictions.csv + evaluation
    python code/main.py --cases case_33,case_10
    python code/main.py --no-model           # skip Claude calls (cached intents are still used)

Layout (code/):
    main.py        this entry point
    pipeline/      the driver: runs stages 0-7 in order and enforces the output gates
    stages/        evidence_index (0), intent_classifier (1), identity (2), retrieval (3),
                   state_report (4: contract + fallback), validator (5), steps (6), output (7)
    lib/           shared libraries: config (paths), api (Claude), logger
    tools/         standalone utilities: build_index.py, inspect_case.py
    evaluation/    evaluation and usage reports

Credentials: ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) from the environment or an uncommitted
.env file at the repository root. They are never logged.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pipeline  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", action="store_true", help="run the 32 public sample cases and score them")
    parser.add_argument("--cases", help="comma-separated case_ids to run (from either request file)")
    parser.add_argument("--no-model", action="store_true", help="do not call Claude (cached intents are used)")
    parser.add_argument("--workers", type=int, default=8, help="concurrent Claude calls in stage 1")
    parser.add_argument("--verbose", action="store_true", help="print one line per case")
    parser.add_argument("--allow-unclassified", action="store_true",
                        help="continue when Claude could not classify some requests (debugging only; "
                             "output.csv is never written in that case)")
    parser.add_argument("--agent", choices=["flagged", "all", "off"], default="flagged",
                        help="stage 4: run the Strands agent on flagged cases (default), on every deletion case, "
                             "or not at all (deterministic draft reports only)")
    parser.add_argument("--agent-workers", type=int, default=4, help="concurrent agent runs in stage 4")
    args = parser.parse_args(argv)
    try:
        pipeline.run(samples=args.samples, case_ids=args.cases.split(",") if args.cases else None,
                     use_model=not args.no_model, workers=args.workers, verbose=args.verbose,
                     allow_unclassified=args.allow_unclassified, agent_mode=args.agent,
                     agent_workers=args.agent_workers)
    except pipeline.PipelineError as exc:
        print(f"\nRun FAILED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
