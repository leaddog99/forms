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

from input.pipeline.url_utils import normalize_url, unwrap_wayback, is_wayback  # noqa: E402

DB = "recipes.db"

# Tables that hold recipes. `recipes` was missing from the first pass, so two
# user rows kept an archive identity after master_recipes had been cleaned.
TABLES = ("master_recipes", "recipes")

# archive.org's OWN authority, which is what Moz returns when you score a
# snapshot URL instead of the page it archived. Every such row is identical —
# PA 47 / DA 94 / OU 0.049 — which is what makes them safe to identify by value.
# These rows are the SECOND failure mode: 13 of them had already had their
# originalUrl unwrapped by the first run of this script, so a scan for archive
# URLs no longer finds them, while the score they are ranked on is still
# archive.org's. Unwrapping the identity without re-scoring only hides it.
ARCHIVE_PA, ARCHIVE_DA = 47.0, 94.0


def unwrap(url: str) -> str | None:
    """The publisher URL inside an archive URL, or None if not one.

    Delegates to the canonical helper (input.pipeline.url_utils) — this module
    used to carry its own regex, one of three independent copies, none of which
    the Moz scoring path consulted."""
    return unwrap_wayback(url) if is_wayback(url) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    ap.add_argument("--rescore", action="store_true", help="re-score the unwrapped URLs via Moz")
    ap.add_argument("--backup", default="docs/reports/wayback-unwrap-backup.json")
    args = ap.parse_args()
    apply = args.apply

    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row

    all_rows, planned, skipped, stale = [], [], [], []
    for tbl in TABLES:
        rows = conn.execute(
            f"SELECT id, url_normalized, data FROM {tbl} "
            "WHERE json_extract(data,'$._source.originalUrl') LIKE '%web.archive.org%'"
        ).fetchall()
        all_rows.extend((tbl, r) for r in rows)
        print(f"{tbl}: {len(rows)} row(s) with an archive.org identity")

        # Existing keys, so unwrapping never silently creates a second row for a
        # recipe we already hold directly.
        existing = {}
        for rid, un in conn.execute(f"SELECT id, url_normalized FROM {tbl}"):
            existing.setdefault(un, rid)

        for r in rows:
            d = json.loads(r["data"])
            src = d.get("_source") or {}
            arc = src.get("originalUrl") or ""
            orig = unwrap(arc)
            if not orig:
                skipped.append((tbl, r["id"], "unparseable", arc))
                continue
            norm = normalize_url(orig)
            clash = existing.get(norm)
            if clash is not None and clash != r["id"]:
                skipped.append((tbl, r["id"], f"duplicate of id {clash}", orig))
                continue
            planned.append((tbl, r["id"], arc, orig, norm, d))

        # SECOND failure mode: identity already unwrapped by an earlier run, but
        # the SCORE is still the one Moz returned for the snapshot. A scan for
        # archive URLs cannot see these — 13 of them, ranked on archive.org.
        n_before = len(stale)
        # `rootDomain = archive.org` is the DISCRIMINATING signal, and the value
        # match alone is not: washingtonpost.com is genuinely DA 94, and 7 of its
        # thin /recipes/ pages genuinely measure PA 47. Verified against Moz —
        # fabricated WaPo URLs return http_code 0 (rejected) while two real ones
        # returned 47 and 55 — so those rows are measurements, not placeholders.
        # Without this clause every run re-scored them to reach the same numbers.
        for r in conn.execute(
            f"SELECT id, url_normalized, data FROM {tbl} WHERE "
            "json_extract(data,'$._scoring.pageAuthority') = ? AND "
            "json_extract(data,'$._scoring.domainAuthority') = ? AND "
            "json_extract(data,'$._scoring.rootDomain') = 'archive.org' AND "
            "COALESCE(json_extract(data,'$._source.originalUrl'),'') "
            "NOT LIKE '%web.archive.org%'",
            (ARCHIVE_PA, ARCHIVE_DA),
        ).fetchall():
            d = json.loads(r["data"])
            target = (d.get("_source") or {}).get("originalUrl") or r["url_normalized"]
            stale.append((tbl, r["id"], target))
        print(f"{tbl}: {len(stale) - n_before} row(s) carrying archive.org's own "
              f"PA {ARCHIVE_PA:.0f}/DA {ARCHIVE_DA:.0f}")

    print(f"\n  to unwrap        : {len(planned)}")
    print(f"  to re-score only : {len(stale)}")
    print(f"  skipped          : {len(skipped)}")
    for tbl, rid, why, u in skipped:
        print(f"    {tbl}.{rid:<6} {why:<22} {u[:64]}")

    if planned:
        print("\n  sample:")
        for tbl, rid, arc, orig, _n, _d in planned[:5]:
            print(f"    {tbl}.{rid}\n      from {arc[:92]}\n      to   {orig[:92]}")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    Path(args.backup).parent.mkdir(parents=True, exist_ok=True)
    json.dump([{"table": t, **{k: r[k] for k in r.keys()}} for t, r in all_rows],
              open(args.backup, "w", encoding="utf-8"),
              indent=2, ensure_ascii=False, default=str)
    print(f"\nbacked up {len(all_rows)} row(s) -> {args.backup}")

    now = datetime.now(timezone.utc).isoformat()
    for tbl, rid, arc, orig, norm, d in planned:
        src = d.get("_source") or {}
        src["archiveUrl"] = arc          # keep the exact snapshot we read
        src["originalUrl"] = orig        # identity = the publisher's page
        d["_source"] = src
        conn.execute(f"UPDATE {tbl} SET data = ?, url_normalized = ?, updated_at = ? "
                     "WHERE id = ?", (json.dumps(d, indent=2), norm, now, rid))
    conn.commit()
    print(f"unwrapped {len(planned)} row(s)")

    if args.rescore:
        from input.pipeline import url_scoring as us
        us.reset_moz_row_stats()
        # Both populations need the same repair — score the PUBLISHER url.
        # score_url_via_moz unwraps internally now, so either form works; pass
        # the unwrapped one so the log names the page we actually mean.
        targets = ([(t, rid, orig) for t, rid, _a, orig, _n, _d in planned]
                   + [(t, rid, u) for t, rid, u in stale])
        scored = cleared = 0
        for tbl, rid, target in targets:
            s = us.score_url_via_moz(target)
            row = conn.execute(f"SELECT data FROM {tbl} WHERE id = ?", (rid,)).fetchone()
            d = json.loads(row["data"])
            sc = d.get("_scoring") or {}
            if not s:
                # Moz has nothing for the publisher URL either. Do NOT leave
                # archive.org's numbers in place looking measured — clear them.
                # Absent is the honest state; a wrong number outranks a missing
                # one and never gets revisited. clear_moz_scores also drops the
                # paywall adjustment derived from the DA we just removed.
                us.clear_moz_scores(
                    sc, note=("cleared archive.org's authority (PA 47/DA 94) — that score "
                              "was Moz's reading of the Wayback snapshot, not of this "
                              "publisher; Moz has no data for the publisher URL"))
                d["_scoring"] = sc
                conn.execute(f"UPDATE {tbl} SET data = ? WHERE id = ?",
                             (json.dumps(d, indent=2), rid))
                cleared += 1
                continue
            # ONE writer: PA/DA/OU + derived power + the mozHttpCode provenance.
            # No paywall stamp — a repair pass re-states measurements, and the
            # adjustment is re-stamped by the calibration job that owns it.
            us.apply_moz_scores(sc, s, stamp_paywall=False)
            d["_scoring"] = sc
            conn.execute(f"UPDATE {tbl} SET data = ? WHERE id = ?",
                         (json.dumps(d, indent=2), rid))
            scored += 1
        conn.commit()
        st = us.moz_row_stats()
        print(f"re-scored {scored}, cleared as unscoreable {cleared} "
              f"(moz rows {st['rows']}, uncrawled {st['uncrawled']})")

    print("integrity:", conn.execute("PRAGMA quick_check").fetchone()[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
