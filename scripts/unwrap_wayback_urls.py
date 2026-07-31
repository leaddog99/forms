"""Unwrap Wayback transport URLs stored as a recipe's identity.

A recipe fetched through the Wayback fallback (the UA -> unblocker -> Wayback
ladder) had the ARCHIVE url written into `_source.originalUrl`:

    https://web.archive.org/web/20251011133422id_/https://www.christinascucina.com/...

That is the transport, not the recipe's identity, and it costs us three ways:

  * SCORING — Moz has never crawled an archive.org snapshot, so every one of
    these came back http_code 0 with a domain-derived placeholder PA (47 across
    the board, ou 0.049). Before the 2026-07-31 gate that placeholder was stored
    as if measured; after it, these rows would simply fail to score forever.
  * ATTRIBUTION — the mandatory link points at archive.org rather than the
    publisher who wrote the recipe.
  * IDENTITY — `url_normalized` keys off the archive url, so the same recipe
    fetched directly later does not dedupe against it.

The original url is embedded in the archive url, so nothing needs re-fetching.
`_source.origin` already records "archive.org", so HOW we fetched it is not
lost; this also preserves the exact snapshot as `_source.archiveUrl`.

Wayback itself stays — it is how we reach blocked and long-dead pages (one of
these is a 1997 NYT piece). Only the mislabelling is wrong.

Usage:
    python -m scripts.unwrap_wayback_urls --dry-run
    python -m scripts.unwrap_wayback_urls --apply [--rescore]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from input.pipeline.url_utils import normalize_url  # noqa: E402

DB = "recipes.db"

# /web/<timestamp><modifier?>/<original>. The modifier (id_, im_, js_ …) tells
# Wayback to serve the raw asset; it is part of the transport, never the identity.
WAYBACK = re.compile(
    r"^https?://web\.archive\.org/web/\d+(?:id_|im_|if_|js_|cs_)?/(?P<orig>https?://.+)$",
    re.IGNORECASE,
)


def unwrap(url: str) -> str | None:
    m = WAYBACK.match(url or "")
    return m.group("orig") if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    ap.add_argument("--rescore", action="store_true", help="re-score the unwrapped URLs via Moz")
    ap.add_argument("--backup", default="docs/reports/wayback-unwrap-backup.json")
    args = ap.parse_args()
    apply = args.apply

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, url_normalized, data FROM master_recipes "
        "WHERE json_extract(data,'$._source.originalUrl') LIKE '%web.archive.org%'"
    ).fetchall()
    print(f"{len(rows)} row(s) with an archive.org identity")

    # Existing keys, so unwrapping never silently creates a second row for a
    # recipe we already hold directly.
    existing = {}
    for rid, un in conn.execute("SELECT id, url_normalized FROM master_recipes"):
        existing.setdefault(un, rid)

    planned, skipped = [], []
    for r in rows:
        d = json.loads(r["data"])
        src = d.get("_source") or {}
        arc = src.get("originalUrl") or ""
        orig = unwrap(arc)
        if not orig:
            skipped.append((r["id"], "unparseable", arc))
            continue
        norm = normalize_url(orig)
        clash = existing.get(norm)
        if clash is not None and clash != r["id"]:
            skipped.append((r["id"], f"duplicate of id {clash}", orig))
            continue
        planned.append((r["id"], arc, orig, norm, d))

    print(f"  to unwrap : {len(planned)}")
    print(f"  skipped   : {len(skipped)}")
    for rid, why, u in skipped:
        print(f"    id={rid:<6} {why:<22} {u[:70]}")

    if planned:
        print("\n  sample:")
        for rid, arc, orig, _n, _d in planned[:5]:
            print(f"    id={rid}\n      from {arc[:96]}\n      to   {orig[:96]}")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    Path(args.backup).parent.mkdir(parents=True, exist_ok=True)
    json.dump([{k: r[k] for k in r.keys()} for r in rows],
              open(args.backup, "w", encoding="utf-8"),
              indent=2, ensure_ascii=False, default=str)
    print(f"\nbacked up {len(rows)} row(s) -> {args.backup}")

    now = datetime.now(timezone.utc).isoformat()
    for rid, arc, orig, norm, d in planned:
        src = d.get("_source") or {}
        src["archiveUrl"] = arc          # keep the exact snapshot we read
        src["originalUrl"] = orig        # identity = the publisher's page
        d["_source"] = src
        conn.execute("UPDATE master_recipes SET data = ?, url_normalized = ?, updated_at = ? "
                     "WHERE id = ?", (json.dumps(d, indent=2), norm, now, rid))
    conn.commit()
    print(f"unwrapped {len(planned)} row(s)")

    if args.rescore:
        from input.pipeline import url_scoring as us
        us.reset_moz_row_stats()
        scored = dropped = 0
        for rid, _arc, orig, _n, _d in planned:
            s = us.score_url_via_moz(orig)
            if not s:
                dropped += 1
                continue
            row = conn.execute("SELECT data FROM master_recipes WHERE id = ?", (rid,)).fetchone()
            d = json.loads(row["data"])
            sc = d.get("_scoring") or {}
            sc["pageAuthority"] = s["page_authority"]
            sc["domainAuthority"] = s["domain_authority"]
            sc["ouScore"] = s["ou_score"]
            d["_scoring"] = sc
            conn.execute("UPDATE master_recipes SET data = ? WHERE id = ?",
                         (json.dumps(d, indent=2), rid))
            scored += 1
        conn.commit()
        st = us.moz_row_stats()
        print(f"re-scored {scored}, still unscoreable {dropped} "
              f"(moz rows {st['rows']}, uncrawled {st['uncrawled']})")

    print("integrity:", conn.execute("PRAGMA quick_check").fetchone()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
