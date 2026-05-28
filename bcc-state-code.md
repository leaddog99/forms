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

## To-do
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
