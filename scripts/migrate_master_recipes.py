"""One-time migration: move batch-tagged recipes out of `recipes` into
`master_recipes`.

Selection criterion: `json_extract(data, '$._batch') IS NOT NULL` — every
recipe stamped by `intake/process_batch.py` carries a `_batch` block
(name/source/rank). Today that's 34 rows (Banana Bread × 15, Spanakopita
× 19).

Safety:
  - Single BEGIN…COMMIT transaction.
  - INSERT first, verify the new count matches the moved-set size, only
    then DELETE the originals.
  - Stamps `user_id = 0` on every moved row (the master/admin-owned
    discriminator), and overwrites the recipe's nested `_source`-style
    data only if needed — the JSON blob is preserved as-is otherwise.
  - --dry-run prints what would move without writing. --commit writes.

Usage:
    python migrate_master_recipes.py                # dry run, default
    python migrate_master_recipes.py --commit       # apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Script lives in scripts/; resolve recipes.db at the project root
# regardless of where it's invoked from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = str(PROJECT_ROOT / "recipes.db")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", action="store_true",
                    help="Actually move rows (default is dry-run).")
    ap.add_argument("--db", default=DB_PATH, help=f"DB path (default {DB_PATH}).")
    ap.add_argument("--force", action="store_true",
                    help="Migrate rows even when _batch.name is missing "
                         "(guard A). Default refuses such rows.")
    args = ap.parse_args()

    print(f"[MIGRATE] DB: {args.db}")
    print(f"[MIGRATE] Mode: {'COMMIT' if args.commit else 'DRY-RUN (no writes)'}")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    # Sanity: both tables exist
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "recipes" not in tables:
        print("[ERROR] recipes table not found")
        return 2
    if "master_recipes" not in tables:
        print("[ERROR] master_recipes table not found — run the server once "
              "to trigger init_db, then re-run this migration.")
        return 2

    # Candidate rows: anything in `recipes` whose data has a _batch block.
    candidates = con.execute("""
        SELECT id, recipe_id, user_id, data, url_normalized, source_changed_at,
               created_at, updated_at
        FROM recipes
        WHERE json_extract(data, '$._batch') IS NOT NULL
        ORDER BY id
    """).fetchall()
    print(f"[MIGRATE] {len(candidates)} candidate rows in `recipes`:")
    by_batch: dict[str, int] = {}
    missing_name_rows: list[tuple[str, str]] = []  # (recipe_id, reason)
    for row in candidates:
        try:
            d = json.loads(row["data"])
            batch = d.get("_batch") or {}
            batch_name = batch.get("name", "")
        except Exception as e:
            batch_name = ""
            missing_name_rows.append((row["recipe_id"], f"bad json: {e}"))
            continue
        if not batch_name:
            missing_name_rows.append((row["recipe_id"], "_batch.name empty/missing"))
            continue
        by_batch[batch_name] = by_batch.get(batch_name, 0) + 1
    for name, n in sorted(by_batch.items()):
        print(f"[MIGRATE]   {name}: {n}")

    # GUARD A: refuse rows without _batch.name unless --force.
    if missing_name_rows:
        print(f"[MIGRATE] {len(missing_name_rows)} candidate(s) lack _batch.name:")
        for rid, reason in missing_name_rows[:10]:
            print(f"[MIGRATE]   {rid}: {reason}")
        if not args.force:
            print(f"[MIGRATE] Refusing to migrate rows without _batch.name. "
                  f"Re-run with --force to override, or fix those rows first.")
            return 3
        else:
            print(f"[MIGRATE] --force set; including those rows anyway.")

    if not candidates:
        print("[MIGRATE] Nothing to do.")
        return 0

    # Conflict check: are any of these URLs already in master_recipes?
    # (Would only happen if migration was partially run before.) Skip dup
    # rows on insert; collect them for reporting.
    candidate_urls = {row["url_normalized"] for row in candidates if row["url_normalized"]}
    existing_in_master = set()
    if candidate_urls:
        placeholders = ",".join("?" * len(candidate_urls))
        existing_in_master = {
            r[0] for r in con.execute(
                f"SELECT url_normalized FROM master_recipes "
                f"WHERE url_normalized IN ({placeholders}) AND user_id = 0",
                tuple(candidate_urls)
            ).fetchall()
        }
    if existing_in_master:
        print(f"[MIGRATE] {len(existing_in_master)} candidate URL(s) already in "
              f"master_recipes — those originals will be DELETED from "
              f"`recipes` without re-inserting (master copy wins).")

    if not args.commit:
        print("[MIGRATE] DRY-RUN complete. Re-run with --commit to apply.")
        return 0

    # Real migration
    now = datetime.utcnow().isoformat()
    inserted = 0
    skipped_existing = 0
    deleted = 0

    try:
        con.execute("BEGIN")
        for row in candidates:
            url_norm = row["url_normalized"] or ""
            if url_norm and url_norm in existing_in_master:
                # Master already has it; just delete the original.
                skipped_existing += 1
                continue
            # Force user_id=0 in BOTH the row column and the embedded JSON
            # (in case any caller infers from one but not the other).
            try:
                d = json.loads(row["data"]) if row["data"] else {}
            except Exception:
                d = {}
            data_str = json.dumps(d, indent=2)

            con.execute("""
                INSERT INTO master_recipes (
                    recipe_id, user_id, data, url_normalized,
                    source_changed_at, created_at, updated_at
                ) VALUES (?, 0, ?, ?, ?, ?, ?)
            """, (
                row["recipe_id"], data_str, url_norm,
                row["source_changed_at"],
                row["created_at"] or now,
                row["updated_at"] or now,
            ))
            inserted += 1

        # Verify
        moved_master_count = con.execute(
            "SELECT COUNT(*) FROM master_recipes WHERE user_id = 0"
        ).fetchone()[0]
        expected_total = inserted + len(existing_in_master)
        if moved_master_count < expected_total:
            raise RuntimeError(
                f"Insert verification failed: master_recipes has "
                f"{moved_master_count} rows with user_id=0, expected "
                f">= {expected_total}. Rolling back."
            )

        # Now delete originals from `recipes`
        recipe_ids = [row["recipe_id"] for row in candidates]
        placeholders = ",".join("?" * len(recipe_ids))
        cur = con.execute(
            f"DELETE FROM recipes WHERE recipe_id IN ({placeholders})",
            tuple(recipe_ids)
        )
        deleted = cur.rowcount
        if deleted != len(candidates):
            raise RuntimeError(
                f"Delete verification failed: removed {deleted}, expected "
                f"{len(candidates)}. Rolling back."
            )

        con.commit()
    except Exception as e:
        con.rollback()
        print(f"[ERROR] {e}")
        return 1

    print(f"[MIGRATE] COMMIT done.")
    print(f"[MIGRATE]   inserted into master_recipes: {inserted}")
    print(f"[MIGRATE]   skipped (already in master):  {skipped_existing}")
    print(f"[MIGRATE]   deleted from recipes:         {deleted}")

    # Final counts for sanity
    rcount = con.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
    mcount = con.execute("SELECT COUNT(*) FROM master_recipes").fetchone()[0]
    print(f"[MIGRATE] Final: recipes={rcount}  master_recipes={mcount}")

    # GUARD C: post-commit spot-check so the operator visually confirms
    # the right rows landed (recipe_id + name + batch.name on a small sample).
    print(f"[MIGRATE] Spot-check (first 5 master_recipes rows):")
    spot = con.execute("""
        SELECT recipe_id,
               json_extract(data, '$.name')          AS name,
               json_extract(data, '$._batch.name')   AS batch_name,
               CAST(json_extract(data, '$._batch.rank') AS INTEGER) AS rank
        FROM master_recipes WHERE user_id = 0
        ORDER BY rank, recipe_id
        LIMIT 5
    """).fetchall()
    for row in spot:
        print(f"[MIGRATE]   batch={row['batch_name']!r:<18} rank={row['rank']!s:<4} name={row['name']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
