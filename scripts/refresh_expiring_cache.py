"""Daily proactive cache refresh — re-extracts cache rows approaching
TTL expiry so users never hit a stale row.

Architectural premise: the cache TTL is 30 days. If we re-extract every
row aged 29+ days once per day, every row gets refreshed before any user
can see "stale" — the user-triggered stale path becomes a fallback for
cron failures, not the normal flow.

Drift handling: when the refresh detects a fingerprint change on a URL
(the source page meaningfully changed), this script stamps
`source_changed_at` on every saved recipe (in both `recipes` and
`master_recipes`) that points at the URL. Users see "Source page
changed (detected <date>)" the next time they open the recipe.

Usage (from project root):
  python -m scripts.refresh_expiring_cache              # all expiring rows
  python -m scripts.refresh_expiring_cache --limit 10   # cap
  python -m scripts.refresh_expiring_cache --dry-run    # report only
  python -m scripts.refresh_expiring_cache --age-days 25   # widen window

Scheduling: nothing automatic yet. Run from Windows Task Scheduler /
cron / a manual `python -m scripts.refresh_expiring_cache` whenever.

Cost shape: per-day work ≈ total_cache_rows / 30. A few thousand URLs
in cache → ~100 LLM refreshes per night → ~$0.10/day at Haiku rates.
"""
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import AFTER load_dotenv so the embedded clients pick up API keys.
from save_recipe_api import extract_recipe_from_url, DB_PATH  # noqa: E402

# 29 days = 24h cushion before the 30-day TTL kicks in. Tunable via
# --age-days if cron has been flaky and rows have already aged past the
# normal refresh slice.
DEFAULT_AGE_DAYS = 29


def find_expiring_urls(conn, age_days: int, limit: int):
    """Return cache rows aged >= age_days. ISO-8601 UTC strings compare
    lexicographically so a plain string < comparison works."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    query = (
        "SELECT url_normalized, semantic_fingerprint, created_at "
        "FROM llm_extract_cache "
        "WHERE created_at < ? "
        "ORDER BY created_at"
    )
    params: list = [cutoff]
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()


def stamp_drift_on_saved_recipes(conn, url_normalized: str) -> int:
    """Stamp `source_changed_at = now` on every saved recipe pointing
    at this URL across both tables. Skips rows that already have a
    non-null `source_changed_at` so we don't overwrite an earlier
    detection's date with a later one (the user is supposed to clear
    it by saving — earlier signal still applies until they do).

    Returns total rows stamped.
    """
    now = datetime.now(timezone.utc).isoformat()
    n_personal = conn.execute(
        "UPDATE recipes SET source_changed_at = ? "
        "WHERE url_normalized = ? AND source_changed_at IS NULL",
        (now, url_normalized),
    ).rowcount
    n_master = conn.execute(
        "UPDATE master_recipes SET source_changed_at = ? "
        "WHERE url_normalized = ? AND source_changed_at IS NULL",
        (now, url_normalized),
    ).rowcount
    conn.commit()
    return n_personal + n_master


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=0,
                        help="Max rows this run. 0 = no cap. Default 0.")
    parser.add_argument("--age-days", type=int, default=DEFAULT_AGE_DAYS,
                        help=f"Refresh rows aged >= this many days. "
                             f"Default {DEFAULT_AGE_DAYS} (= 24h before "
                             f"the 30-day TTL).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report which rows would refresh; do not "
                             "call the LLM or write anything.")
    args = parser.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        rows = find_expiring_urls(conn, args.age_days, args.limit)

    print(f"Cache rows aged >= {args.age_days} days: {len(rows)}")
    if not rows:
        print("Nothing to refresh.")
        return 0

    if args.dry_run:
        for url, fp, created_at in rows:
            print(f"  [dry-run] {url}  created {created_at}  fp={fp[:12]}…")
        return 0

    refreshed = 0
    failed = 0
    drift_count = 0
    drift_stamps_total = 0
    t_total = time.perf_counter()

    for i, (url, prior_fp, created_at) in enumerate(rows, start=1):
        print(f"[{i}/{len(rows)}] {url}")
        t0 = time.perf_counter()
        try:
            result = extract_recipe_from_url(url, user_id=0, force_refresh=True)
        except Exception as e:
            print(f"   FAIL: {type(e).__name__}: {e}")
            failed += 1
            continue
        dt = time.perf_counter() - t0

        # Drift propagates up via timings.source_drift from
        # _extract_cache_write -> _stamp_cache_timings.
        timings = result.get("_timings") or {}
        drifted = bool(timings.get("source_drift"))

        if drifted:
            with sqlite3.connect(DB_PATH) as conn:
                stamps = stamp_drift_on_saved_recipes(conn, url)
            print(f"   DRIFT  stamped {stamps} saved row(s)  ({dt:.1f}s)")
            drift_count += 1
            drift_stamps_total += stamps
        else:
            print(f"   OK     no drift  ({dt:.1f}s)")
        refreshed += 1

    print()
    print(f"Done in {time.perf_counter() - t_total:.1f}s.")
    print(f"  refreshed : {refreshed}/{len(rows)}")
    print(f"  failed    : {failed}")
    print(f"  drift     : {drift_count} URL(s), {drift_stamps_total} saved-recipe stamp(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
