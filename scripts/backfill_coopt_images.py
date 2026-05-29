"""One-shot backfill: coopt + locally host preview images for every
saved recipe so the form + dish tile stop hotlinking from source sites.

For each row in master_recipes + recipes:
  1. Pick the best available image URL to coopt:
       a) existing `_source.previewImage` (re-coopt at new 1200px size)
       b) `image[0]` from JSON-LD (the original hero photo URL)
  2. Run it through input.pipeline.image_pipeline.coopt_image:
       fetch → Pillow normalize → store via active backend
  3. Stamp the resulting URL on `_source.previewImage` and save back.

Pre-flight: wipes the existing `generated/og-thumbs/` directory so
600px artifacts from the prior pipeline version don't shadow the new
1200px backfill (the key hash is identical for the same source URL,
so without this clear the existence check would skip everything).

Skipped rows:
  - image[0] missing / empty
  - source URL returns non-image / 4xx / 5xx
  - Pillow can't decode the response

Idempotent on re-run: the second run sees `previewImage` already
points at the locally-hosted URL and skips. Use --force to re-coopt
anyway (e.g. after another size bump).

Usage:
  python -m scripts.backfill_coopt_images --dry-run
  python -m scripts.backfill_coopt_images --limit 5
  python -m scripts.backfill_coopt_images
  python -m scripts.backfill_coopt_images --force        # re-coopt every row
"""
from __future__ import annotations

import argparse
import json
import shutil
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

from bs4 import BeautifulSoup  # noqa: E402
from urllib.parse import urljoin  # noqa: E402

from input.pipeline.image_pipeline import coopt_image  # noqa: E402
from input.pipeline.image_store import get_image_store  # noqa: E402
from to_markdown.html_to_markdown import (  # noqa: E402
    fetch_with_full_fallback, extract_og_meta,
)

DB_PATH = str(PROJECT_ROOT / "recipes.db")
OG_THUMBS_DIR = PROJECT_ROOT / "generated" / "og-thumbs"


def _is_local_url(u: str) -> bool:
    """True when the URL points at our own image store (already
    cooped). Used so --force gates re-cooping our own thumbnails."""
    return u.startswith("/generated/") or u.startswith("/og-thumbs/")


def _pick_source(d: dict) -> str:
    """Return the best URL to coopt FROM, or empty string when no
    candidate exists. Prefer the original schema.org image[0] which is
    the actual source URL."""
    img = d.get("image")
    if isinstance(img, list):
        for item in img:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _fetch_og_image(source_url: str) -> str:
    """Re-fetch the source page and extract its og:image meta tag.
    Returns absolute URL or "" on any failure. Used by the
    high-quality backfill path to avoid cooping small JSON-LD
    thumbnails when the page has a properly-sized social card image.
    """
    if not source_url or not source_url.startswith(("http://", "https://")):
        return ""
    try:
        resp, _meta = fetch_with_full_fallback(source_url, timeout=20)
        if not (200 <= resp.status_code < 300):
            return ""
        soup = BeautifulSoup(resp.text, "lxml")
        return extract_og_image(soup, resp.url)
    except Exception:
        return ""


def _process_table(conn: sqlite3.Connection, table: str, *,
                   dry_run: bool, limit: int, force: bool) -> Counter:
    rows = conn.execute(
        f"SELECT id, recipe_id, data FROM {table} ORDER BY id"
    ).fetchall()
    print(f"--- {table}: {len(rows)} rows ---")
    counts: Counter = Counter()
    updated = 0
    t0 = time.perf_counter()

    for rid, _ruuid, dj in rows:
        try:
            d = json.loads(dj)
        except Exception:
            counts["error"] += 1
            continue

        src = d.get("_source") or {}
        existing = (src.get("previewImage") or "").strip()
        # Skip rows already on local store unless --force.
        if existing and _is_local_url(existing) and not force:
            counts["already-local"] += 1
            continue

        # PREFERRED: re-fetch source URL to get og:image (designed for
        # social cards at 1200×630 — typically larger + better
        # composed than JSON-LD image[0] which sometimes points at a
        # tiny WordPress thumbnail variant).
        original_url = (src.get("originalUrl") or "").strip()
        coopt_url = ""
        if original_url:
            coopt_url = _fetch_og_image(original_url)
        # FALLBACK: schema.org image[0] when the page didn't have
        # og:image, or when we couldn't re-fetch.
        if not coopt_url:
            coopt_url = _pick_source(d)
        if not coopt_url:
            counts["no-image"] += 1
            continue

        try:
            cooped = coopt_image(coopt_url)
        except Exception as e:
            print(f"  [error] id={rid}: {e}")
            counts["coopt-error"] += 1
            continue

        if not cooped:
            counts["coopt-failed"] += 1
            continue

        counts["cooped"] += 1
        src["previewImage"] = cooped
        d["_source"] = src

        if not dry_run:
            conn.execute(
                f"UPDATE {table} SET data = ?, "
                f"updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                f"WHERE id = ?",
                (json.dumps(d, indent=2), rid),
            )
            conn.commit()

        updated += 1
        if updated % 25 == 0:
            print(f"  ... {updated} cooped (latest id={rid})")
        if limit and updated >= limit:
            print(f"  reached limit ({limit})")
            break

    print(f"  {table} done: {dict(counts)} in {time.perf_counter()-t0:.1f}s")
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true",
                   help="re-coopt rows whose previewImage already points "
                        "at the local store")
    p.add_argument("--master-only", action="store_true")
    p.add_argument("--personal-only", action="store_true")
    p.add_argument("--keep-existing-thumbs", action="store_true",
                   help="skip the pre-flight wipe of generated/og-thumbs")
    args = p.parse_args()

    # Pre-flight wipe so the 600px thumbnails from the prior pipeline
    # version don't shadow the new 1200px coopt (cache key would
    # collide on the same source URL hash).
    if not args.keep_existing_thumbs and OG_THUMBS_DIR.exists():
        print(f"Wiping {OG_THUMBS_DIR} (old 600px thumbnails)")
        if not args.dry_run:
            shutil.rmtree(OG_THUMBS_DIR)

    # Verify the active image store is the expected one.
    store = get_image_store()
    print(f"Image store: {type(store).__name__}")

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
