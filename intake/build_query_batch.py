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

import numpy as np
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
# Step 3's fetch is now shared with step 7's extract via the canonical
# `fetch_with_ua_fallback`. Both go through the SAME UA chain so a URL
# that the extract can fetch will always pass step 3's filter — no
# more silent drops from UA mismatch. See [[single-path]].
from to_markdown.html_to_markdown import fetch_with_ua_fallback, fetch_with_full_fallback

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
    failure (HTTP error, timeout, parse error). Uses the canonical
    fetch_with_full_fallback (UA chain → Wayback Machine) so step 3's
    filter sees the same response the step-7 extract would — no
    UA-mismatch silent drops, and aggressive Cloudflare-fronted sites
    (Kitchn, NYT, WaPo) get caught via Wayback snapshot rather than
    dropping out of the batch entirely."""
    try:
        resp, _meta = fetch_with_full_fallback(url, timeout=FETCH_TIMEOUT_S)
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


_MIN_FIT_N = 25  # Floor below which the regression isn't worth running
                  # (overfitting noise at small N). Falls back to whatever
                  # OU score_url_via_moz already computed via the global
                  # formula.

# === Exceptionalism grading ===
# T-score transformation of OU residuals into a school-style letter grade.
# Math: for each entry, grade_score = (OU / σ_effective) * 10 + 75.
# Base 75 (not 50) reflects the qualified cohort — even rank-100 passed
# SerpAPI's top organic + our domain blacklist + is_recipe filter + Moz
# OU>0 floor, so "average" is closer to B than to C/D.
#
# σ_effective = max(σ_observed, EXC_SIGMA_FLOOR). The floor prevents tight
# cohorts from auto-creating A+'s: when residuals cluster very tight, a
# tiny absolute lead becomes a huge z-score, which over-rewards small
# differences. 0.5 OU is roughly the noise band we've observed between
# back-to-back Moz refreshes on the same URL.
EXC_SIGMA_FLOOR = 0.5
EXC_BASE = 75.0
EXC_SIGMA_MULT = 10.0

# Grade buckets in descending threshold order. The score floor for each
# letter is the bucket's MIN — anything >= floor and < next-higher gets
# that letter. Mirrors a standard 4.0-scale boundary (A-/B+ at 87.5/82.5
# etc.) — 0.5σ wide buckets in T-score space.
_EXC_GRADE_BUCKETS = [
    (97.5, "A+"),
    (92.5, "A"),
    (87.5, "A-"),
    (82.5, "B+"),
    (77.5, "B"),
    (72.5, "B-"),
    (67.5, "C+"),
    (62.5, "C"),
    (57.5, "C-"),
    (52.5, "D+"),
    (47.5, "D"),
    (42.5, "D-"),
]


def _score_to_grade(score: float) -> str:
    """Return the letter grade for a T-score. Below the lowest bucket
    floor (42.5) returns 'F'. Score is assumed to be a finite number."""
    for floor, letter in _EXC_GRADE_BUCKETS:
        if score >= floor:
            return letter
    return "F"


def _r_squared(y_actual: np.ndarray, y_predicted: np.ndarray) -> float:
    """Coefficient of determination. 1.0 = perfect fit; 0.0 = no
    explanatory power vs the mean; can go negative if the model is
    worse than just predicting the mean."""
    ss_res = float(np.sum((y_actual - y_predicted) ** 2))
    ss_tot = float(np.sum((y_actual - y_actual.mean()) ** 2))
    if ss_tot <= 0:
        return 0.0   # all y values identical — degenerate
    return 1.0 - (ss_res / ss_tot)


def _compute_custom_ou(entries: list[dict]) -> dict:
    """Fit a regression of PA on DA across the URLs in THIS dish's batch,
    then recompute each entry's OU as `actual_PA - predicted_PA(DA)`.

    Replaces the global static OU formula (`-3.0273 * DA^0.6034 + PA`)
    that score_url_via_moz uses for single-recipe scoring. The static
    formula was fit on a broad sample of websites; the PA-vs-DA shape
    varies meaningfully by topic, so refitting per-dish surfaces the
    pages that genuinely outperform their domain *within this category*.

    Tries both linear (d=1) and quadratic (d=2) fits, picks the one with
    the higher R² for use, logs both for transparency. Coefficients are
    NOT persisted — fit is ephemeral per refresh.

    Returns a metadata dict (degree, coefficients, R², n) for the
    pipeline counts. The entries list is mutated in place: each entry's
    `ou` value is overwritten with the per-dish custom residual.

    If N < _MIN_FIT_N (25), the fit is skipped and the existing OU
    values (from the global formula) are left in place — a regression
    on a handful of points overfits to noise rather than identifying
    real exceptions.
    """
    # Collect (da, pa) for entries that have both. Some Moz responses
    # have one or the other as None — skip those for the fit, but they
    # still pass through with their existing OU.
    da_vals, pa_vals, fit_indices = [], [], []
    for i, e in enumerate(entries):
        da, pa = e.get("da"), e.get("pa")
        if isinstance(da, (int, float)) and isinstance(pa, (int, float)):
            da_vals.append(float(da))
            pa_vals.append(float(pa))
            fit_indices.append(i)

    n = len(da_vals)
    if n < _MIN_FIT_N:
        print(f"      -> SKIP custom-OU fit: only {n} URLs with PA+DA "
              f"(need >={_MIN_FIT_N}); using global OU formula values "
              f"already computed by Moz step")
        return {"used": False, "n": n, "reason": "below_min_n"}

    da_arr = np.array(da_vals)
    pa_arr = np.array(pa_vals)

    # === Candidate 1: linear  PA = m*DA + b  ===
    coeffs_lin = np.polyfit(da_arr, pa_arr, 1)
    pred_lin = np.polyval(coeffs_lin, da_arr)
    r2_lin = _r_squared(pa_arr, pred_lin)

    # === Candidate 2: quadratic  PA = a*DA^2 + b*DA + c  ===
    coeffs_quad = np.polyfit(da_arr, pa_arr, 2)
    pred_quad = np.polyval(coeffs_quad, da_arr)
    r2_quad = _r_squared(pa_arr, pred_quad)

    # === Candidate 3: power  PA = a*DA^b  ===
    # This is the form Moz's own published formula uses
    # (-3.0273 * DA^0.6034 + PA), so we test it on the current dish too.
    # Power isn't linear in (a, b), but log(PA) = log(a) + b*log(DA) IS
    # linear in (log a, b) — fit via polyfit on log-transformed inputs.
    # Mask out non-positive values where log is undefined.
    pos_mask = (da_arr > 0) & (pa_arr > 0)
    if pos_mask.sum() >= _MIN_FIT_N:
        log_da = np.log(da_arr[pos_mask])
        log_pa = np.log(pa_arr[pos_mask])
        slope, intercept = np.polyfit(log_da, log_pa, 1)
        pwr_a = float(np.exp(intercept))
        pwr_b = float(slope)
        # Compute predicted PA on the ORIGINAL scale across ALL points
        # so R² is comparable to the polynomial fits (DA=0 points
        # predict PA=0, which is the right limit for a power model).
        pred_pwr = np.where(da_arr > 0, pwr_a * (np.maximum(da_arr, 1e-9) ** pwr_b), 0.0)
        r2_pwr = _r_squared(pa_arr, pred_pwr)
        power_available = True
    else:
        pwr_a, pwr_b, r2_pwr, pred_pwr, power_available = 0.0, 0.0, float("-inf"), None, False

    # Pick the model with the highest R². Power and quadratic can each
    # beat linear depending on the dish's PA-DA shape; we let the data
    # choose.
    candidates = [("linear",    r2_lin,  coeffs_lin,  pred_lin),
                  ("quadratic", r2_quad, coeffs_quad, pred_quad)]
    if power_available:
        candidates.append(("power", r2_pwr, np.array([pwr_a, pwr_b]), pred_pwr))
    chosen_name, chosen_r2, chosen_coeffs, chosen_pred = max(candidates, key=lambda c: c[1])

    # Pretty-print the chosen model + the full comparison.
    if chosen_name == "quadratic":
        a, b, c = chosen_coeffs
        formula = f"predicted_PA = {a:+.4f}*DA^2 {b:+.4f}*DA {c:+.4f}"
    elif chosen_name == "linear":
        m, b = chosen_coeffs
        formula = f"predicted_PA = {m:+.4f}*DA {b:+.4f}"
    else:  # power
        a, b = chosen_coeffs
        formula = f"predicted_PA = {a:.4f} * DA^{b:.4f}"
    pwr_str = f"{r2_pwr:.4f}" if power_available else "n/a"
    print(f"      n={n}  R²: linear={r2_lin:.4f}  quad={r2_quad:.4f}  "
          f"power={pwr_str}  -> chose {chosen_name}")
    print(f"      {formula}")

    # Rewrite OU per entry: residual against the chosen model. Entries
    # skipped from the fit (missing PA or DA) keep their existing OU
    # value from the Moz step.
    residuals = np.zeros(n)
    for fit_idx, e_idx in enumerate(fit_indices):
        actual_pa = pa_vals[fit_idx]
        predicted_pa = float(chosen_pred[fit_idx])
        residual = actual_pa - predicted_pa
        residuals[fit_idx] = residual
        entries[e_idx]["ou"] = residual
        entries[e_idx]["_ou_predicted_pa"] = predicted_pa  # debug crumb

    # === Exceptionalism grade ===
    # Compute σ across all post-fit residuals, apply floor, T-score each
    # entry, and stamp the grade. Residual mean is ~0 by construction
    # (polyfit produces zero-sum residuals on the fit set), so the
    # formula simplifies to (residual / σ_eff) * 10 + 75.
    sigma_observed = float(np.std(residuals, ddof=0))
    sigma_effective = max(sigma_observed, EXC_SIGMA_FLOOR)
    residual_mean = float(np.mean(residuals))
    print(f"      σ_obs={sigma_observed:.4f}  σ_eff={sigma_effective:.4f} "
          f"(floor={EXC_SIGMA_FLOOR})  residual_mean={residual_mean:+.4f}")
    for fit_idx, e_idx in enumerate(fit_indices):
        residual = float(residuals[fit_idx])
        score = (residual / sigma_effective) * EXC_SIGMA_MULT + EXC_BASE
        entries[e_idx]["exceptionalism"] = {
            "score": round(score, 2),
            "grade": _score_to_grade(score),
            "basis": {
                "model": chosen_name,
                "sigma_effective": round(sigma_effective, 4),
                "sigma_observed": round(sigma_observed, 4),
                "n": n,
            },
        }

    return {
        "used": True,
        "n": n,
        "model": chosen_name,
        "r2_linear": r2_lin,
        "r2_quadratic": r2_quad,
        "r2_power": r2_pwr if power_available else None,
        "r2_chosen": chosen_r2,
        "coefficients": [float(x) for x in chosen_coeffs],
        # σ_effective is the cohort-wide grading scale. Persisted on the
        # dish row via last_ou_fit so future harvest-from-reject saves
        # can recompute Exceptionalism against the originating batch's
        # scale rather than today's cohort.
        "sigma_observed": round(sigma_observed, 4),
        "sigma_effective": round(sigma_effective, 4),
        "exc_base": EXC_BASE,
        "exc_sigma_mult": EXC_SIGMA_MULT,
        "exc_sigma_floor": EXC_SIGMA_FLOOR,
    }


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

    print(f"\n[5/7] custom OU fit (per-dish regression; "
          f"floor n>={_MIN_FIT_N}, else global formula)")
    ou_fit = _compute_custom_ou(entries)

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
    after_moz_post_fit = after_min_ou + len(dropped_low_ou)
    after_is_recipe = after_moz_post_fit + len(dropped_moz)
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
            "after_moz": after_moz_post_fit,
            "after_min_ou": after_min_ou,
            "final": len(final),
            "dropped_disallowed": len(dropped_disallowed),
            "dropped_not_recipe": len(dropped_not_recipe),
            "dropped_moz": len(dropped_moz),
            "dropped_low_ou": len(dropped_low_ou),
        },
        "ou_fit": ou_fit,
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
