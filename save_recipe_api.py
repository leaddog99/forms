# TODO (revisit): persist the original source image used during AI extraction.
# Today /extract reads the upload and discards it. Consider saving it to a stable
# location (e.g. input/ or object storage) and returning its URL so it can be
# stored on the recipe and shown in the edit view. See matching TODOs in
# recipe_model.py (sourceImage field) and recipe_form_styled.html (UI).
# Decide: storage location, retention, multi-image (re-extractions), privacy.

import sys

# Windows console defaults to cp1252 ("charmap") which can't encode common
# recipe characters like ℉ (℉), curly quotes, em-dashes, etc. Without
# this, the first `print(payload)` that hits one of those throws
# UnicodeEncodeError before save_recipe can even validate input. `replace`
# falls back to "?" rather than crashing if a stranger character appears.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Load .env BEFORE any anthropic-using module is imported below. The
# Anthropic SDK reads ANTHROPIC_API_KEY at client-construction time and
# permanently caches api_key=None if the env is empty in that moment.
# Several to_markdown/extract modules construct module-level clients at
# import (image_to_markdown, pdf_to_markdown, markdown_to_recipe,
# enrich_recipe, chapter_classifier) — without this preamble they all
# silently end up unauthenticated unless the launching shell happens to
# already have ANTHROPIC_API_KEY set.
from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, Response, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from typing import Optional
from pydantic import ValidationError
import hashlib
import sqlite3
import unicodedata
import uuid
import asyncio
import json
import re
import time
from datetime import datetime, timezone
import os
import traceback
import threading   # deferred page-screenshot capture (see _attach_page_screenshot)
from pathlib import Path

# Shadow the builtin print so every existing `print(...)` call in this
# module emits a leading timestamp. Cheaper than converting 100+ call
# sites to the logging module; uvicorn's own INFO/access lines are
# timestamped separately via log_config.json.
import builtins as _builtins
_real_print = _builtins.print
def print(*args, **kwargs):  # noqa: A001 — intentional shadow
    # Local wall clock WITH the offset (…13:18:40-0400). Stored timestamps are
    # UTC, so a bare local log line means doing timezone arithmetic in your head
    # to line a log entry up against the row it wrote. astimezone() is what makes
    # %z produce anything — datetime.now() alone is naive and %z renders empty.
    _real_print(f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S%z')}]", *args, **kwargs)

# In-memory staging for bookmarklet → form handoff. One-time read, TTL pruned.
_STAGE_TTL_SECONDS = 600
_staged_markdown: dict[str, dict] = {}

# IMPORTANT: Keep the imports for the critical business logic files
try:
    from recipe_model import RecipeModel, static_subset

    print("[OK] RecipeModel imported successfully")
except Exception as e:
    print(f"[ERROR] Failed to import RecipeModel: {e}")
    raise

try:
    from sanitize_recipe_data import sanitize_recipe_data

    print("[OK] sanitize_recipe_data imported successfully")
except Exception as e:
    print(f"[ERROR] Failed to import sanitize_recipe_data: {e}")
    raise

try:
    from to_markdown.html_to_markdown import html_to_markdown
    from to_markdown.image_to_markdown import image_to_markdown, IMAGE_TO_MARKDOWN_PROMPT
    from to_markdown.markdown_passthrough import markdown_passthrough
    from to_markdown.pdf_to_markdown import pdf_url_to_markdown, PDF_TO_MARKDOWN_PROMPT
    from extract.markdown_to_recipe import markdown_to_recipe, SYSTEM_PROMPT as _MD_PROMPT
    from extract.jsonld_to_recipe import jsonld_to_recipe, best_recipe_jsonld
    from extract.enrich_recipe import enrich_recipe, SYSTEM_PROMPT as _ENRICH_PROMPT
    from extract.chapter_classifier import classify_chapter, CHAPTERS

    print("[OK] new to_markdown/extract layer imported successfully")
except Exception as e:
    print(f"[ERROR] Failed to import new to_markdown/extract layer: {e}")
    raise

try:
    from input.pipeline.url_utils import normalize_url, unwrap_wayback, is_wayback
    from input.pipeline import (
        ensure_metabase_url_table,
        get_or_create_url_metadata,
        get_metabase_url,
    )
    from input.pipeline.url_scoring import (
        MOZ_UNCRAWLED_RETRY_DAYS, MOZ_SCORING_FIELDS, scoring_url,
        apply_moz_scores, clear_moz_scores,
    )
    from input.pipeline.token_journal import (
        ensure_bcc_token_journal_table,
        write_usage_entries,
    )
    from input.pipeline.extract_cache import (
        ensure_llm_extract_cache_table,
        get_cached_extract,
        set_cached_extract,
        backfill_source_fingerprint,
        compute_recipe_fingerprint,
        prompt_version_for,
    )
    from input.pipeline import dishes as dishes_lib
    from input.pipeline import jobs as jobs_lib
    from input.pipeline.grading import compute_exceptionalism
    from input.pipeline.embeddings import find_best_dish_match
    from extract.dish_signal import generate_dish_signal_for_recipe
    from extract.identity_card import generate_identity_card_for_recipe

    print("[OK] url_utils / url_scoring imported successfully")
except Exception as e:
    print(f"[ERROR] Failed to import url_utils / url_scoring: {e}")
    raise

print("[START] Starting API setup...")

DB_PATH = "recipes.db"


# Typographic characters that appear in recipe titles and would otherwise sort
# above every letter. Mapped to the ASCII the same title uses elsewhere in the
# corpus — this is a normalisation, not a preference.
_TYPOGRAPHIC = {
    "‘": "'", "’": "'",          # curly single quotes
    "“": '"', "”": '"',          # curly double quotes
    "–": "-", "—": "-",          # en / em dash
    "−": "-",                          # minus sign
    " ": " ",                          # non-breaking space
}


def _sort_key_fold(s):
    """Sort key for TEXT columns, matching what the browser was doing.

    The recipe sidebar sorted names client-side with

        localeCompare(other, undefined, { sensitivity: 'base' })

    which ignores BOTH case and accents. Moving that sort into SQL naively —
    `COLLATE NOCASE` — folds case but not accents, and that is not a rounding
    error: over the 5,435 master names it reorders 1,083 positions, the first at
    index 395, where "Basic Béchamel Sauce" jumps from before "Basic bread
    recipe" to long after it. A-Z that puts the accented titles in a different
    place is a visible regression, not an implementation detail.

    NFKD splits an accented letter into base + combining mark; dropping the
    combining marks leaves the base letter, and casefold() handles case. Applied
    as an ORDER BY expression it is evaluated once per row into the sorter
    record, not once per comparison — measured at 475ms over master against
    496ms for plain NOCASE, i.e. free, because the cost of that query is reading
    the JSON rather than ordering it. (Registering it as a COLLATION instead
    costs 842ms, since that form really does call back per comparison.)

    Typographic characters are folded to their ASCII equivalents for a reason
    the verification found rather than predicted: the corpus contains BOTH
    "Chef John's Spaghetti al Tonno" and "Chef John’s Buttermilk Biscuits",
    and U+2019 sorts far above any letter by code point, so the two Chef John
    recipes landed in different parts of the alphabet. Same for "BA’s Best
    Hash Browns" vs "Bavarian ... Strudel", and en/em dashes in
    "Bougatsa – Greek-Style Custard Pastry".

    Four foldings were measured against the browser comparator over all 5,435
    master names, counting inversions (pairs the server orders one way and the
    browser the other):

        accent-fold only ............ 45
        + typographic to ASCII ...... 26   <- this one
        + also drop apostrophes ..... 42
        + drop ALL punctuation ...... 114

    Dropping all punctuation is the intuitive move and it is the WORST of the
    four — ICU weights punctuation low but not to zero, so removing it entirely
    overshoots as badly as ignoring it undershoots. Spaces are kept for the same
    reason; ICU weights them, and dropping them would re-order "Char Siu"
    against "CharSiu".

    The residual 26 of 5,435 (0.5%) are genuine ICU collation subtleties that
    cannot be reproduced without PyICU (not installed, and not worth a native
    dependency for this). They are all adjacent-name orderings inside the same
    letter. NOT byte-identical to ICU, deliberately, and measured rather than
    assumed — if that 0.5% ever matters, the fix is PyICU, not more folding.
    """
    if s is None:
        return None
    return "".join(
        _TYPOGRAPHIC.get(ch, ch)
        for ch in unicodedata.normalize("NFKD", str(s))
        if not unicodedata.combining(ch)
    ).casefold()


def _db() -> sqlite3.Connection:
    """The ONE connection factory for the server (was 120+ sites each carrying
    their own timeout=). 30s busy_timeout so concurrent writers — the out-of-process
    harvest/cook jobs and the server — WAIT for the WAL lock instead of failing with
    'database is locked'. Mirrors input/pipeline/db.connect for the library side."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    # Registered HERE so every query in the app can order text the same way;
    # a per-endpoint registration is how two call sites end up sorting
    # differently. deterministic=True lets SQLite use it in an index later.
    conn.create_function("bcc_sortkey", 1, _sort_key_fold, deterministic=True)
    return conn


def _detached_flags() -> int:
    """creationflags for a spawned job: CREATE_NO_WINDOW = a HIDDEN console, inherited
    by any console child the job spawns (so nothing pops a window).

    We deliberately do NOT use DETACHED_PROCESS: it gives the job NO console, so the
    first console CHILD it spawns (an extract/render helper, git, etc.) makes Windows
    allocate a fresh VISIBLE console — an empty DOS window. Restart-survival does NOT
    come from the spawn flags regardless (the child's PARENT pid still points at the
    server); it comes from bcc_restart.bat SKIPPING 'python -m jobs' processes when it
    kills the listener's child tree. That's the half that actually keeps jobs alive."""
    import subprocess
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# Binary media (page screenshots) live in a SEPARATE, git-ignored DB so the
# 29MB+ git-tracked recipes.db never accumulates BLOBs. Regenerable on
# re-extract; backed up to ADAM/S3, not git. See screenshot_pipeline.py.
MEDIA_DB_PATH = "media.db"

# Strangler flag (DL-6, docs/split-decisions-log.md): route the EXTRACT step
# through the Recipe Enrichment API (enrich.enrich) instead of the
# inline extract calls. OFF by default — with it off, the extract path is
# byte-for-byte the current inline code and the API path is dead. Set
# BCC_ENRICHMENT_API=1 to exercise the seam; unset to revert instantly.
_USE_ENRICHMENT_API = os.getenv("BCC_ENRICHMENT_API", "0").strip() == "1"
print(f"[SPLIT] Enrichment API extract path: {'ON' if _USE_ENRICHMENT_API else 'off'}")

# Instance default canonical language (portable-package: a Greek deployment runs
# BCC_TARGET_LANGUAGE=el). The enrichment API translates IFF source != target, so
# the default "en" preserves today's behavior exactly. A per-user preference,
# once wired, takes precedence over this instance default.
INSTANCE_TARGET_LANGUAGE = (os.getenv("BCC_TARGET_LANGUAGE", "en").strip().lower()[:2] or "en")
print(f"[SPLIT] Instance target language: {INSTANCE_TARGET_LANGUAGE}")

# Placeholder user id until the user-identity field is wired into the form
# (will eventually come from Ghost). Recipes and token-journal rows both use it.
PLACEHOLDER_USER_ID = 1

# Cross-cutting tunables loaded from bcc_config.json (with built-in
# defaults in input/pipeline/config.py). Re-imported here so the live
# form's save gate, self-URL minting, and self-URL recognition all
# track the same single source of truth.
from input.pipeline.config import (  # noqa: E402
    BCC_PUBLIC_DOMAIN,
    BCC_LINK_DOMAIN,
    SAVE_GATE_MIN_INGREDIENTS,
    SAVE_GATE_MIN_INSTRUCTIONS,
)
from input.pipeline.site_names import friendly_site_name  # noqa: E402


def _bcc_permalink(recipe_id: str) -> str:
    """Canonical BCC URL for any saved recipe — what gets displayed in
    the form's Permalink field and copied to the clipboard for sharing."""
    return f"https://{BCC_PUBLIC_DOMAIN}/r/{recipe_id}"


def _bcc_link_permalink(recipe_id: str) -> str:
    """User-facing "Open in BCC" link for the dishes page. Uses
    BCC_LINK_DOMAIN, which normally equals BCC_PUBLIC_DOMAIN but can be
    pointed at the Cloudflare tunnel host (recipes.tbotb.com) via
    bcc_config.json while the bcc domain transfer is in flight. See the
    BCC_LINK_DOMAIN note in input/pipeline/config.py."""
    return f"https://{BCC_LINK_DOMAIN}/r/{recipe_id}"


def _enable_vec_for_delete(conn) -> None:
    """Load sqlite-vec on `conn` so the vec-cleanup AFTER DELETE triggers
    (trg_master_vec_cleanup / trg_dish_vec_cleanup, created in
    vector_store.ensure_vec_triggers) can run. Required on any path that
    may DELETE from master_recipes or dishes — the triggers delete from
    vec0 tables, so the module must be loaded or the DELETE fails. This
    is the single place app delete paths funnel through for that
    prerequisite; the trigger itself is the one canonical cleanup.
    Best-effort: if sqlite-vec is genuinely absent there's no index to
    keep in sync."""
    try:
        from input.pipeline import vector_store
        vector_store.enable_vec(conn)
    except Exception as e:
        print(f"[VEC] enable_vec for delete skipped: {e}")


# Hosts that point at our own /r/<id> redirect. New self-URLs mint under
# BCC_PUBLIC_DOMAIN; recipes.tbotb.com is the legacy host the 16
# pre-2026-05-22 self-URLs use. Either resolves to the same form via
# the /r/<id> route. www. prefix is folded in `_is_bcc_self_url`.
_BCC_SELF_HOSTS = frozenset({
    BCC_PUBLIC_DOMAIN,
    "recipes.tbotb.com",
})


def _is_bcc_self_url(url: str) -> bool:
    """True when the URL is one of our own self-minted permalinks.

    Self-URLs point at OUR database via the /r/<id> redirect to the
    form. Fetching one server-side returns form HTML, not recipe
    content — so feeding a self-URL into html_to_markdown / Moz /
    llm_extract_cache produces garbage. Three guards use this to
    short-circuit:
      - `_extract_cache_lookup` / `_extract_cache_write` keep the
        cache table free of self-URL rows (so the nightly refresh
        script never tries to re-extract one).
      - `/extract-from-url` rejects self-URL extract attempts and
        points the caller at the correct route (GET /recipes/<id>).
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse  # one-line import; not hot path
        host = (urlparse(url).netloc or "").lower().split(":", 1)[0]
        if host.startswith("www."):
            host = host[4:]
        return host in _BCC_SELF_HOSTS
    except Exception:
        return False


def _recipes_table_for(user_id: int) -> str:
    """Pick the recipes table based on owner. user_id=0 → master_recipes
    (sys-admin / batch-curated content); anything else → recipes (personal
    collection). Used by every endpoint that touches the recipes table —
    do NOT inline the choice elsewhere.

    Returns one of two hardcoded literals, so f-string interpolation of
    the result into SQL is safe by construction (never user-controlled).
    """
    table = "master_recipes" if (user_id == 0) else "recipes"
    assert table in ("master_recipes", "recipes")
    return table


def _seed_users_from_recipes(conn: sqlite3.Connection) -> None:
    """One-time bootstrap: ensure every user_id that already appears in
    recipes (or master_recipes) has a matching row in `users`, so the
    picker has something to show on first boot of an existing DB. user_id=0
    is excluded (master/curator pseudo-user). Idempotent — uses INSERT OR
    IGNORE; reruns are no-ops once seeded."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        existing_uids = {
            row[0] for row in conn.execute(
                "SELECT user_id FROM recipes WHERE user_id IS NOT NULL AND user_id != 0 "
                "UNION SELECT user_id FROM master_recipes WHERE user_id IS NOT NULL AND user_id != 0"
            )
        }
        # Always ensure user_id=1 exists (the existing PLACEHOLDER_USER_ID
        # default) even on a fresh DB with no recipes yet.
        existing_uids.add(1)
        for uid in sorted(existing_uids):
            conn.execute(
                "INSERT OR IGNORE INTO users "
                "(user_id, name, status, created_at, updated_at) "
                "VALUES (?, ?, 'test', ?, ?)",
                (uid, f"User {uid}", now, now),
            )
        conn.commit()
    except Exception as e:
        print(f"[WARN] _seed_users_from_recipes failed: {e}")


def _find_recipe_owner(recipe_id: str) -> int | None:
    """Search both recipes and master_recipes for the given UUID; return
    the row's user_id (0 for master, else personal), or None if absent.

    Used by URL-addressed access (/r/<id>) and by the claim endpoint so
    callers don't need to know which table holds the recipe. Cheap — two
    indexed lookups by recipe_id (UUID column).
    """
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT user_id FROM master_recipes WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if row:
                return row[0]
            row = conn.execute(
                "SELECT user_id FROM recipes WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if row:
                return row[0]
    except Exception as e:
        print(f"[WARN] _find_recipe_owner({recipe_id}) failed: {e}")
    return None

# Pipeline cache identity. One key shape for both the JSON-LD fast lane
# (jsonld_to_recipe + enrich_recipe) and the markdown-LLM path
# (markdown_to_recipe). When any of the three load-bearing prompts change,
# the combined version flips and every cache row naturally invalidates.
EXTRACT_MODEL = "claude-haiku-4-5"  # the model markdown_to_recipe defaults to
EXTRACT_PROMPT_VERSION = prompt_version_for(
    _MD_PROMPT + "\n---ENRICH---\n" + _ENRICH_PROMPT
    + "\n---IMAGE---\n" + IMAGE_TO_MARKDOWN_PROMPT
    + "\n---PDF---\n" + PDF_TO_MARKDOWN_PROMPT
)
print(f"[CACHE] EXTRACT_PROMPT_VERSION = {EXTRACT_PROMPT_VERSION}")


def _journal_usage(usage_log, *, recipe_id=None, user_id=PLACEHOLDER_USER_ID):
    """Best-effort token-journal write. Opens its own connection so it can be
    called from anywhere in the request lifecycle; never raises.

    user_id defaults to the placeholder for back-compat with callers that
    haven't been updated to thread it. Batch flows pass user_id=0 so
    master-batch LLM costs are attributable separately from personal usage.

    Also flushes any GATEWAY-buffered usage: modules migrated to llm.py journal via
    the ambient context set by llm.enter() at the handler top, and this is the
    handler's exit point. Best-effort. (Migrated modules no longer append to
    usage_log, so the write below is a no-op for them — no double-count.)
    """
    try:
        import llm
        llm.flush()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] llm.flush failed: {e}")
    if not usage_log:
        return
    try:
        with _db() as conn:
            write_usage_entries(
                conn,
                user_id=user_id,
                recipe_id=recipe_id,
                entries=usage_log,
            )
    except Exception as e:
        print(f"[WARN] token-journal write failed: {e}")


# =====================================================================
# Cache layer — URL-keyed, model+prompt-versioned, TTL=30 days.
#
# Why it exists: stage B (markdown → recipe via LLM) costs ~$0.001 and
# ~15-25s per call and is stable across users for the same source URL.
# Hits skip the LLM entirely; stale rows are refreshed on the next
# extract and used to flag source drift.
#
# Why it was stubbed before this revision: the cache poisoned itself
# with empty extractions (paywall / 404 / anti-bot pages cached as
# empty recipes) and one wildly wrong row ("Easy Meatloaf" cached for
# a curry-chicken URL — the LLM picked a sidebar carousel). Two
# safeguards keep that from recurring now:
#   1. _is_cacheable() refuses to cache rows that look empty or thin
#      (no name, < 2 ingredients, < 2 instructions). Bad extracts no
#      longer pollute the cache.
#   2. Cache stores the STATIC subset only (recipe_model.static_subset)
#      — no per-user fields, no claim provenance, no current_status
#      timestamps. Same boundary discipline as claim.
#
# Lookup order in extract endpoints (unchanged): jsonld-direct fast
# lane (when the source page ships JSON-LD) → cache → LLM. Cache
# catches everything the JSON-LD path doesn't.
# =====================================================================

# === Auto-enrich policy ==============================================
#
# WHO PAYS decides who gets it. Enrichment is ~$0.05 and ~10s of Haiku per row
# (provenance + classification + editorial), so it is deliberately NOT free for
# everyone.
#
#   MASTER (user_id == 0)  -> always. The corpus is ours, the cost is ours, and
#                             the "no un-enriched master row" invariant is what
#                             lets every future claimer inherit rich data
#                             through static_subset.
#   A REAL USER            -> only if their tier includes it. Today: NOBODY.
#
# ANTICIPATED, NOT YET SOLD. `users.subscription_tier` already exists and today
# holds 'Free' | 'Premium' | 'Admin' | NULL. When enrichment becomes a paid
# add-on, add the qualifying tiers to _AUTO_ENRICH_TIERS below and this starts
# working with no other change. Two things to settle BEFORE flipping it:
#   1. It must be a per-user OPT-IN, not just a tier property — a Premium user
#      who does not want to wait ~10s on every save must be able to decline.
#      That wants a `users.auto_enrich` column (nullable; NULL = follow tier),
#      which is why the check below is a function and not an inline tier test.
#   2. Metering. Token spend is already journaled per user_id in
#      bcc_token_journal, so per-user cost is answerable — but nothing enforces
#      a ceiling, and enrichment is the most expensive per-save operation we
#      have. Decide the cap before the first paying user, not after.
# See [[project_multiuser_reality]] (marginal user ~$0.05/mo today; this would
# change that number materially) and [[project_portable_package]].
_AUTO_ENRICH_TIERS: frozenset[str] = frozenset()   # empty = paid tier not sold yet


def _enrich_available(user: dict | None) -> bool:
    """Should the UI offer the Enrich button to this caller?

    While enrichment is NOT sold (`_AUTO_ENRICH_TIERS` empty) it is available to
    any resolved user — there is no paid tier to withhold it from, and the
    endpoint is open to every role anyway. Once tiers are sold this narrows to
    those tiers automatically, with no client change.
    """
    if not user:
        return False
    if not _AUTO_ENRICH_TIERS:
        return True
    tier = (user.get("subscription_tier") or "").strip().lower()
    return tier in {t.lower() for t in _AUTO_ENRICH_TIERS}


def _auto_enrich_applies(user_id: int) -> bool:
    """True when a save to `user_id` should auto-enrich.

    Master always. Real users only when their tier qualifies AND they have not
    opted out — neither of which is true today, so this returns False for every
    human. Kept as a function so turning enrichment on for paying users is a
    data change plus a tier list, not a rewrite of the save path.
    """
    if user_id == 0:
        return True
    if not _AUTO_ENRICH_TIERS:
        return False
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT subscription_tier FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        tier = (row[0] if row else "") or ""
        return tier.strip().lower() in {t.lower() for t in _AUTO_ENRICH_TIERS}
    except Exception as e:
        # Never let a policy lookup fail a SAVE. Erring to "no enrich" also errs
        # to "no surprise charge", which is the safe direction for a paid feature.
        print(f"[SAVE-ENRICH] tier lookup failed for user {user_id} ({e}); not enriching")
        return False


# How much method text an UNENUMERATED single instruction must carry to count as a
# real method. Set at the lowest real example measured in the corpus (Thomas
# Keller's roast chicken, 157 chars) with a little room below it; the junk cases
# sat at 7 and 132 chars. See the note in _is_cacheable.
# What a NON-STAFF user is told when a row could not be scored. Deliberately
# says nothing about which vendor we buy authority data from, why they haven't
# crawled it, or what an operator would do about it — a member capturing a
# recipe is a customer of the product, not an operator of the pipeline. Staff
# still get the full diagnostic; see the redaction at the /recipes boundary.
# How many consecutive failed captures retire a row from the nightly sweep, and
# how long before it is tried again. Two rather than one: a single failure is a
# transient (a slow page, a restart mid-render) and retiring on it would quietly
# shrink coverage. 90 days because the causes seen are publisher-side and slow-
# moving — a TLS chain, a paywall, a timeout — not things that change weekly.
SCREENSHOT_FAIL_LATCH = 2
SCREENSHOT_RETRY_DAYS = 90

GENERIC_UNSCORED_NOTE = (
    "Score not yet available for this page — it usually appears within a few days.")

SINGLE_STEP_MIN_CHARS = 150
# The same floor for a method written in a dense script (CJK). Set from the
# measured ratio on the case that exposed it — 77 Chinese characters carrying
# what took 315 in English, ~4x — so 150/4 ≈ 40, kept at 40 rather than rounded
# down further because the junk cases were short on BOTH axes anyway (7 and 132
# characters with 1 ingredient, killed by the ingredient floor regardless).
SINGLE_STEP_MIN_CJK_CHARS = 40


def _is_cacheable(recipe: dict, *, min_ings: int = 2, min_steps: int = 2) -> tuple[bool, str]:
    """Refuse to cache rows that look like a bad extraction (paywall,
    404, picked-the-wrong-recipe sidebar carousel). Returns
    (cacheable, reason). Defaults to the cache layer's relaxed
    thresholds (≥2 ingredients, ≥2 instructions). The /recipes save
    gate calls this with stricter thresholds (≥3/≥3) because junk in
    the recipes/master_recipes tables corrupts aggregated stats — see
    [[batch-single-program]] for the same reasoning on the batch side.
    """
    name = (recipe.get("name") or "").strip() if recipe else ""
    if not name:
        return False, "no name"
    ings = recipe.get("recipeIngredient") or []
    real_ings = sum(1 for i in ings if str(i).strip())
    if real_ings < min_ings:
        return False, f"fewer than {min_ings} ingredients ({real_ings})"
    steps = recipe.get("recipeInstructions") or []
    real_steps = 0
    for s in steps:
        text = s.get("text") if isinstance(s, dict) else s
        if str(text or "").strip():
            real_steps += 1
    if real_steps < min_steps:
        # A SINGLE SUBSTANTIAL PARAGRAPH IS A METHOD, NOT A FAILED EXTRACTION.
        # Counting steps assumes the publisher numbered them. Plenty don't:
        # m.xiachufang.com/recipe/107744561 ships its whole method as ONE string
        # in its own JSON-LD ("marinate 10 min ... wrap in foil ... 205C for 20
        # ... open foil, 5 more"), four real actions in one paragraph. We
        # reproduced it faithfully and then refused to save it.
        #
        # Measured over the corpus 2026-08-14 — exactly 6 of 5,593 rows have a
        # single instruction, and length separates them cleanly:
        #     7 chars / 1 ing   Pork Rice                     <- junk
        #   132 chars / 1 ing   'Parmesan Chicken | Recipes'  <- junk (title suffix
        #                                                        = wrong node)
        #   157 chars / 5 ing   Thomas Keller's Roast Chicken <- real
        #   239 chars / 7 ing   Spaghetti and Meatballs       <- real
        #   276 chars / 7 ing   Raita                         <- real
        #   315 chars / 6 ing   Air Fryer Garlic Pork Ribs    <- real
        # (median TOTAL instruction text on multi-step rows: 1,053 chars.)
        #
        # So: accept one step only when it carries real METHOD text. The
        # ingredient floor above has already run, which is what kills both junk
        # rows independently — this is deliberately belt-and-braces, because the
        # failure mode we are guarding is a paywall stub or a sidebar carousel,
        # and those are short.
        prose = 0
        cjk = 0
        for s in steps:
            text = str((s.get("text") if isinstance(s, dict) else s) or "").strip()
            prose += len(text)
            cjk += sum(1 for ch in text if 0x2e80 <= ord(ch) <= 0x9fff
                       or 0x3040 <= ord(ch) <= 0x30ff or 0xac00 <= ord(ch) <= 0xd7af)
        # A DENSE SCRIPT SAYS THE SAME THING IN FAR FEWER CHARACTERS, so a
        # character floor calibrated on English rejects an equivalent CJK method.
        # Measured on m.xiachufang.com/recipe/107744561: the identical method is
        # 77 characters in Chinese and 315 in English — a 4x difference in length
        # for the same four cooking actions. A recipe that survives translation is
        # judged on its English text and never reaches this branch; one saved
        # untranslated (curator choice, or a translation we declined) would be
        # refused for being written in Chinese, which is not a quality signal.
        floor = SINGLE_STEP_MIN_CHARS
        if prose and (cjk / prose) >= 0.30:
            floor = SINGLE_STEP_MIN_CJK_CHARS
        if real_steps >= 1 and prose >= floor:
            return True, (f"ok (single {prose}-char method paragraph; publisher "
                          f"did not enumerate steps)")
        return False, f"fewer than {min_steps} instructions ({real_steps})"
    return True, "ok"


# Save-gate thresholds — SAVE_GATE_MIN_INGREDIENTS /
# SAVE_GATE_MIN_INSTRUCTIONS are now loaded from bcc_config.json at the
# top of this file (see the `from input.pipeline.config import ...`
# block). The values keep the recipes/master_recipes tables clean for
# aggregated stats — Wikipedia-style narrative articles that survive
# is_recipe and produce thin extractions land here.


def _extract_cache_lookup(url_normalized, *, usage_log=None):
    """Look up a cached LLM extract for this URL+model+prompt.

    Returns (recipe, prior_fingerprint, status):
      recipe              cached recipe dict (the static subset that was
                          written), or None on miss/stale/error.
      prior_fingerprint   semantic fingerprint of the cached row; passed
                          forward so the eventual cache_write can detect
                          source drift. Empty string on miss.
      status              "skip"  no URL — nothing to key on
                          "hit"   fresh — serve recipe verbatim
                          "stale" past TTL — caller re-extracts; drift
                                  detection runs on next write
                          "miss"  no row, or lookup failed

    Fresh hits append a zero-token 'cache_hit_markdown_to_recipe' entry
    to usage_log so cost reports can total tokens *saved* alongside
    actual spend.
    """
    if not url_normalized:
        return None, "", "", "skip"
    if _is_bcc_self_url(url_normalized):
        # BCC self-URLs aren't extractable via the URL path — they
        # resolve to our form HTML, not recipe content. Treat them
        # like "no URL" so the caller falls through to vision / LLM /
        # whatever path actually has real content to work with.
        return None, "", "", "skip"
    result = get_cached_extract(
        DB_PATH,
        url_normalized=url_normalized,
        model=EXTRACT_MODEL,
        prompt_version=EXTRACT_PROMPT_VERSION,
    )
    if result is None:
        return None, "", "", "miss"
    if result["is_stale"]:
        # Pass the prior fingerprint forward; the write step on the
        # fresh re-extract will compare and surface drift.
        return None, result["semantic_fingerprint"], result.get("source_fingerprint", ""), "stale"
    if usage_log is not None:
        usage_log.append({
            "operation": "cache_hit_markdown_to_recipe",
            "model": EXTRACT_MODEL,
            "input_tokens": 0,
            "output_tokens": 0,
            "meta": {"cached_at": result["cached_at"]},
        })
    return result["llm_output"], result["semantic_fingerprint"], result.get("source_fingerprint", ""), "hit"


def _extract_cache_write(url_normalized, recipe, *, prior_fingerprint="", source_fingerprint=""):
    """Persist a freshly-extracted recipe to the cache.

    Skips the write entirely if `recipe` looks empty/thin (see
    _is_cacheable) so paywall pages and anti-bot stubs don't poison
    future hits. Writes only the static subset of the recipe so on
    hit, callers treat it like a fresh extract result and downstream
    stages (chapter, Moz, save-time validation) re-stamp anything
    per-extract.

    Returns (status, drift):
      status  "written"      row created or refreshed
              "skip"         no URL / no recipe / failed _is_cacheable
              "miss"         write failed (rare; logged)
      drift   True when prior_fingerprint is set (stale-lookup branch)
              AND the new fingerprint differs. Caller stamps
              source_changed_at on the saved recipe row so the UI
              surfaces "source page changed since you last saved."
    """
    if not url_normalized or not recipe:
        return "skip", False
    if _is_bcc_self_url(url_normalized):
        # Never cache a recipe under a self-URL key. The nightly cache
        # refresh would later try to re-extract from that URL, hit our
        # own /r/<id> redirect, and corrupt the cache with form-HTML
        # extractions. Self-URLs live in the recipes table; that's the
        # canonical store, no cache needed.
        return "skip", False
    ok, reason = _is_cacheable(recipe)
    if not ok:
        print(f"[CACHE] refused to cache {url_normalized!r}: {reason}")
        return "skip", False
    try:
        cacheable = static_subset(recipe)
        new_fp = compute_recipe_fingerprint(cacheable)
        set_cached_extract(
            DB_PATH,
            url_normalized=url_normalized,
            model=EXTRACT_MODEL,
            prompt_version=EXTRACT_PROMPT_VERSION,
            llm_output=cacheable,
            semantic_fingerprint=new_fp,
            source_fingerprint=source_fingerprint,
        )
    except Exception as e:
        print(f"[CACHE] write failed for {url_normalized!r}: {e}")
        return "miss", False
    drift = bool(prior_fingerprint) and new_fp != prior_fingerprint
    return "written", drift


def _stamp_cache_timings(timings, *, status, url_normalized, drift=False):
    """Push cache state into the extract trace so the form can render it."""
    if timings is None:
        return
    timings["cache"] = status
    timings["cache_key_url"] = url_normalized or "(no url — cache skipped)"
    if drift and url_normalized:
        timings["source_drift"] = True
        timings["drift_url"] = url_normalized


def _probe_url_head(url: str, timeout: int = 5) -> str:
    """HEAD request to learn Content-Type before fetching the body. Used to
    dispatch PDFs to pdf_to_markdown vs HTML to html_to_markdown. Returns
    the content-type header or empty string on any failure (caller treats
    missing as HTML, which is the existing default)."""
    import requests
    try:
        # allow_redirects so a 301/302 (common for shopify CDN PDFs etc.)
        # lands on the real Content-Type.
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        return r.headers.get("content-type", "") or ""
    except Exception:
        return ""


def _attach_chapter(recipe, *, usage_log=None):
    """Run the cookbook-chapter classifier at extract time and stamp
    recipe.classification.chapter. Cheap: most recipes hit the Tier-1
    keyword shortcut layer (zero API cost); only ambiguous titles fall
    through to a small claude-haiku-4-5 call.

    Doesn't overwrite an existing non-empty chapter — lets the
    /enrich-recipe path and user overrides survive. Skips entirely
    when the recipe has no name."""
    if not recipe:
        return
    cls = recipe.get("classification") or {}
    if cls.get("chapter"):
        return  # already set (user edit, previous extract, etc.)
    name = recipe.get("name") or ""
    if not name.strip():
        return
    # Canonical-dish chapter override (#2): a native-anchored dish (Kolokithopita,
    # Spanakopita…) has ONE authoritative chapter regardless of how the English title
    # wobbles (pumpkin/squash/zucchini) — pin it so the whole family stays together and
    # doesn't re-scatter. Matched off the unambiguous transliterated/native anchor in the
    # name / _source.originalTitle / _master.dish. See docs/dish-alias-normalization.md.
    try:
        from intake import dish_alias
        alias_ch = dish_alias.canonical_chapter(
            name, (recipe.get("_source") or {}).get("originalTitle") or "",
            (recipe.get("_master") or {}).get("dish") or "")
    except Exception:
        alias_ch = None
    if alias_ch:
        cls["chapter"] = alias_ch
        recipe["classification"] = cls
        return
    ingredients = recipe.get("recipeIngredient") or []
    chapter = classify_chapter(name, ingredients, usage_log=usage_log)
    cls["chapter"] = chapter
    recipe["classification"] = cls


def _attach_identity_card(recipe, *, usage_log=None):
    """Generate + stamp `_identity` on a fresh extract if absent.

    Card carries the structured dish fingerprint (cuisine, ingredient
    roles, technique, likelyDish, primaryIngredients) — the same
    artifact the save flow generates. Running it at extract time
    means the form's identity card panel populates immediately, so
    the curator can verify the cohort fit before saving (or skip the
    save if the card looks wrong).

    Cost: ~$0.0001 + ~2s via Haiku. Idempotent: the save flow checks
    `_identity` and skips regeneration. Best-effort: failures don't
    block extract (the panel just hides).
    """
    name = (recipe.get("name") or "").strip() if recipe else ""
    if not name:
        return
    existing = recipe.get("_identity")
    if isinstance(existing, dict) and (existing.get("likelyDish") or "").strip():
        return
    try:
        card = generate_identity_card_for_recipe(recipe, usage_log=usage_log)
    except Exception as e:
        print(f"[IDENTITY] extract stamping failed (continuing): {e}")
        return
    if not card:
        return
    recipe["_identity"] = card
    # Mirror to classification.dishSignal so backward-compat consumers
    # (any UI/code still reading dishSignal) see the canonical phrase.
    cls = recipe.get("classification") or {}
    cls["dishSignal"] = (card.get("likelyDish") or "").strip()
    recipe["classification"] = cls
    print(f"[IDENTITY] extract stamped: likelyDish={card.get('likelyDish')!r}")


def _attach_moz_scoring(recipe, url_normalized):
    """Run Moz scoring at extract time and denormalize PA/DA/OU/rootDomain
    into recipe._scoring so the form can display them before save. The
    metabase_url row is written/refreshed as a side effect.

    No-op when url_normalized is empty. Never raises — Moz outages
    leave the recipe's existing _scoring intact.
    """
    if not url_normalized or not recipe:
        return
    try:
        with _db() as conn:
            ensure_metabase_url_table(conn)
            fallback_title = (
                (recipe.get("_scoring") or {}).get("rawTitle")
                or recipe.get("name")
                or ""
            )
            meta = get_or_create_url_metadata(conn, url_normalized, fallback_title=fallback_title)
            if not meta:
                return
            # ONE writer (input.pipeline.url_scoring.apply_moz_scores). This
            # block used to assign the six fields by hand and, alone among the
            # writers, never wrote `power` — which is why 201 personal and 1,481
            # master rows carried a measured PA that no power-blend ranking could
            # see. It also stamps the paywall DA-adjustment AT SCORING TIME, so
            # the adjustment a page was actually scored under is stored with it —
            # queryable via the adjusted_da / effective_ou_score columns without
            # running the pipeline, and still readable after a recalibration.
            recipe["_scoring"] = apply_moz_scores(
                recipe.get("_scoring") or {}, meta, url=url_normalized)
    except Exception as e:
        print(f"[WARN] Moz scoring at extract failed for {url_normalized!r}: {e}")


def _cache_row_complete(recipe) -> bool:
    """True when a cached recipe already carries the EXPENSIVE URL-static
    enrichment — page screenshot + identity card — so a cache hit can be
    served without re-running them (the ~2s Haiku + ~3-5s screenshot that
    made cache hits feel slow). Rows written before this enrichment was
    cached lack these; they read as incomplete so the full path re-caches
    them once, after which future hits go straight to the fast path."""
    if not recipe:
        return False
    has_shot = bool((recipe.get("_source") or {}).get("pageScreenshot"))
    idy = recipe.get("_identity")
    has_idy = isinstance(idy, dict) and bool((idy.get("likelyDish") or "").strip())
    return has_shot and has_idy


def _maybe_stamp_source_drift(timings, *, user_id):
    """When markdown_to_recipe sets timings["source_drift"] (a TTL-expired
    re-extract whose semantic fingerprint differs from the cached one),
    stamp source_changed_at on every saved recipe matching that URL + user.
    The form reads the stamp and shows a "source updated — review and
    re-save" banner; save clears the stamp.

    Dispatches to recipes or master_recipes based on user_id."""
    if not timings or not timings.get("source_drift"):
        return
    url_normalized = timings.get("drift_url") or ""
    if not url_normalized:
        return
    table = _recipes_table_for(user_id)
    try:
        now = datetime.now(timezone.utc).isoformat()
        with _db() as conn:
            cursor = conn.execute(
                f"UPDATE {table} SET source_changed_at = ? "
                f"WHERE url_normalized = ? AND user_id = ?",
                (now, url_normalized, user_id),
            )
            conn.commit()
            if cursor.rowcount:
                print(f"[DRIFT] Stamped source_changed_at on "
                      f"{cursor.rowcount} recipe(s) in {table} for {url_normalized!r}")
    except Exception as e:
        print(f"[WARN] source_changed_at stamp failed: {e}")


# Ensure tables exist
def init_db():
    print("[SETUP] Creating database tables if needed...")
    try:
        with _db() as conn:
            # WAL is a hard prerequisite for running batch jobs as their own
            # out-of-process executable (docs/jobs-as-executables.md §6): a job
            # process and the server both writing recipes.db under the default
            # journal_mode=delete collide on the whole-file write lock and blow
            # past busy_timeout with "database is locked". WAL lets one writer +
            # many readers coexist across processes. The mode is PERSISTENT —
            # stored in the DB header, so setting it once here sticks for every
            # future connection in any process. The -wal/-shm side files are not
            # a backup concern: our backup is the recipes.sql dump + ADAM disk,
            # and .gitignore already excludes -wal/-shm.
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                print(f"[WARN] requested WAL journal_mode but DB reports '{mode}'")
            else:
                print("[SETUP] recipes.db journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recipes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipe_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER,
                    data TEXT,
                    url_normalized TEXT NOT NULL DEFAULT '',
                    source_changed_at TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
            """)
            # Migration for pre-existing DBs: add url_normalized column +
            # backfill from each row's _source.originalUrl, then create a
            # partial UNIQUE index on (url_normalized, user_id) so future
            # inserts can't make a dup for the same URL+user. Empty URLs
            # are exempt (handwritten/typed/photo recipes).
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(recipes)").fetchall()}
            if "url_normalized" not in existing_cols:
                print("[SETUP] Migrating recipes: adding url_normalized column...")
                conn.execute("ALTER TABLE recipes ADD COLUMN url_normalized TEXT NOT NULL DEFAULT ''")
                rows = conn.execute("SELECT id, data FROM recipes").fetchall()
                for row_id, data_json in rows:
                    try:
                        d = json.loads(data_json) if data_json else {}
                        raw = (d.get("_source") or {}).get("originalUrl") or ""
                        norm = normalize_url(raw) if raw else ""
                        if norm:
                            conn.execute("UPDATE recipes SET url_normalized = ? WHERE id = ?", (norm, row_id))
                    except Exception as e:
                        print(f"[WARN] backfill failed for recipes.id={row_id}: {e}")
                conn.commit()
                print(f"[OK] Backfilled url_normalized on {len(rows)} row(s)")
            # Migration for pre-existing DBs: add source_changed_at column.
            # Stamped on every saved recipe sharing a URL when an LLM
            # re-extract reveals the source page meaningfully changed; cleared
            # when the user saves (i.e. acknowledges the update).
            if "source_changed_at" not in existing_cols:
                print("[SETUP] Migrating recipes: adding source_changed_at column...")
                conn.execute("ALTER TABLE recipes ADD COLUMN source_changed_at TEXT")
                conn.commit()
            # Partial UNIQUE index. If existing data already has dups, this
            # will fail — we log and continue; the application-level upsert
            # still keeps new dups from being created.
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_recipes_url_user "
                    "ON recipes(url_normalized, user_id) WHERE url_normalized != ''"
                )
            except sqlite3.IntegrityError as e:
                print(f"[WARN] could not add unique index (existing dups?): {e}")

            # === master_recipes ===
            # Identical schema to `recipes`. Holds sys-admin / batch-curated
            # content (user_id=0 by convention). Lives in the same DB file
            # so cross-table queries are trivial JOINs, but the table boundary
            # is the authoritative master/user split. Save dispatches by
            # user_id: 0 → master_recipes, else → recipes.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS master_recipes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipe_id TEXT NOT NULL UNIQUE,
                    user_id INTEGER,
                    data TEXT,
                    url_normalized TEXT NOT NULL DEFAULT '',
                    source_changed_at TEXT,
                    created_at TEXT,
                    updated_at TEXT
                );
            """)
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_master_recipes_url_user "
                    "ON master_recipes(url_normalized, user_id) WHERE url_normalized != ''"
                )
            except sqlite3.IntegrityError as e:
                print(f"[WARN] could not add master_recipes unique index: {e}")
            # Migration (2026-05-30): source-of-truth embedding BLOB on the
            # master row (mirrors dishes.embedding). recipes_master_vec is
            # now a DERIVED index rebuilt from this column for free/offline
            # via vector_store.rebuild_master_vec_from_blobs — so the git
            # .sql dump (which excludes vec0 tables) no longer loses the
            # master vectors, and the AFTER DELETE trigger keeps the index
            # clean. 1536 float32 = 6144 bytes/row.
            master_cols = {row[1] for row in conn.execute("PRAGMA table_info(master_recipes)")}
            if "embedding" not in master_cols:
                conn.execute("ALTER TABLE master_recipes ADD COLUMN embedding BLOB")
                print("[MIGRATE] added master_recipes.embedding BLOB column")
            # Typed-membership-block fast paths: VIRTUAL generated columns that surface
            # the dish / domain block markers, each indexed. Makes "best for this dish
            # AND this site" (both non-null), "all dish winners", and "all domain
            # winners" index scans instead of json_extract table scans. VIRTUAL (not
            # STORED) is the only kind addable via ALTER, and it's fully indexable.
            # NOTE: generated columns appear in PRAGMA table_xinfo but NOT table_info,
            # so guard with try/except (idempotent) rather than the master_cols set.
            for _gc, _expr in (("dish_key", "$._master.dish"),
                               ("publisher_key", "$._master.publisher")):
                try:
                    conn.execute(f"ALTER TABLE master_recipes ADD COLUMN {_gc} TEXT "
                                 f"GENERATED ALWAYS AS (json_extract(data,'{_expr}')) VIRTUAL")
                    print(f"[MIGRATE] added master_recipes.{_gc} generated column")
                except sqlite3.OperationalError:
                    pass  # already present
            conn.execute("CREATE INDEX IF NOT EXISTS idx_master_dish_key ON master_recipes(dish_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_master_publisher_key ON master_recipes(publisher_key)")
            # Scoring fast paths (2026-07-30). The blend INPUTS were reachable only
            # by parsing `data` in Python, so a plain corpus question — "top 200 by
            # blended score, grouped by domain" — meant loading every row and
            # recomputing. These make the inputs SQL-native and indexable.
            #
            # The BLEND ITSELF is deliberately NOT here: it is a percentile over a
            # cohort, which needs a window function, and generated columns cannot
            # contain subqueries, joins or window functions. Rank in SQL with
            # PERCENT_RANK() over these columns (see memory/project_ou_power_blend);
            # what the cohort IS remains the open design question.
            #
            # `source_host` is parsed in SQL rather than read from
            # `_scoring.rootDomain`, which is NOT the host — it returns registry
            # suffixes like "co.uk". Scheme stripped, path/query/fragment cut,
            # leading www. dropped, lowercased.
            _url = "json_extract(data,'$._source.originalUrl')"
            _after_scheme = (f"CASE WHEN instr({_url},'://') > 0 "
                             f"THEN substr({_url}, instr({_url},'://') + 3) ELSE {_url} END")
            _host = (f"CASE WHEN instr({_after_scheme},'/') > 0 "
                     f"THEN substr({_after_scheme}, 1, instr({_after_scheme},'/') - 1) "
                     f"ELSE {_after_scheme} END")
            _host_nq = (f"CASE WHEN instr({_host},'?') > 0 "
                        f"THEN substr({_host}, 1, instr({_host},'?') - 1) ELSE {_host} END")
            _host_final = (f"lower(CASE WHEN {_host_nq} LIKE 'www.%' "
                           f"THEN substr({_host_nq}, 5) ELSE {_host_nq} END)")
            # FACET COLUMNS. Everything the search UI can filter or sort on gets a
            # real, indexed SQL surface — reaching into JSON with json_extract at
            # query time is what made DISTINCT cuisine cost 471ms while the
            # indexed source_host equivalent cost 14.6ms over 6.5x more distinct
            # values. The dropdowns are built by SELECT DISTINCT against these,
            # scoped to the filters already chosen, so they have to be cheap.
            #
            # Source is `_identity` (the structured identity card), NOT
            # provenance.ethnicity (6.2% coverage on master) and NOT the
            # site-declared recipeCuisine (86%, and it is whatever the publisher
            # typed). _identity.cuisine and .ethnicity are on 100% of rows in both
            # tables and are URL-static — the card travels with the recipe through
            # claim / cache / promote — so a facet built on them stays put.
            #
            # TRIM + NULLIF because a facet must distinguish "not set" from "set
            # to blank": a '' would otherwise be offered as a selectable cuisine.
            # Semantic junk ("<UNKNOWN>") is scrubbed at the extractor instead —
            # see extract/identity_card._scrub_placeholders — because the sink
            # should not have to know the model's vocabulary of evasions.
            def _facet(path):
                return f"NULLIF(TRIM(COALESCE(json_extract(data,'{path}'),'')),'')"

            for _tbl in ("master_recipes", "recipes"):
                for _gc, _type, _expr in (
                    ("cuisine", "TEXT", _facet('$._identity.cuisine')),
                    ("ethnicity", "TEXT", _facet('$._identity.ethnicity')),
                    ("chapter", "TEXT", _facet('$.classification.chapter')),
                    ("recipe_name", "TEXT", _facet('$.name')),
                    ("ou_score", "REAL", "json_extract(data,'$._scoring.ouScore')"),
                    ("domain_authority", "REAL", "json_extract(data,'$._scoring.domainAuthority')"),
                    ("page_authority", "REAL", "json_extract(data,'$._scoring.pageAuthority')"),
                    # power = DA + PA, the blend's second term, DERIVED not read.
                    # `_scoring.power` IS stored, and it is 0.0 on 1,775 master
                    # rows (51%) whose DA and PA are both real and non-zero —
                    # the zero propagated into powerPercentile, so anything
                    # ranked on the stored pair put half the corpus at the floor
                    # of the power dimension. Computing from DA/PA is correct on
                    # every row, so this column is the trustworthy one.
                    ("power", "REAL", "json_extract(data,'$._scoring.domainAuthority') + "
                                      "json_extract(data,'$._scoring.pageAuthority')"),
                    ("source_host", "TEXT", _host_final),
                    # SEMrush per-page monthly organic traffic, on ~54% of master
                    # rows (publisher harvests carry it; SERP-sourced dish harvests
                    # do not). Exposed as a column so it can be the ORDER BY
                    # tiebreaker when the authority score ties — which it does
                    # constantly, because PA saturates: 23 of shop.legalseafoods'
                    # 30 recipes share PA=30, and allrecipes has 18 distinct PA
                    # values across 153 rows. Ties were being broken on `m.id`.
                    ("traffic", "REAL", "json_extract(data,'$._scoring.traffic')"),
                    # The is-recipe confidence, on ~100% of rows.
                    ("recipe_score", "REAL", "json_extract(data,'$._scoring.recipeScore')"),
                    # The STORED percentiles, exposed for AUDIT rather than for
                    # ranking: they carry the zero-power bug above, and they were
                    # computed per dish-run so they are not comparable across the
                    # corpus anyway (fieldScope is empty on every row, so the
                    # cohort they belong to isn't even recorded). Rank with
                    # PERCENT_RANK() over ou_score/power instead.
                    # PAYWALL DA-ADJUSTMENT (pa_gap_v1) — stored, not derived on
                    # read, so a report can be written in plain SQL instead of by
                    # running the pipeline, and so the adjustment IN FORCE when a
                    # page was scored stays auditable after a recalibration.
                    # `domain_authority` above remains the raw Moz measurement;
                    # these sit beside it as an explicitly labelled judgment.
                    ("adjusted_da", "REAL",
                     "json_extract(data,'$._scoring.adjustedDomainAuthority')"),
                    ("adjusted_ou_score", "REAL",
                     "json_extract(data,'$._scoring.adjustedOuScore')"),
                    ("paywall_discount_pct", "REAL",
                     "json_extract(data,'$._scoring.paywallDiscountPct')"),
                    # WHICH formula produced the number, so a row calibrated by a
                    # superseded method is identifiable rather than merely suspect.
                    ("paywall_adj_method", "TEXT",
                     "json_extract(data,'$._scoring.paywallAdjMethod')"),
                    # THE COLUMN TO RANK ON. Falls back to the raw OU when no
                    # adjustment applies, so a query never has to know whether a
                    # publisher is gated — and can't accidentally rank gated rows
                    # on the un-adjusted figure by forgetting the COALESCE.
                    ("effective_ou_score", "REAL",
                     "COALESCE(json_extract(data,'$._scoring.adjustedOuScore'), "
                     "json_extract(data,'$._scoring.ouScore'))"),
                    ("stored_ou_pct", "REAL", "json_extract(data,'$._scoring.ouPercentile')"),
                    ("stored_power_pct", "REAL", "json_extract(data,'$._scoring.powerPercentile')"),
                    ("stored_power", "REAL", "json_extract(data,'$._scoring.power')"),
                    # PA PROVENANCE — the flag that makes a fabricated authority
                    # score selectable in SQL instead of invisible. Three states:
                    #   NULL  scored before 2026-08-04, when the Moz gate was
                    #         missing — provenance UNKNOWN, needs re-verifying
                    #   0     verified: Moz has no data, so page_authority on
                    #         this row is the domain-derived PLACEHOLDER
                    #   >0    verified measured
                    # The placeholder parks a row near the OU=0 line (measured
                    # 2026-08-04: 0 of the top 200 by OU are fabricated, which is
                    # why they were left in place rather than stripped), but it
                    # must be excluded from any fit over PA — notably the paid-PA
                    # calibration, whose population IS these blocked publishers.
                    ("moz_http_code", "INTEGER", "json_extract(data,'$._scoring.mozHttpCode')"),
                ):
                    try:
                        conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_gc} {_type} "
                                     f"GENERATED ALWAYS AS ({_expr}) VIRTUAL")
                        print(f"[MIGRATE] added {_tbl}.{_gc} generated column")
                    except sqlite3.OperationalError:
                        pass  # already present
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_tbl}_source_host "
                             f"ON {_tbl}(source_host)")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_tbl}_ou_score "
                             f"ON {_tbl}(ou_score)")
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_tbl}_power "
                             f"ON {_tbl}(power)")
                # Facet indexes. (user_id, col) because every list/facet query
                # filters by owner first, so the owner has to lead or the index
                # is only half usable.
                for _fc in ("cuisine", "ethnicity", "chapter", "recipe_name"):
                    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_tbl}_{_fc} "
                                 f"ON {_tbl}(user_id, {_fc})")
            # Same source-of-truth embedding on USER recipes: every save embeds
            # the recipe so its vector is available for dish-matching, "find
            # similar", dedup, and recommendations (not single-use). 2026-06-02.
            recipes_cols = {row[1] for row in conn.execute("PRAGMA table_info(recipes)")}
            if "embedding" not in recipes_cols:
                conn.execute("ALTER TABLE recipes ADD COLUMN embedding BLOB")
                print("[MIGRATE] added recipes.embedding BLOB column")

            # === users ===
            # Test scaffolding for multi-user flows until Ghost (or another
            # auth provider) lands. Column shape mirrors Ghost's `members`
            # table so the eventual migration is a UPSERT-by-email or a
            # UPSERT-by-ghost_uuid, not a schema rewrite:
            #   - user_id: our existing INTEGER surrogate, already wired
            #     into every other table (recipes.user_id, journal rows,
            #     etc.). Keep this as the stable internal key.
            #   - ghost_uuid: nullable; populated when Ghost integrates
            #     (Ghost member id is a UUID).
            #   - email: Ghost's natural key. Nullable for stub users.
            #   - status: 'free' | 'paid' | 'comped' (Ghost values) + 'test'.
            # user_id=0 is reserved for master_recipes (curator pseudo-user)
            # and is NOT a row in this table.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ghost_uuid        TEXT,
                    email             TEXT,
                    name              TEXT,
                    status            TEXT NOT NULL DEFAULT 'test',
                    subscription_tier TEXT,
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL
                );
            """)
            try:
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_users_email "
                    "ON users(email) WHERE email IS NOT NULL AND email != ''"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_users_ghost_uuid "
                    "ON users(ghost_uuid) WHERE ghost_uuid IS NOT NULL AND ghost_uuid != ''"
                )
            except sqlite3.IntegrityError as e:
                print(f"[WARN] could not add users unique indexes: {e}")
            # Migration (2026-05-27): add `role` column to mirror Ghost's
            # staff/member identity model (owner/admin/editor/author/
            # contributor for staff; 'member' for subscribers). Defaults
            # to 'member' — admin status is hand-promoted manually until
            # Ghost integrates and tier-driven promotion can replace it.
            user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
            if "role" not in user_cols:
                conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
                print("[MIGRATE] added users.role column (default 'member')")
            _seed_users_from_recipes(conn)

            ensure_metabase_url_table(conn)
            ensure_bcc_token_journal_table(conn)
            ensure_llm_extract_cache_table(conn)
            dishes_lib.ensure_dishes_table(conn)
            from input.pipeline.chapters import ensure_chapters_table
            ensure_chapters_table(conn)
            jobs_lib.ensure_jobs_table(conn)
            from input.pipeline.system_config import ensure_system_config_table
            ensure_system_config_table(conn)
            from input.pipeline.scheduled_jobs import ensure_scheduled_jobs_table
            ensure_scheduled_jobs_table(conn)
            from input.pipeline.cook_kb import ensure_cook_kb_table
            ensure_cook_kb_table(conn)
            # Per-user bookmarklet API keys (users.api_key_*).
            from input.pipeline.auth import (ensure_api_key_columns,
                                             ensure_password_column,
                                             ensure_api_keys_table)
            ensure_api_key_columns(conn)
            ensure_password_column(conn)
            ensure_api_keys_table(conn)   # per-device keys + migrate the old column
            # Generic admin-managed tables (status_messages, etc.) — each
            # registered AdminModel's table is created + seeded here.
            from admin_models import ensure_admin_tables
            ensure_admin_tables(conn)
            # sqlite-vec virtual tables for dish + master recipe KNN.
            # Best-effort: if the extension is missing the cohort matcher
            # falls back to the in-Python scan path (which has been
            # kept intact during the migration as belt + suspenders).
            try:
                from input.pipeline import vector_store
                vector_store.ensure_vec_tables(conn)
                print("[VEC] sqlite-vec virtual tables ready")
            except Exception as e:
                print(f"[WARN] sqlite-vec init failed (KNN disabled): {e}")
            # Reset any jobs that were 'running' when the prior process died —
            # they're not coming back, but they'd otherwise sit blocking new
            # enqueues for the same entity. SKIP when imported by the jobs CLI
            # (BCC_SKIP_JOB_RESET=1): a worker importing us must NOT wipe a job
            # running concurrently in another process — only the server does this
            # cleanup, on its own startup.
            if not os.getenv("BCC_SKIP_JOB_RESET"):
                interrupted = jobs_lib.reset_interrupted_jobs(conn)
                if interrupted:
                    print(f"[JOBS] reset {interrupted} interrupted job(s) from prior run")
            # Performance indexes (2026-06-15 query audit). Expression indexes on the
            # hot JSON fields the corpus filters/groups by — without them every such
            # query SCANs + parses all ~19KB master_recipes blobs (the recommender
            # path measured 21.9ms -> 0.23ms, ~95x, with idx_mr_dish). Mirrors the
            # existing idx_recipe_json_id precedent. Plus DROP two prefix-redundant
            # indexes (single-col indexes whose columns are a prefix of a composite/
            # unique index that already covers them). All idempotent → safe every boot.
            for _ix_stmt in (
                "CREATE INDEX IF NOT EXISTS idx_mr_dish ON master_recipes(json_extract(data,'$._master.dish'))",
                "CREATE INDEX IF NOT EXISTS idx_mr_likelydish ON master_recipes(json_extract(data,'$._identity.likelyDish'))",
                "CREATE INDEX IF NOT EXISTS idx_recipes_likelydish ON recipes(json_extract(data,'$._identity.likelyDish'))",
                "CREATE INDEX IF NOT EXISTS idx_mr_chapter ON master_recipes(json_extract(data,'$.classification.chapter'))",
                # The sidebar's DEFAULT sort, and the only one that had no index:
                # WHERE user_id = ? ORDER BY updated_at DESC planned as a full SCAN
                # plus a temp B-tree, which is why "Last modified (newest)" measured
                # 187ms on master while the indexed OU sort measured 4ms — the
                # cheapest list in the app was paying the most. DESC in the index so
                # the scan direction matches; user_id leads because every list query
                # filters on it.
                "CREATE INDEX IF NOT EXISTS idx_master_recipes_user_updated "
                "ON master_recipes(user_id, updated_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_recipes_user_updated "
                "ON recipes(user_id, updated_at DESC)",
                # Same shape for "Date added (newest)".
                "CREATE INDEX IF NOT EXISTS idx_master_recipes_user_created "
                "ON master_recipes(user_id, created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_recipes_user_created "
                "ON recipes(user_id, created_at DESC)",
                "DROP INDEX IF EXISTS idx_drdp_dish",                  # prefix of idx_drdp_dish_rank
                "DROP INDEX IF EXISTS idx_dish_editors_choice_dish",   # prefix of the (dish,url) unique
            ):
                try:
                    conn.execute(_ix_stmt)
                except Exception as _ix_e:  # noqa: BLE001
                    print(f"[SETUP] perf-index skipped ({_ix_stmt[:48]}…): {_ix_e}")

            # ---- FULL-TEXT SEARCH -------------------------------------------
            # The list used to search with LIKE %needle%, a CONTIGUOUS SUBSTRING
            # test rather than a search: "shrimp and corn chowder" could not
            # match "Corn and Shrimp Chowder", and "corn" matched Pop-corn
            # Shrimp because a substring has no word boundaries.
            #
            # `porter` stems, so "chowders" finds Chowder — the word-breaking +
            # stemming half of what SQL Server FREETEXT gives. Accents fold via
            # remove_diacritics so "bechamel" finds "Bechamel", matching what
            # bcc_sortkey already does for the name sort.
            #
            # Columns are the two retrieval surfaces plus what bridges them:
            #   name         what the publisher called it — what people type
            #   dish         _identity.likelyDish, the canonical name, so
            #                "risotto" finds every risotto whatever its title.
            #                It is also the ONE field both engines index: the
            #                vector embeds it and so does this.
            #   ingredients  lets "shrimp" reach a dish whose title never says
            #                so — 31 master rows have shrimp AND corn in their
            #                ingredients and were invisible to search entirely
            #   cuisine      cheap, and lets a typed word stand in for the facet
            #
            # A content-carrying table (not content=) so ordinary INSERT/UPDATE/
            # DELETE by rowid work and the triggers stay readable. ~10MB.
            for _tbl in ("master_recipes", "recipes"):
                _fts = f"{_tbl}_fts"
                conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_fts} USING fts5("
                    f"  name, dish, ingredients, cuisine,"
                    f"  tokenize='porter unicode61 remove_diacritics 2')"
                )
                # The ingredient list is a JSON array, flattened with json_each.
                # Doing this in a TRIGGER rather than in Python means a row
                # written by ANY path — server save, harvest job, migration
                # script — lands in the index. That is the only way an index
                # like this stays honest.
                _cols = ("json_extract(NEW.data,'$.name'), "
                         "json_extract(NEW.data,'$._identity.likelyDish'), "
                         "(SELECT group_concat(value, ' ') "
                         " FROM json_each(NEW.data, '$.recipeIngredient')), "
                         "json_extract(NEW.data,'$._identity.cuisine')")
                for _sfx in ("ai", "au", "ad"):
                    conn.execute(f"DROP TRIGGER IF EXISTS {_tbl}_fts_{_sfx}")
                conn.execute(
                    f"CREATE TRIGGER {_tbl}_fts_ai AFTER INSERT ON {_tbl} BEGIN "
                    f"  INSERT INTO {_fts}(rowid, name, dish, ingredients, cuisine) "
                    f"  VALUES (NEW.id, {_cols}); END"
                )
                conn.execute(
                    f"CREATE TRIGGER {_tbl}_fts_au AFTER UPDATE OF data ON {_tbl} BEGIN "
                    f"  DELETE FROM {_fts} WHERE rowid = OLD.id; "
                    f"  INSERT INTO {_fts}(rowid, name, dish, ingredients, cuisine) "
                    f"  VALUES (NEW.id, {_cols}); END"
                )
                conn.execute(
                    f"CREATE TRIGGER {_tbl}_fts_ad AFTER DELETE ON {_tbl} BEGIN "
                    f"  DELETE FROM {_fts} WHERE rowid = OLD.id; END"
                )
                # Backfill whatever the triggers never saw. Self-healing: a row
                # that somehow misses the index is picked up on the next boot
                # instead of staying unsearchable forever.
                _missing = conn.execute(
                    f"SELECT COUNT(*) FROM {_tbl} t "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {_fts} f WHERE f.rowid = t.id)"
                ).fetchone()[0]
                if _missing:
                    conn.execute(
                        f"INSERT INTO {_fts}(rowid, name, dish, ingredients, cuisine) "
                        f"SELECT t.id, json_extract(t.data,'$.name'), "
                        f"       json_extract(t.data,'$._identity.likelyDish'), "
                        f"       (SELECT group_concat(value,' ') "
                        f"        FROM json_each(t.data,'$.recipeIngredient')), "
                        f"       json_extract(t.data,'$._identity.cuisine') "
                        f"FROM {_tbl} t "
                        f"WHERE NOT EXISTS (SELECT 1 FROM {_fts} f WHERE f.rowid = t.id)"
                    )
                    print(f"[SETUP] {_fts}: indexed {_missing} row(s)")

            # Refresh query-planner statistics. WITHOUT them SQLite mis-planned the
            # dish top-10 JOIN — scanning all of master_recipes instead of using the
            # (url_normalized, user_id) index — turning a ~9ms query into a multi-
            # second one on a cold cache (the "long loading" on a fresh dish run,
            # 2026-06-15). PRAGMA optimize re-ANALYZEs only what changed, cheaply,
            # every boot — and establishes stats on a fresh/portable install so the
            # good plan is the default out of the box.
            try:
                conn.execute("PRAGMA optimize")
            except Exception as _opt_e:  # noqa: BLE001
                print(f"[SETUP] PRAGMA optimize skipped: {_opt_e}")
        print("[OK] Database tables ready")
    except Exception as e:
        print(f"[ERROR] Database initialization error: {e}")
        raise


# Initialize the app without lifespan for now to avoid hanging
app = FastAPI()


# ONE PLACE THAT DECIDES WHAT A NON-STAFF CALLER IS TOLD WHEN SOMETHING BREAKS.
#
# 31 endpoints an ordinary member can reach raise `detail=f"...: {e}"` — the raw
# exception, straight to the client. A sqlite error names columns and tables; an
# SDK error names the model and provider we call; an OSError names a path on this
# machine. None of that is meaningful to someone capturing a recipe, and all of it
# is ours.
#
# Fixed HERE rather than at 31 call sites, for the same reason the unscored-note
# redaction lives at the response boundary: a rule applied in one place cannot be
# forgotten by the 32nd endpoint. The detailed text is still LOGGED in full, and
# staff still receive it — an operator debugging a failure needs the real error.
#
# 5xx only. 4xx details are written for the user ("Recipe must have a name",
# "url is required") and are the actionable half of the API; blanketing those
# would make the product worse, not safer.
GENERIC_SERVER_ERROR = (
    "Something went wrong on our end. Your work wasn't lost — please try again.")


@app.exception_handler(HTTPException)
async def _redact_server_errors(request: Request, exc: HTTPException):
    detail = exc.detail
    if exc.status_code >= 500:
        print(f"[HTTP {exc.status_code}] {request.method} {request.url.path} :: {detail}")
        try:
            staff = auth_lib.is_staff(_resolve_caller(request) or {})
        except Exception:
            staff = False          # fail CLOSED — an auth hiccup must not leak
        if not staff:
            detail = GENERIC_SERVER_ERROR
    return JSONResponse(status_code=exc.status_code, content={"detail": detail},
                        headers=getattr(exc, "headers", None))


# Initialize DB immediately instead of using lifespan
print("[SETUP] Initializing database...")
init_db()
# Load the ingredient synonym map (cached) so embeddings.normalize() canonicalizes
# ingredient phrasing during embed composition. Seeds the table on first run.
try:
    with _db() as _c:
        from input.pipeline import ingredients_lib
        ingredients_lib.load_map(_c)
        print(f"[SETUP] ingredient synonym map loaded ({len(ingredients_lib._MAP or {})} terms)")
except Exception as _e:
    print(f"[SETUP] ingredient synonym map load failed: {_e}")
print("[OK] Database initialized successfully")

print("[NET] Setting up CORS...")

# CORS for frontend interaction
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Compress anything worth compressing. The list endpoints return JSON that is
# almost all repeated keys, so it deflates to ~17% of its raw size — measured
# 2026-08-20 on GET /recipes?user_id=0&summary=1: 6.89 MB -> 1.18 MB.
#
# Cloudflare already does this for traffic arriving through the tunnel, so the
# win here is for clients that reach the app DIRECTLY (LAN address, localhost,
# a job, the bookmarklet). Those were downloading the full uncompressed body.
# Doubly-compressing is not a risk: CF sees the Content-Encoding and passes it
# through rather than re-encoding.
#
# 1 KB floor so tiny JSON replies don't pay the gzip framing overhead, and
# level 5 because the marginal bytes past that cost more CPU than they save
# wall-clock (0.07s to compress the 6.89 MB list).
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)


class _NoGzipForSSE:
    """Keep GZipMiddleware away from Server-Sent Events.

    FOUND IN USE, 2026-08-20, hours after adding compression: a publisher-refresh
    job ran to completion and wrote a 127KB log, and BOTH job-log viewers showed
    nothing at all while it ran. The job was fine; the transport was not.

    /jobs/<id>/stream is an SSE endpoint, and SSE only works because each event
    is flushed as it is produced. Compression is a buffering operation — it
    holds bytes back to find something to compress — so gzipping a live stream
    turns "one line at a time" into "everything, eventually". The endpoint
    already sends `X-Accel-Buffering: no`, but that is a request to an upstream
    PROXY and says nothing to middleware running inside this app.

    Content negotiation happens before a handler is chosen, so the response's
    content-type is not known when GZipMiddleware makes its decision. What IS
    known is what the client asked for: EventSource always sends
    `Accept: text/event-stream`. Dropping Accept-Encoding on those requests
    means GZipMiddleware sees a client that does not want compression and leaves
    the stream alone, without reaching into its internals.

    Registered AFTER GZipMiddleware deliberately: Starlette builds the stack in
    reverse, so the last middleware added is the OUTERMOST and runs first — which
    is the only order in which the header can be removed before gzip reads it.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = scope.get("headers") or []
            wants_sse = any(
                k == b"accept" and b"text/event-stream" in v.lower()
                for k, v in headers
            )
            if wants_sse:
                scope = dict(scope)
                scope["headers"] = [(k, v) for k, v in headers
                                    if k != b"accept-encoding"]
        await self.app(scope, receive, send)


app.add_middleware(_NoGzipForSSE)

# --- Public-host gate -------------------------------------------------------
# bestcooksclub.com (customers) and recipes.tbotb.com (staff) are the same app
# behind the same tunnel. On the public host, serve ONLY the customer allowlist
# in input/pipeline/host_gate.py; everything else 404s.
#
# 404 rather than 403 on purpose: a public visitor should not learn that
# /domains is a real endpoint. Staff hosts are untouched and still enforce
# _require_perm — this is a perimeter, not a substitute for the permission
# checks.
from input.pipeline import host_gate as _host_gate  # noqa: E402


@app.middleware("http")
async def _public_host_gate(request: Request, call_next):
    reason = _host_gate.blocked_reason(
        request.headers.get("host", ""), request.url.path, request.method)
    if reason:
        print(f"[HOSTGATE] {reason}")
        # A browser gets a page with a way out; an API client gets the JSON it
        # expects. Both are 404 and both are IDENTICAL for a blocked admin route
        # and a genuinely nonexistent one — that indistinguishability is the
        # point, and is why this stays a 404 rather than a 403.
        if "text/html" in (request.headers.get("accept") or ""):
            return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


_NOT_FOUND_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not found</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#faf6f1;color:#1f1611;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}
.b{text-align:center;padding:32px;max-width:26rem}h1{font-size:1.4rem;margin:0 0 8px}
p{color:#6b5b4f;margin:0 0 20px;line-height:1.5}
a{display:inline-block;padding:10px 20px;background:#b8602a;color:#fff;
text-decoration:none;border-radius:9px;font-weight:600}
</style></head><body><div class="b">
<h1>That page isn't here</h1>
<p>The link may be wrong, or the page may live on a different part of the site.</p>
<a href="/">Go to the home page</a>
</div></body></html>"""


print(f"[NET] Public-host gate active for: {', '.join(sorted(_host_gate.public_hosts()))}")

# Dev shield: keep the whole app OUT of search indexes while it's a development
# surface reachable via the Cloudflare tunnel (recipes.tbotb.com). robots.txt
# governs CRAWLING; this X-Robots-Tag governs INDEXING — so a crawler that is
# allowed to fetch (Cloudflare's managed robots.txt currently Allows search)
# still won't index the page. This header is the reliable in-app shield; the
# /robots.txt route below is belt-and-suspenders. SCOPE/REMOVE per path when
# real public pages ship (SEO recipe pages); for hard shielding use Cloudflare
# Access (auth) in front of the tunnel.
@app.middleware("http")
async def _noindex_header(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/robots.txt")
def robots_txt():
    # Dev: disallow all crawling. (Cloudflare may shadow this at the edge with
    # its managed file; the X-Robots-Tag middleware above is what reliably keeps
    # us out of the index.)
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


print("[FILE] Setting up static files...")

# Serve the web frontend (HTML / JS / CSS / bookmarklet) from the
# dedicated forms/ subdirectory. Previously this mount pointed at the
# project root, which meant /forms/save_recipe_api.py would have leaked
# Python source — moving the static surface into its own directory
# scopes the mount to web assets only. URL paths (`/forms/...`) are
# unchanged; the bat file, bookmarklet, and every <link>/<script>
# reference continue to work as-is.
class _NoCacheHTMLStatic(StaticFiles):
    """StaticFiles that makes browsers REVALIDATE .html on every load.

    HTML pages carry no `?v=` cache-buster (unlike our JS/CSS, which do), so
    without this a soft reload serves a stale page — the recurring "I don't see
    my change" trap (flagged repeatedly in bcc-state-code.md). `no-cache` does
    NOT mean "don't cache"; it means "always revalidate", so ETag/Last-Modified
    still yield cheap 304s when the page is unchanged — only genuinely-changed
    pages re-download. Versioned assets keep StaticFiles' default long cache.
    Also signals Cloudflare not to edge-cache the HTML."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        try:
            if isinstance(path, str) and path.endswith(".html"):
                resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        except Exception:
            pass
        return resp


try:
    forms_path = os.path.join(os.path.dirname(__file__), "forms")
    app.mount("/forms", _NoCacheHTMLStatic(directory=forms_path), name="forms")
    print(f"[OK] Static files mounted: {forms_path}")
except Exception as e:
    print(f"[WARN] Static files mount failed: {e}")

# Per-run log files for dish refreshes. Each /dishes/<name>/refresh
# call tees stdout to a file in this directory; the dish row stores
# the filename, and the dishes form surfaces a "View latest log" link
# via /logs/<filename>.
LOGS_DIR = Path(__file__).resolve().parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)
try:
    app.mount("/logs", StaticFiles(directory=str(LOGS_DIR)), name="logs")
    print(f"[OK] Logs mount: {LOGS_DIR}")
except Exception as e:
    print(f"[WARN] Logs mount failed: {e}")

# AI-generated dish images (DALL-E 3 via image_gen_openai). Each generation
# saves to forms/generated/<recipe_id>.jpg and gets served from here.
# Future: move to S3 / object storage when we have multi-image storage.
GENERATED_DIR = Path(__file__).resolve().parent / "generated"
GENERATED_DIR.mkdir(exist_ok=True)
try:
    app.mount("/generated", StaticFiles(directory=str(GENERATED_DIR)), name="generated")
    print(f"[OK] Generated images mount: {GENERATED_DIR}")
except Exception as e:
    print(f"[WARN] Generated images mount failed: {e}")

# Mount the Recipe Enrichment API (the third entity) as a sub-app so its test
# harness + /enrich are reachable through the main server (and thus the tunnel):
#   /enrich-api/        -> test form     /enrich-api/enrich -> POST
#   /enrich-api/blocks  -> registry      /enrich-api/health
# Build-up convenience: lets us exercise the API in-process / over the tunnel
# before it has its own deployment. The form's fetches are mount-relative.
try:
    from enrich.service import app as _enrichment_app
    app.mount("/enrich-api", _enrichment_app, name="enrichment_api")
    print("[OK] Recipe Enrichment API mounted at /enrich-api")
except Exception as e:
    print(f"[WARN] Enrichment API mount failed: {e}")


# Per-job Tee/lock/log-filename used to live here; moved to
# input/pipeline/jobs.py once the dish refresh became a job. The runner
# in jobs.py owns the tee context now — handlers just print() normally.

print("[ROUTE] Setting up routes...")


# Health check
@app.get("/healthz")
def health_check():
    """Liveness probe. Was served at `/` until 2026-07-30, when the front door
    needed that slot. Kept as JSON at a conventional path so anything polling it
    has somewhere to move to."""
    return {"status": "ok", "message": "Full API with error handling"}


@app.get("/")
def home_page(request: Request):
    """The front door. The URL picks the page:

        bestcooksclub.com  ->  forms/home.html        customer: sign in / sign up
        recipes.tbotb.com  ->  forms/admin_home.html  curator: sign in + alert tiles

    Decided HERE, by the same host_gate.is_public_host() the gate middleware
    calls, on the same Host header. It used to be one file that detected its own
    flavour in JS, which meant a second hostname list to keep in step (it had
    already drifted: the page counted bestcooks.club and every
    *.bestcooksclub.com subdomain as customer, the gate counted only the apex and
    www) and a flavour that could be answered out of the browser cache. A URL
    maps to a page; nothing downstream needs to guess.

    Falls back to the old health JSON if the file is missing, so a bad deploy
    degrades rather than 500s."""
    name = "home.html" if _host_gate.is_public_host(request.headers.get("host", "")) else "admin_home.html"
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forms", name)
    if os.path.exists(path):
        return FileResponse(path, media_type="text/html")
    return {"status": "ok", "message": "Full API with error handling"}


# Open-by-self-URL: /r/{recipe_id} → form pre-loaded with that recipe.
# This is the canonical addressable URL for any recipe. For URL-less
# recipes (handwritten, photo, typed) extract endpoints mint this same
# URL into _source.originalUrl so every recipe has a self-reference.
# Auth is intentionally NOT here yet — that's the visibility / users
# layer, which is a separate change. Right now, knowing the UUID == access.
from fastapi.responses import RedirectResponse

@app.get("/r/{recipe_id}")
def open_recipe_by_url(recipe_id: str):
    # Resolve which table the recipe lives in so the redirect carries the
    # right user_id — otherwise a master recipe URL fails to load when the
    # sidebar default user_id doesn't match the row's table. Unknown UUIDs
    # still redirect (form shows a not-found state); a 404 here would
    # confusingly bypass the form entirely.
    owner = _find_recipe_owner(recipe_id)
    target = f"/forms/recipe_form_styled.html?recipe_id={recipe_id}"
    if owner is not None:
        target += f"&user_id={owner}"
    return RedirectResponse(url=target, status_code=302)


@app.get("/cook/{recipe_id}")
def open_cook_view(recipe_id: str):
    """Clean entry to the hands-free cook view (Phase-4 renderer, forms/cook.html).
    Mirrors /r/<id>: resolve the owner so the page fetches from the right table,
    then redirect. Unknown UUIDs still redirect (the page shows a friendly
    not-reworked / not-found state) rather than 404-ing past the renderer."""
    owner = _find_recipe_owner(recipe_id)
    target = f"/forms/cook.html?recipe_id={recipe_id}"
    if owner is not None:
        target += f"&user_id={owner}"
    return RedirectResponse(url=target, status_code=302)


@app.post("/cook/ask")
def cook_ask_endpoint(payload: dict = Body(...)):
    """Chef — grounded cooking Q&A for the cook view (Tier-3 keystone of the
    hands-free experience; see cook_ask.py + recipe_anchor/voice-cook-spec.md).
    The voice loop will later feed this STT text and speak the reply; today it
    backs the typed "Ask Chef" box. Loads the recipe, answers grounded in its
    `_cook`, journals tokens, and degrades to a friendly 503 on an LLM failure."""
    recipe_id = (payload.get("recipe_id") or "").strip()
    question = (payload.get("question") or "").strip()
    current_step = payload.get("current_step")
    # The voice loop sets allow_actions=true: a wake-gated utterance becomes ONE
    # call that either answers OR returns a navigate action (conversational nav
    # like "I'm done, move on"). The typed "Ask Chef" box leaves it off (the user
    # navigates by clicking) and gets a plain answer.
    allow_actions = bool(payload.get("allow_actions"))
    try:
        user_id = int(payload.get("user_id", PLACEHOLDER_USER_ID))
    except (TypeError, ValueError):
        user_id = PLACEHOLDER_USER_ID
    if not recipe_id or not question:
        raise HTTPException(status_code=400, detail="recipe_id and question are required.")

    table = _recipes_table_for(user_id)
    with _db() as conn:
        row = conn.execute(
            f"SELECT data FROM {table} WHERE recipe_id = ?", (recipe_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Recipe not found.")
    recipe = json.loads(row[0])

    usage_log: list = []
    import llm  # gateway: attribute this Q&A's usage to the recipe/user
    llm.enter(recipe_id=recipe_id, user_id=user_id)
    try:
        if allow_actions:
            from cook_ask import ask_or_act
            result = ask_or_act(recipe, question, current_step=current_step, usage_log=usage_log)
        else:
            from cook_ask import ask as chef_ask
            result = {"kind": "answer",
                      "text": chef_ask(recipe, question, current_step=current_step, usage_log=usage_log)}
    except Exception as e:
        print(f"[ERROR] /cook/ask({recipe_id}): {e}")
        raise HTTPException(status_code=503, detail="Chef is unavailable right now — try again in a moment.") from e
    _journal_usage(usage_log, recipe_id=recipe_id, user_id=user_id)
    if result.get("kind") == "action":
        return {"action": result.get("action"), "step": result.get("step")}
    # Back-compat: always include `answer` so existing callers keep working.
    return {"answer": result.get("text", "")}


@app.post("/cook/ask-stream")
def cook_ask_stream_endpoint(payload: dict = Body(...)):
    """Streaming Chef — the low-latency voice path. Streams the answer as SSE
    `sentence` events (so the cook view speaks the first sentence while Chef is
    still composing the rest) OR a single `action` event when Chef navigates
    conversationally. Recipe adapter over the generic voice_agent engine
    (voice_agent.py / cook_ask.ask_or_act_stream). Tokens journaled at stream end."""
    recipe_id = (payload.get("recipe_id") or "").strip()
    question = (payload.get("question") or "").strip()
    current_step = payload.get("current_step")
    try:
        user_id = int(payload.get("user_id", PLACEHOLDER_USER_ID))
    except (TypeError, ValueError):
        user_id = PLACEHOLDER_USER_ID
    if not recipe_id or not question:
        raise HTTPException(status_code=400, detail="recipe_id and question are required.")

    table = _recipes_table_for(user_id)
    with _db() as conn:
        row = conn.execute(
            f"SELECT data FROM {table} WHERE recipe_id = ?", (recipe_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Recipe not found.")
    recipe = json.loads(row[0])

    usage_log: list = []

    def events():
        try:
            # NOTE: streaming SSE journals via the plain usage_log (voice_agent appends
            # to it), NOT the llm.py contextvar gateway — Starlette drives this generator
            # across separate threadpool contexts, so a contextvar set here is discarded
            # before the flush. The shared list survives. _journal_usage writes it below.
            from cook_ask import ask_or_act_stream
            for ev in ask_or_act_stream(recipe, question, current_step=current_step, usage_log=usage_log):
                yield ev
        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] /cook/ask-stream({recipe_id}): {e}")
            yield {"type": "error", "detail": "Chef is unavailable right now — try again in a moment."}
        finally:
            _journal_usage(usage_log, recipe_id=recipe_id, user_id=user_id)

    import voice_agent
    return StreamingResponse(
        voice_agent.sse(events()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/cook/listen")
async def cook_listen(audio: UploadFile = File(...)):
    """Speech-to-text for the hands-free loop: one VAD-endpointed utterance
    (WAV from the browser) -> faster-whisper (base.en, CPU) -> text. The browser
    routes the text (command keyword vs Chef question). Audio stays on the
    host. Degrades to a friendly 503 if the model can't load/transcribe."""
    data = await audio.read()
    try:
        from starlette.concurrency import run_in_threadpool
        from cook_stt import transcribe
        # OFF the event loop. transcribe() is ~420ms of CPU-bound work behind a
        # global model lock (measured 2026-07-29, base.en/int8: 414-478ms, flat
        # across 1-3s clips because Whisper pads to a 30s window). Called inline
        # in an `async def` it froze EVERY endpoint for that whole window — one
        # user never sees it, three concurrent cooks do.
        text = await run_in_threadpool(transcribe, data)
    except Exception as e:
        print(f"[ERROR] /cook/listen: {e}")
        raise HTTPException(status_code=503, detail="Speech recognition is unavailable.") from e
    return {"text": text}


@app.post("/cook/speak")
def cook_speak(payload: dict = Body(...)):
    """Text-to-speech in Chef's warm voice (OpenAI gpt-4o-mini-tts) — speaks
    a step or a Chef answer. Returns MP3 bytes the cook view plays via an
    <audio> element. The page falls back to browser SpeechSynthesis on failure."""
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required.")
    try:
        from cook_tts import synthesize_cached
        # Cache-first (media.db): a step spoken before — any session/device/user —
        # serves instantly and free. Miss synthesizes once and stores.
        audio, hit = synthesize_cached(text, MEDIA_DB_PATH)
    except Exception as e:
        print(f"[ERROR] /cook/speak: {e}")
        raise HTTPException(status_code=503, detail="Voice synthesis is unavailable.") from e
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"X-TTS-Cache": "hit" if hit else "miss"})


@app.post("/cook/voice-log")
def cook_voice_log(payload: dict = Body(...)):
    """Persist a cook-view voice-debug buffer (client-captured STT transcripts +
    how each routed) to a per-day file under logs/ so the dev can SEE what the mic
    actually heard on a remote device — the whole point being a closed debug loop
    for the voice loop (#5). Best-effort; never errors the caller."""
    try:
        rid = (payload.get("recipe_id") or "").strip()
        name = (payload.get("recipe_name") or "").strip()
        entries = payload.get("entries") or []
        if not entries:
            return {"ok": True, "written": 0}
        os.makedirs("logs", exist_ok=True)
        day = datetime.now().strftime("%Y-%m-%d")
        fname = f"{day}_cook_voice.log"   # date-first so logs/ lists chronologically
        with open(os.path.join("logs", fname), "a", encoding="utf-8") as f:
            f.write(f"\n==== voice log {datetime.now().astimezone().isoformat()} · {name or rid} "
                    f"({len(entries)} entries) ====\n")
            for e in entries[-500:]:
                t = str(e.get("t") or "")
                kind = str(e.get("kind") or "")
                text = str(e.get("text") or "").replace("\n", " ")
                f.write(f"  [{t}] {kind:9} {text}\n")
        return {"ok": True, "written": len(entries), "file": f"/logs/{fname}"}
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] /cook/voice-log: {e}")
        return {"ok": False, "error": str(e)}


# Fetch one recipe by recipe_id. Same shape as list_recipes() rows so the
# form's existing loadForm path can consume it directly.
#
# user_id dispatches to the right table (0 = master_recipes, else =
# recipes). Default 1 preserves prior behavior for any external callers.
# This is also a security boundary: a cross-table fetch (e.g. requesting
# a master row with user_id=1) returns 404 — the caller has no way to
# discover someone else's recipes by guessing recipe_ids.
@app.get("/recipes/{recipe_id}/memberships")
def recipe_memberships_endpoint(recipe_id: str, user_id: int = PLACEHOLDER_USER_ID):
    """All collections this recipe belongs to — dish winners (from the scoring
    ledger) + publisher/other collections (the collection_members junction),
    keyed on the recipe's url_normalized. Powers the form's 'Member of' chips."""
    from input.pipeline import collections_lib
    table = _recipes_table_for(user_id)
    try:
        with _db() as conn:
            row = conn.execute(
                f"SELECT url_normalized FROM {table} WHERE recipe_id = ?", (recipe_id,)
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Recipe not found")
            url_norm = row[0] or ""
            out: list = []
            if url_norm:
                for (dn,) in conn.execute(
                    "SELECT DISTINCT dish_name FROM dish_run_data_points "
                    "WHERE url = ? AND selected = 1", (url_norm,)):
                    if dn:
                        out.append({"type": "dish", "key": dn, "selected": True})
                out += collections_lib.get_memberships_for_url(conn, url_norm)
            return {"recipe_id": recipe_id, "memberships": out}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] recipe_memberships({recipe_id!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


# NOTE ON PLACEMENT: this route MUST be declared before /recipes/{recipe_id}
# below. FastAPI matches in declaration order, so a later /recipes/search would
# be swallowed by the {recipe_id} pattern and arrive as a lookup for a recipe
# whose id is the literal string "search". The implementation lives beside
# list_recipes and _recipe_list_data (search for _recipes_search_impl) where the
# rest of the list machinery is; only the decorator has to be up here.
@app.get("/recipes/search")
def search_recipes(
    user_id: int = PLACEHOLDER_USER_ID,
    q: str = "",
    cuisine: str = "",
    ethnicity: str = "",
    chapter: str = "",
    sort: str = "",
    limit: int = 200,
    offset: int = 0,
    facets: int = 1,
):
    """Faceted recipe search: one page of rows PLUS the counts every dropdown
    needs, in a single round trip. See _recipes_search_impl for the why."""
    return _recipes_search_impl(
        user_id=user_id, q=q, cuisine=cuisine, ethnicity=ethnicity,
        chapter=chapter, sort=sort, limit=limit, offset=offset, facets=facets,
    )


@app.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: str, user_id: int = PLACEHOLDER_USER_ID):
    table = _recipes_table_for(user_id)
    try:
        with _db() as conn:
            row = conn.execute(
                f"SELECT id, recipe_id, user_id, data, source_changed_at, created_at, updated_at "
                f"FROM {table} WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Recipe not found")
            _data = json.loads(row[3])
            # Stamp the paywall DA-adjustment for gated publishers, DERIVED from
            # the live calibration rather than read from storage. Both SELECTORS
            # already adjust (dish: _apply_paywall_remap; publisher:
            # domain_scoring.score_members) but the form showed the raw DA bar
            # and an ouScore computed from it — so an ATK page displayed OU -5.0
            # while selection had scored the same page well above zero. The
            # curator, and scoreCommentary, were reading the un-corrected number.
            # Absent when the publisher carries no adjustment
            # (memory/feedback_absent_not_zero).
            #
            # Resolved from the ORIGINAL URL first: `_scoring.rootDomain` holds
            # the apex while `domains` is keyed at full-host grain, and matching
            # on the apex found 0 of cooking.nytimes.com's 89 rows.
            # Rows saved before the adjustment shipped (or before the publisher
            # was calibrated) carry no stamp, so refresh it on read too. The
            # stamper is the SAME one the save path uses, so a read can never
            # show a different adjustment than the one that was stored.
            try:
                _sc = _data.get("_scoring") or {}
                if _sc:
                    from input.pipeline.url_scoring import stamp_paywall_adjustment
                    _url = (_data.get("_source") or {}).get("originalUrl") or ""
                    if stamp_paywall_adjustment(_sc, _url):
                        _data["_scoring"] = _sc
            except Exception as _e:
                print(f"[WARN] paywall DA-adjustment stamp skipped: {_e}")
            # user_id is returned at the top level (it's a column, not part of
            # the recipe blob) so the form's loadForm hydration can refresh
            # the admin band input to match the loaded row's actual owner —
            # prevents accidental "click master row, save to personal" forks
            # when the user has stale sidebar state.
            return {
                "id": row[0],
                "recipe_id": row[1],
                "user_id": row[2],
                "data": _data,
                "source_changed_at": row[4],
                "created_at": row[5],
                "updated_at": row[6],
                "bccUrl": _bcc_permalink(row[1]),
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error in get_recipe({recipe_id}): {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


def _master_result_row(d: dict, rid: str, *, dish=None, rank_score=None, distance=None) -> dict:
    """Shared result shape for the recommender (dish-anchored + vector paths)."""
    m = d.get("_master") or {}
    exc = m.get("exceptionalism") or {}
    src = d.get("_source") or {}
    img = d.get("image")
    s = d.get("_scoring") or {}
    return {
        "recipe_id": rid,
        "name": d.get("name") or "(no title)",
        "dish": dish if dish is not None else m.get("dish"),
        "grade": exc.get("grade"),
        # Raw signals for blend.rank_by_blend (it derives power = da + pa
        # itself; the stored _scoring.power is unreliable). Absent stays
        # None — percentile_ranks excludes the unmeasured from the
        # denominator rather than scoring them 0.
        "ou": s.get("ouScore"),
        "da": s.get("domainAuthority"),
        "pa": s.get("pageAuthority"),
        "rank_score": (round(rank_score, 1) if rank_score is not None else None),
        "distance": (round(distance, 4) if distance is not None else None),
        "preview_image": src.get("previewImage")
                         or (img[0] if isinstance(img, list) and img else None),
        "bcc_url": _bcc_link_permalink(rid),
        "source_url": src.get("originalUrl") or "",
    }


def _dish_cohort_ranked(conn, dish_name: str, want: int, exclude_recipe_id=None) -> list[dict]:
    """The dish's curated cohort, ordered by quality (rank_score desc, unscored
    last). Exact membership — the right answer to 'show me better <dish>s', with
    no vector cutoff to drop legitimate same-dish recipes."""
    rows = conn.execute(
        """
        SELECT m.recipe_id, m.data, dp.rank_score
        FROM master_recipes m
        LEFT JOIN dish_run_data_points dp
          ON dp.dish_name = :dish AND dp.url = m.url_normalized
        WHERE json_extract(m.data, '$._master.dish') = :dish
        -- Tiebreak on TRAFFIC when we have it, else insertion order. The authority
        -- score ties constantly (PA saturates), and `m.id` alone is only
        -- traffic-ordered WITHIN one harvest run — across runs each appends its
        -- own descending sequence, so 99 tied groups contradicted traffic order.
        -- NULLs last: SERP-sourced rows have no traffic and keep insertion order.
        ORDER BY dp.rank_score IS NULL, dp.rank_score DESC,
                 m.traffic IS NULL, m.traffic DESC, m.id
        """, {"dish": dish_name}).fetchall()
    out: list[dict] = []
    for rid, dj, rank_score in rows:
        if exclude_recipe_id and rid == exclude_recipe_id:
            continue
        try:
            d = json.loads(dj)
        except Exception:
            continue
        out.append(_master_result_row(d, rid, dish=dish_name, rank_score=rank_score))
        if len(out) >= want:
            break
    return out


@app.post("/recipes/similar-master")
def similar_master_recipes(payload: dict = Body(...)):
    """Recommender: given a recipe (the one on screen — may be unsaved), return
    the curated MASTER recipes most like it. Two tiers:

      1. DISH-ANCHORED (preferred): if the recipe confidently matches a dish,
         return that dish's COHORT ordered by quality (rank_score). Exact
         membership answers 'show me better risottos' — no vector cutoff to drop
         legitimate same-dish recipes (the bug: a risotto query landed 0.894
         from the cluster and got dropped though 8 risottos sat right there).
      2. VECTOR fallback: no confident dish match → recipe→recipe KNN over
         recipes_master_vec, cutoff from config `similar_max_distance`.

    Body: {"recipe": {...}, "k": <optional int, default 8>}. Each result:
    recipe_id, name, dish, grade, rank_score, distance (null in dish mode),
    preview_image, bcc_url, source_url."""
    recipe = (payload or {}).get("recipe") or {}
    want = max(1, min(int((payload or {}).get("k") or 8), 25))
    from input.pipeline.embeddings import (
        compose_recipe_text, embed_text, find_best_dish_match, _l2_to_cosine_sim,
    )
    from input.pipeline import vector_store
    # Tier 2 cutoff. 0.95 admitted 0.7-1.4% of the corpus — always more than
    # `want`, so it never trimmed and the window padded to k with whatever was
    # next (a mussel query reached out to fries and moussaka at d~0.90). 0.86
    # is inert for a typical recipe (median 26 neighbours inside it) and only
    # bites in sparse regions, which is exactly where the padding was reaching.
    try:
        from input.pipeline import system_config as _cfg
        similar_max = float(_cfg.get_setting("similar_max_distance", 0.86))
    except Exception:
        similar_max = 0.86
    # Tier 1 confidence bar. The SAVE path rejects a dish match at L2 >
    # `dish_match_max_distance` (0.8 -> cosine 0.68) and stamps `_match.dish =
    # None`; this endpoint was using the looser DEFAULT_MATCH_THRESHOLD (cosine
    # 0.55 ~ L2 0.95) and so ANCHORED to dishes the save path had already
    # thrown out — three mussel recipes anchored to Moussaka / Coquilles Saint
    # Jacques at 0.55-0.59 and returned those cohorts whole (Tier 1 applies no
    # distance cutoff by design). One question, one bar: derive it from the
    # same setting so the two paths cannot drift apart again.
    try:
        from input.pipeline import system_config as _cfg2
        _dish_max_l2 = float(_cfg2.get_setting("dish_match_max_distance", 0.8))
    except Exception:
        _dish_max_l2 = 0.8
    dish_min_conf = _l2_to_cosine_sim(_dish_max_l2)

    try:
        with _db() as conn:
            vector_store.enable_vec(conn)

            # --- Tier 1: dish-anchored -------------------------------------
            dish_match = None
            try:
                dish_match = find_best_dish_match(conn, recipe,
                                                  threshold=dish_min_conf)
            except Exception as e:
                print(f"[SIMILAR] dish-match skipped: {e}")
            if dish_match and dish_match.get("dish_name"):
                cohort = _dish_cohort_ranked(conn, dish_match["dish_name"], want,
                                             exclude_recipe_id=recipe.get("recipe_id"))
                if cohort:
                    print(f"[SIMILAR] {recipe.get('name','')!r} -> dish-anchored "
                          f"{dish_match['dish_name']!r} ({len(cohort)} cohort, "
                          f"conf={dish_match.get('confidence'):.3f})")
                    return {
                        "query_name": recipe.get("name") or "",
                        "mode": "dish",
                        "dish": dish_match["dish_name"],
                        "match_confidence": round(dish_match.get("confidence") or 0, 3),
                        "considered": len(cohort), "shown": len(cohort),
                        "results": cohort,
                    }

            # --- Tier 2: vector fallback -----------------------------------
            txt = compose_recipe_text(recipe)
            if not txt.strip():
                return {"query_name": recipe.get("name") or "", "mode": "vector",
                        "results": [], "considered": 0, "shown": 0}
            qvec = embed_text(txt)
            raw = vector_store.find_similar_master_recipes(conn, qvec, k=max(want * 4, 20))
            near = [r for r in raw if r["distance"] <= similar_max]
            # Drop the query recipe itself. Once a recipe is promoted to master
            # it sits in the index, and "recipes like this" would spend its top
            # slot recommending you the thing you are looking at. recipe_id does
            # NOT catch it — the master copy is a separate row with its own uuid
            # — so match on the source URL, which both carry.
            self_url = ""
            try:
                from input.pipeline.url_utils import normalize_url
                self_url = normalize_url(
                    ((recipe.get("_source") or {}).get("originalUrl") or "").strip())
            except Exception:
                self_url = ""
            results: list[dict] = []
            for r in near:
                row = conn.execute(
                    "SELECT recipe_id, data, url_normalized FROM master_recipes WHERE id = ?",
                    (r["id"],)).fetchone()
                if not row:
                    continue
                rid, dj, urln = row
                if self_url and urln and urln == self_url:
                    continue
                try:
                    d = json.loads(dj)
                except Exception:
                    continue
                dish = (d.get("_master") or {}).get("dish")
                rs = conn.execute(
                    "SELECT rank_score FROM dish_run_data_points WHERE dish_name = ? AND url = ?",
                    (dish, urln)).fetchone() if dish else None
                results.append(_master_result_row(
                    d, rid, dish=dish,
                    rank_score=(rs[0] if rs and rs[0] is not None else None),
                    distance=r["distance"]))
            # Two-stage, same shape as the harvest: SIMILARITY SELECTS the pool
            # (the <= similar_max cutoff above), the OU/power BLEND RANKS within
            # it — "show me the BEST recipes like this", not the most
            # numerically adjacent. Sorting by distance first is the tie-break:
            # rank_by_blend's sort is stable, so candidates that share a blend
            # score (notably the unmeasured, which all land on 0.0) stay in
            # closest-first order.
            # ORDER BY SIMILARITY. Deliberately NOT by quality, after two tries
            # that were wrong in the same way (curator, 2026-08-06).
            #
            # MASTER MEMBERSHIP IS ALREADY THE QUALITY STAGE. Every row here
            # survived harvest selection and curation, so re-ranking them on
            # authority charges a second toll on a signal the pool has already
            # applied — and it demotes whatever is merely unscored (5.3% carry
            # no grade, 0.5% no ouScore) for a missing measurement rather than
            # a bad one ([[feedback_absent_not_zero]]). Ranking on the blend put
            # Sole Meuniere above a near-identical moules mariniere at d=0.26;
            # blend*cosine fixed that case but kept the same flaw underneath.
            # Curator: "i'd rather see mussel recipes than sole regardless of
            # rank".
            #
            # So similarity is the only ordering the pool has not already
            # expressed. Quality is SHOWN, not sorted on: `grade` (94.7%
            # populated, A+..D-) rides in the response and the form renders it,
            # letting the reader judge instead of the ranker deciding for them.
            for r in results:
                dist = r.get("distance")
                r["similarity"] = round(
                    _l2_to_cosine_sim(dist), 4) if isinstance(dist, (int, float)) else None
            results.sort(key=lambda x: x["distance"])
            results = results[:want]
            print(f"[SIMILAR] {recipe.get('name','')!r} -> vector {len(results)} shown "
                  f"(of {len(near)} within {similar_max}, {len(raw)} scanned)")
            return {
                "query_name": recipe.get("name") or "", "mode": "vector",
                "considered": len(near), "shown": len(results), "results": results,
            }
    except Exception as e:
        print(f"[ERROR] similar_master_recipes failed: {e}")
        raise HTTPException(status_code=500, detail=f"Similar lookup failed: {e}") from e


# --- Ingredient synonym dictionary (ACD) -------------------------------------
@app.get("/ingredient-synonyms")
def list_ingredient_synonyms():
    """All synonym groups for the a/c/d editor, plus the alias-type vocab."""
    from input.pipeline import ingredients_lib
    with _db() as conn:
        ingredients_lib.ensure_ingredient_synonyms_table(conn)
        return {"groups": ingredients_lib.list_groups(conn),
                "alias_types": list(ingredients_lib.ALIAS_TYPES)}


@app.post("/ingredient-synonyms")
def upsert_ingredient_synonym(payload: dict = Body(...)):
    """Create/replace a canonical group. Body: {canonical, synonyms:[{alias,type}|str],
    category?, note?}. Reloads the cached map so embed-time normalize() is live."""
    from input.pipeline import ingredients_lib
    canon = (payload or {}).get("canonical")
    if not canon or not str(canon).strip():
        raise HTTPException(status_code=400, detail="canonical is required")
    with _db() as conn:
        ingredients_lib.ensure_ingredient_synonyms_table(conn)
        try:
            g = ingredients_lib.upsert_group(
                conn, canon, payload.get("synonyms") or [],
                payload.get("category"), payload.get("note"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        ingredients_lib.load_map(conn)  # refresh the in-process cache
    return g


@app.delete("/ingredient-synonyms/{canonical}")
def delete_ingredient_synonym(canonical: str):
    from input.pipeline import ingredients_lib
    with _db() as conn:
        ingredients_lib.ensure_ingredient_synonyms_table(conn)
        ok = ingredients_lib.delete_group(conn, canonical)
        ingredients_lib.load_map(conn)
    if not ok:
        raise HTTPException(status_code=404, detail="canonical not found")
    return {"deleted": canonical}


@app.post("/ingredient-synonyms/normalize")
def normalize_ingredient_preview(payload: dict = Body(...)):
    """Preview: what does a term normalize to right now? (editor test box)."""
    from input.pipeline import ingredients_lib
    term = (payload or {}).get("term") or ""
    return {"term": term, "canonical": ingredients_lib.normalize(term)}


# Claim a recipe — fast in-DB copy from wherever it lives (master or
# another user) into the target user's personal collection. Pure SQL,
# no LLM, no re-extract. Use cases:
#   - User browses /r/<master-id>, wants their own editable copy.
#   - Eventually: user-to-user sharing.
#
# Security stub: target_user_id must be non-zero (can't claim INTO master
# — that's a curator-only operation). Source must exist somewhere. No
# per-user ACL yet — same "knowing the UUID == access" model the GET
# endpoint uses. When the users layer lands, this is one of the places
# that needs a real check ("can target_user_id see source?").
@app.post("/recipes/{recipe_id}/claim")
def claim_recipe(recipe_id: str, target_user_id: int = Form(...)):
    if target_user_id == 0:
        raise HTTPException(status_code=403,
                            detail="Cannot claim into master collection")
    if target_user_id < 0:
        raise HTTPException(status_code=400, detail="target_user_id must be positive")

    source_owner = _find_recipe_owner(recipe_id)
    if source_owner is None:
        raise HTTPException(status_code=404, detail="Source recipe not found")

    source_table = _recipes_table_for(source_owner)
    target_table = _recipes_table_for(target_user_id)

    new_recipe_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _db() as conn:
            row = conn.execute(
                f"SELECT data, url_normalized FROM {source_table} WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if not row:
                # Should not happen (we just found the owner) but be defensive.
                raise HTTPException(status_code=404, detail="Source recipe vanished")

            source_data = json.loads(row[0])
            # Use static_subset to filter to platonic fields only — drops
            # the source row's id/user_id/_access/current_status/claim-
            # provenance/affiliateUrl/etc. The static subset INCLUDES the
            # LLM enrichment (provenance/classification/editorial) so the
            # claimer inherits "pay-once" enrichment from master. See
            # recipe_model.STATIC_TOP_LEVEL_FIELDS for the full split.
            data = static_subset(source_data)
            # Mint fresh per-row identity for the target user.
            data["id"] = new_recipe_id
            # Stamp claim provenance INSIDE _source so the UI can show
            # "claimed from master / from user N at <time>" without a
            # separate join. Layered on top of the static subset's
            # _source (which kept originalUrl/origin/type).
            source_block = data.get("_source") or {}
            source_block["claimedFrom"] = (
                "master" if source_owner == 0 else f"user:{source_owner}"
            )
            source_block["claimedAt"] = now
            source_block["claimedFromRecipeId"] = recipe_id
            data["_source"] = source_block

            # "Copy not subscription" — claimed rows are detached from
            # the source URL. We INTENTIONALLY leave url_normalized
            # blank so:
            #   - the daily cache-refresh's drift-stamp query (which
            #     scopes to url_normalized) cannot touch claimed rows;
            #   - the save endpoint's (url_normalized, user_id) dedup
            #     cannot adopt the claimed row when the user later does
            #     a fresh re-extract of the same URL — preserving the
            #     claimer's edits.
            # `_source.originalUrl` stays inside the data blob for
            # display ("claimed from allrecipes.com/..."); it's just no
            # longer the row's identity hook.

            # Re-claim short-circuit: if this user already claimed this
            # exact source recipe before, return their existing copy
            # rather than minting a parallel row. Keyed on the source
            # recipe_id (not URL) so it works under the no-url-link
            # model. JSON-extract on `_source.claimedFromRecipeId`.
            existing = conn.execute(
                f"SELECT recipe_id FROM {target_table} "
                f"WHERE user_id = ? "
                f"AND json_extract(data, '$._source.claimedFromRecipeId') = ? "
                f"LIMIT 1",
                (target_user_id, recipe_id),
            ).fetchone()
            if existing:
                print(f"[CLAIM] Re-claim short-circuit: user {target_user_id} "
                      f"already has {existing[0]} from source {recipe_id}")
                return {
                    "recipe_id": existing[0],
                    "url": f"/r/{existing[0]}",
                    "adopted_existing": True,
                }

            conn.execute(
                f"INSERT INTO {target_table} "
                f"(recipe_id, user_id, data, url_normalized, source_changed_at, created_at, updated_at) "
                f"VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (new_recipe_id, target_user_id, json.dumps(data, indent=2),
                 "", now, now),  # url_normalized="" — detached, see comment above
            )
            print(f"[CLAIM] {source_table}/{recipe_id} -> "
                  f"{target_table}/{new_recipe_id} (user {target_user_id})")
            return {
                "recipe_id": new_recipe_id,
                "url": f"/r/{new_recipe_id}",
                "adopted_existing": False,
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] claim_recipe({recipe_id} -> user {target_user_id}) failed: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Claim failed: {e}") from e
# === Image generation (DALL-E 3) ===
# Per-recipe dish image generation. Restored 2026-05-26 from the deleted
# image_gen_openai.py (commit 143e016^). Live form path:
#   POST /recipes/<id>/generate-image  (optional ?quality=hd&size=...)
# Loads recipe, calls generate_dish_image, saves to forms/generated/
# <recipe_id>.jpg, returns the served URL. The form's "Generate dish
# image" button posts here and stores the returned URL in the recipe's
# image[0] on the next save.
# Fetch an image from an external URL and save it locally — "co-opt
# the source image" so the recipe is permanently independent of
# whether the source site changes / deletes the image. Same target
# directory as /images uploads (forms/generated/upload_<uuid>.<ext>).
#
# Protections:
#   - URL scheme must be http(s); other schemes rejected
#   - Refuses obvious internal-network hostnames (SSRF mitigation)
#   - Content-Type must be image/*
#   - Max size 50 MB (streaming download checks as bytes arrive)
#   - 30s total timeout
@app.post("/images/fetch")
async def fetch_image_from_url(request: Request):
    """Body: {url: "https://..."}. Returns {url, bytes, source_url}."""
    try:
        body = await request.json()
    except Exception:
        # `from None` deliberately: the JSON decode error names a byte offset the
        # caller cannot act on, and "JSON body required" is the whole message.
        raise HTTPException(status_code=400, detail="JSON body required") from None
    source_url = ((body or {}).get("url") or "").strip()
    if not source_url:
        raise HTTPException(status_code=400, detail="`url` is required")
    if not (source_url.startswith("http://") or source_url.startswith("https://")):
        raise HTTPException(status_code=400,
                            detail="URL must be http(s)://")
    # SSRF-lite: reject obvious internal hostnames. This isn't a full
    # network-level protection (real one would resolve DNS + check
    # against RFC1918 ranges + IPv6 link-local) but kills the most
    # common foot-shooting vectors.
    from urllib.parse import urlparse
    host = (urlparse(source_url).hostname or "").lower()
    bad_hosts = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
    bad_prefixes = ("192.168.", "10.", "172.16.", "172.17.", "172.18.",
                    "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                    "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
                    "172.29.", "172.30.", "172.31.", "169.254.", "fe80:")
    if host in bad_hosts or any(host.startswith(p) for p in bad_prefixes):
        raise HTTPException(status_code=400,
                            detail="URL points at an internal/private host")

    import requests as _rq
    MAX_BYTES = 50 * 1024 * 1024  # 50 MB
    # PREFER THE FULL-SIZE ORIGINAL. Publishers routinely advertise a resize
    # derivative as their canonical image — m.xiachufang.com puts
    # `..._1080w_1920h.jpg?imageView2/1/w/300/h/200/q/75` in its own JSON-LD
    # Recipe.image, so taking the URL at face value banks a 300x200 postage
    # stamp of a picture whose filename states the real dimensions. The strip
    # lives HERE rather than in the bookmarklet so there is one implementation,
    # on the machine with the good connection.
    _fetch_url = source_url
    try:
        from input.pipeline.image_pipeline import _full_size_variant
        _full = _full_size_variant(source_url)
    except Exception:
        _full = None

    def _open(u):
        r = _rq.get(u, timeout=30, stream=True, headers={
            "User-Agent": "BCC-image-coopt/1.0 (recipes.tbotb.com)",
        })
        r.raise_for_status()
        return r

    try:
        # stream=True so we can size-check before fully buffering
        if _full and _full != source_url:
            try:
                resp = _open(_full)
                _fetch_url = _full
                print(f"[IMAGES] full-size original used: {_full[:90]}")
            except Exception as e:
                print(f"[IMAGES] full-size probe failed ({type(e).__name__}); "
                      f"using the URL as given")
                resp = _open(source_url)
        else:
            resp = _open(source_url)
    except _rq.RequestException as e:
        raise HTTPException(status_code=502,
                            detail=f"Source fetch failed: {type(e).__name__}: {e}") from e

    content_type = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400,
                            detail=f"Source URL didn't return an image (content-type: {content_type or 'unknown'})")
    # Map content-type to file extension. Same vocabulary as /images.
    ext_by_mime = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
        "image/png":  ".png", "image/webp": ".webp", "image/gif": ".gif",
        "image/heic": ".heic", "image/heif": ".heif",
    }
    ext = ext_by_mime.get(content_type, ".jpg")

    # Stream into memory with the size cap enforced as bytes arrive.
    buf = bytearray()
    for chunk in resp.iter_content(chunk_size=64 * 1024):
        if chunk:
            buf.extend(chunk)
            if len(buf) > MAX_BYTES:
                raise HTTPException(status_code=413,
                                    detail=f"Source image exceeds {MAX_BYTES // (1024*1024)} MB cap")
    if not buf:
        raise HTTPException(status_code=502, detail="Source returned 0 bytes")

    # Phase 1: standardize the fetched bytes (config-driven resize/format/EXIF
    # strip) + capture imageMeta + log. Localizing here is the coopt-to-local
    # step — a pasted/typed URL becomes a permanent, right-sized local JPEG we
    # own (no hotlink). Pillow failure → fall back to storing the raw bytes.
    from input.pipeline.image_pipeline import standardize_and_meta
    processed, meta = standardize_and_meta(bytes(buf), source_url=source_url, localized=True)
    if processed:
        filename = f"upload_{uuid.uuid4()}.jpg"
        (GENERATED_DIR / filename).write_bytes(processed)
    else:
        filename = f"upload_{uuid.uuid4()}{ext}"
        (GENERATED_DIR / filename).write_bytes(bytes(buf))
    url = f"/generated/{filename}"
    print(f"[IMGFETCH] {source_url} -> {filename} | imageMeta={meta}")
    return {"url": url, "bytes": meta.get("bytes", len(buf)),
            "source_url": source_url, "imageMeta": meta}


# Upload a user-supplied image (drag/drop/paste/picker from the form's
# hero-image area). Saves to forms/generated/upload_<uuid>.<ext> and
# returns the URL. The same `/generated/` mount serves both AI-generated
# and uploaded images — single static directory, single mount point.
# User-uploaded files are prefixed `upload_` to keep them visually
# distinct from the AI-generated `<recipe_id>.jpg` files in the directory
# listing.
@app.post("/images")
async def upload_image(image: UploadFile = File(...)):
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    # Map content_type to a reasonable extension. Pillow could sniff
    # this from bytes but the content_type the browser provides is
    # accurate enough for the common cases (jpeg, png, webp, gif).
    ext_by_mime = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
        "image/png":  ".png", "image/webp": ".webp", "image/gif": ".gif",
        "image/heic": ".heic", "image/heif": ".heif",
    }
    ext = ext_by_mime.get(image.content_type.lower())
    if not ext:
        # Fall back to whatever extension the browser claimed; refuse
        # anything that didn't come with an extension we recognize.
        from pathlib import PurePosixPath
        suffix = PurePosixPath(image.filename or "").suffix.lower()
        if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}:
            ext = suffix
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported image type: {image.content_type}",
            )
    content = await image.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty image upload")
    # Phase 1: standardize (config-driven resize/format/EXIF strip) + imageMeta
    # + log. Pillow failure → fall back to storing the raw upload unchanged.
    from input.pipeline.image_pipeline import standardize_and_meta
    processed, meta = standardize_and_meta(content, source_url=None, localized=True)
    if processed:
        filename = f"upload_{uuid.uuid4()}.jpg"
        (GENERATED_DIR / filename).write_bytes(processed)
    else:
        filename = f"upload_{uuid.uuid4()}{ext}"
        (GENERATED_DIR / filename).write_bytes(content)
    url = f"/generated/{filename}"
    print(f"[IMGUP] {filename} | imageMeta={meta}")
    return {"url": url, "bytes": meta.get("bytes", len(content)), "imageMeta": meta}


@app.post("/recipes/{recipe_id}/generate-image")
async def generate_recipe_image_endpoint(
    recipe_id: str,
    request: Request,
    quality: Optional[str] = None,
    size: Optional[str] = None,
    orientation: Optional[str] = None,
):
    # Lazy import — pulls openai client construction only when used.
    from image_gen_openai import (
        generate_dish_image, _build_dish_prompt, moderate_text, TWEAK_MAX_CHARS,
    )

    # Optional JSON body: {extra_prompt?: str}. User-supplied override
    # text appended to the auto-built prompt before generation.
    extra_prompt = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            extra_prompt = (body.get("extra_prompt") or "").strip()
    except Exception:
        pass  # no body, or malformed — treat as empty

    # Guard the ONLY user-controlled text in the prompt (the auto-built body is
    # derived from the recipe, which is trusted). (1) length cap, (2) OpenAI
    # moderation pre-check — reject before we pay for generation. gpt-image-1
    # also moderates the final prompt as a backstop.
    if extra_prompt:
        if len(extra_prompt) > TWEAK_MAX_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"Tweak is too long ({len(extra_prompt)} chars; max {TWEAK_MAX_CHARS}). "
                       f"Keep it to a short styling note.",
            )
        flagged, cats = moderate_text(extra_prompt)
        if flagged:
            print(f"[IMGGEN] tweak rejected by moderation ({cats}): {extra_prompt!r}")
            raise HTTPException(
                status_code=400,
                detail="That tweak was flagged by the content filter. "
                       "Keep it to dish-styling notes (ingredients, plating, lighting).",
            )

    owner = _find_recipe_owner(recipe_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    table = _recipes_table_for(owner)
    with _db() as conn:
        row = conn.execute(
            f"SELECT data FROM {table} WHERE recipe_id = ?",
            (recipe_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Recipe not found")

    try:
        recipe_dict = json.loads(row[0])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Recipe data unreadable: {e}") from e
    name = (recipe_dict.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400,
                            detail="Recipe needs a name before generating an image")

    # Pre-build the prompt so we can log + return it for transparency.
    # generate_dish_image internally calls _build_dish_prompt with the
    # same recipe dict + extra_prompt, so this is purely so the response
    # includes it.
    prompt = _build_dish_prompt(recipe_dict, extra_prompt=extra_prompt)
    print(f"[IMGGEN] {recipe_id} ({owner=}, {quality=}, {size=}, {orientation=}, "
          f"extra_prompt={extra_prompt!r}) name={name!r}")
    print(f"[IMGGEN] prompt: {prompt}")

    try:
        t0 = time.perf_counter()
        img_bytes = generate_dish_image(
            recipe_dict,
            quality=quality, size=size, orientation=orientation,
            extra_prompt=extra_prompt,
        )
        dt_ms = int((time.perf_counter() - t0) * 1000)
    except Exception as e:
        msg = str(e)
        print(f"[IMGGEN] FAILED {recipe_id}: {type(e).__name__}: {e}")
        # gpt-image-1's OWN safety system can reject a prompt — including at the
        # OUTPUT stage, after our text pre-check passes. Surface that as a clean
        # 400 with a friendly message, NOT a 5xx: Cloudflare replaces an origin
        # 5xx with its own HTML error page, which the form can't parse → the
        # "Generate failed: {}" the user saw.
        if any(s in msg for s in ("moderation_blocked", "safety system",
                                  "safety_violations", "content_policy")):
            raise HTTPException(
                status_code=400,
                detail="That image request was blocked by the content filter. "
                       "Keep the tweak to dish-styling notes (ingredients, plating, lighting).",
            ) from e
        raise HTTPException(status_code=500,
                            detail=f"Image generation failed: {type(e).__name__}: {e}") from e

    out_path = GENERATED_DIR / f"{recipe_id}.jpg"
    out_path.write_bytes(img_bytes)
    url = f"/generated/{recipe_id}.jpg"
    print(f"[IMGGEN] OK {recipe_id} -> {out_path} ({len(img_bytes)} bytes, {dt_ms}ms)")
    return {
        "url": url,
        "bytes": len(img_bytes),
        "elapsed_ms": dt_ms,
        "prompt": prompt,
    }


# === Users (test scaffold) ===
# Stub login surface. Backs the /forms/users.html picker page. Returns
# everything in the users table — the UI is the place to filter, not
# the API (so a future admin view can use the same endpoint). Ghost
# integration replaces these with a wrapper around the Members API;
# the column shape is already Ghost-compatible (see init_db users
# section), so callers don't have to change.
@app.get("/users")
def list_users():
    try:
        with _db() as conn:
            auth_lib.ensure_password_column(conn)
            auth_lib.ensure_email_verification_columns(conn)
            rows = conn.execute(
                "SELECT user_id, ghost_uuid, email, name, status, "
                "subscription_tier, role, created_at, updated_at, password_hash, "
                "COALESCE(email_verified, 0), email_verified_at "
                "FROM users ORDER BY user_id"
            ).fetchall()
            return [
                {
                    "user_id": r[0],
                    "ghost_uuid": r[1],
                    "email": r[2],
                    "name": r[3],
                    "status": r[4],
                    "subscription_tier": r[5],
                    "role": r[6] or "member",
                    "created_at": r[7],
                    "updated_at": r[8],
                    # Presence flag only — never the hash. Device keys live in
                    # user_api_keys and are read via /users/{id}/api-keys.
                    "has_password": bool(r[9]),
                    # Claimed vs proven. Every account predating mail reads
                    # false, which is accurate rather than a problem to fix.
                    "email_verified": bool(r[10]),
                    "email_verified_at": r[11],
                }
                for r in rows
            ]
    except Exception as e:
        print(f"[ERROR] list_users failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


# === Auth (pre-Ghost stub) ===
# Identifies the caller via the X-Self-User-Id header that
# library-shell.js auto-attaches from localStorage's app:self_user_id.
# Pre-Ghost this trusts the client header; post-Ghost the resolver
# swaps to validating a session JWT. Either way, callers get back the
# same shape: {user, permissions, is_staff}.
from input.pipeline import auth as auth_lib  # noqa: E402
from input.pipeline import mailer  # noqa: E402
from input.pipeline import mail_messages  # noqa: E402
from html import escape as html_escape  # noqa: E402


def _resolve_caller(request: Request) -> Optional[dict]:
    """Return the user dict for the caller, or None if no/invalid
    self-user-id header. Helper for endpoints that need to know who's
    calling.

    uid 0 (Master/curator) ALSO requires a valid X-Master-Token — see
    input/pipeline/auth.py. The header by itself used to be enough, which on a
    public hostname was a complete admin bypass."""
    header = request.headers.get("x-self-user-id")
    master_token = request.headers.get("x-master-token")
    # Bookmarklets run on a publisher's page with no session, so they carry a
    # per-user API key instead. A valid key outranks the self-id header — it is
    # authentication rather than a claim.
    api_key = (request.headers.get("x-bcc-key")
               or _bearer_key(request.headers.get("authorization")))
    session_token = request.headers.get("x-session-token")
    with _db() as conn:
        return auth_lib.resolve_user(conn, header, master_token, api_key,
                                     session_token)


def _bearer_key(authorization: Optional[str]) -> Optional[str]:
    """Accept `Authorization: Bearer bcc_…` as well as X-BCC-Key — some fetch
    wrappers and proxies strip unknown X- headers."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _require_self_or_perm(request: Request, user_id: int, perm: str) -> dict:
    """Allow a caller acting on their OWN record, else demand `perm`.

    Customers can see their own user record and mint their own bookmarklet key
    from it; only staff with `perm` may touch anyone else's. Without the
    self-clause this would be manage_users-only and no customer could ever
    generate a bookmarklet."""
    user = _resolve_caller(request)
    if user and int(user.get("user_id", -1)) == int(user_id):
        return user
    return _require_perm(request, perm)


# Brute-force throttle for POST /auth/master. scrypt already costs ~100ms per
# attempt, but a password endpoint reachable from the internet deserves an
# explicit ceiling. In-process is sufficient: one uvicorn worker today, and the
# real perimeter is Cloudflare Access.
_MASTER_FAILS: dict[str, list[float]] = {}
_MASTER_FAIL_WINDOW = 900     # 15 min
_MASTER_FAIL_MAX = 8


def _master_throttled(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _MASTER_FAILS.get(ip, []) if now - t < _MASTER_FAIL_WINDOW]
    _MASTER_FAILS[ip] = hits
    return len(hits) >= _MASTER_FAIL_MAX


@app.post("/auth/master")
async def auth_master(request: Request):
    """Exchange the master password for a short-lived curator token.

    Body: {password}. Returns {token, expires_in} on success. The client stores
    the token and sends it as X-Master-Token alongside X-Self-User-Id: 0.

    Returns 503 when no password is configured — that is the fail-closed state,
    not an error to route around. Set one with: python set_master_password.py"""
    ip = (request.client.host if request.client else "?") or "?"
    if _master_throttled(ip):
        raise HTTPException(status_code=429,
                            detail="Too many attempts. Wait 15 minutes and try again.")
    if not auth_lib.master_password_configured():
        raise HTTPException(
            status_code=503,
            detail="No master password is configured on this instance. "
                   "Run: python set_master_password.py")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    password = (payload or {}).get("password") or ""
    if not auth_lib.verify_master_password(password):
        _MASTER_FAILS.setdefault(ip, []).append(time.time())
        print(f"[AUTH] master login FAILED from {ip}")
        raise HTTPException(status_code=401, detail="Incorrect password.")
    token = auth_lib.mint_master_token()
    if not token:
        raise HTTPException(status_code=503, detail="Master token secret unavailable.")
    _MASTER_FAILS.pop(ip, None)
    print(f"[AUTH] master login OK from {ip}")
    return {"token": token, "expires_in": auth_lib.MASTER_TOKEN_TTL}


def _require_perm(request: Request, perm: str) -> dict:
    """Raise 403 unless the caller has `perm`. Returns the caller's
    user dict on success — useful for downstream logging / audit."""
    user = _resolve_caller(request)
    # 401 vs 403 is the difference between "who are you?" and "not you". They
    # need different UI: a lapsed session should offer a re-login, not tell you
    # your permissions are wrong. Reporting both as 403 read as a contradiction
    # — an owner account being told its role was 'anonymous'.
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Your session has expired or you are not signed in. "
                   "Sign in again to continue.")
    if not auth_lib.can(user, perm):
        role = user.get("role", "member")
        extra = ""
        if user.get("staff_locked"):
            extra = (f" This account is '{user.get('actual_role')}' but staff "
                     f"permissions are locked — unlock admin to use them.")
        raise HTTPException(
            status_code=403,
            detail=f"This action requires the '{perm}' permission "
                   f"(your role: '{role}').{extra}"
        )
    return user


@app.get("/auth/me")
def auth_me(request: Request):
    """Identify the caller and return their role + permission list. The
    frontend hits this on page load to decide what UI to render. Returns
    {user: null, role: 'anonymous', permissions: []} when no valid
    self-user-id is supplied — the caller is treated as anonymous."""
    user = _resolve_caller(request)
    if user is None:
        return {
            "user": None,
            "role": "anonymous",
            "permissions": [],
            "is_staff": False,
            # Present and false rather than absent. A field that only sometimes
            # appears is how the client ends up guessing, which is the drift this
            # endpoint now exists to prevent.
            "enrich_available": False,
        }
    role = user.get("role") or "member"
    return {
        "user": user,
        "role": role,
        "permissions": auth_lib.permissions_for(role),
        "is_staff": auth_lib.is_staff(user),
        # Whether the UI should OFFER the Enrich control. Computed here so the
        # policy lives in one place: the form used to carry its own tier list
        # (['premium','admin']) while _AUTO_ENRICH_TIERS was empty, and the two
        # drifted — the button vanished for four of six accounts to protect a
        # feature that is not sold. Note /enrich-recipe itself only requires
        # own_recipes, which every role holds, so hiding the button never gated
        # anything; it only decided what our own UI does.
        "enrich_available": _enrich_available(user),
        # True when this account HAS a staff role but hasn't presented the
        # curator password, so `role` above has been locked down to 'member'.
        # The UI can offer an unlock prompt rather than pretending the account
        # was never staff.
        "staff_locked": bool(user.get("staff_locked")),
        "actual_role": user.get("actual_role") or role,
    }


@app.post("/auth/login")
async def auth_login(request: Request):
    """Exchange {user_id|email, password} for a session token.

    The token is returned and sent back as X-Session-Token. It is bound to the
    uid it was minted for, so it cannot be replayed as another account."""
    ip = (request.client.host if request.client else "?") or "?"
    if _master_throttled(ip):          # same per-IP ceiling as the curator login
        raise HTTPException(status_code=429,
                            detail="Too many attempts. Wait 15 minutes and try again.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    payload = payload or {}
    password = payload.get("password") or ""
    with _db() as conn:
        auth_lib.ensure_password_column(conn)
        uid = payload.get("user_id")
        if uid is None and payload.get("email"):
            row = conn.execute("SELECT user_id FROM users WHERE lower(email) = lower(?)",
                               (payload["email"],)).fetchone()
            uid = row[0] if row else None
        if uid is None:
            raise HTTPException(status_code=400, detail="user_id or email required.")
        uid = int(uid)
        stored = auth_lib.user_password_hash(conn, uid)
    if not stored:
        raise HTTPException(
            status_code=409,
            detail="This account doesn't have a password yet, so there is nothing "
                   "to sign in with. Accounts created before passwords existed "
                   "still work from the user picker — sign in there and set one "
                   "on your own record.")
    if not auth_lib.verify_password(password, stored):
        _MASTER_FAILS.setdefault(ip, []).append(time.time())
        print(f"[AUTH] login FAILED for user {uid} from {ip}")
        raise HTTPException(status_code=401, detail="Incorrect password.")
    token = auth_lib.mint_user_token(uid)
    if not token:
        raise HTTPException(status_code=503, detail="Token secret unavailable — "
                                                    "run set_master_password.py.")
    _MASTER_FAILS.pop(ip, None)
    print(f"[AUTH] login OK user {uid} from {ip}")
    return {"user_id": uid, "token": token, "expires_in": auth_lib.USER_TOKEN_TTL}


@app.get("/admin/home-summary")
def admin_home_summary(request: Request):
    """The admin home's alert tiles. Staff only.

    Deliberately the four things that had to be dug out by hand during the
    2026-07-29/30 session: whether the backup actually verified (one was silently
    truncated), which jobs failed, which accounts are still spoofable because
    they have no password, and what the month costs. Each returns a `level` so
    the page can rank by what is wrong rather than by section order."""
    _require_perm(request, "admin_ui")
    out = {}

    with _db() as conn:
        auth_lib.ensure_password_column(conn)

        # --- jobs -----------------------------------------------------------
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM jobs "
            "WHERE created_at >= datetime('now','-7 day') GROUP BY status").fetchall()
        counts = {r[0]: r[1] for r in rows}
        fail = conn.execute(
            "SELECT id, type, finished_at, error_detail FROM jobs "
            "WHERE status NOT IN ('success','running','queued') "
            "ORDER BY id DESC LIMIT 1").fetchone()
        out["jobs"] = {
            "counts_7d": counts,
            "failed_7d": sum(v for k, v in counts.items()
                             if k not in ("success", "running", "queued")),
            "running": counts.get("running", 0),
            "last_failure": ({"id": fail[0], "type": fail[1], "at": fail[2],
                              "error": (fail[3] or "")[:200]} if fail else None),
            "level": "warn" if any(k not in ("success", "running", "queued")
                                   for k in counts) else "ok",
        }

        # --- accounts without a password (still header-spoofable) ------------
        open_accts = conn.execute(
            "SELECT user_id, name FROM users WHERE password_hash IS NULL "
            "ORDER BY user_id").fetchall()
        out["accounts"] = {
            "total": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "without_password": [{"user_id": r[0], "name": r[1]} for r in open_accts],
            "level": "warn" if open_accts else "ok",
        }

        # --- spend, 30d ------------------------------------------------------
        spend = conn.execute(
            "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COUNT(*) "
            "FROM bcc_token_journal WHERE created_at >= datetime('now','-30 day')").fetchone()
        out["spend_30d"] = {
            "input_tokens": spend[0], "output_tokens": spend[1], "calls": spend[2],
            # Sonnet-class blended estimate — indicative, not an invoice.
            "est_usd": round(spend[0] / 1e6 * 3 + spend[1] / 1e6 * 15, 2),
            "level": "ok",
        }

    # --- backup: did the last run actually verify? ---------------------------
    # backup.log is the record; a truncated dump once looked exactly like a good
    # one, so report the dump's real size and mtime rather than trusting "ran".
    backup = {"level": "warn", "last_line": None, "dump_bytes": None, "dump_mtime": None}
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backup.log")
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                tail = [ln.strip() for ln in f.readlines()[-12:] if ln.strip()]
            backup["last_line"] = tail[-1] if tail else None
            backup["level"] = "ok" if any("exit code: 0" in ln for ln in tail[-3:]) else "warn"
        dump = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recipes.sql.gz")
        if os.path.exists(dump):
            backup["dump_bytes"] = os.path.getsize(dump)
            backup["dump_mtime"] = datetime.fromtimestamp(
                os.path.getmtime(dump), timezone.utc).isoformat()
    except Exception as e:
        backup["error"] = str(e)
    out["backup"] = backup

    return out


@app.post("/auth/signup")
async def auth_signup(request: Request):
    """Public self-signup: {email, password, name?} -> a member account + token.

    DELIBERATELY SEPARATE FROM POST /users. That one is the admin create and
    accepts `role`, because staff legitimately need to make an editor or an
    author. A public endpoint must be *structurally* unable to do that — not
    "validates the role", but has no role parameter at all. Same for status and
    subscription_tier: sent, they are ignored.

    New accounts are role='member', status='free'. Email is NOT verified yet —
    there is no mail infrastructure — so an address here is claimed, not proven.
    Worth wiring before the site is promoted."""
    ip = (request.client.host if request.client else "?") or "?"
    if _master_throttled(ip):
        raise HTTPException(status_code=429,
                            detail="Too many attempts. Wait 15 minutes and try again.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    payload = payload or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    name = (payload.get("name") or "").strip()

    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="A valid email address is required.")
    if len(password) < 8:
        raise HTTPException(status_code=400,
                            detail="Password must be at least 8 characters.")
    if not name:
        name = email.split("@")[0].replace(".", " ").replace("_", " ").title()

    now = datetime.now(timezone.utc).isoformat()
    with _db() as conn:
        auth_lib.ensure_password_column(conn)
        auth_lib.ensure_api_key_columns(conn)
        if conn.execute("SELECT 1 FROM users WHERE lower(email) = ?", (email,)).fetchone():
            # Same wording whether or not the address exists would be better for
            # privacy, but a signup form that silently does nothing is worse —
            # and the address is one the person just typed as their own.
            raise HTTPException(status_code=409,
                                detail="An account with that email already exists. Sign in instead.")
        # subscription_tier is set EXPLICITLY. Leaving it out wrote NULL, and a
        # NULL tier is not the same thing as the Free tier — it is "we never
        # decided", which then reads as no-entitlement everywhere by accident
        # rather than by policy ([[absent is not zero]]). Every self-signup is a
        # Free member; say so in the row.
        cur = conn.execute(
            "INSERT INTO users (email, name, status, subscription_tier, role, "
            "                   created_at, updated_at) "
            "VALUES (?, ?, 'free', 'Free', 'member', ?, ?)", (email, name, now, now))
        uid = cur.lastrowid
        auth_lib.set_user_password(conn, uid, password)   # commits
        auth_lib.ensure_email_verification_columns(conn)
    token = auth_lib.mint_user_token(uid)
    _MASTER_FAILS.pop(ip, None)
    print(f"[AUTH] signup: user {uid} <{email}> from {ip}")

    # Send the confirmation, but do NOT fail the signup if mail is down. The
    # account exists and the person is already signed in; a mail outage should
    # cost them a confirmation they can request again, not the account they just
    # made. The result is reported so the UI can say "check your email" or
    # "we couldn't send that — try again later" honestly.
    from starlette.concurrency import run_in_threadpool
    sent = await run_in_threadpool(_send_verification_email, uid, email, name)
    if not sent.get("ok"):
        print(f"[AUTH] signup verification mail FAILED for {uid}: {sent.get('error')}")

    return {"user_id": uid, "email": email, "name": name, "role": "member",
            "token": token, "expires_in": auth_lib.USER_TOKEN_TTL,
            "verification_sent": bool(sent.get("ok"))}


# === Email verification ======================================================
# The address on a signup is CLAIMED, not proven. Verification is what turns it
# into something we can send a password reset to — which is why it comes first:
# a reset link mailed to an unproven address is an account takeover with extra
# steps.
#
# Not enforced anywhere yet, on purpose. See auth_lib.ensure_email_verification_
# columns: six real accounts predate mail entirely and a flag day would lock
# them out of a public site. Record first, enforce later, one surface at a time.

def _send_verification_email(user_id: int, email: str, name: str) -> dict:
    """Mint a token bound to THIS address and mail the link. Sync — callers in
    the request path must wrap it in run_in_threadpool."""
    token = auth_lib.mint_purpose_token("verify", user_id, email.strip().lower(),
                                        auth_lib.VERIFY_TOKEN_TTL)
    if not token:
        return {"ok": False, "error": "No token secret configured "
                                      "(BCC_MASTER_TOKEN_SECRET)."}
    subject, text, html = mail_messages.verification(name, token)
    return mailer.send_mail(email, subject, text, html=html,
                            stream=mailer.TRANSACTIONAL)


@app.post("/auth/send-verification")
async def auth_send_verification(request: Request):
    """(Re)send the confirmation link for an account's own address.

    Self-service or manage_users — the same rule as setting a password. Rate
    limited on the shared per-IP ceiling, because an unauthenticated-feeling
    endpoint that sends mail is a way to use us to spam a third party."""
    ip = (request.client.host if request.client else "?") or "?"
    if _master_throttled(ip):
        raise HTTPException(status_code=429,
                            detail="Too many attempts. Wait 15 minutes and try again.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    uid = (payload or {}).get("user_id")
    if uid is None:
        raise HTTPException(status_code=400, detail="user_id required.")
    uid = int(uid)
    _require_self_or_perm(request, uid, "manage_users")

    with _db() as conn:
        auth_lib.ensure_email_verification_columns(conn)
        row = conn.execute(
            "SELECT email, name, COALESCE(email_verified, 0) FROM users WHERE user_id = ?",
            (uid,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"No user {uid}.")
    email, name, verified = row[0], row[1], bool(row[2])
    if not email:
        raise HTTPException(status_code=400, detail="That account has no email address.")
    if verified:
        return {"user_id": uid, "sent": False, "already_verified": True}

    from starlette.concurrency import run_in_threadpool
    result = await run_in_threadpool(_send_verification_email, uid, email, name)
    if not result.get("ok"):
        # Surfaced, not swallowed: someone is waiting on this mail, and a silent
        # zero reads as "sent" to every caller upstream.
        raise HTTPException(status_code=502,
                            detail=f"Could not send the email: {result.get('error')}")
    return {"user_id": uid, "sent": True}


@app.get("/auth/verify")
def auth_verify(request: Request, token: str = ""):
    """Redeem a verification link. GET because it is clicked in a mail client.

    Returns a PAGE, not JSON — the only thing that ever calls this is a browser
    following a link out of an email."""
    def page(title: str, message: str, ok: bool) -> HTMLResponse:
        colour = "#2f7a3a" if ok else "#a3382b"
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html_escape(title)}</title>"
            "<div style=\"font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
            "max-width:32rem;margin:14vh auto;padding:0 20px;line-height:1.55;color:#2a211b\">"
            f"<h1 style='font-size:1.4rem;color:{colour};margin:0 0 12px'>{html_escape(title)}</h1>"
            f"<p>{html_escape(message)}</p>"
            "<p style='margin-top:26px'><a href='/' "
            "style=\"background:#b8602a;color:#fff;text-decoration:none;padding:11px 20px;"
            "border-radius:8px;display:inline-block\">Go to Best Cooks Club</a></p></div>",
            status_code=200 if ok else 400)

    parsed = auth_lib.read_purpose_token(token)
    if not parsed:
        # One message for malformed AND expired: a link that tells a stranger
        # whether a token was ever real is a link that helps them guess.
        return page("That link has expired",
                    "Verification links last 24 hours. Sign in and ask for a new "
                    "one — it takes a moment.", False)
    uid, _exp = parsed
    with _db() as conn:
        auth_lib.ensure_email_verification_columns(conn)
        row = conn.execute(
            "SELECT email, COALESCE(email_verified, 0) FROM users WHERE user_id = ?",
            (uid,)).fetchone()
        if not row or not row[0]:
            return page("That link has expired",
                        "Verification links last 24 hours. Sign in and ask for a "
                        "new one — it takes a moment.", False)
        email, already = row[0], bool(row[1])
        # Signed over the address CURRENTLY on the row, so a token minted for a
        # previous address stops working the moment the address changes.
        if not auth_lib.verify_purpose_token(token, "verify", uid,
                                             email.strip().lower()):
            return page("That link has expired",
                        "Verification links last 24 hours. Sign in and ask for a "
                        "new one — it takes a moment.", False)
        if already:
            return page("Already confirmed",
                        f"{email} was confirmed earlier. Nothing else to do.", True)
        auth_lib.mark_email_verified(conn, uid)
    print(f"[AUTH] email verified for user {uid} <{email}>")
    return page("Email confirmed",
                f"Thanks — {email} is confirmed.", True)


@app.post("/users/{user_id}/password")
async def set_user_password_endpoint(request: Request, user_id: int):
    """Set (or change) this user's password. Self-service, or manage_users for
    anyone else. Setting a password HARDENS the account: from then on the
    X-Self-User-Id header alone stops being accepted for it."""
    _require_self_or_perm(request, user_id, "manage_users")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    password = (payload or {}).get("password") or ""
    if len(password) < 8:
        raise HTTPException(status_code=400,
                            detail="Password must be at least 8 characters.")
    with _db() as conn:
        if not auth_lib.set_user_password(conn, user_id, password):
            raise HTTPException(status_code=404, detail=f"No user {user_id}.")
    print(f"[AUTH] password set for user {user_id}")
    return {"user_id": user_id, "password_set": True,
            "note": "This account now requires a login token; the header alone "
                    "is no longer accepted for it."}


@app.get("/users/{user_id}/api-keys")
def list_user_api_keys(request: Request, user_id: int):
    """This user's device keys — metadata only, never a hash."""
    _require_self_or_perm(request, user_id, "manage_users")
    with _db() as conn:
        return {"user_id": user_id, "keys": auth_lib.list_api_keys(conn, user_id)}


@app.post("/users/{user_id}/api-keys")
async def create_user_api_key(request: Request, user_id: int):
    """Mint a key for ONE device. Body: {label}.

    Other devices are untouched — that is the whole reason this is a table
    rather than a column. Returned in full exactly once; only the hash is kept,
    so a lost key is replaced rather than recovered.

    Self-service: a customer opens their own record and adds their phone.
    Touching anyone else's needs manage_users."""
    _require_self_or_perm(request, user_id, "manage_users")
    if user_id == 0:
        raise HTTPException(
            status_code=400,
            detail="Master (user 0) has no bookmarklet key — it authenticates "
                   "with the curator password instead.")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    label = ((payload or {}).get("label") or "").strip()
    with _db() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"No user {user_id}.")
        plain = auth_lib.create_api_key(conn, user_id, label)
        keys = auth_lib.list_api_keys(conn, user_id)
    print(f"[AUTH] bookmarklet key minted for user {user_id} ({label or 'unnamed'})")
    return {"user_id": user_id, "api_key": plain, "label": label, "keys": keys,
            "note": "Shown once. Install it now; it cannot be retrieved again."}


@app.delete("/users/{user_id}/api-keys/{key_id}")
def revoke_user_api_key(request: Request, user_id: int, key_id: int):
    """Revoke ONE device. The others keep working — this is how you kill the key
    on a phone you no longer have without re-installing everywhere else."""
    _require_self_or_perm(request, user_id, "manage_users")
    with _db() as conn:
        if not auth_lib.revoke_api_key(conn, user_id, key_id):
            raise HTTPException(status_code=404, detail="No such key for this user.")
        keys = auth_lib.list_api_keys(conn, user_id)
    print(f"[AUTH] bookmarklet key {key_id} revoked for user {user_id}")
    return {"user_id": user_id, "revoked": key_id, "keys": keys}


@app.post("/users")
async def create_user(request: Request):
    """Create a test user. Body: {name, email?, status?, subscription_tier?}.
    user_id is auto-assigned by SQLite (AUTOINCREMENT). Returns the full
    row including the assigned user_id so the picker UI can navigate the
    user straight to the form as that user. Email uniqueness is enforced
    by a partial index — duplicate email returns 409.

    STAFF ONLY. This is the ADMIN create — it accepts `role` from the payload,
    so an open version lets a caller mint themselves an owner. It was reachable
    unauthenticated until 2026-07-30; the host gate kept it off the customer
    domain, but nothing guarded it on the admin host.

    Public self-signup must NOT reuse this. It needs its own endpoint that
    forces role='member' and cannot be talked into anything else."""
    _require_perm(request, "manage_users")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}") from e
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    email = (payload.get("email") or "").strip() or None
    status = (payload.get("status") or "test").strip()
    tier = (payload.get("subscription_tier") or "").strip() or None
    # Role defaults to 'member' (the vast majority of accounts). Caller
    # supplies a staff role explicitly. Validated against the allowed set.
    role = (payload.get("role") or "member").strip().lower()
    if role not in auth_lib.ROLE_PERMISSIONS:
        raise HTTPException(status_code=400,
                            detail=f"invalid role {role!r}; allowed: "
                                   f"{sorted(auth_lib.ROLE_PERMISSIONS.keys())}")
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _db() as conn:
            cur = conn.execute(
                "INSERT INTO users (email, name, status, subscription_tier, role, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, name, status, tier, role, now, now),
            )
            user_id = cur.lastrowid
        return {
            "user_id": user_id,
            "email": email,
            "name": name,
            "status": status,
            "subscription_tier": tier,
            "role": role,
            "created_at": now,
            "updated_at": now,
        }
    except sqlite3.IntegrityError as e:
        # uniq_users_email collision is the only expected IntegrityError
        # here — surface as 409 so the UI can show a useful message.
        raise HTTPException(status_code=409, detail=f"User already exists: {e}") from e
    except Exception as e:
        print(f"[ERROR] create_user failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.patch("/users/{user_id}")
async def update_user(user_id: int, request: Request):
    """Partial-update a user. Body: any subset of {name, email, status,
    subscription_tier, ghost_uuid}. user_id is NOT mutable (it's our
    surrogate key; every recipes.user_id row out there references it).
    Empty string for email/tier → NULL in the DB. 409 on email/ghost_uuid
    collision."""
    if user_id == 0:
        raise HTTPException(status_code=403,
                            detail="user_id 0 is reserved for master_recipes")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}") from e

    allowed = {"name", "email", "status", "subscription_tier", "ghost_uuid", "role"}
    sets = []
    params: list = []
    for k in allowed:
        if k not in payload:
            continue
        v = payload[k]
        if isinstance(v, str):
            v = v.strip()
            if v == "" and k in ("email", "subscription_tier", "ghost_uuid"):
                v = None
        if k == "name" and (v is None or v == ""):
            raise HTTPException(status_code=400, detail="name cannot be empty")
        if k == "role":
            role_norm = (v or "member").lower()
            if role_norm not in auth_lib.ROLE_PERMISSIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid role {role_norm!r}; allowed: "
                           f"{sorted(auth_lib.ROLE_PERMISSIONS.keys())}",
                )
            v = role_norm
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        raise HTTPException(status_code=400, detail="no updatable fields in body")
    now = datetime.now(timezone.utc).isoformat()
    sets.append("updated_at = ?")
    params.append(now)
    params.append(user_id)
    try:
        with _db() as conn:
            cur = conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE user_id = ?",
                params,
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="User not found")
            row = conn.execute(
                "SELECT user_id, ghost_uuid, email, name, status, "
                "subscription_tier, role, created_at, updated_at "
                "FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return {
            "user_id": row[0], "ghost_uuid": row[1], "email": row[2],
            "name": row[3], "status": row[4], "subscription_tier": row[5],
            "role": row[6] or "member",
            "created_at": row[7], "updated_at": row[8],
        }
    except HTTPException:
        raise
    except sqlite3.IntegrityError as e:
        raise HTTPException(status_code=409, detail=f"Conflict: {e}") from e
    except Exception as e:
        print(f"[ERROR] update_user({user_id}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.delete("/users/{user_id}")
def delete_user(user_id: int):
    """Refuse to delete a user that still owns recipes — orphans break
    referential expectations elsewhere (token journal, claim provenance,
    sidebar lookups). Caller must reassign or delete those recipes first.
    user_id 0 is master, never deletable from here."""
    if user_id == 0:
        raise HTTPException(status_code=403,
                            detail="user_id 0 is reserved for master_recipes")
    try:
        with _db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM recipes WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            if count > 0:
                raise HTTPException(
                    status_code=409,
                    detail=f"User has {count} recipe(s) — delete or reassign them first",
                )
            cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="User not found")
        return {"deleted": True, "user_id": user_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] delete_user({user_id}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


# === Dishes (the dish library) ===
# A dish is the unit of curated top-recipe collection. Each row maps a
# canonical dish name to a set of SerpAPI queries + tuning + refresh
# metadata. The dish name is the IMMUTABLE primary key — every
# master_recipes row from a batch refresh will stamp _master.dish with
# this name (#3 in the implementation plan; not wired yet). See
# memory/project_dish_library.md for the full design.
#
# Endpoints:
#   GET    /dishes              list all
#   POST   /dishes              create (name + queries required)
#   GET    /dishes/{name}       fetch one
#   PATCH  /dishes/{name}       update (NOT name — that's the join key)
#   DELETE /dishes/{name}       delete (cascade-to-master added in #3)
#
# The /dishes/{name}/refresh endpoint lives separately (next implementation
# step) — it imports build_query_batch in-process to do the actual work.


@app.get("/dish-coverage")
def dish_coverage_endpoint(request: Request, min_recipes: int = 2):
    """Dishes the CORPUS holds that the CATALOG has no record of.

    `_match.dish` is a CLOSED set — it is a KNN over dishes_vec, built from the
    dishes table, so it can only ever return a dish that already exists, and the
    nearest neighbour always exists. `_identity.likelyDish` is the opposite:
    free LLM text written at extraction with no knowledge of the catalog. The
    gap between the two is the coverage gap, and this endpoint is that gap.

    Each row also carries `alias_of` — the existing dish sharing most of its
    words. Sometimes that is only a naming variant (Mac and Cheese against
    Macaroni and Cheese), but often it is the generic-bucket problem, where a
    broad dish is absorbing a specific one (Cobbler holding peach cobblers).
    Those need reading, not bulk action, so it is reported, never applied.

    Curator surface: it names the corpus's own gaps and joins keyword demand.
    """
    _require_perm(request, "admin_ui")
    import re as _re
    STOP = {"and", "the", "with", "a", "of", "in", "recipe", "recipes", "style"}

    def _toks(t):
        return {w for w in _re.findall(r"[a-z]+", (t or "").lower())
                if w not in STOP and len(w) > 2}

    with _db() as conn:
        have = {r[0] for r in conn.execute("SELECT name FROM dishes")}
        have_l = {n.strip().lower() for n in have}
        counts: dict = {}
        chapters: dict = {}
        # Indexed by idx_mr_likelydish.
        for ld, ch in conn.execute(
                "SELECT json_extract(data,'$._identity.likelyDish'), "
                "       json_extract(data,'$.classification.chapter') "
                "  FROM master_recipes "
                " WHERE json_extract(data,'$._identity.likelyDish') IS NOT NULL"):
            k = (ld or "").strip()
            if not k or k.lower() in have_l:
                continue
            counts[k] = counts.get(k, 0) + 1
            if ch:
                chapters.setdefault(k, {})
                chapters[k][ch] = chapters[k].get(ch, 0) + 1

        dish_toks = [(d, _toks(d)) for d in have]
        out = []
        for name, n in counts.items():
            if n < max(1, min_recipes):
                continue
            like = f"%{name.lower()}%"
            traffic = conn.execute(
                "SELECT COALESCE(SUM(traffic),0) FROM dish_keywords "
                " WHERE lower(keyword) LIKE ?", (like,)).fetchone()[0]
            top = conn.execute(
                "SELECT keyword FROM dish_keywords WHERE lower(keyword) LIKE ? "
                " ORDER BY traffic DESC LIMIT 1", (like,)).fetchone()
            t = _toks(name)
            best, score = None, 0.0
            for d, dt in dish_toks:
                if not (t and dt):
                    continue
                j = len(t & dt) / len(t | dt)
                if j > score:
                    score, best = j, d
            ch_map = chapters.get(name) or {}
            out.append({
                "dish": name,
                "recipes": n,
                "traffic": int(traffic or 0),
                "top_keyword": top[0] if top else None,
                "chapter": max(ch_map, key=ch_map.get) if ch_map else None,
                # 0.34 keeps one shared significant word out of it while still
                # catching Cobbler/Peach Cobbler.
                "alias_of": best if score >= 0.34 else None,
                "alias_score": round(score, 2),
            })
    out.sort(key=lambda r: (-r["traffic"], -r["recipes"]))
    return {
        "rows": out,
        "uncovered_names": len(counts),
        "catalog_size": len(have),
        "min_recipes": min_recipes,
    }


@app.get("/dishes")
def list_dishes_endpoint():
    try:
        with _db() as conn:
            dishes = dishes_lib.list_dishes(conn)
            # Card image DERIVED from each dish's top recipe (recipe table), not a
            # stored column — Phase 0 of docs/recipe-table-backed-lists.md.
            imgs = dishes_lib.representative_images(conn)
            for d in dishes:
                d["preview_image"] = imgs.get(d.get("name")) or None
            return dishes
    except Exception as e:
        print(f"[ERROR] list_dishes failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/dishes/suggestions")
def suggested_dishes_endpoint(min_count: int = 3):
    """Suggested new dishes — clusters of carded recipes whose
    `_identity.likelyDish` doesn't match any existing dish row.

    Returns the LLM's canonical-dish phrase ranked by how many recipes
    are waiting on it. Pass `min_count=N` to override the threshold
    (default 3 — keeps idiosyncratic LLM outputs out).

    Each entry:
      {
        suggested: "Spaghetti and Meatballs",
        waiting:   10,
        chapters:  ["Pasta & Noodles"],
        cuisines:  ["Italian-American", "American"],
        example_recipe_ids: [42, 81, 117],  # first few, for curator preview
      }

    No persistence — query runs on every call. At 300-row scale this
    is microseconds. If the table grows past ~50K rows and the query
    starts costing real time, materialize after each dish refresh job
    completes (the timing the user suggested 2026-05-28).
    """
    try:
        with _db() as conn:
            sql = """
                WITH carded AS (
                    SELECT
                        json_extract(data, '$._identity.likelyDish') AS suggested,
                        json_extract(data, '$.classification.chapter') AS chapter,
                        json_extract(data, '$._identity.cuisine')      AS cuisine,
                        id
                    FROM master_recipes
                    WHERE json_extract(data, '$._identity.likelyDish') IS NOT NULL
                    UNION ALL
                    SELECT
                        json_extract(data, '$._identity.likelyDish'),
                        json_extract(data, '$.classification.chapter'),
                        json_extract(data, '$._identity.cuisine'),
                        id
                    FROM recipes
                    WHERE json_extract(data, '$._identity.likelyDish') IS NOT NULL
                )
                SELECT
                    suggested,
                    COUNT(*) AS waiting,
                    GROUP_CONCAT(DISTINCT chapter) AS chapters,
                    GROUP_CONCAT(DISTINCT cuisine) AS cuisines,
                    GROUP_CONCAT(id) AS example_ids
                FROM carded
                WHERE LOWER(suggested) NOT IN (
                    SELECT LOWER(name) FROM dishes
                )
                GROUP BY suggested
                HAVING waiting >= ?
                ORDER BY waiting DESC, suggested
            """
            rows = conn.execute(sql, (min_count,)).fetchall()
            out = []
            for suggested, waiting, chapters, cuisines, example_ids in rows:
                ids = [int(x) for x in (example_ids or "").split(",") if x.strip()][:5]
                out.append({
                    "suggested": suggested,
                    "waiting": int(waiting),
                    "chapters": [c for c in (chapters or "").split(",") if c],
                    "cuisines": [c for c in (cuisines or "").split(",") if c],
                    "example_recipe_ids": ids,
                })
            return out
    except Exception as e:
        print(f"[ERROR] suggested_dishes failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/dishes/{name}")
def get_dish_endpoint(name: str):
    try:
        with _db() as conn:
            d = dishes_lib.get_dish(conn, name)
            if d is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            return d
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] get_dish({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/dishes")
async def create_dish_endpoint(request: Request):
    _require_perm(request, "manage_dishes")
    """Create a new dish. Body:
        {
          "name": "Spaghetti and Meat Sauce",       (required, unique, immutable)
          "queries": ["spaghetti with meat sauce",  (required, non-empty)
                      "spaghetti and meat sauce"],
          "top_n_serpapi": 25,                       (optional, default 25)
          "top_n_final": 10,                         (optional, default 10)
          "refresh_ttl_days": 30,                    (optional; null = manual-only)
          "notes": "..."                             (optional)
        }
    """
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}") from e
    try:
        name, queries, top_serp, top_final, ttl, notes, auto_enrich, description = \
            dishes_lib.validate_create_payload(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        with _db() as conn:
            created = dishes_lib.create_dish(
                conn,
                name=name, queries=queries,
                top_n_serpapi=top_serp, top_n_final=top_final,
                refresh_ttl_days=ttl, notes=notes,
                auto_enrich=auto_enrich,
                description=description,
            )
            # create_dish now auto-describes + embeds inside the write itself,
            # so every caller gets it and not just this endpoint. Re-read so the
            # response reflects the auto-filled description + chapter.
            created = dishes_lib.get_dish(conn, name) or created
            return created
    except sqlite3.IntegrityError:
        # PRIMARY KEY COLLATE NOCASE — duplicate (case-insensitive) name.
        # `from None`: the handler is already specific to this one integrity
        # error, so the chained IntegrityError restates what the comment says.
        raise HTTPException(status_code=409,
                            detail=f"Dish {name!r} already exists") from None
    except Exception as e:
        print(f"[ERROR] create_dish failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.patch("/dishes/{name}")
async def update_dish_endpoint(name: str, request: Request):
    _require_perm(request, "manage_dishes")
    """Partial update. Body may include any subset of {queries,
    top_n_serpapi, top_n_final, refresh_ttl_days, notes}. The name
    field is intentionally not updatable — it's the join key into
    master_recipes._master.dish; renaming would orphan recipe rows.
    To rename, delete + recreate."""
    try:
        patch = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}") from e
    if "name" in patch:
        raise HTTPException(
            status_code=400,
            detail="Dish name is immutable (join key into master_recipes). "
                   "Delete + recreate to rename.",
        )
    try:
        with _db() as conn:
            try:
                updated = dishes_lib.update_dish(conn, name, patch)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            if updated is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            # update_dish re-embeds inside the write, so an edit made by a
            # script or a job cannot leave the vector describing old queries.
            # Re-read to pick up anything that refresh auto-filled.
            updated = dishes_lib.get_dish(conn, name) or updated
            return updated
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] update_dish({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.patch("/dishes/{name}/rejects/{reject_id}")
async def update_dish_reject_status(name: str, reject_id: int, request: Request):
    """Update a reject's user-status + notes. Body:
        {status: 'new'|'recovered'|'skipped'|'unreachable', notes?: str}

    Staff-only (manage_dishes) since user marks affect what surfaces
    on subsequent refreshes. 'name' in the path is the dish name; the
    reject_id selects the specific row."""
    _require_perm(request, "manage_dishes")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}") from e
    status = (payload.get("status") or "").strip().lower()
    notes_raw = payload.get("notes")
    notes = notes_raw.strip() if isinstance(notes_raw, str) else None
    if notes == "":
        notes = None
    try:
        with _db() as conn:
            updated = dishes_lib.update_reject_status(
                conn, reject_id, status=status, notes=notes,
            )
            if updated is None:
                raise HTTPException(status_code=404, detail="Reject not found")
            # Defensive: confirm the reject belongs to the named dish
            # (caller might have constructed a URL with a mismatched
            # name; the unique key is the id, but the dish_name in the
            # URL should match for sanity).
            if (updated.get("dish_name") or "").lower() != name.lower():
                raise HTTPException(
                    status_code=404,
                    detail=f"Reject {reject_id} is not under dish {name!r}",
                )
            return updated
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] update_dish_reject_status({name!r},{reject_id}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/extract-product")
async def extract_product_endpoint(request: Request):
    """Mine one retailer product page's staged markdown into a Product, AND return everything
    the product form needs in one shot: the extracted `product`, the reverse-table ATK `facts`
    for its URL/ASIN, and existing-product `matches` (for the 'add as a vendor to X?' prompt)."""
    # GATED 2026-07-30. Was open to any caller — a customer bookmarklet key,
    # or no credential at all, could drive it. Curator surface: the product
    # and review grabbers are the ADMIN bookmarklets. Which bookmarklet can do
    # what now falls out of the owner's permissions rather than needing a
    # separate scope system on the key.
    _require_perm(request, "edit_master")
    from extract.markdown_to_product import markdown_to_product
    from intake.products import catalog_store, review_facts
    body = await request.json()
    md = (body.get("markdown") or "").strip()
    source_url = body.get("source_url") or ""
    title = body.get("title") or ""
    if not md:
        raise HTTPException(status_code=400, detail="markdown required")
    product = markdown_to_product(md, source_url=source_url, title=title)
    if product is None:
        raise HTTPException(status_code=422, detail="could not extract a product from this page")
    offers = product.get("retailer_offers") or []
    asin = (offers[0].get("asin") if offers else "") or ""
    try:
        with _db() as conn:
            facts = review_facts.lookup(conn, asin=asin, url=source_url)
            matches = catalog_store.find_matches(conn, product)
    except Exception as e:
        print(f"[EXTRACT-PRODUCT] facts/matches lookup failed: {e}")
        facts, matches = [], {"exact": None, "candidates": []}
    return {"product": product, "facts": facts, "matches": matches}


@app.post("/products")
async def save_product_endpoint(request: Request):
    """Match-or-create save. Body: {product, merge_into?}. `merge_into` (a product_id) appends
    this vendor's offer to that product; omit it to auto-merge on the manufacturer key or create."""
    from intake.products import catalog_store
    body = await request.json()
    product = body.get("product")
    if not isinstance(product, dict):
        raise HTTPException(status_code=400, detail="product object required")
    try:
        with _db() as conn:
            return catalog_store.save_product(conn, product, merge_into=body.get("merge_into"))
    except Exception as e:
        print(f"[SAVE-PRODUCT] failed: {e}")
        raise HTTPException(status_code=500, detail=f"Save error: {e}") from e


@app.post("/product-fact-voice")
async def product_fact_voice_endpoint(request: Request):
    """Paraphrase one review fact into BCC's voice (brand-safe attribution) for the form."""
    from intake.products import review_facts
    body = await request.json()
    return {"text": review_facts.our_voice(body.get("fact") or {})}


@app.get("/product-facts")
def product_facts_endpoint(url: str = "", asin: str = ""):
    """Reverse-table lookup: ATK verdict facts for a product URL/ASIN (used by the form)."""
    from intake.products import review_facts
    with _db() as conn:
        return {"facts": review_facts.lookup(conn, asin=asin, url=url)}


@app.get("/product-catalog")
def product_catalog_endpoint():
    """Read-only product catalog (categories -> classes -> ranked products) for the demo
    viewer at /forms/products.html. Reads the existing product_categories/classes/products
    tables (the ATK-extracted catalog)."""
    from intake.products import catalog_store
    try:
        with _db() as conn:
            return catalog_store.list_catalog(conn)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Catalog error: {e}") from e


@app.get("/products/list")
def products_list_endpoint():
    """Flat admin roster for the product ACDV editor (forms/products.html): every product +
    the distinct classes/categories present (used as datalist sources for the edit form).
    Separate from /product-catalog (the read-only category->class tree). See
    intake/products/catalog_store.list_products."""
    from intake.products import catalog_store
    with _db() as conn:
        products = catalog_store.list_products(conn)
        classes = [{"name": n, "category": c} for n, c in catalog_store.distinct_classes(conn)]
    categories = sorted({c["category"] for c in classes if c["category"]})
    return {"products": products, "classes": classes, "categories": categories}


@app.get("/products/{product_id}")
def product_get_endpoint(product_id: str):
    """One product's full data blob for the editor detail pane."""
    from intake.products import catalog_store
    with _db() as conn:
        p = catalog_store.get_product(conn, product_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    return p


@app.put("/products/{product_id}")
async def product_update_endpoint(product_id: str, request: Request):
    """Curator in-place edit of one product (merges a partial patch into the stored blob,
    re-normalizes + re-embeds, keeps columns + vec index in lockstep)."""
    from intake.products import catalog_store
    patch = await request.json()
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="patch object required")
    with _db() as conn:
        p = catalog_store.update_product(conn, product_id, patch)
    if p is None:
        raise HTTPException(status_code=404, detail="Product not found.")
    return p


@app.delete("/products/{product_id}")
def product_delete_endpoint(product_id: str):
    """Delete one product. Loads sqlite-vec first so the AFTER DELETE trigger can clean
    products_vec (project_vec_delete_triggers)."""
    from intake.products import catalog_store
    with _db() as conn:
        _enable_vec_for_delete(conn)   # trg keeps products_vec (vec0) clean on delete
        ok = catalog_store.delete_product(conn, product_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Product not found.")
    return {"deleted": product_id}


@app.post("/products/{product_id}/realrank")
def product_realrank_endpoint(product_id: str, payload: dict = Body(default={})):
    """Kick off a RealRank analysis FOR this product and attach the result to its row.

    Enqueues the `realrank_research` job and returns {job_id} immediately — a full run is
    minutes (8 SERP + 8 unblocker fetches + a research call), so the form polls /jobs/{id}
    and tails the log rather than holding a request open. entity_ref locks the product, so
    a second click can't double-run it.

    The search identity comes from the DB ROW (brand + name) and, when we have one, the
    ASIN — never a string typed into the form (§3.1, the --dish rule). A known ASIN also
    lets Rainforest skip its search step.
    """
    from intake.products import catalog_store
    with _db() as conn:
        p = catalog_store.get_product(conn, product_id)
        if p is None:
            raise HTTPException(status_code=404, detail="Product not found.")
        # Most stored names already lead with the brand, so a naive brand + name gave
        # "Le Creuset Le Creuset Enameled Cast Iron..." — a worse search string and a worse
        # output filename. Only prepend when it isn't already there.
        brand = (p.get("brand") or "").strip()
        pname = (p.get("name") or "").strip()
        name = pname if (not brand or pname.lower().startswith(brand.lower())) \
            else f"{brand} {pname}".strip()
        if not name:
            raise HTTPException(status_code=400, detail="Product has no brand/name to research.")
        asin = next((o.get("asin") for o in (p.get("retailer_offers") or [])
                     if (o.get("asin") or "").strip()), "")
        entity_ref = f"product:{product_id}"
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return {"job_id": existing["id"], "status": existing["status"], "already_running": True}
        params = {"product": name, "product_id": product_id}
        if brand:
            params["brand"] = brand
        if asin:
            params["asin"] = asin
        if payload.get("owner_sources"):
            params["owner_sources"] = payload["owner_sources"]
        job_id = jobs_lib.enqueue_job(conn, type="realrank_research", params=params,
                                      entity_ref=entity_ref)
    _spawn_job_runner(job_id)
    return {"job_id": job_id, "status": "queued", "product": name, "asin": asin}


@app.post("/products/{product_id}/curation/approve")
def product_curation_approve_endpoint(product_id: str, payload: dict = Body(default={})):
    """Staff sign-off on the PLACEMENT — where this product came in against its rivals.

    Separate from the RealStory gate on purpose: that approves what we said about the product
    on its own, this approves where we ranked it, and a curator can agree with one and not the
    other. Both reset on a re-run.
    """
    from intake.products import catalog_store
    who = (payload.get("who") or "").strip() or "staff"
    with _db() as conn:
        p = catalog_store.approve_curation(conn, product_id, who)
    if p is None:
        raise HTTPException(status_code=404, detail="Product or curation not found.")
    return {"product_id": product_id, "approved_by": who,
            "approved_at": (p.get("curation") or {}).get("approved_at")}


@app.post("/products/{product_id}/realstory/approve")
def product_realstory_approve_endpoint(product_id: str, payload: dict = Body(default={})):
    """Staff sign-off on the WRITE-UP. The gate between an automated assessment and anything
    that earns affiliate revenue off it. The score half needs no sign-off — it's arithmetic."""
    from intake.products import catalog_store
    who = (payload.get("who") or "").strip() or "staff"
    with _db() as conn:
        p = catalog_store.approve_realstory(conn, product_id, who)
    if p is None:
        raise HTTPException(status_code=404, detail="Product or RealStory assessment not found.")
    return {"product_id": product_id, "approved_by": who,
            "approved_at": (p.get("realstory") or {}).get("approved_at")}


# ---- Product collections ACDV editor (forms/product_collections.html) --------------
# A collection = a NAMED saved Amazon search URL. The curator builds the search by hand in
# Amazon's own UI until the result set is right; the URL carries every criterion, so the URL
# IS the query. A run keeps the WHOLE cohort (winners + also-rans) so selection is auditable.
# See intake/products/collections_store + memory/project_amazon_collection_selection.

@app.get("/product-collections")
def collections_list_endpoint():
    """Roster for the sidebar + the WS taxonomy options the form's category picker uses."""
    from intake.products import collections_store as cst
    with _db() as conn:
        rows = cst.list_collections(conn)
        try:
            cats = [{"id": r[0], "path": r[1]} for r in conn.execute(
                "SELECT id, ws_path FROM ws_categories ORDER BY ws_path")]
        except sqlite3.OperationalError:
            cats = []
    return {"collections": rows, "ws_categories": cats}


@app.get("/product-collections/{name}")
def collection_get_endpoint(name: str, order: str = "wilson"):
    """One collection + its full cohort (NOT just the winners — that's the point)."""
    from intake.products import collections_store as cst
    with _db() as conn:
        c = cst.get_collection(conn, name)
        if c is None:
            raise HTTPException(status_code=404, detail="Collection not found.")
        c["candidates"] = cst.list_candidates(conn, name, order=order)
    return c


@app.post("/product-collections")
async def collection_create_endpoint(request: Request):
    """Create a collection. Auto-classifies it against the WS taxonomy from its name+keyword
    (the same matcher the commerce join uses) unless the curator pinned a category."""
    from intake.products import collections_store as cst, easyparser as ep
    body = await request.json()
    try:
        with _db() as conn:
            c = cst.create_collection(conn, body)
            if not c.get("ws_category_id"):
                params, _ = ep.params_from_url(c["url"])
                term = params.get("keyword") or c["name"]
                try:
                    from intake.products.equipment_match import classify_term
                    hit = classify_term(term, conn)
                    if hit.get("ws_category_id"):
                        c = cst.update_collection(conn, c["name"], {
                            "ws_category_id": hit["ws_category_id"],
                            "ws_path": hit.get("ws_path") or ""})
                except Exception as e:      # best-effort — never block creation
                    print(f"[COLLECTION] taxonomy match skipped: {e}")
        return c
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.put("/product-collections/{name}")
async def collection_update_endpoint(name: str, request: Request):
    from intake.products import collections_store as cst
    body = await request.json()
    with _db() as conn:
        c = cst.update_collection(conn, name, body)
    if c is None:
        raise HTTPException(status_code=404, detail="Collection not found.")
    return c


@app.delete("/product-collections/{name}")
def collection_delete_endpoint(name: str):
    from intake.products import collections_store as cst
    with _db() as conn:
        ok = cst.delete_collection(conn, name)
    if not ok:
        raise HTTPException(status_code=404, detail="Collection not found.")
    return {"deleted": name}


@app.post("/product-collections/{name}/refresh")
def collection_refresh_endpoint(name: str):
    """Run the collection. Entity-locked, out-of-process, tailable — the URL never rides on
    argv (identity only, §3.1); the handler reads it from the DB."""
    from intake.products import collections_store as cst
    with _db() as conn:
        if cst.get_collection(conn, name) is None:
            raise HTTPException(status_code=404, detail="Collection not found.")
        entity_ref = f"collection:{name}"
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return {"job_id": existing["id"], "status": existing["status"],
                    "already_running": True}
        job_id = jobs_lib.enqueue_job(conn, type="collection_refresh",
                                      params={"collection": name}, entity_ref=entity_ref)
    _spawn_job_runner(job_id)
    return {"job_id": job_id, "status": "queued", "stream_url": f"/jobs/{job_id}/stream"}


@app.post("/product-collections/{name}/medal")
async def collection_medal_endpoint(name: str, request: Request):
    """Curator-confirmed gold/silver/bronze on one candidate. Survives a refresh."""
    from intake.products import collections_store as cst
    body = await request.json()
    try:
        with _db() as conn:
            ok = cst.set_medal(conn, name, (body.get("asin") or "").strip(),
                               body.get("medal") or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not ok:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    return {"collection": name, "asin": body.get("asin"), "medal": body.get("medal")}


# ---- Curated collections ACDV editor (forms/curated_collections.html) --------------
# The SECOND selection technique, beside the Amazon-search one above. A curated collection is
# a product CLASS ("Loaf pans"); the run reads what the named authorities published about it,
# ranks it, verifies the picks against our own data, and materializes product records.
# See intake/products/curated_collections + intake/products/curate/.

@app.get("/curated-collections")
def curated_list_endpoint():
    """Roster for the sidebar + the WS taxonomy options the form's category picker uses."""
    from intake.products import curated_collections as ccs
    with _db() as conn:
        rows = ccs.list_collections(conn)
        try:
            cats = [{"id": r[0], "path": r[1]} for r in conn.execute(
                "SELECT id, ws_path FROM ws_categories ORDER BY ws_path")]
        except sqlite3.OperationalError:
            cats = []
    return {"collections": rows, "ws_categories": cats}


@app.get("/curated-collections/{name}")
def curated_get_endpoint(name: str):
    """One collection + every placement it produced, with the reasoning attached."""
    from intake.products import curated_collections as ccs
    with _db() as conn:
        c = ccs.get_collection(conn, name)
        if c is None:
            raise HTTPException(status_code=404, detail="Curated collection not found.")
        c["picks"] = ccs.list_picks(conn, name)
    return c


@app.post("/curated-collections")
async def curated_create_endpoint(request: Request):
    """Create one. Auto-classifies against the WS taxonomy from the class name (the same
    matcher the commerce join uses) unless the curator pinned a category."""
    from intake.products import curated_collections as ccs
    body = await request.json()
    try:
        with _db() as conn:
            c = ccs.create_collection(conn, body)
            if not c.get("ws_category_id"):
                try:
                    from intake.products.equipment_match import classify_term
                    hit = classify_term(c["product_class"], conn)
                    if hit.get("ws_category_id"):
                        c = ccs.update_collection(conn, c["name"], {
                            "ws_category_id": hit["ws_category_id"],
                            "ws_path": hit.get("ws_path") or ""})
                except Exception as e:      # best-effort — never block creation
                    print(f"[CURATE] taxonomy match skipped: {e}")
        return c
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.put("/curated-collections/{name}")
async def curated_update_endpoint(name: str, request: Request):
    from intake.products import curated_collections as ccs
    body = await request.json()
    try:
        with _db() as conn:
            c = ccs.update_collection(conn, name, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if c is None:
        raise HTTPException(status_code=404, detail="Curated collection not found.")
    return c


@app.delete("/curated-collections/{name}")
def curated_delete_endpoint(name: str):
    from intake.products import curated_collections as ccs
    with _db() as conn:
        ok = ccs.delete_collection(conn, name)
    if not ok:
        raise HTTPException(status_code=404, detail="Curated collection not found.")
    return {"deleted": name}


@app.post("/curated-collections/{name}/run")
def curated_run_endpoint(name: str, payload: dict = Body(default={})):
    """THE BUTTON. Research -> verify -> picks -> product records, as one tracked job.

    Entity-locked so a second click can't double-run it, out-of-process and tailable because
    a full run is minutes. The class never rides on argv — the handler reads it from the DB.
    """
    from intake.products import curated_collections as ccs
    with _db() as conn:
        if ccs.get_collection(conn, name) is None:
            raise HTTPException(status_code=404, detail="Curated collection not found.")
        entity_ref = f"curated_collection:{name}"
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return {"job_id": existing["id"], "status": existing["status"],
                    "already_running": True}
        params = {"collection": name}
        if payload.get("refresh"):
            params["refresh"] = True
        job_id = jobs_lib.enqueue_job(conn, type="curated_collection_run", params=params,
                                      entity_ref=entity_ref)
    _spawn_job_runner(job_id)
    return {"job_id": job_id, "status": "queued", "stream_url": f"/jobs/{job_id}/stream"}


@app.post("/curated-collections/{name}/approve")
async def curated_approve_endpoint(name: str, request: Request):
    """Staff sign-off on the brief — the gate before an automated ranking earns anything."""
    from intake.products import curated_collections as ccs
    body = await request.json() if await request.body() else {}
    who = (body.get("who") or "").strip() or "staff"
    with _db() as conn:
        c = ccs.approve(conn, name, who)
    if c is None:
        raise HTTPException(status_code=404, detail="Curated collection not found.")
    return {"collection": name, "approved_by": who, "approved_at": c.get("approved_at")}


# ---- Reviews ACDV editor (forms/reviews.html) -------------------------------------
# Reviews as a first-class, curator-editable object: the AUTHORITY layer of the
# monetization pipeline (memory/project_monetization_pipeline) — reviews SUPPORT product
# selection (rank-within-class + trust copy), they don't drive it. A review = one reviewer's
# roundup of one product_class; its products are DERIVED from the products table's verdicts.
# See intake/products/review_store.

@app.get("/reviews/list")
def reviews_list_endpoint():
    """Flat roster for the reviews editor sidebar + datalist sources (reviewers / classes /
    categories). Registered BEFORE /reviews/{id} so the static route isn't shadowed."""
    from intake.products import review_store, catalog_store
    with _db() as conn:
        reviews = review_store.list_reviews(conn)
        reviewers = review_store.distinct_reviewers(conn)
        classes = [{"name": n, "category": c} for n, c in catalog_store.distinct_classes(conn)]
    categories = sorted({c["category"] for c in classes if c["category"]})
    return {"reviews": reviews, "reviewers": reviewers, "classes": classes,
            "categories": categories, "tiers": review_store.TIERS}


@app.post("/reviews/find")
async def reviews_find_endpoint(request: Request):
    """SERP for candidate review pages covering a product. Body: {product, extra_terms?, want?}.

    Discovery ONLY — one SERP call, no target site is fetched and nothing is written. The
    curator approves a subset from the returned list and those URLs come back through
    /reviews/ingest-url. Registered BEFORE /reviews/{id} alongside the other static routes.
    See intake/products/review_finder.
    """
    from intake.products import review_finder
    body = await request.json()
    product = (body.get("product") or "").strip() if isinstance(body, dict) else ""
    if not product:
        raise HTTPException(status_code=400, detail="product required")
    try:
        with _db() as conn:
            return review_finder.find_candidates(
                conn, product,
                want=int(body.get("want") or 12),
                extra_terms=(body.get("extra_terms") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] /reviews/find({product!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Search error: {e}") from e


@app.post("/reviews/ingest-url")
async def reviews_ingest_url_endpoint(request: Request):
    """Fetch ONE curator-approved review URL through the unblocker and ingest it (same rails as
    the bookmarklet's /extract-review). Body: {url}.

    One URL per call on purpose — an unblocker fetch of a paywalled review takes tens of
    seconds, so the UI walks the approved list sequentially and reports each row as it lands.
    A page we can't fetch or decode fails as itself; we never substitute another source for it.
    """
    from intake.products import review_finder
    body = await request.json()
    url = (body.get("url") or "").strip() if isinstance(body, dict) else ""
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    with _db() as conn:
        return review_finder.ingest_url(conn, url)


@app.get("/reviews/{review_id}")
def review_get_endpoint(review_id: str):
    """One review's metadata + its derived product roster (each with this reviewer's verdict)."""
    from intake.products import review_store
    with _db() as conn:
        r = review_store.get_review(conn, review_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return r


@app.post("/reviews")
async def review_create_endpoint(request: Request):
    """Create a review metadata record. Body: {review: {reviewer, product_class, ...}} (or the
    fields flat). reviewer + product_class are required (the identity/join key)."""
    from intake.products import review_store
    body = await request.json()
    patch = body.get("review") if isinstance(body, dict) and "review" in body else body
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="review object required")
    try:
        with _db() as conn:
            r = review_store.create_review(conn, patch)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return r


@app.put("/reviews/{review_id}")
async def review_update_endpoint(review_id: str, request: Request):
    """Edit a review header's metadata + (optional) `product_verdicts` — a list of per-item
    edits [{review_product_id, tier?, summary?, price_at_test?, …}] written into review_products
    (which reproject to the linked catalog products)."""
    from intake.products import review_store
    patch = await request.json()
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="patch object required")
    with _db() as conn:
        r = review_store.update_review(conn, review_id, patch)
    if r is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return r


@app.delete("/reviews/{review_id}")
def review_delete_endpoint(review_id: str):
    """Delete a review + its review_products + provenance link, then reproject the affected
    catalog products (empties their verdicts if nothing else links). The catalog products are
    NOT deleted (facts are never silently dropped — memory/feedback_no_silent_removal)."""
    from intake.products import review_store
    with _db() as conn:
        ok = review_store.delete_review(conn, review_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Review not found.")
    return {"deleted": review_id}


@app.post("/reviews/{review_id}/products")
async def review_add_product_endpoint(review_id: str, request: Request):
    """Add one reviewed item to a review (auto-resolves its dynamic link to a catalog product)."""
    from intake.products import review_store
    body = await request.json()
    item = body.get("item") if isinstance(body, dict) and "item" in body else body
    if not isinstance(item, dict):
        raise HTTPException(status_code=400, detail="item object required")
    with _db() as conn:
        r = review_store.add_review_product(conn, review_id, item)
    if r is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return r


@app.post("/reviews/{review_id}/resolve-links")
def review_resolve_links_endpoint(review_id: str, force: bool = False):
    """(Re)compute the dynamic review_product -> catalog product links for this review."""
    from intake.products import review_store
    with _db() as conn:
        res = review_store.resolve_links(conn, review_id, force=force)
        r = review_store.get_review(conn, review_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    return {"result": res, "review": r}


@app.delete("/review-products/{rpid}")
def review_product_delete_endpoint(rpid: str):
    """Delete one reviewed item; reprojects the product it was linked to."""
    from intake.products import review_store
    with _db() as conn:
        ok = review_store.delete_review_product(conn, rpid)
    if not ok:
        raise HTTPException(status_code=404, detail="Reviewed item not found.")
    return {"deleted": rpid}


@app.post("/review-products/{rpid}/link")
async def review_product_link_endpoint(rpid: str, request: Request):
    """(Re)link one reviewed item to a catalog product. Body {product_id}: a product_id sets a
    manual link; null/absent auto-resolves by identity."""
    from intake.products import review_store
    body = await request.json() if await request.body() else {}
    product_id = body.get("product_id") if isinstance(body, dict) else None
    with _db() as conn:
        ok = review_store.link_review_product(conn, rpid, product_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Reviewed item or product not found.")
    return {"linked": rpid, "product_id": product_id}


@app.post("/extract-review")
async def extract_review_endpoint(request: Request):
    """Bookmarklet entry: a captured review page (markdown + url) -> a tracked `review_ingest`
    JOB that decodes it and writes the review header + its review_products.

    Returns {job_id, stream_url} immediately; the caller polls /jobs/{id} and reads
    `result.review_id`. It ran inline until 2026-07-27, which left the only ingestion path in
    the system with no job record — a 30s LLM call whose failures existed nowhere but the raw
    stdout log, and which could not be retried. Pass `sync: true` to force the old blocking
    behaviour (useful for scripts and fixtures).
    """
    # GATED 2026-07-30. Was open to any caller — a customer bookmarklet key,
    # or no credential at all, could drive it. Curator surface: the product
    # and review grabbers are the ADMIN bookmarklets. Which bookmarklet can do
    # what now falls out of the owner's permissions rather than needing a
    # separate scope system on the key.
    _require_perm(request, "edit_master")
    from intake.products import review_sources
    body = await request.json()
    md = (body.get("markdown") or body.get("md") or "") if isinstance(body, dict) else ""
    url = (body.get("url") or "") if isinstance(body, dict) else ""
    captured_at = (body.get("captured_at") or "") if isinstance(body, dict) else ""
    if not md.strip():
        raise HTTPException(status_code=400, detail="markdown required")

    if body.get("sync"):
        try:
            with _db() as conn:
                return review_sources.ingest_review(conn, md, url=url, captured_at=captured_at)
        except (ValueError, NotImplementedError) as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    # Entity-locked on the page: re-tapping the bookmarklet on a page already being
    # ingested joins the run in flight rather than paying for a second extraction.
    entity_ref = f"review:{url}" if url else None
    with _db() as conn:
        if entity_ref:
            existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
            if existing:
                return {"job_id": existing["id"], "status": existing["status"],
                        "already_running": True,
                        "stream_url": f"/jobs/{existing['id']}/stream"}
        job_id = jobs_lib.enqueue_job(
            conn, type="review_ingest",
            params={"markdown": md, "url": url, "captured_at": captured_at},
            entity_ref=entity_ref)
    _spawn_job_runner(job_id)
    return {"job_id": job_id, "status": "queued", "stream_url": f"/jobs/{job_id}/stream"}


@app.get("/review-sources")
def review_sources_endpoint():
    """Which review-source decoders exist + whether each is implemented (for the bookmarklet /
    editor to show 'we recognize this source')."""
    from intake.products import review_sources
    return {"sources": review_sources.supported()}


@app.get("/dishes/{name}/top-recipes")
def list_dish_top_recipes(name: str):
    """Return the top-N recipes for this dish, DERIVED from the scoring ledger —
    `dish_run_data_points` rows with `selected = 1` at the latest `model_version`,
    ordered by `rank_score`, JOINed to their `master_recipes` content.

    The ledger is the source of truth for ranking. We deliberately DO NOT trust the
    denormalized `_master.kind='top'` label: a later re-save of a winner (open it
    in the editor, tweak, save) re-grades via embedding-match and overwrites
    `_master`, silently dropping `kind`/`dish`/`rank` — which once demoted a 100/100
    NYT recipe out of the top-10 (2026-06-14) even though the ledger still had it
    `selected`. The batch still STAMPS the label (used for its delete-and-replace
    cleanup), but display no longer depends on it.

    Falls back to the legacy `kind='top'` label query for any dish whose latest run
    predates the `selected` column. Cheap — a single JOIN, ~10-25 rows.
    """
    try:
        with _db() as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            dish = existing["name"]
            rows = conn.execute(
                """
                SELECT m.id, m.recipe_id, m.data,
                       dp.rank_score, dp.ou_percentile, dp.power_percentile
                FROM dish_run_data_points dp
                JOIN master_recipes m
                  ON m.url_normalized = dp.url AND m.user_id = 0
                WHERE dp.dish_name = :dish
                  AND dp.selected = 1
                  AND dp.model_version = (
                      SELECT MAX(model_version) FROM dish_run_data_points
                      WHERE dish_name = :dish)
                ORDER BY dp.rank_score DESC, m.traffic IS NULL, m.traffic DESC, m.id
                """,
                {"dish": dish},
            ).fetchall()
            if not rows:   # legacy fallback — latest run predates the `selected` column
                rows = conn.execute(
                    """
                    SELECT m.id, m.recipe_id, m.data,
                           dp.rank_score, dp.ou_percentile, dp.power_percentile
                    FROM master_recipes m
                    LEFT JOIN dish_run_data_points dp
                      ON dp.dish_name = :dish AND dp.url = m.url_normalized
                    WHERE json_extract(m.data, '$._master.dish') = :dish
                      AND json_extract(m.data, '$._master.kind') = 'top'
                    -- _master.rank stays primary: it IS the curated harvest order,
                    -- and the harvest already applied traffic as ITS tiebreak. Traffic
                    -- here only settles rows whose stored rank is equal or missing.
                    ORDER BY dp.rank_score IS NULL, dp.rank_score DESC,
                             CAST(json_extract(m.data, '$._master.rank') AS INTEGER) ASC,
                             m.traffic IS NULL, m.traffic DESC, m.id
                    """,
                    {"dish": dish},
                ).fetchall()
            out: list[dict] = []
            for seq, (seq_id, recipe_uuid, dj, rank_score, ou_pct, pwr_pct) in enumerate(rows, start=1):
                try:
                    d = json.loads(dj)
                except Exception:
                    continue
                source = d.get("_source") or {}
                master = d.get("_master") or {}
                exc = master.get("exceptionalism") or {}
                scoring = d.get("_scoring") or {}
                out.append({
                    "id": seq_id,
                    "recipe_id": recipe_uuid,
                    "name": d.get("name") or "(no title)",
                    "rank": seq,   # position in canonical rank_score order (1 = top)
                    "source_url": source.get("originalUrl") or "",
                    "site_name": friendly_site_name(
                        source.get("siteName"), source.get("originalUrl")),
                    "bcc_url": _bcc_link_permalink(recipe_uuid),
                    "queries": master.get("queries") or [],
                    "grade": exc.get("grade"),
                    "exc_score": exc.get("score"),
                    "exc_basis": exc.get("basis") or {},
                    "pa": scoring.get("pageAuthority"),
                    "da": scoring.get("domainAuthority"),
                    "ou": scoring.get("ouScore"),
                    # Final SQL-computed blend score + percentiles from the
                    # scoring ledger (null if this dish hasn't been scored yet).
                    "rank_score": rank_score,
                    "ou_percentile": ou_pct,
                    "power_percentile": pwr_pct,
                    # Cooped og:image thumbnail (preferred) — falls
                    # back to the hotlinked schema.org image[0] when
                    # the row pre-dates the coopt pipeline. UI prefers
                    # preview_image; the hotlink is the legacy
                    # fallback for pre-coopt rows.
                    "preview_image": source.get("previewImage") or "",
                    "fallback_image": (
                        (d.get("image") or [None])[0]
                        if isinstance(d.get("image"), list) else None
                    ),
                })
            # Editor's Choice awards — a SEPARATE band, not merged into the ranked
            # list. They carry no ledger row (they never entered a run), so the
            # query above cannot surface them; that separation is the design, not
            # an omission. Their scores ride along so the UI can DESCRIBE them —
            # an award is exempt from ranking, not from measurement.
            awards: list[dict] = []
            try:
                for (aid, a_uuid, adj) in conn.execute(
                    """SELECT id, recipe_id, data FROM master_recipes
                       WHERE json_extract(data,'$._master.dish') = :dish
                         AND json_extract(data,'$._master.kind') = 'editors_choice'
                       ORDER BY json_extract(data,'$._master.awarded_at') DESC, id""",
                    {"dish": dish},
                ).fetchall():
                    ad = json.loads(adj)
                    a_src = ad.get("_source") or {}
                    a_sc = ad.get("_scoring") or {}
                    a_m = ad.get("_master") or {}
                    awards.append({
                        "id": aid,
                        "recipe_id": a_uuid,
                        "name": ad.get("name") or "(no title)",
                        "source_url": a_src.get("originalUrl") or "",
                        "site_name": friendly_site_name(
                            a_src.get("siteName"), a_src.get("originalUrl")),
                        "bcc_url": _bcc_link_permalink(a_uuid),
                        "note": a_m.get("note"),
                        "awarded_at": a_m.get("awarded_at"),
                        "pa": a_sc.get("pageAuthority"),
                        "da": a_sc.get("domainAuthority"),
                        "ou": a_sc.get("ouScore"),
                        "preview_image": a_src.get("previewImage") or "",
                    })
            except Exception as e:
                print(f"[WARN] editors-choice band for {dish!r} skipped: {e}")
            return {
                "dish": existing["name"],
                "refreshed_at": existing.get("last_refreshed"),
                "count": len(out),
                "recipes": out,
                "editors_choice": awards,
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] list_dish_top_recipes({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


# Editor's Choice — a curator AWARD over a (dish, url) membership. Ingested to
# master_recipes at pin time as kind='editors_choice': exempt from the min-OU gate,
# never ranked against the algorithmic winners, preserved across refreshes (which
# delete kind='top' only), and excluded from the OU regression baseline.
#
# It used to be a NOMINATION — injected into the next run's candidate pool, scored,
# and shown "if it ranks". That made the award meaningless in exactly the case it
# existed for: a curator rescuing a recipe the statistics had dropped handed it back
# to the gate that dropped it (Adam Liaw's ramen school, ou -1.04, could be pinned
# and would still never appear). Changed 2026-08-12.
#
# Still the first concrete brick of the many-to-many 'collections' model (membership
# is a junction row, not a stamp on the recipe). See list_dish_top_recipes for how
# the ranked list reads the ledger, not a label — and returns awards as a separate
# band beside it.
@app.get("/dishes/{name}/editors-choice")
def list_dish_editors_choice(name: str):
    """Curator pins for a dish (newest first)."""
    try:
        with _db() as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            pins = dishes_lib.list_editors_choice(conn, existing["name"])
        return {"dish": existing["name"], "count": len(pins), "pins": pins}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] list_dish_editors_choice({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/candidates")
def list_run_candidates(collection_type: str = "dish", collection_key: str = "",
                        job_id: int = 0, limit: int = 500):
    """The candidate ledger for one run — every URL considered and what was decided.

    Defaults to the LATEST run for (collection_type, collection_key) so a caller
    that only knows "the Ramen dish" does not have to hunt for a job id first.
    `mediatable` splits it the way the AI editor will read it: kept, what may be
    reconsidered, and a count of what is excluded as fact.
    """
    try:
        from input.pipeline import candidate_ledger
        with _db() as conn:
            candidate_ledger.ensure_candidate_ledger_table(conn)
            jid = int(job_id or 0)
            if not jid:
                key = (collection_key or "").strip()
                if not key:
                    raise HTTPException(
                        status_code=400,
                        detail="collection_key or job_id is required")
                row = conn.execute(
                    "SELECT job_id FROM run_candidates WHERE collection_type = ? "
                    "AND collection_key = ? ORDER BY run_started_at DESC, id DESC "
                    "LIMIT 1", (collection_type, key)).fetchone()
                if not row:
                    # No run ledgered yet — an empty ledger is a legitimate state
                    # (nothing has run since this shipped), not an error.
                    return {"job_id": None, "kept": [], "reconsider": [],
                            "excluded_as_fact": 0, "summary": {}}
                jid = row[0]
            packet = candidate_ledger.mediatable_for_run(conn, jid)
            packet["job_id"] = jid
            packet["summary"] = candidate_ledger.run_summary(conn, jid)
            # The editor's shadow verdicts, joined onto the rows they judged, so the
            # form shows the arithmetic and the disagreement in one place rather than
            # asking the curator to hold two screens in their head.
            try:
                from input.pipeline import ai_editor
                ai_editor.ensure_mediation_table(conn)
                med = {m["url_normalized"]: m for m in ai_editor.list_for_run(conn, jid)}
                for r in packet["kept"] + packet["reconsider"]:
                    m = med.get(r["url_normalized"])
                    if m:
                        r["mediation"] = {
                            "verdict": m["verdict"], "band": m["band"],
                            "ordinal_rank": m["ordinal_rank"],
                            "evidence": m["evidence"], "rationale": m["rationale"],
                            "applied": bool(m["applied"]),
                        }
                packet["mediated"] = bool(med)
            except Exception as e:
                print(f"[candidates] mediation join skipped: {e}")
                packet["mediated"] = False
            lim = max(1, min(int(limit or 500), 2000))
            packet["reconsider"] = packet["reconsider"][:lim]
            return packet
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] list_run_candidates failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/dishes/{name}/editors-choice")
async def add_dish_editors_choice(name: str, request: Request, payload: dict = Body(...)):
    """Award a URL Editor's Choice for a dish (admin only). Body: {url, note?}.
    Idempotent on the normalized URL.

    An AWARD, not a nomination. It is ingested to master_recipes immediately as
    `kind='editors_choice'` and never competes: it does not pass through the
    min-OU gate, it is not ranked against the algorithmic winners, and it keeps
    its place across refreshes.

    Why it changed (2026-08-12): a pin used to be injected into the next run as
    an extra candidate, scored, and shown "if it ranks" — so a curator rescuing a
    recipe the statistics had dropped handed it straight back to the gate that
    dropped it. Adam Liaw's ramen school (ou -1.04) could be pinned and would
    still never appear. An award that the algorithm can veto is not an award.

    The rest of the machinery already assumed this shape and only needed the
    ingest to write the kind: a dish refresh deletes `kind='top'` rows ONLY, so
    an award survives it, and the OU regression already excludes
    editors_choice/legacy so an award cannot skew the baseline it is exempt from.
    """
    _require_perm(request, "manage_dishes")
    url = (payload.get("url") or "").strip()
    note = (payload.get("note") or "").strip() or None
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    try:
        with _db() as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            pin = dishes_lib.add_editors_choice(conn, existing["name"], url, note)
            dish_name = existing["name"]
        # Ingest NOW so the award is visible immediately rather than at the next
        # refresh. Best-effort: the pin is already recorded, so a fetch failure
        # leaves a re-ingestable award rather than losing the curator's decision.
        ingested = False
        try:
            ingested = await _extract_url_to_master_as_editors_choice(
                url, dish_name, note=note)
        except Exception as e:
            print(f"[EDITORS-CHOICE] ingest failed for {url}: {type(e).__name__}: {e}")
        return {"ok": True, "pin": pin, "ingested": ingested}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] add_dish_editors_choice({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.delete("/dishes/{name}/editors-choice")
def remove_dish_editors_choice(name: str, request: Request, url_normalized: str = ""):
    """Revoke an Editor's Choice award by url_normalized (query param; admin only).

    Removes the master row too. The award IS the row's reason for existing —
    nothing else would ever have admitted it, and a refresh will not clean it up
    because refreshes only delete `kind='top'`. Leaving it would strand an
    un-earned recipe in the cohort permanently.
    """
    _require_perm(request, "manage_dishes")
    if not url_normalized:
        raise HTTPException(status_code=400, detail="url_normalized is required")
    try:
        with _db() as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            n = dishes_lib.remove_editors_choice(conn, existing["name"], url_normalized)
            # Reuse the shared retire path rather than a raw DELETE: it clears the
            # dish block, keeps the row when a publisher block still needs it, and
            # goes through the vec0 cleanup triggers. Scoped to kind
            # 'editors_choice' so revoking an award can never remove an
            # algorithmically-earned row for the same URL.
            removed_rows = dishes_lib.retire_master_membership(
                conn, marker="dish", value=existing["name"],
                other_marker="publisher", remove_fields=["dish", "exceptionalism"],
                also_match=("kind", "editors_choice"),
                url_normalized=url_normalized)[1]
        return {"ok": True, "removed": n, "rows_removed": removed_rows}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] remove_dish_editors_choice({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/dishes/{name}/cohort")
def list_dish_cohort(name: str):
    """The full SCORED cohort for a dish, straight from dish_run_data_points
    — every candidate the last batch fit saw, with its SQL-computed
    ou / power / percentiles / rank_score and the `selected` winner flag.
    Winners (rows whose normalized url has a saved master_recipe) are joined
    to pull name / thumbnail / grade; non-winners (never extracted) carry
    only url + scores. Ordered by rank_score desc, unscored last.

    A diagnostic / validation view — lets you see whether rank_score's
    top-N matches the `selected` winners (the check we want before making
    SQL the selector). Distinct from /top-recipes (winners-only, full
    presentation). Direct read of the ledger; no per-row recompute."""
    try:
        with _db() as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            dish = existing["name"]
            rows = conn.execute(
                """
                SELECT p.url, p.da, p.pa, p.ou, p.power,
                       p.ou_percentile, p.power_percentile, p.rank_score,
                       p.selected, p.model_version, p.cohort_status,
                       m.name, m.preview_image, m.grade
                FROM dish_run_data_points p
                LEFT JOIN (
                    SELECT url_normalized,
                           json_extract(data, '$.name') AS name,
                           COALESCE(json_extract(data, '$._source.previewImage'),
                                    json_extract(data, '$.image[0]')) AS preview_image,
                           json_extract(data, '$._master.exceptionalism.grade') AS grade,
                           MAX(traffic) AS traffic
                    FROM master_recipes
                    WHERE user_id = 0
                      AND json_extract(data, '$._master.dish') = :dish
                    GROUP BY url_normalized
                ) m ON m.url_normalized = p.url
                WHERE p.dish_name = :dish
                -- Same tiebreak as the top-N list, so the Considered panel and the
                -- selected list agree about order among equal scores. p.url was
                -- alphabetical, which is not a signal at all.
                ORDER BY p.rank_score IS NULL, p.rank_score DESC,
                         m.traffic IS NULL, m.traffic DESC, p.url
                """,
                {"dish": dish},
            ).fetchall()
            cols = ["url", "da", "pa", "ou", "power", "ou_percentile",
                    "power_percentile", "rank_score", "selected",
                    "model_version", "cohort_status", "name", "preview_image", "grade"]
            cohort = [dict(zip(cols, r)) for r in rows]
            return {
                "dish": dish,
                "count": len(cohort),
                "scored": sum(1 for c in cohort if c["rank_score"] is not None),
                "selected": sum(1 for c in cohort if c["selected"]),
                "cohort": cohort,
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] list_dish_cohort({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/dishes/{name}/rejects")
def list_dish_rejects(name: str):
    """Return the URLs from the dish's last refresh that made it past
    the batch front-end (filter_disallowed + is_recipe + Moz scoring)
    but then failed extract / save / save-gate. Each row carries the
    original DA / PA / OU and the rejection reason so the dish form
    can render "would have qualified" against last_run_bottom_ou and
    surface a manual-recovery affordance (open the URL in browser,
    use the bookmarklet, save to master normally).
    Returns [] when the dish hasn't been refreshed yet or had no
    rejects on its last run. No staff gate — read-only diagnostic."""
    try:
        with _db() as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            return {
                "dish": existing["name"],
                "bottom_ou": existing.get("last_run_bottom_ou"),
                "ou_fit": existing.get("last_ou_fit"),
                "rejects": dishes_lib.list_rejects_for_dish(conn, existing["name"]),
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] list_dish_rejects({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.delete("/dishes/{name}")
def delete_dish_endpoint(name: str, request: Request):
    _require_perm(request, "manage_dishes")
    """Delete a dish AND its top-kind master_recipes rows. editors_choice
    and legacy rows for this dish are untouched (kind filter)."""
    try:
        with _db() as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            _enable_vec_for_delete(conn)  # trg_dish_vec_cleanup deletes from dishes_vec (vec0)
            cascaded = dishes_lib.delete_master_rows_for_dish(conn, name, kind="top")
            dishes_lib.delete_dish(conn, name)
            return {
                "deleted": True,
                "name": name,
                "cascaded_master_rows": cascaded,
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] delete_dish({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/dishes/{name}/fit-data")
def get_dish_fit_data_endpoint(name: str):
    """Return the (URL, DA, PA) cohort the dish's last refresh fit
    against, joined with a tiny status label per row:

        - saved:        URL is in master_recipes for this dish (kept)
        - rejected:     URL is in dish_rejects (post-Moz, failed
                        extract / save / save-gate)
        - dropped:      URL is in dish_run_data_points but neither
                        of the above — it was dropped at the OU
                        floor in this run

    Used by the dish form to render an expandable "regression data"
    table below the OU fit panel.
    """
    try:
        with _db() as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail=f"Dish not found: {name}")
            rows = conn.execute(
                "SELECT url, da, pa FROM dish_run_data_points "
                "WHERE dish_name = ? ORDER BY pa DESC NULLS LAST, da DESC",
                (name,),
            ).fetchall()
            # dish_run_data_points stores the RAW SerpAPI URL (www., trailing
            # slash, tracking params intact), while master_recipes and
            # dish_rejects store the normalize_url'd form. Compare on the
            # canonical form on BOTH sides or every saved/rejected row falls
            # through to "dropped" (see the agnolotti run: 7 saved showed as
            # dropped because honest-food.net/...-meat/ != ...-meat).
            #
            # A saved row's originalUrl can also be a Wayback snapshot when
            # extraction fell back to archive.org (live site down at save
            # time): https://web.archive.org/web/<ts>id_/https://real...
            # Unwrap to the embedded live URL so it matches the cohort's
            # live URL (agnolotti's mosthungry.com row).
            # scoring_url = unwrap_wayback + normalize_url, the same pair the
            # metabase key uses. Was a hand-rolled find("/http") scan here — the
            # third independent copy of this unwrap. One canonical form or the
            # comparison above silently fails again.
            def _canon(u):
                return scoring_url(u) if u else ""
            saved_urls = {
                _canon(r[0]) for r in conn.execute(
                    "SELECT json_extract(data, '$._source.originalUrl') "
                    "FROM master_recipes "
                    "WHERE json_extract(data, '$._master.dish') = ?",
                    (name,),
                ).fetchall() if r[0]
            }
            rejected_urls = {
                _canon(r[0]) for r in conn.execute(
                    "SELECT url FROM dish_rejects WHERE dish_name = ?",
                    (name,),
                ).fetchall() if r[0]
            }
            out = []
            for url, da, pa in rows:
                norm = _canon(url)
                if norm in saved_urls:
                    status = "saved"
                elif norm in rejected_urls:
                    status = "rejected"
                else:
                    status = "dropped"
                out.append({"url": url, "da": da, "pa": pa, "status": status})
            return out
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] get_dish_fit_data({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


# =========================================================================
# Chapters admin — list + detail + refresh-fit endpoints. The chapters
# table holds the chapter-level OU regression fit used as the
# grading fallback when per-dish cohorts are below_min_n. The form
# (forms/chapters.html) is read-mostly: list every chapter, show its
# fit status, allow recompute + curator notes. No add/delete — the
# canonical chapter set is the CHAPTERS list in extract.chapter_classifier.
# =========================================================================
@app.get("/branding")
def branding_config():
    """Public app-shell branding (site name, logo, home link) for the
    library-shell header. Sourced from bcc_config.json so swapping the
    brand is a config edit, not a code change.

    `bookmarklet_api_base` is the externally-reachable origin the bookmarklets
    POST to (they run on a retailer/recipe page, so they can't read our host
    from location). DB-resident (system_config.public_base_url, seeded from
    bcc_link_domain) so a self-hoster's install page bakes THEIR host into the
    loader with no code change (portable-package)."""
    from input.pipeline.config import BRAND_NAME, BRAND_LOGO_URL, BRAND_HOME_URL
    from input.pipeline.system_config import public_base_url
    return {
        "name": BRAND_NAME, "logo_url": BRAND_LOGO_URL, "home_url": BRAND_HOME_URL,
        "bookmarklet_api_base": public_base_url(),
    }


@app.get("/chapters")
def list_chapters_endpoint():
    try:
        from input.pipeline.chapters import list_chapters_with_status
        from extract.chapter_classifier import CHAPTERS
        with _db() as conn:
            rows = list_chapters_with_status(conn, CHAPTERS)
        # Flag built-in taxonomy chapters so the editor hides Delete on them
        # (only curator-created chapters can be deleted — see delete endpoint).
        builtin = set(CHAPTERS)
        for r in rows:
            r["is_builtin"] = r.get("name") in builtin
        return rows
    except Exception as e:
        print(f"[ERROR] list_chapters failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


def _chapter_known(name: str) -> bool:
    """Valid if in the classifier taxonomy OR present as a chapters-table row
    (curator-created). The table is the source of truth; the code constant is
    a bootstrap seed — see memory/feedback_no_data_in_code.md."""
    from extract.chapter_classifier import CHAPTERS
    if name in CHAPTERS:
        return True
    from input.pipeline.chapters import ensure_chapters_table, chapter_exists
    with _db() as conn:
        ensure_chapters_table(conn)
        return chapter_exists(conn, name)


@app.post("/chapters")
def create_chapter_endpoint(payload: dict = Body(...)):
    """Create a curator-defined chapter (a row in the chapters table). A fresh
    chapter starts unfit until recipes are classified into it + a refresh runs."""
    from input.pipeline.chapters import (
        ensure_chapters_table, create_chapter, get_chapter_detail,
    )
    name = (payload.get("name") or "").strip()
    notes = payload.get("notes")
    if not name:
        raise HTTPException(status_code=400, detail="Chapter name is required")
    if notes is not None and not isinstance(notes, str):
        raise HTTPException(status_code=400, detail="notes must be a string")
    try:
        with _db() as conn:
            ensure_chapters_table(conn)
            create_chapter(conn, name, (notes or "").strip() or None)
            return get_chapter_detail(conn, name)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] create_chapter failed: {e}")
        raise HTTPException(status_code=500, detail=f"Create error: {e}") from e


@app.delete("/chapters/{name}")
def delete_chapter_endpoint(name: str):
    """Delete a curator-created chapter. Refused if any dish still points to it
    (reassign/delete those first — which in turn frees their recipes), or if it's
    a built-in taxonomy chapter (those live in code and would stay 'known' after
    the row is gone, so they can't be retired here)."""
    from extract.chapter_classifier import CHAPTERS
    if name in CHAPTERS:
        raise HTTPException(status_code=409,
                            detail="Built-in taxonomy chapter — can't be deleted "
                                   "here (it's defined in code, not curator-created).")
    from input.pipeline.chapters import ensure_chapters_table, delete_chapter
    try:
        with _db() as conn:
            ensure_chapters_table(conn)
            delete_chapter(conn, name)
        return {"ok": True, "deleted": name}
    except ValueError as e:
        msg = str(e)
        raise HTTPException(status_code=(404 if "not found" in msg else 409), detail=msg) from e
    except Exception as e:
        print(f"[ERROR] delete_chapter({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Delete error: {e}") from e


@app.get("/chapters/{name}")
def get_chapter_endpoint(name: str):
    try:
        from input.pipeline.chapters import get_chapter_detail
        from extract.chapter_classifier import CHAPTERS
        if not _chapter_known(name):
            raise HTTPException(status_code=404, detail=f"Unknown chapter: {name}")
        with _db() as conn:
            detail = get_chapter_detail(conn, name)
            # The chapter's dishes ranked by competitiveness (nightly rollup) —
            # which dishes in this chapter are hotly-covered vs niche.
            rows = conn.execute(
                "SELECT name, competitiveness_pct, field_clout, last_run_count, "
                "last_refreshed, display_name "
                "FROM dishes WHERE chapter = ? "
                "ORDER BY CASE WHEN field_clout IS NULL THEN 1 ELSE 0 END, "
                "field_clout DESC, name",
                (name,),
            ).fetchall()
            detail["dishes"] = [
                {"name": r[0], "competitiveness_pct": r[1], "field_clout": r[2],
                 "last_run_count": r[3], "last_refreshed": r[4],
                 "display_name": (r[5] or "").strip() or r[0]}
                for r in rows
            ]
            return detail
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] get_chapter({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/chapters/{name}/recipes")
def chapter_recipes_endpoint(name: str):
    """The chapter's OU-fit COHORT: every (url, DA, PA) record from the
    chapter's underlying dishes' refreshes — the FULL pre-Moz-trim set the
    formula is fit on, not just the saved winners. Editorial/curated picks
    live only in master_recipes and are never in this cohort, so the fit
    excludes them by construction."""
    if not _chapter_known(name):
        raise HTTPException(status_code=404, detail=f"Unknown chapter: {name}")
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT dp.url, dp.da, dp.pa "
                "FROM dish_run_data_points dp "
                "JOIN dishes d ON d.name = dp.dish_name "
                "WHERE d.chapter = ? "
                # Only records the regression actually consumes (both DA & PA
                # present) — so this list's count == last_ou_fit.n, the number
                # checked against the 25-record minimum. No apples-vs-oranges.
                "AND dp.da IS NOT NULL AND dp.pa IS NOT NULL "
                "ORDER BY dp.da DESC, dp.pa DESC",
                (name,),
            ).fetchall()
        return [{"url": u, "da": da, "pa": pa} for u, da, pa in rows]
    except Exception as e:
        print(f"[ERROR] chapter_recipes({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/chapters/{name}/top-recipes")
def chapter_top_recipes_endpoint(name: str):
    """The chapter's top-10 recipes by OU (snapshot stored on the chapter
    row at fit time), decorated with the BCC permalink."""
    if not _chapter_known(name):
        raise HTTPException(status_code=404, detail=f"Unknown chapter: {name}")
    try:
        from input.pipeline.chapters import get_chapter_top_recipes
        with _db() as conn:
            recs = get_chapter_top_recipes(conn, name)
        for r in recs:
            if r.get("recipe_id"):
                r["bcc_url"] = _bcc_link_permalink(r["recipe_id"])
        return {"chapter": name, "count": len(recs), "recipes": recs}
    except Exception as e:
        print(f"[ERROR] chapter_top_recipes({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/chapters/{name}/refresh")
def refresh_chapter_endpoint(name: str):
    """Recompute the OU fit for a single chapter from the current
    master_recipes corpus. Returns the new fit + detail blob."""
    try:
        from input.pipeline.chapters import (
            compute_and_store_chapter_fit, get_chapter_detail,
        )
        from extract.chapter_classifier import CHAPTERS
        if not _chapter_known(name):
            raise HTTPException(status_code=404, detail=f"Unknown chapter: {name}")
        with _db() as conn:
            fit = compute_and_store_chapter_fit(conn, name)
            detail = get_chapter_detail(conn, name)
        print(f"[CHAPTER-FIT] {name!r}: n={fit.get('n')} used={fit.get('used')} model={fit.get('model')}")
        return detail
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] refresh_chapter({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Refresh error: {e}") from e


@app.post("/chapters/refresh-all")
def refresh_all_chapters_endpoint():
    """One-pass recompute of every chapter's fit. Returns the summary
    dict from backfill_all_chapters."""
    try:
        from input.pipeline.chapters import backfill_all_chapters
        from extract.chapter_classifier import CHAPTERS
        with _db() as conn:
            return backfill_all_chapters(
                conn, [c for c in CHAPTERS if c != "Uncertain"],
            )
    except Exception as e:
        print(f"[ERROR] refresh_all_chapters failed: {e}")
        raise HTTPException(status_code=500, detail=f"Refresh error: {e}") from e


@app.patch("/chapters/{name}")
def patch_chapter_endpoint(name: str, payload: dict = Body(...)):
    """Update curator notes on a chapter row."""
    try:
        from input.pipeline.chapters import update_chapter_notes, get_chapter_detail
        from extract.chapter_classifier import CHAPTERS
        if not _chapter_known(name):
            raise HTTPException(status_code=404, detail=f"Unknown chapter: {name}")
        if "notes" in payload:
            notes = payload["notes"]
            if notes is not None and not isinstance(notes, str):
                raise HTTPException(status_code=400, detail="notes must be a string or null")
            notes = (notes.strip() or None) if isinstance(notes, str) else None
            with _db() as conn:
                update_chapter_notes(conn, name, notes)
        with _db() as conn:
            return get_chapter_detail(conn, name)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] patch_chapter({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}") from e


# =========================================================================
# System config — the DB-resident "config file" (the system record). Backs the
# System admin editor. `system_config` table is canonical; bcc_config.json is
# its bootstrap seed (migrating in incrementally). See
# memory/project_system_config.md. First consumer: the dish scheduler.
# =========================================================================

# Keys the admin UI must NOT write — system-maintained (e.g. the scheduler
# stamps its own last-pass time). The editor renders these read-only too.
_SYSCONFIG_READONLY = {"scheduler_last_tick_at"}


@app.get("/system-config")
def list_system_config_endpoint():
    """All settings, decoded + grouped-ready (ordered by category, key)."""
    from input.pipeline.system_config import ensure_system_config_table, list_settings
    try:
        with _db() as conn:
            ensure_system_config_table(conn)
            return list_settings(conn)
    except Exception as e:
        print(f"[ERROR] list_system_config failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/system-config")
def update_system_config_endpoint(request: Request, payload: dict = Body(...)):
    """Update setting value(s). Accepts either a single {key, value} or a bulk
    {updates: {key: value, ...}}. Unknown keys are rejected; read-only keys are
    refused. Returns the full settings list so the UI re-renders fresh."""
    # GATED 2026-07-29 (was a TODO while this was dev-only). `configure_system`
    # rather than the TODO's `edit_master`: this writes the instance record —
    # including the Amazon Associates tracking IDs — which is owner-level, not
    # curator-level. See input/pipeline/auth.py ROLE_PERMISSIONS.
    _require_perm(request, "configure_system")
    from input.pipeline.system_config import (
        ensure_system_config_table, set_setting, list_settings,
    )
    if "updates" in payload and isinstance(payload["updates"], dict):
        updates = payload["updates"]
    elif "key" in payload:
        updates = {payload["key"]: payload.get("value")}
    else:
        raise HTTPException(status_code=400,
                            detail="Body must be {key, value} or {updates: {...}}")
    try:
        with _db() as conn:
            ensure_system_config_table(conn)
            known = {r["key"] for r in list_settings(conn)}
            for key, value in updates.items():
                if key in _SYSCONFIG_READONLY:
                    raise HTTPException(status_code=400,
                                        detail=f"{key!r} is read-only (system-maintained)")
                if key not in known:
                    raise HTTPException(status_code=400, detail=f"Unknown setting {key!r}")
                set_setting(conn, key, value)
            return list_settings(conn)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] update_system_config failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}") from e


# =========================================================================
# Scheduled jobs — the DB-resident registry of recurring jobs (purpose,
# cadence, on/off), editable via the a/c/d Jobs editor and obeyed by the
# scheduler. Distinct from /jobs (the run queue). See scheduled_jobs.py.
# =========================================================================

@app.get("/scheduled-jobs")
def list_scheduled_jobs_endpoint():
    """All scheduled-job definitions + their last run, plus the registered
    handler types so the editor can offer a job_type picker."""
    from input.pipeline import scheduled_jobs as sched
    try:
        with _db() as conn:
            sched.ensure_scheduled_jobs_table(conn)
            jobs = sched.list_scheduled_jobs(conn)
            # attach the last run's log url for each row
            for j in jobs:
                j["last_log_url"] = None
                if j.get("last_job_id"):
                    run = jobs_lib.get_job(conn, j["last_job_id"])
                    if run and run.get("log_filename"):
                        j["last_log_url"] = f"/logs/{run['log_filename']}"
        return {"jobs": jobs, "handler_types": sorted(jobs_lib.list_handler_types())}
    except Exception as e:
        print(f"[ERROR] list_scheduled_jobs failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/scheduled-jobs/{name}")
def upsert_scheduled_job_endpoint(name: str, payload: dict = Body(...)):
    """Create or update a scheduled job (job_type/purpose/interval_hours/params/
    enabled). Rejects an unknown job_type so the schedule can't reference a
    handler that doesn't exist."""
    from input.pipeline import scheduled_jobs as sched
    try:
        with _db() as conn:
            sched.ensure_scheduled_jobs_table(conn)
            jt = payload.get("job_type")
            if jt and jt not in jobs_lib.list_handler_types():
                raise HTTPException(status_code=400,
                                    detail=f"Unknown job_type {jt!r}")
            return sched.upsert_scheduled_job(conn, name, payload)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] upsert_scheduled_job failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}") from e


@app.delete("/scheduled-jobs/{name}")
def delete_scheduled_job_endpoint(name: str):
    from input.pipeline import scheduled_jobs as sched
    try:
        with _db() as conn:
            sched.ensure_scheduled_jobs_table(conn)
            if not sched.delete_scheduled_job(conn, name):
                raise HTTPException(status_code=404, detail="Scheduled job not found")
        return {"ok": True, "deleted": name}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] delete_scheduled_job failed: {e}")
        raise HTTPException(status_code=500, detail=f"Delete error: {e}") from e


@app.post("/scheduled-jobs/{name}/run")
def run_scheduled_job_now_endpoint(name: str, request: Request):
    """Run a scheduled job NOW, out-of-process (Popen `python -m jobs run`), so
    the admin can trigger it on demand from the editor. Returns the spawned job."""
    _require_perm(request, "refresh_dishes")
    from input.pipeline import scheduled_jobs as sched
    with _db() as conn:
        sched.ensure_scheduled_jobs_table(conn)
        row = sched.get_scheduled_job(conn, name)
        if not row:
            raise HTTPException(status_code=404, detail="Scheduled job not found")
        job_id = jobs_lib.enqueue_job(
            conn, type=row["job_type"], params=row.get("params") or {},
            entity_ref=f"scheduled:{name}")
    import subprocess
    proj = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUNBUFFERED"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jobs", "exec", "--job-id", str(job_id)],
            cwd=proj, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_detached_flags(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to spawn runner: {e}") from e
    return {"name": name, "job_id": job_id, "spawned": True,
            "stream_url": f"/jobs/{job_id}/stream"}


# =========================================================================
# Cook tips/checks KNOWLEDGE BASE — the curated, owned moat. Entries authored in
# OUR words, curated as drafts + published; the augment pass (3c-b) may only
# SELECT from PUBLISHED entries (provenance by id). See cook_kb.py.
# =========================================================================

@app.get("/cook-kb")
def list_cook_kb_endpoint(status: Optional[str] = None):
    """All KB entries (or filter by status=draft|published), + the controlled
    technique vocab for the editor's tag picker."""
    from input.pipeline import cook_kb
    try:
        with _db() as conn:
            cook_kb.ensure_cook_kb_table(conn)
            return {"entries": cook_kb.list_kb(conn, status=status),
                    "vocab": cook_kb.TECHNIQUE_VOCAB,
                    "kinds": list(cook_kb.KINDS), "scopes": list(cook_kb.SCOPES),
                    "confidences": list(cook_kb.CONFIDENCES)}
    except Exception as e:
        print(f"[ERROR] list_cook_kb failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/cook-kb/import")
def import_cook_kb_endpoint(payload: dict = Body(...), overwrite: bool = False):
    """Bulk-import authored entries (e.g. the 100 ATK-topic drafts) as DRAFTS for
    review. Body: {entries: [...]} (+ optional "overwrite": true). Never
    auto-publishes. Default SKIPS existing ids; ?overwrite=true UPDATES them in
    place (keeping their status) — use to backfill a corrected file. Defined
    BEFORE /cook-kb/{kb_id} so the literal 'import' path isn't captured as an id."""
    from input.pipeline import cook_kb
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    ow = overwrite or (isinstance(payload, dict) and bool(payload.get("overwrite")))
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="Body must be {entries:[...]} or a JSON list")
    try:
        with _db() as conn:
            cook_kb.ensure_cook_kb_table(conn)
            return cook_kb.import_drafts(conn, entries, overwrite=ow)
    except Exception as e:
        print(f"[ERROR] import_cook_kb failed: {e}")
        raise HTTPException(status_code=500, detail=f"Import error: {e}") from e


@app.post("/cook-kb/{kb_id}")
def upsert_cook_kb_endpoint(kb_id: str, payload: dict = Body(...)):
    """Create or update a KB entry (curate). New entries default to draft."""
    from input.pipeline import cook_kb
    try:
        with _db() as conn:
            cook_kb.ensure_cook_kb_table(conn)
            return cook_kb.upsert_kb(conn, kb_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] upsert_cook_kb failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}") from e


@app.post("/cook-kb/{kb_id}/status")
def set_cook_kb_status_endpoint(kb_id: str, payload: dict = Body(...)):
    """Publish or unpublish an entry (status=published|draft). Publishing is the
    gate — only published entries reach the augment pass."""
    from input.pipeline import cook_kb
    try:
        with _db() as conn:
            cook_kb.ensure_cook_kb_table(conn)
            updated = cook_kb.set_status(conn, kb_id, payload.get("status", ""))
            if not updated:
                raise HTTPException(status_code=404, detail="Entry not found")
            return updated
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] set_cook_kb_status failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}") from e


@app.delete("/cook-kb/{kb_id}")
def delete_cook_kb_endpoint(kb_id: str):
    from input.pipeline import cook_kb
    try:
        with _db() as conn:
            cook_kb.ensure_cook_kb_table(conn)
            if not cook_kb.delete_kb(conn, kb_id):
                raise HTTPException(status_code=404, detail="Entry not found")
        return {"ok": True, "deleted": kb_id}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] delete_cook_kb failed: {e}")
        raise HTTPException(status_code=500, detail=f"Delete error: {e}") from e


# =========================================================================
# Domain master — the canonical per-publisher record (display name, story,
# extraction tips, country/language provenance, allow/deny, DA). Backs the
# friendly-site-name resolver and the a/c/d Domains editor. The `domains`
# table is the source of truth; domain_display_names.json is its seed.
# =========================================================================
@app.get("/domains")
def list_domains_endpoint():
    """All domain-master rows, seeded from the JSON bootstrap on first call."""
    try:
        from input.pipeline import domains_lib
        with _db() as conn:
            domains_lib.ensure_domains_table(conn)
            if conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0] == 0:
                domains_lib.seed_domains(conn)
            return domains_lib.list_domains(conn)
    except Exception as e:
        print(f"[ERROR] list_domains failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/domains")
def create_domain_endpoint(request: Request, payload: dict = Body(...)):
    """Create a curator-defined domain row (host is the key)."""
    # GATED 2026-07-29. The "when exposed publicly" condition in the old TODO
    # was already true — the app has been on recipes.tbotb.com.
    _require_perm(request, "edit_master")
    from input.pipeline import domains_lib
    host = (payload.get("domain") or "").strip()
    if not host:
        raise HTTPException(status_code=400, detail="domain (host) is required")
    try:
        with _db() as conn:
            return domains_lib.create_domain(conn, host, payload)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] create_domain failed: {e}")
        raise HTTPException(status_code=500, detail=f"Create error: {e}") from e


@app.get("/domains/{domain}/backlinks-file")
def domain_backlinks_file_endpoint(domain: str):
    """Whether a local SEMrush page export exists for this publisher (drives the
    'SEMrush backlinks file' source on the domain form). Returns the detected filename
    or the expected name so the form can show it."""
    from input.pipeline import domains_lib, collections_lib
    import os
    host = domains_lib._canon_host(domain)
    with _db() as conn:
        row = domains_lib.get_domain(conn, host) or {}
    override = (row.get("backlinks_dir") or "").strip() or None
    path = collections_lib.backlinks_file_path(host, extra_dir=override)
    searched = collections_lib.backlinks_search_dirs(override)
    return {"present": bool(path),
            "filename": os.path.basename(path) if path else None,
            "path": path,                       # full path so the admin sees WHERE it read from
            "folder": os.path.dirname(path) if path else None,
            "searched": searched,               # every folder checked (in priority order)
            "expected": collections_lib.expected_export_name(host)}


@app.post("/domains/{domain}/upload-export")
async def domain_upload_export_endpoint(domain: str, file: UploadFile = File(...)):
    """Accept a SEMrush export uploaded FROM THE BROWSER and write it onto the SERVER's
    disk where the harvest reads it — the cross-machine fix for "the .xlsx downloaded on
    the client machine never reaches the server." Lands the file in the configured inbox
    (semrush_inbox_dir → Downloads default) under its own name, then PINS the domain's
    backlinks_dir override to the exact saved path so resolution is deterministic (an
    existing exact path is returned directly by backlinks_file_path). A later upload
    overwrites the pin with the new path — so there's never a stale override to chase."""
    from input.pipeline import domains_lib, collections_lib
    import os, re
    host = domains_lib._canon_host(domain)
    # basename only (strip any client path / traversal), then a conservative sanitize.
    fname = os.path.basename((file.filename or "").replace("\\", "/")).strip()
    if not fname.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400,
                            detail="Upload a SEMrush .xlsx export (Organic Research → Pages → Export).")
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", fname)
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400,
                            detail=f"File too large ({len(data) // (1024 * 1024)} MB > 25 MB).")
    if data[:2] != b"PK":   # .xlsx is a zip container — cheap sanity check
        raise HTTPException(status_code=400, detail="That doesn't look like a valid .xlsx (Excel) file.")
    # Save into the project's own input/ folder — it's a harvest SEARCH dir AND it's
    # writable by the SERVICE account (LocalSystem). The default inbox (~/Downloads) is a
    # HUMAN profile folder the service can't necessarily write, which is the whole reason
    # a client-downloaded export can't just be dropped there. Pinning the exact path below
    # makes resolution work regardless; landing it in input/ keeps it found even if the
    # pin is later cleared.
    folder = collections_lib._input_dir()
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, fname)

    def _write(path):
        with open(path, "wb") as f:
            f.write(data)
    try:
        _write(dest)
    except PermissionError:
        # An identically-named file exists and is owned by another account (can't
        # overwrite) → fall back to the browser-style "(n)" de-dup suffix the glob tolerates.
        base, ext = os.path.splitext(fname)
        for n in range(1, 100):
            alt = os.path.join(folder, f"{base} ({n}){ext}")
            if not os.path.exists(alt):
                _write(alt)
                dest = alt
                break
        else:
            raise
    with _db() as conn:
        domains_lib.update_domain(conn, host,
                                  {"backlinks_dir": dest, "harvest_source": "backlinks_file"})
    return {"saved": True, "filename": fname, "folder": folder, "path": dest}


def _spawn_publisher_refresh(conn, host: str, *, source: str = "backlinks_file",
                             records=None, label: str = "") -> Optional[int]:
    """Enqueue + spawn an out-of-process publisher_refresh job for `host` (the same
    job the manual refresh button uses), deduped on the in-flight entity. Returns the
    job id, or None if one is already in flight. Shared by the manual refresh and the
    SEMrush inbox scan so both routes harvest identically."""
    from input.pipeline import domains_lib
    row = domains_lib.get_domain(conn, host) or {}
    entity_ref = f"publisher:{host}"
    if jobs_lib.find_in_flight_for_entity(conn, entity_ref):
        return None
    keep = int(row.get("keep_top_n") or 10)
    job_id = jobs_lib.enqueue_job(
        conn, type="publisher_refresh",
        params={"host": host, "keep": keep,
                "pages": int(row.get("search_pages") or 10),
                "query": (row.get("serp_query") or "").strip() or None,
                "recipe_path": (row.get("recipe_path") or "").strip() or None,
                "check_recipe": not bool(int(row.get("paywall", 0) or 0)),
                "source": source, "records": records, "log_label": label or host,
                "backlinks_dir": (row.get("backlinks_dir") or "").strip() or None,
                "exclude_words": row.get("exclude_words") or "",
                "unblocker": ((row.get("fetch_strategy") or "") == "unblocker")},
        entity_ref=entity_ref)
    import subprocess
    proj = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUNBUFFERED"] = "1"
    subprocess.Popen(
        [sys.executable, "-m", "jobs", "exec", "--job-id", str(job_id)],
        cwd=proj, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=_detached_flags())
    return job_id


@app.get("/domains/harvest-worklist")
def harvest_worklist_endpoint():
    """The SEMrush human-workflow "Due today" worklist: every SEMrush-managed,
    allowed domain that is NEW or DUE/overdue, each with its deep-link + schedule
    fields. A view over the domains rows — the curator clicks each link, runs the
    SEMrush export, saves it to the watched inbox, then POSTs /semrush-inbox/scan.
    See docs/semrush-harvest-scheduling.md."""
    try:
        from input.pipeline import domains_lib, collections_lib
        with _db() as conn:
            domains_lib.ensure_domains_table(conn)
            items = domains_lib.harvest_worklist(conn)
        return {"count": len(items), "today": domains_lib._today(),
                "items": [{
                    "domain": d["domain"],
                    "display_name": d.get("display_name") or d["domain"],
                    "status": d.get("harvest_status"),
                    "last_harvested_at": d.get("last_harvested_at"),
                    "next_harvest_at": d.get("next_harvest_at"),
                    "harvest_ttl_days": d.get("harvest_ttl_days"),
                    "semrush_report_url": (d.get("semrush_report_url") or "").strip(),
                    "expected_file": collections_lib.expected_export_name(d['domain']),
                } for d in items]}
    except Exception as e:
        print(f"[ERROR] harvest_worklist failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/semrush-inbox/scan")
def semrush_inbox_scan_endpoint(payload: dict = Body(default={})):
    """Scan the watched inbox for SEMrush exports the curator just saved, route each
    to its domain by the `{domain}` filename prefix, move it into input/, and spawn
    the existing backlinks_file harvest (which stamps last_harvested_at on success →
    the domain rolls off the worklist). The semi-automated half of the loop: the
    human does the SEMrush clicks, this does ingest + bookkeeping."""
    from input.pipeline import domains_lib, collections_lib, system_config as _cfg
    import os
    inbox = (payload.get("inbox_dir") or _cfg.get_setting("semrush_inbox_dir", "")
             or os.path.join(os.path.expanduser("~"), "Downloads"))
    # Scan ONLY the watched inbox — intake MOVES each matched file out to input/, so
    # re-scanning input/ too would re-find and re-process the same export every time.
    dirs = [inbox]
    try:
        with _db() as conn:
            hosts = [d["domain"] for d in domains_lib.list_domains(conn)]
            results = []
            # scan_export_inbox returns newest-first, so the FIRST file per domain is
            # the latest — process that one, skip older duplicates (e.g. a re-download
            # "…pages (1).xlsx" supersedes "…pages.xlsx") so we don't double-ingest.
            handled = set()
            for f in collections_lib.scan_export_inbox(dirs):
                prefix = (f["prefix"] or "").lower()
                # Longest matching host wins (prefix == host, or host followed by a
                # subpath separator) so allrecipes.com_recipe maps to allrecipes.com.
                match = max((h for h in hosts
                             if prefix == h or prefix.startswith(h + "_")
                             or prefix.startswith(h + "-")),
                            key=len, default=None)
                if not match:
                    results.append({"file": os.path.basename(f["path"]),
                                    "matched": None, "skipped": "no domain match"})
                    continue
                if match in handled:
                    results.append({"file": os.path.basename(f["path"]), "matched": match,
                                    "skipped": "superseded by a newer file"})
                    continue
                handled.add(match)
                dest = collections_lib.intake_export_file(f["path"])
                job_id = _spawn_publisher_refresh(conn, match, source="backlinks_file",
                                                  label=match)
                results.append({"file": os.path.basename(dest), "matched": match,
                                "job_id": job_id,
                                "skipped": None if job_id else "already in flight"})
        spawned = [r for r in results if r.get("job_id")]
        return {"inbox": inbox, "found": len(results), "spawned": len(spawned),
                "results": results}
    except Exception as e:
        print(f"[ERROR] semrush_inbox_scan failed: {e}")
        raise HTTPException(status_code=500, detail=f"Scan error: {e}") from e


@app.get("/dish-keywords")
def dish_keywords_status_endpoint(limit: int = 50):
    """The demand-side dish-keyword corpus captured from SEMrush Top-Pages exports —
    traffic-ranked, self-classifying dish names (capture-now; normalize-to-catalog later)."""
    from input.pipeline import dish_keywords
    with _db() as conn:
        dish_keywords.ensure_table(conn)
        total = conn.execute("SELECT COUNT(*) FROM dish_keywords").fetchone()[0]
        by_domain = dict(conn.execute(
            "SELECT domain, COUNT(*) FROM dish_keywords GROUP BY domain "
            "ORDER BY COUNT(*) DESC").fetchall())
        rows = [dict(zip(("keyword", "traffic", "intent", "domain", "url"), r))
                for r in conn.execute(
                    "SELECT keyword, traffic, intent, domain, url FROM dish_keywords "
                    "ORDER BY traffic DESC LIMIT ?", (int(limit),))]
    return {"total": total, "by_domain": by_domain, "top_by_traffic": rows}


@app.get("/dish-keywords/list")
def dish_keywords_list_endpoint(search: str = "", domain: str = "", intent: str = "",
                                sort: str = "traffic", limit: int = 100, offset: int = 0):
    """Searchable/sortable page of the dish-keyword corpus for the browse UI."""
    from input.pipeline import dish_keywords
    where, params = [], []
    if search:
        where.append("(keyword LIKE ? OR url LIKE ?)"); like = f"%{search}%"; params += [like, like]
    if domain:
        where.append("domain = ?"); params.append(domain)
    if intent:
        where.append("intent = ?"); params.append(intent)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order = {"traffic": "traffic DESC", "traffic_pct": "traffic_pct DESC",
             "keyword": "keyword ASC", "domain": "domain ASC, traffic DESC",
             "recent": "captured_at DESC"}.get(sort, "traffic DESC")
    cols = ("url_normalized", "keyword", "traffic", "traffic_pct", "intent",
            "answer_engines", "domain", "url", "captured_at")
    with _db() as conn:
        dish_keywords.ensure_table(conn)
        total = conn.execute(f"SELECT COUNT(*) FROM dish_keywords {clause}", params).fetchone()[0]
        rows = [dict(zip(cols, r)) for r in conn.execute(
            f"SELECT {','.join(cols)} FROM dish_keywords {clause} "
            f"ORDER BY {order} LIMIT ? OFFSET ?", [*params, int(limit), int(offset)])]
        domains = [d for (d,) in conn.execute(
            "SELECT DISTINCT domain FROM dish_keywords ORDER BY domain")]
        intents = [i for (i,) in conn.execute(
            "SELECT DISTINCT intent FROM dish_keywords WHERE intent != '' ORDER BY intent")]
    return {"total": total, "rows": rows, "domains": domains, "intents": intents}


@app.post("/dish-keywords/delete")
def dish_keywords_delete_endpoint(payload: dict = Body(...)):
    """Prune row(s) from the dish-keyword corpus by url_normalized (string or list)."""
    from input.pipeline import dish_keywords
    keys = payload.get("url_normalized")
    keys = [keys] if isinstance(keys, str) else (keys or [])
    keys = [k for k in keys if k]
    if not keys:
        raise HTTPException(status_code=400, detail="url_normalized required")
    with _db() as conn:
        dish_keywords.ensure_table(conn)
        conn.executemany("DELETE FROM dish_keywords WHERE url_normalized = ?", [(k,) for k in keys])
        conn.commit()
    return {"deleted": len(keys)}


@app.get("/url-words")
def url_words_status_endpoint():
    """Counts for the self-learning URL-word lists (the recipe-URL pre-filter vocab)."""
    from input.pipeline import url_word_lists
    with _db() as conn:
        url_word_lists.seed_if_empty(conn)
        rows = conn.execute(
            "SELECT kind, source, COUNT(*) FROM url_word_class GROUP BY kind, source"
        ).fetchall()
        recent = [r[0] for r in conn.execute(
            "SELECT word FROM url_word_class WHERE source='ai' AND kind='food' "
            "ORDER BY created_at DESC LIMIT 25")]
    counts: dict = {}
    for kind, source, n in rows:
        counts.setdefault(kind, {})[source] = n
    return {"counts": counts, "recent_food_learned": recent}


@app.post("/url-words/sweep")
def url_words_sweep_endpoint():
    """Sweep the master corpus' URLs for unknown path tokens, classify the batch in ONE
    Haiku call, and INSERT the results into the two word lists (incremental). The utility
    behind the self-learning recipe-URL pre-filter. Synchronous (one model call)."""
    from input.pipeline import url_word_lists
    import llm  # gateway: journal the classify call. Was missing, so this endpoint
                # raised NameError on every call and returned a 500 — caught by the
                # except below, which made a permanent breakage look like a runtime
                # failure. Every other llm.enter() caller imports it locally the same way.
    try:
        llm.enter(recipe_id=None, user_id=0)   # journal the classify call
        res = url_word_lists.sweep_master_urls(db_path=DB_PATH)
        _journal_usage(None, user_id=0)
        return res
    except Exception as e:
        print(f"[ERROR] url_words_sweep failed: {e}")
        raise HTTPException(status_code=500, detail=f"Sweep error: {e}") from e


@app.get("/domains/{domain}")
def get_domain_endpoint(domain: str):
    try:
        from input.pipeline import domains_lib
        with _db() as conn:
            row = domains_lib.get_domain(conn, domain)
            if row is None:
                raise HTTPException(status_code=404, detail=f"Unknown domain: {domain}")
            return row
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] get_domain({domain!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/domains/{domain}/recipes")
def domain_recipes_endpoint(domain: str):
    """Recipes sourced from this domain, split master vs user, for the
    editor's browse dropdowns. Computed live and stores the two counts back
    on the domain row (refresh-on-open); the lists themselves are not stored."""
    try:
        from input.pipeline import domains_lib
        with _db() as conn:
            return domains_lib.recipes_for_domain(conn, domain)
    except Exception as e:
        print(f"[ERROR] domain_recipes({domain!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


async def _extract_url_to_master_as_editors_choice(url: str, dish_name: str,
                                                   *, note: str = None) -> bool:
    """Ingest ONE curator-awarded URL into master_recipes as `kind='editors_choice'`.

    Deliberately bypasses the whole selection apparatus. There is no Moz score to
    clear, no min-OU filter, no rank: the curator's judgement IS the admission
    criterion, which is the entire point of an award. Scores are still computed
    and stored by the normal save path, so the recipe can be *described* by its
    numbers on screen — it just cannot be excluded by them.

    Sibling of _extract_publisher_url_to_master; kept separate rather than
    parameterised because the two differ in the thing that matters (one competes,
    one does not) and collapsing them invites a later edit that re-applies a gate
    to both.
    """
    from input.pipeline import page_cache
    log_prefix = "[EDITORS-CHOICE]"
    try:
        with page_cache.enabled():
            extract_result = await asyncio.to_thread(
                extract_recipe_from_url, url, user_id=0, force_refresh=True)
    except Exception as e:
        print(f"{log_prefix} EXTRACT-FAIL {url}: {type(e).__name__}: {e}")
        return False
    recipe_dict = (extract_result or {}).get("recipe") or {}
    if not recipe_dict:
        print(f"{log_prefix} EXTRACT-EMPTY {url}")
        return False
    # NON-EMPTY check only — deliberately NOT the normal save gate.
    #
    # The standard gate wants >=3 ingredients and >=3 instructions. That is a
    # reasonable admission test for an algorithmic candidate and the wrong test
    # for an award. Found by testing this feature on the exact recipe it was
    # built for: Adam Liaw's Basic Clear Ramen Broth extracts 11 ingredients
    # (whole old chicken, chicken feet, pork trotters, 9L water) and 2
    # paragraph-length steps carrying more technique than eight one-liners —
    # and the gate rejected it for having 2 instructions instead of 3. Step
    # COUNT is not a quality signal; it measures how an author paragraphs.
    #
    # So we check only that an extraction actually happened. Zero ingredients or
    # zero steps means we read a paywall stub or a failed render — a fetch
    # problem, which is a real reason to refuse. Anything above that is the
    # curator's call, which is what an award means.
    def _non_empty(rec):
        ings = rec.get("recipeIngredient") or []
        steps = rec.get("recipeInstructions") or []
        if not ings:
            return False, "no ingredients extracted (likely a paywall stub or failed render)"
        if not steps:
            return False, "no instructions extracted (likely a paywall stub or failed render)"
        return True, ""
    ok, reason = _non_empty(recipe_dict)
    if not ok:
        _worth, _why_not = _render_retry_would_help(url)
        if not _worth:
            print(f"{log_prefix} EMPTY ({reason}) — SKIPPING render-retry: {_why_not}  {url}")
            print(f"{log_prefix} SKIP-EMPTY {reason}  {url}")
            return False
        print(f"{log_prefix} EMPTY ({reason}) — render-retry {url}")
        try:
            with page_cache.enabled():
                extract_result = await asyncio.to_thread(
                    extract_recipe_from_url, url, user_id=0,
                    force_refresh=True, fetch_render=True)
            recipe_dict = (extract_result or {}).get("recipe") or {}
            ok, reason = _non_empty(recipe_dict) if recipe_dict else (False, "empty recipe")
        except Exception as e:
            print(f"{log_prefix} render-retry failed {url}: {type(e).__name__}: {e}")
    if not ok:
        print(f"{log_prefix} SKIP-EMPTY {reason}  {url}")
        return False
    payload = dict(recipe_dict)
    payload["recipe_id"] = extract_result.get("recipe_id") or recipe_dict.get("id")
    payload["user_id"] = 0
    payload["_master"] = {
        "kind": "editors_choice",
        "dish": dish_name,          # belongs to the cohort; exempt from its ranking
        "awarded_at": datetime.now(timezone.utc).isoformat(),
        "note": note,
        "batch_source": "editors-choice",
    }
    payload["_skip_auto_enrich"] = True
    # The save-quality gate has a documented bypass — the same `force_save` the
    # form's "Save anyway" dialog sends when a curator overrides it by hand. An
    # award IS that override, made deliberately and recorded, so it sets the flag
    # rather than being refused by a floor it is exempt from by definition.
    # Without this, Adam Liaw's broth (11 ingredients, 2 dense steps) is rejected
    # at 422 — the gate counts steps, and he writes paragraphs.
    payload["force_save"] = True
    try:
        await asyncio.to_thread(_save_recipe_core, payload)
        print(f"{log_prefix} AWARDED {dish_name!r} <- {url}")
        return True
    except HTTPException as e:
        print(f"{log_prefix} SAVE-FAIL {url}: {e.status_code} {e.detail}")
    except Exception as e:
        print(f"{log_prefix} SAVE-FAIL {url}: {type(e).__name__}: {e}")
    return False


async def _extract_publisher_url_to_master(url: str, host: str, rank: int,
                                           batch_source: str, log_prefix: str = "[INGEST]",
                                           *, traffic=None, traffic_pct=None) -> bool:
    """Extract ONE publisher URL and save it to master_recipes as a kind='top' member.
    SHARED by the harvest's winner auto-extract AND the score-only 'process selected' path
    so the two never drift: extract (force-refresh) → save-gate → render-retry-on-thin →
    `_save_recipe_core` with the `_master` top/publisher block. Honors the domain's
    fetch policy (unblocker/render) via extract_recipe_from_url. Returns True iff saved.
    Ledger lifecycle (retire / re-flag selected) stays with the caller."""
    from input.pipeline import page_cache
    try:
        # REVALIDATE: reuse the cached FINISHED recipe only when the SOURCE is unchanged
        # (raw-source fingerprint compare) — so a re-harvest of an unchanged URL costs NO
        # LLM and NO fetch, while a URL whose recipe actually changed at the source gets a
        # fresh extract. The page_cache (enabled here) shares the one fetch with the
        # is-recipe filter, so the compare is ~free. The thin→render retry below still
        # forces a fresh render to get past a thin/partial cached extract.
        with page_cache.enabled():
            extract_result = await asyncio.to_thread(
                extract_recipe_from_url, url, user_id=0, revalidate=True)
    except Exception as e:
        print(f"{log_prefix} EXTRACT-MISS {url}: {type(e).__name__}: {e}")
        return False
    recipe_dict = (extract_result or {}).get("recipe") or {}
    ok, reason = _is_cacheable(recipe_dict, min_ings=SAVE_GATE_MIN_INGREDIENTS,
                               min_steps=SAVE_GATE_MIN_INSTRUCTIONS) if recipe_dict else (False, "empty recipe")
    # RENDER-RETRY-ON-THIN: a JS publisher can render PARTIALLY (the recipe card loads
    # late → empty/thin extract). Retry ONCE forcing a full-browser render before giving up.
    if not ok:
        _worth, _why_not = _render_retry_would_help(url)
        if not _worth:
            print(f"{log_prefix} THIN ({reason}) — SKIPPING render-retry: {_why_not}  {url}")
            print(f"{log_prefix} SKIP-THIN {reason}  {url}")
            return False
        print(f"{log_prefix} THIN ({reason}) — render-retry {url}")
        try:
            with page_cache.enabled():
                extract_result = await asyncio.to_thread(
                    extract_recipe_from_url, url, user_id=0, force_refresh=True, fetch_render=True)
            recipe_dict = (extract_result or {}).get("recipe") or {}
            ok, reason = _is_cacheable(recipe_dict, min_ings=SAVE_GATE_MIN_INGREDIENTS,
                                       min_steps=SAVE_GATE_MIN_INSTRUCTIONS) if recipe_dict else (False, "empty recipe")
        except Exception as e:
            print(f"{log_prefix} render-retry failed {url}: {type(e).__name__}: {e}")
    if not ok:
        print(f"{log_prefix} SKIP-THIN {reason}  {url}")
        return False
    payload = dict(recipe_dict)
    payload["recipe_id"] = extract_result.get("recipe_id") or recipe_dict.get("id")
    payload["user_id"] = 0
    # publisher recipes carry no _master.dish, so they never leak into a dish's top-N.
    payload["_master"] = {
        "kind": "top", "publisher": host,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
        "rank": rank, "batch_source": batch_source,
    }
    payload["_skip_auto_enrich"] = True   # fast/cheap; enrich later
    # Stamp SEMrush per-page traffic into _scoring so the recipe editor's scoring strip shows
    # it alongside PA/DA/OU. Passed by the harvest (the scored member carries it); for the
    # process-selected path it's looked up from the just-persisted ledger.
    if traffic is None and traffic_pct is None:
        try:
            from input.pipeline.url_utils import normalize_url as _norm
            with _db() as _c:
                _row = _c.execute(
                    "SELECT traffic, traffic_pct FROM collection_members WHERE "
                    "collection_type='publisher' AND collection_key=? AND url_normalized=?",
                    (host, _norm(url) or url)).fetchone()
            if _row:
                traffic, traffic_pct = _row[0], _row[1]
        except Exception:
            pass
    if traffic is not None or traffic_pct is not None:
        _sc = dict(payload.get("_scoring") or {})
        if traffic is not None:
            _sc["traffic"] = traffic
        if traffic_pct is not None:
            _sc["trafficPct"] = traffic_pct
        payload["_scoring"] = _sc
    try:
        await asyncio.to_thread(_save_recipe_core, payload)
        print(f"{log_prefix} SAVED master {url}")
        return True
    except HTTPException as e:
        print(f"{log_prefix} SAVE-FAIL {url}: {e.status_code} {e.detail}")
    except Exception as e:
        print(f"{log_prefix} SAVE-FAIL {url}: {type(e).__name__}: {e}")
    return False


async def _handle_publisher_refresh_job(job: dict) -> dict:
    """Publisher refresh, OUT-OF-PROCESS: run the verbatim query, verify recipes,
    Moz-score, keep top-N, store as publisher collection membership + persist the
    publisher's harvest config. The fetch-each-candidate recipe check makes this a
    1-2 min job (too slow inline), so it's the dish-refresh class of work. Logs each
    candidate (KEEP/DROP) via print → the job log → the form's live stream."""
    from input.pipeline import domains_lib, collections_lib
    p = job.get("params") or {}
    host = p["host"]
    keep = int(p.get("keep") or 10)
    pages = max(1, int(p.get("pages") or 4))
    query = (p.get("query") or "").strip() or None
    recipe_path = (p.get("recipe_path") or "").strip() or None
    check_recipe = bool(p.get("check_recipe", True))
    source = (p.get("source") or "serp").strip() or "serp"
    records = int(p.get("records") or 0) or None   # file-source extract count
    backlinks_dir = (p.get("backlinks_dir") or "").strip() or None  # per-domain export folder override
    exclude_words = p.get("exclude_words") or ""   # per-domain exclusionary section words
    unblocker = bool(p.get("unblocker"))           # fetch_strategy='unblocker' → live paid fetch
    score_only = bool(p.get("score_only"))         # Moz-rank only, no fetch/verify/ingest (curation)
    job_id = job.get("id")
    print(f"[PUBLISHER-REFRESH] {host} | source={source} query={query!r} pages={pages} "
          f"records={records} keep={keep} check_recipe={check_recipe} unblocker={unblocker} "
          f"score_only={score_only}")

    def _should_cancel():
        # Cross-process poll: the server sets cancel_requested; we (the out-of-process
        # job) see it via WAL between candidates and abort gracefully.
        try:
            with sqlite3.connect(DB_PATH, timeout=5) as conn:
                return jobs_lib.is_cancel_requested(conn, job_id)
        except Exception:
            return False

    def _work():
        from input.pipeline import page_cache
        # Cache each candidate's fetched page so the winner-extract below reuses it
        # (shared via page_cache.db) instead of fetching every page a second time.
        with page_cache.enabled():
            res = collections_lib.harvest_publisher_top(
                host, keep=keep, discover_n=pages * 10,
                recipe_path=recipe_path, query=query, check_recipe=check_recipe,
                source=source, records=records, unblocker=unblocker,
                backlinks_dir=backlinks_dir,
                exclude_words=exclude_words, should_cancel=_should_cancel, score_only=score_only)
        with _db() as conn:
            from input.pipeline import domains_lib
            domains_lib.ensure_domains_table(conn)  # self-heal: guarantee harvest_source col exists
            collections_lib.replace_members(conn, "publisher", host, res["members"])
            # Candidate ledger — the pre-scoring drops, which replace_members above
            # never sees (it only ever receives what reached Moz).
            try:
                from input.pipeline import candidate_ledger
                _led = candidate_ledger.build_publisher_rows(
                    res, collection_key=host, job_id=job.get("id"),
                    run_started_at=(job.get("started_at")
                                    or datetime.now(timezone.utc).isoformat()))
                _n = candidate_ledger.record_run(conn, _led)
                _nm = sum(1 for r in _led if r["outcome"] == "dropped" and r["overturnable"])
                print(f"[PUBLISHER-REFRESH] candidate ledger: {_n} row(s) "
                      f"{candidate_ledger.run_summary(conn, job.get('id'))} "
                      f"— {_nm} reconsiderable by the editor")
            except Exception as e:
                print(f"[PUBLISHER-REFRESH] candidate ledger failed (non-fatal): {e}")
            # A LEARNED recipe_path is only trustworthy if the run it was learned
            # from actually found recipes. When recipe_pass is 0, the auto-detect
            # inferred the path from a sample in which NOTHING was a recipe — it is
            # inferring from the failure itself.
            #
            # 2026-08-03: three publishers were harvested off SERP with no
            # serp_query set. Discovery returned only the sites' archive pages, and
            # the run persisted recipegirl.com -> 'set', marionskitchen.com ->
            # 'category', paleogrubs.com -> 'tag'. `category` and `tag` are
            # WordPress taxonomy prefixes; those domains would have scoped every
            # future harvest to their own archives forever. The recipes are at the
            # root (recipegirl.com/pastitsio-greek-lasagna). Keep whatever the
            # curator had rather than overwrite it with a guess from a failed run.
            _learned_path = res.get("recipe_path") or ""
            if res["recipe_pass"]:
                conn.execute(
                    "UPDATE domains SET recipe_path = ?, keep_top_n = ?, serp_query = ?, "
                    "search_pages = ?, harvest_source = ? WHERE domain = ?",
                    (_learned_path, keep, query or "", pages, source, host))
            else:
                if _learned_path:
                    print(f"[PUBLISHER-REFRESH] NOT learning recipe_path={_learned_path!r} — "
                          f"0 of {res['discovered']} discovered URLs were recipes")
                conn.execute(
                    "UPDATE domains SET keep_top_n = ?, serp_query = ?, "
                    "search_pages = ?, harvest_source = ? WHERE domain = ?",
                    (keep, query or "", pages, source, host))
            conn.commit()
            # SEMrush human-workflow loop: a successful backlinks_file ingest is a
            # completed harvest → stamp it so the derived next_harvest_at rolls
            # forward and the domain drops off the "Due today" worklist. Only the
            # file source counts as a scheduled harvest (the SERP refresh is a
            # different, ad-hoc mechanism). See docs/semrush-harvest-scheduling.md.
            if source == "backlinks_file":
                domains_lib.mark_harvested(conn, host)
            # Roll the LLM cascade's fresh poor_quality verdicts up per domain and
            # (re)flag poor publishers, so future harvests stop paying the per-page LLM
            # cascade for a known messy source. Best-effort; scans training.db.
            try:
                pp = domains_lib.refresh_poor_publisher_flags(conn)
                if pp.get("flagged"):
                    print(f"[PUBLISHER-REFRESH] poor-publisher flags: {pp['flagged']}")
            except Exception as _ex:
                print(f"[PUBLISHER-REFRESH] poor-publisher refresh skipped: "
                      f"{type(_ex).__name__}: {_ex}")
        return res

    res = await asyncio.to_thread(_work)
    print(f"[PUBLISHER-REFRESH] done — discovered={res['discovered']} "
          f"recipe_pass={res['recipe_pass']} scored={res['scored']} stored={len(res['members'])}")

    # SCORE-ONLY: stop here — no fetch-verify happened and we deliberately ingest NOTHING.
    # The scored candidates are now in the ledger (selected=0); the curator picks winners
    # in the cohort view and ingests just those via POST /domains/{host}/process-selected.
    if score_only:
        print(f"[PUBLISHER-REFRESH] score-only — {len(res['members'])} candidates scored & "
              f"stored, 0 fetched/ingested. Select winners → Process selected.")
        return {"discovered": res["discovered"], "recipe_pass": res["recipe_pass"],
                "scored": res["scored"], "stored": len(res["members"]),
                "extracted": 0, "score_only": True, "recipe_path": res.get("recipe_path")}

    # ---- ledger -> master: AUTO-EXTRACT the selected winners (mirrors dish refresh) ----
    # The harvest above only built the ranked LEDGER (collection_members). This is the
    # step the dish batch has but the publisher harvest lacked: extract the top-N winners
    # into master_recipes (+ the extract cache, as a byproduct), backfilling from the
    # next-ranked also-rans when a winner fails extract/save-gate ("ensure keep if
    # available"). The 73 also-rans stay index-only in the ledger. The extract fetch is
    # now domain-fetch-policy-aware, so JS-rendered/anti-bot winners render correctly.
    members = res.get("members") or []
    winners = sorted([m for m in members if m.get("selected")], key=lambda m: m.get("rank") or 9999)
    reserve = sorted([m for m in members if not m.get("selected")], key=lambda m: m.get("rank") or 9999)
    pool = winners + reserve
    now_iso = datetime.now(timezone.utc).isoformat()
    # Typed-block delete-and-replace: clear THIS publisher's domain block up front
    # (drop the row only if it has no dish block — the inline refcount). The extract
    # loop below re-adds the block for the current winners; a dropped-out winner that
    # is ALSO a dish winner is kept (dish-only). No orphans, no GC.
    try:
        with _db() as _pc:
            try:
                from input.pipeline import vector_store as _vs
                _vs.enable_vec(_pc)   # so the AFTER DELETE trigger can clean vectors
            except Exception:
                pass
            from input.pipeline import dishes as _dishes_lib
            cl, dl = _dishes_lib.retire_master_membership(
                _pc, marker="publisher", value=host, other_marker="dish",
                remove_fields=["publisher", "refreshed_at"])
        if cl or dl:
            print(f"[PUBLISHER-REFRESH] retired prior domain block: cleared {cl} (kept as dish), deleted {dl}")
    except Exception as e:
        print(f"[PUBLISHER-REFRESH] domain-block retire failed: {type(e).__name__}: {e}")
    extracted = 0
    attempted = 0        # R3: extractions TRIED — the denominator obtainability is judged on
    saved_urls: list[str] = []
    for m in pool:
        if extracted >= keep:
            break
        if _should_cancel():
            print("[PUBLISHER-REFRESH] cancel requested — stopping winner extraction")
            break
        url = m.get("url")
        if not url:
            continue
        # Seq tag on the winner-extract log lines — [winner-slot / top-N kept], mirroring
        # the candidate loop's [N/120] format so the log shows which selected winner is
        # being processed and how many were selected.
        seq_prefix = f"[PUBLISHER-REFRESH] [{extracted + 1}/{keep}]"
        attempted += 1
        if await _extract_publisher_url_to_master(
                url, host, extracted + 1, "/domains/refresh-top", seq_prefix,
                traffic=m.get("traffic"), traffic_pct=m.get("traffic_pct")):
            extracted += 1
            saved_urls.append(url)
    print(f"[PUBLISHER-REFRESH] extracted {extracted} winner(s) into master (+cache)")

    # Re-flag the ledger so selected=1 matches the ACTUALLY-saved winners: backfill
    # may have swapped a failed top-N winner (roundup / thin / paywalled) for a
    # lower-ranked reserve, so the pre-extract selection can be stale. Mirrors the
    # dish refresh re-flagging dish_run_data_points after backfill — keeps the form's
    # ★ winners ⟺ the recipes actually in master.
    if saved_urls:
        try:
            from input.pipeline.url_utils import normalize_url as _norm
            keys = {(_norm(u) or u) for u in saved_urls}
            with _db() as _rc:
                _rc.execute("UPDATE collection_members SET selected = 0 "
                            "WHERE collection_type='publisher' AND collection_key = ?", (host,))
                for k in keys:
                    _rc.execute("UPDATE collection_members SET selected = 1 "
                                "WHERE collection_type='publisher' AND collection_key = ? "
                                "AND url_normalized = ?", (host, k))
                _rc.commit()
            print(f"[PUBLISHER-REFRESH] re-flagged {len(keys)} ledger winner(s) to match master")
        except Exception as e:
            print(f"[PUBLISHER-REFRESH] re-flag selected failed: {type(e).__name__}: {e}")

    # R3: learn whether this publisher's recipes can actually be OBTAINED, from
    # what this run just did. `paywall` says whether it is gated (a business
    # fact); this says whether we can get the content (a technical one). They are
    # orthogonal — cooking.nytimes.com and 177milkstreet.com are both gated and
    # cost 1.1 vs 54 unblocker calls per save — and only the second should drive
    # spending. Measured, so ATK and Milk Street differ by evidence rather than by
    # a hand-written exception.
    try:
        with _db() as _oc:
            _rr = bool((domains_lib.get_domain(_oc, host) or {}).get("render_required"))
            _method = ("unblocker_render" if (unblocker and _rr) else
                       "unblocker" if unblocker else "direct")
            _obt = domains_lib.record_acquisition_outcome(
                _oc, host, attempted=attempted, saved=extracted, method=_method)
        print(f"[PUBLISHER-REFRESH] obtainability: {host} -> "
              f"{_obt['content_obtainable']} ({_obt['note']})")
        if _obt.get("changed") and _obt["content_obtainable"] == "never":
            print(f"[PUBLISHER-REFRESH] {host} marked NEVER — future runs will score "
                  f"without fetching. Capture its recipes with the bookmarklet.")
    except Exception as e:
        print(f"[PUBLISHER-REFRESH] obtainability record skipped: {type(e).__name__}: {e}")

    # Recalibrate the paywall DA-adjustment when a GATED publisher just gained
    # rows. The refresh is the event that changes the evidence — a monthly timer
    # is the wrong trigger, because a publisher can sit at `no_rows` or
    # `low_confidence` for weeks after the harvest that would have settled it.
    # Cheap enough to run inline: the corpus is the sample, so there is no Moz
    # or SERP spend, and restamp only rewrites rows whose value actually moved.
    _recal = None
    try:
        with _db() as _pc:
            if (_pc.execute("SELECT paywall FROM domains WHERE domain = ?",
                            (host,)).fetchone() or [0])[0]:
                from input.pipeline import paywall_calibration
                _out = paywall_calibration.calibrate(_pc, persist=True)
                _mine = next((r for r in _out["results"] if r.get("domain") == host), {})
                _recal = {"status": _mine.get("status"),
                          "discount_pct": _mine.get("discount_pct"),
                          "restamped": _out.get("restamped")}
                print(f"[PUBLISHER-REFRESH] paywall recalibrated: {host} -> "
                      f"{_mine.get('status')}"
                      + (f" ({_mine['discount_pct']}%)" if _mine.get("discount_pct") else "")
                      + f" | {_mine.get('note', '')}")
    except Exception as e:
        # Never fail a completed harvest over the calibration step.
        print(f"[PUBLISHER-REFRESH] paywall recalibration skipped: {type(e).__name__}: {e}")

    return {"discovered": res["discovered"], "recipe_pass": res["recipe_pass"],
            "scored": res["scored"], "stored": len(res["members"]),
            "extracted": extracted, "recipe_path": res.get("recipe_path"),
            "paywall_recalibration": _recal}


jobs_lib.register_handler("publisher_refresh", _handle_publisher_refresh_job)


async def _handle_process_selected_job(job: dict) -> dict:
    """Score-only path #1: ingest a curator-SELECTED set of this publisher's URLs into
    master (via the unblocker, per the domain's fetch policy), OUT-OF-PROCESS. Uses the
    SHARED per-URL extract→master helper (same as the harvest winner-extract), APPENDS
    (does NOT retire the publisher's existing block — this is incremental curation), and
    marks each saved URL selected=1 in the ledger so the worklist reflects it. The cheap
    half (Moz ranking) already ran in the score-only harvest; this pays a render only for
    the URLs the human chose. See docs/score-only-curation.md."""
    p = job.get("params") or {}
    host = (p.get("host") or "").strip()
    urls = [u for u in (p.get("urls") or []) if u]
    job_id = job.get("id")
    if not host or not urls:
        raise ValueError("process_selected requires params.host + non-empty params.urls")
    print(f"[PROCESS-SELECTED] {host} | {len(urls)} selected URL(s)")

    def _should_cancel():
        try:
            with sqlite3.connect(DB_PATH, timeout=5) as conn:
                return jobs_lib.is_cancel_requested(conn, job_id)
        except Exception:
            return False

    from input.pipeline.url_utils import normalize_url as _norm
    saved_urls: list[str] = []
    for i, url in enumerate(urls, 1):
        if _should_cancel():
            print("[PROCESS-SELECTED] cancel requested — stopping")
            break
        print(f"[PROCESS-SELECTED] [{i}/{len(urls)}] {url}")
        if await _extract_publisher_url_to_master(
                url, host, i, "/domains/process-selected", "[PROCESS-SELECTED]"):
            saved_urls.append(url)
    # Mark the ACTUALLY-saved URLs selected=1 (append; leave the rest of the ledger as-is).
    if saved_urls:
        try:
            with _db() as conn:
                for u in saved_urls:
                    conn.execute(
                        "UPDATE collection_members SET selected = 1 WHERE "
                        "collection_type='publisher' AND collection_key=? AND url_normalized=?",
                        (host, _norm(u) or u))
                conn.commit()
        except Exception as e:
            print(f"[PROCESS-SELECTED] ledger re-flag failed: {type(e).__name__}: {e}")
    print(f"[PROCESS-SELECTED] done — ingested {len(saved_urls)}/{len(urls)} into master")
    return {"host": host, "requested": len(urls), "ingested": len(saved_urls)}


jobs_lib.register_handler("process_selected", _handle_process_selected_job)


@app.post("/domains/{domain}/process-selected")
def process_selected_endpoint(domain: str, payload: dict = Body(...)):
    """Score-only path #1 — ingest a curator-selected set of this publisher's URLs into
    master (via the unblocker), OUT-OF-PROCESS (renders are slow). payload {urls:[...]}.
    Returns job id + stream url; in-flight-deduped. See docs/score-only-curation.md."""
    from input.pipeline import domains_lib
    host = domains_lib._canon_host(domain)
    raw = payload.get("urls") or []
    urls = [u for u in (raw if isinstance(raw, list) else [raw]) if u]
    if not urls:
        raise HTTPException(status_code=400, detail="No URLs selected to process.")
    # R4 — refuse at the point money is spent. This endpoint drives ONE paid render
    # per URL; on a human-capture-only publisher every one of them buys a paywall
    # notice. The UI already steers away from the button, but a guard that lives
    # only in the UI is not a guard — it is a suggestion that a stale page, a
    # second tab or a direct POST walks straight past.
    with _db() as conn:
        if domains_lib.human_capture_only(conn, host):
            raise HTTPException(status_code=409, detail=(
                f"{host} is marked HUMAN CAPTURE ONLY: the server cannot obtain its "
                f"recipe bodies at any price, so this would spend {len(urls)} render(s) "
                f"to reach a paywall notice. Use 📋 Queue (manual) instead — open the "
                f"pages and click your bookmarklet; your browser is signed in and ours "
                f"is not. To override, "
                f"clear 'Human capture only' on the domain record."))
    with _db() as conn:
        entity_ref = f"process-selected:{host}"
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return JSONResponse(status_code=409, content={
                "error": "already in flight", "job_id": existing["id"],
                "status": existing["status"], "stream_url": f"/jobs/{existing['id']}/stream"})
        job_id = jobs_lib.enqueue_job(
            conn, type="process_selected",
            params={"host": host, "urls": urls, "log_label": f"{host} (selected ×{len(urls)})"},
            entity_ref=entity_ref)
    import subprocess
    proj = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUNBUFFERED"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jobs", "exec", "--job-id", str(job_id)],
            cwd=proj, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_detached_flags())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to spawn process-selected job: {e}") from e
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "queued", "domain": host, "count": len(urls),
        "stream_url": f"/jobs/{job_id}/stream", "status_url": f"/jobs/{job_id}"})


# --------------------------------------------------------------------------- #
# REMOVED 2026-08-13 — the Tampermonkey "userscript capture queue" (score-only
# path #2): endpoints, job type, log helper and the browser-capture save path.
# Three runs over seven weeks (jobs 358, 825, 826) captured ZERO recipes; it
# never worked once. Worse than inert: it reported "Userscript launched" when
# the pop-up had been blocked, and left a 'running' job no process could cancel,
# holding the publisher's entity lock.
#
# The job is done by the MANUAL queue on the domains page (open the pages, click
# the bookmarklet), which works and now targets master explicitly via the
# `_bcc_master` hint. Gated publishers are a human workflow by design — R4 /
# domains.human_capture_only.
#
# Kept, because they earned their place elsewhere:
#   to_markdown.markdown_from_html    — HTML we already hold -> canonical markdown
#   to_markdown.jsonld_declares_gated + meta-tag JSON-LD reading (R8, in the harvest)
# --------------------------------------------------------------------------- #
_LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


@app.post("/domains/{domain}/refresh-top")
def refresh_domain_top_endpoint(domain: str, payload: dict = Body(default={})):
    """Publisher refresh — the domains-page analog of a dish refresh. Validates +
    enqueues a `publisher_refresh` job and spawns it OUT-OF-PROCESS (the recipe
    check fetches each candidate → 1-2 min, too slow inline + survives a restart),
    returning the job id + SSE stream url. The form tails the log and re-loads the
    stored top list (GET /domains/{domain}/top) on completion."""
    from input.pipeline import domains_lib, collections_lib
    host = domains_lib._canon_host(domain)
    with _db() as conn:
        row = domains_lib.get_domain(conn, host) or {}
        # Discovery source: 'serp' (Google) or 'backlinks_file' (a local SEMrush Top-Pages
        # export — name kept for back-compat; the reader auto-detects backlinks-pages too).
        # The file IS the mechanism, so it bypasses the harvestable/query gates; everything
        # downstream (incl. the recipe filter) is identical to the SERP path.
        # Default to the domain's STORED harvest_source (a bare API refresh shouldn't
        # silently run Google when the domain is configured for the file); the UI always
        # sends `source` explicitly. Fall back to 'serp' only when neither is set.
        source = (payload.get("source") or row.get("harvest_source") or "serp").strip() or "serp"
        records = int(payload.get("records") or 0) or None
        # VERBATIM Google query (payload or stored serp_query) runs as-is, overrides path.
        query = (payload.get("query") or row.get("serp_query") or "").strip() or None
        backlinks_dir = (payload.get("backlinks_dir") or row.get("backlinks_dir") or "").strip() or None
        exclude_words = payload.get("exclude_words")
        if exclude_words is None:
            exclude_words = row.get("exclude_words") or ""
        if source == "backlinks_file":
            if not collections_lib.backlinks_file_path(host, extra_dir=backlinks_dir):
                searched = collections_lib.backlinks_search_dirs(backlinks_dir)
                raise HTTPException(
                    status_code=400,
                    detail=collections_lib.export_not_found_message(host, searched, override=backlinks_dir))
        else:
            harvestable = payload.get("harvestable")
            harvestable = int(row.get("harvestable", 1)) if harvestable is None else (1 if harvestable else 0)
            if not harvestable:
                raise HTTPException(status_code=400,
                                    detail="Publisher marked unharvestable (no mechanical recipe access). "
                                           "Uncheck 'skip' to refresh.")
        keep = int(payload.get("keep") or row.get("keep_top_n") or 10)
        recipe_path = (payload.get("recipe_path") or row.get("recipe_path") or "").strip() or None
        # Trusted publishers: a paywall=1 (gated premium) publisher's recipe pages
        # fetch as stubs with no recipe body / JSON-LD, so the fetch-VERIFY wrongly
        # rejects them. Treat them as TRUSTED — keep by URL/path, skip the verify.
        # The title-based archive + collection/listicle filters still run in the
        # harvest pre-filter, and og:image still captures from the public gate page.
        # An explicit payload check_recipe overrides (e.g. to spot-audit a paywall site).
        paywalled = bool(int(row.get("paywall", 0) or 0))
        check_recipe = bool(payload.get("check_recipe", not paywalled))
        # Score-only: Moz-rank the URLs (zero renders) and ingest NOTHING — the curator
        # selects winners from the scored list and processes just those. See
        # docs/score-only-curation.md.
        score_only = bool(payload.get("score_only"))
        # Depth = per-request → per-publisher (domains.search_pages) → system default
        # (system_config 'serp_default_pages', admin-editable). No hard 10 cap now
        # (Scale SERP page-loops); each page is 1 credit + (verify) 1 fetch.
        from input.pipeline import system_config as _cfg
        pages = max(1, int(payload.get("pages") or row.get("search_pages")
                           or _cfg.get_setting("serp_default_pages", 10)))
        entity_ref = f"publisher:{host}"
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return JSONResponse(status_code=409, content={
                "error": "already in flight", "job_id": existing["id"],
                "status": existing["status"], "stream_url": f"/jobs/{existing['id']}/stream"})
        job_id = jobs_lib.enqueue_job(
            conn, type="publisher_refresh",
            params={"host": host, "keep": keep, "pages": pages, "query": query,
                    "recipe_path": recipe_path, "check_recipe": check_recipe,
                    "source": source, "records": records, "log_label": host,
                    "backlinks_dir": backlinks_dir,
                    "exclude_words": exclude_words, "score_only": score_only,
                    "unblocker": ((row.get("fetch_strategy") or "") == "unblocker")},
            entity_ref=entity_ref)
    import subprocess
    proj = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUNBUFFERED"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jobs", "exec", "--job-id", str(job_id)],
            cwd=proj, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_detached_flags())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to spawn refresh job: {e}") from e
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "queued", "domain": host,
        "stream_url": f"/jobs/{job_id}/stream", "status_url": f"/jobs/{job_id}"})


@app.delete("/domains/{domain}/top")
def clear_domain_top_endpoint(domain: str):
    """Wipe a publisher's stored top-N list (the junction members only) — for a
    botched harvest the curator wants to throw away and rebuild."""
    from input.pipeline import domains_lib, collections_lib
    try:
        host = domains_lib._canon_host(domain)
        with _db() as conn:
            n = collections_lib.clear_members(conn, "publisher", host)
        return {"domain": host, "cleared": n}
    except Exception as e:
        print(f"[ERROR] clear_domain_top({domain!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Clear error: {e}") from e


@app.get("/domains/{domain}/top")
def domain_top_endpoint(domain: str, all: int = 0):
    """Stored publisher ledger (collection_members) for the domains page. Default =
    selected winners only; `?all=1` returns the FULL scored cohort (winners + also-rans,
    each with rank/score/selected) for the cohort panel — like the dishes scored cohort."""
    from input.pipeline import domains_lib, collections_lib
    try:
        host = domains_lib._canon_host(domain)
        want_all = bool(int(all or 0))
        with _db() as conn:
            top = collections_lib.get_collection_top(
                conn, "publisher", host,
                limit=400 if want_all else 100, selected_only=not want_all)
        return {"domain": host, "count": len(top),
                "selected_count": sum(1 for r in top if r.get("selected")), "top": top}
    except Exception as e:
        print(f"[ERROR] domain_top({domain!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/semrush-ranks")
def semrush_ranks_status_endpoint():
    """Per-region summary of the imported SEMrush Rank reference data (rows, file
    date, last import) — for the domains-page ranks tools."""
    from input.pipeline import semrush_ranks
    try:
        with _db() as conn:
            return {"regions": semrush_ranks.region_stats(conn)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/semrush-ranks/refresh")
def refresh_semrush_ranks_endpoint(payload: dict = Body(default={})):
    """Re-import the newest SEMrush Rank export (input/ ∪ ~/Downloads) and re-stamp
    every domain's semrush_rank — OUT-OF-PROCESS (mirrors the publisher refresh).
    The export is MANUAL (no API), so this re-imports whatever fresh file the admin
    dropped; it never downloads. Same job the monthly scheduler runs. Optional
    payload: region (override the filename-detected region)."""
    region = (payload.get("region") or "").strip() or None
    with _db() as conn:
        entity_ref = "semrush_ranks_refresh"
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return JSONResponse(status_code=409, content={
                "error": "already in flight", "job_id": existing["id"],
                "status": existing["status"], "stream_url": f"/jobs/{existing['id']}/stream"})
        params = {"log_label": "semrush_ranks"}
        if region:
            params["region"] = region
        job_id = jobs_lib.enqueue_job(conn, type="semrush_ranks_refresh",
                                      params=params, entity_ref=entity_ref)
    import subprocess
    proj = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUNBUFFERED"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jobs", "exec", "--job-id", str(job_id)],
            cwd=proj, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_detached_flags())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to spawn ranks refresh job: {e}") from e
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "queued",
        "stream_url": f"/jobs/{job_id}/stream", "status_url": f"/jobs/{job_id}"})


@app.post("/domains/rescore")
def rescore_domains_endpoint(payload: dict = Body(default={})):
    """Recompute the SYSTEM-WIDE domain score — OUT-OF-PROCESS (mirrors the publisher
    refresh / ranks refresh). Refits one global PA~DA quadratic over the whole corpus
    and rescores every publisher member's rank_score against it (cross-publisher-
    comparable authority, not raw PA). Same job the weekly scheduler runs. No payload
    needed."""
    with _db() as conn:
        entity_ref = "domain_scoring"
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return JSONResponse(status_code=409, content={
                "error": "already in flight", "job_id": existing["id"],
                "status": existing["status"], "stream_url": f"/jobs/{existing['id']}/stream"})
        job_id = jobs_lib.enqueue_job(conn, type="domain_scoring",
                                      params={"log_label": "domain_scoring"},
                                      entity_ref=entity_ref)
    import subprocess
    proj = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUNBUFFERED"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jobs", "exec", "--job-id", str(job_id)],
            cwd=proj, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_detached_flags())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to spawn domain scoring job: {e}") from e
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "queued",
        "stream_url": f"/jobs/{job_id}/stream", "status_url": f"/jobs/{job_id}"})


@app.get("/collections/leaderboard")
def collections_leaderboard_endpoint(limit: int = 50, selected_only: bool = True):
    """The 'best recipes anywhere' payoff of system-wide scoring: publisher members
    across ALL collections, ordered by the cross-publisher-comparable rank_score.
    `selected_only` (default) restricts to each publisher's kept top-N. LEFT-JOINs
    master_recipes so an ingested recipe shows its real name/grade/thumbnail."""
    limit = max(1, min(int(limit or 50), 500))
    sel = " AND cm.selected = 1" if selected_only else ""
    with _db() as conn:
        from input.pipeline import domain_scoring
        fit = domain_scoring.get_global_fit(conn)
        rows = conn.execute(
            f"""
            SELECT cm.collection_key, cm.url_normalized, cm.title, cm.da, cm.pa,
                   cm.adjusted_pa, cm.rank_score, cm.rank, cm.selected,
                   m.recipe_id, json_extract(m.data, '$.name'),
                   json_extract(m.data, '$._master.exceptionalism.grade'),
                   COALESCE(NULLIF(json_extract(m.data, '$._source.previewImage'), ''),
                            NULLIF(json_extract(m.data, '$.image[0]'), ''),
                            NULLIF(cm.image_url, ''))
            FROM collection_members cm
            LEFT JOIN master_recipes m
              ON m.url_normalized = cm.url_normalized AND m.user_id = 0
            WHERE cm.collection_type = 'publisher' AND cm.rank_score IS NOT NULL{sel}
            ORDER BY cm.rank_score DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    cols = ["publisher", "url", "ledger_title", "da", "pa", "adjusted_pa",
            "rank_score", "rank", "selected", "recipe_id", "name", "grade", "preview_image"]
    items = []
    for r in rows:
        d = dict(zip(cols, r))
        d["ingested"] = d["recipe_id"] is not None
        d["title"] = d.get("name") or d.get("ledger_title") or d["url"]
        items.append(d)
    return {"items": items, "count": len(items),
            "fit_computed_at": (fit or {}).get("computed_at"),
            "fit_used": bool(fit and fit.get("used"))}


@app.post("/jobs/{job_id}/cancel")
def cancel_job_endpoint(job_id: int):
    """Request COOPERATIVE cancellation of a queued/running job. Jobs run out-of-
    process so we can't kill them — this sets a flag the handler polls between work
    units (e.g. the publisher harvest checks between candidates) and aborts → status
    'cancelled'. 409 if the job isn't live."""
    with _db() as conn:
        ok = jobs_lib.request_cancel(conn, job_id)
    if not ok:
        raise HTTPException(status_code=409, detail="Job is not queued/running — nothing to cancel.")
    return {"job_id": job_id, "cancel_requested": True}


@app.post("/domains/{domain}/enrich")
def enrich_domain_endpoint(request: Request, domain: str):
    """Quick Haiku profile of a domain — story, language, country, cuisine
    focus, logo. Returns the SUGGESTED fields (does not save); the editor
    populates them so the curator can review + Save. Token-journaled."""
    # GATED 2026-07-29. Doubly warranted: curator surface AND it spends LLM
    # tokens on our account, so an open version is a billable endpoint.
    _require_perm(request, "edit_master")
    from input.pipeline import domains_lib
    from extract.domain_enrich import enrich_domain
    try:
        with _db() as conn:
            row = domains_lib.get_domain(conn, domain)
        display_name = (row or {}).get("display_name") or ""
        usage_log: list = []
        import llm  # gateway: attribute migrated-module usage to this domain
        llm.enter(recipe_id=f"domain:{domain.strip().lower()}", user_id=0)
        result = enrich_domain(domain, display_name=display_name, usage_log=usage_log)
        _journal_usage(usage_log, recipe_id=f"domain:{domain.strip().lower()}", user_id=0)
        if result is None:
            raise HTTPException(status_code=502, detail="Enrichment failed — the model returned nothing.")
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] enrich_domain({domain!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Enrich error: {e}") from e


@app.post("/domains/{domain}/deep-enrich")
def deep_enrich_domain_endpoint(request: Request, domain: str):
    """Deep domain enrich: Moz V3 FACTS (brand authority, referring domains, ranking
    keywords) + a stronger LLM RESEARCH call grounded on them → a rich multi-paragraph
    `profile`. Returns the SUGGESTED fields (does not save); the editor populates them
    so the curator can review + Save. Token-journaled. ~16 Moz rows + one Sonnet call."""
    # GATED 2026-08-21 — this was missed when its sibling /domains/{d}/enrich was
    # gated on 2026-07-29. It took no `request` at all, so there was nothing to
    # check with. It is the MORE expensive of the two (~16 Moz rows AND a Sonnet
    # call, both billed to us) and it is a curator surface, so the reasoning that
    # gated the cheap one applies with more force here.
    _require_perm(request, "edit_master")
    from input.pipeline import domains_lib
    from extract.domain_enrich import deep_enrich_domain
    try:
        with _db() as conn:
            row = domains_lib.get_domain(conn, domain)
        display_name = (row or {}).get("display_name") or ""
        rid = f"domain:{domain.strip().lower()}"
        usage_log: list = []
        import llm  # gateway: attribute migrated-module usage to this domain
        llm.enter(recipe_id=rid, user_id=0)
        result = deep_enrich_domain(domain, display_name=display_name)
        _journal_usage(usage_log, recipe_id=rid, user_id=0)
        if result is None:
            raise HTTPException(status_code=502, detail="Deep enrich failed — the model returned nothing.")
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] deep_enrich_domain({domain!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Deep enrich error: {e}") from e


@app.patch("/domains/{domain}")
def patch_domain_endpoint(request: Request, domain: str, payload: dict = Body(...)):
    """Update editable fields on a domain row."""
    # GATED 2026-07-29 (see POST /domains).
    _require_perm(request, "edit_master")
    try:
        from input.pipeline import domains_lib
        with _db() as conn:
            return domains_lib.update_domain(conn, domain, payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] patch_domain({domain!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}") from e


# =========================================================================
# Training-data correction UI (is-recipe classifier). Curators review the
# samples captured as a byproduct of the harvest/batch is-recipe filter and
# correct the heuristic where it's wrong (the `human_label` gold signal that
# lets a classifier BEAT the heuristic). Reads the separate, git-ignored
# training.db. See docs/corpus-ml-strategy.md + intake/training_capture.py +
# forms/training.html.
# =========================================================================
@app.get("/training/is-recipe/stats")
def training_is_recipe_stats_endpoint():
    from intake import training_capture
    return training_capture.stats()


@app.get("/training/is-recipe/samples")
def training_is_recipe_samples_endpoint(limit: int = 50, offset: int = 0,
                                        search: str = "", decision: str = "",
                                        source: str = "", label: str = "",
                                        has_content: int = 0, sort: str = "recent",
                                        shadow: str = ""):
    from intake import training_capture
    return training_capture.list_samples(
        limit=max(1, min(int(limit), 200)),
        offset=max(0, int(offset)),
        search=(search or "").strip() or None,
        decision=(decision or "").strip() or None,
        source=(source or "").strip() or None,
        label=(label or "").strip() or None,
        has_content=bool(has_content),
        sort=(sort or "recent").strip() or "recent",
        shadow=(shadow or "").strip() or None,
    )


@app.post("/training/is-recipe/samples/{sample_id}/label")
def training_is_recipe_label_endpoint(sample_id: int, payload: dict = Body(...)):
    from intake import training_capture
    res = training_capture.set_human_label(
        sample_id, payload.get("label"), (payload.get("note") or None))
    if isinstance(res, dict) and res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@app.delete("/domains/{domain}")
def delete_domain_endpoint(domain: str, request: Request, cascade: int = 0):
    """Delete a domain's master record.

    DEFAULT (cascade=0): removes ONLY the `domains` row — its ingested recipes
    and cohort ledger are left intact (the historical, safe behavior; nothing
    can be mass-deleted by accident).

    cascade=1: ALSO removes the publisher's ingested master recipes and clears
    its `collection_members` cohort. Recipe removal goes through the typed-
    membership retire (`retire_master_membership`), so a row ALSO claimed by a
    dish is KEPT (only its publisher block is cleared) and sqlite-vec is loaded
    so the AFTER DELETE trigger cleans `recipes_master_vec`. SCOPED to this one
    publisher_key — it cannot touch another domain's rows. Gated on
    `delete_master` so only a curator can destroy corpus rows."""
    _require_perm(request, "delete_master")
    try:
        from input.pipeline import domains_lib
        host = domains_lib._canon_host(domain)
        result = {"deleted": host, "cascade": bool(cascade)}
        with _db() as conn:
            if cascade:
                from input.pipeline import dishes as _dishes_lib, collections_lib
                try:
                    from input.pipeline import vector_store as _vs
                    _vs.enable_vec(conn)   # so the AFTER DELETE vec trigger can fire
                except Exception:
                    pass
                # (1) Typed-block retire: drop rows whose _master.publisher == host,
                # KEEPING any also claimed by a dish (clears just the publisher block).
                kept_as_dish, by_publisher = _dishes_lib.retire_master_membership(
                    conn, marker="publisher", value=host, other_marker="dish",
                    remove_fields=["publisher", "refreshed_at"])
                # (2) Host sweep: legacy rows ingested BEFORE publisher attribution
                # carry NO _master.publisher, so (1) can't see them. Catch them by
                # URL host so "delete this publisher" is complete — still SPARING any
                # row with a dish block, and SCOPED to this exact host (canonical
                # compare, never a substring match). Vec trigger cleans each delete.
                from urllib.parse import urlparse as _urlparse
                by_host = 0
                for _rid, _data, _un in conn.execute(
                        "SELECT id, data, url_normalized FROM master_recipes").fetchall():
                    try:
                        _hn = _urlparse(_un or "").hostname or ""
                    except Exception:
                        continue
                    if not _hn or domains_lib._canon_host(_hn) != host:
                        continue
                    try:
                        _d = json.loads(_data)
                    except Exception:
                        continue
                    if (_d.get("_master") or {}).get("dish"):
                        continue   # dish-anchored → keep
                    conn.execute("DELETE FROM master_recipes WHERE id = ?", (_rid,))
                    by_host += 1
                conn.commit()
                cohort_cleared = collections_lib.clear_members(conn, "publisher", host)
                result.update({"master_deleted": by_publisher + by_host,
                               "master_by_publisher": by_publisher,
                               "master_by_host_orphan": by_host,
                               "master_kept_as_dish": kept_as_dish,
                               "cohort_cleared": cohort_cleared})
                print(f"[DOMAIN-DELETE] CASCADE {host}: master_deleted="
                      f"{by_publisher + by_host} (publisher={by_publisher}, "
                      f"host-orphan={by_host}), kept_as_dish={kept_as_dish}, "
                      f"cohort_cleared={cohort_cleared}")
            ok = domains_lib.delete_domain(conn, host)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Unknown domain: {domain}")
        return result
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] delete_domain({domain!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Delete error: {e}") from e


# =========================================================================
# Generic admin scaffold — list + Add/Change/Delete for any model registered
# in admin_models.ADMIN_MODELS, driven entirely by the model descriptor.
# View: forms/admin.html?model=<name>. Adding an admin-managed table is one
# edit (append an AdminModel) — no new endpoint, no new page. Writes are
# restricted to each model's whitelisted editable fields, so the generic SQL
# can't reach an arbitrary column/table. Unauthenticated like the rest of the
# app today — gate before exposing publicly.
# =========================================================================
import admin_models as _admin


def _admin_model_or_404(model: str):
    m = _admin.get_model(model)
    if m is None:
        raise HTTPException(status_code=404, detail=f"Unknown admin model: {model}")
    return m


# The generic admin CRUD surface is gated on the `admin_ui` permission so
# only the master/curator identity (user 0 → owner) — or any future staff
# role granted admin_ui — can read or mutate the registered admin models
# (system config, message categories, …). Per-feature endpoints (dishes,
# master recipes) already gate on their own perms; this closes the generic
# registry hole. _require_perm raises 403 for anyone without it.
@app.get("/admin/models")
def admin_list_models(request: Request):
    """The registered models, for the view's model switcher."""
    _require_perm(request, "admin_ui")
    return [{"name": m.name, "label": m.label} for m in _admin.ADMIN_MODELS.values()]


@app.get("/admin/{model}/schema")
def admin_model_schema(model: str, request: Request):
    """Field descriptors so the generic view can render list + form."""
    _require_perm(request, "admin_ui")
    return _admin_model_or_404(model).schema_json()


@app.get("/admin/{model}")
def admin_list_rows(model: str, request: Request):
    _require_perm(request, "admin_ui")
    m = _admin_model_or_404(model)
    cols = [f.name for f in m.fields]
    try:
        with _db() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(cols)} FROM {m.table} ORDER BY {m.order_by}"
            ).fetchall()
        return {"model": m.schema_json(),
                "rows": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        print(f"[ERROR] admin_list({model!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.post("/admin/{model}")
def admin_create_row(request: Request, model: str, payload: dict = Body(...)):
    _require_perm(request, "admin_ui")
    m = _admin_model_or_404(model)
    try:
        for f in m.fields:  # required check first, clearest error
            if f.required and f.editable and (
                f.name not in payload or payload[f.name] in (None, "")
            ):
                raise HTTPException(status_code=400, detail=f"{f.label} is required")
        cols, vals = [], []
        for name in m.editable_names():
            if name in payload:
                cols.append(name)
                vals.append(m.coerce(name, payload[name]))
        if not cols:
            raise HTTPException(status_code=400, detail="no fields supplied")
        ts = datetime.now(timezone.utc).isoformat()
        if m.has_col("created_at"):
            cols.append("created_at"); vals.append(ts)
        if m.has_col("updated_at"):
            cols.append("updated_at"); vals.append(ts)
        ph = ", ".join("?" for _ in cols)
        with _db() as conn:
            cur = conn.execute(
                f"INSERT INTO {m.table} ({', '.join(cols)}) VALUES ({ph})", vals
            )
            new_id = cur.lastrowid
        return {"ok": True, "id": new_id}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] admin_create({model!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.patch("/admin/{model}/{row_id}")
def admin_update_row(request: Request, model: str, row_id: int, payload: dict = Body(...)):
    _require_perm(request, "admin_ui")
    m = _admin_model_or_404(model)
    try:
        sets, vals = [], []
        for name in m.editable_names():
            if name in payload:
                sets.append(f"{name} = ?")
                vals.append(m.coerce(name, payload[name]))
        if not sets:
            raise HTTPException(status_code=400, detail="no editable fields supplied")
        if m.has_col("updated_at"):
            sets.append("updated_at = ?")
            vals.append(datetime.now(timezone.utc).isoformat())
        vals.append(row_id)
        with _db() as conn:
            cur = conn.execute(
                f"UPDATE {m.table} SET {', '.join(sets)} WHERE {m.pk} = ?", vals
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="row not found")
        return {"ok": True}
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        print(f"[ERROR] admin_update({model!r}, {row_id}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.delete("/admin/{model}/{row_id}")
def admin_delete_row(request: Request, model: str, row_id: int):
    _require_perm(request, "admin_ui")
    m = _admin_model_or_404(model)
    try:
        with _db() as conn:
            _enable_vec_for_delete(conn)  # vec-cleanup triggers need the module
            cur = conn.execute(
                f"DELETE FROM {m.table} WHERE {m.pk} = ?", (row_id,)
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="row not found")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] admin_delete({model!r}, {row_id}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/status-messages/active")
def status_messages_active():
    """All categories, each PRESORTED per its order_mode — the recipe form fetches this
    once and rotates. Lean payload: ready strings per category (the new message_categories
    model: one record/category, order mode + a CRLF textarea)."""
    try:
        from admin_models import get_messages, STATUS_MESSAGE_CATEGORIES
        out: dict[str, list] = {}
        with _db() as conn:
            for cat in STATUS_MESSAGE_CATEGORIES:
                msgs = get_messages(conn, cat, fallback=None)
                if msgs:
                    out[cat] = msgs
        return out
    except Exception as e:
        print(f"[ERROR] status_messages_active failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


@app.get("/messages")
def messages_endpoint(category: str, order: str = "", count: int = 0, fallback: str = "general"):
    """The 'messages' subroutine as an endpoint: a READY, presorted message list for ONE
    category. `order` overrides the stored order_mode (top|alpha|random); `count` caps the
    list. Falls back to the 'general' bucket when the category is empty/disabled — pass
    `fallback=none` (or empty) to DISABLE that (e.g. the cook opener, which must never
    borrow the extraction-wait lines)."""
    try:
        from admin_models import get_messages
        fb = None if fallback.strip().lower() in ("", "none", "0", "false") else fallback
        with _db() as conn:
            msgs = get_messages(conn, category, order=(order or None), count=(count or None), fallback=fb)
        return {"category": category, "order": order or None, "messages": msgs}
    except Exception as e:
        print(f"[ERROR] messages({category!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


# =========================================================================
# Dish refresh — now a job handler, not a long-blocking endpoint.
# POST /dishes/<name>/refresh enqueues a `dish_refresh` job and returns 202.
# The runner picks it up, opens a per-job log file, calls
# _handle_dish_refresh_job below. The dishes form connects to the SSE
# stream at /jobs/<id>/stream to watch progress in real time.
# =========================================================================


async def _handle_dish_refresh_job(job: dict) -> dict:
    """Job handler — registered with the runner under type 'dish_refresh'.
    The runner has already tee'd stdout/stderr to the per-job log file
    and stamped log_filename on the job row by the time we run.
    Returns a result dict that the runner stores in jobs.result.

    Mostly the same logic as the prior /refresh endpoint body — the
    stdout-tee bookkeeping moved to the runner, leaving this focused
    on the actual work."""
    from intake.build_query_batch import build_batch

    params = job.get("params") or {}
    name = params.get("dish_name") or ""
    log_filename = job.get("log_filename")

    # Re-fetch dish at run-time (could have been edited/deleted between
    # enqueue and run).
    with _db() as conn:
        dish = dishes_lib.get_dish(conn, name)
    if dish is None:
        raise RuntimeError(f"Dish {name!r} not found at run time (deleted?)")
    canonical_name = dish["name"]

    # Safety net: clamp to the configured hard caps even if a dish row somehow
    # holds an oversized value (the create/edit form rejects above the cap, but
    # this protects pre-existing rows + any direct DB edit from a runaway
    # SerpAPI request). System → Limits.
    _max_serp, _max_final = dishes_lib.dish_limits()
    top_serp = min(int(dish["top_n_serpapi"]), _max_serp)
    top_final = min(int(dish["top_n_final"]), _max_final)
    if top_serp != dish["top_n_serpapi"] or top_final != dish["top_n_final"]:
        print(f"[REFRESH-DISH] CLAMPED to limits: serpapi {dish['top_n_serpapi']}->{top_serp}, "
              f"final {dish['top_n_final']}->{top_final}")

    print(f"=== Dish refresh: {canonical_name!r} ===")
    print(f"queries: {dish['queries']}")
    print(f"top_n_serpapi: {top_serp} per query, top_n_final: {top_final}")
    print(f"[REFRESH-DISH] {canonical_name!r} starting")

    # Editor's Choice awards are NOT injected into the candidate pool any more.
    # They were, and that was the bug: a pin went in as an extra candidate, got
    # scored, and surfaced only "if it ranks" — handing a curator's rescue back to
    # the exact gate that had dropped the recipe. Awards are now ingested at pin
    # time as kind='editors_choice' and are exempt from ranking, so a refresh must
    # leave them alone. They already survive the delete below (it is scoped to
    # kind='top') and are already excluded from the OU fit.
    with _db() as conn:
        pinned_urls = dishes_lib.editors_choice_urls(conn, canonical_name)
    if pinned_urls:
        print(f"[REFRESH-DISH] {len(pinned_urls)} Editor's Choice award(s) preserved "
              f"(exempt from ranking)")

    from input.pipeline.jobs import JobCancelled
    job_id = job.get("id")

    def _should_cancel():
        # Cross-process poll: the server sets cancel_requested; this out-of-process
        # job sees it via WAL between candidates (in _is_recipe_filter) and aborts.
        try:
            with sqlite3.connect(DB_PATH, timeout=5) as conn:
                return jobs_lib.is_cancel_requested(conn, job_id)
        except Exception:
            return False

    try:
        batch_result = await asyncio.to_thread(
            build_batch,
            queries=dish["queries"],
            dish=canonical_name,
            top_n_serpapi=top_serp,
            top_n_final=top_final,
            extra_urls=None,   # see the Editor's Choice note above — awards don't compete
            should_cancel=_should_cancel,
        )
    except JobCancelled:
        # Cooperative cancel — record a clean 'cancelled' run (NOT an error), then
        # re-raise so the runner marks the job cancelled.
        print(f"[REFRESH-DISH] {canonical_name!r} cancelled by user")
        with _db() as conn:
            dishes_lib.record_run_result(
                conn, canonical_name, status="cancelled", count=0,
                log_filename=log_filename, rejects=[], ou_fit=None, bottom_ou=None)
        raise
    except Exception as e:
        print(f"[REFRESH-DISH] build_batch failed: {e}")
        with _db() as conn:
            # Pass rejects=[] (not None) so the per-run wipe still
            # fires — semantically: "this refresh produced zero
            # rejects because it failed before any URL was processed."
            # Otherwise stale rejects from a previous successful run
            # would persist + mislead the form.
            dishes_lib.record_run_result(
                conn, canonical_name,
                status=f"error:build_batch:{type(e).__name__}", count=0,
                log_filename=log_filename,
                rejects=[], ou_fit=None, bottom_ou=None,
            )
        raise  # runner records error status + stores the message

    entries = batch_result["entries"]
    print(f"[REFRESH-DISH] front-end yielded {len(entries)} candidates")

    # Persist the (URL, DA, PA) cohort the dish fit saw — used by the
    # chapter-level aggregate fit to grade niche dishes whose own
    # cohort is below the n=25 floor. Done before saves so that even
    # if the save loop crashes mid-way, the chapter rollups still
    # have today's data.
    fit_points = batch_result.get("fit_data_points") or []
    if fit_points:
        try:
            from input.pipeline.chapters import (
                replace_data_points_for_dish, score_data_points_for_dish,
            )
            from input.pipeline.config import POWER_BLEND_WEIGHT
            job_id = job.get("id")
            ou_fit = batch_result.get("ou_fit") or {}
            winner_urls = [e["url"] for e in entries if e.get("url")]
            with _db() as conn:
                n_written = replace_data_points_for_dish(
                    conn, canonical_name, fit_points, model_version=job_id)
                # SQL scorer: fills ou/power/percentiles/rank_score over the
                # full cohort + flags selected winners (model_version=job id).
                n_scored = score_data_points_for_dish(
                    conn, canonical_name, ou_fit, POWER_BLEND_WEIGHT, winner_urls)
            print(f"[REFRESH-DISH] persisted {n_written} data points "
                  f"(model_version={job_id}); SQL-scored {n_scored}")
        except Exception as e:
            print(f"[REFRESH-DISH] data-points persist/score failed (non-fatal): {e}")

    # The candidate ledger — every URL this run considered and what it decided,
    # winners and losers alike (docs/ai-editor-mediation.md Phase 0). Written
    # HERE, before the save loop, for the same reason the data points are: a
    # crash mid-save must not cost us the record of what we threw away. Until
    # this existed the drops lived only in the log file — dish_rejects captured
    # 1 of 39 on job 794 and 1 of 61 on job 795.
    try:
        from input.pipeline import candidate_ledger
        ledger_rows = candidate_ledger.build_rows(
            batch_result, collection_type="dish", collection_key=canonical_name,
            job_id=job.get("id"),
            run_started_at=job.get("started_at") or datetime.now(timezone.utc).isoformat(),
        )
        with _db() as conn:
            n_led = candidate_ledger.record_run(conn, ledger_rows)
            summary = candidate_ledger.run_summary(conn, job.get("id"))
        n_med = sum(1 for r in ledger_rows if r["outcome"] == "dropped" and r["overturnable"])
        print(f"[REFRESH-DISH] candidate ledger: {n_led} row(s) {summary} "
              f"— {n_med} reconsiderable by the editor")
    except Exception as e:
        print(f"[REFRESH-DISH] candidate ledger failed (non-fatal): {e}")

    # Recompute + persist THIS dish's chapter fit now that its cohort data
    # points are refreshed. Every dish update keeps the chapter formula
    # current, so chapter fits never drift from the dish corpus — there is
    # no corpus-diff to surface in the chapters editor (the fit IS the
    # corpus, re-derived each batch). See memory/project_admin_editor_nav.md.
    dish_chapter = dish.get("chapter") if isinstance(dish, dict) else None
    if dish_chapter and dish_chapter != "Uncertain":
        try:
            from input.pipeline.chapters import compute_and_store_chapter_fit
            with _db() as conn:
                cf = compute_and_store_chapter_fit(conn, dish_chapter)
            print(f"[REFRESH-DISH] recomputed chapter fit {dish_chapter!r}: "
                  f"n={cf.get('n')} used={cf.get('used')} model={cf.get('model')}")
        except Exception as e:
            print(f"[REFRESH-DISH] chapter-fit recompute failed (non-fatal): {e}")

    # (Dish competitiveness vs chapter is a CROSS-dish rollup that drifts when
    # any sibling refreshes — it is NOT computed here. The nightly
    # chapter_rollups job recomputes it for the whole chapter and stores it on
    # dishes.competitiveness_pct; the editorial enrich reads it live by dish.)

    # Delete prior top-kind rows for this dish — editors_choice and
    # legacy survive. Done BEFORE saves so the (url_normalized,
    # user_id=0) unique index can't collide between old + new.
    with _db() as conn:
        deleted = dishes_lib.delete_master_rows_for_dish(conn, canonical_name, kind="top")
    print(f"[REFRESH-DISH] deleted {deleted} prior kind=top rows for {canonical_name!r}")

    now_iso = datetime.now(timezone.utc).isoformat()
    saved_count = 0
    # Unified rejects list: every URL that *made it past the batch's
    # front-end pipeline* but then failed extract / save / save-gate.
    # Each entry preserves the original DA/PA/OU/title from the
    # SerpAPI+Moz step so the form can render "would have qualified"
    # against the dish's last_run_bottom_ou. Pre-extract rejects
    # (filter_disallowed, is_recipe, Moz-fail) are intentionally
    # excluded — they have no recipe candidate to recover.
    rejects: list[dict] = []

    def _record_reject(entry: dict, reason: str) -> None:
        exc = entry.get("exceptionalism") or {}
        rejects.append({
            "url": entry.get("url"),
            "reason": reason,
            "title": entry.get("title") or "",
            "da": entry.get("da"),
            "pa": entry.get("pa"),
            "ou": entry.get("ou"),
            "rank": entry.get("rank"),
            # Cohort grade — shows on the dish-form reject row so the
            # user can see "this reject would have graded A-" before
            # deciding whether to harvest it. None for n<25 dishes that
            # didn't get a per-dish fit.
            "exc_score": exc.get("score"),
            "exc_grade": exc.get("grade"),
        })

    # Save until top_n_final winners land, backfilling from the reserve when a
    # winner fails extract/save-gate ("ensure 10 if available"). The loop stops
    # at the target, so reserve URLs are only extracted when a backfill is needed.
    pool = list(entries) + list(batch_result.get("reserve", []))
    target = top_final
    saved_urls: list[str] = []
    backfilled = 0
    last_saved_ou: Optional[float] = None
    for entry in pool:
        if saved_count >= target:
            break
        url = entry["url"]
        try:
            # Dish refresh always force-refreshes the extract cache:
            # the refresh has already paid SerpAPI + Moz quota; re-extracting
            # is the cheap, deterministic step, and cache hits would
            # mask updates to the extraction pipeline (e.g. extraction-
            # stage translation provenance landing 2026-05-29). Cache
            # is still useful for the interactive form / single-URL
            # extracts that aren't part of a batch refresh.
            extract_result = await asyncio.to_thread(
                extract_recipe_from_url, url, user_id=0, force_refresh=True,
            )
        except Exception as e:
            print(f"[REFRESH-DISH] EXTRACT-MISS {url}: {type(e).__name__}: {e}")
            _record_reject(entry, f"extract-miss: {type(e).__name__}")
            continue
        recipe_dict = (extract_result or {}).get("recipe") or {}
        if not recipe_dict:
            _record_reject(entry, "extract-miss: empty recipe")
            continue

        ok, reason = _is_cacheable(
            recipe_dict,
            min_ings=SAVE_GATE_MIN_INGREDIENTS,
            min_steps=SAVE_GATE_MIN_INSTRUCTIONS,
        )
        if not ok:
            print(f"[REFRESH-DISH] SKIP-THIN {reason}  {url}")
            _record_reject(entry, f"skip-thin: {reason}")
            continue

        payload = dict(recipe_dict)
        payload["recipe_id"] = extract_result.get("recipe_id") or recipe_dict.get("id")
        payload["user_id"] = 0
        master_block = {
            "kind": "top",
            "dish": canonical_name,
            "refreshed_at": now_iso,
            # Re-sequence 1..N over the ACTUAL saved set (a backfilled reserve
            # takes the next slot rather than showing its raw blend rank).
            "rank": saved_count + 1,
            "queries": entry.get("_queries") or [],
            "batch_source": "/dishes/refresh",
        }
        # Exceptionalism grade was computed in _compute_custom_ou at the
        # batch step. Stamp it onto _master so the row carries its grade
        # forever (the cohort's σ is also persisted on dish.last_ou_fit
        # for future harvest-grading). n<25 dishes don't get a custom
        # fit and therefore no exceptionalism — surfaces as em-dash in
        # display.
        exc = entry.get("exceptionalism")
        if exc:
            master_block["exceptionalism"] = exc
        payload["_master"] = master_block
        # Stamp the DISH-COHORT scoring (in-cohort percentiles + field context +
        # competitiveness) onto _scoring. This is the LIVE refresh path — it does
        # NOT use pre_scored_from_entry, so the cohort signals must be merged here,
        # on top of the URL-static Moz _scoring set during extract. Feeds the
        # editorial authority commentary (relative/qualitative only).
        _cs = payload.get("_scoring") or {}
        if entry.get("power") is not None:
            _cs["power"] = float(entry["power"])
        if entry.get("ou_pct") is not None:
            _cs["ouPercentile"] = round(float(entry["ou_pct"]) * 100, 1)
        if entry.get("power_pct") is not None:
            _cs["powerPercentile"] = round(float(entry["power_pct"]) * 100, 1)
        _f = entry.get("_field") or {}
        if _f.get("avg_power") is not None:
            _cs["fieldAvgPower"] = float(_f["avg_power"])
            _cs["fieldMaxPower"] = float(_f.get("max_power", _f["avg_power"]))
            _cs["fieldMinPower"] = float(_f.get("min_power", _f["avg_power"]))
            _cs["fieldN"] = int(_f.get("n", 0))
        if _f.get("site_restriction"):
            _cs["fieldScope"] = ", ".join(_f["site_restriction"])
        payload["_scoring"] = _cs
        # Auto-enrich is opt-in per dish (defaults off). The save core
        # reads this flag to decide whether to fan out the 3 enrich
        # blocks (~$0.05 + ~10s per row). Without it, the dish refresh
        # is fast + cheap; user can enrich later from the form.
        payload["_skip_auto_enrich"] = not bool(dish.get("auto_enrich"))
        try:
            await asyncio.to_thread(_save_recipe_core, payload)
            saved_count += 1
            saved_urls.append(url)
            if isinstance(entry.get("ou"), (int, float)):
                last_saved_ou = float(entry["ou"])   # lowest saved = the real cut bar
            if (entry.get("rank") or 0) > target:
                backfilled += 1
        except HTTPException as e:
            print(f"[REFRESH-DISH] SAVE-FAIL {url}: {e.status_code} {e.detail}")
            _record_reject(entry, f"save-fail-{e.status_code}: {e.detail}")
        except Exception as e:
            print(f"[REFRESH-DISH] SAVE-FAIL {url}: {type(e).__name__}: {e}")
            _record_reject(entry, f"save-fail: {type(e).__name__}")

    # Phase A: record authority-scored FETCH-FAILS (likely anti-bot) as rejects
    # so the dish UI flags which would have qualified (ou vs bottom_ou) for a
    # Playwright/bookmarklet recovery. They carry Moz DA/PA + a fit-derived OU
    # but no recipe candidate (we couldn't crawl the page).
    for c in batch_result.get("fetch_fail_candidates", []):
        rejects.append({
            "url": c.get("url"),
            "reason": "fetch-failed (likely anti-bot — recover via Playwright/bookmarklet)",
            "title": "",
            "da": c.get("da"), "pa": c.get("pa"), "ou": c.get("ou"),
            "rank": None, "exc_score": None, "exc_grade": None,
        })
    _n_ff_qual = sum(1 for c in batch_result.get("fetch_fail_candidates", []) if c.get("would_qualify"))
    if batch_result.get("fetch_fail_candidates"):
        print(f"[REFRESH-DISH] {len(batch_result['fetch_fail_candidates'])} fetch-fail(s) "
              f"recorded as rejects; {_n_ff_qual} would have qualified")

    # Re-flag the ACTUAL saved winners in the cohort data points — backfill may
    # have swapped a failed winner for a reserve, so the pre-save flagging
    # (intended top-N) can be stale. Cheap DB pass, no extracts.
    if saved_urls:
        try:
            from input.pipeline.chapters import score_data_points_for_dish
            from input.pipeline.config import POWER_BLEND_WEIGHT
            # Pass the save-loop rejects so cohort_status can explain WHY a
            # high-authority also-ran isn't a winner (too thin / extract-fail / …).
            reject_map = {r["url"]: r.get("reason", "rejected")
                          for r in rejects if r.get("url")}
            with _db() as conn:
                score_data_points_for_dish(conn, canonical_name,
                                           batch_result.get("ou_fit") or {},
                                           POWER_BLEND_WEIGHT, saved_urls,
                                           reject_reasons=reject_map)
        except Exception as e:
            print(f"[REFRESH-DISH] winner re-flag failed (non-fatal): {e}")

    # Clean, readable end-of-run summary (separate from the per-recipe noise):
    # how many landed, how many were backfilled, and why any were dropped.
    _save_drops = [r for r in rejects if not str(r.get("reason", "")).startswith("fetch-failed")]
    from collections import Counter
    _by_reason = Counter(str(r.get("reason", "?")).split(":")[0].split(" (")[0] for r in _save_drops)
    _drop_summary = ", ".join(f"{k}×{v}" for k, v in _by_reason.items()) or "none"
    print(f"[REFRESH-DISH] SUMMARY {canonical_name!r}: saved {saved_count}/{target}"
          + (f" ({backfilled} backfilled from reserve)" if backfilled else "")
          + f"; dropped this run: {_drop_summary}  (see Rejects)")

    # Bar-to-beat for "would have qualified": the OU of the LOWEST WINNER WE
    # ACTUALLY SAVED (with backfill, that may be a promoted reserve, not the
    # original #N). Falls back to the original cut if nothing saved.
    bottom_ou: Optional[float] = last_saved_ou
    if bottom_ou is None and entries:
        last = entries[-1]
        if isinstance(last.get("ou"), (int, float)):
            bottom_ou = float(last["ou"])

    dish_status = "success" if saved_count > 0 else "error:no_saves"
    with _db() as conn:
        dishes_lib.record_run_result(
            conn, canonical_name, status=dish_status, count=saved_count,
            log_filename=log_filename,
            ou_fit=batch_result.get("ou_fit"),
            rejects=rejects,
            bottom_ou=bottom_ou,
        )

    print(f"[REFRESH-DISH] {canonical_name!r} done: "
          f"saved={saved_count} rejects={len(rejects)} "
          f"bottom_ou={bottom_ou}")

    return {
        "dish": canonical_name,
        "deleted_prior_rows": deleted,
        "saved_count": saved_count,
        "rejects": rejects,
        "bottom_ou": bottom_ou,
        "ou_fit": batch_result.get("ou_fit"),
        "front_end_counts": batch_result["counts"],
        "elapsed_s": batch_result["elapsed_s"],
    }


# Register the handler so the runner knows about it. Done at module
# import time — the runner loop reads JOB_HANDLERS each tick.
jobs_lib.register_handler("dish_refresh", _handle_dish_refresh_job)


async def _handle_chapter_rollups_job(job: dict) -> dict:
    """Nightly CROSS-dish rollup: recompute every dish's in-chapter
    competitiveness percentile and store it on dishes.competitiveness_pct.
    Off the hot refresh path (it drifts whenever any sibling refreshes), so it
    runs on a schedule instead. See dishes_lib.recompute_competitiveness."""
    def _run():
        with _db() as conn:
            return dishes_lib.recompute_competitiveness(conn)
    summary = await asyncio.to_thread(_run)
    print(f"[ROLLUPS] chapter competitiveness: {summary}")
    return summary


jobs_lib.register_handler("chapter_rollups", _handle_chapter_rollups_job)


async def _handle_dish_rematch_job(job: dict) -> dict:
    """Nightly: re-score every master row that carries no curated `_master.dish`
    against the CURRENT dish catalog.

    This is NOT how new recipes get a dish — they are matched at save. It is for
    the other direction: the catalog moves (~45-60 new dishes a month, plus
    description/query edits that move a dish's own vector), and a row otherwise
    keeps the best match from a catalog that no longer exists. Creating
    "Pumpkin Pie" does not move the pumpkin pies out of Cream Pie; only a
    re-score does.

    Cheap by construction: every row already has its embedding stored and the
    dish index is local, so this is a KNN query per row (~14s for 3,200) with no
    embedding call and nothing billable. It writes ONLY the rows whose verdict
    changed, so a quiet night costs reads and no writes.
    """
    def _run():
        from input.pipeline import dish_match
        with _db() as conn:
            return dish_match.rematch_unclaimed(conn, db_path=DB_PATH)
    summary = await asyncio.to_thread(_run)
    moves = summary.pop("moves", [])
    print(f"[REMATCH] {summary}")
    for rid, old, new_dish in moves[:50]:
        print(f"[REMATCH]   {rid}: {old!r} -> {new_dish!r}")
    if len(moves) > 50:
        print(f"[REMATCH]   ... {len(moves) - 50} more")
    return summary


jobs_lib.register_handler("dish_rematch", _handle_dish_rematch_job)


async def _handle_ai_mediation_job(job: dict) -> dict:
    """SHADOW review of one run by the AI editor (docs/ai-editor-mediation.md).

    Params: {job_id} — the run to review — or {dish}/{collection_key} to review that
    collection's LATEST ledgered run. Records verdicts and changes nothing; the
    handler refuses `apply` rather than accepting it and quietly ignoring it.
    """
    p = job.get("params") or {}
    if isinstance(p, str):
        p = json.loads(p or "{}")
    if p.get("apply"):
        raise ValueError("apply is Phase 2 — shadow mediation records verdicts only")
    target = int(p.get("job_id") or 0)
    ctype = p.get("collection_type") or "dish"
    ckey = (p.get("collection_key") or p.get("dish") or "").strip()

    def _run():
        from input.pipeline import ai_editor
        with _db() as conn:
            jid = target
            if not jid:
                if not ckey:
                    raise ValueError("job_id or collection_key/dish is required")
                row = conn.execute(
                    "SELECT job_id FROM run_candidates WHERE collection_type = ? "
                    "AND collection_key = ? ORDER BY run_started_at DESC, id DESC "
                    "LIMIT 1", (ctype, ckey)).fetchone()
                if not row:
                    raise ValueError(
                        f"no candidate ledger for {ctype} {ckey!r} — the ledger is "
                        f"written by a run, so run one first")
                jid = row[0]
            return ai_editor.mediate_run(conn, jid, apply=False)

    res = await asyncio.to_thread(_run)
    print(f"[AI-EDITOR] shadow review: {res}")
    return res


jobs_lib.register_handler("ai_mediation", _handle_ai_mediation_job)


async def _handle_screenshot_refresh_job(job: dict) -> dict:
    """Keep source-page screenshots current. Captures what is MISSING and
    re-shoots what has gone STALE, in one pass.

    Four reasons a row is picked up, all read from data we already keep:

      no-shot     `_source.pageScreenshot` empty — never captured.
      no-blob     the recipe points at a /screenshot/<id> whose media.db row is
                  gone (media.db is git-ignored and regenerable, so this is the
                  expected state after a restore).
      changed     `source_changed_at` is newer than the blob — the publisher
                  edited the page since we shot it, so the image is a picture of
                  something that no longer exists.
      aged        the blob is older than `max_age_days` (default 365; 0 = off).
                  Sites redesign; a two-year-old shot misrepresents them.

    Re-shooting is cheap and idempotent: `screenshot_id_for(url_normalized)` is
    deterministic, so a recapture OVERWRITES one media.db row and the recipe's
    `/screenshot/<id>` URL is unchanged — the recipe row is only rewritten when
    it had no screenshot before.

    params: {mode: "all"|"missing"|"stale", limit: int, max_age_days: int,
             tables: ["master_recipes","recipes"]}
    Wall time is Playwright-bound, ~4s/row, so `limit` is the real control —
    a nightly schedule with limit ~200 keeps the corpus fresh without ever
    running long.
    """
    p = job.get("params") or {}
    mode = (p.get("mode") or "all").strip()
    limit = int(p.get("limit") or 0)
    max_age_days = int(p.get("max_age_days", 365))
    tables = p.get("tables") or ["master_recipes", "recipes"]

    def _run():
        from input.pipeline.screenshot_pipeline import screenshot_id_for
        from datetime import timedelta
        counts = {"scanned": 0, "no-shot": 0, "no-blob": 0, "changed": 0,
                  "aged": 0, "captured": 0, "failed": 0, "skipped": 0,
                  # Always present, so a run that latched nothing still says 0
                  # rather than omitting the key and reading as "not implemented".
                  "latched": 0}
        media = sqlite3.connect(MEDIA_DB_PATH, timeout=30)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max_age_days) if max_age_days > 0 else None
        with _db() as conn:
            for table in tables:
                rows = conn.execute(
                    f"SELECT id, data, url_normalized, source_changed_at FROM {table}"
                ).fetchall()
                for rid, dj, url_norm, changed_at in rows:
                    if limit and counts["captured"] >= limit:
                        break
                    counts["scanned"] += 1
                    try:
                        d = json.loads(dj)
                    except Exception:
                        continue
                    src = d.get("_source") or {}
                    url = (src.get("originalUrl") or "").strip()
                    if not url or not url.startswith(("http://", "https://")):
                        counts["skipped"] += 1
                        continue
                    if _SELF_PERMALINK_RE.search(url):
                        counts["skipped"] += 1
                        continue
                    key = (url_norm or url).strip()
                    had_shot = bool((src.get("pageScreenshot") or "").strip())

                    # Resolve the blob by the id the RECIPE POINTS AT first, and
                    # only then by the id we would compute today. Measured
                    # 2026-08-06: 31 rows carry a working screenshot stored
                    # under a different id than screenshot_id_for(url_normalized)
                    # now yields (the key used at capture time differed). Trusting
                    # only the computed id calls those "missing" and re-shoots a
                    # perfectly good page, orphaning the original blob.
                    blob_at = None
                    stored_id = ((src.get("pageScreenshot") or "")
                                 .strip().rsplit("/", 1)[-1])
                    for sid in (stored_id, screenshot_id_for(key)):
                        if not sid:
                            continue
                        row = media.execute(
                            "SELECT created_at FROM page_screenshots WHERE screenshot_id = ?",
                            (sid,)).fetchone()
                        if row:
                            blob_at = row[0]
                            break

                    reason = None
                    if not had_shot:
                        reason = "no-shot"
                    elif blob_at is None:
                        reason = "no-blob"
                    elif changed_at and blob_at and str(changed_at) > str(blob_at):
                        reason = "changed"
                    elif cutoff and blob_at and str(blob_at) < cutoff.isoformat():
                        reason = "aged"
                    if reason is None:
                        continue
                    if mode == "missing" and reason not in ("no-shot", "no-blob"):
                        continue
                    if mode == "stale" and reason in ("no-shot",):
                        continue
                    # Skip a row that has already failed repeatedly, until its
                    # retry window opens. Counted separately so "we are not trying"
                    # never looks like "we tried and it worked".
                    _fails = int((src.get("screenshotFailures") or 0))
                    if _fails >= SCREENSHOT_FAIL_LATCH:
                        _last = str(src.get("screenshotLastFailedAt") or "")
                        _retry_at = (datetime.now(timezone.utc)
                                     - timedelta(days=SCREENSHOT_RETRY_DAYS)).isoformat()
                        if _last and _last > _retry_at:
                            counts["latched"] = counts.get("latched", 0) + 1
                            continue
                    counts[reason] += 1

                    before = (d.get("_source") or {}).get("pageScreenshot")
                    _attach_page_screenshot(d, url, key, None, force=True)
                    after = (d.get("_source") or {}).get("pageScreenshot")
                    if not after:
                        counts["failed"] += 1
                        # LATCH IT. Measured 2026-08-14: the SAME 45 rows failed on
                        # six consecutive nights (jobs 778->835) — 45 attempted, 45
                        # failed, 0% success — while `scanned` climbed 5,186->5,549.
                        # A capture that has never once worked is not a transient
                        # miss, and at limit=100 those 45 would eat HALF the nightly
                        # budget forever.
                        #
                        # Latched WITH AN EXPIRY, not permanently: washingtonpost
                        # times out and edibleboston fails TLS, and either could be
                        # fixed by the publisher or by us. SCREENSHOT_RETRY_DAYS
                        # later the row is eligible again, so a site that starts
                        # working is picked up without anyone remembering to clear
                        # a flag.
                        _src = d.get("_source") or {}
                        _n = int(_src.get("screenshotFailures") or 0) + 1
                        _src["screenshotFailures"] = _n
                        _src["screenshotLastFailedAt"] = datetime.now(timezone.utc).isoformat()
                        d["_source"] = _src
                        conn.execute(f"UPDATE {table} SET data = ? WHERE id = ?",
                                     (json.dumps(d, indent=2), rid))
                        conn.commit()
                        continue
                    counts["captured"] += 1
                    # Recovered — clear the latch so the counter can't creep up over
                    # years of unrelated misses and quietly retire a working page.
                    _src = d.get("_source") or {}
                    if _src.pop("screenshotFailures", None) is not None:
                        _src.pop("screenshotLastFailedAt", None)
                        d["_source"] = _src
                    if after != before:
                        conn.execute(f"UPDATE {table} SET data = ? WHERE id = ?",
                                     (json.dumps(d, indent=2), rid))
                        conn.commit()
        media.close()
        return counts

    summary = await asyncio.to_thread(_run)
    print(f"[SCREENSHOT-REFRESH] {summary}")
    return summary


jobs_lib.register_handler("screenshot_refresh", _handle_screenshot_refresh_job)


async def _handle_semrush_ranks_refresh_job(job: dict) -> dict:
    """Re-import the newest SEMrush Rank export (input/ ∪ ~/Downloads) and re-stamp
    every domain's semrush_rank from it. The export is MANUAL (no SEMrush API at
    our tier — see docs/semrush-harvest-scheduling.md), so this re-imports whatever
    fresh file the admin dropped; it never downloads. Idempotent (import is delete-
    and-replace per region). Schedulable weekly/monthly — not time-critical.
    Optional params: region (override the filename-detected region)."""
    params = job.get("params") or {}
    region = params.get("region")

    def _run():
        from input.pipeline import semrush_ranks, domains_lib
        path = semrush_ranks.find_newest_ranks_file()
        if not path:
            return {"imported": False, "reason": "no SEMrush ranks file in input/ or ~/Downloads"}
        with _db() as conn:
            imp = semrush_ranks.import_ranks_file(conn, path, region=region)
            ref = domains_lib.refresh_all_semrush_ranks(conn, region=imp["region"])
        return {"imported": True, **imp, **ref}

    summary = await asyncio.to_thread(_run)
    print(f"[SEMRUSH-RANKS] {summary}")
    return summary


jobs_lib.register_handler("semrush_ranks_refresh", _handle_semrush_ranks_refresh_job)


async def _handle_realrank_research_job(job: dict) -> dict:
    """RealRank: research ONE product across the named expert reviews + owner ratings and
    write the attributed brief (docs/RealRank/realrank_research.py).

    Long and costly — 8 SERP calls + 8 unblocker fetches + one research call with web search
    — which is why it belongs here rather than in a request: the run is tracked, tailable via
    the per-job log, and cooperatively cancellable between sources.

    Params: product (required) — the product NAME, the identity, not a search string
    (§3.1, same rule as --dish). Optional out_stem to override the output path. Optional
    owner_sources — a JSON list of ADDITIONAL retailer histograms to pool with Amazon's,
    [{"source":"bestbuy","histogram":[5,4,3,2,1 counts],"total":n}] — how a retailer we
    can't fetch (Best Buy blocks our unblocker) gets its ratings into the score.
    """
    params = job.get("params") or {}
    product = (params.get("product") or "").strip()
    if not product:
        raise ValueError("realrank_research requires params.product")
    extra = params.get("owner_sources") or None
    if isinstance(extra, str):          # --param arrives as a string from the CLI
        extra = json.loads(extra)
    job_id = job["id"]
    print(f"[REALRANK] {product}")

    def _should_cancel():
        try:
            with sqlite3.connect(DB_PATH, timeout=5) as conn:
                return jobs_lib.is_cancel_requested(conn, job_id)
        except Exception:
            return False

    product_id = (params.get("product_id") or "").strip()

    def _run():
        sys.path.insert(0, str(Path(__file__).resolve().parent / "docs" / "RealRank"))
        import realrank_research as rr
        rec = rr.research_product(product, out_stem=params.get("out_stem") or None,
                                  should_cancel=_should_cancel,
                                  extra_owner_sources=extra,
                                  brand=params.get("brand", ""),
                                  asin=params.get("asin", ""))
        summary = rr.job_summary(rec)
        # Attach to the catalog row when the run was launched FROM a product. Storing the
        # analysis is the point — the files are a by-product. Failure here must not lose
        # the run: the record is already on disk, so we report and carry on.
        if product_id:
            try:
                from intake.products import catalog_store
                realrank, realstory = rr.to_product_blocks(rec, job_id=job_id)
                with _db() as conn:
                    saved = catalog_store.set_realrank(conn, product_id, realrank)
                    if saved:
                        saved = catalog_store.set_realstory(conn, product_id, realstory)
                summary["attached_to"] = product_id if saved else None
                if not saved:
                    print(f"[REALRANK] product {product_id} not found — analysis kept on disk only")
            except Exception as e:
                summary["attach_error"] = str(e)
                print(f"[REALRANK] attach to {product_id} failed: {e}")
        return summary

    try:
        summary = await asyncio.to_thread(_run)
    except KeyboardInterrupt as e:      # raised by research_product's cancel checkpoint
        raise jobs_lib.JobCancelled(str(e)) from e
    print(f"[REALRANK] {summary}")
    return summary


jobs_lib.register_handler("realrank_research", _handle_realrank_research_job)


async def _handle_collection_refresh_job(job: dict) -> dict:
    """Run a product collection: the saved Amazon search URL -> the cohort -> a screened
    shortlist (intake/products/collections_store).

    Stage 1 — EasyParser SEARCH on the URL (1 credit per page). Every ASIN returned is KEPT,
    each with a Wilson screen computed from its average and count. Stage 2 — the top
    `keep_top_n` get a real star histogram from the free Amazon widget and are rescored with
    the RealRank index. We deliberately do not spend a fetch on candidates we've already
    screened out; `screened` records which ones we did.

    Params: collection (required, the name — identity, never the URL off argv).
    """
    from intake.products import collections_store as cst, easyparser as ep, amazon_widget as aw
    params = job.get("params") or {}
    name = (params.get("collection") or "").strip()
    if not name:
        raise ValueError("collection_refresh requires params.collection")
    job_id = job["id"]

    def _should_cancel():
        try:
            with sqlite3.connect(DB_PATH, timeout=5) as conn:
                return jobs_lib.is_cancel_requested(conn, job_id)
        except Exception:
            return False

    def _run():
        sys.path.insert(0, str(Path(__file__).resolve().parent / "docs" / "RealRank"))
        from realrank_index import realrank_index, polarization
        with _db() as conn:
            coll = cst.get_collection(conn, name)
        if not coll:
            raise ValueError(f"collection {name!r} not found")

        print(f"[COLLECTION] {name} -> {coll['url']}")
        res = ep.search_url(coll["url"], pages=int(coll.get("pages") or 1))
        for w in (res.get("warnings") or []):
            print(f"[COLLECTION] warning: {w}")
        if not res.get("ok"):
            raise ValueError(f"search failed: {res.get('error')}")
        items = res["items"]
        with _db() as conn:
            cst.replace_candidates(conn, name, items)
            cohort = cst.list_candidates(conn, name)
        print(f"[COLLECTION] {len(items)} candidates kept "
              f"(credits {res.get('credits')}) — screening top {coll.get('keep_top_n')}")

        top = [c for c in cohort if c.get("wilson_score")][:int(coll.get("keep_top_n") or 10)]
        screened, failed = 0, 0
        for c in top:
            if _should_cancel():
                raise KeyboardInterrupt("cancelled during screening")
            h = aw.rating_histogram(c["asin"])
            if not h.get("ok") or not h.get("histogram"):
                failed += 1
                print(f"[COLLECTION]   {c['asin']} histogram unavailable: {h.get('error')}")
                continue
            score = round(realrank_index(h["histogram"], h["ratings_total"]), 1)
            pol = (polarization(h["histogram"]) or {}).get("label") or ""
            with _db() as conn:
                cst.set_screen_result(conn, name, c["asin"], histogram=h["histogram"],
                                      realrank_score=score, polarization=pol)
            screened += 1
            print(f"[COLLECTION]   {c['asin']} wilson {c['wilson_score']} -> real {score}"
                  f"{' (' + pol + ')' if pol else ''}")
        # Every histogram failing is systemic (throttled/endpoint moved), not a bad candidate.
        if top and failed == len(top):
            raise ValueError(f"all {failed} histogram fetches failed — widget may be blocked "
                             f"or its endpoint moved; not publishing an unscored shortlist")
        with _db() as conn:
            conn.execute("UPDATE product_collections SET last_job_id = ? WHERE name = ?",
                         (job_id, name))
            conn.commit()
        return {"collection": name, "candidates": len(items), "screened": screened,
                "histogram_failures": failed, "credits": res.get("credits")}

    try:
        summary = await asyncio.to_thread(_run)
    except KeyboardInterrupt as e:
        raise jobs_lib.JobCancelled(str(e)) from e
    print(f"[COLLECTION] {summary}")
    return summary


jobs_lib.register_handler("collection_refresh", _handle_collection_refresh_job)


async def _handle_curated_collection_run_job(job: dict) -> dict:
    """The review-sourced selection run: a product CLASS -> the expert reviews -> ranked,
    evidenced picks -> catalog product records.

    The sibling of `collection_refresh`. That one starts from a saved Amazon search URL and
    screens the cohort on owner ratings; this one starts from a class name and the reviews the
    named authorities published, then verifies those picks against our own data. Both end in
    products, which is the point of having two — expert consensus and owner arithmetic
    disagree usefully and neither is folded into the other.

    Long and costly — ~8 SERP + ~8 unblocker fetches + one research call, minutes — so it
    belongs here: tracked, tailable, cancellable between stages.

    Params: collection (required, the NAME — identity, never the class string off argv;
    §3.1, the --dish rule). Optional refresh to re-fetch the cached source documents.
    """
    from intake.products import curated_collections as ccs
    from intake.products.curate import pipeline, to_products
    params = job.get("params") or {}
    name = (params.get("collection") or "").strip()
    if not name:
        raise ValueError("curated_collection_run requires params.collection")
    job_id = job["id"]

    def _should_cancel():
        try:
            with sqlite3.connect(DB_PATH, timeout=5) as conn:
                return jobs_lib.is_cancel_requested(conn, job_id)
        except Exception:
            return False

    def _run():
        with _db() as conn:
            coll = ccs.get_collection(conn, name)
        if not coll:
            raise ValueError(f"curated collection {name!r} not found")
        pclass = coll["product_class"]
        cats = coll.get("categories") or []
        print(f"[CURATE] {name} -> {pclass}")

        out = pipeline.run(pclass, cats, refresh=bool(params.get("refresh")),
                           use_network=bool(coll.get("use_network", 1)),
                           should_cancel=_should_cancel)
        record, report, brief_text = out["record"], out["report"], out["brief_text"]

        picks = pipeline.picks_from(record)
        with _db() as conn:
            ccs.replace_picks(conn, name, picks)
            ccs.set_run_result(conn, name, record=record, report=report,
                               brief_text=brief_text, job_id=job_id, pick_count=len(picks))
            stored = ccs.list_picks(conn, name)

        if _should_cancel():
            raise KeyboardInterrupt("cancelled before materializing products")

        # The deliverable. Failure here must not lose the run — the brief and its picks are
        # already stored, so we report and carry on rather than raising over the top of them.
        print(f"[CURATE] materializing {len(stored)} placements into product records…")
        try:
            with _db() as conn:
                def _link(slot, pid, action):
                    ccs.set_pick_product(conn, name, slot, pid, action)
                summary = to_products.materialize(
                    conn, collection=name, product_class=pclass, picks=stored,
                    job_id=job_id, on_result=_link)
        except Exception as e:
            print(f"[CURATE] materialization failed: {e}")
            summary = {"materialize_error": str(e)}

        # Report what each number IS. The first cut labelled the verified-pick count
        # "sources", which read as "3 of 8 publishers answered" when it meant "3 picks had
        # their listing confirmed" — the summary is what a curator sees without opening a log.
        summary.update({"collection": name, "product_class": pclass,
                        "categories": cats, "picks": len(picks),
                        "sources_retrieved": len((out.get("sources") or {}).get("retrieved") or []),
                        "sources_missing": (out.get("sources") or {}).get("missing") or [],
                        "identity_verified": len(report.get("verified") or []),
                        "identity_rejected": len(report.get("rejected") or []),
                        "asins_recovered": len(report.get("filled") or []),
                        "owner_scored": len(report.get("scored") or []),
                        "brief_chars": len(brief_text)})
        return summary

    try:
        summary = await asyncio.to_thread(_run)
    except KeyboardInterrupt as e:
        raise jobs_lib.JobCancelled(str(e)) from e
    except Exception as e:
        # A failed run leaves a record on the collection, not just a job row: the editor is
        # where the curator looks, and "nothing happened" is the worst thing it can say.
        try:
            with _db() as conn:
                from intake.products import curated_collections as _ccs
                _ccs.set_run_error(conn, name, str(e), job_id)
        except Exception:
            pass
        raise
    print(f"[CURATE] {summary}")
    return summary


jobs_lib.register_handler("curated_collection_run", _handle_curated_collection_run_job)


async def _handle_review_ingest_job(job: dict) -> dict:
    """Ingest one captured review page (bookmarklet or paste) into the review store.

    This used to run inline in POST /extract-review, which made it the ONE ingestion path
    with no job behind it: ~30s of LLM time with no tracked record, nothing to find in the
    job list afterwards, and no retry. Four silent truncation failures on 2026-07-27 were
    only recoverable from the raw stdout log. Now it's a job like everything else.

    Params: markdown (required), url, captured_at.
    """
    from intake.products import review_sources
    params = job.get("params") or {}
    md = params.get("markdown") or ""
    url = (params.get("url") or "").strip()
    if not md.strip():
        raise ValueError("review_ingest requires params.markdown")
    print(f"[REVIEW-INGEST] {url or '(no url)'} — {len(md)} chars")

    def _run():
        with _db() as conn:
            return review_sources.ingest_review(
                conn, md, url=url, captured_at=params.get("captured_at") or "")

    review = await asyncio.to_thread(_run)
    summary = {"review_id": review.get("review_id"), "reviewer": review.get("reviewer", ""),
               "title": review.get("title", ""), "url": url,
               "product_count": len(review.get("products") or [])}
    print(f"[REVIEW-INGEST] {summary}")
    return summary


jobs_lib.register_handler("review_ingest", _handle_review_ingest_job)


async def _handle_domain_scoring_job(job: dict) -> dict:
    """System-wide domain scoring: refit ONE global PA~DA quadratic over the whole
    corpus (every dish cohort point + every publisher member), then rescore every
    publisher member's rank_score against it (paywall-remapped PA → OU/power blend,
    percentiled system-wide). Replaces raw-PA ranking with a cross-publisher-
    comparable authority score. Schedulable (the corpus drifts slowly — weekly).
    See input/pipeline/domain_scoring.py + docs/domain-scoring.md."""
    def _run():
        from input.pipeline import domain_scoring
        from input.pipeline.config import POWER_BLEND_WEIGHT
        with _db() as conn:
            return domain_scoring.recompute_and_rescore(conn, weight=POWER_BLEND_WEIGHT)

    summary = await asyncio.to_thread(_run)
    fit = summary.get("fit") or {}
    print(f"[DOMAIN-SCORING] fit used={fit.get('used')} n={fit.get('n')} "
          f"coeffs={fit.get('coefficients')} paywall_cal={fit.get('n_paywall_calibrated')} "
          f"| rescored {summary.get('members_rescored')} members "
          f"across {summary.get('collections')} collections")
    return summary


jobs_lib.register_handler("domain_scoring", _handle_domain_scoring_job)


async def _handle_paid_pa_calibration_job(job: dict) -> dict:
    """Recompute the per-publisher paywall PA-remap calibration.

    WHY THIS IS A JOB. Gated publishers' recipe PAGES are link-starved (paywall →
    few backlinks → low Moz PA) while their DOMAIN authority stays high, so OU
    reads them as under-performing and the selector drops them. The fix is a
    per-publisher shift-and-scale of PA onto its free-equivalent, calibrated from
    the publisher's own PA distribution against matched-DA free publishers. That
    calibration is a SNAPSHOT of two moving distributions: PA drifts as links
    accrue, and the free baseline moves as the corpus grows. Left alone it goes
    stale silently — as it did between 2026-06-23 and 2026-08-09, when all four
    stored calibrations were seven weeks old and nobody noticed, because a stale
    remap fails quietly by mis-ranking rather than by erroring.

    WHAT IT DOES, per domain flagged `paywall=1` in the domains master:
      1. resolve that publisher's recipe-page PA samples — ingested corpus rows
         first, then already-harvested collection_members (both free)
      2. compute its PA mean + spread (mu_paid, sigma_paid)
      3. find the matched-DA FREE reference (free publishers within +/-8 DA) for
         mu_free, sigma_free — paywall-flagged hosts are excluded from that
         baseline so a gated publisher never calibrates against itself
      4. floor sigma_paid at 0.5 x sigma_free, capping the slope at 2.0
      5. require n >= 15 samples, else refuse and report why
      6. persist mu/sigma/n + the free reference onto the domains row
    Scoring then applies `adjusted_PA = max(pa, mu_free + (pa - mu_paid) *
    (sigma_free/sigma_paid))` — one-directional, so a publisher already above the
    free line (NYT) is untouched.

    SPEND. `harvest_missing` (default FALSE) controls the live SERP+Moz fallback
    for publishers with too few local samples. It is off deliberately: this runs
    unattended, and a scheduled job must never surprise-spend. Turn it on per-run
    from the params if a newly-flagged publisher has no corpus footprint yet.

    The result carries a per-domain `results` list including the REASON a
    publisher was skipped (too_few_samples / no_data / no_free_reference), so the
    Job Monitor shows why coverage failed to grow rather than just a count.
    See memory/project_paid_pa_calibration.

    REWIRED 2026-08-12 to pa_gap_v1 (input/pipeline/paywall_calibration.py). The
    docstring above describes the SUPERSEDED shift-and-scale method, which this
    job used to run monthly; leaving it wired would have resurrected the old
    pa_cal_* remap on the next scheduled tick and re-introduced the impossible
    scores it produced. The current method instead measures the PA gap against
    free peers at matched DA and converts it to a DA haircut, gated on effect
    size and peer-window stability, then re-stamps every affected recipe row so
    the stored adjustment tracks the new calibration.

    No SERP/Moz spend at all — the corpus is the sample — so `harvest_missing`
    no longer applies and is ignored.
    """
    p = job.get("params") or {}
    dry_run = bool(p.get("dry_run"))

    def _run():
        from input.pipeline import paywall_calibration
        with _db() as conn:
            return paywall_calibration.calibrate(conn, persist=not dry_run)

    summary = await asyncio.to_thread(_run)
    skipped = [f"{r.get('domain')}({r.get('status')})"
               for r in summary.get("results", [])
               if r.get("status") != "adjusted"]
    print(f"[PAYWALL-CAL] method={summary.get('method')} "
          f"flagged={summary.get('flagged')} adjusted={summary.get('adjusted')} "
          f"corpus={summary.get('corpus_rows')} free={summary.get('free_rows')} "
          f"restamped={summary.get('restamped')}{'  DRY RUN' if dry_run else ''}")
    if skipped:
        print(f"[PAYWALL-CAL] not adjusted: {', '.join(skipped)}")
    return summary


jobs_lib.register_handler("paywall_calibration", _handle_paid_pa_calibration_job)
# The pre-2026-08-12 job type, kept registered so historical `jobs` rows and any
# job queued under the old name still resolve to a handler. The name said "PA"
# but the job now discounts DA — renaming without this alias would turn a
# re-run of an old row into an unhandled-type failure.
jobs_lib.register_handler("paid_pa_calibration", _handle_paid_pa_calibration_job)


def _size_face(size):
    """Coerce a size value to a plain display string. `_cook` equipment sizes are
    measurement DICTS ({imperial, metric, convertible}); the top-level `equipment.size`
    (Tool.size: Optional[str]) MUST be a string, else a raw dict leaks into the recipe
    and serializes as "{'imperial': …}". Prefer the imperial face (source of truth)."""
    if isinstance(size, dict):
        return (str(size.get("imperial") or size.get("metric") or "")).strip() or None
    if isinstance(size, str):
        return size.strip() or None
    return None


def _recipe_equipment_from_cook(cook_equipment) -> list:
    """Mirror the cook-rework's inferred tools (`_cook.equipment`: id/name/size) into
    the recipe's top-level schema `equipment` (HowToTool). Makes the tools REAL recipe
    data — the recipe editor shows them AND the product-commerce match keys off them
    (equipment -> product_class; `size` is the class grain, e.g. "Saucepans (2 qt)").
    Carries `size` (coerced to the imperial-face STRING) when present. Deduped by name."""
    # Guard against a stringified list (would char-explode into single-letter
    # HowToTools, deduped — see enrich.equipment.derive_equipment for the same fix).
    if isinstance(cook_equipment, str):
        try:
            cook_equipment = json.loads(cook_equipment)
        except Exception:
            cook_equipment = []
    if not isinstance(cook_equipment, list):
        cook_equipment = []
    out, seen = [], set()
    for e in cook_equipment:
        name = ((e.get("name") if isinstance(e, dict) else getattr(e, "name", None)) or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        item = {"@type": "HowToTool", "name": name}
        size = _size_face(e.get("size") if isinstance(e, dict) else getattr(e, "size", None))
        if size:
            item["size"] = size
        out.append(item)
    return out


def _ensure_equipment(recipe: dict, *, path_used: str = "") -> None:
    """Guarantee a top-level `equipment` list on an extracted recipe, in place. The
    JSON-LD fast lane (`jsonld_to_recipe`) emits NO equipment (JSON-LD rarely lists
    tools), and the enrich() `do_equipment` fallback is skipped on the legacy extract
    path (_USE_ENRICHMENT_API off), so a fast-lane recipe would reach the form with no
    equipment (the shrimp-creole bug). Derive it here when empty. Markdown-LLM extracts
    already carry it (their prompt derives it) -> no-op. Best-effort; never raises."""
    if not recipe or (recipe.get("equipment") or []):
        return
    try:
        from enrich.equipment import derive_equipment
        eq = derive_equipment(recipe)
        if eq:
            recipe["equipment"] = eq
            print(f"[EXTRACT] equipment derived ({len(eq)} tools)"
                  + (f" [{path_used} had none]" if path_used else ""))
    except Exception as e:
        print(f"[EXTRACT] equipment derive failed: {type(e).__name__}: {e}")


def _finalize_extract_recipe(recipe, *, url_norm=None, usage_log=None) -> None:
    """The per-recipe enrichment triplet every extract endpoint runs AFTER the extract
    cache write: cookbook chapter + Moz PA/DA/OU scoring + dish identity card. Converges
    the identical block that was copy-pasted across extract-from-{image,pdf,markdown}
    (each step idempotent / best-effort). Equipment is deliberately NOT here — it runs
    BEFORE the cache write (see _ensure_equipment) so a fast-lane recipe's derived tools
    are cached; chapter/moz/identity intentionally re-stamp per serve as before."""
    _attach_chapter(recipe, usage_log=usage_log)
    _attach_moz_scoring(recipe, url_norm)
    _attach_identity_card(recipe, usage_log=usage_log)


# A BCC self-permalink — our own page, nothing to screenshot.
_SELF_PERMALINK_RE = re.compile(
    r"/r/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _stored_screenshot_url(url_norm: str) -> Optional[str]:
    """`/screenshot/<id>` if a blob for this URL is ALREADY in media.db, else None.

    The id is a deterministic hash of url_normalized, so existence is a cheap
    lookup with no capture. Three callers need exactly this question: the capture
    helper (so a URL we already shot is never re-shot), the /screenshot-status
    poll the form uses while a deferred capture runs, and the save backstop.
    """
    if not url_norm:
        return None
    try:
        from input.pipeline.screenshot_pipeline import screenshot_id_for, read_screenshot_blob
        sid = screenshot_id_for(url_norm)
        if read_screenshot_blob(MEDIA_DB_PATH, sid):
            return f"/screenshot/{sid}"
    except Exception:
        pass
    return None


def _attach_page_screenshot(recipe, url: str, url_norm: str, timings=None,
                            *, force: bool = False, defer: bool = False) -> None:
    """Capture the source page and stamp `_source.pageScreenshot`.

    Stored as a compact JPEG BLOB in media.db keyed by url_normalized; the
    recipe carries only the short `/screenshot/<id>` URL the blob endpoint
    serves. Durable — image_store's generated/ is git-ignored + ephemeral.

    SHARED (2026-08-06) between the URL path and the markdown/bookmarklet path.
    It had lived inline in extract_recipe_from_url only, so the bookmarklet —
    which stages HTML client-side and lands on /extract-from-markdown — never
    attempted a capture at all. That is why personal saves sat at 172/412 while
    master ran 3927/4356, with NO "capture failed" lines anywhere: nothing was
    failing, nothing was being tried. Call this BEFORE the extract-cache write
    on every path, so the screenshot travels with the cached row.

    Idempotent and best-effort: a row that already has one is left alone (guards
    a cache hit from re-capturing), and any failure is swallowed — a missing
    screenshot must never cost the caller their extraction.
    """
    if not url or not url_norm:
        return
    if not url.startswith(("http://", "https://")):
        return
    if _SELF_PERMALINK_RE.search(url):
        return
    # `force` is the RECAPTURE path (the screenshot_refresh job): the blob key
    # is deterministic from url_normalized, so a re-shoot overwrites the same
    # media.db row and the recipe's /screenshot/<id> URL never changes.
    if not force and (recipe.get("_source") or {}).get("pageScreenshot"):
        return
    # Already shot this URL? Stamp it for free. Without this, a deferred capture
    # would be re-run on every later extract of the same page (a cache hit no
    # longer carries pageScreenshot into the cached row), paying 25s of Chromium
    # to produce a blob we are already holding.
    if not force:
        existing = _stored_screenshot_url(url_norm)
        if existing:
            src = recipe.get("_source") or {}
            src["pageScreenshot"] = existing
            recipe["_source"] = src
            if timings is not None:
                timings["screenshot_ms"] = 0
            return
    if defer:
        # DEFERRED: hand the capture to a background thread and return now.
        #
        # Measured 2026-08-11 on the live log: the capture is 60-75% of an
        # interactive extract — 22s of 27s, worst case 31s of 43s — while a
        # cache-warm extract is 6s. A user watching a spinner for 40 seconds
        # reloads, which is exactly what happened on an allrecipes egg foo
        # young: the server finished at 08:22:49 and the browser had already
        # gone, so the work was done and thrown away, and the retry looked
        # instant because the blob was by then stored.
        #
        # Nothing is stamped on the recipe here. The id is derivable from
        # url_normalized, so we COULD stamp the /screenshot/<id> URL up front —
        # but 45 of 45 captures failed in one recent refresh job, and stamping a
        # URL for a blob that may never exist asserts something false
        # ([[absent-is-not-zero]]). The client polls /screenshot-status instead,
        # and _backfill_screenshot_on_save is the backstop for whoever does not.
        def _bg():
            try:
                # Re-check on the way in. The bookmarklet uploads its own
                # browser-rendered capture (/stage-image) at roughly the same
                # moment this starts, and that one is strictly better — it is the
                # page as the USER saw it, signed in, with no paywall over it.
                # Without this the anonymous capture finishes 5-31s later and
                # overwrites it, which is precisely the ATK complaint.
                if _stored_screenshot_url(url_norm):
                    print(f"[SCREENSHOT] deferred capture skipped for {url_norm} — "
                          f"a browser-rendered capture arrived first")
                    return
                from input.pipeline.screenshot_pipeline import capture_and_store_blob
                shot = capture_and_store_blob(url, url_norm, MEDIA_DB_PATH)
                print(f"[SCREENSHOT] deferred capture {'stored ' + shot if shot else 'FAILED'} "
                      f"for {url_norm}")
            except Exception as e:
                print(f"[SCREENSHOT] deferred capture failed for {url_norm}: {e}")
        threading.Thread(target=_bg, name=f"shot:{url_norm[:40]}", daemon=True).start()
        if timings is not None:
            timings["screenshot_ms"] = 0     # honestly zero: we did not wait
        return
    try:
        from input.pipeline.screenshot_pipeline import capture_and_store_blob
        t_shot = time.perf_counter()
        shot_url = capture_and_store_blob(url, url_norm, MEDIA_DB_PATH)
        if timings is not None:
            timings["screenshot_ms"] = int((time.perf_counter() - t_shot) * 1000)
        if shot_url:
            src = recipe.get("_source") or {}
            src["pageScreenshot"] = shot_url
            recipe["_source"] = src
            print(f"[SCREENSHOT] stored blob: {shot_url}")
        else:
            print(f"[SCREENSHOT] no capture for {url_norm}")
    except Exception as e:
        print(f"[SCREENSHOT] capture failed (continuing): {e}")


def _stamp_translation_provenance(recipe, meta) -> None:
    """Stamp `_source` translation provenance (originalLanguage/translated/translatedAt/
    originalTitle) from a translation meta dict. Was duplicated in the bookmarklet and
    URL extract paths. `_SOURCE_STATIC_SUBKEYS` whitelists these for cache/claim survival."""
    if not recipe or not meta:
        return
    src = recipe.get("_source") or {}
    src["originalLanguage"] = meta.get("originalLanguage", "")
    src["translated"] = True
    src["translatedAt"] = meta.get("translatedAt", "")
    if meta.get("originalTitle"):
        src["originalTitle"] = meta["originalTitle"]
    recipe["_source"] = src


def _extract_response(recipe, *, new_recipe_id, timings, prompts, usage_log,
                      t_start, user_id, ok_msg="Extraction successful") -> dict:
    """The shared extract-endpoint response envelope: journal token usage, stamp source
    drift, record true wall-clock total, and return the `{success, recipe_id, recipe,
    _timings, _prompt, _usage}` dict. `recipe['id']` is stamped by the caller (its position
    differs per endpoint). Converges the identical return tails of extract-from-{image,pdf,
    markdown}. `ok_msg` sets the [OK] log line."""
    _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
    _maybe_stamp_source_drift(timings, user_id=user_id)
    timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
    print(f"[OK] {ok_msg}")
    return {
        "success": True,
        "recipe_id": new_recipe_id,
        "recipe": recipe,
        "_timings": timings,
        "_prompt": prompts,
        "_usage": usage_log,
    }


async def _handle_cook_rework_job(job: dict) -> dict:
    """Cook-rework: turn a captured recipe into a validated `_cook` block
    (cook_rework.rework_recipe) and persist it ONLY when the §5 gauntlet passes.
    SHARED engine — works on a master row (user_id=0) or a personal row. Logs
    each pass + the gate result via print() (captured to the job log)."""
    params = job.get("params") or {}
    recipe_id = params.get("recipe_id")
    user_id = int(params.get("user_id", 0) or 0)
    if not recipe_id:
        raise ValueError("cook_rework requires params.recipe_id")
    table = _recipes_table_for(user_id)

    def _load():
        with _db() as conn:
            row = conn.execute(
                f"SELECT data FROM {table} WHERE recipe_id = ? AND user_id = ?",
                (recipe_id, user_id)).fetchone()
        return json.loads(row[0]) if row else None

    recipe = await asyncio.to_thread(_load)
    if recipe is None:
        raise ValueError(f"recipe {recipe_id} not found in {table} (user_id={user_id})")
    print(f"[COOK-REWORK] {recipe.get('name')!r} (user_id={user_id}, table={table})")

    from cook_rework import rework_recipe, REWORK_PROMPT_VERSION
    import llm
    # Stamp every LLM call inside the rework (opus emit + repair + sonnet augment) with
    # this recipe/user so their token usage lands in bcc_token_journal. asyncio.to_thread
    # propagates the contextvar to the worker; the buffer flushes on `with` exit.
    with llm.context(recipe_id=recipe_id, user_id=user_id):
        cook, report = await asyncio.to_thread(rework_recipe, recipe, print)

    if not report.passed:
        print(f"[COOK-REWORK] gauntlet FAILED ({len(report.failures)}) — NOT persisting. "
              f"First failures: {report.failures[:5]}")
        return {"persisted": False, "passed": False, "failures": report.failures}

    cook.rework_prompt_version = REWORK_PROMPT_VERSION
    cook.reworked_at = datetime.now(timezone.utc).isoformat()

    def _save():
        with _db() as conn:
            cur = json.loads(conn.execute(
                f"SELECT data FROM {table} WHERE recipe_id = ? AND user_id = ?",
                (recipe_id, user_id)).fetchone()[0])
            ck_dump = cook.model_dump()
            cur["_cook"] = ck_dump
            # Mirror the inferred tools into the recipe's REAL top-level `equipment`
            # (HowToTool + size) so the editor shows them and product-commerce can key
            # off equipment -> product_class. The rework is the authoritative derivation,
            # so a re-rework re-syncs it.
            cur["equipment"] = _recipe_equipment_from_cook(ck_dump.get("equipment"))
            conn.execute(
                f"UPDATE {table} SET data = ?, updated_at = ? WHERE recipe_id = ? AND user_id = ?",
                (json.dumps(cur, ensure_ascii=False), cook.reworked_at, recipe_id, user_id))
            conn.commit()

    await asyncio.to_thread(_save)
    print(f"[COOK-REWORK] persisted _cook — {len(cook.steps)} steps, "
          f"{len(cook.bundles)} bundles, {len(cook.reserved)} put-asides; gauntlet PASSED")
    return {"persisted": True, "passed": True, "steps": len(cook.steps),
            "bundles": len(cook.bundles)}


jobs_lib.register_handler("cook_rework", _handle_cook_rework_job)


@app.post("/recipes/{recipe_id}/cook-rework")
def cook_rework_endpoint(recipe_id: str, request: Request, user_id: int = PLACEHOLDER_USER_ID):
    """Kick off a cook-rework, OUT-OF-PROCESS. It's a 30s-2min multi-pass frontier
    job that must survive a server restart (the dal-makhani scar), dodge tunnel
    request timeouts, stream a live log, and batch — exactly the dish_refresh
    class of work, so it reuses that enqueue + Popen-spawn + SSE-stream infra.
    SHARED: master rows (user_id=0) need edit_master; a personal row is the user's
    own. Returns the spawned job + its stream url."""
    if user_id == 0:
        _require_perm(request, "edit_master")
    table = _recipes_table_for(user_id)
    with _db() as conn:
        row = conn.execute(
            f"SELECT json_extract(data, '$.name') FROM {table} "
            f"WHERE recipe_id = ? AND user_id = ?", (recipe_id, user_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Recipe not found")
        # log_label puts the recipe NAME in the log filename (entity_ref is the
        # opaque UUID, needed for locking) — see jobs._build_log_filename.
        job_id = jobs_lib.enqueue_job(
            conn, type="cook_rework",
            params={"recipe_id": recipe_id, "user_id": user_id,
                    "log_label": (row[0] or recipe_id)},
            entity_ref=f"cook:{recipe_id}")
    import subprocess
    proj = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ); env["PYTHONIOENCODING"] = "utf-8"; env["PYTHONUNBUFFERED"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jobs", "exec", "--job-id", str(job_id)],
            cwd=proj, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_detached_flags(),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to spawn runner: {e}") from e
    return {"recipe_id": recipe_id, "job_id": job_id, "spawned": True,
            "stream_url": f"/jobs/{job_id}/stream"}


@app.post("/recipes/{recipe_id}/notes-ask")
def notes_ask_endpoint(recipe_id: str, payload: dict = Body(...)):
    """Chef's-Notes chat: a grounded, recipe-context Q&A whose answer the user keeps
    as a Chef's Notes block. Reuses cook_ask's grounding (this recipe + our KB +
    general cooking knowledge, brand-safe, cooking-only), but journals under
    operation='notes_chat' so this feature's spend is legible per-feature AND
    per-user in bcc_token_journal. Personal, on-device content (the split's local
    'possess & use' side) — never flows to the corpus. Degrades to a friendly 503."""
    question = (payload.get("question") or "").strip()
    try:
        user_id = int(payload.get("user_id", PLACEHOLDER_USER_ID))
    except (TypeError, ValueError):
        user_id = PLACEHOLDER_USER_ID
    if not question:
        raise HTTPException(status_code=400, detail="question is required.")

    # Grounding source, in priority order:
    #   1. the SAVED row (richest — carries `_cook`, KB links, etc.)
    #   2. the form's CURRENT recipe context sent in the payload (`recipe`) — lets
    #      the user ask BEFORE the recipe is ever saved (un-gates the ✨ Ask button;
    #      cook_ask.build_context falls back to name/ingredients/instructions/notes).
    # So a brand-new, never-saved recipe still gets a grounded answer instead of a
    # silent 404. See project_notes_chat.
    recipe = None
    if recipe_id and recipe_id != "_new":
        table = _recipes_table_for(user_id)
        with _db() as conn:
            row = conn.execute(
                f"SELECT data FROM {table} WHERE recipe_id = ?", (recipe_id,)
            ).fetchone()
        if row:
            recipe = json.loads(row[0])
    if recipe is None:
        form_recipe = payload.get("recipe")
        if isinstance(form_recipe, dict) and (
            form_recipe.get("recipeIngredient") or form_recipe.get("recipeInstructions")
            or form_recipe.get("name")
        ):
            recipe = form_recipe
    if recipe is None:
        raise HTTPException(
            status_code=400,
            detail="Add a name, ingredients, or steps first so Chef has something to answer about.",
        )

    import llm  # gateway: attribute this Q&A to the recipe/user, label it notes_chat
    llm.enter(recipe_id=(recipe_id or "_new"), user_id=user_id)
    try:
        from cook_ask import ask as chef_ask
        answer = chef_ask(recipe, question, operation="notes_chat")
    except Exception as e:
        print(f"[ERROR] /recipes/{recipe_id}/notes-ask: {e}")
        raise HTTPException(status_code=503, detail="Chef is unavailable right now — try again in a moment.") from e
    finally:
        llm.flush()   # write the buffered notes_chat usage to the journal
    return {"answer": answer}


# NOTE: the manual POST /recipes/{id}/derive-equipment endpoint was removed —
# equipment is now derived automatically at extract time (enrich/api.py folds it into
# the markdown-LLM prompt and fast-lane fallback). The shared derivation lives on in
# enrich/equipment.py::derive_equipment (used by the extract path + the batch backfill).


@app.get("/recipes/{recipe_id}/equipment-products")
def equipment_products_endpoint(recipe_id: str, user_id: int = PLACEHOLDER_USER_ID):
    """Map each of the recipe's `equipment` items to a Williams-Sonoma product category
    (the `product_class`) via the `ws_categories` embeddings — the commerce bridge from a
    recipe's tools to purchasable products. Cosine over ~186 WS categories (built by
    scripts/build_ws_taxonomy.py); returns per-item best category + score + sample products.
    See intake/products/equipment_match + docs/equipment-product-linking.md."""
    table = _recipes_table_for(user_id)
    with _db() as conn:
        row = conn.execute(
            f"SELECT data FROM {table} WHERE recipe_id = ? AND user_id = ?",
            (recipe_id, user_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Recipe not found.")
        recipe = json.loads(row[0])
        try:
            from intake.products.equipment_match import match_recipe_equipment
            from intake.products.category_link import products_for_ws_category
            matches = match_recipe_equipment(recipe, conn)
            # Attach catalog products per matched category (equipment → category → products).
            for m in matches:
                if m.get("matched") and m.get("ws_category_id"):
                    m["catalog_products"] = products_for_ws_category(conn, m["ws_category_id"], limit=6)
        except Exception as e:
            print(f"[ERROR] /recipes/{recipe_id}/equipment-products: {e}")
            raise HTTPException(status_code=503, detail="Equipment matching is unavailable right now.") from e
    return {"recipe_id": recipe_id, "count": len(matches), "equipment_matches": matches}


@app.get("/ws-categories")
def list_ws_categories_endpoint():
    """The Williams-Sonoma product taxonomy (ws_categories) for the admin viewer — WS's own
    headline/subcategory hierarchy + description + sample products. Omits the embedding BLOB;
    reports has_embedding + a product count. Built by scripts/build_ws_taxonomy.py."""
    with _db() as conn:
        try:
            rows = conn.execute(
                "SELECT id, headline, section, subcategory, leaf, ws_path, url, description, "
                "products_sample, source, (embedding IS NOT NULL) FROM ws_categories "
                "ORDER BY headline, section, subcategory, leaf"
            ).fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"ws_categories unavailable: {e}") from e
    out = []
    for rid, hl, sec, sub, leaf, path, url, desc, samp, source, has in rows:
        prods = [p for p in (samp or "").split("; ") if p.strip()]
        out.append({"id": rid, "headline": hl, "section": sec, "subcategory": sub, "leaf": leaf,
                    "ws_path": path, "url": url, "description": desc, "products": prods,
                    "product_count": len(prods), "has_embedding": bool(has),
                    "source": source or "ws", "curator_added": (source == "curator")})
    return out


@app.get("/ws-categories/match")
def match_ws_categories_endpoint(q: str, k: int = 5):
    """Test the equipment→category matcher: the LLM classification of `q` into the taxonomy
    (the real answer, cached in tool_term_map) PLUS the embedding candidate shortlist for
    context. Powers the taxonomy viewer's 'Test a term' box."""
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="q is required.")
    with _db() as conn:
        try:
            from intake.products.equipment_match import match_equipment_name
            res = match_equipment_name(q, conn, k=max(1, min(int(k), 10)))
        except Exception as e:
            print(f"[ERROR] /ws-categories/match: {e}")
            raise HTTPException(status_code=503, detail="Matching is unavailable right now.") from e
    return {"query": q, **res}


def _ws_path(headline, section, subcategory, leaf) -> str:
    """Full display path from the 4 levels present (blank levels skipped)."""
    return " > ".join(p for p in (headline, section, subcategory, leaf) if (p or "").strip())


def _ws_embed_blob(path: str, description: str, products: str) -> bytes:
    """Embed a WS category the SAME way scripts/build_ws_taxonomy.py does — full path +
    description + sample products — so a curator LEAF matches on equal footing with the
    scraped rows."""
    from input.pipeline.embeddings import embed_text, vec_to_bytes
    text = f"{path}."
    if description:
        text += f" {description}"
    if products:
        text += f" Sample products: {products}"
    return vec_to_bytes(embed_text(text))


def _ws_category_row(rid, headline, section, subcategory, leaf, path, url, description,
                     products, source) -> dict:
    prods = [p for p in (products or "").split("; ") if p.strip()]
    return {"id": rid, "headline": headline, "section": section, "subcategory": subcategory,
            "leaf": leaf, "ws_path": path, "url": url, "description": description,
            "products": prods, "product_count": len(prods), "has_embedding": True,
            "source": source, "curator_added": (source == "curator")}


def _invalidate_tool_term_cache(conn) -> None:
    """Clear the LLM term→category cache after a taxonomy change so added/edited/deleted
    categories (and curator leaves) are reconsidered on the next match. Best-effort."""
    try:
        from intake.products.equipment_match import clear_term_cache
        n = clear_term_cache(conn)
        if n:
            print(f"[WS-TAXONOMY] cleared {n} cached term classifications (taxonomy changed)")
    except Exception as e:
        print(f"[WS-TAXONOMY] term-cache clear skipped: {e}")


@app.post("/ws-categories")
def create_ws_category_endpoint(payload: dict = Body(...)):
    """Curator adds a node to the WS taxonomy inline (from the taxonomy viewer) when the
    matcher misses — typically a LEAF (L4) under an existing subcategory (compost bags, glass
    food-storage containers), but any of headline/section/subcategory/leaf may be supplied.
    Persists WITH its embedding so the very next match can hit it. url is NULL (WS has no page
    for it); source='curator'. No restart needed — the matcher reloads the table each call."""
    headline = (payload.get("headline") or "").strip()
    section = (payload.get("section") or "").strip()
    subcategory = (payload.get("subcategory") or "").strip()
    leaf = (payload.get("leaf") or "").strip()
    description = (payload.get("description") or "").strip()
    products = (payload.get("products_sample") or "").strip()
    if not headline or not (section or subcategory or leaf):
        raise HTTPException(status_code=400,
                            detail="headline plus at least one of section/subcategory/leaf are required.")
    path = _ws_path(headline, section, subcategory, leaf)
    try:
        emb = _ws_embed_blob(path, description, products)
    except Exception as e:
        print(f"[ERROR] create ws-category embed: {e}")
        raise HTTPException(status_code=503, detail="Embedding is unavailable right now.") from e
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO ws_categories (headline, section, subcategory, leaf, ws_path, url, "
            "description, products_sample, embedding, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'curator', strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
            (headline, section or None, subcategory or None, leaf or None, path, None,
             description or None, products or None, emb))
        conn.commit()
        rid = cur.lastrowid
        _invalidate_tool_term_cache(conn)   # new category -> reconsider classifications
    print(f"[WS-TAXONOMY] curator added '{path}' (embedded)")
    return _ws_category_row(rid, headline, section or None, subcategory or None, leaf or None,
                            path, None, description, products, "curator")


@app.put("/ws-categories/{cat_id}")
def update_ws_category_endpoint(cat_id: int, payload: dict = Body(...)):
    """Edit a node (leaf/description/products) and RE-EMBED so a refinement immediately
    changes what it matches. Used to sharpen a near-miss (e.g. add a leaf/description so
    'glass storage containers' lands right)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT headline, section, subcategory, leaf, url, description, products_sample, source "
            "FROM ws_categories WHERE id = ?", (cat_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Category not found.")
        cur_hl, cur_sec, cur_sub, cur_leaf, url, cur_desc, cur_prod, source = row
        def _pick(key, cur):
            v = payload.get(key)
            return (v if v is not None else (cur or "")).strip()
        headline = _pick("headline", cur_hl) or cur_hl
        section = _pick("section", cur_sec)
        subcategory = _pick("subcategory", cur_sub)
        leaf = _pick("leaf", cur_leaf)
        description = _pick("description", cur_desc)
        products = _pick("products_sample", cur_prod)
        path = _ws_path(headline, section, subcategory, leaf)
        try:
            emb = _ws_embed_blob(path, description, products)
        except Exception as e:
            print(f"[ERROR] update ws-category embed: {e}")
            raise HTTPException(status_code=503, detail="Embedding is unavailable right now.") from e
        conn.execute(
            "UPDATE ws_categories SET headline=?, section=?, subcategory=?, leaf=?, ws_path=?, "
            "description=?, products_sample=?, embedding=? WHERE id=?",
            (headline, section or None, subcategory or None, leaf or None, path,
             description or None, products or None, emb, cat_id))
        conn.commit()
        _invalidate_tool_term_cache(conn)   # edited category -> reconsider classifications
    print(f"[WS-TAXONOMY] curator edited #{cat_id} -> '{path}' (re-embedded)")
    return _ws_category_row(cat_id, headline, section or None, subcategory or None,
                            leaf or None, path, url, description, products, source or "ws")


@app.delete("/ws-categories/{cat_id}")
def delete_ws_category_endpoint(cat_id: int):
    """Remove a taxonomy node. Curator leaves are gone for good; a WS-scraped row will
    reappear on the next scrape (the scrape only clears source='ws'). Returns the deleted
    row's path for the UI toast."""
    with _db() as conn:
        row = conn.execute("SELECT ws_path, source FROM ws_categories WHERE id = ?",
                           (cat_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Category not found.")
        conn.execute("DELETE FROM ws_categories WHERE id = ?", (cat_id,))
        conn.commit()
        _invalidate_tool_term_cache(conn)   # removed category -> reconsider classifications
    print(f"[WS-TAXONOMY] deleted #{cat_id} '{row[0]}' (source={row[1]})")
    return {"deleted": cat_id, "ws_path": row[0], "source": row[1] or "ws"}


@app.post("/product-classes/relink")
def relink_product_classes_endpoint():
    """Auto-map every catalog product_class → its nearest ws_category by embedding (the
    commerce join's last hop). Preserves curator (manual) mappings. See
    intake/products/category_link + docs/equipment-product-linking.md."""
    with _db() as conn:
        try:
            from intake.products.category_link import relink_product_classes
            res = relink_product_classes(conn)
        except Exception as e:
            print(f"[ERROR] /product-classes/relink: {e}")
            raise HTTPException(status_code=503, detail=f"Relink failed: {e}") from e
    return res


@app.get("/ws-categories/{cat_id}/products")
def ws_category_products_endpoint(cat_id: int, limit: int = 12):
    """Catalog products linked to a WS category (via product_class → ws_category map),
    best first. Powers the recipe's 'Shop the tools' + the taxonomy viewer."""
    with _db() as conn:
        try:
            from intake.products.category_link import products_for_ws_category
            prods = products_for_ws_category(conn, cat_id, limit=max(1, min(int(limit), 50)))
        except Exception as e:
            print(f"[ERROR] /ws-categories/{cat_id}/products: {e}")
            raise HTTPException(status_code=503, detail="Product lookup unavailable.") from e
    return {"ws_category_id": cat_id, "count": len(prods), "products": prods}


def _inject_dish_competitiveness(recipe: dict) -> None:
    """Stamp the dish's LIVE (nightly-rolled-up) in-chapter competitiveness onto
    _scoring transiently, just before enrich, so the editorial commentary
    reflects the CURRENT chapter standing rather than a frozen snapshot.
    Best-effort — a miss just means the commentary skips that angle."""
    try:
        dish = ((recipe.get("_master") or {}).get("dish") or "").strip()
        if not dish:
            return
        with _db() as conn:
            row = conn.execute(
                "SELECT competitiveness_pct FROM dishes WHERE name = ?", (dish,)
            ).fetchone()
        if row and row[0] is not None:
            recipe.setdefault("_scoring", {})["dishCompetitivenessPct"] = float(row[0])
    except Exception as e:
        print(f"[ENRICH] competitiveness lookup failed (non-fatal): {e}")


@app.post("/dishes/{name}/refresh")
async def refresh_dish_endpoint(name: str, request: Request):
    _require_perm(request, "refresh_dishes")
    """Enqueue a dish_refresh job. Returns 202 with the job_id immediately
    — no long-held HTTP, no Cloudflare 100s timeout. The browser then
    opens an SSE stream at GET /jobs/<id>/stream to watch progress, or
    polls GET /jobs/<id>.

    Refuses if a job for this dish is already queued or running (409,
    with the existing job_id in the response so the UI can attach to
    that stream instead)."""
    with _db() as conn:
        dish = dishes_lib.get_dish(conn, name)
    if dish is None:
        raise HTTPException(status_code=404, detail="Dish not found")
    if not dish["queries"]:
        raise HTTPException(status_code=400,
                            detail=f"Dish {name!r} has no queries")

    entity_ref = f"dish:{dish['name']}"
    with _db() as conn:
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "already in flight",
                    "job_id": existing["id"],
                    "status": existing["status"],
                    "log_filename": existing.get("log_filename"),
                },
            )
        job_id = jobs_lib.enqueue_job(
            conn,
            type="dish_refresh",
            params={"dish_name": dish["name"]},
            entity_ref=entity_ref,
        )

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "queued",
            "entity_ref": entity_ref,
            "stream_url": f"/jobs/{job_id}/stream",
            "status_url": f"/jobs/{job_id}",
        },
    )


# =========================================================================
# Jobs endpoints (generic — usable by any future job type + the future
# /forms/jobs.html admin page)
# =========================================================================

@app.get("/jobs")
def list_jobs_endpoint(type: Optional[str] = None,
                       entity_ref: Optional[str] = None,
                       status: Optional[str] = None,
                       limit: int = 100):
    """List jobs, optionally filtered. Newest first."""
    with _db() as conn:
        return jobs_lib.list_jobs(
            conn, type=type, entity_ref=entity_ref,
            status=status, limit=limit,
        )


@app.get("/jobs/{job_id}")
def get_job_endpoint(job_id: int):
    """Single-job status. Polled by UIs that don't use the SSE stream."""
    with _db() as conn:
        job = jobs_lib.get_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/jobs/{job_id}/spawn")
def spawn_job_endpoint(job_id: int, request: Request):
    """Run an already-enqueued job OUT-OF-PROCESS: Popen `python -m jobs exec
    --job-id N` and return immediately. The child owns its own stdout (the
    per-job log file) and never touches uvicorn's event loop, so the UI stays
    responsive and a server restart can't kill the job (recipes.db is WAL, so
    the child + server write concurrently). The browser tails the same log via
    GET /jobs/{id}/stream — the SSE reads the DB + the log file, which is fully
    cross-process. This is the phase-4 UI path (docs/jobs-as-executables.md)."""
    _require_perm(request, "refresh_dishes")
    with _db() as conn:
        job = jobs_lib.get_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("queued", "running"):
        raise HTTPException(status_code=409,
                            detail=f"Job #{job_id} is {job['status']}, not runnable")
    _spawn_job_runner(job_id)
    return {"job_id": job_id, "spawned": True, "stream_url": f"/jobs/{job_id}/stream"}


def _spawn_job_runner(job_id: int) -> None:
    """Popen `python -m jobs exec --job-id N`, detached. Shared by /jobs/{id}/spawn and by
    the endpoints that enqueue-and-launch in one call (e.g. /products/{id}/realrank), so
    there is ONE way a job reaches a runner process."""
    import subprocess
    proj = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jobs", "exec", "--job-id", str(job_id)],
            cwd=proj, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_detached_flags(),  # no console window (Windows)
        )
        print(f"[JOBSPAWN] out-of-process: python -m jobs exec --job-id {job_id}")
    except Exception as e:
        print(f"[JOBSPAWN] failed to spawn runner for #{job_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to spawn runner: {e}") from e


@app.get("/jobs/{job_id}/stream")
async def job_stream_endpoint(job_id: int):
    """Server-Sent Events stream for a job. Emits:
      - event: status   → status changes (queued → running → success/error)
      - event: log      → new log lines appended to the job's log file
      - event: heartbeat → every ~25s so Cloudflare's idle-close timer
                           never fires (free plan ≈ 100s)
      - event: done     → final event when the job hits a terminal status;
                          the stream closes immediately after.

    Browser opens with `new EventSource('/jobs/<id>/stream')` and adds
    listeners for the four event types. The dishes form's Run button
    uses this for the live log tail."""
    async def event_gen():
        last_log_size = 0
        last_status = None
        last_heartbeat = time.time()
        consecutive_missing = 0
        while True:
            try:
                with _db() as conn:
                    job = jobs_lib.get_job(conn, job_id)
            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
                return

            if not job:
                # Tolerate a few misses immediately post-enqueue (DB
                # commit race). Give up after a handful of polls.
                consecutive_missing += 1
                if consecutive_missing > 5:
                    yield f"event: error\ndata: {json.dumps({'error': 'job not found'})}\n\n"
                    return
                await asyncio.sleep(0.5)
                continue
            consecutive_missing = 0

            # Status change
            if job["status"] != last_status:
                yield (
                    f"event: status\n"
                    f"data: {json.dumps({'status': job['status'], 'started_at': job['started_at'], 'finished_at': job['finished_at'], 'log_filename': job['log_filename'], 'result': job['result'], 'error_detail': job['error_detail']})}\n\n"
                )
                last_status = job["status"]

            # New log content (only if log_filename has been stamped)
            if job.get("log_filename"):
                log_path = LOGS_DIR / job["log_filename"]
                if log_path.exists():
                    size = log_path.stat().st_size
                    if size > last_log_size:
                        try:
                            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                                f.seek(last_log_size)
                                new_text = f.read()
                            last_log_size = size
                            # Send line by line so the client can append cleanly.
                            for line in new_text.splitlines():
                                if not line:
                                    continue
                                yield f"event: log\ndata: {json.dumps({'line': line})}\n\n"
                        except Exception as e:
                            print(f"[SSE] log read failed: {e}")

            # Terminal status → emit `done` and close stream
            if job["status"] in ("success", "error", "cancelled"):
                yield f"event: done\ndata: {json.dumps({'status': job['status'], 'result': job['result'], 'error_detail': job['error_detail']})}\n\n"
                return

            # Heartbeat to keep the connection alive past Cloudflare's
            # idle-close (~100s on free plan).
            now = time.time()
            if now - last_heartbeat > 25:
                yield f"event: heartbeat\ndata: {json.dumps({'t': now})}\n\n"
                last_heartbeat = now

            await asyncio.sleep(1.0)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # tells any nginx/proxy not to buffer
            "Connection": "keep-alive",
        },
    )


# =========================================================================
# On-demand queue drain
# =========================================================================
#
# The background *poll* runner below is disabled on purpose (its 2s
# sqlite poll stalled the event loop — see start_job_runner + memory
# project_job_runner_disabled). The consequence: an enqueued job (a dish
# refresh from the dishes form, etc.) sits in status='queued' forever
# because nothing dispatches it. This endpoint is the event-driven
# alternative — it runs ONLY when a human clicks "Run queued jobs" in
# the nav menu, so it never sits polling. It drains serially because the
# per-job stdout tee in jobs_lib is process-global; one job at a time,
# exactly as runner_loop would have.

_drain_task: Optional["asyncio.Task"] = None


async def _drain_queued_jobs(job_ids: list) -> None:
    """Run the given jobs serially via the same `_run_one_job` path the
    (disabled) runner uses. Re-fetches each job fresh and skips any that
    are no longer queued (cancelled, or a racing drain already took it)."""
    global _drain_task
    try:
        for jid in job_ids:
            with _db() as conn:
                job = jobs_lib.get_job(conn, jid)
            if job is None or job["status"] != "queued":
                continue
            await jobs_lib._run_one_job(job, DB_PATH, LOGS_DIR)
    finally:
        _drain_task = None


@app.post("/jobs/run-queued")
async def run_queued_jobs_endpoint(request: Request):
    """Drain the queued-jobs backlog on demand. Kicks off a single
    background task that runs every currently-queued job serially and
    returns immediately (202) with the ordered id list so the browser
    can watch each one's /jobs/<id>/stream. Returns 200 with count=0 when
    the queue is empty, or 409 if a drain is already in flight."""
    _require_perm(request, "refresh_dishes")
    global _drain_task
    if _drain_task is not None and not _drain_task.done():
        with _db() as conn:
            running = jobs_lib.list_jobs(conn, status="running", limit=10)
        return JSONResponse(
            status_code=409,
            content={"error": "drain already running",
                     "running": [j["id"] for j in running]},
        )
    with _db() as conn:
        queued = jobs_lib.list_jobs(conn, status="queued", limit=100)
    queued.sort(key=lambda j: j["created_at"])  # oldest first
    job_ids = [j["id"] for j in queued]
    if not job_ids:
        return JSONResponse(
            status_code=200,
            content={"count": 0, "job_ids": [], "message": "No queued jobs"},
        )
    _drain_task = asyncio.create_task(_drain_queued_jobs(job_ids))
    return JSONResponse(
        status_code=202,
        content={"count": len(job_ids), "job_ids": job_ids},
    )


# =========================================================================
# Job runner — background asyncio task
# =========================================================================

@app.on_event("startup")
async def start_job_runner():
    """Spawn the jobs runner as a background asyncio task. Runs for the
    life of the uvicorn worker, polling the jobs table every ~2s for
    the next ready job and dispatching to the registered handler.

    DISABLED during development: the 2s poll did a blocking sqlite3.connect
    on the asyncio event loop every tick, stalling all request handling.
    No background timer for now — invoke jobs manually when needed. To
    re-enable, uncomment the create_task below (and consider moving the
    DB calls off the loop via asyncio.to_thread first)."""
    # asyncio.create_task(
    #     jobs_lib.runner_loop(DB_PATH, LOGS_DIR, poll_interval=2.0)
    # )
    # print("[STARTUP] job runner spawned")
    print("[STARTUP] job runner DISABLED (no background poll; invoke jobs manually)")


# Slim projection for the list/sidebar. The sidebar (recipe_form_styled.html
# loadRecipes/renderRecipes) only renders these fields — keep ONLY them so the
# list payload drops the heavy body (ingredients/instructions/_cook/_measurements/
# nutrition/_identity/embedding-adjacent prose…). Nested shape preserved so the
# sidebar's r.data.* pickers work unchanged. If the sidebar ever renders a new
# field, ADD it here (else the card silently loses it) — see the getter list in
# recipe_form_styled.html renderRecipes().
#
# SIZE, and why this keeps needing attention: the original audit (2026-06-15)
# measured 702 master rows at ~13MB full / ~700KB slim. The corpus is now 5,435
# rows and the same projection weighed 6.89MB — the projection did not regress,
# the corpus grew 7.7x underneath it. Anything kept here is multiplied by the
# whole table, so "harmless little field" is not a category that exists.
#
# What is deliberately NOT here (measured 2026-08-20, master, 5,435 rows):
#   classification.story  0.37MB  } read only by renderRecipes' isEnriched(),
#   editorial.opinion     0.22MB  } which was defined and never called. Dead.
#   image (when a preview 1.64MB   getImage() prefers _source.previewImage and
#     image exists)               falls back to image[]; 5,295 of 5,435 rows
#                                 carried both, so the fallback was dead weight
#                                 on 97% of rows. Still emitted for the 140
#                                 pre-coopt rows that have no previewImage.
#   exceptionalism.basis' 0.45MB  renderExcBadge (library-shell.js) builds its
#     match_* / sigma_observed    tooltip from grade/score and basis.{model, n,
#                                 sigma_effective} ONLY. The match provenance is
#                                 still in the full record the card fetches on
#                                 click; it was never on screen from the list.
# Those are per-field sizes and do not sum exactly to the total, which also
# loses the JSON key overhead of the containers they emptied. End to end, over
# all 5,435 master rows and with byte-for-byte parity checked on every field
# the cards read: 6.89MB -> 4.40MB raw, 1.18MB -> 0.66MB gzipped.
_EXC_BASIS_KEYS = ("model", "n", "sigma_effective")


def _slim_exceptionalism(exc):
    """Grade + score + the three basis fields the badge tooltip prints."""
    if not isinstance(exc, dict):
        return exc
    out = {"score": exc.get("score"), "grade": exc.get("grade")}
    basis = exc.get("basis")
    if isinstance(basis, dict):
        out["basis"] = {k: basis[k] for k in _EXC_BASIS_KEYS if k in basis}
    return out


def _recipe_list_data(d: dict) -> dict:
    out: dict = {"name": d.get("name")}
    cls = d.get("classification") or {}
    if cls:
        out["classification"] = {"chapter": cls.get("chapter")}
    sc = d.get("_scoring") or {}
    if sc:
        out["_scoring"] = {k: sc.get(k) for k in
                           ("ouScore", "pageAuthority", "domainAuthority", "rootDomain")}
    src = d.get("_source") or {}
    if src:
        out["_source"] = {k: src.get(k) for k in ("originalUrl", "previewImage", "siteName")}
    b = d.get("_batch") or {}
    if b:
        out["_batch"] = {"rank": b.get("rank"), "name": b.get("name")}
    m = d.get("_master")
    if isinstance(m, dict):
        out["_master"] = {"exceptionalism": _slim_exceptionalism(m.get("exceptionalism"))}
    if d.get("_grade") is not None:
        out["_grade"] = _slim_exceptionalism(d.get("_grade"))
    # Only the image the card will actually use. previewImage is the cooped
    # local copy and getImage() prefers it; shipping the original image[] too
    # just to have it lose a comparison is the single fattest thing this
    # projection used to do.
    if not (src.get("previewImage") or "").strip() and d.get("image") is not None:
        out["image"] = d.get("image")
    pv = d.get("provenance") or {}
    if pv.get("author"):
        out["provenance"] = {"author": pv.get("author")}
    return out


# ORDER BY for every entry in the sidebar's SORT_ORDERS table
# (recipe_form_styled.html). The two must stay in step: a key the client offers
# and the server does not know falls back to updated_desc, which would silently
# ignore the user's choice, so ADD BOTH SIDES when adding a sort.
#
# Null handling is not incidental — it is the contract the client comparator
# already established and this has to reproduce:
#   numbers  missing sorts LAST in BOTH directions, because null means "not
#            measured" and must never outrank a real value ([[absent is not
#            zero]]). SQLite puts NULLs first in ASC and last in DESC, so ASC
#            needs an explicit NULLS LAST and DESC gets one anyway for clarity.
#   text     the client mapped '' to U+FFFF so blanks sort last; the CASE does
#            the same for NULL and ''.
#   power    DERIVED as DA+PA, never read from the stored `_scoring.power`
#            (which is 0 on ~51% of master rows). The generated column is
#            already DA+PA, and SQLite's NULL arithmetic makes it NULL unless
#            BOTH operands exist — exactly the client's "one measured signal is
#            UNMEASURED, not half-powerful".
# Read the FACET COLUMNS, not json_extract. They already carry the
# TRIM/NULLIF normalisation, so "absent" and "blank" are the same NULL here and
# a plain NULLS LAST reproduces the client's '' -> U+FFFF blank-sinking without
# a CASE. `rank` has no column: it lives on 25 rows of 5,435, so it is not worth
# one until something other than this sort reads it.
_NAME_SQL = "bcc_sortkey(recipe_name)"
_CHAPTER_SQL = "bcc_sortkey(chapter)"
_RANK_SQL = "json_extract(data,'$._batch.rank')"


# EVERY sort ends in a total order. Measured over the 5,435 master rows, the
# share of rows sitting in a tie group is: page_authority 99.9%, power 99.9%,
# recipe_score 99.9%, batch rank 99.8%, chapter 100%, ou_score 92.9%, name 15.8%
# — and created_at 0%. Ties are the normal case here, not the edge, because PA
# saturates (0e2be0a measured the same thing from the other end).
#
# Without a total order the list is genuinely non-deterministic: SQLite's sorter
# is not stable, so two identical requests can return tied rows in different
# order, and any LIMIT/OFFSET paging on top of that can show a row twice or skip
# it entirely. The browser was no better — its stable sort merely FROZE whatever
# arbitrary order the fetch arrived in, which looked deterministic and wasn't.
#
# The tail follows the ruling already made in 0e2be0a for authority ranking:
# traffic DESC where we have it, insertion order otherwise. `id` last makes the
# order total, since it is unique and never null.
_TOTAL = "id ASC"
_AUTHORITY_TAIL = f"traffic DESC NULLS LAST, {_TOTAL}"

SORT_SQL = {
    # Only meaningful alongside q= — with no text match temp.q_match does not
    # exist, so _recipes_search_impl falls back to the default rather than
    # letting a relevance sort reference a table that was never built.
    "relevance":    f"(SELECT rel FROM temp.q_match WHERE id = {{T}}.id) DESC, "
                    f"updated_at DESC, {_TOTAL}",
    "updated_desc": f"updated_at DESC, {_TOTAL}",
    "created_desc": f"created_at DESC, {_TOTAL}",
    "name_asc":     f"{_NAME_SQL} ASC NULLS LAST, {_TOTAL}",
    "ou_desc":      f"ou_score DESC NULLS LAST, {_AUTHORITY_TAIL}",
    "pa_desc":      f"page_authority DESC NULLS LAST, {_AUTHORITY_TAIL}",
    "power_desc":   f"power DESC NULLS LAST, page_authority DESC NULLS LAST, {_AUTHORITY_TAIL}",
    "chapter_asc":  f"{_CHAPTER_SQL} ASC NULLS LAST, {_NAME_SQL} ASC NULLS LAST, {_TOTAL}",
    "quality":      f"ou_score DESC NULLS LAST, recipe_score DESC NULLS LAST, "
                    f"updated_at DESC, {_AUTHORITY_TAIL}",
    "batch_rank":   f"{_RANK_SQL} ASC NULLS LAST, {_NAME_SQL} ASC NULLS LAST, {_TOTAL}",
}
DEFAULT_SORT = "updated_desc"


# List recipes for the given owner. user_id=0 returns the master collection
# (master_recipes table); any other value returns that owner's personal
# recipes. `summary=1` returns the slim list projection (the sidebar uses it —
# big payload win); `limit`/`offset` paginate (0 = all, the default). Default
# (no params) preserves the prior full-data behavior for any other consumer.
# ---------------------------------------------------------------------------
# Faceted search
# ---------------------------------------------------------------------------
# The filterable facets. Each maps a query parameter to the indexed generated
# column that answers it — never a json_extract, which is the whole point of
# those columns (SELECT DISTINCT cuisine: 471ms through JSON, 0.4ms through the
# indexed column). Adding a facet is one entry here: the WHERE, the dropdown,
# and the cascade all read this table, so they cannot drift apart.
FACET_COLUMNS = {
    "cuisine": "cuisine",
    "ethnicity": "ethnicity",
    "chapter": "chapter",
}


def _search_where(user_id: int, filters: dict, q: str, *, skip: str = ""):
    """Build the WHERE for a search, optionally omitting ONE facet.

    `skip` is what makes the dropdowns cascade without dead-ending. A facet's
    own list is counted with every OTHER filter applied but not its own: pick
    Greek and the chapter list narrows to the 25 chapters that actually have
    Greek recipes, while the cuisine list still shows every cuisine, so you can
    switch away from Greek instead of having to clear it first. Counting a facet
    against its own filter would collapse it to the single value you chose.
    """
    clauses = ["user_id = ?"]
    params: list = [user_id]
    for key, col in FACET_COLUMNS.items():
        if key == skip:
            continue
        val = (filters.get(key) or "").strip()
        if val:
            clauses.append(f"{col} = ?")
            params.append(val)
    if q:
        # The text match is NOT inlined here — it is pre-resolved into a temp
        # table by _materialise_text_match and joined in, because this predicate
        # is the expensive one and every caller would otherwise re-run it. See
        # that function for the numbers.
        clauses.append("id IN (SELECT id FROM temp.q_match)")
    return " AND ".join(clauses), params


def _fts_query(raw: str) -> str:
    """Turn what a person typed into an FTS5 MATCH expression.

    RAW INPUT CAN NEVER REACH MATCH. FTS5 has its own grammar, so an unbalanced
    quote or a bare `-` is a syntax error, which would surface as a 500 on a
    search box — the one place users type whatever they like. Everything is
    quoted as a literal token and the operators are ones this function emits.

    Supported, in the shape people already expect from a search box:
        two words        -> both must appear (AND, not the OR FTS5 defaults to)
        "exact phrase"   -> kept together
        -word            -> excluded
        word OR word     -> either

    Bare-words-mean-AND is the important one. FTS5's implicit operator is OR, so
    passing "shrimp corn chowder" through untouched returns everything matching
    ANY of the three — thousands of rows, ranked plausibly enough that it looks
    like it worked.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    tokens: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch.isspace():
            i += 1
            continue
        neg = False
        if ch == "-" and i + 1 < n and not raw[i + 1].isspace():
            neg, i = True, i + 1
            ch = raw[i]
        if ch == '"':                      # quoted phrase, to the next quote or end
            j = raw.find('"', i + 1)
            body = raw[i + 1:] if j == -1 else raw[i + 1:j]
            i = n if j == -1 else j + 1
        else:
            j = i
            while j < n and not raw[j].isspace():
                j += 1
            body = raw[i:j]
            i = j
        # Keep only what FTS5 tokenizes anyway; this is what makes a stray
        # apostrophe or bracket harmless rather than fatal.
        body = "".join(c if (c.isalnum() or c.isspace()) else " " for c in body).strip()
        if not body:
            continue
        if body.upper() in ("AND", "OR", "NOT") and not neg:
            tokens.append(body.upper())
            continue
        tokens.append(("NOT " if neg else "") + '"' + body + '"')
    # Join with AND, but never around an operator — either one the user supplied
    # or the NOT this function generates for a -exclusion. FTS5's NOT is BINARY
    # ("a NOT b" = a and not b), so "AND NOT" is a syntax error rather than the
    # emphasis it looks like.
    out: list[str] = []
    for t in tokens:
        starts_op = t.startswith("NOT ")
        if (out and not starts_op and t not in ("AND", "OR", "NOT")
                and out[-1] not in ("AND", "OR", "NOT")):
            out.append("AND")
        out.append(t)
    while out and out[-1] in ("AND", "OR", "NOT"):
        out.pop()
    # A leading NOT has no left operand and is a syntax error. It comes from a
    # query that is nothing but exclusions ("-clam"), which FTS5 cannot express
    # — "everything except clam" needs something to subtract from. Treated as
    # unusable rather than guessed at.
    if out and (out[0] == "NOT" or out[0].startswith("NOT ")):
        return ""
    return " ".join(out)


def _materialise_text_match(conn, user_id: int, q: str) -> None:
    """Resolve the free-text match ONCE into temp.q_match, via FTS5.

    A search issues five queries — the match count, the page, and one count per
    facet — so the text predicate is resolved once and joined rather than
    re-evaluated by each. That mattered enormously when this was a LIKE driven
    by a Python callback (~43,000 callbacks, 964ms); it still matters, and the
    temp table also carries FTS5's bm25 rank so relevance ordering is available
    without running the match again.

    Rank is stored NEGATED. bm25() returns a more-negative number for a better
    match, which sorts correctly ascending but reads backwards everywhere else;
    flipping it here means `ORDER BY rel DESC` means what it says at every
    call site.

    Temp tables are per-connection and _db() hands out a fresh connection per
    request, so this cannot leak between callers.
    """
    expr = _fts_query(q)
    table = _recipes_table_for(user_id)
    conn.execute("DROP TABLE IF EXISTS temp.q_match")
    if not expr:
        # The query was all punctuation. An empty FTS5 expression is a syntax
        # error, and matching everything would silently ignore what was typed,
        # so match nothing and let the caller report no results.
        conn.execute("CREATE TEMP TABLE q_match (id INTEGER PRIMARY KEY, rel REAL)")
        return
    fts = f"{table}_fts"
    conn.execute(
        f"CREATE TEMP TABLE q_match AS "
        f"SELECT rowid AS id, -bm25({fts}) AS rel FROM {fts} WHERE {fts} MATCH ?",
        [expr],
    )


def _recipes_search_impl(*, user_id: int, q: str, cuisine: str, ethnicity: str,
                         chapter: str, sort: str, limit: int, offset: int,
                         facets: int) -> dict:
    """One page of matching rows plus every dropdown's options, in one request.

    Returns an ENVELOPE, unlike GET /recipes which returns a bare array. The
    counts are the reason: a list UI has to say "129 of 5,435" and populate its
    filters, and issuing four more requests to learn that would undo the saving.
    GET /recipes keeps its array shape for the full-record consumers.
    """
    table = _recipes_table_for(user_id)
    filters = {"cuisine": cuisine, "ethnicity": ethnicity, "chapter": chapter}
    q = (q or "").strip()

    order_by = SORT_SQL.get(sort) or SORT_SQL[DEFAULT_SORT]
    if sort == "relevance" and not q:
        order_by = SORT_SQL[DEFAULT_SORT]
    # Relevance reads temp.q_match, which is named per request; bind the table.
    order_by = order_by.replace("{T}", table)
    limit = max(1, min(int(limit or 200), 1000))
    offset = max(0, int(offset or 0))

    try:
        with _db() as conn:
            if q:
                _materialise_text_match(conn, user_id, q)
            where, params = _search_where(user_id, filters, q)
            matched = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", params
            ).fetchone()[0]
            total = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", [user_id]
            ).fetchone()[0]

            rows = conn.execute(
                f"SELECT id, recipe_id, user_id, data, source_changed_at, "
                f"created_at, updated_at FROM {table} WHERE {where} "
                f"ORDER BY {order_by} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()

            out = []
            for row in rows:
                try:
                    data = json.loads(row[3])
                except json.JSONDecodeError as e:
                    print(f"[SEARCH] skipping unparseable recipe {row[1]}: {e}")
                    continue
                out.append({
                    "id": row[0], "recipe_id": row[1], "user_id": row[2],
                    "data": _recipe_list_data(data),
                    "source_changed_at": row[4], "created_at": row[5],
                    "updated_at": row[6], "bccUrl": _bcc_permalink(row[1]),
                })

            facet_counts: dict = {}
            if facets:
                for key, col in FACET_COLUMNS.items():
                    fw, fp = _search_where(user_id, filters, q, skip=key)
                    facet_counts[key] = [
                        {"value": v, "count": n}
                        for v, n in conn.execute(
                            f"SELECT {col}, COUNT(*) FROM {table} WHERE {fw} "
                            f"AND {col} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 ASC",
                            fp,
                        ).fetchall()
                    ]
    except Exception as e:
        print(f"[ERROR] recipe search failed: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e

    # ---- SEMANTIC NEAR-MISSES ------------------------------------------
    # Lexical search is exact: when the words are not there it returns nothing,
    # which is correct and unhelpful. The identity vectors answer the other
    # question — "what dish is this?" — so a query with no literal match can
    # still be answered with what the user probably meant. Measured: "shrimp and
    # corn chowder" has no lexical match anywhere in the corpus, and the vectors
    # return lobster-and-corn chowder, chicken corn chowder, creamy corn chowder.
    #
    # ONLY ON A MISS, deliberately. Embedding the query is a network round trip
    # (~200-900ms) against a search path that is otherwise ~1ms, and the facet
    # dialog re-queries on every dropdown change. Spending that on a search that
    # already succeeded would make the common case pay for the rare one.
    #
    # Returned SEPARATELY from rows, never merged into them: these did not match
    # what was asked for, and presenting them as though they did is how a search
    # box starts lying. The caller labels them.
    # Two gates, both learned from watching it misbehave.
    #
    # A query that produced no usable FTS expression ("((((") has no searchable
    # content, so there is nothing to be near. Embedding it spent 700ms — the
    # slowest path in the endpoint — to return "test" and Tonkotsu Ramen.
    #
    # And a floor, because a nearest neighbour always exists: cosine is a
    # ranking, not a verdict. Real hits sit far above it ("shrimp and corn
    # chowder" -> lobster-and-corn chowder at 0.69, "something brothy and
    # warming" -> hot pot broth at 0.49); the junk run topped out at 0.46 with
    # numbers trailing to 0.23. 0.45 separates those two populations on the
    # evidence available, and is a threshold to revisit with more, not a
    # constant anyone derived.
    SUGGESTION_FLOOR = 0.45
    suggestions: list = []
    if q and matched == 0 and offset == 0 and _fts_query(q):
        try:
            from input.pipeline.embeddings import embed_text
            from input.pipeline import vector_store
            with _db() as vconn:
                vector_store.enable_vec(vconn)
                qvec = embed_text(q)
                near = vector_store.find_similar_master_recipes(vconn, qvec, k=6)
                if near:
                    ids = [n["id"] for n in near]
                    marks = ",".join("?" * len(ids))
                    by_id = {
                        r[0]: r for r in vconn.execute(
                            f"SELECT id, recipe_id, recipe_name FROM master_recipes "
                            f"WHERE id IN ({marks})", ids)
                    }
                    for n in near:
                        row = by_id.get(n["id"])
                        if not row:
                            continue
                        sim = 1 - (n["distance"] ** 2) / 2   # cosine on unit vectors
                        if sim < SUGGESTION_FLOOR:
                            continue
                        suggestions.append({
                            "recipe_id": row[1],
                            "name": row[2],
                            "dish": n.get("dish"),
                            "similarity": round(sim, 3),
                            # WHICH COLLECTION THIS LIVES IN. Suggestions come
                            # from master_recipes ALWAYS — the vector index is
                            # the master one — regardless of the user_id being
                            # browsed. The client opens a row by
                            # /recipes/{id}?user_id=…, and sending the browsed
                            # user_id looked the master id up in the personal
                            # table: 404, and a click that did nothing. Rows
                            # carry their own user_id for exactly this reason;
                            # suggestions now do too.
                            "user_id": 0,
                        })
        except Exception as e:
            # A search that found nothing must not become a search that errored.
            print(f"[SEARCH] semantic suggestions unavailable: {type(e).__name__}: {e}")

    print(f"[SEARCH] user={user_id} q={q!r} {filters} sort={sort or DEFAULT_SORT} "
          f"-> {matched}/{total}, returning {len(out)} from offset {offset}"
          + (f", {len(suggestions)} suggestion(s)" if suggestions else ""))
    return {
        "suggestions": suggestions,
        "rows": out,
        "total": total,
        "matched": matched,
        "limit": limit,
        "offset": offset,
        "hasMore": offset + len(out) < matched,
        "sort": sort or DEFAULT_SORT,
        "filters": {k: v for k, v in filters.items() if v},
        "q": q,
        "facets": facet_counts,
    }


@app.get("/recipes")
def list_recipes(user_id: int = PLACEHOLDER_USER_ID, summary: int = 0,
                 limit: int = 0, offset: int = 0, sort: str = DEFAULT_SORT):
    table = _recipes_table_for(user_id)
    # An unknown key means the client offers a sort this server has never heard
    # of — almost always a deploy skew. Fall back to the default rather than
    # 400, but SAY SO: silently ignoring the user's chosen order is the failure
    # mode where the list looks fine and is simply wrong.
    order_by = SORT_SQL.get(sort)
    if order_by is None:
        if sort != DEFAULT_SORT:
            print(f"[LIST] unknown sort {sort!r} — falling back to {DEFAULT_SORT}")
        order_by = SORT_SQL[DEFAULT_SORT]
    print(f"[LIST] List recipes user_id={user_id} table={table} summary={summary} "
          f"limit={limit} sort={sort}")
    try:
        with _db() as conn:
            sql = (f"SELECT id, recipe_id, user_id, data, source_changed_at, created_at, updated_at "
                   f"FROM {table} WHERE user_id = ? ORDER BY {order_by}")
            params: list = [user_id]
            if limit and limit > 0:
                sql += " LIMIT ? OFFSET ?"
                params += [int(limit), max(0, int(offset))]
            rows = conn.execute(sql, params).fetchall()
            result = []
            for row in rows:
                try:
                    data = json.loads(row[3])
                except json.JSONDecodeError as e:
                    print(f"[WARN] Failed to parse recipe {row[1]}: {e}")
                    continue
                result.append({
                    "id": row[0],
                    "recipe_id": row[1],
                    "user_id": row[2],
                    "data": _recipe_list_data(data) if summary else data,
                    "source_changed_at": row[4],
                    "created_at": row[5],
                    "updated_at": row[6],
                    "bccUrl": _bcc_permalink(row[1]),
                })
            print(f"[OK] Returning {len(result)} recipes")
            return result
    except Exception as e:
        print(f"[ERROR] Error in list_recipes: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


def _grade_recipe_on_save(recipe_dict: dict, *, user_id: int) -> None:
    """Stamp Exceptionalism grade on the recipe dict in place.

    Master rows: result lands at `_master.exceptionalism` (existing
    shape — batch path already populates it this way; we fill in for
    rows that came through other paths).
    Personal rows: result lands at top-level `_grade` so the form's
    badge component reads one location regardless of table.

    Cohort selection:
      - Master rows with `_master.dish`: grade against that dish's
        stored last_ou_fit (cohort known).
      - Master rows without `_master.dish` OR personal rows: embedding-
        match to a dish; below threshold → no grade.

    DA/PA come from `_scoring.{pageAuthority, domainAuthority}` which
    the extract step already populated via Moz. No Moz call here —
    we trust the freshness of what just landed (TTL refresh handled
    elsewhere by url_scoring's get_or_create_url_metadata).

    Best-effort: any failure leaves the recipe ungraded (em-dash in
    UI) rather than blocking the save.
    """
    scoring = recipe_dict.get("_scoring") or {}
    da = scoring.get("domainAuthority")
    pa = scoring.get("pageAuthority")
    if da is None or pa is None:
        return  # no Moz scores → can't grade

    is_master = (user_id == 0)
    master_block = recipe_dict.get("_master") or {}

    # Path 1 — already stamped (batch path). Don't overwrite.
    if is_master and master_block.get("exceptionalism"):
        return

    # Path 2 — explicit cohort via _master.dish
    explicit_dish = (master_block.get("dish") or "").strip() if is_master else ""
    grade: Optional[dict] = None
    if explicit_dish:
        try:
            with _db() as conn:
                dish_row = dishes_lib.get_dish(conn, explicit_dish)
            if dish_row and dish_row.get("last_ou_fit"):
                grade = compute_exceptionalism(
                    da, pa, dish_row["last_ou_fit"],
                    matched_dish=explicit_dish,
                    match_method="explicit",
                )
        except Exception as e:
            print(f"[GRADE] explicit-cohort lookup failed for {explicit_dish!r}: {e}")

    # Path 3 — embedding match (any row without an explicit dish, or
    # explicit-path failed)
    if grade is None:
        try:
            with _db() as conn:
                match = find_best_dish_match(conn, recipe_dict)
            if match and match.get("ou_fit"):
                grade = compute_exceptionalism(
                    da, pa, match["ou_fit"],
                    matched_dish=match["dish_name"],
                    match_confidence=match["confidence"],
                    match_method=("embedding-match-narrow"
                                  if match.get("chapter_filtered")
                                  else "embedding-match-wide"),
                )
        except Exception as e:
            print(f"[GRADE] embedding match failed: {e}")

    # Path 4 — chapter-level fallback. Triggered when neither the
    # explicit dish fit nor the embedding-matched dish fit produced
    # a usable grade (typical cause: dish cohort below_min_n=25).
    # The chapter cohort is broader and noisier but covers the niche-
    # dish gap so recipes still show grades rather than em-dashes.
    if grade is None:
        chapter = ((recipe_dict.get("classification") or {}).get("chapter") or "").strip()
        if chapter:
            try:
                from input.pipeline.chapters import get_chapter_fit
                with _db() as conn:
                    ch_fit = get_chapter_fit(conn, chapter)
                if ch_fit and ch_fit.get("used"):
                    grade = compute_exceptionalism(
                        da, pa, ch_fit,
                        matched_dish=chapter,
                        match_method="chapter-fallback",
                    )
            except Exception as e:
                print(f"[GRADE] chapter-fallback failed for {chapter!r}: {e}")

    if grade is None:
        print(f"[GRADE] no cohort match → ungraded")
        return

    if is_master:
        master_block["exceptionalism"] = grade
        recipe_dict["_master"] = master_block
    else:
        recipe_dict["_grade"] = grade

    basis = grade.get("basis") or {}
    print(f"[GRADE] {grade.get('grade')} (score={grade.get('score')}) "
          f"via {basis.get('match_method') or 'explicit'} "
          f"dish={basis.get('matched_dish') or explicit_dish!r}")


# Save (insert or update) a recipe
# Moz-derived `_scoring` keys. Every one is a MEASUREMENT: absent means we could
# not measure it, and 0 is not a legal measured value for any of them — Moz PA/DA
# are >= 1 for any page it has crawled, and a cohort percentile of exactly 0.0
# alongside fieldN 0 means there was no cohort, not that the recipe came last.
# The authority half comes from MOZ_SCORING_FIELDS — the same tuple
# apply_moz_scores writes and clear_moz_scores removes — minus mozHttpCode
# (see below). The cohort half is batch-derived and belongs to this module.
# One list of "what the Moz writer touches", so a field added there cannot be
# forgotten by the sanitizer that has to recognise its unmeasured zero.
_MOZ_SCORING_KEYS = tuple(
    k for k in MOZ_SCORING_FIELDS if k != "mozHttpCode"
) + ("ouPercentile", "powerPercentile", "fieldAvgPower",
     "fieldMaxPower", "fieldMinPower", "fieldN",
     "dishCompetitivenessPct")
# Of those, the COHORT-derived ones — computed only when a batch ranked this row
# against its peers. For these, 0.0 is a LEGAL measurement: PERCENT_RANK gives
# exactly 0.0 to the bottom-ranked member of every cohort, and fieldMinPower is
# the cohort's floor. `fieldN` is NOT here — fieldN 0 really does mean no cohort.
_COHORT_SCORING_KEYS = ("ouPercentile", "powerPercentile", "fieldMinPower")
# DELIBERATELY NOT IN THAT LIST: `mozHttpCode`. It is the one _scoring field
# where 0 is a REAL measurement — "Moz answered and has no data for this URL",
# i.e. any PA on the row is a placeholder. Stripping it would delete exactly the
# finding it exists to record. See input/pipeline/url_scoring.moz_http_status.


def _render_retry_would_help(url: str) -> tuple[bool, str]:
    """Is a forced-render retry of `url` capable of returning anything new?

    The retry exists for a JS publisher whose recipe card loads late: the first
    STATIC fetch gets a shell, a rendered fetch gets the card. That is a real
    case and it stays.

    It is useless when the domain is already `render_required`: the first fetch
    ALREADY rendered, so `fetch_render=True` re-issues a byte-identical request.
    That is the condition checked here, and it is knowable from one DB read
    before paying anything.

    Measured on job 822 (177milkstreet.com, render_required): five winners each
    paid TWO full unblocker fetches and TWO LLM extracts to reach the same
    "fewer than 2 ingredients (0)" verdict. The second of each pair could not
    have gone differently.

    A second condition is real but NOT implemented here: a page declaring
    itself paywalled (`isAccessibleForFree: false`) cannot be rendered into
    content the server never sent. Wiring it needs the fetched HTML, which this
    function does not have — and for the case that prompted R6 it is redundant,
    since that publisher is render_required anyway.

    Returns (worth_trying, why_not).
    """
    try:
        from urllib.parse import urlparse as _urlparse
        from input.pipeline import domains_lib
        from input.pipeline.url_utils import root_domain as _rootd
        host = (_urlparse(url).hostname or "").lower()
        with _db() as conn:
            row = domains_lib.get_domain(conn, host) or domains_lib.get_domain(conn, _rootd(url) or "")
        if row and row.get("render_required"):
            return False, ("domain is render_required, so the first fetch already "
                           "rendered — the retry is the same request")
    except Exception as e:
        # Unknown policy: keep the retry. Never let a lookup failure remove a
        # recovery path — the whole point of the retry is the uncertain case.
        print(f"[WARN] render-retry policy lookup failed (retrying anyway): {e}")
    return True, ""


def _sanitize_scoring(recipe: dict, url_normalized: str = "") -> None:
    """Strip UNMEASURED zeros out of `_scoring`, and say why they are missing.

    The recipe form serialises its whole scoring section on every save, so an
    input it never received a value for is submitted as 0.0. Saved verbatim, a
    DA-88 publisher is recorded as domainAuthority 0 — indistinguishable from a
    real measurement, and it sits at the floor of every ranking dimension.

    Observed 2026-08-03 on a sun-sentinel.com bookmarklet grab: the article was
    three days old, Moz had not crawled it yet (all four URL variants http_code
    0), score_url_via_moz correctly returned None — and the save wrote a complete
    block of zeros anyway. Same failure as the _scoring.power bug fixed on
    2026-07-30 across 1,881 rows, arriving by a different route: there the writer
    invented the zero, here the form did and the writer accepted it.

    The rule, third time of asking: a value we could not compute must be ABSENT.
    Absent is honest, re-scoreable, and reads correctly everywhere — the metabase
    cache already stores NULL for exactly this state.

    `scoringNote` records WHY, because "no score" with no explanation is the thing
    that sends someone digging through logs. A page Moz has not crawled YET (fresh
    article on a known publisher) is a different situation from one it will never
    crawl, and the note distinguishes them.
    """
    scoring = recipe.get("_scoring")
    if not isinstance(scoring, dict):
        return
    # A cohort was computed iff fieldN says so. When it was, the cohort-derived
    # zeros are REAL (the bottom row of any ranking is the 0th percentile) and
    # must survive — same exemption logic as mozHttpCode above, established by
    # measurement: 26 master rows had already lost a true percentile this way,
    # each of them the lowest-power row of its own dish batch, and each stamped
    # with a scoringNote blaming "saved outside a dish batch" while sitting IN one.
    try:
        _field_n = float(scoring.get("fieldN") or 0)
    except (TypeError, ValueError):
        _field_n = 0.0
    _strip_keys = (_MOZ_SCORING_KEYS if _field_n <= 0 else
                   tuple(k for k in _MOZ_SCORING_KEYS if k not in _COHORT_SCORING_KEYS))
    dropped = [k for k in _strip_keys
               if k in scoring and isinstance(scoring[k], (int, float))
               and not isinstance(scoring[k], bool) and float(scoring[k]) == 0.0]
    # An UNSCORED row needs the explanation just as much as a zero-stripped one —
    # arguably more, since there is nothing on screen to interpret. Observed
    # 2026-08-13 on the sun-sentinel swordfish bookmarklet grab: the form now
    # submits nulls rather than 0.0 (the absent-not-zero fix working as intended),
    # so `dropped` was empty, this function returned here, and the note that
    # exists precisely to stop someone digging through logs was never written.
    # The trigger is the STATE (no page authority), not the repair.
    _unscored = scoring.get("pageAuthority") is None
    if not dropped and not _unscored:
        return
    for k in dropped:
        scoring.pop(k, None)
    if not (scoring.get("fieldScope") or "").strip():
        scoring.pop("fieldScope", None)

    # Why — and be accurate about WHICH thing was missing. If pageAuthority
    # survived, Moz answered fine and only the dish-cohort signals were absent
    # (a recipe saved outside a batch has no cohort to be a percentile of). Saying
    # "no Moz data" there would send the reader to the wrong place.
    if _field_n > 0 and dropped:
        # It WAS in a batch — fieldN proves it. Do not repeat the no-cohort story.
        reason = (f"ranked in a cohort of {int(_field_n)}; the dropped field(s) "
                  f"{dropped} arrived as an unmeasured 0 from the caller")
    elif scoring.get("pageAuthority") is not None:
        reason = ("saved outside a dish batch, so there was no cohort to rank "
                  "against — Moz page/domain authority above is measured")
    else:
        reason = "no Moz data at save time"
        try:
            with _db() as conn:
                ensure_metabase_url_table(conn)
                row = get_metabase_url(conn, url_normalized) if url_normalized else None
            if not row:
                reason = "URL was never submitted to Moz"
            else:
                # Read the PROVENANCE, not the timestamp. moz_http_code 0 is Moz
                # saying "I answered, I have nothing" — a measured fact, now
                # persisted. The old test here was `not moz_last_scored`, which
                # meant the same thing only for as long as the uncrawled case
                # left that column NULL; it no longer does, precisely so the
                # 4-row probe stops repeating. See _record_moz_uncrawled.
                code = row.get("moz_http_code")
                if code is not None and int(code) == 0:
                    reason = ("Moz has not crawled this URL yet — common for a recently "
                              "published article; re-score later rather than treating it "
                              f"as zero (retried every {MOZ_UNCRAWLED_RETRY_DAYS} days)")
                elif not row.get("moz_last_scored"):
                    reason = ("URL is in the Moz queue but has no answer yet — no score "
                              "was recorded, and none was invented")
        except Exception:
            pass
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scoring["scoringNote"] = f"{reason} (checked {stamp})"
    recipe["_scoring"] = scoring
    if dropped:
        print(f"[SCORING] dropped {len(dropped)} unmeasured zero(s) "
              f"{dropped} for {url_normalized or '(no url)'} — {reason}")
    else:
        print(f"[SCORING] unscored: {url_normalized or '(no url)'} — {reason}")


def _stamp_dish_match(conn, recipe_dict: dict, rec_vec, *, label: str) -> bool:
    """Infer which canonical dish a recipe is and persist it on `_match`.

    Gated on NOT ALREADY KNOWING THE DISH — not on which table the row is in.
    A master row from a dish refresh carries `_master.dish` as ground truth; an
    interactive capture carries nothing, and used to keep nothing.

    The matching itself lives in input.pipeline.dish_match, shared with the
    backfill script and the nightly dish_rematch job. It was duplicated across
    those three and they had already drifted on whether to stamp the vec index.
    Reuses the vector the caller computed — no second embed, no API call.
    """
    from input.pipeline import dish_match as _dm
    m = _dm.build_match(conn, rec_vec, max_dist=_dm.max_distance())
    if not m:
        return False
    recipe_dict["_match"] = m
    print(f"[MATCH] {label} -> {m['candidates'][0]['dish']!r} "
          f"d={m['distance']:.3f} confident={m['confident']}  candidates="
          + ", ".join(f"{x['dish']}({x['distance']:.2f})" for x in m["candidates"]))
    return True


def _save_recipe_core(payload: dict) -> dict:
    """Synchronous core of POST /recipes. Same behavior as the endpoint —
    same return shape, same HTTPException raises — but callable
    in-process from other endpoints (e.g. /dishes/<name>/refresh)
    without going through self-HTTP. Sanitize + validate + save-gate +
    dedup + auto-enrich + insert/update + journal.

    All async behavior (request.json(), to_thread wrapping) lives in the
    thin endpoint wrapper below; this function is pure synchronous Python.
    """
    # Manual-from-reject rescue: the bookmarklet harvested #_bcc_dish/
    # #_bcc_run from the dish-form reject link, threaded them through
    # staging, and the form replayed them in this payload. user_id was
    # already forced to 0 at the auth gate; here we stamp the _master
    # block so the row attributes back to its originating batch.
    # kind="harvest" — distinct from "top" (algorithmic batch winners)
    # and "editors_choice" (curator's deliberate elevation).
    hints = payload.pop("bcc_hints", None)
    if isinstance(hints, dict) and (hints.get("dish") or "").strip():
        existing_master = payload.get("_master") or {}
        payload["_master"] = {
            **existing_master,
            "kind": "harvest",
            "dish": hints["dish"].strip(),
            "refreshed_at": (hints.get("run") or "").strip() or datetime.now(timezone.utc).isoformat(),
            "batch_source": "manual-from-reject",
        }
        print(f"[HARVEST] manual-from-reject save: dish={hints['dish']!r} "
              f"run={hints.get('run')!r}")
    print("[SAVE] Save recipe endpoint called")
    try:
        # One-line summary instead of dumping the whole recipe JSON — the full
        # payload flooded the run log and buried the save-loop drop reasons.
        print(f"[DATA] payload: {(payload.get('name') or '?')!r} "
              f"({len(payload.get('recipeIngredient') or [])} ings, "
              f"{len(payload.get('recipeInstructions') or [])} steps) "
              f"user={payload.get('user_id')}")
        cleaned = sanitize_recipe_data(payload)
        recipe = RecipeModel(**cleaned)
        print("[OK] Recipe model validation passed")
    except ValidationError as e:
        print(f"[ERROR] Validation error: {e}")
        raise HTTPException(status_code=422, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error processing request: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=400, detail=f"Bad input: {e}") from e

    # Save-quality gate. Refuse rows below the minimum-ingredients /
    # minimum-instructions floor so the recipes/master_recipes tables
    # stay statistically clean. The form catches the structured 422 and
    # offers a "Save anyway" dialog that retries with force_save=true.
    # The curator claim path bypasses this naturally because it re-saves
    # data that already passed the gate originally.
    force_save = bool(payload.get("force_save"))
    if not force_save:
        cleaned_for_check = recipe.model_dump(by_alias=True)
        save_worthy, reason = _is_cacheable(
            cleaned_for_check,
            min_ings=SAVE_GATE_MIN_INGREDIENTS,
            min_steps=SAVE_GATE_MIN_INSTRUCTIONS,
        )
        if not save_worthy:
            print(f"[SAVE-GATE] Refused: {reason}")
            raise HTTPException(
                status_code=422,
                detail={
                    "thin_recipe": True,
                    "reason": reason,
                    "min_ingredients": SAVE_GATE_MIN_INGREDIENTS,
                    "min_instructions": SAVE_GATE_MIN_INSTRUCTIONS,
                    "message": (
                        f"This recipe looks too thin to save ({reason}). "
                        f"Add more ingredients/steps, or confirm to save anyway."
                    ),
                },
            )

    # recipe_id is now app-minted at extract time and must be present on save.
    # Fallback to a fresh UUID kept only for callers that still POST without
    # one (no UI path produces such a request post-extract changes).
    recipe_id = (payload.get("recipe_id") or "").strip()
    if not recipe_id:
        recipe_id = str(uuid.uuid4())
        print(f"[SAVE] WARNING: payload missing recipe_id; minted {recipe_id}")
    now = datetime.now(timezone.utc).isoformat()

    # Normalize the source URL one more time at save (defensive — covers
    # recipes that were created before normalize_url existed in the extract
    # path, or hand-edited URLs).
    recipe_dict = recipe.model_dump(by_alias=True)
    # user_id is a row-column discriminator (0 = master_recipes, else =
    # recipes); pop it from the JSON blob so we don't double-store. Default
    # to PLACEHOLDER_USER_ID (1) when the caller didn't supply one — keeps
    # existing form payloads working unchanged.
    user_id = recipe_dict.pop("user_id", None)
    if user_id is None:
        user_id = PLACEHOLDER_USER_ID
    table = _recipes_table_for(user_id)
    source = recipe_dict.get("_source") or {}
    raw_source_url = source.get("originalUrl") or ""
    # A Wayback URL reaching this point is the TRANSPORT that fetched the page,
    # never the page's identity. Stored as-is it makes archive.org the recipe's
    # publisher: the row scores archive.org's authority, the mandatory
    # attribution link points at the archive instead of the author, and the same
    # recipe fetched directly later does not dedupe against it. 16 rows had to be
    # repaired on 2026-08-13 (scripts/unwrap_wayback_urls.py) — repairing them
    # without fixing this line just schedules the next repair.
    # The exact snapshot is preserved on `_source.archiveUrl`, so HOW we got the
    # page is not lost; `_source.origin` already records "archive.org".
    if is_wayback(raw_source_url):
        source.setdefault("archiveUrl", raw_source_url)
        raw_source_url = unwrap_wayback(raw_source_url)
        source["originalUrl"] = raw_source_url
        recipe_dict["_source"] = source
        print(f"[SOURCE] Wayback transport unwrapped to publisher identity: {raw_source_url}")
    normalized_source_url = normalize_url(raw_source_url) if raw_source_url else ""
    if normalized_source_url and normalized_source_url != raw_source_url:
        source["originalUrl"] = normalized_source_url
        recipe_dict["_source"] = source

    # Publisher auto-attribution (parity with
    # /process-selected): when a MASTER save's URL is already a publisher
    # cohort member, stamp the publisher block so a manual-queue / bookmarklet
    # capture reaches the corpus WITH publisher provenance — no per-capture
    # hint, no bookmarklet change. Only FILLS an absent publisher (never
    # clobbers a dish/kind from the reject-rescue path; the typed-membership
    # model allows both a dish AND a publisher block on one row). The matching
    # ledger row is re-flagged selected=1 AFTER the insert succeeds (below).
    _pub_attr_host = None
    if user_id == 0 and normalized_source_url:
        try:
            with _db() as _pconn:
                _prow = _pconn.execute(
                    "SELECT collection_key FROM collection_members "
                    "WHERE collection_type='publisher' AND url_normalized = ? LIMIT 1",
                    (normalized_source_url,),
                ).fetchone()
            if _prow:
                _pub_attr_host = _prow[0]
                _m = dict(recipe_dict.get("_master") or {})
                if not (_m.get("publisher") or "").strip():
                    _m["publisher"] = _pub_attr_host
                    _m.setdefault("kind", "top")
                    _m.setdefault("refreshed_at", datetime.now(timezone.utc).isoformat())
                    _m.setdefault("batch_source", "manual-capture")
                    recipe_dict["_master"] = _m
                    print(f"[ATTRIBUTE] master save attributed to publisher "
                          f"{_pub_attr_host!r} (URL is a cohort member)")
        except Exception as e:
            print(f"[ATTRIBUTE] publisher auto-attribution skipped: {e}")

    # Friendly publisher name -> _source.siteName, STORED on the model so
    # every list (recipe sidebar, master winners, chapter top-10) reads one
    # field. Prefer the page's captured og:site_name; fall back to the
    # curated domain map for known publishers. Runs on every save path
    # (paste, bookmarklet, markdown, batch) — this is the single chokepoint
    # all writes funnel through, including the job-runner's master writes.
    try:
        resolved_site = friendly_site_name(source.get("siteName"), raw_source_url)
        if resolved_site and resolved_site != (source.get("siteName") or ""):
            source["siteName"] = resolved_site
            recipe_dict["_source"] = source
    except Exception as e:
        print(f"[SITENAME] resolve failed (continuing): {e}")

    # Ingredient-aware measurements (_measurements) — RECOMPUTED from the
    # current ingredient list on every save so the metric/imperial toggle data
    # always matches what's actually stored (adds/edits/reorders stay in sync).
    # Deterministic-only here: free + instant, no LLM on the save path. Any
    # exotic ingredient an EXTRACT-time LLM pass already resolved is carried
    # forward by string (prior=incoming _measurements), so a plain save never
    # downgrades or re-pays for it. Covers ALL save paths (paste, bookmarklet,
    # image, pdf, manual), not just the URL extract.
    try:
        from enrich.measurement import convert_recipe_measurements
        convert_recipe_measurements(
            recipe_dict,
            use_llm_fallback=False,
            prior=recipe_dict.get("_measurements"),
        )
    except Exception as e:
        print(f"[MEASURE] recompute on save failed (continuing): {e}")

    # "Copy not subscription": claimed rows are detached from the source
    # URL. The `_source.claimedFrom` stamp (set by /recipes/<id>/claim)
    # marks the row as a clone. For claimed rows:
    #   - url_normalized is forced to "" so the dedup query below won't
    #     adopt this row when the user later re-extracts the same URL.
    #     Their fresh extract becomes a new row; their claimed-and-
    #     possibly-edited row stays untouched.
    #   - The daily cache-refresh's drift stamp also scopes by
    #     url_normalized, so it won't touch claimed rows either.
    # `_source.originalUrl` is preserved inside the data blob for
    # display ("claimed from allrecipes.com/..."); it's just not the
    # row's identity hook anymore.
    is_claimed_row = bool(source.get("claimedFrom"))
    if is_claimed_row:
        normalized_source_url = ""

    # Self-URL minting: when no external source URL exists (handwritten,
    # photo, typed recipe), generate one pointing back at this DB record:
    # https://bestcooksclub.com/r/<recipe_id>. The BCC domain is the
    # canonical public URL regardless of which host the server was
    # reached on (tunnel host, localhost, future cnames). Done BEFORE
    # the adopt-existing check below so a re-save of a once-saved local
    # recipe still works (the second save sees the same minted URL and
    # adopts the existing row). Skip for claimed rows — they
    # intentionally have no url_normalized.
    if not raw_source_url and not is_claimed_row:
        synthetic_url = _bcc_permalink(recipe_id)
        normalized_source_url = normalize_url(synthetic_url)
        source["originalUrl"] = synthetic_url
        # Stamp type so the form / future logic can tell apart minted-self
        # URLs from real external sources without parsing the URL.
        if not source.get("type") or source.get("type") in ("cookbook", ""):
            source["type"] = "local"
        recipe_dict["_source"] = source
        print(f"[SAVE] Minted self-URL: {synthetic_url}")

    # Drop unmeasured zeros out of _scoring before anything persists them. Placed
    # here because the URL is final by this point (including a minted self-URL),
    # and because EVERY save reaches it — the form, the bookmarklet, the batch and
    # the in-process callers — rather than fixing the one caller that happened to
    # send zeros. See _sanitize_scoring for why 0 is never a legal measurement.
    _sanitize_scoring(recipe_dict, normalized_source_url)

    # Deferred-screenshot backstop. The interactive extract no longer waits for
    # the capture, so a recipe can reach save with pageScreenshot unset while the
    # blob has since landed. Stamp it here rather than relying on the form having
    # polled — every save passes through this function, and a screenshot that
    # exists but is not referenced is a silent loss of something already paid for.
    try:
        _src = recipe_dict.get("_source") or {}
        if not (_src.get("pageScreenshot") or "").strip():
            _shot = _stored_screenshot_url(normalized_source_url)
            if _shot:
                _src["pageScreenshot"] = _shot
                recipe_dict["_source"] = _src
                print(f"[SCREENSHOT] backfilled at save: {_shot}")
    except Exception as e:
        print(f"[SCREENSHOT] save backstop skipped: {e}")

    # Dedup: if a row already exists for (url_normalized, user_id) in the
    # OWNER'S table, adopt ITS recipe_id instead of the form-sent UUID so
    # the existing record gets updated rather than creating a parallel
    # duplicate. The (url_normalized, user_id) unique index in each table
    # enforces this server-side too.
    adopted = False
    try:
        with _db() as conn:
            if normalized_source_url:
                existing = conn.execute(
                    f"SELECT recipe_id FROM {table} WHERE url_normalized = ? AND user_id = ? LIMIT 1",
                    (normalized_source_url, user_id),
                ).fetchone()
                if existing and existing[0] != recipe_id:
                    print(f"[SAVE] Adopting existing recipe_id {existing[0]} for {normalized_source_url!r} "
                          f"(was {recipe_id}) in {table}")
                    recipe_id = existing[0]
                    adopted = True
    except Exception as e:
        print(f"[WARN] dup lookup failed (continuing as insert): {e}")

    print(f"[SAVE] Saving recipe with ID: {recipe_id} (adopted={adopted}) user_id={user_id} table={table}")

    # Auto-enrich hook for master writes — keeps the "pay-once
    # enrichment" property: any recipe that enters master_recipes
    # carries provenance + classification + editorial, so every future
    # claimer inherits the rich data via static_subset. Idempotent:
    # skips rows where the LLM's biggest unique output
    # (classification.story) is already populated.
    # ~Few seconds per row (claude-haiku-4-5). Batch flows take the
    # latency hit one row at a time; interactive curator saves only pay
    # it if the row arrives un-enriched.
    # Best-effort: enrich failures log and continue — the save still
    # proceeds with whatever data we have. Token usage is appended to
    # save_usage_log so it can be journaled after the INSERT below.
    save_usage_log: list = []
    # _skip_auto_enrich is set true by the dish refresh job when the
    # dish's auto_enrich flag is off (the default). Lets batch saves
    # avoid the ~$0.05 + ~10s enrich cost per row; user can run
    # enrich manually later via the form's Enrich button. Live form
    # saves (no _skip flag set) preserve the original auto-enrich
    # behavior for master writes.
    # `_skip_auto_enrich` is the LEGACY boolean the batch jobs still send; `_enrich`
    # is the per-save control ("auto" | "always" | "never"). Both are honoured, and
    # an explicit `_enrich` wins, so nothing that sends the old flag changes.
    _mode = str(payload.get("_enrich") or "").strip().lower()
    if _mode not in ("auto", "always", "never"):
        _mode = "never" if payload.get("_skip_auto_enrich") else "auto"
    if _auto_enrich_applies(user_id) and _mode != "never":
        cls = recipe_dict.get("classification") or {}
        story = (cls.get("story") or "").strip()
        name = (recipe_dict.get("name") or "").strip()
        ingredients = recipe_dict.get("recipeIngredient") or []
        # AUTO enriches only a row that has no story yet — the "pay once" property.
        # ALWAYS re-runs even when a story exists, which is the only way to refresh
        # a stale one at save time (the form's Enrich button is the other route).
        _wanted = (_mode == "always") or not story
        if _wanted and name and ingredients:
            try:
                print(f"[SAVE-ENRICH] enrich_recipe (mode={_mode}, "
                      f"{'no story yet' if not story else 'refreshing existing story'})")
                t_enrich = time.perf_counter()
                _inject_dish_competitiveness(recipe_dict)
                enrich_recipe(recipe_dict, usage_log=save_usage_log)
                dt = int((time.perf_counter() - t_enrich) * 1000)
                new_story = ((recipe_dict.get("classification") or {})
                             .get("story") or "").strip()
                if new_story:
                    print(f"[SAVE-ENRICH] OK story={len(new_story)} chars ({dt}ms)")
                else:
                    print(f"[SAVE-ENRICH] WARN: no story produced after {dt}ms")
            except Exception as e:
                print(f"[SAVE-ENRICH] FAILED (continuing save): {e}")
    elif user_id == 0 and _mode == "never":
        # Was `skip_auto_enrich` — a name that stopped existing when the flag became
        # the three-way `_enrich` mode, and nothing caught it because this branch is
        # only reached when a caller explicitly opts OUT. Publisher-refresh does
        # exactly that, so every harvest save raised NameError AFTER paying for the
        # extract, the identity card and the screenshot. Read the resolved mode.
        print("[SAVE-ENRICH] skipped (caller opted out)")

    # === Identity card generation ==========================================
    # Every recipe (master or personal) gets a structured dish identity
    # card stamped on top-level `_identity`. The card encodes
    # ingredientRoles (per-ingredient role tags), cuisine, ethnicity,
    # technique, servingForm, and the LLM's canonical-dish conclusion
    # in `likelyDish` — derived from the facts the LLM just committed
    # to via the ordered tool_use schema.
    #
    # This replaces the older classification.dishSignal field. The
    # card is the structured truth; dishSignal becomes a derived
    # display string (filled in below from card.likelyDish) for
    # backward-compat with UI that still reads dishSignal.
    #
    # Cost: ~$0.0001/call via Haiku with ordered tool_use, ~2-3s.
    # Skipped when the card already exists (idempotent) and when the
    # recipe has no name. Failures swallowed — embedding composer
    # falls back to dishSignal then to raw ingredients.
    existing_card = recipe_dict.get("_identity")
    if (recipe_dict.get("name") or "").strip() and not (
        isinstance(existing_card, dict)
        and (existing_card.get("likelyDish") or "").strip()
    ):
        try:
            card = generate_identity_card_for_recipe(recipe_dict, usage_log=save_usage_log)
            if card:
                recipe_dict["_identity"] = card
                # Keep classification.dishSignal in sync as a derived
                # display string. UI surfaces that still read it
                # continue to work; new code reads _identity.
                cls_for_signal = recipe_dict.get("classification") or {}
                cls_for_signal["dishSignal"] = (card.get("likelyDish") or "").strip()
                recipe_dict["classification"] = cls_for_signal
                print(f"[IDENTITY] stamped: likelyDish={card.get('likelyDish')!r} "
                      f"primary={card.get('primaryIngredients')}")
        except Exception as e:
            print(f"[IDENTITY] FAILED (continuing save): {e}")

    # === Exceptionalism grade ==============================================
    # Three paths to a grade, picked in order:
    #   1. Already stamped (batch path stamps _master.exceptionalism per
    #      entry in the batch step) — keep as-is.
    #   2. Master row with explicit _master.dish (harvest / editors_choice /
    #      manually-tagged) — grade against THAT dish's stored last_ou_fit.
    #   3. Master row OR personal row without an explicit dish — embedding-
    #      match the recipe to a dish, grade against the matched dish's
    #      last_ou_fit. Below the confidence threshold, no grade
    #      (em-dash in UI).
    # Master rows stamp the result on `_master.exceptionalism` (existing
    # shape). Personal rows stamp on `_grade` (new top-level field) so
    # the UI can render the same badge for both.
    try:
        _grade_recipe_on_save(recipe_dict, user_id=user_id)
    except Exception as e:
        print(f"[GRADE] FAILED (continuing save): {e}")

    try:
        with _db() as conn:
            # Save clears source_changed_at: the user reviewing and saving is
            # the acknowledgement of any prior drift signal.
            conn.execute(f"""
                INSERT INTO {table} (recipe_id, user_id, data, url_normalized, source_changed_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(recipe_id) DO UPDATE SET
                    data = excluded.data,
                    url_normalized = excluded.url_normalized,
                    source_changed_at = NULL,
                    updated_at = excluded.updated_at;
            """, (
                recipe_id,
                user_id,
                json.dumps(recipe_dict, indent=2),
                normalized_source_url,
                now,
                now
            ))
            # Moz scoring happens at EXTRACT time now (see _attach_moz_scoring
            # in each /extract-from-* endpoint). The recipe arriving at save
            # already carries PA/DA/OU/rootDomain in its _scoring block; we
            # just persist it as-is. Bump last_accessed on the metabase_url
            # row though, so refresh_url_metadata.py's --refresh-stale logic
            # knows the URL is still in active use.
            if normalized_source_url:
                try:
                    conn.execute(
                        "UPDATE metabase_url SET last_accessed = ? WHERE url = ?",
                        (now, normalized_source_url),
                    )
                except Exception as e:
                    print(f"[WARN] metabase_url last_accessed bump failed: {e}")
            print("[OK] Recipe saved to database")
            # Re-flag the publisher ledger so selected=1 matches this actually-
            # saved master row (parity with /process-selected
            # — a manual-queue capture now flips the cohort row to a winner and
            # the harvest worklist / leaderboard reflect it). Idempotent.
            if _pub_attr_host:
                try:
                    conn.execute(
                        "UPDATE collection_members SET selected = 1 WHERE "
                        "collection_type='publisher' AND collection_key = ? "
                        "AND url_normalized = ?",
                        (_pub_attr_host, normalized_source_url),
                    )
                    print(f"[ATTRIBUTE] ledger selected=1 for {_pub_attr_host} :: {normalized_source_url}")
                except Exception as e:
                    print(f"[ATTRIBUTE] ledger selected re-flag skipped: {e}")
            # Journal token usage from the save-time auto-enrich hook
            # (master rows only). Tagged with the row's recipe_id +
            # user_id so cost shows up in bcc_token_journal next to
            # extract-time usage.
            if save_usage_log:
                write_usage_entries(
                    conn,
                    user_id=user_id,
                    recipe_id=recipe_id,
                    entries=save_usage_log,
                )
            # Fetch the DB-assigned integer PK so the form can display it.
            row = conn.execute(f"SELECT id FROM {table} WHERE recipe_id = ?", (recipe_id,)).fetchone()
            seq_id = row[0] if row else None

            # For master rows, embed the recipe and upsert into
            # recipes_master_vec so the "We Think You'd Like"
            # recommender has fresh KNN data. classification.dishSignal
            # is already stamped above — compose_recipe_text uses it as
            # the dominant signal, so the embedding captures dish
            # identity cleanly. Best-effort: failure doesn't break the
            # save (sqlite-vec may be absent, the embedder may fail,
            # etc. — the row still lands).
            # Embed EVERY saved recipe (master AND user) and persist the vector
            # on its row's `embedding` BLOB — the source of truth for
            # dish-matching, "find similar", dedup, and recommendations.
            # compose_recipe_text leans on the identity card / dishSignal so the
            # vector captures dish identity cleanly. Master rows additionally
            # upsert the recipes_master_vec KNN index (the live recommender);
            # user rows just store the BLOB (a recipes_vec index can derive from
            # it later). Best-effort: a failure here never breaks the save.
            if seq_id is not None:
                try:
                    from input.pipeline.embeddings import (
                        EMBED_MODEL, compose_recipe_text, embed_text, text_hash,
                        vec_to_bytes,
                    )
                    from input.pipeline import vector_store
                    txt = compose_recipe_text(recipe_dict)
                    if txt.strip():
                        rec_vec = embed_text(txt)
                        # PROVENANCE, stamped with the vector rather than after
                        # it. Saving has always written the embedding and never
                        # said what text produced it, so every newly saved row
                        # arrived with a NULL embedding_text_hash — and a NULL
                        # hash can never match, which means check_embeddings
                        # cannot tell "current" from "stale" and reembed_identity
                        # redoes the row on every pass. That is how the
                        # 2026-08-06 composer-order bug went unnoticed across
                        # 41% of the corpus. `dishes` stamped these three from
                        # the start; the recipe tables never did.
                        # embeddings.text_hash is THE definition — a local
                        # sha256 here stored 64 chars while both readers
                        # computed 16, so every saved row read as stale.
                        _emb_hash = text_hash(txt)
                        _emb_now = datetime.now(timezone.utc).isoformat()
                        if user_id == 0:
                            # Master: store the source-of-truth vector + the
                            # derived KNN index the recommender reads.
                            vector_store.enable_vec(conn)
                            ch = ((recipe_dict.get("classification") or {}).get("chapter") or None)
                            dish_for_vec = (recipe_dict.get("_master") or {}).get("dish") or None
                            # A master row that does NOT already know its dish
                            # gets one inferred, exactly like a user row. Dish
                            # refreshes curate FOR a dish and carry _master.dish
                            # as ground truth; an interactive capture carries
                            # nothing, and used to keep nothing — embedded, but
                            # with the "Matched dish" chip empty, which reads as
                            # "it never got embedded".
                            _stamped = False
                            if not dish_for_vec:
                                _stamped = _stamp_dish_match(
                                    conn, recipe_dict, rec_vec,
                                    label=f"master recipe {seq_id}")
                                dish_for_vec = (recipe_dict.get("_match") or {}).get("dish") or None
                            if _stamped:
                                # _match changed the row, so `data` goes back too.
                                # compose_recipe_text never reads _match, so the
                                # hash stamped above is still the right one.
                                conn.execute(
                                    "UPDATE master_recipes SET embedding = ?, data = ?, "
                                    "embedding_model = ?, embedding_text_hash = ?, "
                                    "embedding_updated_at = ? WHERE id = ?",
                                    (vec_to_bytes(rec_vec), json.dumps(recipe_dict),
                                     EMBED_MODEL, _emb_hash, _emb_now, seq_id),
                                )
                            else:
                                conn.execute(
                                    "UPDATE master_recipes SET embedding = ?, "
                                    "embedding_model = ?, embedding_text_hash = ?, "
                                    "embedding_updated_at = ? WHERE id = ?",
                                    (vec_to_bytes(rec_vec), EMBED_MODEL, _emb_hash,
                                     _emb_now, seq_id),
                                )
                            vector_store.upsert_recipe_vector(
                                conn, seq_id, rec_vec, chapter=ch, dish=dish_for_vec,
                            )
                            print(f"[VEC] upserted master recipe {seq_id} (dish={dish_for_vec!r}, chapter={ch!r})")
                        else:
                            # User recipe: store the vector AND match it to a dish.
                            # Same helper the master branch uses — the rule is
                            # "infer a dish when the row doesn't know one", and a
                            # user row never knows one.
                            vector_store.enable_vec(conn)
                            _stamp_dish_match(conn, recipe_dict, rec_vec,
                                              label=f"user recipe {seq_id}")
                            conn.execute(
                                "UPDATE recipes SET embedding = ?, data = ?, "
                                "embedding_model = ?, embedding_text_hash = ?, "
                                "embedding_updated_at = ? WHERE id = ?",
                                (vec_to_bytes(rec_vec), json.dumps(recipe_dict),
                                 EMBED_MODEL, _emb_hash, _emb_now, seq_id),
                            )
                except Exception as e:
                    print(f"[VEC] recipe embed/match failed: {e}")
    except Exception as e:
        print(f"[ERROR] Database error: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e

    # WHY THERE IS NO SCORE. Scoring happens HERE, at save — but the form
    # clears itself on a successful save (the 2026-05-29 "each save is a
    # discrete transaction" UX), so the scoring strip the user is looking at was
    # populated at EXTRACT time, before any scoring existed. The reason was
    # written to `_scoring.scoringNote` and displayed to nobody. Return it so
    # the save confirmation can say it out loud.
    _unscored_note = ""
    try:
        _sc = (recipe_dict.get("_scoring") or {})
        if _sc.get("pageAuthority") is None:
            _unscored_note = (_sc.get("scoringNote") or "").strip()
    except Exception:
        _unscored_note = ""

    return {
        "recipe_id": recipe_id,
        "id": seq_id,
        "adopted": adopted,
        "bccUrl": _bcc_permalink(recipe_id),
        # '' when the row scored normally — the caller shows this only when set.
        "unscoredNote": _unscored_note,
    }


@app.post("/recipes")
async def save_recipe(request: Request):
    """Thin async wrapper around _save_recipe_core. Pulls payload from the
    request body and offloads the synchronous DB work to a worker thread
    so the event loop stays free to service other requests (notably the
    self-call pattern when /dishes/<name>/refresh saves many rows).

    Master writes (payload.user_id == 0) require the `edit_master`
    permission. Personal saves are open to anyone (own_recipes is
    granted to all roles including 'member')."""
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}") from e
    # Manual-from-reject rescues: when the bookmarklet harvested a dish
    # hint from #_bcc_dish=… the staged data carries bcc_hints. The form
    # threads them into the save payload. The hint determines the TARGET
    # (force user_id=0 → master), the role check still gates the ACTOR.
    # This means a non-staff member who somehow crafts a bcc_hints
    # payload still gets 403'd at the master gate below — no privilege
    # escalation.
    # `master` joins `dish` as a target hint. A PUBLISHER capture from the domains
    # cohort queue has no dish to name, which is why that flow previously had to
    # nudge the form through localStorage['sidebar:user_id'] — a global that a bare
    # bookmarklet press never set (a Milk Street grab landed in user 5's library)
    # and that, once set, persisted to mis-target the next personal grab. Same
    # channel, same actor gate below: the hint says WHERE, the permission says WHO.
    hints = payload.get("bcc_hints")
    if isinstance(hints, dict) and ((hints.get("dish") or "").strip() or hints.get("master")):
        payload["user_id"] = 0

    # Gate master writes here, before threading off the DB work. The
    # job-runner path (dish refresh) calls _save_recipe_core directly,
    # NOT this endpoint, so it bypasses this gate by design — it's a
    # trusted in-process caller, not user input.
    #
    # Careful: payload.get("user_id", 1) or 1 would mis-fire on 0
    # because Python treats 0 as falsy. Explicit None-check instead.
    uid_raw = payload.get("user_id")
    if uid_raw is not None and int(uid_raw) == 0:
        # Master write. Gate the actor + preserve the explicit 0
        # (don't overwrite with the caller's personal id below).
        _require_perm(request, "edit_master")
    else:
        # Personal save. Honor the X-Self-User-Id header (set by
        # library-shell.js patchFetch from localStorage's
        # app:self_user_id) over whatever the form's hidden user_id
        # field defaulted to. The hidden field in
        # recipe_form_styled.html defaults to "1" on fresh extracts,
        # which silently routes every paste-extract save to user 1
        # regardless of who's signed in — the bug user reported
        # 2026-05-29 (John Landry/Official = user 5, paste-extracted
        # recipes landing on user 1). The header is authoritative for
        # which user owns the write; the hidden form field stays as a
        # last-resort fallback when no header is set.
        caller = _resolve_caller(request)
        caller_uid = (caller or {}).get("user_id")
        if caller_uid is not None and int(caller_uid) > 0:
            payload["user_id"] = int(caller_uid)
    result = await asyncio.to_thread(_save_recipe_core, payload)

    # REDACT OPERATIONAL DETAIL FOR NON-STAFF. A member who bookmarklets a
    # recipe is an END USER of a product, not an operator of our pipeline: the
    # names of the data vendors we buy from, and instructions like "set MOZ
    # creds and run refresh", are ours and mean nothing to them. Done HERE, at
    # the response boundary, rather than in the form — client-side redaction
    # only hides the string, it still ships it.
    try:
        if isinstance(result, dict) and (result.get("unscoredNote") or "").strip():
            if not auth_lib.is_staff(_resolve_caller(request) or {}):
                result["unscoredNote"] = GENERIC_UNSCORED_NOTE
    except Exception:
        # Never fail a save over message cosmetics — but fail CLOSED, to the
        # generic text, so an error here cannot leak the detailed one.
        if isinstance(result, dict) and result.get("unscoredNote"):
            result["unscoredNote"] = GENERIC_UNSCORED_NOTE
    return result


# Read-only metadata lookup for the form's collapsible metadata section.
# URL is passed as a query param to avoid edge cases with slashes in path
# params, and is re-normalized server-side regardless of what the client sent.
@app.get("/url-metadata")
def get_url_metadata(url: str, request: Request = None):
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    try:
        with _db() as conn:
            ensure_metabase_url_table(conn)
            row = get_metabase_url(conn, url)
            # Self-heal: if a row exists but Moz scoring never landed (null
            # moz_last_scored — e.g. transient Moz outage at the save that
            # created the row), try once now so the viewer sees real scores
            # instead of "scoring not yet run." Failed scoring leaves the
            # null state intact; never zeroes existing values.
            if row and not row.get("moz_last_scored"):
                from input.pipeline.url_scoring import score_url_via_moz, _apply_moz_scores
                from datetime import datetime, timezone
                scores = score_url_via_moz(row["url"])
                if scores:
                    _apply_moz_scores(conn, row["url"], scores,
                                      datetime.now(timezone.utc).isoformat())
                    row = get_metabase_url(conn, url)
    except Exception as e:
        print(f"[ERROR] url-metadata lookup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Lookup error: {e}") from e
    if not row:
        # Empty shape so the form can render placeholder fields without
        # branching on null vs missing.
        return _redact_metadata_keys({
            "url": normalize_url(url),
            "root_domain": "",
            "raw_title": "",
            "page_authority": None,
            "domain_authority": None,
            "ou_score": None,
            "moz_last_scored": None,
            "first_seen": None,
            "last_accessed": None,
            "exists": False,
        }, request)
    row["exists"] = True
    return _redact_metadata_keys(row, request)


# The vendor's name is not only in our PROSE — it is in our FIELD NAMES, and a
# key is as readable as a sentence to anyone with devtools open. The 2026-08-14
# sweep caught the messages and missed these, because structured data does not
# look like a message. /url-metadata is on the public-host allowlist
# (host_gate.py) and the recipe form calls it, so a member receives this body.
#
# Renamed rather than dropped: the form legitimately shows "Score updated <date>",
# so the VALUE is the customer's business and only the vendor-named KEY is ours.
# moz_http_code is dropped outright — it is a diagnostic with no user meaning.
_METADATA_STAFF_ONLY_KEYS = ("moz_http_code",)
_METADATA_KEY_RENAMES = {"moz_last_scored": "score_updated_at"}


def _redact_metadata_keys(row: dict, request) -> dict:
    """Strip vendor-named keys from a /url-metadata body for non-staff callers.

    Fails CLOSED: any error resolving the caller redacts. Staff get the row
    unchanged AND the renamed alias, so one client shape works for both.
    """
    if not isinstance(row, dict):
        return row
    try:
        staff = bool(auth_lib.is_staff(_resolve_caller(request) or {})) if request else False
    except Exception:
        staff = False
    out = dict(row)
    for src, dst in _METADATA_KEY_RENAMES.items():
        if src in out:
            out[dst] = out[src]          # alias for every caller...
            if not staff:
                out.pop(src, None)       # ...but only staff keep the original key
    if not staff:
        for k in _METADATA_STAFF_ONLY_KEYS:
            out.pop(k, None)
    return out


@app.get("/public-score")
def get_public_score(ou: float = None, da: float = None, pa: float = None):
    """The PUBLIC face of a recipe's score — stars and, sometimes, a badge.

    The quantising lives on the server on purpose (see
    input/pipeline/public_scoring): a page that receives the raw numbers and
    works out stars in JavaScript has published the ranking method to anyone
    who opens devtools. The admin recipe form already holds the raw scores, so
    this endpoint is not hiding anything FROM it — it exists so there is ONE
    implementation of the mapping, and so the curator sees exactly what a user
    would see rather than a second copy of the arithmetic that can drift.

    Percentiles are computed against master_recipes — the curated index IS the
    cohort a public star is relative to. Two counting scans over ~4.5k rows;
    ou_score is indexed, power is not, which is fine at this size.

    Takes the raw scores as query params rather than a recipe id so it also
    answers for a recipe that has been extracted but not yet saved.
    """
    if ou is None or da is None or pa is None:
        return {}
    try:
        from input.pipeline.public_scoring import public_score
        power = float(da) + float(pa)
        with _db() as conn:
            r = conn.execute(
                """
                SELECT (SELECT COUNT(*) FROM master_recipes
                          WHERE ou_score IS NOT NULL AND ou_score < ?)  AS ou_below,
                       (SELECT COUNT(*) FROM master_recipes
                          WHERE ou_score IS NOT NULL AND power IS NOT NULL) AS n_ou,
                       (SELECT COUNT(*) FROM master_recipes
                          WHERE power IS NOT NULL AND power < ?)        AS pw_below
                """,
                (float(ou), power),
            ).fetchone()
        n = (r["n_ou"] if isinstance(r, sqlite3.Row) else r[1]) or 0
        if n <= 1:
            return {}
        ou_below = r["ou_below"] if isinstance(r, sqlite3.Row) else r[0]
        pw_below = r["pw_below"] if isinstance(r, sqlite3.Row) else r[2]
        out = public_score(ou_below / n, pw_below / n)
        # The inputs are NOT echoed back. This endpoint's whole job is to be
        # the boundary; a convenience echo here is how the boundary leaks.
        return out
    except Exception as e:
        print(f"[ERROR] public-score failed: {e}")
        return {}


# Delete a recipe. user_id dispatches to the right table (0 = master,
# else = personal). Cross-table delete is a 404 — admins must be explicit
# about which collection they're removing from.
@app.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: str, request: Request, user_id: int = PLACEHOLDER_USER_ID):
    # Master-row deletes require the `delete_master` permission.
    # Personal deletes are open (the user is deleting their own row).
    # In neither case do we verify the caller IS the owner of the
    # target personal row — that gate belongs to a later visibility/
    # auth pass; today the trust model is "client sent the right uid".
    if user_id == 0:
        _require_perm(request, "delete_master")
    table = _recipes_table_for(user_id)
    print(f"[DELETE] Delete recipe endpoint called for: {recipe_id} user_id={user_id} table={table}")
    try:
        with _db() as conn:
            _enable_vec_for_delete(conn)  # trg_master_vec_cleanup needs the module
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM {table} WHERE recipe_id = ? AND user_id = ?",
                           (recipe_id, user_id))
            if cursor.rowcount == 0:
                print(f"[ERROR] Recipe {recipe_id} not found in {table} for user_id={user_id}")
                raise HTTPException(status_code=404, detail="Recipe not found")
            conn.commit()
            print(f"[OK] Recipe {recipe_id} deleted successfully from {table}")
        return {"message": "Recipe deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error deleting recipe: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}") from e


# Extract recipe from image (no save). Image is OCR'd to markdown via the
# vision model, then routed through the same /extract-from-markdown pipeline
# so source_url/title plumbing and validation are handled in one place.
@app.post("/extract-from-image")
async def extract_from_image_endpoint(
    request: Request,
    image: UploadFile = File(...),
    source_url: str = Form(""),
    title: str = Form(""),
    user_id: int = Form(PLACEHOLDER_USER_ID),
):
    # PAID WORK — an authenticated caller only. These endpoints each spend
    # real LLM money, and until 2026-07-30 every one of them was reachable
    # anonymously AND allowlisted on the public host, so a stranger could
    # burn our model spend by POSTing in a loop. The recipe form's
    # client-side sign-in prompt never protected this: a client check
    # cannot gate a paid endpoint, it only decides what our own UI does.
    # `own_recipes` is held by every role including member, so this costs
    # no real user anything; anonymous gets a 401 it can act on.
    _require_perm(request, "own_recipes")
    print("[EXTRACT] Extract from image endpoint called")
    try:
        if not image.content_type or not image.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="File must be an image")

        temp_dir = Path("input")
        temp_dir.mkdir(exist_ok=True)

        file_ext = Path(image.filename).suffix.lower() if image.filename else ".jpg"
        temp_filename = f"extract_{uuid.uuid4()}{file_ext}"
        temp_path = temp_dir / temp_filename

        print(f"[EXTRACT] Saving uploaded image to {temp_path}")
        content = await image.read()
        with open(temp_path, "wb") as f:
            f.write(content)

        print(f"[EXTRACT] Running canonical image -> markdown -> recipe chain (source_url={source_url!r})")
        # Mint the recipe UUID now so token-journal entries (and any future
        # ledger writes) can reference the eventual recipe before save.
        new_recipe_id = str(uuid.uuid4())
        # Canonical chain: vision OCR -> markdown -> single LLM extract that
        # also fills provenance + classification. Per-stage timings reported.
        timings: dict = {}
        prompts: dict = {}
        usage_log: list = []
        import llm  # gateway: attribute migrated-module usage to this recipe/user (flushed by _journal_usage)
        llm.enter(recipe_id=new_recipe_id, user_id=user_id)
        t_start = time.perf_counter()

        # Endpoint-level cache: when the bookmarklet supplies a source_url,
        # a previously-extracted recipe for that URL skips both the vision
        # OCR call AND the markdown-extract LLM call.
        url_norm = normalize_url(source_url) if source_url else ""
        recipe, prior_fp, _src_fp, cache_status = _extract_cache_lookup(url_norm, usage_log=usage_log)
        drift = False
        path_used = "cache-hit" if recipe is not None else "image-llm"

        if recipe is None:
            try:
                md = await asyncio.to_thread(image_to_markdown, str(temp_path),
                                             timings=timings, usage_log=usage_log)
            except Exception as e:
                print(f"[ERROR] image_to_markdown failed: {e}")
                print(f"[ERROR] Traceback: {traceback.format_exc()}")
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise HTTPException(status_code=500, detail=f"Vision extraction error: {e}") from e

            if not md or not md.strip():
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise HTTPException(status_code=500, detail="Vision step returned empty markdown")

            # Stash the vision-stage prompt so the UI can surface it. Use a
            # sub-key to avoid colliding with markdown_to_recipe's prompts.
            prompts["vision"] = {
                "model": "claude-sonnet-4-6",
                "system_prompt": IMAGE_TO_MARKDOWN_PROMPT,
            }

            try:
                recipe = await asyncio.to_thread(
                    markdown_to_recipe,
                    md,
                    source_name=image.filename or "",
                    source_url=source_url,
                    title=title,
                    timings=timings,
                    prompts=prompts,
                    usage_log=usage_log,
                )
            except Exception as e:
                print(f"[ERROR] markdown_to_recipe failed: {e}")
                print(f"[ERROR] Traceback: {traceback.format_exc()}")
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise HTTPException(status_code=500, detail=f"Extraction error: {e}") from e

            if recipe is None:
                print("[ERROR] Extraction failed - no result")
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise HTTPException(status_code=500, detail="Failed to extract recipe from image")

            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        # Moz scoring at extract time so the form can show PA/DA/OU/root
        # before the user decides whether to save. Cheap, URL-keyed, no
        # dependency on the recipe being persisted.
        _finalize_extract_recipe(recipe, url_norm=url_norm, usage_log=usage_log)
        # Stamp the minted UUID onto the recipe so the form picks it up.
        recipe["id"] = new_recipe_id
        # Retain the ORIGINAL captured image as the recipe's sourceImage
        # (provenance + a reference to validate our extraction against — e.g.
        # handwritten margin notes), and DEFAULT the hero to it when the recipe
        # has none. Stored via the no-crop hero path so the whole page stays
        # legible; the user can swap the hero later in the editor. Best-effort —
        # a failure here must never fail the extraction.
        try:
            from input.pipeline.image_pipeline import standardize_and_meta
            processed, _smeta = standardize_and_meta(content, source_url=None, localized=True)
            if processed:
                src_name = f"upload_{uuid.uuid4()}.jpg"
                (GENERATED_DIR / src_name).write_bytes(processed)
            else:
                src_name = f"upload_{uuid.uuid4()}{file_ext}"
                (GENERATED_DIR / src_name).write_bytes(content)
            src_url = f"/generated/{src_name}"
            recipe["sourceImage"] = [src_url]
            if not recipe.get("image"):
                recipe["image"] = [src_url]  # hero defaults to the original; editable later
        except Exception as e:
            print(f"[EXTRACT] sourceImage persist skipped: {e}")
        return _extract_response(
            recipe, new_recipe_id=new_recipe_id, timings=timings,
            prompts=prompts, usage_log=usage_log, t_start=t_start, user_id=user_id)

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error extracting from image: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Extraction error: {e}") from e


# Extract recipe from a PDF upload (no save). Mirrors /extract-from-image
# but uses pdf_bytes_to_markdown (multi-page vision OCR) instead of
# image_to_markdown. URL-based PDFs go through /extract-from-url, which
# detects Content-Type: application/pdf and dispatches to pdf_url_to_markdown
# itself — same canonical markdown -> recipe chain at the end.
@app.post("/extract-from-pdf")
async def extract_from_pdf_endpoint(
    request: Request,
    file: UploadFile = File(...),
    source_url: str = Form(""),
    title: str = Form(""),
    user_id: int = Form(PLACEHOLDER_USER_ID),
):
    # PAID WORK — an authenticated caller only. These endpoints each spend
    # real LLM money, and until 2026-07-30 every one of them was reachable
    # anonymously AND allowlisted on the public host, so a stranger could
    # burn our model spend by POSTing in a loop. The recipe form's
    # client-side sign-in prompt never protected this: a client check
    # cannot gate a paid endpoint, it only decides what our own UI does.
    # `own_recipes` is held by every role including member, so this costs
    # no real user anything; anonymous gets a 401 it can act on.
    _require_perm(request, "own_recipes")
    from to_markdown.pdf_to_markdown import pdf_bytes_to_markdown
    print("[EXTRACT] Extract from PDF endpoint called")
    try:
        ctype = (file.content_type or "").lower()
        if "pdf" not in ctype and not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="File must be a PDF")

        pdf_bytes = await file.read()
        if not pdf_bytes:
            raise HTTPException(status_code=400, detail="PDF upload was empty")

        new_recipe_id = str(uuid.uuid4())
        timings: dict = {}
        prompts: dict = {}
        usage_log: list = []
        import llm  # gateway: attribute migrated-module usage to this recipe/user
        llm.enter(recipe_id=new_recipe_id, user_id=user_id)
        t_start = time.perf_counter()

        # Endpoint-level cache: a previously-extracted recipe for this URL
        # skips both the PDF render+vision step AND the markdown-extract LLM
        # call. Empty source_url means cache is skipped (e.g. raw upload
        # with no URL context).
        url_norm = normalize_url(source_url) if source_url else ""
        recipe, prior_fp, _src_fp, cache_status = _extract_cache_lookup(url_norm, usage_log=usage_log)
        drift = False
        path_used = "cache-hit" if recipe is not None else "pdf-llm"

        if recipe is None:
            try:
                md = await asyncio.to_thread(
                    pdf_bytes_to_markdown, pdf_bytes,
                    timings=timings, usage_log=usage_log,
                )
            except Exception as e:
                print(f"[ERROR] pdf_bytes_to_markdown failed: {e}")
                print(f"[ERROR] Traceback: {traceback.format_exc()}")
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise HTTPException(status_code=500, detail=f"PDF extraction error: {e}") from e

            if not md or not md.strip():
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise HTTPException(status_code=500, detail="PDF vision step returned empty markdown")

            prompts["vision"] = {
                "model": "claude-sonnet-4-6",
                "system_prompt": PDF_TO_MARKDOWN_PROMPT,
            }

            try:
                recipe = await asyncio.to_thread(
                    markdown_to_recipe,
                    md,
                    source_name=file.filename or "",
                    source_url=source_url,
                    title=title,
                    timings=timings,
                    prompts=prompts,
                    usage_log=usage_log,
                )
            except Exception as e:
                print(f"[ERROR] markdown_to_recipe failed: {e}")
                print(f"[ERROR] Traceback: {traceback.format_exc()}")
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise HTTPException(status_code=500, detail=f"Extraction error: {e}") from e

            if recipe is None:
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise HTTPException(status_code=500, detail="Failed to extract recipe from PDF")

            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        _finalize_extract_recipe(recipe, url_norm=url_norm, usage_log=usage_log)
        recipe["id"] = new_recipe_id
        return _extract_response(
            recipe, new_recipe_id=new_recipe_id, timings=timings, prompts=prompts,
            usage_log=usage_log, t_start=t_start, user_id=user_id,
            ok_msg="PDF extraction successful")

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error extracting from PDF: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Extraction error: {e}") from e


# Extract recipe from markdown text (no save). Canonical path: markdown ->
# RecipeModel via the single JSON-LD-aware LLM call. Provenance and
# classification are filled in the same call.
@app.post("/extract-from-markdown")
async def extract_from_markdown_endpoint(
    request: Request,
    file: UploadFile = File(...),
    source_url: str = Form(""),
    title: str = Form(""),
    user_id: int = Form(PLACEHOLDER_USER_ID),
):
    # PAID WORK — an authenticated caller only. These endpoints each spend
    # real LLM money, and until 2026-07-30 every one of them was reachable
    # anonymously AND allowlisted on the public host, so a stranger could
    # burn our model spend by POSTing in a loop. The recipe form's
    # client-side sign-in prompt never protected this: a client check
    # cannot gate a paid endpoint, it only decides what our own UI does.
    # `own_recipes` is held by every role including member, so this costs
    # no real user anything; anonymous gets a 401 it can act on.
    _require_perm(request, "own_recipes")
    print("[EXTRACT] Extract from markdown endpoint called")
    try:
        raw = await file.read()
        try:
            markdown_text = raw.decode("utf-8")
        except UnicodeDecodeError:
            markdown_text = raw.decode("latin-1")

        if not markdown_text.strip():
            raise HTTPException(status_code=400, detail="Markdown file is empty")

        source_name = file.filename or ""

        # Pre-pass: normalize the markdown and sniff for an embedded source
        # URL / title that the saver may have stamped on top of the body
        # (e.g. "*Source: <url>*" line from a bookmarklet/converter). Lets
        # plain .md drops still benefit from Moz scoring at save time.
        envelope = markdown_passthrough(
            markdown_text,
            source_url=source_url,
            title=title,
        )
        effective_md = envelope["markdown"]
        effective_url = envelope["source_url"]
        effective_title = envelope["title"]
        # Mint the recipe UUID now so the token-journal row references it.
        new_recipe_id = str(uuid.uuid4())
        print(f"[EXTRACT] Running canonical markdown extraction on {source_name} "
              f"({len(effective_md)} chars) source_url={effective_url!r} title={effective_title!r}")

        timings: dict = {}
        prompts: dict = {}
        usage_log: list = []
        import llm  # gateway: attribute migrated-module usage to this recipe/user
        llm.enter(recipe_id=new_recipe_id, user_id=user_id)
        t_start = time.perf_counter()

        # Cache lookup FIRST — before the expensive translation. A non-English
        # bookmarklet recipe that is already cached (stored as translated
        # English) must return instantly, NOT pay the ~30s translation before
        # the lookup even runs. This mirrors the URL path's speculative
        # fast-path: never do expensive work for a result we already have.
        url_norm = normalize_url(effective_url) if effective_url else ""
        recipe, prior_fp, _src_fp, cache_status = _extract_cache_lookup(url_norm, usage_log=usage_log)
        drift = False
        path_used = "cache-hit" if recipe is not None else ""
        translation_meta_bm: dict | None = None

        # Cache HIT that carries no equipment — a row cached from the JSON-LD fast lane
        # (or before equipment was derived at extract time). Derive it now and HEAL the
        # cache, else every re-extract of this URL keeps returning tools-less (the
        # shrimp-creole bug: a cache hit skipped the miss-block _ensure_equipment below).
        if recipe is not None and not (recipe.get("equipment") or []):
            _ensure_equipment(recipe, path_used="cache-hit")
            if recipe.get("equipment"):
                try:
                    _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)
                    print("[EXTRACT] healed cached recipe with derived equipment")
                except Exception as e:
                    print(f"[EXTRACT] equipment cache-heal skipped: {e}")

        if recipe is None:
            # === Extraction-stage translation (bookmarklet path), MISS only ===
            # Markdown comes from the bookmarklet/browser, so there are no HTTP
            # headers or <html lang>. Detect language from the markdown body via
            # fasttext (tier 3 of detect_language). If non-English, translate
            # before extraction. JSON-LD blob from the envelope is dropped on
            # translation since its content would still be source-language.
            try:
                from intake.translate import (
                    is_non_english, detect_language, translate_extraction_markdown,
                )
                page_lang_bm = detect_language("", headers=None, visible_text=effective_md)
                if is_non_english(page_lang_bm):
                    t_xlate0 = time.perf_counter()
                    try:
                        xr = translate_extraction_markdown(effective_md, page_lang_bm)
                        xlate_ms = int((time.perf_counter() - t_xlate0) * 1000)
                        if xr.plausibility_ok:
                            effective_md = xr.translated_markdown
                            # Drop bookmarklet-harvested JSON-LD so the
                            # downstream fast lane doesn't reach for the
                            # original-language structured data.
                            if envelope.get("jsonld"):
                                envelope["jsonld"] = []
                            translation_meta_bm = {
                                "originalLanguage": xr.source_language,
                                "translated": True,
                                "translatedAt": datetime.now(timezone.utc).isoformat(),
                                "originalTitle": xr.original_title or effective_title or "",
                            }
                            timings["translate_ms"] = xlate_ms
                            print(f"[XLATE] (bookmarklet) translated from "
                                  f"{xr.source_language_name} ({xlate_ms}ms)")
                        else:
                            print(f"[XLATE] (bookmarklet) suspect "
                                  f"({xr.plausibility_reason}); using original")
                    except Exception as e:
                        print(f"[XLATE] (bookmarklet) failed: "
                              f"{type(e).__name__}: {e}; using original")
            except ImportError:
                pass

            # JSON-LD fast lane — the bookmarklet harvests JSON-LD in the
            # browser and embeds it in the markdown body under a fenced
            # ```json``` block. When that block exists and parses to a
            # Recipe-typed object with the required fields, build the
            # recipe directly from it and skip the Claude call entirely.
            # Mirrors the `/extract-from-url` fast lane in
            # extract_recipe_from_url().
            if envelope.get("jsonld"):
                print(f"[EXTRACT] has_jsonld=True -> trying jsonld-direct fast lane")
                try:
                    from extract.jsonld_to_recipe import best_recipe_jsonld
                    block = best_recipe_jsonld(envelope["jsonld"])
                    recipe = jsonld_to_recipe(
                        block if block is not None else envelope["jsonld"][0],
                        source_url=effective_url,
                        title=effective_title,
                        timings=timings,
                    )
                    # Thin markup now returns None from jsonld_to_recipe itself
                    # (>=2 ingredients / >=2 steps), so this lane falls through to
                    # the LLM without needing its own copy of the rule.
                    if recipe is not None:
                        path_used = "jsonld-direct"
                except Exception as e:
                    print(f"[WARN] jsonld_to_recipe raised, will fall back to LLM: {e}")
                    recipe = None

            if recipe is None:
                path_used = "markdown-llm"
                try:
                    recipe = await asyncio.to_thread(
                        markdown_to_recipe,
                        effective_md,
                        source_name=source_name,
                        source_url=effective_url,
                        title=effective_title,
                        timings=timings,
                        prompts=prompts,
                        usage_log=usage_log,
                    )
                except Exception as e:
                    print(f"[ERROR] Extraction failed: {e}")
                    print(f"[ERROR] Traceback: {traceback.format_exc()}")
                    _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                    raise HTTPException(status_code=500, detail=f"Extraction error: {e}") from e

                if recipe is None:
                    print("[ERROR] Extraction failed - no result")
                    _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                    raise HTTPException(status_code=500, detail="Failed to extract recipe from markdown")

            # Stamp translation provenance on cache row (so refetch sees it).
            if recipe is not None and translation_meta_bm:
                _stamp_translation_provenance(recipe, translation_meta_bm)

            # Every extract carries equipment (fast lane emits none). See _ensure_equipment.
            _ensure_equipment(recipe, path_used=path_used)

            # Page screenshot — DEFERRED on this path, because this is the one a
            # human sits and waits on (bookmarklet -> form). Capture was 60-75% of
            # the wait and cost an allrecipes extract its whole response. The
            # batch/URL path keeps the synchronous capture so cached rows still
            # carry a screenshot; here the form polls /screenshot-status and the
            # save backstop catches anyone who does not.
            _attach_page_screenshot(recipe, effective_url, url_norm, timings, defer=True)

            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        _finalize_extract_recipe(recipe, url_norm=url_norm, usage_log=usage_log)
        recipe["id"] = new_recipe_id
        # Journal LLM token usage before returning.
        return _extract_response(
            recipe, new_recipe_id=new_recipe_id, timings=timings, prompts=prompts,
            usage_log=usage_log, t_start=t_start, user_id=user_id)

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error extracting from markdown: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Extraction error: {e}") from e


def _extract_via_enrichment_api(md_result, page_lang, timings, prompts,
                                usage_log, new_recipe_id, user_id):
    """Strangler reroute (DL-4/DL-6): run the EXTRACT step through the Recipe
    Enrichment API instead of the inline jsonld/markdown calls. Same selection
    (JSON-LD fast lane -> markdown LLM). Returns (recipe, path_used).

    Identity/translate/enrich-blocks/embed stay in the caller's tail for now
    (carved in later, one at a time). Merges the API's trace back into the
    caller's timings/prompts/usage_log so journaling + the trace panel are
    unchanged. Raises RuntimeError on failure to match the inline error contract.
    """
    from enrich import enrich, EnrichmentRequest
    req = EnrichmentRequest(
        markdown=md_result["markdown"],
        # Richest Recipe block, not the first — a page that publishes a component
        # sub-recipe ahead of the main one would otherwise enrich the wrong dish.
        jsonld=(best_recipe_jsonld(md_result["jsonld"]) or md_result["jsonld"][0]
                if md_result.get("jsonld") else None),
        source_url=md_result["source_url"],
        title=md_result["title"],
        page_language=page_lang,
        target_language=INSTANCE_TARGET_LANGUAGE,  # per-user override TODO
        do_identity=False,        # identity stays in the tail (DL-4)
        do_measurements=True,     # ingredient-aware metric/imperial conversion
                                  # -> recipe['_measurements'], powers the editor toggle
        enrich=frozenset(),
        profile="full",
    )
    try:
        result = enrich(req)
    except Exception as e:
        print(f"[ERROR] Enrichment-API extract failed: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        raise RuntimeError(f"Extraction error: {e}") from e
    if timings is not None:
        timings.update(result.meta.get("timings") or {})
    if prompts is not None and result.meta.get("prompts"):
        prompts.update(result.meta["prompts"])
    if usage_log is not None:
        usage_log.extend(result.meta.get("usage") or [])
    return result.recipe, (result.meta.get("extract_path") or "markdown-llm")


# Extract recipe from a web page URL (no save). Fetches the page, pulls any
# schema.org Recipe JSON-LD via to_markdown/html_to_markdown, then runs the
# single canonical markdown -> RecipeModel call. Mirrors the JSON shape of
# /extract-from-image and /extract-from-markdown.
def extract_recipe_from_url(
    url: str,
    *,
    pre_scored: dict | None = None,
    batch_overrides: dict | None = None,
    user_id: int = PLACEHOLDER_USER_ID,
    force_refresh: bool = False,
    revalidate: bool = False,
    fetch_render: bool | None = None,
) -> dict:
    """Sync orchestrator: fetch URL → markdown → JSON-LD-or-LLM → enrich
    hooks → attached scoring. Same pipeline as the /extract-from-url
    endpoint, factored out so batch jobs (`intake/process_batch.py`) and
    other in-process callers can run it without HTTP round-trips.

    Returns the same dict shape the endpoint returns (success, recipe_id,
    recipe, source, _timings, _prompt, _usage). Raises plain RuntimeError
    on hard failures — the HTTP wrapper converts to HTTPException.

    Arguments:
        url: target URL to extract.
        pre_scored: when provided, skips the live Moz API call and uses
            these values verbatim. Shape: {"pageAuthority": float,
            "domainAuthority": float, "ouScore": float, "rootDomain": str,
            "rawTitle": str}. Any missing keys fall through to the live
            scoring path. Batch flows pass this in so we don't burn Moz
            quota re-scoring URLs the upstream pipeline already scored.
        batch_overrides: dict applied AFTER all extraction/enrich, taking
            precedence over inferred values. Used by batch ingestion to
            stamp authoritative dish-level fields (name, chapter,
            provenance.ethnicity, etc.). Top-level keys overwrite top-
            level recipe keys; nested dict keys merge into the existing
            nested dict (so {"classification": {"chapter": "Breads"}}
            sets only that one chapter, leaving the rest of
            classification intact).
    """
    print(f"[EXTRACT] extract_recipe_from_url: {url!r}")
    if not url or not url.strip():
        raise RuntimeError("url is required")
    url = url.strip()

    new_recipe_id = str(uuid.uuid4())
    timings: dict = {}
    prompts: dict = {}
    usage_log: list = []
    import llm  # gateway: attribute migrated-module usage to this recipe/user
    llm.enter(recipe_id=new_recipe_id, user_id=user_id)
    t_start = time.perf_counter()

    # === Speculative cache fast-path ===========================
    # Probe the cache on the normalized INPUT url BEFORE paying for
    # HEAD + fetch + parse. For a plainly-pasted url this equals the
    # post-redirect url we'd key on below, so a COMPLETE fresh hit
    # (has screenshot + identity card) lets us skip the network
    # round-trip, HTML parse, screenshot (~3-5s) and identity (~2s)
    # calls entirely — a genuinely sub-second cache hit. A miss or an
    # incomplete/pre-caching row falls through to the full path, which
    # keys on the resolved url and re-caches to self-heal. Batch
    # (pre_scored/batch_overrides) and force_refresh always take the
    # full path so their authoritative fields / fresh extracts apply.
    if not force_refresh and not revalidate and not pre_scored and not batch_overrides:
        spec_norm = normalize_url(url)
        spec_log: list = []
        spec_recipe, _spec_fp, _spec_src, spec_status = _extract_cache_lookup(
            spec_norm, usage_log=spec_log)
        if spec_recipe is not None and _cache_row_complete(spec_recipe):
            usage_log.extend(spec_log)   # journal the hit only if we serve it
            spec_recipe["id"] = new_recipe_id
            _attach_chapter(spec_recipe, usage_log=usage_log)   # no-op if cached
            t_moz = time.perf_counter()
            _attach_moz_scoring(spec_recipe, spec_norm)         # metabase cache read
            timings["moz_ms"] = int((time.perf_counter() - t_moz) * 1000)
            _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
            timings["path"] = "cache-hit"
            timings["fast_path"] = True
            _stamp_cache_timings(timings, status=spec_status, url_normalized=spec_norm)
            timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
            return {
                "success": True,
                "recipe_id": new_recipe_id,
                "recipe": spec_recipe,
                "source": {
                    "url": (spec_recipe.get("_source") or {}).get("originalUrl") or url,
                    "title": spec_recipe.get("name") or "",
                    "has_jsonld": False,
                },
                "_timings": timings,
                "_prompt": prompts,
                "_usage": usage_log,
            }

    # Probe Content-Type so we can route PDFs through pdf_to_markdown
    # instead of html_to_markdown. (Same routing logic as the endpoint.)
    is_pdf = False
    try:
        head = _probe_url_head(url)
        ctype = (head or "").lower()
        is_pdf = "application/pdf" in ctype
        print(f"[EXTRACT] HEAD Content-Type: {ctype!r} -> {'PDF' if is_pdf else 'HTML'} path")
    except Exception as e:
        print(f"[WARN] Content-Type probe failed (assuming HTML): {e}")

    # Resolve THIS domain's fetch policy so the extract uses the same unblocker/
    # render tiers as the harvest filter — otherwise a JS-rendered / anti-bot
    # publisher (Boston Globe) extracts only the static nav shell. Flags default
    # off, so normal domains are byte-for-byte unchanged. Best-effort, one DB read.
    _fetch_unblocker = False
    _fetch_render = False
    try:
        from urllib.parse import urlparse as _urlparse
        from input.pipeline import domains_lib
        from input.pipeline.url_utils import root_domain as _rootd
        _host = (_urlparse(url).hostname or "").lower()
        with _db() as _dc:
            _drow = domains_lib.get_domain(_dc, _host) or domains_lib.get_domain(_dc, _rootd(url) or "")
        if _drow:
            _fetch_unblocker = (_drow.get("fetch_strategy") or "") == "unblocker"
            _fetch_render = bool(_drow.get("render_required"))
    except Exception as e:
        print(f"[WARN] domain fetch-policy lookup failed (using plain): {e}")
    # Caller override (render-retry-on-thin): force a full-browser render — and the
    # unblocker it rides on — regardless of the domain's stored policy.
    if fetch_render is not None:
        _fetch_render = bool(fetch_render)
        if _fetch_render:
            _fetch_unblocker = True
    if _fetch_unblocker or _fetch_render:
        print(f"[EXTRACT] domain fetch policy: unblocker={_fetch_unblocker} render={_fetch_render}")

    try:
        if is_pdf:
            md_result = pdf_url_to_markdown(url, timings, usage_log)
        else:
            md_result = html_to_markdown(url, timings, unblocker=_fetch_unblocker, render=_fetch_render)
    except Exception as e:
        print(f"[ERROR] Fetch/convert failed for {url!r}: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise RuntimeError(f"Failed to fetch/convert URL: {e}") from e

    # Source fingerprint for revalidating reuse: the RAW page recipe-signal from the
    # page's JSON-LD, BEFORE any translation/cleaning — same date-stable basis as
    # compute_recipe_fingerprint (name+ingredients+steps; dates/ratings excluded), so
    # it compares source-to-source and never churns on a date bump. '' when the page
    # has no Recipe JSON-LD (then revalidate can't run cheaply → fall back to the TTL).
    # Parse the page's Recipe JSON-LD ONCE here, reused for BOTH (a) the raw-source
    # fingerprint (revalidating reuse) and (b) the jsonld-direct extraction below — no
    # second parse. None when the page has no usable Recipe JSON-LD (then revalidate
    # falls back to the TTL and extraction falls back to the markdown LLM).
    _src_rec = None
    current_source_fp = ""
    try:
        _jl = (md_result or {}).get("jsonld") or []
        if _jl:
            _src_rec = jsonld_to_recipe(_jl[0], source_url=md_result.get("source_url", ""),
                                        title=md_result.get("title", ""), timings=timings)
            if _src_rec:
                current_source_fp = compute_recipe_fingerprint(_src_rec)
    except Exception as e:
        print(f"[EXTRACT] JSON-LD parse failed (continuing → markdown LLM): {e}")
        _src_rec = None

    print(f"[EXTRACT] has_jsonld={md_result['has_jsonld']} "
          f"markdown_len={len(md_result['markdown'])} "
          f"source_url={md_result['source_url']!r} "
          f"language={md_result.get('language', 'en')!r}")

    # === Extraction-stage translation ===========================
    # Non-English pages get translated to English BEFORE the LLM
    # extract step, so the persisted recipe is canonical English.
    # The JSON-LD section is stripped during translation so the
    # extraction LLM falls back to deriving fields from the (now
    # English) prose rather than trusting original-language JSON-LD.
    # Provenance fields below carry the original-language signal so
    # the UI can render a "Translated from X" pill + view-original link.
    translation_meta: dict | None = None
    page_lang = md_result.get("language") or "en"
    try:
        from intake.translate import (
            is_non_english, language_name, translate_extraction_markdown,
            detect_language,
        )
        # `md_result['language']` is detected on the narrowed main-content node
        # and trusts <html lang> outright, so a thin sample or a bilingual
        # template (English <html lang>, foreign content) can mislabel a
        # foreign page 'en'. That silently skips translation -> the recipe is
        # left in the source language AND is_recipe scores untranslated text
        # (the jsonld-direct fast lane has no translation of its own, so a miss
        # here is exactly how a Greek recipe + recipeScore=0 lands). When the
        # cheap signal says English, re-detect on the FULL assembled markdown
        # (JSON-LD + body) the way the staged/bookmarklet path already does; a
        # non-English result wins, because a miss biases to the unsafe
        # "no translation" direction.
        if not is_non_english(page_lang):
            page_lang = detect_language(
                "", headers=None, visible_text=md_result.get("markdown", "")
            ) or page_lang
        # When routing through the enrichment API, DON'T translate here — pass
        # the raw markdown + JSON-LD + page_language to enrich(), which now
        # translates the JSON-LD (structured) and runs the fast lane instead of
        # discarding it. Language detection above still runs so page_lang is
        # accurate for enrich(). (Flag OFF: legacy translate-then-drop-jsonld.)
        if is_non_english(page_lang) and not _USE_ENRICHMENT_API:
            t_xlate0 = time.perf_counter()
            try:
                xr = translate_extraction_markdown(md_result["markdown"], page_lang)
                xlate_ms = int((time.perf_counter() - t_xlate0) * 1000)
                if xr.plausibility_ok:
                    # Replace the markdown the LLM sees with translated
                    # English; also force the LLM path (skip JSON-LD
                    # fast lane) since the JSON-LD blob still holds
                    # original-language strings.
                    md_result["markdown"] = xr.translated_markdown
                    md_result["has_jsonld"] = False
                    md_result["jsonld"] = []
                    # ...AND drop the pre-translation jsonld-direct recipe. It was
                    # built above from the ORIGINAL-language JSON-LD, and the lane
                    # selection reads `_src_rec` directly, NOT md_result — so
                    # emptying md_result['jsonld'] alone does not stop it. Measured
                    # 2026-08-14 on the first m.xiachufang.com harvest: 3 of 8 rows
                    # shipped wholly in Chinese (name + ingredients + steps, 71-80%
                    # CJK) and they were EXACTLY the 3 whose JSON-LD was rich enough
                    # for the fast lane to accept. The better-structured the page,
                    # the worse the result — and we paid 27-53s per page to translate
                    # markdown that was then discarded.
                    #
                    # current_source_fp is deliberately NOT cleared: it is the RAW
                    # source fingerprint for cache revalidation and must stay
                    # source-to-source, pre-translation, or every row churns.
                    _src_rec = None
                    translation_meta = {
                        "originalLanguage": xr.source_language,
                        "originalLanguageName": xr.source_language_name,
                        "translated": True,
                        "translatedAt": datetime.now(timezone.utc).isoformat(),
                        "originalTitle": xr.original_title or md_result.get("title") or "",
                    }
                    timings["translate_ms"] = xlate_ms
                    print(f"[XLATE] translated from {xr.source_language_name} "
                          f"({xlate_ms}ms) - skip jsonld fast lane")
                else:
                    print(f"[XLATE] suspect ({xr.plausibility_reason}); "
                          f"using original markdown")
            except Exception as e:
                print(f"[XLATE] failed: {type(e).__name__}: {e}; "
                      f"using original markdown")
    except ImportError:
        pass

    url_norm = normalize_url(md_result["source_url"]) if md_result["source_url"] else ""
    recipe, prior_fp, cached_source_fp, cache_status = _extract_cache_lookup(url_norm, usage_log=usage_log)
    drift = False
    path_used = ""
    if force_refresh and recipe is not None:
        # Caller (the proactive daily-refresh job) wants a fresh extract
        # even though the cache row hasn't expired yet. Keep prior_fp so
        # the write step below can still detect drift; just drop the
        # cached recipe so the LLM branch runs.
        print(f"[CACHE] force_refresh: discarding fresh cache hit, "
              f"prior_fp={prior_fp[:12]!r}")
        recipe = None
        cache_status = "stale"
    elif revalidate and recipe is not None:
        # Reuse the cached FINISHED recipe ONLY if the source is unchanged. Compare
        # this fetch's raw-source fingerprint to the one stored at cache time. A
        # mismatch = the recipe changed at the source → drop the hit so the LLM
        # re-extracts. A match (or no fingerprint on either side — e.g. no JSON-LD)
        # → keep the within-TTL hit and skip the LLM. (The harvest passes this; it
        # already fetched the page for the filter, so the compare is ~free.)
        if current_source_fp and cached_source_fp and current_source_fp != cached_source_fp:
            print(f"[REVALIDATE] source CHANGED {url_norm} — re-extracting "
                  f"(cached={cached_source_fp[:8]} != current={current_source_fp[:8]})")
            recipe = None
            cache_status = "stale"
            # The lookup logged a cache-hit; it's not one (we're re-extracting) — drop it.
            if usage_log and usage_log[-1].get("operation") == "cache_hit_markdown_to_recipe":
                usage_log.pop()
        else:
            print(f"[REVALIDATE] source unchanged {url_norm} — reuse cached recipe (no LLM)")
            # Activate change-detection for rows cached before source_fp existed: stamp it
            # now (no TTL reset, no recipe rewrite) so the NEXT harvest can detect a change.
            if current_source_fp and not cached_source_fp:
                backfill_source_fingerprint(DB_PATH, url_norm, current_source_fp)

    if recipe is not None:
        path_used = "cache-hit"
        # A row written before screenshot/identity were cached reads as
        # incomplete — fall through the enrichment below and RE-WRITE it
        # so it self-heals (future hits then take the fast path up top).
        was_incomplete = not _cache_row_complete(recipe)
    else:
        was_incomplete = False
        if _USE_ENRICHMENT_API:
            # Strangler reroute: the extract step goes through the Enrichment
            # API. enrich() raises on no-recipe, so `recipe` is non-None here and
            # the markdown fallback below is skipped.
            recipe, path_used = _extract_via_enrichment_api(
                md_result, page_lang, timings, prompts, usage_log,
                new_recipe_id, user_id,
            )
        elif _src_rec is not None:
            # Reuse the single JSON-LD parse from above (no second parse). A parse
            # failure left _src_rec None → falls through to the markdown LLM below.
            recipe = _src_rec
            path_used = "jsonld-direct"

        if recipe is None:
            try:
                recipe = markdown_to_recipe(
                    md_result["markdown"],
                    source_name="",
                    source_url=md_result["source_url"],
                    title=md_result["title"],
                    timings=timings,
                    prompts=prompts,
                    usage_log=usage_log,
                )
                path_used = "markdown-llm"
            except Exception as e:
                print(f"[ERROR] Extraction failed: {e}")
                print(f"[ERROR] Traceback: {traceback.format_exc()}")
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise RuntimeError(f"Extraction error: {e}") from e

        # Stamp translation provenance BEFORE cache write so the cached
        # recipe carries the same provenance fields a fresh extract
        # would. _SOURCE_STATIC_SUBKEYS in recipe_model.py whitelists
        # these four keys for claim/cache survival.
        if recipe is not None and translation_meta:
            _stamp_translation_provenance(recipe, translation_meta)

    if recipe is None:
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        raise RuntimeError("Failed to extract recipe from URL")

    # Every extract carries equipment — the JSON-LD fast lane emits none and the
    # enrich() do_equipment step is off on the legacy path. Runs in the enrichment
    # tail (before cache write) so a derived list is cached + self-heals old rows.
    # `_eq_healed` = a cache hit that was equipment-less and just gained tools; it
    # forces a cache re-write below (a legacy row is "complete" on screenshot+identity
    # so `was_incomplete` alone wouldn't heal it — the equipment-in-cache miss).
    _eq_missing_before = not (recipe.get("equipment") or [])
    _ensure_equipment(recipe, path_used=path_used)
    _eq_healed = _eq_missing_before and bool(recipe.get("equipment"))

    # === Enrichment tail — runs BEFORE the cache write so its expensive,
    # URL-static outputs (chapter, cooped preview image, identity card,
    # page screenshot) land in the cached recipe_json and are served free
    # on every future hit. Each step is idempotent / guarded so a complete
    # cache hit re-stamps nothing. (Previously this ran AFTER the write,
    # so a cache hit re-paid the ~2s identity + ~3-5s screenshot calls.) ===
    _attach_chapter(recipe, usage_log=usage_log)

    # og: meta block — preview image/description/site name from the page's
    # <meta> tags. The text fields are free to re-stamp; cooping the image
    # refetches + Pillow-processes, so skip it when previewImage is already
    # set (a cache hit carries it).
    og_meta = md_result.get("og_meta") or {}
    if og_meta:
        src = recipe.get("_source") or {}
        for src_key, meta_key in (
            ("previewDescription", "description"),
            ("previewImageAlt",    "imageAlt"),
            ("siteName",           "siteName"),
            ("author",             "author"),
            ("publishedTime",      "publishedTime"),
            ("modifiedTime",       "modifiedTime"),
        ):
            val = (og_meta.get(meta_key) or "").strip()
            if val:
                src[src_key] = val
        og_image_url = (og_meta.get("image") or "").strip()
        # COOPT ANY REMOTE HERO, not just a missing one. The old gate ("previewImage
        # is empty") treated "already set" as "already ours" — but several paths
        # populate previewImage with the publisher's REMOTE url, and those rows then
        # skipped the coopt and hotlinked forever. That is fine until the CDN refuses
        # to be hotlinked: chuimg (xiachufang) serves the image to us server-side but
        # returns 403 to a browser sending OUR referer, so the recipe form showed no
        # hero at all while a server-side fetch of the same url said 200.
        #
        # Non-English rows hit this every time — they always take the markdown-LLM
        # path (the JSON-LD fast lane is skipped for translation), which is one of
        # the paths that pre-fills previewImage.
        _prev = (src.get("previewImage") or "").strip()
        _ours = ("/generated/" in _prev) or _prev.startswith("/screenshot/")
        _coopt_target = _prev if (_prev and not _ours) else og_image_url
        if _coopt_target and not _ours:
            try:
                from input.pipeline.image_pipeline import coopt_image
                t_coopt = time.perf_counter()
                cooped = coopt_image(_coopt_target)
                timings["image_coopt_ms"] = int((time.perf_counter() - t_coopt) * 1000)
                if cooped:
                    src["previewImage"] = cooped
                    print(f"[OG-IMAGE] cooped {_coopt_target[:80]!r} -> {cooped}")
            except Exception as e:
                print(f"[OG-IMAGE] coopt failed (continuing): {e}")
        recipe["_source"] = src

    # Friendly publisher name on the EXTRACT response so the recipe form's
    # "Site name" field is populated before the user saves. Prefer the page's
    # captured og:site_name (stamped just above); fall back to the domain
    # master for known publishers. Runs regardless of og-meta presence so a
    # cache-served or markdown path still fills the field. Save re-resolves
    # at the chokepoint, so this is just for in-form display.
    try:
        _src = recipe.get("_source") or {}
        _resolved = friendly_site_name(_src.get("siteName"), _src.get("originalUrl") or url_norm)
        if _resolved and _resolved != (_src.get("siteName") or ""):
            _src["siteName"] = _resolved
            recipe["_source"] = _src
    except Exception as e:
        print(f"[SITENAME] extract resolve failed (continuing): {e}")

    # Moz scoring. Batch (pre_scored) trusts upstream numbers and skips the
    # Moz call to save quota; its authoritative override is applied AFTER
    # the cache write (below) so batch scores never pollute the shared row.
    # The interactive path's scores ARE cached (cheap metabase read).
    if not pre_scored:
        t_moz = time.perf_counter()
        _attach_moz_scoring(recipe, url_norm)
        timings["moz_ms"] = int((time.perf_counter() - t_moz) * 1000)

    # Identity card (~2s Haiku) — idempotent: no-op if _identity present.
    t_idy = time.perf_counter()
    _attach_identity_card(recipe, usage_log=usage_log)
    timings["identity_ms"] = int((time.perf_counter() - t_idy) * 1000)

    # Page screenshot — shared with the markdown/bookmarklet path.
    _attach_page_screenshot(recipe, url, url_norm, timings)

    # Cache write AFTER enrichment so screenshot/identity/preview travel with
    # the row. Write on a fresh extract, to self-heal a hit row that predated
    # screenshot/identity caching (was_incomplete), OR a hit that just gained
    # equipment (_eq_healed — equipment isn't in _cache_row_complete). Stamp the
    # raw-source fingerprint so a future revalidating harvest can detect a source
    # change without an LLM call.
    if path_used != "cache-hit" or was_incomplete or _eq_healed:
        cache_status, drift = _extract_cache_write(
            url_norm, recipe, prior_fingerprint=prior_fp, source_fingerprint=current_source_fp)

    timings["path"] = path_used
    _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

    # Batch pre_scored override — authoritative upstream numbers win, applied
    # AFTER the cache write so they don't pollute the shared cache row.
    if pre_scored:
        scoring = recipe.get("_scoring") or {}
        # mozHttpCode travels with the pageAuthority it describes — an upstream
        # PA arriving without its provenance would read as verified-measured.
        # 0 is a legal value here and passes the guard below (0 != "").
        for k in ("pageAuthority", "domainAuthority", "ouScore", "rootDomain",
                  "rawTitle", "mozHttpCode"):
            v = pre_scored.get(k)
            if v is not None and v != "":
                scoring[k] = v
        recipe["_scoring"] = scoring

    recipe["id"] = new_recipe_id
    _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
    _maybe_stamp_source_drift(timings, user_id=user_id)

    # Batch overrides: authoritative fields the upstream batch declared.
    # Apply LAST so they win over anything extract/enrich derived. Shallow-
    # merge nested dicts (don't replace classification wholesale — overlay
    # only the keys the batch supplied). Applied after the cache write so
    # batch-specific fields stay out of the shared cache row.
    if batch_overrides:
        for k, v in batch_overrides.items():
            if isinstance(v, dict) and isinstance(recipe.get(k), dict):
                recipe[k].update(v)
            else:
                recipe[k] = v

    timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
    return {
        "success": True,
        "recipe_id": new_recipe_id,
        "recipe": recipe,
        "source": {
            "url": md_result["source_url"],
            "title": md_result["title"],
            "has_jsonld": md_result["has_jsonld"],
        },
        "_timings": timings,
        "_prompt": prompts,
        "_usage": usage_log,
    }


@app.post("/extract-from-url")
async def extract_from_url_endpoint(
    request: Request,
    url: str = Form(...),
    user_id: int = Form(PLACEHOLDER_USER_ID),
):
    # PAID WORK — an authenticated caller only. These endpoints each spend
    # real LLM money, and until 2026-07-30 every one of them was reachable
    # anonymously AND allowlisted on the public host, so a stranger could
    # burn our model spend by POSTing in a loop. The recipe form's
    # client-side sign-in prompt never protected this: a client check
    # cannot gate a paid endpoint, it only decides what our own UI does.
    # `own_recipes` is held by every role including member, so this costs
    # no real user anything; anonymous gets a 401 it can act on.
    _require_perm(request, "own_recipes")
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail="url is required")
    url = url.strip()
    if _is_bcc_self_url(url):
        # Extracting one of our own permalinks would fetch our /r/<id>
        # redirect, follow to form HTML, and produce a garbage extraction.
        # Point the caller at the right route instead — GET /recipes/<id>
        # already loads the saved recipe directly.
        raise HTTPException(
            status_code=400,
            detail=(
                "This URL is a BCC permalink, not an external recipe page. "
                "Open it via /r/<recipe_id> (which lands on the form) "
                "or fetch /recipes/<recipe_id> for the JSON shape."
            ),
        )
    try:
        return await asyncio.to_thread(
            extract_recipe_from_url, url, user_id=user_id,
        )
    except RuntimeError as e:
        # Differentiate fetch/convert failures (network) from extract failures
        # (LLM/parse) so the form can show the right error type. Fetch/convert
        # errors are prefixed in the message; everything else is a 500.
        msg = str(e)
        if msg.startswith("Failed to fetch/convert URL"):
            raise HTTPException(status_code=502, detail=msg) from e
        raise HTTPException(status_code=500, detail=msg) from e


@app.get("/screenshot-status")
def screenshot_status_endpoint(url: str = ""):
    """Has the deferred capture for this source URL landed yet?

    {"ready": bool, "url": "/screenshot/<id>"|None}. The form polls this after an
    extract instead of holding the request open for up to 25 seconds of headless
    Chromium. Cheap: a deterministic id plus one media.db lookup, no capture is
    ever triggered from here — a poll that could start work would let a reload
    loop spawn browsers.
    """
    try:
        norm = normalize_url((url or "").strip()) or ""
        if not norm:
            return {"ready": False, "url": None}
        shot = _stored_screenshot_url(norm)
        return {"ready": bool(shot), "url": shot}
    except Exception as e:
        print(f"[ERROR] screenshot_status failed: {e}")
        return {"ready": False, "url": None}


@app.get("/screenshot/{screenshot_id}")
async def screenshot_blob_endpoint(screenshot_id: str):
    """Serve a page screenshot stored as a JPEG BLOB in media.db. The
    recipe's _source.pageScreenshot holds /screenshot/<id>; this reads the
    BLOB and returns it as image/jpeg. 404 when the id isn't present (e.g.
    media.db wiped — a re-extract regenerates it)."""
    from input.pipeline.screenshot_pipeline import read_screenshot_blob
    data = read_screenshot_blob(MEDIA_DB_PATH, screenshot_id)
    if not data:
        raise HTTPException(status_code=404, detail="screenshot not found")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# Stage markdown from a bookmarklet so the form can pick it up on load.
# Enrich a recipe with provenance + classification (cultural/historical
# context, confidence, hierarchy path, story). Split out of the main
# extract LLM call so it's opt-in — the user clicks Enrich when they've
# decided the recipe is worth keeping. Returns the same recipe shape
# with provenance and classification fields populated.
@app.post("/enrich-recipe")
async def enrich_recipe_endpoint(request: Request):
    # PAID WORK — an authenticated caller only. These endpoints each spend
    # real LLM money, and until 2026-07-30 every one of them was reachable
    # anonymously AND allowlisted on the public host, so a stranger could
    # burn our model spend by POSTing in a loop. The recipe form's
    # client-side sign-in prompt never protected this: a client check
    # cannot gate a paid endpoint, it only decides what our own UI does.
    # `own_recipes` is held by every role including member, so this costs
    # no real user anything; anonymous gets a 401 it can act on.
    _require_perm(request, "own_recipes")
    print("[ENRICH] Enrich-recipe endpoint called")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}") from e
    recipe = payload.get("recipe")
    if not isinstance(recipe, dict) or not recipe:
        raise HTTPException(status_code=400, detail="recipe object required in body")

    timings: dict = {}
    prompts: dict = {}
    usage_log: list = []
    t_start = time.perf_counter()
    recipe_id = recipe.get("id") or recipe.get("recipe_id") or payload.get("recipe_id")
    # user_id can come from either the wrapping payload or the embedded recipe
    # (the form sends it as a sibling to `recipe` today). Default to placeholder.
    user_id = payload.get("user_id")
    if user_id is None:
        user_id = recipe.get("user_id")
    if user_id is None:
        user_id = PLACEHOLDER_USER_ID
    # NOTE: no llm.enter() here — the enrich path journals via usage_log + _journal_usage
    # (legacy enrich_recipe fans blocks across threads that don't inherit the gateway
    # contextvar; the enrichment-API path has its own usage handling). See llm-gateway.md.

    # `blocks`: optional list of enrichment block names to run individually
    # (e.g. ["provenance"], ["editorial","classification"]). Omitted/None -> all
    # blocks (legacy behavior). Only honored on the API path.
    blocks = payload.get("blocks")
    try:
        if _USE_ENRICHMENT_API:
            # Strangler reroute (DL-11): run the SELECTED enrichment blocks via
            # the Recipe Enrichment API instead of the all-or-nothing legacy call.
            from enrich import run_enrichment_blocks
            e_meta = await asyncio.to_thread(
                run_enrichment_blocks, recipe, blocks,
            )
            timings.update(e_meta.get("timings") or {})
            usage_log.extend(e_meta.get("usage") or [])
            if e_meta.get("prompts"):
                prompts["enrich"] = e_meta["prompts"]
            enriched = recipe
        else:
            enriched = await asyncio.to_thread(
                enrich_recipe, recipe,
                timings=timings, prompts=prompts, usage_log=usage_log,
            )
    except Exception as e:
        print(f"[ERROR] enrich_recipe failed: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        _journal_usage(usage_log, recipe_id=recipe_id, user_id=user_id)
        raise HTTPException(status_code=500, detail=f"Enrichment error: {e}") from e

    timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
    timings["path"] = "enrich-api" if _USE_ENRICHMENT_API else "enrich-only"
    _journal_usage(usage_log, recipe_id=recipe_id, user_id=user_id)

    print("[OK] Enrichment successful")
    return {
        "success": True,
        "recipe": enriched,
        "_timings": timings,
        "_prompt": prompts,
        "_usage": usage_log,
    }


@app.post("/recipes/measure")
async def measure_recipe_endpoint(request: Request):
    """Ingredient-context-aware measurement conversion for the CURRENT ingredient
    list — backs the editor's "Refresh measurements" button. Stateless: takes
    the ingredients (+ optional prior _measurements to carry exotic resolutions
    forward), runs the deterministic engine with the LLM fallback for misses
    (this is the deliberate, user-clicked moment), and returns _measurements.
    Does NOT save — the form merges the result and the next save persists it."""
    print("[MEASURE] Refresh-measurements endpoint called")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}") from e
    ingredients = payload.get("recipeIngredient") or []
    if not isinstance(ingredients, list) or not ingredients:
        raise HTTPException(status_code=400, detail="recipeIngredient list required")

    recipe_id = payload.get("recipe_id")
    user_id = payload.get("user_id")
    if user_id is None:
        user_id = PLACEHOLDER_USER_ID

    work = {"recipeIngredient": ingredients}
    try:
        from enrich.measurement import convert_recipe_measurements
        meta = await asyncio.to_thread(
            convert_recipe_measurements, work,
            use_llm_fallback=True, prior=payload.get("_measurements"),
        )
    except Exception as e:
        print(f"[ERROR] measure failed: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Measurement error: {e}") from e

    _journal_usage(meta.get("usage") or [], recipe_id=recipe_id, user_id=user_id)
    return {
        "success": True,
        "_measurements": work.get("_measurements") or [],
        "counts": meta.get("counts") or {},
    }


@app.post("/stage-markdown")
async def stage_markdown_endpoint(request: Request):
    print("[STAGE] Stage markdown endpoint called")
    payload = await request.json()
    md_text = (payload.get("markdown") or "").strip()
    if not md_text:
        raise HTTPException(status_code=400, detail="markdown is required")

    now = time.time()
    for k in [k for k, v in _staged_markdown.items() if v.get("expires_at", 0) < now]:
        _staged_markdown.pop(k, None)

    token = uuid.uuid4().hex
    # Bookmarklet harvests #_bcc_dish/#_bcc_run from the page URL when
    # the user opened the source from a dish reject row. We pass these
    # through to the form, which uses them to force user_id=0 and stamp
    # _master on save (kind="harvest"). Validate shape but don't enforce
    # values — the save-side gate still requires edit_master perm.
    raw_hints = payload.get("bcc_hints")
    bcc_hints: Optional[dict] = None
    if isinstance(raw_hints, dict):
        cleaned_hints = {}
        for k in ("dish", "run"):
            v = raw_hints.get(k)
            if isinstance(v, str) and v.strip():
                cleaned_hints[k] = v.strip()
        if cleaned_hints.get("dish"):
            bcc_hints = cleaned_hints
    _staged_markdown[token] = {
        "markdown": md_text,
        "source_url": payload.get("source_url", ""),
        "title": payload.get("title", ""),
        # The bookmarklet uploads the page's hero image bytes to /images
        # from inside the user's authenticated session (paywall-aware),
        # gets back a /generated/<file>.jpg URL, and stashes it here.
        # The form picks it up and uses it as recipe.image[0], replacing
        # whatever external URL the JSON-LD shipped — coopting the
        # source image so we're independent of the source site.
        "local_hero_image_url": (payload.get("local_hero_image_url") or "").strip() or None,
        "bcc_hints": bcc_hints,
        "expires_at": now + _STAGE_TTL_SECONDS,
    }
    print(f"[OK] Staged markdown under token {token[:8]} ({len(md_text)} chars, "
          f"local_hero={'yes' if _staged_markdown[token]['local_hero_image_url'] else 'no'}, "
          f"bcc_hints={bcc_hints or 'none'})")
    return {"token": token}


@app.get("/staged-latest")
async def staged_latest_endpoint(request: Request, url: str = ""):
    """Newest un-expired staged token for `url` — how the form finds a grab when
    the token never reached it.

    The bookmarklet opens a popup and navigates it to the form, then delivers the
    token by appending `#staged=…` to the SAME url, which fires `hashchange`
    without reloading. That is a race, and it loses whenever staging finishes
    before the popup has COMMITTED its first navigation: the browser coalesces
    the two same-document navigations, commits the fragment-less one, and the
    token is gone. The form then waits out its full two minutes on a message that
    will never arrive. Reproduced on christinascucina.com 2026-07-31 — the popup
    showed `hash:""` and `history.length:1`, proving the second navigation never
    applied — while healthline.com had succeeded minutes earlier on identical
    code, which is the signature of a race rather than a broken path.

    postMessage cannot rescue it: `window.open('', '_blank')` then navigating
    CROSS-ORIGIN to us severs `window.opener` (verified false in the popup), so
    the two windows share no channel at all. The form has to be able to ask.

    Keyed on the source url the form was opened with, so it retrieves the grab it
    is actually waiting for. This returns only the TOKEN; the content still comes
    from /staged-markdown/{token}. Staged entries are transient, anonymous by
    design and worthless without a session to save them into, so this exposes
    nothing /stage-markdown does not already accept from anyone."""
    want = (url or "").strip()
    if not want:
        raise HTTPException(status_code=400, detail="url required")
    now = time.time()
    best_token, best_exp = None, -1.0
    for tok, entry in _staged_markdown.items():
        if entry.get("expires_at", 0) < now:
            continue
        if (entry.get("source_url") or "").strip() != want:
            continue
        # Newest wins: re-tapping the bookmarklet should supersede the earlier grab.
        if entry["expires_at"] > best_exp:
            best_token, best_exp = tok, entry["expires_at"]
    if not best_token:
        return {"token": None}
    print(f"[STAGE] staged-latest matched {best_token[:8]} for {want[:80]}")
    return {"token": best_token}


@app.get("/staged-markdown/{token}")
async def get_staged_markdown(token: str, request: Request):
    print(f"[STAGE] Retrieving staged markdown for token {token[:8]}")
    entry = _staged_markdown.get(token)
    if not entry or entry.get("expires_at", 0) < time.time():
        raise HTTPException(status_code=404, detail="Token not found or expired")
    return {
        "markdown": entry["markdown"],
        "source_url": entry.get("source_url", ""),
        "title": entry.get("title", ""),
        "local_hero_image_url": entry.get("local_hero_image_url"),
        "bcc_hints": entry.get("bcc_hints"),
    }


# Bookmarklet uploads the screenshot here after html2canvas finishes.
@app.post("/stage-image/{token}")
async def stage_image_endpoint(token: str, request: Request):
    print(f"[STAGE] Stage image for token {token[:8]}")
    entry = _staged_markdown.get(token)
    if not entry or entry.get("expires_at", 0) < time.time():
        raise HTTPException(status_code=404, detail="Token not found or expired")

    payload = await request.json()
    image_b64 = payload.get("image_b64", "")
    if not image_b64:
        raise HTTPException(status_code=400, detail="image_b64 is required")
    entry["image_b64"] = image_b64
    # Bump TTL so the form has time to fetch even if the screenshot took a while.
    entry["expires_at"] = time.time() + _STAGE_TTL_SECONDS
    print(f"[OK] Stored image for token {token[:8]} ({len(image_b64)} chars b64)")

    # ALSO adopt it as the page screenshot for this URL.
    #
    # This image was rendered by html2canvas in the user's OWN browser, inside
    # their own session. The server's headless capture cannot be: it fetches the
    # page anonymously, so on a subscription site it photographs the paywall
    # instead of the recipe. Reported 2026-08-12 on an ATK recipe the curator was
    # logged in to — the saved screenshot showed the paywall overlay, not the page
    # they were looking at.
    #
    # This is the same principle the HERO image already follows a few lines up in
    # stage_markdown_endpoint ("uploads the page's hero image bytes ... from
    # inside the user's authenticated session (paywall-aware)"); the page
    # screenshot simply never adopted it. Doing so also means the bookmarklet path
    # normally needs NO headless capture at all — _attach_page_screenshot's
    # existing "already stored?" short-circuit finds this blob and skips Chromium,
    # which is 5-31s of work that was buying a worse picture.
    try:
        src_url = (entry.get("source_url") or "").strip()
        if src_url:
            import base64 as _b64
            raw_b64 = image_b64.split(",", 1)[1] if image_b64.startswith("data:") else image_b64
            raw = _b64.b64decode(raw_b64)
            from input.pipeline.screenshot_pipeline import (
                _to_blob_jpeg, store_screenshot_blob, crop_above_fold)
            # html2canvas hands back the WHOLE recipe element — often thousands of
            # pixels tall — so crop to the same above-the-fold window a headless
            # capture would have produced before the shared 800px/q65 encode.
            # Same tile, same framing, whichever path took the picture.
            blob = _to_blob_jpeg(crop_above_fold(raw))
            if blob:
                norm = normalize_url(src_url) or src_url
                shot = store_screenshot_blob(MEDIA_DB_PATH, norm, blob)
                if shot:
                    entry["page_screenshot"] = shot
                    print(f"[SCREENSHOT] adopted the browser-rendered capture "
                          f"({len(blob):,} bytes) as the page screenshot for {norm}")
    except Exception as e:
        # Never fail the upload over this — the vision-extraction fallback still
        # has its image, and the headless capture remains as the backstop.
        print(f"[SCREENSHOT] could not adopt the staged image: {type(e).__name__}: {e}")
    return {"ok": True}


@app.get("/staged-image/{token}")
async def get_staged_image(token: str):
    entry = _staged_markdown.get(token)
    if not entry or entry.get("expires_at", 0) < time.time():
        # 404 means "this screenshot will never arrive" — bookmarklet never
        # staged anything OR the entry expired. Form callers fail fast on 404.
        raise HTTPException(status_code=404, detail="Token not found or expired")
    img = entry.get("image_b64")
    if not img:
        # 425 means "html2canvas is still running on the source page; keep
        # polling." Distinguishing this from 404 lets the form give up
        # immediately when the bookmarklet never ran, instead of waiting
        # out the full poll timeout.
        raise HTTPException(status_code=425, detail="Image not yet available")
    return {"image_b64": img}


print("[DONE] API setup complete!")