"""Stamp PA provenance onto rows scored before the Moz gate existed.

WHY THIS EXISTS
---------------
Until 2026-08-04 `score_url_via_moz` computed `usable` — whether Moz actually
had data for a URL — and then never consulted it on the way out. A URL Moz had
never crawled came back with the DOMAIN-DERIVED PLACEHOLDER page authority it
ships with, and that value was stored as though it had been measured. The gate
is fixed, but the fix only stops NEW fabrications; rows already in the corpus
carry a PA that is indistinguishable from a real one.

This script does not repair or delete anything. It records WHICH IS WHICH, by
re-probing Moz and writing `_scoring.mozHttpCode`:

    NULL   never verified (every row written before the fix)
    0      verified: Moz has no data — this row's PA is FABRICATED
    >0     verified: measured

Deliberately non-destructive. Measured 2026-08-04: 0 of the top 200 by OU are
fabricated (the placeholder parks a row near the OU=0 line, ~18 points below
the top-200 floor), so stripping the values would change nothing anyone sees
while destroying the population the paid-PA calibration needs — those rows are
uncrawled because their publishers BLOCK CRAWLERS (bostonglobe, cooking.nytimes,
smittenkitchen, washingtonpost), not because nobody links to them. Marking beats
deleting: a marked row can be excluded from a fit, re-crawled later, or remapped
by the calibration. A deleted one is gone.

COST
----
One Moz row per URL with canonical-variant learning on (~$0.002 at Growth
Medium: $250 / 120k rows) — but a FABRICATED url costs ~5, because a learned
single-variant probe that comes back with no data triggers the self-heal
re-expansion to all four variants. At the measured 5% fabrication rate that is
~1.2 rows/url average, so the 4,179-row corpus is **~$10**, not $8.70. It is
also ~2 API round trips per fabricated url, so the run is slow: budget minutes
per hundred, not seconds.

Prints the estimate and makes you pass --limit or --all, because it is real money.

Usage:
    python -m scripts.verify_pa_provenance --limit 100          # a bite
    python -m scripts.verify_pa_provenance --host bostonglobe.com --all
    python -m scripts.verify_pa_provenance --all --dry-run      # cost only
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from input.pipeline.url_scoring import moz_http_status  # noqa: E402  (loads .env)

DB = "recipes.db"
COST_PER_ROW = 250 / 120_000   # Growth Medium


def _targets(conn, table: str, host: str | None, limit: int | None) -> list[sqlite3.Row]:
    """Rows carrying a PA whose provenance we have never checked."""
    sql = (f"SELECT id, json_extract(data,'$._source.originalUrl') url, source_host, "
           f"page_authority pa, ou_score ou FROM {table} "
           f"WHERE moz_http_code IS NULL AND page_authority IS NOT NULL "
           f"AND json_extract(data,'$._source.originalUrl') IS NOT NULL")
    args: list = []
    if host:
        sql += " AND source_host = ?"
        args.append(host)
    sql += " ORDER BY ou_score DESC"      # most consequential rows first
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql, args))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", default="master_recipes",
                    choices=("master_recipes", "recipes"))
    ap.add_argument("--host", help="restrict to one source_host")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--all", action="store_true", help="no limit — costs real money")
    ap.add_argument("--dry-run", action="store_true", help="count and cost, no Moz calls")
    a = ap.parse_args()
    if not a.limit and not a.all:
        print("pass --limit N or --all (this spends Moz rows)"); return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = _targets(conn, a.table, a.host, None if a.all else a.limit)
    print(f"{a.table}: {len(rows):,} row(s) with UNVERIFIED provenance"
          f"{f' on {a.host}' if a.host else ''}")
    # 1.2 rows/url: measured urls cost 1, fabricated ones ~5 via the self-heal
    # re-probe, at a measured 5% fabrication rate.
    est = int(len(rows) * 1.2)
    print(f"estimated cost: ~{est:,} Moz rows  ~${est * COST_PER_ROW:.2f}"
          f"   (slow: ~2 API round trips per fabricated url)\n")
    if a.dry_run or not rows:
        return 0

    measured = fabricated = unknown = 0
    for i, r in enumerate(rows, 1):
        code = moz_http_status(r["url"])
        if code is None:                     # never got an answer — leave NULL, retry later
            unknown += 1
            print(f"  [{i:>4}/{len(rows)}] NO-ANSWER   {r['url'][:78]}")
            continue
        # Provenance ONLY. The PA is left exactly as it is, fabricated or not.
        conn.execute(
            f"UPDATE {a.table} SET data = json_set(data,'$._scoring.mozHttpCode', ?) WHERE id = ?",
            (int(code), r["id"]),
        )
        if code:
            measured += 1
        else:
            fabricated += 1
            print(f"  [{i:>4}/{len(rows)}] FABRICATED  pa={r['pa']} ou={r['ou']:.2f}  "
                  f"{r['source_host']}  id={r['id']}")
        if i % 50 == 0:
            conn.commit()
    conn.commit()

    n = measured + fabricated
    print(f"\n  measured   {measured:>6}" + (f"  ({measured/n*100:.1f}%)" if n else ""))
    print(f"  FABRICATED {fabricated:>6}" + (f"  ({fabricated/n*100:.1f}%)" if n else ""))
    print(f"  no answer  {unknown:>6}  (left NULL, safe to re-run)")
    if fabricated:
        print(f"\n  find them:  SELECT * FROM {a.table} WHERE moz_http_code = 0")
        print(f"  exclude:    ... WHERE moz_http_code IS NULL OR moz_http_code > 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
