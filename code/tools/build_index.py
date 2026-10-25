"""Stage 0: build the evidence index (cache/index.db) from dataset/.

Run once per machine (re-run after the dataset changes; unchanged inputs are skipped):

    python code/tools/build_index.py            # build or confirm up to date
    python code/tools/build_index.py --force    # rebuild unconditionally

(code/main.py also builds the index automatically when it is missing or stale.)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # code/

from lib import config  # noqa: E402
from stages.evidence_index import build  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", default=config.DATASET_DIR, help="dataset directory")
    parser.add_argument("--out", default=config.INDEX_PATH, help="output SQLite path")
    parser.add_argument("--media", default=config.MEDIA_EXTRACTIONS_PATH,
                        help="cached vision extraction for image evidence (optional)")
    parser.add_argument("--force", action="store_true", help="rebuild even if inputs are unchanged")
    args = parser.parse_args(argv)
    if not os.path.isfile(os.path.join(args.dataset, "evidence_files.csv")):
        parser.error(f"no evidence_files.csv under {args.dataset}")
    build(args.dataset, args.out, args.media, force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
