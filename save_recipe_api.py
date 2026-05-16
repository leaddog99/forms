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

# In-memory staging for bookmarklet → form handoff. One-time read, TTL pruned.
_STAGE_TTL_SECONDS = 600
_staged_markdown: dict[str, dict] = {}

# IMPORTANT: Keep the imports for the critical business logic files
try:
    from recipe_model import RecipeModel

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
    from extract.markdown_to_recipe import markdown_to_recipe, SYSTEM_PROMPT as _MD_PROMPT
    from extract.jsonld_to_recipe import jsonld_to_recipe
    from extract.enrich_recipe import enrich_recipe, SYSTEM_PROMPT as _ENRICH_PROMPT

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

# Pipeline cache identity. One key shape for both the JSON-LD fast lane
# (jsonld_to_recipe + enrich_recipe) and the markdown-LLM path
# (markdown_to_recipe). When any of the three load-bearing prompts change,
# the combined version flips and every cache row naturally invalidates.
EXTRACT_MODEL = "gpt-4o-mini"
EXTRACT_PROMPT_VERSION = prompt_version_for(
    _MD_PROMPT + "\n---ENRICH---\n" + _ENRICH_PROMPT
    + "\n---IMAGE---\n" + IMAGE_TO_MARKDOWN_PROMPT
)
print(f"[CACHE] EXTRACT_PROMPT_VERSION = {EXTRACT_PROMPT_VERSION}")


def _journal_usage(usage_log, *, recipe_id=None):
    """Best-effort token-journal write. Opens its own connection so it can be
    called from anywhere in the request lifecycle; never raises."""
    if not usage_log:
        return
    try:
        with sqlite3.connect(DB_PATH) as conn:
            write_usage_entries(
                conn,
                user_id=PLACEHOLDER_USER_ID,
                recipe_id=recipe_id,
                entries=usage_log,
            )
    except Exception as e:
        print(f"[WARN] token-journal write failed: {e}")


def _extract_cache_lookup(url_normalized, *, usage_log=None):
    """Endpoint-side cache lookup. Returns (recipe_or_None, prior_fingerprint, status).

    Status is one of: 'hit' (fresh, recipe returned), 'refresh' (stale row
    exists, prior fingerprint returned for drift comparison after re-extract),
    'miss' (no row), 'skip' (no URL — caching not applicable).
    On a fresh hit, journals a zero-token 'cache_hit_extract' entry."""
    if not url_normalized:
        print(f"     CACHE LOOKUP: skip (no url)")
        return None, "", "skip"
    cached = get_cached_extract(
        DB_PATH,
        url_normalized=url_normalized,
        model=EXTRACT_MODEL,
        prompt_version=EXTRACT_PROMPT_VERSION,
    )
    print(f"     CACHE LOOKUP: url={url_normalized!r} -> "
          f"{'no row' if cached is None else ('stale' if cached['is_stale'] else 'fresh hit')}")
    if cached and not cached["is_stale"]:
        if usage_log is not None:
            usage_log.append({
                "operation": "cache_hit_extract",
                "model": EXTRACT_MODEL,
                "input_tokens": 0,
                "output_tokens": 0,
                "meta": {
                    "cache_key_url": url_normalized,
                    "cached_at": cached["cached_at"],
                },
            })
        return cached["llm_output"], "", "hit"
    if cached:
        return None, cached["semantic_fingerprint"], "refresh"
    return None, "", "miss"


def _extract_cache_write(url_normalized, recipe, *, prior_fingerprint=""):
    """Endpoint-side cache write. Computes the recipe fingerprint, stores
    the row (or replaces the stale one), and returns (final_status,
    drift_detected). drift_detected fires only when a prior fingerprint
    existed and the new one differs from it."""
    if not url_normalized or not recipe:
        return ("skip" if not url_normalized else "miss"), False
    new_fp = compute_recipe_fingerprint(recipe)
    drift = bool(prior_fingerprint and prior_fingerprint != new_fp)
    print(f"     CACHE WRITE: url={url_normalized!r} fp={new_fp[:12]} "
          f"prior_fp={prior_fingerprint[:12] if prior_fingerprint else '-'} "
          f"drift={drift}")
    set_cached_extract(
        DB_PATH,
        url_normalized=url_normalized,
        model=EXTRACT_MODEL,
        prompt_version=EXTRACT_PROMPT_VERSION,
        llm_output=recipe,
        semantic_fingerprint=new_fp,
    )
    if drift:
        return "refresh-drift", True
    if prior_fingerprint:
        return "refresh-fresh", False
    return "miss", False


def _stamp_cache_timings(timings, *, status, url_normalized, drift=False):
    """Push cache state into the extract trace so the form can render it."""
    if timings is None:
        return
    timings["cache"] = status
    timings["cache_key_url"] = url_normalized or "(no url — cache skipped)"
    if drift and url_normalized:
        timings["source_drift"] = True
        timings["drift_url"] = url_normalized


def _maybe_stamp_source_drift(timings, *, user_id):
    """When markdown_to_recipe sets timings["source_drift"] (a TTL-expired
    re-extract whose semantic fingerprint differs from the cached one),
    stamp recipes.source_changed_at on every saved recipe matching that URL
    + user. The form reads the stamp and shows a "source updated — review
    and re-save" banner; save clears the stamp."""
    if not timings or not timings.get("source_drift"):
        return
    url_normalized = timings.get("drift_url") or ""
    if not url_normalized:
        return
    try:
        now = datetime.utcnow().isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                "UPDATE recipes SET source_changed_at = ? "
                "WHERE url_normalized = ? AND user_id = ?",
                (now, url_normalized, user_id),
            )
            conn.commit()
            if cursor.rowcount:
                print(f"[DRIFT] Stamped source_changed_at on "
                      f"{cursor.rowcount} recipe(s) for {url_normalized!r}")
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


# List all recipes
@app.get("/recipes")
def list_recipes():
    print("[LIST] List recipes endpoint called")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, recipe_id, data, source_changed_at, created_at, updated_at "
                "FROM recipes ORDER BY updated_at DESC"
            )
            rows = cursor.fetchall()
            result = []

            for row in rows:
                try:
                    recipe_entry = {
                        "id": row[0],
                        "recipe_id": row[1],
                        "data": json.loads(row[2]),
                        "source_changed_at": row[3],
                        "created_at": row[4],
                        "updated_at": row[5]
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
    user_id = 1  # Placeholder

    # Normalize the source URL one more time at save (defensive — covers
    # recipes that were created before normalize_url existed in the extract
    # path, or hand-edited URLs).
    recipe_dict = recipe.model_dump(by_alias=True)
    source = recipe_dict.get("_source") or {}
    raw_source_url = source.get("originalUrl") or ""
    normalized_source_url = normalize_url(raw_source_url) if raw_source_url else ""
    if normalized_source_url and normalized_source_url != raw_source_url:
        source["originalUrl"] = normalized_source_url
        recipe_dict["_source"] = source

    # Dedup: if a row already exists for (url_normalized, user_id), adopt
    # ITS recipe_id instead of the form-sent UUID so the existing record
    # gets updated rather than creating a parallel duplicate.
    adopted = False
    try:
        with sqlite3.connect(DB_PATH) as conn:
            if normalized_source_url:
                existing = conn.execute(
                    "SELECT recipe_id FROM recipes WHERE url_normalized = ? AND user_id = ? LIMIT 1",
                    (normalized_source_url, user_id),
                ).fetchone()
                if existing and existing[0] != recipe_id:
                    print(f"[SAVE] Adopting existing recipe_id {existing[0]} for {normalized_source_url!r} "
                          f"(was {recipe_id})")
                    recipe_id = existing[0]
                    adopted = True
    except Exception as e:
        print(f"[WARN] dup lookup failed (continuing as insert): {e}")

    print(f"[SAVE] Saving recipe with ID: {recipe_id} (adopted={adopted})")

    try:
        with sqlite3.connect(DB_PATH) as conn:
            # Save clears source_changed_at: the user reviewing and saving is
            # the acknowledgement of any prior drift signal.
            conn.execute("""
                INSERT INTO recipes (recipe_id, user_id, data, url_normalized, source_changed_at, created_at, updated_at)
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
            # If the recipe has a source URL, make sure the metabase_url row
            # exists (and bump last_accessed). Moz scoring happens inline
            # when a brand-new URL is seen and creds are configured.
            # We then denormalize the scores into recipe._scoring so they
            # travel with the recipe — the metabase_url row stays canonical.
            if normalized_source_url:
                fallback_title = (
                    (recipe_dict.get("_scoring") or {}).get("rawTitle")
                    or recipe_dict.get("name")
                    or ""
                )
                try:
                    meta = get_or_create_url_metadata(conn, normalized_source_url, fallback_title=fallback_title)
                except Exception as meta_err:
                    meta = None
                    print(f"[WARN] metabase_url upsert failed for {normalized_source_url}: {meta_err}")
                if meta:
                    scoring = recipe_dict.get("_scoring") or {}
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
                    recipe_dict["_scoring"] = scoring
                    conn.execute(
                        "UPDATE recipes SET data = ?, updated_at = ? WHERE recipe_id = ?",
                        (json.dumps(recipe_dict, indent=2), now, recipe_id),
                    )
            print("[OK] Recipe saved to database")
            # Fetch the DB-assigned integer PK so the form can display it.
            row = conn.execute("SELECT id FROM recipes WHERE recipe_id = ?", (recipe_id,)).fetchone()
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


# Delete a recipe
@app.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: str):
    print(f"[DELETE] Delete recipe endpoint called for: {recipe_id}")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM recipes WHERE recipe_id = ?", (recipe_id,))
            if cursor.rowcount == 0:
                print(f"[ERROR] Recipe {recipe_id} not found")
                raise HTTPException(status_code=404, detail="Recipe not found")
            conn.commit()
            print(f"[OK] Recipe {recipe_id} deleted successfully")
        return {"message": "Recipe deleted successfully"}
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
                _journal_usage(usage_log, recipe_id=new_recipe_id)
                raise HTTPException(status_code=500, detail=f"Vision extraction error: {e}")

            if not md or not md.strip():
                _journal_usage(usage_log, recipe_id=new_recipe_id)
                raise HTTPException(status_code=500, detail="Vision step returned empty markdown")

            # Stash the vision-stage prompt so the UI can surface it. Use a
            # sub-key to avoid colliding with markdown_to_recipe's prompts.
            prompts["vision"] = {
                "model": "gpt-4o",
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
                _journal_usage(usage_log, recipe_id=new_recipe_id)
                raise HTTPException(status_code=500, detail=f"Extraction error: {e}")

            if recipe is None:
                print("[ERROR] Extraction failed - no result")
                _journal_usage(usage_log, recipe_id=new_recipe_id)
                raise HTTPException(status_code=500, detail="Failed to extract recipe from image")

            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        # Stamp the minted UUID onto the recipe so the form picks it up.
        recipe["id"] = new_recipe_id
        # Journal LLM token usage before returning (extract happened regardless
        # of whether the user later saves the recipe).
        _journal_usage(usage_log, recipe_id=new_recipe_id)
        _maybe_stamp_source_drift(timings, user_id=PLACEHOLDER_USER_ID)

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


# Extract recipe from markdown text (no save). Canonical path: markdown ->
# RecipeModel via the single JSON-LD-aware LLM call. Provenance and
# classification are filled in the same call.
@app.post("/extract-from-markdown")
async def extract_from_markdown_endpoint(
    file: UploadFile = File(...),
    source_url: str = Form(""),
    title: str = Form(""),
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
        path_used = "cache-hit" if recipe is not None else "markdown-llm"

        if recipe is None:
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
                _journal_usage(usage_log, recipe_id=new_recipe_id)
                raise HTTPException(status_code=500, detail=f"Extraction error: {e}")

            if recipe is None:
                print("[ERROR] Extraction failed - no result")
                _journal_usage(usage_log, recipe_id=new_recipe_id)
                raise HTTPException(status_code=500, detail="Failed to extract recipe from markdown")

            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        timings["path"] = path_used
        _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

        recipe["id"] = new_recipe_id
        # Journal LLM token usage before returning.
        _journal_usage(usage_log, recipe_id=new_recipe_id)
        _maybe_stamp_source_drift(timings, user_id=PLACEHOLDER_USER_ID)

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
@app.post("/extract-from-url")
async def extract_from_url_endpoint(url: str = Form(...)):
    print(f"[EXTRACT] Extract from URL endpoint called: {url!r}")
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail="url is required")

    # Mint the recipe UUID now so token-journal entries reference it from
    # the very first LLM call.
    new_recipe_id = str(uuid.uuid4())
    timings: dict = {}
    prompts: dict = {}
    usage_log: list = []
    t_start = time.perf_counter()

    try:
        md_result = await asyncio.to_thread(html_to_markdown, url.strip(), timings)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] Fetch/convert failed for {url!r}: {e}")
        print(f"[ERROR] Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=502, detail=f"Failed to fetch/convert URL: {e}")

    print(f"[EXTRACT] has_jsonld={md_result['has_jsonld']} "
          f"markdown_len={len(md_result['markdown'])} "
          f"source_url={md_result['source_url']!r}")

    # Endpoint-level cache check covers both the JSON-LD fast lane and the
    # markdown-LLM path — whichever path originally produced the recipe, a
    # repeat extract on the same URL short-circuits to the cached result.
    url_norm = normalize_url(md_result["source_url"]) if md_result["source_url"] else ""
    recipe, prior_fp, cache_status = _extract_cache_lookup(url_norm, usage_log=usage_log)
    drift = False
    path_used = ""

    if recipe is not None:
        path_used = "cache-hit"
    else:
        # Fast lane: when the page ships complete schema.org Recipe JSON-LD,
        # parse it directly (no LLM) and run only a small enrichment LLM
        # call for provenance + classification. Falls through to the
        # big-prompt path if JSON-LD is missing or lacks required fields.
        if md_result.get("jsonld"):
            try:
                recipe = await asyncio.to_thread(
                    jsonld_to_recipe,
                    md_result["jsonld"][0],
                    source_url=md_result["source_url"],
                    title=md_result["title"],
                    timings=timings,
                )
            except Exception as e:
                print(f"[WARN] jsonld_to_recipe raised, will fall back: {e}")
                recipe = None
            if recipe is not None:
                try:
                    recipe = await asyncio.to_thread(
                        enrich_recipe,
                        recipe,
                        timings=timings,
                        prompts=prompts,
                        usage_log=usage_log,
                    )
                    path_used = "jsonld-direct"
                except Exception as e:
                    print(f"[WARN] enrich_recipe raised, keeping unenriched recipe: {e}")
                    path_used = "jsonld-direct-unenriched"

        if recipe is None:
            try:
                recipe = await asyncio.to_thread(
                    markdown_to_recipe,
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
                _journal_usage(usage_log, recipe_id=new_recipe_id)
                raise HTTPException(status_code=500, detail=f"Extraction error: {e}")

        if recipe is not None:
            cache_status, drift = _extract_cache_write(url_norm, recipe, prior_fingerprint=prior_fp)

    if recipe is None:
        _journal_usage(usage_log, recipe_id=new_recipe_id)
        raise HTTPException(status_code=500, detail="Failed to extract recipe from URL")

    timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
    timings["path"] = path_used
    _stamp_cache_timings(timings, status=cache_status, url_normalized=url_norm, drift=drift)

    recipe["id"] = new_recipe_id
    # Journal LLM token usage before returning.
    _journal_usage(usage_log, recipe_id=new_recipe_id)
    _maybe_stamp_source_drift(timings, user_id=PLACEHOLDER_USER_ID)

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


# Stage markdown from a bookmarklet so the form can pick it up on load.
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
        raise HTTPException(status_code=404, detail="Token not found or expired")
    img = entry.get("image_b64")
    if not img:
        # Caller should poll; image hasn't been uploaded yet.
        raise HTTPException(status_code=404, detail="Image not yet available")
    return {"image_b64": img}


print("[DONE] API setup complete!")