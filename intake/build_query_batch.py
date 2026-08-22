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
import random
import re
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import urlparse

import numpy as np
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
    POWER_BLEND_WEIGHT,
    SERPAPI_MAX_PAGES as _CFG_SERPAPI_MAX_PAGES,
)
from input.pipeline.blend import rank_by_blend                              # noqa: E402
from input.pipeline.url_scoring import score_url_via_moz                    # noqa: E402
from input.pipeline.url_utils import normalize_url, root_domain             # noqa: E402
from input.pipeline.validators import (is_recipe, score_recipe_text,          # noqa: E402
                                       score_recipe_text_lang, recipe_phrase_lang_available,
                                       lang_recipe_threshold, score_recipe_bilingual,
                                       normalize_lang, instance_base_language,
                                       has_recipe_structure)


SERPAPI_KEY = os.getenv("SERPAPI_KEY")
FETCH_TIMEOUT_S = 10
# Step 3's fetch is now shared with step 7's extract via the canonical
# `fetch_with_ua_fallback`. Both go through the SAME UA chain so a URL
# that the extract can fetch will always pass step 3's filter — no
# more silent drops from UA mismatch. See [[single-path]].
from to_markdown.html_to_markdown import (
    fetch_with_ua_fallback, fetch_with_full_fallback, extract_recipe_jsonld,
    fetch_via_unblocker, unblocker_available, blocked_reason, page_declares_recipe,
)
from intake.translate import (
    detect_language, translate_markdown, translate_title, is_translation_plausible,
    is_non_english, language_name,
)

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

    `query` is sent to Google VERBATIM — whatever the admin authored on
    the dish (quotes, `|` OR, `-site:` operators) goes straight into `q`.
    We do NOT quote, OR-join, or splice exclusions: Google's NOT/AND/OR
    precedence is unreliable (in testing, parenthesizing a quoted OR
    silently dragged back the off-topic banana-fruit results we were
    trying to exclude), so query construction belongs to the human who
    knows the dish. Domain blacklisting is handled deterministically
    downstream by _filter_disallowed instead.

    Two mechanics we DO own — they're *how we fetch*, not *what we search*:

      - **Pagination via `start`**: Google's first page is heavily
        decorated with featured snippets, People Also Ask, video rows,
        and recipe carousels — typically only 7-9 slots are actual
        organic links. Subsequent pages return more cleanly. Each page
        costs one SerpAPI quota unit; we cap at _SERPAPI_MAX_PAGES. A
        tight query just runs out of pages early — the caller surfaces
        the shortfall; there is NO silent fallback to a looser query
        (that would re-admit exactly the junk a tight query excludes).
      - **Locale + dedup params**: `gl=us hl=en` pins to a stable SERP
        and `filter=0` disables Google's automatic similar-page
        collapsing for more candidate variety.
    """
    from input.pipeline.serp_search import has_key, active_provider as _ap
    if not has_key():
        raise RuntimeError(f"No SERP key for active provider '{_ap()}' "
                           f"(set SERPAPI_KEY or SCALESERP_KEY in .env)")

    # Verbatim — with ONE normalization: smart/curly quotes → straight. An
    # editor/OS autocorrect turns "..." into "..." (“ ”), and Google
    # only honors STRAIGHT double-quotes as phrase delimiters — curly ones are
    # ignored, silently demoting a quoted query to a loose one (this is exactly
    # how a `"Banana Bread"` query still let healthline's banana article in).
    # Curly→straight preserves the admin's INTENT; everything else is verbatim.
    # Domain exclusion stays downstream in _filter_disallowed.
    full_query = (query or "").translate(str.maketrans({
        "“": '"', "”": '"', "‘": "'", "’": "'",
    }))

    # Append the domain blocklist as Google `-site:` exclusions so the SERP
    # itself filters known clutter (social/aggregators) — Google fills those
    # slots with usable recipe sites instead, so fewer rejects + more keeps per
    # page. This is the SAFE subset of query construction: `-site:DOMAIN` is a
    # clean, additive, domain-only operator that never touches the verbatim
    # search TERMS (the precedence trap the note above warns about), and
    # _filter_disallowed stays as the downstream safety net. Config-gated +
    # capped (Google's query length is finite). Coexists with a `site:gr` query.
    try:
        from input.pipeline import system_config as _cfg
        if _cfg.get_setting("serp_exclude_blocklist", True):
            from input.pipeline.domains_lib import parse_serp_exclusions
            cap = int(_cfg.get_setting("serp_max_exclusions", 18))
            domains, terms = parse_serp_exclusions()
            term_parts = [(f'-"{t}"' if " " in t else f"-{t}") for t in terms]
            parts = ([f"-site:{d}" for d in sorted(domains)] + term_parts)[:cap]
            if parts:
                full_query = (full_query + " " + " ".join(parts)).strip()
                nd = sum(1 for p in parts if p.startswith("-site:"))
                print(f"  [SERPAPI] +{len(parts)} query exclusions appended "
                      f"({nd} domains, {len(parts) - nd} terms)")
    except Exception as e:
        print(f"  [SERPAPI] query-exclusions skipped ({type(e).__name__}: {e})")

    # Fetch via the provider-agnostic chokepoint (SerpApi or Scale SERP, by config).
    # Query CONSTRUCTION (verbatim + curly-quote norm + exclusions, above) stays
    # here; only the HTTP fetch + pagination delegate. gl/hl/filter=0 preserved.
    from input.pipeline.serp_search import serp_search, active_provider
    results = serp_search(full_query, pages=_SERPAPI_MAX_PAGES, want=target_n, gl="us", hl="en")
    out: list[dict] = []
    for r in results:
        url = r.get("link")
        if not (isinstance(url, str) and url.startswith(("http://", "https://"))):
            continue
        out.append({
            "url": url,
            "title": r.get("title") or "",
            "google_rank": r.get("rank"),   # 1-based in returned order (dedup-stable)
            "domain": root_domain(url),
        })
    print(f"  [SERP:{active_provider()}] {len(out)} URLs (target {target_n})")
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
    # The domains table is the source of truth for blocking (allowed = 0);
    # the config list is unioned in as a safety net so nothing is lost if the
    # table hasn't been seeded yet. Both are root-domain grain, matching the
    # entry's root_domain(url).
    try:
        from input.pipeline.domains_lib import get_blocked_root_domains
        table_block = get_blocked_root_domains()
    except Exception:
        table_block = set()
    domain_block = {d.lower() for d in DISALLOWED_DOMAINS} | table_block
    # URL-path fragments: the LIVE editable list (system_config), falling back to the
    # config.py seed (imported as DISALLOWED_URL_PATH_FRAGMENTS) if unset/unreadable.
    try:
        from input.pipeline.system_config import get_setting as _gs
        _frags = _gs("disallowed_url_path_fragments", None)
        _frags = _frags if isinstance(_frags, (list, tuple)) and _frags else DISALLOWED_URL_PATH_FRAGMENTS
    except Exception:
        _frags = DISALLOWED_URL_PATH_FRAGMENTS
    path_block = {str(f).lower() for f in _frags}
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


class FilterFetch(NamedTuple):
    """The result of fetching one candidate for the is-recipe filter.

    Replaces a bare `Optional[tuple]`, where every failure collapsed to None and
    the caller could only write "fetch-failed" with no reason. That opacity is
    what let a captcha stub (HTTP 202, 213 bytes) be recorded as
    `no-recipe-structure` — a claim about content we never received.

    `ok` False ⇒ `failure` is a short human phrase saying WHY, suitable for both
    the run log and the ledger reason. `ok` True ⇒ the three signals are valid.
    """
    ok: bool
    text: str = ""
    has_recipe_jsonld: bool = False
    lang: str = ""
    source: str = "direct"
    failure: Optional[str] = None

    @classmethod
    def failed(cls, why: str) -> "FilterFetch":
        return cls(ok=False, failure=why)


def _fetch_for_filter(url: str, *, unblocker: bool = False,
                      render: bool = False) -> FilterFetch:
    """Fetch a URL and return a `FilterFetch` — either the filter signals or an
    explicit reason we could not get the page.

    A failure is NEVER anonymous: `ok=False` always carries `failure`, a phrase
    the run log and the ledger both print. This is the R7 restructure — the old
    bare-`None` return forced the caller to invent a verdict ("no recipe
    structure") for pages it had never actually received.

    Three signals returned in one round-trip when `ok`:
      - `text` — phrase-scored against RECIPE_PHRASES for the English-
                  language is_recipe check.
      - `has_recipe_jsonld` — True iff the page publishes a
                  schema.org/Recipe block. Language-agnostic, and a
                  STRONG positive signal: any page that declares itself
                  a Recipe via structured data is, by author intent,
                  a recipe. We accept those unconditionally; the phrase
                  check is the fallback for pages without JSON-LD.
      - `lang_code` — ISO 639-1 detected from <html lang>, Content-Language
                  header, or fasttext on body text. Used by
                  `_is_recipe_filter` to decide whether to translate
                  before phrase-scoring; also stamped on the entry as
                  `_lang` for downstream extraction-stage translation.

    The JSON-LD + language signal landed 2026-05-29 after a Spanakorizo
    dish refresh dropped 21 of 23 Greek-language sites — all of which
    publish JSON-LD Recipe but failed the English-only phrase scorer.
    Trusting JSON-LD when present unlocks every non-English recipe site
    that follows the schema.org convention (which is virtually all of
    them); translation handles the rest.

    Uses the canonical fetch_with_full_fallback (UA chain -> Wayback)
    so step 3's filter sees the same response step 7's extract would.
    """
    try:
        # R2: `render` is passed when the DOMAIN is already known to need a real
        # browser (render_required). Without it the filter always fetched static
        # first, got a JS shell, and escalated — paying twice on every page of a
        # domain we had ALREADY learned about. mark_render_required wrote that
        # fact; nothing read it at the point where it saves a credit.
        resp, _meta = fetch_with_full_fallback(
            url, timeout=FETCH_TIMEOUT_S, unblocker=unblocker, render=render)
    except Exception as ex:
        return FilterFetch.failed(f"{type(ex).__name__}: {str(ex)[:120]}")
    src = (_meta or {}).get("source") or "direct"
    # REFUSE a response we did not actually receive the page from, BEFORE scoring
    # it. fetch_with_full_fallback accepts any 2xx, so a challenge interstitial
    # arrives looking like a success; phrase-scoring it yields 0 and the caller
    # concludes "not a recipe" about a page it never saw. The detector for this
    # already existed (`blocked_reason`) but was consulted only when deciding
    # whether to spend on the paid tier — never on the path that writes the verdict.
    # strict=True: this decides the candidate's FATE, not whether to spend a
    # credit. An eager false positive here throws away a real recipe.
    why = blocked_reason(resp, strict=True)
    if why:
        return FilterFetch.failed(f"{why} [via {src}]")
    try:
        text, jsonld, lang = _response_to_filter_signals(resp)
    except Exception as ex:
        return FilterFetch.failed(f"parse failed — {type(ex).__name__}: {str(ex)[:100]}")
    # Carry the fetch SOURCE (direct | unblocker | wayback) so the candidate log
    # can show where each page's content actually came from.
    return FilterFetch(ok=True, text=text, has_recipe_jsonld=jsonld,
                       lang=lang, source=src)


def _response_to_filter_signals(resp) -> tuple[str, bool, str]:
    """Derive the three filter signals (lower-cased visible text, has_recipe_jsonld,
    lang_code) from an already-fetched response. Shared by `_fetch_for_filter` and
    the render-escalation path so both score the page exactly the same way."""
    # JSON-LD usually lives in <script type="application/ld+json"> blocks —
    # extract BEFORE we strip scripts for phrase-scoring text.
    #
    # `page_declares_recipe`, not `extract_recipe_jsonld`: this is the IS-IT-A-
    # RECIPE question, and it counts a GATED declaration too. 177milkstreet.com
    # publishes its Recipe JSON-LD in a <meta name="application/ld+json"> tag
    # with isAccessibleForFree=false; every page scored 0 structure and dropped
    # as "no-recipe-structure", which was simply false about the page. Whether
    # we can READ it is a later question, answered by the extractor on the full
    # markdown — which for this publisher usually succeeds (the Jordanian
    # flatbread came through complete: 3 ingredients, 4 steps).
    try:
        has_recipe_jsonld = page_declares_recipe(resp.text, resp.url)
    except Exception as e:
        print(f"      JSON-LD parse error for {getattr(resp, 'url', '?')!r}: {e}")
        has_recipe_jsonld = False

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    normalized_text = " ".join(soup.get_text(separator=" ").split())
    # Language detection runs on the visible text BEFORE we lower-case it;
    # fasttext is mixed-case aware. Header dict is passed in case the server set
    # Content-Language (some CDNs do).
    lang_code = detect_language(
        resp.text, headers=dict(resp.headers), visible_text=normalized_text,
    )
    # Phrase matching expects lower-cased text (RECIPE_PHRASES is all-lower).
    return normalized_text.lower(), has_recipe_jsonld, lang_code


# Backward-compat alias — keeps any caller that imports _fetch_text by
# name working. The new signature is preferred for new code.
def _fetch_text(url: str) -> Optional[str]:
    """Legacy single-return shim. Returns just the text component;
    callers needing JSON-LD detection should use _fetch_for_filter."""
    result = _fetch_for_filter(url)
    return result.text if result.ok else None


# Score we stamp on entries whose JSON-LD detection passes them
# without needing the phrase check. 100 is well above any phrase-derived
# score (the scorer caps in the 50s on rich English pages), making the
# stamp visually distinctive in the log + downstream display ("score=100
# means JSON-LD bypass; score=NN means phrase-derived").
_JSONLD_PASS_SCORE = 100

# A title whose MAIN part (before the site-name separator) contains the PLURAL word
# "recipes" is a collection / listicle / index page — "30 Greek Recipes", "Best
# Dinner Recipes", "Greek Recipes" — NOT a single dish. Singular "recipe" ("Authentic
# Tzatziki Recipe") is welcomed. Two deliberate scoping choices:
#   • LEADING segment only — so a brand suffix ("… | Simply Recipes", "… - Allrecipes")
#     doesn't trigger it. We split on the usual title separators.
#   • TITLE only, never the URL — a real recipe very commonly lives under a /recipes/
#     PATH prefix (Milk Street /recipes/hummus, Zimmern /recipes/popovers); a URL rule
#     would nuke real recipes.
# This is a HARD reject that overrides even a JSON-LD pass: a collection page can embed
# Recipe JSON-LD (that's exactly the leak), so the title verdict wins.
_TITLE_SEP_RE = re.compile(r"\s+(?:[|–—:•·]|-)\s+")
_PLURAL_RECIPES_RE = re.compile(r"\brecipes\b", re.IGNORECASE)

# Listicle signature: a COUNT (number) followed — within a couple of adjective words —
# by a plural noun ("10 Dinners", "30 Projects", "25 Easy Weeknight Pastas"). Serious
# Eats and similar don't separate recipes from roundups, so this is the tell. Two
# guards keep it off real recipes:
#   • a plural that is a TIME / MEASURE / COUNT unit is NOT a listicle head — so
#     "Tzatziki in 15 Minutes", "4 Ingredients Banana Bread", "Serves 4" survive.
#   • a bare-number token only — "5-Ingredient Dinner" tokenizes as one hyphenated
#     token, not a number, so it's never a listicle.
_LISTICLE_NUM_RE = re.compile(r"^\d{1,3}$")
_PLURAL_WORD_RE = re.compile(r"^[a-z][a-z'-]*[a-rt-z]s$", re.IGNORECASE)  # ends in s, not 'ss'/'us'-ish handled by stoplist
_LISTICLE_UNIT_STOP = {
    # time
    "minutes", "mins", "seconds", "secs", "hours", "hrs", "days", "weeks", "months", "years",
    # measure / serving / quantity
    "servings", "calories", "grams", "kg", "cups", "tablespoons", "tbsps", "teaspoons",
    "tsps", "ounces", "oz", "pounds", "lbs", "ml", "liters", "litres", "pieces", "slices",
    "portions", "ingredients", "calorie", "carbs", "macros",
}


def _looks_like_listicle(text: str) -> bool:
    """True if `text` has a `<number> … <plural-noun>` listicle head (not a unit/time)."""
    toks = re.findall(r"[A-Za-z0-9'\-]+", text)
    for i, t in enumerate(toks):
        if _LISTICLE_NUM_RE.match(t):
            for w in toks[i + 1:i + 4]:  # allow up to 2 adjectives between (10 Easy Dinners)
                if _PLURAL_WORD_RE.match(w) and w.lower() not in _LISTICLE_UNIT_STOP:
                    return True
    return False


def _looks_like_recipe_collection(title: str) -> bool:
    """True if `title` (an ENGLISH title) reads as a collection / listicle / index page,
    NOT a single dish. Two signals, both checked on the LEADING segment (before the
    site-name separator) so a brand suffix can't trigger it:
      1. the plural word 'recipes' ("30 Greek Recipes", "Greek Recipes"); and
      2. a number-then-plural listicle head ("10 Dinners", "30 Projects").
    Singular "recipe" ("Authentic Tzatziki Recipe") is welcomed. Non-English titles
    must be translated first (see `_english_title`) — the plural word differs per
    language ('recetas', 'συνταγές', 'ricette')."""
    if not title:
        return False
    lead = _TITLE_SEP_RE.split(title.strip(), 1)[0]
    return bool(_PLURAL_RECIPES_RE.search(lead)) or _looks_like_listicle(lead)


def _english_title(title: str, lang_code: str) -> str:
    """The English form of a SERP title for the collection check. English titles pass
    through; a non-English title is translated (small, cheap Haiku call — titles are a
    few tokens). Best-effort: a translation failure returns '' so we don't false-drop."""
    if not title:
        return ""
    if not is_non_english(lang_code):
        return title
    try:
        return translate_title(title, lang_code)
    except Exception as ex:
        print(f"      [collection-title] title-translate failed ({type(ex).__name__}); skipping check")
        return ""


def _is_recipe_filter(entries: list[dict], *, capture_source: str = "unknown",
                      capture_provenance: dict | None = None,
                      unblocker: bool = False, url_prefilter: bool = False,
                      keyword_prescreen: bool = False, prescreen_domain: str | None = None,
                      domain_lang: str | None = None,
                      exclude_words=None, should_cancel=None,
                      render_escalate: bool = True,
                      render: bool = False,
                      subject_words=None,
                      ) -> tuple[list[dict], list[dict]]:
    """Fetch each URL, decide is-this-a-recipe via JSON-LD first, then
    phrase check (with translation for non-English pages) as fallback.
    Stamps `recipe_score` and `_lang` on every entry.

    Every decision is also logged as a labeled training sample (best-effort,
    off the hot path, separate git-ignored training.db) via
    `intake.training_capture` — input content + signals + score + decision +
    reason — so we accumulate an is-recipe classifier dataset as a free
    byproduct. `capture_source`/`capture_provenance` tag which surface called us
    (dish_batch vs domain_harvest). See docs/corpus-ml-strategy.md.

    Decision tree:
      1. Fetch fails (HTTP / timeout / parse) -> DROP with reason 'fetch-failed'.
      2. Page publishes schema.org/Recipe JSON-LD -> KEEP with score=100.
         The site declared itself a recipe; we don't second-guess it.
         Catches non-English recipe sites where the phrase scorer would
         have dropped them on language alone — and skips translation cost
         entirely, since JSON-LD is language-agnostic.
      3. No JSON-LD, non-English page -> translate visible text via
         Haiku, then phrase-score against RECIPE_PHRASES. Catches
         non-English recipe sites that don't publish JSON-LD.
      4. No JSON-LD, English page -> phrase-score directly.
      5. KEEP if score >= IS_RECIPE_THRESHOLD, else DROP.

    `_lang` is stamped on every kept entry so downstream extraction can
    translate again at markdown_to_recipe time (one source of truth on
    language detection — filter and extract agree).

    Returns (kept, dropped).
    """
    # ε-EXPLORATION rate: a small random fraction of would-be url-prefilter SKIPS are let
    # through the full fetch+verify anyway, to mint UNBIASED ground-truth labels in the
    # region the filter skips (else the is-recipe classifier trains only on survivors and
    # re-learns the filter's blind spots). See docs/corpus-ml-strategy.md.
    explore_rate = 0.0
    if url_prefilter or keyword_prescreen:
        try:
            from input.pipeline.system_config import get_setting as _gs
            explore_rate = float(_gs("url_prefilter_explore_rate", 0.08) or 0.0)
        except Exception:
            explore_rate = 0.08

    # KEYWORD PRE-SCREEN (opt-in): one batched Haiku call up front classifies every
    # candidate recipe-vs-not from its URL slug + SEMrush Top Keyword, so confident
    # non-recipes are dropped BEFORE the (expensive) fetch + whole-page translation. Used
    # on non-English mixed publishers where the post-fetch path is a full-page translate
    # (~40 s/URL). Negative-only: only a 'not' verdict drops (in the loop below). Best-
    # effort — any failure leaves verdicts empty → nothing dropped. See docs/keyword-prescreen.md.
    _prescreen: dict = {}
    if keyword_prescreen and entries:
        try:
            from intake.url_prescreen import prescreen as _kw_prescreen
            kw_map = {}
            dom = prescreen_domain or (
                (capture_provenance or {}).get("domain") if capture_provenance else None)
            if dom:
                try:
                    from input.pipeline.dish_keywords import keywords_for_domain
                    kw_map = keywords_for_domain(dom)
                except Exception:
                    kw_map = {}
            _prescreen = _kw_prescreen([e["url"] for e in entries], keyword_map=kw_map)
            if _prescreen:
                _n_not = sum(1 for v in _prescreen.values() if v == "not")
                print(f"  [kw-prescreen] {len(_prescreen)} classified, {_n_not} 'not' "
                      f"(dropped pre-fetch){' + keywords' if kw_map else ' (slugs only)'}")
        except Exception as ex:
            print(f"  [kw-prescreen] skipped: {type(ex).__name__}: {ex}")

    # Scoring language:
    #  - DOMAIN-context call (publisher harvest, domain_lang is a string incl. '') → the
    #    curator-set domains.language (normalized, 'gr'→'el') is AUTHORITATIVE; '' (unspecified)
    #    defaults to the instance base language. Per-page auto-detect is NOT trusted here.
    #  - No-domain call (the multi-domain dish batch passes domain_lang=None) → there's no one
    #    domain, so fall back to each result's PER-PAGE detected language (computed in the loop).
    _base_lang = instance_base_language()
    _has_domain_ctx = domain_lang is not None
    _fixed_page_lang = (normalize_lang(domain_lang) or _base_lang) if _has_domain_ctx else None

    kept, dropped = [], []

    # Bounded render-escalation prerequisites for this run: the feature is on and
    # BYOK unblocker creds exist. WHICH entries may escalate is decided per-entry
    # (below) so a multi-domain dish batch can escalate only its render-flagged
    # domains, while a single-domain publisher harvest escalates the whole run.
    _render_ready = bool(render_escalate and unblocker_available())
    # Only escalate a page whose PLAIN fetch came back SUSPICIOUSLY THIN — the
    # signature of a JS shell (nav only, body injected client-side, e.g. Boston
    # Globe's ~2.2k-char shell). A page that returned its FULL text but simply
    # isn't a recipe (a news/restaurant feature) is NOT a shell — rendering it
    # again would only burn a credit to confirm what we already know. Configurable.
    _render_thin_chars = 3500
    # PROBE (the chicken-and-egg fix): a render escalation normally fires only for a
    # domain ALREADY known to need a real browser (render_required / unblocker). But a
    # FRESH JS site (e.g. delish.com) is never flagged until one of its pages is rescued
    # — so its every page comes back a thin stub, is dropped, and it's never learned.
    # When probing is on, a would-be-dropped THIN stub on a not-yet-eligible domain still
    # gets ONE render attempt; a success stamps `_render_escalated`, so the caller's
    # auto-learn (mark_render_required) flags the domain and the NEXT run renders it up
    # front. Bounded per-domain-per-run so a genuinely-thin non-recipe site can't burn
    # many credits confirming it's not a recipe. Configurable.
    _render_probe = True
    _render_probe_max = 3
    try:
        from input.pipeline.system_config import get_setting as _gs2
        _render_thin_chars = int(_gs2("render_escalate_thin_chars", 3500) or 3500)
        _render_probe = bool(_gs2("render_escalate_probe", True))
        _render_probe_max = int(_gs2("render_escalate_probe_max", 3) or 0)
    except Exception:
        pass
    _probe_counts: dict[str, int] = {}   # host -> probe escalations spent this run

    def _host_of(u: str) -> str:
        try:
            return u.split("//", 1)[1].split("/", 1)[0].lower() if "//" in u else ""
        except Exception:
            return ""

    # FILTER-STAGE translation cap: for a non-English page with no JSON-LD we translate the
    # visible text only to phrase-score keep-vs-drop — the EXTRACTION stage re-translates the
    # FULL page for the canonical recipe, so a cap here never degrades final quality; the only
    # risk is a false DROP of a recipe whose ingredients/method sit past the cut (long-intro
    # blogs). Translation latency is dominated by OUTPUT tokens, so capping the input ≈ caps the
    # cost. 0 = no cap (translate whole page). Configurable. See docs/keyword-prescreen.md.
    _xlate_max = 6000
    try:
        from input.pipeline.system_config import get_setting as _gs3
        _xlate_max = int(_gs3("filter_translate_max_chars", 6000) or 0)
    except Exception:
        pass

    def _render_rescue(e: dict, url: str, i: int) -> bool:
        """LAST RESORT for a JS-rendered publisher (e.g. Boston Globe) whose article
        body is injected client-side: the cheap render=False verify scored only the
        nav shell and would DROP a real recipe. Re-fetch this ONE page with a real
        browser (unblocker render=True) and re-evaluate. Bounded: fires only on a
        would-be drop, once per URL, only when the plain fetch looked like a THIN JS
        shell (a full non-recipe page is dropped without paying for a render), and only
        when the entry is either render-eligible (the harvest is unblocker-flagged OR the
        caller marked it `_allow_render`) OR — for a not-yet-learned domain — within the
        bounded PROBE budget (so a fresh JS site can be auto-learned). Returns True iff it
        now qualifies; on failure it leaves the caller's original verdict/score intact."""
        if not _render_ready:
            return False
        # R2: the capture was ALREADY the rendered variant (render_required domain, or
        # a prior escalation), so re-rendering asks the same question again. This is
        # what made 177milkstreet log "still scores N rendered" and then pay to
        # discover it a second time at extract.
        if e.get("_rendered_upfront") or e.get("_render_escalated"):
            return False
        # Skip pages that already returned their full text — they're genuinely not a
        # recipe, not a JS shell, so a render won't change the answer (saves a credit).
        if len(e.get("_cap_text") or "") >= _render_thin_chars:
            return False
        eligible = bool(unblocker or e.get("_allow_render"))
        probed = False
        if not eligible:
            # Domain isn't (yet) known to need a browser. PROBE this thin stub anyway,
            # bounded per-domain-per-run, so a fresh JS site (delish.com) can earn its
            # render_required flag on a success — otherwise it's dropped forever.
            if not (_render_probe and _render_probe_max):
                return False
            host = _host_of(url)
            if _probe_counts.get(host, 0) >= _render_probe_max:
                return False
            _probe_counts[host] = _probe_counts.get(host, 0) + 1
            probed = True
        try:
            # R1: go through fetch_with_full_fallback, NOT fetch_via_unblocker.
            # Only the former consults the page cache — the direct unblocker call
            # bypassed it in BOTH directions, so this rendered page was never
            # stored and the winner-extract had to fetch it again. Measured
            # 2026-08-13 on 177milkstreet: 54 unblocker calls for 20 URLs.
            # The harvest already wraps this whole phase in page_cache.enabled().
            resp2, _meta2 = fetch_with_full_fallback(
                url, timeout=FETCH_TIMEOUT_S, unblocker=True, render=True)
        except Exception as ex:
            print(f"      [render-escalate] {type(ex).__name__}: {ex}")
            return False
        if resp2 is None:
            return False
        # Same refusal as `_fetch_for_filter`: a rendered response can ALSO be a
        # challenge stub. Scoring it yields 0 and the caller would file the page
        # `no-recipe-structure` — the exact mislabel R7 exists to stop. Stamp the
        # reason so the drop site can say "blocked", not "not a recipe".
        why2 = blocked_reason(resp2, strict=True)
        if why2:
            e["_blocked_reason"] = f"{why2} [rendered]"
            print(f"      [render-escalate] {url}\n"
                  f"      why: {why2} — refusing to score a page we never received")
            return False
        try:
            text2, jsonld2, lang2 = _response_to_filter_signals(resp2)
        except Exception as ex:
            print(f"      [render-escalate] parse failed: {type(ex).__name__}: {ex}")
            return False
        n = len(entries)
        # Score the rendered page WITHOUT mutating e yet — only adopt it on success.
        used_translation = False
        if jsonld2:
            score = _JSONLD_PASS_SCORE
        else:
            scored = text2
            if is_non_english(lang2):
                try:
                    tr = translate_markdown(text2, lang2)
                    if is_translation_plausible(text2, tr.translated_markdown)[0]:
                        scored = tr.translated_markdown.lower()
                        used_translation = True
                except Exception:
                    pass
            score = score_recipe_text(scored)
        if jsonld2 or score >= IS_RECIPE_THRESHOLD:
            # SUCCESS — adopt the rendered signals (better content + training label).
            e["_cap_text"] = text2
            e["_lang"] = lang2
            e["_render_escalated"] = True
            e["recipe_score"] = score
            e["jsonld_recipe"] = bool(jsonld2)
            if used_translation:
                e["_translated_for_filter"] = True
            kept.append(e)
            tag = "json-ld" if jsonld2 else f"score={score:>2}"
            print(f"  [{i:>2}/{n}] {'unblocker':<9} KEEP {tag}* {url}  (render-{'probe' if probed else 'escalated'})")
            return True
        # FAILURE — quiet sub-note (NOT a decision line); the caller logs the drop
        # and keeps the original plain score. No mutation of e.
        print(f"      [render-escalate] {url} still scores {score} rendered — not a recipe")
        return False

    # TRUST-EXTRACTION hosts: publishers whose real recipes have an unconventional
    # structure the cheap gate + cascade wrongly drop (Boston Globe) — keep their
    # candidates past the structure gate + cascade catch → the extractor decodes them.
    try:
        from input.pipeline.domains_lib import get_trust_extraction_hosts
        _trust_hosts = get_trust_extraction_hosts()
    except Exception:
        _trust_hosts = set()

    for i, e in enumerate(entries, start=1):
        # Cooperative cancel: a long publisher harvest can be aborted between
        # candidates (each is a fetch + score, the slow unit). Raises up to the job
        # runner → status 'cancelled'.
        if should_cancel and should_cancel():
            from input.pipeline.jobs import JobCancelled
            raise JobCancelled(f"cancelled after {i - 1}/{len(entries)} candidates")
        url = e["url"]
        if _trust_hosts:
            _h = url.split("//", 1)[-1].split("/", 1)[0].lower()
            _h = _h[4:] if _h.startswith("www.") else _h
            if _h in _trust_hosts or (root_domain(url) or "").lower() in _trust_hosts:
                e["_trust_extraction"] = True
        # PER-DOMAIN EXCLUSION (admin-set, exclusionary): a URL whose path SECTION matches
        # one of this publisher's exclude words (bostonchefs: restaurant/chef/news) is the
        # site's own "not a recipe" taxonomy — skip outright, overriding any food word. No
        # ε-exploration (admin is certain). Distinct from the global, non-exclusionary food
        # gate below. See url_word_lists.url_excluded_by_domain.
        if exclude_words:
            from input.pipeline.url_word_lists import url_excluded_by_domain
            if url_excluded_by_domain(url, exclude_words):
                e["recipe_score"] = 0
                e["_dropped_reason"] = "domain-exclude"
                dropped.append(e)
                print(f"  [{i:>2}/{len(entries)}] EXCLUDE     {url}")
                continue
        # OPTIONAL URL-text skip (opt-in per caller) — drop URLs whose path names no
        # food/recipe word BEFORE the (paid) fetch, using the shared self-learning word
        # lists. The single canonical place for the pre-fetch skip, so the harvest AND
        # the dish batch share it. See input.pipeline.url_word_lists.
        if url_prefilter:
            from input.pipeline.url_word_lists import url_lacks_recipe_signal
            # subject_words = what this run is FOR. A dish refresh must never
            # pre-filter away the word it searched for: Tiramisu dropped 48 of 94
            # results before the fetch because `tiramisu` was not among the 2,085
            # learned food words (job 914, 2026-08-22).
            if url_lacks_recipe_signal(url, extra_food=subject_words):
                if random.random() < explore_rate:
                    # ε-exploration: verify this would-be-skip anyway → unbiased label.
                    e["_explore"] = True
                    print(f"  [{i:>2}/{len(entries)}] URL-EXPLORE {url}  (would-skip; verifying for training)")
                    # fall through to the normal fetch+verify+capture path below
                else:
                    e["recipe_score"] = 0
                    e["_dropped_reason"] = "url-prefilter"
                    dropped.append(e)
                    print(f"  [{i:>2}/{len(entries)}] URL-SKIP    {url}")
                    continue
        # Collection/listicle guard (English fast-path) — a plural-"recipes" SERP title
        # ("30 Greek Recipes") is an index page, not a dish. Drop BEFORE the fetch.
        # Non-English titles don't match the English word here; they're caught
        # post-fetch via the translated title (below).
        if _looks_like_recipe_collection(e.get("title", "")):
            e["recipe_score"] = 0
            e["_dropped_reason"] = "collection-title"
            dropped.append(e)
            print(f"  [{i:>2}/{len(entries)}] DROP collection {url}  (title: {e.get('title','')!r})")
            continue
        # KEYWORD PRE-SCREEN drop (negative-only): the up-front Haiku pass judged this URL a
        # clear non-recipe from its slug/keyword — skip the fetch + full-page translate. Same
        # ε-exploration as url_prefilter so we still mint unbiased labels in the skipped region.
        if keyword_prescreen and _prescreen.get(url) == "not":
            if random.random() < explore_rate:
                e["_explore"] = True
                print(f"  [{i:>2}/{len(entries)}] KW-EXPLORE  {url}  (would-skip; verifying for training)")
            else:
                e["recipe_score"] = 0
                e["_dropped_reason"] = "kw-prescreen"
                dropped.append(e)
                print(f"  [{i:>2}/{len(entries)}] KW-SKIP     {url}")
                continue
        # R2: render up front when this domain is ALREADY known to need a browser
        # (run-level `render` from the publisher's render_required, or the
        # per-entry `_allow_render` the dish batch stamps for render-eligible
        # hosts). Otherwise the static fetch is a guaranteed JS shell and the
        # escalation below pays for the same page a second time.
        _render_now = bool(render or e.get("_allow_render"))
        result = _fetch_for_filter(url, unblocker=unblocker, render=_render_now)
        if _render_now:
            # Already the rendered variant — the escalation has nothing left to try.
            # DELIBERATELY not `_render_escalated`: that flag drives the auto-learn
            # (mark_render_required) and must keep meaning "a render RESCUED this",
            # not "we started rendered because we already knew".
            e["_rendered_upfront"] = True
        if not result.ok:
            e["recipe_score"] = 0
            # The reason travels with the drop. `fetch-failed` keeps its prefix so
            # the ledger's _REASON_MAP still classifies it (longest-prefix match),
            # while the suffix says WHAT we saw — the difference between "this page
            # is not a recipe" and "we were served a captcha".
            e["_dropped_reason"] = f"fetch-failed: {result.failure}"
            dropped.append(e)
            # ASCII only in log lines: the job runner's stdout is cp1252 on this
            # host and box-drawing characters raise UnicodeEncodeError mid-harvest.
            print(f"  [{i:>2}/{len(entries)}] {'':<9} FETCH-FAIL  {url}")
            print(f"                        why: {result.failure}")
            continue
        text, has_recipe_jsonld, lang_code, src = (
            result.text, result.has_recipe_jsonld, result.lang, result.source)
        e["_fetch_source"] = src
        # Decision-line prefix carrying the fetch source (direct | unblocker | wayback),
        # so every post-fetch KEEP/DROP line shows where the content came from — aligned
        # in the same column as the [N/total] index.
        _dl = f"  [{i:>2}/{len(entries)}] {src:<9}"
        e["_lang"] = lang_code
        e["_cap_text"] = text  # transient: byproduct training capture (popped before return)
        # Effective scoring language for THIS page: the fixed domain language (publisher
        # harvest), else the per-page detected language (dish batch / no domain), else base.
        _eff_lang = _fixed_page_lang or normalize_lang(lang_code) or _base_lang

        # Collection guard (non-English) — the SERP title was in the source language,
        # so the English fast-path above couldn't see it. Translate the title now and
        # re-check. Runs BEFORE the JSON-LD branch so it overrides a JSON-LD pass (a
        # non-English listicle can carry Recipe JSON-LD too).
        if is_non_english(lang_code):
            title_en = _english_title(e.get("title", ""), lang_code)
            if _looks_like_recipe_collection(title_en):
                e["recipe_score"] = 0
                e["_dropped_reason"] = "collection-title"
                dropped.append(e)
                print(f"{_dl} DROP collection [{lang_code}] {url}  (en-title: {title_en!r})")
                continue

        if has_recipe_jsonld:
            # Author declared this a recipe via structured data. Trust it.
            # Language-agnostic; no translation needed at this stage.
            e["recipe_score"] = _JSONLD_PASS_SCORE
            e["jsonld_recipe"] = True
            kept.append(e)
            lang_tag = f" [{lang_code}]" if is_non_english(lang_code) else ""
            print(f"{_dl} KEEP json-ld   {url}{lang_tag}")
            continue

        # No JSON-LD. Score by the EFFECTIVE language (_eff_lang: domain language for a
        # publisher harvest, else the page's detected language for a dish batch). Two cases:
        #  (a) that language has a phrase pack (or == base) → score RAW text against base+page
        #      lists (no per-page translation). A site MIXES base + its own language in the
        #      recipe body/headers, so score_recipe_bilingual sums both disjoint vocabularies
        #      (cross hits ~0; when page==base it's just the base list once).
        #  (b) language ≠ base AND no pack → translate (capped) then phrase-score (legacy path).
        if recipe_phrase_lang_available(_eff_lang) or _eff_lang == _base_lang:
            # FREE structural gate: a real recipe has BOTH an ingredients section AND a method
            # section (bilingual, accent-insensitive). A vocabulary-rich GUIDE/tips article has
            # at most one → dropped. This replaces the raw phrase-COUNT threshold, which
            # false-kept verbose guides. recipe_score (the count) is still stamped for the
            # training record + ranking signal, but it's not the keep decision.
            score, thr = score_recipe_bilingual(text, _eff_lang)
            e["recipe_score"] = score
            e["jsonld_recipe"] = False
            e["_lang_phrase_scored"] = True
            tag = "" if _eff_lang == _base_lang else f" [{_eff_lang}]"
            if has_recipe_structure(text, _base_lang, _eff_lang):
                kept.append(e)
                print(f"{_dl} KEEP struct phrase={score:>2}{tag}  {url}")
            elif e.get("_trust_extraction"):
                # Per-domain trust override: this publisher's pages are known to
                # carry a real recipe that the cheap structure gate can't see
                # (unconventional markup — e.g. Boston Globe's story-format
                # recipes with no "Ingredients" heading). The full extractor
                # decodes them correctly, so keep the entry and let extraction
                # do the real parse. Safe because trust is granted per-host on
                # the domains master, typically paired with a SEMrush URL=recipe
                # filter that already constrains the candidate set. See
                # docs/recipe-candidate-pipeline.md (Stage 2, trust gate).
                kept.append(e)
                print(f"{_dl} KEEP trust  phrase={score:>2}{tag}  {url}")
            elif _render_rescue(e, url, i):
                continue
            elif e.get("_blocked_reason"):
                # The render escalation got a challenge stub, not the page. This is
                # an ACQUISITION failure, not a judgment about the content — it must
                # not be filed as "no recipe structure" (overturnable → salvageable).
                e["_dropped_reason"] = f"fetch-failed: {e['_blocked_reason']}"
                dropped.append(e)
                print(f"{_dl} FETCH-FAIL  {url}\n"
                      f"                        why: {e['_blocked_reason']}")
            else:
                e["_dropped_reason"] = "no-recipe-structure"
                dropped.append(e)
                print(f"{_dl} DROP no-struct phrase={score:>2}{tag}  {url}")
            continue

        # language ≠ base AND no phrase pack for it: translate the page's text (in its own
        # language), capped to _xlate_max for the filter only (extraction re-translates in full).
        xtext = text[:_xlate_max] if (_xlate_max and len(text) > _xlate_max) else text
        try:
            tr = translate_markdown(xtext, _eff_lang)
            ok, why = is_translation_plausible(xtext, tr.translated_markdown)
            if not ok:
                e["_dropped_reason"] = f"translation-suspect:{why}"
                dropped.append(e)
                print(f"{_dl} DROP xlate-bad {url} [{_eff_lang}: {why}]")
                continue
            score = score_recipe_text(tr.translated_markdown.lower())
            e["recipe_score"] = score
            e["jsonld_recipe"] = False
            e["_translated_for_filter"] = True
            if score >= IS_RECIPE_THRESHOLD:
                kept.append(e)
                print(f"{_dl} KEEP xlate={score:>2} [{_eff_lang}]  {url}")
            elif _render_rescue(e, url, i):
                continue
            else:
                e["_dropped_reason"] = f"recipe-score<{IS_RECIPE_THRESHOLD}"
                dropped.append(e)
                print(f"{_dl} DROP xlate={score:>2} [{_eff_lang}]  {url}")
        except Exception as ex:
            # Translation API failure -> raw phrase check rather than dropping. Logged loudly.
            print(f"      [translate] {type(ex).__name__}: {ex} -- falling back to raw phrase check")
            score = score_recipe_text(text)
            e["recipe_score"] = score
            e["jsonld_recipe"] = False
            e["_translation_failed"] = True
            if score >= IS_RECIPE_THRESHOLD:
                kept.append(e)
                print(f"{_dl} KEEP score={score:>2} [{_eff_lang}, xlate-fail]  {url}")
            elif _render_rescue(e, url, i):
                continue
            else:
                e["_dropped_reason"] = f"recipe-score<{IS_RECIPE_THRESHOLD}"
                dropped.append(e)
                print(f"{_dl} DROP score={score:>2} [{_eff_lang}, xlate-fail]  {url}")

    # LLM cascade (system_config `is_recipe_cascade_mode`: off | shadow | decide;
    # back-compat with the legacy `is_recipe_cascade_shadow` bool). shadow_classify
    # stamps a three-way verdict (recipe|not_recipe|poor_quality) on the gray zone
    # (content-bearing, non-JSON-LD candidates); capture (below) records BOTH the
    # HEURISTIC decision and the cascade verdict. In 'decide' mode we then re-partition
    # kept/dropped per the asymmetric rescue/catch policy — AFTER capture, so the
    # training label stays the heuristic's call. Best-effort. See docs/is-recipe-classifier.md.
    _casc_mode = "off"
    try:
        from intake.isrecipe_cascade import cascade_mode, shadow_classify
        _casc_mode = cascade_mode()
        if _casc_mode in ("shadow", "decide"):
            shadow_classify(kept + dropped)
    except Exception as _ex:  # never let the cascade break a harvest
        print(f"  [cascade] skipped ({type(_ex).__name__}: {_ex})")

    # Byproduct training-data capture (best-effort, off the hot path, separate
    # git-ignored training.db). One labeled sample per HEURISTIC decision + the cascade
    # verdict; then pop the transient content so it never leaks into the downstream JSON.
    try:
        from intake.training_capture import capture_samples
        capture_samples(kept, dropped, source=capture_source,
                        provenance=capture_provenance,
                        threshold=IS_RECIPE_THRESHOLD)
    except Exception:
        pass

    # DECIDE mode: now let the cascade override the heuristic (rescue lost recipes /
    # catch quality-leaks). Done AFTER capture so training keeps the heuristic label.
    if _casc_mode == "decide":
        try:
            from intake.isrecipe_cascade import apply_decide
            apply_decide(kept, dropped)
        except Exception as _ex:
            print(f"  [cascade-decide] skipped ({type(_ex).__name__}: {_ex})")
    for _e in kept:
        _e.pop("_cap_text", None)
    for _e in dropped:
        _e.pop("_cap_text", None)
    return kept, dropped


def _moz_score(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Add pa/da/ou per URL via the existing in-process Moz helper.
    Drops URLs Moz can't score (missing credentials, no-such-page, etc).
    score_url_via_moz already computes ou internally."""
    kept, dropped = [], []
    from input.pipeline import url_scoring as _us
    _us.reset_moz_row_stats()
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
        # Provenance travels WITH the pa it describes, so the row that lands in
        # the DB says whether its PA was measured or a placeholder.
        e["moz_http_code"] = scores.get("moz_http_code")
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
    _ms = _us.moz_row_stats()
    print(f"  [moz] rows: {_ms['rows']} billed for {_ms['calls']} URL(s) "
          f"(canonical-variant learning saved ~{_ms['saved_vs_4x']} rows vs the old 4-variant probe)")
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

    # Standardized on QUADRATIC (user call 2026-06-02). Quadratic won the
    # best-R² selection on every dish (24/24), its margin over linear was
    # tiny, and pinning the model (a) kills "model-flip jitter" — a dish's
    # grades lurching because best-R² flipped quad↔power on a trivial data
    # shift — and (b) gives the SQL scorer ONE fixed formula shape
    # (a*DA^2 + b*DA + c). Linear/power are still computed above and
    # reported below for transparency; they're just never chosen.
    chosen_name, chosen_r2, chosen_coeffs, chosen_pred = (
        "quadratic", r2_quad, coeffs_quad, pred_quad)

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
        e = entries[e_idx]
        # Paywall DA-adjustment: a gated publisher is scored against the bar for
        # the DA its WALLED SECTION behaves like, not the whole domain's. The
        # curve itself is fit on MEASURED (da, pa) — the observed relationship is
        # what we want to model; only this publisher's position on it moves.
        _eff_da = e.get("_da_eff")
        if _eff_da is not None and chosen_name in ("quadratic", "linear"):
            predicted_pa = float(np.polyval(chosen_coeffs, float(_eff_da)))
            e["_ou_da_effective"] = round(float(_eff_da), 2)   # provenance crumb
        else:
            predicted_pa = float(chosen_pred[fit_idx])
        residual = actual_pa - predicted_pa
        residuals[fit_idx] = residual
        e["ou"] = residual
        e["_ou_predicted_pa"] = predicted_pa  # debug crumb

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


# Two-letter TLDs that are used like generic gTLDs rather than as a real
# country signal — a `site:` operator on one of these is NOT treated as a
# foreign-locale batch. Any OTHER two-letter TLD (.gr, .it, .fr, .de, .es, …)
# in a `site:` operator IS.
_GENERIC_2L_TLDS = {"io", "co", "ai", "me", "tv", "cc", "ly", "fm", "gg", "app", "dev"}
_SITE_TLD_RE = re.compile(r"site:\s*(?:https?://)?(?:www\.)?[^\s/]*\.([a-z]{2})\b",
                          re.IGNORECASE)

# A query written in a NON-LATIN script is as strong a foreign-locale signal as
# `site:.gr` — it can only match foreign-language pages. Ranges cover the scripts
# we actually harvest in: Greek, Cyrillic, Hebrew, Arabic, Thai, the CJK block
# (Chinese/Japanese kanji), kana, and Hangul. Latin-with-accents (French,
# Spanish, Italian) is deliberately NOT here: those queries also match English
# pages, so the script tells us nothing. Those languages need the explicit dish
# locale field.
_NON_LATIN_RANGES = (
    (0x0370, 0x03FF),   # Greek
    (0x0400, 0x04FF),   # Cyrillic
    (0x0590, 0x05FF),   # Hebrew
    (0x0600, 0x06FF),   # Arabic
    (0x0E00, 0x0E7F),   # Thai
    (0x3040, 0x30FF),   # Hiragana + Katakana
    (0x3400, 0x4DBF),   # CJK Unified Extension A
    (0x4E00, 0x9FFF),   # CJK Unified
    (0xAC00, 0xD7AF),   # Hangul syllables
)


def _query_is_non_latin(q: str) -> bool:
    """True when the query carries characters from a non-Latin script."""
    return any(any(lo <= ord(ch) <= hi for lo, hi in _NON_LATIN_RANGES)
               for ch in (q or ""))


def _query_targets_foreign_country(queries: list[str]) -> bool:
    """TEMPORARY heuristic: True when a query can only be answered by foreign
    pages — either it pins results to a country via a `site:.<ccTLD>` operator
    (e.g. `site:.gr`), or it is WRITTEN in a non-Latin script (e.g. `担担面`).
    Such batches harvest low-authority foreign publishers that the
    global/US-calibrated OU baseline scores negative almost by construction, so
    the min-OU floor is relaxed for them (see `_min_ou_filter`).

    The script test was added 2026-08-14 after the Dan Dan Noodles run: the
    query was `担担面`, which pins results to Chinese-language pages just as
    hard as `site:.cn` would, but tripped none of the ccTLD checks. Both of
    that run's min-OU drops were its Chinese pages (OU -7.64 and -3.04) — the
    exact cull this relax exists to prevent.

    Replace once dishes carry an explicit locale/country field — see
    docs/dish-variants-membership.md §5/§7. Note this only ever relaxes a
    FLOOR; ranking still orders on OU, so a genuinely weak foreign page still
    loses on rank.
    """
    for q in queries or []:
        if _query_is_non_latin(q):
            return True
        for m in _SITE_TLD_RE.finditer(q or ""):
            tld = m.group(1).lower()
            if tld != "us" and tld not in _GENERIC_2L_TLDS:
                return True
    return False


def dish_source_language(dish: Optional[str]) -> str:
    """The ISO 639-1 language a dish harvests in, '' when unset or unknown.

    Read through a fresh read-only connection rather than threaded in as a
    parameter: `build_batch` already knows the dish NAME, and the name is the
    identity ([[feedback_cli_args_identity_not_query]]). Never raises — a
    missing table, a missing row or a missing column all mean "no locale
    stated", which is the same as English here.
    """
    if not dish:
        return ""
    try:
        import sqlite3
        with sqlite3.connect("file:recipes.db?mode=ro", uri=True, timeout=5) as conn:
            row = conn.execute(
                "SELECT source_language FROM dishes WHERE name = ?", (dish,)
            ).fetchone()
        return ((row[0] or "").strip().lower()[:2] if row else "")
    except Exception:
        return ""


def _batch_is_foreign_locale(queries: list[str], dish: Optional[str]) -> tuple[bool, str]:
    """(is_foreign, why) for this batch, stated fact first, guess second.

    Order matters. A dish that DECLARES `source_language` has been given an
    answer by the curator and no inference should be able to overrule it —
    including the negative case: a dish explicitly marked `en` is domestic even
    if some query happens to carry a `site:.gr` operator, because the operator
    might be scoping one sub-query of an English dish.
    """
    lang = dish_source_language(dish)
    if lang:
        # Compare against THIS instance's base language, not a hardcoded 'en' —
        # a Greek-hosted instance harvesting Greek dishes is domestic, and its
        # English dishes are the foreign ones ([[project_portable_package]]).
        base = (normalize_lang(instance_base_language()) or "en")[:2]
        return (lang != base, f"dish locale={lang}")
    return (_query_targets_foreign_country(queries), "inferred from query")


def _min_ou_filter(entries: list[dict], *,
                   drop_below_threshold: bool = True) -> tuple[list[dict], list[dict]]:
    """Drop entries whose Moz OU score is below MIN_OU_SCORE (default 0.0).

    Negative OU is Moz literally saying the page under-performs its
    domain baseline — almost always a roundup or article rather than a
    hero recipe page. The americastestkitchen articles/24-the-best-
    beef-stew case (OU=-6.64, slipped through every other filter on
    the first beef stew run) is the motivating example. Page-quality
    floor — separate concern from rank_by_ou's top-N truncation.

    `drop_below_threshold=False` relaxes the negative-OU floor for
    foreign-locale batches (e.g. `site:.gr`): the global/US-calibrated OU
    baseline scores low-authority foreign publishers negative almost by
    construction, so the floor would cull genuine hero recipes purely for
    being non-US. Unscoreable (None) entries are still dropped — ranking
    needs an OU. TEMPORARY until foreign cohorts get their own fit
    (docs/dish-variants-membership.md §5).
    """
    kept, dropped = [], []
    for i, e in enumerate(entries, start=1):
        ou = e.get("ou")
        # None OU happens when Moz didn't return ou_score for the page;
        # treat as "can't decide quality" and drop (always — ranking needs
        # an OU). The negative-threshold cut is what the relax flag governs.
        unscoreable = ou is None
        below = (not unscoreable) and drop_below_threshold and ou < MIN_OU_SCORE
        if unscoreable or below:
            e["_dropped_reason"] = f"ou<{MIN_OU_SCORE} (ou={ou})"
            dropped.append(e)
            ou_disp = f"{ou:.2f}" if isinstance(ou, (int, float)) else str(ou)
            print(f"  [{i:>2}/{len(entries)}] OU-DROP    ou={ou_disp}  {e['url']}")
        else:
            kept.append(e)
    return kept, dropped


def _rank_blended(entries: list[dict], top_n_final: int,
                  reserve_n: int = 0) -> tuple[list[dict], list[dict]]:
    """Final ranking: the canonical OU/power percentile blend (see
    input.pipeline.blend.rank_by_blend). Returns (winners, reserve): the top
    top_n_final winners, plus the next `reserve_n` ranked as a backfill pool
    for the save loop (when a winner fails extract/save-gate, the next reserve
    candidate takes its slot so the dish still lands top_n_final). Every entry
    gets a continuous 1-indexed `rank`, so a promoted reserve keeps a truthful
    blend rank. Entries already carry `ou`, `da`, `pa`; rank_by_blend stamps
    `power`, `ou_pct`, `power_pct`, `blend_score`."""
    ranked = rank_by_blend(entries)
    for rank, e in enumerate(ranked, start=1):
        e["rank"] = rank
    return ranked[:top_n_final], ranked[top_n_final:top_n_final + reserve_n]


def _predict_pa_from_fit(ou_fit: dict, da: float) -> Optional[float]:
    """Predict PA for a given DA from the dish's stored OU fit (model +
    coefficients) — so a fetch-fail we couldn't crawl can still get an OU
    (= actual PA − predicted PA) from its Moz DA/PA. Returns None if the fit
    is unusable."""
    coeffs = ou_fit.get("coefficients") or []
    model = ou_fit.get("model")
    if da is None or not coeffs:
        return None
    da = float(da)
    try:
        if model == "linear" and len(coeffs) == 2:
            return coeffs[0] * da + coeffs[1]
        if model == "quadratic" and len(coeffs) == 3:
            return coeffs[0] * da * da + coeffs[1] * da + coeffs[2]
        if model == "power" and len(coeffs) == 2:
            return coeffs[0] * (da ** coeffs[1]) if da > 0 else 0.0
    except Exception:
        return None
    return None


def _apply_paywall_remap(entries: list[dict]) -> int:
    """SELECTION-side paywall correction (pa_gap_v1): lower a gated publisher's
    EXPECTATIONS BAR before the OU fit + rank_by_blend, so the winner SELECTOR
    stops penalizing the paywall. Mirrors the display-side adjustment in
    score_data_points_for_dish.

    Stamps `_da_eff` — the DA the walled section behaves like — and leaves `da`
    and `pa` as the measurements they are. The previous version rewrote `pa` in
    place to a "free-equivalent"; that both destroyed the measured value on the
    entry and, because the free reference sigma was pooled across a DA±8 window
    while the publisher sigma was within-site, inflated the top of each gated
    publisher's band (a Boston Globe OU of +31.7 against a corpus maximum of
    +25.4). See input/pipeline/paywall_calibration.py.

    The caller snapshots RAW da/pa into fit_data_points (the ledger) and
    score_data_points re-derives the SAME adjustment there, so no double-count.
    Returns # of entries adjusted."""
    try:
        from input.pipeline.domains_lib import (get_paywall_da_adjustments,
                                                adjustment_for_url)
        adjustments = get_paywall_da_adjustments()
    except Exception:
        adjustments = {}
    if not adjustments:
        return 0
    n = 0
    for e in entries:
        url = e.get("url") or ""
        da = e.get("da")
        if not isinstance(da, (int, float)):
            continue
        # Resolve from the full URL, not a stripped apex — cooking.nytimes.com
        # must find its own domains row rather than falling through to nothing.
        adj = adjustment_for_url(url, adjustments)
        pct = float((adj or {}).get("discount_pct") or 0)
        if pct <= 0:
            continue
        e["_da_eff"] = float(da) * (1.0 - pct / 100.0)
        e["_da_discount_pct"] = pct     # provenance, for the run log + ledger
        n += 1
    return n


def build_batch(
    queries: list[str] | str,
    *,
    dish: Optional[str] = None,
    top_n_serpapi: int = DEFAULT_TOP_SERPAPI,
    top_n_final: int = DEFAULT_TOP_FINAL,
    extra_urls: Optional[list[str]] = None,
    should_cancel=None,
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
    print(f"\n[1/7] SerpAPI lookup (verbatim queries): dish={dish!r} "
          f"queries={queries} target_n_per_query={top_n_serpapi}")
    entries = _multi_query_lookup(queries, top_n_serpapi)
    serpapi_union = len(entries)
    print(f"      -> {serpapi_union} unique URLs across {len(queries)} "
          f"verbatim queries (paginated)")

    # Editor's Choice — merge curator-pinned URLs into the candidate pool so they
    # are scored alongside the SerpAPI results (and surface in the top-N IFF they
    # rank — junction-style membership, not a forced override). A pin already in
    # the SERP results is just tagged; a new one is appended as a candidate.
    if extra_urls:
        existing_norms = {normalize_url(e["url"]) or e["url"] for e in entries}
        pinned_added = 0
        for pin in extra_urls:
            pin = (pin or "").strip()
            if not pin:
                continue
            key = normalize_url(pin) or pin
            if key in existing_norms:
                for e in entries:
                    if (normalize_url(e["url"]) or e["url"]) == key:
                        e["_pinned"] = True
                continue
            entries.append({
                "url": pin, "title": "", "google_rank": None,
                "domain": root_domain(pin), "_queries": ["editors-choice"],
                "_pinned": True,
            })
            existing_norms.add(key)
            pinned_added += 1
        print(f"      -> + {pinned_added} Editor's Choice pin(s) merged into the pool "
              f"({len(extra_urls)} pinned total)")
    # Surface a thin candidate pool rather than silently loosening the
    # query. A tight (quoted) query legitimately returns fewer matches;
    # the admin broadens it by hand if they want more. We never fall back
    # to an unquoted query — that would re-admit exactly the junk a tight
    # query was written to exclude.
    if serpapi_union < top_n_serpapi:
        print(f"      [thin] only {serpapi_union} candidate(s) for a target "
              f"of {top_n_serpapi} — broaden the query (add a `| \"phrasing\"` "
              f"term, or relax quotes) if you want more.")

    print(f"\n[2/7] filter_disallowed (domain + URL-path blacklist)")
    entries, dropped_disallowed = _filter_disallowed(entries)
    print(f"      -> kept {len(entries)}, dropped {len(dropped_disallowed)}")

    print(f"\n[3/7] is_recipe fetch+score (threshold={IS_RECIPE_THRESHOLD})")
    # URL-text pre-skip (shared self-learning word lists) before the SERP fetch — opt-in
    # via system_config 'url_prefilter_dish_batch'. Proven ~0.2% false-drop on the corpus
    # (foreign-script slugs only), so it's safe to skip obvious non-recipe SERP hits.
    try:
        from input.pipeline.system_config import get_setting as _get_setting
        _dish_url_prefilter = bool(_get_setting("url_prefilter_dish_batch", True))
    except Exception:
        _dish_url_prefilter = True
    # Dish batches span many domains, so render-escalation is gated PER ENTRY: mark
    # results whose publisher is render-eligible (render_required or unblocker) so the
    # filter may rescue a JS-rendered recipe (e.g. a Boston Globe result for "coq au
    # vin"). Bounded — escalation still fires only on a would-be drop. Best-effort.
    try:
        from input.pipeline.domains_lib import get_render_eligible_hosts
        from input.pipeline.url_utils import root_domain as _root_domain
        _render_hosts = get_render_eligible_hosts()
        if _render_hosts:
            for _e in entries:
                _u = (_e.get("url") or "").lower()
                try:
                    _host = _u.split("//", 1)[1].split("/", 1)[0] if "//" in _u else ""
                except Exception:
                    _host = ""
                if _host in _render_hosts or (_root_domain(_e.get("url") or "") or "").lower() in _render_hosts:
                    _e["_allow_render"] = True
    except Exception:
        pass
    # The dish's own name (and the words of its queries) are food words FOR THIS RUN,
    # whatever the learned list happens to know. Tokenised the same way the filter
    # tokenises a URL path, so "Lok Lak" contributes {lok, lak}.
    _subject_words = set()
    try:
        import re as _re
        for _src in [dish or ""] + [str(_q.get("q") if isinstance(_q, dict) else _q)
                                    for _q in (queries or [])]:
            _subject_words |= {w for w in _re.findall(r"[a-z0-9]+", _src.lower())
                               if len(w) > 2}
    except Exception:
        _subject_words = set()
    entries, dropped_not_recipe = _is_recipe_filter(
        entries, capture_source="dish_batch", capture_provenance={"dish": dish},
        url_prefilter=_dish_url_prefilter, should_cancel=should_cancel,
        subject_words=_subject_words)
    # Auto-learn the JS-rendered hint from a dish batch too (symmetry with the
    # publisher harvest): any kept result that needed a render escalation flags its
    # domain so the form shows it + future runs escalate up front.
    try:
        from input.pipeline import domains_lib
        from input.pipeline.url_utils import root_domain as _rd
        for _e in entries:
            if _e.get("_render_escalated"):
                _h = (_rd(_e.get("url") or "") or "")
                if _h:
                    domains_lib.mark_render_required(_h)
    except Exception:
        pass
    print(f"      -> kept {len(entries)}, dropped {len(dropped_not_recipe)}")

    print(f"\n[4/7] Moz scoring on survivors")
    entries, dropped_moz = _moz_score(entries)
    print(f"      -> kept {len(entries)}, dropped {len(dropped_moz)}")

    print(f"\n[5/7] custom OU fit (per-dish regression; "
          f"floor n>={_MIN_FIT_N}, else global formula)")
    # Snapshot the (url, DA, PA) cohort that feeds _compute_custom_ou
    # BEFORE the OU floor filter. Chapter-level aggregation depends on
    # the full cohort the per-dish fit saw, not the saved-winners
    # subset that survives extraction + save-gate. The handler will
    # persist these to dish_run_data_points for chapter rollups.
    fit_data_points = [
        (e.get("url"), e.get("da"), e.get("pa"))
        for e in entries
        if isinstance(e.get("da"), (int, float)) and isinstance(e.get("pa"), (int, float))
    ]
    # Selection-side paywall PA-remap (AFTER the raw fit_data_points snapshot, so the
    # ledger stays truthful and score_data_points re-applies the same remap without
    # double-counting). Lifts gated premium publishers so they're ranked + selected
    # on merit, not penalized for the paywall. No-op when no domains are flagged.
    _n_remap = _apply_paywall_remap(entries)
    if _n_remap:
        print(f"      -> paywall PA-remap lifted {_n_remap} premium-publisher entr(ies)")
    ou_fit = _compute_custom_ou(entries)

    # Foreign-locale batches harvest low-authority publishers the global/
    # US-calibrated OU baseline scores negative by construction; relax the
    # negative-OU floor so they aren't culled for being non-US.
    #
    # The DISH'S OWN `source_language` is authoritative when set — it is a stated
    # fact about what this dish harvests. The query-text heuristic stays as the
    # fallback for dishes that have not been given a locale yet (and for the
    # ad-hoc/no-dish path, which has no row to read), so nothing that worked
    # before stops working.
    foreign_locale, locale_src = _batch_is_foreign_locale(queries, dish)
    relax_note = (f"  — RELAXED (foreign-locale batch, {locale_src})"
                  if foreign_locale else "")
    print(f"\n[6/7] min-OU filter (>= {MIN_OU_SCORE}){relax_note}")
    entries, dropped_low_ou = _min_ou_filter(entries, drop_below_threshold=not foreign_locale)
    print(f"      -> kept {len(entries)}, dropped {len(dropped_low_ou)}")

    print(f"\n[7/7] rank by OU/power blend "
          f"(OU {100 - POWER_BLEND_WEIGHT:.0f} / power {POWER_BLEND_WEIGHT:.0f}, "
          f"percentile), keep top {top_n_final}")
    # Keep a reserve (the next top_n_final ranked) so the save loop can backfill
    # to top_n_final when a winner fails extract/save — "ensure 10 if available".
    final, reserve = _rank_blended(entries, top_n_final, reserve_n=top_n_final)
    print(f"      -> final batch: {len(final)} URLs (+{len(reserve)} reserve for backfill)")

    # Phase A — salvage FETCH-FAILS (likely anti-bot blocks). They were dropped
    # before Moz, but Moz scores by URL (no page crawl needed), so we can still
    # authority-rank them: Moz DA/PA -> OU via this dish's fit -> compare to the
    # cut bar (the #N winner's OU). Recorded as rejects so the dish UI flags the
    # ones that "would have qualified" for a Playwright/bookmarklet recovery.
    # startswith, not ==: fetch failures now carry their reason inline
    # ("fetch-failed: blocked — thin body ..."), and an exact match here would
    # have quietly excluded the very blocks this salvage exists to recover.
    fetch_fails = [e for e in dropped_not_recipe
                   if (e.get("_dropped_reason") or "").startswith("fetch-failed")]
    fetch_fail_candidates: list[dict] = []
    if fetch_fails:
        bar = float(final[-1]["ou"]) if final and isinstance(final[-1].get("ou"), (int, float)) else None
        scored_ff, _ff_moz_fail = _moz_score(fetch_fails)
        for e in scored_ff:
            da, pa = e.get("da"), e.get("pa")
            if not (isinstance(da, (int, float)) and isinstance(pa, (int, float))):
                continue
            ou = e.get("ou")
            if ou_fit.get("used"):
                pred = _predict_pa_from_fit(ou_fit, da)
                if pred is not None:
                    ou = float(pa) - pred
            would_qualify = (bar is not None and isinstance(ou, (int, float)) and float(ou) >= bar)
            fetch_fail_candidates.append({
                "url": e["url"], "da": float(da), "pa": float(pa),
                "ou": round(float(ou), 3) if isinstance(ou, (int, float)) else None,
                "would_qualify": bool(would_qualify),
            })
        n_qual = sum(1 for c in fetch_fail_candidates if c["would_qualify"])
        print(f"\n[Phase A] {len(fetch_fail_candidates)} fetch-fail(s) authority-scored "
              f"(of {len(fetch_fails)}); {n_qual} would have made the top {top_n_final} "
              f"(cut bar OU={bar}) — recoverable via Playwright/bookmarklet")

    elapsed = time.perf_counter() - t0
    print(f"\n[BATCH] Done in {elapsed:.1f}s")

    # Stamp the canonical dish name on every surviving entry so
    # downstream consumers (process_batch.py, eventually the
    # /master/refresh delete-and-replace logic) can key on it. See
    # the dish-library memo for the broader plan.
    for e in final:
        e["dish"] = dish

    # Field context (dish-level, identical across the cohort): the field's
    # absolute clout + any geo/site restriction in the query. Lets the editorial
    # commentary read "a field contested by established publishers" vs "a niche
    # field of specialist sites", and "among Greek (.gr) sites" when the search
    # is site-restricted. Stays relative/qualitative downstream — no raw score leaks.
    powers = [float(e["power"]) for e in final if e.get("power") is not None]
    field_ctx: dict = {}
    if powers:
        field_ctx["avg_power"] = round(sum(powers) / len(powers), 1)
        field_ctx["max_power"] = round(max(powers), 1)
        field_ctx["min_power"] = round(min(powers), 1)
        field_ctx["n"] = len(powers)
    sites = sorted({
        m.strip().lstrip(".").lower()
        for q in queries
        for m in re.findall(r"site:([^\s]+)", q, flags=re.IGNORECASE)
    })
    if sites:
        field_ctx["site_restriction"] = sites
    for e in final:
        e["_field"] = field_ctx

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
        # The dropped entries themselves, not just the counts above. The caller
        # persists them via candidate_ledger — a reject is free to record HERE
        # (we are holding its url/title/rank/DA/PA/OU right now) and costs money
        # to reacquire later: the OU-dropped URLs land in neither the Moz cache
        # nor the extract cache. See docs/ai-editor-mediation.md.
        "dropped_disallowed": dropped_disallowed,
        "dropped_not_recipe": dropped_not_recipe,
        "dropped_moz": dropped_moz,
        "dropped_low_ou": dropped_low_ou,
        # Full (url, DA, PA) cohort fed to _compute_custom_ou — the
        # caller persists these to dish_run_data_points so the
        # chapter-level fit aggregates the same URL universe the
        # per-dish fit saw, not just the saved-winners subset.
        "fit_data_points": fit_data_points,
        # Fetch-fails authority-scored (Phase A) — recorded as rejects so the
        # dish UI can flag the would-have-qualified ones for recovery.
        "fetch_fail_candidates": fetch_fail_candidates,
        # Backfill pool: the next-ranked survivors beyond the top_n_final
        # winners. The save loop pulls from here when a winner fails.
        "reserve": reserve,
        "top_n_final": top_n_final,
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
