"""Print the retrieval bundle for one or more cases from cache/index.db.

    python code/tools/inspect_case.py case_30
    python code/tools/inspect_case.py case_27 case_05 --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # code/

from lib import config  # noqa: E402
from stages.retrieval import connect, retrieve  # noqa: E402


def show(bundle: dict) -> None:
    s = bundle["subject"]
    print(f"== {bundle['case_id']}  email={bundle['email']}  audit_at={bundle['audit_at']}  "
          f"reported={bundle['reported_status']}")
    print(f"   request: {bundle['request_text'][:110]}")
    if not s:
        print("   subject: NOT RESOLVED")
        return
    print(f"   subject: {s['user_id']} (request email is {'current' if s['request_email_is_current'] else 'historical'}); "
          f"{len(s['identifiers'])} identifiers")
    print(f"   {'resource':<20}{'system':<22}{'type':<20}{'state@audit':<14}{'cite':<14}{'link':<28}flags")
    for r in bundle["resources"]:
        print(f"   {r['resource_id']:<20}{(r['system_id'] or '-'):<22}{(r['record_type'] or '?'):<20}"
              f"{(r['latest_state'] or '-'):<14}{','.join(r['citable']) or '-':<14}{','.join(r['link_basis']):<28}"
              f"{','.join(r['flags'])}")
    for ex in bundle.get("excluded", []):
        print(f"   excluded {ex['resource_id']}: {ex['reason']}")
    if bundle["case_flags"]:
        print(f"   case flags: {', '.join(bundle['case_flags'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("case_ids", nargs="+")
    parser.add_argument("--db", default=config.INDEX_PATH)
    parser.add_argument("--json", action="store_true", help="print the full bundle as JSON")
    args = parser.parse_args()
    if not os.path.exists(args.db):
        parser.error(f"{args.db} not found; run: python code/tools/build_index.py")
    db = connect(args.db)
    for case_id in args.case_ids:
        bundle = retrieve(db, case_id)
        print(json.dumps(bundle, indent=2, ensure_ascii=False) if args.json else "", end="")
        if not args.json:
            show(bundle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
