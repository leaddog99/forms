"""Refresh PA/DA/OU on every recipe in recipes.db using the canonical-
URL-variant Moz call (the fix landed in forms commit 142911a /
pipelineRecipes b462377).

Recipes saved before that fix have under-scored PA because the old
`_url_variants` only toggled host (www-on/off) and never the trailing
slash; `normalize_url` strips the slash, so the Moz query missed the
canonical form for sites that canonicalize WITH the slash. Empirically
the gap was 13-17 PA points (natashaskitchen 41 vs 56, allrecipes 47
vs 63, etc.).

What this script does:
  - Group recipes by source URL
  - For each unique URL, call score_url_via_moz (which now probes all
    4 host/slash variants and picks the canonical-PA crawled result)
  - Update metabase_url cache + every recipe's embedded _scoring
  - Re-compute ouScore from the fresh PA/DA so it stays consistent

Defaults to dry-run. Pass --commit to actually write.

Usage:
    python backfill_url_scoring.py                # dry run, print deltas
    python backfill_url_scoring.py --commit       # write changes
    python backfill_url_scoring.py --commit --limit 10
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Script lives in scripts/; project root is parent.parent. The sys.path
# bootstrap lets `input.pipeline.url_scoring` (and other top-level
# packages) resolve regardless of where the script is invoked from.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from input.pipeline.url_scoring import (  # noqa: E402
    score_url_via_moz, _apply_moz_scores, ensure_metabase_url_table,
    normalize_url,
)

DB_PATH = str(PROJECT_ROOT / "recipes.db")


def gather_recipes(con: sqlite3.Connection) -> dict[str, list[tuple[int, dict]]]:
    """Return {normalized_url: [(id, recipe_dict), ...]}. Skips recipes
    with no source URL (handwritten / synthetic-self-URL records)."""
    out: dict[str, list[tuple[int, dict]]] = {}
    cur = con.execute("SELECT id, data FROM recipes")
    for id_, data in cur.fetchall():
        try:
            d = json.loads(data)
        except Exception as e:
            print(f"  [WARN] recipe id={id_} JSON parse failed: {e}")
            continue
        src = (d.get("_source") or {}).get("originalUrl") or ""
        if not src.startswith(("http://", "https://")):
            continue
        norm = normalize_url(src)
        if not norm:
            continue
        out.setdefault(norm, []).append((id_, d))
    return out


def refresh_url(con: sqlite3.Connection, url: str, dry_run: bool) -> tuple[dict | None, dict | None]:
    """Re-score `url` and write metabase_url + return the scores dict.
    Returns (old_meta, new_scores). old_meta is the prior metabase_url
    row dict (or None); new_scores is what score_url_via_moz returned
    (or None if Moz failed)."""
    ensure_metabase_url_table(con)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM metabase_url WHERE url = ?", (url,)).fetchone()
    old_meta = dict(row) if row else None

    new_scores = score_url_via_moz(url)
    if not new_scores:
        return old_meta, None

    if not dry_run:
        now = datetime.now(timezone.utc).isoformat()
        if old_meta is None:
            # Insert new row
            from input.pipeline.url_scoring import root_domain  # noqa
            con.execute(
                "INSERT INTO metabase_url (url, root_domain, raw_title, first_seen, last_accessed) VALUES (?, ?, ?, ?, ?)",
                (url, "", new_scores.get("raw_title", ""), now, now),
            )
        _apply_moz_scores(con, url, new_scores, now)
        con.commit()
    return old_meta, new_scores


def update_recipe_scoring(d: dict, scores: dict) -> tuple[float | None, float | None]:
    """Mutate d['_scoring'] with the fresh PA/DA/OU. Returns (old_pa, new_pa)
    so the caller can report deltas."""
    s = d.get("_scoring") or {}
    old_pa = s.get("pageAuthority")
    s["pageAuthority"] = scores["page_authority"]
    s["domainAuthority"] = scores["domain_authority"]
    s["ouScore"] = scores["ou_score"]
    if scores.get("raw_title") and not s.get("rawTitle"):
        s["rawTitle"] = scores["raw_title"]
    d["_scoring"] = s
    return old_pa, scores["page_authority"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", action="store_true",
                    help="Actually write changes (default is dry-run).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Only process the first N unique URLs (0 = all).")
    ap.add_argument("--db", default=DB_PATH, help=f"DB path (default {DB_PATH}).")
    args = ap.parse_args()

    print(f"[BACKFILL] DB: {args.db}")
    print(f"[BACKFILL] Mode: {'COMMIT' if args.commit else 'DRY-RUN (no writes)'}")

    con = sqlite3.connect(args.db)
    grouped = gather_recipes(con)
    print(f"[BACKFILL] {sum(len(v) for v in grouped.values())} recipes across {len(grouped)} unique URLs")

    items = list(grouped.items())
    if args.limit > 0:
        items = items[: args.limit]
        print(f"[BACKFILL] --limit {args.limit}: processing first {len(items)} URLs")
    print()

    rows: list[tuple[str, float | None, float | None, float | None]] = []
    skipped_moz = 0
    updated = 0
    t_start = time.perf_counter()

    for i, (url, recipes) in enumerate(items, 1):
        old_meta, new_scores = refresh_url(con, url, dry_run=not args.commit)
        if new_scores is None:
            print(f"[{i:>3}/{len(items)}] SKIP (Moz returned nothing): {url}")
            skipped_moz += 1
            continue

        new_pa = new_scores["page_authority"]
        old_pa_meta = old_meta.get("page_authority") if old_meta else None

        # Apply to each recipe sharing this URL.
        recipe_deltas = []
        for id_, d in recipes:
            old_pa, np = update_recipe_scoring(d, new_scores)
            recipe_deltas.append((id_, old_pa, np))
            if args.commit:
                con.execute("UPDATE recipes SET data = ? WHERE id = ?",
                            (json.dumps(d, indent=2), id_))
                updated += 1

        # Pick the most-different recipe's old PA for the report (they're
        # usually all the same, but handle the case where they aren't).
        old_pa = recipe_deltas[0][1] if recipe_deltas else None
        delta = (new_pa - old_pa) if old_pa is not None else None
        rows.append((url, old_pa, new_pa, delta))

        # Compact progress line
        delta_str = f"{delta:+.1f}" if delta is not None else "  n/a"
        meta_str = f"meta={old_pa_meta!s:>4}" if old_meta else "meta=new "
        n_recipes = len(recipes)
        print(f"[{i:>3}/{len(items)}] old_pa={old_pa!s:>5} new_pa={new_pa:>5} delta={delta_str:>6} {meta_str} ({n_recipes} recipe{'s' if n_recipes != 1 else ''}) {url[:70]}")

    if args.commit:
        con.commit()

    elapsed = time.perf_counter() - t_start

    # Summary: distribution of deltas
    deltas_only = [r[3] for r in rows if r[3] is not None]
    if deltas_only:
        deltas_only.sort()
        gained = [d for d in deltas_only if d > 0]
        lost = [d for d in deltas_only if d < 0]
        same = [d for d in deltas_only if d == 0]
        print()
        print(f"[BACKFILL] Done in {elapsed:.1f}s")
        print(f"[BACKFILL]   URLs scored:     {len(rows)}")
        print(f"[BACKFILL]   Moz skipped:     {skipped_moz}")
        print(f"[BACKFILL]   recipe rows updated: {updated}")
        print(f"[BACKFILL]   PA distribution:")
        print(f"[BACKFILL]     gained PA:  {len(gained)}  (mean +{sum(gained)/len(gained):.1f})" if gained else f"[BACKFILL]     gained PA:  0")
        print(f"[BACKFILL]     unchanged:  {len(same)}")
        print(f"[BACKFILL]     lost PA:    {len(lost)}  (mean {sum(lost)/len(lost):.1f})" if lost else f"[BACKFILL]     lost PA:    0")
        if gained:
            top = sorted([(d, u) for u, _, _, d in rows if d and d > 0], reverse=True)[:10]
            print(f"[BACKFILL]   Biggest gainers:")
            for d, u in top:
                print(f"[BACKFILL]     +{d:5.1f}  {u[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
