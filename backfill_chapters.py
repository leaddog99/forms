"""One-shot backfill: stamp classification.chapter on existing recipes
in recipes.db that don't have one yet.

Walks every row, parses data JSON, runs the chapter classifier on the
recipe name + ingredients, writes back. Idempotent — re-running only
touches rows still missing a chapter.

Most recipes will hit the keyword-shortcut layer (zero API cost).
Ambiguous titles fall through to gpt-4o-mini at ~$0.0001 each. For
~130 recipes, expect <$0.01 total even if every single one needs the
LLM (and most won't).

Usage:
  python backfill_chapters.py           # do the work
  python backfill_chapters.py --dry-run # show what would change, no writes
"""
import argparse
import json
import sqlite3
import sys
import time
from collections import Counter

from extract.chapter_classifier import classify_chapter

DB_PATH = "recipes.db"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="show planned changes, don't write")
    p.add_argument("--limit", type=int, default=0, help="stop after N updates (0 = no limit)")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, recipe_id, data FROM recipes ORDER BY id").fetchall()
    print(f"Walking {len(rows)} recipes...")
    print()

    counts = Counter()
    updates = []
    skipped_empty_name = 0
    skipped_already_set = 0
    t_start = time.perf_counter()

    for rid, recipe_uuid, data_json in rows:
        try:
            d = json.loads(data_json)
        except Exception as e:
            print(f"  id={rid}: JSON parse failed ({e}) — skipping")
            continue

        cls = d.get("classification") or {}
        if cls.get("chapter"):
            skipped_already_set += 1
            continue

        name = (d.get("name") or "").strip()
        if not name:
            skipped_empty_name += 1
            continue

        ingredients = d.get("recipeIngredient") or []
        chapter = classify_chapter(name, ingredients)
        counts[chapter] += 1
        cls["chapter"] = chapter
        d["classification"] = cls
        updates.append((rid, name, chapter, d))
        # Per-recipe log line — easy to scan for surprises.
        print(f"  id={rid:4} {name[:50]:50} -> {chapter}")
        if args.limit and len(updates) >= args.limit:
            print(f"\n--limit {args.limit} reached, stopping")
            break

    elapsed = time.perf_counter() - t_start
    print()
    print(f"Skipped: {skipped_already_set} (chapter already set), "
          f"{skipped_empty_name} (no name)")
    print(f"Classified: {len(updates)} recipes in {elapsed:.1f}s")
    print()
    print("Distribution:")
    for chapter, n in counts.most_common():
        print(f"  {n:3}  {chapter}")

    if args.dry_run:
        print("\n(--dry-run set — no writes)")
        return

    if not updates:
        print("\nNothing to write.")
        return

    print(f"\nWriting {len(updates)} updates to {DB_PATH}...")
    for rid, name, chapter, d in updates:
        conn.execute(
            "UPDATE recipes SET data = ? WHERE id = ?",
            (json.dumps(d, indent=2), rid),
        )
    conn.commit()
    print("Done.")


if __name__ == "__main__":
    main()
