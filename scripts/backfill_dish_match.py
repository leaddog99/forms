"""Re-score which canonical dish each unclaimed master recipe is.

Thin CLI over `input.pipeline.dish_match.rematch_unclaimed` — the logic lives
there because three callers share it (the save path, this script, and the
nightly `dish_rematch` job) and they were already drifting.

WRITE ONLY ON CHANGE, ALWAYS. A row is re-scored every run but written only if
the verdict actually moved. Re-stamping an unchanged row would dirty thousands
of JSON blobs a night, inflate the WAL, and make each night's recipes.sql
backup diff the size of the table — for no information.

FREE TO RUN. Every row already has its embedding stored and the dish index is
local sqlite-vec, so this is a KNN query per row: ~14s for 3,200 rows, no
embedding call, nothing billable.

Usage:
  python -m scripts.backfill_dish_match --dry-run        # report, write nothing
  python -m scripts.backfill_dish_match                  # re-score everything
  python -m scripts.backfill_dish_match --only-unmatched # first-pass backfill
"""
from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The Windows console is cp1252 and recipe names are not. A title it cannot
# encode used to raise inside the progress line and take the row's write with
# it — 16 rows were counted as matched and never written.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from input.pipeline import dish_match                    # noqa: E402

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recipes.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows")
    ap.add_argument("--only-unmatched", action="store_true",
                    help="only rows that have never been matched")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout = 30000")

    summary = dish_match.rematch_unclaimed(
        conn, db_path=args.db, limit=args.limit, dry_run=args.dry_run,
        only_unmatched=args.only_unmatched,
    )

    print(f"[REMATCH] threshold (L2)    {summary['threshold']}")
    print(f"[REMATCH] mode              {'DRY RUN (no writes)' if args.dry_run else 'APPLY'}")
    print(f"[REMATCH] scanned           {summary['scanned']}")
    print(f"[REMATCH] confident         {summary['confident']}")
    print(f"[REMATCH] CHANGED (written) {summary['changed']}")
    print(f"[REMATCH] unchanged (skip)  {summary['unchanged']}")
    print(f"[REMATCH] failed            {summary['failed']}")

    if summary["moves"]:
        tally = collections.Counter((old, new) for _rid, old, new in summary["moves"])
        print()
        print("[REMATCH] moves (was -> now):")
        for (old, new), k in sorted(tally.items(), key=lambda kv: -kv[1])[:40]:
            print(f"    {k:5}  {str(old):<34} -> {new}")
        if len(tally) > 40:
            print(f"    ... {len(tally) - 40} more distinct moves")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
