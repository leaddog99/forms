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
from fastapi.responses import JSONResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from typing import Optional
from pydantic import ValidationError
import sqlite3
import uuid
import asyncio
import json
import time
from datetime import datetime, timezone
import os
import traceback
from pathlib import Path

# Shadow the builtin print so every existing `print(...)` call in this
# module emits a leading timestamp. Cheaper than converting 100+ call
# sites to the logging module; uvicorn's own INFO/access lines are
# timestamped separately via log_config.json.
import builtins as _builtins
_real_print = _builtins.print
def print(*args, **kwargs):  # noqa: A001 — intentional shadow
    _real_print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]", *args, **kwargs)

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
    from extract.jsonld_to_recipe import jsonld_to_recipe
    from extract.enrich_recipe import enrich_recipe, SYSTEM_PROMPT as _ENRICH_PROMPT
    from extract.chapter_classifier import classify_chapter, CHAPTERS

    print("[OK] new to_markdown/extract layer imported successfully")
except Exception as e:
    print(f"[ERROR] Failed to import new to_markdown/extract layer: {e}")
    raise

try:
    from input.pipeline.url_utils import normalize_url
    from input.pipeline import (
        ensure_metabase_url_table,
        get_or_create_url_metadata,
        get_metabase_url,
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


def _db() -> sqlite3.Connection:
    """The ONE connection factory for the server (was 120+ sites each carrying
    their own timeout=). 30s busy_timeout so concurrent writers — the out-of-process
    harvest/cook jobs and the server — WAIT for the WAL lock instead of failing with
    'database is locked'. Mirrors input/pipeline/db.connect for the library side."""
    return sqlite3.connect(DB_PATH, timeout=30)


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
        now = datetime.utcnow().isoformat()
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
            scoring = recipe.get("_scoring") or {}
            if meta.get("page_authority") is not None:
                scoring["pageAuthority"] = meta["page_authority"]
            if meta.get("domain_authority") is not None:
                scoring["domainAuthority"] = meta["domain_authority"]
            if meta.get("ou_score") is not None:
                scoring["ouScore"] = meta["ou_score"]
            if meta.get("root_domain"):
                scoring["rootDomain"] = meta["root_domain"]
            if meta.get("raw_title") and not scoring.get("rawTitle"):
                scoring["rawTitle"] = meta["raw_title"]
            recipe["_scoring"] = scoring
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
        now = datetime.utcnow().isoformat()
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
                "DROP INDEX IF EXISTS idx_drdp_dish",                  # prefix of idx_drdp_dish_rank
                "DROP INDEX IF EXISTS idx_dish_editors_choice_dish",   # prefix of the (dish,url) unique
            ):
                try:
                    conn.execute(_ix_stmt)
                except Exception as _ix_e:  # noqa: BLE001
                    print(f"[SETUP] perf-index skipped ({_ix_stmt[:48]}…): {_ix_e}")

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
@app.get("/")
def health_check():
    print("[HEALTH] Health check endpoint called")
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
        raise HTTPException(status_code=503, detail="Chef is unavailable right now — try again in a moment.")
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
        from cook_stt import transcribe
        text = transcribe(data)
    except Exception as e:
        print(f"[ERROR] /cook/listen: {e}")
        raise HTTPException(status_code=503, detail="Speech recognition is unavailable.")
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
        raise HTTPException(status_code=503, detail="Voice synthesis is unavailable.")
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
        fname = f"cook_voice_{day}.log"
        with open(os.path.join("logs", fname), "a", encoding="utf-8") as f:
            f.write(f"\n==== voice log {datetime.now().isoformat()} · {name or rid} "
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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
            # user_id is returned at the top level (it's a column, not part of
            # the recipe blob) so the form's loadForm hydration can refresh
            # the admin band input to match the loaded row's actual owner —
            # prevents accidental "click master row, save to personal" forks
            # when the user has stale sidebar state.
            return {
                "id": row[0],
                "recipe_id": row[1],
                "user_id": row[2],
                "data": json.loads(row[3]),
                "source_changed_at": row[4],
                "created_at": row[5],
                "updated_at": row[6],
                "bccUrl": _bcc_permalink(row[1]),
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error in get_recipe({recipe_id}): {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


def _master_result_row(d: dict, rid: str, *, dish=None, rank_score=None, distance=None) -> dict:
    """Shared result shape for the recommender (dish-anchored + vector paths)."""
    m = d.get("_master") or {}
    exc = m.get("exceptionalism") or {}
    src = d.get("_source") or {}
    img = d.get("image")
    return {
        "recipe_id": rid,
        "name": d.get("name") or "(no title)",
        "dish": dish if dish is not None else m.get("dish"),
        "grade": exc.get("grade"),
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
        ORDER BY dp.rank_score IS NULL, dp.rank_score DESC, m.id
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
    from input.pipeline.embeddings import compose_recipe_text, embed_text, find_best_dish_match
    from input.pipeline import vector_store
    try:
        from input.pipeline import system_config as _cfg
        similar_max = float(_cfg.get_setting("similar_max_distance", 0.95))
    except Exception:
        similar_max = 0.95

    try:
        with _db() as conn:
            vector_store.enable_vec(conn)

            # --- Tier 1: dish-anchored -------------------------------------
            dish_match = None
            try:
                dish_match = find_best_dish_match(conn, recipe)
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
            results: list[dict] = []
            for r in near:
                row = conn.execute(
                    "SELECT recipe_id, data, url_normalized FROM master_recipes WHERE id = ?",
                    (r["id"],)).fetchone()
                if not row:
                    continue
                rid, dj, urln = row
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
        raise HTTPException(status_code=500, detail=f"Similar lookup failed: {e}")


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
            raise HTTPException(status_code=400, detail=str(e))
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
    now = datetime.utcnow().isoformat()
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
        raise HTTPException(status_code=500, detail=f"Claim failed: {e}")


# Promote-to-master is the inverse of /claim — clones a personal recipe
# into master_recipes (user_id=0). Mirrors claim's "copy not subscription"
# semantics so the master copy is independently editable; the original
# personal row stays in place untouched. Stamps `_source.promotedFrom`
# (rather than `claimedFrom`) so the two provenance trails stay
# distinguishable. Re-promote of the same source short-circuits to the
# existing master copy, same pattern as claim's re-claim short-circuit.
#
# Curator authorization is a TODO: today any caller can promote. When
# Ghost SSO lands, gate this on curator role. See
# memory/project_master_recipes_ui.md.
@app.post("/recipes/{recipe_id}/promote-to-master")
def promote_to_master(recipe_id: str, request: Request):
    _require_perm(request, "promote_to_master")
    source_owner = _find_recipe_owner(recipe_id)
    if source_owner is None:
        raise HTTPException(status_code=404, detail="Source recipe not found")
    if source_owner == 0:
        raise HTTPException(status_code=409, detail="Recipe is already in master")

    source_table = _recipes_table_for(source_owner)
    target_table = "master_recipes"
    new_recipe_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()

    try:
        with _db() as conn:
            row = conn.execute(
                f"SELECT data FROM {source_table} WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Source recipe vanished")

            source_data = json.loads(row[0])
            # static_subset drops user-scoped/identity fields and keeps the
            # platonic recipe content + LLM enrichment — same filter the
            # claim path uses, just in the opposite direction.
            data = static_subset(source_data)
            data["id"] = new_recipe_id
            source_block = data.get("_source") or {}
            source_block["promotedFrom"] = f"user:{source_owner}"
            source_block["promotedAt"] = now
            source_block["promotedFromRecipeId"] = recipe_id
            data["_source"] = source_block

            # Re-promote short-circuit: if this exact source has already
            # been promoted to master, return the existing master copy
            # rather than minting a parallel one. Mirrors claim's
            # re-claim short-circuit, keyed on the source recipe_id.
            existing = conn.execute(
                f"SELECT recipe_id FROM {target_table} "
                f"WHERE user_id = 0 "
                f"AND json_extract(data, '$._source.promotedFromRecipeId') = ? "
                f"LIMIT 1",
                (recipe_id,),
            ).fetchone()
            if existing:
                print(f"[PROMOTE] Re-promote short-circuit: master already "
                      f"has {existing[0]} from source {recipe_id}")
                return {
                    "recipe_id": existing[0],
                    "url": f"/r/{existing[0]}",
                    "bccUrl": _bcc_permalink(existing[0]),
                    "adopted_existing": True,
                }

            # Master copy gets its own self-URL (the promoted-from URL is
            # on the source row, not this one). url_normalized stays
            # blank — promoted rows, like claimed rows, are detached
            # from URL-based dedup. Auto-enrich is a no-op when the
            # source row already carried full enrichment (which a
            # static_subset copy preserves).
            conn.execute(
                f"INSERT INTO {target_table} "
                f"(recipe_id, user_id, data, url_normalized, source_changed_at, created_at, updated_at) "
                f"VALUES (?, 0, ?, ?, NULL, ?, ?)",
                (new_recipe_id, json.dumps(data, indent=2), "", now, now),
            )
            print(f"[PROMOTE] {source_table}/{recipe_id} -> "
                  f"{target_table}/{new_recipe_id} (master)")
            return {
                "recipe_id": new_recipe_id,
                "url": f"/r/{new_recipe_id}",
                "bccUrl": _bcc_permalink(new_recipe_id),
                "adopted_existing": False,
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] promote_to_master({recipe_id}) failed: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Promote failed: {e}")


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
        raise HTTPException(status_code=400, detail="JSON body required")
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
    try:
        # stream=True so we can size-check before fully buffering
        resp = _rq.get(source_url, timeout=30, stream=True, headers={
            "User-Agent": "BCC-image-coopt/1.0 (recipes.tbotb.com)",
        })
        resp.raise_for_status()
    except _rq.RequestException as e:
        raise HTTPException(status_code=502,
                            detail=f"Source fetch failed: {type(e).__name__}: {e}")

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
        raise HTTPException(status_code=500, detail=f"Recipe data unreadable: {e}")
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
            )
        raise HTTPException(status_code=500,
                            detail=f"Image generation failed: {type(e).__name__}: {e}")

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
            rows = conn.execute(
                "SELECT user_id, ghost_uuid, email, name, status, "
                "subscription_tier, role, created_at, updated_at "
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
                }
                for r in rows
            ]
    except Exception as e:
        print(f"[ERROR] list_users failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


# === Auth (pre-Ghost stub) ===
# Identifies the caller via the X-Self-User-Id header that
# library-shell.js auto-attaches from localStorage's app:self_user_id.
# Pre-Ghost this trusts the client header; post-Ghost the resolver
# swaps to validating a session JWT. Either way, callers get back the
# same shape: {user, permissions, is_staff}.
from input.pipeline import auth as auth_lib  # noqa: E402


def _resolve_caller(request: Request) -> Optional[dict]:
    """Return the user dict for the caller, or None if no/invalid
    self-user-id header. Helper for endpoints that need to know who's
    calling."""
    header = request.headers.get("x-self-user-id")
    with _db() as conn:
        return auth_lib.resolve_user(conn, header)


def _require_perm(request: Request, perm: str) -> dict:
    """Raise 403 unless the caller has `perm`. Returns the caller's
    user dict on success — useful for downstream logging / audit."""
    user = _resolve_caller(request)
    if not auth_lib.can(user, perm):
        role = (user or {}).get("role", "anonymous")
        raise HTTPException(
            status_code=403,
            detail=f"This action requires the '{perm}' permission "
                   f"(your role: '{role}')."
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
        }
    role = user.get("role") or "member"
    return {
        "user": user,
        "role": role,
        "permissions": auth_lib.permissions_for(role),
        "is_staff": auth_lib.is_staff(user),
    }


@app.post("/users")
async def create_user(request: Request):
    """Create a test user. Body: {name, email?, status?, subscription_tier?}.
    user_id is auto-assigned by SQLite (AUTOINCREMENT). Returns the full
    row including the assigned user_id so the picker UI can navigate the
    user straight to the form as that user. Email uniqueness is enforced
    by a partial index — duplicate email returns 409."""
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}")
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
    now = datetime.utcnow().isoformat()
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
        raise HTTPException(status_code=409, detail=f"User already exists: {e}")
    except Exception as e:
        print(f"[ERROR] create_user failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}")

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
    now = datetime.utcnow().isoformat()
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
        raise HTTPException(status_code=409, detail=f"Conflict: {e}")
    except Exception as e:
        print(f"[ERROR] update_user({user_id}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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


@app.get("/dishes")
def list_dishes_endpoint():
    try:
        with _db() as conn:
            return dishes_lib.list_dishes(conn)
    except Exception as e:
        print(f"[ERROR] list_dishes failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}")
    try:
        name, queries, top_serp, top_final, ttl, notes, auto_enrich, description = \
            dishes_lib.validate_create_payload(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
            # Auto-describe (when blank) + embed so the dish is
            # immediately participating in cohort matches. Best-effort:
            # failures don't block the create.
            try:
                from input.pipeline.embeddings import ensure_dish_embedding
                ensure_dish_embedding(conn, created)
                # Re-read so the response reflects the auto-filled
                # description + chapter.
                created = dishes_lib.get_dish(conn, name) or created
            except Exception as e:
                print(f"[WARN] post-create dish embed failed for {name!r}: {e}")
            return created
    except sqlite3.IntegrityError:
        # PRIMARY KEY COLLATE NOCASE — duplicate (case-insensitive) name
        raise HTTPException(status_code=409,
                            detail=f"Dish {name!r} already exists")
    except Exception as e:
        print(f"[ERROR] create_dish failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}")
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
                raise HTTPException(status_code=400, detail=str(e))
            if updated is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            # If the edit touched queries/description, the embedding's
            # input text may have changed. Re-embed (idempotent — the
            # staleness check inside ensure_dish_embedding compares the
            # cached embedding_text to the freshly composed one).
            try:
                from input.pipeline.embeddings import ensure_dish_embedding
                ensure_dish_embedding(conn, updated)
                updated = dishes_lib.get_dish(conn, name) or updated
            except Exception as e:
                print(f"[WARN] post-edit dish re-embed failed for {name!r}: {e}")
            return updated
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] update_dish({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}")
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
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[ERROR] update_dish_reject_status({name!r},{reject_id}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.post("/extract-product")
async def extract_product_endpoint(request: Request):
    """Mine one retailer product page's staged markdown into a Product, AND return everything
    the product form needs in one shot: the extracted `product`, the reverse-table ATK `facts`
    for its URL/ASIN, and existing-product `matches` (for the 'add as a vendor to X?' prompt)."""
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
        raise HTTPException(status_code=500, detail=f"Save error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Catalog error: {e}")


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
                ORDER BY dp.rank_score DESC, m.id
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
                    ORDER BY dp.rank_score IS NULL, dp.rank_score DESC,
                             CAST(json_extract(m.data, '$._master.rank') AS INTEGER) ASC, m.id
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
            return {
                "dish": existing["name"],
                "refreshed_at": existing.get("last_refreshed"),
                "count": len(out),
                "recipes": out,
            }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] list_dish_top_recipes({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


# Editor's Choice — curator pins (a (dish, url) membership). The dish's next
# refresh adds these URLs to its candidate pool so they're scored into the ledger
# like any SerpAPI result and surface in the top-N if they rank. This is the first
# concrete brick of the many-to-many 'collections' model (membership is a junction
# row, not a stamp on the recipe). See list_dish_top_recipes for how display reads
# the ledger, not a label.
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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.post("/dishes/{name}/editors-choice")
def add_dish_editors_choice(name: str, request: Request, payload: dict = Body(...)):
    """Pin a URL to a dish (admin only). Body: {url, note?}. Idempotent on the
    normalized URL. The pin takes effect on the dish's next refresh."""
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
        return {"ok": True, "pin": pin}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] add_dish_editors_choice({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.delete("/dishes/{name}/editors-choice")
def remove_dish_editors_choice(name: str, request: Request, url_normalized: str = ""):
    """Unpin by url_normalized (query param; admin only)."""
    _require_perm(request, "manage_dishes")
    if not url_normalized:
        raise HTTPException(status_code=400, detail="url_normalized is required")
    try:
        with _db() as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            n = dishes_lib.remove_editors_choice(conn, existing["name"], url_normalized)
        return {"ok": True, "removed": n}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] remove_dish_editors_choice({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
                           json_extract(data, '$._master.exceptionalism.grade') AS grade
                    FROM master_recipes
                    WHERE user_id = 0
                      AND json_extract(data, '$._master.dish') = :dish
                    GROUP BY url_normalized
                ) m ON m.url_normalized = p.url
                WHERE p.dish_name = :dish
                ORDER BY p.rank_score IS NULL, p.rank_score DESC, p.url
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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
            def _canon(u):
                if not u:
                    return ""
                pos = u.find("web.archive.org/web/")
                if pos != -1:
                    h = u.find("/http", pos)
                    if h != -1:
                        u = u[h + 1:]
                return normalize_url(u)
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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        print(f"[ERROR] create_chapter failed: {e}")
        raise HTTPException(status_code=500, detail=f"Create error: {e}")


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
        raise HTTPException(status_code=(404 if "not found" in msg else 409), detail=msg)
    except Exception as e:
        print(f"[ERROR] delete_chapter({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Delete error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Refresh error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Refresh error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Update error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.post("/system-config")
def update_system_config_endpoint(payload: dict = Body(...)):
    """Update setting value(s). Accepts either a single {key, value} or a bulk
    {updates: {key: value, ...}}. Unknown keys are rejected; read-only keys are
    refused. Returns the full settings list so the UI re-renders fresh."""
    # TODO: gate with _require_perm(request, "edit_master") before public exposure.
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
        raise HTTPException(status_code=500, detail=f"Update error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[ERROR] upsert_scheduled_job failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Delete error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Failed to spawn runner: {e}")
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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Import error: {e}")


@app.post("/cook-kb/{kb_id}")
def upsert_cook_kb_endpoint(kb_id: str, payload: dict = Body(...)):
    """Create or update a KB entry (curate). New entries default to draft."""
    from input.pipeline import cook_kb
    try:
        with _db() as conn:
            cook_kb.ensure_cook_kb_table(conn)
            return cook_kb.upsert_kb(conn, kb_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[ERROR] upsert_cook_kb failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}")


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
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[ERROR] set_cook_kb_status failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Delete error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.post("/domains")
def create_domain_endpoint(payload: dict = Body(...)):
    """Create a curator-defined domain row (host is the key)."""
    # TODO: gate with _require_perm(request, "edit_master") when the admin
    # surface is exposed publicly — ungated in dev to match the chapters editor.
    from input.pipeline import domains_lib
    host = (payload.get("domain") or "").strip()
    if not host:
        raise HTTPException(status_code=400, detail="domain (host) is required")
    try:
        with _db() as conn:
            return domains_lib.create_domain(conn, host, payload)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        print(f"[ERROR] create_domain failed: {e}")
        raise HTTPException(status_code=500, detail=f"Create error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Scan error: {e}")


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
    try:
        llm.enter(recipe_id=None, user_id=0)   # journal the classify call
        res = url_word_lists.sweep_master_urls(db_path=DB_PATH)
        _journal_usage(None, user_id=0)
        return res
    except Exception as e:
        print(f"[ERROR] url_words_sweep failed: {e}")
        raise HTTPException(status_code=500, detail=f"Sweep error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
            conn.execute(
                "UPDATE domains SET recipe_path = ?, keep_top_n = ?, serp_query = ?, "
                "search_pages = ?, harvest_source = ? WHERE domain = ?",
                (res.get("recipe_path") or "", keep, query or "", pages, source, host))
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
        if await _extract_publisher_url_to_master(
                url, host, extracted + 1, "/domains/refresh-top", "[PUBLISHER-REFRESH]",
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

    return {"discovered": res["discovered"], "recipe_pass": res["recipe_pass"],
            "scored": res["scored"], "stored": len(res["members"]),
            "extracted": extracted, "recipe_path": res.get("recipe_path")}


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
        raise HTTPException(status_code=500, detail=f"Failed to spawn process-selected job: {e}")
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "queued", "domain": host, "count": len(urls),
        "stream_url": f"/jobs/{job_id}/stream", "status_url": f"/jobs/{job_id}"})


# --------------------------------------------------------------------------- #
# Score-only path #2 (zero-click): the USERSCRIPT capture queue. A Tampermonkey
# userscript runs in the curator's REAL browser on each queued publisher page
# (beating the anti-bot for free), harvests the page's JSON-LD, POSTs it here to
# save to master, and self-advances with human-paced delays. The run is a tracked
# `userscript_capture` job so it shows in the Job Monitor with a live log.
# See docs/score-only-curation.md.
# --------------------------------------------------------------------------- #
_LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def _userscript_log(log_filename: str, line: str) -> None:
    """Append a timestamped line to the job's log file so it streams in the Monitor."""
    if not log_filename:
        return
    try:
        with open(os.path.join(_LOGS_DIR, log_filename), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {line}\n")
    except Exception:
        pass


def _capture_jsonld_to_master(host: str, url: str, jsonld: list, rank: int = 0) -> dict:
    """Build a recipe from BROWSER-captured JSON-LD (NO server fetch — the userscript's
    real browser already bypassed the anti-bot) and save it to master as a kind='top'
    publisher member. Free jsonld-direct lane, falls back to the markdown LLM. Returns
    {saved, name?, reason?}."""
    recipe = None
    if jsonld:
        try:
            recipe = jsonld_to_recipe(jsonld[0], source_url=url, title="")
        except Exception as e:
            print(f"[USERSCRIPT] jsonld_to_recipe raised: {type(e).__name__}: {e}")
    if recipe is None and jsonld:
        try:
            blob = f"*Source: {url}*\n\n```json\n{json.dumps(jsonld, indent=2)}\n```\n"
            recipe = markdown_to_recipe(blob, source_name=host, source_url=url, title="")
        except Exception as e:
            print(f"[USERSCRIPT] markdown fallback raised: {type(e).__name__}: {e}")
    if not recipe:
        return {"saved": False, "reason": "no recipe JSON-LD on page (stub/blocked?)"}
    ok, reason = _is_cacheable(recipe, min_ings=SAVE_GATE_MIN_INGREDIENTS,
                               min_steps=SAVE_GATE_MIN_INSTRUCTIONS)
    if not ok:
        return {"saved": False, "reason": f"thin ({reason})"}
    payload = dict(recipe)
    payload["user_id"] = 0
    payload["_master"] = {"kind": "top", "publisher": host,
                          "refreshed_at": datetime.now(timezone.utc).isoformat(),
                          "rank": rank, "batch_source": "/userscript-capture"}
    payload["_skip_auto_enrich"] = True
    try:
        _save_recipe_core(payload)
        return {"saved": True, "name": recipe.get("name") or url}
    except Exception as e:
        return {"saved": False, "reason": f"save-fail: {type(e).__name__}: {e}"}


@app.post("/domains/{domain}/userscript/start")
def userscript_start_endpoint(domain: str, payload: dict = Body(...)):
    """Begin a userscript capture run: stores the queue + opens a tracked
    `userscript_capture` job (running). Returns the first URL + the delay range the
    userscript paces with. `slow` widens the delays for touchy sites."""
    from input.pipeline import domains_lib
    host = domains_lib._canon_host(domain)
    raw = payload.get("urls") or []
    urls = [u for u in (raw if isinstance(raw, list) else [raw]) if u]
    if not urls:
        raise HTTPException(status_code=400, detail="No URLs to queue.")
    slow = bool(payload.get("slow"))
    mn, mx = (30, 60) if slow else (8, 25)
    with _db() as conn:
        entity_ref = f"userscript:{host}"
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return JSONResponse(status_code=409, content={
                "error": "already in flight", "job_id": existing["id"],
                "stream_url": f"/jobs/{existing['id']}/stream"})
        job_id = jobs_lib.enqueue_job(
            conn, type="userscript_capture",
            params={"host": host, "urls": urls, "slow": slow, "min_delay": mn,
                    "max_delay": mx, "log_label": f"{host} userscript ×{len(urls)}"},
            entity_ref=entity_ref)
        job = jobs_lib.get_job(conn, job_id)
        log_filename = jobs_lib._build_log_filename(job)
        jobs_lib.mark_running(conn, job_id, log_filename)
        conn.execute("UPDATE jobs SET result=? WHERE id=?",
                     (json.dumps({"total": len(urls), "attempted": [], "saved": []}), job_id))
        conn.commit()
    _userscript_log(log_filename, f"=== Userscript capture {host} — {len(urls)} URL(s), "
                                  f"delay {mn}-{mx}s{' [SLOW]' if slow else ''} ===")
    return {"job_id": job_id, "host": host, "next_url": urls[0], "total": len(urls),
            "min_delay": mn, "max_delay": mx, "stream_url": f"/jobs/{job_id}/stream"}


@app.post("/domains/{domain}/userscript/capture")
def userscript_capture_endpoint(domain: str, payload: dict = Body(...)):
    """Save ONE browser-captured page (jsonld) to master, log it on the job, and return
    the NEXT queued URL (or null when done). The userscript calls this per page."""
    from input.pipeline import domains_lib
    from input.pipeline.url_utils import normalize_url as _norm
    host = domains_lib._canon_host(domain)
    job_id = payload.get("job_id")
    url = (payload.get("url") or "").strip()
    jsonld = payload.get("jsonld") or []
    if not job_id or not url:
        raise HTTPException(status_code=400, detail="job_id + url required")
    with _db() as conn:
        job = jobs_lib.get_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    params = job.get("params") or {}
    result = job.get("result") or {}
    if isinstance(result, str):
        try: result = json.loads(result or "{}")
        except Exception: result = {}
    log_filename = job.get("log_filename")
    queue = params.get("urls") or []
    attempted = set(result.get("attempted") or [])
    saved = list(result.get("saved") or [])

    res = _capture_jsonld_to_master(host, url, jsonld, rank=len(saved) + 1)
    attempted.add(url)
    if res.get("saved"):
        saved.append(url)
        _userscript_log(log_filename, f"SAVED  {res.get('name')}  {url}")
        with _db() as conn:
            conn.execute("UPDATE collection_members SET selected=1 WHERE "
                         "collection_type='publisher' AND collection_key=? AND url_normalized=?",
                         (host, _norm(url) or url))
            conn.commit()
    else:
        _userscript_log(log_filename, f"SKIP ({res.get('reason')})  {url}")

    nxt = next((u for u in queue if u not in attempted), None)
    new_result = {"total": len(queue), "attempted": list(attempted), "saved": saved}
    with _db() as conn:
        if nxt is None:
            jobs_lib.mark_finished(conn, job_id, status="success", result=new_result)
            _userscript_log(log_filename, f"=== done — saved {len(saved)}/{len(queue)} ===")
        else:
            conn.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(new_result), job_id))
            conn.commit()
    return {"saved": bool(res.get("saved")), "name": res.get("name"), "reason": res.get("reason"),
            "next_url": nxt, "remaining": len(queue) - len(attempted),
            "saved_count": len(saved), "total": len(queue),
            "min_delay": params.get("min_delay", 8), "max_delay": params.get("max_delay", 25)}


@app.post("/domains/{domain}/userscript/finish")
def userscript_finish_endpoint(domain: str, payload: dict = Body(...)):
    """Finalize a userscript run early (e.g. the userscript hit a block-stub and backed
    off, or the user stopped). reason: complete | blocked | stopped."""
    job_id = payload.get("job_id")
    reason = (payload.get("reason") or "stopped").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id required")
    with _db() as conn:
        job = jobs_lib.get_job(conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        result = job.get("result") or {}
        if isinstance(result, str):
            try: result = json.loads(result or "{}")
            except Exception: result = {}
        result["finish_reason"] = reason
        status = "success" if reason == "complete" else "cancelled"
        jobs_lib.mark_finished(conn, job_id, status=status, result=result)
    _userscript_log(job.get("log_filename"), f"=== finished: {reason} ===")
    return {"ok": True, "status": status}


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
        raise HTTPException(status_code=500, detail=f"Failed to spawn refresh job: {e}")
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
        raise HTTPException(status_code=500, detail=f"Clear error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.get("/semrush-ranks")
def semrush_ranks_status_endpoint():
    """Per-region summary of the imported SEMrush Rank reference data (rows, file
    date, last import) — for the domains-page ranks tools."""
    from input.pipeline import semrush_ranks
    try:
        with _db() as conn:
            return {"regions": semrush_ranks.region_stats(conn)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Failed to spawn ranks refresh job: {e}")
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
        raise HTTPException(status_code=500, detail=f"Failed to spawn domain scoring job: {e}")
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
def enrich_domain_endpoint(domain: str):
    """Quick Haiku profile of a domain — story, language, country, cuisine
    focus, logo. Returns the SUGGESTED fields (does not save); the editor
    populates them so the curator can review + Save. Token-journaled."""
    # TODO: gate with _require_perm when exposed publicly (see POST /domains).
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
        raise HTTPException(status_code=500, detail=f"Enrich error: {e}")


@app.patch("/domains/{domain}")
def patch_domain_endpoint(domain: str, payload: dict = Body(...)):
    """Update editable fields on a domain row."""
    # TODO: gate with _require_perm when exposed publicly (see POST /domains).
    try:
        from input.pipeline import domains_lib
        with _db() as conn:
            return domains_lib.update_domain(conn, domain, payload)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        print(f"[ERROR] patch_domain({domain!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Delete error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[ERROR] admin_create({model!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"[ERROR] admin_update({model!r}, {row_id}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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

    # Editor's Choice pins for this dish — added to the candidate pool so they're
    # scored alongside the SerpAPI results and surface in the top-N if they rank.
    with _db() as conn:
        pinned_urls = dishes_lib.editors_choice_urls(conn, canonical_name)
    if pinned_urls:
        print(f"[REFRESH-DISH] {len(pinned_urls)} Editor's Choice pin(s) to include")

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
            extra_urls=pinned_urls or None,
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


def _recipe_equipment_from_cook(cook_equipment) -> list:
    """Mirror the cook-rework's inferred tools (`_cook.equipment`: id/name/size) into
    the recipe's top-level schema `equipment` (HowToTool). Makes the tools REAL recipe
    data — the recipe editor shows them AND the product-commerce match keys off them
    (equipment -> product_class; `size` is the class grain, e.g. "Saucepans (2 qt)").
    Carries `size` when present. Deduped by name (first wins), order preserved."""
    out, seen = [], set()
    for e in (cook_equipment or []):
        name = ((e.get("name") if isinstance(e, dict) else getattr(e, "name", None)) or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        item = {"@type": "HowToTool", "name": name}
        size = e.get("size") if isinstance(e, dict) else getattr(e, "size", None)
        if size:
            item["size"] = size
        out.append(item)
    return out


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
        raise HTTPException(status_code=500, detail=f"Failed to spawn runner: {e}")
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

    table = _recipes_table_for(user_id)
    with _db() as conn:
        row = conn.execute(
            f"SELECT data FROM {table} WHERE recipe_id = ?", (recipe_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Recipe not found.")
    recipe = json.loads(row[0])

    import llm  # gateway: attribute this Q&A to the recipe/user, label it notes_chat
    llm.enter(recipe_id=recipe_id, user_id=user_id)
    try:
        from cook_ask import ask as chef_ask
        answer = chef_ask(recipe, question, operation="notes_chat")
    except Exception as e:
        print(f"[ERROR] /recipes/{recipe_id}/notes-ask: {e}")
        raise HTTPException(status_code=503, detail="Chef is unavailable right now — try again in a moment.")
    finally:
        llm.flush()   # write the buffered notes_chat usage to the journal
    return {"answer": answer}


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
        raise HTTPException(status_code=500, detail=f"Failed to spawn runner: {e}")
    return {"job_id": job_id, "spawned": True, "stream_url": f"/jobs/{job_id}/stream"}


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
# nutrition/_identity/embedding-adjacent prose…). At user_id=0 that's ~702 rows ×
# ~19KB ≈ 13MB → ~700KB (audit 2026-06-15). Nested shape preserved so the
# sidebar's r.data.* pickers work unchanged. If the sidebar ever renders a new
# field, ADD it here (else the card silently loses it) — see the getter list in
# recipe_form_styled.html renderRecipes().
def _recipe_list_data(d: dict) -> dict:
    out: dict = {"name": d.get("name")}
    cls = d.get("classification") or {}
    if cls:
        out["classification"] = {"chapter": cls.get("chapter"), "story": cls.get("story")}
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
        out["_master"] = {"exceptionalism": m.get("exceptionalism")}
    if d.get("_grade") is not None:
        out["_grade"] = d.get("_grade")
    if d.get("image") is not None:
        out["image"] = d.get("image")
    ed = d.get("editorial") or {}
    if ed.get("opinion"):
        out["editorial"] = {"opinion": ed.get("opinion")}
    pv = d.get("provenance") or {}
    if pv.get("author"):
        out["provenance"] = {"author": pv.get("author")}
    return out


# List recipes for the given owner. user_id=0 returns the master collection
# (master_recipes table); any other value returns that owner's personal
# recipes. `summary=1` returns the slim list projection (the sidebar uses it —
# big payload win); `limit`/`offset` paginate (0 = all, the default). Default
# (no params) preserves the prior full-data behavior for any other consumer.
@app.get("/recipes")
def list_recipes(user_id: int = PLACEHOLDER_USER_ID, summary: int = 0,
                 limit: int = 0, offset: int = 0):
    table = _recipes_table_for(user_id)
    print(f"[LIST] List recipes user_id={user_id} table={table} summary={summary} limit={limit}")
    try:
        with _db() as conn:
            sql = (f"SELECT id, recipe_id, user_id, data, source_changed_at, created_at, updated_at "
                   f"FROM {table} WHERE user_id = ? ORDER BY updated_at DESC")
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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
            "refreshed_at": (hints.get("run") or "").strip() or datetime.utcnow().isoformat(),
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
        raise HTTPException(status_code=422, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error processing request: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=400, detail=f"Bad input: {e}")

    # Save-quality gate. Refuse rows below the minimum-ingredients /
    # minimum-instructions floor so the recipes/master_recipes tables
    # stay statistically clean. The form catches the structured 422 and
    # offers a "Save anyway" dialog that retries with force_save=true.
    # Curator-only paths (claim/promote) bypass this naturally because
    # they re-save data that already passed the gate originally.
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
    now = datetime.utcnow().isoformat()

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
    normalized_source_url = normalize_url(raw_source_url) if raw_source_url else ""
    if normalized_source_url and normalized_source_url != raw_source_url:
        source["originalUrl"] = normalized_source_url
        recipe_dict["_source"] = source

    # Publisher auto-attribution (parity with /userscript-capture +
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
                    _m.setdefault("refreshed_at", datetime.utcnow().isoformat())
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
    skip_auto_enrich = bool(payload.get("_skip_auto_enrich"))
    if user_id == 0 and not skip_auto_enrich:
        cls = recipe_dict.get("classification") or {}
        story = (cls.get("story") or "").strip()
        name = (recipe_dict.get("name") or "").strip()
        ingredients = recipe_dict.get("recipeIngredient") or []
        if not story and name and ingredients:
            try:
                print(f"[SAVE-ENRICH] master row missing story; calling enrich_recipe")
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
    elif user_id == 0 and skip_auto_enrich:
        print(f"[SAVE-ENRICH] skipped (dish auto_enrich=off)")

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
            # saved master row (parity with /userscript-capture + /process-selected
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
                        compose_recipe_text, embed_text, vec_to_bytes,
                    )
                    from input.pipeline import vector_store
                    txt = compose_recipe_text(recipe_dict)
                    if txt.strip():
                        rec_vec = embed_text(txt)
                        if user_id == 0:
                            # Master: store the source-of-truth vector + the
                            # derived KNN index the recommender reads.
                            conn.execute(
                                "UPDATE master_recipes SET embedding = ? WHERE id = ?",
                                (vec_to_bytes(rec_vec), seq_id),
                            )
                            vector_store.enable_vec(conn)
                            ch = ((recipe_dict.get("classification") or {}).get("chapter") or None)
                            dish_for_vec = (recipe_dict.get("_master") or {}).get("dish") or None
                            vector_store.upsert_recipe_vector(
                                conn, seq_id, rec_vec, chapter=ch, dish=dish_for_vec,
                            )
                            print(f"[VEC] upserted master recipe {seq_id} (dish={dish_for_vec!r}, chapter={ch!r})")
                        else:
                            # User recipe: store the vector AND match it to a dish
                            # (master rows already carry _master.dish). Reuse the
                            # vector we just computed — no second embed. L2 distance
                            # <= MATCH_MAX_DIST is a confident match (validated
                            # 2026-06-02: Banana Bread 0.20 / Chicken Piccata 0.25
                            # confident; Mac&Cheese 1.02 / Salmon 0.98 = no real dish
                            # in the set → not confident). Persisted as `_match` so
                            # the form + future user-recipe scoring can read it.
                            from input.pipeline import system_config as _cfg
                            MATCH_MAX_DIST = float(_cfg.get_setting("dish_match_max_distance", 0.85))
                            vector_store.enable_vec(conn)
                            cands = vector_store.find_similar_dishes(conn, rec_vec, k=3)
                            if cands:
                                best = cands[0]
                                confident = best["distance"] <= MATCH_MAX_DIST
                                recipe_dict["_match"] = {
                                    "dish": best["name"] if confident else None,
                                    "distance": round(best["distance"], 4),
                                    "confident": confident,
                                    "candidates": [
                                        {"dish": m["name"], "distance": round(m["distance"], 4)}
                                        for m in cands
                                    ],
                                    "matched_at": datetime.now(timezone.utc).isoformat(),
                                }
                                print(f"[MATCH] user recipe {seq_id} -> {best['name']!r} "
                                      f"d={best['distance']:.3f} confident={confident}  candidates="
                                      + ", ".join(f"{m['name']}({m['distance']:.2f})" for m in cands))
                            conn.execute(
                                "UPDATE recipes SET embedding = ?, data = ? WHERE id = ?",
                                (vec_to_bytes(rec_vec), json.dumps(recipe_dict), seq_id),
                            )
                except Exception as e:
                    print(f"[VEC] recipe embed/match failed: {e}")
    except Exception as e:
        print(f"[ERROR] Database error: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

    return {
        "recipe_id": recipe_id,
        "id": seq_id,
        "adopted": adopted,
        "bccUrl": _bcc_permalink(recipe_id),
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
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}")
    # Manual-from-reject rescues: when the bookmarklet harvested a dish
    # hint from #_bcc_dish=… the staged data carries bcc_hints. The form
    # threads them into the save payload. The hint determines the TARGET
    # (force user_id=0 → master), the role check still gates the ACTOR.
    # This means a non-staff member who somehow crafts a bcc_hints
    # payload still gets 403'd at the master gate below — no privilege
    # escalation.
    hints = payload.get("bcc_hints")
    if isinstance(hints, dict) and (hints.get("dish") or "").strip():
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
    return await asyncio.to_thread(_save_recipe_core, payload)


# Read-only metadata lookup for the form's collapsible metadata section.
# URL is passed as a query param to avoid edge cases with slashes in path
# params, and is re-normalized server-side regardless of what the client sent.
@app.get("/url-metadata")
def get_url_metadata(url: str):
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
        raise HTTPException(status_code=500, detail=f"Lookup error: {e}")
    if not row:
        # Empty shape so the form can render placeholder fields without
        # branching on null vs missing.
        return {
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
        }
    row["exists"] = True
    return row


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
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


# Extract recipe from image (no save). Image is OCR'd to markdown via the
# vision model, then routed through the same /extract-from-markdown pipeline
# so source_url/title plumbing and validation are handled in one place.
@app.post("/extract-from-image")
async def extract_from_image_endpoint(
    image: UploadFile = File(...),
    source_url: str = Form(""),
    title: str = Form(""),
    user_id: int = Form(PLACEHOLDER_USER_ID),
):
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
                raise HTTPException(status_code=500, detail=f"Vision extraction error: {e}")

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
                raise HTTPException(status_code=500, detail=f"Extraction error: {e}")

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
        _attach_chapter(recipe, usage_log=usage_log)
        _attach_moz_scoring(recipe, url_norm)
        _attach_identity_card(recipe, usage_log=usage_log)
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
        # Journal LLM token usage before returning (extract happened regardless
        # of whether the user later saves the recipe).
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        _maybe_stamp_source_drift(timings, user_id=user_id)

        # Stamp total AFTER the enrich tail (chapter/moz/identity) so the
        # reported time is true wall-clock, not just up to the cache write.
        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        print("[OK] Extraction successful")
        return {
            "success": True,
            "recipe_id": new_recipe_id,
            "recipe": recipe,
            "_timings": timings,
            "_prompt": prompts,
            "_usage": usage_log,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error extracting from image: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Extraction error: {e}")


# Extract recipe from a PDF upload (no save). Mirrors /extract-from-image
# but uses pdf_bytes_to_markdown (multi-page vision OCR) instead of
# image_to_markdown. URL-based PDFs go through /extract-from-url, which
# detects Content-Type: application/pdf and dispatches to pdf_url_to_markdown
# itself — same canonical markdown -> recipe chain at the end.
@app.post("/extract-from-pdf")
async def extract_from_pdf_endpoint(
    file: UploadFile = File(...),
    source_url: str = Form(""),
    title: str = Form(""),
    user_id: int = Form(PLACEHOLDER_USER_ID),
):
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
                raise HTTPException(status_code=500, detail=f"PDF extraction error: {e}")

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
                raise HTTPException(status_code=500, detail=f"Extraction error: {e}")

            if recipe is None:
                _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                raise HTTPException(status_code=500, detail="Failed to extract recipe from PDF")

            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        _attach_chapter(recipe, usage_log=usage_log)
        _attach_moz_scoring(recipe, url_norm)
        _attach_identity_card(recipe, usage_log=usage_log)
        recipe["id"] = new_recipe_id
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        _maybe_stamp_source_drift(timings, user_id=user_id)

        # Stamp total AFTER the enrich tail for true wall-clock timing.
        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        print("[OK] PDF extraction successful")
        return {
            "success": True,
            "recipe_id": new_recipe_id,
            "recipe": recipe,
            "_timings": timings,
            "_prompt": prompts,
            "_usage": usage_log,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error extracting from PDF: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Extraction error: {e}")


# Extract recipe from markdown text (no save). Canonical path: markdown ->
# RecipeModel via the single JSON-LD-aware LLM call. Provenance and
# classification are filled in the same call.
@app.post("/extract-from-markdown")
async def extract_from_markdown_endpoint(
    file: UploadFile = File(...),
    source_url: str = Form(""),
    title: str = Form(""),
    user_id: int = Form(PLACEHOLDER_USER_ID),
):
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
                    recipe = jsonld_to_recipe(
                        envelope["jsonld"][0],
                        source_url=effective_url,
                        title=effective_title,
                        timings=timings,
                    )
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
                    raise HTTPException(status_code=500, detail=f"Extraction error: {e}")

                if recipe is None:
                    print("[ERROR] Extraction failed - no result")
                    _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
                    raise HTTPException(status_code=500, detail="Failed to extract recipe from markdown")

            # Stamp translation provenance on cache row (so refetch sees it).
            if recipe is not None and translation_meta_bm:
                src = recipe.get("_source") or {}
                src["originalLanguage"] = translation_meta_bm["originalLanguage"]
                src["translated"] = True
                src["translatedAt"] = translation_meta_bm["translatedAt"]
                if translation_meta_bm.get("originalTitle"):
                    src["originalTitle"] = translation_meta_bm["originalTitle"]
                recipe["_source"] = src

            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        _attach_chapter(recipe, usage_log=usage_log)
        _attach_moz_scoring(recipe, url_norm)
        _attach_identity_card(recipe, usage_log=usage_log)
        recipe["id"] = new_recipe_id
        # Journal LLM token usage before returning.
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        _maybe_stamp_source_drift(timings, user_id=user_id)

        # Stamp total AFTER the enrich tail for true wall-clock timing.
        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        print("[OK] Extraction successful")
        return {
            "success": True,
            "recipe_id": new_recipe_id,
            "recipe": recipe,
            "_timings": timings,
            "_prompt": prompts,
            "_usage": usage_log,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Error extracting from markdown: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Extraction error: {e}")


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
        jsonld=(md_result["jsonld"][0] if md_result.get("jsonld") else None),
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
            src = recipe.get("_source") or {}
            src["originalLanguage"] = translation_meta["originalLanguage"]
            src["translated"] = True
            src["translatedAt"] = translation_meta["translatedAt"]
            if translation_meta.get("originalTitle"):
                src["originalTitle"] = translation_meta["originalTitle"]
            recipe["_source"] = src

    if recipe is None:
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        raise RuntimeError("Failed to extract recipe from URL")

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
        if og_image_url and not src.get("previewImage"):
            try:
                from input.pipeline.image_pipeline import coopt_image
                t_coopt = time.perf_counter()
                cooped = coopt_image(og_image_url)
                timings["image_coopt_ms"] = int((time.perf_counter() - t_coopt) * 1000)
                if cooped:
                    src["previewImage"] = cooped
                    print(f"[OG-IMAGE] cooped {og_image_url[:80]!r} -> {cooped}")
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

    # Page screenshot — capture only when missing (guards a complete cache
    # hit from re-capturing). Stored as a compact JPEG BLOB in media.db
    # keyed by url_normalized; _source.pageScreenshot holds the short
    # /screenshot/<id> URL the blob endpoint serves. Durable alongside the
    # cache row (image_store's generated/ is git-ignored + ephemeral).
    if url_norm and not (recipe.get("_source") or {}).get("pageScreenshot"):
        try:
            from input.pipeline.screenshot_pipeline import capture_and_store_blob
            t_shot = time.perf_counter()
            shot_url = capture_and_store_blob(url, url_norm, MEDIA_DB_PATH)
            timings["screenshot_ms"] = int((time.perf_counter() - t_shot) * 1000)
            if shot_url:
                src = recipe.get("_source") or {}
                src["pageScreenshot"] = shot_url
                recipe["_source"] = src
                print(f"[SCREENSHOT] stored blob: {shot_url}")
        except Exception as e:
            print(f"[SCREENSHOT] capture failed (continuing): {e}")

    # Cache write AFTER enrichment so screenshot/identity/preview travel with
    # the row. Write on a fresh extract, or to self-heal a hit row that
    # predated screenshot/identity caching. Stamp the raw-source fingerprint so a
    # future revalidating harvest can detect a source change without an LLM call.
    if path_used != "cache-hit" or was_incomplete:
        cache_status, drift = _extract_cache_write(
            url_norm, recipe, prior_fingerprint=prior_fp, source_fingerprint=current_source_fp)

    timings["path"] = path_used
    _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

    # Batch pre_scored override — authoritative upstream numbers win, applied
    # AFTER the cache write so they don't pollute the shared cache row.
    if pre_scored:
        scoring = recipe.get("_scoring") or {}
        for k in ("pageAuthority", "domainAuthority", "ouScore", "rootDomain", "rawTitle"):
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
    url: str = Form(...),
    user_id: int = Form(PLACEHOLDER_USER_ID),
):
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
            raise HTTPException(status_code=502, detail=msg)
        raise HTTPException(status_code=500, detail=msg)


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
    print("[ENRICH] Enrich-recipe endpoint called")
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}")
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
        raise HTTPException(status_code=500, detail=f"Enrichment error: {e}")

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
        raise HTTPException(status_code=400, detail=f"Bad JSON: {e}")
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
        raise HTTPException(status_code=500, detail=f"Measurement error: {e}")

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


@app.get("/staged-markdown/{token}")
async def get_staged_markdown(token: str):
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