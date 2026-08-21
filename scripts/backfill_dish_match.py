"""Backfill `_match` on master recipes that do not know their dish.

WHY THIS EXISTS. The save path's dish match was gated on which TABLE a row
was in rather than on whether the row already knew its dish, so it never ran
for master recipes at all. That was fine for rows a dish refresh curated FOR
a dish (they carry `_master.dish` as ground truth) and wrong for every
interactive capture, which carries no dish at all. Those rows stored a vector
and then discarded the one thing the vector was for: the form's "Matched dish"
chip read an em-dash on a row that WAS embedded, which reads as "never
embedded". Fixed at the source in save_recipe_api._stamp_dish_match; this
catches the rows saved before that.

FREE TO RUN. The dish index is local (sqlite-vec) and every row already has
its embedding stored, so this is a KNN query per row — no embedding call, no
API spend, nothing billable.

WHAT IT WRITES. `_match` on `data`, with the same shape the save path stamps:
the confident dish (or None), the L2 distance, and the top-3 candidates. A
CONFIDENT match additionally stamps the dish onto the vec index row so the
KNN filter can use it — mirroring what save does. `updated_at` is deliberately
NOT bumped: this is derived metadata, not an edit, and bumping it would
reorder the sidebar's default sort and make 3,253 rows look freshly touched.

Idempotent + resumable: a row that already carries `_match` is skipped, so an
interrupted run is restarted by re-running it.

Usage:
  python -m scripts.backfill_dish_match --dry-run          # preview, writes nothing
  python -m scripts.backfill_dish_match --dry-run --limit 25
  python -m scripts.backfill_dish_match                    # apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from input.pipeline.embeddings import bytes_to_vec          # noqa: E402
from input.pipeline import vector_store                     # noqa: E402
from input.pipeline import system_config as _cfg            # noqa: E402

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recipes.db")

# Rows with no curated dish AND no inferred one. `embedding IS NOT NULL`
# because the match is derived from the stored vector — a row without one is a
# coverage problem for check_embeddings, not something to fix here.
SELECT_SQL = """
    SELECT id, data, embedding
      FROM master_recipes
     WHERE embedding IS NOT NULL
       AND json_extract(data, '$._master.dish') IS NULL
       AND json_extract(data, '$._match')       IS NULL
     ORDER BY id
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    # db_path passed EXPLICITLY. get_setting() with no db_path lazily imports
    # save_recipe_api to read DB_PATH, and that import runs the app's startup,
    # which resets in-flight jobs. That is the landmine documented in
    # bcc-state-code.md, and this is the line that would trip it.
    max_dist = float(_cfg.get_setting("dish_match_max_distance", 0.85,
                                      db_path=args.db))

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout = 30000")
    vector_store.enable_vec(conn)

    rows = conn.execute(SELECT_SQL).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    print(f"[BACKFILL] {len(rows)} master rows with no dish and no match")
    print(f"[BACKFILL] confidence threshold (L2) = {max_dist}")
    print(f"[BACKFILL] mode = {'DRY RUN (no writes)' if args.dry_run else 'APPLY'}")
    print()

    confident = weak = nocand = failed = 0
    by_dish: dict[str, int] = {}

    for n, (rid, dj, blob) in enumerate(rows, 1):
        try:
            d = json.loads(dj)
            vec = bytes_to_vec(blob)
            cands = vector_store.find_similar_dishes(conn, vec, k=3)
            if not cands:
                nocand += 1
                continue

            best = cands[0]
            is_conf = best["distance"] <= max_dist
            d["_match"] = {
                "dish": best["name"] if is_conf else None,
                "distance": round(best["distance"], 4),
                "confident": is_conf,
                "candidates": [
                    {"dish": m["name"], "distance": round(m["distance"], 4)}
                    for m in cands
                ],
                "matched_at": datetime.now(timezone.utc).isoformat(),
                # Provenance: this row was matched by the backfill, not at save
                # time. Worth being able to tell them apart later.
                "matched_by": "backfill_dish_match",
            }

            if is_conf:
                confident += 1
                by_dish[best["name"]] = by_dish.get(best["name"], 0) + 1
                name = (d.get("name") or "")[:44]
                print(f"  [{n}/{len(rows)}] {rid:6} {name:<44} -> "
                      f"{best['name']!r} d={best['distance']:.3f}")
            else:
                weak += 1

            if not args.dry_run:
                # `data` only. NOT updated_at — see the module docstring.
                conn.execute("UPDATE master_recipes SET data = ? WHERE id = ?",
                             (json.dumps(d), rid))
                if is_conf:
                    ch = ((d.get("classification") or {}).get("chapter") or None)
                    vector_store.upsert_recipe_vector(
                        conn, rid, vec, chapter=ch, dish=best["name"])
                if n % 200 == 0:
                    conn.commit()
                    print(f"  … committed through row {n}")
        except Exception as e:
            failed += 1
            print(f"  [{n}] row {rid} FAILED: {type(e).__name__}: {e}")

    if not args.dry_run:
        conn.commit()

    print()
    print(f"[BACKFILL] confident matches   {confident}")
    print(f"[BACKFILL] no confident match  {weak}   (candidates still stored)")
    print(f"[BACKFILL] no candidates       {nocand}")
    print(f"[BACKFILL] failed              {failed}")
    if by_dish:
        print()
        print("[BACKFILL] confident matches by dish:")
        for dish, c in sorted(by_dish.items(), key=lambda kv: -kv[1]):
            print(f"    {c:5}  {dish}")
    if args.dry_run:
        print()
        print("[BACKFILL] DRY RUN — nothing was written.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
