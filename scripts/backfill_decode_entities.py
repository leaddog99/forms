"""Backfill: decode HTML entities (&amp;, &#x27;, &mdash;, etc.) in
every recipe and master_recipes row's JSON blob.

Some JSON-LD sources ship pre-encoded titles and descriptions (NYT,
Kitchn, AllRecipes occasionally), and the pre-2026-05-26 sanitizer
didn't decode them, so they got stored literally. Sidebar / form
displays via .textContent surface them as raw "&amp;" — visually wrong.

The sanitizer was fixed 2026-05-26 to run html.unescape on every string
during normalize. This script does a one-shot pass on the existing data.

Idempotent: a row whose JSON has no '&' is skipped instantly; rows with
no entities also no-op (the deep walker only mutates entity-bearing
strings).

Run from project root:
    python -m scripts.backfill_decode_entities --dry-run
    python -m scripts.backfill_decode_entities
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "recipes.db"


def _decode_entities_deep(obj):
    """Same recursive walker the sanitizer uses. Inlined here to keep
    this script importless of the larger save_recipe_api/sanitize
    module graph."""
    if isinstance(obj, str):
        return html.unescape(obj) if "&" in obj else obj
    if isinstance(obj, dict):
        return {k: _decode_entities_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_entities_deep(v) for v in obj]
    return obj


def _count_entities(s: str) -> int:
    """Rough count of decoded changes — for the per-row log line."""
    decoded = html.unescape(s)
    return 0 if decoded == s else 1


def _walk_count(obj) -> int:
    """How many string fields in this blob changed under unescape."""
    if isinstance(obj, str):
        return _count_entities(obj)
    if isinstance(obj, dict):
        return sum(_walk_count(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_walk_count(v) for v in obj)
    return 0


def backfill_table(conn: sqlite3.Connection, table: str, dry_run: bool) -> tuple[int, int]:
    """Returns (scanned, changed)."""
    cur = conn.execute(f"SELECT recipe_id, data FROM {table}")
    scanned = 0
    changed = 0
    for recipe_id, data_json in cur.fetchall():
        scanned += 1
        if "&" not in (data_json or ""):
            continue  # cheap short-circuit; no entity candidate strings
        try:
            data = json.loads(data_json)
        except Exception as e:
            print(f"  [SKIP] {table} {recipe_id}: JSON parse failed: {e}")
            continue
        n_changed = _walk_count(data)
        if n_changed == 0:
            continue
        decoded = _decode_entities_deep(data)
        new_json = json.dumps(decoded, indent=2)
        changed += 1
        print(f"  {table} {recipe_id}  ({n_changed} string{'s' if n_changed != 1 else ''} decoded)")
        if not dry_run:
            conn.execute(
                f"UPDATE {table} SET data = ?, updated_at = updated_at WHERE recipe_id = ?",
                (new_json, recipe_id),
            )
    if not dry_run:
        conn.commit()
    return scanned, changed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change; don't write.")
    args = ap.parse_args()

    print(f"DB: {DB_PATH}")
    print(f"dry_run: {args.dry_run}\n")
    with sqlite3.connect(DB_PATH) as conn:
        for table in ("recipes", "master_recipes"):
            print(f"=== {table} ===")
            scanned, changed = backfill_table(conn, table, args.dry_run)
            print(f"  scanned {scanned}, decoded {changed}\n")


if __name__ == "__main__":
    sys.exit(main())
