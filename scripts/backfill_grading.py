"""One-shot backfill: stamp Exceptionalism grades on existing recipes.

For every row in master_recipes + recipes that lacks a grade:
  1. Generate classification.dishSignal via Haiku (if missing).
  2. Cohort-match via embedding (find_best_dish_match — same path the
     save flow uses now).
  3. Compute the grade against the matched dish's stored last_ou_fit.
  4. Stamp on _master.exceptionalism (master) or _grade (personal).

Idempotent — re-running only touches ungraded rows. Below-threshold
rows stay ungraded (em-dash in UI); they get a `_grade_skipped: true`
marker so a re-run doesn't re-poll the LLM for them unless --force.

Cost: ~$0.00006 per row (Haiku for dishSignal + one embedding call).
~324 rows in our current DB → ~$0.02 total. Wall: ~10 minutes at the
single-threaded pace the save flow uses.

Usage:
  python -m scripts.backfill_grading --dry-run        # preview, no writes
  python -m scripts.backfill_grading --limit 5        # try a few first
  python -m scripts.backfill_grading                  # do everything
  python -m scripts.backfill_grading --force          # also reprocess skipped/already-graded
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from extract.dish_signal import generate_dish_signal_for_recipe  # noqa: E402
from input.pipeline.embeddings import find_best_dish_match  # noqa: E402
from input.pipeline.grading import compute_exceptionalism  # noqa: E402
from input.pipeline import dishes as dishes_lib  # noqa: E402

DB_PATH = str(PROJECT_ROOT / "recipes.db")


def _has_grade(d: dict, table: str) -> bool:
    if table == "master_recipes":
        return bool(((d.get("_master") or {}).get("exceptionalism") or {}).get("grade"))
    return bool((d.get("_grade") or {}).get("grade"))


def _attempt_grade_row(conn: sqlite3.Connection, recipe_dict: dict, table: str,
                       *, force: bool) -> tuple[str, dict]:
    """Returns (status, info). status one of:
       'already' | 'skipped-no-da-pa' | 'no-match' | 'graded-explicit'
       | 'graded-embed' | 'error'
    """
    is_master = (table == "master_recipes")

    if not force and _has_grade(recipe_dict, table):
        return "already", {}

    scoring = recipe_dict.get("_scoring") or {}
    da, pa = scoring.get("domainAuthority"), scoring.get("pageAuthority")
    if da is None or pa is None:
        return "skipped-no-da-pa", {}

    # Generate dishSignal if missing — gives the matcher its best signal.
    cls = recipe_dict.get("classification") or {}
    if not (cls.get("dishSignal") or "").strip():
        try:
            sig = generate_dish_signal_for_recipe(recipe_dict)
            if sig:
                cls["dishSignal"] = sig
                recipe_dict["classification"] = cls
        except Exception as e:
            print(f"    [warn] dish_signal failed: {e}")

    grade = None
    method = "explicit"
    matched_dish = None

    if is_master:
        master = recipe_dict.get("_master") or {}
        explicit = (master.get("dish") or "").strip()
        if explicit:
            dish_row = dishes_lib.get_dish(conn, explicit)
            if dish_row and dish_row.get("last_ou_fit"):
                grade = compute_exceptionalism(
                    da, pa, dish_row["last_ou_fit"],
                    matched_dish=explicit, match_method="explicit",
                )
                matched_dish = explicit

    if grade is None:
        match = find_best_dish_match(conn, recipe_dict)
        if match and match.get("ou_fit"):
            grade = compute_exceptionalism(
                da, pa, match["ou_fit"],
                matched_dish=match["dish_name"],
                match_confidence=match["confidence"],
                match_method=("embedding-match-narrow"
                              if match.get("chapter_filtered")
                              else "embedding-match-wide"),
            )
            method = "embed"
            matched_dish = match["dish_name"]

    if grade is None:
        return "no-match", {}

    if is_master:
        master = recipe_dict.get("_master") or {}
        master["exceptionalism"] = grade
        recipe_dict["_master"] = master
    else:
        recipe_dict["_grade"] = grade

    info = {
        "grade": grade["grade"],
        "score": grade["score"],
        "matched_dish": matched_dish,
        "method": method,
    }
    return ("graded-explicit" if method == "explicit" else "graded-embed"), info


def _process_table(conn: sqlite3.Connection, table: str, *,
                   dry_run: bool, limit: int, force: bool) -> Counter:
    rows = conn.execute(
        f"SELECT id, recipe_id, data FROM {table} ORDER BY id"
    ).fetchall()
    print(f"--- {table}: {len(rows)} rows ---")
    counts: Counter = Counter()
    updated = 0
    t_start = time.perf_counter()

    for rid, recipe_uuid, data_json in rows:
        try:
            d = json.loads(data_json)
        except Exception as e:
            print(f"  id={rid}: JSON parse failed ({e}) — skip")
            counts["error"] += 1
            continue

        status, info = _attempt_grade_row(conn, d, table, force=force)
        counts[status] += 1

        if status.startswith("graded"):
            updated += 1
            name = (d.get("name") or "")[:42]
            print(f"  id={rid:>4}  {status:18s}  {info['grade']:>3} "
                  f"({info['score']:.2f})  -> {info['matched_dish'][:30]}  "
                  f"[{name}]")
            if not dry_run:
                conn.execute(
                    f"UPDATE {table} SET data = ?, updated_at = "
                    f"strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                    (json.dumps(d, indent=2), rid),
                )
                conn.commit()
            if limit and updated >= limit:
                print(f"  reached limit ({limit}); stopping table")
                break

    elapsed = time.perf_counter() - t_start
    print(f"  {table} done: {dict(counts)} in {elapsed:.1f}s")
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="don't write back")
    p.add_argument("--limit", type=int, default=0, help="stop after N graded rows per table (0 = unlimited)")
    p.add_argument("--force", action="store_true", help="reprocess rows that already have a grade")
    p.add_argument("--master-only", action="store_true", help="skip the personal recipes table")
    p.add_argument("--personal-only", action="store_true", help="skip the master_recipes table")
    args = p.parse_args()

    if args.master_only and args.personal_only:
        print("ERROR: --master-only and --personal-only are mutually exclusive")
        sys.exit(2)

    conn = sqlite3.connect(DB_PATH)
    grand = Counter()
    if not args.personal_only:
        grand.update(_process_table(conn, "master_recipes",
                                    dry_run=args.dry_run, limit=args.limit,
                                    force=args.force))
    if not args.master_only:
        grand.update(_process_table(conn, "recipes",
                                    dry_run=args.dry_run, limit=args.limit,
                                    force=args.force))
    print()
    print(f"=== TOTAL: {dict(grand)} ===")
    if args.dry_run:
        print("(dry-run — no writes)")


if __name__ == "__main__":
    main()
