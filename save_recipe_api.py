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

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
import sqlite3
import uuid
import asyncio
import json
import time
from datetime import datetime
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

    print("[OK] url_utils / url_scoring imported successfully")
except Exception as e:
    print(f"[ERROR] Failed to import url_utils / url_scoring: {e}")
    raise

print("[START] Starting API setup...")

DB_PATH = "recipes.db"

# Placeholder user id until the user-identity field is wired into the form
# (will eventually come from Ghost). Recipes and token-journal rows both use it.
PLACEHOLDER_USER_ID = 1


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

_CACHE_STUBBED = False


def _is_cacheable(recipe: dict) -> tuple[bool, str]:
    """Refuse to cache rows that look like a bad extraction (paywall,
    404, picked-the-wrong-recipe sidebar carousel). Returns
    (cacheable, reason). Thresholds match what's reasonable for a real
    recipe: a name AND at least 2 ingredients AND at least 2 instructions.
    """
    name = (recipe.get("name") or "").strip() if recipe else ""
    if not name:
        return False, "no name"
    ings = recipe.get("recipeIngredient") or []
    if sum(1 for i in ings if str(i).strip()) < 2:
        return False, "fewer than 2 ingredients"
    steps = recipe.get("recipeInstructions") or []
    real_steps = 0
    for s in steps:
        text = s.get("text") if isinstance(s, dict) else s
        if str(text or "").strip():
            real_steps += 1
    if real_steps < 2:
        return False, "fewer than 2 instructions"
    return True, "ok"


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
            _seed_users_from_recipes(conn)

            ensure_metabase_url_table(conn)
            ensure_bcc_token_journal_table(conn)
            ensure_llm_extract_cache_table(conn)
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

# Serve static HTML files (e.g., recipe_form.html)
try:
    forms_path = os.path.dirname(__file__)  # Use the directory this file is in
    app.mount("/forms", StaticFiles(directory=forms_path), name="forms")
    print("[OK] Static files mounted successfully")
except Exception as e:
    print(f"[WARN] Static files mount failed: {e}")

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
                "subscription_tier, created_at, updated_at "
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
                    "created_at": r[6],
                    "updated_at": r[7],
                }
                for r in rows
            ]
    except Exception as e:
        print(f"[ERROR] list_users failed: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


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
    now = datetime.utcnow().isoformat()
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO users (email, name, status, subscription_tier, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (email, name, status, tier, now, now),
            )
            user_id = cur.lastrowid
        return {
            "user_id": user_id,
            "email": email,
            "name": name,
            "status": status,
            "subscription_tier": tier,
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

    allowed = {"name", "email", "status", "subscription_tier", "ghost_uuid"}
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
                "subscription_tier, created_at, updated_at "
                "FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return {
            "user_id": row[0], "ghost_uuid": row[1], "email": row[2],
            "name": row[3], "status": row[4], "subscription_tier": row[5],
            "created_at": row[6], "updated_at": row[7],
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
                        "updated_at": row[6]
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


# Save (insert or update) a recipe
@app.post("/recipes")
async def save_recipe(request: Request):
    print("[SAVE] Save recipe endpoint called")
    try:
        # Get the payload
        payload = await request.json()
        print(f"[DATA] Received payload: {payload}")

        # IMPORTANT: Use the critical business logic files
        cleaned = sanitize_recipe_data(payload)
        print(f"[CLEAN] Sanitized data: {cleaned}")

        recipe = RecipeModel(**cleaned)
        print("[OK] Recipe model validation passed")

    except ValidationError as e:
        print(f"[ERROR] Validation error: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        print(f"[ERROR] Error processing request: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=400, detail=f"Bad input: {e}")

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
    # https://<host>/r/<recipe_id>. Done BEFORE the adopt-existing check
    # below so a re-save of a once-saved local recipe still works (the
    # second save sees the same minted URL and adopts the existing row).
    # Skip for claimed rows — they intentionally have no url_normalized.
    if not raw_source_url and not is_claimed_row:
        base = str(request.base_url).rstrip("/")
        synthetic_url = f"{base}/r/{recipe_id}"
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
    if user_id == 0:
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
    except Exception as e:
        print(f"[ERROR] Database error: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Database error: {e}")

    return {"recipe_id": recipe_id, "id": seq_id, "adopted": adopted}


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
def delete_recipe(recipe_id: str, user_id: int = PLACEHOLDER_USER_ID):
    table = _recipes_table_for(user_id)
    print(f"[DELETE] Delete recipe endpoint called for: {recipe_id} user_id={user_id} table={table}")
    try:
        with sqlite3.connect(DB_PATH) as conn:
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

            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        _attach_chapter(recipe, usage_log=usage_log)
        _attach_moz_scoring(recipe, url_norm)
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
          f"source_url={md_result['source_url']!r}")

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

        if recipe is not None:
            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

    if recipe is None:
        _journal_usage(usage_log, recipe_id=new_recipe_id, user_id=user_id)
        raise RuntimeError("Failed to extract recipe from URL")

    timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
    timings["path"] = path_used
    _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

    _attach_chapter(recipe, usage_log=usage_log)

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
    try:
        return await asyncio.to_thread(
            extract_recipe_from_url, url.strip(), user_id=user_id,
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
    _staged_markdown[token] = {
        "markdown": md_text,
        "source_url": payload.get("source_url", ""),
        "title": payload.get("title", ""),
        "expires_at": now + _STAGE_TTL_SECONDS,
    }
    print(f"[OK] Staged markdown under token {token[:8]} ({len(md_text)} chars)")
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