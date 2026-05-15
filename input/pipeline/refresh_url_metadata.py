"""
Out-of-band maintenance for the metabase_url table.

Two modes (combine with the same invocation):
    --refresh-stale --days N    Rescore rows whose moz_last_scored is older
                                than N days (or null).
    --prune-orphans             Delete rows no recipe references via
                                _source.originalUrl.

Examples:
    python pipeline/refresh_url_metadata.py --refresh-stale --days 30
    python pipeline/refresh_url_metadata.py --prune-orphans
    python pipeline/refresh_url_metadata.py --refresh-stale --days 30 --prune-orphans

Run from the repo root so the relative DB path and the `pipeline` package
imports resolve.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Allow `python pipeline/refresh_url_metadata.py` to import siblings.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from input.pipeline.url_scoring import (
    ensure_metabase_url_table,
    score_url_via_moz,
)
from input.pipeline.url_utils import normalize_url

DB_PATH = "recipes.db"


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def refresh_stale(conn: sqlite3.Connection, days: int) -> int:
    """Re-score rows whose moz_last_scored is null or older than `days`."""
    cutoff = _cutoff_iso(days)
    rows = conn.execute(
        "SELECT url FROM metabase_url WHERE moz_last_scored IS NULL OR moz_last_scored < ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        print(f"[refresh] no rows stale beyond {days}d.")
        return 0

    rescored = 0
    now = datetime.now(timezone.utc).isoformat()
    for (url,) in rows:
        scores = score_url_via_moz(url)
        if not scores:
            # Missing creds or Moz error — leave row, try again later.
            continue
        conn.execute(
            """
            UPDATE metabase_url SET
                page_authority = ?,
                domain_authority = ?,
                ou_score = ?,
                raw_title = CASE WHEN ? <> '' THEN ? ELSE raw_title END,
                moz_last_scored = ?
            WHERE url = ?
            """,
            (
                scores["page_authority"],
                scores["domain_authority"],
                scores["ou_score"],
                scores["raw_title"], scores["raw_title"],
                now,
                url,
            ),
        )
        rescored += 1
    conn.commit()
    print(f"[refresh] rescored {rescored} / {len(rows)} candidate rows.")
    return rescored


def prune_orphans(conn: sqlite3.Connection) -> int:
    """Delete metabase_url rows no recipe currently references."""
    # Collect referenced URLs from the recipes table. Each recipe's data is
    # a JSON blob; pull _source.originalUrl and normalize defensively.
    referenced: set[str] = set()
    for (data,) in conn.execute("SELECT data FROM recipes").fetchall():
        try:
            obj = json.loads(data)
        except Exception:
            continue
        src = (obj.get("_source") or {}).get("originalUrl")
        if src:
            referenced.add(normalize_url(src))

    rows = conn.execute("SELECT url FROM metabase_url").fetchall()
    orphans = [url for (url,) in rows if url not in referenced]
    if not orphans:
        print("[prune] no orphan rows.")
        return 0

    conn.executemany("DELETE FROM metabase_url WHERE url = ?", [(u,) for u in orphans])
    conn.commit()
    print(f"[prune] deleted {len(orphans)} orphan row(s).")
    return len(orphans)


def main() -> None:
    parser = argparse.ArgumentParser(description="Maintain metabase_url table.")
    parser.add_argument("--refresh-stale", action="store_true", help="Re-score rows older than --days.")
    parser.add_argument("--days", type=int, default=30, help="Staleness threshold in days (default 30).")
    parser.add_argument("--prune-orphans", action="store_true", help="Delete rows not referenced by any recipe.")
    args = parser.parse_args()

    if not (args.refresh_stale or args.prune_orphans):
        parser.print_help()
        sys.exit(1)

    with sqlite3.connect(DB_PATH) as conn:
        ensure_metabase_url_table(conn)
        if args.refresh_stale:
            refresh_stale(conn, args.days)
        if args.prune_orphans:
            prune_orphans(conn)


if __name__ == "__main__":
    main()
