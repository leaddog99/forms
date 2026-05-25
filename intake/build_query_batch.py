"""Batch front-end: query -> SerpAPI -> filter -> is_recipe -> Moz -> rank -> JSON.

One Python program that takes a search query (e.g. "spanakopita") and
produces a flat-shape batch JSON ready for `process_batch.py` to extract
and save into master_recipes.

Pipeline stages (all in-process, no separate uvicorn workers):
  1. SerpAPI Google query -> top N organic results
  2. filter_disallowed     -> drop reddit/youtube/pinterest/etc by domain
  3. is_recipe fetch+score -> fetch page, strip HTML, count recipe phrases,
                             drop URLs scoring below IS_RECIPE_THRESHOLD.
                             Runs BEFORE Moz so we don't burn quota on
                             pages that aren't recipes anyway.
  4. Moz scoring           -> PA / DA / OU per URL via the existing
                             input.pipeline.url_scoring.score_url_via_moz
  5. rank+cull             -> sort by OU descending, keep top N_final.
                             OU = -3.0273 * DA^0.6034 + PA (page beats
                             domain baseline = positive).

Usage:
    # Build the batch JSON and stop (user inspects before saving)
    python -m intake.build_query_batch "spanakopita" \\
      --out intake/context-spanakopita.json --top-serpapi 50 --top-final 20

    # Build AND immediately run extract+save into master_recipes
    python -m intake.build_query_batch "spanakopita" \\
      --out intake/context-spanakopita.json --run

Env requirements:
    SERPAPI_KEY                      (.env, used by SerpAPI step)
    MOZ_ACCESS_ID + MOZ_SECRET_KEY   (.env, used by Moz step)

Memory: per [[batch-single-program]], this lives in forms/intake/ as one
in-process program — no FastAPI workers in pipelineRecipes/. The
is_recipe filter is intentionally NOT applied to the live extract path;
see [[live-is-recipe-warn]] for the open warn-and-continue work.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# load_dotenv BEFORE importing url_scoring — that module reads
# MOZ_ACCESS_ID/MOZ_SECRET_KEY at import time. Same pattern as
# save_recipe_api.py's preamble.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv()

from input.pipeline.config import (  # noqa: E402
    DEFAULT_TOP_FINAL as _CFG_DEFAULT_TOP_FINAL,
    DEFAULT_TOP_SERPAPI_PER_QUERY as _CFG_DEFAULT_TOP_SERPAPI,
    DISALLOWED_DOMAINS,
    DISALLOWED_URL_PATH_FRAGMENTS,
    IS_RECIPE_THRESHOLD,
    MIN_DA_SCORE,
    MIN_OU_SCORE,
    SERPAPI_MAX_PAGES as _CFG_SERPAPI_MAX_PAGES,
)
from input.pipeline.url_scoring import score_url_via_moz                    # noqa: E402
from input.pipeline.url_utils import normalize_url, root_domain             # noqa: E402
from input.pipeline.validators import is_recipe, score_recipe_text          # noqa: E402


SERPAPI_KEY = os.getenv("SERPAPI_KEY")
SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SERPAPI_TIMEOUT_S = 30
FETCH_TIMEOUT_S = 10
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
    ),
}

# Defaults now come from bcc_config.json via input.pipeline.config.
# Re-exported under the historical names so existing CLI / callers
# keep working without import churn.
DEFAULT_TOP_SERPAPI = _CFG_DEFAULT_TOP_SERPAPI
DEFAULT_TOP_FINAL = _CFG_DEFAULT_TOP_FINAL


_SERPAPI_PAGE_SIZE = 10        # Google's organic results per page (protocol constant)
_SERPAPI_MAX_PAGES = _CFG_SERPAPI_MAX_PAGES  # safety cap from bcc_config.json


def _serpapi_lookup(query: str, target_n: int) -> list[dict]:
    """SerpAPI Google engine, paginated until we hit target_n or run out
    of organic results. Returns [{url, title, google_rank, domain}].

    Three improvements over a single-page call (the obvious mistake we
    hit on the beef-stew test, which returned only 7 of a requested 50):

      - **Pagination via `start`**: Google's first page is heavily
        decorated with featured snippets, People Also Ask, video rows,
        and recipe carousels — typically only 7-9 slots are actual
        organic links. Subsequent pages return more cleanly. Each page
        costs one SerpAPI quota unit; we cap at _SERPAPI_MAX_PAGES.
      - **Site-exclusion operators in the query**: appending
        `-site:youtube.com -site:wikipedia.org ...` keeps known-bad
        domains out of Google's results entirely, so organic slots go
        to real recipe sites instead of being burned and then
        post-filtered by us.
      - **Locale + dedup params**: `gl=us hl=en` pins to a stable SERP
        and `filter=0` disables Google's automatic similar-page
        collapsing for more candidate variety.
    """
    if not SERPAPI_KEY:
        raise RuntimeError("SERPAPI_KEY not set in .env")

    # Splice -site:domain operators into the query. Costs nothing extra
    # (still one quota unit per page) but stops Google from spending
    # organic slots on already-blocked domains.
    excluded = " ".join(f"-site:{d}" for d in sorted(DISALLOWED_DOMAINS))
    full_query = f"{query} {excluded}" if excluded else query

    out: list[dict] = []
    seen_urls: set[str] = set()
    for page in range(_SERPAPI_MAX_PAGES):
        if len(out) >= target_n:
            break
        start = page * _SERPAPI_PAGE_SIZE
        params = {
            "engine": "google",
            "q": full_query,
            "api_key": SERPAPI_KEY,
            "num": _SERPAPI_PAGE_SIZE,
            "start": start,
            "gl": "us",
            "hl": "en",
            "filter": "0",  # disable Google's auto-dedup
        }
        try:
            resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=SERPAPI_TIMEOUT_S)
            resp.raise_for_status()
            organic = (resp.json() or {}).get("organic_results", []) or []
        except Exception as e:
            print(f"  [SERPAPI] page {page+1} failed ({type(e).__name__}: {e}); stopping pagination")
            break

        if not organic:
            print(f"  [SERPAPI] page {page+1} returned 0 organic — stopping pagination")
            break

        added_this_page = 0
        for r in organic:
            url = r.get("link")
            if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
                continue
            if url in seen_urls:
                continue  # belt-and-suspenders against filter=0 surfacing dupes
            seen_urls.add(url)
            out.append({
                "url": url,
                "title": r.get("title") or "",
                "google_rank": r.get("position"),
                "domain": root_domain(url),
            })
            added_this_page += 1
        print(f"  [SERPAPI] page {page+1}: +{added_this_page} URLs (running total: {len(out)})")

    return out[:target_n]


def _multi_query_lookup(queries: list[str], top_n_per_query: int) -> list[dict]:
    """Run each query through _serpapi_lookup, union the results, dedup
    by normalized URL. Each surviving entry carries `_queries` (the list
    of query strings that surfaced it — usually 1, but a URL appearing in
    multiple queries' results is a stronger dish signal) and
    `google_rank` (the BEST position across queries that surfaced it).

    Designed for the multi-query dish case (e.g. "spaghetti with meat
    sauce" + "spaghetti and meat sauce" → one dish, broader funnel).
    A single-query call works too: list of one query, behaves
    identically to the prior single-query path.
    """
    # Per-URL accumulator. Key = normalize_url() of the result, so two
    # subtly different URLs (trailing slash, http vs https, query
    # tracking params) that point at the same canonical resource
    # dedupe correctly.
    by_norm: dict[str, dict] = {}
    for q_index, query in enumerate(queries):
        print(f"  [QUERY {q_index+1}/{len(queries)}] {query!r}")
        per_query_results = _serpapi_lookup(query, top_n_per_query)
        added, merged = 0, 0
        for entry in per_query_results:
            key = normalize_url(entry["url"]) or entry["url"]
            existing = by_norm.get(key)
            if existing is None:
                # First time we see this URL. Stamp the query list +
                # google_rank as the position from THIS query.
                entry["_queries"] = [query]
                by_norm[key] = entry
                added += 1
            else:
                # URL already came from a previous query. Merge:
                # - append this query to the queries list (a stronger
                #   signal — URL ranked for both phrasings)
                # - keep the better (lower) google_rank
                # - keep the longer title (paraphrased queries sometimes
                #   surface different title fragments; longer is usually
                #   more complete)
                existing.setdefault("_queries", []).append(query)
                this_rank = entry.get("google_rank")
                if this_rank is not None and (
                    existing.get("google_rank") is None
                    or this_rank < existing["google_rank"]
                ):
                    existing["google_rank"] = this_rank
                new_title = entry.get("title") or ""
                if len(new_title) > len(existing.get("title") or ""):
                    existing["title"] = new_title
                merged += 1
        print(f"     -> {added} new, {merged} merged with prior queries")

    out = list(by_norm.values())
    print(f"  [DEDUP] {len(out)} unique URLs across {len(queries)} queries")
    return out


def _filter_disallowed(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop entries by root-domain OR by URL-path substring. Both checks
    are zero-cost (set/substring membership), run before any HTTP fetch.

    Path fragments catch roundup/article patterns that survive
    is_recipe scoring (e.g. americastestkitchen.com/articles/24-the-
    best-beef-stew used the same recipe vocabulary as a real recipe
    page but is structurally an article)."""
    kept, dropped = [], []
    domain_block = {d.lower() for d in DISALLOWED_DOMAINS}
    path_block = {f.lower() for f in DISALLOWED_URL_PATH_FRAGMENTS}
    for e in entries:
        domain = (e.get("domain") or "").lower()
        if domain in domain_block:
            e["_dropped_reason"] = f"disallowed-domain:{e.get('domain')}"
            dropped.append(e)
            continue
        url_lower = (e.get("url") or "").lower()
        bad_frag = next((f for f in path_block if f in url_lower), None)
        if bad_frag:
            e["_dropped_reason"] = f"disallowed-path:{bad_frag}"
            dropped.append(e)
            continue
        kept.append(e)
    return kept, dropped


def _fetch_text(url: str) -> Optional[str]:
    """Fetch a URL and return lower-cased plain text. Returns None on any
    failure (HTTP error, timeout, parse error). No Playwright fallback —
    JS-rendered/paywalled pages get dropped, which is the right call for
    batch (we don't want them anyway)."""
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT_S, headers=FETCH_HEADERS)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ")
        # Collapse whitespace and lowercase so phrase matching in
        # score_recipe_text is consistent with markdown_to_recipe's path.
        return " ".join(text.split()).lower()
    except Exception:
        return None


def _is_recipe_filter(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Fetch each URL, run is_recipe on the page text, drop low-scoring.
    Stamps `recipe_score` on every entry (kept and dropped) so the user
    can see where the threshold landed. Threshold from
    input.pipeline.config.IS_RECIPE_THRESHOLD.

    Returns (kept, dropped).
    """
    kept, dropped = [], []
    for i, e in enumerate(entries, start=1):
        url = e["url"]
        text = _fetch_text(url)
        if text is None:
            e["recipe_score"] = 0
            e["_dropped_reason"] = "fetch-failed"
            dropped.append(e)
            print(f"  [{i:>2}/{len(entries)}] FETCH-FAIL  {url}")
            continue
        score = score_recipe_text(text)
        e["recipe_score"] = score
        if score >= IS_RECIPE_THRESHOLD:
            kept.append(e)
            print(f"  [{i:>2}/{len(entries)}] KEEP score={score:>2}  {url}")
        else:
            e["_dropped_reason"] = f"recipe-score<{IS_RECIPE_THRESHOLD}"
            dropped.append(e)
            print(f"  [{i:>2}/{len(entries)}] DROP score={score:>2}  {url}")
    return kept, dropped


def _moz_score(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Add pa/da/ou per URL via the existing in-process Moz helper.
    Drops URLs Moz can't score (missing credentials, no-such-page, etc).
    score_url_via_moz already computes ou internally."""
    kept, dropped = [], []
    for i, e in enumerate(entries, start=1):
        url = e["url"]
        scores = score_url_via_moz(url)
        if not scores:
            e["_dropped_reason"] = "moz-unavailable"
            dropped.append(e)
            print(f"  [{i:>2}/{len(entries)}] MOZ-FAIL    {url}")
            continue
        # score_url_via_moz returns snake_case keys
        # (page_authority/domain_authority/ou_score/raw_title) — not the
        # camelCase _scoring shape the recipe blob uses.
        e["pa"] = scores.get("page_authority")
        e["da"] = scores.get("domain_authority")
        e["ou"] = scores.get("ou_score")
        # Moz often has a better page title than SerpAPI; prefer Moz's
        # when present, otherwise keep what SerpAPI gave us.
        if scores.get("raw_title"):
            e["title"] = scores["raw_title"]
        kept.append(e)
        # Some Moz responses return one or more None scores even on a
        # successful call (page not in their crawl, etc). Render those
        # as '?' instead of crashing the print.
        def _fmt_num(v, width=3, decimals=None):
            if v is None:
                return "?".rjust(width)
            if decimals is None:
                return f"{v:>{width}}"
            return f"{v:>{width}.{decimals}f}"
        print(f"  [{i:>2}/{len(entries)}] MOZ-OK      "
              f"pa={_fmt_num(e['pa'])} da={_fmt_num(e['da'])} "
              f"ou={_fmt_num(e['ou'], width=6, decimals=2)}  {url}")
    return kept, dropped


def _min_da_filter(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop entries whose Moz domain_authority is below MIN_DA_SCORE
    (default 30.0 from bcc_config.json).

    Low-DA domains routinely surface low-quality recipes — even the
    enrichment's own editor commentary flags them. Dropping them before
    rank+cull keeps master_recipes statistically clean. Page-quality
    floor (separate from _min_ou_filter which drops pages that
    under-perform their domain baseline).
    """
    kept, dropped = [], []
    for i, e in enumerate(entries, start=1):
        da = e.get("da")
        if da is None or da < MIN_DA_SCORE:
            e["_dropped_reason"] = f"da<{MIN_DA_SCORE} (da={da})"
            dropped.append(e)
            da_disp = f"{da:.1f}" if isinstance(da, (int, float)) else str(da)
            print(f"  [{i:>2}/{len(entries)}] DA-DROP    da={da_disp:>5}  {e['url']}")
        else:
            kept.append(e)
    return kept, dropped


def _min_ou_filter(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Drop entries whose Moz OU score is below MIN_OU_SCORE (default 0.0).

    Negative OU is Moz literally saying the page under-performs its
    domain baseline — almost always a roundup or article rather than a
    hero recipe page. The americastestkitchen articles/24-the-best-
    beef-stew case (OU=-6.64, slipped through every other filter on
    the first beef stew run) is the motivating example. Page-quality
    floor — separate concern from rank_by_ou's top-N truncation.
    """
    kept, dropped = [], []
    for i, e in enumerate(entries, start=1):
        ou = e.get("ou")
        # None OU happens when Moz didn't return ou_score for the page;
        # treat as "can't decide quality" and drop. Stricter than the
        # rank step's -1e9 sentinel which only pushes them to the back.
        if ou is None or ou < MIN_OU_SCORE:
            e["_dropped_reason"] = f"ou<{MIN_OU_SCORE} (ou={ou})"
            dropped.append(e)
            ou_disp = f"{ou:.2f}" if isinstance(ou, (int, float)) else str(ou)
            print(f"  [{i:>2}/{len(entries)}] OU-DROP    ou={ou_disp}  {e['url']}")
        else:
            kept.append(e)
    return kept, dropped


def _rank_by_ou(entries: list[dict], top_n_final: int) -> list[dict]:
    """Sort by OU descending, keep top_n_final, stamp 1-indexed rank."""
    # None OU sorts to the bottom (treated as -inf so it always loses).
    entries_sorted = sorted(
        entries,
        key=lambda e: (e.get("ou") if e.get("ou") is not None else -1e9),
        reverse=True,
    )
    kept = entries_sorted[:top_n_final]
    for rank, e in enumerate(kept, start=1):
        e["rank"] = rank
    return kept


def build_batch(
    queries: list[str] | str,
    *,
    dish: Optional[str] = None,
    top_n_serpapi: int = DEFAULT_TOP_SERPAPI,
    top_n_final: int = DEFAULT_TOP_FINAL,
) -> dict:
    """Run the full front-end pipeline. Accepts a single query string OR
    a list of queries (the multi-query dish case — e.g. "spaghetti with
    meat sauce" AND "spaghetti and meat sauce" both feed one Spaghetti
    and Meat Sauce dish). Each query is run separately against SerpAPI;
    results are union-deduped before the rest of the pipeline runs.

    `dish` is the canonical name for the dish-library row. Required for
    multi-query (since neither phrasing alone is the right name);
    optional for single-query (defaults to the query string itself).
    Carried through to per-entry stamps for downstream consumption.
    """
    # Normalize single-string input to a list so the rest of the code
    # has one shape to reason about.
    if isinstance(queries, str):
        queries = [queries]
    queries = [q.strip() for q in queries if q and q.strip()]
    if not queries:
        raise ValueError("at least one non-empty query is required")
    if len(queries) > 1 and not dish:
        raise ValueError(
            "multiple queries require an explicit `dish` name — "
            "no single query string is canonical for the dish"
        )
    if dish is None:
        dish = queries[0]

    t0 = time.perf_counter()
    print(f"\n[1/7] SerpAPI lookup: dish={dish!r} queries={queries} "
          f"target_n_per_query={top_n_serpapi}")
    entries = _multi_query_lookup(queries, top_n_serpapi)
    serpapi_union = len(entries)
    print(f"      -> {serpapi_union} unique URLs across {len(queries)} "
          f"queries (paginated, site-exclusion applied)")

    print(f"\n[2/7] filter_disallowed (domain + URL-path blacklist)")
    entries, dropped_disallowed = _filter_disallowed(entries)
    print(f"      -> kept {len(entries)}, dropped {len(dropped_disallowed)}")

    print(f"\n[3/7] is_recipe fetch+score (threshold={IS_RECIPE_THRESHOLD})")
    entries, dropped_not_recipe = _is_recipe_filter(entries)
    print(f"      -> kept {len(entries)}, dropped {len(dropped_not_recipe)}")

    print(f"\n[4/7] Moz scoring on survivors")
    entries, dropped_moz = _moz_score(entries)
    print(f"      -> kept {len(entries)}, dropped {len(dropped_moz)}")

    print(f"\n[5/7] min-DA filter (>= {MIN_DA_SCORE})")
    entries, dropped_low_da = _min_da_filter(entries)
    print(f"      -> kept {len(entries)}, dropped {len(dropped_low_da)}")

    print(f"\n[6/7] min-OU filter (>= {MIN_OU_SCORE})")
    entries, dropped_low_ou = _min_ou_filter(entries)
    print(f"      -> kept {len(entries)}, dropped {len(dropped_low_ou)}")

    print(f"\n[7/7] rank by OU descending, keep top {top_n_final}")
    final = _rank_by_ou(entries, top_n_final)
    print(f"      -> final batch: {len(final)} URLs")

    elapsed = time.perf_counter() - t0
    print(f"\n[BATCH] Done in {elapsed:.1f}s")

    # Stamp the canonical dish name on every surviving entry so
    # downstream consumers (process_batch.py, eventually the
    # /master/refresh delete-and-replace logic) can key on it. See
    # the dish-library memo for the broader plan.
    for e in final:
        e["dish"] = dish

    after_min_ou = len(entries)
    after_min_da = after_min_ou + len(dropped_low_ou)
    after_moz = after_min_da + len(dropped_low_da)
    after_is_recipe = after_moz + len(dropped_moz)
    after_disallowed = after_is_recipe + len(dropped_not_recipe)
    return {
        "dish": dish,
        "queries": queries,
        "elapsed_s": elapsed,
        "counts": {
            "serpapi_per_query": top_n_serpapi,
            "num_queries": len(queries),
            "serpapi_union": serpapi_union,
            "after_disallowed": after_disallowed,
            "after_is_recipe": after_is_recipe,
            "after_moz": after_moz,
            "after_min_da": after_min_da,
            "after_min_ou": after_min_ou,
            "final": len(final),
            "dropped_disallowed": len(dropped_disallowed),
            "dropped_not_recipe": len(dropped_not_recipe),
            "dropped_moz": len(dropped_moz),
            "dropped_low_da": len(dropped_low_da),
            "dropped_low_ou": len(dropped_low_ou),
        },
        "entries": final,
    }


def to_flat_shape(entries: list[dict]) -> list[dict]:
    """Strip internal/dropped fields and emit the flat-shape JSON
    process_batch.py consumes. Keeps `recipe_score`, `dish`, and
    `_queries` because they're useful debug info; process_batch
    ignores fields it doesn't recognize."""
    out = []
    for e in entries:
        record = {
            "url": e["url"],
            "title": e.get("title", ""),
            "domain": e.get("domain", ""),
            "rank": e.get("rank"),
            "pa": e.get("pa"),
            "da": e.get("da"),
            "ou": e.get("ou"),
            "recipe_score": e.get("recipe_score"),
        }
        if e.get("google_rank") is not None:
            record["google_rank"] = e["google_rank"]
        if e.get("dish"):
            record["dish"] = e["dish"]
        if e.get("_queries"):
            record["queries"] = e["_queries"]
        out.append(record)
    return out


def write_batch_json(entries: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    flat = to_flat_shape(entries)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(flat, f, indent=2)
    print(f"\n[OUT] Wrote {len(flat)} entries to {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # Backwards-compat: a single positional query still works (matches
    # the original CLI shape). Multi-query callers use --query repeatedly.
    ap.add_argument("query", type=str, nargs="?",
                    help="(legacy) Single SerpAPI Google query — kept for "
                         "backwards compat; prefer --query/--dish for new use")
    ap.add_argument("--query", dest="queries", action="append", default=[],
                    help="SerpAPI Google query. Pass multiple times to union "
                         "queries for one dish (e.g. --query 'spaghetti with "
                         "meat sauce' --query 'spaghetti and meat sauce').")
    ap.add_argument("--dish", type=str, default=None,
                    help="Canonical dish name (required when --query is used "
                         "multiple times; defaults to the single query string "
                         "otherwise). Stamped on every saved row as the "
                         "dish-library join key.")
    ap.add_argument("--out", type=str, required=True,
                    help="Output path for the batch JSON (e.g. intake/context-spanakopita.json)")
    ap.add_argument("--top-serpapi", type=int, default=DEFAULT_TOP_SERPAPI,
                    help=f"SerpAPI top-N PER QUERY (default {DEFAULT_TOP_SERPAPI}). "
                         f"Total candidates ~= this * len(queries) before dedup.")
    ap.add_argument("--top-final", type=int, default=DEFAULT_TOP_FINAL,
                    help=f"Final ranked top-N to keep (default {DEFAULT_TOP_FINAL})")
    ap.add_argument("--run", action="store_true",
                    help="After writing the JSON, invoke intake.process_batch on it")
    args = ap.parse_args()

    # Merge positional and --query forms. At least one needs to be set.
    queries = list(args.queries)
    if args.query:
        queries.insert(0, args.query)  # positional comes first by convention
    if not queries:
        ap.error("at least one query is required (positional or --query)")

    result = build_batch(
        queries,
        dish=args.dish,
        top_n_serpapi=args.top_serpapi,
        top_n_final=args.top_final,
    )
    out_path = Path(args.out).resolve()
    write_batch_json(result["entries"], out_path)

    if args.run:
        print(f"\n[RUN] Invoking intake.process_batch on {out_path}")
        # Lazy import so non-run usage doesn't pay process_batch's startup cost.
        from intake.process_batch import main as run_process_batch
        # process_batch.main reads sys.argv directly; rewrite argv to point at
        # the just-written file and let it run.
        old_argv = sys.argv
        sys.argv = [old_argv[0], str(out_path)]
        try:
            return run_process_batch()
        finally:
            sys.argv = old_argv
    return 0


if __name__ == "__main__":
    sys.exit(main())
