"""Backfill _source.siteName on existing recipes + master_recipes.

For every row with a source URL but no stored siteName, resolve a friendly
publisher name via the domain master (the `domains` table, seeded from the
JSON map) and write it back. Idempotent: rows that already carry a siteName
are left untouched, and BCC self-URLs are skipped (resolver returns "").

og:site_name capture on fresh extracts handles new rows going forward; this
fills the historical gap so the recipe sidebar + master lists stop falling
back to bare domains.

Usage:
  python -m scripts.backfill_site_names            # dry run (report only)
  python -m scripts.backfill_site_names --apply    # write changes
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter

from input.pipeline.site_names import friendly_site_name, host_from_url

DB = "recipes.db"


def run(apply: bool) -> None:
    db = sqlite3.connect(DB)
    filled = Counter()
    unmapped = Counter()
    for tbl in ("recipes", "master_recipes"):
        rows = db.execute(f"SELECT rowid, data FROM {tbl}").fetchall()
        for rowid, dj in rows:
            try:
                data = json.loads(dj)
            except Exception:
                continue
            src = data.get("_source") or {}
            url = (src.get("originalUrl") or "").strip()
            if not url:
                continue
            if (src.get("siteName") or "").strip():
                continue  # already has a name — leave it
            resolved = friendly_site_name("", url)
            if not resolved:
                unmapped[host_from_url(url)] += 1
                continue
            src["siteName"] = resolved
            data["_source"] = src
            filled[tbl] += 1
            if apply:
                db.execute(
                    f"UPDATE {tbl} SET data = ? WHERE rowid = ?",
                    (json.dumps(data, ensure_ascii=False), rowid),
                )
    if apply:
        db.commit()
    db.close()

    print("APPLIED" if apply else "DRY RUN (use --apply to write)")
    for tbl in ("recipes", "master_recipes"):
        print(f"  {tbl}: {filled[tbl]} rows would get a siteName")
    print(f"  unmapped domains (no map entry, left blank): {sum(unmapped.values())} rows "
          f"across {len(unmapped)} domains")
    if unmapped:
        print("  top unmapped (candidates for the domains table):")
        for host, n in unmapped.most_common(20):
            print(f"    {n:3}  {host}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()
    run(args.apply)
