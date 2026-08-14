"""Domain master — the canonical per-publisher record.

A *domain* (full host, e.g. ``cooking.nytimes.com``) is the entity that owns
everything we know about a source site independent of any single recipe:

  - editorial    display_name, story, logo
  - provenance   country + primary language (a default for the multilingual
                 pipeline's _source.originalLanguage), optional cuisine_focus
                 HINT (NOT authoritative recipe ethnicity — that stays dish-
                 derived; a Greek site can still post a taco)
  - extraction   fetch_strategy / extract_notes — capture hints for the harvest
  - gatekeeping  harvestable (skip a known publisher's refresh); the hard host
                 blocklist moved OUT to system_config `disallowed_domains` (a
                 junk host no longer needs a domains-master row to be blocked)
  - authority    domain_authority (DA is a domain property; metabase_url keeps
                 only the per-URL PA) + da_last_scored
  - ops          notes, timestamps

This is the source of truth (per ``feedback_no_data_in_code``). The shipped
``domain_display_names.json`` is demoted to a one-time BOOTSTRAP SEED — same
relationship ``chapter_classifier.CHAPTERS`` has to the ``chapters`` table.

Keyed on the full host (per the grain decision) so subdomains can carry
distinct display names / extraction tips ("NYT Cooking" vs "The New York
Times"); ``root_domain`` is carried alongside so DA can be shared across a
publisher's subdomains.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from input.pipeline.db import connect as _connect  # WAL busy_timeout — input/pipeline/db.py
from datetime import datetime, timedelta, timezone
from typing import Optional

from input.pipeline.url_utils import root_domain

_SEED_PATH = os.path.join(os.path.dirname(__file__), "domain_display_names.json")
_DEFAULT_DB = "recipes.db"

# Editable columns surfaced by the a/c/d editor, in display order. domain
# (PK) + the timestamps are managed, not in this list.
EDITABLE_FIELDS = (
    "display_name",
    "story",
    "logo_url",
    "country",
    "language",
    "cuisine_focus",     # the publisher's cuisine (searchable; was a "hint", now a real field)
    "ethnicity",         # the publisher's cultural origin (optional; not searched yet)
    "fetch_strategy",
    "render_required",   # JS-rendered site → fetch with a real browser (unblocker render=True)
    "score_only",        # harvest MODE: 1 = Curate (score URL-only, pick manually); persists the picker choice
    "extract_notes",
    "domain_authority",
    "da_last_scored",
    "notes",
    "keep_top_n",     # publisher top-N to keep on refresh
    "harvest_records",# records to pull from the SEMrush export on a backlinks_file harvest
    "recipe_path",    # publisher recipe URL path segment (detected, overridable)
    "serp_query",     # VERBATIM Google query for the harvest (overrides recipe_path)
    "search_pages",   # how many SERP pages (~10 results each) to fetch on refresh
    "harvest_source", # discovery source: 'serp' (Google site:) or 'backlinks_file' (SEMrush export)
    "harvestable",    # 0 = no mechanical recipe access; skip publisher refresh
    "paywall",        # gated premium publisher (FACT: is it gated — not "is it penalized")
    # R4 — HUMAN CAPTURE ONLY. A publisher whose recipe BODY the server can never
    # obtain, however much we spend: 177milkstreet returns title/hero/headnote and
    # then "To access this recipe, you need to be a member." Distinct from
    # `paywall` (which is about SCORING — gated pages earn fewer links, so OU needs
    # the DA haircut) and from `harvestable` (which skips the publisher entirely).
    # Here the URLs are worth discovering, scoring and ranking; only the paid
    # CONTENT fetch is futile. Ingestion happens through the curator's signed-in
    # browser instead. A domain property, not a per-run checkbox, because a
    # per-run choice is one someone has to remember every time — and forgetting it
    # costs a render per URL to rediscover a paywall we already measured.
    "human_capture_only",
    # MIXED MEDIA — the domain's authority is earned by content that is NOT its
    # recipes: a newspaper (washingtonpost), a general forum (wenxuecity), a
    # lifestyle portal (marthastewart), a supermarket chain (ab.gr), a restaurant
    # directory (bostonchefs), a TV personality's site (andrewzimmern). DA is
    # measured across the WHOLE domain, so on these the recipe section is judged
    # against an expectations bar its own news/listings/crafts built — the exact
    # structural fault the paywall haircut corrects, arriving by a different
    # route. Feeds the SAME `pa_gap_v1` calibration.
    #
    # CURATED, never inferred: there is no reliable signal for it. `recipe_path`
    # looks like a proxy and is not — it is set on washingtonpost and epicurious
    # but NOT on marthastewart or bostonglobe, which are as mixed as either.
    #
    # NOT a quality judgment. A pure recipe blog whose pages run below its DA
    # cohort is simply a weaker site and must NOT be flagged — discounting it
    # manufactures a permanent bonus for being mediocre, which is the failure
    # mode paywall_calibration's MIN_EFFECT gate exists to prevent.
    "mixed_media",
    # Curator override for the DA haircut. Setting it flips paywall_adj_source
    # to 'manual', which makes the calibration job leave the row alone; clearing
    # it (blank/None) hands ownership back to the job on its next run.
    "paywall_da_discount_pct",
    "harvest_ttl_days",    # refresh cadence (days) → drives the due-today worklist
    "semrush_report_url",      # the curator's SEMrush link: seeded once at create, hand-editable thereafter
    "trust_extraction",        # 1 = keep candidates past the structure gate + cascade catch → extractor
    "backlinks_dir",       # OPTIONAL per-domain override folder for the SEMrush export
    "exclude_words",       # OPTIONAL per-domain EXCLUSIONARY sections (restaurant/chef/news)
    "profile",             # long researched bio (deep-enrich; curator-editable)
    "brand_authority",     # Moz V3 Brand Authority 0-100 (managed by deep-enrich; overridable)
    "referring_domains",   # Moz V3 referring-domain count (managed; overridable)
    "ranking_keywords",    # JSON list of top keywords the site ranks for (managed)
)


def _canon_host(host: str) -> str:
    """Canonical host key: lowercase, no scheme, no path/query, no port, no
    leading dot, no ``www.``. Tolerates a pasted URL or trailing slash so the
    create form is forgiving about what the curator types."""
    h = (host or "").strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]  # drop path/query/frag
    h = h.split(":", 1)[0]                                    # drop :port
    h = h.lstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Denormalized recipe counts — STORED on the row (refreshed on access /
# domain-form open) so the list can show them at a glance. The recipe LISTS
# themselves are never stored — they're computed live by recipes_for_domain.
_COUNT_COLUMNS = {
    "master_recipe_count": "INTEGER NOT NULL DEFAULT 0",
    "user_recipe_count": "INTEGER NOT NULL DEFAULT 0",
    "counts_updated_at": "TEXT",
}

# Paywall PA-calibration (shift-and-scale remap of a gated publisher's PA to its
# free-equivalent). SUPERSEDED 2026-08-12 by the paywall_da_* columns below and
# by input/pipeline/paywall_calibration.py; these columns are retained read-only
# so historic calibrations stay inspectable. Nothing writes them any more.
_PAYWALL_COLUMNS = {
    "paywall": "INTEGER NOT NULL DEFAULT 0",        # 1 = gated premium publisher
    "pa_cal_mean": "REAL",                          # this publisher's recipe-PA mean
    "pa_cal_std": "REAL",                           # ...std (spread)
    "pa_cal_n": "INTEGER",                          # sample size behind the calibration
    "pa_cal_free_mean": "REAL",                     # matched-DA free PA mean (target)
    "pa_cal_free_std": "REAL",                      # ...std
    "pa_cal_at": "TEXT",                            # when calibrated (ISO)
    # How many top recipes to KEEP (mark selected) on a publisher refresh — the
    # domains-page analog of a dish's top_n_final. Default 10, curator-overridable.
    "keep_top_n": "INTEGER NOT NULL DEFAULT 10",
    # How many records to pull from the SEMrush export file on a backlinks_file
    # harvest (the form's "Records to pull from file"). A harvest knob like
    # keep_top_n/search_pages; PERSISTED so it sticks across edits/reloads (was
    # read only at refresh time and reset to 100 each load). Default 100.
    "harvest_records": "INTEGER NOT NULL DEFAULT 100",
    # ── Paywall DA-adjustment (pa_gap_v1, 2026-08-12) ──────────────────────────
    # SUPERSEDES the pa_cal_* shift-and-scale above. That remap rewrote PA — the
    # one thing we actually measured — by dividing a publisher's WITHIN-site PA
    # spread by a free reference σ pooled across a DA±8 window. The pooled σ
    # carries BETWEEN-site variance, so the ratio was apples-to-oranges: it drove
    # the slope to its 2.0 cap and manufactured a Boston Globe OU of +31.7 when
    # the highest OU ever observed in 4,896 master rows is +25.4.
    #
    # The real mismatch is the BAR, not the page. DA is measured across the whole
    # domain — bostonglobe.com is DA 91 on the strength of free news — while the
    # recipe sits behind the wall. So we hold a gated page to an expectations bar
    # set by an ungated domain. Fix the bar: discount DA, leave PA alone.
    #
    # The discount is a JUDGMENT and is stored as one — never overwriting
    # `domain_authority`, and always beside the method id and the inputs that
    # produced it, so a later method change can't silently re-mean old rows.
    "paywall_da_discount_pct": "REAL",   # % haircut applied to measured DA
    "paywall_da_adjusted": "REAL",       # the resulting DA (derived, for display)
    "paywall_adj_method": "TEXT",        # formula id, e.g. 'pa_gap_v1'
    "paywall_adj_inputs": "TEXT",        # JSON: every input behind the number
    "paywall_adj_n": "INTEGER",          # sample size (thin => low confidence)
    "paywall_adj_at": "TEXT",            # when computed (ISO)
    # WHO set the discount. 'measured' = computed by the calibration job and
    # owned by it; 'manual' = a curator disagreed with the computed number and
    # set it by hand. The job REFUSES to overwrite a manual row — without this
    # marker a hand-set value would survive exactly until the next scheduled
    # run and then vanish with no trace, which is worse than not allowing the
    # override at all.
    "paywall_adj_source": "TEXT",        # 'measured' | 'manual'
    # WHY a flagged publisher has no discount. Written for EVERY flagged
    # publisher on every run, not just the adjusted ones: 'no adjustment' has
    # four distinct causes (not starved / too few rows / gap inside the noise /
    # never harvested) and they call for different actions. Without this they
    # are indistinguishable from a publisher nothing ever looked at.
    "paywall_adj_status": "TEXT",        # adjusted | inconclusive | low_confidence | no_rows | no_penalty | no_free_reference | manual
    "paywall_adj_note": "TEXT",          # one-line human-readable reason
    # The publisher's recipe URL path segment (e.g. 'recipes', 'recipe', 'cooking').
    # NOT assumed — detected per publisher (collections_lib.detect_recipe_path) and
    # stored here, curator-overridable. '' = not yet detected.
    "recipe_path": "TEXT NOT NULL DEFAULT ''",
    # VERBATIM Google query for the publisher harvest (e.g. 'site:bostonglobe.com
    # recipe'). When set, it's run as-is via SerpAPI and OVERRIDES recipe_path — the
    # curator owns the Google syntax. '' = fall back to recipe_path detection.
    "serp_query": "TEXT NOT NULL DEFAULT ''",
    # 1 = this publisher has a mechanical way to enumerate recipes (refresh runs).
    # 0 = no mechanical access (no clean index / JS-only / hard paywall) → refresh
    # skips it so it doesn't pollute the list. Curator-set.
    "harvestable": "INTEGER NOT NULL DEFAULT 1",
    # SERP search depth on refresh: how many ~10-result pages to fetch (= discover_n
    # /10). More pages = more candidates + (Scale SERP) more credits + slower when
    # the recipe-check fetches each. Default 4 (~40 candidates).
    "search_pages": "INTEGER NOT NULL DEFAULT 4",
    # Discovery source for the publisher harvest: 'serp' (Google site: search) or
    # 'backlinks_file' (local SEMrush export, ranked by referring domains — the better
    # method for big sites with NO clean recipe subdir, where site: scatters and most
    # candidates are rejects). Curator-set; PERSISTED so the choice sticks across edits
    # / reloads (was previously read only at refresh time and lost). Default 'serp'.
    "harvest_source": "TEXT NOT NULL DEFAULT 'serp'",
    # SEMrush Rank — global ordinal by organic search TRAFFIC (rank 1 = most
    # traffic), looked up from the semrush_ranks reference table (see
    # input/pipeline/semrush_ranks.py). An independent traffic-authority signal
    # alongside DA/PA. NULL when the domain isn't in the (top-N, per-region) file.
    # Stamped at create + by refresh_all_semrush_ranks; NOT hand-edited (managed,
    # like the recipe counts) → kept out of EDITABLE_FIELDS. The corpus-relative
    # "rank the rank" is DERIVED, never stored (adding one domain reshuffles all).
    "semrush_rank": "INTEGER",
    "semrush_rank_at": "TEXT",   # when the rank was last stamped (ISO)
}

# Harvest scheduling — the SEMrush human-workflow loop (docs/semrush-harvest-
# scheduling.md). A "harvest" of a domain is the semi-automated SEMrush export:
# the system tells the curator WHICH domains are due, hands them a one-click deep-
# link into SEMrush, they press Run + Save the export, the watched inbox routes the
# file back here by its `{domain}` filename prefix, the existing backlinks_file
# pipeline ingests it, and on success we stamp `last_harvested_at` → the derived
# `next_harvest_at` rolls forward → the row drops off the "Due today" worklist.
# All on the domain row — NO separate task table (per the curator: "just a
# different view of the data incl. the scheduling stuff in the domain record").
_SCHEDULE_COLUMNS = {
    # When this domain's recipes were last successfully (re)harvested (ISO). NULL =
    # never harvested → "new" on the worklist. MANAGED (stamped by mark_harvested on
    # a successful backlinks_file ingest), not hand-edited.
    "last_harvested_at": "TEXT",
    # Per-domain refresh cadence in days (default ~quarterly). next_harvest_at =
    # last_harvested_at + this. Curator-overridable, like search_pages.
    "harvest_ttl_days": "INTEGER NOT NULL DEFAULT 90",
    # The editable one-click deep-link that opens SEMrush at THIS domain's Indexed-
    # Pages report (subfolder filter pre-applied) — the hotlink the curator clicks
    # from the worklist, then just presses Run + Export. '' = not captured yet (the
    # worklist still lists the domain; the link is just absent). Curator-owned, like
    # serp_query, so a SEMrush URL re-skin is a per-row field edit, not a code change.
    "semrush_report_url": "TEXT NOT NULL DEFAULT ''",
    # OPTIONAL per-domain override folder for THIS domain's SEMrush export. Blank =
    # use the configured inbox folder (system_config.semrush_inbox_dir → Downloads).
    # The harvest reads the file DIRECTLY from here — no copy into input/.
    "backlinks_dir": "TEXT NOT NULL DEFAULT ''",
    # OPTIONAL per-domain EXCLUSIONARY section words (comma/space-separated, e.g.
    # "restaurant chef news holiday event jobs"). A URL whose path has a SECTION matching
    # one is skipped outright — this site's taxonomy says it's not a recipe — overriding
    # any incidental food word (/restaurant/coppa/). PER-DOMAIN, never global. See
    # url_word_lists.url_excluded_by_domain.
    "exclude_words": "TEXT NOT NULL DEFAULT ''",
}

_RENDER_COLUMNS = {
    # JS-rendered publisher hint. 1 = the article body is injected client-side, so a
    # static (render=False) fetch sees only the nav shell and the is-recipe verify
    # under-scores real recipes (Boston Globe). When set, the harvest fetches this
    # domain with a real browser (unblocker render=True) up front instead of paying a
    # wasted plain pass first. AUTO-LEARNED: the harvest sets it the first time a
    # render-escalation rescues a recipe here; also curator-editable. Needs an
    # unblocker-capable fetch_strategy + BYOK creds to take effect.
    "render_required": "INTEGER NOT NULL DEFAULT 0",
    # When render_required was last auto-learned by a render-escalation (ISO). MANAGED.
    "render_learned_at": "TEXT",
    # Harvest MODE persistence: 1 = the curator's default for this domain is the
    # "Curate · score & pick" mode (score every candidate URL-only, select nothing,
    # skip auto-extract). It's what distinguishes Curate from Blocked (both use
    # fetch_strategy=unblocker), so without persisting it the Curate choice was lost
    # on save. Editable via the harvest-mode picker; the form's Save now stores it.
    "score_only": "INTEGER NOT NULL DEFAULT 0",
}

# Editorial provenance the curator can set (optional). cuisine_focus already lives in
# the base CREATE; ethnicity (cultural origin) is added here so PRE-EXISTING DBs migrate.
_EDITORIAL_COLUMNS = {
    "ethnicity": "TEXT",
}

# Deep-enrich fields (Moz V3 FACTS + LLM-research STORY). The short `story` stays a
# 1-2 sentence blurb; `profile` is the long researched bio. `brand_authority` /
# `referring_domains` come from Moz V3 (input/pipeline/moz_v3.py); `ranking_keywords`
# is a JSON list [{keyword, volume, rank}] of what the publisher ranks for (grounds the
# LLM story in what they're genuinely authoritative on). See extract/domain_enrich.py
# deep_enrich_domain + project_domain_master.
_ENRICH_COLUMNS = {
    # TRUST EXTRACTION: this publisher embeds real recipes in an unconventional structure
    # (no "Ingredients" header, in article prose — e.g. Boston Globe) that the cheap
    # is-recipe gate + LLM cascade wrongly drop, but the full EXTRACTOR decodes fine. When
    # 1, the harvest KEEPS this domain's candidates past the structure gate AND the cascade
    # poor_quality/not_recipe CATCH, so they reach the extractor. Safe when paired with a
    # SEMrush URL filter (pre-narrows to recipe URLs). See build_query_batch + isrecipe_cascade.
    "trust_extraction": "INTEGER NOT NULL DEFAULT 0",
    "profile": "TEXT NOT NULL DEFAULT ''",
    "brand_authority": "INTEGER",
    "referring_domains": "INTEGER",
    "ranking_keywords": "TEXT NOT NULL DEFAULT ''",   # JSON list
    "enriched_at": "TEXT",                            # when deep-enrich last ran (ISO)
}

# SEMrush deep-link builder fields (per-domain) — used to GENERATE the Top-Pages /
# Organic-Pages report URL with an Advanced Filter, so the exported page list is
# PRE-FILTERED (e.g. keyword containing "Recipe") before it ever reaches our own
# filters. The report is the ORGANIC pages view (what a domain ranks for in organic
# search — excludes paid/promoted); toppages is an alias of organic/pages (same data).
#
# REMOVED 2026-08-12: the six advanced-filter columns (semrush_db,
# semrush_search_type, semrush_filter_{word,field,include,criterion}) that fed the
# URL generator. They existed to build SEMrush's Advanced Filter from per-domain
# config, and the generator re-derived semrush_report_url on every read — which
# silently reverted any hand-built link. The workflow is now two modes: take the
# seeded default, or paste a URL built in the SEMrush UI. See
# seed_semrush_pages_url for the full argument.
_SEMRUSH_FILTER_COLUMNS = {
    # ── OBTAINABILITY (R3, 2026-08-13) ───────────────────────────────────────
    # The axis `paywall` could not express. Measured 2026-08-13:
    # cooking.nytimes.com and 177milkstreet.com carry IDENTICAL flags
    # (paywall=1, unblocker, render_required=1) and cost 1.1 vs 54 unblocker
    # calls per saved recipe. `paywall` is a BUSINESS fact — is this publisher
    # gated. Whether we can actually obtain the recipe is a TECHNICAL fact, it
    # is orthogonal, and it is the one that should drive spending.
    #
    # MEASURED, never curated: set from run outcomes, so ATK and Milk Street
    # differ by evidence rather than by a hand-written special case. A per-domain
    # exception drifts; a measured capability maintains itself.
    #
    # 'never' gates FETCHING ONLY, never membership — the publisher keeps its
    # DA/PA and stays scoreable, rankable and linkable. We stop trying to hold
    # its recipes; we do not stop pointing at them.
    "content_obtainable": "TEXT NOT NULL DEFAULT 'unknown'",  # unknown|direct|unblocker|unblocker_render|never
    "obtainable_at": "TEXT",           # when last determined (ISO)
    "obtainable_n": "INTEGER",         # saves behind a positive verdict
    "obtainable_tried": "INTEGER",     # attempts behind it — n/tried is the YIELD
    "obtainable_streak": "INTEGER NOT NULL DEFAULT 0",  # consecutive runs that saved NOTHING
    # R4. CURATED, unlike the measured columns above — it is a decision ("stop
    # paying to find out") that the measurement informs but does not make. The
    # harvest still discovers, scores and ranks; only the paid content fetch is
    # skipped, and ingestion routes to the curator's signed-in browser.
    "human_capture_only": "INTEGER NOT NULL DEFAULT 0",
    # CURATED. 1 = the domain's authority is earned by non-recipe content, so its
    # recipe pages are judged against a bar they did not build. Widens the
    # pa_gap_v1 calibration beyond gated publishers — same fault, different cause.
    # See the EDITABLE_FIELDS entry for why this cannot be inferred.
    "mixed_media": "INTEGER NOT NULL DEFAULT 0",
    # VESTIGIAL LATCH, kept deliberately and pinned to 1 on every row. Nothing
    # regenerates semrush_report_url any more, so this flag no longer decides
    # anything — it stays as a belt-and-braces guard in case a generation path
    # survived the removal, and so a re-introduced generator would find every
    # existing row already opted out. Drop it once that confidence is earned.
    "semrush_url_uncoupled": "INTEGER NOT NULL DEFAULT 1",
}

# Poor-publisher signal (2026-07-08). The is-recipe LLM cascade tags harvest pages
# recipe|not_recipe|poor_quality; a domain whose pages are repeatedly poor_quality
# (a messy source we can't extract cleanly) is a POOR PUBLISHER. We roll the cascade
# verdicts up per domain (from training.db) and, past a sample+fraction threshold,
# flag it here so the harvest STOPS paying the per-page LLM cascade for its pages and
# the curator can review it. MANAGED (recomputed by refresh_poor_publisher_flags), not
# hand-edited → kept out of EDITABLE_FIELDS.
_QUALITY_COLUMNS = {
    "poor_quality_flag": "INTEGER NOT NULL DEFAULT 0",   # 1 = flagged poor publisher
    "poor_quality_rate": "REAL",                         # fraction of poor_quality verdicts
    "poor_quality_samples": "INTEGER",                   # cascade-classified pages behind it
    "poor_quality_flagged_at": "TEXT",                   # when last (re)computed (ISO)
}


def ensure_domains_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domains (
            domain              TEXT PRIMARY KEY,
            root_domain         TEXT NOT NULL DEFAULT '',
            display_name        TEXT NOT NULL DEFAULT '',
            story               TEXT,
            logo_url            TEXT,
            country             TEXT,
            language            TEXT,
            cuisine_focus       TEXT,
            ethnicity           TEXT,
            fetch_strategy      TEXT NOT NULL DEFAULT 'plain',
            extract_notes       TEXT,
            allowed             INTEGER NOT NULL DEFAULT 1,
            domain_authority    REAL,
            da_last_scored      TEXT,
            notes               TEXT,
            master_recipe_count INTEGER NOT NULL DEFAULT 0,
            user_recipe_count   INTEGER NOT NULL DEFAULT 0,
            counts_updated_at   TEXT,
            created_at          TEXT NOT NULL DEFAULT '',
            updated_at          TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # Migrate pre-existing tables that lack the count columns.
    have = {r[1] for r in conn.execute("PRAGMA table_info(domains)")}
    for col, decl in _COUNT_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    # Paywall PA-calibration (2026-06-16): premium/gated publishers earn far less
    # PAGE authority than free sites at the same DA, so OU penalizes them unfairly.
    # We store a per-publisher shift-and-scale calibration so the scorer can remap
    # their PA to a free-equivalent. `paywall=1` flags a gated premium publisher;
    # pa_cal_* are this publisher's own PA mean/std/n; pa_cal_free_* are the matched-
    # DA free reference at calibration time. SUPERSEDED — see paywall_da_* above.
    # NOTE (2026-08-12): the pa_cal_* half of this block is SUPERSEDED by the
    # paywall_da_* columns in the same dict — see the comment there. The old
    # columns stay readable so historic calibrations remain inspectable.
    for col, decl in _PAYWALL_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    # Harvest-scheduling columns (the SEMrush human-workflow loop).
    for col, decl in _SCHEDULE_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    for col, decl in _RENDER_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    for col, decl in _EDITORIAL_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    for col, decl in _ENRICH_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    for col, decl in _SEMRUSH_FILTER_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    for col, decl in _QUALITY_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_domains_root ON domains(root_domain)"
    )
    conn.commit()


def set_paywall_calibration(conn, domain, *, da=None, pa_mean, pa_std, pa_n,
                            free_mean, free_std, paywall=True):
    """Store/refresh a premium publisher's PA-calibration on its domains row
    (creating a minimal row if absent). Recomputed by the harvester; read by the
    scorer to remap gated PA to free-equivalent."""
    ensure_domains_table(conn)
    host = _canon_host(domain)
    now = _now()
    if not domain_exists(conn, host):
        conn.execute(
            "INSERT INTO domains (domain, root_domain, created_at, updated_at) VALUES (?,?,?,?)",
            (host, root_domain(host) or host, now, now),
        )
    sets = ["paywall = ?", "pa_cal_mean = ?", "pa_cal_std = ?", "pa_cal_n = ?",
            "pa_cal_free_mean = ?", "pa_cal_free_std = ?", "pa_cal_at = ?", "updated_at = ?"]
    params = [1 if paywall else 0, pa_mean, pa_std, pa_n, free_mean, free_std, now, now]
    if da is not None:
        sets.append("domain_authority = ?"); params.append(da)
    params.append(host)
    conn.execute(f"UPDATE domains SET {', '.join(sets)} WHERE domain = ?", params)
    conn.commit()


def get_paywall_calibrations(conn=None, db_path: str = _DEFAULT_DB) -> list:
    """Flagged premium publishers with a usable calibration — for the scorer AND
    the batch winner-selector. Returns dicts {domain, da, pa_mean, pa_std,
    free_mean, free_std}; only rows with a positive pa_cal_std (a real spread to
    scale against) and a solid sample (n>=15). Pass a `conn`, or omit it to open
    `db_path` (lets build_query_batch read it connection-free)."""
    own = conn is None
    if own:
        conn = _connect(db_path)
    try:
        if own:
            ensure_domains_table(conn)
        rows = conn.execute(
            "SELECT domain, domain_authority, pa_cal_mean, pa_cal_std, "
            "pa_cal_free_mean, pa_cal_free_std FROM domains "
            "WHERE paywall = 1 AND pa_cal_mean IS NOT NULL AND pa_cal_std > 0 "
            "AND pa_cal_free_mean IS NOT NULL AND pa_cal_free_std IS NOT NULL "
            # Ignore thin/unreliable calibrations (e.g. a publisher with too few
            # discovered recipe pages) so the remap only fires on solid samples.
            "AND COALESCE(pa_cal_n, 0) >= 15"
        ).fetchall()
        return [{"domain": r[0], "da": r[1], "pa_mean": r[2], "pa_std": r[3],
                 "free_mean": r[4], "free_std": r[5]} for r in rows]
    except Exception:
        return []
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def set_paywall_da_adjustment(conn, domain, *, discount_pct, da_adjusted,
                              method, inputs, n, note=None):
    """Persist a publisher's DA haircut TOGETHER with the method and inputs that
    produced it. `domain_authority` (the Moz measurement) is deliberately not
    touched: the measured DA is a fact, the discount is our judgment about how
    much of that domain-wide authority the gated section actually inherits, and
    the two must stay separately readable."""
    ensure_domains_table(conn)
    host = _canon_host(domain)
    now = _now()
    if not domain_exists(conn, host):
        conn.execute(
            "INSERT INTO domains (domain, root_domain, created_at, updated_at) VALUES (?,?,?,?)",
            (host, root_domain(host) or host, now, now),
        )
    conn.execute(
        "UPDATE domains SET paywall_da_discount_pct = ?, paywall_da_adjusted = ?, "
        "paywall_adj_method = ?, paywall_adj_inputs = ?, paywall_adj_n = ?, "
        "paywall_adj_source = 'measured', paywall_adj_status = 'adjusted', "
        "paywall_adj_note = ?, paywall_adj_at = ?, updated_at = ? WHERE domain = ?",
        (discount_pct, da_adjusted, method, json.dumps(inputs, sort_keys=True),
         n, note, now, now, host),
    )
    conn.commit()


def clear_paywall_da_adjustment(conn, domain, *, status=None, note=None, n=None) -> None:
    """Drop a publisher's DA adjustment (the paywall flag was wrong, or the
    evidence no longer supports a haircut). Clears to NULL, not 0 — an absent
    adjustment must not read as 'measured, and it came out zero'.

    `status`/`note` record WHY there is no adjustment, so 'not starved',
    'too few rows', 'inside the noise' and 'never harvested' stay tellable
    apart on the domain record instead of all rendering as a blank field."""
    ensure_domains_table(conn)
    conn.execute(
        "UPDATE domains SET paywall_da_discount_pct = NULL, paywall_da_adjusted = NULL, "
        "paywall_adj_method = NULL, paywall_adj_inputs = NULL, paywall_adj_n = ?, "
        "paywall_adj_source = ?, paywall_adj_status = ?, paywall_adj_note = ?, "
        "paywall_adj_at = ?, updated_at = ? WHERE domain = ?",
        (n, "measured" if status else None, status, note, _now(), _now(),
         _canon_host(domain)),
    )
    conn.commit()


# How many consecutive zero-save runs before we stop fetching a publisher. Two,
# not one: a single bad run is a bad export, an outage, or a site change. Two in
# a row is a property of the publisher.
OBTAINABLE_FAIL_STREAK = 2


def record_acquisition_outcome(conn, domain: str, *, attempted: int, saved: int,
                               method: str) -> dict:
    """Learn whether this publisher's recipes can actually be obtained, from what
    a run just did. Called once per publisher refresh.

    `method` is how the run fetched ('direct' | 'unblocker' | 'unblocker_render').
    A save PROVES obtainability by that method. A run that attempted real
    extractions and saved NOTHING is evidence against; `OBTAINABLE_FAIL_STREAK`
    consecutive such runs marks the domain `never`.

    Deliberately asymmetric: one success clears the streak instantly, because
    obtainability is proven by a single existence case, while impossibility can
    only ever be inferred from repetition. Cheaper to re-try a recovered
    publisher than to permanently write off a working one.
    """
    ensure_domains_table(conn)
    host = _canon_host(domain)
    # Create a minimal row if absent, mirroring set_paywall_da_adjustment. Without
    # this the UPDATE below silently matches zero rows and the fail streak never
    # advances — the verdict would reset on every run and `never` could never be
    # reached. (Caught by the state-machine test, where two zero-save runs both
    # reported streak=1.)
    now0 = _now()
    if not domain_exists(conn, host):
        conn.execute(
            "INSERT INTO domains (domain, root_domain, created_at, updated_at) VALUES (?,?,?,?)",
            (host, root_domain(host) or host, now0, now0))
        conn.commit()
    row = get_domain(conn, host) or {}
    prev = (row.get("content_obtainable") or "unknown").lower()
    streak = int(row.get("obtainable_streak") or 0)
    now = _now()

    if saved > 0:
        verdict, streak = (method or "unknown"), 0
        pct = (100.0 * saved / attempted) if attempted else 0.0
        # YIELD, not just the verdict. One save proves obtainability, so a
        # publisher that yields 1 of 17 reads "obtainable" — technically true and
        # practically useless. 177milkstreet did exactly that at 54 unblocker
        # calls per save. Recording saved/tried lets the record say "works, badly"
        # instead of forcing that into a binary it does not fit, and leaves the
        # write-off decision with a human who can see the number.
        note = f"{saved} of {attempted} extracted via {method} ({pct:.0f}% yield)"
    elif attempted <= 0:
        # Nothing was even tried (score-only, or everything cut before extract).
        # Not evidence either way — leave the verdict and the streak alone.
        return {"domain": host, "content_obtainable": prev, "streak": streak,
                "changed": False, "note": "no extraction attempted"}
    else:
        streak += 1
        verdict = "never" if streak >= OBTAINABLE_FAIL_STREAK else prev
        note = (f"0 of {attempted} extracted (streak {streak}/{OBTAINABLE_FAIL_STREAK})")

    conn.execute(
        "UPDATE domains SET content_obtainable = ?, obtainable_at = ?, "
        "obtainable_n = ?, obtainable_tried = ?, obtainable_streak = ?, "
        "updated_at = ? WHERE domain = ?",
        (verdict, now,
         saved if saved > 0 else row.get("obtainable_n"),
         attempted if saved > 0 else row.get("obtainable_tried"),
         streak, now, host))
    conn.commit()
    return {"domain": host, "content_obtainable": verdict, "streak": streak,
            "saved": saved, "tried": attempted,
            "changed": verdict != prev, "note": note}


def content_obtainable(conn, domain: str) -> str:
    """'unknown' | a working method | 'never'. Read before spending on fetches."""
    try:
        row = conn.execute("SELECT content_obtainable FROM domains WHERE domain = ?",
                           (_canon_host(domain),)).fetchone()
    except Exception:
        return "unknown"
    return ((row[0] if row else None) or "unknown").lower()


def human_capture_only(conn, domain: str) -> bool:
    """R4 — is this publisher's recipe BODY unobtainable by the server at any price?

    True means: discover, score and rank its URLs as normal, but never spend a
    fetch trying to ingest one. 177milkstreet returns title, hero, headnote and
    then "To access this recipe, you need to be a member" — measured yield 1 of 9
    (11%), and the one that worked had its method above the paywall.

    Checked at the two places money is spent: the harvest's winner-extract loop
    and /domains/<d>/process-selected. Falls OPEN (False) on any error — a
    lookup failure must not silently stop a publisher being harvested.
    """
    try:
        row = conn.execute("SELECT human_capture_only FROM domains WHERE domain = ?",
                           (_canon_host(domain),)).fetchone()
    except Exception:
        return False
    return bool(row and row[0])


# Longest hint we will paste into an extraction prompt. Generous enough for real
# publisher guidance, short enough that a field someone pasted an essay (or a
# whole page) into cannot quietly dominate the system prompt.
EXTRACT_HINT_MAX_CHARS = 1200

# A hint that is only a URL is not guidance — `extract_notes` has been used as a
# scratchpad (cooking.nytimes.com holds a pasted SEMrush link), and pasting that
# into an extraction prompt would be worse than sending nothing.
_URLISH_RE = re.compile(r"^\s*<?https?://\S+>?\s*$", re.IGNORECASE)


def extract_hint_for_url(url_or_host: str, db_path: str = _DEFAULT_DB) -> str:
    """Publisher-specific extraction guidance for this URL, or '' when none.

    `extract_notes` has existed since the table was created, is described in this
    module's own header as "capture hints for the harvest", is editable in the
    domain form — and until now was read by NOTHING. This is the read side.

    The text goes into the extraction system prompt as publisher context. It is
    prose written by the curator for the model, deliberately not a mini-language:
    the same free-text-hint shape the review extractors already use per source
    ([[project_review_extractor_variants]]).

    Resolves by walking UP the host labels, because `domains` is keyed at
    full-host grain while a recipe URL may sit on a subdomain — the same walk
    `adjustment_for_url` does, and the reason an equality check silently matched
    0 of NYT's rows.

    Never raises: a hint is an enhancement, and a lookup failure must not stop an
    extraction that would otherwise succeed.
    """
    try:
        host = _canon_host(url_or_host)
        if not host:
            return ""
        labels = host.split(".")
        candidates = [".".join(labels[i:]) for i in range(max(1, len(labels) - 1))]
        import sqlite3 as _sq
        with _sq.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5) as conn:
            for cand in candidates:
                row = conn.execute(
                    "SELECT extract_notes FROM domains WHERE domain = ?", (cand,)
                ).fetchone()
                note = (row[0] or "").strip() if row else ""
                if not note or _URLISH_RE.match(note):
                    continue
                return note[:EXTRACT_HINT_MAX_CHARS]
    except Exception:
        return ""
    return ""


def paywall_adjustment_is_manual(conn, domain) -> bool:
    """True when a curator owns this publisher's discount. The calibration job
    checks this before writing: a hand-set value that the next scheduled run
    silently reverts is worse than no override at all."""
    try:
        row = conn.execute(
            "SELECT paywall_adj_source FROM domains WHERE domain = ?",
            (_canon_host(domain),)).fetchone()
    except Exception:
        return False
    return bool(row and (row[0] or "").lower() == "manual")


def get_paywall_da_adjustments(conn=None, db_path: str = _DEFAULT_DB) -> dict:
    """{host: {discount_pct, da_adjusted, method, n, inputs}} for every publisher
    carrying a DA adjustment. Keyed by the domains row's own FULL-HOST key, which
    is what `adjustment_for_url` resolves against."""
    own = conn is None
    if own:
        conn = _connect(db_path)
    try:
        if own:
            ensure_domains_table(conn)
        rows = conn.execute(
            "SELECT domain, paywall_da_discount_pct, paywall_da_adjusted, "
            "paywall_adj_method, paywall_adj_n, paywall_adj_inputs FROM domains "
            "WHERE paywall_da_discount_pct IS NOT NULL AND paywall_da_discount_pct > 0"
        ).fetchall()
        out = {}
        for r in rows:
            try:
                inputs = json.loads(r[5]) if r[5] else {}
            except Exception:
                inputs = {}
            out[r[0]] = {"discount_pct": r[1], "da_adjusted": r[2],
                         "method": r[3], "n": r[4], "inputs": inputs}
        return out
    except Exception:
        return {}
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def adjustment_for_url(url_or_host: str, adjustments: dict) -> Optional[dict]:
    """Resolve a URL (or host) to its publisher adjustment, walking UP the label
    chain: cooking.nytimes.com → nytimes.com → com, first match wins.

    Why not a plain equality check on `_scoring.rootDomain`: that field holds the
    APEX ('nytimes.com') while `domains` is canonical at FULL-HOST grain
    ('cooking.nytimes.com'), and there is no domains row for the apex. Measured
    2026-08-12, that grain mismatch meant the paywall flag on cooking.nytimes.com
    matched exactly 0 of its 89 master rows — the adjustment would have silently
    no-opped for every subdomain publisher. So resolve from the URL's real host,
    which is the fact, rather than from the derived apex.
    """
    if not adjustments:
        return None
    host = _canon_host(url_or_host)
    if not host:
        return None
    labels = host.split(".")
    for i in range(len(labels) - 1):
        hit = adjustments.get(".".join(labels[i:]))
        if hit:
            return hit
    return None


def seed_domains(conn: sqlite3.Connection) -> int:
    """One-time bootstrap from the shipped JSON name map. INSERT OR IGNORE so
    curator edits to the table are never clobbered on a later boot (same as
    the chapters seed). Returns the number of rows inserted."""
    ensure_domains_table(conn)
    try:
        with open(_SEED_PATH, encoding="utf-8") as f:
            seed = json.load(f)
    except Exception:
        return 0
    now = _now()
    inserted = 0
    for raw_host, name in seed.items():
        host = _canon_host(raw_host)
        if not host:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO domains
                (domain, root_domain, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (host, root_domain(host) or host, name, now, now),
        )
        inserted += cur.rowcount or 0
    conn.commit()
    invalidate_cache()
    return inserted


# --- Harvest scheduling (the SEMrush human-workflow worklist) -----------------
# A domain is part of the SEMrush manual flow when its discovery source is the
# backlinks file OR it carries a captured SEMrush report URL. For those, we derive
# the schedule on read (never stored — adding a domain or editing a TTL can't leave
# it stale, exactly like bcc_rank and the dish next_run_at).

def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def semrush_pages_base_url() -> str:
    """Base SEMrush report URL the deep-link builder targets (params added in code, so
    NO `{domain}` here). Default = the Top-Pages / Organic-Pages report (organic-search
    demand; excludes paid). Config-overridable (system_config `semrush_pages_base_url`);
    e.g. flip to `.../analytics/organic/pages/` — same organic data, different path."""
    from input.pipeline import system_config
    return (system_config.get_setting(
        "semrush_pages_base_url",
        "https://www.semrush.com/analytics/toppages/") or "").strip()


def seed_semrush_pages_url(host: str) -> str:
    """The starting SEMrush Top-Pages link for a NEWLY created domain — written
    once, at create, and thereafter owned by the curator.

    Deliberately the simplest URL that works: db + host + searchType, no filter.
    The previous version built SEMrush's Advanced Filter from six per-domain
    columns, and that was a mistake in two ways. It could not express what the
    SEMrush UI can (multi-condition AND filters), so complex publishers —
    cooking.nytimes.com and the like — always ended up hand-built anyway. And it
    RE-DERIVED the URL on every read, so a hand-tuned link silently reverted:
    measured 2026-08-12, several rows carried `db=gr` while every row's
    `semrush_db` said `us`, meaning the generator rewrote those Greek reports to
    the US database each time the form displayed them.

    The workflow is therefore two modes and no third: take this default, or
    build the query in SEMrush and paste the URL in. Copy-paste already
    expresses everything the generator was trying to reach, at no risk of an
    undocumented filter code quietly filtering the wrong column.
    """
    from urllib.parse import urlencode
    host = (host or "").strip()
    base = semrush_pages_base_url()
    if not host or not base:
        return ""
    return base.split("?")[0] + "?" + urlencode(
        {"db": "us", "q": host, "searchType": "domain"})


def _derive_schedule(d: dict) -> None:
    """Stamp DERIVED harvest-schedule fields onto a domain dict in place:
      (semrush_report_url is NOT derived here any more — it is a stored,
       curator-owned value; see the note in the body.)
      - next_harvest_at : last_harvested_at's date + harvest_ttl_days (None if the
                          domain isn't SEMrush-managed; '' last → never)
      - harvest_status  : 'new' (never harvested) | 'due' (next <= today) | 'ok'
                          | None (not part of the SEMrush flow)
      - harvest_due     : bool — convenience for the worklist filter
    next_harvest is date-grain (the cadence is days); a missing/garbage timestamp
    reads as 'new' rather than raising."""
    # Worklist membership = the SEMrush backlinks-file flow ONLY. Deliberately NOT
    # keyed on semrush_report_url — that link exists for every domain, so keying on
    # it would put the whole corpus on the worklist.
    managed = d.get("harvest_source") == "backlinks_file"
    # semrush_report_url is NO LONGER derived here. It used to be regenerated on
    # every read from six per-domain filter columns, which meant a curator's
    # hand-built URL was silently overwritten by a worse one the moment the form
    # displayed it. It is now a plain stored value: seeded once at create
    # (seed_semrush_pages_url) and owned by the curator from then on. The 2026-08-12
    # migration materialized the last derived value into the column for all 322
    # rows, so nothing was lost when the generator went away.
    d["next_harvest_at"] = None
    d["harvest_status"] = None
    d["harvest_due"] = False
    if not managed:
        return
    last = (d.get("last_harvested_at") or "").strip()
    if not last:
        d["harvest_status"] = "new"
        d["harvest_due"] = True
        return
    try:
        ttl = int(d.get("harvest_ttl_days") or 90)
        nxt = (datetime.fromisoformat(last).date()
               + timedelta(days=ttl)).isoformat()
    except Exception:
        d["harvest_status"] = "new"
        d["harvest_due"] = True
        return
    d["next_harvest_at"] = nxt
    if nxt <= _today():
        d["harvest_status"] = "due"
        d["harvest_due"] = True
    else:
        d["harvest_status"] = "ok"


def mark_harvested(conn: sqlite3.Connection, domain: str,
                   when: Optional[str] = None) -> None:
    """Stamp a successful harvest → resets the derived next_harvest_at and drops the
    domain off the worklist. Called by the publisher_refresh job on a successful
    backlinks_file ingest (covers BOTH the manual refresh button and the watched-
    inbox path). Best-effort; never raises into the job."""
    host = _canon_host(domain)
    try:
        ensure_domains_table(conn)
        conn.execute("UPDATE domains SET last_harvested_at = ? WHERE domain = ?",
                     (when or _now(), host))
        conn.commit()
        invalidate_cache()
    except Exception:
        pass


def mark_render_required(domain: str, conn: Optional[sqlite3.Connection] = None,
                         db_path: str = _DEFAULT_DB) -> None:
    """Auto-learn the JS-rendered hint: the first time a render-escalation rescues a
    recipe on this domain, flag it so future harvests fetch it with a real browser up
    front (skipping the wasted plain pass). Idempotent — only writes if not already
    set. Best-effort; never raises into a job. Pass a `conn`, or omit to open
    `db_path` (lets the out-of-process harvest call it connection-free)."""
    own = conn is None
    if own:
        conn = _connect(db_path)
    try:
        ensure_domains_table(conn)
        host = _canon_host(domain)
        row = conn.execute("SELECT render_required FROM domains WHERE domain = ?",
                           (host,)).fetchone()
        if row is None or row[0]:           # absent row, or already flagged → nothing to do
            return
        conn.execute("UPDATE domains SET render_required = 1, render_learned_at = ?, "
                     "updated_at = ? WHERE domain = ?", (_now(), _now(), host))
        conn.commit()
        invalidate_cache()
    except Exception:
        pass
    finally:
        if own:
            conn.close()


def harvest_worklist(conn: sqlite3.Connection) -> list[dict]:
    """The "Due today" worklist: every SEMrush-managed, allowed domain that is NEW
    (never harvested) or DUE/overdue, each carrying its deep-link + schedule fields.
    Ordered new-first, then by oldest next_harvest_at (most overdue first). A plain
    view over the domains rows — no separate table."""
    out = [d for d in list_domains(conn)
           if d.get("allowed") and d.get("harvest_due")]
    out.sort(key=lambda d: (d.get("harvest_status") != "new",
                            d.get("next_harvest_at") or ""))
    return out


def list_domains(conn: sqlite3.Connection) -> list[dict]:
    ensure_domains_table(conn)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM domains ORDER BY domain COLLATE NOCASE"
    ).fetchall()]
    # Derive bcc_rank = the corpus-relative ordinal ("rank the rank"): order the
    # ALLOWED domains that HAVE a semrush_rank by it asc, assign 1..N. Blocked
    # domains (allowed=0) and null-rank domains get None. Computed here (not
    # stored) so adding/blocking a domain can't leave it stale.
    ranked = sorted(
        (d for d in rows if d.get("semrush_rank") is not None and d.get("allowed")),
        key=lambda d: d["semrush_rank"],
    )
    for i, d in enumerate(ranked, 1):
        d["bcc_rank"] = i
    for d in rows:
        d.setdefault("bcc_rank", None)
        _derive_schedule(d)
    return rows


def get_domain(conn: sqlite3.Connection, domain: str) -> Optional[dict]:
    ensure_domains_table(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM domains WHERE domain = ?", (_canon_host(domain),)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["bcc_rank"] = _semrush_rank_local(conn, d)
    _derive_schedule(d)
    return d


def domain_exists(conn: sqlite3.Connection, domain: str) -> bool:
    ensure_domains_table(conn)
    return bool(conn.execute(
        "SELECT 1 FROM domains WHERE domain = ?", (_canon_host(domain),)
    ).fetchone())


def create_domain(conn: sqlite3.Connection, domain: str, fields: dict) -> dict:
    """Insert a curator-created domain row. Raises ValueError on a blank or
    duplicate host. Unknown keys in `fields` are ignored."""
    host = _canon_host(domain)
    if not host:
        raise ValueError("Domain (host) is required")
    ensure_domains_table(conn)
    if domain_exists(conn, host):
        raise ValueError(f"Domain '{host}' already exists")
    now = _now()
    payload = {k: fields.get(k) for k in EDITABLE_FIELDS if k in fields}
    payload.setdefault("display_name", "")
    cols = ["domain", "root_domain", "created_at", "updated_at", *payload.keys()]
    vals = [host, root_domain(host) or host, now, now, *payload.values()]
    conn.execute(
        f"INSERT INTO domains ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        vals,
    )
    # Seed the SEMrush link ONCE, here, if the curator didn't supply one. From
    # this point the column is theirs: nothing regenerates it, so a hand-built
    # URL for an awkward publisher survives every later save.
    if not (payload.get("semrush_report_url") or "").strip():
        seeded = seed_semrush_pages_url(host)
        if seeded:
            conn.execute("UPDATE domains SET semrush_report_url = ? WHERE domain = ?",
                         (seeded, host))
    conn.commit()
    stamp_semrush_rank(conn, host)   # look up + persist the new domain's SEMrush rank
    invalidate_cache()
    return get_domain(conn, host)


def update_domain(conn: sqlite3.Connection, domain: str, fields: dict) -> dict:
    """Patch an existing row. Only keys in EDITABLE_FIELDS are written."""
    host = _canon_host(domain)
    if not domain_exists(conn, host):
        raise ValueError(f"Domain '{host}' not found")
    sets = {k: fields[k] for k in EDITABLE_FIELDS if k in fields}
    if not sets:
        return get_domain(conn, host)

    # A curator touching the discount takes OWNERSHIP of it. Stamp the row
    # 'manual' so the calibration job skips it; clearing the field hands it back
    # to the job. Done here rather than in the form so the guarantee holds for
    # every writer of update_domain, not just the one UI that happens to exist.
    if "paywall_da_discount_pct" in sets:
        raw = sets["paywall_da_discount_pct"]
        manual = raw not in (None, "")
        try:
            pct = float(raw) if manual else None
        except (TypeError, ValueError):
            raise ValueError("paywall_da_discount_pct must be a number, or blank to clear")
        if manual and not (0 < pct < 100):
            raise ValueError("paywall_da_discount_pct must be between 0 and 100 (exclusive)")
        sets["paywall_da_discount_pct"] = pct
        sets["paywall_adj_source"] = "manual" if manual else None
        sets["paywall_adj_status"] = "manual" if manual else None
        sets["paywall_adj_note"] = (
            "Set by hand; the calibration job will not overwrite it. "
            "Clear this field to return the publisher to automatic calibration."
            if manual else None)
        sets["paywall_adj_method"] = "manual" if manual else None
        sets["paywall_adj_at"] = _now()
        # The measured DA is a fact and is never rewritten; recompute the
        # DERIVED adjusted DA so the stored pair can't disagree with itself.
        row = get_domain(conn, host) or {}
        da = row.get("domain_authority")
        sets["paywall_da_adjusted"] = (
            round(float(da) * (1.0 - pct / 100.0), 2)
            if (manual and isinstance(da, (int, float)) and da) else None)

    # DA is a PAID Moz measurement and it goes stale, so when it changes, record
    # when. The domain form has always rendered a "DA scored <date>" pill from
    # `da_last_scored` — and nothing ever wrote the column, so the pill could not
    # appear on any of 326 rows. Stamping here is what makes the existing UI real.
    # Only on an actual CHANGE: re-saving the form with the same number is not a
    # rescore, and stamping it would quietly reset the staleness clock.
    if "domain_authority" in sets:
        _prev = (get_domain(conn, host) or {}).get("domain_authority")
        _new = sets["domain_authority"]
        try:
            _changed = (_new is not None) and (
                _prev is None or abs(float(_new) - float(_prev)) > 1e-9)
        except (TypeError, ValueError):
            _changed = _new != _prev
        if _changed:
            sets["da_last_scored"] = _now()

    sets["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(
        f"UPDATE domains SET {assignments} WHERE domain = ?",
        (*sets.values(), host),
    )
    conn.commit()
    invalidate_cache()
    if "paywall_da_discount_pct" in sets:
        # The scorers cache adjustments per process; without this the running
        # server keeps applying the OLD discount until it restarts.
        try:
            from input.pipeline.url_scoring import reset_paywall_cache
            reset_paywall_cache()
        except Exception:
            pass
    return get_domain(conn, host)


def delete_domain(conn: sqlite3.Connection, domain: str) -> bool:
    host = _canon_host(domain)
    ensure_domains_table(conn)
    cur = conn.execute("DELETE FROM domains WHERE domain = ?", (host,))
    conn.commit()
    invalidate_cache()
    return bool(cur.rowcount)


# --- SEMrush traffic rank -----------------------------------------------
# A domain's global SEMrush Rank (organic-traffic ordinal) is looked up from the
# semrush_ranks reference table and STORED on the row (a snapshot, stable until a
# refresh). The corpus-relative local rank ("rank the rank") is DERIVED on read.

def stamp_semrush_rank(conn: sqlite3.Connection, domain: str,
                       region: str = "us") -> Optional[int]:
    """Look up `domain`'s SEMrush rank in the reference table and persist it on
    its domains row. Returns the rank (or None when the domain isn't in the file).
    Best-effort — never raises if the ranks table is empty/absent."""
    from input.pipeline import semrush_ranks
    host = _canon_host(domain)
    try:
        row = semrush_ranks.get_rank(conn, host, region=region)
    except Exception:
        return None
    rank = row.get("rank") if row else None
    try:
        ensure_domains_table(conn)
        conn.execute(
            "UPDATE domains SET semrush_rank = ?, semrush_rank_at = ? WHERE domain = ?",
            (rank, _now(), host),
        )
        conn.commit()
    except Exception:
        pass
    return rank


def refresh_all_semrush_ranks(conn: sqlite3.Connection, region: str = "us") -> dict:
    """Re-stamp every domain's semrush_rank from the current semrush_ranks table —
    run after importing a fresh ranks file. Matched by the domain's exact host OR
    its two-part root (so subdomains inherit their root's rank). Returns
    {matched, total}. The two-part-root match is fiddly in pure SQL, so we build
    the rank map once and match in Python via lookup_keys — ~270 rows, trivial."""
    from input.pipeline import semrush_ranks
    ensure_domains_table(conn)
    semrush_ranks.ensure_semrush_ranks_table(conn)
    now = _now()
    rank_by_domain = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT domain, rank FROM semrush_ranks WHERE region = ?", (region,)
        ).fetchall()
    }
    matched = total = 0
    for (host,) in conn.execute("SELECT domain FROM domains").fetchall():
        total += 1
        rank = None
        for key in semrush_ranks.lookup_keys(host):
            if key in rank_by_domain:
                rank = rank_by_domain[key]
                break
        conn.execute(
            "UPDATE domains SET semrush_rank = ?, semrush_rank_at = ? WHERE domain = ?",
            (rank, now, host),
        )
        if rank is not None:
            matched += 1
    conn.commit()
    invalidate_cache()
    return {"matched": matched, "total": total, "region": region}


def _semrush_rank_local(conn: sqlite3.Connection, row: dict) -> Optional[int]:
    """BCC rank for a domain row: how it ranks among ONLY our ALLOWED domains
    that carry a SEMrush rank (1 = most-traffic publisher). DERIVED, not stored.
    Blocked domains (allowed=0) keep their raw semrush_rank but get no BCC rank —
    they aren't sourcing targets, so they'd only skew the ladder."""
    rank = row.get("semrush_rank")
    if rank is None or not row.get("allowed"):
        return None
    n = conn.execute(
        "SELECT COUNT(*) FROM domains "
        "WHERE allowed = 1 AND semrush_rank IS NOT NULL AND semrush_rank < ?",
        (rank,),
    ).fetchone()[0]
    return int(n) + 1


def recipes_for_domain(conn: sqlite3.Connection, domain: str) -> dict:
    """Live lookup of the recipes sourced from a domain, split master vs user.

    Returns ``{"master": {"count", "items"}, "user": {"count", "items"}}``
    where each item is ``{recipe_id, name}`` (lightweight — enough to fill a
    browse dropdown). Matches on the recipe's canonical HOST (derived from
    url_normalized, falling back to _source.originalUrl), consistent with the
    full-host grain of the domain master.

    Side effect: STORES the two counts (and counts_updated_at) on the domain
    row so the list view can show them without recomputing. The LISTS are not
    stored — they're cheap to recompute and would drift.
    """
    from input.pipeline.site_names import host_from_url

    host = _canon_host(domain)
    result = {
        "master": {"count": 0, "items": []},
        "user": {"count": 0, "items": []},
    }
    for tbl, bucket in (("master_recipes", "master"), ("recipes", "user")):
        try:
            # Cheap pass: pull ONLY recipe_id + url_normalized (no ~19KB data blob).
            # Match the host off url_normalized; rows with an empty url_normalized
            # fall back to the source URL's host (older rows predating the url
            # backfill — 38 such rows still carry _source.originalUrl). The data
            # blob is then parsed ONLY for candidates (matches + the few empties),
            # not the whole table — the audit's 960-blob scan → ~matches+empties.
            id_rows = conn.execute(
                f"SELECT recipe_id, url_normalized FROM {tbl}"
            ).fetchall()
        except Exception:
            continue
        match_ids: list = []      # url_normalized host == this domain
        fallback_ids: list = []   # empty url_normalized → check _source.originalUrl
        for recipe_id, url_norm in id_rows:
            h = host_from_url(url_norm or "")
            if h == host:
                match_ids.append(recipe_id)
            elif not h:
                fallback_ids.append(recipe_id)
        items = []
        need = match_ids + fallback_ids
        if need:
            mset = set(match_ids)
            placeholders = ",".join("?" * len(need))
            for recipe_id, dj in conn.execute(
                f"SELECT recipe_id, data FROM {tbl} WHERE recipe_id IN ({placeholders})",
                need,
            ):
                try:
                    d = json.loads(dj)
                except Exception:
                    d = {}
                if recipe_id in mset:
                    items.append({"recipe_id": recipe_id, "name": d.get("name") or "(no title)"})
                else:  # empty url_normalized — match on the source URL's host
                    fh = host_from_url((d.get("_source") or {}).get("originalUrl") or "")
                    if fh == host:
                        items.append({"recipe_id": recipe_id, "name": d.get("name") or "(no title)"})
        items.sort(key=lambda x: x["name"].lower())
        result[bucket]["items"] = items
        result[bucket]["count"] = len(items)

    # Persist the counts (refresh-on-access). Best-effort; never raises.
    try:
        ensure_domains_table(conn)
        if domain_exists(conn, host):
            conn.execute(
                "UPDATE domains SET master_recipe_count = ?, user_recipe_count = ?, "
                "counts_updated_at = ? WHERE domain = ?",
                (result["master"]["count"], result["user"]["count"], _now(), host),
            )
            conn.commit()
    except Exception:
        pass

    result["domain"] = host
    return result


# --- display-name cache for the hot resolver path -----------------------
# friendly_site_name() runs on every save and every master-list row, so it
# can't open a connection per call. Cache the {host: display_name} map for
# allowed-or-not rows (display is independent of the allow flag) and let the
# CRUD writers above invalidate it.

_DISPLAY_CACHE: Optional[dict] = None
_BLOCKED_CACHE: Optional[set] = None
_RENDER_CACHE: Optional[set] = None
_POOR_CACHE: Optional[set] = None
_TRUST_CACHE: Optional[set] = None


def invalidate_cache() -> None:
    global _DISPLAY_CACHE, _BLOCKED_CACHE, _RENDER_CACHE, _POOR_CACHE, _TRUST_CACHE
    _DISPLAY_CACHE = None
    _BLOCKED_CACHE = None
    _RENDER_CACHE = None
    _POOR_CACHE = None
    _TRUST_CACHE = None


def get_trust_extraction_hosts(db_path: str = _DEFAULT_DB) -> set:
    """Cached set of hosts+roots flagged `trust_extraction = 1` — publishers whose real
    recipes have an unconventional structure the cheap is-recipe gate/cascade wrongly drop
    but the extractor decodes. The harvest keeps their candidates past the structure gate +
    cascade catch. CRUD writers invalidate the cache."""
    global _TRUST_CACHE
    if _TRUST_CACHE is None:
        hosts: set = set()
        try:
            with _connect(db_path) as conn:
                ensure_domains_table(conn)
                for dom, root in conn.execute(
                    "SELECT domain, root_domain FROM domains WHERE trust_extraction = 1"
                ):
                    if dom:
                        hosts.add(dom.lower())
                    if root:
                        hosts.add(root.lower())
        except Exception:
            pass
        _TRUST_CACHE = hosts
    return _TRUST_CACHE


def parse_serp_exclusions(db_path: str = _DEFAULT_DB) -> tuple[set, list]:
    """Parse the editable `serp_exclusions` system_config list into
    (domains, terms). A line containing a dot is a DOMAIN (drives the SERP
    `-site:` exclusion AND the downstream domain filter); any other line is a
    bare TERM (`-term`, SERP-query only). Lenient: strips a stray leading '-'
    or 'site:', drops blank/`#` lines, accepts comma or newline separators.
    Returns (set(), []) on any error."""
    try:
        from input.pipeline import system_config as cfg
        raw = cfg.get_setting("serp_exclusions", "", db_path=db_path) or ""
    except Exception:
        return set(), []
    domains: set = set()
    terms: list = []
    for line in str(raw).replace(",", "\n").splitlines():
        s = line.strip().lstrip("-").strip()
        if s.lower().startswith("site:"):
            s = s[5:].strip()
        if not s or s.startswith("#"):
            continue
        if "." in s:
            d = s.lower()
            if d.startswith("www."):
                d = d[4:]
            domains.add(d)
        else:
            terms.append(s)
    return domains, terms


def get_render_eligible_hosts(db_path: str = _DEFAULT_DB) -> set:
    """Set of hosts+roots whose pages may use the full-browser render escalation:
    domains flagged ``render_required = 1`` (learned/curated JS-rendered sites) OR
    carrying an unblocker fetch_strategy. Used by the DISH batch to mark per-result
    entries `_allow_render`, so a multi-domain batch escalates only these (a publisher
    refresh of a single flagged domain escalates the whole run without this). Cached;
    CRUD writers + mark_render_required invalidate it."""
    global _RENDER_CACHE
    if _RENDER_CACHE is None:
        hosts: set = set()
        try:
            with _connect(db_path) as conn:
                ensure_domains_table(conn)
                for dom, root in conn.execute(
                    "SELECT domain, root_domain FROM domains "
                    "WHERE render_required = 1 OR fetch_strategy = 'unblocker'"
                ):
                    if dom:
                        hosts.add(dom.lower())
                    if root:
                        hosts.add(root.lower())
        except Exception:
            pass
        _RENDER_CACHE = hosts
    return _RENDER_CACHE


def get_poor_publisher_hosts(db_path: str = _DEFAULT_DB) -> set:
    """Cached set of hosts+roots flagged POOR publishers (poor_quality_flag = 1). Used
    by the is-recipe LLM cascade to SKIP the per-page classify for a domain we've already
    judged a messy source — stop re-paying to relearn it. CRUD writers +
    refresh_poor_publisher_flags invalidate it."""
    global _POOR_CACHE
    if _POOR_CACHE is None:
        hosts: set = set()
        try:
            with _connect(db_path) as conn:
                ensure_domains_table(conn)
                for dom, root in conn.execute(
                    "SELECT domain, root_domain FROM domains WHERE poor_quality_flag = 1"
                ):
                    if dom:
                        hosts.add(dom.lower())
                    if root:
                        hosts.add(root.lower())
        except Exception:
            pass
        _POOR_CACHE = hosts
    return _POOR_CACHE


def refresh_poor_publisher_flags(conn: Optional[sqlite3.Connection] = None,
                                 db_path: str = _DEFAULT_DB, *,
                                 min_samples: Optional[int] = None,
                                 threshold: Optional[float] = None) -> dict:
    """Roll the is-recipe LLM cascade's per-page verdicts (from the git-ignored
    training.db) up per HOST and (re)set each domain's poor_quality_* columns. Keyed on
    the URL host across EVERY source (dish batches AND publisher harvests) — a messy
    source is a messy source wherever it shows up. A host with at least `min_samples`
    cascade-classified pages whose poor_quality FRACTION is >= `threshold` is flagged a
    poor publisher (poor_quality_flag = 1) — EXCEPT a curated `paywall = 1` domain, which is
    EXEMPT (its stubs read poor_quality because they're GATED, not messy) — auto-creating a
    minimal domains row if absent (mirrors set_paywall_calibration) so the flag persists + the
    curator can review it; a host that already HAS a row also gets its rate/samples refreshed
    (and cleared to 0 if it no longer crosses). Thresholds default from system_config
    (poor_publisher_min_samples / poor_publisher_threshold). Best-effort; returns a summary
    {flagged:[...], exempted_paywall:[...], scored:int, min_samples, threshold} ({} on error). Pass a
    `conn`, or omit to open `db_path` (lets the out-of-process harvest call it
    connection-free)."""
    if min_samples is None or threshold is None:
        try:
            from input.pipeline import system_config as cfg
            if min_samples is None:
                min_samples = int(cfg.get_setting("poor_publisher_min_samples", 8) or 8)
            if threshold is None:
                threshold = float(cfg.get_setting("poor_publisher_threshold", 0.5) or 0.5)
        except Exception:
            pass
    min_samples = int(min_samples if min_samples is not None else 5)
    threshold = float(threshold if threshold is not None else 0.5)

    def _host(u: str) -> str:
        try:
            h = u.split("//", 1)[1].split("/", 1)[0].lower() if "//" in u else ""
            return h[4:] if h.startswith("www.") else h
        except Exception:
            return ""

    # Aggregate cascade verdicts per URL HOST from training.db (every source).
    try:
        from intake.training_capture import TRAINING_DB_PATH
        tconn = sqlite3.connect(TRAINING_DB_PATH, timeout=5.0)
        try:
            rows = tconn.execute(
                "SELECT url, shadow_verdict FROM is_recipe_samples "
                "WHERE shadow_verdict IS NOT NULL AND url IS NOT NULL"
            ).fetchall()
        finally:
            tconn.close()
    except Exception:
        return {}
    counts: dict = {}   # canon host -> [n, poor]
    for url, verdict in rows:
        dom = _canon_host(_host(url or ""))
        if not dom:
            continue
        c = counts.setdefault(dom, [0, 0])
        c[0] += 1
        if verdict == "poor_quality":
            c[1] += 1
    own = conn is None
    if own:
        conn = _connect(db_path)
    flagged, scored = [], 0
    try:
        ensure_domains_table(conn)
        now = _now()
        exempted = []
        for dom, (n, poor) in counts.items():
            rate = (poor / n) if n else 0.0
            flag = 1 if (n >= min_samples and rate >= threshold) else 0
            row = conn.execute("SELECT paywall FROM domains WHERE domain = ?", (dom,)).fetchone()
            exists = row is not None
            # PAYWALL EXEMPTION: a gated publisher's pages fetch as STUBS and get tagged
            # poor_quality — that's the PAYWALL, not a messy source. Never flag a paywall=1
            # domain (we'd wrongly suppress its cascade); still record the rate for the
            # curator's visibility. (An auto-created host is never paywalled, so this only
            # spares curated paywall publishers like Milk Street.)
            if flag and exists and row[0]:
                flag = 0
                exempted.append(dom)
            if not exists:
                if not flag:
                    continue   # don't mint rows for unknown hosts that aren't poor
                conn.execute(
                    "INSERT INTO domains (domain, root_domain, display_name, notes, "
                    "created_at, updated_at) VALUES (?, ?, '', ?, ?, ?)",
                    (dom, root_domain(dom) or dom,
                     "auto-added: poor-publisher signal (is-recipe cascade)", now, now),
                )
            conn.execute(
                "UPDATE domains SET poor_quality_flag = ?, poor_quality_rate = ?, "
                "poor_quality_samples = ?, poor_quality_flagged_at = ? WHERE domain = ?",
                (flag, round(rate, 4), n, now, dom),
            )
            scored += 1
            if flag:
                flagged.append(dom)
        conn.commit()
        invalidate_cache()
    except Exception:
        return {}
    finally:
        if own:
            conn.close()
    return {"flagged": flagged, "exempted_paywall": exempted, "scored": scored,
            "min_samples": min_samples, "threshold": threshold}


def get_blocked_root_domains(db_path: str = _DEFAULT_DB) -> set:
    """Set of blocked root domains: the editable system_config `disallowed_domains`
    list UNIONed with the `serp_exclusions` textarea's domain lines. Used by the batch
    SERP filter at root-domain grain, BEFORE any fetch. Both sources are DB-resident +
    curator-editable — no code change, and (unlike the retired ``domains.allowed = 0``
    flag this replaced) no per-publisher domains-master row is needed to block a junk
    host. Read fresh through system_config's own write-invalidated cache, so edits take
    effect without a restart. Degrades to whatever it can read on error."""
    blocked: set = set()
    try:
        from input.pipeline import system_config as cfg
        raw = cfg.get_setting("disallowed_domains", [], db_path=db_path) or []
        if isinstance(raw, str):                    # tolerate a text/newline value
            raw = raw.replace(",", "\n").splitlines()
        for item in raw:
            h = _canon_host(str(item))
            if not h:
                continue
            blocked.add(h)
            r = (root_domain(h) or "").lower()
            if r:
                blocked.add(r)
    except Exception:
        pass
    try:
        extra, _ = parse_serp_exclusions(db_path)
    except Exception:
        extra = set()
    return blocked | extra


def seed_disallowed_domains(conn: sqlite3.Connection, hosts) -> int:
    """Seed the batch's hardcoded disallowed list into the table as
    ``allowed = 0`` rows so the table becomes the source of truth. Existing
    rows are forced to allowed=0 (a curator can re-allow later). Returns the
    number of rows touched."""
    ensure_domains_table(conn)
    now = _now()
    n = 0
    for raw in hosts:
        host = _canon_host(raw)
        if not host:
            continue
        if domain_exists(conn, host):
            conn.execute(
                "UPDATE domains SET allowed = 0, updated_at = ? WHERE domain = ?",
                (now, host),
            )
        else:
            conn.execute(
                """
                INSERT INTO domains (domain, root_domain, display_name, allowed,
                                     notes, created_at, updated_at)
                VALUES (?, ?, '', 0, 'seeded from disallowed list', ?, ?)
                """,
                (host, root_domain(host) or host, now, now),
            )
        n += 1
    conn.commit()
    invalidate_cache()
    return n


def get_display_map(db_path: str = _DEFAULT_DB) -> dict:
    """Cached {host: display_name} for every domain row with a name. Seeds
    the table from JSON on first use if it's empty. Falls back to the raw
    JSON map if the DB is unavailable so the resolver never hard-fails."""
    global _DISPLAY_CACHE
    if _DISPLAY_CACHE is not None:
        return _DISPLAY_CACHE
    try:
        with _connect(db_path) as conn:
            ensure_domains_table(conn)
            row = conn.execute("SELECT COUNT(*) FROM domains").fetchone()
            if not row or row[0] == 0:
                seed_domains(conn)
            rows = conn.execute(
                "SELECT domain, display_name FROM domains "
                "WHERE display_name != ''"
            ).fetchall()
        _DISPLAY_CACHE = {d: n for d, n in rows}
    except Exception:
        try:
            with open(_SEED_PATH, encoding="utf-8") as f:
                _DISPLAY_CACHE = {_canon_host(k): v for k, v in json.load(f).items()}
        except Exception:
            _DISPLAY_CACHE = {}
    return _DISPLAY_CACHE
