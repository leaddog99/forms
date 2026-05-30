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
from fastapi.responses import JSONResponse, StreamingResponse
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
        with sqlite3.connect(DB_PATH) as conn:
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
    """
    if not usage_log:
        return
    try:
        with sqlite3.connect(DB_PATH) as conn:
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
        return None, "", "skip"
    if _is_bcc_self_url(url_normalized):
        # BCC self-URLs aren't extractable via the URL path — they
        # resolve to our form HTML, not recipe content. Treat them
        # like "no URL" so the caller falls through to vision / LLM /
        # whatever path actually has real content to work with.
        return None, "", "skip"
    result = get_cached_extract(
        DB_PATH,
        url_normalized=url_normalized,
        model=EXTRACT_MODEL,
        prompt_version=EXTRACT_PROMPT_VERSION,
    )
    if result is None:
        return None, "", "miss"
    if result["is_stale"]:
        # Pass the prior fingerprint forward; the write step on the
        # fresh re-extract will compare and surface drift.
        return None, result["semantic_fingerprint"], "stale"
    if usage_log is not None:
        usage_log.append({
            "operation": "cache_hit_markdown_to_recipe",
            "model": EXTRACT_MODEL,
            "input_tokens": 0,
            "output_tokens": 0,
            "meta": {"cached_at": result["cached_at"]},
        })
    return result["llm_output"], result["semantic_fingerprint"], "hit"


def _extract_cache_write(url_normalized, recipe, *, prior_fingerprint=""):
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
            # Reset any jobs that were 'running' when the prior process
            # died — they're not coming back, but they'd otherwise sit
            # blocking new enqueues for the same entity.
            interrupted = jobs_lib.reset_interrupted_jobs(conn)
            if interrupted:
                print(f"[JOBS] reset {interrupted} interrupted job(s) from prior run")
        print("[OK] Database tables ready")
    except Exception as e:
        print(f"[ERROR] Database initialization error: {e}")
        raise


# Initialize the app without lifespan for now to avoid hanging
app = FastAPI()

# Initialize DB immediately instead of using lifespan
print("[SETUP] Initializing database...")
init_db()
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

print("[FILE] Setting up static files...")

# Serve the web frontend (HTML / JS / CSS / bookmarklet) from the
# dedicated forms/ subdirectory. Previously this mount pointed at the
# project root, which meant /forms/save_recipe_api.py would have leaked
# Python source — moving the static surface into its own directory
# scopes the mount to web assets only. URL paths (`/forms/...`) are
# unchanged; the bat file, bookmarklet, and every <link>/<script>
# reference continue to work as-is.
try:
    forms_path = os.path.join(os.path.dirname(__file__), "forms")
    app.mount("/forms", StaticFiles(directory=forms_path), name="forms")
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


# Fetch one recipe by recipe_id. Same shape as list_recipes() rows so the
# form's existing loadForm path can consume it directly.
#
# user_id dispatches to the right table (0 = master_recipes, else =
# recipes). Default 1 preserves prior behavior for any external callers.
# This is also a security boundary: a cross-table fetch (e.g. requesting
# a master row with user_id=1) returns 404 — the caller has no way to
# discover someone else's recipes by guessing recipe_ids.
@app.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: str, user_id: int = PLACEHOLDER_USER_ID):
    table = _recipes_table_for(user_id)
    try:
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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

    filename = f"upload_{uuid.uuid4()}{ext}"
    out_path = GENERATED_DIR / filename
    out_path.write_bytes(bytes(buf))
    url = f"/generated/{filename}"
    print(f"[IMGFETCH] {source_url} -> {filename} ({len(buf)} bytes, mime={content_type})")
    return {"url": url, "bytes": len(buf), "source_url": source_url}


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
    filename = f"upload_{uuid.uuid4()}{ext}"
    out_path = GENERATED_DIR / filename
    out_path.write_bytes(content)
    url = f"/generated/{filename}"
    print(f"[IMGUP] {filename} ({len(content)} bytes, mime={image.content_type})")
    return {"url": url, "bytes": len(content)}


@app.post("/recipes/{recipe_id}/generate-image")
async def generate_recipe_image_endpoint(
    recipe_id: str,
    request: Request,
    quality: Optional[str] = None,
    size: Optional[str] = None,
    orientation: Optional[str] = None,
):
    # Lazy import — pulls openai client construction only when used.
    from image_gen_openai import generate_dish_image, _build_dish_prompt

    # Optional JSON body: {extra_prompt?: str}. User-supplied override
    # text appended to the auto-built prompt before generation.
    extra_prompt = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            extra_prompt = (body.get("extra_prompt") or "").strip()
    except Exception:
        pass  # no body, or malformed — treat as empty

    owner = _find_recipe_owner(recipe_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    table = _recipes_table_for(owner)
    with sqlite3.connect(DB_PATH) as conn:
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
        print(f"[IMGGEN] FAILED {recipe_id}: {type(e).__name__}: {e}")
        raise HTTPException(status_code=502,
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
        with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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


@app.get("/dishes/{name}/top-recipes")
def list_dish_top_recipes(name: str):
    """Return the master_recipes rows tagged as the top-N for this dish
    (`_master.dish = name AND _master.kind = 'top'`). Used by the
    dishes form to surface what's currently in the curated set —
    each row links back to its original source URL and to the
    BCC permalink for the saved master copy.

    Ordered by `_master.rank` ascending (1 = top), with un-ranked rows
    at the end. Returns [] when the dish has never refreshed or had
    no successful saves. Cheap — single SELECT, ~10-25 rows typically.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
            rows = conn.execute(
                "SELECT id, recipe_id, data FROM master_recipes "
                "WHERE json_extract(data, '$._master.dish') = ? "
                "AND json_extract(data, '$._master.kind') = 'top' "
                "ORDER BY CAST(json_extract(data, '$._master.rank') AS INTEGER) ASC, id",
                (existing["name"],),
            ).fetchall()
            out: list[dict] = []
            for seq_id, recipe_uuid, dj in rows:
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
                    "rank": master.get("rank"),
                    "source_url": source.get("originalUrl") or "",
                    "site_name": source.get("siteName") or "",
                    "bcc_url": _bcc_link_permalink(recipe_uuid),
                    "queries": master.get("queries") or [],
                    "grade": exc.get("grade"),
                    "exc_score": exc.get("score"),
                    "exc_basis": exc.get("basis") or {},
                    "pa": scoring.get("pageAuthority"),
                    "da": scoring.get("domainAuthority"),
                    "ou": scoring.get("ouScore"),
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
            existing = dishes_lib.get_dish(conn, name)
            if existing is None:
                raise HTTPException(status_code=404, detail="Dish not found")
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
        with sqlite3.connect(DB_PATH) as conn:
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
    brand is a config edit, not a code change."""
    from input.pipeline.config import BRAND_NAME, BRAND_LOGO_URL, BRAND_HOME_URL
    return {"name": BRAND_NAME, "logo_url": BRAND_LOGO_URL, "home_url": BRAND_HOME_URL}


@app.get("/chapters")
def list_chapters_endpoint():
    try:
        from input.pipeline.chapters import list_chapters_with_status
        from extract.chapter_classifier import CHAPTERS
        with sqlite3.connect(DB_PATH) as conn:
            return list_chapters_with_status(conn, CHAPTERS)
    except Exception as e:
        print(f"[ERROR] list_chapters failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.get("/chapters/{name}")
def get_chapter_endpoint(name: str):
    try:
        from input.pipeline.chapters import get_chapter_detail
        from extract.chapter_classifier import CHAPTERS
        if name not in CHAPTERS:
            raise HTTPException(status_code=404, detail=f"Unknown chapter: {name}")
        with sqlite3.connect(DB_PATH) as conn:
            return get_chapter_detail(conn, name)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] get_chapter({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.get("/chapters/{name}/recipes")
def chapter_recipes_endpoint(name: str):
    """Component records of a chapter: the master_recipes whose
    classification.chapter matches, best (OU) first. Each links to BCC
    via /r/<recipe_id>. Grade isn't stored (it's computed), so we surface
    source + OU as the at-a-glance signal."""
    from extract.chapter_classifier import CHAPTERS
    if name not in CHAPTERS:
        raise HTTPException(status_code=404, detail=f"Unknown chapter: {name}")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT recipe_id, "
                "json_extract(data, '$.name'), "
                "json_extract(data, '$._scoring.rootDomain'), "
                "json_extract(data, '$._scoring.ouScore'), "
                "json_extract(data, '$.classification.dishSignal') "
                "FROM master_recipes "
                "WHERE json_extract(data, '$.classification.chapter') = ? "
                "ORDER BY json_extract(data, '$._scoring.ouScore') DESC NULLS LAST, "
                "json_extract(data, '$.name')",
                (name,),
            ).fetchall()
        return [
            {
                "id": rid,
                "name": nm or "(untitled)",
                "host": host or "",
                "ou": ou,
                "dish": dish or "",
                "bcc_url": _bcc_link_permalink(rid) if rid else None,
            }
            for rid, nm, host, ou, dish in rows
        ]
    except Exception as e:
        print(f"[ERROR] chapter_recipes({name!r}) failed: {e}")
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
        if name not in CHAPTERS:
            raise HTTPException(status_code=404, detail=f"Unknown chapter: {name}")
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        if name not in CHAPTERS:
            raise HTTPException(status_code=404, detail=f"Unknown chapter: {name}")
        if "notes" in payload:
            notes = payload["notes"]
            if notes is not None and not isinstance(notes, str):
                raise HTTPException(status_code=400, detail="notes must be a string or null")
            notes = (notes.strip() or None) if isinstance(notes, str) else None
            with sqlite3.connect(DB_PATH) as conn:
                update_chapter_notes(conn, name, notes)
        with sqlite3.connect(DB_PATH) as conn:
            return get_chapter_detail(conn, name)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] patch_chapter({name!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update error: {e}")


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


@app.get("/admin/models")
def admin_list_models():
    """The registered models, for the view's model switcher."""
    return [{"name": m.name, "label": m.label} for m in _admin.ADMIN_MODELS.values()]


@app.get("/admin/{model}/schema")
def admin_model_schema(model: str):
    """Field descriptors so the generic view can render list + form."""
    return _admin_model_or_404(model).schema_json()


@app.get("/admin/{model}")
def admin_list_rows(model: str):
    m = _admin_model_or_404(model)
    cols = [f.name for f in m.fields]
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(cols)} FROM {m.table} ORDER BY {m.order_by}"
            ).fetchall()
        return {"model": m.schema_json(),
                "rows": [dict(zip(cols, r)) for r in rows]}
    except Exception as e:
        print(f"[ERROR] admin_list({model!r}) failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


@app.post("/admin/{model}")
def admin_create_row(model: str, payload: dict = Body(...)):
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
        with sqlite3.connect(DB_PATH) as conn:
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
def admin_update_row(model: str, row_id: int, payload: dict = Body(...)):
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
        with sqlite3.connect(DB_PATH) as conn:
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
def admin_delete_row(model: str, row_id: int):
    m = _admin_model_or_404(model)
    try:
        with sqlite3.connect(DB_PATH) as conn:
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
    """Enabled status messages grouped by category, for the recipe form's
    rotating-wait-message helper. Lean payload: ordered strings per category."""
    try:
        out: dict[str, list] = {}
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT category, message FROM status_messages "
                "WHERE enabled = 1 ORDER BY category, sort_order, id"
            ).fetchall()
        for cat, msg in rows:
            out.setdefault(cat, []).append(msg)
        return out
    except Exception as e:
        print(f"[ERROR] status_messages_active failed: {e}")
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
    with sqlite3.connect(DB_PATH) as conn:
        dish = dishes_lib.get_dish(conn, name)
    if dish is None:
        raise RuntimeError(f"Dish {name!r} not found at run time (deleted?)")
    canonical_name = dish["name"]

    print(f"=== Dish refresh: {canonical_name!r} ===")
    print(f"queries: {dish['queries']}")
    print(f"top_n_serpapi: {dish['top_n_serpapi']} per query, "
          f"top_n_final: {dish['top_n_final']}")
    print(f"[REFRESH-DISH] {canonical_name!r} starting")

    try:
        batch_result = await asyncio.to_thread(
            build_batch,
            queries=dish["queries"],
            dish=canonical_name,
            top_n_serpapi=dish["top_n_serpapi"],
            top_n_final=dish["top_n_final"],
        )
    except Exception as e:
        print(f"[REFRESH-DISH] build_batch failed: {e}")
        with sqlite3.connect(DB_PATH) as conn:
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
            from input.pipeline.chapters import replace_data_points_for_dish
            with sqlite3.connect(DB_PATH) as conn:
                n_written = replace_data_points_for_dish(conn, canonical_name, fit_points)
            print(f"[REFRESH-DISH] persisted {n_written} data points "
                  f"for chapter-fit aggregation")
        except Exception as e:
            print(f"[REFRESH-DISH] data-points persist failed (non-fatal): {e}")

    # Delete prior top-kind rows for this dish — editors_choice and
    # legacy survive. Done BEFORE saves so the (url_normalized,
    # user_id=0) unique index can't collide between old + new.
    with sqlite3.connect(DB_PATH) as conn:
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

    for entry in entries:
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
            "rank": entry.get("rank"),
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
        # Auto-enrich is opt-in per dish (defaults off). The save core
        # reads this flag to decide whether to fan out the 3 enrich
        # blocks (~$0.05 + ~10s per row). Without it, the dish refresh
        # is fast + cheap; user can enrich later from the form.
        payload["_skip_auto_enrich"] = not bool(dish.get("auto_enrich"))
        try:
            await asyncio.to_thread(_save_recipe_core, payload)
            saved_count += 1
        except HTTPException as e:
            print(f"[REFRESH-DISH] SAVE-FAIL {url}: {e.status_code} {e.detail}")
            _record_reject(entry, f"save-fail-{e.status_code}: {e.detail}")
        except Exception as e:
            print(f"[REFRESH-DISH] SAVE-FAIL {url}: {type(e).__name__}: {e}")
            _record_reject(entry, f"save-fail: {type(e).__name__}")

    # Compute the bar-to-beat: the OU of the lowest-ranked URL that
    # made it into the final top-N (the LAST surviving entry — they're
    # rank-ordered by OU descending). Used by the dish form to flag
    # rejects whose OU exceeds this — "would have qualified."
    bottom_ou: Optional[float] = None
    if entries:
        last = entries[-1]
        if isinstance(last.get("ou"), (int, float)):
            bottom_ou = float(last["ou"])

    dish_status = "success" if saved_count > 0 else "error:no_saves"
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
        dish = dishes_lib.get_dish(conn, name)
    if dish is None:
        raise HTTPException(status_code=404, detail="Dish not found")
    if not dish["queries"]:
        raise HTTPException(status_code=400,
                            detail=f"Dish {name!r} has no queries")

    entity_ref = f"dish:{dish['name']}"
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
        return jobs_lib.list_jobs(
            conn, type=type, entity_ref=entity_ref,
            status=status, limit=limit,
        )


@app.get("/jobs/{job_id}")
def get_job_endpoint(job_id: int):
    """Single-job status. Polled by UIs that don't use the SSE stream."""
    with sqlite3.connect(DB_PATH) as conn:
        job = jobs_lib.get_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


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
                with sqlite3.connect(DB_PATH) as conn:
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
            with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
            running = jobs_lib.list_jobs(conn, status="running", limit=10)
        return JSONResponse(
            status_code=409,
            content={"error": "drain already running",
                     "running": [j["id"] for j in running]},
        )
    with sqlite3.connect(DB_PATH) as conn:
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


# List recipes for the given owner. user_id=0 returns the master collection
# (master_recipes table); any other value returns that owner's personal
# recipes. Default preserves the prior behavior for the form's sidebar.
@app.get("/recipes")
def list_recipes(user_id: int = PLACEHOLDER_USER_ID):
    table = _recipes_table_for(user_id)
    print(f"[LIST] List recipes endpoint called user_id={user_id} table={table}")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT id, recipe_id, user_id, data, source_changed_at, created_at, updated_at "
                f"FROM {table} WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            )
            rows = cursor.fetchall()
            result = []

            for row in rows:
                try:
                    recipe_entry = {
                        "id": row[0],
                        "recipe_id": row[1],
                        "user_id": row[2],
                        "data": json.loads(row[3]),
                        "source_changed_at": row[4],
                        "created_at": row[5],
                        "updated_at": row[6],
                        "bccUrl": _bcc_permalink(row[1]),
                    }
                    result.append(recipe_entry)
                except json.JSONDecodeError as e:
                    print(f"[WARN] Failed to parse recipe {row[1]}: {e}")
                    continue

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
            with sqlite3.connect(DB_PATH) as conn:
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
            with sqlite3.connect(DB_PATH) as conn:
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
                with sqlite3.connect(DB_PATH) as conn:
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
        print(f"[DATA] Received payload: {payload}")
        cleaned = sanitize_recipe_data(payload)
        print(f"[CLEAN] Sanitized data: {cleaned}")
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
            if user_id == 0 and seq_id is not None:
                try:
                    from input.pipeline.embeddings import (
                        compose_recipe_text, embed_text,
                    )
                    from input.pipeline import vector_store
                    from input.pipeline.embeddings import vec_to_bytes
                    txt = compose_recipe_text(recipe_dict)
                    if txt.strip():
                        rec_vec = embed_text(txt)
                        vector_store.enable_vec(conn)
                        master_block_for_vec = recipe_dict.get("_master") or {}
                        ch = ((recipe_dict.get("classification") or {}).get("chapter") or None)
                        dish_for_vec = master_block_for_vec.get("dish") or None
                        # Source-of-truth: persist the vector on the row so
                        # recipes_master_vec can be rebuilt for free (and the
                        # .sql backup preserves it). The vec0 upsert is the
                        # derived index used for live KNN.
                        conn.execute(
                            "UPDATE master_recipes SET embedding = ? WHERE id = ?",
                            (vec_to_bytes(rec_vec), seq_id),
                        )
                        vector_store.upsert_recipe_vector(
                            conn, seq_id, rec_vec,
                            chapter=ch, dish=dish_for_vec,
                        )
                        print(f"[VEC] upserted master recipe {seq_id} (dish={dish_for_vec!r}, chapter={ch!r})")
                except Exception as e:
                    print(f"[VEC] master recipe vec upsert failed: {e}")
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
        with sqlite3.connect(DB_PATH) as conn:
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
        with sqlite3.connect(DB_PATH) as conn:
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
        t_start = time.perf_counter()

        # Endpoint-level cache: when the bookmarklet supplies a source_url,
        # a previously-extracted recipe for that URL skips both the vision
        # OCR call AND the markdown-extract LLM call.
        url_norm = normalize_url(source_url) if source_url else ""
        recipe, prior_fp, cache_status = _extract_cache_lookup(url_norm, usage_log=usage_log)
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

        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
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
        # Journal LLM token usage before returning (extract happened regardless
        # of whether the user later saves the recipe).
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        _maybe_stamp_source_drift(timings, user_id=user_id)

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
        t_start = time.perf_counter()

        # Endpoint-level cache: a previously-extracted recipe for this URL
        # skips both the PDF render+vision step AND the markdown-extract LLM
        # call. Empty source_url means cache is skipped (e.g. raw upload
        # with no URL context).
        url_norm = normalize_url(source_url) if source_url else ""
        recipe, prior_fp, cache_status = _extract_cache_lookup(url_norm, usage_log=usage_log)
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

        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        _attach_chapter(recipe, usage_log=usage_log)
        _attach_moz_scoring(recipe, url_norm)
        _attach_identity_card(recipe, usage_log=usage_log)
        recipe["id"] = new_recipe_id
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        _maybe_stamp_source_drift(timings, user_id=user_id)

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
        t_start = time.perf_counter()

        # === Extraction-stage translation (bookmarklet path) =======
        # Markdown comes from the bookmarklet/browser, so there are no
        # HTTP headers or <html lang>. Detect language from the markdown
        # body via fasttext (the third tier of detect_language). If
        # non-English, translate before extraction. JSON-LD blob from
        # the envelope is dropped on translation since its content
        # would still be source-language.
        translation_meta_bm: dict | None = None
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

        url_norm = normalize_url(effective_url) if effective_url else ""
        recipe, prior_fp, cache_status = _extract_cache_lookup(url_norm, usage_log=usage_log)
        drift = False
        path_used = "cache-hit" if recipe is not None else ""

        if recipe is None:
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

        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        _attach_chapter(recipe, usage_log=usage_log)
        _attach_moz_scoring(recipe, url_norm)
        _attach_identity_card(recipe, usage_log=usage_log)
        recipe["id"] = new_recipe_id
        # Journal LLM token usage before returning.
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        _maybe_stamp_source_drift(timings, user_id=user_id)

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
    t_start = time.perf_counter()

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

    try:
        if is_pdf:
            md_result = pdf_url_to_markdown(url, timings, usage_log)
        else:
            md_result = html_to_markdown(url, timings)
    except Exception as e:
        print(f"[ERROR] Fetch/convert failed for {url!r}: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise RuntimeError(f"Failed to fetch/convert URL: {e}") from e

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
        )
        if is_non_english(page_lang):
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
    recipe, prior_fp, cache_status = _extract_cache_lookup(url_norm, usage_log=usage_log)
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

    if recipe is not None:
        path_used = "cache-hit"
    else:
        if md_result.get("jsonld"):
            try:
                recipe = jsonld_to_recipe(
                    md_result["jsonld"][0],
                    source_url=md_result["source_url"],
                    title=md_result["title"],
                    timings=timings,
                )
                if recipe is not None:
                    path_used = "jsonld-direct"
            except Exception as e:
                print(f"[WARN] jsonld_to_recipe raised, will fall back: {e}")
                recipe = None

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

        if recipe is not None:
            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

    if recipe is None:
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        raise RuntimeError("Failed to extract recipe from URL")

    timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
    timings["path"] = path_used
    _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

    _attach_chapter(recipe, usage_log=usage_log)

    # Stamp the full og: meta block on _source: cooped preview image
    # (locally hosted, no hotlinking), description, alt text, site
    # name, author + timestamps. These come from the page's <meta>
    # tags — the source's own consent-given preview data.
    og_meta = md_result.get("og_meta") or {}
    if og_meta:
        src = recipe.get("_source") or {}
        # Stash the non-image text fields directly — cheap, no fetch
        # required. UI surfaces (tile description, alt text, site
        # name attribution) can consume immediately.
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
        # Coopt the image — fetch + Pillow process + store via active
        # backend (Local or S3). Best-effort: failure leaves
        # previewImage unset and the UI falls back to schema.org
        # image[0]. Skipped when og:image is missing.
        og_image_url = (og_meta.get("image") or "").strip()
        if og_image_url:
            try:
                from input.pipeline.image_pipeline import coopt_image
                cooped = coopt_image(og_image_url)
                if cooped:
                    src["previewImage"] = cooped
                    print(f"[OG-IMAGE] cooped {og_image_url[:80]!r} -> {cooped}")
            except Exception as e:
                print(f"[OG-IMAGE] coopt failed (continuing): {e}")
        recipe["_source"] = src

    # Scoring: when the caller (typically batch ingestion) provides
    # pre_scored values, trust those as canonical and SKIP _attach_moz_scoring
    # entirely. _attach_moz_scoring unconditionally overwrites recipe._scoring
    # from the metabase_url cache / Moz API — fine for the form's interactive
    # path where no upstream scores exist, but wrong for batch where the
    # upstream pipeline has already produced authoritative numbers. Side
    # effect: metabase_url isn't refreshed from batch runs; the form's
    # metadata-refresh path remains the way to update it.
    if pre_scored:
        scoring = recipe.get("_scoring") or {}
        for k in ("pageAuthority", "domainAuthority", "ouScore", "rootDomain", "rawTitle"):
            v = pre_scored.get(k)
            if v is not None and v != "":
                scoring[k] = v
        recipe["_scoring"] = scoring
    else:
        _attach_moz_scoring(recipe, url_norm)
    recipe["id"] = new_recipe_id
    _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
    _maybe_stamp_source_drift(timings, user_id=user_id)

    # Batch overrides: authoritative fields the upstream batch declared.
    # Apply LAST so they win over anything extract/enrich derived. Shallow-
    # merge nested dicts (don't replace classification wholesale — overlay
    # only the keys the batch supplied).
    if batch_overrides:
        for k, v in batch_overrides.items():
            if isinstance(v, dict) and isinstance(recipe.get(k), dict):
                recipe[k].update(v)
            else:
                recipe[k] = v

    # Identity card — populates the form's cohort matching panel
    # immediately on extract, before save. See _attach_identity_card.
    _attach_identity_card(recipe, usage_log=usage_log)

    # Page screenshot — capture the above-fold view of the source page
    # via Playwright + store via image_store. Stamps the URL on
    # _source.pageScreenshot so the form can show "this is what the
    # source actually looked like." Best-effort: failures don't block
    # the extract. ~3-5s per call.
    try:
        from input.pipeline.screenshot_pipeline import capture_screenshot
        screen_url = capture_screenshot(url, new_recipe_id)
        if screen_url:
            src = recipe.get("_source") or {}
            src["pageScreenshot"] = screen_url
            recipe["_source"] = src
            print(f"[SCREENSHOT] stamped: {screen_url}")
    except Exception as e:
        print(f"[SCREENSHOT] capture failed (continuing): {e}")

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

    try:
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
    timings["path"] = "enrich-only"
    _journal_usage(usage_log, recipe_id=recipe_id, user_id=user_id)

    print("[OK] Enrichment successful")
    return {
        "success": True,
        "recipe": enriched,
        "_timings": timings,
        "_prompt": prompts,
        "_usage": usage_log,
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