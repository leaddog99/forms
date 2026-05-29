"""One-shot backfill: capture page screenshots for every saved recipe
with a source URL. Walks master + personal, calls
input.pipeline.screenshot_pipeline.capture_screenshot on each, stamps
_source.pageScreenshot, saves back.

Wall time: ~3-5s per row via Playwright. For 354 rows expect 20-30
minutes total. Idempotent — already-screenshotted rows skip unless
--force.

Usage:
  python -m scripts.backfill_page_screenshots --dry-run
  python -m scripts.backfill_page_screenshots --limit 5
  python -m scripts.backfill_page_screenshots
  python -m scripts.backfill_page_screenshots --force   # re-capture every row
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

from input.pipeline.screenshot_pipeline import capture_screenshot  # noqa: E402

DB_PATH = str(PROJECT_ROOT / "recipes.db")


def _process_table(conn: sqlite3.Connection, table: str, *,
                   dry_run: bool, limit: int, force: bool) -> Counter:
    rows = conn.execute(
        f"SELECT id, recipe_id, data FROM {table} ORDER BY id"
    ).fetchall()
    print(f"--- {table}: {len(rows)} rows ---")
    counts: Counter = Counter()
    captured = 0
    t0 = time.perf_counter()

    for rid, recipe_uuid, dj in rows:
        try:
            d = json.loads(dj)
        except Exception:
            counts["error"] += 1
            continue
        src = d.get("_source") or {}
        original_url = (src.get("originalUrl") or "").strip()
        if not original_url or not original_url.startswith(("http://", "https://")):
            counts["no-source-url"] += 1
            continue
        existing = (src.get("pageScreenshot") or "").strip()
        if existing and not force:
            counts["already"] += 1
            continue

        try:
            shot_url = capture_screenshot(original_url, recipe_uuid)
        except Exception as e:
            print(f"  [error] id={rid}: {e}")
            counts["capture-error"] += 1
            continue
        if not shot_url:
            counts["capture-failed"] += 1
            print(f"  [skip] {table}.id={rid} (capture returned None)")
            continue

        src["pageScreenshot"] = shot_url
        d["_source"] = src
        counts["captured"] += 1
        if not dry_run:
            conn.execute(
                f"UPDATE {table} SET data = ?, "
                f"updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                f"WHERE id = ?",
                (json.dumps(d, indent=2), rid),
            )
            conn.commit()

        captured += 1
        if captured % 10 == 0:
            elapsed = time.perf_counter() - t0
            rate = captured / elapsed if elapsed else 0
            print(f"  ... {captured} captured, {rate:.2f}/s "
                  f"(latest id={rid}, {original_url[:60]})")
        if limit and captured >= limit:
            print(f"  reached limit ({limit})")
            break

    print(f"  {table} done: {dict(counts)} in {time.perf_counter()-t0:.1f}s")
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true")
    p.add_argument("--master-only", action="store_true")
    p.add_argument("--personal-only", action="store_true")
    args = p.parse_args()

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
