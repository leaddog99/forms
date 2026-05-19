"""Batch ingestion: walk an `intake/context-<slug>.json` file and run
each accepted URL through the canonical extract pipeline, then save
the result via the existing /recipes endpoint.

Usage:
    python -m intake.process_batch intake/context-bananabread.json
    python -m intake.process_batch intake/context-bananabread.json --dry-run
    python -m intake.process_batch intake/context-bananabread.json --limit 3

Conventions:
    - The batch's identity (dish name) is encoded in the filename:
      'context-bananabread.json' -> 'Banana Bread'. Once upstream batches
      start emitting an explicit dish-name field, prefer that.
    - URLs are processed in rank order (rank.value ascending: 1 is the
      best candidate, picked first).
    - Per-URL Moz scores (pa/da/ou + rootDomain + rawTitle) are passed
      to extract via `pre_scored=` so we don't re-burn Moz API quota on
      values the upstream batch pipeline already produced.
    - Any field the batch JSON declares authoritatively (chapter,
      subchapter, ethnicity, ...) is applied via `batch_overrides=`
      AFTER extract+enrich, so it wins over inferred values. These
      fields aren't in today's JSON yet, but the code is wired to honor
      them the moment they appear.
    - Save goes through HTTP POST /recipes so we use the same
      validation/sanitize/save path the interactive form uses. Requires
      the FastAPI server to be running on API_BASE.

The script is idempotent at the save endpoint level: /recipes upserts
on (originalUrl, user_id), so a re-run on the same batch will refresh
existing recipes rather than duplicate them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests


API_BASE = "http://127.0.0.1:8009"
SAVE_TIMEOUT_S = 30
PER_URL_RETRY_DELAY_S = 2


def derive_dish_name(path: Path) -> str:
    """`context-bananabread.json` -> `Banana Bread`.

    Strips the `context-` prefix, splits on camelCase/Pascal/snake/kebab
    boundaries, title-cases each word. Imperfect (it'll mis-cap proper
    nouns like 'McDonald'), but the batch slug isn't meant to be
    authoritative — once the JSON ships its own `dish_name` field, prefer
    that.
    """
    stem = path.stem
    if stem.startswith("context-"):
        stem = stem[len("context-"):]
    # Try delimiter splits first (kebab/snake), then a fall-back fuzzy split
    # for runs-of-lowercase-letters when the slug is all-lowercase.
    parts = re.split(r"[-_\s]+", stem)
    if len(parts) == 1:
        # Crude word-split: looks for an "and" or treats CamelCase boundaries.
        parts = re.findall(r"[A-Z][a-z]*|[a-z]+", stem) or [stem]
    return " ".join(p.capitalize() for p in parts if p)


def _val(field: Any) -> Any:
    """Pull `.value` from the {value, history} pattern the batch uses.
    Returns the field as-is if it's not in that shape — so this works
    on both the audited shape and the flat-dict shape transparently."""
    if isinstance(field, dict) and "value" in field:
        return field["value"]
    return field


def normalize_batch(raw: Any) -> dict[str, dict]:
    """Accept either of the two batch shapes the upstream pipeline has
    emitted and return a uniform `{url: entry}` dict.

    Shape A (audited, e.g. context-bananabread.json):
        {url: {url, history, current_status, pa: {value, history}, ...}}
    Shape B (flat list, e.g. context-Spanakopita.json):
        [{url, title, domain, rank, pa, da, ou}, ...]

    For Shape B we synthesize `current_status: 'accepted'` since the
    upstream's culling step has already excluded rejects (anything in
    the list is by definition a keeper).
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        out: dict[str, dict] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
                continue
            # Add a default current_status if absent; tolerate an explicit
            # one if a future flat-shape batch starts setting it.
            entry.setdefault("current_status", "accepted")
            out[url] = entry
        return out
    raise ValueError(f"Unrecognized batch JSON shape: {type(raw).__name__}")


def select_accepted_urls(batch: dict) -> list[tuple[int, str, dict]]:
    """Return [(rank, url, entry)] sorted by rank ascending, for entries
    whose current_status is 'accepted'. Unranked entries get pushed to
    the end (rank=9999) so they still run, just last."""
    out: list[tuple[int, str, dict]] = []
    for url, entry in batch.items():
        if not url.startswith(("http://", "https://")):
            continue  # batch-level metadata key, skip
        if not isinstance(entry, dict):
            continue
        if entry.get("current_status") != "accepted":
            continue
        rank = _val(entry.get("rank"))
        out.append((int(rank) if rank is not None else 9999, url, entry))
    out.sort(key=lambda t: t[0])
    return out


def pre_scored_from_entry(entry: dict) -> dict:
    """Translate the batch's {value, history} fields into the shape
    extract_recipe_from_url expects in `pre_scored=`."""
    out: dict[str, Any] = {}
    pa = _val(entry.get("pa"))
    da = _val(entry.get("da"))
    ou = _val(entry.get("ou"))
    domain = _val(entry.get("domain"))
    title = _val(entry.get("title"))
    if pa is not None:
        out["pageAuthority"] = float(pa)
    if da is not None:
        out["domainAuthority"] = float(da)
    if ou is not None:
        out["ouScore"] = float(ou)
    if domain:
        out["rootDomain"] = domain
    if title:
        out["rawTitle"] = title
    return out


def batch_overrides_for(
    entry: dict,
    batch_meta: dict,
    *,
    dish_name: str,
    batch_source: str,
    rank: int,
) -> dict:
    """Compose the authoritative-overrides dict the batch declares.

    Always stamps `_batch` (name + source + rank) so the recipe can be
    grouped with siblings in the same batch — and so the user can later
    add a paywalled/protected site MANUALLY and have it join the group
    by setting the same _batch.name.

    For fields the batch declares (chapter, subchapter, ethnicity, ...),
    reads from EITHER per-URL entry fields OR top-level batch metadata
    (batch_meta), with per-URL winning when both are set. Today only a
    handful of fields are recognized; add more here as the upstream
    pipeline starts emitting them.
    """
    overrides: dict[str, Any] = {
        # Grouping identity. _batch.name is the load-bearing field — it
        # lets us list "all Banana Bread recipes" later regardless of
        # which URL or manual entry produced each one.
        "_batch": {
            "name": dish_name,
            "source": batch_source,
            "rank": rank,
        },
    }

    def take(key: str) -> Any:
        v = _val(entry.get(key))
        if v in (None, ""):
            v = batch_meta.get(key)
        return v

    chapter = take("chapter")
    subchapter = take("subchapter")
    ethnicity = take("ethnicity")

    if chapter:
        overrides.setdefault("classification", {})["chapter"] = chapter
    if subchapter:
        # subchapter maps to hierarchyPath until we add a dedicated field.
        overrides.setdefault("classification", {})["hierarchyPath"] = subchapter
    if ethnicity:
        overrides.setdefault("provenance", {})["ethnicity"] = ethnicity

    return overrides


def extract_one(
    url: str,
    entry: dict,
    batch_meta: dict,
    *,
    dish_name: str,
    batch_source: str,
    rank: int,
) -> Optional[dict]:
    """Run the in-process extract callable. Returns the full result dict
    (with recipe / recipe_id / _timings) or None on failure.

    Extract failures are EXPECTED for paywalled / heavily-protected
    sites (NYT Cooking behind login, Washington Post, etc.) — the user
    handles those manually. Caller treats None as "expected miss",
    not "batch broken".
    """
    # Import lazily so --help and arg parsing don't pay the save_recipe_api
    # startup cost (DB init, model imports, etc.).
    from save_recipe_api import extract_recipe_from_url

    pre = pre_scored_from_entry(entry)
    overrides = batch_overrides_for(
        entry, batch_meta,
        dish_name=dish_name, batch_source=batch_source, rank=rank,
    )
    t0 = time.perf_counter()
    try:
        result = extract_recipe_from_url(url, pre_scored=pre, batch_overrides=overrides)
    except Exception as e:
        dt = time.perf_counter() - t0
        print(f"  [EXTRACT-MISS] {dt*1000:.0f}ms — {type(e).__name__}: {e}")
        print(f"                 (likely paywalled/protected; handle manually with _batch.name={dish_name!r})")
        return None
    dt = time.perf_counter() - t0
    path = result.get("_timings", {}).get("path", "?")
    name = (result.get("recipe") or {}).get("name") or "(no name)"
    print(f"  [EXTRACT-OK]   {dt*1000:.0f}ms path={path} name={name!r}")
    return result


def save_one(result: dict) -> bool:
    """POST the recipe to /recipes. Returns True on HTTP 200, prints
    detail on failure."""
    payload = dict(result["recipe"])
    payload["recipe_id"] = result["recipe_id"]
    try:
        r = requests.post(f"{API_BASE}/recipes", json=payload, timeout=SAVE_TIMEOUT_S)
    except Exception as e:
        print(f"  [SAVE-FAIL]    transport: {type(e).__name__}: {e}")
        return False
    if r.status_code == 200:
        print(f"  [SAVE-OK]      HTTP 200")
        return True
    print(f"  [SAVE-FAIL]    HTTP {r.status_code}: {r.text[:300]}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("batch", type=str, help="Path to intake/context-<slug>.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="Extract and report, but DO NOT save to recipes DB.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process only the top N ranked URLs (0 = all).")
    args = ap.parse_args()

    batch_path = Path(args.batch).resolve()
    if not batch_path.exists():
        print(f"[ERROR] Batch file not found: {batch_path}")
        return 2

    dish_name = derive_dish_name(batch_path)
    print(f"[BATCH] Dish: {dish_name!r}")
    print(f"[BATCH] File: {batch_path}")
    print(f"[BATCH] Mode: {'DRY-RUN (no save)' if args.dry_run else 'COMMIT (save to recipes.db via /recipes)'}")
    print()

    with batch_path.open(encoding="utf-8") as f:
        batch = normalize_batch(json.load(f))

    # Batch-level metadata isn't in the JSON yet, but reserve a hook so
    # when it shows up (chapter / subchapter / ethnicity at the batch
    # level, not per-URL), it gets picked up automatically. Today we
    # synthesize the dish name from the filename.
    batch_meta: dict[str, Any] = {}
    batch_meta["_dish_name_inferred"] = dish_name
    # (When the JSON gains a top-level field, hoist it here. E.g.:
    #  batch_meta["chapter"] = batch.get("_batch", {}).get("chapter"))

    queue = select_accepted_urls(batch)
    if args.limit > 0:
        queue = queue[: args.limit]
    print(f"[BATCH] {len(queue)} URLs queued (rank order):")
    for rank, url, _ in queue:
        print(f"  {rank:>2}. {url}")
    print()

    counts = {"extract_ok": 0, "extract_miss": 0, "save_ok": 0, "save_fail": 0, "save_skipped": 0}
    misses: list[tuple[int, str]] = []  # for the final paywall-list summary
    t_batch = time.perf_counter()

    for i, (rank, url, entry) in enumerate(queue, start=1):
        print(f"=== [{i}/{len(queue)}] rank={rank}  {url}")
        result = extract_one(
            url, entry, batch_meta,
            dish_name=dish_name,
            batch_source=batch_path.name,
            rank=rank,
        )
        if result is None:
            counts["extract_miss"] += 1
            misses.append((rank, url))
            continue
        counts["extract_ok"] += 1

        if args.dry_run:
            counts["save_skipped"] += 1
            print(f"  [SAVE-SKIP]    dry-run")
        else:
            if save_one(result):
                counts["save_ok"] += 1
            else:
                counts["save_fail"] += 1

    elapsed = time.perf_counter() - t_batch
    print()
    print(f"[BATCH] Done in {elapsed:.1f}s")
    print(f"[BATCH]   extracted: ok={counts['extract_ok']}  miss={counts['extract_miss']} (expected for paywalled/protected sites)")
    if args.dry_run:
        print(f"[BATCH]   saved:     skipped={counts['save_skipped']} (dry-run)")
    else:
        print(f"[BATCH]   saved:     ok={counts['save_ok']}  fail={counts['save_fail']}")
    if misses:
        print(f"\n[BATCH] Manual-handling list (extract missed — open in browser, save with _batch.name={dish_name!r}):")
        for rank, url in misses:
            print(f"  rank={rank}  {url}")
    # Only TRUE failures (save errors) flip exit code. Extract misses are
    # expected since the upstream pipeline can't see paywalls / anti-bot.
    return 0 if counts["save_fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
