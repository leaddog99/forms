"""System config — the DB-resident "config file" (the system record).

The apex of the portable-package model (memory/project_system_config.md,
project_portable_package): every customizable knob reaches the app via the DB,
not a source file, so a recipient of the package never edits code. Today's
`bcc_config.json` becomes a bootstrap SEED (memory/feedback_no_data_in_code) —
the table is canonical once seeded.

Shape: key/value rows, one per setting. `value` is JSON-encoded so a setting
keeps its type (bool/int/float/str/list). `type`/`category`/`label`/
`description` drive how the admin editor renders + groups it.

Reads go through `get_setting(key, default)` — cached process-wide and
invalidated on any write, mirroring domains_lib.get_blocked_root_domains. The
cache makes hot-path reads (e.g. a scheduler gate) free.

This first slice ships only the scheduler settings; the rest of bcc_config.json
migrates in incrementally as code touches those keys.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

# SEED sources for two lists migrated OUT of config.py into this DB-resident record
# (feedback_no_data_in_code). The code lists stay as the diffable bootstrap fallback;
# these references keep the seed in one place instead of re-typing ~158 phrases. Safe
# one-way import — config.py imports no pipeline module (no cycle).
from input.pipeline.config import (
    RECIPE_PHRASES as _RECIPE_PHRASES_SEED,
    _DEFAULT_DISALLOWED_URL_PATH_FRAGMENTS as _URL_PATH_FRAGMENTS_SEED,
    BCC_LINK_DOMAIN as _PUBLIC_HOST_SEED,
)


def _as_origin(host: str) -> str:
    """Normalize a bare host or URL to a scheme-qualified origin, no trailing slash."""
    h = (host or "").strip()
    if h and "://" not in h:
        h = "https://" + h
    return h.rstrip("/")


# ============================================================
#  Bootstrap seed (DEFAULTS) — code constant is seed-only.
#  Each row: key, default value, type, category, label, description.
#  `type` is a UI/rendering hint; the value is always JSON-encoded.
# ============================================================

SYSTEM_DEFAULTS: list[dict] = [
    {
        "key": "public_base_url",
        "value": _as_origin(_PUBLIC_HOST_SEED),
        "type": "string",
        "category": "Branding",
        "label": "Public base URL (externally reachable)",
        "description": "The externally-reachable origin of this app — e.g. the "
                       "Cloudflare tunnel host. Bookmarklets run on a third-party "
                       "(retailer/recipe) page and POST here, and the install page "
                       "bakes it into the loader, so a self-hoster ships their own "
                       "host with no code change. Seeded from bcc_link_domain; "
                       "scheme optional (https assumed).",
    },
    {
        "key": "scheduler_enabled",
        "value": True,
        "type": "bool",
        "category": "Scheduler",
        "label": "Auto-refresh scheduler enabled",
        "description": "Master on/off for unattended dish refreshes. When off, "
                       "`python -m jobs schedule` does nothing (manual Run still works).",
    },
    {
        "key": "scheduler_interval_hours",
        "value": 6,
        "type": "int",
        "category": "Scheduler",
        "label": "Scheduler interval (hours)",
        "description": "Minimum hours between scheduler passes. The OS heartbeat "
                       "fires hourly; a pass is skipped until this many hours have "
                       "elapsed since the last one. Per-dish timing still obeys each "
                       "dish's Refresh TTL.",
    },
    {
        "key": "scheduler_last_tick_at",
        "value": None,
        "type": "string",
        "category": "Scheduler",
        "label": "Last scheduler pass (UTC)",
        "description": "Stamped by the scheduler after each real pass. Read-only; "
                       "drives the interval gate.",
    },
    {
        "key": "rollups_last_run_at",
        "value": None,
        "type": "string",
        "category": "Scheduler",
        "label": "Last chapter-rollups pass (UTC)",
        "description": "Stamped after the nightly cross-dish rollup (dish "
                       "competitiveness). Read-only; gates the ~daily rollup that "
                       "rides the hourly scheduler heartbeat.",
    },
    # --- Images: standardization knobs for the coopt/upload pipeline ---
    {
        "key": "image_jpeg_quality",
        "value": 85,
        "type": "int",
        "category": "Images",
        "label": "JPEG quality",
        "description": "Quality (1–95) for standardized hero/cooped images. "
                       "Higher = sharper + bigger. 85 ≈ cookbook-grade.",
    },
    {
        "key": "image_hero_max_px",
        "value": 1600,
        "type": "int",
        "category": "Images",
        "label": "Hero image max edge (px)",
        "description": "A user's hero image is resized PRESERVING its whole "
                       "frame + orientation (no crop), scaled down so the longest "
                       "edge is at most this many pixels. Small images are never "
                       "upscaled. (The corpish-thumbnail crop below is separate.)",
    },
    {
        "key": "image_landscape_target",
        "value": "1500x1000",
        "type": "string",
        "category": "Images",
        "label": "Landscape target (WxH)",
        "description": "Exact pixel size landscape-ish images are center-cropped + "
                       "scaled to. 3:2 is the cookbook standard.",
    },
    {
        "key": "image_portrait_target",
        "value": "1000x1500",
        "type": "string",
        "category": "Images",
        "label": "Portrait target (WxH)",
        "description": "Exact pixel size portrait images are center-cropped + scaled to.",
    },
    # --- Limits: hard caps that protect against fat-finger / runaway cost ---
    {
        "key": "dish_max_serpapi",
        "value": 200,
        "type": "int",
        "category": "Limits",
        "label": "Max SerpAPI candidates per query",
        "description": "Hard cap on a dish's top_n_serpapi. Creating/editing a dish "
                       "above this is rejected, and a refresh clamps to it — guards "
                       "against e.g. an accidental 2550 asking SerpAPI for 2550 rows.",
    },
    {
        "key": "dish_max_final",
        "value": 200,
        "type": "int",
        "category": "Limits",
        "label": "Max kept winners per dish",
        "description": "Hard cap on a dish's top_n_final (selected rows). Same "
                       "enforcement as the SerpAPI cap.",
    },
    # --- Matching: recipe -> canonical-dish vector NN at save time ---
    {
        "key": "dish_match_max_distance",
        "value": 0.85,
        "type": "float",
        "category": "Matching",
        "label": "Dish match cutoff (L2 distance)",
        "description": "A user recipe is a CONFIDENT match to its nearest dish "
                       "when their embeddings are within this L2 distance. Lower = "
                       "stricter. 0.85 (≈ cosine 0.64) catches compound names like "
                       "'Shrimp Risotto' (0.82) while excluding true non-matches "
                       "(≥0.98). Validated: real matches ≤0.25 or ~0.82, non-matches ≥0.98.",
    },
    # --- Search: how the dish front-end builds the Google/SerpAPI query ---
    {
        "key": "serp_exclude_blocklist",
        "value": True,
        "type": "bool",
        "category": "Search",
        "label": "Exclude blocklist domains in the query",
        "description": "Append the domain blocklist as Google `-site:` exclusions so "
                       "the SERP itself filters known clutter (social/aggregators) — "
                       "Google fills those slots with usable recipe sites, giving fewer "
                       "rejects and more keeps. Only -site: is spliced; search terms are "
                       "untouched. filter_disallowed still runs as the safety net.",
    },
    {
        "key": "serp_max_exclusions",
        "value": 18,
        "type": "int",
        "category": "Search",
        "label": "Max query exclusions",
        "description": "Cap on how many exclusions (domains + terms) are appended "
                       "to a query — Google's query length is finite.",
    },
    {
        "key": "serp_exclusions",
        "value": ("facebook.com\ninstagram.com\nlinkedin.com\npinterest.com\n"
                  "reddit.com\ntiktok.com\ntwitter.com\nwikipedia.org\nyoutube.com\n"
                  "roundup"),
        "type": "text",
        "category": "Search",
        "label": "SERP query exclusions (one per line)",
        "description": "The editable exclusion list appended to every dish query. A "
                       "line with a dot is a DOMAIN (→ Google -site:, and also dropped "
                       "downstream by filter_disallowed); any other line is a bare TERM "
                       "(→ -term, removed from the SERP only). Domains kill social/"
                       "aggregator clutter; terms kill listicles (roundup). NOTE: bare "
                       "terms like 'best'/'top' also appear in legit single-recipe "
                       "titles, so they can drop good results — use with care.",
    },
    {
        "key": "serp_page_retries",
        "value": 3,
        "type": "int",
        "category": "Search",
        "label": "SERP page fetch attempts (transient-error retries)",
        "description": "How many times to fetch a single SERP page before giving up on it. "
                       "A transient ReadTimeout/ConnectionError on one page used to abandon "
                       "the whole pagination, silently truncating a deep query (e.g. 20 of a "
                       "target 75). Retrying rides out a network blip. 1 = no retry (old "
                       "behavior).",
    },
    {
        "key": "serp_page_retry_backoff",
        "value": 2.0,
        "type": "float",
        "category": "Search",
        "label": "SERP page retry backoff (seconds)",
        "description": "Seconds to wait between retry attempts for a timed-out SERP page.",
    },
    # --- Harvest: the SEMrush human-workflow loop (docs/semrush-harvest-scheduling.md) ---
    {
        "key": "semrush_indexed_pages_url_template",
        "value": ("https://www.semrush.com/analytics/organic/pages/"
                  "?q={domain}&searchType=domain"),
        "type": "string",
        "category": "Harvest",
        "label": "SEMrush Top-Pages URL template",
        "description": "The deep-link the worklist opens to land you on a domain's "
                       "SEMrush Organic Research → Pages report (Top Pages by TRAFFIC — our "
                       "default export), ready to Export. `{domain}` is substituted per row. "
                       "A domain's own `semrush_report_url` (if set) overrides this. Edit "
                       "here if SEMrush re-skins its URLs. (Old backlinks-pages report was "
                       "/analytics/backlinks/pages/?…sort_field=domainsnum.)",
    },
    {
        "key": "semrush_inbox_dir",
        "value": "",
        "type": "string",
        "category": "Harvest",
        "label": "SEMrush export inbox folder",
        "description": "Folder the 'Scan inbox' button reads for saved SEMrush exports "
                       "(`*-backlinks*pages*.xlsx`). Blank = the OS Downloads folder.",
    },
    {
        "key": "url_prefilter_dish_batch",
        "value": True,
        "type": "bool",
        "category": "Harvest",
        "label": "URL pre-filter on the dish SERP batch",
        "description": "Skip SERP results whose URL path names no food/recipe word BEFORE "
                       "fetching, using the self-learning word lists (url_word_class). "
                       "Proven ~0.2% false-drop on the corpus (foreign-script slugs only). "
                       "Publisher harvests use the per-domain `url_prefilter` flag instead.",
    },
    {
        "key": "playwright_browsers_path",
        "value": "",
        "type": "string",
        "category": "Screenshots",
        "label": "Playwright browsers path (override)",
        "description": "Folder where Playwright's headless-Chromium browsers are installed. "
                       "Blank = auto-detect the standard per-user install locations (incl. "
                       "every Windows profile, so a LocalSystem service still finds them). "
                       "Set only if your browsers live somewhere non-standard — no "
                       "machine-specific service-env editing needed (portable).",
    },
    {
        "key": "screenshot_nav_timeout_ms",
        "value": 25000,
        "type": "int",
        "category": "Screenshots",
        "label": "Screenshot navigation timeout (ms)",
        "description": "Hard per-capture timeout so a hung page can't stall a harvest. Default 25000.",
    },
    {
        "key": "screenshot_settle_ms",
        "value": 1500,
        "type": "int",
        "category": "Screenshots",
        "label": "Screenshot settle delay (ms)",
        "description": "Wait after domcontentloaded for JS widgets to render before the shot. "
                       "Default 1500 (enough for most async content, short enough to beat a "
                       "paywall modal).",
    },
    {
        "key": "screenshot_viewport_w",
        "value": 1500,
        "type": "int",
        "category": "Screenshots",
        "label": "Screenshot viewport width (px)",
        "description": "Headless viewport width. Default 1500 (matches the landscape thumbnail target).",
    },
    {
        "key": "screenshot_viewport_h",
        "value": 900,
        "type": "int",
        "category": "Screenshots",
        "label": "Screenshot viewport height (px)",
        "description": "Headless viewport height. Default 900.",
    },
    {
        "key": "screenshot_capture_height",
        "value": 800,
        "type": "int",
        "category": "Screenshots",
        "label": "Screenshot capture height (px)",
        "description": "Above-fold capture window height (masthead through intro). Default 800.",
    },
    {
        "key": "page_cache_enabled",
        "value": True,
        "type": "bool",
        "category": "Harvest",
        "label": "Harvest raw-page cache",
        "description": "Cache the raw fetched page during a publisher harvest so the "
                       "is-recipe filter and the winner-extract share ONE fetch (no "
                       "double-fetch / double unblocker credit) and re-harvests within "
                       "the TTL skip the network. Stored in a separate page_cache.db. "
                       "Off = always re-fetch (the old behavior).",
    },
    {
        "key": "page_cache_ttl_days",
        "value": 5.0,
        "type": "float",
        "category": "Harvest",
        "label": "Harvest page-cache TTL (days)",
        "description": "How long a cached raw page stays fresh before a harvest re-fetches "
                       "it. Default 5.",
    },
    {
        "key": "semrush_export_patterns",
        "value": ["{domain}*[Pp]ages*.xlsx",
                  "*.{domain}*[Pp]ages*.xlsx",
                  "*_{domain}*[Pp]ages*.xlsx"],
        "type": "list",
        "category": "Harvest",
        "label": "SEMrush export filename patterns",
        "description": "Glob patterns (one per line, `{domain}` substituted) used to FIND a "
                       "publisher's SEMrush page export in the inbox/Downloads folder. The "
                       "reader auto-detects the FORMAT from the columns, so these can be "
                       "broad. Edit here if SEMrush changes its export filenames — no code "
                       "change. A per-domain exact file path overrides this entirely.",
    },
    {
        "key": "url_prefilter_explore_rate",
        "value": 0.08,
        "type": "float",
        "category": "Harvest",
        "label": "URL pre-filter ε-exploration rate",
        "description": "Fraction (0-1) of would-be url-prefilter SKIPS that are verified "
                       "anyway (fetched + scored) to mint UNBIASED is-recipe training "
                       "labels in the region the filter skips — without it the classifier "
                       "trains only on survivors and re-learns the filter's blind spots. "
                       "0 disables. Costs ~rate × (skipped count) extra fetches.",
    },
    {
        "key": "keyword_prescreen_default",
        "value": False,
        "type": "bool",
        "category": "Harvest",
        "label": "Keyword pre-screen (LLM) on publisher harvests",
        "description": "Before the per-URL fetch+translate, run ONE batched Haiku pass that "
                       "judges each candidate recipe-vs-not from its URL slug + SEMrush Top "
                       "Keyword, and drop the confident non-recipes pre-fetch. Big win on "
                       "non-English MIXED publishers (tips/guides interleaved with recipes) "
                       "where the post-fetch path is a full-page translation (~40s/URL). "
                       "Negative-only + conservative → minimal false drops; ε-exploration "
                       "reuses the url-prefilter rate. See docs/keyword-prescreen.md.",
    },
    {
        "key": "is_recipe_cascade_mode",
        "value": "off",
        "type": "string",
        "category": "Harvest",
        "label": "Is-recipe LLM cascade mode (off | shadow | decide)",
        "description": "Cheap Haiku three-way pass (recipe|not_recipe|poor_quality) over the GRAY "
                       "ZONE (content-bearing candidates with NO schema.org/Recipe), on recipe-"
                       "anchored snippets. 'off' = never run. 'shadow' = classify + RECORD the "
                       "verdict next to the heuristic's in training.db but do NOT change keep/drop "
                       "(measure + mint gold labels; review in ⋮ admin → Labeling). 'decide' = also "
                       "APPLY the asymmetric override: rescue a heuristic-drop the LLM calls 'recipe' "
                       "(~77% precision) and drop a heuristic-keep it calls poor_quality/not_recipe "
                       "(~88%); training still records the heuristic label. See docs/is-recipe-classifier.md.",
    },
    {
        "key": "filter_translate_max_chars",
        "value": 6000,
        "type": "int",
        "category": "Harvest",
        "label": "Filter-stage translation cap (chars)",
        "description": "Max characters of a non-English page sent to the FILTER's translate "
                       "(keep-vs-drop phrase score). Extraction re-translates the FULL page, so "
                       "this never affects final recipe quality — it only bounds the per-URL "
                       "filter cost (translation latency ≈ output tokens). Lower = faster but "
                       "risks dropping a recipe whose ingredients/method sit past the cut "
                       "(long-intro blogs). 0 = no cap (translate the whole page).",
    },
    {
        "key": "render_escalate_thin_chars",
        "value": 3500,
        "type": "int",
        "category": "Harvest",
        "label": "Render-escalation thin-content threshold (chars)",
        "description": "A would-be-dropped page whose PLAIN fetch returned FEWER than this "
                       "many characters looks like a JS shell (nav only, body injected client-"
                       "side) → worth re-fetching with a real browser (unblocker render=True). "
                       "A page that returned MORE is genuinely not a recipe, not a shell, so "
                       "rendering it again would only burn a credit. Default 3500.",
    },
    {
        "key": "render_escalate_probe",
        "value": True,
        "type": "bool",
        "category": "Harvest",
        "label": "Render-escalation PROBE of unlearned domains",
        "description": "Break the chicken-and-egg: normally only a domain already flagged "
                       "render_required (or unblocker) may render-escalate, so a FRESH JS site "
                       "(e.g. delish.com) is never rescued and never learned. When on, a would-"
                       "be-dropped THIN stub on a not-yet-eligible domain still gets one render "
                       "attempt; a success auto-learns the domain (render_required) so future "
                       "runs render it up front. Bounded by the per-domain cap below.",
    },
    {
        "key": "render_escalate_probe_max",
        "value": 3,
        "type": "int",
        "category": "Harvest",
        "label": "Render-escalation probe cap (per domain per run)",
        "description": "Most probe renders a not-yet-learned domain may spend in one filter "
                       "run before giving up — so a genuinely-thin non-recipe site can't burn "
                       "many credits confirming it's not a recipe. One success is enough to "
                       "auto-learn the domain. 0 disables probing.",
    },
    {
        "key": "poor_publisher_min_samples",
        "value": 4,
        "type": "int",
        "category": "Harvest",
        "label": "Poor-publisher flag: min classified samples",
        "description": "A host needs at least this many LLM-cascade-classified pages (across ALL "
                       "sources — dish batches AND harvests, from training.db) before it can be "
                       "flagged a POOR publisher. Guards against flagging on a tiny sample.",
    },
    {
        "key": "poor_publisher_threshold",
        "value": 0.6,
        "type": "float",
        "category": "Harvest",
        "label": "Poor-publisher flag: poor_quality fraction",
        "description": "When a domain's fraction of cascade 'poor_quality' verdicts reaches this "
                       "(0-1) over at least the min-samples above, flag it a poor publisher: the "
                       "harvest STOPS paying the per-page LLM cascade for its pages (they fall "
                       "back to the heuristic) and the domain is badged for curator review. "
                       "Default 0.5.",
    },
    {
        "key": "disallowed_domains",
        "value": ["youtube.com", "facebook.com", "reddit.com", "twitter.com",
                  "pinterest.com", "tiktok.com", "linkedin.com", "wikipedia.org"],
        "type": "list",
        "category": "Harvest",
        "label": "Disallowed domains (hard blocklist)",
        "description": "Root domains dropped from EVERY harvest/batch BEFORE any fetch (at "
                       "root-domain grain), unioned with the SERP exclusions' domain lines. "
                       "Social/aggregator/encyclopedia hosts whose pages use recipe vocabulary "
                       "but aren't recipes. Replaces the old per-domain `allowed=0` flag — one "
                       "editable list, no domains-master row needed to block a junk host.",
    },
    {
        "key": "disallowed_url_path_fragments",
        "value": _URL_PATH_FRAGMENTS_SEED,
        "type": "list",
        "category": "Harvest",
        "label": "Disallowed URL-path fragments",
        "description": "URL-path SUBSTRINGS that mark a roundup/article/news page rather than a "
                       "single recipe, dropped from every batch BEFORE any fetch (e.g. "
                       "'/articles/', '/roundup', '/buying-guide' — the "
                       "americastestkitchen.com/articles/24-the-best-beef-stew pattern that "
                       "survives phrase scoring because narrative articles use recipe "
                       "vocabulary). Matched as a lowercase substring of the full URL, so "
                       "anchor them with slashes to avoid hitting a recipe slug. The sibling "
                       "of 'disallowed_domains' (which blocks whole hosts).",
    },
    {
        "key": "recipe_phrases",
        "value": _RECIPE_PHRASES_SEED,
        "type": "list",
        "category": "Harvest",
        "label": "Recipe-phrase detection list (English / base is_recipe score)",
        "description": "The ~158 phrases the English/base is-recipe HEURISTIC counts to score a "
                       "page (a page scoring >= the is_recipe threshold is a candidate). One "
                       "phrase per line. IMPORTANT: leading/trailing SPACES are SIGNIFICANT and "
                       "preserved — ' ounce' (with the space) avoids matching 'announce', "
                       "'yield ' avoids 'yielding'; do NOT trim them. Entries are matched as a "
                       "lowercase substring, so keep them specific enough to not fire in "
                       "narrative prose (bare ' cup' / single spice names were pruned for that "
                       "reason — quantified/anchored forms like '1/2 cup', 'squeeze of' stay). "
                       "NON-ENGLISH pages do NOT use this list; each language has its own "
                       "recipe_phrases/<lang>.json, regenerated from this one by "
                       "scripts/translate_recipe_phrases.py — re-run that after editing here.",
    },
    {
        "key": "structure_ingredient_markers",
        "value": ["ingredients"],
        "type": "list",
        "category": "Harvest",
        "label": "Structure gate — ingredient section markers (English)",
        "description": "Header words that mark the INGREDIENTS section for the free is-recipe "
                       "structural gate (base/English). A real recipe has BOTH an ingredients "
                       "AND a method section. Non-English sites carry their own markers in their "
                       "recipe_phrases/<lang>.json 'sections' block.",
    },
    {
        "key": "structure_method_markers",
        "value": ["instructions", "directions", "method", "preparation", "steps"],
        "type": "list",
        "category": "Harvest",
        "label": "Structure gate — method section markers (English)",
        "description": "Header words that mark the METHOD/directions section for the structural "
                       "is-recipe gate (base/English). If none is present, the verb fallback "
                       "below can still satisfy the method side.",
    },
    {
        "key": "structure_method_verbs",
        "value": ["preheat", "combine", "mix", "stir", "whisk", "bake", "boil", "simmer",
                  "fold", "knead", "pour", "cook", "roast", "saute", "chop", "slice", "dice",
                  "mince", "melt", "beat", "blend", "grease", "drain", "season", "sprinkle",
                  "spread", "cover", "remove", "transfer", "serve", "reduce", "garnish",
                  "marinate", "refrigerate", "chill", "grill", "fry", "steam", "toss",
                  "brush", "strain", "spoon", "layer", "dissolve", "scrape"],
        "type": "list",
        "category": "Harvest",
        "label": "Structure gate — method verbs (header-less fallback, English)",
        "description": "Imperative cooking verbs. Many blog recipes write the method as prose "
                       "with NO 'Directions/Method' header; a cluster of at least the minimum "
                       "(below) DISTINCT verbs stands in for the missing header, so the cheap "
                       "heuristic keeps header-less recipes (that still have an ingredients "
                       "section) without paying the LLM. Word-boundary matched; accent-free.",
    },
    {
        "key": "structure_method_verb_min",
        "value": 4,
        "type": "int",
        "category": "Harvest",
        "label": "Structure gate — min distinct method verbs",
        "description": "How many DISTINCT method verbs (from the list above) count as a "
                       "header-less method section. Higher = stricter (fewer false keeps, more "
                       "reliance on the LLM); lower = more recall. Default 4.",
    },
    {
        "key": "snippet_recipe_anchors",
        "value": ["ingredients", "instructions", "directions", "method:", "method ",
                  "prep time", "cook time", "total time", "yield", "servings", "serves",
                  "preheat"],
        "type": "list",
        "category": "Harvest",
        "label": "Recipe-region snippet anchors",
        "description": "Markers used to find WHERE the recipe region begins when excerpting a "
                       "page for the LLM cascade / Labeling UI — so the model sees the "
                       "ingredient/step region, not the page header. The earliest match wins.",
    },
    {
        "key": "snippet_lead_chars",
        "value": 80,
        "type": "int",
        "category": "Harvest",
        "label": "Recipe-region snippet lead-in (chars)",
        "description": "How many characters of context to include BEFORE the recipe anchor in "
                       "an excerpt (snapped to a word boundary). Small by design — the bulk of "
                       "the window is the recipe text AFTER the anchor (the caller's max_chars, "
                       "e.g. 1600 for the LLM cascade). Not the window size.",
    },
    {
        "key": "snippet_comment_markers",
        "value": ["leave a reply", "leave a comment", "leave a review", "post a comment",
                  "comments are closed", "your email address will not be published",
                  "required fields are marked", "related posts", "related recipes",
                  "you may also like", "post navigation", "join the conversation",
                  "write a review", "rate this recipe"],
        "type": "list",
        "category": "Harvest",
        "label": "Comment-section boundary markers (recipe back-anchor)",
        "description": "A blog page runs [intro] → [recipe] → [comments]. When NO recipe anchor "
                       "is found (a header-less recipe buried under a long intro), the excerpt "
                       "falls back to the window ENDING at the earliest of these comment/footer "
                       "markers — 'scrolling back' from the comments to catch the recipe — "
                       "instead of handing the LLM the intro prose.",
    },
]

_SEED_BY_KEY = {d["key"]: d for d in SYSTEM_DEFAULTS}


# ============================================================
#  Schema + seed
# ============================================================

def ensure_system_config_table(conn: sqlite3.Connection) -> None:
    """Create the system_config table if absent and seed any missing default
    rows. Idempotent — safe on every startup. Seeding only INSERTs keys that
    don't exist yet, so it never clobbers a curator's edited value."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_config (
            key         TEXT PRIMARY KEY,
            value       TEXT,                         -- JSON-encoded (typed)
            type        TEXT NOT NULL DEFAULT 'string',
            category    TEXT NOT NULL DEFAULT 'General',
            label       TEXT,
            description TEXT,
            updated_at  TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_system_config_category "
        "ON system_config(category)"
    )
    now = datetime.now(timezone.utc).isoformat()
    existing = {r[0] for r in conn.execute("SELECT key FROM system_config")}
    for d in SYSTEM_DEFAULTS:
        if d["key"] in existing:
            # Backfill metadata (label/desc/type/category) so doc edits to the
            # seed reach already-seeded rows, WITHOUT touching the value.
            conn.execute(
                "UPDATE system_config SET type=?, category=?, label=?, description=? "
                "WHERE key=?",
                (d["type"], d["category"], d.get("label"), d.get("description"), d["key"]),
            )
            continue
        conn.execute(
            "INSERT INTO system_config (key, value, type, category, label, description, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d["key"], json.dumps(d["value"]), d["type"], d["category"],
             d.get("label"), d.get("description"), now),
        )
    conn.commit()
    _invalidate_cache()


# ============================================================
#  Cached read / write
# ============================================================

# Process-wide cache: key -> decoded value. Populated lazily, dropped on any
# write. None means "not loaded yet" (distinct from an empty dict).
_cache: Optional[dict[str, Any]] = None


def _invalidate_cache() -> None:
    global _cache
    _cache = None


def _load_cache(db_path: str) -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    out: dict[str, Any] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for key, value_json in conn.execute("SELECT key, value FROM system_config"):
                try:
                    out[key] = json.loads(value_json) if value_json is not None else None
                except Exception:
                    out[key] = value_json  # tolerate a non-JSON legacy value
    except Exception as e:
        # Table may not exist yet (very early boot) — fall back to seed defaults.
        print(f"[SYSCONFIG] cache load failed ({e}); using seed defaults")
        return {k: d["value"] for k, d in _SEED_BY_KEY.items()}
    _cache = out
    return out


def get_setting(key: str, default: Any = None, *, db_path: Optional[str] = None) -> Any:
    """Read a setting (cached). Falls back to the seed default, then `default`.
    db_path defaults to save_recipe_api.DB_PATH so callers don't have to pass it."""
    if db_path is None:
        import save_recipe_api as _api  # lazy to avoid import cycle at module load
        db_path = _api.DB_PATH
    cache = _load_cache(db_path)
    if key in cache:
        return cache[key]
    if key in _SEED_BY_KEY:
        return _SEED_BY_KEY[key]["value"]
    return default


def public_base_url() -> str:
    """The externally-reachable origin clients (bookmarklets, permalinks) target.
    Reads system_config.public_base_url (canonical), normalized to a scheme-qualified
    origin; falls back to the bcc_link_domain seed. Empty string only if nothing is set."""
    return _as_origin(get_setting("public_base_url", _PUBLIC_HOST_SEED))


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Upsert a setting's value (JSON-encoded) and invalidate the cache. Carries
    the seed's type/category/label/description for a brand-new key so the admin
    UI can still render it."""
    now = datetime.now(timezone.utc).isoformat()
    seed = _SEED_BY_KEY.get(key, {})
    conn.execute(
        """
        INSERT INTO system_config (key, value, type, category, label, description, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, json.dumps(value), seed.get("type", "string"),
         seed.get("category", "General"), seed.get("label"),
         seed.get("description"), now),
    )
    conn.commit()
    _invalidate_cache()


def list_settings(conn: sqlite3.Connection,
                  category: Optional[str] = None) -> list[dict]:
    """All settings (optionally one category), decoded, for the admin editor.
    Ordered by category then key for stable grouping."""
    sql = "SELECT key, value, type, category, label, description, updated_at FROM system_config"
    args: list = []
    if category is not None:
        sql += " WHERE category = ?"
        args.append(category)
    sql += " ORDER BY category, key"
    out: list[dict] = []
    for key, value_json, type_, cat, label, desc, updated in conn.execute(sql, args):
        try:
            value = json.loads(value_json) if value_json is not None else None
        except Exception:
            value = value_json
        out.append({
            "key": key, "value": value, "type": type_, "category": cat,
            "label": label, "description": desc, "updated_at": updated,
        })
    return out
