"""One-time: populate master_recipes.embedding from the existing
recipes_master_vec vectors, then rebuild the vec index from those BLOBs.

Context (2026-05-30): master recipe embeddings used to live ONLY in the
recipes_master_vec sqlite-vec table — no source-of-truth column — so the
git .sql dump (which excludes vec0 tables) lost them, and deletes left
orphan vectors behind (the index had more rows than master_recipes). We
added a master_recipes.embedding BLOB (source of truth, like dishes) +
AFTER DELETE triggers. This script backfills that column for existing
rows WITHOUT re-embedding (the vectors are already in the vec table — we
read them straight out as float32 bytes), then calls
rebuild_master_vec_from_blobs which clears + rebuilds the index from the
BLOBs — dropping any orphans as a side effect.

Idempotent: only fills NULL embedding columns; safe to re-run.

Importing save_recipe_api runs init_db(), which adds the embedding column
and creates the triggers — so this script also doubles as the migration
trigger if the server hasn't been restarted yet. No server is started.

Usage:
  python -m scripts.backfill_master_embedding_blob            # do it
  python -m scripts.backfill_master_embedding_blob --dry-run  # report only
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# init_db() runs at import: adds master_recipes.embedding + creates the
# vec-cleanup triggers. Gives us DB_PATH too.
import save_recipe_api as api  # noqa: E402
from input.pipeline import vector_store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report counts, write nothing")
    args = ap.parse_args()

    db_path = api.DB_PATH
    conn = sqlite3.connect(db_path)
    vector_store.enable_vec(conn)

    master_total = conn.execute("SELECT COUNT(*) FROM master_recipes").fetchone()[0]
    vec_before = conn.execute("SELECT COUNT(*) FROM recipes_master_vec").fetchone()[0]
    have_blob_before = conn.execute(
        "SELECT COUNT(*) FROM master_recipes WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    orphans = conn.execute(
        "SELECT COUNT(*) FROM recipes_master_vec "
        "WHERE id NOT IN (SELECT id FROM master_recipes)"
    ).fetchone()[0]

    print(f"master_recipes rows      : {master_total}")
    print(f"recipes_master_vec rows  : {vec_before}")
    print(f"  of which orphaned      : {orphans}")
    print(f"master rows w/ embedding : {have_blob_before}")

    # Pull every vector for an id that still exists in master_recipes and
    # whose BLOB is currently NULL.
    vec_rows = conn.execute(
        "SELECT v.id, v.embedding FROM recipes_master_vec v "
        "JOIN master_recipes m ON m.id = v.id "
        "WHERE m.embedding IS NULL"
    ).fetchall()
    print(f"\nvectors to copy into BLOBs: {len(vec_rows)}")

    if args.dry_run:
        print("--dry-run: no writes.")
        return 0

    for rid, blob in vec_rows:
        conn.execute(
            "UPDATE master_recipes SET embedding = ? WHERE id = ?",
            (blob, rid),
        )
    conn.commit()
    print(f"backfilled {len(vec_rows)} embedding BLOBs")

    # Rebuild the index from the BLOBs — clears the table (dropping
    # orphans) and reinserts one row per master recipe that has a BLOB.
    written = vector_store.rebuild_master_vec_from_blobs(conn)
    print(f"rebuilt recipes_master_vec from BLOBs: {written} vectors")

    # Final state.
    vec_after = conn.execute("SELECT COUNT(*) FROM recipes_master_vec").fetchone()[0]
    have_blob_after = conn.execute(
        "SELECT COUNT(*) FROM master_recipes WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    orphans_after = conn.execute(
        "SELECT COUNT(*) FROM recipes_master_vec "
        "WHERE id NOT IN (SELECT id FROM master_recipes)"
    ).fetchone()[0]
    print(f"\nFINAL:")
    print(f"  recipes_master_vec rows : {vec_after}  (was {vec_before})")
    print(f"  orphaned vectors        : {orphans_after}  (was {orphans})")
    print(f"  master rows w/ embedding: {have_blob_after}  (was {have_blob_before})")
    missing = master_total - have_blob_after
    if missing:
        print(f"  NOTE: {missing} master rows still have no embedding "
              f"(saved before vec upsert existed) — they'll get one on "
              f"their next save/refresh, or run a re-embed backfill.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
