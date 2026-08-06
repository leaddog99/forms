"""Strip manufactured zeros out of `_scoring` on rows already in the DB.

ScoringMetadata used to declare every numeric scoring field `= 0.0`, so every
extract that passed through RecipeModel carried a full block of zeros whether or
not anything measured them (see docs/recipe-scoring-design.md §11b.2). The
contract is fixed as of 2026-08-06, but that only stops NEW rows — the ones
already saved still read as "measured, and it's nothing", which puts them at the
floor of every ranking dimension instead of out of the ranking.

Applies `save_recipe_api._sanitize_scoring`, the same function the save boundary
uses, rather than re-implementing the rule. It drops only values that are
exactly 0.0 in the Moz/cohort fields and records a `scoringNote` saying why —
strings, recipeScore and any genuinely measured number are untouched.

NOT a re-score: it removes false values, it does not invent true ones. Run
`backfill_url_scoring.py --zeros-only` FIRST if you want Moz re-measured, since
that recovers real numbers and this cannot.

Safe because every reader tolerates absence — audited 2026-08-06: setScoreChip
renders "—" for null, blend._power returns None unless both operands are
numbers, enrich_recipe guards with `is not None`, chapters.py filters
IS NOT NULL, dishes.py wraps in COALESCE.

Usage:
    python -m scripts.backfill_strip_unmeasured_zeros                  # dry run
    python -m scripts.backfill_strip_unmeasured_zeros --commit
    python -m scripts.backfill_strip_unmeasured_zeros --table master_recipes
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("BCC_SKIP_JOB_RESET", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from save_recipe_api import _MOZ_SCORING_KEYS, _sanitize_scoring  # noqa: E402

DB_PATH = str(Path(__file__).resolve().parents[1] / "recipes.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write (default: dry run)")
    ap.add_argument("--table", default="recipes", choices=("recipes", "master_recipes"))
    ap.add_argument("--db", default=DB_PATH)
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    print(f"[STRIP] {a.table} in {a.db}")
    print(f"[STRIP] Mode: {'COMMIT' if a.commit else 'DRY-RUN (no writes)'}\n")

    dropped_counts: Counter = Counter()
    changed = scanned = 0
    for row in con.execute(f"SELECT id, data FROM {a.table}").fetchall():
        try:
            d = json.loads(row["data"])
        except Exception:
            continue
        sc = d.get("_scoring")
        if not isinstance(sc, dict):
            continue
        scanned += 1
        before = set(sc)
        url = (d.get("_source") or {}).get("originalUrl") or ""
        _sanitize_scoring(d, url)
        after = set(d.get("_scoring") or {})
        gone = before - after
        if not gone:
            continue
        changed += 1
        dropped_counts.update(gone)
        if a.commit:
            con.execute(f"UPDATE {a.table} SET data = ? WHERE id = ?",
                        (json.dumps(d, indent=2), row["id"]))
    if a.commit:
        con.commit()

    print(f"\n[STRIP] rows with _scoring : {scanned:,}")
    print(f"[STRIP] rows changed       : {changed:,}")
    if dropped_counts:
        print("[STRIP] fields dropped:")
        for k, n in dropped_counts.most_common():
            mark = "  (Moz measurement)" if k in _MOZ_SCORING_KEYS[:3] else ""
            print(f"          {k:<26}{n:>6}{mark}")
    if not a.commit and changed:
        print("\n[STRIP] dry run — re-run with --commit to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
