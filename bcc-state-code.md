# bcc-state-code

Running state log for the recipe forms project. Append-only style; prune as items complete.

## Interesting links

- https://claude.ai/public/artifacts/fd58ba67-876d-47fc-9610-561ada60639f — TBD context (logged 2026-05-13)

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

### Cache-key design discussion — PENDING DECISION

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
- LLM extract cache table (`llm_extract_cache`) + `input/pipeline/extract_cache.py` helpers; threaded through `markdown_to_recipe` via `cache_db_path` kwarg; cache hits journaled as `cache_hit_markdown_to_recipe` with zero tokens. Initial design used `(url_normalized, markdown_hash, model, prompt_version)` as the cache key — commit `ec0d41e`. **Pending decision: simplify the key to URL+TTL (see Session log "Cache-key design discussion").**

## To-do

- **TOP OF QUEUE: Cache-key simplification.** Currently the LLM extract cache (commit `ec0d41e`) uses `(url_normalized, markdown_hash, model, prompt_version)`. Discussion converged on switching to `(url_normalized, model, prompt_version)` + TTL (30 days default), and adding a **semantic fingerprint** (sha256 of `{name, ingredients[], instruction-texts[]}`) stored on each cache row for **drift detection** — when a re-extract produces a different fingerprint, stamp `recipes.source_changed_at` on every saved recipe with that URL+user so the UI can flag "source page was updated; review and re-save." See the 2026-05-15 session-log section "Cache-key design discussion — PENDING DECISION" for the full reasoning. Choice point: simplification only (~30 lines) vs. simplification + drift detection (~110 lines). User leaning toward the combined plan.
- **User identity model.** Add a user-email field to the form (default `john@johnlandry.com`); replace hardcoded `user_id = 1` in `save_recipe` and `_journal_usage`'s `PLACEHOLDER_USER_ID`. Eventual upstream: Ghost. Every recipe is owned by a user; duplicate source URLs across users are intentionally fine — each gets their own customizable row.
- **General ledger / transactions layer** on top of `bcc_token_journal`. Aggregation queries to roll journal rows into a per-user monthly view: `SELECT user_id, model, SUM(input_tokens), SUM(output_tokens), strftime('%Y-%m', created_at) FROM bcc_token_journal GROUP BY ...`. Then map model + token counts → estimated USD via a price table. Subscription tier model (hard cap / soft cap / overage) still TBD.
- **Re-point journal rows on adopt.** When `save_recipe` adopts an existing recipe_id, the LLM calls from this extract are already journaled under the *originally-minted* UUID. Consider updating `bcc_token_journal SET recipe_id = <adopted>` for those rows so the journal trail joins cleanly to the surviving recipe. Currently their cost history is queryable but doesn't join to `recipes.recipe_id` for the user's canonical record.
- **Refresh existing `metabase_url` rows** scored before the www-variant fix so their PA matches the Moz UI. One-liner: `python -m input.pipeline.refresh_url_metadata --refresh-stale --days 0`.
- Move `RECIPE_PHRASES` out to an editable `pipeline/recipe_phrases.txt` (one per line, `#` comments). User explicitly asked for this; deferred during NYT debugging and never circled back.
- Access-control the form's Metadata section (currently marked `TODO: secure later`).
- Update `pipelineRecipes/` batch project to import schema + stages from `forms/` rather than maintain its own copies.
- Investigate the one Kitchn URL where markdown extraction came back empty (image fallback worked, but worth understanding why JSON-LD or DOM walk missed it).

## Ideas

- PDF input: `pypdfium2` renders each page to an image, sends all pages to vision in one call, returns combined markdown that flows into the same `extract_from_markdown` pipeline.
- Other URL-keyed metadata on `metabase_url`: favicon URL, og:image, domain category, content fingerprint for change detection.
- Source-page error UX: bookmarklet currently `alert()`s when it fails (source page can't render our styled modal). Could inject a styled overlay into the foreign DOM if it becomes worth it.
- Bookmarklet variant that *only* sends markdown (no screenshot upload) for users who never want the cost; or a modifier-key gate (shift-click = force screenshot).
- Make `forms/` pip-installable so `pipelineRecipes/` and any future consumer can `pip install -e ../forms` instead of path-shimming imports.
- **$ cost estimate per call.** Hardcode current per-1M-token prices for the models we use; show in extraction trace; aggregate into ledger. Constants need updating when prices change.
- **Per-user monthly token caps tied to subscription tier.** Hard-cap, soft-cap with warning, or overage charged at $/1K tokens — depends on business model.
- **Ledger granularity.** Per-LLM-call entries (clean atomic units, easy to query) vs per-operation rollup ("one extract" with vision + extract counted as one op). Probably both: per-call ledger rows, plus an `operation_id` foreign key so an op's component charges roll up cleanly.
- **Recipe `_usage` field** as denormalized rollup of *this recipe's* LLM cost, alongside `_scoring`. Ledger stays source of truth; `_usage` is a convenience for showing "this recipe cost you N tokens" in the UI without a join.
- **Auto-snapshot `bcc-state-code.md` updates** at end of session via a hook or memory note so we don't keep forgetting to log changes the same day.
