# TODO (revisit): persist the original source image used during AI extraction.
# Today /extract reads the upload and discards it. Consider saving it to a stable
# location (e.g. input/ or object storage) and returning its URL so it can be
# stored on the recipe and shown in the edit view. See matching TODOs in
# recipe_model.py (sourceImage field) and recipe_form_styled.html (UI).
# Decide: storage location, retention, multi-image (re-extractions), privacy.

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
    from extract.markdown_to_recipe import markdown_to_recipe
    from extract.jsonld_to_recipe import jsonld_to_recipe
    from extract.enrich_recipe import enrich_recipe

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

    print("[OK] url_utils / url_scoring imported successfully")
except Exception as e:
    print(f"[ERROR] Failed to import url_utils / url_scoring: {e}")
    raise

print("[START] Starting API setup...")

DB_PATH = "recipes.db"

# Placeholder user id until the user-identity field is wired into the form
# (will eventually come from Ghost). Recipes and token-journal rows both use it.
PLACEHOLDER_USER_ID = 1


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
                    created_at TEXT,
                    updated_at TEXT
                );
            """)
            ensure_metabase_url_table(conn)
            ensure_bcc_token_journal_table(conn)
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
            cursor.execute("SELECT id, recipe_id, data, created_at, updated_at FROM recipes ORDER BY updated_at DESC")
            rows = cursor.fetchall()
            result = []

            for row in rows:
                try:
                    recipe_entry = {
                        "id": row[0],
                        "recipe_id": row[1],
                        "data": json.loads(row[2]),
                        "created_at": row[3],
                        "updated_at": row[4]
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

    print(f"[SAVE] Saving recipe with ID: {recipe_id}")

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO recipes (recipe_id, user_id, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(recipe_id) DO UPDATE SET
                    data = excluded.data,
                    updated_at = excluded.updated_at;
            """, (
                recipe_id,
                user_id,
                json.dumps(recipe_dict, indent=2),
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

    return {"recipe_id": recipe_id, "id": seq_id}


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

        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        timings["path"] = "image-llm"

        # Stamp the minted UUID onto the recipe so the form picks it up.
        recipe["id"] = new_recipe_id
        # Journal LLM token usage before returning (extract happened regardless
        # of whether the user later saves the recipe).
        _journal_usage(usage_log, recipe_id=new_recipe_id)

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

        timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
        timings["path"] = "markdown-llm"

        recipe["id"] = new_recipe_id
        # Journal LLM token usage before returning.
        _journal_usage(usage_log, recipe_id=new_recipe_id)

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

    # Fast lane: when the page ships complete schema.org Recipe JSON-LD, parse
    # it directly (no LLM) and run only a small enrichment LLM call for
    # provenance + classification. Falls through to the big-prompt path if
    # JSON-LD is missing or lacks required fields.
    recipe = None
    path_used = ""
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

    if recipe is None:
        _journal_usage(usage_log, recipe_id=new_recipe_id)
        raise HTTPException(status_code=500, detail="Failed to extract recipe from URL")

    timings["total_ms"] = int((time.perf_counter() - t_start) * 1000)
    timings["path"] = path_used

    recipe["id"] = new_recipe_id
    # Journal LLM token usage before returning.
    _journal_usage(usage_log, recipe_id=new_recipe_id)

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