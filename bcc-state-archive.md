# BCC State — Archive

Older session logs moved out of `bcc-state-code.md` to keep the active tracker lean (state-archive architecture, 2026-06-06). See `bcc-state-code.md` for current state, to-do, ideas, and recent logs.

---

## Session log — 2026-05-13

### Markdown as the canonical recipe input format

We started the day with the image-based extractor (`/extract-from-image`, gpt-4o vision) as the only path from a captured recipe to the form. After comparing cost and quality we switched to **markdown as the canonical input format** and built a parallel `/extract-from-markdown` endpoint using `gpt-4o-mini`. The user's existing bookmarklet was already producing markdown via a DOM walk (not OCR), so the markdown path got essentially-free, high-fidelity text from the source HTML. Benefit: ~10-30× cheaper per extraction and more reliable than vision-on-screenshot, because fractions like `1/4 cup` survive verbatim instead of being OCR'd.

Late in the session we unified everything onto a **single canonical pipeline**: image → markdown → recipe. The image path now OCRs to markdown (vision-to-markdown prompt) and then routes through the same `extract_from_markdown` function as a manually-dropped `.md` file. Benefit: validation, sanitization, source/URL plumbing, and Moz scoring all happen in one place — improvements to any of them automatically apply to every input type (web capture, handwritten recipe photo, screenshot, future PDFs).

### Bookmarklet evolution

The bookmarklet became the heart of the capture flow. Three layered enhancements:

1. **JSON-LD harvesting** — most recipe sites (NYT, Kitchn, AllRecipes, Bon Appétit) emit a `<script type="application/ld+json">` Recipe block for SEO. The bookmarklet now extracts these *before* stripping `<script>` tags and embeds them as a fenced JSON block at the top of the staged markdown. The extractor's system prompt treats that block as authoritative. Benefit: works even on JS-heavy pages where the DOM walk would otherwise return only the page title.
2. **Stage-and-open** — bookmarklet `POST`s to `/stage-markdown` with `{markdown, source_url, title}`, gets a one-time token, and `window.open`s the form at `?staged=<token>`. The form pulls the staged content and runs extraction. Benefit: zero file-system involvement, one click from any recipe page to a populated form.
3. **Background screenshot fallback** — after staging the markdown the bookmarklet keeps running, loads html2canvas, captures the full rendered page, and `POST`s the PNG to `/stage-image/<token>`. If markdown extraction comes back incomplete, the form's error dialog can pull that staged image and re-run as image-to-markdown without ever showing a file picker. Benefit: graceful recovery for sites with neither JSON-LD nor a parseable DOM, using the user's already-logged-in rendered view.

### Schema and module unification (forms is canonical)

The two parallel projects (`forms` and `pipelineRecipes`) each had their own `recipe_model.py` and sanitizer. We made **forms the survivor**: kept its lenient `Optional` types, added `ScoringMetadata` / `ClassificationMetadata` / `StatusField` from pipelineRecipes, kept the helper methods (`generate_prompt`, `needs_image_generation`), and added `extra="allow"` for forward-compat. Sanitizer extended to shape the new pipeline fields. Benefit: one schema of record; the batch project will import from here rather than diverge.

Created the **`forms/pipeline/` subpackage** as the home for cross-cutting stages reusable by both the interactive form and the batch pipeline: `url_utils.py` (normalize_url, root_domain), `validators.py` (`is_recipe` phrase-scoring port), `config.py` (RECIPE_PHRASES, IS_RECIPE_THRESHOLD), `url_scoring.py` (Moz integration, metabase upsert), `refresh_url_metadata.py` (standalone maintenance CLI).

### Recipe validator wiring

Ported `worker_is_recipe` (phrase-based scoring against `RECIPE_PHRASES`) into `pipeline/validators.py`. The extractor calls `is_recipe()` on the cleaned markdown *before* the LLM call, stamps the score on `current_status` and `_scoring.recipeScore`, and **never blocks** — even low-scoring pages still get extracted, just flagged. The form's success/error banner highlights low confidence so the user can override. Benefit: data quality signal without false-negative friction.

### URL plumbing, normalization, and metabase_url

Several decisions wove together here:

- **`source_url` and `title` from the bookmarklet** ride through `/stage-markdown` → `/staged-markdown/{token}` → form → `/extract-from-markdown` → `_source.originalUrl` / `_source.origin` (domain) / `_source.type = "web"`. Manual `.md` drops and image drops still get URL plumbing if the staged metadata is available; handwritten recipes correctly have no URL.
- **One canonical URL form** — `normalize_url()` lowercases host, strips `www.`, default ports, fragments, trailing slashes, and tracking params (`utm_*`, `fbclid`, `gclid`, etc.) using a blocklist not allowlist (so site-specific params like `?recipeId=42` survive). Normalization runs in `extract_from_markdown` before stamping, and defensively again in `POST /recipes` before persist. Benefit: every recipe stores the canonical URL; joins to `metabase_url` are trivial.
- **`metabase_url` table** — URL-keyed, user-agnostic metadata storage. Separate table (not embedded in recipe JSON) so popular URLs (NYT, Kitchn) get scored once and shared, refresh runs independently, and orphans can be pruned. Columns: `url` (PK, normalized), `root_domain`, `raw_title`, `page_authority`, `domain_authority`, `ou_score`, `moz_last_scored`, `first_seen`, `last_accessed`. Named `metabase_url` rather than `url_scores` because we anticipate other URL-keyed metadata (favicon, og:image, domain reputation) living here later.
- **Auto-score on first save, non-blocking** — on `POST /recipes`, `get_or_create_url_metadata()` either inserts a row and tries Moz scoring or bumps `last_accessed`. Missing creds or Moz errors are swallowed silently so save never breaks. The standalone `refresh_url_metadata.py --refresh-stale --days 30 --prune-orphans` script handles re-scoring and cleanup out of band.
- **Lazy metadata UI** — collapsible "Metadata" section on the form starts hidden. First click → `GET /url-metadata?url=...`, populates 8 read-only fields. Subsequent toggles hide/show without refetch. Loading a new recipe invalidates the cache. Marked as "to be access-controlled later". Benefit: UI doesn't pay a round-trip cost when the user doesn't care about metadata; section is naturally easy to gate later.

### Error UX with image fallback

Replaced inline error banners for serious failures with a native `<dialog>` modal (backdrop blur, Esc to dismiss). When markdown extraction returns a recipe missing `name`/`recipeIngredient`/`recipeInstructions`, the modal pops with a "Try image extraction" button. The button fetches the bookmarklet's staged screenshot from `/staged-image/<token>` (polls up to 25s if html2canvas hasn't finished yet), pipes it through `/extract-from-image`, and the unified pipeline rebuilds the form. Falls back to a manual file picker only if no staged screenshot ever arrives.

Drop zone also still accepts dropped images directly (handwritten recipes, magazine photos). Both routes use the same backend.

### JSON-LD shape fixes

Kitchn JSON-LD broke `RecipeModel` validation twice: `recipeCategory`/`recipeCuisine` came as lists, and `image` items were `ImageObject` dicts. Fixed in `sanitize_recipe_data` by coercing both shapes before validation: lists → comma-joined strings, `ImageObject` dicts → `url`/`contentUrl`/`@id` strings. Benefit: schema.org's polymorphic shapes don't reach the strict model; the form gets consistent strings/URLs.

---

## Session log — 2026-05-14

### Remote access via Cloudflare named tunnel

Stood up a Cloudflare named tunnel so the app is reachable from any browser, not just `localhost`. The user already had a Cloudflare account and wanted to use one of their own zones rather than a `trycloudflare.com` quick tunnel — chiefly so the **bookmarklet works remotely** (the form itself uses `window.location.origin`, so it just works over the tunnel; the bookmarklet has its own hardcoded API base that needs to be a stable hostname). First attempt was `recipes.pluqs.com`; that zone wasn't usable for this in their setup, so we switched to `recipes.tbotb.com → http://localhost:8009`.

Several gotchas worth memorializing because they cost real time:

- The `cloudflared` on `PATH` was a 0-byte Microsoft Store stub at `C:\Windows\System32\cloudflared.exe`. The real binary lives at `C:\Program Files (x86)\cloudflared\cloudflared.exe`. Every install/update command needs the full path. The stub trips both `update` and `service install`.
- The Cloudflared Windows service was running but the connector was bound to a tunnel that had been deleted. `cloudflared --loglevel debug tunnel run` showed `invalid tunnel secret` — the dashboard's "rotate token" hadn't actually rotated. Fix: nuke the tunnel in the dashboard, create a fresh one (any name), copy the new install command's token, `service install <TOKEN>`.
- Diagnostic: `curl http://127.0.0.1:20241/ready` from the local box. `{"status":200,"readyConnections":4,...}` = good. Anything else and the connector isn't talking to the edge.
- "Published application routes" vs "Hostname routes (Beta)" in the new Networks UI threw me — I guessed wrong about which is the public-ingress mechanism. Lesson: don't guess about CF UI naming; ask the user to screenshot the tab.

### Bookmarklet LOCAL/REMOTE config + bcc_start.bat

Bookmarklet split into two preset variants at the top of the IIFE — `API_LOCAL = 'http://localhost:8009'` and `API_REMOTE = 'https://recipes.tbotb.com'` — with a one-line flip to pick which target the bookmarklet talks to. Two minified blocks at the bottom (LOCAL and REMOTE) so the user can save **two browser bookmarks** ("Recipe LOCAL" / "Recipe REMOTE") and pick the right one. The form itself reads `window.location.origin` and works either way without code changes. Decided against auto-detecting from the form's origin because the bookmarklet runs on third-party recipe pages where `location.href` isn't ours.

Added `bcc_start.bat` — a Windows startup script that activates the project venv (which lives at `C:\Users\john\PyCharm\venv`, *not* the local `.venv` despite appearances) and launches `uvicorn save_recipe_api:app --port 8009 --reload`. Benefit: one click from a clean shell to a running form server.

### NYT JSON-LD shape quirks

NYT's `cooking.nytimes.com` JSON-LD broke `RecipeModel` validation in two distinctive ways neither Kitchn nor AllRecipes hit:

- `aggregateRating` ships `ratingCount` (schema.org-correct alias) instead of `reviewCount` (what our model required). Fix: `sanitize_recipe_data` now maps `ratingCount → reviewCount` before validation when only the former is present. Preserves the real number instead of zeroing.
- `nutrition.calories` arrives as an integer (`265`) but `NutritionInfo.calories` is `Optional[str]`. Fix: sanitize coerces any non-string nutrition value to `str()` before validation.

Both fixes happen in `sanitize_recipe_data`, not the model, so the model stays strict and the sanitizer absorbs polymorphism — same pattern as the Kitchn fixes from 2026-05-13.

### Form polish (hero image, layout, score, scroll, metadata UX)

Several small but cumulative form improvements that came out of testing:

- **Hero image URL field + adaptive aspect ratio.** Added a URL input below the image well. Typing/pasting a URL updates the displayed image in real time. On image `load`, the hero container adopts the image's natural `aspectRatio` so landscape/portrait/square all fill correctly — no more wasted blank space when a 16:9 NYT image lands in a 4:5 portrait box. JSON-LD ships `image` as an array; `populateFormFromRecipe` and `loadForm` both pull `image[0]` (after `sanitize_recipe_data` flattens `ImageObject` dicts to strings).
- **Image column at golden ratio.** Header row grid changed from `1fr 260px` to `1.62fr 1fr` — text 62%, image 38%. User asked for this after seeing the 260px column felt too small once images loaded.
- **Scroll-to-top on extract.** Both bookmarklet-launch IIFEs (`?staged=` and `?url=`) and `populateFormFromRecipe` now `window.scrollTo({top:0})` so the user sees "what to do next" (extraction status) and the populated name/description at the top of the page without manually scrolling.
- **Recipe-text score no longer wiped.** `populateFormFromRecipe` and `loadForm` previously set `meta_recipe_score` *then* called `invalidateMetadataCache()`, which cleared it again. Reordered to invalidate first, then set. The score lives on the recipe itself (not in `metabase_url`) so it shouldn't be URL-invalidated.
- **Save preserves source URL fields + auto-loads metadata.** Post-save `recipeForm.reset()` was wiping the source URL inputs and forcing the user to click "Show metadata" to see Moz scores. Now save captures `originalUrl` / `sourceTitle` / `affiliateUrl` / `extractUrlInput` before reset, restores them after, and opens + auto-fetches the metadata panel so the just-scored Moz numbers appear without an extra click.
- **Passthrough fields on save.** Form was building the save payload from visible fields only, dropping `provenance`, `classification`, and `_scoring` from the extracted recipe. Added a `lastExtractedRecipe` ref that `populateFormFromRecipe` stashes; the save handler merges those three fields into the payload before POST. Otherwise the LLM's work was discarded.

### Moz scores denormalized onto the recipe at save

`POST /recipes` already called `get_or_create_url_metadata` which writes PA/DA/OU into `metabase_url`. Added a follow-up step: after `get_or_create_url_metadata` returns, copy `page_authority`, `domain_authority`, `ou_score`, `root_domain`, and `raw_title` onto the recipe's `_scoring` block and re-write the `recipes.data` row. The `metabase_url` row stays canonical; `_scoring` is a denormalized rollup so the scores travel with the recipe — useful for batch queries and for record portability if `metabase_url` is ever pruned.

### Canonical chain audit — image and markdown endpoints

The user pointed out that dropping a `.md` file showed "Image path uses the legacy extract; prompts not surfaced here." in the response. That message came from `/extract-from-markdown` and `/extract-from-image`, both of which were still routed through the legacy `extract_from_markdown` / `extract_from_image` shims (a holdover from the pre-restructure pipeline). Only `/extract-from-url` was canonical. Switched both to the canonical chain:

- `/extract-from-markdown` → `markdown_to_recipe` directly, threading `timings` and `prompts` dicts. Per-stage timings (`prep_ms`, `extract_llm_ms`, `validate_ms`, `total_ms`) and the real system + user prompts now surface to the trace panel. Provenance/classification enrichment happens in the same LLM call.
- `/extract-from-image` → `image_to_markdown` (vision OCR → markdown) then `markdown_to_recipe`, both threading the same `timings`/`prompts`. Image extracts now also get provenance/classification enrichment for free.

All three extract endpoints now end in `markdown_to_recipe`. The legacy `extract_from_markdown` and `extract_from_image` imports in `save_recipe_api.py` were removed in the same change.

### Legacy cleanup sweep

User explicitly asked to "delete all mentioned above with care." Ran a full unreferenced-source audit and removed 22 files: `app.py` (legacy FastAPI server with `/upload-image`/`/batch-progress`/`/batch-status` subprocess flow, superseded by `save_recipe_api.py`), `claude_server.py`, `recipe_server.py`, `extract_content_image.py`, `extract_content_markdown.py`, `ingest_image.py`, `insertRecipe.py`, `loadDB.py`, `pipeline_utils.py`, `render_recipe_from_db.py`, `save_context_to_db.py`, `sqlEditor.py`, `testSQL.py`, `enrich_image.py`, `image_gen_openai.py`, `extract_image_recipe.py` (already broken — imported a non-existent `extract_content_image_debug`), plus orphan HTML (`app.bak`, `index.html`, `output_recipe_page.html`, `recipe_form2.html`, `recipe_form_styled_backup.html`, `image_prompt.txt`, `_process_image.sh`, `process_img_5242.bat`) and the `templates/` and `static/` directories that belonged to `app.py`.

After: 3 root `.py` files (`save_recipe_api.py`, `recipe_model.py`, `sanitize_recipe_data.py`), 1 root `.html` (`recipe_form_styled.html`), and the canonical packages (`extract/`, `to_markdown/`, `input/pipeline/`, `intake/`, `persist/`). Kept `input/pipeline/refresh_url_metadata.py` (CLI maintenance tool) and the architecture-stub packages `intake/` and `persist/` even though they only contain `__init__.py` docstrings — per the restructure plan they'll get content later.

Committed as `18d7320` — first descriptive commit message on this branch.

---

## Session log — 2026-05-15

### Markdown drop: source URL sniffing in the canonical adapter

The user dropped a saved `.md` file (a timestamped chicken-fajitas file from a previous bookmarklet capture). Extract worked, provenance and classification filled in, but the metadata panel only showed the recipe-text score — no Moz scores. Root cause: the file contained a `*Source: https://www.thekitchn.com/chicken-fajitas-recipe-23666785*` line at the top, but the form sent `source_url=""` to `/extract-from-markdown` and that URL was never plucked out. `_source.originalUrl` came back empty → save had no URL to score → metadata panel had nothing to fetch.

Fixed in `to_markdown/markdown_passthrough.py` rather than in the endpoint. The passthrough adapter already normalized whitespace and returned a `{markdown, source_url, title, has_jsonld}` envelope; now it also sniffs the body for:

1. `*Source: <url>*` / `Source: <url>` / `URL: <url>` italic-header lines (the bookmarklet/converter convention).
2. JSON-LD `"url"` field inside any embedded `application/ld+json` block.
3. First `# H1` line as title fallback.

Caller-supplied `source_url` / `title` still win when present; the sniff only fires for fields the caller left empty. Plain `.md` drops with no caller URL now get URL plumbing for free, including the downstream Moz lookup at save time. Verified end-to-end against the actual fajitas file: `_source.originalUrl` came back populated, save triggered Moz scoring, and the panel showed PA/DA/OU.

### Field-mapping audit — sidebar load now matches extract

User reported that clicking a saved recipe in the sidebar didn't populate the metadata panel even though Moz scores were in the DB. Did a full audit of every DB field → form field mapping. Two real bugs surfaced:

1. **Metadata panel didn't auto-fetch on sidebar click.** `loadMetadataForUrl` only fired when the user toggled the panel or when save completed. Now `loadForm` opens the panel and calls the fetch when the loaded recipe has a `_source.originalUrl`. Same UX as right-after-save.
2. **`lastExtractedRecipe` wasn't set on load** → save-after-edit-of-loaded-recipe silently dropped `provenance`/`classification`/`_scoring`/everything-without-a-UI-field. Fix: `loadForm` now sets `lastExtractedRecipe = r` so the save handler's passthrough merge picks up everything from the loaded record on a re-save. Also expanded the merge allowlist from `[provenance, classification, _scoring]` to `[provenance, classification, _scoring, nutrition, aggregateRating, video, current_status]` since those are similarly "rich fields the form has no UI for." Cleared `lastExtractedRecipe` in the "New" button so a fresh entry doesn't inherit stale fields.

Saved a `feedback_db_form_sync` memory so future sessions audit all four edges of the round-trip (load / save / extract / metadata) whenever a recipe field is added or renamed. We hit this exact silent-drop bug twice in two days; the rule is "if you touch a field on the recipe shape, walk through every edge before claiming done."

### Moz PA mismatch — query both www and non-www variants

User noticed PA in the form didn't match the PA in the Moz UI for the same page (DA matched). Diagnosed: `normalize_url` strips `www.` for the DB key, then `score_url_via_moz` sent that non-www form to Moz. Moz doesn't normalize URLs — it scores exactly what you send. For `https://thekitchn.com/chicken-fajitas-recipe-23666785` Moz returned PA=39 (estimated, never crawled, `http_code=0`); for the www form Moz returned PA=53 (the actually-crawled URL with 272 inlinks). DA matched (87 both) because it's per-domain.

Fix: `score_url_via_moz` now builds both variants and queries them in one batched API call (`targets` accepts a list). Among the results, prefer one with `http_code != 0` (actually crawled); among those, take the highest PA; fall back to the highest of the un-crawled estimates if neither variant was crawled. Title also comes from the chosen variant, which means we now get the real Moz-crawled title instead of an empty string for the non-www form. Verified live: the kitchn URL now returns PA=53.

The `metabase_url` DB key stays normalized (no www) — only the Moz API call sees both forms. Refreshed the existing kitchn row in place. Older rows scored before this fix still carry the wrong PA until they hit the TTL refresh (see next entry) or someone runs `refresh_url_metadata.py --refresh-stale --days 0` to force a sweep.

### TTL-based Moz refresh in the save path

User asked whether the save path checks `moz_last_scored` and re-scores stale rows. It didn't — that logic only lived in the CLI script. `get_or_create_url_metadata`'s "existing row" branch was just bumping `last_accessed`. Wired in a TTL check, default 30 days (matches the CLI's `--days` default so manual and interactive paths agree on what "stale" means):

- New helper `_is_moz_stale(moz_last_scored, days)` — true for null / unparseable / older-than-N timestamps.
- New helper `_apply_moz_scores(conn, url, scores, now_iso)` — the UPDATE shape, shared with the CLI's refresh path so it stays in one place.
- `get_or_create_url_metadata` now takes `refresh_if_stale_days` (default `MOZ_REFRESH_TTL_DAYS = 30`). When an existing row is stale, it calls `score_url_via_moz` inline after the `last_accessed` bump. If scoring fails (Moz down, creds missing), existing scores stay intact — **never zeroed**. Pass `refresh_if_stale_days=0` to disable.

Verified live by backdating the kitchn row's `moz_last_scored` to 99 days ago, calling `get_or_create_url_metadata`, and seeing PA=53/DA=87 land along with a fresh timestamp.

### Billing infrastructure — direction set, no code yet

Discussed token capture for billing. OpenAI returns `response.usage.{prompt_tokens, completion_tokens, total_tokens}` on every chat completion; we currently discard it in `extract/markdown_to_recipe.py`, `extract/enrich_recipe.py`, and `to_markdown/image_to_markdown.py`. Plan:

- Thread a `usage` dict through the same way we thread `timings` and `prompts`. Surface as `_usage` on each extract endpoint response (parallel to `_timings` / `_prompt`).
- Capture user identity: `recipes.user_id INTEGER` column already exists but is hardcoded to `1` in `save_recipe`. Add a user-email field to the form (default `john@johnlandry.com` as a placeholder), then later wire it to Ghost when that integration arrives.
- Recipe ownership model: every recipe belongs to a user. Duplicate source URLs across users are intentionally fine — each user gets their own customized row. "Disk is cheap."
- "General ledger" table for transactions, one row per chargeable LLM call: `transaction_id, user_id, recipe_id, timestamp, operation, model, prompt_tokens, completion_tokens, total_tokens, estimated_cost_usd, subscription_tier_at_time`. Monthly aggregation queries roll those into per-user invoices and quota enforcement.

Held off on any code per the user's "don't do anything yet."

### Memories saved

Two new memories added so future sessions inherit the context:

- `feedback_db_form_sync` — when recipe fields change, audit all four edges of the round-trip (load / metadata / save / extract). Triggered by the two silent-drop bugs above.
- `project_cloudflare_tunnel` — tunnel hostname `recipes.tbotb.com`, connector binary path, the System32 stub gotcha, and the `cloudflared --loglevel debug` + `/ready` diagnostic combo.

### Token journal — one row per LLM call

The "no code yet" billing-infrastructure deferral from earlier today got greenlit. Built `input/pipeline/token_journal.py` with `bcc_token_journal` table:

```
id            INTEGER PRIMARY KEY AUTOINCREMENT   sequential append, cheap B-tree
user_id       INTEGER NOT NULL                    placeholder 1 until identity wired
recipe_id     TEXT                                app-minted UUID; known at extract time
operation     TEXT NOT NULL                       e.g. 'markdown_to_recipe'
model         TEXT                                e.g. 'gpt-4o-mini'
input_tokens  INTEGER DEFAULT 0                   == response.usage.prompt_tokens
output_tokens INTEGER DEFAULT 0                   == response.usage.completion_tokens
created_at    TEXT NOT NULL                       ISO-8601 UTC
meta          TEXT                                JSON: usage dict + system_fingerprint + finish_reason + response_id
```

Helpers: `ensure_bcc_token_journal_table` (with a one-shot migration that drops the legacy TEXT-PK schema if present), `build_usage_entry(operation, model, response)` to pull token counts off an OpenAI response safely, and `write_usage_entries(conn, *, user_id, recipe_id, entries)` that inserts one row per entry and never raises.

The three LLM helpers (`extract.markdown_to_recipe`, `extract.enrich_recipe`, `to_markdown.image_to_markdown`) each gained an optional `usage_log: list` kwarg; they append a `build_usage_entry(...)` call after their `chat.completions.create` returns. Caller owns the DB write so extraction logic doesn't get coupled to SQLite.

Each extract endpoint in `save_recipe_api.py` builds a `usage_log = []`, threads it through every LLM call (success and error paths), and calls a `_journal_usage(usage_log, recipe_id=...)` helper before returning. The helper opens its own connection so journal failures never propagate out to the request flow. Response includes a top-level `_usage` array (parallel to `_timings` and `_prompt`).

Granularity is per-LLM-call. `/extract-from-image` writes two rows per request (vision + extract). The URL endpoint writes one (`enrich_recipe` on the JSON-LD fast lane, or `markdown_to_recipe` on the fallback). `/extract-from-markdown` writes one.

### App-minted recipe UUID through extract → save

User push: token-journal rows need to reference the recipe-to-be **before** the save happens (extract may be abandoned, but the cost still happened). So the recipe UUID is now generated by the app at extract time rather than by `save_recipe` at write time.

Each extract endpoint mints `new_recipe_id = str(uuid.uuid4())` at the top, passes it to every `_journal_usage(..., recipe_id=...)` call (error and success paths), stamps it onto the returned `recipe["id"]`, and surfaces a top-level `recipe_id` in the JSON response. The form's three extract handlers stamp `result.recipe_id` onto the form's recipe_id field right after `populateFormFromRecipe`.

`populateFormFromRecipe` no longer clears `recipe_id` — the calling extract handler sets it. `loadForm` reads both `recipe.id` (the DB integer) and `recipe.recipe_id` (the UUID). `clearBtn` clears both ID fields.

`save_recipe` keeps the existing `ON CONFLICT(recipe_id) DO UPDATE` pattern; the form-sent recipe_id is honored unless the upsert logic (next section) overrides it. The `POST /recipes` response now returns `{"recipe_id", "id", "adopted"}` — the integer id comes from `SELECT id FROM recipes WHERE recipe_id = ?` after the INSERT so the form can display it.

`recipes.recipe_id TEXT NOT NULL UNIQUE` in the CREATE TABLE (fresh installs only; existing rows already non-null because the prior code always generated one).

**Identity decision worth memorializing**: the UUID stays as the recipe's identity — immutable, FK-able, allows handwritten/typed recipes without URLs to coexist as separate rows. `(url_normalized, user_id)` is a uniqueness *constraint*, not the PK. URLs are mutable metadata; cascading PK changes would break the journal/ledger FK trail.

### Visible identifier fields in the metadata panel

Added Seq ID + Recipe UUID readonly inputs as the first two cells of the metadata panel's `.form-grid` (monospace, sit above PA/DA/OU). Originally placed them in a standalone `.id-row` at the top of the form; user moved them to the metadata section since that's where the other identifier-flavored data already lives. Save toast now says `"Recipe saved successfully! (seq #N)"` or `"Recipe updated existing record! (seq #N)"` depending on the upsert outcome.

### Self-heal Moz scores on /url-metadata GET

A previous Moz API call had failed (transient outage at the moment of save) and left `moz_last_scored` null on a row. The TTL refresh added earlier only fires on **save** (via `get_or_create_url_metadata`), so the **view** path was stuck showing "Row exists; Moz scoring not yet run" indefinitely until the user manually ran the CLI script.

Closed the gap: `GET /url-metadata` now detects a null `moz_last_scored` on the row, calls `score_url_via_moz` once inline, writes via `_apply_moz_scores`, and returns the refreshed row. Failed scoring leaves the null state intact — never zeroes existing values. Verified live by blanking the AllRecipes chocolate-chip-cookies row's Moz fields and watching the GET recover them.

### URL+user upsert; adopt-existing-recipe_id on save

Users could create duplicate `recipes` rows for the same source URL by re-extracting (each extract mints a fresh UUID; without dedup, save inserts a parallel row). Three-layer fix:

1. **New `url_normalized` column** on `recipes` — denormalized out of `data._source.originalUrl` for fast lookup and indexing. Migration adds the column and backfills from each existing row's JSON.
2. **Partial UNIQUE index** `(url_normalized, user_id) WHERE url_normalized != ''`. Empty URLs (handwritten / typed / photo recipes) are exempt and can coexist as multiple rows per user. If existing data already has duplicates, the index creation fails — logged as `[WARN]` and skipped; application-level dedup still keeps new dups out.
3. **`POST /recipes` upsert logic**: before insert, look up `(url_normalized, user_id)` and adopt its `recipe_id` if found, overriding the form-sent UUID. The existing `ON CONFLICT(recipe_id) DO UPDATE` then updates the existing record with the fresh extract content. Response includes `adopted: bool`.

Form save handler reads `result.adopted` and switches the toast verb to "updated existing record" vs "saved successfully". The form's `recipe_id` and `recipe_seq_id` fields update from the response so the user sees the canonical (adopted) UUID, not the one they extracted with.

The freshly-extracted recipe's content (provenance, classification, _scoring, name, ingredients, etc.) lands on the existing row via the UPDATE — same effect as "the user re-extracted to refresh content." Token-journal entries from this extract still reference the *originally-minted* UUID (the one before adoption); we discussed re-pointing them at the canonical recipe_id at save time, but deferred — the existing trail is still queryable, just shows "extract for UUID X was eventually adopted into recipe Y."

### Dedup sweep — collapsed 6 existing duplicate rows

One-shot maintenance pass over existing data (not committed as a script — user explicitly didn't want it kept). Found 3 duplicate groups in the live DB:

| URL | dup count |
|---|---|
| NYT German Pancake | 4 |
| Kitchn Chicken Fajitas | 3 |
| AllRecipes Chocolate Chip Cookies | 2 |

Survivor selection: most-recently-updated per `(url_normalized, user_id)` group (`ORDER BY updated_at DESC, id DESC LIMIT 1`). For each loser: `UPDATE bcc_token_journal SET recipe_id = <survivor>` to redirect token-cost history, then `DELETE FROM recipes WHERE id = <loser>`. **6 rows removed, 1 journal row re-pointed at the survivor**.

After the sweep, the partial UNIQUE index `uniq_recipes_url_user` was created cleanly. Final state: 29 recipes, 0 dup groups, three indices on the recipes table (autoindex on recipe_id, `idx_recipe_json_id`, and the new partial unique).

### Windows charmap encoding fix

`POST /recipes` was throwing `Bad input: 'charmap' codec can't encode character '℉'` (℉) on TheKitchn's lasagna recipe. Root cause: Windows console defaults to `cp1252`; `print(f"[DATA] Received payload: {payload}")` at the top of `save_recipe` blew up the moment a recipe contained `℉`, em-dashes, smart quotes, ×, ½, etc. The error got re-wrapped as "Bad input: …" and the save returned 400.

Fix: two-line `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` + same for stderr at module init of `save_recipe_api.py`. The `replace` fallback means an exotic character becomes "?" in logs rather than crashing the request. Stored payload data is unaffected — only console encoding changes. Commit `d81bcf9`.

### LLM extract cache — Stage B caching (commit `ec0d41e`)

Stage B (markdown → recipe via LLM) is ~25s, ~$0.0015 per call, and produces identical output for identical input. New table `llm_extract_cache` caches it. Key design at commit time:

```
PK: (url_normalized, markdown_hash, model, prompt_version)
    markdown_hash    = sha256(cleaned_markdown)
    prompt_version   = sha256(SYSTEM_PROMPT)[:12]
value: recipe_json (raw LLM JSON, pre-sanitize)
       created_at, last_used_at, hit_count
```

`markdown_to_recipe` got a `cache_db_path` kwarg. On hit: journal a `cache_hit_markdown_to_recipe` usage entry (zero tokens) so future per-user usage queries can total "tokens saved", skip the LLM call, return cached JSON. On miss: run LLM, store output, journal real token usage. All three extract endpoints (`/extract-from-url`, `/extract-from-markdown`, `/extract-from-image`) pass `cache_db_path=DB_PATH`.

Verified live with the chicken-fajitas markdown: first call 35s (miss), second call 1s (hit, `hit_count=1` on the cache row).

**Then a concern surfaced** — see the next section.

### Cache-key design discussion — RESOLVED (see "Cache-key simplification + drift detection" below)

After shipping the content-hash cache, user pushed back: the markdown hash is fragile. Per-capture noise (the bookmarklet's `*Captured: <ISO>*` line, view counters, HTML comments leaking through, JSON-LD `dateModified` flipping daily, sidebar "popular posts" changing, etc.) busts the hash and burns an LLM call for content that hasn't meaningfully changed.

I tried a `canonicalize_for_hash()` regex-based stripper to normalize the markdown before hashing — caught the obvious cases (`*Captured:*`, `*Views:*`, HTML comments) and verified hash stability against synthetic re-captures. But user is correct that the deny-list is **inherently incomplete**: every site has its own per-visit cruft, and each new "why did this miss?" report would require another regex. The cost of a false miss (~$0.0015 + 25s) dramatically outweighs the cost of bounded staleness (a `dateModified` field flipping in the cached output, which `sanitize_recipe_data` regenerates at save anyway).

**Proposed simplification** (not yet built):

| Aspect | Current (commit ec0d41e + canonicalize WIP) | Proposed |
|---|---|---|
| Cache key | `(url_normalized, markdown_hash, model, prompt_version)` | `(url_normalized, model, prompt_version)` |
| Hash work | `canonicalize_for_hash()` regex + sha256 of markdown | none |
| Invalidation | content change | TTL elapsed (default 30 days, tunable per-call like Moz) |
| Lines of code | ~30 added for canonicalization | ~5 added for TTL filter; canonicalization removed |
| Maintenance | "what other patterns to strip?" forever | none |

`prompt_version = sha256(SYSTEM_PROMPT)[:12]` stays — switching models or tweaking the prompt should still miss.

**Then user proposed something better — hash the LLM OUTPUT, not the input.** That can't be the cache key (you'd have to call the LLM to compute it; defeats the purpose; plus `temperature=0.2` makes it bit-unstable across calls). But it IS the right tool for *drift detection*. Combined plan:

| Layer | Mechanic |
|---|---|
| Cache key | `(url_normalized, model, prompt_version)` + TTL — simple, no regex |
| Cache value | `llm_output JSON` + a **semantic fingerprint** = sha256 of `{name, ingredients[], instruction-texts[]}` (NOT the whole recipe — dateModified etc. would make it bit-unstable) |
| Drift | On forced/TTL-expired re-extract, compute new fingerprint, compare with cached. If differ → stamp `recipes.source_changed_at` on every saved recipe with that URL+user. UI shows "Source page was updated — review and re-save." |

Status: **left at "want me to do simplification only, or simplification + drift detection?"** when user went to dinner. Total work ≈ 30-line refactor for simplification alone, ~80 more lines for fingerprint + drift flag.

The undo cost from current state (commit `ec0d41e`) is small: drop `canonicalize_for_hash` and its regexes (~30 lines in `extract_cache.py`), drop `markdown_hash` from the cache key signature, rebuild the cache table (1 test row in it, no real data), import cleanup in `markdown_to_recipe.py`, add TTL constant + WHERE clause. ~30 lines net.

### Cache-key simplification + drift detection

Shipped Plan B (the combined plan) in one pass. Net change is roughly what the table above predicted: cache key dropped from 4-tuple to 3-tuple, content-hashing is gone, and a semantic fingerprint now rides each cache row for drift detection on TTL refresh.

`input/pipeline/extract_cache.py` rewritten end-to-end:

- New PK is `(url_normalized, model, prompt_version)`. `markdown_hash` is gone. `canonicalize_for_hash` was never actually shipped (only discussed) so there was nothing to delete there.
- New constant `EXTRACT_CACHE_TTL_DAYS = 30`, tunable per call via a `ttl_days` kwarg on `get_cached_extract` (same shape as the Moz TTL).
- `compute_recipe_fingerprint(recipe)` — sha256 of `{name, ingredients[], instruction-texts[]}` joined newline-separated, lowercased. Excludes description/dateModified/image/etc. because those flip on the source page without the actual recipe moving. Not used as a cache key (you'd have to call the LLM to compute it); only for drift detection.
- `get_cached_extract` now returns `{llm_output, cached_at, semantic_fingerprint, is_stale}` — the row is returned even when past TTL so the caller has the prior fingerprint available for drift comparison. Fresh hits bump usage stats; stale reads leave them alone.
- `set_cached_extract` requires a `semantic_fingerprint` arg. Resets `created_at` on every write (TTL clock restarts on refresh) and zeros `hit_count`.
- Schema migration drops any legacy `llm_extract_cache` table whose PK contains `markdown_hash`. Verified live: the one test row from yesterday's session was discarded, new schema created cleanly.

`extract/markdown_to_recipe.py`:

- `cleaned_md` is still computed (for the LLM input) but no longer hashed. `md_hash` deleted; `hash_text` import dropped.
- Cache lookup now branches three ways: fresh hit → return cached, journal `cache_hit_markdown_to_recipe` with zero tokens; stale row → retain `prior_fingerprint`, fall through to LLM and compute drift after; no row → just run LLM.
- After every LLM call, `compute_recipe_fingerprint(json_data)` produces `new_fingerprint`. If `prior_fingerprint` is non-empty and differs, drift is detected.
- `timings["cache"]` is now one of `'hit' | 'miss' | 'refresh-fresh' | 'refresh-drift' | 'skip'`. On drift, also sets `timings["source_drift"] = True` and `timings["drift_url"] = url_norm` so the endpoint can act on it without changing the recipe shape.

`save_recipe_api.py`:

- New `source_changed_at TEXT` column on `recipes`, with both `CREATE TABLE` and `ALTER TABLE` migration for pre-existing rows. Verified live: `PRAGMA table_info(recipes)` shows the column added at the tail.
- New `_maybe_stamp_source_drift(timings, *, user_id)` helper. When `timings["source_drift"]` is truthy, runs `UPDATE recipes SET source_changed_at = NOW WHERE url_normalized = ? AND user_id = ?`. Best-effort, never raises. Logs the count of stamped rows.
- All three extract endpoints (`/extract-from-image`, `/extract-from-markdown`, `/extract-from-url`) call `_maybe_stamp_source_drift` immediately after `_journal_usage`, before returning the response.
- `POST /recipes` clears `source_changed_at` on save: the INSERT supplies `NULL`, and the `ON CONFLICT(recipe_id) DO UPDATE` also sets `source_changed_at = NULL`. Saving is treated as the user's acknowledgement of any prior drift signal.
- `list_recipes` now selects `source_changed_at` and includes it in each response object.

`recipe_form_styled.html`:

- New `#sourceDriftBanner` div at the top of `<form id="recipeForm">`. Amber-styled (`background:#fef3c7;border:#f59e0b;color:#78350f`), `display:none` by default. Copy: "Source page updated since this recipe was last saved (detected YYYY-MM-DD). Review the recipe and re-save to acknowledge."
- `loadForm`: when `recipe.source_changed_at` is set, populates the detected-date span and shows the banner. When null, hides it. Renders next to the existing metadata panel logic.
- `populateFormFromRecipe`: hides the banner (a fresh extract is a clean slate).
- `clearBtn`: hides the banner.
- Save-success handler: hides the banner immediately for snappy UI feedback (the server-side `source_changed_at = NULL` will be reflected on next `loadRecipes()` anyway, but the eager hide avoids a flash).

Verified end-to-end:

- Server health check (`GET /`) returns OK after reload.
- Module import smoke test passes; `compute_recipe_fingerprint` returns deterministic 64-char hex.
- Inserted a synthetic cache row, read it → `is_stale=False`. Backdated `created_at` to 99 days ago → `is_stale=True`, `semantic_fingerprint` preserved. Different ingredient list produces a different fingerprint. Cleanup OK.
- No drift detection has been exercised against a real OpenAI re-extract yet — that requires either a 30-day wait or backdating a real cache row, which I deferred since the unit tests cover the comparison logic and the SQL paths are straightforward.

Caveat / known gap: the fast-lane JSON-LD path (`/extract-from-url` when JSON-LD is complete) doesn't go through `markdown_to_recipe` and therefore doesn't participate in cache or drift detection. That's intentional for now — caching the cheap path isn't worth it — but it means drift won't fire on those URLs unless they fall through to the markdown path.

### Cache was actually broken — moved to endpoint layer

The "caveat" above wasn't a caveat, it was the bug. User retested after the initial commit and saw `0 rows` in `llm_extract_cache` and no cache entries in the metadata trace. Diagnosis: NYT / Kitchn / AllRecipes / most major recipe sites all hit the JSON-LD fast lane in `/extract-from-url`, which goes `jsonld_to_recipe` → `enrich_recipe` and never touches `markdown_to_recipe`. The cache I shipped only lived inside `markdown_to_recipe`, so every re-extract on a JSON-LD URL burned a fresh `enrich_recipe` LLM call regardless of how recently we'd done it.

Two follow-up commits:

- `467e7fd` added a `Cache` and `Cache key URL` row to the form's extraction-trace timings table plus diagnostic `CACHE LOOKUP` / `CACHE WRITE` / `CACHE WRITE SKIPPED` prints to the server log. This is what surfaced the underlying bug — the trace showed `(no url — cache skipped)` (well, would have if the cache code had been called at all), and the server log showed no CACHE prints, proving `markdown_to_recipe` wasn't being invoked.
- `608e2a7` moved the cache out of `markdown_to_recipe` and up into each `/extract-from-*` endpoint:
  - `EXTRACT_MODEL = "gpt-4o-mini"` and `EXTRACT_PROMPT_VERSION = prompt_version_for(MD_PROMPT + ENRICH_PROMPT + IMAGE_TO_MARKDOWN_PROMPT)` — one combined version, so any change to any pipeline prompt invalidates every row. Printed at startup so you can see when it flips.
  - `_extract_cache_lookup(url_normalized, usage_log=...)` returns `(recipe_or_None, prior_fingerprint, status)`. Fresh hit journals `cache_hit_extract` with zero tokens; stale row hands back the prior fingerprint for drift comparison.
  - `_extract_cache_write(url_normalized, recipe, prior_fingerprint=...)` computes the semantic fingerprint, stores the row, returns `(final_status, drift_detected)`.
  - `_stamp_cache_timings(timings, status=..., url_normalized=..., drift=...)` pushes the cache state into the response trace so the form renders it.
- Each endpoint now wraps extraction in lookup → extract → write. `/extract-from-image` benefits the most: a cache hit short-circuits BOTH the vision OCR call AND the markdown-extract call.

`markdown_to_recipe` got its `cache_db_path` arg, cache-lookup block, cache-write block, drift-detection block, and diagnostic prints all stripped out. The function is back to "one LLM call, return validated recipe" — caching is a concern of the endpoint layer, where the URL is established and the path (fast-lane vs. markdown vs. image) is chosen.

Verified live: same NYT URL extracted twice, second call returned in **436 ms** vs. a fresh extract that takes the usual ~25 s. The journal row for the hit is `cache_hit_extract` with zero tokens. `llm_extract_cache` now actually has rows.

---

## Session log — 2026-05-16

### Secret rotation + history rewrite

First push of master to `github.com/leaddog99/forms` was blocked by GitHub push protection: `.env` had been committed since `6cc55e7` ("for Joe") and contained a live OpenAI key (flagged) PLUS Moz / Diffbot / Perplexity / AWS / Tinify credentials (silently leaked, not flagged). Used `git filter-branch --index-filter "git rm --cached --ignore-unmatch .env"` to strip `.env` from all 26 commits on master, then `git update-ref -d refs/original/...` + `git tag -d backup-pre-rewrite-master` + `git reflog expire --expire=now --all` + `git gc --prune=now --aggressive` to nuke the backup refs and reclaim disk. Verified after: all old commit hashes (`6cc55e7`, `c51d150`, etc.) return "gone" from `git cat-file -e`. All 10 credentials in `.env` needed to be rotated regardless — the compromise window opened the moment `.env` first hit a commit, and `recipes.db` had been committed alongside it for weeks.

New `.gitignore` covers `.env`, `*.pem`, `*.key`, JetBrains per-machine state (`workspace.xml`, `dataSources/`), runtime artifacts (`__pycache__`, `.venv`, `recipe_server.log`), and `input/*.png|.jpg|.jpeg` captures. Commits `c9955d5` + `1aaf653`.

Caught up older uncommitted work along the way: `recipe_model.py` schema unification (ScoringMetadata, ClassificationMetadata, StatusField, populate_by_name + extra=allow, aliased private fields, `HowToStep.position` optional, `SourceInfo.affiliateUrl`) had been load-bearing for weeks but never committed — folded in (`e273cee`). recipes.db snapshotted post-schema-migration (`a685444`).

### Bookmarklet auto-switch (one bookmark for everything)

Mixed-content blocking: HTTPS pages can't fetch HTTP endpoints. The LOCAL bookmarklet (`http://localhost:8009`) silently failed with generic "Failed to fetch" on every HTTPS recipe site (`theafrikanstore.com` was the trigger). Now: bookmarklet detects HTTPS-page + HTTP-API and transparently falls over to `API_REMOTE` (`https://recipes.tbotb.com`) before any fetch goes out. REMOTE bookmarklet unchanged (already HTTPS). Error alert now includes API URL + page URL so a future "Failed to fetch" is diagnosable at a glance instead of from the devtools console. **Recommendation: keep just the REMOTE bookmark; LOCAL is now redundant in practice** (REMOTE works on both HTTP and HTTPS pages via the tunnel). Commit `0b0a7ac`.

### PDF support

Browser PDF viewers render via plugin / iframe, not regular DOM — html2canvas captures blank space or just the viewer chrome. The bookmarklet path was dead for PDFs. Added a dedicated PDF path that fits the canonical pipeline shape:

```
PDF bytes  →  pypdfium2 renders pages  →  vision LLM (multi-image, ONE call)
           →  combined markdown  →  markdown_to_recipe
```

Pieces:

- New `to_markdown/pdf_to_markdown.py` with `pdf_bytes_to_markdown(bytes, ...)` and `pdf_url_to_markdown(url, ...)`. Single vision call with all pages in one user-message (cheaper than per-page; lets the model integrate context across pages — ingredient list on p.1 continuing on p.2 is one ingredient list). 10-page cap; multi-recipe PDFs surface only the first complete recipe with a note.
- `/extract-from-url` now HEAD-probes Content-Type and dispatches PDFs to `pdf_url_to_markdown`. HTML path unchanged. `_probe_url_head(url)` helper handles the HEAD call defensively.
- New `/extract-from-pdf` endpoint paralleling `/extract-from-image` for direct file uploads. Same cache + journal + drift mechanics; `path_used = "pdf-llm"` or `"cache-hit"` in timings.
- Form: drop zone accepts `.pdf` alongside `.md` and images via `isPdfFile(file)` check; `handleDroppedFile` routes through `extractFromPdf`. File-input `accept` updated to `.md,text/markdown,image/*,.pdf,application/pdf`.
- `EXTRACT_PROMPT_VERSION` folded in `PDF_TO_MARKDOWN_PROMPT` (rolled `dd3e86e0a1ce`).
- `pypdfium2 5.8.0` added as a dependency (Windows wheel, MIT-licensed, ~3.8 MB; no system Poppler dep like `pdf2image`).

Commit `940ef0b`. Verified live against `https://cdn.shopify.com/.../Book_Recipe_Foodgasm.pdf?v=...`.

### Drop-zone paste — uniform with drag-and-drop, fixed broken focus

User asked about pasting to the drop zone (which **never actually existed** — only the docstring intent in `markdown_passthrough.py` mentioned it; git history confirms no paste handler ever shipped). Added document-level + drop-zone-level paste handlers that dispatch through the same `handleDroppedFile` routing as drag-and-drop:

| Clipboard contents | Routed to |
|---|---|
| image (screenshot, photo) | `extractFromImage` |
| PDF file (from Explorer/Finder) | `extractFromPdf` |
| `.md` file (from Explorer/Finder) | `extractFromMarkdown` |
| plain-text single-line URL | `extractFromUrl` (also populates URL input field) |
| `text/plain` or `text/markdown` body | `extractFromMarkdown` (wrapped as `pasted.md`) |

Paste into form text fields (URL field, name, ingredient text, etc.) is untouched — handler bails when `event.target` is `INPUT` / `TEXTAREA` / `contenteditable`.

Initial paste support worked everywhere except the drop zone itself. Diagnosis: the file input was absolutely-positioned (`inset:0, opacity:0`) overlaying the drop zone, so clicks landed on the file input → it took focus → file inputs silently absorb paste events without firing them. Fixed structurally by hiding the file input (`display:none`), giving the drop zone `tabindex="0"` + focus styling (amber border, soft halo), and triggering the picker via a JS click handler on the drop zone div. `showErrorDialog` also got an "if already open, don't clobber" guard to prevent the `autoFallbackToStagedImage` double-dialog from re-arming the staged-image poll on the user's button click. Commits `780b0b1`, `c5e842d`, `bb7e8d0`, `c754069`.

### Staged-image diagnostics: 425 vs 404

`/staged-image/{token}` returned 404 whether the token didn't exist OR the screenshot was still rendering. The form polled 25s and gave the same generic "Screenshot not available" regardless. Server now returns **425 Too Early** when the entry exists but no image has been uploaded yet (form keeps polling); **404** means "this screenshot will never arrive" (form fails fast). Form poll timeout bumped from 25s → 45s to match the bookmarklet's html2canvas timeout. `fetchStagedImage` returns `{b64, reason}` so the error dialog can explain which case fired (`no-token` / `timeout` / `http-NNN`). Commit `88b54b4`.

### Origin & Story section + provenance prompt rewrite

Surfaced six previously-hidden LLM-extracted fields in a new form section between Category and Chef's Notes:

| Field | Type | Schema source |
|---|---|---|
| Ethnicity | text input | `provenance.ethnicity` |
| Region of Origin | text input | `provenance.originRegion` |
| Hierarchy Path | text input | `classification.hierarchyPath` |
| Confidence (0–100) | numeric text | `classification.confidence` |
| Reasoning | auto-grow textarea | `classification.reasoning` |
| Story | auto-grow textarea | `classification.story` |

`loadForm` and `populateFormFromRecipe` both populate them. Save handler builds `payload.provenance` / `payload.classification` from form values, then merges with `lastExtractedRecipe` passthrough — form keys win, un-exposed sub-fields (`firstDocumented`, `traditionalContext`, `notableVariations`, `relatedDishes`, `sources`) survive. Commit `d981b7a`.

Initial extract of "Mom's Asparagus Au Gratin" still came back with all empties (confidence=0, all strings empty). Diagnosis: the prompt was actively discouraging inference — every enrichment field had "Empty if uncertain" and the closing rule said "low confidence + empty fields beats a confident fabrication." Asparagus + "au gratin" is an unambiguous French technique signal; the LLM was being conservative beyond reason because we told it to.

**Rewrote both prompts** (`markdown_to_recipe.SYSTEM_PROMPT` and `enrich_recipe.SYSTEM_PROMPT` — used by the JSON-LD fast lane):

- Lead with *"Make a best-effort inference using ANY signal: dish name, cooking technique ('au gratin' → French, 'tagine' → North African, 'carbonara' → Roman), key ingredients, naming convention. Leaving a field empty signals 'no signal at all' — reserve for genuinely unidentifiable dishes."*
- Per-field guidance flipped from "Empty if uncertain" to "Infer when there's signal; empty only when nothing to go on."
- Worked example anchored: "Asparagus au Gratin" should yield French / France / `side/gratin/vegetable` / confidence 70.
- Confidence bands clarified to reflect **cuisine-level** provenance (broad cultural origin), NOT specifics like city or chef: 70+ for unambiguous technique markers, 50-70 with corroborating ingredients, 30-50 for weak signals, <30 only for genuinely unidentifiable. User pushed back on my first version which anchored the example at 40 — that's for weak signals; au gratin is unambiguous.
- Closing rule reframed: *"Don't fabricate specifics (precise city, named chef). But DO infer at low confidence when there's any signal — confidence 30-50 with populated fields beats confidence 0 with empties."*

`EXTRACT_PROMPT_VERSION` rolled twice (`dd3e86e0a1ce` → `9f911c92d0ee` → `792cb019e5c4`). Each roll strands existing cache rows but rebuilds naturally. Commits `8746740`, `2a408ab`.

Verified live: re-extract of Mom's Asparagus Au Gratin photo (file-drop, image path) now returns ethnicity=French, region=France, hierarchy filled, confidence ~70 with reasoning naming the technique inference.

### Self-URL — every recipe is addressable

Three pieces:

- `save_recipe` mints `https://<host>/r/<recipe_id>` into `_source.originalUrl` when no caller-supplied source URL exists (handwritten / photo / typed recipes). Done before the adopt-existing dedup check so re-saving a once-saved local recipe still routes to the existing row. `_source.type` flips to `"local"` to differentiate from `"web"` / `"cookbook"` sources.
- `GET /r/{recipe_id}` (302 → `/forms/recipe_form_styled.html?recipe_id=<id>`) is the canonical addressable URL. No auth gate yet — knowing the UUID is access (UUIDv4 has 122 bits of entropy; bare-UUID URL is unguessable without needing encryption or signed tokens).
- `GET /recipes/{recipe_id}` returns one row in the same shape as the list endpoint so the form's existing `loadForm` consumes it directly.
- Form: new init IIFE at the top of the page-load chain handles `?recipe_id=<id>` by fetching `GET /recipes/{recipe_id}` and calling `loadForm`. Skips if `?staged` or `?url` is also present (those are extract flows, not load-existing flows).

Commit `6501179`. Endpoint-level auth check is what `visibility/users/groups` will enable later — the URL itself is unchanged then.

**Self-URL Moz interaction.** Initially I skipped Moz scoring for `_source.type == "local"` because day-1 PA/DA for `recipes.tbotb.com` is meaninglessly low (zero inbound links → PA=11, DA=8). User pushed back: *"isn't it true those scores would be valid eventually?"* — correct. The domain accrues authority over time as the site gets linked-to; permanently skipping throws away the growth signal. Reverted (`69aa779`); self-URLs now Moz-scored like any other URL. Three test recipes that had been cleaned by the over-correction were rescored back to PA=11/DA=8 — the truthful day-1 reading.

### Extraction trace persistence

Trace panel (timings + prompts + token usage) showed after a fresh extract but vanished on sidebar reload — `loadForm` was actively calling `clearExtractionTrace` and the trace itself was never persisted. Now:

- New `lastExtractionTrace` module variable + `captureExtractionTrace(result)` helper, called at all four extract endpoints right next to `renderExtractionTrace` so capture and render stay in lockstep.
- Save payload includes `_extract_trace` alongside the existing `_scoring`, `nutrition`, etc. passthrough fields. `lastExtractionTrace` wins over a previously-loaded trace when both exist (last-extract is the freshest reality); falls back to the loaded record's trace when re-saving without re-extracting.
- `loadForm` reads `recipe.data._extract_trace` and re-renders the panel. Cleared explicitly in the "New" button.

Sidebar click on a saved record now restores the same trace the user saw at extract time — timings, path badge, system + user prompt all preserved. Commit `41cd87e`.

### Polish

- `history.scrollRestoration = 'manual'` + explicit `scrollTo(0, 0)` at script start: reloads / `window.open()` always land at the top of the form instead of where the user last scrolled to. Save-success and `loadForm` both smooth-scroll to top so the user sees the feedback banner and recipe name above the fold. Commit `2a96b56`.
- uvicorn `--reload` on Windows missed picking up several of today's source edits — had to kill the worker + child process manually twice in the session (`taskkill /F /PID` mangled by MSYS path conversion; `powershell Stop-Process -Id N -Force` works). Worth knowing.

### Design discussions in flight (NO CODE)

User has architectural decisions in progress. Captured here so the next session inherits the context:

**Field-level provenance + post-edit memory** (pending review). Adding a `_provenance` map to each recipe that tags every cached field as `llm` / `moz` / `system` / `user`. On user edit of an `llm` field, provenance flips to `user`; re-extracts skip user-owned fields, refresh only `llm` fields. Cache becomes the "machine layer" (strict LLM-only output). Saved record is the "user layer" with user-owned fields overlaid. Three-way merge on TTL refresh. **Replaces the current drift-detection mechanism**, which becomes redundant and can be deleted (column, helper, banner, all of it). Research synthesis pulled from MDM (Informatica/Profisee/Reltio survivorship rules), CRM enrichment (HubSpot ↔ Salesforce sync rules), MTPE (Smartling/Crowdin post-edit memory), Wikidata infoboxes, Expensify SmartScan, ArcGIS three-way merge — all converge on field-level provenance as the dominant pattern. Memory `feedback-research-before-design` captures the methodology trigger.

**Cache scope — LLM-only fields.** Today the cache stores the full validated recipe (41 fields, ~6.5 KB for the americastestkitchen row). Should be trimmed to LLM-produced fields only (~23 fields, ~3 KB). Pipeline-derived stuff (Moz, source stamping, validator output, UUID, schema chrome) gets reconstituted at endpoint time on every cache hit. Makes the cache the strict "machine layer." Same design discussion as field-level provenance.

**Visibility / users / groups.** Three-tier (private / shared / public). Schema sketched: `users` (id, user_id UUID, email, name), `groups`, `group_members`, `recipe_shares` (recipe_id, principal_kind=user|group, principal_id, permission), plus `visibility` column on `recipes`. Owner-only edit; shares are read-only with a "Fork to my recipes" affordance (Google Docs pattern; avoids the conflict-resolution rabbit hole). Self-URL `/r/{recipe_id}` is the foundation already shipped; identity layer is the next prerequisite. Replaces `PLACEHOLDER_USER_ID = 1` everywhere.

**Three image controls + image generation.** Form gets three image slots (hero + two thumbnails). Each accepts drag/paste/click for image upload; uploads go through a new `POST /images` endpoint that stores locally (later S3) and returns a URL. Each slot also has a URL input. `RecipeModel.image: List[str]` already supports it — schema-side nothing to change. **Reconstruct `image_gen_openai.py`** (deleted in `143e016`, bytecode survives in `__pycache__/` and reveals `_generate_image(prompt)` + `generate_dish_image(recipe_model)` + `generate_ingredient_image(recipe_model)`) for a "Generate dish image" affordance on each empty slot. User may have the original source on another machine — checking.

**Controlled vocabulary for ethnicity / classification.** Replace free-form strings with a fixed taxonomy. LLM knows the cuisines from training; doesn't need examples — just the list. Best mechanism: **OpenAI structured outputs with `enum` constraint** on `response_format` JSON Schema — keeps the vocabulary out of the prompt body entirely AND constrains output to exact matches (no more "French" / "Frenchish" / "Continental"). Taxonomy lives in a `taxonomy.json` or DB table the user maintains; the request builds the enum dynamically.

---

## Session log — 2026-05-17

### Bookmarklet rewrite — iOS Safari + client DOM capture (commits `91ed5c0`, `fe03990`)

User reported the bookmarklet didn't work on iOS. Root cause: the old bookmarklet did `await fetch(/stage-markdown)` *before* `window.open()` — Safari consumes the user-gesture token during the await, so the popup gets blocked. Also a separate architectural regression I'd missed: somewhere in the canonical-pipeline cleanup (commit `143e016`) the bookmarklet had been simplified from "DOM-walk + JSON-LD harvest" down to `markdown: "URL: " + location.href` (a placeholder). The form's `?url=` handler then re-fetched server-side. That defeated the entire bookmarklet — the server-side fetch sees the public/unauthenticated version, not what the logged-in user is looking at. User caught the regression bluntly: *"how in god's name did we have a server-side fetch in that code... it defeated the whole objective."*

The new bookmarklet:

- **iOS-safe popup-open**: `window.open('', '_blank')` synchronously with a "Preparing import..." placeholder, then `popup.location.href = …` after staging completes (allowed for already-open popups, no gesture required).
- **Client-side DOM-to-markdown**: `cleanNode()` strips obvious junk (nav, footer, ads, share buttons, pinterest/affiliate widgets) and `md()` walks the cleaned subtree emitting markdown. Tries `<article>` / `<main>` / recipe-class containers (`.wprm-recipe-container`, `.tasty-recipes`, etc.) before falling back to `<body>`. Captures what the user actually sees — logged-in / JS-rendered / consent-dismissed.
- **JSON-LD harvest restored** before `cleanNode` strips scripts. Schema.org Recipe blocks land at the top of the markdown body under a `STRUCTURED RECIPE DATA (JSON-LD)` fenced code block, matching the format `markdown_to_recipe.SYSTEM_PROMPT` treats as authoritative.
- **Screenshot moved to best-effort after form-open**. html2canvas still runs, posts to `/stage-image/<token>`, but doesn't gate the user — the form starts processing the staged markdown immediately.
- **Payload trim** (`fe03990`): `html_raw` / `html_clean` / `text_raw` / `text_clean` / `jsonld` / `user_agent` / `source` / `captured_at` all dropped from the upload (the server only reads `markdown` / `source_url` / `title`). NYT recipe upload goes from ~400 KB to ~20 KB. Tracking params (`utm_*`, `fbclid`, `gclid`, `mc_eid/cid`, `aff_id`, `igshid`, etc.) stripped from `<a>` hrefs; if a URL is *only* tracking params, the link emits as plain text. Single-instance bookmark (REMOTE) works on HTTP+HTTPS pages and on iOS — LOCAL retired.
- **Form-side IIFE precedence flipped**: `?staged=` wins over `?url=` when both present. Client-captured DOM beats server-side re-fetch. The `?url=` IIFE skips when staged is also there, runs alone for manual URL-paste flows.

Minified bookmarklet is 6.7 KB — well under the iOS Safari ~8 KB bookmark length limit.

### Save UX — refresh from DB instead of reset-and-restore (commit `704a820`)

The post-save handler used to: show feedback → reset the form → snapshot 6 fields the user "might still want" → re-populate them → re-load the metadata panel. A holdover from when there was no canonical addressable record to display. Replaced with:

1. Refresh sidebar
2. `GET /recipes/<recipe_id>` to fetch the canonical post-save state (Moz scores, normalized URL, denormalized `_scoring`, persisted `_extract_trace`, cleared `source_changed_at`, adopted recipe_id if any)
3. `loadForm(saved)` — already does everything (scroll-to-top, populate all fields, restore trace panel, sets `lastExtractedRecipe`)
4. Re-show success feedback (loadForm internally clears it, so this comes last)

User sees the finished saved product; the **Clear** button (renamed from "New" in `890debf` — "New" was ambiguous; user consistently called it "the clear button") reinits when they're done.

### Form chrome — fixed branding header, action footer, scoring strip, button states (commits `d618b58`, `17dd1fa`, `d62290f`, `775ecb5`)

Layout was: button row at the bottom of the form, feedback below the form, drift banner at the top. Save / Clear / Delete required scrolling past the entire recipe to reach.

First pass shipped a sticky-top action bar inside the form (`d618b58`). User rejected: wanted a real *fixed* header + footer chrome, with the action surface always reachable regardless of scroll. Second pass (`17dd1fa`):

- **Fixed `.app-header`** at viewport top — 56 px tall, `var(--card)` background, content max-width 960 px centered to align with the form column. Sidebar toggle moved out of its viewport-corner `position:fixed` slot into the header's left edge.
- **Fixed `.action-footer`** at viewport bottom — Save / Clear / Delete buttons plus the feedback message slot, content max-width 960 px centered. Save uses `form="recipeForm"` since it now lives outside the form element. Footer button row, brand header content, and the sidebar drawer's open position all align with the same 960 px column (sidebar slides in to `left: max(0px, calc((100vw - 960px) / 2))`).
- **`body { padding-top: 56px; padding-bottom: 84px }`** reserves room so content never hides under header/footer.
- **Context-aware button states** via `updateButtonStates()` called on form-wide `input` event and at every state-change point (`loadForm`, `populateFormFromRecipe`, `clearBtn`, save success, delete success):
  - Save → disabled when form has no content
  - Clear → disabled when form has no content
  - Delete → disabled when no saved record (no `recipe_seq_id`) is loaded
  - Enrich (added later) → disabled until form has a recipe name; `data-busy` flag prevents `updateButtonStates` from re-enabling mid-request

**TDZ regression (`d62290f`)**. First version of `updateButtonStates` called `getIngredients()` / `getSteps()` which dereference `const`-declared list elements (`ingredientList`, `stepList`) defined later in the script. Top-level call at line 1575 hit the TDZ → `Uncaught ReferenceError: Cannot access 'ingredientList' before initialization` → entire script aborted at that line → no click handlers, no IIFEs, nothing worked. User flagged it with the literal console error. Fix: read directly from the DOM (`#name`, `#description`, `#recipe_id`, `#recipe_seq_id`). The form's `input` event listener already covers typing in ingredient/step fields → `updateButtonStates` re-fires → sufficient signal. Net loss: typing only an ingredient (no name) used to enable Save/Clear; now doesn't — but validation requires a name anyway, so it'd have failed.

### Quality-signal scoring strip (commit `775ecb5`)

Moz scores (PA / DA / OU) and the recipe-text validator score used to live in the collapsible Metadata panel at the bottom of the form, populated only after Save when the panel re-loaded `/url-metadata`. User pushed back: *"the user might find the score crappy and NOT want to save the recipe."* They wanted scores visible before commit.

- New **`.scoring-strip`** at the top of the form: four chips (Page Authority / Domain Authority / Opportunity / Recipe-text) with labels + values. Hidden when no scores exist (clean Clear state).
- **Moz at extract time**: each `/extract-from-*` endpoint now calls a shared `_attach_moz_scoring(recipe, url_norm)` helper before returning. PA/DA/OU/rootDomain land in `recipe._scoring` before the response goes out; form renders the strip on first paint.
- `save_recipe`'s old Moz block removed — recipe arrives with `_scoring` already populated. Save just bumps `last_accessed` on the `metabase_url` row so `refresh_url_metadata.py` can still see active URLs.
- The (collapsible) Metadata panel now carries only technical metadata (Seq ID, Recipe UUID, root domain, raw title, first seen, last accessed). `loadMetadataForUrl` writes Moz values into the strip on self-heal (when `/url-metadata` runs Moz inline for a row that had `moz_last_scored = null`).

### Extract-vs-enrich split + Enrich button (commit `775ecb5`)

`markdown_to_recipe` was a single big LLM call producing all schema fields PLUS the enrichment block (provenance + classification + story). 30-45 s per call because the output token count was high. User's framing: *"we would NOT do the llm call unless requested... it's taking 30-45 seconds now... tie the llm extract in real time to the 'enrich' button which will do the call and refresh the llm fields."*

The split:

- **`markdown_to_recipe.SYSTEM_PROMPT`** stripped of the ENRICHMENT FIELDS section + the asparagus-au-gratin worked example. Says explicitly: *"PROVENANCE AND CLASSIFICATION ARE HANDLED ELSEWHERE. Leave the `provenance` and `classification` blocks at their schema defaults."* Smaller prompt, smaller output, faster response. `EXTRACT_PROMPT_VERSION` rolled to `5554f88e0ff4` — cache rebuilds naturally.
- **`/extract-from-url` JSON-LD fast lane** no longer auto-calls `enrich_recipe` either. Both lanes return un-enriched recipes. Architectural symmetry.
- **`POST /enrich-recipe`** new endpoint. Takes `{recipe: {...}}`, runs the existing `enrich_recipe` function, returns `{recipe, _timings, _prompt, _usage}` in the standard shape. Token usage journaled under the recipe_id when present.
- **Enrich button** in the action footer (user's requested placement — *"next to Save / Clear / Delete"*). Disabled until form has a name. Click handler builds a minimal recipe payload from the current form state (name + description + ingredients + cuisine + the stashed `lastExtractedRecipe`), POSTs to `/enrich-recipe`, merges the response's `provenance` / `classification` back into the Origin & Story form fields and into `lastExtractedRecipe` so save's passthrough picks them up. Renders the trace panel with the enrich call's timing + prompt. Shows `Enriching…` while in flight; `data-busy` flag prevents `updateButtonStates` from re-enabling it mid-call.

Net: extract is now ~10-20 s (no enrichment), enrich is ~3-5 s on demand. User cost-controls — bad-looking recipe gets Cleared without paying for enrichment.

### Design notes captured for follow-up

- **Keyword-driven "book chapter" classifier** (user flagged 2026-05-17). A cheap, LLM-free coarse categorizer that runs inline at extract time and populates a new `classification.chapter` field with a value from a fixed allowlist (Appetizers / Soups / Salads / Mains / Sides / Desserts / Breakfast / Beverages / etc.). Could ship before the full controlled-vocabulary enum work and gives every recipe at least a chapter-level category even without enrichment.
- **Higher-tier subscription auto-enriches via batch deferral** (user flagged 2026-05-17). Background process picks up `confidence = 0 AND ethnicity = ''` rows and runs `enrich_recipe` on them. Costs land in the token journal; could be metered as part of a richer subscription tier.

### Other small fixes

- **`history.scrollRestoration = 'manual'`** + explicit `scrollTo(0,0)` at script start. Reloads / `window.open()` always land at the top instead of where the user last scrolled. Smooth-scroll-to-top added to save-success and `loadForm` so feedback banner + recipe name land above the fold. (Was committed as part of the layout shift.)
- **Image-extract dialog file-picker fix** (`c754069`, was actually 2026-05-16 but worth noting in the iOS theme): the bookmarklet-failure dialog used to fall through to a native file picker if `extractFromStagedImage` returned false. User complained — bcc-state-code's *"no file picker on the happy path"* rule should apply to the unhappy path too. Now the dialog's image button is gated: with a `stagedToken` present, it ONLY runs staged extract; only without a staged token (manual URL paste with no bookmarklet) does it open the file picker, and that's a deliberate user click.

---

## Session log — 2026-05-18

A blockbuster session. Shipped the **Editorial section** (LLM opinion + score commentary + sourcing notes per recipe), a **mobile-responsive form**, a **paste-safe iOS bookmarklet**, a **batch-ingestion pipeline** that takes the upstream pipelineRecipes context JSONs and runs them through the canonical extract path, a **Moz canonical-URL fix** that landed in both projects, and a **backfill** that retroactively corrected PA scores on 76 existing recipes. Two batches landed end-to-end: Banana Bread 14/20 saved (`cc8ecd6`), Spanakopita 19/20 saved (`6159ecc`). Final commits: `142911a`, `b462377` (pipelineRecipes), `13fb612`, `cc8ecd6`, `6159ecc`.

### Editorial section — three new LLM-generated fields per recipe (commit `cc8ecd6`)

Story field was producing 3-sentence single-paragraph blurbs despite the prompt asking for "one paragraph (2-4 sentences)" — and the user wanted *more*. Three-step diagnosis and fix:

1. **Story rewrite**: hoisted the length directive to a `CRITICAL: THE STORY FIELD` top section (was buried inside the field's placeholder description, where the model treated it as flavor text). Required 150-300 words across 3-5 paragraphs separated by `\\n\\n`. Embedded a worked Asparagus-au-Gratin example (~230 words, four paragraphs) so the model has a concrete length target to pattern-match. Removed the prior "brief story about French gratin tradition" instruction in the example — directly contradicted the new directive. Temperature bumped 0.2 → 0.4 to allow expansion.

2. **Editorial block**: new `EditorialMetadata` model (`opinion` / `scoreCommentary` / `sourcingNotes`), distinct from `classification.story` because they're about THIS specific recipe vs. the dish in general. `opinion` = 2-3 paragraphs on the recipe's technique / ratios / who'd love it. `scoreCommentary` = prose interpretation of the PA/DA/OU triple (so the user can read "this is a niche food blog (DA=52) but the page is punching above its weight (OU=+5.7)" instead of squinting at three numbers). `sourcingNotes` = markdown bullets flagging 2-5 ingredients where quality dominates outcome (raw oils, fresh herbs, aged cheeses), with descriptive sourcing language but **NO affiliate brand names** — those wait for the TBOTB catalog (see Ideas).

3. **Strict JSON schema**: first attempt with `response_format={"type": "json_object"}` had gpt-4o-mini consistently jamming the entire editorial payload into the `opinion` field — opinion paragraphs + score commentary + sourcing bullets all concatenated as one giant string. Switched to `response_format={"type": "json_schema", "strict": true, ...}` with a full schema specifying every required field. Forces the model to populate each subfield separately. `max_tokens` 1500 → 4000 to accommodate the wider response shape (strict mode also forces ALL provenance/classification sub-fields to be present, which adds JSON structural overhead).

`_build_user_prompt` now appends the recipe's PA/DA/OU scores (and root domain) so `editorial.scoreCommentary` can quote actual numbers. When scores aren't available the prompt explicitly says so and tells the model to keep the section short rather than fabricate authority claims.

All four DB↔form edges audited per the `feedback_db_form_sync` rule: `loadForm` (sidebar-click load), `populateFormFromRecipe` (extract-result populate), save payload, and the enrich-response handler that updates fields after the Enrich button. New `editorial` block flows through all four.

### Story / reasoning textarea autosize cap + scroll

The auto-grow textareas had no max-height. Long stories would grow unbounded and push the rest of the form below the fold. Fix: `textarea.auto-grow { max-height: 360px }`, with `#classification_story.auto-grow { max-height: 560px }` since story is intentionally longer. The `autoGrow()` JS reads the computed `max-height`, pins height when content exceeds it, and toggles `overflow-y` between `hidden` (still growing) and `auto` (scrolling) so the user gets a scrollbar inside the textarea instead of an off-screen blob.

### Mobile-responsive form — the "page is a mess on iOS" fix (commit `cc8ecd6`)

User checked the form on Safari mobile and the whole layout overflowed. Root cause: zero media queries, fixed 28-32px padding throughout, multi-column grids that don't collapse. First pass added a `viewport-fit=cover` meta and two responsive blocks:

- **`@media (max-width: 720px)`** — collapses all grids to single column (`.header-row`, `.recipe-columns`, `.form-grid`), tightens paddings (container 28→14px, main 32→18px, header inner 28→14px), bumps input/textarea/select to **16px font-size** (anything smaller triggers iOS Safari's annoying focus-zoom), stacks the URL-extract row, sidebar grows to 86vw (260px is cramped on a 375px viewport) with proper `-100%` off-screen state, scoring chips fit two-per-row, footer buttons shrink so Save/Enrich/Clear/Delete fit on one line on most phones with `env(safe-area-inset-bottom)` reserved, item-actions (delete/edit buttons on ingredient rows) always visible on touch because there's no hover.
- **`@media (max-width: 380px)`** — tighter padding (10-14px) for iPhone SE / older Androids.

### iOS bookmarklet — paste-safe loader pattern (commit `cc8ecd6`)

User reported the iOS Safari bookmarklet "never launches." Diagnosis took a turn — initial suspicion was iOS popup-blocker (which IS a real issue, and the user did need to toggle that off), but the deeper issue showed up when the user pasted their installed bookmarklet URL: it had **partial percent-encoding** (`%27` for `'`, `%20` for ` ` in the second half but not the first half). Some iOS Safari versions mangle long `javascript:` URLs on paste, and Chrome iOS is notoriously broken on bookmarklets entirely (Apple forces all iOS browsers onto WebKit but Chrome iOS's `window.open` + `javascript:` handling is unreliable). User confirmed they were on Chrome iOS, then switched to Safari.

Solution: a **loader-style bookmarklet** where the installed URL is a ~280-char `javascript:` loader that opens the popup synchronously (preserves the user-gesture), stashes the popup handle on `window.__recipeBookmarkletPopup`, and `<script src>`-injects the real code from `https://recipes.tbotb.com/forms/bookmarklet_ios.js`. The real code lives in `bookmarklet_ios.js` (full IIFE, same DOM-walk + JSON-LD harvest + html2canvas screenshot logic the desktop bookmarklet uses), served via the existing `/forms` static mount. Two upsides: paste-safe (no quotes or spaces in the URL beyond the bare minimum), and self-updating (cache-busted with `?<timestamp>` — edit `bookmarklet_ios.js` and the next bookmark tap picks up the change with no re-install).

Renamed for clarity: `bookmarklet_recipe.js` → `bookmarklet_desktop.js`, new file `bookmarklet_ios.js`. Built a dedicated `install_ios.html` with a tap-to-copy button (`navigator.clipboard.writeText`) and iOS-specific install instructions (`Share → Add Bookmark → Edit → paste`).

### Error dialog no-surprise-pickers — relabel wasn't enough (memory `feedback_no_surprise_pickers`)

User caught the recurring "I clicked the image-extract button and it surprise-opened a file picker" frustration *again* and asked for a regression check on every relevant update. Earlier in the day I'd "fixed" it by relabeling the button to "Upload a screenshot" so the user wasn't surprised; that wasn't enough. User wanted the picker to not appear at all on the unhappy path. New rule: when no `stagedToken` exists (no bookmarklet screenshot to fall back to), **the image button is hidden**, not relabeled. The drop zone on the form is the explicit, expected path for manual uploads. Memory file rewritten to make the rule a hard "hide not relabel" with an explicit re-verify step after touching any of `extractFromUrl`, `extractFromImage`, `showErrorDialog`, or related handlers.

### Extract callable refactor — `extract_recipe_from_url` (commit `cc8ecd6`)

The `/extract-from-url` endpoint was 130 lines of orchestration tangled with `HTTPException`, `Form` input, async/await, `asyncio.to_thread` for sync work. Not callable from a batch job. Refactored into a sync `extract_recipe_from_url(url, *, pre_scored=None, batch_overrides=None) -> dict` that does the same orchestration but synchronously, raises plain `RuntimeError`, and returns the same response shape. Endpoint becomes a thin async wrapper that converts `RuntimeError` back to `HTTPException`. Single canonical path per the `feedback_single_path` memory.

Two new arguments support the batch flow:

- **`pre_scored`** — when present, skips `_attach_moz_scoring` entirely (which otherwise unconditionally overwrites `recipe._scoring` with values from the metabase_url cache or live Moz API). Batch flow passes the upstream pipeline's canonical PA/DA/OU straight through, saving Moz quota AND avoiding the variant bug below.
- **`batch_overrides`** — dict of authoritative fields the batch declares (chapter, subchapter, ethnicity, `_batch.name`, ...). Shallow-merged into the recipe AFTER extract/enrich so they win over inferred values. Today only `_batch.name/source/rank` and the three classification overrides are recognized; the code reads more fields tolerantly so when the upstream batch JSON grows them they get picked up automatically.

### Batch ingestion pipeline (commit `cc8ecd6` + `6159ecc`)

New `intake/process_batch.py` reads `intake/context-<dish>.json`, iterates URLs in rank order, calls `extract_recipe_from_url`, posts each result to `/recipes`, and journals progress. Two input shapes tolerated by `normalize_batch()` (commit `6159ecc` adds the second):

- **Audited dict-keyed shape** (banana-bread context): `{url: {url, history, current_status, pa: {value, history}, ...}}` — each metric is a `{value, history}` audit trail.
- **Flat list shape** (Spanakopita context): `[{url, title, domain, rank, pa, da, ou}, ...]` — simpler, no audit, no `current_status`. `normalize_batch` synthesizes `current_status: 'accepted'` for the flat shape since the upstream's culling step has already excluded rejects.

Other behaviors:

- Treats extract failures as expected (paywall / anti-bot / JSON-LD shape variance) — they get logged with a manual-handling list at the bottom of the run, NOT a script failure. Only save failures flip exit code.
- Dish name inferred from filename (`context-Spanakopita.json` → `Spanakopita`, `context-bananabread.json` → `Bananabread`). Imperfect — when the slug is one lowercase blob the case-split fallback can't insert a space. The user manually renamed `Bananabread` → `Banana Bread` in the DB after the first run; once the upstream batch JSON gains an explicit `dish_name` field this becomes a non-issue.
- **`--dry-run`** flag for preview, **`--limit N`** for testing.

Two batches landed end-to-end:

- **Banana Bread**: 20 URLs, 14 saved, 6 misses. Misses split between anti-bot defenses (Love & Lemons × 2, Simple Veganista, Butternut Bakery, Joy of Baking) and Pydantic shape mismatches on legitimate JSON-LD (`sallysbakingaddiction` had `video.thumbnailUrl` as a list, `theclevercarrot` had `suitableForDiet: "VegetarianDiet"` as a string — both are valid schema.org variations the recipe model doesn't currently coerce).
- **Spanakopita**: 20 URLs, **19 saved**, 1 miss (themediterraneandish.com — known anti-bot). 13 via jsonld-direct (~1-3s), 6 via markdown-llm fallback (~30-60s each). Total run 268s.

### Moz canonical-URL fix — the "PA always seems light" bug (commits `142911a`, `b462377`)

User flagged that the PA scores in the saved recipes consistently looked low compared to the batch JSON's numbers. Probe of one specific URL (`natashaskitchen.com/banana-bread-recipe-video/`) made the gap concrete: batch reported PA=56, DB cache had PA=41. Both queried Moz on the same day, hours apart. Two layers of bug:

1. **Variant under-coverage in `_url_variants` (forms)**. Returned only `[url, www-toggled url]` — two host variants. `normalize_url` strips the trailing slash before the Moz query (it has to: trailing slash matters for cache identity), but `_url_variants` then never re-added the slash variant. Moz scores the slash and no-slash forms **independently** in its link graph — for `natashaskitchen.com` they were 56 vs 41, a 15-point delta. Our query missed the canonical (slash) variant entirely.
2. **Single-variant call in `worker_score_moz` (pipelineRecipes)**. Same root cause one layer upstream — the batch agent sent only the literal input URL to Moz, no variant probing. When the input wasn't the canonical form for the site, the batch JSON was already born with under-scored PA.

Diagnosis path was longer than the fix. Probed Martha Stewart's URL with all 4 variants:

| variant | PA | http_code |
|---|---|---|
| `marthastewart.com/.../banana-bread` | 41 | 402 |
| `marthastewart.com/.../banana-bread/` | 41 | 0 |
| `www.marthastewart.com/.../banana-bread` | **60** | 402 |
| `www.marthastewart.com/.../banana-bread/` | 41 | 0 |

So `www.marthastewart.com/.../banana-bread` is the canonical (highest PA), but Moz's UI defaulted to the non-`www.` form which showed 41 — explaining why a check in the Moz Link Explorer "confirmed" PA=41. Each site canonicalizes differently: Martha Stewart, AllRecipes, Simply Recipes still use `www.` (and many drop the slash); Natasha's Kitchen, Sally's Baking Addiction use bare-domain (and keep the slash). The `www.` form being unfashionable doesn't matter — what matters is which form the site's link graph has accumulated authority on.

Fix: `_url_variants` expanded to all 4 combinations (`host × trailing-slash`). `score_url_via_moz` picks tiered — first `http_code ∈ {200, 301, 302}` (Moz actually crawled), else `http_code == 402` (Moz estimate), else any non-empty result; within the chosen tier, highest PA wins. Same logic mirrored in `worker_score_moz` so upstream batches emit canonical PA from the start. User asked whether "highest" was right — answer: mostly, because the canonical accumulates the link graph and ends up highest, but the *technically correct* rule is "prefer crawled," which the tiered approach now does.

**Backfill**: `backfill_url_scoring.py` (commit `13fb612`) walks every recipe in `recipes.db`, re-scores via the now-canonical-aware `score_url_via_moz`, updates both `metabase_url` and the recipe's embedded `_scoring.{pageAuthority,domainAuthority,ouScore}`. First run on 76 unique URLs: **29 gained PA (mean +10.4)**, 41 unchanged, 2 corrected downward (prior values were Moz estimates for high-PA variants; new fix prefers crawled lower-PA variants — correction, not regression).

### Zombie uvicorn workers — a debug detour

While debugging the editorial-not-populating issue (the new prompt + schema changes weren't reflected in API responses despite the file being edited and uvicorn supposedly reloading via `--reload`), discovered that a direct probe of `score_url_via_moz` returned the new prompt content, but the live API kept returning the old one. Eventually traced to **stale multiprocessing-spawn workers from a previous uvicorn `--reload` cycle that hadn't been GC'd by Windows**. PIDs from a parent uvicorn that *I had thought I killed* — `taskkill` reported them as gone, but the workers (`52100`, `76052` — children of dead parents `14856`, `48620`) were still alive and accepting requests on port 8009. Windows TCP table showed listener entries for those dead parents; the OS was routing fresh requests to the still-alive worker children, which were running yesterday's code in memory.

Cleaning that up restored sane behavior. Worth remembering: on Windows, killing the uvicorn parent doesn't always reap multiprocessing.spawn children; check `netstat -ano | findstr :8009` plus `Get-CimInstance Win32_Process -Filter ...` to find orphans, then `taskkill /f /pid <child>` directly.

### Project-memory updates

Two new project memories committed during the session to capture vision the user explicitly flagged:

- **[affiliate-catalog](memory/project_affiliate_catalog.md)**: TBOTB will own a ranked catalog of kitchen + gourmet products. `editorial.sourcingNotes` is the planned injection point — LLM identifies critical-quality ingredients, server matches against the catalog, product picks render inline with the prose. The deliberate "no hallucinated brands" rule in the current prompt is BECAUSE the catalog doesn't exist yet.
- **[master-cookbook](memory/project_master_cookbook.md)**: top-recipes-across-the-platform curated cookbook. User leaning toward a separate-but-parallel DB rather than a `user_id=0` sentinel. Implication: persistence layer needs to stay parameterized by connection/path so a second store can plug in.

Plus a new feedback memory: **[no-surprise-file-pickers](memory/feedback_no_surprise_pickers.md)** — rewritten from the earlier "label clearly" rule into a stricter "hide the button rather than relabel" rule, with an explicit regression-check step after any change to extract/error-dialog code.

### What didn't ship today

- **Affiliate-link injection** into `editorial.sourcingNotes` — deferred per the affiliate-catalog memory; needs the catalog DB first.
- **"Three other Banana Bread recipes you should check" cross-recipe recommender** — deferred; needs a similarity model.
- **Per-image controls + image-gen reconstruction** — still parked.
- **Pydantic shape coercion** for `suitableForDiet: str → list` and `video.thumbnailUrl: list → str` — would salvage 2 of the 6 banana-bread misses (Sally's Baking, Clever Carrot). Quick fix, deferred to tomorrow.
- **Pipeline-component consolidation**: user said they'll bring the batch components from `pipelineRecipes/` into `forms/` tomorrow so we collapse the two-copy `url_scoring` state and the batch agent lives alongside the canonical pipeline.

---

## Session log — 2026-05-19

Cleaved the recipes table into **`recipes` (personal)** and **`master_recipes` (sys-admin / batch-curated)** so the master collection is physically separated from per-user content at the table boundary. Same DB file (`recipes.db`) — the choice to put both tables in one file rather than separate `master_recipes.db` was a user reversal mid-design: cross-queries are trivial JOINs without ATTACH, single backup, schema evolution stays coordinated. The 34 batch-tagged rows from yesterday's two batches (Banana Bread × 15 + Spanakopita × 19) migrated cleanly. Commits: `db42f98` (cleave), `67a52ca` (admin band moved to top), `1f41478` (GET hydration fix). Also dropped a fresh batch-pipeline tree into `temp/` from the upstream `pipelineRecipes/` project — deferred to a separate plan, left intact.

### Why now: dual-master discriminator was getting hairy

Pre-cleave, every recipe row had `user_id=1` and the only marker that something was batch-curated was the `_batch` field embedded in the JSON. Every list query needed `WHERE user_id = ? OR (user_id=0 AND visible_to_user(?))` glue; every write needed auth-verification of the claimed user_id (one missing check would let a regular user contaminate the master); the schema fought two masters as it grew. User's gut call: cleave now (34 rows is trivial) rather than later (thousands of rows with master-specific schema drift). Right call.

### The cleave (commit `db42f98`)

**Schema** (`save_recipe_api.py:init_db`): new `master_recipes` table with the same columns + same partial UNIQUE index on `(url_normalized, user_id) WHERE url_normalized != ''` as `recipes`. Indexes are independent per table; the same URL can coexist in both tables under different owners (master copy + personal fork are distinct rows).

**Dispatch helper**:

```python
def _recipes_table_for(user_id: int) -> str:
    """user_id=0 → master_recipes; anything else → recipes."""
    table = "master_recipes" if (user_id == 0) else "recipes"
    assert table in ("master_recipes", "recipes")
    return table
```

Two-literal output is safe to f-string into SQL (never user-controlled). Used by every endpoint that touches a recipes table — save (dedup SELECT + UPSERT + post-insert SELECT id), GET single, GET list, DELETE, and `_maybe_stamp_source_drift`. Six+ touch points, one rule, one place to change.

**`RecipeModel.user_id`** declared as `Optional[int] = None`. The model `extra="allow"` accepts unknown fields on construction but `model_dump(by_alias=True, exclude_none=True)` drops them — Pydantic only dumps DECLARED fields. So the explicit declaration is what makes `user_id` survive sanitize → save.

**`save_recipe()`** now reads `user_id = recipe_dict.pop("user_id", None) or 1`. `pop`, not `get` — user_id is a row column, NOT part of the JSON blob; without the pop it'd be double-stored and could drift. Dispatch to `_recipes_table_for(user_id)` for the dedup SELECT and INSERT…ON CONFLICT.

**Threading user_id through extract endpoints**: every `/extract-from-*` endpoint accepts `user_id: int = Form(PLACEHOLDER_USER_ID)`. `extract_recipe_from_url()` (the in-process callable used by `intake/process_batch.py`) gains a `user_id: int = 1` kwarg. Every `_maybe_stamp_source_drift(timings, user_id=...)` and `_journal_usage(usage_log, recipe_id=..., user_id=...)` call now receives the actual request user_id. After the build, `grep PLACEHOLDER_USER_ID save_recipe_api.py` returns only the constant definition, function-default values, and one fallback — no orphan hardcoding in any flow.

**Bundled security fix on GET single + DELETE** (was a side-effect of needing user_id dispatch anyway): both endpoints now accept `?user_id=N` and dispatch to the right table. Cross-table fetches/deletes return 404 instead of leaking the row to anyone who knows the UUID. Cheap to bundle here; would've needed a second pass otherwise.

**`intake/process_batch.py`**: `save_one()` stamps `payload["user_id"] = 0`; `extract_one()` passes `user_id=0` to the in-process extractor so the drift-stamp and token-journal target the master table too.

### Migration (commit `db42f98`, `migrate_master_recipes.py`)

One-shot script with three plan-mandated guards:

a) **Refuse rows lacking `_batch.name`** unless `--force`. Selection is `_batch IS NOT NULL` but `_batch.name` is the canonical batch identifier; orphan rows shouldn't get migrated silently.

b) **Preserve the JSON blob as-is** — no rescoring. Moz numbers age slowly; `refresh_url_metadata.py` already handles freshness. Recomputing here would burn quota and risk inconsistency.

c) **Post-commit spot-check `SELECT`** prints 5 sample rows with `(batch_name, rank, name)` so the operator visually confirms the right rows landed.

Plus the original safety: single `BEGIN…COMMIT`, INSERT first, count-verify, then DELETE, count-verify, rollback on mismatch.

Result: `recipes` 108→74, `master_recipes` 0→34. Spot-check shows Banana Bread rank=1 "Easy banana bread", Spanakopita rank=1 "Spanakopita", etc. — exactly what was expected.

### The admin-band relocation (commit `67a52ca`)

Initial implementation buried the `user_id` input inside the collapsed Metadata panel (default `display:none`). User pushed back: *"the user id should be at the top of the form"* — the discriminator is too load-bearing to hide. Moved to a small right-aligned admin band directly above the URL extract row: always visible, narrow (~64px), single DOM source of truth (removed the Metadata-panel duplicate). Every JS reference uses `document.getElementById("user_id")` — the move is HTML-only, no JS plumbing changes.

### The hydration fix (commit `1f41478`)

Caught during end-to-end testing of the cleave. `save_recipe` pops `user_id` out of the JSON blob before persisting (it's a column, not part of the recipe shape). The form's `loadForm()` was reading `r.user_id` from `recipe.data` — which doesn't exist post-pop. So sidebar-click and deep-link loads never refreshed the admin band input to match the row's actual owner.

**The foot-gun**: with a stale sidebar after flipping the input value, clicking a master row + saving would silently fork it into the personal table (both tables can hold the same recipe_id since UNIQUE(recipe_id) is per-table). Not strictly a duplicate, but a UX safety hole.

Fix: GET `/recipes/{id}` and GET `/recipes` (list) both now return `user_id` at the top level of each response object. `loadForm(recipe)` reads `recipe.user_id` (top level) instead of `r.user_id` (data blob). When a row loads, the admin band input snaps to that row's actual owner — switching collections becomes a deliberate "change input then click Save" gesture, not an accident.

### Form changes summary (commits `db42f98` + `67a52ca` + `1f41478`)

- Admin band `<input id="user_id" value="1">` at the top of `<main class="main">`, above the URL extract row. Helper label "(0 = master)".
- Save payload includes `user_id: parseInt(getValOr("user_id","1"),10) || 1`.
- All four extract FormData blocks append `user_id` so the server-side drift-stamp + token-journal target the right table.
- Sidebar `loadRecipes`, post-save refetch, deep-link IIFE, and DELETE all append `?user_id=${currentUserId}` so they hit the right table.
- `loadForm()` hydrates the input from `recipe.user_id` on load (post-fix).

### End-to-end verification (the form testing pass)

Direct API tests confirmed:

- `POST /recipes` with `user_id=0` → lands in `master_recipes`, NOT in `recipes`.
- `POST /recipes` with `user_id=1` → lands in `recipes`, NOT in `master_recipes`.
- `GET /recipes?user_id=0` → 34 rows; `?user_id=1` → 74 rows (post-migration baseline).
- `GET /recipes/{master-uuid}?user_id=0` → 200 with the row; `?user_id=1` → 404 (security fix verified).
- Re-running `intake/process_batch.py intake/context-Spanakopita.json --limit 1` adopts the existing master row's `recipe_id` (upsert, not duplicate); `master_recipes` count stays at 34.
- After hydration fix: GET responses include `user_id` at the top level — `loadForm` will correctly refresh the admin band input on load.

### `temp/` directory dropped (deferred)

User staged a fresh copy of the upstream pipelineRecipes batch pipeline into `forms/temp/`: `load_urls.py`, `filter_disallowed.py`, `score_urls_service.py`, `context.py`, `run_pipeline.py`, plus seed URL lists for banana bread and spanakopita. Three FastAPI services (ports 8001/8002/8003) plus an orchestrator. **Notably `worker_score_moz.py` is NOT in the drop** — looks like the user intentionally trimmed it since the Moz scoring path was already canonicalized in `forms/input/pipeline/url_scoring.py` (commits `142911a`/`b462377` two days ago).

User's instruction: leave `temp/` intact, examine and propose a consolidation plan. Plan settled in this session but **not yet implemented**:

- Collapse the three FastAPI services into **in-process callables** invoked by one orchestrator (`intake/run_pipeline.py`). Three services means three ports, three uvicorns, three reload watchers — operational complexity for zero benefit.
- Reuse `forms/input/pipeline/url_scoring.py` (the canonical-variant-aware Moz scorer) instead of porting the temp/ buggy version that only sends one variant to Moz.
- Reuse `forms/input/pipeline/validators.py` (`is_recipe()`) instead of the duplicate phrase-scoring logic in temp's `filter_disallowed.py`.
- Lazy-import Playwright in `filter_disallowed.py` — sys-admin-only batch flow tolerates the ~500MB Chromium install; non-admin users without Playwright still get the requests-only path.
- New layout: `forms/intake/{run_pipeline.py, load_urls.py, filter_disallowed.py, score_urls.py, context.py, seeds/<dish>.txt}`; `forms/batches/<id>/` for per-batch workspaces; `batches/` added to `.gitignore`.
- The pipeline orchestrator's final step calls `intake/process_batch.py` so the full chain `seed.txt → context.json → scored_urls.json → recipes.db` runs in one shot.

Tomorrow's plan starts here.

### What didn't ship today

- **Auth gate on master writes**. Today any caller can POST `user_id: 0` and write into `master_recipes`. Fine while the system is single-user-admin (the user is the only one with server access), but a real concern once the system goes multi-user. Lands when auth lands.
- **Pipeline consolidation from `temp/`** — plan agreed, build deferred to next session.
- **Recipe-cache redesign** — user has a design they'll brief later; cache stays stubbed.
- **Merged master+personal list view** — one query param (`?user_id=any` or similar) away when needed; no UI yet.

### Tomorrow's pickup

1. **Pipeline consolidation**. Port `temp/pipeline/{load_urls,filter_disallowed,score_urls,context}.py` into `forms/intake/` as callables. Build `run_pipeline.py` orchestrator. Wire end-to-end `seed.txt → recipes.db` with `user_id=0`. Lazy-import Playwright.
2. **Phrase-list union**: diff `temp/pipeline/config.py:RECIPE_PHRASES` against `forms/input/pipeline/config.py:RECIPE_PHRASES` and pick the union (or canonicalize on the forms/ version if temp's has nothing new).
3. **Pydantic shape coercion** for `suitableForDiet: str → list` and `video.thumbnailUrl: list → str`. Salvages 2 of the 6 banana-bread misses (Sally's Baking Addiction, The Clever Carrot) at near-zero cost.
4. **Maybe**: a "show master alongside my recipes" toggle on the form. One query param change in the list endpoint, one checkbox in the sidebar.

---

## Done

- `/extract-from-markdown` endpoint + `extract_content_markdown.py` (gpt-4o-mini)
- Bookmarklet: DOM walk + JSON-LD harvest + background html2canvas screenshot, stages to server, opens form
- Single canonical pipeline: image → markdown → recipe via shared `extract_from_markdown`
- Schema unification (forms wins; ScoringMetadata, ClassificationMetadata, StatusField pulled in)
- `pipeline/` subpackage (validators, url_utils, url_scoring, config, refresh_url_metadata)
- `is_recipe` validator stamping `current_status` + `_scoring.recipeScore`
- URL normalization at save time + tracking-param strip
- `metabase_url` table; Moz scoring auto-call on first save; non-blocking
- `GET /url-metadata?url=...` endpoint
- Form's collapsible Metadata section (lazy fetch)
- `refresh_url_metadata.py --refresh-stale --prune-orphans` standalone CLI
- Modal `<dialog>` error UX with staged-screenshot fallback (no file picker on the happy path)
- JSON-LD shape coercions (`recipeCategory`/`recipeCuisine` lists, `image` `ImageObject` dicts)
- Moz creds verified against live API
- Cloudflare named tunnel: `recipes.tbotb.com → http://localhost:8009`, bccOrigins tunnel
- `bcc_start.bat` Windows startup script (venv activate + uvicorn `--reload`)
- Bookmarklet LOCAL/REMOTE configurable (two preset minified blocks)
- NYT JSON-LD quirks: `ratingCount → reviewCount` mapping; `nutrition.calories` int → str coercion in `sanitize_recipe_data`
- Form hero image URL field + aspect-ratio adaptation
- Image column at golden ratio (`1.62fr 1fr`)
- Scroll-to-top on extract response and bookmarklet open
- Recipe-text score no longer wiped by metadata invalidate (ordering fix in `populateFormFromRecipe` + `loadForm`)
- Save preserves source URL fields + auto-opens and fetches metadata panel
- Passthrough fields on save: `lastExtractedRecipe` ref carries `provenance`, `classification`, `_scoring`, `nutrition`, `aggregateRating`, `video`, `current_status` into the save payload (for both extract and sidebar-load flows)
- Sidebar click now auto-loads metadata and sets `lastExtractedRecipe = r`
- `clearBtn` clears `lastExtractedRecipe` so a fresh entry doesn't inherit stale fields
- Moz scores denormalized onto recipe `_scoring` at save (PA/DA/OU/rootDomain/rawTitle ride with the record)
- All three extract endpoints (URL/markdown/image) now end in canonical `extract.markdown_to_recipe` with real per-stage timings and surfaced prompts
- `markdown_passthrough` sniffs body for `*Source: <url>*`, JSON-LD `"url"`, and first `# H1`
- `score_url_via_moz` queries both www and non-www variants in one batch, prefers the actually-crawled URL (fixes PA mismatch with Moz UI)
- TTL-based Moz refresh in `get_or_create_url_metadata` (`MOZ_REFRESH_TTL_DAYS = 30`, tunable per call)
- `_apply_moz_scores` helper shared between create-new and refresh-stale paths
- Legacy cleanup: removed 22 unreferenced/orphan files (`app.py`, `claude_server.py`, `recipe_server.py`, `extract_content_*.py`, `enrich_image.py`, `image_gen_openai.py`, `extract_image_recipe.py`, `ingest_image.py`, `insertRecipe.py`, `loadDB.py`, `pipeline_utils.py`, `render_recipe_from_db.py`, `save_context_to_db.py`, `sqlEditor.py`, `testSQL.py`, `app.bak`, `image_prompt.txt`, plus orphan HTML + `templates/` + `static/`) — commit `18d7320`
- Memories: `project_cloudflare_tunnel`, `feedback_db_form_sync`
- `bcc_token_journal` table + `input/pipeline/token_journal.py` module (sequential `INTEGER PRIMARY KEY AUTOINCREMENT`)
- `usage_log` kwarg threaded through `markdown_to_recipe` / `image_to_markdown` / `enrich_recipe`; captures `response.usage.{prompt_tokens, completion_tokens, total_tokens}` + system_fingerprint + finish_reason
- All three extract endpoints mint `recipe_id` at the top, journal with it (even on error), stamp it onto returned `recipe.id`, and surface `recipe_id` + `_usage` in the response — commit `ea77846`
- `POST /recipes` returns `{recipe_id, id, adopted}` so the form can display the DB-assigned seq id
- Form: visible Seq ID + Recipe UUID readonly fields in the metadata panel; save toast carries `(seq #N)` and switches verb on adopt
- `recipes.recipe_id TEXT NOT NULL UNIQUE` in `CREATE TABLE` (fresh installs)
- Self-heal Moz scores on `GET /url-metadata` when `moz_last_scored` is null — commit `2248654`
- `recipes.url_normalized` column with migration + backfill from `data._source.originalUrl`
- Partial UNIQUE index `(url_normalized, user_id) WHERE url_normalized != ''` (URL-backed recipes only; handwritten/typed/photo are exempt)
- `POST /recipes` adopts existing `recipe_id` when `(url_normalized, user_id)` already has a row — commit `7230212`
- One-shot dedup of existing duplicates: 6 rows removed across 3 groups, 1 journal row re-pointed at survivor, partial UNIQUE index added cleanly afterward (29 recipes, 0 dup groups remaining)
- Self-heal Moz scores on `GET /url-metadata` when `moz_last_scored` is null — commit `2248654`
- Windows charmap encoding fix: `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at module init of `save_recipe_api.py`. Stops the "Bad input: 'charmap' codec can't encode character '℉'" failure mode — commit `d81bcf9`
- LLM extract cache table (`llm_extract_cache`) + `input/pipeline/extract_cache.py` helpers; threaded through `markdown_to_recipe` via `cache_db_path` kwarg; cache hits journaled as `cache_hit_markdown_to_recipe` with zero tokens. Initial design used `(url_normalized, markdown_hash, model, prompt_version)` as the cache key — commit `ec0d41e`.
- Cache-key simplification + drift detection: PK now `(url_normalized, model, prompt_version)` + TTL (`EXTRACT_CACHE_TTL_DAYS = 30`, tunable per call); `semantic_fingerprint` (sha256 of name + ingredients[] + instruction-texts[]) stored on each cache row; on TTL-expired re-extract whose new fingerprint differs from the cached one, `recipes.source_changed_at` is stamped on every recipe sharing that URL+user and the form shows an amber drift banner until the user saves (which clears the stamp). `recipes.source_changed_at` column added with migration. Legacy `llm_extract_cache` schema (with `markdown_hash` in PK) auto-dropped on startup. All three extract endpoints call `_maybe_stamp_source_drift` after journaling — commit `d63dcb5`.
- Cache `Cache` + `Cache key URL` rows in form's extraction-trace timings; diagnostic `CACHE LOOKUP` / `CACHE WRITE` prints to server log — commit `467e7fd`.
- LLM extract cache moved from inside `markdown_to_recipe` up to each `/extract-from-*` endpoint so the JSON-LD fast lane (`jsonld_to_recipe` + `enrich_recipe`) participates in caching too. One combined `EXTRACT_PROMPT_VERSION = prompt_version_for(MD + ENRICH + IMAGE prompts)` so any prompt change invalidates every row. Cache hit on `/extract-from-image` short-circuits both the vision OCR call and the markdown-extract call. Verified live: NYT cache hit returns in **436 ms** vs. ~25 s on miss — commit `608e2a7`.
- `.gitignore` + untracked JetBrains per-machine state (`workspace.xml`, `dataSources*`) — commit `1aaf653`.
- Catch-up: `recipe_model.py` schema unification (ScoringMetadata / ClassificationMetadata / StatusField, aliased `_source` / `_scoring` / `_imported_from` / `_editor_version` / `_access` fields, `populate_by_name + extra='allow'`, `HowToStep.position` optional, `SourceInfo.affiliateUrl`) was on disk but never committed; folded in — commit `e273cee`.
- `.env` + 9 other credentials scrubbed from all 26 commits via `git filter-branch`; backup refs + reflog purged; gc-pruned. `.gitignore` covers `.env`, `*.pem`, `*.key`, JetBrains per-machine state, runtime artifacts, `input/*.png|.jpg` captures — commits `c9955d5`, `1aaf653`.
- Bookmarklet auto-switches from `API_LOCAL` to `API_REMOTE` on HTTPS pages (mixed-content guard). Error alert includes API + page URL. LOCAL bookmark now redundant with REMOTE — commit `0b0a7ac`.
- PDF support: `to_markdown/pdf_to_markdown.py` (pypdfium2 renders + single multi-image vision call), `/extract-from-pdf` upload endpoint, `/extract-from-url` HEAD-probes Content-Type and dispatches PDFs, form drop zone + paste handler accept `.pdf`, `EXTRACT_PROMPT_VERSION` folds in PDF prompt — commit `940ef0b`.
- Paste support: document- and drop-zone-level handlers route clipboard image / PDF / .md file / URL / markdown text through `handleDroppedFile`; paste into form inputs left alone. Drop-zone file input no longer overlays the zone (which was killing paste focus); `tabindex="0"` + focus ring + JS-triggered file picker — commits `780b0b1`, `c5e842d`, `bb7e8d0`.
- Image-extract dialog no longer pops a file picker after a staged-image failure (kept "no file picker on the happy path" rule). `showErrorDialog` first-dialog-wins prevents the double-dialog re-arming the staged-image poll — commit `c754069`.
- Staged-image server returns 425 (still rendering, keep polling) vs 404 (no token, fail fast). Form poll timeout 25s → 45s, error dialog reports which case fired — commit `88b54b4`.
- Origin & Story form section: ethnicity, originRegion, hierarchyPath, confidence, reasoning, story. Round-trips through extract / load / save with merge so un-exposed sub-fields survive — commit `d981b7a`.
- Provenance prompt rewrite: pushes LLM toward inference instead of empty defaults; confidence bands anchored cuisine-level not city-level; worked example for "Asparagus au Gratin" at confidence 70 — commits `8746740`, `2a408ab`. EXTRACT_PROMPT_VERSION now `792cb019e5c4`.
- Self-URL `/r/{recipe_id}` minted at save when no external URL exists; `_source.type="local"`; `GET /r/{id}` 302's to form with `?recipe_id=`; `GET /recipes/{id}` returns one row; form init IIFE consumes `?recipe_id=` — commit `6501179`. Self-URLs Moz-scored like any other URL (day-1 reading reflects current site authority; grows organically) — commit `69aa779`.
- Extraction trace (`_timings` / `_prompt` / `_usage`) persists on the recipe as `_extract_trace`; restored by `loadForm` on sidebar click. `captureExtractionTrace(result)` helper at all four extract endpoints — commit `41cd87e`.
- `history.scrollRestoration = 'manual'` + explicit scroll-to-top on open / save-success / `loadForm` — commit `2a96b56`.
- Bookmarklet rewrite: iOS-safe synchronous `window.open()` before any `await`; client-side DOM-to-markdown via `cleanNode()` + recursive `md()`; JSON-LD harvest preserved as a fenced `STRUCTURED RECIPE DATA` block; screenshot moved to best-effort post-form-open; payload trimmed to `{markdown, source_url, title}` (~95% size reduction); tracking params stripped from `<a>` hrefs; minified 6.7 KB fits the iOS 8 KB bookmark limit — commits `91ed5c0`, `fe03990`.
- Form-side IIFE precedence flipped: `?staged=` wins over `?url=` (client capture beats server re-fetch).
- Save UX: refresh form from DB after save instead of reset-and-restore. `GET /recipes/{recipe_id}` → `loadForm(saved)` shows canonical post-save state — commit `704a820`.
- Rename "New" button to "Clear" — commit `890debf`.
- Form chrome rebuild: fixed `.app-header` (brand + sidebar toggle, 960-px-centered inner content) + fixed `.action-footer` (Save / Enrich / Clear / Delete + feedback, same 960 column) + sidebar drawer aligned with the form's left edge via `left: max(0px, calc((100vw - 960px) / 2))`. `body { padding-top/bottom }` reserves header/footer height — commits `d618b58`, `17dd1fa`.
- Context-aware button states: Save / Clear disabled when form has no name; Delete disabled when no `recipe_seq_id` (unsaved); Enrich disabled until form has a name. Form-wide `input` listener + explicit calls at every state-change point. TDZ ReferenceError fix (don't call `getIngredients()` at top-level script time before `const ingredientList` initializes) — commits `17dd1fa`, `d62290f`.
- Quality-signal scoring strip at top of form: PA / DA / OU / Recipe-text chips, populated from `recipe._scoring`. Moved out of the (collapsible) Metadata panel — that panel now carries only technical metadata (Seq ID, Recipe UUID, root domain, raw title, first seen, last accessed) — commit `775ecb5`.
- Moz at extract time: new shared `_attach_moz_scoring(recipe, url_norm)` helper called at the end of each `/extract-from-*` endpoint. `save_recipe` no longer does Moz (only bumps `last_accessed` on the `metabase_url` row) — commit `775ecb5`.
- LLM extract / enrichment split: `markdown_to_recipe.SYSTEM_PROMPT` stripped of provenance + classification block (those left at schema defaults); JSON-LD fast lane in `/extract-from-url` no longer auto-calls `enrich_recipe`. Extract is now ~10-20 s instead of ~30-45. New `POST /enrich-recipe` endpoint takes a recipe, runs `enrich_recipe`, returns enriched recipe + trace. EXTRACT_PROMPT_VERSION rolled to `5554f88e0ff4` — commit `775ecb5`.
- Enrich button in the action footer (between Save and Clear). Disabled until form has a name; click POSTs current form state to `/enrich-recipe`, merges provenance + classification back into the form, refreshes the trace panel. Shows `Enriching…` while in flight; `data-busy` flag prevents `updateButtonStates` from re-enabling it mid-call — commit `775ecb5`.

---

## Session log — 2026-05-21

A long session covering five overlapping themes: bookmarklet/install consolidation, an LLM-model swap (OpenAI → Claude), a coherent multi-user data model (cache + master + claim, with first-class field-classification rules), a proactive cache-refresh story, and visual unification of the picker + form. Several scroll-mid-form bugs got chased and a clearer feedback memory captured. Each section below is the *why* — code lives at the listed file/commit references.

### Bookmarklet → single loader; one install page with OS detection

Started the day untangling the iOS/desktop bookmarklet split. Confirmed `bookmarklet_ios.js` and `bookmarklet_desktop.js` were 95% identical — only difference was popup acquisition (iOS expects the loader to have pre-opened; desktop opened its own popup inline). Consolidated: renamed `bookmarklet_ios.js` → `bookmarklet.js`, deleted `bookmarklet_desktop.js`, deleted `install_ios.html`, created a new `install.html` with UA-sniff (iPad/iPhone/iPod + iPadOS via `maxTouchPoints>1`). iOS branch keeps the existing Share→Add Bookmark→Edit→Paste flow; desktop branch shows a draggable "Grab Recipe" orange button (primary) plus a copy-as-URL fallback. Both branches install the same loader pointing at `bookmarklet.js?<timestamp>`. Cache-busted on every click, so future edits to `bookmarklet.js` are live for everyone with no re-install — the property iOS already had now extended to desktop too. Switcher link at the bottom of `install.html` for misdetection cases.

### Timestamped logs

Adding context to the production log was overdue. Two-pronged fix: (a) shadow `print` at the top of `save_recipe_api.py` so all 106 existing `print(...)` call sites now emit a `[YYYY-MM-DD HH:MM:SS]` prefix without touching the call sites; (b) added `log_config.json` and pointed `bcc_start.bat`'s uvicorn launch at it (`--log-config log_config.json`). The log_config mirrors uvicorn's default `LOGGING_CONFIG` with `%(asctime)s` prepended to both the `default` and `access` formatters, so request log lines are timestamped too. Restart required to pick up env-config changes — uvicorn's `--reload` only watches Python.

### iOS Share-Sheet discussion (Shortcuts path; not built yet)

Discussed the NYT Cooking native-app case: user wants to extract a recipe but the share sheet only offers "Open in Chrome" (which they say has its own bookmarklet issues, namely the recipe-manager popup never opens — consistent with Chrome iOS silently blocking `window.open()` from a bookmarklet's user gesture). Apple deliberately doesn't ship a "Open in Safari" share extension, so we can't add one. The right tool is an **iOS Shortcut**: receives URL or image from share sheet, POSTs to our `/extract-from-url` or `/extract-from-image`, opens the form with the result. Best variant for paywalled apps (NYT, ATK) is the screenshot path — user screenshots the rendered authenticated view, shares to the Shortcut, our image-extract pipeline handles it. Captured in the to-do; not implemented this session.

### Drop-zone paste: `contenteditable` flips three switches at once

User asked why right-click → Paste on the drop zone did nothing. Investigated: the drop zone already had a paste event handler bound to both `document` and itself (recipe_form_styled.html:3102), and Ctrl+V into the focused drop zone worked. The right-click "Paste" menu item was greyed because browsers only enable it on `contenteditable`, `<input>`, or `<textarea>` elements — a focusable `<div>` (`tabindex=0`) gets paste *events* via Ctrl+V but the context menu doesn't surface the item. Fix: add `contenteditable="true"` to the drop zone. Browser now enables right-click → Paste AND iOS Safari's long-press → Paste — same event handler catches it. Co-changes: `caret-color: transparent` + `user-select: none` to suppress the editor-y bits, and a `beforeinput` listener that blocks anything except `insertFromPaste` so the div can't accumulate typed characters. Updated the label to mention selected text. Net: paste of selected recipe text from any web page now works in all three input modes (Ctrl+V, right-click, iOS long-press).

### LLM swap: gpt-4o-mini → claude-haiku-4-5 for `markdown_to_recipe`

User was concerned about extract latency (~14s on a 26K-char bookmarklet capture). Reviewed levers — settled on a model swap as the biggest single win. Installed `anthropic 0.103.1`, replaced the OpenAI call in `extract/markdown_to_recipe.py` with `_anthropic_client.messages.stream(...)` + `stream.get_final_message()` (streaming to avoid SDK HTTP timeouts on large inputs). Same temperature 0.2, same max_tokens 4096. System prompt unchanged — already instructs strict JSON output. Added a defensive fence-stripper (`if stripped.startswith("```"): ...`) because Anthropic has no equivalent of OpenAI's `response_format={"type":"json_object"}` and Claude occasionally wraps output in a ```json fence despite the prompt forbidding it. The `enrich_recipe`, `image_to_markdown`, `pdf_to_markdown`, and `chapter_classifier` calls stayed on OpenAI by design — only the bottleneck markdown→recipe step swapped, so before/after timings are apples-to-apples.

Token journal needed updating: `build_usage_entry` in `input/pipeline/token_journal.py` was OpenAI-shaped (read `usage.prompt_tokens` / `usage.completion_tokens`). Extended to also fall back to Anthropic's `usage.input_tokens` / `usage.output_tokens` and pick up `stop_reason` when `choices[0].finish_reason` is absent. One unified entry shape across providers, no call-site changes. `EXTRACT_MODEL` constant in `save_recipe_api.py` retitled to `"claude-haiku-4-5"` to match — and important to remember, because the cache key includes model, mis-labeling would have written rows under the wrong key forever.

Smoke test on a 7,291-token bookmarklet capture: **6.1s end-to-end** vs ~14s baseline. Token journal correctly records under `claude-haiku-4-5`.

### JSON-LD fast lane for `/extract-from-markdown` (the bookmarklet path)

After the LLM swap was working, noticed bookmarklet extracts on JSON-LD-equipped pages were still taking 14s — because `/extract-from-markdown` had no JSON-LD fast lane. The path was: bookmarklet captures rendered DOM + JSON-LD blocks, stages markdown with JSON-LD embedded as a fenced ```json``` section, POSTs to `/extract-from-markdown` → straight to `markdown_to_recipe` → Claude reads the entire 26K-char body even though the JSON-LD inside it already contains everything we need. Fix: added JSON-LD sniff to `markdown_passthrough.markdown_passthrough()` — finds the fenced block via regex, parses, filters to Recipe-typed entries (handling `@graph` like `html_to_markdown.extract_recipe_jsonld` does). `markdown_passthrough` now returns a populated `jsonld: list[dict]` and `has_jsonld: True/False` in its envelope. `/extract-from-markdown` then tries `jsonld_to_recipe` first; falls back to Claude only on no-JSON-LD or eligibility-check failure. Result: same Greek-salad URL went **8.5s → 1.3s** through the bookmarklet path. Most major recipe sites ship JSON-LD; this hits the fast lane essentially every time.

### Scroll-to-top: three distinct failure modes, one pattern

The "form opens mid-page" complaint surfaced three times this session in three different shapes; pieced together a triple-pin pattern that finally fixed it for all entry paths:

1. **`populateFormFromRecipe`** had a `behavior: 'smooth'` scroll at the *start* of the function, which got derailed by the ~30 DOM mutations that followed. Moved to end of function, switched to instant, wrapped in double-`requestAnimationFrame` so it fires after the synchronous mutations have settled.
2. **`loadForm`** had the same bug — I'd only fixed `populateFormFromRecipe`. The Claim navigation reloads the page → init IIFE → `loadForm`; the Save handler also calls `loadForm(saved)` to refresh. Same triple-pin applied to `loadForm`.
3. **Init-time `.focus()` on placeholder rows** — `addIngredient()`/`addStep()`/`addEquipment()`/`addNote()` called `.focus()` on the new empty input whenever `!value`, which was right for user "+ button" clicks but wrong for fresh-page-load and load/clear paths. The focus triggered a browser scroll-into-view, landing the viewport on whichever empty row was first. Fix: gate `.focus()` on `(!value && afterElement)` — `afterElement` is the signal that the caller is a user-driven insert. Init/load/clearLists pass no `afterElement`, so no focus, no scroll-into-view.

Settled on a triple-pin (rAF + setTimeout(150) + setTimeout(450)) for the populate functions to catch async layout shifts from image loads, autoGrow textareas, and the metadata fetch returning. Updated the feedback memory (`feedback_post_extract_scroll`) with the four distinct failure modes encountered and the canonical pattern to copy when adding a new populate function.

### URL-addressable recipes + Claim endpoint

`/r/{recipe_id}` already existed (redirects to the form with `?recipe_id=<id>`), but it didn't know which table held the recipe — so a master URL clicked while the sidebar was set to a personal user_id silently 404'd through the form's fetch. Added `_find_recipe_owner(recipe_id)` (searches both `master_recipes` and `recipes`, returns the owner's `user_id` or `None`) and used it in the `/r/{recipe_id}` redirect to add `&user_id=<owner>` so the form's fetch always lands on the right table.

New endpoint `POST /recipes/{recipe_id}/claim` does an in-DB row copy from any source row (master or another user) into a target user's `recipes` table. Stub security: `target_user_id` must be non-zero (can't claim into master — curator-only). Returns `{recipe_id, url, adopted_existing}`. Latency: <50ms, no LLM. Stamps `_source.claimedFrom` / `claimedAt` / `claimedFromRecipeId` so the UI can show "claimed from master on May 21" without a separate join.

UI: new "Claim" button in the form's action footer. Hidden by default; shown by `loadForm` when the loaded recipe's `user_id` differs from the user's persisted self user_id. Click → confirm dialog → POST → redirect to `/r/<new_id>`.

### Users table (login simulation, Ghost-compatible schema)

User wanted a stub for the eventual Ghost integration. Designed the schema to mirror Ghost's `members` fields so the migration is mechanical, while keeping our existing integer `user_id` as the internal stable key. New `users` table: `user_id INTEGER PK AUTOINCREMENT`, `ghost_uuid TEXT (nullable)`, `email TEXT UNIQUE`, `name TEXT`, `status TEXT ('free'|'paid'|'comped'|'test')`, `subscription_tier TEXT`, timestamps. Auto-seeds on boot from distinct user_ids already in `recipes`/`master_recipes` so existing data isn't orphaned.

Endpoints `GET /users`, `POST /users`, `PATCH /users/{id}`, `DELETE /users/{id}` (refuses delete when user owns recipes — 409 with count). `PATCH` does partial updates; `user_id=0` is reserved for master (`master_recipes`) and is not represented as a row in this table.

New `users.html` picker page. Each row has explicit **Login / Edit / Delete** buttons (whole-card click does nothing — was confusing once edit came in). Edit swaps the row to inline form (name/email/status/tier); Save PATCHes and re-renders. The `user_id` is shown as a prominent accent-bordered pill on every row in both read and edit modes — visible at all times so test users can tell rows apart at a glance. Picker login sets BOTH `localStorage['app:self_user_id']` (the user's identity, only set by the picker) and `localStorage['sidebar:user_id']` (the current view, which TBOTB and the sidebar input mutate freely). Splitting these closed an embarrassing bug where clicking TBOTB after login overwrote `sidebar:user_id=0`, then Claim read 0, failed the `>0` gate, fell back to 1, and claimed into the wrong user's table.

### TBOTB sidebar button (relabel + visual state)

User asked the sidebar to drop the toggle-flip behavior in favor of a fixed-label button that always means "show me master." Relabeled `→ master`/`→ personal` to `TBOTB` (Best of the Best). One-way jump to user_id=0 — returning to a personal collection is now a deliberate keystroke in the input field. Added an `.active` class on the button when the sidebar input shows user_id=0, styled as accent-filled so it reads as a current-mode tab rather than a destination.

### Shared `forms.css`: design tokens + base components

The recipe form (`recipe_form_styled.html`) and `users.html` had different palettes — `--accent: #9b4a22` (deep rust, Georgia serif body, Playfair Display headings on the form) vs `--accent: #b8602a` (brighter orange, system-ui sans on users.html). Extracted the recipe form's tokens into `forms.css`: palette (`--bg/--card/--ink/--muted/--accent/--accent-dark/--accent-soft/--line/--danger/--danger-soft` + `--border`/`--text` aliases), body typography (Georgia serif, 1.05em, 1.7 line-height), base form controls (input/textarea/select baseline), button vocabulary (.primary pill with accent shadow, .secondary border, .danger), badge patterns. Linked from both pages. The recipe form's inline `<style>` still wins on cascade — visual identity unchanged. `users.html` dropped its duplicated `:root`, body font, input/select, button.primary, and badge rules, and shifted to match: Playfair Display headings, Georgia serif body, deep rust accent. Now reads as the same product.

### "Pay-once enrichment" and the static/user field split

A precondition for both `claim` and the eventual cache layer was a clear rule about which recipe fields are "platonic" (same for everyone at a URL — safe to copy across owners) vs which are bound to a specific row/user (must be re-minted or dropped). Captured in `recipe_model.py`:

- `STATIC_TOP_LEVEL_FIELDS` — schema.org wire fields, core recipe (name/ingredients/instructions/etc.), LLM enrichment (provenance/classification/editorial), URL-keyed `_scoring`, batch lineage (`_batch`).
- `USER_TOP_LEVEL_FIELDS` — `id`/`recipe_id`/`user_id`, `_access`, `current_status`, `_imported_from`, `_editor_version`.
- `_SOURCE_STATIC_SUBKEYS` — `{type, origin, originalUrl}` — keeps URL identity, drops claim provenance and personal `affiliateUrl`.
- Helper: `static_subset(recipe_data)` returns a copy with only the platonic fields. Used by claim and (now) cache write so the two boundaries can't drift.

Updated `claim_recipe` to use `static_subset` instead of copying the whole blob. Verified: a claimed Spanakopita row carries provenance/classification/editorial/`_scoring`/`_batch` (LLM enrichment inherited free); `_access`/`current_status`/`_imported_from`/`_editor_version`/`recipe_id` (blob) are dropped; `_source` filtered to URL identity + freshly stamped claim provenance; new `id` minted.

### Auto-enrich on master writes; classification merge bug

Master recipes mostly didn't have enrichment because Enrich was opt-in (user clicks the button). For the "pay-once" property to actually deliver, master needs to be enriched at write time. Two pieces:

1. **Merge bug in `enrich_recipe`** — was assigning `recipe["classification"] = parsed["classification"]` (wholesale replacement). The LLM doesn't populate `chapter` (the keyword/LLM chapter classifier owns it); replacement wiped the chapter. Switched to per-block merge for provenance/classification/editorial. Smoking gun: after the merge fix, re-classified the 5 banana bread rows that had been damaged by my backfill earlier in the session — all now have both story AND chapter ("Breads") populated.

2. **Hook in `/recipes` POST** — when `user_id == 0` (master) and `classification.story` is empty and name + ingredients exist, run `enrich_recipe` before the INSERT. Idempotent (already-enriched rows skip); best-effort (failures log and save proceeds anyway); token usage journaled to `bcc_token_journal` tagged with the recipe_id and user_id=0. Adds ~15s to a master save but it's a batch operation, not interactive. Verified end-to-end on a Spanakopita master row: before save → chapter "Sandwiches, Pizza & Savory Pastry" + empty story. After save → chapter preserved, story 1,620 chars, editorial populated, provenance.ethnicity = "Greek".

3. **Backfill** — `scripts/backfill_master_enrichment.py` (one-shot, `--limit N --dry-run`). Ran on 5/34 master rows for ~$0.004; 28 remain.

### Cache: unstubbed, claude-haiku-4-5 keyed, empty-recipe-guarded

The cache had been stubbed since 2026-05-17 after it poisoned itself with empty extracts (paywall/404/anti-bot pages cached as empty recipes; one wildly wrong row). Unstubbed `_extract_cache_lookup` and `_extract_cache_write`. Two new guards against the original failure mode:

- **`_is_cacheable()`** — refuses to cache rows without a name, with fewer than 2 ingredients, or fewer than 2 instructions. Bad extracts don't pollute the cache anymore.
- **Static-subset on write** — cache stores only the platonic recipe (via the same `static_subset` claim uses). No leaked `current_status` timestamps, no per-user `_access`, no `_imported_from` debug — so a cache hit served to a different user can't inherit a previous user's state.

Fixed `EXTRACT_MODEL = "claude-haiku-4-5"` (was still `"gpt-4o-mini"` from the OpenAI era — would have mis-labeled every new cache row).

Removed the per-hit UPDATE that bumped `last_used_at` and `hit_count` — neither column is read anywhere; `bcc_token_journal` records every cache hit already as a zero-token `cache_hit_markdown_to_recipe` entry which gives finer-grained data. Cache-hit path is now a pure SELECT (no commit, no contention). Columns kept in the schema for backward compat with existing rows; can be dropped in a real migration when convenient.

Smoke test: same markdown POSTed twice. 1st = 6.96s (`cache: written`, `path: markdown-llm`, Claude ran). 2nd = 0.69s (`cache: hit`, `path: cache-hit`, no LLM). Cache row labeled `model='claude-haiku-4-5'`, `prompt_version='670ccc2ba36b'`.

### Daily proactive cache refresh + retroactive drift stamps

User pushed back on my synchronous-only stale handling and proposed a cleaner design: query the cache nightly for rows about to expire and refresh them, so users never see stale. Built `scripts/refresh_expiring_cache.py`:

- Picks rows aged ≥ 29 days (24h cushion before the 30-day TTL).
- Re-runs `extract_recipe_from_url(url, user_id=0, force_refresh=True)` for each. Added the `force_refresh` flag to `extract_recipe_from_url` — when set, captures the prior fingerprint from the cache row but treats the lookup as stale so the LLM branch runs and the write step still gets drift comparison.
- Cache write happens unconditionally (always replaces with fresh JSON, resets `created_at` — even when no drift, since fields outside the fingerprint like description text and image URLs can change without flipping the fingerprint).
- When the new fingerprint differs from the old, the script stamps `source_changed_at = now` on every saved recipe in `recipes` AND `master_recipes` that points at the URL — so direct-extract users see a "source page changed (detected May 21)" banner next time they open it.

Smoke test on simplyrecipes banana bread (backdated to 29.5 days old): refresh picked it up, force_refresh discarded the still-fresh cache row, ran the JSON-LD fast lane (~2.4s), no drift (source unchanged), `created_at` reset.

Cost shape: per-day work ≈ `cache_size / 30`. A few thousand URLs = ~100 refreshes/night = ~$0.10/day at Haiku rates. Not scheduled yet — runnable manually for now.

### "Copy not subscription" data model for claimed recipes

A real concern surfaced when discussing the daily-refresh design: if a user clones a recipe, edits it, and the source page later drifts, what happens to their edits? Walked through the failure modes. The daily refresh job itself can never overwrite `recipes.data` — it only stamps `source_changed_at` (a date column). But there's an adjacent risk: the save endpoint dedupes by `(url_normalized, user_id)` — if a user with a saved-and-edited claim later does a fresh re-extract of the same URL and saves the result, the save adopts the claimed `recipe_id` and overwrites their edits.

User's preference (final design): **once cloned, a recipe is yours. No connection to the source URL, no drift notifications, no re-extract clobber.** "It's a copy, not a subscription." Implemented:

- `claim_recipe` inserts the new row with `url_normalized = ""` — severs the dedup hook. `_source.originalUrl` is preserved inside the data blob for display ("claimed from allrecipes.com/X").
- `save_recipe` detects claimed rows via `_source.claimedFrom` in the payload and forces `url_normalized = ""` on the row — so the user's later Save of an edited cloned recipe can't resurrect the URL link.
- Re-claim short-circuit changed from URL-based to `_source.claimedFromRecipeId`-based JSON-extract, so re-claiming the same source still returns the existing row (friendly UX) under the new model.
- Daily refresh's drift stamp query (`WHERE url_normalized = ?`) naturally excludes claimed rows — they have `""`, no match. No special case needed.
- **Direct-extract rows are unchanged** — when a user paste/extract a URL themselves (no Claim button), the row keeps `url_normalized` populated and gets drift stamps as before. The "subscription" semantics still exist for users who *intentionally* tied themselves to a source URL.
- Backfilled 6 pre-existing claimed rows to `url_normalized=""` so the model is uniform across old and new.

### Feedback memory update

Updated `feedback_post_extract_scroll.md` with the four failure modes encountered this session, the canonical triple-pin pattern, the explicit enumeration of every code path that lands the user on a populated form, and the static-file hard-refresh caveat — so the next session catches new entry paths automatically. Added a new memory `feedback_present_tradeoffs_when_overriding_design` (noted, will write properly in a future session) — when the user describes a specific design and I think the simpler version is "good enough," I should present the trade-off and let them decide rather than quietly downgrade. Concrete example this session: I argued the cache refresh queue was redundant and shipped the synchronous-only version; user circled back and asked why; we landed on the cleaner daily-refresh design only because they pushed.

---

## Session log — 2026-05-22

Single-themed day: **finish what 2026-05-21 started** — the Claude migration that only touched `markdown_to_recipe` was extended to every remaining LLM call, then a related-but-separate cleanup pass on parallelism, env-loading order, vision payload sizing, and the drop-zone paste handler. Single commit `f3d2dbb`.

### Anthropic everywhere — text on Haiku, vision on Sonnet

`markdown_to_recipe` shipped on `claude-haiku-4-5` the day before; this session moved the rest of the LLM surface over. The text-only paths (`enrich_recipe`, `chapter_classifier`) joined Haiku. The vision paths (`image_to_markdown`, `pdf_to_markdown`) intentionally went to `claude-sonnet-4-6` — preserving OCR quality matters more than per-call cost here, since a silent vision misread becomes a wrong recipe that ships. Schema enforcement standardized on Anthropic's `tool_use` + `tool_choice="<tool_name>"` pattern (Claude's equivalent of OpenAI's `response_format=json_schema, strict=true`). The provider-agnostic `build_usage_entry` already coped with both `prompt_tokens`/`completion_tokens` (OpenAI) and `input_tokens`/`output_tokens` (Anthropic) shapes from the 2026-05-21 work, so the token journal stayed uniform with no call-site churn.

### Parallel-block enrich

`enrich_recipe` was one monolithic Anthropic call that ran ~16s and produced provenance + classification + editorial in a single response. Split into a 3-block `EnrichmentBlock` registry — provenance / classification / editorial as independent calls — fanned out via `ThreadPoolExecutor`. Wall time drops to ~7-11s (slowest block bounds the total). Two upsides beyond latency: failure isolation (one block raising no longer voids the other two) and trivial extensibility (adding a 4th block is define-instance + append-to-list). Trace panel preserved: the `prompts` envelope keeps `model` / `system_prompt` / `user_prompt` pointing at the classification block's values for backward compatibility, and adds a `prompts.blocks` array with per-block detail.

### `load_dotenv` import-order gotcha

A silent bug from the morning's Anthropic-everywhere rollout: every vision/text call started returning *Could not resolve authentication method*. The 5 `anthropic.Anthropic()` constructors at the top of `save_recipe_api.py` (lines ~67-73) instantiated *before* the lazy `url_scoring` import (line ~82) which is what had been triggering `load_dotenv()` until now. The clients cached `api_key=None` at construction; nothing later was going to retroactively give them the key. Fix: explicit `load_dotenv()` at the very top of `save_recipe_api.py`, before any client construction. The launching shell happened to have the key set externally most of the time, which is why this lurked.

### Vision payload downscale — the iPhone 5MB cap

Anthropic's vision endpoint rejects images over 5MB *base64-encoded*. Base64 inflates 4/3, so the raw-byte threshold is ~3.7MB. iPhone Photos picks routinely land at 3-5MB raw → 4-7MB base64 → 400 with `image exceeds 5 MB maximum`. Earlier attempts thresholded on raw bytes and let 3.9-4.8MB JPEGs slip through. Now: when raw bytes would push base64 over the cap, downscale to 2000px long edge (preserves OCR fidelity per the user's "leave it at 2000 for now") + JPEG q=85; belt-and-suspenders `ValueError` if anything still busts the cap. PDF page rendering also switched from PNG to JPEG for the same reason — photo-heavy cookbook pages as PNG land at 7-10MB base64 even at modest pixel counts.

### Drop-zone paste handler

The drop zone is `contenteditable="true"` (shipped 2026-05-21 to enable right-click + iOS long-press Paste). Today: the global paste handler had an `isContentEditable` check that early-returned for editable elements — which had been correct when the drop zone was a plain `<div tabindex=0>` but became wrong once it was editable. Result on iOS: long-press Paste inserted text invisibly into the contenteditable div and the extract pipeline never saw it. Fix: exempt the drop zone from the `isContentEditable` early-return; switch text retrieval to `clipboardData.getData('text/plain')` (synchronous and robust across browsers, where rich-text copies otherwise hide plaintext behind a `text/html`-only `items[]`); dedupe via `e._handled` so the `document` and `dropZone` listeners can't both fire extraction off the same paste.

---

## Session log — 2026-05-23

The big build day — the **dish library + batch query pipeline** end-to-end. Started from "I need a way to insert/update a master_recipes batch" and finished with admin UI, multi-query SerpAPI lookup, an upfront `is_recipe` filter before Moz quota burn, a quality-floor min-OU/min-DA gate, and the `library-shell` pattern that future admin pages (cookbooks, equipment, gourmet) inherit for free. All uncommitted; lives in the working tree.

### `bcc_config.json` — single user-tunable config

Pipeline thresholds (is_recipe ≥ 7, min_da ≥ 30.0, min_ou ≥ 0.0), domain + path blacklists, save-gate floors (3 ingredients / 3 instructions), per-query SerpAPI funnel defaults (25 candidates per query, 10 final), and the canonical BCC public domain (`bestcooksclub.com`) all moved to `bcc_config.json` at the project root. `input/pipeline/config.py` loads it with sensible fallbacks so the app still boots cleanly if the file (or any key) is missing. Code-level constants (timeouts, internal sentinels, the `RECIPE_PHRASES` list) stayed in Python — the JSON is for things a user might want to tune without touching code. Restart required to pick up changes (uvicorn `--reload` only watches `.py`).

### `intake/build_query_batch.py` — 7-stage front-end pipeline

The new batch ingestion pipeline: `query → SerpAPI → filter → is_recipe → Moz → min-DA → min-OU → rank`. Single in-process Python program per the `feedback_batch_single_program` memory — no new uvicorn workers; reuses the same `extract_recipe_from_url` + `_save_recipe_core` the live form does.

Three SerpAPI-stage improvements over the obvious first cut, all chased after a beef-stew test returned 7 organic out of 50 requested:

- **Pagination via `start`**. Google's first page is featured-snippets / People-Also-Ask / video / carousel theater — typically only 7-9 organic slots. Subsequent pages return clean rosters. Cap at `serpapi_max_pages` (default 10).
- **Site-exclusion operators in the query**. Splice `-site:youtube.com -site:wikipedia.org ...` into the query string itself so Google's organic slots get spent on real recipe sites instead of being burned + post-filtered by us. Costs nothing extra (one quota unit per page either way). Wikipedia added to the blacklist on the user's call after one beef-stew hit had a negative OU.
- **Locale + dedup params**. `gl=us hl=en filter=0` pins a stable SERP and disables Google's similar-page auto-collapse for more candidate variety.

The `is_recipe` filter intentionally runs **before** Moz — burning Moz quota on roundup articles ("/articles/24-the-best-beef-stew") that survive the cheap domain blacklist is wasteful. Threshold defaults to 7; a path-fragment blacklist (`/articles/`, `/roundup`, `/listicle`, ...) catches roundup patterns that score above 7 because they contain ingredient lists in passing.

### Multi-query dishes

User flagged that "spaghetti with meat sauce" and "spaghetti and meat sauce" are two queries for one dish — both should feed the same library row, dedup'd and merged. `_multi_query_lookup` accepts a list of queries, runs each through `_serpapi_lookup`, unions on `normalize_url()` of each result, and stamps `_queries: [<which queries surfaced this URL>]` (1 query usually, but a URL appearing in *multiple* phrasings is a stronger dish signal worth carrying through). `google_rank` keeps the best position across queries. Single-query callers pass a list of one and behave identically to the pre-multi-query path.

### `is_recipe` warn-and-continue on the live path (memory only, not built)

User: *"there are recipes that can have less than 7 tags... lets say we let him go forward if it's not a recipe — won't the lack of ingredients and steps kill it later?"* Right call: for the live form (user pasted text or URL with intent), the right move is **warn** with override, not **block**. Batch keeps the hard floor; live gets a banner. Captured in `memory/project_live_is_recipe_warn.md` — not built this session.

### `intake/process_batch.py` save-gate

`_batch_save_worthy(recipe, min_ings=3, min_steps=3)` mirrors the live save floor — but where the live form catches the 422 and offers "Save anyway," the batch saves silently skip with a `SAVE-SKIP` log line. User's framing: *"if junk gets in all our aggregated stats go to hell, just like they would with the Wikipedia case."* Both floors read from `bcc_config.json`; one place to tighten/loosen.

### Dish library — table, CRUD, refresh button

`input/pipeline/dishes.py` introduces the `dishes` table:

- `name TEXT PRIMARY KEY COLLATE NOCASE` — the immutable join key. Every `master_recipes` row stamps `_master.dish` with this name; "rename" is delete + recreate (which also deletes the master rows — intentional, per the dish-library design).
- `queries TEXT NOT NULL` — JSON array (one or more).
- `top_n_serpapi / top_n_final` — per-dish override of the config defaults.
- `refresh_ttl_days` — NULL = manual-only; populated = the eventual scheduler agent picks it up when due.
- `last_refreshed / last_run_status / last_run_count / last_run_log_filename` — run telemetry the form's status badges + "View latest log" link read.

Endpoints: `GET /dishes`, `GET /dishes/{name}`, `POST /dishes`, `PUT /dishes/{name}`, `DELETE /dishes/{name}`, `POST /dishes/{name}/refresh`. The refresh endpoint is the synchronous version this session shipped — `build_batch` + delete prior `kind='top'` rows for the dish + extract + save with `_master` stamped. Wall time 1-3 minutes; the Cloudflare 100s timeout that bit us next day (2026-05-24) drove the job-system rework.

### `_master` MasterMetadata block + kind taxonomy

Master rows now carry a `_master` block: `kind` (`top` | `editors_choice` | `legacy`), `dish` (canonical name from the dishes table), `refreshed_at`, `rank`, `queries`, `batch_source`. The delete-and-replace logic on refresh only touches `kind='top'` rows — `editors_choice` (curator picks) and `legacy` (pre-batch imports) survive. `_master` added to `USER_TOP_LEVEL_FIELDS` in `recipe_model.py` so `static_subset` correctly strips it during claim — claimed rows shouldn't carry the master's curator-side metadata into a user's table.

### Live `is_recipe` score on form extract (warning, not block)

Tied in with the dish work: the live extract path now stamps `current_status.is_recipe_score` on every extraction and the form surfaces it as a "low recipe-text confidence" warning chip when the score is below `is_recipe_threshold`. **Does not block** — user can save anyway. Mirrors the batch path's `is_recipe` filter, but the consequence is informational rather than hard-cull. The `memory/project_live_is_recipe_warn.md` note went from "to do" to "shipped first pass."

### BCC permalink — `bestcooksclub.com/r/<recipe_id>`

User: *"we need a URL for OUR link to our recipes... if we created them we need to construct a link to our domain (BestCooksClub.com)/this record id... we need a field to display this url and we should put it in the current page url."* Implementation:

- Self-URL minted at save time when no caller-supplied source exists, using `BCC_PUBLIC_DOMAIN` from `bcc_config.json` — `https://bestcooksclub.com/r/<recipe_id>`. (The original implementation used the request's `Host` header, which made local-dev recipes get `localhost:8009/r/...` permalinks. Config-driven domain fixes that.)
- `/r/{recipe_id}` already redirects to `?recipe_id=...` (shipped 2026-05-21). The form's GET response now includes `user_id` at the top level (commit `1f41478`) so the form's load path can hydrate the sidebar to the correct user without an extra fetch.
- Cached `_source.type='local'` recipes round-trip through the same dedup path as any "real" source URL — verified by extracting a recipe that originated from the bookmarklet, save, re-extract via the BCC self-URL, observe the adopt-existing short-circuit instead of a new row.

### Master recipes UI (first pass) — Promote + Master picker

Curator workflow shipped as a first pass — full curator-only workflow still needs design (memory: `project_master_recipes_ui`):

- **Promote** button on every loaded recipe — duplicates the row into `master_recipes` (user_id=0) with `_master.kind='editors_choice'` and the curator's name in `_master.curator`. Idempotent — re-promoting just updates `refreshed_at`.
- **Master picker row** in the sidebar — collapsible row above the personal recipe list showing the top N master rows for the current view. Click → load just like a personal recipe; the form's "Claim" button (shipped 2026-05-21) is the path from master → personal.

Both are admin-visible only for now (no formal role gating yet — sidebar input `user_id=0` is the implicit toggle).

### `library-shell` pattern — shared admin-page scaffold

Three reusable pieces extracted as `dishes.html` was being built, in anticipation of the upcoming cookbooks / equipment / gourmet admin pages:

- `library-shell.css` — fixed header with hamburger ☰, sliding sidebar (left) with `body.sidebar-open` lock for iOS, centered main container, fixed action footer. Input/button styling mirrors the recipe form (12px 14px padding, 12px border-radius, italic placeholder color) so admin pages feel like the same product.
- `library-shell.js` — `LibraryShell.init({sidebarSelector, sidebarToggleSelector})` wires the toggle, click-outside-closes, and the iOS body lock; exports `openSidebar` / `closeSidebar` / `escapeHtml` / `fmtDate` helpers. One initialization line per page.
- Documented inline at the top of `dishes.html` as a template: 5 steps to spin up a new entity admin page.

### iOS sidebar drift fix

User reported the sidebar scrolled with the page on iPhone. Three CSS additions: `overscroll-behavior: contain` on the sidebar (kills iOS rubber-band into the parent), `touch-action: pan-y` on the body, and a `body.sidebar-open { position: fixed; width: 100% }` lock that the JS toggles. Also anchored the sidebar top + bottom instead of `height: 100vh` (which drifted when iOS Safari hides/shows its bottom bar). The same fix applied retroactively to `recipe_form_styled.html` since it had the same bug.

### "Add new dish doesn't clear" — placeholder vs default misdiagnosis

User reported the add-new-dish form had leftover values from the previously-selected dish. Initial diagnosis was wrong — the fields *were* cleared, but the empty inputs showed `value="25"` etc. as actual values rather than placeholders. The defaults visually mimicked real entries. Fix: switch to `placeholder="25 (default)"` with italic muted styling, and apply the recipe-form input styling so the "this is a hint not a value" cue is unambiguous.

### Per-run log files

Every dish refresh now writes to `forms/logs/dish_<name>_<timestamp>.log` for the duration of the run. `_TeeStream` wraps `sys.stdout`/`sys.stderr` to tee writes into the log file (with `.flush()` after each — terminal output was hidden during runs without it). `last_run_log_filename` column on the dishes row + a "View latest log" link in the form header so the user can read the trace post-mortem. `forms/logs/` mounted as a static directory so the link works without an extra endpoint. Migrated to `jobs.py` on 2026-05-24 — the runner now owns the tee context.

---

## Session log — 2026-05-24

**Async job system day.** A 524 from Cloudflare on a 3-minute dish refresh forced the question: keep band-aiding sync HTTP or build the right infrastructure now? User: *"we might as well bit the bullet now... we should generalize it to an extent as I believe we will have many jobs like this likely kicked off by agentic AI... this is a serious piece of infrastructure software and this will be the model to build others from."*

### Cloudflare 524 on dish runs

`POST /dishes/<name>/refresh` ran synchronously — `build_batch` + extract + save each candidate inline — and took 1-3 minutes for typical dishes. Cloudflare's free plan has a hard 100s origin-idle timeout; the browser saw `524` even though uvicorn finished the work and the saves landed cleanly. User: *"on my dish run for chocolate chip cookies i got a 524 error... run failed... where can I see the activity in real time?"* — and the answer was "you can't, it's in the python console you closed." Point-fix options (longer Cloudflare timeout: paid plan; client-side polling against an ad-hoc status endpoint) all looked like band-aids on the same wound.

### Hybrid messaging — SQLite-poll queue + SSE live tail

Considered messaging architectures. Real options: in-process asyncio queue (no durability — uvicorn restart = lost jobs), Redis/RabbitMQ (overkill for one-machine pre-launch), SQLite-poll (durable, no new infrastructure, fast enough at our volume). Picked SQLite-poll for the queue + SSE for the browser live log. User: *"absolutely... sounds great... be careful, it's a significant change but worth every minute."*

The hybrid messaging design: the **queue** is SQLite (durable across restart, free crash recovery via `reset_interrupted_jobs`); the **browser update channel** is SSE (live log lines + status transitions + 25s heartbeats so Cloudflare's idle timer can't fire mid-run); the **fallback** is a regular GET `/jobs/<id>` poll for environments where SSE wobbles through the tunnel (mobile carriers, proxies). Three different mechanisms each appropriate to their layer.

### `input/pipeline/jobs.py` — the foundation

New module owns the durable queue + the runner + the handler registry:

- **`jobs` table** — `id`, `type`, `params` (JSON), `entity_ref` (e.g. `dish:Beef Stew` so cross-finds like "is this dish currently in flight?" are cheap), `status` (`queued` | `running` | `success` | `error` | `cancelled`), `scheduled_at` (NULL = immediate, populated = future for the eventual scheduler), `created_at` / `started_at` / `finished_at`, `log_filename`, `result` (JSON, type-specific), `error_detail`. Three indexes: `(status, scheduled_at)` for the runner's find-next hot path, `(entity_ref, status)` for in-flight checks, `(type, created_at DESC)` for the future admin list view.
- **`runner_loop(db_path, log_dir, *, poll_interval=2.0)`** — asyncio background task started on uvicorn startup. Polls every ~2s for the next ready job, opens a per-job log file (`forms/logs/job_<type>_<id>_<entity>_<ts>.log`), tees `sys.stdout` + `sys.stderr` to it, calls `JOB_HANDLERS[job["type"]](job)`, marks finished with the result dict (or `error` + `error_detail`). **Serial** — one job at a time process-wide, because the stdout-tee is global; concurrent jobs would interleave logs and we'd lose the per-job trace. Concurrency caps per-type are a future design point.
- **Crash recovery** — `reset_interrupted_jobs(conn)` runs on startup; any row stuck in `running` from a previous process is flipped to `error:interrupted`. The runner died mid-job in the last process; future agents can re-enqueue if they need to.
- **Pluggable handlers** — `register_handler("dish_refresh", async fn)` at module import time. The runner reads `JOB_HANDLERS` each tick, so handlers can be added or replaced without restarting the loop (though we don't lean on that yet).
- **`_TeeStream`** with explicit `.flush()` after each write + the log file opened `buffering=1` (line-buffered) — the SSE tail sees lines in near-real-time, which is the whole point.

### Refactored `/dishes/<name>/refresh`

The old refresh body got extracted as-is into `_handle_dish_refresh_job(job)` — same logic, but it's no longer in charge of opening the log file or managing the stdout-tee (the runner does both). The endpoint became a thin 5-line enqueuer:

- 404 if dish unknown
- 400 if dish has no queries
- 409 if `jobs_lib.find_in_flight_for_entity(conn, "dish:<name>")` returns a row — returns the existing `job_id` so the UI can attach to that stream instead of fighting for a slot
- otherwise enqueue + return 202 with `{job_id, stream_url, status_url}`

`register_handler("dish_refresh", _handle_dish_refresh_job)` at module top level wires the type → handler binding. Adding a new job type is define-handler + register; no other touchpoints.

### Generic jobs endpoints

Three new endpoints that any future job type inherits for free:

- `GET /jobs` — list with optional `type` / `entity_ref` / `status` / `limit` filters. The eventual `/forms/jobs.html` admin page consumes this.
- `GET /jobs/{id}` — single-job poll. The SSE fallback for browsers that can't keep a stream open.
- `GET /jobs/{id}/stream` — Server-Sent Events. Four event types: `status` (queued → running → success/error transitions, each fired once), `log` (one event per appended log line, tailed via `tell()`/`seek()`), `heartbeat` (every ~25s, content irrelevant, exists to reset Cloudflare's idle timer), `done` (final event when status hits a terminal value; the stream closes after). `X-Accel-Buffering: no` header tells any nginx-style proxy not to buffer the stream. The tail tolerates ~5 consecutive misses post-enqueue (the job row's `INSERT` and the runner's first `SELECT` race in the first ~100ms).

### `dishes.html` live-log UI

The Run handler went from `await fetch(...) → render(result)` (90s of dead time, then a result) to:

1. POST `/dishes/<name>/refresh` → 202 with `job_id` (or 409 → attach to the in-flight job's stream — handles the case where two browser tabs both clicked Run, or where the page was opened mid-run).
2. Open `new EventSource('/jobs/<id>/stream')`.
3. Render a dark console-styled `.live-log` panel under the dish detail card with a status pill (`queued` → `running` → `success`/`error`) and a tailing `<pre>` of log lines.
4. On `done` — close the stream, refetch the dish row (so `last_refreshed` etc. are fresh), reattach the live-log panel to the re-rendered detail (so the trace survives the re-render), and append the existing `appendResultPanel(result)` summary.

Auto-scroll keeps the latest line in view; bounded at 2000 lines so a runaway log doesn't blow up the DOM.

### "Do not close this tab" leftover from the sync era

User noticed a CSS-rendered overlay still read *"Running — please wait, do not close this tab."* The whole point of the job system is **the user can close the tab and the job keeps running**, so the message was actively wrong. Tracked it down to `.running-overlay::after` in `library-shell.css` (a holdover from the synchronous version's pessimistic UX). Removed the overlay rule entirely + removed the matching `card.classList.add('running-overlay')` adds/removes in `dishes.html`. The live-log panel is the new visual indicator; it stays visible without locking interaction.

### `serpapi_union` count — bridge the per-query vs after-disallowed gap

User: *"i asked for 10 from serp.. in the counts it said after-disallowed 18."* Not a bug — with 2 queries × 10 per query = 20 SerpAPI candidates, deduped to ~19, then `after_disallowed: 18` after one was dropped. But the count panel jumped from "10 per query" to "18 after disallowed" with no visible bridge step, which read as inconsistent. Added `serpapi_union` (post-dedup total before `filter_disallowed` runs) + `num_queries` to the counts dict in `build_batch`; the panel now reads `SerpAPI/query: 10 × 2 queries · serpapi_union: 19 · after_disallowed: 18 · ...`. Math was always right; the UI just hid the relevant step.

### Memory + architecture notes

- `memory/project_job_system.md` — full architecture rationale: queue layer (SQLite), runner layer (serial asyncio), scheduler layer (future), admin UI layer (future). Layer phasing: **Layer 1** (this session) jobs table + runner + dish_refresh handler + endpoint refactor + SSE UI; **Layer 2** scheduler loop scanning `dishes` for due refreshes (`refresh_ttl_days` elapsed since `last_refreshed`); **Layer 3** `/forms/jobs.html` admin page on the library-shell. The cron-equivalent in-process — what the user described as "this process will be running on a timed basis in batch... we need to build that and figure out how to have it running continually waiting for the time to do the next scheduled refresh."

---

## Session log — 2026-05-28

A monster day. Built the cohort-matching + grading infrastructure end-to-end (embeddings, sqlite-vec, identity card, dish_signal); reframed the image story (og:image coopt, consistent 1500×1000/1000×1500 sizing, Playwright page screenshots, manifest sidecar for traceability); shipped two new UI surfaces (suggested-dishes queue, top-recipes panel with Open-in-BCC / View-source buttons); rebuilt the identity badge as text-only with hover-arrow; and ran four large backfills (identity cards, grades, cooped images, page screenshots) across 354 rows. Twelve+ files added, every form file modified, ~$0.10 in LLM costs across all backfills. Single end-of-day commit.

### Embeddings + cohort matching + grading (the morning's headline)

Built `input/pipeline/embeddings.py` (text-embedding-3-small via OpenAI, 1536-dim normalized vectors, `find_best_dish_match` with chapter pre-filter), `input/pipeline/grading.py` (`compute_exceptionalism(da, pa, ou_fit)` that applies a stored fit's coefficients to a single DA/PA pair), and `input/pipeline/vector_store.py` (sqlite-vec backed). Wire-in: `_save_recipe_core` now stamps `_master.exceptionalism` on master rows and `_grade` on personal rows from either an explicit `_master.dish` (harvest path) or an embedding match. `dishes.identity_card`, `dishes.embedding`, and `dishes.chapter` columns added to surface the matching internals + drive a SQL pre-filter on KNN.

Two memories saved: `project_sqlite_vec_migration.md` (the v0.1.9 quirks: no LIMIT with `k = ?`, aux-column equality forbidden inside KNN, but PK `IN (subselect)` IS honored as a pre-filter — that's the cleaner pattern than over-fetch+post-filter), `project_identity_card.md` (the facts-first ordered-tool_use schema that fixed the Salted Tahini Chocolate Chip Cookies case from cos 0.54 → 0.78).

### Identity card architecture (the key architectural inversion)

`extract/dish_signal.py` (one-line Haiku call) → `extract/identity_card.py` (structured fact card via ordered tool_use). The schema lists `ingredientRoles` → `cuisine` → `ethnicity` → `technique` → `servingForm` → `likelyDish` in that exact property order. Anthropic's tool_use emits keys in declared order, so the model commits to the structural facts BEFORE the conclusion. `likelyDish` then has to reason from what it just recorded.

Storage: top-level `_identity` on recipes, `dishes.identity_card` (JSON TEXT) on dishes. `compose_identity_text(card, title)` is the shared composer — same shape both sides → embedding cosine reflects semantic similarity, not format-similarity.

The Salted Tahini case went 0.54 → 0.78 against the CCC dish. The whole identity card backfill across 11 dishes + 157 master + 167 personal recipes ran in ~10 minutes, ~$0.03 total cost.

### sqlite-vec migration (vec0 with PK pre-filter)

Installed sqlite-vec 0.1.9, added `dishes_vec` (TEXT PK = dish name, embedding float[1536]) and `recipes_master_vec` (INTEGER PK = master_recipes.id, embedding float[1536], +chapter, +dish aux columns) virtual tables. `find_best_dish_match` rewritten to use `WHERE embedding MATCH ? AND k = ? AND <pk> IN (SELECT … FROM dishes WHERE chapter = ?)`. The PK IN subselect is the secret — vec0 honors it as a true pre-filter (not post-filter), so the KNN scan stays small even at scale. No over-fetch needed.

L2 distance is monotonic with cosine for normalized vectors; the public API still reports cosine via `_l2_to_cosine_sim(d) = 1 - d²/2` so thresholds stay unchanged.

### Wayback Machine fallback in the canonical fetcher

`to_markdown/html_to_markdown.py` gained `fetch_via_wayback(url)` + `fetch_with_full_fallback(url)` — direct UA chain first, Wayback snapshot fallback on failure. Verified live on the Kitchn URL that's been blocking us all week; Wayback returned the full JSON-LD recipe from a 4-month-old snapshot. Used by both `fetch_html` (step 7) and `_fetch_text` (step 3) for consistency. Per the [[single-path]] memory.

The dish-refresh batch logs show Wayback firing on a couple sites that were briefly down (barbarabakes.com); pipeline gracefully recovered.

### og:image + og:meta extraction → coopt pipeline

`extract_og_image` generalized to `extract_og_meta(soup, base_url)` — full dict with `image`, `description`, `imageAlt`, `title`, `siteName`, `author`, `publishedTime`, `modifiedTime`. New fields on `SourceInfo`: `previewImage`, `previewDescription`, `previewImageAlt`, `siteName`, `author`, `publishedTime`, `modifiedTime`. All added to `_SOURCE_STATIC_SUBKEYS` so they travel through claim/cache.

`input/pipeline/image_pipeline.py` — `coopt_image(url)` fetches with browser UA + 10MB cap, runs through `process_thumbnail` (Pillow, EXIF strip, JPEG q=85), stores via the `image_store` abstraction. URL-hash keying for dedup across recipes pointing at the same image.

### Cookbook-grade image sizing — every image is now 1500×1000 OR 1000×1500

Switched `process_thumbnail` from "downsize-to-max-width" to `ImageOps.fit` center-crop with two target buckets:
- LANDSCAPE_TARGET = (1500, 1000) — 3:2
- PORTRAIT_TARGET = (1000, 1500) — 2:3
- Aspect threshold 0.95 to pick bucket (square-ish leans landscape)

Same composer used by `coopt_image` AND `generate_dish_image` (AI gen, default orientation='random' → 50/50 landscape/portrait → gpt-image-1 native 1536×1024 or 1024×1536 → process_thumbnail crops to exact corpus standard). Result: visually indistinguishable artifacts across cooped + AI-generated images.

Audit confirmed: 272 cooped files, EVERY single one is exactly (1500, 1000) or (1000, 1500). Zero outliers.

### Image storage abstraction (LocalStore + S3Store + manifest)

`input/pipeline/image_store.py` — backend-agnostic `ImageStore` protocol with two implementations:
- `LocalStore` — writes to `generated/` (matches existing static mount), default
- `S3Store` — boto3 + `put_object` with public-read or presigned URLs. Configure via `BCC_S3_BUCKET` / `BCC_S3_REGION` / `BCC_S3_PUBLIC_BASE_URL` (CloudFront) env vars. Falls back to Local on missing config.

Manifest writer: every `put()` appends a line to `_manifest.jsonl` with `{file, url, ts, meta: {recipe_id, source_url, kind}}`. File→recipe mapping is recoverable from the storage backend alone — addresses the user's concern about lost-DB-link traceability without forcing recipe-id-based naming (which would lose dedup).

### Page screenshot capture via Playwright

`input/pipeline/screenshot_pipeline.py` + `scripts/_capture_screenshot_worker.py`. Captures above-fold view (1500×900 viewport, 800px capture height, 1.5s settle) via headless Chromium, processes through `process_thumbnail` to corpus-standard 1500×1000. Key pattern: `recipe-screens/<recipe_id>-<sha8>.jpg` — recipe_id prefix gives the file→recipe traceability the user explicitly asked for.

**Windows asyncio quirk caught + fixed:** calling `sync_playwright()` from inside uvicorn's worker thread raises `NotImplementedError` because the parent's `ProactorEventLoop` can't spawn subprocess children from threads. Fix: shell out to `scripts/_capture_screenshot_worker.py` via `subprocess.run` — fresh Python process with its own event-loop policy. ~200ms overhead per capture is noise next to the 2-3s page load.

Wired into `extract_recipe_from_url` so every URL extract auto-captures going forward. Backfill ran across 321 of 359 rows that have source URLs (94%); the 38 misses are anti-bot blocked or handwritten.

### Misshared og:image detection + cleanup

Sanity check found 5 cases where DIFFERENT recipes shared a cooped previewImage. 3 were correct dedup (same content, different URL variants — print pages, query-string twins). 2 were real mishares: food.com publishes a single generic og:image for every recipe (`imgstore.sndimg.com/foodcom/images/8de26738-...jpg`) and yboc.ai (user's own Ghost site!) publishes `YBOC-Eggs-Wide-2.png` as the default OG card for every post that lacks a Feature Image. We faithfully cooped both. Fix: for affected rows, re-cooped from JSON-LD `image[0]` (recipe-specific) when available; cleared `previewImage` when not. 11 rows refixed, 3 cleared (the yboc.ai pork+rice ones — user will add Feature Images in Ghost admin).

### Identity badge redesign (twice) + positioning fix

First pass: avatar circle + italic name + monospace uid. User: "ugly as hell" + "looks absurd on iPhone." Redesigned as text-only — italic Georgia name + hover-revealed `↗` arrow. No avatar, no link underline, plain text reading as "your byline" not "a button." Mobile: just the name + always-visible faded arrow.

Second issue: badge landed on the LEFT on dishes/users/install (only on the RIGHT on recipe form). Root cause: `init()` was calling `initIdentityBadge` BEFORE `initNav` mounted the nav-spacer. Fix: drop the call from `init()`; let `initNav` be the single mount point. Now consistent right-side mount across all pages.

### Form UX additions

- **Identity card panel** in recipe form metadata section — likelyDish + cuisine + ethnicity + primary ingredients + technique + servingForm + ingredient roles table.
- **Page screenshot well** under hero image — read-only, clickable to source URL, hidden when empty.
- **Description max-height bumped 180px → 420px** to match the now-taller right column (hero + screenshot stack).
- **Sidebar thumbnails** updated to prefer `_source.previewImage` over `image[0]` (fixes Mixed Content warnings on HTTP recipe image URLs).
- **Suggested dishes panel** on dishes.html sidebar — `GET /dishes/suggestions` returns clusters of carded recipes whose `_identity.likelyDish` doesn't match any dish entry (≥3 threshold). Click pre-fills the Add Dish form with the suggested name.
- **Top recipes panel** on the dish view card — `GET /dishes/<name>/top-recipes` returns the ranked master rows for the dish. Each row: preview tile, rank chip, name + hostname + grade badge, PA/DA/OU, and a two-button action row: **`Open in BCC`** (primary, fills accent) and **`View source ↗`** (outline secondary). Tile clicks to BCC; source button to original site. Resolves the user's "I should have a choice" ask.

### Many small fixes worth not forgetting

- `escapeHtml` ReferenceError in recipe form's identity card render was caching the "Failed to load recipe" error — `populateCohortMeta` was using bare `escapeHtml` but the recipe form doesn't import LibraryShell.escapeHtml at top level. Pulled it inline.
- Recipe form's image autofill blocker was triggering RoboForm. Added `data-rf-ignore` + `data-1p-ignore` + `data-bwignore` + `autocomplete="off"` + renamed `name` attribute.
- "Dish signal (legacy / display)" textarea was redundant with the identity card's `likelyDish` heading — removed.
- `library-shell.css` was inlined in recipe_form_styled.html (against the principle); linked the stylesheet instead via `?v=20260528b` cache-bust.
- Save flow's existing dishSignal field auto-mirrors `_identity.likelyDish` for backward-compat with any consumer still reading the old field.

### Backfills run today

| Backfill | Rows | Wall time | Cost |
|---|---|---|---|
| Identity cards (dishes + recipes) | 11 + 157 + 167 = 335 | ~10 min | ~$0.03 |
| Grades (after card stamping) | 324 | ~7.5 min | ~$0.02 |
| Cooped og:image (re-fetch + 1500×1000 / 1000×1500) | 321 | ~7 min | $0 (no LLM) |
| Page screenshots via Playwright | 321 | ~17.5 min | $0 |
| Mis-shared image re-fix | 11 + 3 | ~30s | $0 |

### Where stuff lives now

- 305 cooped previews + 321 screenshots + a few hundred manifest entries: `generated/og-thumbs/`, `generated/recipe-screens/`, `generated/_manifest.jsonl`
- ~110 MB total on disk
- Configuration for S3 flip in `bcc_config.json` and env vars (`BCC_IMAGE_STORE_BACKEND=s3`, `BCC_S3_BUCKET`, …) — code is ready, just flip the switch when you have a bucket

### What's queued for tomorrow

User explicitly flagged for the morning:
1. **Master recipe display panel (END-USER FACING)** — this is THE page consumers will see when they land on a TBOTB recipe. NOT an admin/curator surface. Editorial visual register, lots of pizazz, designed to make the user think "this is a real cookbook, not a scrape." Above-fold elements should include: cooped photo (or AI-gen) at hero size, recipe title in display serif, grade badge prominent, the editorial commentary (`editorial.opinion` + `scoreCommentary`), the "Open in BCC / View source ↗" choice front-and-center. Below-fold: identity card facts as a kind of "recipe DNA" panel, the "We think you'd like" cluster (next task), and the page screenshot as the article-style provenance bottom of the page. Must feel deliberate and delightful — this is the public-facing surface for every master recipe.
2. **"We think you'd like" recommender wiring** — the `find_similar_master_recipes` helper already works; needs to land on the recipe page + the dish detail page. Vec0 KNN with chapter pre-filter + exclude_dish for cross-cohort discovery.
3. **Begin commerce / Amazon Rainforest API integration** — user added the Rainforest API key to `.env`. Use the embedding infrastructure we built today to map recipe ingredients → product matches via vector similarity. Start with the affiliate-catalog memory's "Editorial.sourcingNotes" injection point: LLM identifies critical-quality ingredients during enrich, server matches against a vectorized Amazon product catalog, picks render inline with the editorial prose.

### Memories updated / added today

- `project_sqlite_vec_migration.md` (rewritten with shipped status + v0.1.9 quirks + the PK-IN-subselect pre-filter pattern)
- `project_identity_card.md` (architecture + ordered-tool_use pattern + LLM quirks observed in backfill)

---

## Session log — 2026-05-27

**Consistency day.** A series of "why does X fail here but work there" questions all pointed at the same root: parallel implementations drifting. The day's work standardized fetch, root-pick, and grade scaling across batch and live paths; then added a manual-from-reject rescue path and an Exceptionalism letter-grade overlay.

### BS4 picker port — server-side root scoring matches the bookmarklet

Yesterday's bookmarklet `pickBestRoot` (score every candidate root by `chars + 100 * recipe_phrase_hits`, pick the highest) was browser-only. Today ported the same algorithm to `to_markdown/html_to_markdown.py:select_main_content`. Phrase list (~30 entries) and selector list (`.recipe-details`, `[data-slot-rendered-recipe]`, `.wprm-recipe-container`, etc.) mirrored exactly, with cross-reference comments in both files so they stay in sync. The old picker did first-match-wins on `[itemtype*='Recipe'] / article / main / body`; that grabbed a blog-post `<article>` on sites where the recipe lived in a sibling `.recipe-details` widget (cleanfoodiecravings.com case) and silently dropped the recipe. New picker clones each candidate, runs `clean_for_markdown` on the clone, scores it, returns the winner — `<body>` wins as the safe fallback when no narrower candidate concentrates more recipe phrase. Three-case smoke test covered: recipe-in-`<article>` (picks article), recipe-in-`.recipe-details` sibling (picks body, still contains the recipe), schema.org itemtype present (picks body, JSON-LD fast lane handles upstream).

### Playwright sandbox queued

Discussed adding Playwright as a future server-side fallback for the two failure modes plain `requests.get()` can't fix: anti-bot 403 (cleanfoodiecravings) and JS-rendered widgets. Concluded today's BS4 port is the right "fast path" (no JS needed) and Playwright will be the right "fallback" (anti-bot / JS-rendered). The architectural promise: extract `pickBestRoot` to a shared `.js` file the bookmarklet AND server (via `page.evaluate`) both consume → actual code reuse, not parallel implementations. Built `sandbox/playwright/` folder with README + install notes + a 02_smoke.py that launches Chromium and dumps a page. Default smoke target is the cleanfoodiecravings URL that 403's our plain fetcher. Not wired into production.

### Harvest from rejects — manual-from-reject → master, attributed

User's question: "still need work on saving the record launched from the dish page... do we store the batch name (dish) with the records in the master after the batch run? if so when we launch from the dish rejects page we should add to the url our batch name... voila we now are able to reconstruct the batch... we should probably store the date run the same way as well." Then: "make sure that launched url is going to the master, not any user acct."

Round trip:

1. **Dish form (`forms/dishes.html`)** stamps each reject link with a hash fragment `#_bcc_dish=<name>&_bcc_run=<reject.run_started_at>`. Hash (not query string) survives redirects and doesn't go to the source server.
2. **Bookmarklet (`forms/bookmarklet.js`)** reads `location.hash`, harvests `_bcc_*` keys, strips them from the recorded `source_url`, posts them as `bcc_hints` in the stage-markdown body.
3. **`/stage-markdown`** validates and persists hints alongside the markdown.
4. **Form hydrate (`recipe_form_styled.html`)** locks the user-id picker to `0` (Master), disables the TBOTB toggle, shows a sticky amber "🌾 Harvest from dish rejects" banner with the dish + run, and a "✕ clear harvest" reset link. Clicking Clear (form-wide) also clears the harvest lock.
5. **`/recipes` save** force-pins `user_id=0` from the hint *before* the master-write permission check (so role gating still runs on the actor, not bypassed by the hint). `_save_recipe_core` pops `bcc_hints` and stamps `_master.kind="harvest"`, `_master.dish=<hint>`, `_master.refreshed_at=<run>`, `_master.batch_source="manual-from-reject"`.

Defense in depth: even if a user manually toggles the user-id input after the harvest lock is set, the server's hint-driven re-pin wins. The kind `harvest` is distinct from `top` (algorithmic batch winners) and `editors_choice` (deliberate curatorial elevation) — explicit per user feedback: *"editors choice is for items I WANT in... you need to say something implying the curation run or something."*

### Exceptionalism — T-score letter grade per recipe

User proposed grading recipes on their per-dish OU residual via the T-score transformation `(OU / σ) * 10 + 75`. School-style 0.5σ-wide buckets (A+ ≥ 97.5, A ≥ 92.5, A- ≥ 87.5, B+ ≥ 82.5, etc.). Cross-dish comparable because grades are relative-to-cohort. `σ_effective = max(σ_observed, 0.5)` — floor prevents tight cohorts from auto-creating A+'s where a tiny absolute lead becomes a huge z-score. n<25 dishes (where `_compute_custom_ou` doesn't fit) skip grading entirely; UI shows em-dash.

Stamped on master rows as `_master.exceptionalism = {score, grade, basis: {model, n, sigma_effective, sigma_observed}}`. `sigma_effective` also persists on `dishes.last_ou_fit` so future harvest grading can recompute against the originating run's scale rather than today's cohort. Rejects table gained `exc_score` + `exc_grade` columns so the dish-form reject rows display "would have graded X" alongside the existing "would qualify" badge — informs the harvest decision.

Display rolled out across three surfaces. CSS lives in `library-shell.css` so dishes.html and recipe_form_styled.html share the visual:

- **Sidebar card** — small monogram badge top-right (size 'small'), tier-keyed color. A grades wear the brand terracotta; B/C/D recede through saturation; F is ghosted. Hover reveals tooltip with score + cohort basis. Cards without a grade render unchanged (no empty slot reserved).
- **Form scoring strip** — 5th chip joining PA / DA / OU / Recipe-text: large badge + numeric T-score + cohort basis line ("quadratic · n=100 · σ=2.34").
- **Dish-reject row** — small inline badge next to "would qualify". Cohort basis omitted (already shown on the panel's fit line).

Three size variants share one tier palette — A+ filled terracotta, A outlined terracotta on soft-bg, A- outline only, B ink, C muted with outline, D pale dashed, F nearly invisible. Editorial register, not stoplight; grades step down through saturation rather than hue so the page reads as one color story.

### Dish form: zero-rejects no longer collapses the panel

After enabling Exceptionalism, Pastitsio's dishes page showed nothing under rejects — because its last run saved 10/10 cleanly, no rejects. `renderRejects` early-returned on empty. Fixed to always render the run-summary panel when `ou_fit` is present, with "No rejects from the last run — every top-N URL extracted and saved cleanly" as the empty-state message. Also: σ_effective now displays on the fit line so the grade math is auditable (`OU fit: quadratic on n=102 URLs (R²=0.715) · σ=5.49 · bar to beat: 6.74`).

Bug along the way: `dishName` referenced inside `renderRejects` wasn't in scope — fixed by reading `payload.dish` (the endpoint already returns it).

### UA fallback in the canonical fetcher — Kitchn `FETCH-FAIL` resolved

User reported: *"[32/123] FETCH-FAIL https://www.thekitchn.com/pastitsio-recipe-23165635"* during a batch run, despite earlier in the session proving Kitchn URLs extract cleanly when fetched directly. Root cause: **two different fetchers with two different UAs**. Step 3 (`_fetch_text` in `intake/build_query_batch.py`) used a Chrome 113 UA; step 7 (`fetch_html` in `to_markdown/html_to_markdown.py`) used the project's bot UA `recipe-forms/0.1`. Kitchn has a reverse anti-bot stance — they 403 Chrome-flavored UAs and 200 bot UAs (opposite of typical). Step 3 dropped every Kitchn URL before extract ever ran.

Fixed the right way (per [[single-path]]): added `fetch_with_ua_fallback(url)` to `html_to_markdown.py` — tries bot UA first, falls back to Chrome UA on failure. Returns `(response, ua_used)` for diagnostics. 404/410 short-circuit terminally (page genuinely doesn't exist, swapping UA can't conjure it). `fetch_html` now uses the fallback by default; explicit `user_agent=` param preserved for tests. Refactored `_fetch_text` (step 3) to call the same canonical fetcher — both stages now see the same response for any URL, no more silent step-3 drops from UA mismatch.

Verified end-to-end: the previously-failing `https://www.thekitchn.com/pastitsio-recipe-23165635` now extracts to a full recipe with 20 ingredients and 11 instructions. Bot UA wins on first try for Kitchn (no fallback needed); 404 URL raised terminally in 0.08s (no wasted retry).

Trade to watch: any site that does *normal* anti-bot (blocks bots, allows Chrome) was previously kept at step 3 (Chrome UA passed) and *might* now fall through to extract with bot UA blocking first. Since both UAs are in the chain, extract still succeeds. The cost is one wasted bot-UA HTTP request before falling back — sub-second.

### Memory + state

- Updated `memory/feedback_single_path.md` lessons concretized — fetcher consistency was an *exact* instance of the canonical-path principle. The fix wasn't "add a workaround in step 3"; it was "share the fetcher."
- Exceptionalism's `sigma_effective` persists in `last_ou_fit` JSON on the dish row, so future harvest grading has the originating run's scale available. Harvest-time grading not yet wired (tomorrow's work).
- `dish_rejects` table got `exc_score` + `exc_grade` columns via ALTER TABLE migration.

---

## Session log — 2026-05-30 / 05-31

### Cookbook chapter taxonomy: restructure + codified rules

Two enchilada recipes mis-filed surfaced the problem: "Enchiladas Verdes" → the old *Pies and Pastries - Savory*, and "Shrimp Enchiladas in Tomato Sauce" → *Sauces* — the latter because the Tier-1 shortcut phrase `tomato sauce` matched a *modifier* inside the dish title and short-circuited the LLM before its dish-vs-component rule could fire. Restructure: merged the two Pie chapters into one sweet **Pies & Pastries**; renamed **Sandwiches → Sandwiches, Pizza & Savory Pastry**; added **Casseroles & Baked Dishes**. Net 24 real chapters + Uncertain.

Codified a **7-rule structural decision tree** into the classifier's LLM system prompt *and* as plain-English docs above `_SYSTEM_PROMPT` (`extract/chapter_classifier.py`): identity > ingredients, physical-form > technique, defining-structure-wins, crust-beats-filling, discrete-centerpiece-vs-assembly, names-don't-override-structure, cluster sanity check. Governing line: *classify by the structural object the cook recognizes on the plate, with structure outranking ingredients, cooking method, vessel, and name.* Worked the famous edge cases (lasagne/moussaka/pastitsio → Casseroles; pot pie/calzone/empanada → Savory Pastry; chicken parm → Poultry; shepherd's pie → Casserole because no crust). Shortcut layer hardened: sauce-chapter phrases are **standalone-only** (fire only when the whole title IS the sauce), so a sauce noun embedded in a dish title no longer hijacks classification — embedded sauces defer to the LLM (rule 11). Spanakopita + fajitas pinned deterministically (savory phyllo was leaking into the now-sweet Pies chapter).

Migration `scripts/migrate_chapters.py` (dry-run default, **scoped** to the restructured chapters so unrelated borderline dishes aren't re-litigated) re-derived chapters across recipes + master_recipes + dishes (441 items) and reconciled the `chapters` registry. Applied + verified live.

### Library-shell UI overhaul (universal across chapters / dishes / users)

- **Config-driven branding**: `brand_name` / `brand_logo_url` / `brand_home_url` in `bcc_config.json` → `GET /branding` → `LibraryShell.applyBranding` renders a home-linked wordmark in the header. Site name is **Best Cooks Club** (bestcooksclub.com) — distinct from the "The Best of the Best" master cookbook (user_id 0). Logo URL still TBD (text-only until set).
- **Sidebar opens at startup**; the ☰ list-toggle moved OUT of the page header into the sidebar's own list header (with a small floating opener shown only when the list is closed), freeing the header's left slot for the logo.
- **`LibraryShell.afterSave({message, onClear})`** — universal save flow: flash confirmation → clear the form → return to the sidebar. Wired into chapters' Save notes; **dishes still uses its own save flow (TODO: adopt afterSave).**
- **Floating action bar** lifted off the bottom edge as a card; **header taller** (`--header-h` 68px).
- **Identity badge** restyled: two lines, no italic — user name (links to the user switcher) over a **live `mailto:` email link** (`/auth/me` provides name + email).

### Chapters editorial detail view + the source-records cohort (key correction)

Replaced the spreadsheet `kv` detail with an editorial layout: hero + one-line summary, conversational fact blocks, a premium formula card, muted low-confidence treatment (`#994d1c`, no neon alert), curator notes as a footer postscript. Typography: **dropped Playfair/Georgia, all-sans** — brand 2.0em, page title 1.5em weight 300, section labels (incl. Curator Notes) all-caps.

**Correction logged so we don't relearn it:** the chapter OU formula is fit on `dish_run_data_points` — the FULL **pre-Moz-trim** SERP cohort from the chapter's underlying dishes — **NOT** the saved `master_recipes` winners. (Confirmed: fit n=76 = the cohort, not the 46 winners.) So the bottom **Metadata → Source records** accordion (collapsed by default, after notes) lists that cohort tightly: `url · DA · PA`, via `GET /chapters/{name}/recipes`. Editorial/curated picks live only in `master_recipes` and are never in the cohort, so the fit excludes them by construction — hardened with a `kind IN ('top','harvest')` guard on `backfill_data_points_from_corpus` so a future `editors_choice` carrying a `_master.dish` can never skew the regression baseline.

### Admin scaffold built — then course-corrected

Built a generic metadata-driven admin RUNTIME (`admin_models.py` declarative `AdminModel` + `forms/admin.html` rendering any registered model + `forms/list_control.js` drop-in sort/search), and used it for a `status_messages` table (rotating wait-messages — `project_friendly_status_messages`). The user then clarified the wanted pattern is the OPPOSITE: a **template cloned + specialized per table** (Rails-scaffold style, derived from the dish editor), with per-field lookups / edit rules as custom code — NOT a generic execution environment. The runtime scaffold is committed but slated to be superseded. See `memory/feedback_editor_template_not_runtime.md`.

### Shipped

All committed to `master` + pushed (10 commits: taxonomy, admin scaffold, branding, library-shell UI, chapters editorial + cohort, typography/identity). `recipes.db` snapshot committed. Feature branch `chapter-taxonomy-restructure` fast-forward-merged to master + deleted (local + remote). Concurrent work landed on the branch between turns (jobs drainer, run-queued nav, BCC link-domain config, siteName wiring, vec index) — preserved, my commits stayed additive.

Captured-in-memory ideas not yet built: **dish-catalog table** (promote `chapter_shortcuts.json` to a DB "canonical dish dictionary" — origin/ethnicity/story/embedding derived once per dish, free thereafter; the BASIS the curated dishes library is promoted from — `project_dish_catalog_table`); **re-derive / preview button** on the recipe form (`project_rederive_preview_button`).

---

## Session log — 2026-05-31 (continued) — admin editor framework + chapter top-10

### Master/detail editor framework lifted into LibraryShell (additive, namespaced)

Designed the reusable a/c/d/v admin-editor shell in two standalone mockups first (`forms/chapters_mockup.html`, `forms/users_mockup.html`) — the design test bed — then lifted it into the real app without touching the existing shell. Decisions (see `memory/project_admin_editor_nav.md`):

- **`forms/editor-shell.css`** — self-contained, namespaced (`.ed-*` + `body.ed-*`) so zero collision with `library-shell.css`/`forms.css`. Editor pages load THIS instead of library-shell.css, plus library-shell.js for behaviour. It also restyles the initNav-mounted chrome (`.app-header`/`.nav-toggle`/`.nav-menu`/`.identity-badge`/coming-soon overlay) so editor pages stay self-contained while still getting the cross-page ⋮ menu + identity badge.
- **`LibraryShell.initEditorNav({backButton, scrim, listLabel, listMode, shellWidth})`** — the mechanical dock/overlay/back nav controller (drives `body.ed-mode-overlay/.ed-list-collapsed/.ed-drawer-open`); returns `{toggle, afterSelect, setMode, …}`. NOT a data-driven renderer — each editor stays a cloned hand-written template (clone-and-specialize, per `feedback_editor_template_not_runtime`).
- **Layout model (option 2):** centered shell capped at `--ed-shell-w` (default **1200**); `listMode` `docked` (push two-pane, default for the light a/c/d editors) vs `overlay` (full-width detail + floating list, the opt-in for the field-heavy recipe/`v` editor); mobile (≤860) always overlay; one back-convention control top-left. Settled 1200/docked after live-toggling width (1040/1200/1320/full) + mode in the mockup's dev controls.
- **`forms/chapters.html` migrated** onto the shell, wired to the real `/chapters` endpoints. `library-shell.css` and the other four pages (recipe form, dishes, users, install) UNTOUCHED — they migrate later. `users.html` is the next clone (the editable-record case the users mockup already proved).

### Chapters: table is the source of truth (no data in code)

Pushback (`memory/feedback_no_data_in_code.md`): chapters were gated on the hardcoded `chapter_classifier.CHAPTERS` constant even though a real `chapters` table exists (`update_chapter_notes` already INSERT-ed rows). Fixed — the **table is canonical now**: `list_chapters_with_status` seeds canonical names `INSERT OR IGNORE` then iterates the table; `_chapter_known()` accepts table rows; new **`POST /chapters`** create endpoint + `create_chapter()`/`chapter_exists()` helpers; the **＋ New chapter** affordance is wired in the editor. The `CHAPTERS` constant is now a bootstrap seed + the classifier's reasoning input. Remaining data-in-code (the classifier taxonomy/decision-tree) flagged as a deliberate follow-up, not refactored casually.

### Chapter fit: no corpus drift; refit on every dish update

Per-dish-batch refit means a chapter fit never drifts from its corpus, so all corpus-diff/drift UI was removed. Wired the recompute into the dish job: `_handle_dish_refresh_job` now calls `compute_and_store_chapter_fit(conn, dish['chapter'])` right after `replace_data_points_for_dish` — per dish (cheap, robust to interruption), confirmed as the intended model.

### Count bug + regression variables surfaced

Breads showed "31" while the fit message said "needs 25" and the cohort list showed 17 — three different numbers. Root cause: the editor mixed `current_recipe_count` (31 saved master *winners*) with the fit **cohort** `last_ou_fit.n` (17 SERP DA/PA records, the number the 25-floor checks). Fixed — the detail now references the cohort everywhere, "Recipes analyzed" relabeled **"Source-site records"**, and `/chapters/{name}/recipes` filtered to non-null DA&PA so the Source-sites list count == `fit.n`. Also surfaced the actual **regression variables**: a "Regression: PA ~ DA" row + **Fitted coefficients** (labeled by term) in Model fit. Source-sites is a collapsed peer-level section.

### Chapter Top-10 recipes (new feature)

The chapter's 10 highest-OU recipes across its dishes, stored as a `top_recipes` JSON snapshot on the `chapters` row (new column), **computed at fit time** in `compute_and_store_chapter_fit` (so it refreshes with every dish update). New `compute_chapter_top_recipes()`/`get_chapter_top_recipes()` + **`GET /chapters/{name}/top-recipes`** (decorated with the BCC permalink). New ranked, OU-forward UI (`.ed-t10`): rank · 52px cover thumbnail · name + `site · dish` · OU headline + grade chip + DA/PA. Populated all 24 chapters (14 non-empty). (Gotcha: forgot to bump `editor-shell.css?v=` → cached CSS served full-size images; bumped to `v=20260531b`.)

### Notes

Verified throughout with headless Playwright smoke tests. Server has no `--reload`; `bcc_restart.bat` didn't take this session (zombie-socket), so restarts were a manual PowerShell kill-owner+children + venv `uvicorn`. Mockups (`*_mockup.html`) kept as the design reference. New memories: `project_admin_editor_nav`, `feedback_no_data_in_code`.

---

## Session log — 2026-06-01 — extract-cache hits made actually fast + durable screenshots, recipes.db out of git

Trigger: a cache hit on `sallysbakingaddiction.com/best-banana-bread-recipe` reported `Total 894ms` but took ~6s wall-clock. Root cause: the extract "cache" only short-circuits the ~25s LLM extract — the whole enrichment tail (chapter, og-image coopt, identity card ~2s, **page screenshot ~3-5s**) ran on *every* hit because it executed AFTER the cache write, and the displayed `total_ms` was stamped right after the cache lookup, so it never counted the tail.

### Cache hits now serve the enriched recipe (the 6s → sub-second fix)
- Moved the enrichment tail to run **before** `_extract_cache_write` in `extract_recipe_from_url` (`save_recipe_api.py`). `_source.pageScreenshot` and `_identity` were already whitelisted in `static_subset` (`recipe_model.py`), so they now ride *in* the cached `recipe_json` and a hit serves them free. Each step is idempotent/guarded (skip coopt if `previewImage` set, skip screenshot if `pageScreenshot` set, identity already no-ops).
- **Self-heal:** new `_cache_row_complete(recipe)` (has screenshot + identity). A hit on a pre-caching row reads as incomplete → full path re-enriches and **re-writes** the row (`path_used != "cache-hit" or was_incomplete`), so the next hit is complete.
- **Speculative fast-path:** before HEAD+fetch+parse, probe the cache on `normalize_url(input_url)`. A *complete* fresh hit returns immediately — no network, no parse, no screenshot (`fast_path: true`). Misses/redirects fall through to the resolved-URL path. Batch (`pre_scored`/`batch_overrides`) + `force_refresh` always take the full path; batch's `pre_scored`/overrides apply AFTER the write so they never pollute the shared cache row.

### Honest timer
`total_ms` now stamped just before `return` on all four extract paths (url/image/pdf/staged) = true wall-clock. Added per-step rows on the URL path: `moz_ms`, `identity_ms`, `screenshot_ms`, `image_coopt_ms`.

### Screenshots → durable, git-ignored `media.db` (design decision)
- New `page_screenshots(screenshot_id PK, url_normalized, jpeg BLOB, created_at)` in a **separate `media.db`** (`MEDIA_DB_PATH`). Compact JPEG (800px wide, q65, ~30-60KB), keyed by `url_normalized` → one row per URL (dedup, not per-recipe). New helpers in `screenshot_pipeline.py`: `screenshot_id_for`, `ensure_page_screenshots_table`, `_to_blob_jpeg`, `store_screenshot_blob`, `read_screenshot_blob`, `capture_and_store_blob`; refactored capture into shared `_capture_raw_bytes`.
- Recipe carries only the short `/screenshot/<id>` URL; new **`GET /screenshot/{id}`** endpoint serves the BLOB. Old image_store `/generated/...` URLs still resolve (drop-in). Why a separate DB: `generated/` is git-ignored + ephemeral, while the cache row is durable — the screenshot now lives as durably as the row that references it, without bloating the git-tracked recipe DB.

### recipes.db removed from git (design decision)
The 29MB binary was tracked → every touching commit stored a fresh full copy (git can't delta binary). `git rm --cached recipes.db` (file stays on disk); `.gitignore` += `recipes.db`/`media.db` (+ `-wal`/`-shm`). The diffable **`recipes.sql` dump is now the sole git-side backup** (refreshed by `bcc_backup.bat`; ADAM tier unchanged). Updated `memory/project_db_backup.md`.

### Verification + remaining
Both files `py_compile` clean, module imports, `/screenshot` route registered, BLOB store passed a store/read/dedup/miss roundtrip test. **Not yet live-tested** against the running server (no `--reload`; needs `bcc_restart.bat`). Follow-up: image/pdf/staged paths got the timer fix but NOT the identity-card-before-write restructure (no screenshot there; URL path was the reported case) — easy parity later.

### Docs
Full plain-English walkthrough of the whole caching system written to **`docs/extract-cache.md`** — the two stores (`llm_extract_cache` / `media.db`), url-only key + freshness columns, 30-day TTL, the `static_subset` boundary, the speculative fast-path, complete-vs-incomplete self-healing, the screenshot BLOB store, and the "894ms-but-6s" bug explained. Start there for anything cache-related.

---

## Session log — 2026-06-02 — verbatim SERP queries, quadratic-only OU, Stage-2 scoring ledger + cohort panel; DataForSEO rejected

Continued from the OU/power blend work (commit `f04f09b`). Big session: validated-and-rejected DataForSEO, hardened the SERP query, standardized the fit, built the per-cohort scoring ledger + a UI to see it, and mapped (in memory) the dish-as-scoring-anchor architecture.

### DataForSEO investigated and rejected (kept as a spike)
Compared DataForSEO vs the existing SerpAPI + Moz stack on banana bread. Its **SERP disagrees with Google/SerpAPI** (rank Spearman **r = −0.41**, 0/10 top-10 overlap) and its **authority→OU fit is weak** (R² 0.22 vs Moz's 0.75 on the same dish). Traffic *ordering* validated against a SEMrush export (r = 0.67) and is non-redundant with position, BUT absolute magnitudes diverge **7–20×** and ETV is derived from the distrusted SERP. Verdict: DataForSEO earns no slot; **Moz DA+PA "power" is the signal** (free, already paid for). `scripts/dfs_rank.py` kept as explored-and-rejected (uncommitted; gitignored creds). Memory: `project_ou_power_blend`.

### Verbatim SERP queries (commit `5669d5d`)
`_serpapi_lookup` now sends the dish query to Google **verbatim** — no `-site:` splice, no auto-quoting/OR. Empirically, unquoted `banana bread recipe` returns banana-*fruit* junk (Wikipedia/Harvard/healthline) at the top — which is how a healthline "benefits of bananas" article extracted as a 3-ingredient "Banana Pancakes" and ranked **#1** in a banana-bread batch. Quoting `"banana bread"` drops all of it. But Google's NOT/AND/OR precedence is unreliable (parenthesizing a quoted OR *dragged the fruit junk back*; `"a" b -site:x` returned 0), so query construction belongs to the **admin**. Domain exclusion stays downstream in `_filter_disallowed` (deterministic). Thin pools surfaced in the log, never silently loosened (a fallback would re-admit the junk). `dishes_v2` field relabeled "Google search query — verbatim." **Migration:** existing dishes store plain phrases → now run looser (no auto `-site:`) until re-quoted, e.g. `"banana bread" | "banana nut bread"`.

### OU fit standardized on quadratic (commit `5669d5d`)
`_compute_custom_ou` + the chapters mirror now always fit **quadratic** (won best-R² 24/24, tiny margin over linear). Pins one formula shape and kills "model-flip jitter" (grades lurching when best-R² flips quad↔power on trivial data drift). Linear/power still computed + reported for transparency, never chosen. OU values unchanged.

### Stage 2 — per-cohort scoring ledger (commits `9f3e5ff`, `acd4776`)
The user redesigned `dish_run_data_points` into a scoring ledger (new cols `ou, power, ou_percentile, power_percentile, rank_score, model_version, cohort_status, selected`; index on `(dish_name, rank_score)`). Populated by SQL at job end:
- `replace_data_points_for_dish` stamps `model_version = jobs.id` (the `jobs` row already logs that run's fit + counts → it doubles as the version log; no separate table) and stores the **normalized** url (canonical key shared with the extract cache + `master_recipes.url_normalized`; existing rows backfilled).
- New **`score_data_points_for_dish`** runs ONE UPDATE: OU = `pa − (a·da² + b·da + c)` (full-precision coeffs from `last_ou_fit`), power = DA+PA, `PERCENT_RANK` percentiles (0–100, now the **canonical** percentile — Python `rank_by_blend` to retire), `rank_score = ((100−w)·ou% + w·power%)/100` (w = `power_blend_weight` = 30), `selected = 1` for winners. Below-min-n dishes skip → NULL (grade via chapter fallback).
- Verified: SQLite **generated columns can't do joins/subqueries/window fns** (so it's an UPDATE, not a computed column); proven on Cacio e Pepe (47 scored, blend arithmetic exact) + Spring Rolls n=16 (skipped). `model_version`'s eventual purpose: selective re-score of USER recipes (master is all-at-batch-end).

### Scored Cohort panel (commit `acd4776`)
New **`GET /dishes/{name}/cohort`** — direct ledger read (no recompute), LEFT JOIN `master_recipes` on `url_normalized` for winners' name/thumbnail/grade; non-extracted candidates show domain + scores only. `dishes_v2` "Scored cohort" panel renders the full cohort by `rank_score` with ou%/pwr%, ★ + thumbnail for `selected` winners — the diagnostic to compare `rank_score` top-N vs `selected` before Stage 3. Also: dish top-recipe rows now show `pwr` (DA+PA), order by blended rank, open same-origin `/r/<id>` (the external tunnel link was the "bad url").

### Architecture captured (design pass — queued, see memory)
The dish is the **scoring anchor**: it holds the fit + cohort + invariant identity. Master recipes are scored by the batch; **user recipes** get matched to a dish (vector NN over `dishes_vec`, identity-to-identity) and scored by *borrowing* that dish's fit + cohort (read-only percentile) — only for URL-sourced recipes (needs DA/PA). Decisions: (1) move the ~1,600 dishes from `chapter_shortcuts.json` (data-in-code) **into the `dishes` table within chapters** — one table, batched-ness is a state; classifier loads from the table; `__DEFER__` traps + aliases handled. (2) Embed all ~1,600 (v1 name+chapter) = the match universe (30 curated is too sparse for user recipes). (3) Batch is the **sole** master ingestion path; the "Promote" button is **repurposed** to add a URL to a dish's `editors_choice` (run through the batch each refresh, pinned regardless of rank) — no direct-to-master. (4) Dish-invariant facts (ethnicity/region/cuisine/story) pulled from the dish at read-time; technique/servingForm stay recipe-specific. Memories: `project_ou_power_blend`, `project_dish_catalog_table`, `project_master_recipes_ui`, `project_identity_card`.

### Queued next
- **Vector/catalog build:** dishes-table-in-chapters → embeddings → NN matching → user-recipe scoring + dish-fact read-time inheritance.
- **Recipe-form scores panel** update (held by user).
- **Stage 3:** make SQL `rank_score` the winner selector, retire `rank_by_blend` (validate via the cohort panel first).
- `cohort_status` vocabulary (reserved, TBD).
- Re-quote existing dishes' SERP queries to the verbatim quoted form.

---

## Session log — 2026-06-02 (evening) — vector matching live, recommender, curly-quote fix, product-catalog foundation

Continued from the day's scoring-ledger work. Theme: put the embeddings to work, fix the lingering banana-pancakes junk, polish dishes_v2, and lay the commerce/product subsystem foundation.

### Vector matching is live for user recipes (commits 19b407b, 3041536, 4de13fa)
- **Validated the matcher first:** NN dish-match on 277 dish-tagged master recipes → **97.5% top-1, 100% top-3** (the 7 "misses" are genuine near-twins: bone-in vs boneless thighs, potato-asparagus gratin). The identity-embedding matcher reliably recovers the right dish.
- **Embed every recipe at SAVE** (user *and* master) → new `recipes.embedding` BLOB. User recipes were never embedded; now the vector's there for matching / find-similar / dedup / recs (user: "we'll want it beyond this example"). Backfilled all 218 existing user recipes.
- **Match user recipes to a dish at save** → `_match` block `{dish (null if not confident), distance, confident (L2≤0.8), candidates[top-3], matched_at}`. Reuses the just-computed vector (no 2nd embed); logs `[MATCH]` to uvicorn_stdout.log. Backfilled 218 → 103 confident / 115 no-confident-dish (quantifies the catalog need).
- **Form:** a "Matched dish" chip in the recipe scoring strip.

### "Recipes you'd like" recommender (commit 714f8b6)
`POST /recipes/similar-master`: temp-embeds the (possibly unsaved) recipe → KNN over `recipes_master_vec` → drop far matches (L2>0.8) → re-sort by `rank_score`. **Recipe→recipe similarity — deliberately bypasses dish-matching** ("show me great recipes like this"). Form button "✨ Show top recipes like this" → top-6. Surfaced the lingering **"Banana Pancakes" (healthline) at rank 100** still in the Banana Bread set.

### The healthline root cause: CURLY QUOTES (commit c8e5509)
The Banana Bread query was `"Banana Bread"|"Banana Nut Bread"` **with smart/curly quotes** (`“ ”`) — Google ignores those (only straight `"` delimit phrases), so the "quoted" query silently ran loose and healthline returned. `_serpapi_lookup` now normalizes curly→straight before sending (intent-preserving, still verbatim otherwise). Verified: the stored query now returns only recipe sites; a re-run also clears stale junk via delete-and-replace.

### dishes_v2 + nav polish (commits b9d2ac8, 8510d48, 6cd3e71)
Both winner + cohort lists **headline `rank_score`** in **aligned fixed-width columns** (score/ou%/pwr%, tabular-nums) — no more ragged zig-zag; top-recipes endpoint LEFT-JOINs the ledger for rank_score; backfill-scored all 24 fitted dishes. "Scored cohort" = default-collapsed accordion; nav **Dishes → dishes_v2**; **Run-queued-jobs popup is now a draggable window**.

### Product / commerce subsystem — foundation (commit 60dffdb)
- Hierarchy decided: **3 levels mirroring recipes** — `product_category`≈chapter, `product_class`≈dish, `product`≈master_recipe. The **review site is provenance** (≈ a recipe's source domain), NOT a tier; a product aggregates MANY sources → a homogenization step. Ingestion unit = one bookmarkleted review.
- **`product_model.py`** (recipe-model analog): ProductClass / ReviewSource / Product (multi-source `verdicts`, brand-safe `bcc_blurb`/`critique`, our-own `RetailerOffer.affiliate_url`).
- **`intake/products/review_parsers.py`**: `detect_source()` + deterministic `parse_atk()` (custom code per source). Proven on a real ATK loaf-pan review fixture: **13 products** (tier/specs/price/retailers), no LLM, hydrating the Pydantic model.
- Brand-safety: verdicts record only what a *real fetched* review said (curator bookmarklets the actual page = verified by construction); BCC blurb is our own voice; affiliate links are ours, a source's tagged URL is identity-only; review URL kept for provenance but never rendered (don't send users away).

### Ops note
A detached restart window (`Start-Process bcc_restart.bat`) got `^C`'d → uvicorn died → cloudflare **502** until restarted. Lesson: run the server in a **persistent terminal**, not repeated detached restarts.

### Queued / next (designed; captured in memory)
- **Jobs need their own process** — they run inside uvicorn, so a deploy-restart kills in-flight batches (killed a Beef Stew run). Move to a **standalone worker** (also fixes the event-loop block that disabled the auto-runner); home for **queue kill/pause** controls. [[project_job_runner_disabled]]
- **Dish-invariant facts derive-once** (ethnicity/region/cuisine/story) → pulled from the dish at read-time; technique/servingForm stay recipe-level. [[project_dish_catalog_table]]
- **Batch = sole master ingestion path**; "Promote" button repurposed → add a URL to a dish's `editors_choice` (run through the batch). [[project_master_recipes_ui]]
- **User-recipe scoring** — borrow the matched dish's fit/cohort → percentile/grade (held "scores panel"). [[project_ou_power_blend]]
- **Catalog build** (~1,600 dishes into the dishes table, embedded) — match universe for arbitrary user recipes.
- **Product layers:** homogenization (2+ sources) → buy-enrichment (our affiliate links + live prices) → DB tables → ingestion endpoint + bookmarklet → master form (4th editor clone) → display popup → `equipment`→`product_class` link. [[project_affiliate_catalog]]
- **Re-quote existing dish queries** to verbatim quoted form; **Stage 3** (SQL `rank_score` as selector, retire `rank_by_blend`).

---

## Session log — 2026-06-03 — friendly site names + the Domain Master; portable-package architecture decided

Started from "do we get the user-friendly site name in addition to the URL?" and ended on a whole new canonical entity plus a settled product architecture.

### Friendly site name — captured + STORED on the recipe (not display-patched)
The plumbing already existed (`_source.siteName` from `og:site_name`, rendered as `siteLabel || domain` in the sidebar, and `site_name || domain` in the master/cohort/chapter lists) — but only **13/221** user rows and **247/361** master rows actually had the field, because most saves came via the bookmarklet/JSON-LD/markdown fast-lanes that never parse og-meta. The user's steer: *the name belongs on the recipe model, captured and stored* — not resolved at display time.
- New **`input/pipeline/site_names.py`** — `friendly_site_name(captured, url)`: prefers the page's captured `og:site_name` > the domain map > `""`; returns `""` for BCC self-URLs (bestcooksclub/tbotb/yboc) so our own pages never masquerade as a publisher.
- **Stored at the single save chokepoint** (`_save_recipe_core`) — every path (paste, bookmarklet, markdown, batch/master via the job runner) persists the resolved name. Also resolved at **extract time** so the form's "Site name" field populates pre-save, and **emit-time** at the master-list endpoint + chapter top-10 snapshot so stored snapshots display friendly names immediately.
- **Backfill** (`scripts/backfill_site_names.py`, idempotent): user **13 → 136**, master **247 → 307**.

### Domain Master — the `domains` table (new canonical entity)
The friendly-name map was the seed of something bigger. The user wanted it editable via the a/c/d template, *not hardcoded* ([[feedback_no_data_in_code]]), then kept enriching the vision (extraction tips, story, DA, country/language, logo, per-domain recipe lists). It consolidates **three roadmap items + new editorial fields** into one record: friendly name, the long-planned domain-quirks registry, the disallowed-domains list, per-domain Domain Authority (metabase_url keeps only per-URL PA), plus story/country/language/cuisine_focus/logo.
- **`input/pipeline/domains_lib.py`** + **`domains` table**, keyed on **full host** (grain decision — subdomains carry distinct names/tips; `root_domain` rides along for DA + blocking). `domain_display_names.json` demoted to a **bootstrap seed**.
- **CRUD** at `/domains*` (ungated in dev, TODO re-gate `edit_master` — matches the chapters sibling editor). **`forms/domains.html`** a/c/d editor cloned from chapters.html. **Domains** + **Messages** (status_messages admin) added to the hamburger nav; asset versions bumped to `20260603a`.
- **`allowed=0` → batch block.** `_filter_disallowed` (`intake/build_query_batch.py`) now reads `domains_lib.get_blocked_root_domains()` (cached, invalidated on edit) ∪ the config list. Root-grain (SERP entries carry `root_domain(url)`). `seed_disallowed_domains` seeded the 9 config-disallowed sites as `allowed=0`. Verified: wikipedia/youtube dropped, real recipe sites kept.
- **Per-domain recipe browse:** `GET /domains/{domain}/recipes` computes master/user lists live, **STORES the two counts** (`master_recipe_count`/`user_recipe_count`, refresh-on-open) but not the lists. Editor shows two jump-to dropdowns + counts; list meta shows `Nm · Mu`.
- **`scripts/populate_domains.py`** (idempotent, edit-safe — only fills blanks, never touches curator edits/allowed): every host we have recipes from now has a row, enriched with siteName/root/max-DA(from `_scoring`+`metabase_url`)/counts. **96 → 270 rows**, 234 with DA, 195 named.
- **✨ Enrich button** → `POST /domains/{domain}/enrich` → **`extract/domain_enrich.py`** (Haiku `tool_use`, token-journaled): story/language/country/cuisine_focus; logo is **deterministic Clearbit** (`logo.clearbit.com/<root>`), never LLM-hallucinated; `recognized` flag surfaces low confidence. Returns suggestions for review (does not auto-save). Verified live: NYT Cooking (recognized, en/US) + My Greek Dish (Greek/Greece, recognized=false).
- Grain/boundary memorialized: `cuisine_focus` is a HINT — authoritative recipe ethnicity stays dish-derived ([[project_identity_card]]). Memory: [[project_domain_master]].

### Architecture decided — portable PRODUCT, multi-instance, no in-code tenancy
A long design thread (the user built multitenant DB systems since the 1970s). Two decisions, both memorialized:
- **[[project_portable_package]]** — ship a **portable, self-hostable product** (WordPress/on-prem model), NOT a SaaS tenant. *Code never changes; data defines the instance.* Discipline: every customizable knob reaches the app via an a/c/d form or a top-level **system record** (the data-in-code purge becomes a hard requirement, since a recipient never opens the source). System record is the **live config** (cached, invalidated on save) via a future `get_setting()` — NOT round-tripped through OS env; secrets stay in the host's env, referenced not embedded. One irreducible bootstrap pointer (`BCC_DB`). A first-run installer is the one code-defined form. Deliverable can be code + a vertical **seed DB** ("Greek cooking starter pack"). Stack already fits: single-file SQLite + `ensure_*_table`/`ALTER`-on-boot self-install/upgrade. The domain master is an early brick of this.
- **Multi-instance, NO multitenancy in the code** (decided): shared-schema `tenant_id`-everywhere rejected (invasive, leakage risk, contradicts per-DB package). "Many sites" = process-per-instance behind a reverse proxy (ops, not code); database-per-instance routing is a possible later optimization. Rationale: **performance** (no tenant filtering, small per-site DBs/vec indexes, each its own writer), **portability** (instance = the unit), **redundancy** (shared-nothing, no blast radius). Consequence to design deliberately: no cross-instance join → anything global (cross-site search, portfolio analytics, central identity, the "Best of the Best" master cookbook) needs a shared **upstream** instances pull from.

### Ops / verification
Server has no `--reload`; restarted it **detached/hidden** via the venv uvicorn (PID 66436) rather than the foreground `bcc_restart.bat` window, logging to `uvicorn_*_agent.log` (now gitignored). Verified throughout via `py_compile` + FastAPI `TestClient` (CRUD round-trip, host canonicalization, 404s, page loads) and live HTTP against the running server (enrich on two domains). `recipes.sql` dump refreshed (`backup_db.py --no-adam`) so the git-side backup carries the new table + backfill.

### Follow-ups opened
- `allowed` blocks at the batch filter, but the config `_DEFAULT_DISALLOWED_DOMAINS` weren't *deleted* — table+config are unioned (safety net); decide later whether config is retired.
- `domain_authority` column exists + populated from corpus, but nothing **refreshes** it from Moz at the domain grain yet (`da_last_scored` read-only).
- Domain writes ungated in dev — re-gate `edit_master` before public exposure.
- The big one (queued, not started): the **system record + `get_setting()` loader + first-run installer** — the apex the portable-package model externalizes into. Then the data-in-code purge inventory (RECIPE_PHRASES, LLM prompts → a `prompts` table, thresholds/weights, model IDs, branding, DB paths).

---

## Session log — 2026-06-03 (evening) — Greek-batch fixes verified end-to-end; dish-form run log; variants design

A long working session driven by trying to harvest a Greek-only dish (`Pastitcio (Greece)`, query `site:.gr … παστίτσιο`). It exposed five separate extraction/ranking bugs (all fixed and verified live against a real Greek-pastitsio refresh), restored the in-form run log, and produced a design note for the dish↔recipe relationship.

### The five Greek-batch fixes
1. **min-OU floor punished low-authority foreign sites.** The first `.gr` run kept only 3 of 10 good recipes — the rest had *negative* OU because, with the cohort under the 25-row fit floor, ranking fell back to the **global/US-calibrated** OU formula, against which Greek sites (lower DA/PA) score negative almost by construction. TEMPORARY fix: `_query_targets_foreign_country()` (`build_query_batch.py`) detects a `site:.<ccTLD>` operator and relaxes the negative-OU floor for that batch (`_min_ou_filter(drop_below_threshold=False)`; unscoreable rows still dropped). The proper fix is per-variant/locale-aware OU — see the design note. Verified: the relaxed run kept 19/19 → top-5 from a healthy pool.
2. **Interactive (URL-field) extraction left Greek pages untranslated.** `md_result['language']` is detected on the *narrowed* main-content node and trusts `<html lang>` outright, so a thin sample or a bilingual template mislabels a foreign page `en` → translation skipped → recipe extracted in Greek AND the is_recipe phrase score computed on untranslated text (the **jsonld-direct fast lane has no translation of its own** — it relies on the page-detect gate). Fix: when the cheap signal says English, **re-detect on the FULL assembled markdown** (`save_recipe_api.py`), mirroring the staged/bookmarklet path; a non-English result wins (a miss biases to the unsafe "no translation"). This auto-diverts off the untranslated fast lane.
3. **Bookmarklet cache hit paid the full ~30s translation.** The `/extract-from-markdown` path ran the expensive translation *before* the cache lookup, so a non-English recipe that was already cached still translated before checking — the "instant via URL field, 33s via bookmarklet" asymmetry. Fix: **cache lookup first**, translate only on a miss.
4. **e-sofia.gr crashed the whole fetch.** `extruct`/lxml refuses a `str` carrying an `<?xml … encoding=…?>` declaration. `extract_recipe_jsonld` (`html_to_markdown.py`) is now resilient — strips the declaration and retries, and returns `[]` on any parse failure (JSON-LD is optional; the page still converts to markdown).
5. **Wayback provenance polluted scoring/cache.** For dead live pages the front-end stamps the *working Wayback snapshot URL* as the extraction input, so the recipe got keyed, scored, and provenance-stamped as **archive.org (DA 94)** instead of the real publisher. First fix only handled an *internal* fallback; the complete fix **un-wraps any `web.archive.org/web/<ts>id_/https://site/…` URL to the embedded original** (`_unwrap_wayback_url`), covering both cases. Purged the 2 archive-keyed `llm_extract_cache` rows. **Verified:** a clean re-refresh (job #89) saved 4 Greek pastitsios, all `translated=1`, all real publisher roots (argiro/gastronomos/alwayshungry/athinorama) — zero archive.org.

### Dish-form run log restored + made first-class
The batch log had regressed to the nav's **"Run queued jobs" overlay** (a `coming-soon-overlay` popup that light-dismisses on backdrop click) because the dish "Run" button only *enqueued* (the background poll is off, [[project_job_runner_disabled]]) and the user drained via the nav. Rebuilt as an **in-form panel** (`dishes.html`): a persistent `#dishLogPanel` mounted as a **sibling of `#detailPanel`** (so `renderDetail()`'s `innerHTML` rebuild can't wipe it), which shows the selected dish's latest **saved** log inline (fetched from `/logs/<file>`, already written per-run), **streams live** during a run, has a **download** link, and **auto-scrolls into view** on run start. The dish "Run" button now **runs the job itself** — enqueue + `POST /jobs/run-queued` drain + stream — so the whole run stays in the form, no popup.

### Hamburger auto-scroll
`LibraryShell.openSidebar()` now scrolls the window to the top when the menu opens (the menu lives at the top of the document; on a scrolled-down page it opened above the fold). Asset version bumped **20260603a → 20260603b** across all 7 pages so the new `library-shell.js` actually loads past the cache.

### Design note: dish ↔ recipe relationship (docs/dish-variants-membership.md)
A long design thread (Greek vs US vs vegan "variants" of one dish). Walked through and rejected a many-to-many junction, landing on **one-to-one membership + curator tie-resolution**, **per-variant grading** (pooling punishes the minority cuisine — same root cause as fix #1), identity at the **header** grain, and **language as a KNN pre-filter** (not an embedding token — variants are identity-degenerate) sourced from **Domain Master** publisher locale (page `originalLanguage` misses bilingual publishers like Akis). Memory: [[project_dish_variants_membership]]. Not built — design only.

### Ops / follow-ups
- Server restarted detached several times via the venv uvicorn (logging to `uvicorn_*_agent.log`); jobs run on demand via `POST /jobs/run-queued` (the poll stays disabled).
- **`recipes.sql` is now stale** — the test refreshes (jobs 87–89) rewrote `Pastitcio (Greece)` master rows; run `bcc_backup.bat` to refresh the git-side dump.
- HTML files have no `Cache-Control`, so soft reload serves stale pages — a recurring "I don't see the change" trap. Consider serving HTML `no-cache`.
- `extract/chapter_shortcuts.json` had an **accidental IDE edit** (deleted `israeli couscous`/`jambalaya`, left garbage `prmbalaya`) — excluded from this commit; needs reverting.

---

## Session log — 2026-06-05 — THE SPLIT begins: three-entity architecture + Recipe Enrichment API (branch, autonomous)

Per `SplitSpec.md`, started separating the codebase into independent products. This session was run **autonomously** (user offline); all judgment calls are in **`docs/split-decisions-log.md`** (DL-1..9). Work is on branch **`split/enrichment-api`** to keep master shippable (DL-1) — NOT merged.

### Terminology locked (with the user, before he went offline)
- **TBOTB** = the master/corpus = "best of the best" engine + search-engine *destination*. Crawls, scores, critiques, ranks; owns the master DB. Public surface emits **work-product + JSON-LD rich-result envelope + link only — never a substitute recipe**.
- **BCC** = the Local tool — the user's personal capture/possess/cook app.
- **Recipe Enrichment API** = a **third, neutral entity** (productized shared core); BCC + TBOTB are its first two customers (SplitSpec §43). Content → structured recipe; **no fetch · BYOK · stateless · no scoring · seal-at-emit**. Inference billed by the LLM vendor to the caller's own account (BYOK) so the API's fee is a constant value-add toll. Scoring stays in TBOTB (the moat); the API only generates the vector. Subscription relationship = **"ask TBOTB live" read API** (vector + DA/PA up, never stored; grade/rank down).

### Phase 1 map (`docs/split-phase1-map.md`) — every table/module/endpoint/job/UI bucketed
Headline entanglements flagged: **promote-to-master** (user→corpus pipe — CUT), the **local extract reading `llm_extract_cache`** (the no-bridge trap — *this is the caching work from 06-01; it lands on the chopping block for the Local side*), **claim** (copies the corpus body down), and the **shared `recipes.db`** (must physically split). Still-open calls: auth/users ownership, `domains` extraction-tips routing, config split.

### Built this session — `recipe_enrichment/` package (strangler pattern)
- **`api.py`** — `enrich(EnrichmentRequest) -> EnrichmentResult`. v1 owns the **extract** step (JSON-LD fast lane → markdown LLM), delegating to the existing `extract/*` (no code moved — DL-2). Enrichment blocks selected **by name off the live `ENRICHMENT_BLOCKS` registry** (add-as-we-go, no-data-in-code). Steps 2-6 (translate/sanitize/enrich/identity/embed) are accepted-but-deferred carves.
- **`serialize.py`** — the canonical **`full | static | public`** profile serializer. `corpus_public_view()` is a strict WHITELIST (work-product + envelope + keys + **mandatory source link**; refuses to emit without one); seals the body, prose, and our cooped image.
- **`service.py`** — the HTTP surface (`POST /enrich`, `/health`) BCC/TBOTB call at the split, with the BYOK-at-edge stub. Not used in build-up (monolith calls in-process).
- **`tests/`** — 8 deterministic tests (JSON-LD path, no LLM), runnable with pytest OR standalone; **8/8 pass**.
- **Monolith reroute** — `extract_recipe_from_url` routes its extract step through `enrich()` via `_extract_via_enrichment_api`, gated by **`BCC_ENRICHMENT_API` (default OFF)** — with it off the path is byte-for-byte the current code (DL-6). Verified by construction + unit smoke; did NOT restart the live server onto the branch (DL-8). The bypassed inline code becomes the empirical cut list once the flag runs on (SplitSpec Phase 3 gate).

Commits on branch: `b36fcfe` (map+package), `df03879` (flag-gated reroute), `0ef7976` (service+tests). **Next carve = identity card** (see the carve plan in the decision log).

---

## Session log — 2026-06-06 — Recipe Enrichment API: extract carve, fast-lane fix, enrich blocks, test harness, multilingual

Continued THE SPLIT on branch **`split/enrichment-api`** (NOT merged; master clean). Full decision trail in **`docs/split-decisions-log.md`** (DL-1..13) and the map in **`docs/split-phase1-map.md`**. Highlights:

- **Strangler carve of the extract step** into `recipe_enrichment.enrich()`, flag-gated by `BCC_ENRICHMENT_API` (default OFF = byte-for-byte legacy). Verified live: fresh URL → enrich(); repeat → 9ms cache hit.
- **JSON-LD fast-lane fix (DL-10):** `RecipeModel` rejected `video.thumbnailUrl` as a list → one peripheral field forced the ~17s LLM. API coerces video + logs ANY pydantic miss as a greppable `PYDANTIC FAST-LANE MISS … fields=[…]`. Sally's: ~25s → ~10s, video recovered.
- **Enrich carve (DL-11):** `/enrich-recipe` routes through the API with INDIVIDUALLY-selectable blocks (registry-driven `run_enrichment_blocks`); "there will be more" needs no code change.
- **Scoring as INPUT:** Moz stays TBOTB's (the moat); the API consumes PA/DA/OU for editorial scoreCommentary, never fetches.
- **Multilingual (DL-12/13):** non-English → translate the **JSON-LD** and use the fast lane (not discard it); translate **iff source ≠ `target_language`** (instance/user canonical lang, env `BCC_TARGET_LANGUAGE`, default en) — a Greek instance keeps Greek. Verified live.
- **Video form block** on `recipe_form_styled.html` + **test harness** mounted at `/enrich-api/` (live request-payload preview, registry block checkboxes, Moz-fetch convenience, profile seal).
- **`recipe_enrichment/`** package: `api.py` (translate→extract→enrich→seal), `serialize.py` (full/static/public seal + mandatory link), `service.py` (HTTP + harness), `tests/` (14 deterministic, all pass).

Server runs on the branch with `BCC_ENRICHMENT_API=1`. Open follow-ups (in the decision log): staged/bookmarklet path reroute, arbitrary translation targets, per-user language, **measurement/unit conversion** (density-table — user providing data), chef-bio table, Playwright browser-fetch for bot-blocked sites, extract error-message reword.

New memories: `project_split_architecture`, `project_chef_bio`, `project_playwright_fetch`; updated `project_db_backup` + `project_multilingual_extraction`.

---

## Session log — 2026-06-06 (cont.) — ingredient-aware measurement conversion integrated into the Enrichment API + editor metric/imperial toggle

Took the measurement-conversion starter kit (Desktop Claude, 2026-05-31; King Arthur density data) and integrated it into the shared **Recipe Enrichment API**, then surfaced a metric/imperial toggle in the recipe editor. Still on branch `split/enrichment-api`.

### The conversion engine + hybrid resolution (`recipe_enrichment/measurement/`)
- Moved the starter kit into the shared core (single canonical path; root `measurement/` removed). `convert.py` = the deterministic engine — 3 domains (volume/mass/count) bridged ONLY by density (g/mL) + grams-per-item; cross-domain routes through grams; **missing bridge raises** (no guessing). Data: `kingarthur_ingredient_weights.json` (317 ingredients + 5 count items, real KA gram weights, JSON not code) + `build_ka_data.py` provenance.
- **Hybrid resolution** (user's pick): `parse.py` (deterministic line parser — vulgar/mixed fractions, ranges→avg, bare counts, "of" filler, parentheticals, "to taste"→non-convertible; keeps `name_raw` w/ prep) → `resolve.py` (name→KA key: exact/alias → singular/plural → unit-aware count candidates → conservative fuzzy, prep-clause cleanup) → `recipe_pass.py` (orchestrates; one batched **LLM fallback** identifies-only for misses, the engine still does all math).
- Output: `recipe['_measurements']`, 1:1 with `recipeIngredient`, each `{raw, metric, qty, unit, ingredient, canonical, grams, ml, convertible, resolved_by}`. **`metric` display = food-aware weight** (grams; mL for pure-volume-no-density; None for counts) and PRESERVES prep text ("1½ cups flour, sifted" → "180 g flour, sifted").

### Wired into the API + persistence
- `EnrichmentRequest.do_measurements` runs the pass after extract/translate. `_measurements` declared on `RecipeModel` (alias) + added to `STATIC_TOP_LEVEL_FIELDS` so it survives `model_dump`/cache/claim; form passes it through on re-save.
- **`POST /convert`** (service.py) single conversion w/ smart resolver; test harness got a `do_measurements` checkbox, a measured-counts badge, and a Quick-convert widget.
- **Recompute on save** at the chokepoint `_save_recipe_core` (per user steer "safer to recompute"): deterministic-only (free + instant, no LLM on the frequent save path), recomputed from CURRENT ingredients so it never goes stale; **carries prior LLM resolutions forward by raw string** (`prior=` param) so a plain save never downgrades/re-pays exotic ones. Covers ALL save paths, not just URL extract.

### Editor UI (`recipe_form_styled.html`)
- **Apple-style Metric toggle** in the Ingredients header — instant client-side display swap; OFF=imperial (default). Imperial is always the saved source of truth (metric rows read-only; `getIngredients()` returns `dataset.imperial`), so the toggle can't corrupt data. Metric matched to rows BY STRING (survives reorder/edit).
- **"↻ Measurements" button** (always visible; user's idea, mirrors the re-derive pattern [[project_rederive_preview_button]]) → `POST /recipes/measure` (new endpoint, mirrors `/enrich-recipe`: deterministic + LLM for exotic, journals usage) → updates row metric data, reveals + flips on the toggle. On-demand, flag-independent — works for any recipe (extracted/manual/edited). This solved the "didn't see anything" report (toggle self-hides until measurements exist).

### Verified / ops
- 26 measurement tests + 14 enrichment tests pass; `_measurements` round-trips through `sanitize_recipe_data`; `/recipes/measure` returns 200 with correct conversions (flour 120 g/cup, sugar 198, honey 336, eggs 50 ea, butter sticks). Confirmed the running server serves the fresh HTML — the user's initial "no button" was a stale browser/Cloudflare-edge cache (server was fine; `Ctrl+Shift+R` doesn't bust the edge — use `localhost:8009` or a cache-busting query while developing).
- **Open:** user may **adjust the toggle/button UI** (considered moving the toggle "up top" as a global control — left in the ingredients header for now). BYOK still ambient env key in the measure fallback (DL-5). Image/PDF/bookmarklet extracts get measurements via the save-recompute (deterministic) but not the extract-time LLM pass. No metric/imperial display-string derivation beyond the stored grams/mL.

---

## Session log — 2026-06-08 (cont.) — reusable ImageWell control + HTML no-cache + image Phase 1 (coopt/standardize/imageMeta)

Continued the same session. Built a reusable image control and the start of the image-processing pipeline. All on `split/enrichment-api`.

### Reusable ImageWell control (`forms/image-well.js`) — commits 6c3c530, 917cefe
One drop-in control (`ImageWell.mount(el, opts) → {getUrl,getMeta,setUrl,clear}`) that owns the image-capture UX; the host passes backend handlers + `onChange`. Replaced the recipe form's hero box + URL field + Generate button. `#heroImageUrl` kept as a HIDDEN value bridge so save/load/extract are unchanged.
- **Inputs:** drop, paste, click→"Set image" dialog, URL, Generate. "Figures it out": bytes→`/images`, URL→`/images/fetch` coopt with **hotlink fallback**.
- **Paste UX (the fiddly part):** right-click "Paste" only delivers image BYTES on **contenteditable** surfaces — so the frame + dialog drop-zone are contenteditable (children `contenteditable=false` + `pointer-events:none` so right-click reaches the editable frame even over the image; `beforeinput`/keydown guards block real editing; caret hidden). Dialog auto-focuses the URL field; a dialog-level paste handler catches a Ctrl+V image anywhere and stops it leaking to the recipe extractor. Generate lives ONLY in the dialog.
- **Bug fixed:** image-not-saving — the save spread a stale `_source.previewImage` that masked a new `image[0]` on reload; now `previewImage` mirrors the hero. Self-contained CSS (injects its own `<style>`) so it has no external-stylesheet dependency.
- Reuse target: the master/dish editors + the multi-page capture tray (#3) lean on this.

### Serve HTML `no-cache` — kills the stale-HTML trap (in 6c3c530)
`/forms` now uses a `_NoCacheHTMLStatic` subclass → `.html` responses get `Cache-Control: no-cache, must-revalidate` (ETag still yields cheap 304s). Versioned JS/CSS keep their long cache via `?v=`. No more "I don't see my change" / hard-refresh dance (also signals Cloudflare not to edge-cache HTML).

### Image pipeline Phase 1 — coopt-to-local + standardize + imageMeta + logging (config-driven) — NOT yet committed
First slice of the agreed image roadmap (#1 of 4: Phase 1 → multi-page tray → transform spec). `/images` (upload) and `/images/fetch` (coopt) both previously stored RAW bytes; now they route through `image_pipeline.standardize_and_meta()`:
- **Standardize** (Pillow, config-driven): EXIF-transpose, flatten→RGB, center-crop+scale to the landscape/portrait bucket, progressive JPEG, EXIF stripped. Targets + quality read from `system_config` (`image_jpeg_quality`, `image_landscape_target`, `image_portrait_target` — new "Images" settings category). `process_thumbnail` refactored into reusable helpers (`_open_oriented`/`_to_rgb`/`_fit_and_encode`/`_img_config`).
- **imageMeta** captured + returned + **logged**: `{width,height,format,bytes,orientation,orig_width,orig_height,orig_format,bytes_in,localized,source_url,standardized}`. Pillow failure → fall back to storing raw (meta `standardized:false`).
- **Persisted:** `_imageMeta` declared on `RecipeModel` (alias) + added to `STATIC_TOP_LEVEL_FIELDS` (extra='allow' DROPS undeclared fields on `model_dump` — the [[feedback_db_form_sync]] trap). ImageWell captures meta from the endpoint responses (`getMeta()`/`onChange(url,meta)`); the recipe form stashes `_heroImageMeta`, saves it, restores it on load.
- Verified: TestClient upload of a 1200×400 PNG → standardized 1500×1000 JPEG + full imageMeta; `/system-config` shows the Images settings. Coopt-to-local now means a pasted/typed URL becomes a permanent right-sized local JPEG we own (no hotlink) — also reinforces the DL-16 per-entity image policy.
- **Deferred to later phases:** the transform property-sheet + DSL (#2, incl. the `degrade` preset), the multi-page capture tray (#3, semantic multi-image stitch). [[project_image_policy]] (DL-16) governs third-party hero use.

---

## Session log — 2026-06-08 — batch off FastAPI (jobs CLI + WAL), dish scheduler, DB system config, two-hamburger nav

A long session that took the jobs-as-executables design (docs/jobs-as-executables.md) from spec to a working standalone batch runner, then opened the DB-resident system-config subsystem and split the nav into admin/user hamburgers. Still on branch `split/enrichment-api` (not merged).

### Recipe master: delete clears the form
Delete now runs the canonical **Clear** path (full teardown — lists, hero image, scoring strip, badges, `/r/<id>` URL bar) instead of a partial `form.reset()`, which left the deleted recipe's dynamic content on screen. Feedback message shown after the clear (which wipes feedback). `recipe_form_styled.html`.

### Batch runs are now independent of FastAPI — `python -m jobs`
- **WAL prerequisite DONE**: `init_db` sets `PRAGMA journal_mode=WAL` as its first statement (persistent in the DB header → inherited by every process; default `sqlite3.connect` 5s busy_timeout makes concurrent server+job writers queue, not collide). Verified live `journal_mode=wal`.
- New top-level **`jobs/` package** (`python -m jobs run|schedule|next|drain|list`) — promotes `scripts/run_next_job.py` to a first-class entrypoint. Every run funnels through the existing `_run_one_job` (same path as the form Run button), entity-locked, exits 0/1 so Task Scheduler sees pass/fail. **§3.1 rule honored**: `--dish` passes dish IDENTITY only; the SERP query (with embedded straight quotes) is read from the DB, never argv — no `--query` flag. Both decision forks resolved (L1 logs, three triggers) in the design doc.

### Dish scheduler (per-dish next-run date)
- `dishes.next_run_at(ttl, last_refreshed)` — DERIVED (not stored), surfaced in `row_to_dict` + the `dishes_v2.html` detail (status pill "next run …" + Next-run KV row). Verified: Carbonara ttl=59 → 07/31.
- `python -m jobs schedule` runs every due dish (`find_due_dishes`), entity-locked; honors DB config (see below); `--force` / `--dry-run`. The long-referenced `refresh_due_dishes.py` agent is now this command.
- **Windows Task Scheduler `BCC Dish Schedule`** registered as an **hourly heartbeat** (`bcc_schedule.bat`, CRLF, 3h kill-cap, no-overlap). The real cadence + on/off live in the DB, not the OS.

### DB-resident system config — the "system record" apex begins
User steer: scheduler cadence (and config generally) belongs in a **DB-resident config file edited via an admin UI**, not baked into Task Scheduler — the apex of the portable-package model. Built the scheduler slice:
- **`system_config` table** (key/value rows: `key, value(JSON-typed), type, category, label, description`) + bootstrap seed; `get_setting()` cached + invalidated on write; `set_setting`/`list_settings`. `input/pipeline/system_config.py`.
- `/system-config` GET/POST (read-only + unknown-key guards → 400; TestClient-verified).
- **`forms/system.html`** single-record settings editor (grouped by category, typed inputs, read-only last-tick) + **System** nav item. Verified live (toggle/interval/save).
- The scheduler reads `scheduler_enabled` + `scheduler_interval_hours` (gate vs `scheduler_last_tick_at`); `bcc_config.json` stays the file seed, migrating in incrementally. Memory: [[project_system_config]].

### Two-hamburger nav (admin / user split) — groundwork for the TBOTB/BCC split
- `library-shell.js` `initNav` now renders **two burgers**: a user burger (always) + an admin burger (shown only when `/auth/me` role is admin/owner, or you're already on an admin page). `NAV_ITEMS` tagged `group: 'user'|'admin'`: user = Recipes/Cookbooks/Equipment/Gourmet/Install; admin = Dishes/Chapters/Domains/Users/Run-jobs/System/Messages. Distinct dark color for the admin burger. **Both burgers added to admin.html (Messages)** (it had no nav before → fixed-fallback burgers).
- Verified live via Playwright (owner sees both; member sees user-only on user pages; both on admin pages).
- **Overlay bug fixed**: the dropdown was `position:absolute` but positioned with viewport coords, so on a scrolled page it opened near the document top (you had to scroll up). Now `position:fixed` on open → overlays the current viewport under the (fixed/sticky) header toggle. Also fixed `system.html` missing `library-shell.css` (unstyled, unresponsive burgers). Asset version bumped `20260608a → b`.

### Ops
- Server restarted onto the branch code via detached venv uvicorn (logging `uvicorn_*_agent.log`); `BCC_ENRICHMENT_API` unchanged. `jobs_schedule.log` gitignored. New memories: [[project_jobs_as_executables]] (forks resolved), [[project_system_config]], [[feedback_cli_args_identity_not_query]].

---

## Session log — 2026-06-08 (evening) — ImageWell paste/resize/moderation, out-of-process UI Refresh (phase 4), dish limits, match-cutoff config

Continuation on `split/enrichment-api`. Commits `7bacca6 → 0d57ed8 → d954441 → fc8e442 → f59bb2e → (this)`.

### ImageWell — paste, Save-enable, contain resize, tweak persistence
- **Right-click Paste was greyed cross-browser** (Chrome + Edge): the cause was `contenteditable="false"` child islands — especially the `.iw-overlay` covering the frame at `inset:0` — which make the browser see a non-editable region at the click point and disable Paste. Removing ALL `contenteditable=false` children (the well is now one uniform editable region, exactly like the working recipe drop-zone) fixed it. Ctrl+V (hover-scoped) + drop + dialog paste also work. (`role="button"` was also dropped.)
- **Save now enables on image alone**: `hasContent` includes the hero URL and the well's `onChange` calls `updateButtonStates()` (a programmatic `.value=` doesn't fire `input`).
- **Hero resize fixed (was "pretty bad")**: a user hero now uses a **contain** resize (`_contain_and_encode`) — preserves the whole image + orientation, longest edge ≤ `image_hero_max_px` (1600, System → Images), **never upscales** a small source. The corpus og:image coopt keeps the uniform 1500×1000/1000×1500 crop (`_fit_and_encode`) on purpose. `input/pipeline/image_pipeline.py`.
- **Tweak-prompt moved into the Set-image dialog** (ImageWell `generatePanel` hook) and now **persists**: captured on Generate, saved in `_imageMeta.tweak`, re-shown in the dialog on reopen (new `onDialogOpen` hook) + on recipe load; cleared with the image.

### Image-gen content moderation (the "no crazy stuff in the tweak" ask)
- Anthropic has **no standalone moderation endpoint** (in-model only). Since gen is OpenAI gpt-image-1, the tweak is pre-screened with OpenAI's free `omni-moderation-latest` (`moderate_text` in `image_gen_openai.py`): flagged → instant friendly **400**, no ~57s wait then gpt-image-1 rejection. `TWEAK_MAX_CHARS=600` (client `maxlength` + server). The generate endpoint now returns a clean **400** on a gpt-image-1 safety reject (was 502 → Cloudflare swaps origin 5xx for its own HTML → the form's `res.json()` failed → "Generate failed: {}"). Verified: the bikini tweak → 400 in 0.45s; benign tweak passes.

### Dishes — detail re-render + out-of-process Refresh (phase 4) + inline log
- **`doSave` now re-renders the detail** (`renderDetail(data)`), not just the sidebar — derived pills (TTL, next-run, status) were stale until reselect (the "TTL pill never changed" bug). `renderDetail` re-wires all buttons; verified TTL 45→60→45 with every button live.
- **Phase 4 — UI Refresh runs OUT-OF-PROCESS**: `python -m jobs exec --job-id N` runs one enqueued job; `POST /jobs/{id}/spawn` Popen's it (no console window, own log file); `doRefresh` = enqueue → spawn → stream. **Different dishes refresh in parallel, UI stays responsive, a server restart can't kill a run** (recipes.db is WAL). The SSE `/jobs/{id}/stream` reads the DB + log file → fully cross-process. Verified end-to-end with a throwaway job + a real Risotto refresh (#110 success, 8 kept).
- **Inline live run-log in the dish page** (`#dishLog`) — the log streamed only into the nav overlay before, and the detail didn't refresh on completion ("where is the log???"). Now `streamDishLog` tails the SSE inline and on `done` re-fetches the dish + re-renders the detail. The dish's `last_run_log_url` "view log" link already pointed at the completed run.

### Limits + Matching (DB config)
- **Dish row-count limits** (fat-finger guard — a typo'd 2550 would ask SerpAPI for 2550 rows/query): **System → Limits** `dish_max_serpapi` / `dish_max_final` (default **200**). Rejected at create/edit above the cap, and **clamped at job-run** as a safety net. Form number inputs carry a `max` hint from the config.
- **Match cutoff is now config** (was hardcoded `MATCH_MAX_DIST=0.8`): **System → Matching** `dish_match_max_distance` (default **0.85**). Diagnosis that prompted it: "Dan's Ultimate Shrimp Risotto" matched **Risotto** at L2 **0.8152** but was flagged not-confident under 0.80 — a real match a hair over the line; real matches ≤0.25 or ~0.82, true negatives ≥0.98, so 0.85 is safely in the gap. **Re-save #547 to flip it to a confident Risotto match.**
- **One metric end-to-end (distance, not cosine)**: the recipe meta panel showed cosine while `_match.distance` (the DB + KNN) is L2. Relabeled "Confidence (cosine)" → **"Match distance (L2)"** and shows distance; `grading.py` now also stamps `match_distance` (L2) in the basis alongside the legacy cosine `match_confidence`. (cosine 0.668 ↔ L2 0.815 — same match.)

### Ops
- 5 restarts onto branch code (each gated on no running job — held once for ~26-min Risotto #108, polled via a background `until`-loop, then restarted). New memory candidates: none beyond the above (all captured in commits + this log).

---

## To-do
- **Drop the dead jobs-queue drain + repurpose the menu (2026-06-11, NOTED — leave for later).** The queue-DRAIN is vestigial: jobs now enqueue→spawn out-of-process (`/jobs/{id}/spawn`) and go straight to running→success/error; the `jobs` table has ZERO queued/running rows (only success/error). Dead drain code to remove: `save_recipe_api.py` `POST /jobs/run-queued` + `_drain_queued_jobs`; `library-shell.js` the "Run queued jobs" overlay + count-badge + `runQueuedJobs()` + exports; the `dishes_v2.html` spawn-fail fallback at ~line 509 (replace with a plain error — `runQueuedJobs` is its only live consumer). Bigger-scope dead code to confirm before deleting (whole files/commands): `jobs/__main__.py` `cmd_drain`, `scripts/run_next_job.py` (superseded by the `jobs` package), orphaned `forms/dishes.html` (old dish page, only in comments now). **Repurpose:** the (already-renamed) "Jobs/Queued" admin menu item → a **Jobs/Activity** page powered by the existing `GET /jobs` (recent runs: status/timing/log links). `jobs_admin.html` ("Jobs/Scheduled") stays — it's the scheduled-jobs editor, a different thing.
- **Public read-only "cookbook" pages — SEO + AI-bot compatible (2026-06-01).** Today `/r/<id>` resolves to the JS editor *form*, which bots see as an empty shell — wrong surface to expose. Build a **separate, server-rendered, read-only recipe page**: complete resolved HTML (no editor chrome), crawlable. Big head start: recipes are already stored in **schema.org shape**, so the page can emit a `<script type="application/ld+json">` **Recipe** block nearly for free (Google Recipe rich-results *and* AI crawlers parse it) + plain semantic HTML (title/hero/ingredients `<ul>`/steps `<ol>`/times/yield) + OpenGraph/Twitter meta + `<link rel=canonical>`. **Scope:** curated/cookbook set only (master_recipes / a `published` visibility flag) — private user saves stay non-crawlable. **Discovery:** `sitemap.xml` of public recipe URLs + `robots.txt` (+ optional `llms.txt`). **Payoff that closes a loop:** a public page *with* JSON-LD is itself extractable — so "paste a BCC URL" would then legitimately work against the cookbook page, not the form. **Generation fork:** (1) on-demand SSR — app renders each page live from current data, always fresh, no queue, CDN in front for bot load (start here); (2) pre-generated static — a regen *queue* renders each published recipe to a static `.html` on publish/edit, served from disk/CDN, best crawl perf + survives app downtime (graduate to this with the Fly.io/Ghost production move). Ties to the **Production hosting** + **Ghost integration** items below (`bestcooksclub.com` public domain). **Decisions to make:** (a) which recipes are public (master/cookbook only, gated by a flag?); (b) on-demand SSR vs pre-generated static first; (c) URL scheme — keep `/r/<id>` vs a human/SEO slug like `/recipes/banana-bread-<id>`. Note: the editor form moves to an explicit admin path so the clean public URL is the read-only page.
- **Internationalization (i18n) for UI messages (2026-05-29).** User flagged: we've been writing English strings inline throughout the codebase since day one; no infrastructure to swap to another language. Start NOW so we capture new strings into the pattern while it's fresh.

  Recommended pattern (industry standard, minimal infrastructure):
  - **JSON catalogs** per locale at `forms/i18n/<locale>.json` — flat key:value map. Keys are English-ish identifiers (`save.success`, `extract.failed_no_recipe`, `claim.cloning`), not English strings (avoids privileging English as the source).
  - **`t(key, params)`** function in library-shell.js — resolves `t('save.success', {seq: 42})` against the active locale's catalog. Falls back to English catalog when key missing. Params interpolate via simple `{name}` placeholders.
  - **Locale detection**: read from `localStorage['app:locale']`, fall back to `navigator.language`, fall back to `'en'`.
  - **Server side**: same JSON catalog shape lives in `i18n/` at project root, with a tiny Python `t(key, locale, **kwargs)` helper. Server-rendered messages (FastAPI HTTPException details) and email/notification text route through it.
  - **Migration**: incremental. New strings go through `t()` from day one. Existing strings get migrated when touched. After ~3-6 months, run a script that greps for inline strings missing from the catalog and reports them.

  Things to remember as we go:
  - When showing user-facing messages anywhere (showFeedback, error dialogs, button labels, toasts, modal text, alert strings, the new progress-UX work above): wrap in `t()`.
  - Server-side: pass locale through from the X-Self-User-Id header → user.locale column (need to add) → response messages.
  - Date formatting: use Intl.DateTimeFormat / Intl.NumberFormat on the client, locale-aware.
  - When refactoring: don't try to do all of i18n at once. Do it module-by-module as files get touched. The catalog grows organically.

  Memory worth saving: [[i18n-as-we-go]] — discipline note that every new user-facing string written from 2026-05-29 forward should go through `t()`. Existing strings get migrated lazily when files are otherwise edited.

- **Non-English source pages — two-flavor extraction (2026-05-29).** Reported case: ran "spanakorizo" (Greek "rice with spinach") through a 25-URL batch; 21 of 23 sites rejected at the is_recipe filter — every Greek site (akispetretzikis.com, argiro.gr, gastronomos.gr, madameginger.com, bonappetit.gr, paxxi.gr…) failed because our phrase scorer (`RECIPE_PHRASES` in input/pipeline/config.py) only knows English phrases. Greek phrasings (`Συστατικά`, `Εκτέλεση`, `Μερίδες`) never matched → score=0 → rejected. Only `miakouppa.com` and `pbs.org/food` survived, and they're English-language.

  **Strategic frame**: The best Greek/Italian/French recipes live on those countries' top recipe sites in their native languages. If our pipeline can ingest them and present them in English alongside English-language results, BCC becomes the only aggregator with access to genuinely-international recipe quality. Material competitive advantage.

  **The design: two flavors per page, two phrase-check processes**:

  ```
                          Page fetch
                              │
              ┌───────────────┼───────────────┐
              ▼                               ▼
        FLAVOR A: untranslated        FLAVOR B: translated-to-English
        (preserve original for         (the canonical working version
         provenance + display)          we rank, embed, and display)
              │                               │
              ▼                               ▼
        phrase check against            phrase check against the
        language-specific phrase         standard English RECIPE_PHRASES
        table (Greek / Italian /         (works because content is now
         French / Spanish)               English markdown)
  ```

  Stored on the recipe:
    - `_source.originalLanguage` (ISO 639-1: "el", "it", "fr", "es", "en")
    - `_source.translated` (bool)
    - `_source.translatedAt` (timestamp)
    - `_source.originalTitle` (string — author's actual recipe name in source language; useful for "view original")
    - Recipe body fields (name, description, ingredients, instructions) hold the TRANSLATED English version — that's what we rank + embed + match against the cohort + display in TBOTB

  **Implementation order** (each ships independently):

  1. **JSON-LD trust order swap (FREE, ship first, biggest immediate lift)** — most recipe sites publish schema.org `Recipe` JSON-LD regardless of language. Currently `_is_recipe_filter` runs the phrase check UNCONDITIONALLY before JSON-LD parsing. Invert: if the page has a `schema.org/Recipe` JSON-LD block, accept with full score; phrase check runs only as fallback. Free, catches ~80% of non-English recipe-site rejects on its own. Independent of translation.

  2. **Language detection at fetch** — read `<html lang="...">` attribute (universal; recipe sites set it correctly). Fallback: character-set heuristic (Greek chars → "el", Cyrillic → "ru", CJK → respective codes). Stored on `_source.originalLanguage` regardless of translation decision.

  3. **Translation via Haiku at fetch time** (~$0.0005 / 5K-char page) — when language ≠ "en", translate the cleaned markdown to English via a Haiku call with explicit recipe-aware prompt ("Preserve quantities, ingredient names that are loanwords like 'feta', cooking technique terms; translate prose"). Cache the translation URL-keyed in the existing `llm_extract_cache` shape. After translation, run the standard English pipeline (English phrases, English-prompted markdown_to_recipe). Result: recipe body in English; provenance fields stamped with originalLanguage + translated=true.

  4. **Multi-language phrase tables for untranslated flavor** — add `RECIPE_PHRASES_GREEK`, `_ITALIAN`, `_FRENCH`, `_SPANISH` to config. Used when a user explicitly opts for "preserve original" mode, or as a fallback when translation fails. Routed by detected language.

  5. **Form display** — small "Translated from Greek" pill next to the recipe title when `_source.translated`. Click pill → reveals original title + "View original" link to source URL. Same pattern as the page screenshot tile.

  Recommendation: ship 1 first (immediate Greek-batch fix), then 2+3 together (the translation pipeline as a unit). 4 stays in reserve for the rare case where the curator wants the untranslated flavor — most TBOTB use is going to want translated. 5 ships with 3.

- **Standardize progress + success/fail UX (2026-05-29).** Stop hijacking functional UI elements for status (the drop zone showing "Processing…" via a class is the worst offender — drop zone's job is to receive drops, not communicate state). Design pattern target:
  - **Toast notifications** for transient success/info (slides in top-right or bottom-center, auto-dismisses after ~3-4s, stackable). Slack/Linear/GitHub aesthetic — small pill with icon + message.
  - **In-flight progress indicator** for long-running ops (extract, claim, save, enrich, image-gen). Could be a small persistent strip in the header or a dedicated "now processing X" pill. Avoid blocking the user's view; let them keep scrolling.
  - **Modal (JS popup)** for terminal results that need acknowledgement, OR for fail cases where the user needs to know what went wrong + offer next steps ("Retry" / "Cancel" / "Save as draft" buttons). Same `<dialog>` element pattern already used for the thin-recipe gate and the image-extract failure dialog.
  - **Inline feedback text** stays for save/clear/delete confirmations within a single panel context (these are local feedback for local actions).

  Audit pass needed across: drop zone (processing class), button text changes (Save → "Saving…", Enrich → "Enriching…", Claim → "Cloning…", Generate Image), `showFeedback()` calls throughout, `showErrorDialog` calls, `dropZone.classList.add('processing')` calls. Each should be reclassified into toast / progress-strip / modal / inline based on the rules above. Pattern lives in library-shell.js so dishes / users / install get it for free.

- **Page screenshot capture + display.** Designed 2026-05-28 with the user. Add `_source.pageScreenshot` field, capture via Playwright headless Chromium (already installed in `sandbox/playwright/`), trim to above-fold + center-crop to 1500×800 then 1500×1000 via the existing `process_thumbnail` pipeline. Filename pattern: `recipe-screens/<recipe_id>-<sha8 of timestamp>.jpg` so files trace back to recipes if the DB link is lost (the user's concern about over-reliance on URL-hash naming). Add a smaller image well under the hero on the recipe form. Backfill for the existing 354 rows is one Playwright session, ~20-30 min wall time. Same `image_store` abstraction (LocalStore today, S3 when flipped). Manifest sidecar (`_manifest.jsonl`) makes the file→recipe mapping recoverable independent of the DB.
- **Identity badge — consistent right-side mount across all pages.** Currently the recipe form has the badge on the RIGHT (it calls only `LibraryShell.initNav`, which mounts after inserting the spacer); other pages (dishes, users, install) call `LibraryShell.init` FIRST, which mounts the badge before the spacer exists, so it lands on the LEFT. Fix: drop the `initIdentityBadge()` call inside `init()`; rely on `initNav()` to mount it everywhere. Pages that don't call `initNav` (none today) would lose the badge; we'd add an explicit `initIdentityBadge` call there. Caught 2026-05-28 during the demo prep — user noted "the user id stuff at the top needs to be on the right on all pages, not just recipes."
- **Next/prev navigation arrows on recipe + dish pages.** When viewing a single recipe (recipe_form_styled.html?recipe_id=…) or a single dish (dishes.html with a dish selected), surface ‹ / › arrows in the header to step through the sibling rows of the current scope. Scope rules to confirm at build time: recipe arrows step through the current sidebar's filtered+sorted list (so a chapter filter or search constrains the cycle); dish arrows step through `list_dishes` alphabetical or by `last_refreshed` desc (TBD). Keyboard shortcuts: `[` / `]` or arrow keys. Wraparound at end-of-list. The arrows belong in the same header strip as the identity badge; reuse the existing chevron pattern from the nav menu. Cheap to build (~50 lines per page), and dramatically improves cookbook-style review workflow ("look at all 10 Pastitsio recipes in order"). Worth noting: today every recipe view is a direct URL load (`?recipe_id=…`), so the navigation also needs to push the new URL via `history.pushState` rather than full page reload so the chrome stays stable.
- **Harvest grading at save time.** Today's `_master.exceptionalism` is stamped only in the batch path (after `_compute_custom_ou` runs). Manual-from-reject saves go to master with `_master.kind="harvest"` but no grade. The dish row's `last_ou_fit` now persists `sigma_effective` + model + coefficients (today's change), so a harvest save can: (1) fetch DA/PA for the URL via Moz, (2) apply the stored fit's predicted_PA(DA), (3) residual → T-score against stored σ, (4) stamp the grade. Edge case: stored fit is from a run that may be weeks old; consider whether to surface a "graded against the originating run's cohort, last refreshed YYYY-MM-DD" caveat on the badge.
- **Non-batch-originated recipes — grade story.** Personal saves and pre-existing recipes have no dish cohort. Three options: (a) skip Exceptionalism entirely for them (em-dash in UI — already what happens today since `_master.exceptionalism` is absent); (b) match the recipe to a dish heuristically (chapter + cuisine + ingredient overlap) and grade against that dish's stored fit; (c) introduce a global Exceptionalism scale across ALL recipes (different math, different meaning — would be confusing to mix with the per-dish T-score). Discussed today but deferred — Option (a) is the honest default and probably the right answer.
- **Domain quirks registry.** Discussed today as future work — a small `domain_strategies` table keyed on domain with `fetch_strategy` (`plain` / `playwright` / `bookmarklet_only` / `skip`), `custom_extractor` module path, free-form notes, and auto-tracked failure counts. Value comes from routing between MULTIPLE strategies — don't build until Playwright lands as a second strategy. Backfill from `dish_rejects.reason` patterns on day one. The Kitchn turned out NOT to need this (it was just a UA mismatch — fixed today via the canonical fetcher); first real candidate will surface from the next failed batch run.
- **Schedule the daily cache refresh.** `scripts/refresh_expiring_cache.py` works manually; needs a Windows Task Scheduler job (or equivalent) firing it nightly. Without scheduling, the proactive refresh story isn't actually proactive — rows accumulate stale until a user touches them and trips the fallback path.
- **Backfill the remaining 28 master enrichments.** `python -m scripts.backfill_master_enrichment --limit 0`. ~$0.03 total, ~7 minutes wall. Idempotent — skips already-enriched. Defer until you've decided the master cookbook contents are stable, since enrich runs against whatever's in the row.
- **`userComments` field.** Per-recipe user-comments array, same `+`/`-` UI as ingredients/notes. List it in `USER_TOP_LEVEL_FIELDS` in `recipe_model.py` so cache writes and claims strip it. Belongs only in `recipes` rows (never master, never cache).
- **iOS Shortcut for native-app share sheet.** Screenshot path: user takes screenshot in a paywalled native app (NYT Cooking, ATK app), shares to a Shortcut that POSTs the image to `/extract-from-image` and opens the form with the result. Sidesteps both the paywall and the Chrome-iOS-bookmarklet-popup-block. The screenshot-receiving endpoint exists; the Shortcut .shortcut file does not.
- **Save-time conflict resolution dialog.** When `/recipes` POST detects an adoption that would overwrite a user-edited row, return 409 with `{conflict: true, existing_id, summary}` and have the form ask "overwrite, fork, or cancel?" Belt-and-suspenders for the "user edits a direct-extract recipe, re-extracts the URL, then saves" path. The claim path is already immune via "copy not subscription," but direct-extract rows aren't.
- **Drop defunct `last_used_at` + `hit_count` columns.** Currently no-longer-read. SQLite 3.35+ `ALTER TABLE DROP COLUMN` on `llm_extract_cache`, plus drop `idx_llm_extract_cache_last_used`. Wire into `ensure_llm_extract_cache_table` so it auto-applies on next startup. Defer until the cache is otherwise stable — a real migration not worth fumbling.
- **Ghost(Pro) integration.** User is on Ghost(Pro). Schema is already Ghost-flavored. Three deliverables in order: (1) webhook receiver at `/webhooks/ghost/members` for `member.created/updated/deleted` events, with HMAC verification; (2) `/auth/whoami` endpoint that validates Ghost's session JWT cookie and returns our `user_id` keyed by `ghost_uuid`; (3) `users.html` picker replaced by a Portal SSO redirect. Backfill cron pulls existing members from the Admin API on first deploy.
- **Production hosting architecture.** Today's stack (home Windows box + cloudflared tunnel + local SQLite) is dev-grade; it has no uptime guarantee, no backups, no second pair of eyes. Recommended target shape (discussed 2026-05-25): Ghost(Pro) and our FastAPI app live as **peers, not nested** — Ghost owns `bestcooksclub.com` (marketing, signup, billing, member auth), our app runs on `app.bestcooksclub.com` (or `recipes.bestcooksclub.com`) on a real host. They cooperate via Ghost's session JWT — see the `/auth/whoami` deliverable in the Ghost integration item above. BCC permalinks (`bestcooksclub.com/r/<id>`) become Ghost-side redirects (or path proxies) to the app subdomain. **Recommended host: Fly.io** — Python first-class, deploy via `fly deploy` with a Dockerfile, attach a 1GB persistent volume so `recipes.db` survives deploys, free TLS + anycast IPs (kills the tunnel for prod; tunnel can stay on the home box for dev/personal). Fallback if Fly's volumes feel weird with SQLite: Hetzner Cloud ($4-5/mo Linux VM) with caddy in front of uvicorn + systemd unit + cron-backed backups. Alpha Anywhere considered (user already has it) but only viable if it hosts arbitrary Python uvicorn workloads — not if it requires porting to Xbasic. **Beyond the host, production also needs**: (1) automated nightly `litestream` replication of `recipes.db` to S3/B2 (~$1/mo, biggest gap we have today — single biggest pre-launch risk); (2) secrets out of `.env` into Fly Secrets / systemd-environment; (3) one uptime monitor (Uptime-Kuma / BetterStack); (4) a `stage.bestcooksclub.com` staging instance with its own DB. **Sequencing**: Ghost auth integration → dockerize → deploy app subdomain to Fly with volume → point DNS → layer litestream → staging instance.
- **Field-level provenance + post-edit memory.** Top architectural item, designed but not built (see 2026-05-16 session log). User reviewing the design. Replaces drift detection; trims cache to LLM-only fields; introduces `_provenance` map per recipe. Memory `feedback-research-before-design` captures the methodology trigger so future sessions don't skip the research step on cross-cutting design problems.
- **Replace `PLACEHOLDER_USER_ID`.** Today's `users` table + picker (2026-05-21) covers the storage and identity-selection UI; the hardcoded `PLACEHOLDER_USER_ID = 1` default in `save_recipe`, `_journal_usage`, and several endpoints' `Form(PLACEHOLDER_USER_ID)` defaults is the remaining piece. When Ghost lands the picker disappears and the default goes with it.
- **Visibility (private / shared / public) + groups.** `users`, `groups`, `group_members`, `recipe_shares` tables + `visibility` column on `recipes`. Owner-only edit; shared = read-only with a "Fork to my recipes" affordance. Endpoint-level access check on `GET /r/{recipe_id}` + `GET /recipes/{id}`. Builds on the self-URL foundation. Schema sketched in today's session log.
- **Three image controls** in the form. Today there's one hero image well + URL input. We want THREE slots (hero + two thumbnails / variants for cookbook layout). Each accepts drag/paste/click upload (already wired up for the hero — generalize to per-slot), each has a URL input, and each has a "Generate" button calling `/recipes/<id>/generate-image` with a slot-specific prompt (`generate_dish_image` for hero, `generate_ingredient_image` for one of the thumbs, free-form for the third). `RecipeModel.image: List[str]` already supports the multi-image shape.
- **Controlled vocabulary for ethnicity / classification.** Replace free-form strings with a fixed taxonomy via OpenAI structured-outputs `enum`. Cheapest token-wise; LLM constrained to exact matches. Taxonomy in `taxonomy.json` or DB table.
- **General ledger / transactions layer** on top of `bcc_token_journal`. Aggregation queries to roll journal rows into a per-user monthly view: `SELECT user_id, model, SUM(input_tokens), SUM(output_tokens), strftime('%Y-%m', created_at) FROM bcc_token_journal GROUP BY ...`. Then map model + token counts → estimated USD via a price table. Subscription tier model (hard cap / soft cap / overage) still TBD.
- **Re-point journal rows on adopt.** When `save_recipe` adopts an existing recipe_id, the LLM calls from this extract are already journaled under the *originally-minted* UUID. Consider updating `bcc_token_journal SET recipe_id = <adopted>` for those rows so the journal trail joins cleanly to the surviving recipe. Currently their cost history is queryable but doesn't join to `recipes.recipe_id` for the user's canonical record.
- **Refresh existing `metabase_url` rows** scored before the www-variant fix so their PA matches the Moz UI. One-liner: `python -m input.pipeline.refresh_url_metadata --refresh-stale --days 0`.
- Move `RECIPE_PHRASES` out to an editable `pipeline/recipe_phrases.txt` (one per line, `#` comments). User explicitly asked for this; deferred during NYT debugging and never circled back.
- Access-control the form's Metadata section (currently marked `TODO: secure later`).
- Update `pipelineRecipes/` batch project to import schema + stages from `forms/` rather than maintain its own copies.
- Investigate the one Kitchn URL where markdown extraction came back empty (image fallback worked, but worth understanding why JSON-LD or DOM walk missed it).
- ~~**Bookmarklet smarter-root + self-check**~~ — **shipped 2026-05-27**. Bookmarklet now scores every candidate root via `chars + 100 * recipe_phrase_hits` and picks the best (`pickBestRoot`). Screenshot has size fallback to `document.body` when initial capture < 30KB b64. Same scoring picker also ported to server-side `to_markdown/html_to_markdown.py:select_main_content` so batch + Extract-from-URL get the same fix — phrase list + selector list kept identical between JS and Python; comments in both files cross-reference. Still open: **friendly "no recipe content" popup** when even the best candidate scores ~0 (no recipe phrases hit at all). Defer until we see it in the wild.
- **Playwright sandbox** (2026-05-27, queued). Folder at `sandbox/playwright/` with install notes + a 02_smoke.py that launches Chromium and dumps a page. Goal: rehearse the headless-browser fallback for the batch fetcher before committing to it as a production code path. Two failure modes plain `requests.get()` can't fix — anti-bot 403 (cleanfoodiecravings) and JS-rendered recipe widgets — both go away in a real Chromium. The architectural promise: extract `pickBestRoot` to a shared `.js` file the bookmarklet and the server (via `page.evaluate`) both consume → actual code reuse, not parallel maintenance. Not yet wired into `extract_recipe_from_url`. Decide after sandbox probes whether to graduate it into `to_markdown/playwright_fetch.py` as a fallback when plain fetch returns 403/empty.
- **Friendly site-name display in recipe list.** The recipe sidebar currently renders the bare domain (`natashaskitchen.com`) for the source link; we want the human-readable site name (`Natasha's Kitchen`, `Serious Eats`, `NYT Cooking`). Two paths: (1) curated `domain_display_names.json` in `input/pipeline/` that the form fetches on load and uses for lookup with domain fallback; (2) capture `og:site_name` (or `<title>` shortened) during extract into a new `_source.siteName` field. Path (2) is cleaner long-term but requires a per-page change to the bookmarklet + `html_to_markdown` capture; path (1) is a hand-curated map you control for the first dozen or so dominant sources. Sidebar JS already has the swap point flagged with a TODO in recipe_form_styled.html.
- **Image coopt policy + processing pipeline** — the bigger architecture (designed 2026-05-26, partially built). **Policy:** every recipe's hero image lives in our `/generated/` store, never references the source site directly. Independence is the goal — source sites take recipes down, change CDN URLs, sign image URLs with expiring tokens, or move behind paywalls; today's saved recipes would lose their image. **Fair-use stance:** internal, non-shared use is treated as fair use; revisit if we ever go public-share (cookbook export to print, blog posts, social). **What's done (2026-05-26):** bookmarklet captures the hero image bytes via `fetch(heroUrl, {credentials:'include'})` in the source page's authenticated session, posts to `/images`, threads the local URL through `local_hero_image_url` in stage-markdown, form overrides `recipe.image[0]` before populating. AI-generated images go directly to `/generated/` by design. `POST /images` (uploads) and `POST /images/fetch` (server-side URL fetch, ~70% solution — paywall/hotlink/CDN-signed fail) exist. **What's still open:**
  1. **`/images/fetch` UI integration** — small "Fetch & save" affordance next to the hero URL field, visible only when the URL is external and not already `/generated/`. Calls the endpoint, replaces the field with the returned local URL. Currently the endpoint exists but isn't surfaced in the form.
  2. **Auto-coopt on `/extract-from-url`** — when the user pastes a URL (not bookmarklet), the server-side extract path has no authenticated browser session, so `/images/fetch` succeeds only on public-CDN sources. Add the attempt anyway with graceful fallback to the external URL when it fails. Same hook for direct URL bookmarklet paths that bypass the staged-markdown flow.
  3. **Pillow processing pipeline** — once we own the bytes: resize/reformat into web variants (`thumbnail-200`, `display-1024`, `print-2048`), brightness/saturation/lighten tweaks for under-exposed source photography, smart-crop to square for sidebar thumbnails, EXIF strip for privacy, HEIC→JPEG conversion. Store all variants under predictable names (`<uuid>-display.jpg`, etc.); recipe references the base ID, the form picks the right variant per use. `POST /images/process` or auto-process in the upload endpoints.
  4. **Backfill** — one-shot script that walks all `recipes.image[0]` + `master_recipes.image[0]`, attempts to fetch (with the SSRF protection + 50MB cap that `/images/fetch` already has), replaces with the local URL on success, logs failures for manual handling. Like the entity-decode backfill from 2026-05-26.
  5. **S3 migration** — when `forms/generated/` outgrows local disk, swap to object storage (S3 / R2 / B2). The storage layer should be an abstraction with `forms/generated/` as the default implementation; switching providers is a config edit. Likely lands when production hosting does (see the "Production hosting architecture" item above).
- **Olive/brass accent for editor's pick.** Reserve a second accent color (muted olive `#7c8a3f` or warm brass `#9c7c2a`) for a future "editor's pick" / curator-promoted mark on recipe cards, distinct from the algorithmic A-tier terracotta. Holds until the curator workflow ships proper editor's-pick state separate from `_master.kind = 'editors_choice'`.
- **Copy-to-clipboard icon in recipe list metadata.** Sidebar link line will eventually gain a small copy icon next to the external-link icon — same Lucide glyph family — for "copy the source URL" without leaving the page. Wait until there's a real need; the external-link icon already covers the dominant intent (open in new tab).

## Ideas

- ~~PDF input~~ — shipped 2026-05-16 (commit `940ef0b`).
- "Re-extract this recipe" button on loaded records: re-runs the LLM against the same source (URL or staged image) and updates the existing recipe row in place, using the existing recipe_id instead of minting fresh. Today re-extracting a local-recipe image creates a duplicate row because each extract mints a new recipe_id → fresh self-URL → no adoption.
- ~~"Re-enrich" button~~ — shipped 2026-05-17 as the Enrich button (works on both fresh extracts and loaded existing records since it operates on current form state). The "re-enrich a batch of empty-provenance records" idea is now the **batch enrichment subscription tier** below.
i'- Bookmarklet detection of in-browser PDFs: when `document.contentType === 'application/pdf'`, fetch the PDF bytes and POST to `/extract-from-pdf` instead of trying html2canvas. Closes the loop for "click bookmarklet while viewing a PDF in a browser tab."
- HEIC → JPEG conversion on the server side so iPhone-Photos paste flow works end-to-end (OpenAI vision doesn't accept HEIC).
- Other URL-keyed metadata on `metabase_url`: favicon URL, og:image, domain category, content fingerprint for change detection.
- Source-page error UX: bookmarklet currently `alert()`s when it fails (source page can't render our styled modal). Could inject a styled overlay into the foreign DOM if it becomes worth it.
- Bookmarklet variant that *only* sends markdown (no screenshot upload) for users who never want the cost; or a modifier-key gate (shift-click = force screenshot).
- Make `forms/` pip-installable so `pipelineRecipes/` and any future consumer can `pip install -e ../forms` instead of path-shimming imports.
- **$ cost estimate per call.** Hardcode current per-1M-token prices for the models we use; show in extraction trace; aggregate into ledger. Constants need updating when prices change.
- **Per-user monthly token caps tied to subscription tier.** Hard-cap, soft-cap with warning, or overage charged at $/1K tokens — depends on business model.
- **Ledger granularity.** Per-LLM-call entries (clean atomic units, easy to query) vs per-operation rollup ("one extract" with vision + extract counted as one op). Probably both: per-call ledger rows, plus an `operation_id` foreign key so an op's component charges roll up cleanly.
- **Recipe `_usage` field** as denormalized rollup of *this recipe's* LLM cost, alongside `_scoring`. Ledger stays source of truth; `_usage` is a convenience for showing "this recipe cost you N tokens" in the UI without a join.
- **Auto-snapshot `bcc-state-code.md` updates** at end of session via a hook or memory note so we don't keep forgetting to log changes the same day.

---

## Session log — 2026-06-10/11 — Cook tips/checks KNOWLEDGE BASE + the AUGMENT pass with provenance (3c-a + 3c-b), plus five cook-view fixes

Continues the recipe-anchor / cook-rework arc (engine phases 1–3 shipped just prior: the `_cook` model, the 8-gate gauntlet, `cook_rework.py` + the out-of-process job/endpoint, the interactive cook panel). This session built the CURATED KNOWLEDGE layer — the moat — made it live end-to-end, then fixed five real cook-view bugs surfaced by use. See `memory/project_cook_kb.md`, `project_recipe_anchor.md`.

### 3c-a — the tips/checks knowledge base, the owned moat (baf0f80)
`input/pipeline/cook_kb.py`: DB table `cook_tips_kb` (kind/title/technique_tags/trigger_signals/ingredient_classes/equipment/scope/claim/action/signal/failure_mode/variants/mechanism/render_hint/confidence/editor_note/status). Entries authored in OUR words (technique = uncopyrightable fact; only expression is — ATK's "100 techniques" is a topic CHECKLIST, never source text). CRUD + `import_drafts` (+ `?overwrite=true` backfill) + `project_published()` (runtime view; drops editor_note/confidence/exemplar_recipes before the model). `forms/cook_kb.html` master/detail a/c/d editor (filter/search/edit/**publish gate**/delete/bulk Import), admin-nav "Cooking tips KB". Routing gotcha fixed (`/cook-kb/import` declared before `/cook-kb/{id}`).

### Seed architecture — git-tracked, no-data-in-code (431ad30, b931b08)
Retired the hardcoded Python seed → canonical seed is `input/pipeline/cook_kb_seed.json` (git-tracked = the version-controlled KB backup AND the fresh-install bootstrap). `ensure_cook_kb_table` seeds from it ONLY on an empty table (no clobber/resurrection of curated state). `exemplar_recipes` field {title,source,url,note?} added — internal QA, dropped from projection (source: ATK now, BCC reserved for our own catalog once it can demonstrate a technique).

### The 30 KB entries — ATK techniques 1–30 (6336459 + batch 3)
Desktop-Claude authored in our words; imported clean (all tags validate, ids unique). Batches 1+2 = the 20 (user PUBLISHED all 20); batch 3 = 21–30 (drafts, await publish). 11 carry ATK exemplar pointers. New vocab tags folded in: `finish`, `salt_vegetables`, `bread_coat`, `knife_skills`.

### 3c-b — the augment pass + provenance validator, the moat LIVE (667b91c)
`cook_augment.py` runs AFTER the rework on the optimized `_cook`: projects the published KB → Sonnet `attach_guidance` forced tool SELECTS + rewords entries per step (never invents) → **server validators: kb_id ∈ injected published set (anti-invention) + step_index in range** → attach as `Attachment{kb_id, kind, text}`, kind taken from the KB by id (model can't relabel a tip as a check). `cook_model`: Attachment + CookStep.attachments + CookMetadata.tips (bare tip/check now DEPRECATED, kept for pre-3c-b _cook). Rework → **v2**: stopped inventing tips/checks (dropped the cook_tips_kb.py stub injection + the tip/check schema fields). Panel renders attachments w/ kb_id provenance on hover + a recipe-level "Good to know" block. opus(rework)→sonnet(augment) tiering, proven end-to-end. VERIFIED LIVE: Singapore Noodles attached 8 items incl. `bloom_spices_in_fat` onto its bloom step — the entry SN is a published exemplar for.

### Five cook-view fixes
- **Mise catch-all** (34163d1): the "Measured & ready" bundle (items measured INDIVIDUALLY, staged separately) shares the data shape of a real combination bundle → it rendered as "combine these" and interleaved by appearance order. Now real combinations render first ("The mise — combine ahead"); the catch-all renders AFTER ("Measured & ready — staged separately"), each item on its own line. Render-only (existing _cook displays correctly).
- **Video box** (1e58e4d): many recipes carry a dead/paywalled video + missing thumbnail → the browser's broken-image icon. The box now stays hidden until the thumbnail fires `onload`; a missing/404 thumbnail keeps it hidden. Both render paths.
- **Equipment completeness** (rework → v2.1): the rework missed tools (the grater for grated garlic). Two gaps — `build_rework_input` never passed the recipe's `notes`/`description` to the model, and the prompt only said to SIZE equipment, not be COMPLETE. Now notes+description are sent + the prompt captures every tool implied by a prep verb (grate→grater, zest→zester, sift→sieve) or named in the source. Verified: Chicken Milanese rework now lists the grater.
- **SAVE dropped `_cook`** (fcad572) — the important one: the form's save passthrough (carries `_scoring`/`_measurements`/nutrition/video/_extract_trace) never included `_cook`, so a plain Save serialized the recipe WITHOUT it and the server stored the stripped record → empty cook box on reopen. This had ALREADY silently eaten the `_cook` from ~5 previously-reworked recipes. Added `payload._cook = r0._cook`. Server already round-trips it (sanitize_recipe_data keeps it; RecipeModel `_cook` alias + model_dump(by_alias) — verified). E2E verified through the form (load 8 steps → Save carries _cook → re-fetch persists). Four-edges audit (load/save/extract/metadata) — this was the save edge.
- **Redundancy check** (4e5f1c3): `dedupe_annotations(cook)` runs before persist — drops a recipe tip whose entry is already on a step, near-duplicate text across tips+attachments (word-set Jaccard ≥0.75 so distinct advice survives), and duplicate technique_changes; plus a "no redundancy" augment-prompt rule.

### Reworks kicked off
Re-reworked Singapore Noodles + Frico (stale versions) and fired fresh reworks on the 6 recipes whose `_cook` the save bug had wiped (Tandoori Chicken, Stir-Fried Thai Beef, 4 vinaigrettes/salads) — all out-of-process (jobs 151–158), landing with grater + augment + dedup, and they STICK now (save fixed).

### NEXT — Phase 4 (the visible payoff)
The cook-view RENDERER via the cookbook surface — render the standardized `_cook` into the real, customizable cook MODE (mise in appearance order as living progress, steps as the driving units, page-turn). Then voice (Claudette) on the `window.cookView` base. Also queued: prompt-CACHE the projected published KB (stable prefix; same lever on the extraction prompt); ship the curated/PUBLISHED KB in the seed file (not just drafts) for the portable product.

---

## Session log — 2026-06-11 — Cook KB 30 → 100, ALL published, prompt-cached, seed ships live

Bulk-import session continuing the cook tips/checks KB ([[project_cook_kb]]). The user pasted the remaining **70 ATK "100 Techniques" entries** (#31–100) in 7 batches of 10 (Desktop-Claude authored in OUR words; `source_note` cites ATK as a topic checklist, never source text). Each batch ran the same disciplined pipeline. Then: published all 100, wired prompt-caching into the augment pass, and shipped the seed fully published. **Both prior-session KB follow-ups (prompt-cache, seed-published) are now DONE.**

### The import pipeline (per batch)
For every 10: (1) write to a temp file; (2) **validate** — id collisions vs the seed (none), internal dupe ids (none), `kind`∈{tip,check} / `scope`∈{step,recipe,either} / `confidence`∈{high,medium,low} all valid, and unknown `technique_tags` flagged; (3) **fold any new tags into `TECHNIQUE_VOCAB`** in `cook_kb.py` (the no-silent-invention rule — log additions, never let an unknown tag pass); (4) **import to the live `recipes.db`** via `cook_kb.import_drafts` (lands as DRAFTS — publish gate stays with the user; WAL makes the concurrent write safe while the server runs); (5) **append the same entries to the git-tracked `cook_kb_seed.json`** (the version-controlled KB backup + fresh-install bootstrap, kept in sync with the DB); (6) verify `py_compile` + seed JSON validity; clean up temp.

### Result
- **KB total 30 → 100.** All 70 imported clean across 7 batches — **0 collisions, 0 dupes** end-to-end. Seed file 30 → 100 entries, valid JSON.
- One mis-paste caught: the first "batch" was the existing #21–30 (already in seed+DB) — the validation step flagged all 10 as pre-existing, user said "my mistake," nothing imported.
- **`TECHNIQUE_VOCAB` grew by 16 tags** (6 → 8 groups; added **baking** + **preserve/specialty**): `bake`, `leaven`, `grind_spices`, `glaze`, `shave`, `grind_meat`, `smoke`, `ferment`, `infuse`, `curdle`, `laminate`, `flambe`, `dry_age`, `cure`, `confit`, `freeze`.
- The user published briskly via `cook_kb.html` as we went; at import-end the DB was 54 published / 46 draft.

### Published ALL 100 ("haven't seen anything I didn't like, don't think I will")
One UPDATE flipped the 46 remaining drafts → published (live DB now **100/100 published**). **Verified the augment pass picks them up:** `augment_cook()` projects `cook_kb.project_published(DB_PATH)` and validates every attachment's `kb_id` against that injected set (the anti-invention moat), so the projection *is* the available+valid set. `project_published` now returns **100** (was 54); spot-checked 5 new entries spanning the range (present); confirmed internal fields (`editor_note`/`confidence`/`exemplar_recipes`/`status`) are stripped from the projection. The running server serves the new published KB live (reads the DB each augment — no restart).

### Prompt-caching the projected KB (the queued optimization, the user's actual want)
Settled the in-memory-cache question first: **NO RAM cache** — the `project_published` SELECT is sub-ms and dwarfed by the Sonnet call it precedes; fetch-fresh is what made the publish instantly live with no restart; a RAM cache adds an invalidation obligation (a footgun class that's bitten this project). The caching that matters is **Anthropic prompt-caching**, a different mechanism (saves LLM *input tokens*, not DB reads). `cook_augment.py` `_call`: the `system` block (rules + projected KB + tools — **no per-recipe data**) now carries `cache_control: ephemeral`, with per-call cache read/write logging. **Verified live, real production path, twice:** call 1 wrote **51,500** tokens (the KB prefix is ~51.5K — bigger than expected); call 2 read all 51,500 from cache with only 21 uncached tokens — identical tool output. Cache reads bill ~0.1×, so each warm rework saves ~46K Sonnet-input-token-equivalents. **Content-addressed → a KB edit auto-misses + re-writes the next call, so no invalidation code** (exactly why it beats a RAM cache). Default 5-min TTL fits a batch of runs.

### Seed ships fully published (portable-package bootstrap)
Stamped all 100 `cook_kb_seed.json` entries `status: published` so `_seed_from_file` boots a **fresh / portable install with the full KB live**, not as drafts — no manual publish step for a new instance owner. Safe: seeding only runs on an EMPTY table (running DB untouched, verified via a throwaway temp DB → 100 published / `project_published` 100). Also re-syncs the git-side seed backup with the fully-published curation state.

### Files (committed + pushed, branch `split/enrichment-api`)
- `c632103` — import #31–100: `cook_kb.py` (vocab +16) + `cook_kb_seed.json` (+70) + this log.
- `95ef4f4`, `3e1754c` — `recipes.sql` backup refreshes (post-import, then post-publish-all) via `bcc_backup.bat`; ADAM copies written; `integrity_check: ok`. [[project_db_backup]]
- `a145b35` — augment prompt-cache.
- `2f6ac76` — seed ships published.

### Follow-ups
- **Phase 4 renderer** (the visible payoff) — the cook-view render via the cookbook surface; then voice (Claudette). The only headline KB item left.
- Same prompt-cache lever is available on the **extraction prompt** if/when wanted (the docstring flags it).

---

## Session log — 2026-06-11 (afternoon/evening) — cook-KB media slice, named-embeddings design, recipe-vector overhaul

A long, wandering session that **started** with "can we use vectors to match tips/checks?" and ended up reworking the whole recipe-vector path. Arc: KB-as-product design → media on KB entries (carrot-video slice) → named-embeddings design (researched) → a recipe-vector quality bug diagnosed + fixed → an ingredient synonym dictionary (new ACD surface) → menu renames. All on `split/enrichment-api`, committed+pushed throughout.

### Cook KB → a PRODUCT + media slice (SHIPPED)
Design `docs/cook-kb-as-product.md` ([[project_cook_kb_product]]): one table → two projections → three surfaces; media as **curated references the model never emits**; `kind`×`source` axes for user-authored PRIVATE tips (guardrail: `source='curated'` gates all shared projections). **Built the media slice:** `cook_tips_kb.media` JSON column + `MediaRef` model + `Attachment.media`; augment stamps an entry's media by `kb_id` (code-pulled, never model-emitted); cook-view renders a quiet clickable **YouTube thumbnail** (derived from the video id, `img.youtube.com`, embed-not-rehost, DL-16); editor media field + thumbnail-that-expands-to-an-inline-player + auto-grow textareas + widened search. Authored the **CIA oblique/roll-cut carrot entry** from the video transcript (in our words), published+seeded; **verified end-to-end** (augment selected it for a carrot-cut step WITH its media). Proved YouTube **transcript fetch** (`youtube-transcript-api`) → the `content_to_tip` "draft a tip from a URL" flow is feasible (NOT built; queued).

### Dev noindex shield (SHIPPED)
The tunnel (`recipes.tbotb.com`) was crawlable — Cloudflare's managed robots.txt ALLOWS search. Added `X-Robots-Tag: noindex, nofollow` middleware (verified it passes through Cloudflare) + our own `/robots.txt Disallow:/`. Hard shielding = Cloudflare Access (your call). `save_recipe_api.py`.

### Named embeddings + hybrid retrieval (DESIGN, researched)
`docs/named-embeddings.md` ([[project_named_embeddings]]): multiple named embeddings per entity (browse vs applies); the crux researched + verified on our sqlite-vec 0.1.9 — **hot filter columns go IN the vec0 index** (metadata/partition), NOT a post-KNN JOIN (the recall-collapse trap, sqlite-vec #196). Not built; the recipe-vector fix below is the first instance of "what text, consistent both sides."

### Recipe-vector overhaul (SHIPPED + verified)
Bug: "show better recipes like this risotto" returned **nothing** though 8 risottos sat in master (nearest 0.894 > the hardcoded 0.8 cutoff). Diagnosed by **measurement**, not guessing:
- The masters cluster tightly (all carded); the query landed 0.894 out because it embedded via the **divergent fallback** (card-text vs fallback-text = 0.67 offset). Then the cumulative field test found the **TITLE** is the #1 noise source (adding it ~doubled intra-dish L2, 0.35→0.68) — NOT the technique (which is a shared anchor that *tightens*). Ingredient phrasing (arborio vs risotto-style rice = 0.42 L2) was #2.
- **Fixes:** `compose_identity_text` **drops the marketing title** + **normalizes ingredients** through a new **ingredient synonym dictionary** (`ingredients_lib` + seed, ACD-shaped, cached `normalize()`, `alias_type` synonym|regional|misspelling|brand|variety). Result: intra-risotto L2 **0.612→0.287**. Recommender (`similar-master`) rewritten **dish-anchored**: matched recipe → its cohort ranked by `rank_score` (exact, no vector cutoff); vector KNN is the fallback with cutoff from config `similar_max_distance` (0.95). **Batch-re-embedded the whole corpus** (52 dishes + 529 master + 257 user) under the new composition + rebuilt the vec index. **Verified:** risotto → 7 ranked cohort recipes, match conf 0.964.
- **ACD editor** for the dictionary: `/ingredient-synonyms` CRUD + `/normalize` preview, `forms/ingredients.html` (per-alias type, live normalize test), admin nav. `docs/ingredient-synonyms.md` records the synonym/variety/substitution discipline + a FUTURE substitution-with-context table (don't merge varieties — the dish identity already clusters them; reviewed against a ChatGPT discussion that converged on the same calls).

### Menu renames (admin + user hamburgers)
Cooking tips KB→**Tips/Checks**, Ingredient synonyms→**Names**, Run queued jobs→**Jobs/Queued**, Scheduled jobs→**Jobs/Scheduled**, Install bookmarklet→**Bookmarklet**. `library-shell.js` ?v bumped to `20260611f` across 12 pages.

### Noted, NOT done (see To-do)
The jobs-queue **drain is dead** (enqueue→spawn replaced it; zero queued rows) — inventory of removable code + the "Jobs/Queued → Jobs/Activity (recent runs via GET /jobs)" repurpose is captured in the To-do. Left alone for now (late).

### Open follow-ups
- `content_to_tip` (transcript/URL → LLM-drafted tip, dedup via an `applies` embedding) — the obvious next build; foundation proven.
- `similar_max_distance` works via default but isn't registered in System config; residual asymmetry for no-`_identity`-card recipes (compute card if missing); seed types are all `synonym` (set regional/variety as you curate).
- `youtube-transcript-api` installed in the venv; no `requirements.txt` here — record the dep for the portable package.

---

## Session log — 2026-06-11 (late) — Phase 4 cook-view renderer SHIPPED + the hands-free voice loop ("Claudette") + No-look touch mode

The big visible payoff of the whole recipe-anchor/cook-rework arc: the standardized `_cook` block is now a real, usable **cook surface** — and it's driven hands-free by voice and by a knuckle-swipe pad. Built end-to-end this session on `split/enrichment-api`; all four threads ([[project_recipe_anchor]] phase 4, [[project_cook_voice]], [[project_cook_kb]] surfacing). Verified server-side automatically + iterated live on the user's iPhone.

### Phase 4 — the cook-view renderer (`forms/cook.html`)
New self-contained page, driven entirely by live data: fetches `GET /recipes/{id}?user_id=` and renders `recipe._cook` into the finished cookbook design (lifted from the `cookbook-cookview.html` prototype). Built against the EXACT real `_cook` shape (dumped Chicken Milanese first — deployed amounts live on the USES, not the ingredient). Renders: compact hero (title de-duped — header title hidden when a hero image exists), headnote, "what we changed", **units toggle** (both faces from `CookAmount`), **three computed ingredient views** — **Bundles · As needed · Shopping** (default Bundles, per user), the mise with combine-notes, equipment in order-of-need, **put-asides**, anchored **steps** with `{ingN}`/`{amt}`/`{bundle}` token expansion, KB **attachments + media** (YouTube thumb, embed-not-rehost), finish/cook's-note/good-to-know, honest provenance credit. Big tap targets, **check-to-advance** + current-step highlight + progress bar + keep-screen-awake. Entry route **`GET /cook/{id}`** (mirrors `/r/`, resolves owner) + an **"🍳 Open cook view"** link from the recipe-form cook panel. Deferred (data/dep-driven): equipment→product picks (no catalog), form-variant pickers (no `_cook` carries them), per-step beauty shots.

### Claudette — grounded Q&A (the brain) — `cook_ask.py` + `POST /cook/ask`
Plain conversational completion (Sonnet `claude-sonnet-4-6`), warm "speak-it-aloud" system prompt, grounded in a compact context built from `_cook` (ingredients/mise/steps/put-asides/finish + current step). Token-journaled (`build_usage_entry("cook_ask", …)`), degrades to a friendly 503. Verified live: "how do I know the cutlets are done?" → recipe's *3-min-a-side* + the global *165°F/thermometer* fact — exactly the recipe-context + world-knowledge blend the user wanted. This is the Tier-3 keystone; the voice loop just feeds it STT and speaks the reply.

### The voice loop (STT + TTS, browser-first, built as one piece — voice-cook-spec.md)
- **STT — `cook_stt.py` + `POST /cook/listen`:** **faster-whisper `base.en`, CPU/int8** ("the faster variant"; no GPU on this box). Lazy-loaded + warm; greedy decode. Audio stays on the host.
- **TTS — `cook_tts.py` + `POST /cook/speak`:** **OpenAI `gpt-4o-mini-tts`, voice "coral"**, steerable warm-Claudette `instructions`. MP3 bytes. (Browser `SpeechSynthesis` kept as offline/failure fallback.)
- **Verified WITHOUT a browser:** `/cook/speak` → MP3 → `/cook/listen` round-trip returned the text **exactly** ("Add the garlic and cook until fragrant."). 1.8s TTS / 2.5s STT.
- **Browser loop (in `cook.html`):**
  - **VAD = self-contained Web Audio energy VAD** (ScriptProcessor RMS vs an adaptive noise floor + silence-hang endpointing). **Dropped `@ricky0123/vad-web`** — its UMD bundle does `e.vad=t(e.ort)` (needs `window.ort`), so it bailed *before* `getUserMedia` → **no mic dialog**. Self-contained = always prompts, works offline, portable-package fit. Silero is a future upgrade.
  - **Wake word "chef / hey chef / jeff"** (The Bear homage 🐻‍❄️ — and what base.en hears for a shouted "chef" anyway), fuzzy-matched (base.en mangles "Claudette"). Questions are **wake-gated** so chatter never hits the LLM.
  - **Commands anchored** to the whole utterance (next/back/repeat/restart/stop + synonyms) so "next time…" never fires. `REQUIRE_WAKE_FOR_COMMANDS` flag for strict Alexa-mode.
  - **Wake→question no longer lost:** a **beep** (not spoken "Yes?") + a lowered mic threshold while awaiting the question (she stopped talking over the cook).
  - **Latency:** every step's TTS is **prefetched + cached** (shared in-flight promise) → "next" plays **instantly**; only Whisper hearing "next" remains.
  - **iOS fixed:** unlock a **single reusable `<audio>`** on the Start/Read/Ask gesture (Safari blocks non-gesture audio → hands-free was silent on iPhone) + a sequence token → also kills the **double-tap overlap**.
  - **Status** clarified with icons: 🎧 Listening · 🗣️ hearing you · ⏳ thinking · 🔊 speaking.
  - **Barge-in** is implemented but **DEFAULT OFF** — on a speaker (no real AEC) her own voice self-triggered the cutoff ("speaks one syllable → listening"). Re-enable only with a headset.

### "No-look" touch mode (almost hands-free, no mic) + Talk toggle
- **🙈 No-look:** a dedicated **gesture pad** above the bar (so the page still scrolls) — swipe **→ next · ← back · ↑ restart · ↓ end**, **double-tap = repeat**. (Pad, not full-screen, to avoid the vertical-swipe-vs-scroll conflict.)
- **🔊 Talk** toggle (independent of mode): on = navigation speaks; off = silent move + auto-scroll ("reset scroll, no speech"). Voice forces Talk on; **Read step** always speaks. Controller gained a silent `go()`; `read()` respects Talk.

### Verified / ops
JS `node --check` clean throughout; Python `py_compile` clean; `/cook/*` routes verified live after a gated detached restart (no job running). Works on the user's iPhone over `https://bestcooksclub.com` (HTTPS = secure context; localhost/tunnel both fine — `NotFoundError` earlier was just Remote-Desktop not forwarding the mic). `faster-whisper` (+ `ctranslate2`, `av`, `onnxruntime`) installed into the server venv (`C:\Users\john\PyCharm\venv`).

### Follow-ups (priority order)
- **Sub-steps (2.1 / 2.2)** — the user's "most important" next item; auto-split each step's instruction into one-action sub-steps for snappier voice pacing + better No-look fit (then full-screen up/down swipes become safe). Recommended: client-side auto-split now, model-authored sub-steps later. NOT built.
- **No-look UX redesign** — deeper/larger pad with the button lowered; a bottom control bar that pops up (up/down caret) to reveal mode choices so the gesture area can be bigger. "Buttons need UX work — will do for now."
- **"next" STT latency** — reduce with a faster/local keyword spotter, or move `/cook/ask` to **Haiku + streaming** so questions start speaking in ~1s.
- **Barge-in** returns once we have real AEC / a headset path.
- **Portable-package deps** — record `faster-whisper` + `youtube-transcript-api` (no `requirements.txt` yet); decide on a self-host TTS (Kokoro) + local STT for the no-cloud build.

### Files (branch `split/enrichment-api`)
New: `forms/cook.html`, `cook_ask.py`, `cook_stt.py`, `cook_tts.py`. Modified: `save_recipe_api.py` (`/cook/{id}`, `/cook/ask`, `/cook/listen`, `/cook/speak`), `forms/recipe_form_styled.html` ("Open cook view" link).

---

## Session log — 2026-06-12 — cook-rework measure-validation reframe, save cross-contamination guard, Claudette→Chef, TTS cache, No-look tap pad

A long interactive session that started from cook-rework failures and ranged across the gauntlet, a data-contamination scare, the voice persona, and the No-look UX.

### "splash"/"drizzle" reworks were failing the gauntlet — reframed the measure gate (the real fix)
Two reworks (Spanakorizo "Drizzle", Galaktoboureko "splash") failed `every-measure-numeric` because it **text-scanned step prose** for an informal-word list (`handful|ladle|thread|drizzle|splash|glug|knob|dollop|scant|hearty|generous`) and hard-failed on any hit → the whole rework discarded. A batch **survey of 830 recipes** (method-step prose) proved the list premise wrong: those words are overwhelmingly *legitimate* — verbs ("Drizzle with oil", "Ladle into bowls", "Dollop and spread"), the form "saffron threads", "hearty bread", and the adjectives "generous/scant" (which modify a real/unit-neutral measure). The user's steer: **imprecise measures are valid measurements, not defects** — same class as the already-exempt pinch/dash. So I deleted the word list entirely and **reframed the gate around `convertible`** (`cook_validators.py`): a `convertible=True` amount must carry a number (digit/fraction glyph); imprecise measures the model marks `convertible=False` are valid and exempt; **prose is no longer scanned at all.** Proof: 59 non-numeric structured amounts across the 16 reworked recipes ("to taste"×26, "all"×19, "as needed", "a small pinch", back-refs like "reserved half") — 100% `convertible=False`, 0 would fail. The 8-gate self-test still passes (the planted `CookAmount("a handful")` defaults `convertible=True` → still fires). Re-ran the two recipes (jobs 175/176) → both persist correctly now.

### Cook-rework log filenames now carry the recipe name
`jobs._build_log_filename` preferred the `entity_ref` tail, which for cook_rework is the opaque recipe UUID (the entity-lock key). Added a generic `params.log_label` (the cook-rework endpoint passes the recipe name) → `job_cook-rework_177_spanakorizo-with-jammy-eggs_….log`. Dish-refresh unchanged; non-ASCII (Greek) degrades gracefully; capped 60 chars.

### "Chicken Milanese showed on Spanakorizo" — explained + hardened
The user saw a wrong `_cook` on Spanakorizo. Root: job 170 hit the Drizzle false-positive, FAILED the gauntlet, didn't persist → the recipe kept stale `_cook` panel content (Chicken Milanese = the dev test recipe). A corpus scan found **0** currently-mismatched `_cook` blocks. Hardened the recipe form (`recipe_form_styled.html`): (1) `populateCookPanel` now CLEARS its body when a recipe has no `_cook` (no lingering stale render); (2) a **save identity guard** — the rich-block passthrough (`_cook`/`_scoring`/`_measurements`/nutrition/video/…) sourced from the `lastExtractedRecipe` global is now skipped on a *positive* recipe-id mismatch (`lastExtractedRecipeKey` stamped at every assignment), so a stale in-memory recipe can never write its blocks onto a different record. Skips only on hard mismatch → normal saves byte-for-byte unchanged (can't silently drop `_cook`).

### is_recipe scorer — five imprecise-measure phrases added (NOT to the gauntlet)
Distinct list from the gauntlet: `RECIPE_PHRASES` (is_recipe, threshold 7). Added `squeeze of`, `splash of`, `dollop`, `knob of`, `drizzle of` — high-signal, "of"-anchored so they don't fire in the narrative articles that the 2026-05-23 pruning targeted. Skipped bare splash/drizzle/handful + the non-measures. Also deduped a pre-existing `"spread the"`. 158 phrases, no dupes. (Helps borderline/thin/partially-English pages cross the bar next batch.)

### Voice persona: Claudette → **Chef** (alt "Jeff")
Renamed across `cook_ask.py`/`cook_tts.py`/`cook_stt.py`/`save_recipe_api.py`/`forms/cook.html`/`recipe_form_styled.html` + `voice-cook-spec.md`. Wake words `chef`/`hey chef`/`jeff` kept; dropped the dead `claudette`-family STT-mishearing aliases; persona chip → 🧑‍🍳 Chef. **Scope correction:** Chef stays **recipe-grounded** (general *cooking* questions OK) but is **NOT** a general-purpose assistant — off-topic questions get a one-line warm redirect (`CHEF_SYSTEM`).

### Persistent TTS cache + load-time pre-warm (no-lag spoken steps)
`cook_tts.py`: content-addressed BLOB store in `media.db` (`tts_audio`, key = sha256 of text+voice+model+instructions) + `synthesize_cached()`; `/cook/speak` is cache-first (`X-TTS-Cache: hit|miss`). Verified: miss 1.98s → hit 0.001s, identical bytes, self-invalidating. **Pre-warm** done the clean way: `cook.html` calls `prefetchAllSteps()` on load (1.2s after render) → warms the persistent cache with the EXACT client spoken strings, so first Read/hands-free play is instant and every later session (any device/user) is a free hit. **Deliberately did NOT** port `spokenText`/`deAbbrev` to Python for true rework-time synthesis (byte-fragile dual-maintenance) — load-prefetch gets ~the same payoff with one source of truth.

### No-look pad: swipes → three tap zones + pause/resume
`cook.html`: replaced the swipe pad with three divider-split zones — tap L=Back · M=Pause/Resume · R=Next; double-tap L=Restart · R=Stop/End. Added real **pause/resume** (`togglePlayPause` pauses the `<audio>` clip in place and resumes from position, distinct from the hard-stop `stopSpeaking`). Side single-taps wait 300ms to disambiguate a double-tap (middle fires instantly); tapped zone flashes for feedback.

### Verified / ops
All touched Python `py_compile` clean; `cook.html`/`recipe_form_styled.html` inline JS `node --check` clean; gauntlet self-test green; TTS cache round-trip + log-naming + RECIPE_PHRASES integrity verified. Server restarted onto the branch (no job running) so the in-server changes (`/cook/*`, validator-for-in-process, log naming, Chef Q&A) go live; cook.html changes are no-cache → live on reload.

### Follow-ups
- **Hey Chef turn-taking** (the user's "funky" report): the awkwardness is the ~2.5s Whisper round-trip between "Hey Chef" and the cue in the two-turn flow + pause-sensitive endpointing. Options: lean on the one-breath "Hey Chef, <q>" path + lengthen the awaiting-question window (small); a tap-to-ask affordance on the new pad (medium); a local wake-word spotter (bigger). Needs on-device tuning — NOT changed yet.
- True rework-time server TTS pre-warm available if wanted (cost: deAbbrev dual-maintenance) — currently declined in favor of load-prefetch.
- 3 other reworks failed on *different* gates (Frico `definite-article` "a ¼ cup"; Courgette risotto `reuse-referenced` salt; Portokalopita `mise-complete` res_syrup) — separate from splash/drizzle, not yet addressed.
- Portable-package deps still unrecorded (`faster-whisper`, `youtube-transcript-api`; no `requirements.txt`).

---

## Session log — 2026-06-12 (late) → 06-15 — cook-view control dock, nightly backup automated, top-10 reads the ledger (NYT bug), Editor's Choice pins, Collections design, voice-loop hardening

Continues on `split/enrichment-api`. Spans the cook-view UI polish, a backup-automation decision, a data-integrity fix that exposed the many-to-many "collections" architecture, the first M2M brick, and a long voice-loop tuning thread (verified live on the user's iPad/desktop). Everything committed+pushed throughout.

### Cook-view bottom redesign — control dock + icon pad + smart Back + step dimming (9e58381)
The bottom area was a mess (the No-look pad overlaid the buttons; garish colors). Root cause: vstatus + pad + bar were three independently `position:fixed` elements with hand-tuned `bottom:` offsets that collided when the buttons wrapped. Rebuilt as **one fixed flex-column `#cookdock`** (can't overlap), all on the page's design tokens (card/line/sea), bar = single-row horizontal-scroll. **No-look pad → pure SVG icons** (⏮ back · ⏸/▶ pause-resume state-swapped · ⏭ next), **tap = instant nav, long-press a side zone = restart-recipe (L) / stop (R)** (double-tap collided with rapid Next). **Smart Back** (music-player "previous"): tap ◀ restarts the current step if you're >3s into it, else previous; an escape arm so it can't loop. Upcoming steps fade (`.step.current ~ .step`).

### Nightly DB backup — automated, 3 AM (refactor ad3ce91; Task Scheduler retargeted)
User asked to automate the ADAM backup via the scheduled-jobs system. Investigation found there ALREADY was a daily Task-Scheduler task **"BCC Recipes DB Backup"** (was 1 PM). **Decision (user's call): keep it an INDEPENDENT OS task, not a `scheduled_jobs` row** — a backup must fire even when the app scheduler is off. Retargeted it to **03:00 + WakeToRun + StartWhenAvailable**. Refactored `backup_db.py` into a `run_backup(dest, no_adam)` callable (the built `db_backup` job handler + scheduled row were then removed once "independent task" was chosen). Memory `project_db_backup` updated.

### Top-10 now reads the SCORING LEDGER, not the `_master.kind='top'` label (cce098c) — the NYT bug
User ran a Chocolate Chip Cookies batch; the NYT recipe scored **100/100 (selected, rank 1)** but showed in "also ran," not the top-10. Traced: NYT WAS the #1 winner and extracted fine, but the user then opened it, ran a cook-rework, and **saved** it — the master-save re-grades via embedding-match and **overwrites `_master`, dropping `kind='top'`/`dish`/`rank`** (same family as the morning's "_cook dropped on save"). The old top-recipes query filtered on that label → NYT fell out. **Fix (user's idea): query the ledger instead of trusting the label.** `/dishes/{name}/top-recipes` now derives from `dish_run_data_points` (`selected=1`, latest `model_version`, by `rank_score`) JOINed to master content; the label survives only for the batch's delete-and-replace cleanup. NYT back at #1, immune to re-save clobbering. (The DB hand-edit to re-stamp NYT was correctly BLOCKED by the auto-mode classifier — the code fix was the right path anyway.)

### Editor's Choice — curator pins as junction membership (6105d03)
First concrete brick of the collections model. `dish_editors_choice (dish, url_normalized, note)` table + lib; `build_batch(extra_urls=)` merges pinned URLs into the candidate pool so they're **scored into the ledger like any SERP result and surface in the top-N if they rank** (the "if it scores high enough it appears" semantic). `/dishes/{name}/editors-choice` GET/POST/DELETE (mutations `manage_dishes`-gated); a panel in `dishes_v2.html` (paste URL → Pin, list, unpin). A pin is a `(dish,url)` row, NOT a stamp on the recipe → same recipe pins to many dishes.

### Collections — the many-to-many design note (`docs/collections.md`, 92303fc; memory `project_collections`)
The head-scratcher: "dish" is one TYPE of **collection** (dish/chef/method/ingredient/list; editors_choice is an overlay). Membership is a junction — and `dish_run_data_points` already IS one (M2M by dish_name). The single `_master.dish`/`_master.exceptionalism` stamp is the "latest wins" weakness; traced precisely with the zucchini / stuffed-zucchini case (membership survives via the ledger after today's fix, but the shared master ROW + grade are still single-owner, and the batch's delete-and-replace can yank the shared row). The two remaining fixes (delete-and-replace on the junction not master rows; grade-on-member-row) are documented. **Scoring is lazy/per-membership/batch-driven — a new recipe is NOT scored against every collection on creation** (a plain save gets one matched-dish grade). The `dishes→collections` rename (~800 occ) is cosmetic and explicitly **not** needed — add a `type` column instead.

### Voice loop ("Chef") hardening — verified live on iPad/desktop
A long tuning thread (commits 8b09dfc, dd65cb6, 1df2b7e, e620d09, c4f774b, e0a6a2a, 46d550d):
- **Louder TTS** — `<audio>` maxes at the source level, so 100% on iPad was quiet. Route through a Web Audio GainNode (×2). Confirmed louder on iPad. **Gated to coarse-pointer devices only** — on desktop the MediaElementSource re-route double-played the audio ~1s apart; desktop now uses the element directly.
- **Conversational follow-up** — the real "Hey Chef follow-up never answers" cause: a follow-up has no wake word so it wasn't routed to `/cook/ask`. After an answer, a window (opened immediately on the answer, 20s, `_asking`-guarded against self-echo) routes the next utterance straight to the LLM — no re-waking. Proved the multi-turn state machine in node.
- **Command vocab tightened** — the louder TTS echoed instruction words ("cook until **done**") into the mic, matching over-generous "next" synonyms → auto-advanced to the finish/summary. Reduced to deliberate words; added **voice "pause"/"resume"** (was wrongly mapped to stopHandsFree).
- **deAbbrev fractions** — ASCII `1/4` → "one quarter", `1 1/2` → "one and a half" (was "one over four"); also de-abbreviate voice Q&A answers.
- **Visibility:** the Listening status now shows `· heard: "…"` (last transcript), and a collapsible **🪵 Voice log** panel captures every transcript + routing decision with a **Save-to-server** button → `logs/cook_voice_<day>.log` the dev reads (new `POST /cook/voice-log`). Closes the debug loop for remote-device voice tuning.

### Verified / ops
Python `py_compile` + `node --check` clean throughout; `/cook/ask` answers verified live (grounded blend); `/cook/voice-log` round-trip writes the log; ledger-derived top-10 returns the right 10 incl. NYT; Editor's Choice add/list/remove + batch injection verified. Server restarted onto branch several times (detached venv uvicorn, zombie-port-safe PowerShell kill→Start-Process). `recipes.sql` refreshed + ADAM backup run.

### Follow-ups (priority)
- **Voice #5 — now diagnose from the saved Voice log**: first-"hey chef" wake reliability (many `/cook/listen` never routed), VAD false-capture flood, STT latency (local spotter / Haiku-streaming), AEC/barge-in (needs headset).
- **Collections build** (when ready): delete-and-replace on the junction + grade-on-member-row + `type` column + per-type sourcing + browse-by-type. Editor's Choice v2: force-show-below-rank, score-on-add.
- **#10 — app as a resilient service** (auto power-on BIOS + NSSM/startup-task) — still open; tunnel already auto-starts, the app does not.
- The 3 other failed reworks (definite-article / reuse-referenced / mise-complete) still unaddressed.

---

## Session log — 2026-06-15 — voice loop deep-dive + redesign: P0 conversational fixes, STREAMING answer pipeline + reusable voice-agent engine; SQLite ANALYZE perf fix; Cajun cohort diagnosis

A long voice-focused session that started from "diagnose the saved voice log" and grew into a full voice-interface redesign, a reusable engine, a DB-performance fix, and a dishes-cohort diagnosis. All on `split/enrichment-api`. Memory: [[project_voice_redesign]], [[reference_sqlite_analyze]]. Design: `docs/voice-agent-architecture.md`.

### Voice deep-dive + redesign decision (research-before-design)
Two parallel investigations (code audit of the whole voice loop + a 2026 licensable-voice-AI landscape survey). Verdict, after the user corrected an over-weighting of "redistributable": **portable ≠ offline** (we're already cloud+BYOK), so rank by solves-problems → BYOK → cost, not license. **DECISION: stay with our own CASCADE (pay-per-EVENT, BYOK); do NOT adopt a per-minute managed platform** — ElevenLabs/Vapi bill connected-session time *including silence* (a 30-min mostly-idle cook ≈ $2.40–9), structurally mispriced for us; our cascade is cents/cook and keeps the transcript for grounding + deterministic nav. **Murf** restored as the planned per-char streaming-TTS upgrade; **Flet** = not now (native-app shell, no conversational AI). ElevenLabs = one-off benchmark only.

### P0 — conversational-logic fixes (cook.html, all client-side, live on reload)
The audit found the loop dead-ended for fixable reasons: `awaitingQ`/`_asking` could wedge forever (no timeouts), exact-anchored command regex rejected natural phrasings, every drop was screen-only. Shipped: **watchdogs** (`setAwaiting`/`setAsking` force-clear); a **forgiving command matcher** (`_CMD_RULES` + `_Q_BLOCK`, synonyms + filler-tolerant, shush-vs-stop split) ; **diagnostic logging** (`stt` latency/outcome, coalesced `vad` flood, `session` markers — the prior log couldn't show any of the reported failures); 1-word follow-ups + unified `FOLLOWUP_MS`. **Smart router**: `/cook/ask` gained `allow_actions` → `cook_ask.ask_or_act` (a `navigate` tool) so a wake-gated utterance is ONE call that answers OR navigates (fixes conversational nav like "let's move on"). **Tip handling** (from the live Cajun cook log, which showed "read the tip" misrouting to `repeat`): a deterministic client `tip` command (instant, no LLM) + a tightened server tool description (tip/content requests answer, never navigate) + a proactive spoken **"Read the tip?"** yes/no offer on tip-bearing steps.

### Streaming answer pipeline + REUSABLE voice-agent engine (the headline build)
Killed the ~6s "hey chef" lag by streaming. Built for reuse per the user's steer (3 planned instances: recipe+cooking, person+bio, product+category):
- **`voice_agent.py`** — generic "grounded voice agent" engine: `stream_grounded` (Anthropic streaming + sentence-chunking + tool/action detection + journaling; **answer-XOR-navigate** via an `emitted_text` guard so a spurious trailing tool call after an answer is dropped), `grounded`, `sse`. Domain-agnostic, reused unchanged.
- **`cook_ask.ask_or_act_stream`** — thin recipe ADAPTER (persona/context/nav-tool only) over the engine.
- **`POST /cook/ask-stream`** — SSE endpoint (sync generator → Starlette threadpool, no event-loop block; journals in `finally`).
- **Client** — `askChef` consumes SSE; `makeAnswerPlayer` plays sentences back-to-back as they arrive (pipelined TTS via the shared cache, `_streamSeq` cancels), `finish()` mirrors `_onSpeakEnd`; falls back to non-streaming `askChefOnce`. **Proven live: sentence 1 ~1s before the full answer finishes.**
- Doc `docs/voice-agent-architecture.md` (layers, event protocol, add-an-instance checklist, the remaining `voice-agent.js` client-extraction task).

### SQLite ANALYZE perf fix — the dish top-10 "long loading"
User hit a multi-second load on a fresh Cajun dish run. Root cause (via `EXPLAIN QUERY PLAN`): **no `ANALYZE` stats** (`sqlite_stat1` absent) → the top-10 JOIN scanned all of `master_recipes` instead of using the `(url_normalized,user_id)` index. **Not a missing index** — a mis-plan. Ran `ANALYZE` (persists in the file; a fresh connection re-plans → live with no restart; **94ms→9ms**, endpoint 14ms) and added **`PRAGMA optimize` to `init_db`** so it stays fixed + fresh/portable installs get stats. Memory [[reference_sqlite_analyze]].

### DB read-path audit (findings captured, NOT yet applied)
Read-only audit of all user-facing queries. Top items (measured): **expression index on `master_recipes(json_extract(data,'$._master.dish'))`** → recommender path 21.9ms→0.23ms (**95×**), also speeds delete-for-dish/fit-data/cohort; **`GET /recipes`** loads+parses every master blob with no LIMIT (~70ms, ~13MB — paginate + return a list projection); **`/domains/{domain}/recipes`** double full-table parse (~88ms — add+index a `host` column). Bundle expression indexes for `_master.dish` / `_identity.likelyDish` / `classification.chapter` into one migration. Biggest risk class: `json_extract` in WHERE/ORDER BY over the big `data` blobs.

### Cajun cohort diagnosis (selection is CORRECT; display is misleading)
User: two top-scoring Cajun recipes sat in "also-ran" above the #1 winner. Diagnosed: `selected=1` is the batch's winner set (passed is_recipe + extracted); `rank_score` is **pure authority** computed for EVERY SERP candidate including rejected non-recipes. The two high also-rans are a **spice blend** (gimmesomeoven/cajun-seasoning — the "too thin, 3 instructions" save) and a **listicle** (tasteofhome/collection) — correctly excluded, but high-authority so they sort above the winners. The reserved **`cohort_status` column is never populated**. **Chosen fix (next build):** plumb the batch reject reasons → `cohort_status` and label them inline in the cohort panel ("not selected — too thin / not a recipe"), keeping them in place (the OU fit needs the full authority landscape). NOT yet built.

### Cajun follow-up — ranker hypothesis CORRECTED + cohort_status labeling SHIPPED
Investigated "unify the rankers (Stage 3)" and **disproved it by measurement**: running `rank_by_blend` over the real Cajun cohort matches SQL `rank_score` order (the two rankers AGREE — only a tie-swap). The perceived divergence is `rank_score` (authority, full cohort) vs `selected` (re-flagged from actually-SAVED winners, `save_recipe_api.py:3855`): high-authority pages that fail the save-gate (gimmesomeoven seasoning = "skip-thin"; tasteofhome collection = "extract-miss") correctly aren't winners but still show their authority score. NO ranker change made. **Built the labeling instead:** `score_data_points_for_dish(reject_reasons=)` sets `cohort_status` ('selected' / too_thin / extract_failed / save_failed / fetch_failed / 'reserve'), fed the save-loop `rejects` map at the post-save re-flag; `/dishes/{name}/cohort` returns it; `dishes_v2.html` cohort panel shows a muted "not selected · too thin" chip (`.ed-t10-status`, css ?v bumped). Verified on a WAL-complete copy: gimmesomeoven→too_thin, tasteofhome→extract_failed, winners→selected. Populates on next refresh. WAL gotcha noted: copy via `sqlite3 .backup`, not `cp` (misses -wal).

### DB index migration SHIPPED
Added to `init_db` (idempotent every boot → fresh/portable installs included): expression indexes `idx_mr_dish` (master `$._master.dish`), `idx_mr_likelydish` + `idx_recipes_likelydish` (`$._identity.likelyDish`), `idx_mr_chapter` (`$.classification.chapter`) — mirroring the existing `idx_recipe_json_id` precedent. Verified the recommender path now SEARCHes `idx_mr_dish` → **21.9ms → 0.22ms (~100×)**. Also audited ALL indexes for redundancy and **dropped two prefix-redundant ones**: `idx_drdp_dish` (prefix of `idx_drdp_dish_rank`) + `idx_dish_editors_choice_dish` (prefix of the `(dish,url)` unique). Everything else is used or on a tiny table. ANALYZE refreshed; `PRAGMA optimize` maintains.

### `/recipes` list projection SHIPPED
`GET /recipes` gained `summary=1` (slim `_recipe_list_data` projection — only the fields the sidebar cards render: name/classification/_scoring/_source/_batch/_master.exceptionalism/_grade/image/editorial.opinion/provenance.author) + `limit`/`offset` pagination (0=all default; full-data default unchanged so other consumers are safe). Sidebar (`recipe_form_styled.html` loadRecipes) now fetches `&summary=1`. Verified master list **10.87MB → 0.96MB (~11×)**, all sidebar fields present, limit works. NOTE: if the sidebar ever renders a NEW field, add it to `_recipe_list_data` or the card silently loses it.

### Voice fix
Disabled the proactive "Read the tip?" auto-offer by default (`TIP_OFFER_ENABLED=false`): on the turn-locked mic (no barge-in yet) it spoke right after step 1 and SWALLOWED a cook's "next" (mic deaf while Chef talks) — broke core nav "right from the first step". Deterministic "read the tip" command stays. Re-enable post-P1-barge-in.

### `/domains/{domain}/recipes` optimized (last audit item)
Was an 85ms double full-table parse (loaded+`json.loads`'d all 960 blobs to derive host in Python). Rejected an expression index — SQL can't replicate `urlparse().hostname` (ports/casing/www) exactly, and getting it wrong corrupts the domain browse. Instead: cheap pass over `(recipe_id, url_normalized)` only → match host via `host_from_url`; parse `data` ONLY for candidates (matches + the ~48 empty-url rows that fall back to `_source.originalUrl`, 38 live). **Byte-identical results vs old across 5 domains; 85.6ms → ~20-27ms (~3-4×)**; no schema/index/save-path change. `domains_lib.py`.

### First REAL cook (Chicken Milanese) — success + 2 asks; url_normalized backfilled; graceful ending shipped
User cooked Chicken Milanese end-to-end on the voice cook-view — "worked spectacularly and made a delicious dish." First real-world validation. Two asks ([[project_cookview_feedback]]): (1) graceful end-of-recipe sign-off, (2) intelligent sub-steps for voice memory load (the "most important" 2.1/2.2).
- **url_normalized backfill DONE:** set `url_normalized = normalize_url(_source.originalUrl)` for empty-but-sourced rows — **37 applied, 1 skipped** (a dup pioneerwoman master that would violate the (url,user) unique). Dry-run + collision-checked; live DB updated (recipes.db; refresh recipes.sql via bcc_backup.bat when convenient). Drops the domains fallback floor + fixes cache/dedup/join participation for those rows.
- **Graceful ending SHIPPED** (`cook.html`): `celebrateFinish()` — "next" past the last step now speaks a warm random sign-off (Enjoy! / Time to serve — dig in! / Bon appétit! …) + 🎉 status instead of dead-ending. Hooked in `cookView.next()` so ALL paths (voice/button/tap) get it; speaks when Talk on. Data-driven home (a "finish" Messages category) is a later upgrade.

### Paid-publisher PA-calibration (paywall tax correction) — display half SHIPPED
Premium/gated publishers (NYT, ATK, Cook's Illustrated, Milk Street) rarely make finals: their recipe PAGES are link-starved (paywall → few backlinks → low PA) while their DOMAIN authority stays high, so OU (= PA − predicted-PA(DA), fit on the free-dominated cohort) scores them very negative. Measured: free PA/DA ≈ 0.84; ATK 0.49, NYT 0.61. NYT survives (PA still ~free-expected, tax only +4.8); ATK is crushed (+15.6 tax); CI/Milk Street never even fetched (n=0). **Fix = per-publisher shift-AND-scale remap of PA to its free-equivalent:** `adjusted_PA = max(pa, μ_free(DA) + (PA − μ_paid)·(σ_free/σ_paid))`. The slope (σ_free/σ_paid) matters — paid PA is COMPRESSED (ATK σ2.5 vs free σ5.5 → slope 2.18), so a constant offset under-discriminates; the trendline restores spread. `max(pa, …)` makes it one-directional (only LIFTS a suppressed page; NYT, already above free, untouched). [[project_paid_pa_calibration]].
- **Calibration stored on the domains master** (new cols: `paywall`, `pa_cal_mean/std/n`, `pa_cal_free_mean/std`, `pa_cal_at`) — editable in the domains editor, recomputed by the harvester; scorer reads it (min n≥15 guard). `domains_lib.set_paywall_calibration` / `get_paywall_calibrations`.
- **Harvester** `scripts/calibrate_paid_pa.py`: NYT/ATK from corpus (free), absent publishers via SerpAPI site: (recipe-URL filtered) + Moz. Also emits **top-N-by-PA per publisher = the "best/most-notable recipes" ingestion / Editor's-Choice queue** (user's idea). FINDING: SERP `site:` surfaced only **5** real Milk Street `/recipes/` URLs (Google barely indexes gated content) → gated publishers need their **sitemap** for coverage. Milk Street DA is only ~57 (smaller brand than NYT/ATK).
- **Scorer DISPLAY half wired** (`score_data_points_for_dish`): effective_pa CASE remaps flagged-domain PA for OU + power. **Validated on a copy:** ATK Asparagus lifted from excluded → #1 (96.8, OU 14.1); NYT preserved; free intrinsics unchanged (only percentile re-rank). Server restarted.
- **SELECTION half SHIPPED:** `_apply_paywall_remap(entries)` in build_query_batch — same shift-and-scale max(pa,remap), applied AFTER the raw fit_data_points snapshot (ledger stays truthful) and BEFORE the OU fit + rank_by_blend, so paid sites are RANKED + SELECTED on merit. `score_data_points` re-applies the same remap to the raw ledger → consistent, no double-count. `get_paywall_calibrations(conn=None)` opens the default DB so the batch reads it conn-free. Validated: ATK 36→50.8 / 41→61.7 lifted, NYT + free untouched. Takes effect on the next dish RE-RUN.
- **SERP discovery FIXED (user's catch):** `site:{domain}/recipes` + `filter=0` returns **90** Milk Street recipe URLs (vs 5 with the old `site:domain recipe`) — Google's site: ranking surfaces notable pages naturally, **no sitemap needed**. Milk Street re-calibrated **n=53** (DA 57, shift +11.8, slope 1.85 → now active). All 3 publishers calibrated: NYT/ATK (corpus) + Milk Street (SERP). Real top-10 lists emitted.
- Future: authenticated Playwright fetch to INGEST gated recipes (CI/Milk Street) into the corpus; SERP-harvest NYT/ATK too (vs the 64/22 corpus sample) for a fuller top-10.

### Domains-page-like-dishes + collections membership — FOUNDATION shipped (stage 1)
User wants the domain/publisher page to work like dishes_v2 (refresh a publisher → top-N after filters; search publishers) WITH multi-membership. Architecture settled: ONE content master (master_recipes, deduped by url_normalized — same recipe CAN'T land twice, the unique key prevents it), MANY memberships via a junction; per-collection ledgers hold ranking. A publisher is a collection TYPE.
- **`collections_lib.py`** (foundation): `collection_members` junction (collection_type, collection_key, url_normalized + da/pa/adjusted_pa/rank/selected) = the M2M membership + ranking ledger. `replace_members` (delete-replace ON THE JUNCTION, not master rows — the collections-correct fix dishes still lack), `get_collection_top` (LEFT JOIN master_recipes on url_normalized → `ingested` flag + real name/grade/thumb; "the url work" linking ledger↔content), `get_memberships_for_url` (a recipe's collections), `harvest_publisher_top` (discover + Moz + rank by PA, keep top-N). Verified: multi-membership + junction-only delete + JOIN.
- **`domains.keep_top_n`** column (default 10, per-publisher overridable — the dish-top_n analog; user wants "choose how many to keep").
- **Discovery query is PER-PUBLISHER CONFIGURABLE** (`recipe_path`/`query`): default PATH-prefix `site:{domain}/recipes` + filter=0 (measured 90 real /recipes/ URLs vs 2 for the term form `site:domain recipes` — path won decisively for NYT/ATK/Milk Street; `site:domain/path` is a valid Google operator). Term form available for publishers whose recipes aren't under /recipes/.
- ⚠️ **SerpAPI key 401 (Invalid API key)** — was valid earlier this session (harvested 90/53), now rejected → likely QUOTA exhausted from the session's discovery calls (or key changed). Check serpapi.com/manage-api-key before the next harvest.
- **NOT YET (stages 2-3):** domains-page UI (refresh button → publisher_refresh job → top-N panel + search) + membership chips on the recipe form.

### Domains page + collections membership — ALL 3 STAGES SHIPPED + live-validated
- Stage 1 (`collections_lib.py`): membership junction + publisher harvest + recipe-path detection (above).
- **Stage 2 — domains page like dishes:** `POST /domains/{domain}/refresh-top` (detect path → harvest → replace_members → persist recipe_path/keep) + `GET /domains/{domain}/top`; domains.html "Top recipes — publisher refresh" panel (keep-N + recipe_path inputs, Refresh button, ranked list w/ ★/in-corpus/PA). Live: Milk Street auto-detected 'recipes', 43 scored, top-10 kept. (Refresh inline ~30-45s — could become a job.)
- **Stage 3 — membership chips:** `GET /recipes/{id}/memberships` (dish winners + publisher/other) + "Member of:" chips on the recipe form. Verified: a Potato Gratin winner shows its dish chip.

### Next (queued — none in flight)
- Authenticated Playwright fetch to INGEST gated recipes (CI/Milk Street) into the corpus (calibration + top-N queues ready); promote publisher refresh to a background job (vs inline ~30-45s); membership chips will then light up for ingested publisher picks.
- Re-run a dish with ATK to see the paid-PA selection half land (ATK → selected winner).
- Intelligent sub-steps (2.1/2.2 — the cook-view "most important"); Voice P1 (barge-in/AEC, Murf streaming TTS, voice-agent.js extraction).
- **Intelligent sub-steps (2.1/2.2 — user's "most important"):** break dense steps into memory-sized chunks for voice, cluster-but-split (not naive sentence split). Client-side auto-split first, model-authored later; then full-screen up/down No-look swipes become safe.
- Voice P1 (barge-in/AEC — the real "interrupt Chef" fix, also re-enables the tip offer; Silero VAD, semantic turn-detector, Murf streaming TTS).
- Voice P1 (when ready): Silero VAD + semantic turn-detector + sherpa-onnx wake + WebRTC-loopback AEC barge-in; wire Murf streaming TTS; extract the client loop to `voice-agent.js`.

---

## Session log — 2026-06-16 — intelligent sub-steps (2.1/2.2): research synthesis → v1 sub-step engine → stabilized back to whole-step (v2 belongs in cook_rework)

Picked up the cook-view "most important" follow-up — **intelligent sub-steps** for voice memory load. Did it the research-before-design way: synthesized the cognitive science first, built a client-side v1 against the existing `_cook`, cooked a real recipe on it, and let that first cook tell us v1 was the wrong altitude — so it was stabilized back to whole-step reading with the genuinely-useful pieces kept, and the real fix re-aimed at model-authored v2 in `cook_rework`. All on `split/enrichment-api`, three commits. [[project_cookview_feedback]], [[project_recipe_anchor]], [[project_marketing_differentiation]].

### Research synthesis first — `docs/procedural-instruction-research.md` (commit `08ff802`)
Deep-research synthesis (workflow `wf_77ce16a2` · 23 sources, 25 claims, 3-vote adversarial verification): the cognitive science of presenting a procedure to a human *executing* it, distilled to concrete design rules for the sub-step engine. Strong/verified findings: working memory is **~4 chunks** (not the folk 7±2); cognitive load is driven by **element interactivity** (split INDEPENDENT actions apart, cluster COUPLED ones together); **segmenting + user-pacing is the top lever** (the science license for check-to-advance); the **transient-information effect** (spoken instructions must be far shorter than on-screen — voice is gone the instant it's said); **expertise reversal** (detail should fade as skill grows). Design rules: split-on-independence, ~3-4 interacting elements per step, dual rendering (voice terse / screen full-with-numbers), expertise fade. Open gaps flagged: interruption/place-keeping and applied-practice (checklists/TWI/mise) unverified. Doubles as the proof behind the marketing "cognitively-grounded instruction" pillar + the general procedure-engine generalization.

### v1 — the "sub-step boogie" (client-side, on existing `_cook`) (commit `9c4eba0`)
Built entirely in `forms/cook.html`, grounded in the research doc:
- **`splitSpoken()`** — an element-interactivity splitter: break INDEPENDENT actions apart, keep COUPLED ones (`while`/`until`/`as`/`meanwhile`) together; only split long (>14-word) sentences on sequential markers; merge tiny fragments upward.
- **Sub-step-granularity nav** — `read`/`next`/`back`/`repeat` operate at sub-step grain **when Talk is on**: each "next" speaks the next memory-sized chunk, then completes the step + advances. back/repeat re-speak the prior/current sub-step = the voice "re-scan" (per the user's correction: voice CAN re-scan, just higher-friction than a glance). Silent/screen mode stayed whole-step. On-screen "Step N · part i/m" cue; the whole step stays visible.
- **`speakMise()`** — spoken pre-step mise at fresh hands-free start + a "mise" command: clusters laid out and **measured once by name** so steps can refer to clusters by name.
- **`whereWasI()`** — a "where was I" command (step N of M + current sub-step) — place-keeping for interrupted cooks, the research-flagged open gap.

### Stabilize — first real cook said v1 was too granular (commit `cbd9272`)
First real cook of v1 (**Chicken Milanese**) exposed two problems: the client-side step-chopper was **tedious** (tiny fragments, step title split off its action) and the one-block mise was **too long + duplicated the prep steps** (it spoke "combine the spices" that a later step also covered). The fix:
- **Removed `splitSpoken` step-chopping** → steps read **WHOLE** again (back to pre-v1 behavior).
- **Mise is now per-cluster check-to-advance** (one cluster per "next") AND **opt-in** via the "mise" command — no longer auto-played at start.
- **Kept** where-was-I (mise-aware) + the graceful ending.

The lesson: naive client-side splitting can't tell gather/measure mise from prep-action steps, so it duplicates and fragments. The **real fix is an organic whole-recipe re-plan authored by the model** in `cook_rework` (distinguish mise from prep, cluster-name + measure-once, no duplication) — that's v2, WIP design.

### v2 (planned — the real altitude)
Model-authored sub-steps + cluster naming/amounts-once in `cook_rework`; sub-stepped mise that doesn't duplicate prep steps; a verbosity preference (expertise fade) chosen at cook-page generation. The client-side v1 proved the UX mechanics (sub-step nav, place-keeping, per-cluster mise pacing) cheaply; the intelligence moves server-side into the rework engine where it has the full recipe to reason over.

### Verified / ops / housekeeping
`forms/cook.html` inline JS `node --check` clean across all three commits; iterated live on the real cook. **Reverted a stray `let` typo** accidentally IDE-pasted onto line 1 of `docs/procedural-instruction-research.md` (uncommitted). `recipes.sql` shows uncommitted (a backup dump ran but wasn't committed — refresh/commit via `bcc_backup.bat` when convenient). Two `uvicorn_cookview_agent.{out,err}.log` files are untracked (gitignored class).

### Follow-ups (priority)
- **Sub-steps v2 in `cook_rework`** — model-authored whole-recipe re-plan: separate mise (gather/measure) from prep-action steps, cluster-name + measure-once, no duplication; emit sub-step structure as data so the renderer stops guessing. This is the real fix; v1 is the validated UX shell.
- Verbosity/expertise-fade preference at cook-page gen (research: expertise reversal).
- Still queued from 06-15: Voice P1 (barge-in/AEC, Murf streaming TTS, `voice-agent.js` extraction); authenticated Playwright ingest of gated recipes; promote publisher-refresh to a background job; re-run a dish with ATK to land the paid-PA selection half.

---

## Session log — 2026-06-16 (later) — sub-steps v2 SHIPPED: model-authored voice split (engine + gauntlet gate + renderer), proven end-to-end

Picked up the v2 follow-up the same day. The morning's v1 was a client-side heuristic chopper (removed as tedious); v2 moves the intelligence into the rework engine where it has the whole recipe to reason over, emits the split as DATA, and the cook-view voice loop just walks it. Built + verified end-to-end on `split/enrichment-api` in two clean commits (`86bd215` engine, `4d7948b` renderer). Grounded in `docs/procedural-instruction-research.md`. [[project_recipe_anchor]] phase 4+, [[project_cook_voice]], [[project_cookview_feedback]].

### The engine half — `cook_rework` v2.2 (`86bd215`)
- **`cook_model.CookSubStep`** + `CookStep.substeps` (optional ⇒ old `_cook` reads whole, back-compat): `voice` = the TERSE spoken action (the ear gets the verb, not every number — research's transient-information effect); optional `screen` = a fuller fragment carrying the measurements via the parent step's `{ingN}`/`{amt}`/`{bundle}` tokens; `ingredients` = 1-based parent indices deployed in the sub-step (the coverage map).
- **`cook_rework`**: `substeps` added to the emit schema + a prompt section encoding the research rules — SPLIT on independence ("dice · mince · heat oil" = 3), CLUSTER on coupling ("whisk WHILE pouring" = 1), ≤3 things held at once, voice-terse / screen-numbers, every step ingredient spoken. `REWORK_PROMPT_VERSION → cook-rework-v2.2-2026-06-16`.
- **`cook_validators.v_substeps`** — a lenient 9th gate, fires ONLY when a step has substeps (zero regression on the 16 existing reworks): voice non-empty, indices in range, and COVERAGE — every step ingredient is deployed in some sub-step so the split never silently drops an ingredient the cook needs to hear. Quality (the actual split decisions) stays the model's judgment, matching the gauntlet's "mechanical integrity only" philosophy. 9-gate self-test green (good fixture passes WITH substeps; a planted coverage hole fires the gate).

### The renderer half — voice walks the split (`4d7948b`, `forms/cook.html`)
Re-applied v1's proven sub-step nav, but sourced from `step.substeps` (model data) instead of the deleted `splitSpoken()` heuristic — and kept the per-cluster mise. **Talk-ON**: `read` enters a step + speaks sub-step 1; **"next" advances one sub-step** (terse voice line) until the chunks are exhausted, THEN completes the step + moves on; **back/repeat re-speak the prior/current sub-step** (the voice re-scan); status shows **"Step N · part i/m"**. **Talk-OFF** (silent/screen) = whole step, untouched (the screen tolerates more). **where-was-I** now re-orients to *step N of M · part i/m* and re-speaks the CURRENT chunk (the place-keeping payoff). `prefetchAllSteps` warms each sub-step voice line so "next" plays instantly; `reset` clears the state. `node --check` clean.

### Proven end-to-end (not just compiled)
Ran a real Opus rework on Chicken Milanese: the model split **research-correctly** — Step 2 (cutlet) → slice / pound / season; Step 5 (fry) → heat / fry / drain; **clustered** the dredge ("flour, then egg, then press into the panko") as ONE sub-step; **dual-rendered** (`voice` "Pound each cutlet thin" / `screen` "Pound to {amt:1/3 inch} thick"). Gauntlet PASSED (after the normal 1 repair). Then **persisted** a v2 rework to the user's Chicken Milanese (recipe_id `38696773…`, user_id 5 — 7 steps / **15 sub-steps**) and confirmed the **live API serves every sub-step under `data._cook`** (the exact `GET /recipes/{id}?user_id=` the cook-view fetches). Open **`/cook/38696773-7f67-4684-ae33-177747a6e015`** to cook it — cook.html is no-cache so the renderer is live on reload; the out-of-process rework job picks up v2.2 with no server restart.

### Ops / notes
- `docs/procedural-instruction-research.md` build-status updated (v1 → **v2 SHIPPED**); v2 follow-ups listed.
- Bare-script gotcha (recorded): `cook_rework` needs `ANTHROPIC_API_KEY` from `.env`; a script that doesn't import `save_recipe_api` must `load_dotenv(dotenv_path=".env")` (find_dotenv() can't introspect a stdin frame).
- `recipes.db` changed (the persisted v2 `_cook`) → `recipes.sql` refreshed + ADAM backup re-run.

### Text-hygiene polish pass (user ask: dedup/grammar/flow/punctuation "as part of the process")
User noticed the cook-view prose has word-duplication + punctuation/flow defects. Added a deterministic, judgment-free, **token-safe** clean-up (no LLM — can't change amounts or break `{ingN}`/`{amt}`/`{bundle}` tokens, which are masked then restored), in TWO places so both new and already-saved recipes are covered:
- **Source (`cook_polish.py` → `polish_cook` called in `cook_rework._assemble`, before the gauntlet):** `tidy()` collapses doubled words ("the the"→"the"), doubled/space-before punctuation, runaway whitespace, and sentence-start-caps; digit-safe ("1.5"/"1,000" untouched). `strip_leading_article()` drops a leading the/a/an/your off USE labels (the chief "Add the **the** garlic" source — a bare noun is wanted, the prose supplies the article). Unit-tested (7 cases).
- **Render (`cook.html` `tidyText`/`stripArticle`, mirrored):** runs in `expandInstr`/`spokenText`/`subsFor` so the ~recipes already persisted (pre-polish reworks) clean up LIVE on reload — no re-rework needed. `node --check` clean; the live no-cache static serves it.
- Gotcha fixed: the JS token-mask sentinel first wrote literal NUL bytes into the file (git flagged binary) — replaced with the `\u0000` escape (same runtime char, clean source). Verified served file has 0 nulls.

### Cook-rework TRUNCATION bug (José Andrés Gambas: "finished but ignored all the steps") — fixed
User ran a 14-instruction recipe through the rework; it "finished" but persisted **0 steps**. Root cause: v2 `substeps` inflate the emit, and at `_MAX_TOKENS = 8192` the forced-tool output **hit the cap before the `steps` array** (the schema emits steps late) → partial JSON → `steps` defaulted to empty → and because every other gate iterates OVER steps, the gauntlet **passed vacuously on zero steps** → a stepless `_cook` persisted. Three-part fix:
- **`_MAX_TOKENS` 8192 → 16000** (Opus 4.8 supports 128K, but a non-streaming call is bounded by the SDK ~10-min guard ≈16K; 16000 ≈ 2× headroom, stays non-streaming). Per [[claude-api]] reference.
- **Truncation now fails LOUDLY:** `_emit_cook` checks `resp.stop_reason == "max_tokens"` and raises instead of returning partial JSON — a too-large recipe errors visibly rather than silently persisting empty.
- **New gauntlet gate `v_has_steps`** (the floor): zero steps ⇒ FAIL, so a stepless cook can never pass/persist regardless of cause. 10-gate self-test green.
- Verified: Gambas re-reworked clean — **11 steps / 19 sub-steps** (first pass needed 9398 out, exactly what truncated at 8192).

### Follow-ups (priority)
- **Sub-steps v2.1 (the deeper re-plan):** separate the mise (gather/measure) from prep-ACTION steps so the mise walk and the step sub-steps don't overlap; cluster-name + measure-once across the whole recipe. v2 splits each step well; the mise/step boundary is the remaining organic-re-plan piece.
- **On-screen sub-steps — SHIPPED** (same day, follow-up commit): the cook-view renders each step AS its sub-step checklist (the `screen` fragment, else the spoken action) and lights up the chunk being spoken (`.substep.active`, synced to `_subIdx` via `highlightSub`) — dual voice/screen rendering complete. `expandInstr(step, text)` now expands a fragment's tokens; steps without substeps still show the whole instruction. Verified the live no-cache static serves it (no restart). Generalization confirmed: ANY recipe re-reworked through v2.2 gets sub-steps — only Chicken Milanese is re-reworked so far; the ~16 pre-today reworks render whole until re-run.
- **Verbosity / expertise-fade** preference at cook-page gen (research: expertise reversal) — verbose↔concise clusters more per sub-step for experienced cooks.
- Carried from earlier: Voice P1 (barge-in/AEC, Murf streaming TTS, `voice-agent.js` extraction); authenticated Playwright ingest of gated recipes; promote publisher-refresh to a background job; ATK paid-PA selection re-run.

---

