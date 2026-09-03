the # bcc-state-code

Running state log for the recipe forms project. Append-only style; prune as items complete.

## Interesting links

- https://claude.ai/public/artifacts/fd58ba67-876d-47fc-9610-561ada60639f — TBD context (logged 2026-05-13)
- **[docs/harvest-and-cache-explained.md](docs/harvest-and-cache-explained.md)** — plain-language (high-school level) end-to-end walkthrough of the SEMrush harvest → page cache → recipe cache → change-detecting fingerprint → cache refresh, with a full exceptions table. The gate-by-gate detail is in [docs/recipe-candidate-pipeline.md](docs/recipe-candidate-pipeline.md).

---

## Session log — 2026-07-12 — equipment auto-derive + backfill, Ask Chef un-gated, dishes-list images, log naming, two design notes

Big multi-thread session. All code SHIPPED + live (service restarted, pid 7820); DB backfill done.

- **Equipment on BASE extraction (not a button).** `Tool.size` added to `recipe_model.py` (was
  silently dropped on every RecipeModel round-trip). Equipment derivation folded into the
  markdown-LLM extract prompt (`extract/markdown_to_recipe.py`) AND a fast-lane fallback in
  `enrich/api.py` (`do_equipment` flag — fires a single `enrich.equipment.derive_equipment` ONLY
  when the JSON-LD fast lane yields no equipment; no-op cost on the LLM path). **Correction logged:**
  the earlier belief that "we only extract equipment for cook recipes" was WRONG — base extraction
  had been populating `equipment` all along (2,189 non-cook recipes have it, 657 sized). See
  [[project_equipment_standardization]].
- **✨ Derive button REMOVED** (UI + `deriveEquipment()` JS + `POST /recipes/{id}/derive-equipment`).
  Redundant once extraction auto-derives; the shared `enrich/equipment.derive_equipment` stays (used
  by the extract fast-lane + the backfill). Confirmed 404.
- **Ask Chef (Chef's-Notes chat) — un-gated + made obvious.** Dedicated "💬 Ask Chef about this
  recipe" card at the top of Chef's Notes (was only a subtle ✨ per note bullet — "far from
  obvious"). `notes-ask` endpoint now accepts the form's CURRENT recipe context (`recipe` payload) →
  grounds an answer BEFORE the recipe is ever saved (killed the silent save-gate). `askChefGrounded`
  shared by the card + the per-block ✨. See [[project_notes_chat]].
- **Dishes-list card images — derived from the recipe table, no column.** `dishes.representative_images`
  picks each dish's best-ranked master recipe hero (`_source.previewImage || image[0]`) via a single
  `master_recipes` scan; `/dishes` returns `preview_image`; `forms/dishes.html` renders a 40px
  thumbnail per dish. First cut used a `dish_run_data_points` window-join = **17.7s**; rewrote to the
  master scan = **68ms**, 100/100 dishes. This is Phase 0 of [[project_recipe_table_backed_lists]].
- **Log filenames now timestamp-FIRST** (flat, no subfolder — curator's call). `jobs._build_log_filename`
  → `{ISO-ts}_job_{type}_{id}_{label}.log`; `cook_voice_{day}.log` → `{day}_cook_voice.log`. Sorts
  chronologically in `logs/`; no DB migration (still flat filenames). Takes effect next restart.
- **Equipment BACKFILL saga** (`scripts/backfill_equipment.py`, NEW). Started a blind `--mode all`
  (2,563) — caught via runtime data that it was NOT adding sizes (544→541 on master; sizes come from
  the source text / `_cook`, not derivation) AND was degrading 3 cook-reworked recipes. STOPPED at
  ~255, RESTORED the 3 from `_cook` (lossless), made the script `_cook`-aware (mirrors from `_cook`,
  never re-derives a reworked recipe). Ran `--mode missing` instead → filled 332 empties. Final:
  master 2,224/2,225 equipped (627 sized), personal 338/338 (112 sized), 1 edge-case empty.
- **Two design notes + memories.** [[project_recipe_table_backed_lists]]
  (docs/recipe-table-backed-lists.md) — dishes & domains → first-class recipe-table-backed lists;
  batch-stamp swap (`source`+`batch_id`); VERIFIED domains already do source-scoped deletes safely via
  `retire_master_membership` (typed-block refcount: dish vs publisher). [[project_equipment_standardization]]
  (docs/equipment-product-linking.md) — equipment names/sizes NOT standardized (16,270 items/1,815
  names); design = canonical tool dictionary + xref tables (tool_aliases/canonical_tools/tool_products)
  anchored to the Google Product Taxonomy; buildable offline (block→embed→cluster→LLM-classify→review).
  Surfaced a bug: stringified `_cook` size-dicts leaked into `equipment.size`.
- **Lesson:** an f-string edit crash-looped NSSM at startup (literal `{...}` in `SYSTEM_PROMPT`);
  `ast.parse` passed but the format-spec only errors at real import. VERIFY prompt/f-string edits by
  IMPORTING the module, not just parsing.

## Session log — 2026-07-10 (later) — Chef's Notes chat (✨ Ask), multi-block notes, cook-voice dredge fix, reprocess; "keep source links" decision

Continuation of the cook-voice session, into the recipe form. All SHIPPED + verified live.

- **Chef's Notes chat v1 SHIPPED (33c4d6d)** — each Chef's Notes block gets a **✨ Ask** button:
  sends the block text as a question, grounded in the SAVED recipe (reuses `cook_ask` —
  this recipe + KB + general cooking knowledge, brand-safe, cooking-only), stores the answer
  as an editable `Q: … / A: …` block; `+` = a new block → running chat history. `cook_ask.ask()`
  gained an `operation` label; new `POST /recipes/{id}/notes-ask` wraps in
  `llm.context(recipe_id,user_id)`+`flush` and journals as **`notes_chat`** (per-feature + per-user
  billing). Verified live: ~5s round-trip, grounded answer, journal row stamped
  (user 5 / recipe / sonnet-4-6). See [[project_notes_chat]]. Deferred: streaming, notes-tuned
  prompt, AI-provenance flag, multi-turn context.
- **Chef's Notes = multi-block field (b40c8a8)** — was splitting a pasted block into one item
  per line (single-`\n` between-entries separator). Now blocks separate on a BLANK line (`\n\n`),
  so a pasted block keeps its own newlines and stays ONE entry; Enter = newline within a block
  (+ adds a block). Legacy single-`\n` notes load as one merged block (no data loss).
- **Cook-voice dredge fix (06ceee8)** — the earlier "voice reads the screen fragment" change was
  WRONG: `screen` is visual shorthand (arrows/chips), so the Milanese breading dredge voiced as a
  bare ingredient list. Reverted: voice reads the `voice` field (ear-written), fall back to `screen`
  only when empty; `→` voices as "then"; equipment lead-in keeps its trailing space. Plus "Tip
  available" spoken at the end of a step that has a tip (274dbf2), and `go`/`start` next-synonyms
  + `comment(s)` tip-synonym.
- **Reprocessed Chicken Milanese** through the current cook-rework engine (v2.2, job 496) — gauntlet
  passed first Opus pass; 7→6 steps; the dredge is now explicit in the `voice` field and the
  "half the half seasoned salt" oddity is gone. ~$0.44.
- **DECISION — "unlink from original": NOT happening.** Curator confirmed the source/provenance
  links stay (`_source.originalUrl`/`siteName`/`pageScreenshot`, `provenance`, `_match`,
  `url_normalized` all remain on user rows). "Recipe unique per user" = freely editable, NOT stripped
  of attribution. No unlink migration to build.

## Session log — 2026-07-10 (cook voice polish) — equipment lead-in · voice=screen · wake mis-hears · filler grace beat · onset capture

Live cook-view voice session ("hey chef works much better!"), five fixes in `forms/cook.html`
(+ one in `cook_stt.py`), all verified by parse-check; see [[project_cookview_feedback]], [[project_cook_voice]].

- **Equipment lead-in.** Entering a NEW step announces `equipmentPreamble(step)` — "You'll need a large
  skillet and a baking sheet." (sizes voiced inline, de-abbreviated) — then bridges to the first sub-step.
  Fires ONLY on entry (`read()` → `speakSub(0, lead)`; next/back/repeat/re-scan don't), prefetched with the
  entry utterance so it's instant.
- **Voice reads the SCREEN, no condensing** (curator: "don't truncate… you are losing too much information").
  New `spokenFragment(step,text)`; `subsFor` now voices each sub-step's `ss.screen` fragment (tokens
  expanded, units de-abbreviated), NOT the model's terse `ss.voice`. REVERSES the [[project_recipe_anchor]]
  terse-voice/screen split for the voice half (model still authors both fields).
- **Wake-word mis-hears** (voice log showed base.en hears "hey chef" as pay/lay/it/today chef → those fell
  through to the LLM and got the think-filler cut off). `_WAKE_HEY` now matches the whole "-ay" rhyming
  family generatively (`[a-z]?ay` = lay/pay/say/day/way/… + bare ay) + hey/hi/eh/it/a/okay, glued to `chef`.
  So "lay chef next" strips clean → INSTANT local command, no LLM, no filler. Verified no false positives
  ("is it done yet", "play the video", mid-word "today chef" all safe via `\b`).
- **Filler grace beat.** `startThinking()` waits `THINK_LEAD_MS=700` before the first "still working" line; a
  fast reply cancels it, so short interactions never hear a filler start then get cut mid-word.
- **Onset capture.** VAD `preRollMs` 250 → **400** (ring copy, no new false triggers) so a soft onset
  consonant isn't clipped + Whisper gets a lead-in run-up. STT `_HOTWORDS` now leads with **"hey"** (was
  "chef" only) to stop base.en decoding the wake onset as pay/lay/today — the audio HAS the /h/; the decoder
  was guessing. (hotwords change needs the BCC restart to take effect.)
- **Also fixed:** the `message_categories.general` fallback bucket held a `__SMOKE_TEST__` sentinel that the
  cook think-filler read aloud ("why does it keep saying smoke test") — removed from recipes.db. Real
  `cook_ack`/`cook_wait` lines activate on restart. See [[project_friendly_status_messages]].

**All uncommitted-until-now cook.html + cook_stt.py; reload applies everything except the two restart-gated
bits (hotwords + cook_ack/cook_wait seeding).**

## Session log — 2026-07-10 (product commerce, cont.) — factual `description` field + product sqlite-vec + size normalizer + portable bookmarklet host

Continuation of the 2026-07-08 product session (see [[project_product_commerce_build]], [[project_affiliate_catalog]]).
Closed the extractor's image/description/sizing gaps, gave products their own vec0 KNN, and made the
bookmarklet host portable — all on `split/enrichment-api`. UNCOMMITTED work from an interrupted session,
now committed. Server is STALE (last start 2026-06-25) so NONE of this has run live yet.

- **Factual `description` field** (`product_model.Product`) — LLM-normalized, de-marketed product copy
  (material / construction / size / key features); human-facing AND the primary semantic signal for
  matching/embedding. Distinct from `bcc_blurb` (our opinion). Wired through `forms/product_form.html`
  (load + save) and embedded on save as the canonical text.
- **Size normalizer** (`intake/products/measures.py`, NEW) — one canonical form for the messy retailer
  size syntax: unicode fractions → decimals (`8⅝ → 8.625`), `×/by → x`, `2 Qt → 2 qt`, ranges preserved
  (`6-7 qt`), and class-token sizes (`Dutch Ovens (6-7 Quarts) → (6-7 qt)`). Owns only PARSING; the unit
  vocabulary stays single-source in `enrich.measurement.convert` ([[feedback_single_path]]) — added a
  `LENGTH` domain (mm-based, non-culinary) + a `PREFERRED_SYMBOL` display-canonicalization table +
  `is_known_unit`/`preferred_symbol`. `catalog_store` normalizes on save + match (idempotent).
- **Product sqlite-vec SHIPPED** ([[project_sqlite_vec_migration]], [[project_vec_delete_triggers]]) —
  `products_vec` vec0 table in `vector_store.py` (`ensure_product_vec_tables` + AFTER DELETE trigger,
  `upsert`/`delete`/`find_similar_products`, `rebuild_products_vec_from_blobs`), aux columns
  `product_class` + `category` for same-class cross-sell / exclusion KNN. `catalog_store` keeps it in
  lockstep with the source-of-truth `products.embedding` BLOB (one-time backfill on init), embeds the
  canonical `description`. Replaces the in-Python numpy scan (mirrors `find_similar_dishes`).
- **Portable bookmarklet host** ([[project_portable_package]], [[feedback_no_data_in_code]]) —
  `system_config.public_base_url` (seeded from `bcc_link_domain`, scheme-normalized to an origin) is now
  the canonical externally-reachable host; `save_recipe_api` `/brand` returns it as `bookmarklet_api_base`.
  `install.html` + `product_bookmarklet.js` read the host back from the server (nothing baked into the
  bookmarklet code) so a self-hoster ships their own domain with no code change. `forms/product_install.html`
  (NEW) = the catalog-ADMIN install page (clay/teal accent, visually distinct from the user-facing recipe
  grabber).
- **NEXT (unchanged):** BCC restart to exercise the bookmarklet → form → extract → save loop + all product
  endpoints live; then classify into category/product_class + the `products_vec` recommender + the
  recipe→product_class link. Restart = `Restart-Service BCC` (admin; `bcc_restart.bat` self-elevates).

## Session log — 2026-07-10 (cook voice) — spoken think-acks (`cook_ack`) + rotating wait lines (`cook_wait`)

Small hands-free-cook polish ([[project_cook_voice]], [[project_friendly_status_messages]]), committed
separately from the product work above. Two new DB message kinds in the `admin_models` seed list:
`cook_ack` (the SHORT immediate ack SPOKEN the instant a "hey chef" question dispatches, before the
rotating lines) and `cook_wait` (rotated while Chef's answer generates so a long wait never sits in dead
silence — written to sound natural aloud, no ellipsis). `forms/cook.html` fetches both from the DB message
store (`loadCookMessages`, no code list) and prefetch-warms the TTS; `cook_stt.py` gains transcript
`_norm` / `_looks_repetitive` hygiene. Authored — needs a restart to seed the new message kinds.

## Session log — 2026-07-08 (product commerce) — product-first affiliate catalog: demo viewer + retailer extractor + match-or-create + URL→ATK reverse table

Monetization arc (see [[project_affiliate_catalog]]). Goal: recommend products **in context** of a
recipe/step, "advice not ads". Discovered a well-built but forgotten 2026-06 subsystem
(`product_model.py` + `intake/products/review_parsers.py:parse_atk` + `catalog_store.py`, proven
end-to-end: an ATK loaf-pan review → **13 products** under Bakeware/Loaf Pans (1 lb); design in
`docs/reviews-and-product-commerce.md`). Built the missing pieces + pivoted the ingestion model.

- **Demo viewer SHIPPED** — `forms/products.html` (served at `/forms/products.html`) renders the
  existing catalog: category → class → ranked product cards (tier badge, verbatim verdict +
  reviewer, specs, buy chips). Brand-safe by construction (reviewer name + quote, NO review
  link-out; only buy links leave). `catalog_store.list_catalog()` + `GET /product-catalog`
  (needs a BCC restart to go live; the page falls back to the static `forms/product_catalog_demo.json`
  snapshot meanwhile). **Gotcha:** open via the SERVER url, not the file:// path (else fetch fails).
- **Matching decision:** the recipe↔product match is DETERMINISTIC via curated `product_class`
  references (recipe.equipment → class → ranked products), and SIZE is the class grain
  ("Saucepans (2 qt)" ≠ "Dutch Ovens (6-7 qt)") — so vectors are for discovery/cross-sell, not the
  primary matcher. The vector experiment (`experiments/affiliate/`, equipment extractor + seed
  catalog + needs-string-vs-affordance A/B) is PARKED as that discovery tool.
- **PIVOT to product-first** (curator's call — review sites lock down → client extraction):
  `extract/markdown_to_product.py` mines ONE retailer product page (Amazon/SLT/WS) → the existing
  `Product` model — proven on a Cuisinart saucepan → `product_class="Saucepans (2 qt)"`, specs,
  Amazon offer, NO fabricated verdict/editorial. The per-source roundup parsers become the "volume
  extractors" for later.
- **Match-or-create save path SHIPPED + tested** (`catalog_store.save_product`/`find_matches`): one
  product = the entity (our `product_id`), a vendor = one `RetailerOffer`. First vendor (Amazon)
  CREATES; later vendors MERGE onto the same product_id via the VENDOR-AGNOSTIC manufacturer key
  (brand + `mpn`/`gtin`/`model_number`, added to `ProductSpecs` — NOT the Amazon-only ASIN); offers
  de-dupe by (retailer, asin/sku) so re-scraping updates price not rows; no-key fallback =
  embedding-nearest SUGGESTION (dist ≤ 0.55 confident) for the form's "add as a vendor to X?" prompt.
  Embeds on save. Verified: Amazon create → WS merge (same model 719-16) → Amazon re-extract updates
  price → 1 row, 2 vendors.
- **URL→ATK reverse table SHIPPED** (`intake/products/review_facts.py`) — the curator's "mass ATK
  scan" idea: materialized `product_review_links` keyed by ASIN + normalized URL → the ATK verdict
  facts, so a product the curator extracts at a retail URL surfaces the ATK rec we already hold
  (in our voice, brand-safe paraphrase via `our_voice()`). Affiliate-tag-agnostic (strips `?tag=`),
  so any vendor's URL resolves. This repurposes the review-roundup path as the ACQUISITION ENGINE
  for the reverse index (not a display surface). Verified on the 13 ATK rows (ASIN + URL lookups).
- `review_model.py` — first-class schema.org `Review` (the "#2 first-class independent object"
  direction) restored, but REVIEWS DEPRIORITIZED this session ("work on the product not reviews").
- **UI SHIPPED (pending a BCC restart to exercise the endpoints):** `POST /extract-product` (markdown
  → Product + reverse-table facts + match suggestions in one call), `POST /products` (match-or-create
  save), `POST /product-fact-voice`, `GET /product-facts`; `forms/product_form.html` (extract → ATK
  facts + "add as a vendor to X?" merge prompt → edit specs/vendors/blurb → save; also paste-to-test);
  `forms/product_bookmarklet.js` (retail page → harvest Product JSON-LD + markdown → /stage-markdown →
  open the form; loader one-liner at the file's end). Reuses the recipe /stage-markdown rails.
- **NEXT:** BCC restart → verify the bookmarklet→form→extract→save loop end-to-end; then classify into
  category/product_class + the products_vec recommender + the recipe→product_class link (§8).

## Session log — 2026-07-08 — is-recipe recall (render-probe · poor-publisher · verb-fallback · comments back-anchor) + BIG externalization sweep

Continuation of the is-recipe arc, on `split/enrichment-api`. Shipped three queued items, then
chased a "clear recipes being dropped" report into a structure-gate fix — and (three curator
call-outs later) moved ALL the structural word-lists out of code into `system_config`.

### The three queued items (all SHIPPED)
- **Render-escalation PROBE (delish fix)** — `build_query_batch._render_rescue`: the chicken-and-
  egg was that escalation only fired for domains ALREADY flagged render-eligible, so a fresh JS
  site was never rescued → never auto-learned. Now a would-be-dropped THIN stub on a NOT-yet-
  eligible domain gets one bounded render probe (per-domain-per-run cap); a success stamps
  `_render_escalated` → existing `mark_render_required` auto-learns it → next run renders up front.
  Knobs: `render_escalate_thin_chars`/`render_escalate_probe`/`render_escalate_probe_max`.
- **Poor-publisher signal** ([[project_domain_master]]) — `domains_lib.refresh_poor_publisher_flags`
  rolls the cascade's per-page verdicts up **by URL host across ALL sources** (the state note's
  "both batches" meant DISH batches; harvest rows have no verdicts yet — verified in training.db),
  flags a host past sample+fraction thresholds (auto-creating a minimal domains row, mirroring
  set_paywall_calibration), and the cascade now **SKIPS the per-page LLM for flagged hosts**
  (`get_poor_publisher_hosts`, new `_QUALITY_COLUMNS`). Runs after each publisher refresh. Verified
  live: **oliveandmango.com (75%) + cravingsbychrissyteigen.com (60%) flagged**, badged in
  domains.html (repurposed the retired allowed pill/legend/sort). Defaults `min_samples=4`,
  `threshold=0.6` (tuned to current sparse data; editable).
- **`allowed` → `disallowed_domains`** — retired the per-domain `allowed` gatekeeper (0 rows ever
  set it). `get_blocked_root_domains` now reads a `system_config.disallowed_domains` LIST (seeded
  from the old hardcoded set) ∪ serp_exclusions; removed the checkbox/badge/legend/sort/save-field
  from domains.html; dropped `allowed` from EDITABLE_FIELDS. Column kept (default 1) to avoid a
  schema migration; `harvestable=0` already covers "keep the record, skip harvest".

### "Clear recipes dropped" → the structure gate (grounded in the Indian Pudding human votes)
Curator's is-recipe conflicts showed real recipes DROPPED `no-recipe-structure`. Diagnosed in
`training.db`: NOT intro length (the gate scans full text) — the method had **no section HEADER**
(narrative prose: "whisk… bake…"), and 2/5 also lacked an ingredients header. **`cascade_mode` is
`decide`, so these were already auto-rescued** (the recorded "dropped" is the pre-override HEURISTIC
label, by design). Still improved the cheap heuristic: **header-less method VERB fallback** in
`validators.has_recipe_structure` — ≥N distinct imperative cooking verbs substitute for a missing
method header, ingredients section still required. Measured on the human labels: **+18 recipes
recovered / 2 not_recipe leaks (both caught by the cascade)**. ("instructions" was ALREADY a method
marker — no change needed there.)
- **Comments back-anchor (curator idea)** — `training_capture._smart_snippet`: when NO recipe anchor
  is found (header-less recipe under a long intro), 'scroll back' a full `max_chars` from the
  earliest comments/footer marker ([intro]→[recipe]→[comments]) instead of handing the LLM the intro
  prose. Verified on a synthetic header-less page. (Clarified for the curator: the snippet JUMPS to
  the anchor anywhere in the page + takes `max_chars`=1600 forward; the old `40`/now-`80` is only
  pre-anchor lead-in, not the reach.)

### THE THEME: no data in code ([[feedback_no_data_in_code]] — called out 3×, corrected)
Moved every structural word-list into DB-resident, curator-editable `system_config` (code keeps only
`*_SEED` fallbacks): `structure_ingredient_markers`, `structure_method_markers`,
`structure_method_verbs`, `structure_method_verb_min` (validators), `snippet_recipe_anchors`,
`snippet_comment_markers`, `snippet_lead_chars` (training_capture — read via lazy runtime
get_setting, memoized regex, no import cycle). New **`docs/is-recipe-vocab-lists.md`**: per-list
purpose + consuming module + authoring guidelines + **multi-language** story (English/base →
system_config; other languages → `recipe_phrases/<lang>.json` "sections"; the verb-fallback / snippet
anchors / comment markers are English-only gaps to promote into the packs for a non-English base).
Also renamed the Labeling UI 'Poor layout' → 'Poor quality' (verbiage match; value unchanged).

### Notes / open
- **Server restart** picks up the save_recipe_api handler + in-process filter; out-of-process
  harvests already see the shared libs; HTML is static. New system_config keys seed on next boot
  (get_setting falls back to seed meanwhile). `allowed` column + a couple vestigial `allowed`
  rank/worklist reads left in place (harmless, all rows =1).
- **recipes.db schema changed** (`_QUALITY_COLUMNS` + poor flags + new domain row) — recipes.sql NOT
  re-dumped this session.
- **Deferred (offered, not done):** rename `is_recipe_cascade_mode`→`llm` (curator DECLINED — kept
  the cascade term-of-art); carried delish stub auto-learn end-to-end verification.

### Follow-up (same session) — training.db backup + finish the config-list migration
- **training.db → ADAM backup** (`backup_db.py`): the git-ignored is-recipe corpus (gold human
  labels) is now copied to ADAM alongside recipes.db, integrity-checked, best-effort (a
  missing/locked training.db can't fail the recipes backup). Its ONLY off-machine copy.
- **Last two `config.py` lists → system_config** (curator: "migrate config py lists to the system
  db"): `RECIPE_PHRASES` → `recipe_phrases` (158 entries; **whitespace is significant — `' ounce'`
  not `'ounce'` — the reader `validators.recipe_phrases()` preserves it, NO strip**) and
  `_DEFAULT_DISALLOWED_URL_PATH_FRAGMENTS` → `disallowed_url_path_fragments` (read live in
  `_filter_disallowed`). Code lists kept as the diffable SEED (system_config seeds reference them, so
  no re-typing); consumers (validators score + `_phrases_and_threshold`, training_capture
  `_matched_phrases`, translate_recipe_phrases script) read live w/ seed fallback. Rich descriptions
  built into each setting. **Bug found + fixed:** `forms/system.html` had NO `list`-type editor at
  all (every list setting incl. the pre-existing `semrush_export_patterns` fell through to a broken
  string input) → added a whitespace-preserving one-per-line textarea + JSON dirty-compare. All the
  session's externalized lists are now genuinely editable. `docs/is-recipe-vocab-lists.md` updated.

### Follow-up (same session) — paywall exemption · cross-job artifact bleed · dishes Refresh/Cancel/Clear
- **Paywall exemption for the poor-publisher flag** (`domains_lib.refresh_poor_publisher_flags`): a
  `paywall = 1` domain is NEVER flagged a poor publisher — its stubs read `poor_quality` because
  they're GATED, not messy (Milk Street was wrongly flagged). Rate/samples still recorded for
  visibility; returns `exempted_paywall`. Verified: 177milkstreet.com cleared.
- **Cross-job ARTIFACT BLEED — fixed (both pages).** Root cause (traced): async job-`done`/reload
  handlers painted into shared list DOM keyed off the CLOSURE's entity with no current-selection
  guard, so a job finishing for A dumped its list onto the open B. `domains.html`: `renderResult`
  bails `if (selected !== domain)`, `loadDomainTop` snapshots+checks a `topGen` token + `selected`.
  `dishes.html`: `doRun` done-handler only rebuilds/append the detail card when `selectedName ===
  d.name` (still refreshes the sidebar row + cache), `loadTopRecipes` guards on `selectedName`+`topGen`.
- **Domains "Clear list" didn't stick — fixed.** Backend was correct (keys match, no master
  fallback); the list was repainted by a trailing async write. `doClearTop` now bumps `topGen` so any
  in-flight/late `loadDomainTop` bails. (Same root as the bleed.)
- **Dishes Refresh · Cancel · Clear buttons** (curator: "like the domain extract has… move the
  refresh from the footer up"). Clarified: **Clear = clean the job-log WINDOW** (view-only, NOT a data
  wipe — I'd overthought a destructive dish-top clear). `dishes.html`: control row lifted out of the
  footer to sit under the header (footer now just Edit/Delete); `clearDishLog()` empties the live-log
  panel; `cancelDishRun()` + `currentRunJobId` arm Cancel while streaming. **Backend cancel made
  REAL**: `build_batch` gained `should_cancel`, threaded into the dish-batch `_is_recipe_filter`;
  `_handle_dish_refresh_job` polls `is_cancel_requested` and records a clean `cancelled` (not error).
  UI verified in-browser (no console errors); **backend cancel needs a BCC restart** to go live.

## Session log — 2026-07-07 (evening) — LLM cascade SHIPPED + DECIDE mode ON + rejected ideas

Culmination of the is-recipe arc. **The LLM cascade is live and DECIDING.**
- **Cascade (SHIPPED):** `intake/isrecipe_cascade.py` — Haiku three-way (recipe|not_recipe|
  **poor_quality**) over the GRAY ZONE (content-bearing, non-JSON-LD candidates) on recipe-anchored
  snippets. `system_config.is_recipe_cascade_mode` = off|shadow|**decide** (replaced the shadow bool).
  **Flipped to `decide` this session** — applies the asymmetric override AFTER training capture (so the
  recorded label stays the heuristic's): RESCUE a heuristic-drop the LLM calls 'recipe'; CATCH a
  heuristic-keep it calls poor_quality/not_recipe; never touches a JSON-LD keep. Harvest jobs read it
  fresh (out-of-process) — no restart needed; admin editor shows it after next restart.
- **`poor_quality` third label** (user's "reject on style" insight — the real target is EXTRACTABILITY,
  not presence): human_label + LLM verdict + Labeling UI button, with a stricter tie-break ("when torn,
  default poor_quality"). Keeps training data honest (a messy recipe is NOT labeled not_recipe).
- **Validated on 28 curator labels (2 dishes):** cascade decision accuracy **75% vs heuristic 29%**
  (≈2.6×); asymmetric-policy precision **RESCUE 77% / CATCH 88%** (catch jumped from a coin-flip once
  poor_quality existed). Deployment-ready → flipped on.
- **TESTED & REJECTED (data killed them — logged in docs/is-recipe-classifier.md):** (1) feed the
  markers to the LLM — explicit hint = no gain; markers-guided **two-section snippet** = REGRESSION
  (68%→46% decision, catch 8/16→5/16, because stripping context made Haiku rubber-stamp roundups).
  (2) count repeats / **structure pairs** to spot multi-recipe pages — REVERSED: json-ld single recipes
  have median 2 pairs (print/jump/related widgets), roundups have 0 (they LINK OUT, don't embed cards).
- **`srsltid` dedup fix** (`url_utils.normalize_url`): Google's per-impression tracking param made one
  page look like 4 → fetched/scored/LLM'd 4×. Added srsltid + Google click-ids to the blocklist.
- **Labeling UI** (`forms/training.html`): three-way `poor_quality` button + filter + badge, LLM-verdict
  badge + "LLM disagrees" filter (fixed its missing reload), human_labeled_at timestamps.
- **Docs:** new `docs/how-the-pipeline-decides.md` (plain-language every gate + embedding=which-dish /
  regression=ranking / embedding-LR classifier=prototype-not-shipped); is-recipe-classifier.md updated.
- Embeddings persisted on `is_recipe_samples` (6,932 rows) → free retrain; hybrid artifact saved but NOT
  live (cascade won on unseen-domain generalization).

**Meta-win this session: having the labeled data made every design call decidable** — the cascade, the
tie-break, and THREE rejected ideas were all settled by measurement, not intuition (my snippet
hypothesis was wrong; the data caught it). See [[feedback_verify_with_runtime_data]].

**Open items for next session:** (1) delish.com fetch-stub fix (67% of stub false-drops = one JS site
set to plain fetch; broaden render-escalation trigger to thin content + auto-learn). (2) domain-level
"poor publisher" signal (oliveandmango tagged poor_quality across BOTH batches → flag the domain, stop
re-paying per-page). (3) `allowed` → a system_config disallowed_domains list (drop it from the extract
form). (4) domain-form redesign mockup → port to live (`forms/domains_mockup.html`, awaiting sign-off).

## Session log — 2026-07-07 (late) — is-recipe classifier: analysis → embeddings → HONEST group-CV

Deep dive on the harvest is-recipe gate (user: "we're losing too many recipes" + summaries leak in).
Grounded in `logs/` + `training.db` (10.9k labeled), NOT code-reads (I was wrong twice from a
code-read/Explore summary — see [[feedback_verify_with_runtime_data]]).
- **Discriminator = structured recipe-card SECTION markers, 32×** (prep/cook time, servings, yield,
  directions:). Verbs 1.7× (how-tos have them), ingredients 1.4×. **Repetition/multi-recipe idea =
  NULL** (real recipes repeat card-timers too, related-recipe widgets). Most false-drops are **fetch
  stubs** (median 226 chars → render/unblocker, not scoring).
- Models, held-out then **group-CV by domain + dedupe** (the honest protocol; random split leaks via
  domain memorization): flat-count leak 73%; embeddings-LR random-split **0.988 = leakage** → group-CV
  0.90; struct-feats-only 0.79; **WINNER = HYBRID embeddings+section-features C=0.05 → group-CV AUC
  0.936 ±0.026**. Moderate honest win over the live structural gate, not a blowout.
- SHIPPED: `is_recipe_samples.embedding BLOB`+`embedding_model` (6,932 rows backfilled → free retrain);
  artifact `models/is_recipe/hybrid_lr.joblib`; design `docs/is-recipe-classifier.md`. Cost: embed
  ~$0.00003/candidate (60× < LLM classify; local embedder = $0). [[project_corpus_ml]] updated.
- **NOT wired live** — integration staged behind an OFF-by-default flag in `_is_recipe_filter` (heuristic
  = offline fallback; optional Haiku cascade for the gray zone; separate render-fetch track for stubs).
  OPEN DECISION: does ~0.94 clear the bar to wire live, vs. more-domains / LLM-cascade first.

Also this session (separate arcs, all shipped): cross-machine SEMrush **upload** endpoint; **SERP per-page
retry**; **recipe_path** as a source-agnostic keep scope; domain-form **redesign mockup**
(`forms/domains_mockup.html`, full reorg — awaiting sign-off). PENDING design: move `allowed`→a
system_config disallowed_domains list (0 domains have both fields today); min-phrase/min-step thresholds
as system_config defaults + dish/domain overrides (superseded in part by the classifier direction).

---

## Session log — 2026-07-07 — recipe_path becomes a source-agnostic KEEP scope (identity vs extract-scope)

**Problem (user):** a domain was auto-created as `epicurious.com` from dish runs; trying to add
`epicurious.com/recipes` (to scope the harvest + filter spurious content) failed with "already
exists" because the domain host is the identity key ([[project_domain_master]] — correct). User's
insight: separate the domain IDENTITY (host) from the EXTRACT scope (the recipe path). Follow-ups:
`recipe_path` was Google-only in the UI, arguably redundant with the path inside a `site:` query,
and had NO counterpart for the SEMrush/file source.

**Diagnosis (grounded in `harvest_publisher_top`):** path scope was fragmented + inconsistent —
`backlinks_file` = `used_path=None` (NO scoping), verbatim `serp_query` = host-check only (soft),
`recipe_path` = the only hard `_under_path` filter (Google-only, "used if query blank"). No single
source-agnostic statement of "this publisher's recipes live under /<path>".

**Fix (single-prefix, reuses the existing `recipe_path` column — no schema change):**
- `collections_lib.harvest_publisher_top`: normalize `recipe_path` to one leading segment
  (`/recipes/`→`recipes`), then apply `_under_path` as a HARD KEEP filter to EVERY source AFTER
  discovery — file rows (the missing counterpart), verbatim-query hits (was host-only), and the
  path-discovery list (idempotent). Blank = no scope (Boston Globe /YYYY/…). DISCOVERY (`source`/
  `query`) and SCOPE (`recipe_path`) are now orthogonal; docstring updated.
- UI (`domains.html`): moved `f_recipe_path` OUT of the Google-only group into the common area,
  relabeled "Recipe URL path — scopes EVERY source" + help text; `serp_query` relabeled
  "how to FIND candidates" (discovery-only). Field is outside any `.src-group`, so it stays
  enabled for all sources.
- Migration risk = zero: 0 domains currently have both `serp_query` and `recipe_path` set, so the
  verbatim-query branch's new hard-scope changes no existing behavior.

Verified: normalization + `_under_path` on real epicurious URLs (keeps `/recipes/<slug>`, drops the
index / `/gallery` / `/video` / near-miss `/recipes-menus`). **No server restart needed** — harvest
runs in a spawned `python -m jobs` subprocess (picks up collections_lib immediately), the UI is
static (browser reload), and `recipe_path` was already an editable/persisted field. To use on
epicurious: open the record, set **Recipe URL path = `recipes`**, refresh. NEXT (deferred): the
add-domain UX guard (path-bearing input on an existing host → open + prefill recipe_path); multi-
prefix if a publisher needs it.

---

## Session log — 2026-07-06 — SERP per-page retry (transient timeout no longer truncates a query)

**Problem (user):** a `dish_refresh` (Hungarian Goulash, job #457, recurring — seen on an
earlier run too) logged `[scaleserp] page 3 failed: ReadTimeout` and then `20 URLs (target 75)`.
Root cause in `serp_search.py`: BOTH `_scaleserp` and `_serpapi` wrapped the per-page GET in
`except Exception: break` — so a SINGLE transient network blip on one page abandoned the entire
remaining pagination, silently truncating a deep query to whatever had already arrived.

**Fix:** new `_serp_get_json()` helper retries a page on TRANSIENT errors only
(`requests.exceptions.Timeout` / `ConnectionError`) — default 3 attempts, 2.0s backoff — before
giving up; a non-transient error (bad JSON etc.) still stops paging immediately, and a real API
error (`request_info.success=false`, e.g. out of credits) / genuine empty page still breaks as
before. Both providers now route through it (single path). Retry knobs externalized +
documented in `system_config` (`serp_page_retries`, `serp_page_retry_backoff`, category
Search). Verified with a simulated page-3 timeout → retried → continued → 50 URLs (old code
stopped at 20). **No server restart needed** — dish/publisher refreshes spawn fresh
`python -m jobs` subprocesses, so a re-run of Hungarian Goulash picks up the new code immediately.

---

## Session log — 2026-07-02 — cross-machine SEMrush upload (client→server)

**Problem (user):** running a `backlinks_file` harvest from a DIFFERENT machine than the
server failed — the SEMrush `.xlsx` was downloaded on the *client*, but the harvest job
runs on the *server* and reads the *server's* filesystem, so the file was never visible.
Confirmed by the improved "no export found … on THIS (server) machine" message + a live
resolution test (barefootcontessa: the saved override pointed at a `…16_24_42Z.xlsx` that
didn't exist; the real file was `…16_26_34Z.xlsx` — the stale-override fallback already
picked the newest match on the server, so it only *ran* here).

**Fix — an upload path.** `POST /domains/{domain}/upload-export` (`save_recipe_api.py`,
after the `backlinks-file` GET): accepts a browser `UploadFile`, validates `.xlsx` +
non-empty + ≤25 MB + `PK` zip magic, sanitizes to a basename, writes it into the configured
inbox (`collections_lib.semrush_inbox_dir()` → Downloads default) — so an uploaded file is
indistinguishable from a downloaded one — then **pins** `domains.backlinks_dir` to the exact
saved path (+ `harvest_source='backlinks_file'`) so resolution is deterministic (exact
existing path returned directly by `backlinks_file_path`; a later upload overwrites the pin,
never stale). UI (`forms/domains.html`): "⤴ Upload export from this device" button + hidden
file input in the backlinks_file group; `uploadExport()` POSTs FormData, updates the
`f_backlinks_dir` field + selects the file source + re-checks the file line. Verified: py_compile
clean; resolution smoke test (pin exact path → resolves; == newest match). **Needs a
Restart-Service BCC to expose the new route.**

---

> 📦 **Earlier session logs (2026-05-13 … 2026-06-16) are archived** in [bcc-state-archive.md](bcc-state-archive.md) to keep this tracker lean. Recent logs (2026-06-17 →) remain below.

## Session log — 2026-06-17 — sub-step re-rework batch · deep cognitive-science report · THE SERP VENDOR SWITCH (SerpApi→Scale SERP) · domain-harvest overhaul + background job · system-wide domain scoring decided

A long, wide session on `split/enrichment-api`. Three arcs: finish the cook sub-step rollout, switch the SERP vendor end-to-end, and rebuild the publisher/domain harvest into a real tool. Everything committed + the server kept live (now via a truly-detached `Start-Process`, not a session-bound process).

### Cook sub-steps — re-rework batch (all recipes gained sub-steps)
`scripts/rerework_cooks.py` (idempotent/resumable): re-ran every pre-v2.2 `_cook` through the engine so they all carry voice sub-steps. **28 re-reworked, 2 skipped** (gauntlet-correct: Best Chocolate Chip Cookies `reuse-referenced`, Singapore Noodles `mise-complete` — kept their old `_cook`, nothing lost). The pre-existing stepless "Traditional Greek Galaktoboureko" (0 steps) repaired. No stepless blocks remain.

### Deeper cognitive-science report (no new spend)
`docs/procedural-instruction-research-deep.md` (commit `a4b17f7`) — expanded the terse synthesis into a full explainer (Miller vs Cowan WM capacity, CLT + element interactivity, modality + its boundary, transient-information effect, segmenting, expertise reversal, TWI/checklist applied traditions) with study mechanisms + the study→design mapping. `[VERIFIED]` (our wf_77ce16a2 run) vs `[LITERATURE]` tags; bibliography flagged to-confirm; honest open-questions. Backs the marketing "cognitively-grounded instruction" pillar.

### THE SERP SWITCH — SerpApi → Scale SERP (Traject Data) — LIVE
Decision + rationale in [[project_serp_provider]]: ~55% cheaper at our future (20×) scale + **vendor consolidation** (Traject also makes Rainforest API, the planned product-data source) + trusted quality. NOT a cost-only call. Gated on fidelity, not price.
- **`serp_search()` chokepoint** (`input/pipeline/serp_search.py`): one provider-agnostic fetch, config-selected (`serp_provider` → `SERP_PROVIDER` env → serpapi). SerpApi path verified **byte-for-byte** unchanged.
- **Fidelity A/B** (`scripts/serp_ab.py`): same real queries through both → Spearman **+0.54 to +0.99**, top-10 overlap 6–9/10 (vs the rejected DataForSEO's **−0.41 / 0-overlap**). Same Google, normal scrape variance. PASS. (Banana-bread "miss" was Scale SERP returning *better* results — real recipes vs SerpApi's fruit junk — on an ambiguous unquoted query.)
- **Flipped** all 3 call sites through `serp_search` (`_serp_links`, batch `_serpapi_lookup`, calibrate script); `serp_provider=scaleserp`.
- **Two live bugs the error-logging caught instantly** (the silent-0 lesson paying off): (1) free trial **credits exhausted (HTTP 402)** mid-session → drop in a paid plan; (2) **`max_page` hard-capped at 10** (400 above) — switched `_scaleserp` to **page-loop the 1-based `page` param** (unlimited depth, early-stop, uniform with SerpApi). Pagination model: SerpApi loops `start`, Scale SERP loops `page`; both 1 credit/page.
- **Catalog-scale path = Scale SERP Batches API** (studied, not built): one batch up to 15k searches, free API + per-search billing, webhook/poll collect; slots into the job system. We have zero real-time SERP needs → async is right. NEXT: a `serp_batch` module.

### Domain / publisher harvest — overhauled into a real tool
The publisher "Top recipes" panel was discovery+raw-PA only. Rebuilt (`collections_lib`, `domains_lib`, `save_recipe_api`, `domains.html`):
- **Verbatim Google query** (`domains.serp_query`) run as-is, overrides path detection — fixes date-pathed/CMS publishers where `site:domain/recipes` finds nothing (Boston Globe detected path '2026'!). Curator owns the syntax.
- **Recipe check** — reuse the dish batch's `_is_recipe_filter` (JSON-LD or phrase score) so "is this a recipe" is decided ONE way everywhere; drops index/listicle/`/recipe-database/` pages. UI toggle (off for paywalled). Proven: addapinch 50→48 kept; aglaiakremezi 99 found→55 passed.
- **Archive pre-filter** (`_looks_like_archive`): drop `/tag/`, `/category/`, `/page/N/`, feed/admin, bare root BEFORE the fetching recipe check — saves credits/time (a bare `site:` on a WP blog is mostly these).
- **Skip flag** (`domains.harvestable=0`) — refresh refuses (no mechanical recipe access; the Boston-Globe-paywall case).
- **Clear list** (`DELETE /domains/{domain}/top` + `clear_members`).
- **Search depth (pages)** field — `domains.search_pages`, default from `system_config.serp_default_pages` (admin-editable); no 10 cap now.
- **Background job**: `POST /domains/{domain}/refresh-top` enqueues `publisher_refresh` (entity_ref `publisher:<host>`, in-flight dedup) + spawns `python -m jobs exec`, returns 202 + stream_url; `_handle_publisher_refresh_job` runs the harvest off the event loop, persists list + config; `domains.html` tails `/jobs/{id}/stream` into a live log panel and reloads the top list on done. Verified job #235 end-to-end.
- **Save-button clarification**: the job auto-persists the survivors (`replace_members`) + harvest config, so the form Save correctly stays disabled post-refresh (reverted a premature enable). Only the Skip flag is Save-only (and toggling it dirties the form normally).

### System-wide domain scoring — DECIDED (design)
Raw PA ranks publisher recipes badly (ignores DA context). Decision ([[project_domain_scoring]]): score domain recipes by a **system-wide** OU/power blend (single global PA~DA fit, not per-cohort), batch-recomputed, reusing the dish math + calibrated PA. Replaces `rank_score = pa`. Design only — `docs/domain-scoring.md` + build to come.

### Process note
Layout drift called out → [[feedback_reuse_layout_components]]: lift the existing component instead of authoring new per-page styles. Open task: domains recipe list → match the dishes top-recipes layout.

### Follow-ups (priority)
- **Build system-wide domain scoring** (single global OU/power fit, scheduled batch) — design decided.
- **Domain recipe list → dishes top-recipes layout** (reuse the component).
- **`serp_batch`** — Scale SERP Batches API (catalog-scale fan-out).
- **Sub-steps v2.1** (mise/method re-plan — design in `docs/cook-substeps-v2.1-design.md`).
- Carried: Voice P1; authenticated Playwright ingest of gated recipes; verbosity/expertise-fade; the 2 batch-skipped reworks (definite-article/etc. nits).

## Session log — 2026-06-18 — shared CSS token system · domain-harvest recipe-filter quality (collection/listicle, paywalled-trusted) · SERP-depth findings → SEMrush backlinks-file source · cook-rework cost + metric fix

Long session on `split/enrichment-api`. Themes: stand up a shared CSS style system and pull the domain + recipe forms onto it; harden the publisher harvest's recipe filtering; empirically settle "how deep is deep enough" and add a backlinks-file discovery source for big sites; harden translation + cook-rework; add rework cost reporting. All committed. **Restart gotcha (cost real confusion this session):** `bcc_restart.bat` HANGS when invoked non-interactively (abort path has `pause`, uvicorn runs foreground) → the OLD stale-code server keeps serving. I restart by killing the :8009 listener + `Start-Process` python -m uvicorn directly, and ALWAYS verify new code is live (curl the changed endpoint / listener StartTime) before trusting a screenshot. (Recorded in [[project_restart_zombie_port]].)

### Shared CSS style system (dishes = reference)
Root cause of "domains list ≠ dishes list": the forms had DIFFERENT design tokens (accent/line/font), so even a copied component rendered differently.
- **`forms/tokens.css`** — THE single token source (warm palette + Georgia serif), loaded LAST so it overrides any page-local `:root`. **`forms/components.css`** — the top-recipes list defined ONCE + a reusable `.src-*` source-chooser. Wired into dishes/domains/chapters/users/dishes_v2 + the recipe form. (Editor labels are UPPERCASE — that's the standard, not drift.)
- **Placeholders** read as examples: faded italic (`--placeholder`) + an "ex: " prefix convention in the domain `field()` helper (a prompt ending in "…" is left alone). Fixed 2 placeholders that passed `placeholder:` instead of `ph:` (never rendered).
- **`--accent-soft` = SELECTION ONLY** (user rule). Neutralized resting pink fills: top-recipe rows → `--card`; recipe-form scoring-strip/chapter-pill/hero → neutral; cohort identity-card → `--card`.

### Domain / publisher harvest — recipe-filter quality
- **Collection/listicle filter** in the shared `_is_recipe_filter` (dish batch AND domain harvest both get it): drop a title whose LEADING segment (brand suffix ignored) has the plural word **"recipes"**, OR a **number+plural listicle** ("10 Dinners", "30 Projects" — unit/time stoplist so "15 Minutes"/"4 Ingredients" survive). Title-only (never URL — real recipes live under `/recipes/`). HARD reject (overrides JSON-LD — collections embed it). **Language-aware**: English titles pre-fetch; non-English post-fetch on the TRANSLATED title via new `translate.translate_title` (literal one-shot — `translate_markdown` HALLUCINATES a recipe from a bare title). Serious Eats: 50→26→10.
- **Archive filter tightened** — utility/commerce/boilerplate segments (/about /shop /cart /privacy…) + date-only archives (/2023, /2023/04); segment-anchored so recipe slugs containing the word survive.
- **Winners-only**: `/domains/{domain}/top` returns `selected_only` (kept top-N, like dishes) not all 48. **Thumbnails**: og:image captured at harvest for the selected (`collection_members.image_url`), existing ledgers backfilled.
- **Paywalled → TRUSTED**: `paywall=1` now ALSO skips the fetch-verify (gated pages fetch as stubs → verify wrongly rejects); `check_recipe` defaults to `not paywall`; title filters + og:image still run. Milk Street 2→10. (paywall flag drives BOTH the PA-remap and trusted-harvest now — see [[project_paid_pa_calibration]].)
- **Re-refreshed all 8 publishers** with the new filters; 30daysofgreekfood re-harvested with a `site:…recipe` query (was harvesting /category/ pages); **domains form redesigned** into radio-selected source groups; master/user recipe pickers moved above the top list.

### SERP depth — experiments → findings → backlinks-file source ([[project_backlinks_source]])
- **PA vs `site:` order uncorrelated** (ρ≈0 across 4 sites): a `site:` query isn't relevance-ranked, so high-PA recipes scatter (SE top recipe at SERP #16) → a shallow harvest = near-random top-10.
- **Depth**: recall ~linear; need ~8–10 pages (~80 results) for ~9/10 of the true top-10. **`serp_default_pages` 6→10**. For big sites the `site:` ceiling (~100) is itself a non-representative sample.
- **PA SATURATES on big authoritative sites** (allrecipes all DA92/PA 60-64); **referring DOMAINS** has the range → the real discriminator there.
- **SEMrush backlinks-file source SHIPPED**: drop `input/{domain}-backlinks-pages.xlsx`, pick it on the domain form (records-to-pull), ranked by referring **Domains**; SAME downstream pipeline incl. recipe filter. Tolerates subdir exports (`{domain}_recipe-backlinks_pages.xlsx`) + their quirks — keep 3xx (http pre-HTTPS), backfill empty titles (og:title/`<title>`/slug), dedup `/id/`+`/detail.aspx` aliases. allrecipes /recipe → 20 unique canonical recipes w/ thumbnails. (`serp_batch` still NEXT.)

### Translation + cook-rework hardening + cost
- **Hallucination guard** ([[project_multilingual_extraction]]): `is_translation_plausible` now FAILS on inflation (large translation from a near-empty source) instead of skipping the check; prompt rule 10 "translate only what's present, never fabricate".
- **cook-rework metric fix** ([[project_recipe_anchor]]): `_clean_metric_face` strips stranded IMPERIAL approximations ("454 g … about 2 cups") from the metric face before the model sees it — the resolver leaves them in `_measurements.metric`, tripping `v_unit_consistency` (Beet-Cured Salmon failed; re-run passes). Source `_measurements` still wrong — resolver fix is a follow-up.
- **Rework cost summary** (`cook_costs.py`): after each rework, log per-call token cost at latest prices + cold vs warm-cache totals + volume projection, attached to `_cook.rework_cost`. Beet salmon ≈ $0.45 cold / $0.27 warm/recipe → 100k ≈ $26.5k.

### Other
- **Dish delete fix**: `DELETE /dishes/{name}` now loads sqlite-vec (the `trg_dish_vec_cleanup` trigger needs it; deletes were 500ing). Removed a URL-as-dish-name junk row. See [[project_vec_delete_triggers]].
- **Recipe editing IS a feature** (user): the editor changes any quantity/ingredient/step → a cook customizes + re-reworks (user fixed the salmon rework by hand-editing, independent of the code fix).

### Follow-ups (priority)
- **Finish the recipe-form style sweep** — cohort card / scoring strip / chapter pill / hero done; remaining sections + retire the form's local `:root` once tokens.css is confirmed.
- **Measurement-resolver fix at source** — stop writing imperial tails into `_measurements.metric` (+ re-resolve affected); the cook-rework sanitizer is the band-aid.
- **System-wide domain scoring** (decided, design — [[project_domain_scoring]]) + **`serp_batch`** (Scale SERP Batches) — carried.
- Optional: surface `rework_cost` as a form panel line (not just the stream log).
- Carried: sub-steps v2.1; Voice P1; authenticated Playwright gated ingest.

## Session log — 2026-06-18 (later) — cook-voice latency/acks + restart-vs-reset semantics · recipe-form style sweep finished

> **CAPSTONE (evening wrap — read this first; detailed sub-sections below).** All committed + pushed on `split/enrichment-api` (head `47beeff`+). `recipes.sql` re-dumped + ADAM backup done. Server running new code; cook.html/forms are static (reload to get them).
>
> **Shipped today, verified:**
> - **Cook-voice — the big arc.** VAD migrated to an **AudioWorklet** (`forms/vad-worklet.js`, off the main thread) — **iPad-confirmed: "VAD:AudioWorklet", next is snappy.** ScriptProcessor fallback kept (`USE_AUDIO_WORKLET=false` forces it). Plus: adaptive silence-hang (snappy one-word cmds; "hey chef" keeps full hang), 180ms lead-in silence (anti first-syllable clip), short wake/stop spoken acks, **arm-then-go** start, tip keyword + STT-mishear synonyms (chip/hip), **"go to step N"**, restart(steps-only)-vs-Reset-button(full-wipe), and **"in"→"inches" only as a measurement** (preposition was being read "inches"). The earlier main-thread pre-roll REGRESSED iPad (per-frame alloc in the callback) → reverted, then redone allocation-free inside the worklet.
> - **LLM gateway (`llm.py`) — P1 SHIPPED + live-verified.** One choke point; auto-journals to `bcc_token_journal` via a `contextvars` context. RULE: **sequential-sync paths → gateway; internally-threaded / SSE / save-path → `usage_log`.** Gateway: markdown_to_recipe, image/pdf_to_markdown, dish_signal, chapter_classify, translate, domain_enrich, cook_ask(sync), cook_rework/augment. **usage_log camp** (do NOT migrate): `enrich_recipe` (ThreadPoolExecutor), `voice_agent` (SSE), `identity_card` (save path). Closed the cook-rework accounting gap. Design: `docs/llm-gateway.md`.
> - **Save pipeline audited + live-verified** end-to-end (a throwaway save regenerated _identity/_match/_grade/_scoring/embedding; user recipes don't persist a vec row — `_match` is the embed artifact). Fixed an enrich 500 (UnboundLocalError) + identity-on-save attribution.
> - **UI:** recipe-form **metadata panel** (collapsible, cook-rework cost), recipe-form **dish-style sweep finished**, **admin nav burger** de-garished (soft-clay chip, not dark brown).
> - **Docs/memory:** `docs/voice-architecture-deep-dive.md` (knowledge-transfer), `docs/llm-gateway.md`; memories `project_llm_gateway`, `project_voice_pack`.
>
> **OPEN / NEXT (priority):**
> 1. **Cook-voice features:** step-level **timers** (Alexa-collision fix; `duration_minutes` exists); **equipment-first + include prep tools** (rework prompt → v2.3 + re-rework); the **"okay okay"** voice-log mystery (need the log lines).
> 2. **LLM gateway tail:** migrate **measurement** (`estimate` BYOK + `recipe_pass`) when live-testable (already journals, no gap); cleanup vestigial `usage_log` params + the duplicate `enrich/journal.py:build_usage_entry` + dead per-module `_anthropic_client`s; confirm `recipe_anchor/pipeline.py` live-vs-legacy.
> 3. **Voice-pack i18n** (design parked, `project_voice_pack`); **sub-steps v2.1** (mise/method); the perf-safe pre-roll is now IN the worklet (done).
> 4. **Carried (pre-voice):** system-wide domain scoring, `serp_batch`, measurement-resolver-at-source.

### Image retrieval — consolidate to ONE url→OG routine ([[feedback_single_path]], raised 2026-06-18 late)
thekitchn SEMrush harvest returned ~0 thumbnails. Cause: `collections_lib._fetch_og_meta` (the harvest's og:image grab) used a bespoke `requests.get`+regex that anti-bot publishers (thekitchn) BLOCK — even though those same pages PASSED the is_recipe filter, which uses the robust `fetch_with_full_fallback` (UA-rotation→Wayback). The one thumbnail the user saw was a corpus fallback for an already-ingested recipe. **FIXED in place:** `_fetch_og_meta` now delegates to the CANONICAL `fetch_with_full_fallback` + `extract_og_meta` (same path as extract + scripts/backfill_coopt_images) — no bespoke regex. (Lives in the user's UNCOMMITTED `input/pipeline/collections_lib.py` WIP — commit with the harvest work; the harvest job is out-of-process so it picks up the change; re-run thekitchn to get thumbnails.) **BROADER follow-up (user is right — custom image fetchers everywhere):** image/OG logic is scattered across `to_markdown/html_to_markdown.py` (`extract_og_meta`/`fetch_with_full_fallback` = the canonical pieces), `collections_lib`, `save_recipe_api` extract path, `scripts/backfill_coopt_images.py`, `input/pipeline/image_pipeline.py`. Add ONE `og_meta_for_url(url)->dict` (fetch+soup+extract) in html_to_markdown and route ALL callers through it; keep `coopt_image` (download/rehost) as the separate, single download step.
- **og:image now fetches DIRECT only** (`try_wayback=False`, commit 0411bd7) — a thumbnail isn't worth hammering Wayback (the thekitchn cascade: thekitchn blocks direct → Wayback → Wayback rate-limited us, WinError 10061, FETCH-FAIL cascade). Recipe-FILTER fetch still uses Wayback (that's where recall comes from); flip there too if we'd rather lean on fetch-fail salvage.
- **THUMBNAIL via "google seo stuff" (user idea, DESIGN — build+test next session):** get the thumbnail from the SERP provider instead of fetching the (blocked) page. `serp_search.py` is the single chokepoint (SerpApi default; Scale SERP staged); both expose a **Google Images engine** (SerpApi `engine=google_images`→`images_results[]`; Scale SERP `search_type=images`→`image_results[]`). Tiering: **direct og:image (free) → SERP organic `thumbnail` (free; serp_search currently DROPS it — only keeps link/title/rank; add `it.get("thumbnail")`; helps SERP source only) → `serp_image_search(title|"site:domain title")` Google-Images lookup (1 credit, fetch-free, works for blocked/backlinks sources like thekitchn)**, bounded to the selected top-N. No Wayback for thumbnails. Cost knob + image-relevance to eyeball → test before shipping.

### Cancel a running batch job from the invoking page (follow-up, raised 2026-06-19)
Need: a **Cancel** button while a long job runs (87-URL harvest, etc.). Jobs are OUT-OF-PROCESS (`subprocess.Popen(python -m jobs run)`, `jobs` table), so cancellation must be **COOPERATIVE** (a flag the job polls), not a kill. The status enum ALREADY includes `cancelled` (jobs.py:50) — schema-ready, just unbuilt. Plan: (1) `jobs.py` add `cancel_requested` col + `request_cancel(id)`/`is_cancel_requested(id)`; (2) `POST /jobs/{id}/cancel` sets the flag; (3) long handlers poll between units and abort gracefully → status `cancelled` — biggest win is `publisher_refresh`'s 87-URL loop in `build_query_batch` (check between URLs; `cook_rework` can only check between passes, can't abort an in-flight Opus call → coarser); (4) UI Cancel button in the stream/log panel (domains.html refresh, cook-rework btn) → POST cancel; stream shows cancelling→cancelled. Touches jobs infra + handlers (harvest WIP) + multiple pages → build + test as its own pass.

Short iterative session on `split/enrichment-api`, all in two front-end files (`forms/cook.html`, `forms/recipe_form_styled.html`) — static, so a browser reload picks them up (no restart). UNCOMMITTED at time of writing.

### Cook-voice loop ([[project_cook_voice]], [[project_voice_redesign]]) — latency + acknowledgements
- **Adaptive silence-hang** (the "delay between saying *next* and playback"): endpointing hang is now `SILENCE_HANG_SHORT_MS=420` for a crisp one-word command vs `SILENCE_HANG_MS=800` for longer utterances. Gate = `SHORT_CMD_MAX_VOICE_MS=360` (voiced ≤ this ⇒ short hang). The `stt` diagnostic line now prints `· Nms hang`. **Tuned to 360 specifically so "hey chef" (~450ms voiced) keeps the FULL hang** — at the first cut (600) it fast-endpointed, split "hey chef" off its question on the natural pause, and the ack's deaf window clipped the question's front (the bug the user hit). TTS premaking was already done (prefetch + media.db cache) → NOT the bottleneck; the win was the hang.
- **Lead-in silence baked into clips**: `getSpeechUrl` decodes each MP3, prepends `LEAD_SILENCE_MS=180` of silence, re-encodes to WAV (reuses `encodeWAV`), caches that. Fixes the "first syllable clipped" cold-start (output device/decoder warm-up eats silence, not speech). Best-effort, falls back to raw MP3.
- **Short spoken acks** (user: the "hey chef" reply was too long; stop was silent so "you don't know it stopped"): bare wake → `_WAKE_ACKS ["Yes?","Here!"]` (was "I'm here, what can I do for you?"); a VOICE stop → `_STOP_ACKS ["All done!","See you!","Stopping."]` via `stopHandsFree({spoken:true})` (button/tap stop stays silent — you see it reset). Both prefetched. Caveat surfaced: no AEC, so a spoken ack is deaf-while-speaking — real fix is barge-in (Voice P1).
- **Tip keyword broadened** (`_CMD_RULES` tip): now matches bare `tip`, `play the tip`, `hear/say the tip`, etc. — plays deterministically (no Chef call), no wake word needed.
- **Arm-then-"go" start** (`START_ON_GO=true`, default on): the hands-free button no longer launches into step 1 — it ARMS, gives a short "Ready when you are." cue, and waits for a spoken go-word ("go/start/begin/ready/let's cook/…", forgiving for base.en) before reading step 1; "stop/cancel" while armed aborts. `_armedWaiting` state + an early branch in `routeUtterance`; `_onSpeakEnd` keeps the "say go" hint visible. Flip `START_ON_GO=false` for the old instant-start.

### Restart vs Reset — DECIDED semantics (record this)
Two distinct "start over" actions, deliberately different scope:
- **Reset BUTTON** → `fullReset()` = clears **step boxes AND ingredient boxes** (`checked={}` + every `.irow.done`) + back to step 1. Full wipe. (Original button behavior, refactored into a named shared fn.)
- **Voice "restart"** (a watchword: "restart / start over / from the top / begin again") → `cookView.reset()` = clears **step boxes only**, lands on step 1, **keeps** the ingredient checks.
- Rationale (user's mental model): *"I was interrupted — go back to the beginning to see what I did and where to restart."* Restart should rewind the steps like continuous "back" presses while preserving the ingredients you've already gathered/checked. The destructive full wipe stays on the deliberate Reset button.

### Recipe-form dish-style sweep — FINISHED ([[feedback_reuse_layout_components]])
Local `:root` retired (tokens.css already won, loaded last). url-row input focus → neutral bg + focus ring (matches every input). uid-toggle resting fill neutralized (accent-soft = selection-only). cook-panel / "similar recipes" button + results + their JS-rendered rows → token vars instead of off-palette browns. Remaining hardcoded colors are intentional (NYT-card greys, exc-badge grade ladder, semantic danger/success/amber).

### Open design — externalize the voice LANGUAGE out of code ([[project_voice_pack]])
User flagged (correctly): command keyword regexes, wake words, and spoken phrases are now a lot of literal English **in code** — changing language should be a config/record edit, not a code change (fits [[feedback_no_data_in_code]], [[feedback_i18n_as_we_go]], [[project_portable_package]], [[project_system_config]]). Direction: a per-language **voice pack** (JSON) of DATA (wake list, per-intent synonym lists, filler/q-block lists, spoken phrases); code keeps the LOGIC (regex assembly + matching + routing) and builds the regexes from the lists at load. Caveat: true non-English voice ALSO needs a multilingual STT model (base.en is English-only) + per-language TTS persona — so the pack is necessary-not-sufficient. Design note + implementation TBD.

### Follow-ups
- **Externalize voice language to a voice-pack catalog** (above) — design note `docs/voice-pack.md` then refactor `cook.html`.
- **Local command spotter** — user chose "measure first": read an `stt · Nms · …hang` line during a real cook before deciding (Web Speech API fast-path vs in-browser whisper vs cheap server tuning).
- Shipped + pushed: recipe-form style sweep (`fc2fd59`), cook-voice loop (`bf5dcac`), "check"→next (`a8135df`).

### Recipe-form metadata panel + tip-command fix + LLM-gateway design
- **Form metadata panel** (`recipe_form_styled.html`): collapsible `<details id="metaPanel">` (COLLAPSED by default) at the end of the form — surfaces the cook-rework cost (`_cook.rework_cost`: per-model tokens, cold/warm total, volume projections) + rework prompt-version/when. A catch-all home for future processing metadata. Populated on load + after a rework; hidden on Clear. Inline `display` toggling (no working `.hidden` rule exists — `cookPanel` relies on the same non-existent class, a latent issue left alone).
- **Tip command fix** (`cook.html` `currentStepTips`): "tip" now reads tips AND checks. It excluded `kind==='check'`, so a step carrying only a doneness CHECK falsely said "no tip" — while `cook_ask.build_context` (which feeds "hey chef what's the tip") reads ALL attachments. Surfaced on Stuffed Zucchini step 2.
  - **DESIGN QUESTION raised (tips: step vs sub-step):** today tips/checks attach to the STEP (`CookStep.attachments`) + recipe (`CookMetadata.tips`); `CookSubStep` has NO attachments. So a step-level tip is shared across all sub-steps — fine on-demand, but can't do just-in-time per-sub-step surfacing. Decision: STAY step-level now; future path = optional `Attachment.applies_to_substep` index (back-compat) alongside sub-steps v2.1 + re-enabling the proactive tip offer.
- **LLM gateway — APPROVED, design done** ([[project_llm_gateway]], `docs/llm-gateway.md`): central `llm.py` all model calls route through so journaling is automatic (contextvars `llm.context`, auto-write to `bcc_token_journal`, reuse `build_usage_entry`/`write_usage_entries`). Fixes cook-rework/augment slipping the ledger (they only hit `cook_costs`→`_cook.rework_cost`). Build order: gateway core → migrate cook_rework+augment FIRST → endpoints by spend/risk → retire shims.
  - **SHIPPED (P1 steps 1–2):** `llm.py` gateway — `create()`/`stream()` mirror the Anthropic SDK (kwargs passthrough, same objects), `contextvars` `llm.context(recipe_id,user_id,api_key)` stamps every call + buffers usage, flush-on-exit writes to `bcc_token_journal` (reuses `build_usage_entry`/`write_usage_entries`); BYOK client cache; best-effort (never breaks a call). **Migrated cook_rework + cook_augment** through it; the cook-rework job handler (+ `scripts/rerework_cooks.py`) wraps the run in `llm.context` — to_thread propagates the contextvar. Verified end-to-end (nested inherit user_id=0 / override recipe_id, flush, journal rows). The accounting gap is CLOSED. `cook_costs`→`_cook.rework_cost` still produces the live summary/panel (becomes a derived view later).
  - **SHIPPED (P1 step 3):** migrated the request paths through the gateway. Mechanism for endpoints: `llm.enter()` (no-`with` ambient context; per-request contextvars isolation, propagates into `asyncio.to_thread`) at the handler top, and `_journal_usage()` now also `llm.flush()` — so every existing exit point flushes the gateway buffer with correct `recipe_id`/`user_id`, and migrated modules (which no longer populate `usage_log`) make the legacy write a no-op (no double-count). Migrated: markdown_to_recipe, image_to_markdown, pdf_to_markdown, enrich_recipe, identity_card, dish_signal, chapter_classify, translate (had NO journaling before — gap closed), domain_enrich, cook_ask (×2), voice_agent (stream). Endpoints wired with `enter()`: 4 extract + enrichment-api + enrich + domain-enrich + cook/ask + cook/ask-stream (enter set INSIDE the SSE generator to stay in its execution context). All verified: py_compile + import across every edited file; gateway journaling unit-tested (nested context, to_thread propagation, no cross-request leak).
  - **DEFERRED — measurement** (`enrich/measurement/estimate.py` BYOK + `recipe_pass.py`): intricate usage-in-dict aggregation chain (estimate returns `usage` in its result, recipe_pass appends to a `usage` list, both flow to the endpoint's `_journal_usage`). It ALREADY journals today (no gap), and rewiring it correctly needs live smoke-testing — migrate when testable. The gateway accepts `api_key=` for its BYOK key.
  - **CLEANUP remaining (non-urgent):** retire the now-vestigial `usage_log` params + the duplicate `enrich/journal.py:build_usage_entry` + dead per-module `_anthropic_client` singletons. `recipe_anchor/pipeline.py` (likely legacy) not migrated — confirm live-vs-legacy first.
  - **LIVE-VERIFIED 2026-06-18:** restarted (fresh uvicorn, new code confirmed) + smoke-tested. Sync `/cook/ask` → journal row `cook_ask`/sonnet-4-6, recipe_id+user_id correct, exactly one row (gateway `enter()`/`flush()` proven on the live Starlette path). `domain_enrich` row also confirmed correct. **SSE gotcha found + fixed:** `/cook/ask-stream` first journaled NOTHING — Starlette drives an SSE generator across SEPARATE threadpool contexts per iteration, so the `enter()` contextvar is discarded before `voice_agent`'s flush → entry lost. Fix: **reverted `voice_agent.stream_grounded` to journal via the plain `usage_log` list** (shared by reference, context-immune) + the endpoint `_journal_usage`; re-tested → `/cook/ask-stream` journals correctly. RULE: **streaming SSE paths use `usage_log`, not the contextvar gateway** (sync paths use the gateway). Recorded in `docs/llm-gateway.md`.
  - **ENRICH 500 found + fixed (user-reported "Unexpected token 'I'…not valid JSON" = a 500 the form couldn't parse):** TWO bugs. (1) the enrich endpoint resolves `recipe_id`/`user_id` AFTER the `usage_log` init, but I'd put `llm.enter()` at the init → `UnboundLocalError` → 500. (2) deeper: `enrich_recipe` fans its blocks across a `ThreadPoolExecutor` whose worker threads DON'T inherit the gateway contextvar → gateway calls mis-attributed to placeholder (`user_id=1`, `recipe_id=None`). Fix: **reverted `enrich_recipe` to `usage_log`** (same as voice_agent) + removed the enrich-endpoint `enter()`. Re-tested live: enrich → HTTP 200, `user_id=5` correct; markdown EXTRACT → gateway rows correctly attributed (recipe_id+user_id). **GENERAL RULE (now in the doc): sequential-sync → gateway; internally-threaded (ThreadPoolExecutor) OR SSE-streamed → `usage_log`.** Only `enrich_recipe` + `voice_agent` are in the usage_log camp; all other migrated modules are sequential-sync. py_compile is a syntax check only — it did NOT catch the runtime UnboundLocalError; live smoke-testing did.
  - **SAVE pipeline audited + verified (user asked "do all save-triggered functions work?"):** mapped `_save_recipe_core` (POST /recipes) — it produces `_identity` (identity_card LLM), `_match` (OpenAI embedding → `find_similar_dishes` vs `dishes_vec`), `_grade`/`_master.exceptionalism`, `_scoring`, the `embedding` BLOB + `recipes_master_vec` upsert (master only), `_measurements`, auto-enrich (master only). USER recipes do NOT persist a vec row (only master_recipes + dishes do); the user-recipe embedding is transient → `_match` is the persisted "embed" artifact. **Live save test (throwaway copy, unique URL, then deleted): HTTP 200, regenerated `_identity`/`_match`(confident)/`_grade`(C-)/`_scoring`/`embedding`(6144B) — ALL save functions work.** The oatmeal `53a608de` is fully populated (from its 06-09 save); nothing broken. **Caught + fixed an attribution regression:** `identity_card` runs on EVERY save, but the save path has no gateway context (it uses `save_usage_log`+`write_usage_entries`, and also runs threaded enrich) → identity-on-save journaled placeholder. **Reverted `identity_card` to `usage_log`** (correct on both save + extract). Re-verified: identity_card_recipe journal row now `user_id=5`. So usage_log camp = `enrich_recipe`, `voice_agent`, `identity_card`.
- **Tip mishear fix** (`cook.html` `_CMD_RULES` tip): base.en mangles the short plosive "tip" → "chip"/"hip"/"ship", so a bare "tip" kept missing (cook repeated it 20×). Now accepts those mis-hears anchored to the whole utterance (so "chocolate chips" mid-step can't trigger) — same trick as the wake word's jeff/geoff.

### Cook-voice: VAD → AudioWorklet (off the main thread) — the "do it right" iPad fix
After the pre-roll regression, migrated the VAD off the main-thread ScriptProcessor onto an **AudioWorklet** (`forms/vad-worklet.js`, processor `vad-processor`) on the dedicated audio thread — so main-thread/GC jank can't stall capture (root cause of the iPad lag/lost-nexts). **All buffers pre-allocated** (acc frame, capture buffer, pre-roll ring) → NO per-callback allocation (the exact mistake that tanked the last pre-roll). Faithful port of `onAudioFrame`: adaptive noise floor, awaiting-Q thresholds, adaptive silence-hang, MIN_VOICE drop, speaking guard, + a now-SAFE onset pre-roll. Worklet posts EVENTS only (start/end/drop/hb/barge); main thread ships config via `_vadCfg()` and notifies speaking/awaiting (`_setSpeaking` routes all `_speaking` writes; `setAwaiting` notifies). **ScriptProcessor kept as FALLBACK** (no-AudioWorklet or any failure → old working path; force with `USE_AUDIO_WORKLET=false`). So worst case = no-improvement, never regression. Diagnostics: the `session` vlog line shows `VAD:AudioWorklet` vs `VAD:ScriptProcessor`. Syntax-checked (node --check worklet + extracted cook.html inline JS); worklet served 200. **NEEDS A REAL IPAD TEST** — verify the log shows `VAD:AudioWorklet` and that next/hey-chef are responsive without lag/lost captures. Deep-dive doc VAD section updated.

### Cook-voice: pre-roll REVERTED (iPad regression) + go-to-step kept
Shipped VAD onset pre-roll (9d92647) to fix clipped onsets — but it REGRESSED iPad hands-free badly (3s+ delays on "next", lost nexts, mic-timing flicker). Cause: the idle path allocated a fresh `new Float32Array(4096)` EVERY frame to feed the pre-roll ring — continuous GC churn inside the ScriptProcessor `onaudioprocess` (high-priority audio thread). Desktop absorbed it; iPad didn't. **Reverted the pre-roll** (restored the prior capture path); kept the "go to step N" command (timing-neutral). LESSON: never allocate per-frame in the mic callback. The onset-clip fix still wanted — redo as a PERF-SAFE pre-roll: a FIXED ring of pre-allocated Float32Arrays, copy-into (set) not allocate, on a real iPad test. Open cook-voice items also: step-level timers (Alexa-collision fix; duration_minutes exists), equipment-first + include prep tools (rework prompt + re-rework), and the unexplained "okay okay" in the voice log (need the log lines).

### Admin nav burger restyle (domains "garish dark brown" fix)
User: domains UI garish — bold white on really dark brown, want it stylish like dishes. Screenshotted (headless Edge) → the eyesore was the **admin nav burger**: `library-shell.js` set an inline dark `bg:'#4a4039'` + `.nav-toggle--admin{color:#fff}` (white glyph on dark-brown), clashing with the warm cream page. Fix: removed the hardcoded JS dark fill (also satisfies [[feedback_no_data_in_code]]); restyled `.nav-toggle--admin` in BOTH `editor-shell.css` and `library-shell.css` to a **soft-clay chip** (`--accent-soft` bg + `--accent-dark` glyph, hover → solid clay) — distinct from the user burger but in-palette. Verified via re-screenshot. Cache-bust: bumped editor-shell.css / library-shell.css / library-shell.js `?v` → `20260618c` across all `forms/*.html`.

### Voice component deep-dive doc
**`docs/voice-architecture-deep-dive.md`** — formal maintenance/knowledge-transfer report for the whole voice loop (client `cook.html`, the 3 server endpoints, the `_cook` substrate + validator gauntlet + rework engine, the cognitive-science grounding with [VERIFIED]/[LITERATURE] discipline, portability + cross-domain generalization, and the catalogued moats). Built from 4 parallel code-reads; accurate at the code level (models pinned: Sonnet 4.6 Q&A, Opus 4.8 rework, faster-whisper base.en STT, gpt-4o-mini-tts/coral TTS). Start here to maintain/extend the cook voice.

---

## Session log — 2026-06-19 — SEMrush Rank table + BCC rank · domain-form ranks UI + monthly refresh job · SERP-image thumbnails for blocked sites · Wayback jitter+circuit-breaker · unblocker fetch_strategy (Oxylabs/Bright Data)

A wide session on `split/enrichment-api`, driven by two threads: a new SEMrush-traffic-rank signal on the domain master, and last night's thekitchn anti-bot/Wayback trouble. Everything committed.

### SEMrush Rank reference table + BCC rank
- **`input/pipeline/semrush_ranks.py`** — `semrush_ranks(domain, region, rank, organic_*, adwords_*, file_date, imported_at)`, PK `(domain, region)` + a traffic index + a `(region, rank)` index. `import_ranks_file` (tolerant header map, parses region+date from the filename, **delete-and-replace per region**), `get_rank` (exact host → www-stripped → **subdomain→two-part root**), `find_newest_ranks_file` (input/ ∪ ~/Downloads), `region_stats`. Multi-region by design (US now). The bulk web export is **top-N + per-region** (the file = top 10,000 US domains), so small/paywalled/foreign domains are absent → null rank (normal).
- **Imported the real file** (`ranks.semrushranks-us-20260618-…xlsx`): 10,000 rows; **132/272 of our domains matched** a US rank.
- **`domains.semrush_rank` + `semrush_rank_at`** (managed, not hand-edited) — stamped at create (`stamp_semrush_rank`) + `refresh_all_semrush_ranks` (re-stamps the whole corpus by exact-host OR two-part-root match).
- **`bcc_rank`** = the "rank the rank" — corpus-relative ordinal, **DERIVED on read** (never stored; adding/blocking a domain reshuffles it), computed over **ALLOWED domains only** (user's call — blocked giants keep their raw global rank but drop off the ladder). Surfaced in `list_domains` (whole-corpus pass) + `get_domain` (`_semrush_rank_local` count query). Decision (user): the *formula* is right; non-recipe-but-allowed domains (amazon=store w/ cookbook captures, healthline, Guardian/PBS = legit) sit on top by raw traffic and that's accepted — "leave it alone." Deleted `nytimes.com` (0 recipes).

### Domain-form ranks UI + corpus-wide endpoints + MONTHLY refresh job
- `domains.html`: **BCC rank + SEMrush # pills** on the detail, `BCC #N` in the list meta, a **"Sort: BCC rank"** option, and a **"↻ Refresh SEMrush ranks"** button (streams the job, reloads the list, shows `US · 10,000 · file 20260618`).
- `save_recipe_api.py`: **`GET /semrush-ranks`** (region stats) + **`POST /semrush-ranks/refresh`** (enqueue+spawn out-of-process, in-flight-deduped) + the **`semrush_ranks_refresh` job handler** (find newest file → import → refresh_all; manual export, no API → re-imports whatever's dropped). Verified end-to-end: POST → job #266 → `imported 10,000, 132/272 matched, success`.
- `scheduled_jobs.py`: seeded **`semrush_ranks_refresh` @ 720h (~monthly)**, enabled. Memory: [[project_domain_master]] gains a traffic-authority signal (complements DA/PA + referring domains; feeds [[project_domain_scoring]]).

### SERP-image thumbnails for blocked sites (the thekitchn `0 thumbnails` fix)
Root: a publisher harvest's og:image is **direct-only** (`try_wayback=False`), so anti-bot sites (thekitchn) get `captured 0 thumbnails for 10 selected`. Fix = **fetch-free SERP image lookup** ([[feedback_single_path]]):
- **`serp_search.serp_image_search(query)`** — Google Images via the active vendor (SerpApi `engine=google_images` / Scale SERP `search_type=images`), normalized `[{image, thumbnail, title, source_link, source_domain, rank}]`, 1 credit.
- **`collections_lib`** harvest thumbnail loop: when og:image is empty, fall back to `serp_image_search("site:{domain} {clean_title}")` rank-1 — bounded to the **selected top-N + only on a miss** (~10 credits/blocked publisher, zero otherwise). Eyeballed on thekitchn's top-10 (the `site:` query reliably returns the publisher's OWN hero from its CDN; plain-title drifts to other sites) before wiring. Log now reads `captured N thumbnails … (N via SERP image fallback)`.

### Wayback jitter + circuit-breaker (`to_markdown/html_to_markdown.py`)
The thekitchn FETCH-FAIL flood was the **recipe-verify** Wayback (not thumbnails — those were already de-Waybacked). archive.org rate-limits our ~87-URL burst → connect-timeouts / `WinError 10061`. Fixes: a **0.4–1.5s jittered delay** before each Wayback hit, and a **global circuit-breaker** — 4 consecutive connection failures opens it (skip Wayback for 120s, fast-fail), any success resets. Shared → the dish batch benefits too. Verified the breaker opens/resets. (Anti-bot reasoning settled: pauses help *rate-based* blocks like archive.org, NOT *fingerprint/challenge* blocks like thekitchn's PerimeterX — request #1 is already challenged regardless of timing.)

### `unblocker` fetch_strategy — paid web-unblocker as a per-domain last resort (SKETCH)
Conceded the earlier overreach: plain Playwright doesn't beat PerimeterX (fingerprint, not rate), and we already use a *managed unblocker* — the SERP vendor, pointed at Google. A **direct** unblocker earns its cost only for **live full-content ingest** of high-value blocked publishers.
- `html_to_markdown.py`: **`fetch_via_unblocker`** dispatches **proxy-style** (Oxylabs `unblock.oxylabs.io:60000`, Bright Data `brd.superproxy.io:33335` — `requests.get(url, proxies=…, verify=False)`, `_fetch_via_proxy_unblocker`) vs **GET-style** (ScraperAPI/ScrapingBee/Zyte, `_fetch_via_get_unblocker`). BYOK env (`UNBLOCKER_API_KEY` or `UNBLOCKER_PROXY_USER`/`PASS`), provider/host/port via `system_config` (no-data-in-code). New `unblocker=False` tier in `fetch_with_full_fallback` (`direct → unblocker → Wayback`; 404/410 terminal). No-ops without creds (verified). `unblocker` added to the domain Fetch-strategy vocab. Doc: **`docs/unblocker-fetch-strategy.md`**.
- **Remaining wire (not done):** make the harvest/`_is_recipe_filter` pass `unblocker=True` for a flagged domain — intentionally deferred until a key exists.

### Vendor research (web-verified, 2026-06-19)
- **Traject Data (Scale SERP's parent) has NO general unblocker** — only target-specific SERP + ecommerce APIs (Rainforest/Amazon, etc.). So consolidation doesn't cover arbitrary publisher fetches.
- Best-in-class for hardest anti-bot: **Bright Data Web Unlocker** (most bulletproof) / **Oxylabs Web Unblocker** (top tier, cheapest). Zyte if bundling extraction; ScraperAPI/ScrapingBee = value tier (credit multiplier hurts on hard sites).
- **Oxylabs plan rec:** per-GB billing → **free trial → Pay-As-You-Go (~$7/GB)**; skip monthly commits (our bounded use is <1 GB/mo = cents). Buy the **Web Unblocker** product, NOT the cheaper "Micro" datacenter-proxy tier. User getting Oxylabs + Bright Data keys.

### Follow-ups
- **Validate the Oxylabs trial** on a thekitchn URL, then **wire the harvest to pass `unblocker=True`** for `fetch_strategy='unblocker'` domains (+ optional per-run cost cap, `system_config` seed for `unblocker_provider`).
- **`recipes.sql` refresh** — DB gained `semrush_ranks` + `domains.semrush_rank/_at` + data; run `bcc_backup.bat` once job #269 finishes (left out of this commit to avoid a mid-harvest dump).
- Tidy the `.env` line-15 parse warning (dotenv).
- Still queued (earlier): SEMrush **Keyword Magic** dish source (new parser) + the unified due/new human-workflow worklist; system-wide domain scoring; `serp_batch`.

### (same session, later) — unblocker ACTIVATED end-to-end + cooperative job-cancel SHIPPED
- **Oxylabs Web Unblocker validated LIVE on thekitchn**: `fetch_via_unblocker` returned 200 + recipe JSON-LD + **no press-and-hold** — it beats PerimeterX. Creds in `.env` (`UNBLOCKER_PROVIDER=oxylabs` + `UNBLOCKER_PROXY_USER/PASS`). Fixed a malformed `.env` line 15 (a stray curl snippet `OXYLABS_ID:-U '…'`) that tripped `python-dotenv`.
- **Wired the deferred tier through the harvest**: `fetch_with_full_fallback(unblocker=)` ← `_fetch_for_filter` ← `_is_recipe_filter(unblocker=)` ← `harvest_publisher_top(unblocker=)` ← the `publisher_refresh` job (`p["unblocker"]`) ← the `/domains/{d}/refresh-top` endpoint (`row.fetch_strategy == 'unblocker'`). **thekitchn flagged `fetch_strategy='unblocker'`** → its harvest now fetches LIVE (direct→unblocker→Wayback; the live unblocker fires before Wayback, so no Wayback hammering). Out-of-process → live without a server restart for the harvest path.
- **Cooperative job cancel SHIPPED** (the "stuck job" pain — though job #269 had actually *succeeded* in 5.5 min, the browser stream just hung): jobs run out-of-process so cancel is a **flag the handler polls**, not a kill. `jobs.py`: `cancel_requested` column (migration) + `request_cancel`/`is_cancel_requested` + `JobCancelled` + `_run_one_job` catches it → status **`cancelled`**. **`POST /jobs/{id}/cancel`** sets the flag (409 if not live). The harvest polls `should_cancel()` (cross-process WAL read) **between candidates** (`_is_recipe_filter`) and **between Moz scores** → aborts gracefully. `domains.html`: a **✋ Cancel** button on the refresh panel (shows during the stream, POSTs cancel, relabels "Cancelled."). Verified: cancel endpoint 409 on a done job; `cancel_requested` migrated on restart. Server restarted onto the new code. Generalizes to dish-refresh/cook-rework later (poll between units).
- **SERP-image thumbnails confirmed working** on job #269 (`captured 10 thumbnails for 10 selected (10 via SERP image fallback)`, all 10 persisted as `collection_members.image_url` → served as `preview_image`). A first diagnostic checked the wrong dict key (`image_url` vs `preview_image`) — no bug.
- **Follow-ups:** re-run thekitchn to see the unblocker land end-to-end (live, fast, cancellable); extend the Cancel button to the dish-refresh + cook-rework panels; optional per-run unblocker cost cap + `system_config` seed for `unblocker_provider`; `recipes.sql` refresh still pending (DB has the new `semrush_ranks` + `jobs.cancel_requested`).

### (same session, final) — render-off perf, reattach hardening, reset-on-import bug fix, cancel verified LIVE
- **Unblocker render OFF for verify** (commit `08508dc`): `x-oxylabs-render` spun a real browser per page (~17–30s, 1.5MB). The verify only needs JSON-LD/phrase text, which is in the static HTML → `fetch_with_full_fallback` calls `fetch_via_unblocker(render=False)`: **~5s vs ~17–30s (~3–7× faster) and ~3× cheaper** (464KB vs 1.5MB), still beats the challenge + has the Recipe JSON-LD. **Proven live: thekitchn harvest (#271) ran ~4.5 min, 31 passed, 10 selected, 10/10 SERP thumbnails — all live Oxylabs fetches, zero Wayback.**
- **Reattach hardening** (`domains.html` `streamRefresh`): reattaching to an already-finished job's SSE replays no `done` event → the display hung forever ("never cleared to post"). Added a `resolveFromJob()` safety net — reads `GET /jobs/{id}` on attach AND on a dropped stream; if terminal, renders the result (success/cancelled/error) instead of hanging. Shared `renderResult()` for SSE-done + safety-net.
- **Reset-on-import bug FOUND + FIXED** (the real cause of #270/#272 dying mid-run as `error: interrupted by uvicorn restart`): `reset_interrupted_jobs` lives in `init_db`, which runs on EVERY `save_recipe_api` import — and `jobs exec`/any diagnostic import re-ran it, **wiping a concurrently-running job's status to error**. Fix: `jobs/__main__.py` sets `BCC_SKIP_JOB_RESET=1` before importing the app; `init_db` skips the reset when set. Only the uvicorn server resets on its own startup now; workers don't. (Also why my own diagnostic scripts kept killing live harvests.)
- **Cancel VERIFIED live**: job #274 stayed `running` (no spurious reset) → `POST /jobs/274/cancel` → polled `running`→`cancelled`; the harvest aborted cleanly after candidate 4 (`=== Job #274 CANCELLED ===`). End-to-end: flag → cross-process WAL poll → graceful abort → status `cancelled`.
- Committed `08508dc` (render-off); this batch (reattach + reset-fix) committing now. Server NOT restarted (worker picks up the reset-fix as a fresh process; cancel endpoint/handler already live from the earlier restart).

---

## Session log — 2026-06-20 — image upload downscale (ONE canonical spec for every image) + honest weak-network errors; diagnosed the spanakopita extract failures as upload-not-extractor

> **CAPSTONE.** Reported bug: at a relative's house, on **weak network**, tried 3× to extract a recipe (spanakopita) from an iPhone photo — each ground for 3-4 min then errored. Diagnosis from the logs was unambiguous and **exonerates the extractor**: there is **zero server trace** for spanakopita (the uploads died on the weak link before ever reaching the server). The only image extract that got through that evening was a *different* recipe ("Banitsa", a cheese pie) at 19:27 — and it extracted server-side in **21 seconds**. So: the ~2 MB raw photo upload over a weak connection was the bottleneck; the client also **misreported** the failure ("Image extraction failed" for what was really a dropped upload) and had **no timeout** (hence the multi-minute silent grind). Also spotted (separate, now cleared): a 15:07 `MemoryError` in `list_recipes` from **multiple stale uvicorns** running at once.
>
> **Shipped (static `.html`/`.js` — no Python changed; a reload picks them up). Server restarted clean (single process, PID 7692). UNCOMMITTED → committing this batch.**

### ONE canonical downscale for ANY image ([[feedback_single_path]])
User's call: *every* image upload should be downscaled to the same spec — hero/dish photo included, not just the extract photo. So the canonical shrink lives in the reusable control:
- **`forms/image-well.js` → `downscaleForUpload(file)`** (exported on `window.ImageWell`): long edge ≤ **1568px** (the vision model's effective ceiling — larger is resized down server-side anyway) @ **JPEG 0.88**, EXIF-orientation-aware (`createImageBitmap(..., {imageOrientation:'from-image'})` with a plain-`createImageBitmap` fallback). A 12MP/2-4MB iPhone photo → ~1.5MP / **~250-450KB (~8-10× lighter)**, text still OCR-legible. **Best-effort**: returns the ORIGINAL file unchanged on a non-image / decode-fail / already-small / no-size-win — never blocks an upload. Called at the top of `handleFile` (covers the dish/hero photo via drop, paste, file picker, **photo library**, camera).
- **`forms/recipe_form_styled.html` → `extractFromImage`** now **delegates** to `window.ImageWell.downscaleForUpload` (no duplicate spec), with a fallback to the original file if the shared control isn't loaded. Every image-extract entry point (drop, camera, library, bookmarklet-staged, fallback input) funnels through `extractFromImage`, so all get it.
- Cache-bust: `image-well.js?v=20260608n → 20260620a` in `recipe_form_styled.html` (only file that loads it today; the dish/master editors are its "later" consumers).

### Honest weak-network errors + a real timeout (image AND pdf extract)
Both `extractFromImage` and `extractFromPdf` previously did `fetch → res.json()` with one catch labeled "…extraction failed" and **no timeout** → a dropped/slow upload read as an extractor failure and hung for minutes.
- **`uploadErrorMessage(err)`** classifies: `AbortError` → "upload timed out — connection too weak/dropped"; `TypeError` (Failed to fetch / Load failed) → "couldn't upload the photo — connection too weak/dropped"; `SyntaxError` (non-JSON, i.e. a timeout/proxy error page) → "server didn't return a valid result (often a timeout)". Falls through to the raw message otherwise.
- **`UPLOAD_TIMEOUT_MS = 90000` + `AbortController`** on both paths → fails fast with the honest message instead of a silent 3-4 min grind. `clearTimeout` in `finally`.
- Split the `try` so a network-level fetch reject and a JSON-parse failure each get their own honest dialog ("Couldn't upload the photo" / "Couldn't reach the extractor"); the generic outer catch stays as a safety net.

### Ops
- Server was **down** (no python, nothing on :8009) when the session started. Restarted via the canonical zombie-safe **`bcc_restart.bat`** (detached) → clean single process, startup complete, no errors. Verified the new assets are served (image-well.js 200 w/ `downscaleForUpload`; form 200 w/ the bumped `?v` + delegation + pdf signal).
- Stray dead-process logs (`smoke_uvicorn*`, `uvicorn_cookview_agent*`, `uvicorn_claude*`) are untracked noise from earlier multi-process runs — safe to delete.
- Verification: `node --check` clean on `image-well.js`; all inline `<script>` blocks of the form syntax-OK. Live browser/phone re-test of the spanakopita extract on weak signal still TODO (server + code ready).
- NOTE: commits `b369f0e`→`418a5f5` (SEMrush human-workflow harvest scheduling, "Due today" worklist, inbox scan, report-URL deep-link) landed earlier on 06-20 — **now written up** in the 2026-06-21 session log below.

### HTML markup in recipe intro/prose — stripped at the model + backfilled ([[feedback_recipe_model_first]], [[feedback_single_path]])
User flagged a master recipe (Spaghetti Vongole, `c58f3faa…`) showing literal `<p>…</p>` in the intro. Source JSON-LD `description` (and a `<i>The Tucci Table</i>` in an instruction) carried raw HTML that `recipe_model._as_str` passed through untouched (it short-circuited on `isinstance(v,str)`), and the form renders prose as TEXT (correctly — we must NOT innerHTML source markup, XSS). So the fix is server-side at the model boundary, not the client.
- **`recipe_model.py`**: new **`_strip_html(s)`** — unescape entities → drop tags → collapse whitespace → no space before punctuation. **Guarded** by `_LOOKS_HTML_RE` so it ONLY touches strings that actually contain a tag/entity (a lone `<` in "cook < 5 min" or `&` in "salt & pepper" is left alone). Routed `_as_str` through it, so EVERY field that coerces via `_as_str` is covered — `description`, `HowToStep.text`/`.name` (instructions), `name`, headline, etc. — for BOTH master and personal user recipes (one shared model). Asked-and-answered: **no client/user-side change needed**; the data is clean at rest.
- **Backfill `scripts/strip_html_prose.py`** (`--apply`): strips HTML from DISPLAY prose only (description/headline/name + instruction text/name incl. nested HowToSection + ingredients), deliberately **NOT** `_extract_trace` (it preserves the raw HTML input as provenance on purpose). Ran: scanned 1072, **cleaned 12** (master + user). Verified `c58f3faa` now serves a clean description + tag-free steps. No real char corruption found (U+FFFD count 0 — the `�` in "Stein's"/"We'll" are real curly apostrophes the terminal can't render; browser shows them fine).
- Server restarted (PID 8348) so new extracts strip at save time. `recipes.db` changed in place (not in git — run `bcc_backup.bat` to refresh the `recipes.sql` dump at session end).

### Save 500 — `Provenance` was the one submodel with NO tolerant validators ([[feedback_recipe_model_first]])
User hit (on Save of the Velouté of Summer Squash and Leek): `2 validation errors for RecipeModel — provenance.notableVariations / provenance.relatedDishes: Input should be a valid list [got str]`. The enrich/provenance LLM block emitted a bare STRING where `List[str]` is declared, and **`Provenance` had no `mode='before'` validators** (every other submodel coerces off-type values so "an off-type value never drops the whole recipe") → the whole save 500'd. Fix (`recipe_model.py`): added `_v_text` (`_as_str`) for the str fields + `_v_list` (`_as_str_list`) for `notableVariations`/`relatedDishes` → a string wraps to a single-element list. **Did NOT split** on comma (notableVariations is prose; relatedDishes has parenthetical commas — "Vichyssoise (French leek…), squash soup" would shred). Verified: string→`['…']`, list passes through, no error. Server restarted (PID 29832). **Source-side tighten (so the model stops emitting strings in the first place):** `extract/enrich_recipe.py` `PROVENANCE_BLOCK` — added per-property `description`s to the tool input_schema (notableVariations/relatedDishes spelled out as ARRAYS, one item each, never a comma-joined string; the str fields marked plain strings) + a "FIELD SHAPES" section in `_PROVENANCE_JOB` with an example. The schema already declared `array`, but with no descriptions/prompt reinforcement the model occasionally violated it; coercion stays as the safety net (PID 32008).

### Multi-recipe page → extract 500 (`list indices must be integers or slices, not str`)
User re-shot the spanakopita (upload now SUCCEEDS — phone downscaled ~2MB→659KB, that fix proven in the wild) but extraction 500'd. Cause: the cookbook page held **TWO recipes** (Spanakopita + Pilaf below it), so the vision model returned a JSON **array**; `markdown_to_recipe` assumed one recipe dict → `sanitized["inputImage"]=…` on a list threw the TypeError. **Pre-existing gap** (broccoli page had one recipe → fine), not today's model edits. Fix (`extract/markdown_to_recipe.py`): new `_first_recipe(data)` (+ `_is_recipe_typed`) collapses a bare list OR a JSON-LD `@graph` to the primary recipe — prefers the first Recipe-typed object (the page's top recipe = what the user shot), logs `NOTE: extraction returned N recipes; using '…' (the first)` when >1, returns None if nothing usable. Called right after `json.loads`, before sanitize/validate. **Verified end-to-end on the real saved image**: POST /extract-from-image → 200, name=Spanakopita, 8 ingredients / 9 steps, provenance.relatedDishes=[] (list). Server PID 38224. Future: a "this page has 2 recipes — extract the other?" UX instead of silently dropping #2.

### `sourceImage` — retain the original capture, separate from the hero (completes the long-standing TODO)
Decision (user): the photo we extract FROM should ride along on the recipe as **a separate image** from the editable hero — immutable provenance, and a reference to **validate our extraction against the original** (e.g. the spanakopita page's handwritten "use smaller pan" margin notes). Hero defaults to it; user can swap the hero later WITHOUT losing the source. **Step (a) plumbing SHIPPED + verified** ([[feedback_db_form_sync]] — all four edges):
- **Model** (`recipe_model.py`): `sourceImage: Optional[List[str]] = []` (mirrors `image`), added to the `_coerce_str_lists` validator + `STATIC_TOP_LEVEL_FIELDS` (carries across owner boundaries). Completes the top-of-file `sourceImage` TODO.
- **Extract** (`save_recipe_api.py /extract-from-image`): after extraction, persist the captured bytes via the **no-crop hero path** (`standardize_and_meta`, ≤1600px → `/generated/upload_*.jpg`), set `recipe["sourceImage"]=[url]` ALWAYS + default `recipe["image"]` to it when no hero. Best-effort (never fails the extract).
- **Form round-trip** (`recipe_form_styled.html`): `_loadedSourceImage` set on load, carried into the save payload (`sourceImage:`), reset on Clear — so the immutable original survives the form's save-rebuild (it's not a user-edited field).
- **Verified**: re-ran the real spanakopita image → `sourceImage` + hero both set to a served `/generated/upload_*.jpg`, **1176×1568 portrait, uncropped** (whole page legible); served 200; model dump+reparse round-trips; form JS clean. Server PID 9456.
- **NOT yet done — step (b)**: the editor VALIDATION VIEW (show the original capture beside our extracted version so the user can eyeball it). Also: PDF extract doesn't set sourceImage yet (photos were the use case); "extract the 2nd recipe" UX.

### END OF SESSION 2026-06-20 — carry-forward (pick up here tomorrow)
A long, productive bug-bash session, all on `split/enrichment-api`, all committed + pushed (head `d26c870`). Server live on the latest code (single clean process via `bcc_restart.bat`; stray multi-process logs deleted). The whole spanakopita-from-a-photo flow now works end-to-end.
- **Shipped today (in order):** (1) downscale EVERY image before upload (canonical `image-well.downscaleForUpload`, ≤1568px) + honest weak-network errors + 90s timeout on image & pdf extract; (2) strip source HTML from recipe prose at the model boundary + backfill (12 rows); (3) tolerant `Provenance` coercion (save 500) + tightened enrich prompt to emit arrays; (4) `blob:` paste guard; (5) multi-recipe page handling (`_first_recipe`); (6) one-bullet-per-line `sourcingNotes`; (7) `sourceImage` retain-the-original plumbing (hero defaults to it).
- **NEXT (priority):**
  1. **`sourceImage` step (b) — the validation/compare view** in the editor (read-only "Original (as captured)" panel beside the extracted fields; collapsible-panel vs split-view is the open design choice). This is what actually delivers "validate our version against the original."
  2. **PDF extract → set `sourceImage`** (render page 1 as the capture) for parity with the image path.
  3. **Multi-recipe UX** — "this page has 2 recipes — extract the other (Pilaf)?" instead of silently keeping only the first.
  4. Optional: re-shoot just the Pilaf half / capture the spanakopita margin notes as `notes`.
- **Still carried from earlier (pre-today):** ~~write up the `b369f0e`→`418a5f5` SEMrush human-workflow harvest commits~~ (done 2026-06-21); validate the Oxylabs trial + wire `unblocker=True` harvest (+ cost cap); system-wide domain scoring; `serp_batch`; SEMrush Keyword-Magic dish source.
- **Ops:** `bcc_backup.bat` run at wrap (DB had the HTML-strip cleanup + new `sourceImage` writes). `.env` line-15 dotenv warning still untidy.

### blob:/data: paste guard on the extract path (KEEP — user confirmed)
While diagnosing the above (user pasted a `blob:https://outlook.office.com/…` link — an Outlook-internal image URL that no external app/server can fetch), found the recipe-form paste handler had no `blob:`/`data:` guard: such a URL fell through Pass-2 and was fed to the **markdown extractor as literal text**. Added a guard in `recipe_form_styled.html handlePaste` → an honest dialog ("That image link won't work here… right-click the image → Copy image, then paste; or download and drop the file"). The dish-photo image-well already guarded blob: in `handleUrlString`; this closes the extract path. (Static HTML — reload picks it up.)

---

## Session log — 2026-06-21 — sorted out the SEMrush harvest thread (wrote up the carried backend arc, shipped the V1.2 UX, cleaned the tree)

Cleared the lingering SEMrush stuff in the working tree + the "not yet written up" carry-forward. Authoritative detail lives in **`docs/semrush-harvest-scheduling.md`** (§0 walkthrough + V1/V1.1/V1.2 history) — this is just the tracker pointer. All on `split/enrichment-api`.

### The semi-automated publisher-harvest loop (the human-workflow arc — `b369f0e`→`418a5f5`, written up at last)
The model is **system = dispatcher + bookkeeper, human = the free, ToS-safe SEMrush "press Export + Save" hands** (we deliberately don't automate the SEMrush click — §6). One harvest = one domain's SEMrush backlinks export through the existing `backlinks_file` pipeline (is-it-a-recipe → Moz-score → keep top-N), which stamps `last_harvested_at` on success so the domain rolls off "due."
- **Schedule fields on the domain master** (`domains_lib._derive_schedule`, stamped onto every row in `list_domains`/`get_domain`): `harvest_ttl_days` + `last_harvested_at` → derived `next_harvest_at`, `harvest_status` (`new`/`due`/`ok`/None) and `harvest_due` (bool). Worklist membership = `harvest_source=='backlinks_file'` ONLY (NOT keyed on the link — that's now universal).
- **SEMrush deep-link auto-defaults** from `system_config.semrush_indexed_pages_url_template` (`…/backlinks/pages/?q={domain}&…sort_field=domainsnum`) with `{domain}` substituted — zero hand-pasting; a per-domain `semrush_report_url` overrides.
- **Endpoints:** `GET /domains/harvest-worklist` (the canonical due/new read API) + `POST /semrush-inbox/scan` (scans Downloads — or `system_config.semrush_inbox_dir` — for `*-backlinks*pages*.xlsx`, routes each to its domain by filename prefix, moves into `input/`, spawns the harvest job). Open/Copy icons on the report-URL field.

### V1.2 UX legibility — SHIPPED today (`5e35856`)
The loop worked but was illegible: its two primary actions (the "SEMrush due" sort + **Scan inbox**) were buried in the collapsible search panel, and nothing connected the per-domain deep-link to the inbox scan as one flow. Fix (all `domains.html`, no backend change):
- **Always-visible "SEMrush harvest" sidebar strip** out of the search panel: a collapsible 3-step explainer (the in-product docs = doc §0), a live **"⏰ N due"** chip that sorts the list due-first on click (computed **client-side** from the already-loaded rows — the worklist endpoint stays as the canonical API but the UI no longer needs it), and the **⤵ Scan inbox** button + log.
- **Per-domain step hint** in the backlinks source group spelling out Open → Export → Save → Scan inbox. The ↻ Refresh-ranks tool stays in the search panel (it's monthly corpus maintenance, not the per-domain loop).
- Verified self-consistent: `isAllowed`/`harvest_due`/`harvest`-sort-key all resolve; inline scripts parse; backend supplies `harvest_due`/`harvest_status` per row via `_derive_schedule`.

### Tree cleanup
- **Deleted** two junk browser re-downloads (`input/allrecipes.com-backlinks_pages (1).xlsx`/`(2).xlsx` — duplicates of the already-tracked canonical file).
- **Tracked** `input/bostonchefs.com-backlinks_pages.xlsx` (`85e7cb1`) — the actual source for job #288, matching the existing tracked exports; was on disk but never committed.
- **Left unstaged on purpose:** `.idea/dataSources.xml` (IDE-local `training.db` datasource churn) + `recipes.sql` (incidental nightly-scheduler churn — job #290 chapter_rollups + ticks; refreshed properly at session wrap by `bcc_backup.bat`).

### Still carried (SEMrush-adjacent, not done)
- **Oxylabs `unblocker=True` harvest** validated live last session but still wants a per-run cost cap + `system_config` seed for `unblocker_provider`.
- **SEMrush Keyword-Magic dish source** (new parser); system-wide domain scoring; `serp_batch` (Scale SERP Batches).
- **Traffic/Trends ingestion** (doc §5) — the "What's Hot" surface + queue prioritization.

---

## Session log — 2026-06-21 — publisher harvest hardening: self-learning URL filter, per-domain exclude, SEMrush Top-Pages source, dish-keyword demand corpus, domains scored-cohort

A long iterative session on `split/enrichment-api`, all committed + pushed (head `~`). Drove from the SEMrush direct-import friction into a whole publisher-harvest overhaul + two new corpora. **Server live; `bostonchefs`/`thepioneerwoman`/`funwithoutfodmaps` exercised; recipes.sql refreshed at wrap.**

### SEMrush import: direct-read + configurable, no more `/input` dance
- **Read the export DIRECTLY** from a configurable folder (was: only globbed `<project>/input`; the `semrush_inbox_dir` setting was ignored by the per-domain refresh). `backlinks_search_dirs` = per-domain override → configured inbox (→ Downloads) → input/. Newest match wins; most-recent duplicate (`…(1).xlsx`) preferred.
- **Per-domain override accepts an EXACT FILE PATH** (or folder, or bare filename; quotes/`~` tolerated) — the long-asked "use THIS file" escape hatch.
- **Filename match patterns are CONFIG** (`system_config.semrush_export_patterns`, `{domain}` substituted) — a SEMrush rename is a config edit, not code. The reader still auto-detects FORMAT from COLUMNS.
- **SEMrush format auto-detection** (`_read_backlinks_file`): backlinks-pages (`Domains` col → rank by referring domains) OR **organic Top-Pages** (`…-organic.PagesV3…`, `Traffic` col → rank by traffic). **A Top-Pages `/recipe` subfolder export is the FAR better source** — clean, current, traffic-ranked recipe URLs: bostonchefs **28 recipes from 30** vs **3 from 80** via backlinks.

### Self-learning URL recipe pre-filter (NEW subsystem — [[project_url_word_filter]])
- Two-list table `url_word_class(word, kind food|stop, source seed|ai|manual)`. Filter = **food-presence is the ONLY gate; non-food words are NEVER exclusionary** (`/julia-child-beef-stew/` is safe; `stop` is just a negative cache). Retrieval = cached frozensets, O(1), model never on the hot path.
- **AI classification** (one Haiku call via the gateway) of a deduped unknown-token batch into food/not_food. **STRICT prompt** (venue/dining/category/color/season words → not_food, after a loose first cut poisoned `food` with `bistro`/`dining`). **References the existing lists** (curator corrections first) as few-shot.
- **Two learning moments:** master **sweep** (`POST /url-words/sweep`, confirmed recipes) + per-harvest **tee-up learn** (incoming batch). Corpus false-drop **2.8%→0.2%** (residual = foreign-script slugs). Demoted generic mis-classifications (food/tiki/summer/colors/seasons → stop, `manual`).
- **`GET /url-words`** (counts + recently-learned).

### Per-domain EXCLUSIONARY sections — the directory-site fix
`domains.exclude_words` (e.g. `restaurant chef news holiday event jobs`): a URL whose path SECTION matches is skipped outright (the site's taxonomy), overriding any incidental food word (`/restaurant/coppa/`). Matches whole path components (splits on `/`+`_`, NOT `-`, so `/recipe/restaurant-style-chicken/` is safe). PER-DOMAIN, never global. Checked in `_is_recipe_filter` before the food gate. bostonchefs: 80 → 66 EXCLUDE / 4 skip / **5 fetched**.

### ε-exploration capture (is-recipe ML hygiene — [[project_corpus_ml]])
The url-prefilter skips before fetch → the classifier would never see/​correct the skipped region. `system_config.url_prefilter_explore_rate` (default 0.08): a random fraction of would-be-skips are verified anyway and captured as UNBIASED labels (`training_capture.explore` column); a verified explore-keep is also recovered into the harvest.

### Fetch: plain-first, escalate to unblocker only when blocked (credit-conscious)
A flagged domain tries the FREE direct fetch first; escalates to the PAID unblocker ONLY when `_looks_blocked` (a 200 anti-bot stub) or the fetch fails — so plain-loadable pages cost zero credits. (Iterated: plain+escalate → unblocker-first → back to plain-first per user.) Pioneer-woman soft-block fix proven earlier (recipe_pass 2→40).

### Dish-keyword demand corpus (NEW — capture-now/normalize-later)
SEMrush Top-Pages carries a traffic-validated **Top Keyword** (+ traffic, traffic%, intent, answer-engines) per recipe URL — a self-classifying dish name. `dish_keywords` table + `dish_keywords.py` capture from the Top-Pages reader (upsert on normalized URL). **`forms/dish-keywords.html`** searchable/sortable browse view + `GET /dish-keywords[/list]` + `POST /dish-keywords/delete`. bostonchefs → 129 captured (milk cookies 140/22.91%). Feeds (later, deliberate): dish catalog/library (demand-ranked), per-recipe dish-identity hint, prioritization. Whole-domain exports add noise (filter at promote time).

### Domains scored-cohort panel ([[feedback_reuse_layout_components]])
Harvest stores ALL scored candidates (`selected=1` on kept top-N); `/top` filtered to winners. New **`?all=1`** returns the full cohort; a **"☰ Scored cohort"** toggle on the Domains form flips winners-only ↔ full cohort (winners ★, also-rans dimmed) — the domains analog of the dishes scored cohort. funwithoutfodmaps: 10 ↔ 86.

### Ops / follow-ups
- 529s mid-session = Anthropic "Overloaded" (transient, cleared).
- Open: dish-keyword **normalize-to-canonical-dishes** pass; **promote-to-dish** action on the browse view; url-words **curate UI** + a few junk food words (bakery/market/restaurant-names) to demote; a **"↻ Sweep URL words"** admin button + scheduled sweep; relabel still-pending items carried.

## Session log — 2026-06-22 — ledger→master auto-extract · single-master typed membership blocks · render-escalation for JS publishers · live Job Monitor · is-recipe labeler UI

> ⚠️ **Reconstructed 2026-06-23 from git** (`3454652`→`8b1cc21`). This whole day shipped without a session-log entry; the gap later misled a catch-up read (which trusted the then-latest 06-21 logs + the "ingestion is SEPARATE" docstrings) into wrongly believing the publisher harvest was discovery-only. Recording it now so the next catch-up is correct. Lesson: a feature commit with no same-day session-log entry makes the state file actively misleading ([[feedback_update_state_log]]). The misleading `collections_lib` docstrings were also corrected (now point at the job handler).

### THE BIG ONE — publisher harvest AUTO-EXTRACTS winners into master (`f306d80`)
Before this, `_handle_publisher_refresh_job` only built the ranked **LEDGER** (`collection_members` via `replace_members`); ingesting into `master_recipes` was separate/manual (the 06-17→06-21 logs + docstrings all say discovery-only). This closed the gap — after persisting the ledger, the job **extracts the selected top-N winners into `master_recipes`** (the step the dish batch had, the publisher harvest lacked). Flow (the SECOND HALF of the handler, `save_recipe_api.py:3742+` — easy to miss if you stop at `replace_members`):
- **Pool = winners + reserve** (also-rans), by rank. For each until `extracted >= keep`: `extract_recipe_from_url(url, user_id=0, force_refresh=True)` → save-gate (`_is_cacheable`) → **render-retry once** (full-browser) on a thin JS page → `_save_recipe_core` with `_master.kind="top"`, `_master.publisher=host`, `_skip_auto_enrich=True`. Publisher rows carry **no `_master.dish`** → never leak into a dish top-N.
- **Backfill:** a failed winner (roundup/thin/paywalled) is replaced by the next reserve. **Re-flag** ledger `selected=1` to match ACTUALLY-saved winners → form ★ ⟺ master.
- Fetch is domain-policy-aware (unblocker/render); paywalled publishers ingest fewer (gate fails, reserves backfill). Proven 06-23: mygreekdish 27 / dimitrasdishes 24 / thepioneerwoman 22 / allrecipes 20 in master; jobs #325–333 `extracted=N`.

### Single master + typed membership blocks (the corpus-shape decision — `1381e95` reverted → `ae77271`/`5a34707`/`1891111`/`8b1cc21`)
First tried a **two-master split** (`domain_master` table alongside `master_recipes`, `1381e95`) — **REVERTED** (`505126f`): it duplicated content rows for cross-listed URLs ("same data twice"). Replacement (`ae77271`): keep **ONE `master_recipes`**; represent membership as **typed blocks on the row** — a dish block (`_master.dish`) + a domain block (`_master.publisher`), room for a third. Cardinality ~1:1 per type → a junction table is the wrong tool; single-valued blocks stay effectively 3NF with no content dup / join-on-read. **Lifecycle = inline refcount:** to retire one owner's claim, CLEAR that block, drop the row ONLY when no other block remains (no junction, no orphan GC, no shared-URL deletion hazard). `retire_master_membership(marker, value, other_marker, remove_fields, also_match)` — publisher refresh calls it directly; `delete_master_rows_for_dish` is a thin wrapper (`also_match=('kind','top')` spares editors_choice/legacy). Unit-tested both. **Perf** (`5a34707`): indexed **VIRTUAL generated columns** `dish_key`/`publisher_key` (json_extract scans → index scans; "both blocks" = `WHERE dish_key IS NOT NULL AND publisher_key IS NOT NULL`); idempotent migration (`1891111` — `table_info` hides VIRTUAL cols, so the guard re-checks). `8b1cc21` deduped the clear-block-or-drop into the one shared fn.

### Render-escalation for JS-rendered publishers + `render_required` hint (`684928f`)
Boston Globe (and other client-side-rendered sites) inject the article via JS, so a static `render=False` verify scores only the nav shell and DROPS real recipes (coq au vin: 2199-char shell, score 0 → dropped; `render=True` scores 17). Fix: in `_is_recipe_filter`, when a page would be dropped (score < threshold AND no JSON-LD) **and looks like a thin shell** (< `render_escalate_thin_chars`, default 3500), re-fetch that ONE page with a real browser (unblocker `render=True`) and re-score. Bounded: once per URL, render-eligible entries only, and a full non-recipe article is dropped WITHOUT paying for a render (credit-saving). `domains.render_required` (+ `render_learned_at`) is a JS-rendered hint — editable in the form AND auto-learned (`mark_render_required`) the first time an escalation rescues a recipe. Both the publisher harvest and the dish batch learn it; dish batches gate escalation on it.

### Live Job Monitor page (`f0d33b2`, polished `1a92d33`)
New `forms/jobs_monitor.html` — there was a Scheduled registry + a Queued drain popup but nowhere to WATCH live runs. Lists running/queued/recent (default Active), status/type filters, 4s auto-refresh, pulsing dot + ticking duration; 📜 Log slides in a true SSE stream (`/jobs/{id}/stream`) for live jobs or the static file for finished ones; ✋ Cancel / ▶ Run / inline `error_detail`. Reads only existing endpoints (no backend change). New "Jobs/Monitor" nav item; `library-shell.js ?v` → `20260622a`. `1a92d33` reworked the log panel into a **draggable/resizable floating window** (was a docked slide-in colliding with the nav burger) with a prominent ✕ Close / Esc.

### is-recipe labeling UI legible + draining borderline queue (`3454652`) — corpus-ML hygiene ([[project_corpus_ml]])
The labeler showed the first 400 chars of raw page text = always site chrome → a curator couldn't tell if a row was a recipe. Fixed read-time (no re-harvest): `_smart_snippet()` anchors the window on the first structural recipe marker (ingredients/instructions/preheat…); `_matched_phrases()` shows the RECIPE_PHRASES actually present (green chips / red "none"). **Borderline sort** (uncertainty sampling): `ABS(recipe_score - threshold)` asc floats the coin-flips up, sinks the score-100 certainties — now the default. Plain-English drop reasons, live source link, self-draining queue (a vote that no longer matches the filter fades out). `forms/training.html` un-ignored → a mature admin page.

## Session log — 2026-06-23 — SYSTEM-WIDE domain scoring (build) · paywall = curator data (editor toggle + data-driven calibrator + σ-floor) · harvest-UI fixes · the catch-up-error post-mortem

A focused session on `split/enrichment-api`. Built the long-decided system-wide domain score, then hardened the paywall calibration into data-driven shape, then fixed two harvest-UI papercuts surfaced while validating — and corrected a confidently-wrong answer I gave about the harvest (see the post-mortem). All committed; server restarted onto the scoring endpoints; calibration + rescore run against the live DB.

### System-wide domain scoring — SHIPPED (`469efe7`) — [[project_domain_scoring]], doc `docs/domain-scoring.md`
Replaced raw-PA ranking of publisher recipes (`rank_score = pa` in `harvest_publisher_top`) with the corpus-grain generalization of the dish OU/power blend. **One global PA~DA quadratic** over the whole scored population (`dish_run_data_points` ∪ `collection_members`, deduped by url); `OU = adjPA − pred(DA)`, `power = DA + adjPA`, each percentiled against the **whole-system** distribution and blended (`POWER_BLEND_WEIGHT`). Fed paywall-remapped PA. **Engine `input/pipeline/domain_scoring.py`:** global fit (reuses `chapters._fit_da_pa`, pinned quadratic) + single-row **`domain_score_fit`** store (coeffs + 101-pt OU/power quantile SKETCHES so live harvest + batch percentile identically — a computed artifact, the corpus analog of `dishes.last_ou_fit`, NOT system_config); `score_members()`, `recompute_and_rescore()`. **Safety property (verified):** within a publisher DA is ~constant → score monotonic in PA → winner picks DON'T churn, only the number becomes cross-publisher-comparable. **Missing-signal rule (latent-bug fix):** no fit yet → raw-PA fallback for everyone (uniform scale); fit exists but member lacks PA/DA → `rank_score = NULL` (NOT raw PA — a 0–100 PA mixed into 0–1 scores would corrupt the cross-publisher leaderboard). Wired: `harvest_publisher_top` scores via the engine (raw-PA fallback when no fit); `domain_scoring` job handler + weekly schedule + `POST /domains/rescore` + `GET /collections/leaderboard` (the "best recipes anywhere" payoff) + a Rescore button on `domains.html`. First run: 5,587 pts, R² 0.71, 729 members across 21 publishers.

### Paywall = curator DATA, not code (`451c9a8`) — [[project_paid_pa_calibration]]
(1) **Data-driven calibrator:** `scripts/calibrate_paid_pa.py` now reads `WHERE paywall=1` from the domains master (no hardcoded publisher list / `paid_substr`), auto-resolves each flagged publisher's PA samples (ingested corpus → harvested `collection_members` → opt-in `--harvest-missing` SERP), excludes all flagged hosts from the free baseline. (2) **Editor toggle:** a paywall checkbox on `domains.html` (shows calibration date/n) → PATCH → `update_domain` (`paywall` already in EDITABLE_FIELDS); the flag drives BOTH the PA-remap AND trusted-harvest. (3) **σ-FLOOR (overshoot fix):** floor `pa_std` at `0.5 × free_std` so the shift-and-scale slope can't exceed 2.0; the FLOORED σ is STORED so every consumer (dish SQL scorer + the new domain_scoring engine, both recompute slope from `pa_std`) is bounded. Boston Globe's compressed σ=1.8 had made slope 3.24, reading a 7-pt PA spread as "+3σ exceptional" → BG to #1 of 249. σ-floor tempers the artifact (slope→2.0, top-page OU +18.1→+11.2) but BG stays #1 (0.992) because DA 91 + the full shift-to-free-mean is the calibration's *premise* working, not a bug — softening the SHIFT (partial relocation) is the lever, **deferred** (user: "leave it, return later"). Untick'd a bogus `cleanfoodiecravings.com` paywall flag (DA 24, σ=0, n=7; the n≥15 guard had refused it anyway).

### Harvest-UI fixes (`178a029`, `3243c73`)
- **`178a029`** — dropped the redundant "★ kept" pill from the winners-only top list (every row is a winner there; `renderDomainTop` is now mode-aware → ★ only in the scored-cohort view where it distinguishes winners from dimmed also-rans).
- **`3243c73`** — (a) the harvest auto-extracts winners into master, but the refresh done-handler (`renderResult`) only reloaded the top list, never the master/user dropdowns → freshly-ingested recipes didn't appear until reopening the form ("looked like ingestion didn't happen"). Now calls `loadDomainRecipes(domain)`. (b) Also-rans are never thumbnailed at harvest → their tile was a dead `<div>` ("the button does nothing"); made the empty tile a link to source/BCC when there's a destination.

### Post-mortem — I gave a confidently WRONG answer about the harvest
Asked why scanned recipes weren't in the master dropdown, I claimed the harvest is "discovery-only, doesn't ingest into master." **Wrong** — it auto-extracts winners (06-22 `f306d80`, above). Causes: (1) **the state file was a day behind** — the 06-22 ingestion feature had no session-log entry, so catching up gave me the pre-06-22 "discovery-only" model; (2) **partial code read + confirmation bias** — I stopped at `replace_members` (line 3739), 3 lines above the auto-extract block, and the stale `collections_lib` docstring ("ingestion is SEPARATE — not done here") confirmed the wrong model (that docstring is true for the library fn but the JOB HANDLER ingests). Remediation: back-filled the 06-22 log (above), corrected the docstrings to point at the handler, and read the full pipeline end-to-end. Standing rule reinforced: don't assert what a pipeline does from a partial read — especially when the claim contradicts the user's stated expectation.

### Ops / open
- Server restarted onto the scoring endpoints (detached `Start-Process` uvicorn). `domains.html` changes are static (reload). `recipes.db` has the new `domain_score_fit` + paywall calibrations + a full rescore — **run `bcc_backup.bat` at wrap** (recipes.sql not yet refreshed for this session). `6c6bf39` (kernel_power_check.bat) is unrelated ops tracking ([[project_host_thermal_shutdowns]]).
- **Deferred:** partial-shift calibration lever (BG #1); surface `/collections/leaderboard` as a page; per-field (chapter/cuisine) domain-score refinement; the harvest→master "ingest" UX is already automatic for winners — a manual "ingest this also-ran" action is the natural next ask.

## Session log — 2026-06-23 (later) — DB concurrency hardening (busy_timeout + connection factory + detached jobs) · score-only curation for anti-bot publishers (paths #1 & #2) · cuisine/ethnicity fields

> ⚠️ **Reconstructed 2026-06-25 from git** (`e1e2d25`→`25ba8d3`, 06-23 15:51–18:14) after a session restart lost the live history. Same-day continuation of the morning's domain-scoring session.

### DB concurrency hardening — "database is locked" during harvest
A `publisher_refresh` harvest writes a recipe every ~7s out-of-process, holding SQLite's single WAL writer slot; concurrent server writes (save a domain/recipe) waited only Python's default 5s for the lock and 500'd.
- **`e1e2d25`** — raised every `save_recipe_api` connection to `timeout=30` (WAL was already on; this was the missing `busy_timeout` half).
- **`eb5121a`** — central **`input/pipeline/db.connect()`** owns the 30s timeout in ONE place; routed the 121 save_recipe_api sites through a local `_db()` + the runtime lib writers (jobs runner/`jobs.py`, domains_lib, domain_scoring, dish_keywords, url_word_lists, extract_cache, screenshot_pipeline) through it. Standalone `scripts/*` left as-is (not concurrent). Also tried detaching job subprocesses (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`).
- **`64c8c6a`** — but `DETACHED_PROCESS` gave the spawned job NO console, so its first console child made Windows pop an **empty DOS window** → reverted to **`CREATE_NO_WINDOW`** (hidden console, inherited by children). Lesson recorded: restart-survival was NEVER from the spawn flags — it comes from `bcc_restart.bat` skipping `-m jobs` processes when it kills the listener's child tree (kept). Verified: job #340 survived a restart, no popup. (See [[project_restart_zombie_port]].)

### Score-only curation for anti-bot / expensive publishers ([[project_fetchfail_salvage]], doc `docs/score-only-curation.md`)
Stop rendering every candidate just to pick 10 on sites the unblocker is costly/blocked on. Two paths:
- **Path #1 — score-only harvest + process-selected (`faa7d5f` spec, `6ac2fc2` build).** New **`score_only`** harvest mode: force `check_recipe` off, Moz-score ALL candidates URL-only (**zero renders**), store the ranked ledger with NOTHING selected, skip auto-extract. Curator checks worthy rows → **`POST /domains/{d}/process-selected`** ingests just that set via the unblocker (per the domain's fetch policy), appends (no retire), marks each saved `selected=1`. Shared **`_extract_publisher_url_to_master`** helper factored out of the winner-extract so the two ingest paths can't drift ([[feedback_single_path]]). Per-row checkboxes + "⚙ Process selected" in the scored-cohort view. Cost = Moz + 1 render per CHECKED recipe vs ~180 render-verifies.
- **Path #2 — manual capture queue, rough prototype (`25ba8d3`).** Free alternative to #1's paid unblocker: check rows → 📋 Queue → a floating panel opens each page so the human clicks their bookmarklet (real browser beats the anti-bot, no unblocker cost); auto-advances by polling `/top`. Spec records the automation analysis: a web app can't cross-origin auto-inject, so the per-page click is irreducible in-app — full zero-click needs a userscript (built next day) or extension; local Playwright is a detectable-automation trap.
- **`b1b3b51`** — fixed the `☰ Scored cohort` toggle (was wired once in global init before any detail render → null button; moved into `renderDetail` so it re-binds every domain select).

### Cuisine (searchable) + ethnicity (`b81e131`)
Promoted the buried `cuisine_focus` hint to a first-class **"Cuisine (searchable)"** field (reused the column — it IS the publisher's cuisine, Enrich populates it; added to the text filter + list meta). New optional **`ethnicity`** column (publisher cultural origin), in EDITABLE_FIELDS + forms, not searched yet; idempotent migration. NOTE: persisting `ethnicity` on save needs a restart (live process's EDITABLE_FIELDS is in-memory) — was deferred so a running kalofagas harvest #343 wouldn't get reset.

### UI / ops
- **`2af3e79`** — dropped the alarming "no file yet … Refresh will error" status line (the how-to-get-the-file steps already live above).
- **`dc50f1f`** — session-wrap `recipes.sql` re-dump (domain_score_fit + paywall calibrations + rescore) + ADAM copy, integrity_check ok.

## Session log — 2026-06-24 — score-only ZERO-CLICK userscript (path #2 done) · the "pumpkin pie" fix (translate glossary #1 + dish-alias normalization #2) · status-messages one-record-per-category refactor · blank-thumbnail fix

> ⚠️ **Reconstructed 2026-06-25 from git** (`a765514`→`7d94ee7`, 06-24 12:59–14:34) after the restart.

### Score-only path #2 — ZERO-CLICK Tampermonkey userscript (`a765514`)
The free, hands-off path: **`forms/bcc-capture.user.js`** runs in the curator's REAL browser on each queued publisher page (beats the anti-bot for free), harvests the page JSON-LD, POSTs it to master, and self-advances with human-paced randomized delays (8–25s, or 30–60s "slow" mode) so a burst doesn't look botty. Stub/block detection: no Recipe JSON-LD (challenge/rate-trip) → back off and stop. Server: **`/domains/{d}/userscript/{start,capture,finish}`** — `start` opens a tracked `userscript_capture` JOB (live in the Job Monitor), `capture` saves one page's JSON-LD to master (`jsonld_to_recipe` → save-gate → `_save_recipe_core`, kind=top) + returns next URL + delay, `finish` finalizes. **No server fetch — content comes from the browser** (GM_xmlhttpRequest bypasses CORS/mixed-content). UI: ⚡ Run userscript + "slow" toggle. Verified routing/serving; the LIVE Tampermonkey test on kalofagas is the user's.

### The "pumpkin pie" fix — Greek savory phyllo pies were translating/grouping as sweet dessert
Two complementary fixes (the report: savory Greek pies showing up as "pumpkin pie"). Docs in `docs/dish-alias-normalization.md`.
- **#1 translate glossary (`51b63ae`)** — `intake/glossary/el.json` + `_glossary_block()` injected into BOTH `translate_markdown` and `translate_title`. Disambiguates the traps: κολοκύθι→zucchini vs κολοκύθα→pumpkin, κολοκυθόπιτα → savory zucchini phyllo pie, **NEVER** sweet "pumpkin pie". Applied in-context (notes), not find-replace. Seed file (no DB table yet — promote when it grows). Forward-looking only: protects NEW Greek-title translations; does NOT fix existing rows (those are ENGLISH-sourced, `originalTitle` empty). ([[project_multilingual_extraction]])
- **#2 dish-alias canonical normalization (`9cab175` scope, `f1189be` build)** — the real defect was the dish's members **scattered across 4 chapters** (grouping itself worked). Key dish identity off the UNAMBIGUOUS native/transliterated anchor (never loose English): `intake/dish_aliases.json` (Greek phyllo-pie family Kolokithopita/Spanakopita/Tyropita/Hortopita/Kreatopita → chapter "Sandwiches, Pizza & Savory Pastry"); `intake/dish_alias.py` diacritic-insensitive resolver; `scripts/normalize_dish_aliases.py` backfill **APPLIED** (9 outliers pinned → Kolokithopita 10/10 in one chapter); `save_recipe_api._attach_chapter` alias override at extract so new harvests don't re-scatter (needs restart). Chapter only — author "pumpkin" titles KEPT for provenance; canonical DISPLAY name is the separate open question. Seed file folds into the dish catalog later ([[project_dish_catalog_table]]).

### Status/wait messages — one record per category (`3f6cff0`, styling `7d94ee7`)
Were one DB row PER MESSAGE, edited one-at-a-time. New model **`message_categories`** (category UNIQUE, `order_mode`, `messages` TEXT, enabled) — a category is a SINGLE record with an order dropdown (top/alpha/random) + a CRLF textarea (one message per line); reuses the generic admin editor. Old per-message rows migrated (grouped + newline-joined, edits preserved; old table dormant). Read side = `admin_models.get_messages(category, order, count)` returns a READY presorted list (server owns ordering); `/status-messages/active` presorts per category; new `GET /messages?category=&order=&count=`. Admin textarea sized up (180–240 tall). Needs a restart (new model + endpoints). ([[project_friendly_status_messages]])

### Blank thumbnails (`4de57ca`)
A failed image co-opt left `_source.previewImage=''` (29 master rows; 6 argiro.gr winners); COALESCE treated `''` as real → blank `<img src>`. Wrapped each COALESCE arm in `NULLIF(...,'')` in `get_collection_top` + `/collections/leaderboard` so it falls through to the recipe's own/og:image. Re-coopting the 29 to local thumbs is a separate optional backfill.

## Session log — 2026-06-25 — homepage-grounded domain enrich (real bios) · harvest MODE selector (one intent picker)

> ⚠️ **Reconstructed from git** (`da9df91`, `c49fc3d`, 06-25 11:19–13:50) — this is the session that was live when the restart happened.

### Homepage-grounded domain enrich (`da9df91`) — [[project_domain_master]]
`domain_enrich` profiled a site from the domain NAME + Haiku's memory only, with a prompt saying "empty if you don't recognize the site" → small blogs Haiku doesn't know got an empty `story` almost every time. Now **`_homepage_snippet()`** fetches the site's homepage (title + og:site_name + meta description, honoring the domain's unblocker/render policy) and the prompt GROUNDS the bio in that content. Verified: themediterraneandish → real Mediterranean-diet bio; funwithoutfodmaps → "dietitian-created low-FODMAP recipes". Best-effort — a blocked/down homepage falls back to name-only. (Also demoted url-word strays war/wedding/sicilian food→stop in the DB so a Greek site's non-recipe blog slugs are dropped by the prefilter — [[project_url_word_filter]]; data change pending in the next `recipes.sql`.)

### Harvest MODE selector (`c49fc3d`)
The harvest controls were scattered (fetch/render up in Extraction; verify/score-only down in Harvest) → "which 8 switches?" confusion. Three named modes at the top of the harvest panel — **🟢 Auto·open site · 🔴 Auto·blocked site · ✋ Curate·score & pick** — each presets the low-level flags (`fetch_strategy`, `render`, `verify`, `score_only`). Raw controls stay editable as the source of truth; the mode just sets them, and `doRefreshTop` PATCHes `fetch_strategy`/`render_required` before the run so the job reads the right policy with no separate Save. Initial mode inferred from the saved fetch policy; Curate routes anti-bot/junk sites to score-only → cohort → process/userscript.

### Ops / open at restart
- **Pending wrap (do at next stable point):** `recipes.sql` shows uncommitted changes — re-dump + `bcc_backup.bat` + commit (captures the dish-alias chapter backfill + url-word demotions + any data since `dc50f1f`). Untracked `input/*.xlsx` are SEMrush exports (harvest inputs); `uvicorn_stderr_new.log` is a stray log.
- **Restarts needed to go live:** `ethnicity` persist-on-save, the `/messages` endpoints + `message_categories` model, dish-alias `_attach_chapter` override, and translate-glossary on the server extract endpoints (harvest jobs pick it up on next spawn). Verify new code is live before trusting a screenshot ([[project_restart_zombie_port]]).
- **Carried:** score-only path #2 LIVE Tampermonkey test (kalofagas); canonical dish DISPLAY name (the pumpkin-title open question); partial-shift paywall calibration (BG #1); `/collections/leaderboard` as a page; `serp_batch`; sub-steps v2.1; Voice P1.

## Session log — 2026-06-26 — post-restart recovery + ops · the NON-ENGLISH HARVEST rework (LLM-free recipe filtering for Greek/foreign sites) · full pipeline documented

Long session on `split/enrichment-api`. Opened with recovery (a restart lost live history), then a deep, iterative rework of how the publisher harvest decides "is this a recipe" on non-English sites — ending at a **fully LLM-free** filter for any language we have a phrase pack for. All committed + pushed.

### Recovery + ops (early)
- **State reconstruction:** the 06-23(later)/06-24/06-25 sessions had shipped without log entries; reconstructed all three from git (`2324ae0`), re-dumped `recipes.sql`, committed/pushed. ([[feedback_update_state_log]] reinforced.)
- **Server is now a Windows SERVICE:** the FastAPI app runs as the NSSM service **`BCC`** (`nssm.exe`→`python -m uvicorn :8009`). Killing the PID no-ops (SCM respawns it). Rewrote `bcc_restart.bat` to self-elevate + `Restart-Service BCC` + verify the port (`1d9bd44`). **The agent's shell is non-elevated → it CANNOT restart the service**; the user runs the bat. Memory updated ([[project_restart_zombie_port]]).

### The problem
A Greek/mixed publisher harvest paid **~40s/URL**: with `check_recipe` on and no Recipe JSON-LD, every candidate got a full fetch **+ a whole-page Haiku translate** just to run the English phrase count. Diagnosed on `meatandgrillstories.com` (job #374).

### What shipped (the arc, in order it evolved)
1. **Keyword pre-screen** (`intake/url_prescreen.py`, `fa8ac5d`) — one batched Haiku call classifies candidates recipe/not/unsure from slug + SEMrush Top Keyword BEFORE fetch; negative-only drop. Built, wired, validated… then **demoted to OFF by default** (see #5).
2. **Per-language phrase packs** (`scripts/translate_recipe_phrases.py` → `intake/recipe_phrases/<lang>.json`) — translate the English `RECIPE_PHRASES` list ONCE into a language's natural recipe phrasing (incl. abbreviations κ.σ/κ.γ/γρ. + conjugations); then score the **RAW** page text against it — **no per-page translation**. `el.json` calibrated on real pages.
3. **Bilingual, domain-driven scoring** (`validators.score_recipe_bilingual`) — scoring language = curator-set `domains.language` (normalized, `gr`→`el`), defaulting to the **instance base language** (`BCC_TARGET_LANGUAGE`) when unspecified; per-page auto-detect no longer trusted for the keep/drop call. The **base list is always scored; the page-language pack is ADDED** when different (a site mixes languages), scored **once** when the same. `domains.language` migrated to 2-letter ISO + form is now a **dropdown** + enrich normalizes.
4. **Phrase-list calibration via an audit** — fully translated each test page + LLM-audited recipe-truth and missing phrases. All rejects confirmed non-recipes; the audit exposed the machine-translation's misses: the headers **`υλικά`/`εκτέλεση`** (one pruned from the English master, one rendered as a dictionary word) + imperative-verb **grammar**. Added → real recipes 10→15-27 vs junk 2.
5. **FREE STRUCTURAL is-recipe gate** (`validators.has_recipe_structure`, `962d265`) — the keep decision is no longer a phrase COUNT (which false-kept vocabulary-rich guides — a Greek pork guide scored 17) but a **structural** test: a real recipe has BOTH an **ingredients section** AND a **method section**, matched **accent-insensitively** (Greek headers are often UPPERCASE → no accents: `ΥΛΙΚΑ`==`υλικά`), across base + page language. Section markers live in each pack's `sections` block. This is what let us **drop the LLM keyword pre-screen** from the default path (`keyword_prescreen_default=False`) → the Greek backlinks harvest now runs with **ZERO LLM calls**. `recipe_score` (the count) is still stamped for ranking/training, just not the gate.
6. **Regression caught + fixed** (`docs` step): the **dish batch is also a Google path** and calls the filter with NO domain → the domain-language refactor had defaulted every no-domain page to base `en`, which would **wrongly drop non-English dish-batch results**. Fixed: domain-context calls use the domain language; no-domain calls fall back to **per-page detection** (`_eff_lang`). Verified both modes.

### Verified
End-to-end on `meatandgrillstories.com` (8 URLs): 3 real recipes KEPT, 4 guides + 1 restaurant-review DROPPED — including the phrase=17 guide the old count would have kept — **no LLM calls**, no per-page translation. Dish-batch mode re-tested: detects `el` per page, keeps recipe / drops guide.

### Docs / decisions
- **`docs/recipe-candidate-pipeline.md`** — the whole pipeline in plain language, every flow-changing `if`, 3 entry points, and the explicit "Google harvest == SEMrush-file harvest from Stage 1 on" answer.
- `docs/keyword-prescreen.md` updated with the final LLM-free design.
- New languages: `python -m scripts.translate_recipe_phrases <lang>` mints a pack (phrases + section headers) in one offline call.

### Addendum (later 06-26)
- **`url_prefilter` REMOVED from the publisher harvest + domain form** (UI checkbox, both JS sends, endpoint/job/lib wiring, `domains` column + EDITABLE_FIELDS). The structural gate makes the keep call reliably after a now-cheap fetch, so the food-word skip was redundant + useless on foreign slugs. **The DISH BATCH keeps it** (config `url_prefilter_dish_batch`, default on — still useful on English food-word slugs); `_is_recipe_filter` keeps the param.
- **Ledger titles now translated to the base language.** Diagnosis: extracted MASTER recipes are correctly translated (English `name`, original `el` preserved) — but the harvested LEDGER (`collection_members`) shown in the form's top-recipes list stored the raw Greek og:title (`Μπουγάτσα με κρέμα`) → unreadable. Fix: (a) going-forward, `harvest_publisher_top` translates each SELECTED member's title to base via `translate_title` (bounded to top-N; only when domain lang ≠ base; image lookup still uses the native title first); (b) backfilled 50 existing non-Latin selected titles (akispetretzikis 20, argiro 20, meatandgrillstories 10) → readable English.

- **Traffic as a ranking tiebreaker (SEMrush).** Foreign sites often share a near-identical PA across all pages (PA saturates) → near-identical authority score → no useful order. Captured per-page **traffic + traffic% + file sequence** from the SEMrush Top-Pages export (`_read_backlinks_file` now returns a `{url→meta}` map) → stored on `collection_members` (+`traffic`/`traffic_pct`/`file_seq` cols) AND on the extracted recipe's `_scoring.traffic`/`trafficPct`. Selection now orders by **rank_score DESC, then traffic DESC**. Recipe editor scoring strip shows a **"Traffic / mo" + "% of site"** chip. Existing harvests backfilled from the files. SEMrush-API path for the OTHER extracts (dish batch / live) scoped in `docs/semrush-traffic-api-scoping.md` (storage/display/tiebreaker already done; just needs an `url_organic` API client behind a `traffic_provider` chokepoint). NEEDS RESTART (recipe_model + save_recipe_api).

## Session log — 2026-06-27/28 — live-harvest debugging: anti-bot images via unblocker · Milk Street paywall + client-capture path · SEMrush URL-form filename fix + Top-Pages defaults

Debugging real harvests on `split/enrichment-api` (server now the NSSM `BCC` service). Findings + fixes:

- **Oxylabs 550 = "Faulted"** (it retried internally + gave up). Confirmed transient/recovering: `fetch_with_full_fallback` does render-first then a non-render retry → "fetched LIVE". Harvest succeeds despite the scary log line. For sites it can't beat → the bookmarklet/userscript client path.
- **"No images" on anti-bot sites → FIXED.** Root cause: the image co-opt (`image_pipeline._fetch_image_bytes`) fetched the CDN image **directly** (no unblocker) → anti-bot 403 → blank `previewImage`. Added an **unblocker fallback** (`_fetch_image_via_unblocker`, render=False) so a blocked image still downloads + rehosts locally. (Separate, NOT fixed in code: **Playwright screenshots fail under the service account** — `BCC` runs as LocalSystem → chromium is under the user profile; fix = `nssm set BCC AppEnvironmentExtra PLAYWRIGHT_BROWSERS_PATH=C:\Users\john\AppData\Local\ms-playwright` + restart. USER ops.)
- **177milkstreet dropped all 35 → it's a HARD PAYWALL, not a bug.** Milk Street is Next.js RSC (`__next_f`, no `ld+json`); the embedded recipe is a **3-ingredient teaser** ("Sign up for full access"). The structural gate correctly dropped the teasers. No anonymous fetch (static/unblocker/RSC-parse) gets the full recipe. **Path = the client capture we built but never tested:** log into Milk Street → the **manual bookmarklet queue** (score-only #2) captures the full recipe in your authenticated session (cookie defeats the paywall; server fetches can't). The zero-click userscript is ld+json-only → won't fit Milk Street (RSC); a DOM/RSC-capture upgrade is the durable option. (Did NOT build the `__NEXT_DATA__` extractor — it'd ingest teasers for Milk Street; still worth it for genuinely-open Next.js sites WITH a teaser guard.)
- **SEMrush "No export found" → FIXED.** The URL-form export (`https___www.{domain}…-organic.PagesV3…`) put the domain mid-filename; the `{domain}*Pages*` glob (anchored at start) missed it. Added separator-anchored prefix patterns `*.{domain}*[Pp]ages*.xlsx` + `*_{domain}*[Pp]ages*.xlsx` (in DB `system_config.semrush_export_patterns` + code seed). Verified it matches apex/subpath/subdomain, dash+underscore, domain-form+URL-form (9/9 milkstreet variants).
- **Defaults moved to Top-Pages** (we harvest organic Top-Pages now, not backlinks-pages): `semrush_indexed_pages_url_template` → `…/analytics/organic/pages/?q={domain}&searchType=domain` (DB row + seed); domain-form hints/labels/placeholders reframed to Organic Research → Pages. `semrush_inbox_dir` already `C:\Users\john\Downloads\`. Left 2 deliberate per-domain backlinks overrides (allrecipes, themediterraneandish — big sites where referring-domains ranking is the better discriminator).
- **Config discipline confirmed:** templates/patterns are canonical in the DB `system_config` table; code constants are bootstrap SEED only ([[feedback_no_data_in_code]]). The per-process `get_setting` cache is just an in-memory copy of the DB row — refresh via restart OR an in-server admin-editor save (same-process `set_setting` invalidates its own cache → live, no restart).

- **Manual capture queue UX fixed (score-only #2).** Bug: "▶ Open next" advanced `idx` ONLY when the poll detected the save in master — which often misses (lag, or the bookmarklet saving where `/top`'s `user_id=0` join can't see it) → it re-opened the SAME record; only "Skip" advanced. Also the candidate URL wasn't shown until after opening. Redesigned (`captureQueue` in domains.html): always show the NEXT-UP URL/slug up front (decide skip-vs-capture first); advancement is now EXPLICIT — **↗ Open & capture / ✓ Saved → next / Skip ▶** all move independently of the poll (poll is a bonus auto-advance). OPEN QUESTION flagged: if the "Saved" auto-count never fires after a real bookmarklet save, the bookmarklet is saving to the USER library not master (`user_id=0`) → curated recipe isn't entering the corpus; wire the curate-bookmarklet to master if so.

### Follow-ups / open
- USER ops: `Restart-Service BCC` (picks up traffic/image/pattern/template changes + the stale config cache); set `PLAYWRIGHT_BROWSERS_PATH` for the service (screenshots); test the manual bookmarklet queue on Milk Street logged-in.
- **Curate-bookmarklet → master?** confirm the manual-queue capture lands in `master_recipes` (user_id=0), not the user library — else curation doesn't reach the corpus.
- The LLM keyword pre-screen has no form control (global flag, off).
- Stand up packs for the other languages actually harvested (es/it/fr…) as they come up.
- Carried (unchanged): score-only #2 live test; canonical dish display name; partial-shift paywall calibration; `/collections/leaderboard` page; `serp_batch`; sub-steps v2.1; Voice P1.

## Session log — 2026-06-28 — user 0 restored as the loginable Master/curator identity (saves to master + unlocks admin)

Short, focused session on `split/enrichment-api` (`fd6d64b`, pushed). User's call: go back to **logging in as user 0** to do master work — it should behave like any normal user but save to the master collection and unlock the admin tools, rather than the "log in as a staff user, then tick the Master checkbox" model.

### The key finding — the hard parts already existed
The recipe form ALREADY has a two-layer model: **identity** (`app:self_user_id`, set by the users.html login) vs **store-context** (`#user_id` hidden field, toggled by the Master checkbox → 0 → `payload.user_id=0` → `master_recipes`, gated on `edit_master`). And users.html `login(uid)` already writes BOTH `app:self_user_id` AND `sidebar:user_id`. So the only real blocker was that **user 0 couldn't be a resolved identity** — three guards rejected it. Tiny surgical change, not a rewrite. ([[project_master_cookbook]], [[project_dish_library]])

### What changed (`fd6d64b`, 18 files, +80/−32)
- **`input/pipeline/auth.py` `resolve_user`** — `uid==0` returns a SYNTHETIC `owner` user `Master (curator)` (NOT a users-table row; PK is AUTOINCREMENT from 1 and bootstrap skips 0). `uid<0`/blank → None (anonymous). Owner grants `edit_master` + `admin_ui` + everything, so the EXISTING save gate (`payload.user_id==0 → require edit_master → master_recipes`) and admin nav both light up for the master login. **No save-endpoint change needed** — store-context decides WHERE, header identity gates PERMISSION; the existing block was already correct for this model.
- **Two client identity guards now accept an explicit 0** — `library-shell.js selfUid()` and `recipe_form_styled.html getSelfUserId()` both guarded `> 0`, so `X-Self-User-Id: 0` was never sent. Now accept `>= 0` for an EXPLICIT value; missing/blank still falls back to anonymous/1 (never 0 by accident).
- **users.html** — the pinned Master (#0) entry is loginable again: replaced the old "there's no Master account to log in as" copy with a **"Log in as Master"** button → `login(0)`; list-item meta now "saves to master · admin".
- **Hard-gated the generic `/admin/*` CRUD endpoints** (`admin_list_models`/`schema`/`list`/`create`/`update`/`delete`) on the **`admin_ui`** permission — they were completely unauthenticated. Anonymous → 403; Master/owner → 200. Per-feature endpoints (dishes/master) already gate on their own perms; this closed the generic-registry hole. Only `admin.html` (the generic editor, reached via the admin-group nav) consumes `/admin/*`.
- Cache-bust `library-shell.js?v` → `20260628a` across all 15 forms.

### Verified LIVE (after the user's `Restart-Service BCC`)
- `auth.py` unit: `resolve_user('0')` → owner w/ edit_master+admin_ui; `-1`/blank → None.
- curl: `/auth/me` header 0 → owner+perms; anonymous → anonymous; `/admin/models` anonymous **403**, header 0 **200**.
- **Browser end-to-end** (drove Chrome): users.html → Master → Log in as Master → recipe form loads as **Master (curator)**, `self_user_id=0`, `#user_id=0`, Master checkbox checked, picker greyed, admin burger appears. **Throwaway save** through the real endpoint (patched header): HTTP 200, **found_in_master: true**, **leaked_into_user5: false**, then DELETE 200 (no junk left; recipes.db unchanged → no backup needed).

### Decision noted
Admin gate is on the `admin_ui` PERMISSION (which user 0/owner holds), not a literal `user_id==0` check — keeps it consistent with how every other admin endpoint already gates (on perms), and since user 0 is the only owner it's effectively user-0-only today. Tighten to strict `user_id==0` if ever wanted (one helper).

### Manual capture queue → master (`04b481f`) — the carried curate open item, CLOSED
Traced all 3 capture paths (Explore agent): **userscript** (`/userscript/capture` → server-side `user_id=0`) and **reject-link bookmarklet** (`#_bcc_dish=` → `bcc_hints` forces 0) already land in master; the **manual capture queue** (score-only #2) did NOT — it opened each publisher page with no hint, so the bookmarklet → `/stage-markdown` → recipe-form save fell back to the form's default store-context (`#user_id=1` = personal library). Curation silently missed the corpus.
- Fix in `domains.html captureQueue` (no bookmarklet reinstall, no server change): (1) **require `edit_master`** (checks `/auth/me`) — else a "Log in as Master" block renders instead of opening pages, so a master write can't silently 403 into nowhere; (2) **force the store to Master** (`localStorage sidebar:user_id=0` — the same-origin key the recipe form restores on load) so the staged save routes to `master_recipes`; (3) overlay now reads "→ saves to Master". The server's `edit_master` gate on `user_id==0` still authorizes every write (the store-context just routes; the auth gate protects). Works for ANY edit_master actor (Master login OR staff editor/author), not just user 0.
- **Verified live as Master:** `/auth/me` → edit_master; store flipped `5`→`0`; the queue overlay (not the warning) rendered; a queue-style capture (`POST /recipes user_id=0`) landed in `master_recipes`, did NOT leak into user 5, then deleted (no junk; recipes.db unchanged).
- The cohort's **`ingested` flag flips on a url-join** (collections_lib LEFT JOINs master by url, not by `_master.publisher`) → the queue's auto-advance poll now works too.
- **Publisher attribution threaded through (`169035f`) — DONE, parity reached.** Rather than a per-capture hint (would force a bookmarklet reinstall), did it server-side in `_save_recipe_core`: when a MASTER save (user_id=0) has a URL that's already a `collection_members` publisher member, **stamp `_master={kind:'top', publisher:<host>, refreshed_at, batch_source:'manual-capture'}`** (FILLS an absent publisher only — never clobbers a dish/kind from the reject-rescue path; typed-membership model allows both blocks) and **re-flag that ledger row `selected=1`** after the insert succeeds. Matching is by `url_normalized` (same normalizer the harvest stores), so the manual-queue capture — whose URL IS the checked cohort row — self-attributes; works for ANY master-save path, not just the queue. **Verified live (post-restart):** throwaway publisher cohort row + master save → `_master.publisher` stamped, `kind='top'`, `batch_source='manual-capture'`, ledger `selected` flipped 0→1; cleaned up (delete master via the server endpoint so the vec0 cleanup trigger loads — a plain sqlite DELETE hits `no such module: vec0`, see [[project_vec_delete_triggers]]). recipes.db unchanged.

### Domain deletion: NO cascade → built an opt-in cascade (+ host-sweep) + removed fnl-guide.com + 2 new sort fields
User asked to delete two non-recipe domains (turned out to be ONE site — fnl-guide.com; "FNL" = Food 'N' Leisure) and whether deleting a domain cascades to its recipes/vectors. **It did NOT** — `DELETE /domains/{domain}` ran only `DELETE FROM domains` (no FKs; publishers link by host-string / `_master.publisher` JSON), leaving orphaned `master_recipes`, `collection_members`, and `recipes_master_vec` entries.
- **fnl-guide.com fully removed** via the canonical typed-membership path (`retire_master_membership(marker='publisher')` + `clear_members` + `delete_domain`, sqlite-vec loaded so `trg_master_vec_cleanup` fires): 20 harvest-winner master rows + their vectors + 48 cohort rows + the domain row. Verified 0 everywhere + 0 orphaned vec rows + integrity ok.
- **Opt-in cascade SHIPPED (`a578b6c`):** `DELETE /domains/{domain}?cascade=1` removes the publisher's master recipes (vec-safe, KEEPING dish-cross-listed rows — typed-membership) + clears the cohort; **default `cascade=0` keeps recipes untouched** (the safe historical behavior). Now gated on `delete_master`. `domains.html doDelete` shows exact recipe+cohort counts, makes cascade a deliberate SECOND confirm (Cancel = keep recipes), + a THIRD final-check for ≥25 recipes — can't mass-delete by a stray click. Verified live: cascade=1 → all gone; cascade=0 → recipe SURVIVES.
- **Host-sweep gap fix (`a32…`, committed):** the cascade keyed on `_master.publisher==host`, so **legacy rows ingested before publisher-attribution (no publisher block) were missed** — exactly what left 2 fnl orphans ("Oven-baked fish", "Grilled Soutzouakia") after the first removal. Added a second sweep: delete master rows whose **URL host == domain** (canonical compare, not substring), still **SPARING any dish-tagged row**, scoped to that host, vec-clean. Response reports `master_by_publisher` vs `master_by_host_orphan`. **Verified live (4-way throwaway):** pub-only→deleted, publisher-less host-orphan→deleted, no-pub+dish→KEPT, pub+dish→KEPT (publisher block cleared); cohort+domain gone.
- **Two new domain-list sort fields (`…`):** "newest added" (`created_at` desc) + "recently modified" (`updated_at` desc) — already in `list_domains` SELECT *; verified live (rendered order matches, all 290 domains carry both timestamps).
- `recipes.sql` re-dumped twice (after fnl removal + after the orphan cleanup); ADAM copy + integrity ok.

## Session log — 2026-06-29 — domain-form persistence gaps (harvest MODE + records) · UI delete confirm-flow verified

Continuation of the domain-delete work. Fixed two "form field doesn't save" bugs the user hit, verified the delete confirm flow through the real UI, and audited the whole form for other persistence gaps.

### Delete confirm flow — verified via the real UI ([[project_domain_master]])
Drove the actual `doDelete` handler with `window.confirm` intercepted (native dialogs would freeze the browser-automation extension) — captured the exact dialog text + fed programmed answers, then checked DB outcomes on 3 throwaway domains. **All three paths correct:** Cancel-first → 1 dialog, no fetch, nothing deleted; OK→Cancel-cascade → 2 dialogs, `cascade=0`, domain gone + recipes KEPT; OK→OK→OK (≥25) → 3 dialogs incl. the ⚠ FINAL CHECK, `cascade=1`, all gone. 0 orphan vec rows.

### Harvest MODE (Curate) didn't persist — FIXED (`…`)
Picking "Curate · score & pick" + Save was lost on reload. Root cause: `score_only` was never a persisted field, and `inferHarvestMode` could only return `open`/`blocked` (from `fetch_strategy`) — Curate and Blocked BOTH use `fetch_strategy=unblocker`, so Curate re-inferred as Blocked. Fix: new `domains.score_only` column (migrates on startup) + in `EDITABLE_FIELDS`; `collectFields` sends it; `inferHarvestMode` returns `curate` when `score_only=1`. Verified live (real select→Curate→Save→reload→`curate`).

### "Records to pull from file" didn't persist — FIXED (`…`)
Surfaced by the user's follow-up audit question. `f_records` was read only at refresh time + hardcoded to default 100 on render → reset every reload, unlike its sibling knobs (`keep_top_n`/`search_pages`/`harvest_ttl_days` all persist). Fix: new `domains.harvest_records` column (INTEGER DEFAULT 100) + `EDITABLE_FIELDS` + `collectFields` sends it + the `f_records` input populates from `d.harvest_records`. Verified live (set 37 → Save → reload → 37).

### Field-persistence audit (the user's question: "any other fields not persisted?")
Cross-referenced all 27 form `f_*` inputs ↔ `collectFields` (what Save sends) ↔ `EDITABLE_FIELDS` (what persists). Result after both fixes: **every field Save sends now persists** (no silent drops). `harvestable_skip`→maps to persisted `harvestable`; `harvest_source`→persists via its radio group. Only `check_recipe` is rendered-but-not-persisted, and that's **intentional** (a per-run flag fully determined by the harvest mode now that `score_only` persists). Lesson reinforced ([[feedback_db_form_sync]]): a form field with no `EDITABLE_FIELDS` entry is silently dropped on save — audit all edges.

### Ops
- **`DELETE /domains/{domain}` cascade is LAZY-migration-gated:** the `score_only`/`harvest_records` columns are added by `ensure_domains_table`, which runs on the first `/domains` request after a restart (not at startup) — a direct DB file check right after restart shows the column missing until something hits the domains path. Verified both migrate correctly once `/domains` is hit.
- recipes.db unchanged this session (throwaway domains added+removed net-zero); last backup `1d4d42e` still current.

## Session log — 2026-06-29 (later) — hero-image audit + 3-part fix (relative-URL backfill · extraction absolutize · paste-persist hardening)

User couldn't get an image onto the oliveandmango.com potato salad (pasted → showed → save → gone), and worried the batch had WIPED images. Full audit + three fixes ([[feedback_db_form_sync]], [[feedback_single_path]], [[project_image_policy]]).

### Audit — NOT a wipe
Scanned all 1363 master + 315 user recipes by `image[0]` state. 1284 master / 267 user have URLs. Gaps: **21 unique oliveandmango recipes** stored a **site-relative** image path (`/images/uploads/…` — 404s on our host; 22 rows incl. the potato-salad user copy), **1 dead `blob:`** (John's Turkey Meatloaf — a paste that persisted a `blob:https://gemini.google.com/…` URL), ~46 empty (mostly typed/handwritten user recipes that never had one). Only oliveandmango had the relative-path defect (grouped by source host).

### Root causes
- **Batch "didn't pick up the image":** og:image is absolutized in `html_to_markdown.extract_og_meta` (urljoin), but the recipe **`image` field** (from JSON-LD/page) was NOT — oliveandmango's relative `/images/uploads/…` stored as-is. The images exist: `https://www.oliveandmango.com/images/uploads/…` → 200 (apex 301→www).
- **Paste "didn't save":** the image well shows an **optimistic data:-URL preview BEFORE** the real `/images` upload. If the upload fails (or Save is clicked mid-upload), the preview looks added but `heroImageUrl` keeps the old/empty value → Save persists THAT. The meatloaf is the variant where a `blob:` URL itself reached `heroImageUrl` and saved.

### Fixes (all shipped; backup taken before + after)
1. **Backfill (data):** absolutized the 22 oliveandmango rows' relative `image` + `_source.previewImage` → `https://www.oliveandmango.com/…` (idempotent; left absolute/`/generated/` untouched). 0 relative left. Cleaned the 1 dead meatloaf blob → `[]` (0 blob/data images remain corpus-wide).
2. **Extraction (code):** `extract/markdown_to_recipe._attach_source_metadata` now absolutizes site-relative `image`/`previewImage` against the page origin (urljoin; leaves `//`, `http(s)`, `/generated/` alone) — stops new relative paths recurring. **Needs BCC restart.**
3. **Paste hardening (code, static):** `image-well.js handleFile` now **reverts the optimistic preview + shows a real error on upload failure** (no more silent stale-save), and the recipe-form **Save guards against a `data:`/`blob:` hero** (blocks the save with "re-add the image" instead of persisting a dead URL). Cache-bust `image-well.js?v=20260629a`.

### Ops
- recipes.sql re-dumped (backfill + blob cleanup); ADAM + integrity ok. **Restart needed** for the extraction absolutize (Python); the well/form fixes are static (reload). After restart, verify a fresh oliveandmango extract stores an absolute image, and a paste round-trips.

## Session log — 2026-06-29 (later 2) — SEMrush harvest: de-staled "export not found" messages + stale-override fallback

User hit "Refresh failed: No SEMrush export found for sallysbakingaddiction.com … Expected '…-backlinks-pages.xlsx'" and feared a backlinks regression. Investigated: **no regression** — live server's default deep-link is Top-Pages (`/analytics/organic/pages/`), export patterns match `*Pages*` (both shapes), and an entered override path DOES rule over the default. The actual failure was **the export was downloaded on another machine** (the harvest job runs on the server → never saw it; no `sallysbakingaddiction` file in the server's Downloads/input). Two real cleanups fell out:

- **De-staled the 4 "export not found / expected file" strings** (`save_recipe_api` export-check `expected`, worklist `expected_file`, refresh-top 400; `collections_lib` FileNotFoundError) — they hardcoded `{domain}-backlinks-pages.xlsx` (the "still backlinks" red herring). New canonical helpers `collections_lib.expected_export_name()` + `export_not_found_message()` (one source, no drift): Top-Pages wording, and when a per-domain override is set-but-missing the message says the OVERRIDE didn't resolve, that an entered path takes precedence, and that the file must be on **THIS (server) machine** (the actual gotcha). Added the missing `collections_lib` import in the worklist fn.
- **Stale-override fallback:** `backlinks_search_dirs` skipped a file-path override (`not isdir`), so when the exact override file went stale (re-downloaded w/ new timestamp / moved) its FOLDER wasn't even searched → silent fail. Now a file-path override contributes its PARENT FOLDER to the search set, so `backlinks_file_path` finds the newest matching export in that folder. Verified 4 cases: stale exact-file → newest-in-folder; EXISTING exact-file → that file still rules (not newest); folder override → newest; quoted path → newest. (The 6 domains whose overrides point at now-missing timestamped files in Downloads will self-heal to the newest match.)

Both Python — **needs BCC restart** (user restarted for the messages; this fallback is a second change). No file-matching behavior change beyond the stale-folder add.

**Verified live (after restart):** re-downloaded sallysbakingaddiction Top-Pages export on the SERVER (`…21_13_03Z.xlsx`); the override resolved cleanly and the file harvest ran **40 discovered → 39 recipe_pass → 39 scored/stored → 20 extracted to master**. (A bare API refresh with no `source` ran as SERP — the endpoint defaulted `source` to `'serp'` instead of the domain's stored `harvest_source`; the UI sends it explicitly so it didn't bite in practice.)

**Naming + footgun cleanup (`…`):** the harvest source is internally `'backlinks_file'` but reads Top-Pages now — decided (user deferred) to **relabel UI only**, NOT rename the value (an opaque identifier; a rename = ~30-row migration + many sites for zero user benefit + risk). Fixed the last user-facing "SEMrush backlinks file" string (domains.html explainer step → "SEMrush Top-Pages export"; the radio label already said that; left the accurate "old backlinks-pages still auto-detected" note). Also fixed the endpoint to **default `source` to the domain's stored `harvest_source`** (fall back to 'serp' only when neither request nor row specifies) so a bare refresh can't silently run Google. Endpoint change needs a restart; the relabel is static.

## Session log — 2026-06-30 — harvest caching: raw-page fetch cache + reuse the finished-recipe cache (stop re-running the LLM)

User: "make sure the cache is updated with the saved full-page results … i thought the rationale was to save the LLM step, not the fetch." Two caches now, clarified + both working:
1. **`llm_extract_cache`** = the FINISHED recipe (LLM result), keyed by URL. Already existed; the harvest just wasn't USING it (it passed `force_refresh=True`, which discards a fresh hit and re-extracts every run). **FIX:** `_extract_publisher_url_to_master` (publisher harvest + process-selected) now extracts with **`force_refresh=False`** → re-harvest of an unchanged URL reuses the stored recipe = **no extract LLM**. Thin→render retry still forces fresh. Dish-refresh batch keeps its deliberate `force_refresh=True` (separate path, documented).
2. **NEW `page_cache.py` / `page_cache.db`** (git-ignored sibling, gzip HTML) = raw fetched page, opt-in via a contextvar (default OFF; non-harvest fetches unchanged). `fetch_with_full_fallback` is now a thin cache wrapper over `_fetch_with_full_fallback_uncached` (one chokepoint shared by the is-recipe filter, the JSON-LD lane, and markdown conversion). The harvest enables it around the filter + the winner-extract → **the page is fetched ONCE** (no filter+extract double-fetch / double unblocker credit) and re-harvests within the TTL skip the network. system_config: `page_cache_enabled` + `page_cache_ttl_days` (5). `CachedResponse` stand-in covers `.text/.url/.status_code/.headers/.encoding/.content/.raise_for_status`.

**Verified live (re-harvest #413, sallysbakingaddiction):** token journal = **22 × `cache_hit_markdown_to_recipe` at 0 tokens** (LLM reused, no `markdown_to_recipe` op) + 1 chapter + 1 identity one-off backfill; page_cache `hit_count` 41→122 with `created_at` unchanged (filter reused pages, zero refetch); ~55s vs ~135s cold. Both savings real.

**Residual (no token cost, known ops issue):** `_cache_row_complete` requires `_source.pageScreenshot`; only 5/34 sallys rows have one because **Playwright screenshots fail under the BCC LocalSystem service** ([[project_restart_zombie_port]] note). Screenshot-less rows read "incomplete" → each harvest re-runs the (failing) screenshot step + re-writes the row (the `created_at` churn) — wastes seconds, **no LLM tokens**. Clean fix = set `PLAYWRIGHT_BROWSERS_PATH` on the BCC service (USER ops) → rows become complete → instant fast-path, enrichment re-runs stop. Alternative (if screenshots can't be fixed): decouple LLM-reuse from the screenshot in `_cache_row_complete`.

### Screenshots fixed PORTABLY (no machine-specific service surgery) + screenshot knobs externalized
User: keep it portable (not locked to this machine), externalize config params simply + documented ([[project_portable_package]], [[feedback_no_data_in_code]], [[project_system_config]]). So instead of hand-editing the NSSM service env (`PLAYWRIGHT_BROWSERS_PATH`, machine-locked), the APP resolves the browsers dir at runtime — `screenshot_pipeline._resolve_playwright_browsers_path()`: (1) honor an existing `PLAYWRIGHT_BROWSERS_PATH` env, (2) `system_config.playwright_browsers_path` (documented per-instance knob), (3) AUTO-DETECT — scan the standard install dirs incl. **every Windows user profile** (`C:\Users\*\AppData\Local\ms-playwright`), since a LocalSystem service can't see the launching user's `%LOCALAPPDATA%`. The capture subprocess gets the resolved dir via `env=`. Verified: auto-detect returns `C:\Users\john\...\ms-playwright`, and the `C:\Users\*` glob finds it (so it resolves under the service too) — **no nssm edit needed; just restart.** Works zero-config on other hosts (or they set the one config knob).
- Also externalized the 5 capture knobs to `system_config` (documented): `screenshot_viewport_w/h`, `screenshot_capture_height`, `screenshot_settle_ms`, `screenshot_nav_timeout_ms` (defaults = the former module constants, read via `_screenshot_cfg()` at capture time). Dropped the now-redundant dim params from `_capture_raw_bytes`/`capture_screenshot` (no dead code). + `playwright_browsers_path` config key. New "Screenshots" config category.
- **VERIFIED (post-restart):** a live extract of a sallysbakingaddiction URL — running in the BCC LocalSystem service — captured `pageScreenshot: /screenshot/7dbfaa184f1943de`, the cache row flipped to `_cache_row_complete`, and the blob landed in media.db. So the auto-resolved browser path works under the service; NO nssm edit was needed. Remaining screenshot-less rows self-heal on the next full harvest → fast-path thereafter.

### Change-detected refresh — re-extract only when the SOURCE changed (the right freshness model)
The harvest's blunt reuse (force_refresh=False = reuse within the 30-day TTL regardless of source edits) raised the user's "how does a record ever refresh?" The answer: TTL/prompt/model. The better model, built: **revalidate** — reuse the cached finished recipe iff the SOURCE is unchanged, else re-extract.
- **`source_fingerprint`** column on `llm_extract_cache` (additive migration): the RAW page recipe-signal, computed via the EXISTING date-stable `compute_recipe_fingerprint(jsonld_to_recipe(page jsonld))` — name+ings+steps only, **excludes dateModified/description/aggregateRating**, so a date bump never causes a false re-extract (the user's explicit worry — unit-proven: date/desc/rating change → fp UNCHANGED; ingredient change → fp CHANGES). Computed PRE-translation, so it compares source-to-source (works for the Greek/translated sites; the cached recipe is English but the source-fp is Greek both times).
- `extract_recipe_from_url(revalidate=True)`: skips the no-fetch fast-path, parses the page JSON-LD ONCE (reused for the fingerprint AND the jsonld-direct extraction — deduped the prior double-parse), and on a within-TTL hit reuses iff `current_source_fp == cached_source_fp` else re-extracts. `_extract_cache_lookup`→4-tuple (source_fp), `_extract_cache_write` stamps it.
- **`backfill_source_fingerprint`**: on the first revalidate-reuse of a row cached before this column, stamps source_fp via a column-only UPDATE (no TTL reset, no recipe rewrite, never overwrites) so change-detection activates immediately for existing rows.
- Publisher harvest (`_extract_publisher_url_to_master`) passes `revalidate=True`; the page_cache shares the one fetch so the compare is ~free. Live single extracts + the dish batch unchanged. Cleanup: deduped a redundant local `compute_recipe_fingerprint` import + the double JSON-LD parse (parse once, reuse for fingerprint + jsonld-direct extraction).
- **VERIFIED live (sallysbakingaddiction):** phase 1 re-harvest = 28 cache-hits at 0 tokens, **0 real extract-LLM calls**, source_fp backfilled 0→21. Phase 2 corrupted one row's source_fp → that URL logged `source CHANGED → re-extracting (cached=ZZZ_SIMU != current=a43796a3)` and re-extracted (real fp restored), the other 20 logged `source unchanged → reuse (no LLM)`. Bonus: the changed JSON-LD page re-extracted via the FREE jsonld-direct lane (0 LLM); the markdown LLM only fires for non-JSON-LD/failed-jsonld pages.

### Follow-ups
- Carried (unchanged): score-only #2 live test; canonical dish display name; partial-shift paywall calibration; `/collections/leaderboard` page; `serp_batch`; sub-steps v2.1; Voice P1.
- Optional: `jsonld_to_recipe` (userscript capture path) doesn't share `_attach_source_metadata` — JSON-LD images are usually absolute, but absolutize there too for full parity if a relative one ever shows up. The ~46 empty (mostly user typed recipes) are expected, not a defect.

## Session log — 2026-07-01 — diagnosed a cook-rework gate failure (greek lemon potatoes) + follow-up to surface the failing gate
User: "cook mode for greek potatoes failed a gate — dig in + note a better error message." Dug in: job **#424** (`greek-lemon-potatoes`, master `c2823473…`, 2026-06-30) failed the **`mise-complete`** gate — `[mise-complete] step 8: 'reserved_pan_juices' is not a declared ingredient or bundle — introduced fresh in a method step`; the Opus repair pass didn't fix it → `_cook` NOT persisted → cook mode has no data. The failure IS in the job result (`{persisted:false, failures:[…]}`) and the recipe FORM's Rework button already shows it (`showReworkFailures`), but the **cook view (`/cook/{id}`)** — the likely launch point — just finds no `_cook` and says nothing about which gate. **Follow-up ([[project_cookrework_error_message]]):** surface the failing gate wherever cook mode launches (esp. the cook view), and plain-language the gate strings.

**ROOT-CAUSE FIXED (same session):** it wasn't a model miss — `v_mise_complete` built `valid_refs = ingredient_ids | bundle_ids` and OMITTED the ReservedItem (put-aside) ids, so a correctly-declared put-aside reference (`reserved_pan_juices`, an emergent reduced-pan-juices item with no starting-ingredient id) was wrongly flagged "introduced fresh." This blocked EVERY recipe reserving an emergent item (pan juices, reserved pasta water, browned meat) from passing the gauntlet. Fix: `valid_refs |= {r.id for r in cook.reserved}` (still requires the put-aside to be DECLARED; lifecycle stays enforced by appearance-order/consumed-step). **Verified:** existing gauntlet tests green + isolated test + the real recipe re-reworked (job #428) → passed FIRST pass (9 steps, 1 put-aside, `_cook` persisted) → cook mode works. Cook-rework jobs spawn fresh → live without a restart. The error-message-surfacing follow-up above still stands (general improvement).

## Session log — 2026-07-12 — equipment standardization → WS product taxonomy → LLM recipe→category matcher (commerce join)

Big arc: make each recipe's equipment linkable to sellable products. Split into (1) equipment IS always extracted + sized, (2) a real product taxonomy to classify against, (3) an LLM matcher that beats embeddings on terse tool names, (4) the product-catalog link. Also fixed the equipment-drag copy bug.

### Equipment: base extraction, not a button
- Equipment must be part of BASE extraction of every recipe (commerce needs it), NOT a flaky user-triggered "derive" button. Removed `POST /recipes/{id}/derive-equipment` + the UI button/JS entirely. Added `_ensure_equipment(recipe, path_used=)` so the fast-lane (JSON-LD) path — which was skipping equipment — derives it when empty. `Tool.size` (recipe_model.py) was silently dropped on RecipeModel round-trips → added `size: Optional[str]`. `_size_face()` coerces `_cook` measurement dicts → imperial-face string; `_recipe_equipment_from_cook` mirrors sized tools from `_cook` for reworked recipes.
- **Cache heal (the real bug):** `_ensure_equipment` was inside the cache-MISS block, so `llm_extract_cache` HITS returned no equipment (shrimp-creole symptom). Fixed BOTH extract paths to heal on cache-hit (markdown path `_extract_cache_write`; url path `_eq_healed` re-cache). Confirmed `equipment` IS in `STATIC_TOP_LEVEL_FIELDS` so it persists once written. (commits eb66760, f3582ac, fb0cf46)
- **Correction:** the premise that equipment was only extracted for cook recipes was WRONG — base extraction had already populated 2,189/2,563 non-cook recipes. `scripts/backfill_equipment.py` (cook-aware; mirrors `_cook`, never re-derives reworked recipes) filled the remaining sized gaps via mode=missing (332 rows). The mode=all re-derive was net-neutral on sizes AND degraded 3 `_cook` recipes → stopped and restored from `_cook`.

### Equipment standardization = design note + memory
Not standardized today (16,270 items / 1,815 distinct names, avg 1.89 words). Captured `docs/equipment-product-linking.md` + memory [[project_equipment_standardization]]: canonical tool dictionary + xref tables anchored to a product taxonomy, buildable offline (block→embed→cluster→classify→review).

### WS product taxonomy (their hierarchy, not ours) — 4 levels + ACDV editor
- `scripts/build_ws_taxonomy.py` scrapes Williams-Sonoma's OWN category hierarchy (via our Oxylabs anti-bot `fetch_via_unblocker`). 3 scraped levels — headline > section > subcategory — recovered by ordered anchors delimited by "All <section>" terminators; drops brand/deal/material sections (`_DROP_SECTION`/`_BRANDS`). Result: **213 clean rows** (from 339 polluted). 4th level = curator **`leaf`** (editable, folded into the embed). `ws_categories` gains section/leaf/source; `ws_path` = full path; carries product samples by URL. Embeddings via `input/pipeline/embeddings.py` (`_ws_embed_blob` = path + description + products).
- **ACDV editor** `forms/ws_taxonomy.html` on the editor-shell (like domains.html), reached via admin hamburger → "Taxonomy". 4-level grouped list, add/change/delete/view with headline/section/subcategory/leaf/description/products; "Test a term" box shows the LLM pick + embedding candidates; "Add a leaf" from a failed match. Endpoints: GET/POST/PUT/DELETE `/ws-categories`, GET `/ws-categories/match`, `/ws-categories/{id}/products`. Every mutation calls `_invalidate_tool_term_cache(conn)`. (commits c44d941, 74d72be, 344880d, 14511a4)

### LLM recipe→category matcher (primary; embeddings = fallback) — `intake/products/equipment_match.py`
- Terse tool names mis-fire on embedding cosine (oven→Dutch Ovens, fork→Can Openers, lid→Coolers, parchment→Pie Dishes). Switched to **Haiku classification**: `classify_terms_llm(terms, paths)` = ONE call with the whole taxonomy in a `cache_control`'d system block, terms numbered, model replies `N. <path or NONE>`. Each DISTINCT term cached in `tool_term_map` (cleared on taxonomy mutation); embedding is now (a) fallback when off-list/failed and (b) the candidate shortlist in the tester. `_resolve_terms` batches misses (`_BATCH=50`); `match_recipe_equipment` = one batched call per fresh recipe. All 12 test terms correct incl. consumables→NONE; added curator categories so `parchment paper`→Parchment & Baking Papers and `aluminum foil`→Foil, Wraps & Storage Bags classify in (217 categories total).
- **Cost per recipe (Haiku $1/$5 per 1M; cache write 1.25×, read 0.1×):** $0 when all a recipe's tools are already cached; ~$0.001–0.002 for a fresh recipe when the ~2,950-token taxonomy prefix is cache-warm (mid-batch); ~$0.004–0.005 for the first cold call that also writes the taxonomy cache. Output tokens (~225) dominate a warm call. Ceiling ~half a cent/recipe.

### Product catalog link — `intake/products/category_link.py`
Catalog is sparse (19 products / 5 classes) so link at the **product_class** grain: `product_class_ws_map` maps each distinct product_class → nearest ws_category by embedding; `relink_product_classes` (auto, preserves source='manual'), `set_manual_map` (curator override), `products_for_ws_category` (best by rank_score). `GET /recipes/{id}/equipment-products` now attaches `catalog_products` per matched item; `POST /product-classes/relink`. Verified Loaf Pans (1 lb)→Bread & Loaf Pans (0.67)→5 real products. Recipe form gets a "🛒 Shop the tools" button + `shopEquipment()`. (commit ee06871)

### Drag copy-bug fix
Couldn't copy text from the recipe master's equipment/ingredients/steps/notes — drag hijacked the selection. `setupDrag` now sets `li.draggable=false` by default; the ⋮⋮ handle arms `draggable=true` on mousedown, disarms on mouseup/dragend → drag ONLY from the handle, text selectable everywhere else.

### Ops
- Logs now timestamp-FIRST (`{ts}_job_{type}_{id}.log`, `{day}_cook_voice.log`) so they sort chronologically; no subfolders (user's call).
- **PENDING RESTART (UAC):** all of the above HTTP endpoints (LLM matcher, `/product-classes/relink`, `/ws-categories/*`, equipment-products with catalog_products, updated tester UI) go live only after `bcc_restart.bat` (self-elevates → click Yes on the UAC prompt; the agent shell can't). Drag fix + tester UI are static (also need a reload). Then verify end-to-end live: parchment→NONE (no shop button), loaf pan → Le Creuset/Emile Henry pans. In-process verification all passed; only the live HTTP surface is unexercised.

### Follow-ups
- Carried: score-only #2 live test; canonical dish display name; paywall calibration SELECTION half; `/collections/leaderboard`; `serp_batch`; sub-steps v2.1; Voice P1; cook-view failing-gate message ([[project_cookrework_error_message]]).
- New: grow the product catalog (only 19 products) so the commerce join has depth; consider promoting `tool_term_map` hits into a canonical tool dictionary ([[project_equipment_standardization]]).


### VERIFIED LIVE (post-restart, 2026-07-14)
Commerce join confirmed over HTTP: GET /recipes/{recipe_id}/equipment-products keys on the UUID recipe_id + user_id (master=uid 0). Easy Banana Bread (f1e799c1…): loaf pan -> Bakeware > Bread & Loaf Pans [llm] -> 6 real products (Chicago Metallic, Cuisinart); fork -> Tongs & Forks (old embed mis-fire to Can Openers gone); toothpick -> NONE. /ws-categories/match tester: parchment paper -> Parchment & Baking Papers, aluminum foil -> Foil, Wraps & Storage Bags (both curator categories), cast iron skillet -> Fry Pans & Skillets via LLM while the embedding candidate wrongly said Outdoor Cookware (0.545) — the exact mis-fire the LLM matcher fixes. 217 categories serving. The pending-restart caveat above is cleared.
## Session log — 2026-07-14 — Products ACDV editor (the catalog was invisible; now editable) + commerce join verified live

### Commerce join VERIFIED LIVE (recap, post-restart)
GET /recipes/{recipe_id}/equipment-products keys on the UUID recipe_id + user_id (master = uid 0). Easy Banana Bread: loaf pan → Bakeware > Bread & Loaf Pans [llm] → 6 real products; fork → Tongs & Forks (old embed mis-fire to Can Openers gone); toothpick → NONE. /ws-categories/match tester: parchment paper → Parchment & Baking Papers, aluminum foil → Foil, Wraps & Storage Bags, cast iron skillet → Fry Pans & Skillets via LLM while the embedding candidate wrongly said Outdoor Cookware (0.545). 217 categories serving. (commit 863d416)

### "I can't see any products" — root cause + the ACDV rebuild
The 20 products span 6 classes / 3 categories, but the old `forms/products.html` was a **read-only demo viewer** that nested everything under the `product_categories`/`product_classes` DICTIONARY tables — which only hold 1 row each — so nearly every product was orphaned and hidden. User wanted the full ACDV treatment (sidebar etc.) like domains, in the admin menu, menu alphabetized.

- **`forms/products.html` rebuilt** on the shared editor-shell (cloned from ws_taxonomy.html / domains.html): sidebar grouped **category → class → product** (searchable, badges = tier / image? / offer count); detail pane = image + BCC blurb + description + specs table + reviews + offers, with **Edit** and two-click **Delete**; **Add / Edit form** = brand, name, category + product_class (datalists of values actually present), BCC pick (select: Best Overall/Value/Premium), image URL, blurb, description, a Specs fieldset (material/capacity/dimensions/weight/model#/dishwasher), and a Primary-offer row (retailer/price/buy-URL). Replaces the demo viewer entirely.
- **catalog_store.py** additions: `list_products` (flat roster, sorted category→class→tier→name), `get_product`, `update_product` (merges a partial patch into the data blob, re-normalizes, **re-embeds**, keeps top-level columns + products_vec in lockstep), `delete_product`, `distinct_classes` (datalist source — the dictionary tables are under-populated so we read the products table itself).
- **Endpoints** (save_recipe_api.py): `GET /products/list` (products + classes + categories), `GET /products/{id}`, `PUT /products/{id}` (patch→update_product), `DELETE /products/{id}` (calls `_enable_vec_for_delete` first so the AFTER DELETE trigger cleans products_vec — project_vec_delete_triggers). `/products/list` is registered BEFORE `/products/{id}` so the static route isn't shadowed. Add reuses the existing `POST /products` (catalog_store.save_product, match-or-create). Old `GET /product-catalog` demo endpoint left in place (now unused).
- **library-shell.js**: added `{ page:'products', label:'Products', href:'/forms/products.html' }` to the admin group AND **alphabetized the admin group by label** (Chapters, Dishes, Domains, Jobs/Monitor, Jobs/Queued, Jobs/Scheduled, Labeling, Messages, Names, Product Grabber, Products, System, Taxonomy, Tips/Checks, Users). User group left in its intentional product order. Bumped `library-shell.js?v=20260709a → 20260714a` across all 18 pages so the new nav loads without a hard refresh.
- **In-process verified** (no restart needed for these): compile OK; list_products → 20; distinct_classes → 6; get_product round-trips; update_product self-edit exercised embed + vec upsert + column sync cleanly. `products_vec` shows 7 rows for 20 products — a PRE-EXISTING backfill gap (BLOB is source of truth; find_matches falls back), not from this change.

### PENDING — live verification after restart
The 4 new endpoints are new routes → **need a restart** (`bcc_restart.bat`, UAC). NOT yet exercised over HTTP. After restart, verify live: open admin → Products → see all 20 (14 loaf pans under Bakeware + bread oven / casserole / frying pan / wok / 2 santoku); open one (specs/offers/image render); Edit a blurb → Save (re-embeds, no error); Add a throwaway product → appears in the list → Delete it (no vec error). Then confirm the alphabetized admin menu + Products item show on a normal reload.

### Follow-ups
- Carried: grow the sparse product catalog (only 20 products / 6 classes — that's why most recipe tools match a category but return 0 products); score-only #2 live test; paywall calibration SELECTION half; `/collections/leaderboard`; sub-steps v2.1; Voice P1; cook-view failing-gate message.
- New: products_vec backfill gap (7/20) — rebuild from BLOBs (vector_store.rebuild_products_vec_from_blobs) so product↔product KNN is complete. Consider a "relink product classes" button on the Products or Taxonomy page (POST /product-classes/relink already exists) so new products join the recipe→product map without a manual call.

## Session log — 2026-07-14 (later) — Reviews subsystem: two-table model (reviews + review_products, DYNAMIC link) + per-source parser package + bookmarklet ingest loop

The catalog's review facts were invisible (buried inside `products.verdicts[]`, no UI) and welded to
one review at ingest. Built Reviews as a first-class ACDV surface AND re-architected the model with
the curator (product-first monetization). All SHIPPED to disk + verified over HTTP via TestClient;
needs a **BCC restart** (`bcc_restart.bat`, UAC) to serve the new routes + `forms/reviews.html`.
See [[project_reviews_architecture]] + [[project_monetization_pipeline]] (both NEW memories).

- **Architecture locked (curator, product-first):** reviews are the AUTHORITY layer that SUPPORTS a
  product's pick (rank-within-class + trust copy) — they do NOT select it (the recipe→class→rank
  commerce join does). Map: recipe monetization SURFACES (equipment · ingredients · cuisine→travel ·
  author→cookbooks) → product category assignment → recommended products → **supported by reviews** →
  reworked (our voice) + affiliate-monetized → the synthesized product block IS the interface.
- **TWO-TABLE normalized model** (`intake/products/review_store.py`, recipes.db — NEW tables
  `reviews` + `review_products`). `review_products` = one row per item AS REVIEWED = **source of
  truth** for tier/verdict/price/specs + retail identity (asin/model/url). The link to a catalog SKU
  is **DYNAMIC** (`review_products.product_id`, resolved by identity match — model/mpn/gtin/asin/url —
  or manual), **not predetermined**: one product can be supported by many reviews; an item can be
  UNLINKED. `products.verdicts[]` is now a **DERIVED PROJECTION** (`_reproject_product`, direct write,
  no re-embed) — no dual authoring. Migration lifted the 13 ATK loaf-pan verdicts into review_products,
  linked by identity, reprojected (guarded per-review so it can't resurrect deleted rows). Delete
  keeps catalog products + reprojects ([[feedback_no_silent_removal]]).
- **Per-source parser PACKAGE** (`intake/products/review_sources/`, curator: "a module per source
  style, like recipe standardization a method each"): `base` (shared retailer/asin/spec helpers) +
  `atk` (real, deterministic, ported from the old `review_parsers.py`) + `wirecutter`/`williams_sonoma`/
  `wsj` STUBS (detect the source, `IMPLEMENTED=False`, raise "not built yet") + registry
  (`detect_source`/`parse_review`/`supported`) + **`ingest_review`** bridge (parse → create_review →
  idempotent `upsert_review_product` by asin/name → resolve_links). `review_parsers.py` is now a thin
  back-compat shim re-exporting the package. ATK class is still hardcoded "Loaf Pans (1 lb)" — flagged
  TODO in atk.py (infer class+size grain from the page).
- **We can author our OWN reviews** — a review with reviewer="Best Cooks Club" (our editorial), no
  parser, items added by hand in the editor. First-class in the Add form (datalist offers it).
- **Bookmarklet ingest loop** (curator's vision: browse to a review → bookmarklet → extract header +
  product recs): `forms/review_bookmarklet.js` (clone of product_bookmarklet — harvests page markdown,
  keeps anchors for buy-links/ASINs, POSTs `/extract-review`, opens the editor at `?review=<id>`) +
  `POST /extract-review` (markdown+url → ingest_review) + `GET /review-sources`. reviews.html honors
  `?review=` deep-link.
- **UI** `forms/reviews.html` (ACDV, cloned from products.html): sidebar category→review (items/linked
  counts); detail shows each reviewed item with 🔗 link status, per-item Edit/Auto-relink/Remove, ＋Add
  item, Resolve-links, header Edit, Delete. Nav: **Reviews** added to admin group (alphabetized, between
  Products/System); library-shell cache bumped 20260714a→**20260714b** across all 18 pages + reviews.html.
- **Endpoints** (save_recipe_api.py): `/reviews/list`, GET/POST/PUT/DELETE `/reviews/{id}`,
  `POST /reviews/{id}/products`, `POST /reviews/{id}/resolve-links`, `DELETE /review-products/{rpid}`,
  `POST /review-products/{rpid}/link`, `POST /extract-review`, `GET /review-sources`.
- **Verified over HTTP (TestClient, real app):** list/get/create/update/delete; add item w/ real ASIN
  → auto-linked via 'asin', bogus ASIN → unlinked; verdict edit reprojects to the product block;
  `/extract-review` on the ATK fixture idempotent (13→13, all linked); unrecognized→422, wirecutter
  stub→422 "not built yet". Final DB clean: 1 review / 13 review_products / 13 linked / 0 orphans / 0
  dup verdicts. **recipes.db schema changed** (reviews + review_products) — recipes.sql NOT re-dumped.
- **PENDING live (post-restart):** open admin→Reviews→ATK loaf pans (13 items, all 🔗 linked); edit a
  verdict→Save→check the product block; Add a "Best Cooks Club" review + an item; run the review
  bookmarklet on a live ATK page (real-world capture fidelity vs the fixture is the open question).
- **NEXT:** infer ATK product_class (drop the hardcode); build the wirecutter/ws/wsj decoders
  (LLM-assisted for the unstructured ones); a manual product-picker for relink (auto-resolve only
  today); a review install page (mirror product_install.html) so the bookmarklet is one-click.

### Follow-up (same session, post-restart) — WS-taxonomy single type-ahead + shared URL control
Restart #1 confirmed the reviews subsystem live (port 8009: /review-sources, /reviews/list w/ 13
linked items, reviews.html HTTP 200). Then two curator refinements:
- **Review classification = a single type-ahead over the WS taxonomy** (curator: not cascading
  dropdowns — one searchable field; and it's a **4-level** tree headline > section > subcategory >
  our-added **leaf**, e.g. "Loaf Pans"). `reviews` gained additive cols `ws_category_id` + `ws_path`
  (idempotent ALTER); the pick resolves id→path+headline (headline seeds `category` for grouping).
  **`product_class` is no longer a join key** (items link by identity) so it's unlocked — the natural
  key is now (reviewer, url) via `_existing_review_id` (falls back to product_class only for url-less
  manual reviews); `_backfill_ws_taxonomy` seeds each review's taxonomy from `product_class_ws_map`
  (ATK → id 79, Bakeware > … > Bread & Loaf Pans). reviews.html: the locked reviewer+class fields
  became one `#f_tax` datalist type-ahead over `/ws-categories` (label = full ws_path [+ leaf]),
  editable on add AND edit; only reviewer stays locked. Verified over HTTP: ATK backfilled; create own
  review w/ pick → path+category derived; retaxonomize changes path; delete clean; ingest still
  idempotent (13→13).
- **Shared URL-field control** ([[feedback_url_field_control]], NEW memory) — `LibraryShell.urlControl(url,{display}|{inputId})`
  renders click-to-open (↗) + copy (⧉) icons, self-wires one delegated handler + CSS; works for
  read-only display and for a live `<input>`. First use: reviews.html source-URL input + provenance
  display. **Retrofit backlog: apply to ALL url fields** (domains, products, offers/buy URLs, install
  pages, system_config…). library-shell cache bumped **20260714b→20260714c** across all 18 pages +
  reviews.html (urlControl was added to the same cached file). node --check on library-shell.js passed.
- **Still needs another restart** for the taxonomy BACKEND (new `ws_*` cols + create/update handling +
  URL-less natural key) — the client (reviews.html + library-shell.js) is static so a browser reload
  picks it up, but POST /reviews from the new form (no product_class, sends ws_category_id) will fail
  on the pre-taxonomy server code until restarted.

### Follow-up (same session) — LLM review extraction (Grabber pulled 0 items on real pages)
Root cause (verified live on ATK `.../1482-13-by-9-inch-baking-pans-slash-dishes`, not paywalled): the
deterministic `atk.parse` skips any product block with no specs AND no price, but real ATK pages hide
specs/price behind collapsed "Full Ratings & Specs" toggles → it skipped all 13 → review imported EMPTY
(and mislabeled class via the hardcode). Curator's call: let the LLM extract (low-volume, infrequent).
- **`extract/markdown_to_review.py` (NEW)** — LLM extractor mirroring `markdown_to_product.py`
  (`llm.stream(operation="review_extract", model="claude-sonnet-4-6", max_tokens=8192)`, JSON-in-prompt,
  fence-strip + validate). Outputs the DECODER dict (singular `verdict` per product, NOT the pydantic
  plural `verdicts`) via local pydantic models. Rules: class from the HEADER (one class per roundup),
  every tested product with tier+verdict, FULL name incl. brand, offers/asin only from real links, and
  hard BRAND-SAFETY (never fabricate specs/prices/asins/verdicts). Verified on the 13x9 fixture: class
  "13x9 Baking Pans"/Bakeware, 11 products, correct tiers, real ASIN only where a /dp/ link existed, no
  invented prices.
- **`review_sources/__init__.py`** — new `extract_review()` = DETERMINISTIC-FIRST + LLM FALLBACK: try the
  regex decoder (free/instant; loaf-pan fixture still wins in 0.00s w/ specs), fall through to the LLM when
  it's unrecognized/stub/**yields 0 products**. `ingest_review` now (a) calls extract_review, (b) backfills
  EMPTY header fields (title/date/scale) on re-ingest without clobbering curator edits, (c) **SELECTS the
  WS taxonomy from the list** via `equipment_match.classify_term` (the same matcher behind the type-ahead
  sibling + commerce join — curator corrected: taxonomy is a search-SELECTION from the ws_categories list,
  NOT an LLM-invented label). Curator's manual type-ahead pick is respected (only fills when unset).
- **No endpoint/UI/bookmarklet changes** — `/extract-review` already routed to ingest_review.
- **Verified end-to-end (in-proc + TestClient HTTP):** the empty live 13x9 review now filled to 11 items,
  title "The Best 13 by 9-Inch Baking Pans/Dishes", ws_path "Bakeware > … > Casseroles & Baking Dishes"
  (selected from list), Winner = WS Goldtouch; idempotent re-ingest (24→24 review_products, no dupes);
  loaf-pan deterministic fast-path unaffected. NEW fixture `intake/products/fixtures/atk_13x9_baking_pans.md`.
- **Note:** a 3rd earlier-grabbed empty review exists (ATK "Rimmed Baking Sheets", 1718) — same root cause,
  fills on re-ingest. **Restart** needed so the LIVE bookmarklet uses the LLM path (in-proc run already
  populated the 13x9 review's data, visible via /reviews now).

### Follow-up (same session, cont.) — PER-SOURCE prompts + editorial "buying guide" capture
Two curator refinements after the first LLM pass:
1. **Extraction = LLM with PER-SOURCE PROMPTS (not deterministic, not one generic prompt).** Curator: "LLM
   with specific prompts... a set of source-specific prompts might improve efficiency." Refactored: the
   shared mechanism (extract/markdown_to_review) now builds its system prompt from a BASE + a `source_hints`
   fragment; each source module contributes an **`EXTRACT_HINTS`** string (atk = "Everything We Tested" tiers
   + "What You Need to Know" editorial + specs-behind-toggle warning; wirecutter = "Our pick/Runner-up/Also
   great" roles + "How we picked"; williams_sonoma; wsj = "Best Overall" + Pros/Cons). `extract_review` now
   detects the source, loads its hints, and runs the LLM tuned to that site — **LLM is the PRIMARY path**
   (the deterministic `parse()` regex stays only for the shim/offline, NOT called live). So ATK-specific
   knowledge lives in `atk.py` as a PROMPT, not a parser.
2. **Capture the review's EDITORIAL HEADER for writing OUR summaries** (curator: the per-product verdicts are
   sparse; the rich content is the "What to Look For / Avoid / considerations / FAQs" BEFORE the product
   list). Added `product_class.buying_guide` to the extractor (thorough, faithful, brand-safe capture) +
   `criteria` bullets. Stored on the review: new `reviews.buying_guide` column (additive ALTER) + `criteria`
   in the data blob; wired through create/update/get_review, `ingest_review` (passes it + backfills EMPTY on
   re-ingest, never clobbers curator edits), and `forms/reviews.html` (detail shows "Buying guide — source
   editorial" + "What to look for"; header form has an editable textarea). Verified over HTTP: 13x9 review
   carries a 573-char buying_guide (thin only because the hand-written fixture condensed it — real page is
   richer), GET returns it, PUT edits persist. NEXT (follow-up): an LLM step to REWORK buying_guide into OUR
   editorial voice (bcc_blurb / a class buying guide) — the "reworked" node of the monetization pipeline.
- **Restart still required** for the live bookmarklet + form to use the LLM extraction, taxonomy backend,
  and buying_guide columns (running server predates all of it; the DB data is already populated).

## Session log — 2026-07-17 — equipment "char-explosion" corruption: root cause + fix + 10-recipe repair

Curator flagged a recipe (Best Chocolate Chip Cookies, `30054f22…`) whose equipment was "a mess."
Its `equipment` was 27 `HowToTool` objects, each `name` a SINGLE CHARACTER (`[ { " n a m e : s i v
}` …). See [[project_equipment_standardization]].

- **Root cause (reproduced exactly):** `enrich.equipment.derive_equipment` reads the model's
  `equipment_list` tool value as `items` and iterates `for it in items`. The Sonnet tool call
  **intermittently serializes `equipment` as a JSON STRING** instead of an array (verified live: the
  tool_use input came back `type=str`) — iterating that string char-by-char, wrapping each char in a
  `HowToTool`, and deduping produces exactly the 27 single-letter "tools." The tool SCHEMA is correct
  (`equipment: array`); the model just doesn't always honor it. Pydantic would REJECT a string
  (`RecipeModel` raises → clean extract failure), but `derive_equipment` writes straight to
  `recipe["equipment"]`, bypassing that guard.
- **Fix (`enrich/equipment.py`):** new `_as_item_list()` coerces the tool value to a list —
  pass-through for a real list, `json.loads` for a JSON string, and **lenient on the model's trailing
  commas** (`{"name":"large bowl", }`, which fail strict `json.loads` and silently emptied the result
  — a quieter second bug), hard-reject any scalar so a string can never be char-iterated. Same guard
  added to `save_recipe_api._recipe_equipment_from_cook` (the `_cook` mirror path). Verified: module
  imports, `save_recipe_api` compiles, helper handles string/trailing-comma/scalar/list; end-to-end
  `derive_equipment` on the cookies recipe now returns 9 clean tools.
- **Data repair (`scripts/`-style one-shot):** scanned all 2,669 recipes-with-equipment → **10
  corrupted** (spanning 2026-06-03 → 2026-07-12, all base-extract, none `_cook` — a long-standing
  intermittent bug, NOT the recent backfill). Re-derived each via the fixed `derive_equipment`, wrote
  back ONLY the equipment field via fresh re-read (safe vs the live service; WAL + `busy_timeout`).
  All 10 now carry sensible tools (Berry Crisp, Julia Child's coq au vin, both banana breads,
  blueberry muffins, bougatsa, bratwurst, burrito bowls, Korean short ribs). Re-scan: **0 remaining**.
  (`bcc_token_journal` lock warnings during the run were the running service holding that table; the
  master_recipes/recipes writes committed fine.)
- **Blast radius small (10/2,669)** because the string-return is intermittent AND only the derive
  path (button/`_ensure_equipment`/backfill) is exposed — the markdown-LLM extract goes through
  pydantic, which rejects a string outright.
- **PENDING RESTART:** the running service still holds the OLD `derive_equipment` in memory, so a
  fresh extract could still char-explode until `bcc_restart.bat` (UAC). Code fix on disk +
  branch `split/enrichment-api`, uncommitted (alongside the reviews work). recipes.db data already
  repaired live — a page reload shows the clean tools now.
- **Not done (low risk):** the identical loop in `scripts/backfill_equipment.py:_mirror_from_cook`
  (mirrors structured `_cook.equipment`, far less likely a string) — left unguarded; flag if touched.

### Follow-up (same day) — reviews: per-item "product sources" links
Curator (after a live bookmarklet extract): "no ability to link to the list of product sources from
our individual product review summaries — that's a must." Each `review_product` already carried
`retailer_offers` (real affiliate buy-links: Amazon `?tag=…`, Sur La Table, Le Creuset…) but the
per-item view never exposed them. Fix: `review_store._review_products` now returns `retailer_offers`
per item, and `forms/reviews.html` `itemCard` renders a **"Sources:"** row of retailer chips, each
with the shared `LibraryShell.urlControl(url,{display:false})` open ↗ / copy ⧉ icons (buy-links are
the ONLY outbound links — brand-safe; the review's own page `url` stays provenance-only). Falls back
to the item's single source `url` when it has no offers. Verified: review_store compiles,
`_review_products` emits offers for the extracted ATK bread-oven review (7 items, 1–3 offers each),
reviews.html inline JS parses. **Needs a restart** for the offers to reach the live API (old
`_review_products` in memory returns only the `url` fallback until then).

## Session log — 2026-07-22 — recipes.sql backup was silently NON-restorable (generated columns) + Ask Chef delete fix

- **`backup_db.py` dump could not be restored — FIXED.** Re-dumping recipes.sql surfaced that the
  git-side backup (the "restore source") had been broken since `master_recipes` gained the
  `dish_key` / `publisher_key` **GENERATED ALWAYS (…) VIRTUAL** columns: `write_sql_dump` emitted
  `SELECT *` + bare `INSERT … VALUES(…)`, so each row carried the 2 computed generated values that
  can't be INSERTed → `executescript` failed with "master_recipes has 9 columns but 11 values". The
  **currently-committed HEAD recipes.sql failed the same way** — the backup silently didn't restore.
  Fix: the dump now names the INSERTABLE columns explicitly (`PRAGMA table_xinfo`, excluding hidden
  2=VIRTUAL / 3=STORED generated) in both the column list and `INSERT INTO t (cols) VALUES(…)`.
  master_recipes is the only table with generated columns (full scan). Verified: fresh dump
  **round-trips into a clean sqlite DB**, all row counts match the live DB (recipes 344,
  master_recipes ~2889, reviews 4, review_products 31, products 21, ws_categories 217, domains 298,
  dishes 116), and `master_recipes.dish_key` recomputes on the restored copy.
- **Dump is now GZIP-compressed — `recipes.sql.gz`.** The valid re-dump was **158 MB uncompressed**,
  past GitHub's 100 MB per-file limit (push rejected by the pre-receive hook) — the old 72 MB dump
  squeaked under, but real corpus growth blew it past. Curator's call (vs Git LFS / untracking):
  gzip. `backup_db.py` writes `recipes.sql.gz` via `gzip.open` (~5× smaller — **32.8 MB**); ADAM copy
  is `recipes_{ts}.sql.gz`; plain `recipes.sql` is now git-ignored (never tracked); `.gitignore` +
  docstrings updated. Restore: `gunzip -c recipes.sql.gz | sqlite3 new.db`. Verified the .gz
  round-trips clean. ADAM copy skipped (`--no-adam`); run `bcc_backup.bat` for the off-machine copy.
  (The old 72 MB `recipes.sql` blob stays in git history — harmless, under the limit, already on origin.)
- **Ask Chef / Chef's Notes delete (×) fixed** (`forms/recipe_form_styled.html`, committed 430a859) —
  the note-delete was guarded by `notesList.children.length > 1`, so the X silently no-opped on the
  SOLE remaining block; an Ask Chef Q/A that was the only note could never be removed. Last block now
  clears in place (keeps the ≥1-block invariant like ingredients/steps); multi-block delete still
  fully removes. Verified live in-browser (real mouse click).
- **Unblocker "402" diagnosis — it's the ORIGIN, not billing; log message fixed.** seriouseats
  publisher-refresh (jobs 574/575) showed every URL 402-ing from the unblocker → Wayback fallback,
  looking like an unpaid Oxylabs account. Runtime data disproved that: SAME proxy/creds/moment,
  thekitchn returned 200 (200 live fetches, 0 errors); only seriouseats 402'd. Captured the discarded
  402 BODY — it's a **People Inc. / Dotdash access-block page** ("contact support@people.inc"):
  seriouseats serves **HTTP 402 as its bot-block status** (unusual — most use 403), and Oxylabs
  relays that origin status through the proxy. So it's target-side IP blocking, INTERMITTENT
  (IP-dependent: on retry 4/5 fetched LIVE), not an account issue. Fix (`to_markdown/html_to_markdown.py`
  `_fetch_via_proxy_unblocker`): the non-2xx log now reads "{provider} proxied OK; target ORIGIN
  returned {status} — origin anti-bot block" (flags 401/402/403/429/503) instead of the misleading
  "{provider} returned {status}", and the transport/except branch says "transport/account error" — so
  a future origin 402 never again reads as "unpaid". Verified live (new message fires on a real 402).
  The Wayback fallback already covers the content. See [[project_fetchfail_salvage]].
- **Oxylabs tuning explored → CLIENT-SIDE RETRY-ON-BLOCK shipped (the real lever).** Measured live +
  read the docs. Findings: (1) **geo-targeting doesn't help** — `x-oxylabs-geo-location: United States`
  produced a 402 where no-geo gave a benign 404 on the same URL. (2) **Residential proxies would be a
  DOWNGRADE** — raw IPs, no anti-bot/JS-render; Web Unblocker is the right product for a Dotdash site.
  (3) **Root mechanism (from Oxylabs docs):** Web Unblocker marks ANY target **4xx as success and
  passes it straight through WITHOUT rotating** — it only auto-retries 5xx / AI-detected blocks — and
  `x-oxylabs-successful-status-codes` **can't** force a 4xx retry (4xx always "successful"). So a 402
  bot-block inherently slips its retry logic. (4) Web Unblocker rotates fingerprint+IP **per request**
  (observed via `X-Oxylabs-Request-Headers` cycling UA/platform); the block is IP-intermittent
  (measured ~80–100% single-shot live now vs 10/10 blocked during the hot job window). **Fix**
  (`_fetch_via_proxy_unblocker`): a bounded fresh-IP retry loop on origin block statuses
  (401/402/403/429/503), gated by the `X-Oxylabs-Content-Status-Code` echo so it retries an ORIGIN
  block but NOT an Oxylabs-side 429 rate-limit; count = `system_config.unblocker_block_retries`
  (default **2**, code-fallback like the sibling unblocker_* keys; 0 disables). Verified live: 2xx →
  LIVE, real **404 → NO retry**, import clean. `x-oxylabs-render:html` unchanged.
- **Post-restart re-run (job 576) → the retry alone does NOT rescue a BURST block.** Live tally: 0 LIVE,
  36 origin blocks, 73 retry attempts, 31 Wayback. The retry fires correctly but every fresh IP still
  402s during a harvest burst — People Inc. blocks the whole reachable Oxylabs **exit pool** (the set of
  proxy IPs Web Unblocker rotates through), not one IP, so rotation can't win. Isolated/spaced requests
  recover only partially (delay experiment: **2/6** with 60–100s spacing + 90s cooldown, and the block
  RETURNED after 2 successes → cumulative/reputation, not a simple rate window).
- **CIRCUIT BREAKER shipped** (`_UNBLOCKER_HOST_BLOCKS` in html_to_markdown): after N consecutive origin
  blocks on a host in a run, stop calling the (slow, now-known-billable) unblocker for that host and go
  straight to Wayback; a non-block response (2xx or real 404) resets it. Process-scoped → resets per job
  run. Count = `system_config.unblocker_circuit_trip` (default **3**, 0 disables). Unit-tested (trips at
  3, resets on success, 0 disables; fixed a `www.`-strip host-parse bug so `washington.com` isn't mangled).
- **Per-domain DELAY: built then REVERTED** (curator: "forget the delay — it didn't work and increases
  complexity"). Only ~33% effective and the underlying cause is reputation/licensing, not rate. Backed out
  the `domains.harvest_delay_ms` column + throttle cleanly (zero diff).
- **Deep web research (subagent) — is there a NATIVE Oxylabs fix? NO.** `x-oxylabs-successful-status-codes`
  is additive-only ("2xx and 4xx are always marked successful" — can't demote a 402); Web Scraper API has
  the SAME rule; no product/header exposes content-vs-status block detection. **CORRECTION to an earlier
  claim:** a 402 is a **billable "success"** to Oxylabs (4xx=success), so the retries are NOT cost-free —
  each blocked URL with retry=2 is 3× billed, which is exactly why the circuit breaker matters (caps it).
  **Reframe:** the 402 + "contact support@people.inc" is likely a deliberate **Pay-Per-Crawl / licensing
  gate** (industry trend since ~mid-2025, incl. Cloudflare), i.e. monetizing identified crawler traffic,
  not a solvable anti-bot puzzle. **The one real lever = Bright Data Web Unlocker's `x-unblock-expect`**
  (content-validation → auto-retry on a block that returns a 2xx/4xx) — the behavior Oxylabs won't expose;
  a per-domain provider swap (our code already lists `brightdata`), billing flips to 100%-of-requests,
  FUTURE work. Pragmatic present: **Wayback already carries seriouseats**; strategic endgame for a
  Pay-Per-Crawl publisher is licensing, not brute-force rotation. See [[project_fetchfail_salvage]], [[project_serp_provider]].
- **Harvest log readability — 3 additions** (curator, while watching the seriouseats log): (1) the
  **winner-extract** lines now carry a `[N/keep]` seq (`[PUBLISHER-REFRESH] [3/5] SAVED master …`),
  mirroring the candidate loop's `[N/120]` — caller builds a seq'd log_prefix in `_extract_publisher_url_to_master`;
  correctly tracks the SLOT through backfill (a THIN winner's slot shows the same `[2/5]` until filled).
  (2) each **candidate KEEP/DROP line shows the FETCH SOURCE** (`direct | unblocker | wayback | page-cache`)
  in an aligned column — `_fetch_for_filter` now returns the source (`meta["source"]`), threaded via a
  `_dl` decision-line prefix into every post-fetch line (`[ 4/120] page-cache KEEP json-ld …`); pre-fetch
  drops (EXCLUDE/URL-SKIP/collection-title/KW-SKIP) have no source, left as-is. Resolves the earlier
  "why did some come through directly?" confusion — cache hits now read `page-cache`, nothing was live.
  (3) the **Moz DA/PA block now shows OU** — the per-URL MOZ-OK line can't (OU needs the whole-corpus fit
  stamped by `score_members` AFTER all candidates score), so a ranked `*[rank/n] pa= da= ou=` display is
  emitted post-scoring (`*` = selected top-N). Verified live (job 578, success): all three formats render,
  source column shows wayback+page-cache, OU displayed, winners `[1/5]…[5/5]`. Fresh job process picks up
  all three without a server restart (`python -m jobs exec` imports current code).

## Session log — 2026-07-22 (later) — SERP provider audit: SerpApi bill was subscription waste; SerpApi kept as the product-engine reserve

Curator got a "$300 SerpApi bill" and wanted to lower it. Audited the actual code/config (not the UI labels).
- **Both search blocks are already on Scale SERP** (since the 2026-06-17 switch), not just one. There is ONE global `serp_provider` config; BOTH the domain harvest (`collections_lib._serp_links`) and the dish refresh (`build_query_batch._serpapi_lookup`) route through the same `serp_search()` chokepoint. `active_provider()`='scaleserp' at runtime (the stored value is `'"scaleserp"'` but `get_setting` strips the JSON quotes correctly — no quote-trap bug). NO direct SerpApi calls bypass the chokepoint (the `SERPAPI`/`_serpapi_lookup`/`[SERPAPI]`/`top_n_serpapi` names are all LEGACY; only `scripts/serp_ab.py` forces `provider="serpapi"`, manual-run only). So SerpApi web-search usage is ~0 since 2026-06-17.
- **The "$300 bill" = subscription waste, not usage.** Curator was on a **$150/mo** SerpApi plan doing nothing → 2 idle months. Downgraded to SerpApi **$25/mo basic**; **~5,000 credits carried forward**.
- **STANDING DECISION — keep SerpApi as the PRODUCT-ENGINE reserve (do NOT cancel).** SerpApi bills one credit per search across ALL engines (Google, **Google Shopping**, **Amazon**, Walmart, eBay, Images), so the 5,000 credits are **transferable** — reserve them for the commerce build (product discovery to populate the sparse ~20-product catalog, offer/price/ASIN enrichment, review→product identity matching), NOT for web search. Web search stays on Scale SERP (cheaper + equivalent). When product discovery is built, put it behind a `product_search()` chokepoint mirroring `serp_search()` so Amazon/Shopping lookups can spend these credits now and swap to Traject/Rainforest (the planned consolidation) later. Recorded in [[project_serp_provider]] + [[project_monetization_pipeline]].
- **`serp_provider` is a DB `system_config` row, form-editable** (category General) — flip serpapi⇄scaleserp in `forms/system.html`, no code change. Rough edge: it's NOT seeded in `SYSTEM_DEFAULTS`, so it shows as a bare key with null label/description (candidate polish: seed it with a label + the two valid options). Safety: `SERPAPI_KEY` still in `.env`; nothing live needs it — could rotate/remove so an accidental SerpApi call fails loudly (but keep it while the 5,000 credits are in play).

## Session log — 2026-07-22 (later) — Moz row reduction: per-domain canonical-variant learning (~4x fewer rows)

Curator upgraded Moz to Growth Medium ($250/mo, 120k rows, overages $20/10k) after blowing the starter plan (49k rows in 22 days, running larger domain/dish samples). Audited the Moz cost driver + built the fix.
- **ROOT COST: ~4 Moz rows per scored URL.** `url_scoring.score_url_via_moz` probed up to **4 variants** (`_url_variants`: www/non-www × trailing-slash) per URL to find the CANONICAL form (the one the site serves — concentrates the link graph; other forms score ~15 PA lower). Moz bills **PER TARGET** (confirmed: batching saves round-trips, NOT rows), so every URL = 4 rows. 49k rows ≈ only ~12k unique URLs.
- **FIX — per-domain canonical learning** (`moz_domain_canonical` table + in-process `_CANON` cache): the canonical pattern is CONSTANT per domain, so learn it ONCE (first URL of a domain = full 4-variant probe → record `(use_www, trailing_slash)`) then query only that SINGLE variant for the rest = **1 row**. ~4× fewer rows on the dominant case (a publisher harvest is all one domain: 120 URLs = 4 + 119×1 = 123 rows vs 480). Persists across runs (DB), so a re-harvest of a known domain is 1 row/URL from the start. Self-healing: a learned single-variant that returns no Moz data (http_code 0) re-expands + re-learns. Kill switch: `system_config.moz_canonical_learning` (default on).
- **Moz http_code finding (corrected my gate):** for real recipe URLs Moz returns **402 = "has estimated data"** and **0 = "no data / wrong form"** — NOT 200. The canonical = highest-PA variant with `http_code != 0`. First learn-gate (200/301/302 only) never fired; broadened to "usable" (200/301/302/402).
- **Verified LIVE (real Moz, ~5 rows):** seriouseats call 1 → 4 variants → learned (www, no-slash), persisted; call 2 → **1 target**, PA 61/DA 89 (canonical value, not the ~40 under-scored form). No accuracy loss, 4× fewer rows. Fresh job process picks it up (no server restart).
- **Moz beta (V3) assessed:** NO organic-traffic metric (keep SEMrush); beta adds Brand Authority (link-independent domain-authority axis — candidate enhancement to domain scoring), Ranking Keywords + volume (DIY traffic proxy), `site.metrics.fetch_multiple` (batch — saves requests not rows). No row-cost relief in the beta → the canonical-learning fix is the only row lever. Recorded in [[project_domain_scoring]].

## Session log — 2026-07-22/23 (later) — Moz V3 unlocked + DEEP domain enrich (Moz facts + grounded LLM research)

Follow-on from the Moz-row work. Curator: "add brand authority... what other data is available at the domain level... our domain enrich is weak, I was going to buy an LLM research call to embellish the record."
- **Moz V3 API cracked + client SHIPPED** (`input/pipeline/moz_v3.py`, commit 05bb788). The v2 `url_metrics` endpoint does NOT return Brand Authority; it's a V3 method. V3 auth = **`x-moz-token` header** (base64 ACCESS_ID:SECRET, same creds — NOT `Authorization: Basic`, which V3 rejects), methods need the **`data.` prefix** (the "Action not found" fix), `id`≥24 chars. Functions (never raise): `brand_authority`, `site_metrics` (DA/PA/spam/referring-domains/inbound), `ranking_keywords` (what a site ranks for w/ volume+rank — 1 ROW PER KEYWORD), `quota_rows` (free introspection). Verified live: seriouseats BA 64, DA 89, 138,351 referring domains, ranks #1 for air fryer (293k/mo)/prime rib/elotes/cacio e pepe; quota 50,360/120,000 used. Spec in [[project_moz_scoring_cost]].
- **DEEP domain enrich SHIPPED** (commit 32397d2) — two-layer record replacing the 1-2 sentence Haiku blurb:
  - **Facts (Moz V3):** new `domains` cols `brand_authority` / `referring_domains` / `ranking_keywords` (JSON) / `profile` / `enriched_at` (`_ENRICH_COLUMNS`, auto-migrated; first four curator-editable).
  - **Story (LLM research):** `extract/domain_enrich.deep_enrich_domain` — Moz facts + homepage → a **Sonnet** call GROUNDED on the facts (esp. ranking keywords = what they're authoritative on) → 2-4 paragraph `profile`. Told not to fabricate people/awards it can't support (verified: it declined to invent seriouseats' founding team).
  - **Endpoint** `POST /domains/{domain}/deep-enrich` (returns suggested fields; curator reviews+saves, like the quick enrich). ~16 Moz rows + 1 Sonnet call/domain.
  - **UI** `forms/domains.html`: Profile textarea + "Authority & reach (Moz)" block (BA + referring-domains fields + ranking-keyword chips) + "🔬 Deep enrich" button + save wiring.
  - Verified: deep_enrich live (seriouseats 3,022-char grounded profile saved to the row), save round-trip, JS parse. **NEEDS RESTART** for the new `/deep-enrich` route to serve over HTTP (deep_enrich_domain itself + the schema + the static HTML are already live/migrated). See [[project_domain_master]].
  - No web-search in the LLM stack, so the "research" is a strong model grounded on Moz facts + homepage + its training knowledge (not live web crawl); a future upgrade = add a web_search tool for current facts.

## Session log — 2026-07-23 (later) — SEMrush advanced-filter deep-link generator (verified live)

Curator wants pre-filtered SEMrush exports (sites like southernliving/bostonglobe have lots of non-recipe content). SHIPPED + verified live (commits 9570a03, 95aa590; needs no further restart — done).
- **The domain form now GENERATES the SEMrush Top-Pages (Organic Pages) URL with an Advanced Filter.** Per-domain fields (`_SEMRUSH_FILTER_COLUMNS`): `semrush_db` (country, us/**gr**), `semrush_search_type` (domain/subdomain/subfolder/url — manual dropdown), `semrush_filter_word`, `semrush_filter_field` (**default `url`**), `semrush_filter_include`, `semrush_filter_criterion`. `domains_lib.build_semrush_pages_url(d)` byte-matches the curator's proven URL; `semrush_report_url` is now DERIVED (dropped from EDITABLE_FIELDS + old override). Form mirrors SEMrush's own filter form + a live preview. Verified live: southernliving shows Word=Recipe + the generated `toppages/?db=us&q=…&filter=…` link.
- **`url` default** (was mkwd): curator found URL matches ~900 recipes vs Top-Keyword's ~100 on bostonglobe; bulk-migrated 299 rows.
- **Report = ORGANIC pages** (research): toppages == organic/pages (same data, excludes paid); the paid "Top Pages" is Traffic-Analytics at a different URL. Base is config (`semrush_pages_base_url`).
- **Open/future (curator notes, in [[project_backlinks_source]]):** (1) the `url`/other fld·cri codes are GUESSES — SEMrush doesn't document them; confirm by decoding a live URL. (2) multi-condition filters (`advanced:{"0","1"}`, AND) — support one now. (3) an "Uncouple" switch to paste a custom URL + grey out the form fields. (4) [[project_moz_scoring_cost]] TODO: Brand Authority on the recipe form/post — a CALL to `moz_v3.brand_authority`, not a code copy.

## Session log — 2026-07-23 (later) — Trust-extraction override + domain-form field glossary + dead-field cull

Follow-on from the Boston Globe drop investigation. The Globe embeds real recipes in a story-format
layout (no "Ingredients" heading); the cheap structure gate + LLM cascade DROP them before extraction,
yet the full extractor decodes them perfectly (16 ingredients, 3 steps on the gateau-basque test).
- **Trust-extraction per-domain override SHIPPED** (commit e214edf). New `trust_extraction` flag on the
  domains master. When set, the harvest KEEPS that host's candidates past BOTH the structure gate and
  the cascade poor_quality/not_recipe catch, so they reach the extractor. `domains_lib`:
  `trust_extraction` col (`_ENRICH_COLUMNS`, auto-migrated) + `EDITABLE_FIELDS` +
  `get_trust_extraction_hosts()` cache (+ invalidation). `build_query_batch`: load trust hosts, stamp
  `_trust_extraction` per entry, trust-keep branch in the structure gate (`KEEP trust`).
  `isrecipe_cascade.apply_decide`: skips the catch for trusted hosts. `domains.html`: checkbox + save
  wiring. **Safe when paired with a SEMrush URL filter** that already narrows to recipe pages. NEEDS
  RESTART before the form can save the flag (schema + `domains_lib` code in the server process); the
  harvest job process picks up its side automatically (`python -m jobs exec` fresh import). See
  [[project_split_architecture]] / docs/recipe-candidate-pipeline.md (Stage 2 trust gate).
- **Domain-form field GLOSSARY written** (commit 71258f8, `docs/domain-form-fields.md`). Curator: "we
  have options in the domain form that are opaque TO ME!! ... i suspect some of them don't do anything."
  Every control on the Domains editor, in form order, tagged 🟢 LIVE / 🟡 CURATOR-TOOL / ⚪ INERT,
  verified against the code (an Explore agent traced each of ~40 fields to its consumption site; I
  hand-verified the surprising calls — it wrongly flagged `harvest_records` dead, but that one is LIVE
  via the refresh payload like keep_top_n). Key clarifier: the open/blocked/curate **mode cards are
  presets over four low-level switches** (fetch_strategy/render_required/check_recipe/score_only), not
  fields. The whole SEMrush deep-link group is CURATOR-TOOL (builds the export hotlink; no automated
  step reads it).
- **Two truly-vestigial fields REMOVED** (this commit). `custom_extractor` (deceptive name — there was
  NEVER a per-domain custom-extractor mechanism wired up; zero dispatch) and `failure_count` (never even
  written; was only a display pill). Dropped from the form + `EDITABLE_FIELDS` + `CREATE TABLE`. Existing
  DBs keep the now-ignored columns (harmless). Aspirational inert fields KEPT on purpose: story/profile,
  the Moz-enrich trio (brand_authority/referring_domains/ranking_keywords), country/cuisine_focus/
  ethnicity, extract_notes, domain_authority/da_last_scored, notes.
- **Also this session (earlier, already committed):** SEMrush "Uncouple" switch (2980c5e), Moz dish-batch
  rows summary line (cb99020), jobs stderr-to-per-file-log (0bd8374/20edeee), old dishes.html removed
  (fb4d473).

## Session log — 2026-07-23/24 (continued) — cohort panel + urlField hoist + product bookmarklet folded into the ACDV editor

Same session, after the trust-extraction/glossary/field-cull work above.
- **Boston Globe = authority-lens site (stamped).** Ran a Globe harvest (job 598): 250 URL slice → all 250 pass the is-recipe gate (trust_extraction bypasses it) → Moz-scored → **30 extracted into master**. The "very long pause" the curator saw was the ~2.5-min sequential Moz scoring of the 250-URL slice (un-timestamped log lines + a Cloudflare-tunnel SSE stall that flushed in a burst); the job was never stuck — the browser↔server SSE dropped while the detached `python -m jobs` subprocess kept writing its file log. Insight: the Globe gets **near-zero organic traffic** (only ~top-15 recipes have any, most <3/mo) precisely because its story-format recipes are structurally invisible to Google (no recipe JSON-LD) — the same trait that needed trust_extraction. So it's a **hidden-gems / authority-lens** site (select by Moz OU/power, NOT traffic), opposite of a high-traffic "hot" site. Stamped in `domains.notes` for now; promote to a first-class `selection_lens` field when the hot/lens feature is built. New memory [[project_selection_lens]] (two lenses: traffic→"hot" collection type vs authority→"editor's find"; cross-site aggregate hot is the hard part — traffic isn't comparable across domains).
- **Domain scored cohort = a persistent panel** (commit cc693bb). Was a toggle that swapped the winners list in place (easy to miss; reset to winners-only after each refresh). Now an always-present `<details class="ed-accordion">` mirroring the dishes form (reuses the shared component from editor-shell.css): winners list stays winners-only above; the full ledger cohort (winners ★ + dimmed also-rans) lives in its own panel below with the count in the summary (`Scored cohort (30★ / 250 scored)`) even while collapsed. Curation buttons (Process selected / manual queue / userscript) moved into the panel; `.ms-pick` selectors read `#domainCohortList`. Both lists load on open + after every refresh/process.
- **URL fields → shared copy/open icons; helper HOISTED into LibraryShell** (commits 1ee38ce, 51f6d3d). Per [[feedback_url_field_control]]: every URL field ships with ↗ open + ⧉ copy, programmed in. First retrofitted products.html (Image URL, Buy URL) with a local `urlField` copy, then **hoisted `urlField` into `library-shell.js`** as a thin wrapper over the existing `urlControl(inputId)` — whose `_ensureUrlCtl()` already self-wires ONE global delegated `.ls-url-open`/`.ls-url-copy` handler + CSS. domains.html + products.html now `const { …, urlField } = LibraryShell` and deleted their local helper AND per-form handlers (bumped the library-shell.js cache-buster). No more per-form copies. (domains_mockup.html still has an old `url-ic` copy — throwaway prototype.)
- **Product bookmarklet folded into the ACDV editor** (commits f470224, e04f33e). The "Grab Product" bookmarklet landed in a standalone one-off (`product_form.html`) with no product list. Now mirrors the recipe grabber: bookmarklet → **`products.html?staged=<token>`** → auto `/staged-markdown` → `/extract-product` → **prefilled Add form inside the normal ACDV shell** (list + hamburger menus one tap away). Ported into products.html: the merge/dedup block (add-vendor-to-existing vs new; sends `merge_into`), the review "facts" block (per-source verdict → "rewrite in our voice → blurb"), MPN/GTIN spec fields, and split **Source URL vs affiliate Buy URL**. **Source links** (the missing "back to source"): detail view shows per-offer `source ↗ / buy ↗`; form has a Source URL field + the grabbed-from source with the shared control. **Retired `product_form.html`** (255 lines; bookmarklet retargeted, no live refs). Validated `/extract-product` shape live against the ATK fixture (product+specs incl. mpn/gtin + 3 match candidates). The product importer is tuned for **RETAILER product pages** (`extract/markdown_to_product`), distinct from the REVIEW bookmarklet (reviews.html → ATK/Wirecutter/Williams Sonoma/WSJ). Extended the retailer host→name map 7→37 (Amazon/Target/Walmart/Wayfair/Costco + WS/Sur La Table/Crate&Barrel/CB2/WebstaurantStore + KitchenAid/Lodge/Le Creuset/Zwilling/Staub/All-Clad/OXO/Vitamix/Breville/Cuisinart/Ninja/Made In/Caraway/… ) and fixed a duplicate surlatable key.
- **Restart notes:** static HTML/JS (domains.html, products.html, library-shell.js, bookmarklet) are live on next load. Python-in-server changes need a restart (admin, `bcc_restart.bat`): the trust_extraction schema+`domains_lib` (so the form can SAVE the flag) and the extended `_RETAILERS` map (so `/extract-product` auto-fills the new retailer names). Harvest jobs pick up their side automatically (fresh `python -m jobs` import).

## Session log — 2026-07-25/26 — crash recovery · RealRank rebuilt (fetch our own sources, score from real histograms) · RealRank on the product record + form · Amazon collection selection designed

Started as catch-up after a hard shutdown; became the product/commerce build.

### Crash recovery (2026-07-25)
- **`cook_ask.py` had `like ` typed into line 1** — a stray keystroke saved into the wrong window (PyCharm focus is ambiguous; new memory [[feedback_stray_keystroke_corruption]]). A SyntaxError, but invisible: the import is lazy and inside `try/except`, so the server started fine and only Ask Chef would have failed. `compileall` over the tree was otherwise clean.
- **The outage was NOT the app**: Kernel-Power 41 unexpected shutdown 22:04, then TrustedInstaller reboots and KB5121767 installing successfully at 22:29. Third Kernel-Power 41 since 6/25 — still [[project_host_thermal_shutdowns]]. DB verified intact (quick_check ok, WAL, 349 recipes); repo was already in sync with origin.
- Housekeeping: `.gitignore` now covers `input/*.xlsx|csv` (13 loose SEMrush exports) and `uvicorn_stderr_new.log`. Deleted a stale memory claiming the status-messages work was uncommitted — it shipped and became `message_categories`.

### Review acquisition: SERP → curator approves → we fetch our own copy (645f93a)
`intake/products/review_finder.py` + `POST /reviews/find` / `/reviews/ingest-url` + a finder panel on reviews.html. Discovery costs one SERP call and touches no target site; the curator picks which sources count; only then do we fetch each through the unblocker and run it down the existing ingest rails. **Verified: Serious Eats fetched LIVE → 6 products with tiers** — the source a general web agent could only reach through a Bloglovin mirror. reviews 4→6, review_products 31→38.

### RealRank: the curator's prototype, given BCC's reach (99fa024, cd07241, e7fde14)
Curator built `docs/RealRank/` in Claude Desktop: product name in → attributed brief out. It ran on the model's own `web_search`, which is blocked by ATK, Serious Eats and Wirecutter — and **when a named source is unreachable the model doesn't stop, it quietly backfills** (measured: all three missing, Forbes/Foodal/Woman&Home in their place, nothing saying so).
- **`fetch_source_docs()`** — SERP each source's own page, then the same tiered ladder recipes climb (UA → unblocker → Wayback), parallel, relevance-trimmed. 8/8 retrieved on both test products.
- **`_host_ok()`** — a `site:` query that matches nothing returns UNRESTRICTED results; three publishers once resolved to **kitchenaid.com's homepage** and were labelled as those reviewers. Every hit is now validated against the host it claims. **Never relax this.**
- **`source_coverage` + `fetch_log`** make a gap visible; a failed fetch keeps the URL so it can be bookmarkleted instead. Adding ATK's real verdict (Ankarsrum wins) moved the KitchenAid **Top Pick → Highly Recommended**.
- **Owner data**: `intake/products/amazon_rainforest.py` (ASIN + histogram + top reviews). Score computed from the FEED, never the model. **Pooling** (`pool_histograms`) sums counts across retailers rather than averaging scores — 86.2 vs the correct 86.8 on the Lodge. **Polarization** reads the J-shaped barbell (Lodge: 1★ 3% > 2★ 1%) and says so in plain language.
- **The posting** (`realrank_posting.to_html`) renders the record into the three placements prototyped in `realrank-loafpan.html` — editorial note / full card / compact — reusing that CSS rather than inventing a second look.
- Fixes that made it run at all: repo-root `.env`, max_tokens 4000→12000 (JSON was truncating), utf-8 writes, raw reply saved before parsing, per-product output paths, chdir so a direct run stops creating a second empty `recipes.db`. Now calls through **`llm.create`** so the priciest call we make is in the token ledger.
- Runs as job type **`realrank_research`** — tracked, logged, cancellable per source.

### RealRank on the product record + the product form (026c46f, 8945642)
- **Model**: one nested `realrank` block (RealRank/OwnerRatings/RatingSource/ExpertFinding). Two deliberate separations: `realrank.score` (owner arithmetic) is NOT `rank_score` (expert consensus) — they legitimately disagree; and `realrank.findings` are NOT `verdicts` — a verdict comes from a page a curator chose. Three auto-migrated indexed columns (`realrank_score/_at/_verdict`). `set_realrank()` does NOT re-embed (`compose_product_text` is an allow-list, so review prose never fed similarity) and a re-run RESETS approval.
- **Endpoints**: `POST /products/{id}/realrank` (entity-locked, identity from the DB row) and `/realrank/approve`. Extracted `_spawn_job_runner()` so one path reaches a runner.
- **Form block** is an AUDIT surface, the inverse of the consumer card: coverage and freshness first, prose in a `<details>`. Findings carry `via` and link OUT — deliberate departure from the consumer no-outbound rule, since staff auditing an attribution need the page. **Verified live on the WS Goldtouch loaf pan (job #612)**: 5/8 sources, Reviewed/Tom's Guide/TechGearLab correctly ABSENT (no loaf-pan review), score 92.1, cheaper-alternative callout naming the USA Pan at half price.

### Amazon selection — vendor testing + the free histogram widget (a5b0b86, eb5c468, 835fd94)
- **`intake/products/amazon_widget.py`** — Amazon's own rating-popover endpoint, per-ASIN, free, no key, no product page. Verified identical to the paid API on two products. The supplied draft parser returned histograms totalling **410% and 445%** (it walked every element with loose regexes; 410 = the 5-star value assigned to all five bars) — rewritten to parse ONLY the canonical `aria-label="5 stars represent 82% of rating"` bar; anything else is a failure, not a guess. **Selection-stage only** — the enhancement stage keeps its one call for ratings + listing facts.
- **Ladder**: free direct → EasyParser product → STOP. Not the unblocker (a credit costs more than the API call, and we'd spend it on the ~17 of 20 candidates we're discarding); and if a paid product API can't return the product, no anti-bot bypass fixes that.
- **EasyParser tested live** (key `EASYPARSER` in .env, free tier 100 credits, 1/call). **SEARCH is a win: 48 items for ONE credit** with asin/rating/ratings_total/brand/price/image/categories — and it demonstrated the Wilson stage perfectly, since Amazon's own `review-rank` puts a 4.9★-from-111 second while the screen lifts the 4.8★-from-22,754. **DETAIL's `rating_breakdown` is ALL ZEROS** on both test ASINs (rating and totals exactly right). That kills EasyParser as the histogram fallback AND as the widget's cross-check. Zeros are the dangerous shape — they read as data — but `pool_histograms` already skips zero-sum histograms, so it degrades to absent.
- **Rainforest KEPT as the canary** (curator's call): `verify_against_rainforest(asin)` reports per-star deltas + `agree`. First run exact. Curator is contacting EasyParser to fix the histogram — **keep the breakdown-handling code**.

### Designed, NOT built — Amazon collection selection
Greenfield, and explicitly NOT to be mapped onto `product_class` (the curator rejected that framing). A *collection* = a NAMED saved Amazon search URL; unrestricted (may be a class, may be "top 20 holiday stuff"); **the common thread is that it yields ASINs**. Funnel: EasyParser SEARCH -> Wilson prefilter (`p=(mean-1)/4` + lower bound — must be a DIFFERENTLY NAMED score from the RealRank index) -> top ~20 -> free widget histograms -> rescore -> top ~10 -> **LLM bake-off** (stored rubric, cited evidence, order-shuffle stability check, output is a RECOMMENDATION a curator confirms) -> gold/silver/bronze -> top ~3 into RealRank. **Selection CREATES/UPDATES the product rows**; ASIN is the upsert identity, and a re-run must not clobber curator edits. EasyParser SEARCH takes no `url`, but Amazon's own query string maps ~1:1 (`k`->keyword, decoded `rh`->refinements, `s`->sort_by), so the saved URL stays the artifact and we translate at call time. **UI = the existing ACDV shell** (collection = the dish-equivalent): sortable+searchable index, detail with a Refresh that runs the extract into a results pane, **non-selected candidates KEPT** like the recipe cohort, metadata block, typography identical to the domain form, job-progress window, and category (against the **WS taxonomy**, reusing `equipment_match.classify_term`) as a display+sort dimension. See [[project_amazon_collection_selection]] / [[project_easyparser_migration]].

### Also parked, with notes
SEMrush advanced-filter `fld`/`cri` codes are guesses ([[project_semrush_filter_codes]]). Six call sites still bypass the LLM gateway — voice_agent, recipe_anchor/pipeline, extract/identity_card, extract/enrich_recipe, enrich/measurement/{recipe_pass,estimate} ([[project_llm_gateway]]). A reviews→purchase deep dive ([[project_review_purchase_research]]). And an observation worth acting on: **recipe ranking is pure publisher-authority proxy** — we judge provenance, never the artifact — so the products bake-off/medals pattern arguably belongs on recipes too.

### Restart notes
Static HTML/JS (products.html, reviews.html) live on next load. **`bcc_restart.bat` needed** for `POST /products/{id}/realrank` (a `_spawn_job_process` -> `_spawn_job_runner` rename fix; the endpoint 500s until then). `/reviews/find`, `/reviews/ingest-url` and `/realrank/approve` verified live post-restart. Jobs pick up Python changes automatically (fresh `python -m jobs` import).

## Session log — 2026-07-26/27 — reviews as jobs · ASIN identity from affiliate links · RealRank/RealStory split · the curate experiment · the revenue path (buy links + affiliate settings)

The commerce build. Started with a broken review grab, ended with a written brief whose every buy link earns us money.

### Review acquisition hardened (be78e05, 3c70595, 209c449, c7c16a4)
- **The ATK Dutch-oven grab reported "no products" — it had TRUNCATED.** 8192 max_tokens on a large roundup; the model was still emitting when it was cut, and a partial JSON parse fell through to an empty product list. Now 20000, products emitted FIRST (so a truncation loses prose, not picks), and an unclosed fence is reported as **TRUNCATION**, never as a JSONDecodeError. A silent zero is the failure mode to design against — it looks like "this page had no products."
- **Bookmarklet grabs are now tracked jobs** — logged, cancellable, visible in the monitor (whose width now matches the shell). A grab that fails leaves a record instead of a shrug.
- **Serious Eats award phrases** ("Best Overall", "Best Budget", "Also Great") decoded to our tiers, and the tier field constrained to the controlled vocabulary — the model was inventing tier names per publisher.
- **Job #618**: two bugs behind one error. The JSON error was the truncation above; underneath it, `find_asin` picked the **most-rated** match, so an **Amazon Basics** Dutch oven was scored as the Le Creuset. Brand match now required. (First fix derived brand "Le" from "Le Creuset" and matched "**Le**akproof"/"Ename**le**d" — tokens must be >=4 chars, and the real brand is threaded through rather than guessed from the title.)

### Product identity: the reviewers' own links are the answer (785e165, 7b4dc11, 2bd3233, 3394508)
Curator's insight: *"nearly all the reviews have affiliate links — can't we use those to determine if we have the right product?"* Yes, and it beats name-matching outright.
- **`asin_from` now URL-decodes first** — 46 of 50 ATK links carried the ASIN inside a percent-encoded `trx-hub.com?q=` wrapper and were invisible to a raw regex. Plus `asins_in` / `asins_near` / `consensus_asin` (the ASIN several reviewers independently linked).
- **`asin` is a first-class indexed column** on `review_products`, populated on ingest and backfilled by `recover_asins_via_redirects()`. Exposed as its own field in the reviews editor — the curator asked for it explicitly, and it is the join key to everything Amazon.
- **Variant FAMILY, not one ASIN**: `variant_asins()` returns the parent's children (Le Creuset: **61 ASINs**), so a reviewer who linked a different colour still matches. Colour and size do NOT split Amazon ratings — the family shares one histogram — so this is safe for identity *and* explains why the free widget's numbers agree across variants.
- **`intake/products/product_types.py`** — one shared type vocabulary. `_verify_asin` had rejected a **Staub cocotte** as not-a-Dutch-oven (and earlier accepted a **bread oven** as one). Synonym groups (dutch oven = french oven = cocotte = coquette; bread oven = cloche), `AMBIGUOUS_TERMS` that resolve to nothing, and **fail-open**: a conflict is only declared when both sides resolve to disjoint groups. A verifier that cannot tell must not veto. (One more wrinkle: the brand check compared the whole string `"staub (zwilling)"` against `"STAUB"` — leading-token compare now.)

### RealRank index: the small-sample collapse (e95bee7)
**13 five-star ratings scored 100.0.** Agresti-Coull variance is `promoters + detractors - nps^2`, which at unanimity is `1 + 0 - 1 = ZERO` — the confidence penalty vanished exactly where it was most needed. Padding (`z^2/2` spread across five bins) and `n_eff = n + z^2` fix it: 13x5-star **100.0 -> 72.5**, 9x5-star **88.0 -> 64.5**, and the well-sampled products are untouched (7,823 ratings stays 92.5; 144,696 stays 86.8). A scoring bug that only fires on thin data is the one that ships — every test product had thousands of ratings.

### RealRank / RealStory (eaf4334)
Split while exactly one product had a block, at the curator's instruction. **RealRank = the number** (score/basis/owner/computed_at, no approval — arithmetic isn't approved). **RealStory = the words** (verdict/prose/findings/coverage/files/approval). They can legitimately disagree, and conflating them made the approval semantics incoherent.

### `experiments/curate/` — the curator's ChatGPT loop, grounded (fde6329)
Explicitly **isolated from production** at the curator's request. Adapts a curation loop they'd been running by hand, with three changes they asked for: **text, not a spreadsheet** (`render.py` — three worksheets become three sections, plus an owner-evidence line and a sources line a workbook couldn't carry); **typical price, not sale price** as the ranking input (a discount is news, not a ranking signal — a daily sale scan for winners is the follow-up); and our own grounding/verification on top.
- Lessons re-learned the expensive way: **no doc cache** meant a re-run spent ~6 min re-fetching (curator: *"we are six minutes in… what's going on"*) — now cached under `experiments/curate/cache/`; and the **raw reply wasn't saved before parsing**, the exact lesson RealRank had already taught. Both fixed.

### The revenue path (02bad9a, 5755a27, 1ada42a, 64445d6, 6818bf9)
Curator: *"we need more buy-it links than just Amazon… this is important as this is our revenue source."*
- **`intake/products/buy_links.py`**. The trap it exists for: **69 of the 139 buy links we hold carry someone else's affiliate tag** (`tag=atkequipland-20`, Wirecutter's `/out/link/` redirector). Republishing those doesn't merely fail to earn — **it pays a competitor on every sale we generate**. A harvested URL is therefore IDENTITY ONLY; `clean_url()` strips every tracking parameter, and returns **""** for an opaque redirector it cannot unwrap. Dropping an offer costs one placement; mis-emitting one costs the revenue *and* funds a rival.
- **Multi-retailer** by design — `offers_from_reviews()` returns one offer per retailer, matched by **exact ASIN** (matching the 61-ASIN family gathered 48 "retailers" for a single pick). Le Creuset and Staub sell far better direct than on Amazon.
- **Codes applied at CLICK time, not storage time** (curator's call). Stored rows keep clean destinations; `amazon_affiliate_url()` mints the link at render/click. `tag` is the ONLY parameter Amazon requires — SiteStripe's `linkCode`/`linkId`/`language`/`ref_` are its own reporting residue — and **`ascsubtag`** (which SiteStripe omits) carries the placement, so the Associates report says which surface earned the sale.
- **Where the codes live: the system record, not `.env`, not code.** Not `.env` because they aren't secrets (they appear in every published link, and `.env` is for credentials); not code because they're per-instance business config — a self-hoster ships their own exactly as they ship their own `public_base_url`. Three settings under a new **Affiliate** section: `amazon_tracking_id` (mbg99-20), `amazon_store_id` (leaddogventur-20), `affiliate_subtag_enabled`. **SiteStripe exposes two ids and they are not interchangeable** — Store ID is the account, Tracking ID is a channel under it; we publish the Tracking ID because that's how Amazon reports *which property* earned a sale. Verified live in the System editor after restart.

### Loaf Pans — the whole new pipeline, end to end
Second category through, clean on the first run (Dutch ovens took three: a 32k truncation, then the cocotte rejection). 8/8 sources fetched, **15 picks** across 3 overall + 4 categories, 6 distinct ASINs. USA Pan 1140LF wins on four independent sources with 11,623 real ratings behind it; no identity rejections (the loaf pan / loaf tin / bread pan synonyms held); Williams Sonoma and OXO-direct links came through beside Amazon, all stripped of ATK's tag and re-minted with ours plus a per-placement subtag.

**Measured cost — one `claude-sonnet-5` call, 237s: 55,117 in / 17,496 out = $0.285** at intro pricing ($0.43 at standard $3/$15). External APIs (~8 SERP, ~7 unblocker fetches, ~10-20 Rainforest) add an estimated $0.14-0.24 — **~$0.45-0.55 per product category, all in**. Amazon Kitchen & Dining pays **4.50%**, so a single $21 USA Pan sale (~$0.95) covers roughly two category builds. Worth noting because it decides how freely we can re-run: the doc cache plus the saved raw reply mean a retry now costs the LLM call only.

### Restart notes
Static HTML/JS lives on next load. The Affiliate settings needed a restart (new `system_config` rows) and were verified live afterwards. Jobs pick up Python changes automatically.

### Parked / next
Daily sale scan for winners (typical vs current price is already modelled). EasyParser histogram fix — code stays wired. SEMrush `fld`/`cri` codes still guesses. Six LLM-gateway bypasses still open. Reviews->purchase deep dive. The recipe-side bake-off/medals idea from the last session is still unbuilt.

### Addendum — how the curate ranking actually works, and `edge_over_next`

Traced on the curator's question "where do the weights influence the scoring, if at all". Answer: **nowhere in code.**

- `prompt.py:DEFAULT_WEIGHTS` — the seven scoring attributes from the curator's original ChatGPT prompt (Cooking performance 25 / Durability 20 / Ease of use 15 / Versatility 15 / Value at typical price 15 / Capacity and shape 5 / Brand support 5) — came over intact and go into the prompt under RANKING WEIGHTS. They are **prose**. Nothing multiplies them; the only numeric check is that they total 1.0 (`verify.py:105`).
- **The model writes `place: 1/2/3` directly**, like any other field. Code only validates that the three places are 1-2-3, never why.
- **RealRank is attached afterward** (`verify.py:244`) as evidence and never reorders — which is why a #3 can outscore a #2 (loaf pans: #2 = 89.3, #3 = 90.7).

So: expert consensus ranks, owner arithmetic rides alongside, and the two are never reconciled. Defensible — it is the same split as `rank_score` vs `realrank.score` on the product record — but it was undocumented.

**Curator's call: explanation, not arithmetic.** Per-criterion numeric scoring was offered and **rejected** — *"it might ask more questions than it answers"*. Instead, new field **`edge_over_next`**: name the product immediately below you and the criterion from RANKING WEIGHTS that separated you; for the LAST place, name the strongest product that did NOT make the list and what kept it out; if two are genuinely close, say so rather than inventing a difference. Since ranking is judgment rather than arithmetic, the reasoning IS the audit trail — without it a rank can only be accepted, not challenged.

**Verified on Loaf Pans (re-run on cached docs — LLM call only, ~$0.29): asking the model to defend the cut line CHANGED the cut.** #3 went OXO → Lodge cast iron, with the exclusion argued explicitly ("TechGearLab found its thin walls gave less even heat distribution and it lacks handles or a lip"). #1 vs #2 is now crisp folded corners vs rounded molded ones, "the meaningful performance difference between the two co-winners."

### NEXT UP — categories must be staff-supplied ([[project_curate_staff_inputs]])
`run.py:29` hardcodes `DEFAULT_CATEGORIES` (two of the four are Dutch-oven concepts) and will silently apply them to any product class run without explicit ones — and on the Loaf Pans run **the assistant invented the four categories**. Categories decide what gets recommended at all, so nothing automated should be inventing them. Fix: delete the constant, make categories a REQUIRED input that fails loudly; CLI arg now, the collection record once that editor exists. Same shape applies to `DEFAULT_WEIGHTS`, lower stakes — the curator judged the seven universal, and `build_prompt()` already accepts `weights`.

## Session log — 2026-07-29/30 — the host is dying · and the app had no authentication

Started as "why didn't the PC come back up." Became the security session.

### The host (64e0440, ec4ab4f, e4a7e04, 5a807f2, c2fdee5, 50241ce)

MARLEY_SVR crashed 01:28 and sat dead ~12 hours. Bugcheck **`0x101 CLOCK_WATCHDOG_TIMEOUT`** — a CPU core stopped answering interrupts — while **awake and idle** (zero System events 00:30–01:35).

**The decisive fact: microcode `0x12F` is already loaded.** That is Intel's latest Vmin-shift mitigation, released for "systems continuously running for multiple days with low-activity, lightly-threaded workloads," which is exactly this host's duty cycle. The F.40 → F.45 flash on 6/23 was an explicit experiment with a stated pass condition — *does the idle-crash cadence stop?* — and it failed: four crashes since. Mitigation applied and did not hold ⇒ the silicon has already degraded. A newer BIOS cannot help.

**Why it never restarted:** `AutoReboot=1` is set, but `0x101` halts the cores, so the crash-dump-and-reset path itself wedges. **No Windows setting can fix this** — recovery has to come from outside the OS. Sleep is ruled out for good (S0ix unsupported on this board, AC standby + hibernate timeouts 0, empty wake history) — ignore the misleading `ConnectedStandbyInProgress=true` in Event 41.

**`kernel_power_check.bat` was lying, and against us.** Off by one on BOTH columns: it read `BugcheckParameter1` as the bugcheck code (so the two `0x101`s displayed as "8") and `SleepInProgress` as PowerButton (so the 4/14 event looked like a deliberate power-button hold and, under the script's own documented rule, would have been discarded). Fixed. Reading it correctly also **corrects the count downward — 11 spontaneous shutdowns, not 14** — because 4/14 and 5/21 do carry a real `PowerButtonTimestamp` and cannot honestly be claimed. The load-bearing figure is untouched: four spontaneous crashes after the microcode fix, none power-button.

**Warranty, settled from the receipt.** Bought at Best Buy **2023-11-21**, order `BBY01-806817898714`, $749.99. *Do not date this machine from the filesystem* — the `john` profile (2025-01-11) is a Windows REINSTALL, and dating from it wrongly put the purchase at 2025-01 for an hour. Coverage: HP 1yr expired 2024-11; **Best Buy Protection (up to 24 mo) expired 2025-11-21**, with no in-window-failure argument available since the documented crash history starts 2026-05-10; **Intel's Vmin extension runs 5 years → open to 2028-11-21**, the only live route. Routing is system-manufacturer-first (HP, expired) then Intel escalation citing their published "unsuccessful in prior RMAs" clause — a documented HP refusal is the *entry ticket*, not a dead end. Docs written: `docs/host-stability-and-watchdog.md`, `docs/warranty-claim-submissions.md`, `warranty-evidence/crash-evidence.txt`.

Applied: **min processor state 5% → 100% on AC**. Pending: Fast Startup off (needs admin), BIOS *After Power Loss = Power On*. Also written: `migration.txt` (runbook for moving to the new host) and `requirements-frozen.txt` — **there was no dependency manifest at all**; the venv was the only record of what the app needs, sitting on the machine that keeps dying.

### Collateral (b661032)

`recipes.sql.gz` was found **truncated** — the 03:00 backup missed its slot (host was dead), fired as a boot catch-up at 13:46 and was killed 15s in (`^C`, scheduled-task result `0xC000013A`). HEAD's committed copy and ADAM's newest were both fine, but a truncated dump sitting in the working tree looks exactly like a backup, and `recipes.db` is untracked. Repaired to 39.2 MB, verified, fresh ADAM copy. Gotcha recorded: **`bcc_backup_scheduled.bat` does not survive being invoked from Git Bash** — the venv `activate.bat` swallows the rest of the script and it still exits 0.

### Whisper, measured rather than assumed (d9639bb)

`/cook/listen` was `async def` calling `transcribe()` **inline on the event loop** — ~420 ms of CPU-bound work behind a global model lock, freezing *all 173 endpoints* for the duration of every utterance. Moved to `run_in_threadpool`.

Benchmarked on this host: **base.en 414–478 ms, tiny.en 213–236 ms**, and **flat across 1–3 s clips** because Whisper pads everything to a 30 s mel window. So `cook_stt.py`'s "1-2s" docstring is out of date, trimming utterances buys nothing, and only model size moves the number. Conclusion: **do not build a separate STT machine** — `tiny.en` routed by clip length gets most of a GPU's benefit for one line and zero dollars, and the remaining "next" latency is more likely browser VAD endpointing.

### The authentication hole (ac3daf1, e2e1ff1, 4315a5c, 17fc719, f93766e)

**`X-Self-User-Id: 0` alone returned a synthetic `owner`** carrying admin_ui / edit_master / delete_master / manage_users / configure_system. `auth.py` was candid that it trusted the client header and called that "fine for a private-app threat model" — but the app has been public on recipes.tbotb.com, and the access log shows continuous automated scanning (`/admin/.env`, `/admin/phpinfo.php`, `/admin/config.php`, `/xmlrpc.php` from several IPs). One header on a known URL was a complete admin bypass.

Fixed uid 0 behind a curator password (scrypt + HMAC token, **fail closed** — no password configured means no master, ever; an unconfigured install must not ship a guessable admin). Then found **the identical hole one number away**: user 5 carries `role='owner'`, so `X-Self-User-Id: 5` alone returned all ten permissions with no password.

> **The lesson worth keeping: the bug was never "uid 0 is special", it was "the header is trusted." Every privilege reachable from an unverified header is the same finding wearing a different number.**

So staff permissions now require the password for *every* account (identity preserved, role locked — `staff_locked` so the UI can offer an unlock rather than silently hiding admin), and then **per-user passwords** generalise it properly. The token binds the uid *into* the signature so it cannot be replayed as another account. Rollout has **no flag day**: a password being set is what enforces it, so accounts harden one at a time and nothing breaks while the migration is half done.

Also gated the four endpoints whose own TODOs said "gate before public exposure" — a condition that had already been true for months — and **`/extract-product` + `/extract-review`, which were reachable by any caller with no credential at all**.

**Per-user API keys** for the bookmarklets (`bcc_<uid>_<43 urlsafe>`): they run on a publisher's page with no session and no cookie, so every grab was anonymous and could not land in anyone's collection. The key authenticates identity and grants nothing by itself — what a bookmarklet can do follows from its owner's permissions, so there is no parallel scope system to keep in sync. SHA-256 rather than scrypt, deliberately: 256 bits of CSPRNG output is unguessable by construction and is checked on every request; slow hashing exists for low-entropy human secrets. A leaked bookmarklet is worth member access, because staff still requires the password.

Plus **401 vs 403** (a lapsed session was reporting as a permissions error, which is exactly what made an account whose record reads `owner` get told its role was `anonymous`), **sign out / lock admin** (there was a way in and no way out — the only exits were switching user or clearing localStorage by hand), and **per-item menu permissions** (an `editor` saw Users and System and got a 403 on click; an `author` saw no admin burger at all despite holding edit_master).

### The split, expressed in DNS (1d75944, e65ef9c, 5219415)

`bestcooksclub.com` and `recipes.tbotb.com` both served the **full app**, which is why putting Cloudflare Access on tbotb protected nothing at all — the identical admin surface answered on the other hostname with no login. A lock on one of two doors into the same room.

`input/pipeline/host_gate.py`: the public host serves an explicit customer allowlist and 404s everything else (404 not 403 — a public visitor should not learn that `/domains` is a real endpoint). Deny-by-default means a route nobody remembered to gate is not automatically public.

**Got the allowlist wrong twice** — first all of `/forms/*` (which put the entire admin application on the customer domain), then none of it (which killed the customer menu's own targets) — before landing on the right frame, which was the curator's: **two hamburger menus IS the split**, so the allowlist mirrors the `group: 'user'` entries in `NAV_ITEMS`. The recipe form is on both sides deliberately; what differs is privilege, not the file. Getting that backwards is what produced both wrong passes.

### Cloudflare Access — created, then deleted

Turned on, and it immediately broke the bookmarklet **and** "show top recipes" — both simply hung. The tell: **no log entry at all**, meaning the request never reached the app. Access was answering with a 302 to a login page that could never complete, because the Zero Trust org is seat-limited (one seat, one user listed). Deleted; `recipes.tbotb.com` back to 200.

> **General lesson: a hang with no log line means the request never arrived — look at the edge (tunnel, Access, DNS), not the code. A slow query would have appeared as a logged request with a long duration.**

Correct sequence is Access on the **admin host only, after** the host gate — then customers never see a Cloudflare login, only staff consume seats, and six is plenty.

### Decisions taken

**Ghost dropped for now** — the password dialog *is* the login, and Authlib social sign-in bolts onto the same `users` row later. **Cloudflare Access is for staff, not customers** — a seat per authenticated user and no self-serve signup make it wrong for a consumer product. **Not the NVIDIA DGX Spark**: it is ARM Linux. (The RTX Spark variant *is* Windows-on-ARM — I said otherwise and was wrong — but the risk simply moves to ARM64 Python wheels, and `sqlite-vec` / `ctranslate2` are exactly where that is thinnest.) Replacement should be x86 Ryzen with **16 GB VRAM** — a 5060 Ti 16GB beats a 5070 12GB for local models, and current pricing is ~$2,100, not the ~$1,200 first estimated. **Never Intel 13th/14th Gen again**; Best Buy still sells them. **Do not take Best Buy's $100 trade-in** — the Intel RMA yields a free CPU and the Envy becomes a warm spare, which is the redundancy this setup has never had.

### Parked / next

Recipes in the **admin** menu as the only master-editing path (mechanism undecided: open as Master via `?user_id=0` reusing `_recipes_table_for`, or a scope switch inside the form). The ~147 remaining ungated endpoints, unclassified. Consumer displays — until they exist the public host serves APIs and the cook view but no general UI. The Master login prompt in the recipe form is still a native `prompt()` now that `LibraryShell.passwordField` exists. `extractUrlInput` never got the shared URL icons (deliberately — it sits in the extract path). **And the 2026-07-27/28 session still has no log** — curated collections, the EasyParser histogram work, the affiliate programs table and cook-KB grounding, four commits' worth, written up nowhere.

### Addendum — everything after the log above was written

The session kept going. Sixteen more commits, and the two largest turned out to be
things the curator saw and I had missed.

**Time, standardised on UTC (7f472e0, 195921a, 3a88967).** A freshly minted key
displayed as "237 min ago". SQLite's `datetime('now')` returns
`YYYY-MM-DD HH:MM:SS` — UTC with nothing saying so — and JS parses that shape as
LOCAL, which on EDT put a just-written stamp four hours in the FUTURE; `fmtDate`'s
"< 1 hour" branch is also true for negatives, so it rendered the negative minute
count. The curator's response was the right one: *"we need to look at the WHOLE
SYSTEM and standardize on UTC."* The audit found **three conventions in one
database** — `jobs.created_at` with `+00:00`, `users.created_at` as ISO with no
offset, and the new columns as bare `datetime('now')`. Now: 14 `datetime.utcnow()`
(UTC but NAIVE, so `.isoformat()` drops the offset) → `datetime.now(timezone.utc)`;
11 SQL `datetime('now')` → `strftime('%Y-%m-%dT%H:%M:%SZ','now')`. 102
timezone-aware call sites, 0 naive, 0 unmarked. **The rule: store UTC with an
explicit offset, convert to local only at display.** Verified safe before
sweeping — every `fromisoformat` site already coerces naive→UTC before comparing
(`extract_cache`, `url_scoring`) or uses `.date()` (`domains_lib`) — which is why
this was one commit and not a rollback. Log lines keep the local wall clock but
gained the offset (`[2026-07-30 13:28:43-0400]`), because correlating a log entry
against the UTC row it wrote should not need mental arithmetic. `astimezone()` is
the load-bearing detail: `datetime.now()` is naive, so `%z` renders empty — the
exact shape that started this.

**The front door (ec0466b, 084b834, 693695a, 17aa508).** There was no home page —
`/` returned the API health JSON and the only way in was to navigate straight to
`/forms/users.html`, which is the admin user editor and correctly 404s on the
customer host. So **a customer had no way to sign in at all**, and two supporting
gaps made it worse: `/auth/login` was never allowlisted (allowed `/auth/me` and
forgot the endpoint that authenticates), and `POST /users` — which accepts `role`
from the payload — was **ungated**, so an anonymous caller on the admin host could
mint themselves an owner row. Now `/` serves `forms/home.html`, health moved to
`/healthz`, and `POST /auth/signup` exists as the ONLY public account-creation
path: deliberately not a mode of `POST /users`, because a public endpoint must be
*structurally* unable to set a role — `'member'`/`'free'` are written into the
INSERT itself. Which flavour of home you get is decided by the HOSTNAME, the same
split `host_gate.py` enforces, so page and server agree by construction. Admin
home carries the four signals that had to be dug out by hand all session: did the
backup VERIFY, which jobs failed, which accounts are still spoofable, and what the
month costs.

**THE BOOKMARKLET REWORK — the curator's call, and the best decision of the day
(43d3f16, b8d606b).** Testing found the identity check did nothing: signed in as
Sara, grabbed with #5's bookmarklet, and it sailed through. Two reasons. The
installed bookmarklet predated key-baking so it carried no key at all (confirmed:
every key read NEVER USED), and key-less grabs were waved through for backward
compatibility. But the real finding was underneath, and it corrects a claim made
earlier in this session: **the key never determined ownership.** `user_id` comes
from the form's payload at save time — the key only authenticated the staging
call. So the curator reasoned it out: *"there's only one bookmarklet and it is
universal.. it will save correctly to whoever is signed in OR if no one is it will
prompt."* Correct, and it deletes the whole problem rather than policing it. The
recipe bookmarklet now carries **no identity**: one install, any user, any number
of devices, nothing to mismatch. Not-signed-in is the only remaining question and
`LibraryShell.signInDialog` answers it — a modal whose `verify` callback made
sign-in a loop rather than one shot, with Cancel always present.

Tracing callers to place the keys correctly turned up **a live bug**: gating
`/extract-review` this morning broke the review grabber, which posts cross-origin
and acts immediately with no form round-trip to supply a session. It had been
403ing silently since. The product grabber was fine — it stages and hands off to
`products.html`, which calls `/extract-product` same-origin. Worth the check
rather than assuming the two admin grabbers were symmetrical; they were not.

**Per-device keys (43d3f16), and knowing when to stop.** One key per account meant
generating one for a phone silently killed the laptop's, with no way to re-display
the old one. `user_api_keys` (label, created_at, last_used_at, last_seen_ua) fixes
that, migrating the old column across as "Original bookmarklet"; revocation is
scoped by user_id as well as key id so guessing a number gets nothing. **Device
identity is a label the user types** — there is no honest automatic answer in a
browser — with the user-agent recorded on first use as a recognition hint. It now
applies only to the review grabber, which is the curator's own machine. A
timestamp-in-the-bookmarklet idea was raised and dropped: regenerating a key
already invalidates old installs, so it was a second lock on the same door. The
useful instinct underneath was expiry, which stays unbuilt.

**Loader versioning.** The payload is cache-busted on every click, so logic ships
without a re-install — *"No re-install ever"* — but the one-liner in the bookmarks
bar is frozen at install time, and it changed twice today. `__bccLoaderV=3` with a
non-blocking warning when the payload sees something older. Confirmed working on a
real stale install within the hour.

**Smaller, and the pattern in them.** Password minimum 12 → 8 at every point that
enforces or describes it. Promote-to-master removed from the form and then the
code — a second route into `master_recipes`, and the endpoint carried its own
stale "any caller can promote" TODO that the morning's `_require_perm` work had
already closed. The nav burger was pinned to the viewport edge rather than the
content column on every fallback page (columns run 560–1200px, so measuring
`.wrap` was the only thing that fits all). A browser hitting a blocked page got
bare `{"detail":"Not Found"}` — now a styled page with a way home, still identical
for a blocked admin route and a genuinely missing one. And the identity badge did
not update after signing in mid-page: it hydrates once and `fetchAuth()` caches
the promise, so re-rendering alone would have redisplayed the stale answer —
clearing the cache is the load-bearing half.

**Three corrections worth keeping.** My test scripts leaked a password and key
onto Ann's account while claiming to roll back — `set_user_password()` and
`resolve_api_key()` both `commit()` internally. Cleared. `FileResponse` and
`HTMLResponse` were each used without being imported; `compileall` does not catch
that and both would have been NameErrors on first request — now verified by
resolving the attribute on the imported module rather than trusting the compile.
And the staged-grab check first returned the *name* of the account that made the
grab, which would have told anyone holding a stray bookmarklet whose it was; the
curator caught it — *"we shouldn't be identifying other users!!"* — and the
comparison moved server-side to return a boolean.

**Still open.** Ann is the last account without a password. `.env.bak` holds the
previous master password hash and should be deleted. Admin-menu Recipes as the
master-editing path is still undecided (open as Master via `?user_id=0`, or a
scope switch in the form). Key expiry, and rate-limiting anonymous
`/stage-markdown`, are both deliberate not-yets. And the 2026-07-27/28 session log
is still missing.

### Closing state — 2026-07-30 evening

**All six accounts have passwords.** `X-Self-User-Id` alone no longer resolves for
anybody. That header opened the day as a complete admin bypass on a public
hostname under active automated scanning; it ends the day inert. Everything else
in this log follows from pulling that thread.

Dead code from the bookmarklet rework removed (`d3cc2cf`): `_staged_by()`, the
`staged_by` entry field and the `staged_by_you` comparison were still executing on
every staged grab, resolving a key the universal bookmarklet no longer sends and
computing an answer the form had stopped reading. `/users` also stopped reporting
`has_api_key` and the old key timestamps — device keys live in `user_api_keys` and
the editor reads `/users/{id}/api-keys`. The migration path in `auth.py` stays:
it reads `users.api_key_hash` to carry pre-existing keys across on first run, and
must survive until every install has migrated.

**Restarts needed nothing beyond the schema auto-migrations.** `ensure_api_keys_table`,
`ensure_password_column` and `ensure_api_key_columns` all run at init, so the
device-key table and its migration happen on first boot with no manual step.

#### Where to pick up

Decided but unbuilt:
- **Admin-menu Recipes** as the only master-editing path — mechanism still open:
  open as Master via `?user_id=0` reusing `_recipes_table_for`, or a scope switch
  inside the form. `author` is now `edit_master` + `own_recipes`, so gating the
  entry on `edit_master` still admits them.
- **Cloudflare Access on `recipes.tbotb.com` only**, once the Zero Trust seat
  count is sorted (it showed 1 seat, one user listed). Now worth doing: the host
  gate means the admin surface only answers on that hostname, so Access finally
  protects something. Customers never see a Cloudflare login and only staff
  consume seats.
- **Consumer displays.** Until they exist the public host serves APIs, the cook
  view and the recipe form, but no browsing surface.

Deliberate not-yets, with the reasoning so they are not re-litigated:
- **Key expiry.** Would bound a lost device without anyone remembering to revoke.
  Only applies to the review grabber now, which is the curator's own machine, so
  the value dropped sharply once the recipe bookmarklet went keyless.
- **Rate-limiting anonymous `/stage-markdown`.** It accepts anonymous grabs
  permanently by design — staged content is transient and worthless until someone
  signs in and saves it, so the exposure is junk in memory, not a credential.
- **The ~147 ungated endpoints**, still unclassified. The host gate is the
  perimeter; `_require_perm` is the control. Both layers exist, but only the
  endpoints reached this session have been walked.
- **`prompt()` in the recipe form** for the Master password, now that
  `LibraryShell.signInDialog` exists.
- **The 2026-07-27/28 session log**, still missing — curated collections, the
  EasyParser histogram work, the affiliate programs table, cook-KB grounding.

Housekeeping: `.env.bak` holds the previous master password hash and should be
deleted now the current one is proven. `recipes.sql.gz` carries the morning's
scheduled refresh, uncommitted.

The host is still dying. Intel's window runs to 2028-11-21 and the evidence is
assembled in `warranty-evidence/crash-evidence.txt`; `migration.txt` is the
runbook for the new machine when it arrives.

## Session log — 2026-07-30 (later) — the native password prompt goes · `.env` destroyed and restored · BCC can send mail

Three threads. The middle one was not on the agenda and is the reason to read this entry.

### `prompt()` → `signInDialog`

The state note said the native prompt was in the recipe form. It was not — the recipe
form's staged-grab path already used `LibraryShell.signInDialog`. The real one was
`users.html` `login(uid)`, the switcher that *lands* you in the recipe form, which asked
for a password with `prompt()`: clear text on screen, no password manager, and errors
with nowhere to go.

`signInDialog` gained a **known-account mode** (`userId`). The switcher already knows
which row was clicked, so an email box there would let you click one account and sign in
as another; with `userId` the email field is absent and the login posts
`{user_id, password}`. `/auth/login` already accepted either key — no server change.
Also `current-password` autocomplete (it was announcing `new-password`, so managers
offered to *generate* rather than fill), an overridable mismatch message so the
bookmarklet's "doesn't match this bookmarklet" wording stays in its own flow, and a
`placeholder` option so a login stops reading "At least 1 characters".

Verified in-browser: known-account mode blocks an empty submit **client-side** (0 server
calls, so no throttle counter burned), Cancel resolves `{ok:false, cancelled:true}` and
tears down the overlay, and the bookmarklet's email mode is unregressed. Master (uid 0)
is untouched — there is no user 0 row, so `loginAsMaster()` → `login(0)` never reaches the
password branch and still uses `#masterPw` + `/auth/master`. Cache-buster `?v=20260730k`
across 22 pages.

### `.env` was destroyed, and nearly silently

Found while checking which mail libraries were installed: **`.env` was 6 bytes containing
the literal word `done`** (CRLF), mtime 16:56. All 22 keys — Anthropic, OpenAI, Moz,
SEMrush, ScaleSERP, Rainforest, EasyParser, the unblocker proxy credentials, **and
`BCC_MASTER_PASSWORD` + `BCC_MASTER_TOKEN_SECRET`** — gone from disk. The running server
still held them in memory, so nothing had failed; it would have failed on the next
restart, on a host that hard-hangs weekly.

**It survived by ten minutes.** `backup_db.py` uses `shutil.copy2`, which preserves the
source mtime — so `Z:\Backups\recipes-db\env.backup` carrying a Jul 29 16:33 stamp proves
`.env` was still intact when today's 16:46 backup ran. Restored, byte-for-byte identical,
verified to parse. The `.env`-to-ADAM commit (951883f) was written *that morning*; without
it this would have been re-issuing eight vendors' credentials one at a time.

**Cause never identified.** CRLF means a Windows-native writer (PowerShell/cmd redirect or
an editor save), not Git Bash. Neither shell history targets it. Same family as
[[feedback_stray_keystroke_corruption]] except the whole file was replaced rather than
appended to. Repo history was checked and is intact — the `git filter-repo --path .env`
lines in PSReadline history belong to the old `visual_recipe_extractor` project.

### BCC can send mail

Curator has an **SMTP2GO** account with **175k/mo**, for newsletters, recipe digests, and
alerts on new recipes/dishes/domains.

**Vendor decision: one, SMTP2GO, for everything.** 175k already paid for dwarfs anything
this app sends at six users; SES's $0.10/1k or Postmark's deliverability edge would be a
second bill and a second integration to buy capacity already owned. Also corrected a
standing assumption: this host is **Verizon Business with 5 static IPs**, not residential
— no ISP port blocking, so the alt-port/REST-API contingency was moot. New memory
[[project_network_infra]].

**The DNS audit found the record that looked right and was not.** `_dmarc` read exactly
`"p=none"` with **no `v=DMARC1;` tag**, which receivers discard entirely — so a DMARC row
was present in Cloudflare and there was no DMARC policy at all. There was also no SPF
record anywhere on the zone. Both fixed and verified live. The SPF include is
`spf.smtp2go.com` (**.com**, read out of `return.smtp2go.net`'s own record — the recalled
`.net` was wrong). Now: DMARC valid, DKIM selector `s112864` serving a live RSA key,
return-path subdomain-aligned, every SMTP2GO record correctly DNS-only rather than
orange-clouded.

**Then the SPF record had to be merged, and that one was my error.** Cloudflare **Email
Routing** was already on this zone (those `route1/2/3.mx.cloudflare.net` MX records), and
it needs its own SPF include. A domain may have **exactly one** SPF record, so the
SMTP2GO-only record I supplied occupied the slot and Email Routing reported *DNS records:
Not configured* — with its rules showing **Active** while the service sat **Disabled**,
which is why the first `dmarc@` test vanished. I had seen those MX records in the first
screenshot and identified them, and still handed over an SPF record that ignored them.
Correct value, both senders in one record, 2 of 10 lookups:

    v=spf1 include:spf.smtp2go.com include:_spf.mx.cloudflare.net ~all

A trap worth remembering: Cloudflare's DNS-check page offers to *add records
automatically*, which would have published a SECOND `v=spf1` record and broken SPF for
both senders — worse than having none.

> **Both new records took two attempts — one lost its leading `v`, the other the final `l`
> of `~all`. Cloudflare pre-fills the Content field on edit, so pasting into a partial
> selection eats the edges. `Ctrl+A` first, and verify the first and last characters after
> saving. This is almost certainly how the original DMARC record lost its `v=DMARC1;`.**

**`input/pipeline/mailer.py`** — one `send_mail()` chokepoint, the `serp_search()` pattern,
so a vendor swap is a config edit. **Two streams on two SMTP users**: bulk complaints must
not be able to poison the reputation verification and reset mail depend on. Implicit TLS on
465 (STARTTLS as the fallback path), `Message-ID` on our own domain rather than the relay's,
UTC `Date`, and `List-Unsubscribe` + `List-Unsubscribe-Post` on **bulk only** — an
unsubscribe link on a password reset is how someone opts out of being able to log in.
Delivery failures return a result dict rather than raising, because a caller mid-signup has
to tell the user something. Request-path callers **must** use `run_in_threadpool` (the
`/cook/listen` lesson). New `system_config` category **Mail** (kill switch, two
from-addresses, display name, daily cap) — credentials stayed in `.env`, these did not, per
the rule set with the affiliate codes.

**Verified live.** First attempt failed `550 From header sender domain not verified` — but
at the DATA stage, which proved auth, TLS and the outbound path all worked. After verifying
the sender domain in SMTP2GO (the same `112864` records already in the zone), **both
streams delivered to the Gmail inbox on a first-ever send from a cold domain**, From
rendering as `Best Cooks Club <noreply@bestcooksclub.com>`.

**And the DMARC reporting loop closes.** After the SPF merge, a message to
`dmarc@bestcooksclub.com` reached `john@johnlandry.com` at Outlook through Cloudflare
Email Routing. That is the demanding case: a **forwarded** message relays from Cloudflare's
IP rather than SMTP2GO's, so **SPF necessarily fails** at the destination — Microsoft
accepting it into the inbox means it authenticated on the **DKIM** leg, which is the
alignment that could not be confirmed from a header. Forwarded delivery into Microsoft on
a domain's first day of sending is as good a signal as this gets, and the aggregate
reports now have somewhere to land.

### Decided, not built

- **Verification + password reset** on a `v{uid}:{email}:{exp}` HMAC — deliberately NOT
  `mint_user_token`, whose output *is* a 30-day session: mailing one hands a session to
  anyone who reads the mailbox, a forward, or a proxy log. Binding the email into the
  signature also stops a token minted for the old address verifying a changed one. Reset
  rides the identical rails; building verify alone means building the mail half twice.
- **The alerts vs newsletters split.** "Alerts when a dish or publisher I follow gets a new
  recipe" is event-driven, per-subscriber and preference-filtered — it must come from BCC,
  the only thing that knows what you follow, and there is no `follows`/`subscriptions`
  table yet (nearest is `collection_members`). Newsletters and digests are curated
  campaigns. One relay, two producers. Build the alert half; hand campaigns to **Listmonk**
  later (self-hosted, free, sends through the SMTP2GO plan) rather than hand-rolling a list
  manager.
- **Sending subdomains.** The curator verified the **root** domain, so both streams
  currently share one reputation. Splitting to `mail.`/`news.` is cheap now and expensive
  after 100k newsletters have built a complaint history the transactional stream inherits.

### Open

- **SMTP passwords still need rotating** — both users currently share one human-chosen
  value that was pasted into a chat transcript. A leaked SMTP credential on an
  authenticated domain is worse than a typical key leak: it sends as us with **valid
  DKIM**, so the phishing passes every check our real mail passes. `.env` must be updated
  in the same pass or sending breaks. Note `backup_db.py` copies `.env` to ADAM in
  plaintext by design, so it lands there too.
- **Tighten DMARC past `p=none`** once a few weeks of aggregate reports come back clean.
  The reports are the entire point of `p=none`; publishing it without a working `rua` is a
  policy that observes nothing. That path is now proven end to end.
- **Sending subdomains, and the `~all` → `-all` tightening**, both deliberately deferred
  until there is sending history to reason from.
- **Click tracking rewrites URLs.** `link.bestcooksclub.com` → `track.smtp2go.net` is live
  with an SSL cert. Confirm Amazon `tag` and `ascsubtag` survive the rewrite before the
  first digest, or newsletter sales pay nobody ([[project_buy_links_revenue]]).

## Session log — 2026-07-30 (night) — email verification · the blend becomes SQL · a 51% scoring bug · and the bookmarklet auth thread

Continuation of the same day. Restarted and verified live at the end.

### Email verification (16bb58c)

`users.email_verified` / `email_verified_at`, auto-migrated. `POST /auth/send-verification`
(self or `manage_users`, per-IP throttled — an endpoint that sends mail is a way to use us
to spam a third party) and `GET /auth/verify?token=…`, which returns a **page** because it
is clicked from a mail client. Signup now sends the confirmation but does **not** fail if
mail is down: the account exists and they are already signed in, so an outage should cost a
confirmation they can re-request, not the account. Users editor gained an Email block
(claimed vs proven) with a resend button and a status pill.

> **The token is NOT a session token, and that is the whole design.** `mint_user_token`
> returns a 30-day credential; mailing one hands a logged-in account to anyone who reads the
> mailbox, a forwarded copy, a proxy log or a `Referer`. Emailed links use
> `auth.mint_purpose_token(purpose, user_id, bound, ttl)`, signed over a payload binding
> **purpose** (a verify token cannot be replayed as a reset), **user_id** (cannot be replayed
> as another account) and **bound** — a value re-read from the DB at redemption. For
> verification `bound` is the current email, so a token minted for an old address dies when
> the address changes. For reset it will be the password hash, which kills the link the
> moment the password changes — no revocation table. Verified: valid / wrong-purpose /
> wrong-user / changed-email / tampered / expired / garbage all behave, and the two token
> types are **not interchangeable in either direction**.

Malformed, expired and unknown-user all return the SAME message so a stranger cannot learn
whether a token was ever real. Both paths are allowlisted in `host_gate` — anything in
customer email must be, or the mail is fine and the link is dead on arrival. Same reason
`customer_base_url` exists rather than reusing `public_base_url`, which is the admin host.
**Nothing enforces verification yet**: six accounts predate mail and a flag day would lock
them out of a public site. `docs/email-setup.md` written — account, DNS, code, and the traps.

### The blend becomes queryable (581c9fb)

Curator: *"we need the data to easily do this."* A plain corpus question — top 200 by blended
score, grouped by domain — needed all 3,451 rows loaded into Python. Now VIRTUAL generated
columns on **both** recipe tables, beside the existing `dish_key`/`publisher_key`:
`ou_score`, `domain_authority`, `page_authority`, `power`, `source_host`, `recipe_score`,
plus `stored_*` for audit. `source_host` is parsed in SQL because **`_scoring.rootDomain`
is not the host** — it returns registry suffixes like `co.uk`. The blend itself is
deliberately not a column (a cohort percentile needs a window function); `PERCENT_RANK()`
over these reproduces the Python ranking exactly in ~170ms. Open question stays **scope** —
a stored blend must declare its cohort, and `fieldScope` is empty on every row.

### The zero-power bug — 51% of the corpus (4fc0dbd)

`_scoring.power` was **0.0 on 1,778 master rows whose DA and PA were both real**, and the
zero propagated into `powerPercentile`. Anything ranked on the stored percentiles pinned
half the corpus to the floor of the power dimension — at weight 30, up to 0.30 of blend
score wrongly deducted.

**My first hypothesis (a race with Moz scoring) was wrong.** The data corrected it: EVERY
affected row has `fieldN: 0` and no row with a real cohort is affected. `power` and the
percentiles come from the BATCH ENTRY, whose Moz call returned 0/0 for small sites, while
DA/PA on the same row were written from a later successful scoring.

Fix in `process_batch.pre_scored_from_entry`: `power` is DERIVED as da+pa, and the
percentiles and field block are OMITTED when the cohort is degenerate. **A value we could
not compute must be absent, never a number that reads as "measured, and it's nothing."**
Backfill (`scripts/backfill_scoring_power.py`, dry-run first, DB copied): 1,775 master +
**106 user** recipes — it was not master-only. Cohort keys removed rather than zeroed.
0 rows remain, `quick_check ok`, ranking unchanged (correct — the derived column never read
the corrupt value). The 63 still differing are the **paid-PA calibration**, not corruption.

### The concentration report

731 publishers in the corpus; the top 200 comes from **34**, three of them supplying a third.
**Healthline and WebMD hold exactly one recipe each — and both went straight into the top
200**, above every page from Serious Eats (2 of 27), Bon Appétit (1 of 50) and Food Network.
Measured properly at the curator's prompt: the corpus is the long tail (HHI 80, 63% of
publishers holding one recipe); the ranking **truncates** it rather than preserving it —
95.3% of publishers never appear, HHI rises 8× to 667, and the surviving tail carries only
6% of the mass. Gini *falls* (0.689 → 0.553) while HHI rises, because selection already
removed everyone who would have made it unequal. Sharpest evidence yet for
[[project_domain_scoring]]: we rank provenance, never the artifact.

### The bookmarklet auth thread — the curator's catch (0f144c6, fe77e8c, 426d310, 369db1d)

Curator: *"the check needs to be at recipe load, not save… we already paid for the
processing."* Right, and it uncovered three separate bugs.

- **The paid endpoints had NO auth at all.** `/extract-from-url`, `/extract-from-markdown`,
  `/extract-from-image`, `/extract-from-pdf`, `/enrich-recipe` — all reachable anonymously
  and all allowlisted on the public host. Anyone could POST in a loop and burn LLM spend.
  **A client-side check never protected this**; it only decides what our own UI does. Same
  shape as the header bypass: every paid operation reachable without a credential is that
  finding wearing a different number. All five now require `own_recipes` (held by every role
  including member). `/stage-markdown` stays anonymous by design.
- **The client gate was a localStorage sniff.** `app:self_user_id` outlives its session
  token, so a lapsed session passed, the extraction ran, and the 401 arrived at SAVE. Now
  asks the server via `/auth/me`.
- **A mid-flow sign-in filed the grab under user 1.** `#user_id` decides ownership at save
  and is filled by `restoreSidebarState()` at PAGE LOAD — before the login existed. A browser
  that had never signed in kept the hardcoded `1`, so the user signed in correctly as
  themselves and the recipe landed in someone else's collection. Silently, because the store
  control does show a 1 — just one nobody chose.
- **Then the ordering, properly.** The popup is opened synchronously at click time, before a
  node of the page is read, so it now goes to the form immediately (`?awaiting=1`) and
  resolves identity **in parallel** with the extraction and staging. The token is delivered
  afterwards as a URL **fragment**: `hashchange` fires without reloading, so it cannot
  destroy a login dialog someone is typing into — a real navigation would. Cross-origin we
  may set `location.href` but not read it, hence rebuilding the identical URL. Verified live
  with an anonymous session: dialog on screen before any token exists; fragment delivered
  mid-dialog did not reload (instance canary survived), fired `hashchange`, left the dialog
  open, token read back intact. Also stopped a **double paid extraction** the change would
  have caused — the `?url=`-only IIFE now stands down on `awaiting` as it already did on
  `staged`. Loader untouched, so no re-install.

### `bcc_restart.bat` was corrupted too

Second file damaged today after `.env`: `localhost:%` had been cut out of the "Waiting for
http://…" echo and left stranded on its own line as `echo.localhost:%`. Cosmetic — both are
echo statements, the restart worked — but it is the same signature, and next time it may
land somewhere load-bearing. Repaired.

### Verified after the restart

`/auth/verify` and `/auth/send-verification` exist (400, not 404). `/extract-from-url`
returns **401** to an anonymous caller. `/stage-markdown` still 200s anonymously.

### Open

- **Rotate both SMTP passwords** — still one human-chosen value shared by two users, pasted
  into a chat transcript. `.env` must change in the same pass or sending breaks.
- **DMARC reports** should now arrive at `john@johnlandry.com`; they will confirm the DKIM
  alignment leg and are the basis for ever tightening past `p=none`.
- **Password reset** — same rails, `bound` = the password hash. Unbuilt.
- **Verification is not enforced** anywhere; needs a grandfathering decision.
- **A pass over endpoints that SPEND MONEY** — narrower and more valuable than auditing all
  ~147 ungated ones, and the second real hole today came out of that set.
- **A per-publisher cap at selection**, and the click-tracking check (`link.bestcooksclub.com`
  rewrites URLs — confirm Amazon `tag`/`ascsubtag` survive before the first digest).
- **`.env` still has no explanation.** Overwritten with the word `done` at 16:56, restored
  from a backup ten minutes old. If it happens a third time it is a pattern, not an accident.

## Session log — 2026-07-31 — the review grabber loses its key · url→page · one style, one header · and three causes wearing one symptom

Started as "convert the review grabber", became a UI/auth session. All shipped and verified live.

### The review grabber stages and hands off (c23a6f0)

Curator: *"shouldn't all the bookmarklets run the same way"* — and then the sharper cut, *"all
other bookmarklets operate on admin level forms."* Right grouping: ONE customer bookmarklet
(recipe) and TWO admin ones (product, review), and the two admin ones didn't match each other.
`host_gate.py:110` already said so in a comment — *"The customer bookmarklet only. /extract-product
and /extract-review are curator tools."*

**The mechanism that settles it:** a bookmarklet runs on the PUBLISHER's origin, and the session is
`localStorage['app:session_token']` + `X-Self-User-Id` on OURS. localStorage is per-origin, so a
bookmarklet can never carry your session — a property of the browser, not an omission. That leaves
exactly two designs: hand off to a page that has the session, or bake a second credential. Recipe
and product hand off. Review baked a device key, and its own comment explained the symptom ("no
form round-trip afterwards") rather than the cause: it chose to spend BEFORE reaching one.
`/extract-review` enqueues a ~30s LLM job, measured at ~$0.29 on a large roundup, authorised by a
bearer token sitting in a bookmarks bar with no human in front of an editor.

Now it POSTs `/stage-markdown` (anonymous by design — staged content is worthless until someone
signs in and spends) and navigates to `reviews.html?staged=<token>`, which decodes with the
curator's own session. A 401 there opens `LibraryShell.signInDialog` and retries rather than sending
you back to re-grab a page that may be paywalled. Also forward-compatible with the parked Cloudflare
Access decision: Access can challenge a human who NAVIGATES, and answers a cross-origin XHR with a
302 to a login that never completes — the silent hang from 07-29.

No re-install (payload is cache-busted; a stale loader setting `__reviewBookmarkletKey` is ignored).
**`user_api_keys` now has no caller** — table, endpoints and UI left intact per no-silent-removal;
retiring them is a separate decision. **Every other `/reviews/*` endpoint is ungated**, including
`/reviews/find` (a SERP call) and `/reviews/ingest-url` (unblocker + LLM) — the tightest remaining
cluster for the spend-endpoint pass.

### url→page, after two wrong turns (f296df7, 1c5d2f3, d5d10fe)

`home.html` decided its own flavour in JS with a hardcoded hostname regex, and the two lists had
drifted: the page counted `bestcooks.club` and every `*.bestcooksclub.com` subdomain as customer
while `host_gate` counted only the apex and `www`. So a customer front door could render over the
full admin surface, and `BCC_PUBLIC_HOSTS` — the override a self-hoster needs — was invisible to the
page.

I fixed it by adding `is_public_host` to `/branding`, then had to add `cache: 'no-store'` because a
copy predating the field sat in the browser cache and silently rendered the customer home on the
admin host. Curator: *"youre overcomplicating this.. it should be straightforward and simple... url
to page."* Correct. Three layers answering a question the URL had already answered.

`GET /` now reads the Host header and serves `home.html` or `admin_home.html`. That is the whole
mechanism; `/branding` is byte-identical to before. Two files rather than one branching file, per
the clone+specialize preference. Then one edit added `bestcooks.club` + `www.` to
`_DEFAULT_PUBLIC_HOSTS` — and worth recording which side had been right: the page's old regex DID
count it as customer, so the page reflected intent and the gate hadn't caught up.

### One style, one header (bbfdb49, 03306b7, 342a7af)

Curator: *"the look should be the same across the whole system so why am i differing pallettes"* and
*"i want a 'style' that doesn't change page to page.. and that's easy to change."*

**It already existed.** `tokens.css` is 39 lines and its own header has always said *"No page should
define these tokens itself anymore."* Eight pages had simply never opted in — six carrying a
copy-pasted `--accent:#b8602a` that isn't even the canonical clay (`#9b4a22`), so the "same" brown
was two browns. Two more (product_install teal, review_install plum) were deliberate; that
distinction now lives in their admin chip instead of the whole page.

Ten pages now load `library-shell.css` then `tokens.css` AFTER their own `<style>` (tokens last, as
its header instructs) and carry the shared `.app-header`. Page-specific tokens tokens.css doesn't own
(`--warn`/`--ok`/`--info`) were preserved. Five pages had a duplicate `library-shell.css` link,
deduped. `nav.css` — which I had created an hour earlier to dedupe two pages — deleted, because
`library-shell.css` already had those rules for every page that loads it.

Two shell bugs the rollout exposed, both fixed once rather than per page: **`initNav` never branded
the header** (the `applyBranding` call lives inside the SIDEBAR `init()`, so only sidebar pages
filled their `<h1>` — everything else showed an empty bar with a burger floating in it); and both
home pages **return early when signed out**, before `initNav`, so they brand the header themselves.
Verified in the browser, not just the markup.

The contract for new pages is now at the top of `library-shell.js` — the file every page loads —
with the two nevers (no page-local `:root` palette, no pasted nav CSS) and the drift that justifies
each. New memory [[feedback_page_shell_contract]].

### Sign out, and the account you can't leave (ce6a30e)

`initIdentityBadge()` is called only in `initNav`'s header branch, so **ten header-less pages had no
identity display and no way out**. Sign out now appends to BOTH burgers and names the account's
email — the account you need to leave is exactly the one whose menu you're stuck in. Also a real
bug: `signOut()` redirected to `/forms/users.html`, an admin page that **404s on the customer
host**, so signing out of bestcooksclub.com landed on not-found. Goes to `/` now.

### THREE CAUSES, ONE SYMPTOM — the diagnostic lesson

"recipes.tbotb.com shows the customer menu" was true three times for three unrelated reasons, and I
chased them in the worst order:

1. **The interim window.** My own `/branding` version, pre-restart, falling back to customer.
2. **The wrong account.** There are TWO users named John Landry — **uid 1 is a `member`
   (own_recipes only)**, uid 5 is "John Landry (Official)", the owner. Signed in as uid 1 the admin
   burger correctly stays hidden, and the page says "Signed in as John Landry" either way. A
   wrong-account problem reads as a broken page.
3. **A genuine bug (69a1e6d).** As uid 5, "unlock admin" was dead: the password was accepted,
   `/auth/master` minted a token, the browser stored it and reloaded — and the fetch patch attached
   `X-Master-Token` only when `uid === '0'`, so it never left the browser. `_resolve_caller` keys on
   the token alone (`if role != "member" and not verify_master_token(...)`), no uid anywhere, and
   `unlockAdmin`'s own comment already said the token isn't bound to a user_id. The client was the
   only thing that disagreed. **Nothing errored, because nothing failed** — the credential was
   simply never sent.

> **The lesson: I started at the routing because that's what I'd just changed. The cheap
> discriminator was `/auth/me`'s actual answer versus what the client sends — one look at the fetch
> patch beside the server's clamp would have found #3 immediately. Check what the credential
> actually IS before re-reading the code that consumes it.**

### The top-200 report, recovered (1f92b9b)

"Who owns the top 200 — a corpus concentration audit" existed only in the 07-30 session's
scratchpad, a temp directory that gets cleaned; the state log's ten lines were the only thing that
would have survived. Now `docs/reports/corpus-concentration.html` + its `-data.json` (all 200 ranked
rows, per-host counts, corpus totals) so the figures can be re-derived rather than re-measured.

### And the one to actually remember: I deleted two real recipes (a068ae5, reverted f4f1541)

Asked to remove the "recipes" for the two medical sites, I inspected them with `d.get("ingredients")`
and `d.get("title")`, got nothing, and reported **"no title, ZERO ingredients, ZERO instructions"** as
the finding that justified the delete. The rows are schema.org shaped: the keys are
`recipeIngredient`, `recipeInstructions`, `name`. id 478 is **Homemade Kimchi** (11 ingredients, 8
steps) and id 1419 is **Strawberry Salad with Grilled Shrimp** (12 ingredients, 5 steps). Curator
caught it — *"actually healthline IS a recipe"* — and WebMD was one too, by the same evidence.

Restored from the backup taken before the delete, via the base (non-generated) columns, embedding
BLOB recovered from its `repr` and the vec index rebuilt from blobs: 3450 to 3452, index matching,
`quick_check ok`. Deletes went through an `enable_vec` connection so the AFTER DELETE trigger stayed
in lockstep ([[project_vec_delete_triggers]]).

> **The lesson is not the key names. It is that I let an ABSENCE stand as evidence. A read returning
> nothing means the content is missing OR I asked the wrong question, and only one of those is a
> finding. Anything about to be deleted must be POSITIVELY identified — print what IS there before
> concluding what is not. Two lines of top-level keys would have shown `recipeIngredient` in plain
> view.**

Compounding it: I then offered to "correct that section" of the concentration report — and the report
was already right. It says, verbatim, *"The pages are real recipes, correctly extracted. The ranking
simply has no way to know that a health encyclopaedia is not a cooking authority,"* and names both by
title in the rank table. **The artifact I had just recovered contained the evidence that contradicted
me, and I hadn't read it.** Left unchanged; the false claim was only ever mine.

Also: `bcc_restart.bat` was corrupted a THIRD time — `endlocal` truncated to `end`, after `.env`
(overwritten with `done`) and this same file's echo line. Same signature
([[feedback_stray_keystroke_corruption]]).

### Open

- **Endpoints that spend money**, still the highest-value pass. `/reviews/find` and
  `/reviews/ingest-url` are the tightest cluster, both ungated.
- **`user_api_keys`** — no caller now; retire or keep is a decision, not a cleanup.
- **The four `/reviews/*` write endpoints** and the ~147 ungated ones generally.
- **Rotate both SMTP passwords**; password reset; verification is still unenforced.
- **The 2026-07-27/28 session log** — still missing, four sessions running now.

## Session log — 2026-08-01/04 — the Moz placeholder and the regression that followed · Wayback unwrapped · six publishers rescued · and the scoring design written down

Long session. Started with a live harvest showing suspicious scores, ended with a 711-line
design doc for the thing the scoring cannot currently do. Three of my own regressions in the
middle, all caught by the curator watching output rather than by me.

### The Moz placeholder — and the fix that broke everything (9485520, ecc8e8a)

Curator, mid-harvest: *"ALL of the first pass recipes get a 44 pa."* Half right, and the half
that wasn't is the bug. Healthline's crawled recipe pages genuinely cluster at 44-51 — but
every page Moz has NEVER crawled also comes back `http_code 0` with a **domain-derived
placeholder PA**, and `score_url_via_moz` computed `usable` and then never consulted it on the
way out. 15% of a 60-recipe sample was running on a fabricated number, and the placeholder is
HIGHER than an obscure page deserves, so the bug systematically inflated the weakest pages.

Fixed to return `None`. Curator's call on dropping rather than keeping-with-absent-PA: *"if it
can't crawl them they probably aren't popular enough to matter."*

**Then I broke it worse.** I wrote the gate as an allow-list — `http_code in (200,301,302,402)`
— reusing four codes that exist in `_pick` to RANK variants as a rejection test. Moz returns
other real codes: pinchofyum comes back `http_code 5` with `pa 49 / da 75`, plainly measured,
and the allow-list binned all of it. A publisher_refresh billed **496 Moz rows across 124 URLs
and scored ZERO**, finishing "successfully" because every URL failing individually reads
exactly like an uncrawled publisher. Curator found it from the output: *"got moz fail on all..
didn't get trapped anywhere."*

The rule was in the original comment and I failed to read it: **0 is the sentinel, and the only
thing the gate may reject.** `usable = bool(code)`. Re-run: 124/124 scored, and **124 Moz rows
instead of 496** — the broken gate had also silently disabled the canonical-variant learning,
since it only learns from a probe it considers usable.

**Two guards so this cannot go quiet again** (ca00579): a harvest that BILLS and stores nothing
now raises, distinguishing a genuinely uncrawled publisher (warn) from creds/quota/a bad gate
(raise); and `discovered > 0, recipe_pass == 0` raises too — the case the first guard
structurally cannot catch, since nothing reaches scoring.

### A learned recipe_path from a run that found no recipes (ca00579)

Three publishers harvested off SERP with no `serp_query` set. Discovery returned only the
sites' own archives, and the run PERSISTED `recipegirl.com -> 'set'`, `marionskitchen.com ->
'category'`, `paleogrubs.com -> 'tag'`. Those are WordPress taxonomy prefixes; each domain would
have scoped every future harvest to its own archive pages forever. Inferring the path from a
sample in which nothing was a recipe is inferring from the failure itself — same shape as the
placeholder PA. Now the curator's value is kept and the skipped guess is logged.

Curator diagnosed the cause: **SERP returns these sites' taxonomy, SEMrush returns their
recipes.** Switching source fixed all three.

### Wayback URLs were the recipe's identity (0d42fcc)

44 master rows stored `web.archive.org/web/<ts>id_/https://real...` as `_source.originalUrl`.
That is the TRANSPORT, not the recipe, and it cost three ways: Moz has never crawled an
archive snapshot so all 44 carried the same placeholder (pa 47, ou 0.049); attribution pointed
at archive.org rather than the publisher; and `url_normalized` keyed off the snapshot so the
same page fetched directly would not dedupe. The original url is embedded in the archive url,
so nothing needed re-fetching. 43 unwrapped, `_source.archiveUrl` preserving the snapshot.

**The finding that matters: Wayback is worth MORE, not less.** 33 of 43 turned out to be
well-scored publisher pages we simply could not fetch directly — Serious Eats 47 -> 62 (ou
0.049 -> 16.573), Taste of Home -> 59, Mayo Clinic -> 57, including a 1997 NYT piece. Retiring
the fallback, which looked like the implication when every archive row scored 47, would have
cost us those.

### Six publishers rescued from "unreachable"

Curator: *"did we even use unblocker when we ran christina's???"* **No.** `fetch_strategy` was
`plain` on christinascucina, washingtonpost and smittenkitchen, and SIX of the ten blocked
hosts had **no domains row at all**. Only 11 of 305 domains were on `unblocker`. The ladder ran
UA -> 403 -> Wayback and the paid tier was never attempted. We concluded "unreachable" from a
test we never ran.

Opted them in; five of six then fetched LIVE first try (recipegirl 645KB, washingtonpost 950KB,
marionskitchen, paleogrubs, christinascucina). Final harvests: **495 scored, 495 stored, 171
extracted**, corpus 3,451 -> 4,169. Washington Post passed **149 of 149** candidates — a paper
we had written off.

`haitianfoodie` was the exception and I read it wrong twice: I called its 402 "Pay-Per-Crawl
licensing", mapping it onto the seriouseats pattern. One curl of the homepage showed
`<title>Store unavailable</title>` — **a dead Shopify store**. Retired `harvestable=0` with the
finding recorded; its one real recipe dropped because the corpus is discover-and-judge with a
MANDATORY LINK and that link is not coming back (cf2d055).

### The bookmarklet hung, and postMessage could never have fixed it (dc04ca8, b6e27fb)

Curator: *"it's hanging on the recipe screen... BUT WE MUST FIGURE OUT THE BOOKMARKLET.. that's
in the users face!!"* Reproduced it in-browser. The token is handed over by appending
`#staged=…` to the url the popup is ALREADY on, so it fires `hashchange` without reloading —
but when staging finishes before the popup COMMITS its first navigation, the browser coalesces
the two same-document navigations, keeps the fragment-less one, and the token vanishes. The
popup showed `hash:""` and `history.length:1`. A race — which is why it grabbed healthline and
hung on christinascucina on identical code.

**My first fix was a postMessage handshake and it was impossible**: `window.open('','_blank')`
plus a CROSS-ORIGIN navigation SEVERS `window.opener` (verified null). The two windows share no
channel in either direction — which is WHY the hand-off was a fragment to begin with.

So the form stops waiting to be told and ASKS: it knows the page from `?url=`, and polls
`GET /staged-latest?url=…`. Verified end to end — stage -> staged-latest matched in 1s ->
staged-markdown -> extract 31,678 chars. The fragment lost the race again and the poll carried
it.

Also: the review grabber's device key is gone (c23a6f0, prior session but completed here) —
`user_api_keys` now has no caller.

### The form invented zeros the server never measured (29b17ff)

Curator grabbed a sun-sentinel recipe: *"it didn't score it."* Moz was right — the article was
THREE DAYS OLD and uncrawled. The save was wrong: it wrote `_scoring` with pageAuthority 0,
domainAuthority 0, every percentile 0. A DA-88 publisher recorded as DA 0. The form serialises
its whole scoring section, so an input with no value arrives as `0.0` and the writer stored it.
**Third route by which fabricated zeros reached `_scoring`** — after the batch writer (1,881
rows, 07-30) and the Moz gate. So the fix went at the SAVE BOUNDARY where every caller passes.
Per the curator's ask it records WHY, distinguishing "Moz has not crawled this yet" from "never
submitted" from "saved outside a dish batch".

### THE SCORING WORK — docs/recipe-scoring-design.md (711 lines)

Started as a curator question about paid/paywalled sites being penalised. Became the design for
what the system cannot currently do.

**I was wrong about the ranking and had to be corrected twice.** I called the authority signal
"hardly meaningful" and proposed an age confound; both died against data. Measured on
smittenkitchen (114 pages, one domain so DA constant): **correlation(PA, log10 external links)
= +0.905**. Within a domain PA IS third-party endorsement. And I had forgotten the selection
stage entirely — the curator's correction is now GOSPEL in the doc and in
[[project_two_stage_selection]]: **selection is last month's TRAFFIC, ranking is OU**, and
judging either in isolation gives wrong answers.

Then the design, all curator-led:

* **Traffic is APPETITE, not need.** Excess PA is ADVOCACY. Different acts; the gap between
  them is the informative part. The two axes are orthogonal — verified by quadrant counts.
* **Two objectives, two filters.** A LIBRARY of tried-and-true (leads on OU, EXCLUDES velocity)
  and RISING (leads on `Or` velocity, EXCLUDES PA/OU entirely — a new page has none by
  construction). Blending them destroys both. Flagged at extraction via `selection_lens`.
* **The RISING detector**, from a measured breakout. christinascucina/porridge: `Or` climbed
  from Nov 2025 (+17,+20,+33,+23,+32,+29,+17,+39) while `Ot` sat at 0,7,9,13 — **keywords lead
  traffic by 2-3 months**. `Ot`/`Or` went 0.3 -> 46.7 as positions crossed the page-1 cliff.
  Against a stuck page: porridge converts **84% of a 103,710 Nq pool**; vongole converts **0%
  of 40,410**, every keyword on page two. Same publisher, same DA.
* **Momentum must compare LIKE MONTHS.** Shortbread reads -1,962 month-over-month and 0.68x on
  a trailing mean — but is UP on every comparable month year over year, with the same
  Feb-peak/summer-trough in all three years. It is a Christmas bake. A raw `Traffic Change`
  cannot tell a breakout from a season.
* **Storage: no second recipe table.** Rising candidates live in the LEDGER
  (`collection_type='rising'`, already 10,276 rows) and a PROMOTION GATE admits them to master.
  Purity made structural via a `library_recipes` VIEW on a `selection_lens` generated column.
* **Execution — curator's catch that two gates kill a riser**: discovery truncates 820 export
  rows to the top 100 BY TRAFFIC, and keep-top-N sorts on PA-driven `rank_score`. Four phases
  instead: capture free at harvest (full export depth, append a snapshot), judge free in SQL
  (3 months of `Or` growth), pay `url_organic` only for survivors, promote only what triggers.
  **Rising is CHEAPER per candidate than the current extract.**

**API facts, probed live** (`scripts/semrush_url_probe.py`, ~400 units of a 50k balance):
`url_ranks` gives absolute per-url `Ot`/`Or` — hard-boiled-egg 24,625, shortbread 9,616 which
matches the UI exactly. `url_organic` gives keywords with `Nq`. **`url_rank_history` gives the
full monthly series** — and my "history is blocked on this plan" was wrong twice over: the 403
comes from `url_ranks` + `display_date`, a WRONG TYPE NAME producing a permissions error. The
real constraint is that it returns all 175 months at 50 units/line = **8,750 units per url**,
so it is shortlist-only.

Gotchas recorded: the type is `url_ranks` NOT `url_overview` (the docs page is TITLED "URL
Overview"); `url_organic` bills PER KEYWORD LINE; its `Tr` is a percentage while `Ot` is
absolute; an unindexed url returns `ERROR 50 :: NOTHING FOUND`; and **`url_ranks` is
variant-sensitive** — our slash-stripped `url_normalized` returns nothing at all.

**And the export already carries three fields we parse and discard**: `Traffic Change`
(momentum), `Answer Engines` (`google-ai`, `gemini`, `search-gpt`), `LLM Prompts`. Plus
`Number of Keywords` — the single most important field for Rising. `replace_members` is
delete-and-replace, so every re-harvest destroys the history Rising needs. Every month that
capture is not running is a month of series that cannot be bought back cheaply.

### The one to remember: I deleted two real recipes (a068ae5, reverted f4f1541)

Asked to remove the "recipes" for two medical sites, I inspected them with `d.get("ingredients")`
and `d.get("title")`, got nothing, and reported **"no title, ZERO ingredients, ZERO
instructions"** as the finding that justified the delete. The rows are schema.org shaped —
`recipeIngredient`, `recipeInstructions`, `name`. id 478 is **Homemade Kimchi** (11 ingredients,
8 steps); id 1419 is **Strawberry Salad with Grilled Shrimp** (12/5). Curator caught it:
*"actually healthline IS a recipe."* Both restored, embedding BLOBs recovered from their repr,
vec index rebuilt.

> **The lesson is not the key names. I let an ABSENCE stand as evidence. A read returning
> nothing means the content is missing OR I asked the wrong question, and only one of those is
> a finding. Anything about to be deleted must be POSITIVELY identified — print what IS there
> before concluding what is not.**

Compounding it, I then offered to "correct" the concentration report — which was already right,
says *"The pages are real recipes, correctly extracted"*, and names both by title. **The
artifact I had just recovered contained the evidence contradicting me and I had not read it.**

### The pattern across all of it

Five times I asserted a mechanism and the data refused: the age confound, internal linking,
"meaningless", `url_overview`, "history is blocked". Each error message described the wrong
cause — *"query type not found"*, *"not allowed on this plan"*, *"no data"* — and I took it at
face value. The habit that would have caught every one is probing adjacent names, or running
the thing, before believing the message.

I also killed two live job records by importing the app in ad-hoc scripts: the
`BCC_SKIP_JOB_RESET=1` guard exists and its comment says exactly why, and `python -m jobs` sets
it. My throwaway scripts did not.

### Open

- **The strip decision** — ~15% of the corpus on fabricated PAs. Option 2 (strip) or 3
  (read-only pass first, see the blast radius). Untouched.
- **Five archive-47 rows still fabricated** (1343, 2171, 3656, 3802, 5025) — all now scoreable
  (+8 to +12 OU); only 340 was fixed. Backup at `docs/reports/rescore-archive47-backup.json`.
- **`user_api_keys`** — no caller since the review grabber went keyless. Retire or keep.
- **Snapshot capture** — the free change that starts the Rising clock. Not built.
- **Cadence** — `harvest_ttl_days=90` on 295/311 domains; Rising needs monthly.
- **`/staged-latest` and the zeros fix are live**; nothing pending a restart.

## Session log — 2026-08-04 (later) — the experiment: keywords do lead traffic, weakly · traffic momentum is INVERTED · and four of my claims died

Continuation of the scoring thread. Three commits, but the content is mostly **subtraction** —
the day's work was testing things I had asserted, and most of them failed.

### THE EXPERIMENT (8dd78ce, `scripts/keyword_lead_experiment.py`)

Curator: *"let's do the experiment... how do you propose to do it."* The proposal that mattered
was theirs: *"why don't I just run a seriouseats extract now"* — I had costed a 30-page
`url_rank_history` buy at 18,000 units when a **free workbook download** gave 5,064 pages. Then:
*"I don't actually need the extract, I just need the semrush file."* Right — no harvest, no
fetches, no Moz rows.

Three Organic Pages exports (Jun 21 / Jul 21 / Aug 03), n=5,064 pages present in all three,
**zero API units**. Design avoids the obvious trap: taking known breakouts and confirming
keywords rose first conditions on the outcome, and porridge will always look convincing. Cohort
is EVERY overlapping page, so the cases where keywords rose and nothing happened are counted.
And `Or` must beat a null, since traffic's own past predicts its future.

```
corr( dOr Jun->Jul , dOt Jul->Aug )  = +0.1162     the hypothesis
corr( dOt Jun->Jul , dOt Jul->Aug )  = -0.2330     the null
PARTIAL corr( dOr | dOt )            = +0.1865     unique contribution
deciles by dOr: worst 29% grew -> best 51% grew    (monotonic, all ten)
dOr > +0.10:    52% grew vs 43% baseline
```

**The hypothesis survives WEAKLY.** Ten monotonic deciles on n=5,064 is not noise, but `Or`
explains only ~3-4% of variance and 52-vs-43 is a 1.2x lift. My "keywords lead traffic by 2-3
months" framing came from porridge — ONE vivid case — and at population scale it is far softer.
So `Or` slope is a **ranking signal for a watchlist**, never a confident breakout detector.

> **The most valuable result is the NULL: traffic momentum is INVERTED (-0.233).** A page that
> gained traffic last month tends to LOSE it next month. **Ranking risers by traffic change
> performs WORSE THAN RANDOM** — which falsified two things I had already written into the
> design doc: the "Rising = steep positive arrow" archetype, and using the export's
> `Traffic Change` as a positive flag.

Standing check now, not a one-off: every future export pair adds a month, and the script runs on
any three files.

### The mechanism I explained was wrong (in 4f5f4da)

Curator: *"how exactly does google add new keywords to a url... I don't understand."* I explained
it as pages climbing past rank 100 into Semrush's counted set. They asked me to verify it. It is
false:

```
porridge's 224 keywords:  pos 1-3: 25   4-10: 84   11-20: 36   21-75: ~30   76+: 0
```

**Nothing beyond position 75.** Keywords ARRIVE already ranking decently — mostly 4-20 — not at
the bottom of a ladder they climb. Google widens the query set it treats the page as a good
answer for and places it reasonably on each. Semantic matching, not rank creep.

Two process failures on the way, both mine. The first pull sorted ASCENDING with a limit below
the keyword count, truncating away the exact region under test — **1,500 units for an unusable
answer**. And I presented a traffic-attribution table that the curator immediately spotted did
not sum to 100%: I had labelled `url_organic`'s `Tr` column "% of the page's traffic" **without
checking what it denominates**. Tested: a 12-keyword page returns 7 rows summing to 10.05 with
`Traffic(%)`=0.07 and page traffic 149 — reconciles with neither page nor domain. **`Tr` is now
documented as unusable.**

### The Rising metric and the watchlist (e452247)

Both curator designs, both better than what they replaced.

**`rising_score = Or_log_slope x R2`** — *"why don't we compute slope and filter on that."* Kills
every cliff-edge rule. Porridge 6mo `+0.083 R2 0.98` vs shortbread `-0.170 R2 0.91`. R2 settles
the `Or`-vs-`Ot` question quantitatively: porridge's TRAFFIC slope is `+0.167` at **R2 0.37**
(the 1,383->383 spike) while its KEYWORD slope is `+0.083` at **R2 0.98** — same page, same
window, one is noise and the other a straight line. And it dissolves the counterexample that
killed the old prune rule: a traffic-fell-drop-it rule kills porridge in March, two months before
it takes off; a negative-slope rule cannot, because `Or` never fell.

Seasonality scoped honestly: shortbread's `-0.170` at R2 0.91 is a CONFIDENT downtrend that is
really just July, so slope is confounded **for mature pages**. A Rising page has never been
through a cycle. **Seasonality is a LIBRARY problem, not a RISING problem.**

**The watchlist** — *"screen using the current extract file, pull out the new into a new table...
then monthly refresh that file using the api. Drop ones that fell, keep ones that survived."*
`Traffic == Traffic Change` IS the "New" filter (verified: 26 of 820 rows on christinascucina,
porridge correctly excluded because it already had traffic). Then `url_ranks` at **10 units/url**
monthly — 260 for one publisher's new pages, 3,000 for a 300-url steady state, self-limiting
because pruning caps the list. Build the series rather than buy it: `url_rank_history` with
`display_limit=12` is 600 units/url, reserved for backfilling a MATURE page. **This decouples the
clocks**, so `harvest_ttl_days=90` on 295/311 domains stopped being the blocker it looked like.

### Semrush API, mapped properly

`url_ranks` (Ot/Or, any url, 10/line) · `url_organic` (keywords + Nq, 10/line) ·
**`url_rank_history`** (monthly series, 50/line) · `domain_organic_unique` (a domain's whole page
list — 807 rows for christinascucina, matching the export exactly) · `domain_rank_history`. No
"New" filter and no multi-domain call.

Three name traps, each paid for once: the type is `url_ranks` not `url_overview` (the docs page
is TITLED "URL Overview"); history is `url_rank_history` not `url_ranks`+`display_date` — **the
403 "History reports are not allowed" was a WRONG TYPE NAME producing a permissions error**, and
I recorded it as a plan limitation in two commits; and `url_rank_history` returns 175 months
unless `display_limit` is set, which is 8,750 units for one url. Also `url_ranks` is
variant-sensitive — our slash-stripped `url_normalized` returns nothing at all.

### The doc rewritten (4f5f4da)

Curator: *"much of the earlier content has now been disproved... we should refresh the document."*
882 -> 405 lines. Two structural changes: **every claim is marked MEASURED or ASSUMED** (most of
the trouble came from plausible reasoning presented in the same voice as measurement), and a
**Disproved table** carries the dead claims with their refutations so they are not re-argued.

Twelve entries. Four are mine from these two sessions: the authority signal being "hardly
meaningful" (corr(PA, log external links) = **+0.905**), an age bias in ranking (corr(publish
year, PA) = **+0.530**, the other way), keywords gained by crossing rank 100, and traffic history
being blocked.

### The pattern, and the cost of it

Across both halves of this session I asserted a mechanism and the data refused **five times**.
Every one had a cheap test available first. Two of them cost real money: 1,500 units on a
badly-sorted pull, and ~17,500 on two unbounded history calls where `display_limit=12` would have
been 600. **Balance 50,000 -> 26,490.**

The recurring shape is an error message describing the wrong cause — *"query type not found"*,
*"not allowed on this plan"*, *"no data"* — taken at face value instead of probed. And a derived
column used without checking its definition, where the arithmetic not summing to 100% was sitting
in plain view.

### Open

- **Rerun the experiment** on the 21 Aug export (clean 30-day window vs the current 13) and on a
  second publisher — everything measured is one site.
- **Snapshot capture** — still the free change that starts the clock, still not built.
- **Structured-data prerequisite** — bostonglobe shows no keyword signal at all
  (`_source.type=article`, poor_quality_rate 0.41). A publisher Google cannot parse as recipes
  reads as "not rising" for reasons unrelated to quality. Systematic blind spot, unquantified.
- Unchanged from earlier: the strip decision, the five archive-47 rows, `user_api_keys`, cadence.

## Session log — 2026-08-04 (close) — the strip decision: measured, then declined

The parked item from 2026-07-31 resolved, and it resolved **against the action I had
recommended**. Two cheap measurements were enough.

### The number I reported was 3x too high

The claim was *"~15% of the corpus carries a fabricated PA"* — a domain-derived placeholder
returned for URLs Moz has never crawled, stored as if measured. Re-running the same seeded 60-row
sample through the CORRECTED gate:

```
scores fine               : 57  (95%)
no Moz data -> would strip:  3  ( 5%)
2026-07-31, same seed, BROKEN allow-list gate: 9/60 (15%)
```

The 15% was measured **through my own regression** — the `http_code in (200,301,302,402)`
allow-list that also made a pinchofyum harvest bill 496 rows and score none. Two-thirds of
"fabricated" was the measuring instrument. True scope ~210 rows of 4,179.

### Why stripping was wrong, structurally

The placeholder is derived from DA, and OU measures PA *against* DA — so a fabricated row lands
near the **OU = 0 line** by construction:

```
1470  bostonglobe.com      pa 41.0  da 91.0  ou  -5.041   (par = 46.0)
1481  cooking.nytimes.com  pa 49.0  da 95.0  ou  +1.749   (par = 47.3)
                                  corpus median OU 11.05 · top-200 floor 18.04
```

Checked **exhaustively, not sampled**: `0 of the top 200 by OU` are fabricated. A fabricated row is
~18 points short of the door and cannot climb. Stripping would have moved rows from "ranked last"
to "not ranked" and changed nothing anyone sees.

**The limit, stated because it cuts the other way:** the top-200 test selects BY stored OU, so it
proves nothing fabricated is wrongly IN the top — it cannot prove none is wrongly OUT. And that
direction is real: Boston Globe at OU −5 against a median of 11. These hosts are uncrawled because
they **block crawlers**, not because nobody links to them, so *"if it can't crawl them they
probably aren't popular enough to matter"* does not hold for this subset. They are the same gated
publishers the paid-PA calibration exists to rescue. Stripping deletes the population that work
needs and makes the exclusion permanent.

### What shipped instead — provenance, not deletion

The defect was never that the value exists; it is that it is **indistinguishable from a measured
one**. So `_scoring.mozHttpCode`, three states: `NULL` unverified (everything pre-fix) · `0`
verified fabricated · `>0` verified measured.

- `_moz_lookup` returns `(scores, http_code)`; two wrappers sit on it — `score_url_via_moz` (gated,
  unchanged contract) and `moz_http_status` (the code, no score). **Kept separate deliberately** so
  no caller can opt back into scoring an uncrawled URL, which is the original bug.
- Generated column `moz_http_code` on both recipe tables (+ `metabase_url` column), so it is
  SQL-selectable like ou_score/power.
- Excluded from `_sanitize_scoring`'s zero-strip list — it is the one `_scoring` field where **0 is
  a real measurement**, and stripping it would delete the finding it exists to record.
- `scripts/verify_pa_provenance.py` — stamps existing rows, never touches the PA. Resumable (NULL is
  the worklist; a no-answer row stays NULL). **Ran on bostonglobe's 32: 30 measured, 2 fabricated
  (6.2%) — ids 1470 and 3277, exactly the two the random sample had flagged.** Corpus-wide cost
  corrected UP to **~$10**: a fabricated url costs ~5 Moz rows, not 1, because the learned
  single-variant probe finds nothing and self-heals by re-expanding to all four. It is also ~2 round
  trips each, so the run is slow — 32 urls took 7 minutes.
- **One of those 30 came back `http_code 1`.** Not in any documented set, real data attached. Third
  observed non-standard code after pinchofyum's `5`, and independent confirmation that `bool(code)`
  is the right gate and the `(200,301,302,402)` allow-list was discarding measurements.
- `calibrate_paid_pa.corpus_da_pa` now skips `mozHttpCode == 0`. The doc asserted consumers must
  exclude it, so the one consumer that exists was changed to actually do it.

Docs: `recipe-scoring-design.md` §11b (new) + two more rows in the Disproved table, including my
own 15%.

### The pattern, again

Same shape as the five failures logged above: a number asserted from a broken instrument and
carried forward as fact for four days, plus a remedy recommended before its blast radius was
measured. The $0.79 of Moz rows that settled it was available the moment the question was asked.

### Open

- **Provenance backfill** — ~4,150 rows still NULL. Non-destructive, no rush; nothing visible
  depends on it.
- **The five archive-47 rows** (1343, 2171, 3656, 3802, 5025) — still fabricated, now scoreable.
- Unchanged: snapshot capture, cadence, rerun the experiment on a second publisher, `user_api_keys`.

## Session log — 2026-08-05/06 — the bookmarklet hang, and four layers under "why are my scores zero"

Two threads, both starting from a one-line report and both bottoming out somewhere other than where
the symptom pointed.

### The product bookmarklet "hang" (9083021)

The log had **no King Arthur trace at all** — no `stage-markdown`, not even the
`product_bookmarklet.js` fetch every successful run starts with. So it died in the page. Two
hypotheses, both killed by measurement before any code was written: **not CSP** (neither King Arthur
host sends one; an injected `<script src>` loaded AND executed) and **not the `md()` DOM walk**
(3,686 nodes, depth 25, **6 ms**).

Reproducing on the real page found it. `products.html` painted `renderImporting()` and then, on ANY
failure, flashed a toast and returned `false` — and the boot did nothing with `false`:

```js
if (await tryStagedImport()){ renderList(); renderDetail(); ... }
```

The page sat on *"Grabbing product…"* forever, toast long gone. The trigger is credential expiry:
`/extract-product` needs `edit_master`, `MASTER_TOKEN_TTL` is **12h**, and a lapsed curator still
resolves as themselves but reads as `member` (`staff_locked`) — so 403, and the grabber had no way to
ask for what it needed.

**And the sign-in retry ported from reviews.html was the WRONG CREDENTIAL.** `signInDialog` posts
`/auth/login`, which proves who you are and never mints a master token; against `staff_locked` it
would have looped forever. Added `LibraryShell.unlockDialog()` (the `/auth/master` sibling),
`unlockAdmin` now returns true/false and takes `{reload:false}` so an unlock does not discard work in
flight, and both grabbers pick the prompt by asking `/auth/me`.

Nearly shipped invisible: `library-shell.js?v=` is a **manual** cache-buster. Until it was bumped
(20260731d to 20260805a, 23 pages) every browser kept the old shell.

### "i've had zero scores many times" — and the recurrence was the tell

Zero-carrying saves by month: **May 3% / Jun 10% / Jul 15% / Aug 94%**. Four layers, only the last
one causal.

**Layer 1 — the fix that never ran.** `_sanitize_scoring` was committed 08-03 and executed for the
first time on 08-06. BCC is an NSSM service with no `--reload` and had not been restarted in three
days. Proof was one command: the function prints on every drop, and `grep -c` over 25MB of log
returned **1**, timestamped three minutes after the restart. The confusing part was that in-process
JOBS (fresh process each run) did apply it, so job-written rows looked right and service-written rows
looked broken — the difference was **process age, not logic**.

**Layer 2 — "no Moz data" was usually OUR bug** (226635a). The stored URL is whatever the bookmarklet
grabbed: a mobile path from a phone, an AMP page, a share sheet's tracking query. Moz indexes the
canonical page and had never seen those strings.

```
cooking.nytimes.com/recipes/1025200-...?unlocked_article_code=...  NO DATA -> pa=57 da=95
williams-sonoma.com/m/recipe/stanley-tucci-chicken-cacciatore      NO DATA -> pa=41 da=82
```

`_canonical_form()` adds the cleaned URL as an EXTRA candidate; `url_normalized` (dedup + cache key)
is untouched. Guarded three ways because silent over-scoring is the worse failure: tracking keys are
an ALLOW-LIST (sites use `?p=123` as real identity), only a LEADING `/m/` is stripped, and a canonical
form collapsing to the site root is REFUSED outright.

**Layer 3 — a usable variant losing a tie-break**, found only because `/amp` still failed after layer
2. `_pick` tiered on `crawled` (200/301/302) and `estimated` (402) and dumped the rest into a generic
pool ranked by PA. On travel-gourmet all 8 variants returned pa=21 and only the trailing-slash form
carried **code=3** — so `max()` broke the tie by list order, returned a code-0 row, and the page
scored nothing. **That is the (200,301,302,402) allow-list for the THIRD time** (the `usable` gate
that made pinchofyum bill 496 rows and score 0; the inflated 15% estimate measured through it; now a
tie-break). Those four RANK known tiers. "Has data" is `bool(http_code)` and nothing else. A fourth
undocumented code — **15** — turned up later during sampling.

**Layer 4, the actual cause — the CONTRACT manufactured the zeros** (f70a075). `ScoringMetadata`
declared every numeric field `= 0.0`; every extract passes through `RecipeModel`, so the model created
a full block of zeros on every recipe. `sanitize_recipe_data` re-inserted PA/DA/OU as a second
manufacturer. **Three prior fixes had all been at the SINK** — the `pre_scored` writer (07-30), the
save boundary (08-03), the restart (08-06) — each deleting values the contract had just created.

Three genuinely different states had been collapsed into one `0`: **NOT APPLICABLE** (a handwritten
recipe mints a `/r/<uuid>` self-URL; nothing can link to it) / **NOT MEASURED YET** / **NO COHORT**
(saved outside a dish batch). 11 / 16 / 41 of the 70 affected rows. `None` expresses all three.

Audited every reader before flipping, because None in a ranking path is worse than a zero — all of
them already tolerated absence (`setScoreChip` renders an em-dash, `blend._power` returns None unless
both operands are numbers, `chapters.py` filters `IS NOT NULL`, `dishes.py` COALESCEs). **The "222
refs and `da + pa` would break" warning was wrong**: lines grepped without reading their guards.

Caught by the same audit: **`mozHttpCode` was never declared on the model**, so pydantic silently
dropped the provenance flag on every extract that round-tripped it — written by `url_scoring`,
surviving batch and save, vanishing at validation. Exactly what the model-first rule prevents.

### Repairs, each measured rather than assumed

- **Re-score** (`--zeros-only`): 42 urls, **5 recovered, 0 lost** — the Tucci recipes and the NYT
  shawarma, all `0.0 -> real`. The curator's proposal ("just re-score them") was right for 16 of 70
  and could not touch the other 54; my projection of 16 recoveries produced 5.
- **Strip** the manufactured zeros: 110 rows in `recipes`, **2,214** in `master_recipes`. Checked the
  one real hazard first — a row with genuine PA whose `ouScore` was exactly 0.0 would silently lose
  its OU — and found **zero** such rows. Ranking after: rankable 4353 -> 4334, exactly the 19 whose PA
  was already 0; top-10 by OU unchanged, still pancakerecipes.com at 25.38.
- **Tie-break blast radius, measured not estimated** (3288756). The obvious sample would have lied:
  corpus rows are SURVIVORS (they scored under the old code, so damage reads 0 by construction) and
  the affected population — candidates dropped mid-harvest — is never persisted. Used `dish_rejects`
  rows rejected for FETCH reasons instead. **Damage 0/120 (0.0%)** on the unbiased sample, 1/40 on the
  survivor control, ~0.6% combined, roughly 26 floor-dwelling rows. The at-risk condition was common
  (22%) but the failure needs a second coincidence. **Recommendation: do NOT re-score the corpus.**
  I predicted the control would be 0 "by construction" and said otherwise meant my reasoning was
  broken; it found 1, and the reasoning WAS off — survivors are guaranteed against the response at
  HARVEST time, not today's.
- **Aggregates** (e6627c8), curator: *"the 'empty' stats should not be included in any aggregate
  analysis."* Two were counting them. `recompute_competitiveness` averaged
  `COALESCE(da,0)+COALESCE(pa,0)`, so an unmeasured winner contributed 0 and made its dish read as
  nicher than it is (recomputed: 5 dishes moved, Cream Pie 50.0 -> 75.0). And `percentile_ranks` kept
  None in the DENOMINATOR, squeezing measured pages into the top of the range. That instruction also
  retroactively fixed something invisible: the per-dish OU regression filters on
  `isinstance(x,(int,float))`, and `0.0` IS a float — so unmeasured rows had been entering the fit as
  `(0,0)` points and bending the curve.

### The pattern

Every layer was a plausible mechanism asserted where a cheap measurement was available. The two
bookmarklet hypotheses died in one browser call each; the 15% fabrication figure had been measured
through a broken instrument; the re-score projection was 3x optimistic; the reader audit contradicted
my own blast-radius warning. **What worked was refusing to act on the estimate.**

### Open

- **Provenance backfill** — ~4,150 rows still NULL, ~$10, non-destructive. Fold a re-score into it if
  it ever runs (`backfill_url_scoring` does both in one pass).
- **`recipes.sql.gz` is stale** — predates today's re-scores and both strips. Run `bcc_backup.bat`.
- **`docs/reports/strip-master-backup.json`** — 1.7MB rollback snapshot, untracked, deletable.
- Unchanged: the five archive-47 rows, snapshot capture, cadence, `user_api_keys`.

## Session log — 2026-08-06 (later) — "why am I not getting the other mussel recipes" — four defects under one symptom, and the embedding side audited

Started from one report: three mussel recipes saved by bookmarklet, and "Show top recipes
like this" returned neither them nor the other mussels — it returned **moussaka** and
**coquilles st jacques**. Two independent causes, and I got the second one wrong twice
before measuring it.

### The missing mussels — structural, working as designed

The three saves are `user_id 5` in the `recipes` table. `recipes_master_vec` is populated
ONLY for `user_id == 0` (`save_recipe_api.py:8512`); user rows get the BLOB and nothing
reads it — the comment's *"a `recipes_vec` index can derive from it later"* never happened.
Curator's call: leave master alone, personal recipes stay out. See
[[project_discover_vs_possess]].

### The weird results — two wrong theories, then the evidence

**Wrong once:** "moussaka is rank 8 of the KNN window." The button posts `k: 6`, so that
could not have been it. **Wrong twice:** the query composes differently from the index —
no, `lastExtractedRecipe = r` is the full record on both load and extract, so `_identity`
is sent and the card path is used on both sides.

The answer was in the rows the whole time. `_match`, stamped at save:

```
794 Carl Dooley's Mussels -> nearest dish Moussaka  (0.9251)  confident:False dish:None
795 Moules Marinieres     -> nearest dish Coquilles (0.9002)  confident:False dish:None
796 Moules Frites         -> nearest dish Moussaka  (0.9293)  confident:False dish:None
```

**The save path had already rejected all three.** The endpoint asked the same question with
a different metric and a looser bar — save uses `dish_match_max_distance` 0.8 L2 (= cosine
0.68), the endpoint used `DEFAULT_MATCH_THRESHOLD` cosine 0.55 (~L2 0.95) — anchored to the
rejected dish, and Tier 1 returns a cohort with **no distance cutoff by design**. Six
coquilles. Reproduced exactly. One question, one bar: the endpoint now derives it from the
same setting ([[feedback_single_path]]).

Three more in the same endpoint: the Tier-2 cutoff `similar_max_distance` **never bound**
(0.95 admits 0.7-1.4% of the corpus, always more than `k`, so the window padded); no
self-exclusion, so a promoted recipe recommends itself (`recipe_id` misses it — the master
copy is a separate row with its own uuid; matched on normalized source URL instead); and
results ranked by proximity rather than quality. Now two-stage like the harvest —
**similarity SELECTS, the OU/power blend RANKS** ([[project_two_stage_selection]]). Cutoff
0.86, seeded into `system_config`; measured inert for a typical recipe (median 26 neighbours
inside it) and biting only in sparse regions, which is where the padding was reaching.

### The identity card led with an incidental ingredient

Curator: *"we must prioritize the title ingredients."* `_derive_primary_ingredients` returned
primaries in RECIPE-LISTING order, so Moules Frites embedded as
`primary: yukon gold potatoes, mussels, ...` — the aioli and fries came first in the source
list. `INGREDIENT_ROLES` was already in importance order and simply was not used for sorting.
Now ranked on it (main_protein > primary_vegetable > starch), and that list is **load-bearing**.
**41.4% of master cards change order, 28.5% get a new lead** (onion -> shrimp, flour ->
blueberries, grits -> shrimp).

The title itself stays OUT — measured and removed 2026-06-11 (titles nearly doubled intra-dish
L2, 0.35 -> 0.68). The directive is satisfied by ordering the ingredients the title implies.

### The embedding audit — the plumbing was never the problem

Four vector surfaces, all `text-embedding-3-small`. Composition **symmetric** on every one
(query and index built by the same composer); vec0 agrees with the source BLOB (0 drift /
623 sampled); no orphans either direction; no zero vectors, dimension or norm anomalies;
delete triggers present. The single defect was in the TEXT being embedded — the one layer
nothing watched.

Root cause of the blind spot: `dishes` carried `embedding_text` / `embedding_model` /
`embedding_updated_at`; `master_recipes`, `recipes`, `products` carried none, so composer
drift was **undetectable**. Added. Curator, unprompted and correct: *"an embedding timestamp
might be a useful add."*

`scripts/reembed_identity.py` (`--dry-run`, resumable by text hash) re-derives from stored
`ingredientRoles` — pure function, no LLM, roles never modified, so the pass is reversible.
**Ran: 4,775 rows, 2,023 reordered, 21 personal + 11 product rows that had NO embedding now
filled, ~$0.006.** Those 11 products are the original ATK loaf-pan catalog, which predates
the `description` field — they could never have surfaced in `find_similar_products`.

### The regression cycle

Curator: *"we need a regression test cycle to run on demand or daily to flag these items
before they cause trouble."* First one shipped: `scripts/check_embeddings.py`, read-only,
non-zero exit, covering dimension / norm / zero-vectors / index-vs-BLOB / orphans / coverage
/ model drift / **composer-drift staleness** — the last compares each row's stored
`embedding_text_hash` against what the current composer produces, and is what would have
caught this the day it shipped. **Now 0 failures, 0 warnings.** See
[[project_regression_check_cycle]], [[project_embedding_architecture]].

### Screenshots

Not failing — **not attempted** on the personal/bookmarklet path. Master 3927/4356 (90%),
personal 172/412 (42%), and zero "capture failed" lines in any log. Same recipe saved
personally at 16:55 had no blob; extracted into master at 17:20 it got one. Separately, the
429-row backfill script called `capture_screenshot()` (image_store -> `generated/`,
git-ignored and ephemeral) instead of `capture_and_store_blob()` (media.db) — running it
would have written 429 URLs that break on the next cleanup AND, being non-empty, locked
those rows out of self-healing. Fixed; dry-run verified at 4.2s/row.

### The pattern

Same as the last two sessions. Every wrong answer I gave was a plausible mechanism asserted
where a cheap measurement was available — the rank-8 theory died on one glance at `k: 6`,
the composition theory on one grep, and the actual cause was sitting in `_match` on the rows
from the moment they were saved. Two of my four claims this session were wrong and both were
caught by running something rather than reasoning harder.

### Open

- **`recipes.sql.gz` stale** — the re-embed rewrote 4,775 rows AND added three columns, so a
  restore from the current dump comes back without provenance. Run `bcc_backup.bat`.
- **Restart needed** for self-exclusion + blend ranking (committed, not yet running — the
  restart earlier today only carried the tier-1 bar and the cutoff).
- **Personal-path screenshots** — capture never attempted; plus `pageScreenshot` defaults to
  `""` in `recipe_model`, so never-attempted is indistinguishable from failed
  ([[feedback_absent_not_zero]] shape).
- **429 master screenshots** missing — script is safe now, ~30 min.
- `power_blend_weight` (30.0) lives in `bcc_config.json`, not `system_config` — one more
  migration candidate ([[project_system_config]]).
- Unchanged: provenance backfill (~4,150 NULL `mozHttpCode`), the five archive-47 rows,
  snapshot capture, cadence, `user_api_keys`.

## Session log — 2026-08-06 (close) — the ranking arc (three orderings, the third was the curator's), dishes_vec, and grades that outlived their scores

Continuation of the mussel thread. Two threads, and the first one I got wrong twice in a row
before the curator supplied the reasoning that settled it.

### The ranking arc — selection had already happened

Shipped **rank by OU/power blend** within the similarity pool, reasoning by analogy to the
harvest gospel: similarity SELECTS, quality RANKS ([[project_two_stage_selection]]). The
curator caught it live: *"i might have jumped the gun... sole meuniere came out on top whereas
the next dish was mussels at a .27 distance vs the sole at .82."* Confirmed — inside the pool
the blend had discarded distance entirely, so Sole Meuniere (blend 1.00, d=0.8288) outranked a
near-identical moules mariniere at **d=0.2618**.

Curator proposed multiplying. Measured five formulas on the real pool; **blend × cosine** fixed
the case and left the other two queries IDENTICAL (it only bites when the pool holds a genuinely
close match — same property that made the 0.86 cutoff safe). Chose cosine over a similarity
percentile deliberately: rank flattens the very thing that matters, the gap between d=0.26 and
d=0.71, and a percentile hands the pool-worst an exact 0 which annihilates a product. Shipped
with a 0.15 floor against that zero.

Then the curator supplied the argument that killed both versions: *"recipes that weren't scored
won't participate... just the fact that these are master recipes implies they are great...
i'd rather see mussel recipes than sole regardless of rank."*

**Right, and it inverts the two-stage frame. MASTER MEMBERSHIP IS THE SELECTION STAGE.** Every
row in the pool already survived harvest selection and curation, so re-ranking on authority
charges a second toll on a signal the pool has already applied — and demotes rows that are
merely UNSCORED rather than bad. Similarity is the only ordering the pool has not already
expressed. Final: **order by distance; SHOW the grade** (94.7% populated, A+..D-, the form
already renders it) so the reader judges instead of the ranker deciding.

The proof is Moules Frites: under blend, Air Fryer French Fries and Perfect Crispy French Fries
sat 2nd and 3rd. Under distance, **three mussels recipes take the top three.**

Tier 1 (exact dish cohort) still ranks by rank_score, correctly — there every member IS the
same dish, so similarity carries no information.

**My error both times:** reaching for a pattern that fit the shape of the problem
([[project_two_stage_selection]]) without checking whether its precondition held. It did not:
the selection stage was upstream, in the corpus itself.

### dishes_vec had no rebuild path (closes an open item from the previous entry)

`backup_db.py` EXCLUDES the vec0 virtual tables from `recipes.sql` — they are derived. Master
and products have `rebuild_*_vec_from_blobs`; dishes did not, so a restore came back with the
dish index empty and no route home but re-embedding all 150 through the API. The BLOBs were in
the dump the whole time. Written and verified non-destructively: captured all 150 vectors,
rebuilt for real, compared — 150 written, 0 missing, 0 extra, 0 drifted, KNN unchanged.

Small tell worth remembering: `rebuild_master_vec_from_blobs`' docstring had described itself as
mirroring *"how dishes_vec rebuilds from dishes.embedding"* — a function nobody had written. A
docstring asserting a sibling exists is not evidence it does.

### Grades that outlived their scores

Curator: *"i think the d- is a result of no score... if there isn't a score nor should there be
a grade."* Half right, and the half that was wrong is the interesting half.

The D grades are overwhelmingly GENUINE — the ladder tracks OU monotonically (A+ median 17.03 →
D median −5.72) and 14 of 15 D/D- rows carry real negative OU. And `compute_exceptionalism`
cannot grade without DA/PA; `float(None)` raises and it returns None.

So these were graded when scores existed, and **the manufactured-zero strip (e942c78) removed
the fabricated PA/DA from `_scoring` while leaving the DERIVED exceptionalism block behind.**
The grade outlived its basis — the same shape as the bug the strip was fixing, one level down.

**47 rows, not the 17 I first reported**: master uses `_master.exceptionalism`, personal uses
`_grade`, and my first count only looked at the former. The personal table held the worse cases.

Curator chose re-score over delete. Yield was 1 of 47, but the diagnosis was worth it:

```
regraded                             :  1
self-url (not applicable) -> dropped : 19
no-moz-data -> dropped               : 25
scored but no cohort -> dropped      :  1
no-url -> dropped                    :  1
```

**The 19 are the finding.** Handwritten recipes carrying their own `/r/<uuid>` permalink —
nothing external can ever link to them, so Moz can never answer, and a `D-` was grading the
curator's own recipe as failing for want of inbound links to a URL that exists only for them.
That is NOT APPLICABLE, distinct from NOT MEASURED ([[feedback_absent_not_zero]]). Now skipped
BEFORE the Moz call, matched on the `/r/<uuid>` path rather than the host (the public host has
changed over time; the path shape has not). Result: **0 orphaned grades in either table.**

### Open

- **Restart needed** — the similarity ordering (33ed9ae) is committed but not running.
- **`recipes.sql.gz` stale again** — the grade repair rewrote 47 rows after the 14:09 dump.
- `scripts/repair_orphan_grades.py --dry-run` **still spends Moz rows** (previewing a re-score
  requires scoring; only the write is suppressed). Ran it twice before committing, ~3x the
  necessary spend. Small (~130 rows of a 120k plan) but the flag promises more than it delivers.
- A **scoring counterpart to check_embeddings.py** — assert no row carries a grade without a
  live PA/DA, so this cannot recur silently. This is the second derived-artifact-outlives-source
  bug in three days ([[project_regression_check_cycle]]).
- Unchanged: personal-path screenshots never attempted, 429 master screenshots,
  `power_blend_weight` still in `bcc_config.json`, provenance backfill (~4,150 NULL
  `mozHttpCode`), the five archive-47 rows, `user_api_keys`.

## Session log — 2026-08-06 (screenshots) — the capture that was never attempted, and a recapture job

Third block of the day. Closes the screenshot thread opened by *"how come i'm not getting
screenshot on the mussel entries"*.

### The personal path never tried (ca80649)

Diagnosed earlier from three numbers: personal saves 172/412, master 3927/4356, and **zero
`capture failed` lines in any log**. Nothing was failing — nothing was being attempted.

The capture lived INLINE in `extract_recipe_from_url`, the URL path. The bookmarklet grabs the
page client-side and lands on `/extract-from-markdown`, which has its own tail and never called
it. The 42% that did have screenshots were the recipes saved by URL rather than by bookmarklet.

Lifted to a shared `_attach_page_screenshot` called from BOTH paths, before the extract-cache
write in each so the shot travels with the cached row ([[feedback_single_path]] — one capture,
not a second copy on the markdown side). Guards, each verified directly: skip when a screenshot
already exists (a cache hit must not re-shoot), skip non-http and empty urls, and skip our own
`/r/<uuid>` permalinks — a handwritten recipe's "source" is our own page, there is nothing to
photograph. Failures are swallowed; a missing screenshot must never cost the caller their
extraction. Needed a module-level `import re` that had never been there.

### The 429-row master backfill

Ran with the fixed script (the ephemeral-store bug was fixed in fc535dc earlier today).
**429 -> 214 remaining and still draining at ~4.2s/row** as this was written; media.db 4,904 ->
5,106 blobs. The stragglers are the anti-bot hosts (washingtonpost, nytimes) and the Greek
harvest sites, which is the expected tail.

### The recapture job (6a4f689)

Curator: *"we might need a recapture job for screenshots going forward (batch)."* Built as a
first-class handler + nightly schedule (limit 200) rather than another one-off script
([[project_jobs_as_executables]]) — schedulable, DB-logged, in the Job Monitor. Verified
registered on the live service after restart.

Four pickup reasons, all from data already kept: **no-shot** (never captured) · **no-blob** (the
recipe points at a `/screenshot/<id>` with no media.db row — the EXPECTED state after a restore,
since media.db is git-ignored and regenerable) · **changed** (`source_changed_at` newer than the
blob, so the image shows a page that no longer exists) · **aged** (older than `max_age_days`,
default 365).

Recapture is idempotent: `screenshot_id_for(url_normalized)` is deterministic, so a re-shoot
overwrites ONE media.db row and the recipe's `/screenshot/<id>` never changes; the recipe row is
rewritten only when it had no shot before.

**The measurement that paid for itself.** Of 197 rows that looked blob-less, **31 carry a
perfectly good screenshot stored under a DIFFERENT id** than `screenshot_id_for(url_normalized)`
yields today — the key used at capture time differed. Only 179 are truly gone. A job trusting
the computed id alone would have re-shot those 31 and orphaned the originals. The lookup now
checks the id THE RECIPE POINTS AT first, computed id second.

Census at write time: no-shot 537 (backfill still draining it), no-blob 179, changed 0, aged 0,
ok 3947, skipped 88. `changed` and `aged` are both 0 today — they exist for going forward,
exactly as the curator framed it. Steady state is near zero, so nightly/limit-200 catches up in
a few nights and then only handles drift.

### Open

- **`recipes.sql.gz` stale** — the grade repair and the screenshot backfill both rewrote rows
  after the 14:09 dump. No schema change this time, so ordinary hygiene, not the restore hazard
  the re-embed created.
- **Personal-table screenshots still 240/412 missing** — the backfill ran `--master-only`. The
  new job covers both tables and will drain it nightly; run `--personal-only` to force it now.
- **`pageScreenshot` still defaults to `""`** in `recipe_model`, so never-attempted stays
  indistinguishable from attempted-and-failed. That default is precisely what made this read as
  a 42% success rate for a month instead of a path that never ran
  ([[feedback_absent_not_zero]]). `Optional[str] = None` + a reader audit.
- Unchanged: the scoring counterpart to `check_embeddings.py`, `power_blend_weight` in
  `bcc_config.json`, provenance backfill (~4,150 NULL `mozHttpCode`), the five archive-47 rows,
  `user_api_keys`.

## Session log — 2026-08-06 (evening) — the no-struct gate is not a recall metric · domains gets the dishes list

Fourth and final block of a long day. One investigation that reversed my own recommendation,
and one UI job that closes a month-old open item.

### "dropped on no struct" — the Guardian, and why I was wrong about the rest

Curator ran a **dish** extract for mussels; a Guardian recipe was dropped `no-struct` while the
bookmarklet handled the same page fine. *"I've been seeing this quite a bit lately."*

Reproduced exactly with the harvest's own fetcher. `has_recipe_structure` needs BOTH an
ingredients marker and a method marker; the Guardian page has the method half and **the word
"ingredient" appears zero times** — its Word of Mouth column goes straight from `serves 2` into
the list. The recipe is entirely there (`1kg mussels`, `2 shallots`, `150ml dry white wine`,
`50g butter`, numbered method) and it scored **phrase=12 against a threshold of 7**.

**The structural defect is an asymmetry:** there is a header-less METHOD fallback
(`_method_verb_count` substitutes for a missing "Method" heading) but **no header-less
INGREDIENTS fallback**. So a recipe with an unlabelled ingredient list cannot get through,
however obviously it is a recipe.

The bookmarklet succeeded because it never runs this gate — the gate protects PAID harvest
fetches. The two paths disagreeing is correct behaviour, not a bug in either.

**The trust flag is cross-cutting** (curator asked, and it matters): the lookup lives inside
`_is_recipe_filter`, matching each CANDIDATE's own host, so the domains master acts as a
publisher policy layer for EVERY batch — dish batches included, not just that publisher's own
harvest. Same is true of its neighbours in that loop (`exclude_words`, `url_prefilter`,
`render_required`, `fetch_strategy`). Granted `theguardian.com`; verified end-to-end, the exact
line flipped from `DROP no-struct phrase=12` to `KEEP trust phrase=12`. Boston Globe validated
the mechanism historically: its 149 drops are all in ONE log dated 2026-07-23, before its grant,
and it now shows 226 `KEEP trust`.

**Then I proposed granting a 10-host "recipe-only" tier and was WRONG.** I ranked hosts by
no-struct drop volume (12 hosts = 990 of 2,199 = 45%) and inferred a recall problem from
publisher reputation **without ever looking at the URLs**. Sampling them killed it:

```
foodgal        dining-at-mustards-grill · tokyo-eats · a-visit-to-thomas-kellers-burgers
marthastewart  1505788/recipes · 1513477/healthy-recipes · 1502264/cookie-recipes
thekitchn      guy-fieri-net-worth · masterchef-winners · closet-decluttering-tips
epicurious     expert-advice/...-article · ingredients/what-is-greek-yogurt · ...-gallery
recipegirl     set/thanksgiving/ · set/cuisine/       delish  .../g69290004/...  (g = gallery)
```

Restaurant reviews, roundups, galleries, taxonomy pages, ingredient explainers, celebrity news.
**The gate is working.** Trust bypasses the cheap gate AND the LLM cascade catch, so granting it
would have pushed thousands of those into paid extraction.

**THE LESSON, worth keeping: the `DROP no-struct` COUNT IS NOT A RECALL METRIC.** Killing
roundups and galleries before they cost money is the gate's JOB, so a high count is mostly
success. The honest false-drop figure is the **4.2%** measured against the 31,776-sample
`training.db` corpus (JSON-LD-positive pages as ground truth), of which a quantity-token
ingredients fallback rescues only ~9.5% at a 0.5% precision cost — roughly break-even, so NOT
shipped. Caveat stated because it cuts the other way: JSON-LD pages never reach this gate, so
that population is the wrong one and may understate the gain. Finding the genuine misses needs
per-URL labelling — which is exactly what the is-recipe labeler UI and those 31,776 samples
exist for.

### The domains page gets the dishes list (a711152)

Curator: *"too garish with the brick coloring vs the nice soft grey tones which I want the whole
site to look like… change the content list to the same data and style used in dishes — they are
very similar pages and should look like it."*

Neither page has a local `:root`; both load the same three stylesheets. The brick came from
domains rendering its recipe list with a page-local `.top-recipe-*` block that had drifted much
louder — a **solid `--accent` rank badge AND a solid `--accent` "Open in BCC" button on EVERY
row**, plus `--accent` badge pills and a bold serif title. Now uses the SHARED `.ed-t10-*` row
from `editor-shell.css` (the dishes component), `<li>` in `<ol class="ed-t10">`. Retired 60
lines of dead CSS — domains was its only consumer. **Closes the open item in
[[feedback_reuse_layout_components]]**, outstanding since 2026-07-12.

Two gotchas: the row is a DIV not the `<a>` dishes uses (a domain row carries a curation
checkbox), so the NAME is the link and `.ed-t10-name` gained `text-decoration:none`. And domain
`rank_score` is **0-1** while dishes' is **0-100** in the same column — scaled, or every row
reads "1.0". Stats are score / DA / PA (what a publisher row actually has).

Also softened: the harvest mode card's selected state (was an `--accent-soft` FILL) and
`.src-group.is-active` (was an `--accent` border + 2px glow) → a quiet `--ed-paper` lift.
Left the `<b>` in help text alone — it names clickable controls, which is emphasis doing real
work.

**`editor-shell.css` + `components.css` are MANUALLY cache-busted** (11 and 14 pages). Bumped
twice this session. Any future shared-CSS edit needs the same sweep or browsers keep the old
stylesheet — the same trap that nearly shipped invisible with `library-shell.js` this morning.

Verified in the browser, not by reading: both lists screenshotted, they match; no console
errors; JS parses under `node --check`.

## Session log — 2026-08-07/08 — the public star · the export archive that was never backed up · two wrong answers about one table

Long session in two halves: designing what a USER sees of the scoring (versus what the curator
sees), then a data-preservation thread that started as a one-line query and turned up a
`.gitignore` rule quietly discarding a year of SEMrush exports.

### "Do you think blend and power are redundant?"

Curator's framing: hide the algorithm from the user surface, keep the numbers for admin, and
translate the useful scores into stars — the About page may DESCRIBE what we do but must never
name PA/DA/OU/power/volume.

Measured on 4,525 scored master rows before answering. **The redundant pair is not blend and
power — it is blend and OU.**

```
spearman   ou <-> power    +0.235      nearly independent
           blend <-> power +0.556
           blend <-> ou    +0.937      near-duplicate

as 5-star buckets:  blend vs ou     63% identical, 0.02% off by 2+
                    blend vs power  33% identical, 28%   off by 2+
```

At `power_blend_weight=30` the blend is 70% OU by construction, so showing OU stars AND blend
stars is showing one axis twice. OU and power are the genuinely independent pair, so the second
axis is spent on a **badge**, never a second scale — two continuous scales side by side invite
someone to work out the relationship between them. Populations: `hidden_gem` (OU top-20%, power
bottom-40%) 3.6%, `trusted` (inverse) 6.8%, so ~90% of recipes carry no badge, which is the point.

**3–5 stars, never lower.** Every recipe in the index already survived selection — the index IS
the filter — so a percentile band handing 20% of a curated set one star insults a recipe we chose
ourselves. Five levels in half steps.

**Cuts land on the blend VALUE, not on rank.** This started as my own mislabelling (the parameter
said "percentile", I passed the value) and the distribution came out centre-heavy rather than
flat — 12/27/24/25/12. Kept deliberately: cutting on rank forces 20% into each band, so every
time the corpus grows some recipe is demoted by arithmetic alone and a stored card silently loses
a star. Cutting on the value makes a star an absolute claim, and it makes 5★ scarce, which is
what makes a top band worth having.

**Drift was measured, and it killed my own recommendation.** Replaying corpus growth: steady
state 2–4% of rows shift one band, none shift two (the 45% at the 25% checkpoint is a corpus
composition shock, not drift). I was about to recommend freezing the thresholds — measured it,
43%/3.4%/3.1%, **no better**, because `blend` is itself built from in-cohort percentiles so the
cohort dependency sits a level BELOW any cut point. The only fully drift-free option is a
raw-scale blend, which reorders 11.7% of the corpus. Not worth it for a 2–4% problem. The fix is
provenance: a stored card keeps its stars plus a `scored_at`, and a refresh surfaces a change as
an EVENT rather than rewriting a number someone already read.

### The star control, and where the leak actually is

`input/pipeline/public_scoring.py` is the single chokepoint. **Coarsening only protects you if it
happens BEFORE the wire** — a page that receives `{"ou": 12.4, "power": 131}` and computes stars
in JS has published the method to anyone who opens devtools. The pixels are not the leak, the
payload is. `GET /public-score` returns `{stars, starFill, starLabel, badge}` and does not echo
its inputs; the accessible name is "3½ out of 5", never a number.

Geometry, from the curator's own sketch (a 100px 5-star image clipped to N%): one SVG glyph tiled
at exactly 20%, star drawn at 2..18 inside a 20×20 tile so the **padding is baked into the tile,
not added with CSS spacing**. That symmetry is load-bearing — the centre of tile k sits at
(k+0.5)·20%, so 60/70/80/90/100 land on glyph centres and inter-star gaps. With trailing-gap
geometry instead, "70%" would render as a 62.5%-filled star. The mask also means a fill edge
landing in a gap paints nothing, so a rounding slip cannot leak visually.

Admin recipe form gains a **Public rating** chip beside PA/DA/OU (stars fetched, never computed
client-side) and mirrors `editorial.scoreCommentary` read-only under the strip, live-bound to the
editable field so it cannot go stale. `forms/stars_mockup.html` renders the scale, the
distribution, a ranked list and a stored card off real corpus rows; `stars_demo.json` carries
public fields ONLY — if you can reconstruct a score from it, the chokepoint has a hole.

Verified live after restart: Kimchi (PA 41 / DA 16 / OU 24.9) → **4½ + HIDDEN GEM**, a page
massively outperforming a small domain, which is exactly the case the badge exists for.

**Known, and it blocks the card:** the existing `scoreCommentary` names the metrics outright —
*"The page scores modestly (PA 31) on a domain with moderate authority (DA 49)…"*. Fine as an
admin audit trail, unusable as public copy. The card needs a SECOND, public-voice variant; keep
both, because the admin one is what lets you check the public one is honest.

### Positioning — the curator corrected me, and the correction is the pitch

I wrote "most sites rank on popularity". Wrong, and lazy. **Publisher sites rank only their own
inventory** — their "best" is bounded by what they happen to own, rated by people already on
their site. Google is the only cross-site ranker and it ranks on search performance, not cooking.

So the differentiator is not the formula, it is: **we have no recipes of our own to promote.** A
site with inventory structurally cannot tell you a competitor did it better. And that is already
the architecture, not just copy — [[project_discover_vs_possess]]: index rather than cache, link
out always. The rights posture and the credibility claim are the same decision.

Filed as YELLOW in `docs/marketing-differentiation-brief.md`'s claims ledger — it is a
competitive claim needing a dated competitor audit before it goes public.

**Teaser bound settled** ([[project_user_as_publisher]], [[project_image_policy]]): blurred
full-page screenshot, no recipe text at all, publisher opt-out with a generated replacement
image. Blurred is *stronger* fair use than a clear thumbnail — unreadable cannot substitute for
the visit. Two things make the promise keepable: the card stores a `/screenshot/<id>` URL and not
bytes, so one flag revokes the image everywhere including cards saved months ago; and the blur
must be baked server-side, since a CSS filter ships the readable JPEG and devtools strips it.
Policy belongs on the `domains` table beside `trust_extraction` — a small enum, image-opt-out vs
full delisting, plus honouring `X-Robots-Tag: noimageindex` automatically.

### Two wrong answers about the domains table

Curator asked for domains lacking a "SEMrush extract selection", ordered by DA. I keyed on
`semrush_report_url` (252 empty) and flagged 30 domains as misconfigured. **Both wrong, and the
code says so in a comment I had not read:**

- `semrush_report_url` is **DERIVED at read time** from the `semrush_*` filter fields via
  `build_semrush_pages_url()` unless `semrush_url_uncoupled=1`. `domains_lib.py:511` explicitly
  warns not to key on it — "that link is now auto-defaulted for every domain, so keying on it
  would put the whole corpus on the worklist."
- `backlinks_dir` is an **optional per-domain override FOLDER**; blank means "use the configured
  inbox" (Downloads). recipetineats harvested 250 URLs with it `None`. Blank is normal.

The real membership signal is `harvest_source`. And `'backlinks_file'` does NOT mean
backlinks-shape — it is the legacy label for "read a local SEMrush export", carried by 101
domains, most reading traffic-ranked Top-Pages files. **The name outlived the thing it named**,
and it misled me twice in one session.

Corrected list: 219 domains not on the export flow, nearly all sitting at 1–5 master recipes with
no `last_harvested_at` — SERP-discovered, never harvested as publishers. nytimes has 81 master
recipes from SERP alone; foodandwine and williams-sonoma have zero.

### The exports were never being backed up

Curator: *"we should be keeping ALL of the export files!! they are a rich source of valuable data
that we're only skimming."*

Two mechanisms were losing them:

1. **`.gitignore` line 82 is `input/*.xlsx`, which matches only the ROOT of input/.** 35 of the 55
   exports there were untracked. The other **20 predated the rule and stayed tracked — which is
   exactly why the gap was invisible.** It looked backed up, and a fifth of it was.
2. Exports downloaded on this machine were never copied into the project at all — the harvest
   reads them in place from Downloads, so 71 existed only in a folder nobody backs up.

`input/semrush/` is a **subfolder on purpose**: `input/*.xlsx` does not match it, so the files are
tracked with no `.gitignore` edit and no exception to forget later. 110 current + 17 superseded,
24MB, and the 20 tracked ones moved as **renames** so their history followed them.

**The supersede key is (stem, database), NOT the domain.** Keying on the domain would have retired
four files that are different data: 101cookbooks has a -gr run AND a -us run; thepioneerwoman has
a backlinks-pages export AND an organic one.

**Superseded files are moved, never deleted** — `--delete` is opt-in and unused. Newest is not
always richest, and three real cases prove it: marthastewart 74KB → 26KB *the same day*,
seriouseats 1013KB → 667KB, tasteofhome 1024KB → 837KB. That is what a re-run with a narrower
filter looks like, and SEMrush will not re-issue the wider one. Those three still want a human.

`collections_lib.archive_export()` fires from `_from_backlinks_file` at the point an export is
CONSUMED — the one place every export harvest passes through, and after a successful parse so a
corrupt download is never archived as good. Non-fatal: a harvest must never fail because
bookkeeping did. `scripts/collect_semrush_exports.py` is the batch sweep, dry-run by default,
sharing the key with the job so the two cannot disagree.

**Two bugs of my own on the way.** I archived superseded inbox files without checking WHY they
were flagged, which re-copied ~70 files already filed (9MB of duplicates) — guarded on
`why == "superseded"` and removed them by hash. And **Chrome's `" (1)"` suffix defeated the name
parser**, silently skipping `thepioneerwoman.com-backlinks_pages (1).xlsx` rather than archiving
it; stripped before matching now. That one would have recurred forever.

`--clean-inbox` deletes an inbox copy only on a **sha256** match, never a filename match —
`allrecipes.com-backlinks_pages.xlsx` exists in two different versions under one name, so a name
match is a false guarantee. 73 removed, 10.8MB reclaimed, 0 unique.

### Backlinks ranking retired

Curator: *"I don't think we are using backlinks anymore!"* — right, and the data sharpened it. 194
harvests ranked by traffic vs 37 by referring domains, and every backlinks-shape FILE is from
2026-06-01..21. **But it was still running:** thekitchn and allrecipes both harvested on
2026-07-22 off June backlinks files, because those were the only exports they had.

`backlinks_file_path` now skips the legacy shape and prints which file it ignored. Four domains
(allrecipes, thekitchn, themediterraneandish, edibleboston) needed a fresh Top-Pages export;
three that had BOTH shapes now pick the organic one **by shape rather than by whichever was
downloaded last**. Not removed and not unreachable — the reader still handles the shape, and an
explicit per-domain file override still uses it ([[feedback_no_silent_removal]]).

Confirmed end-to-end in production the same evening, unprompted: job 762 refused allrecipes'
June export and said so; job 763, a minute later, read a fresh Top-Pages export (360 URLs by
traffic) and the hook filed it. bbcgoodfood (320) and toriavey (240) likewise.

### The video that 404'd on our own host

*"I just tried to play the video on Kenny halal cart chicken and rice and get a not found."*

`video.contentUrl` was **site-relative** — `/kenjilopezalt/posts/this-halal-cart-153948287` — and
the form assigns `link.href` straight from it, so the browser resolved it against OUR origin.
Same bug class as the hero-image relative-URL fix (2026-06-29); video was not included in that
pass, and it fails louder than an image does — a broken image hides its box, a broken link sends
the user to a 404 on our site.

Fixed in three places: extraction absolutizes video URLs beside the existing image handling; the
form falls back to `_source.originalUrl` when `contentUrl` is not absolute (so old rows work with
no migration, and both copies of `showVideo()` were patched); the one row backfilled. Scope was
genuinely small — ONE relative contentUrl across ~5,100 recipes with video, plus one
protocol-relative thumbnail that has no filename and already degrades correctly.

**Judgement call:** `urljoin`'s standards-correct answer duplicates the creator segment
(`…/kenjilopezalt/posts/…`), and the original path is patreon.com's URL shape rather than this
custom domain's. Patreon 403s every probe INCLUDING the recipe's own source page, so neither
candidate is verifiable by fetching. Rather than guess, the row points at the source post —
known good, and the page the video plays on. The generic fix still does the standard `urljoin`.

## Session log — 2026-08-09 — scoring under scrutiny: three bad comparisons of mine, a traffic axis that survived, and a doc claim that cost the credits

A long analysis day that went badly before it went well. The curator caught every error; the
findings that survived are theirs more than mine.

### Calibration — two follow-ons to the public star

**DA commentary calibration.** A critique called a mid-60s DA publisher "a marginal site".
Measured: DA 65 is the **exact median** of the corpus (p10 33 · p25 47 · median 64 · p75 82 ·
p90 92) and the 74th percentile of curated publishers. Root cause was calibration, not wording —
`scoreCommentary` got raw numbers and no reference, so the model judged 65 against "the scale
runs to 100". The prompt now receives `>> DA IN CONTEXT: 50th percentile of our corpus ->
well-established`, plus an instruction to judge only on that and reserve "small/marginal/obscure"
for the bottom tenth. `authority_corpus_context()` in url_scoring, cached per process.

**paid_pa_calibration is now a JOB** (monthly, 720h). The paywall PA-remap had not been
recomputed since 2026-06-23 — seven weeks, silently, because a stale calibration mis-ranks
rather than erroring. `run_calibration()` extracted as the shared entry point so CLI and job
cannot drift; `calibrate()` returns per-domain results carrying the REASON a publisher was
skipped. First run: flagged 6, calibrated 4, skipped 2 (`177milkstreet` n=10, `capecodtimes`
n=6, against the n>=15 floor). latimes.com was flagged paywalled and had NEVER been calibrated.
`harvest_missing` defaults FALSE — a scheduled job must never surprise-spend.

**Also corrected `memory/project_paid_pa_calibration`**, whose Status said the SELECTION half was
unbuilt. It shipped 2026-06-16 (ee7b8da). The real gaps are coverage (4 of ~96), staleness (now
fixed) and the paywall-only TRIGGER.

**adjustedPageAuthority reaches the form** (code-complete, needs verification): both selectors
already rank on the remapped PA, but the form showed raw PA and an ouScore derived from it — an
ATK page displayed **PA 36 / OU -5.0** while selection scored it **50.9 / +10.0**. Derived on
read, never stored, because the calibration now moves monthly.

### THE STRUCTURAL FINDING — exceptionalism needs a varying baseline

In a PUBLISHER harvest DA is constant, so `OU = PA - g(DA)` is `PA - constant` whatever g is —
power law or quadratic. Verified: `PA - OU` = **46.345 with spread 0.0000** across all 143
allrecipes rows; **spearman(PA, rank_score) = 1.0000**. The 70/30 blend reproduces PA ordering
exactly. That is not a defect — PA is a good within-site ranker (below) — but the OU machinery
contributes nothing in that path. It does real work in a DISH cohort, where DA varies.

### Three bad comparisons, all mine, all pushing the same way

I built a case that the scoring was useless. Each step was wrong:

1. **Compared DA to recipe inventory using newspapers and a health site** — washingtonpost,
   latimes, healthline. Their DA measures news authority. Including them manufactured the result.
2. **Mixed `-gr` exports into a US analysis** — bonappetit showed *zero* recipes above 1k/mo
   because it was a Greek-database export of a US site. I had identified that exact trap earlier
   the same day and walked into it.
3. **Used Pearson on log-distributed traffic**, turning a **7.2x** effect into "+0.36, weak".

Corrected: among recipe sites on the US database, **spearman(DA, recipes >=100/mo) = +0.711**.
The curator's keep-formula premise holds. And PA carries strong within-site signal — allrecipes
PA>=60 median traffic **28,112** vs PA<=52 **3,925**, cleanly monotonic across bands.

Curator: *"you check your own conclusions more closely next time."* The check I skipped: verify
the populations are comparable, and sanity-read whether a number is plausible for a recipe site.
bonappetit at zero should have stopped me instantly; I printed it in a table and reasoned from it.

### How deep to skim — measured properly

Curator's experiment: take the top 10 by blended score at increasing sample depths and count new
entrants. Ran it on 10 recipe sites, then bought honest ground truth with two score-only runs at
depth 1000 (recipetineats, budgetbytes, ~$3.80).

    recall of the deepest top-10, by skim depth
      recipetineats   250 -> 90%    360 -> 100%     (export 2,688 rows)
      budgetbytes     360 -> 80%    500 -> 100%     (export 3,336)
      allrecipes      750 -> 90%   1000 -> 90%      (export 10,000, still climbing)

Roughly **6-12% of the export gets 90% of the best 10**. An earlier "250 saturates" was an
artifact of ground truth only 250 deep — measuring agreement with itself. Current configs land
60-90%, worst on the biggest sites: allrecipes at 3.6% depth returns 60%, so 4 of its best 10 sit
below where the harvest stops.

### TRAFFIC EXCEPTIONALISM — the curator's idea, and it survived

*"is there an exceptionalism buried there... similar to the OU but for traffic not PA."*

**The baseline is PA, not DA.** DA explains only **13%** of per-page log-traffic variance and its
band medians are non-monotonic. PA explains **33%**, and `corr(PA, log traffic)` is **positive in
56 of 56 publishers**.

    TU = log10(traffic) - f(PA)      f: -0.000374*PA^2 + 0.09992*PA - 0.8308, R^2 0.330

Near-independent of OU (**+0.166**) — a genuine third axis. Top TU is the shape the product
exists to find: loveandlemons/chimichurri (PA 35, 106k/mo), argiro.gr/giouvarlakia-avgolemono
(PA 34, 51k/mo — the curator's own dish). allrecipes' 660k/mo page does NOT appear, because PA 64
predicts it.

**Caveat that matters: 71% of TU variance is BETWEEN sites, 29% within.** Raw TU is a publisher
badge; site-centred TU is the page signal. Newspapers score -2.0 to -2.2, so their recipe pages
are not merely under-linked, they are **not read** — a stronger reason to discount them than OU
gave. Filed as `memory/project_traffic_exceptionalism`; parked pending data supply.

One-month traffic is NOISY (seriouseats, 4,982 URLs a month apart): median moves 1.03x but
**39% swing >=1.5x, 24% >=2x, 13% >=3x**; 82% of the top decile stays in the top decile.

### The doc claim that cost the credits

Curator: *"if you look back at the state file you'll see we burnt all the credits."* Correct —
50,000 -> 26,490 on 2026-08-04, ~17,500 of it on two unbounded `url_rank_history` calls where
`display_limit=12` would have been 600.

I had told the curator six-month history was unavailable on this plan, having re-run the probe
and read `ERROR 403 :: History reports are not allowed` at face value. **Section 12 of the design
doc already carried that as disproved** — the 403 is a WRONG TYPE NAME. Third time that error has
been made. It also went into a memory before being caught.

Then the corollary. That same doc sentence claimed `display_filter` is ignored on
`domain_organic_unique`. Since its companion claim was already known false, I tested it: sorted
`tg_asc`, unfiltered bottom rows `0,0,0,0,0`; with a `Tg > 1000` filter they are
**1001, 1006, 1013, 1021, 1022**. **The filter works.** 100 units to find out.

That changes the economics of the thing the curator actually wants — *"it might get rid of me
having to do the web based search/save/process dance"*. Filtering server-side means paying only
for rows above a traffic floor (pinchofyum 1,661 -> 451; seriouseats 7,099 -> 3,154), and
`display_offset` pages past the 10,000-row export cap. A ~96-publisher refresh at a 100/mo floor
is **~50,000 lines ~= 500,000 units** against a 2M minimum package — about four cycles a year,
matching `harvest_ttl_days=90`. I had costed only the traffic-backfill case and called the
package 15x over-provisioned; that was wrong too.

Semrush is now **Adobe** (acquisition completed April 2026). No startup programme found. Units
come in 2M/5M/10M/20M packages at roughly $50/M (third-party figures; Semrush does not publish
them). The curator's read that the 50k never refreshes is almost certainly right: the smallest
package is 2 MILLION, so 50k was a grant, not a purchase.

### Also today

- **URL-field controls rebuilt** — icons moved INSIDE the input (the old `flex-wrap:wrap` made
  them stack on a phone), 30px/36px, a ✕ clear that dispatches `input`+`change`, hover/focus
  reveal of the full URL that flips above when the keyboard would cover it.
- **Checkbox/radio sizing** — no shared rule existed; browser default ~13px. Now 20px/24px with
  a 44px touch row. This cost money: a domains harvest ran twice in score-only from a phone
  because the curator could not see the checkbox state.
- **The score-only trap closed** — the harvest MODE cards set `score_only` silently; the run
  button now reads "📊 Score only — ingests nothing" in warn red BEFORE the click.
- **Dish query rows phase 1** — `{q, n, gl, hl}` with lazy migration; `queries` still returns
  plain strings because `compose_dish_text`/`dish_signal`/`identity_card` all do `str(q)`.
  Locale work then PARKED: curator wants US nailed first.
- **Video `contentUrl`** — a site-relative path made the play link 404 on our own host.

### The pattern

Yesterday's lesson was mechanisms asserted without measurement. Today's is narrower and worse:
**three separate analyses contaminated in the same direction, and a documented correction
re-broken because I trusted a fresh error message over our own written record.** What worked was
the curator refusing each conclusion, and one 100-unit test against a claim that had already been
caught lying once.

---

## Session log — 2026-08-10 — the product thesis gets named, and keyword data says which dishes to chase

Half a day of strategy with one shipped fix. The strategy half matters more: several
questions that had been circling for weeks resolved, and they resolved through the
curator's reframings, not mine.

### Shipped — adjustedPageAuthority on the recipe form (ee5856f)

Verified live after restart. Both SELECTORS already ranked gated publishers on their
remapped PA; the form showed the raw number, so an ATK page displayed **PA 36 / OU -5.0**
while selection had scored it **50.9 / +10.0** — and `scoreCommentary` was writing its
verdict from the wrong figure.

    gated + calibrated   ATK 34->46.8 · bostonglobe 41->51.7 · latimes 50->57.4
    not calibrated       allrecipes, recipetineats, smittenkitchen -> None
    above the free line  cooking.nytimes 49 -> None   (max(pa,...) one-directional)

Derived on read, never stored — the calibration now moves monthly, and the same page
mapped 36->50.9 in the morning and 43->63.1 after the job re-measured it.

**Process note worth keeping:** I twice reported a restart as done when it had not
happened. `bcc_restart.bat` self-elevates, so run non-interactively it silently no-ops on
the UAC prompt; and polling an endpoint that already existed proves nothing. **Check the
service PID's start time against the file mtime.** Both times the curator had to catch it.

### THE PRODUCT THESIS — named, and it settles several open questions

The arc: *"we're like a reviews site"* -> *"I don't think there's enough in the review to
warrant the paywall"* -> the real stack.

    1  curated access   the algos already ran; here are the best, ranked
    2  capture          it becomes YOUR book
    3  your own stuff   selections, creations
    4  optimizations    cook view, voice, tips -- icing

Curator: *"that combined to me is why I want to be a member of this club."* The review is
NOT the product; the membership is. That reordering resolved three things at once:

**The paywall line.** Free to browse, membership to KEEP. The conversion moment is
**capture**, and it sits UPSTREAM of the click-out — someone saves on our page before
going to the source, so "we send them away" stops being the objection it looked like.
The source link is ATTRIBUTION, not the destination; nothing obliges us to make it the
loud button.

**The free layer is not generosity, it is the channel.** Our own calibration data: gated
pages earn 12-16 fewer PA points because they collect no links (ATK +15.6, bostonglobe
+12.3, milkstreet +11.0). Gate the ranking and you inherit the exact tax we built a
calibrator to undo — no links, no organic, every visit paid.

**JSON-LD: `ItemList` + `Review`, NEVER `Recipe`** on a master. Recipe markup requires
`recipeIngredient` + `recipeInstructions` for rich results, which is the content the
teaser bound excludes, and it would put us in the SERP competing against the publisher
whose link we are supposed to be sending traffic to. Recipes we AUTHOR get full Recipe
markup legitimately.

Legal position checked and it is the curator's: ingredients are facts (*Publications
International v. Meredith*), method is functional, headnotes are protected expression.
But **all 4,811 stored `recipeInstructions` are the publisher's verbatim prose** — only
the 15 rows with `_cook` carry OUR expression. So publishing method is a SUPPLY problem
(cook-rework has covered 0.3% of the corpus), not a legal one.

**The bookmarklet is the freebie that makes it work** — it captures from ANY site, so a
new member gets value before our coverage is deep, and it is retention (they never have
to leave the club to keep a recipe found elsewhere). iOS install flow already handles the
popup-inside-the-gesture case. Gap: no PWA `share_target`, so Android users cannot capture
from the native share sheet.

### THE MARKET — measured, not asserted

Curator: 300M+ searches in 30 days involving "recipe". True, but not addressable — most
is *"chicken recipe"* (cook now), not *"best chicken recipe"* (choose). Measured the
comparison slice on `docs/Recipe_all-keywords_us_2026-08-10.xlsx` (top 10,003 US
keywords, 129.4M total volume):

**ratio = vol('best X') / vol('X') = 8.1%** across 263 matched pairs. So roughly 25-28M
searches/month of genuine comparison intent — still a large market, and the one where
this product is the answer rather than a worse version of what exists.

Three findings that matter more than the headline:

* **Comparison queries are EASIER.** `best pancake recipe` KD 45 vs `pancake recipe` 68;
  `best chocolate chip cookie recipe` 59 vs 70. Publishers optimise the head term and
  ignore the comparison. That gap is the door.
* **They carry COMMERCIAL intent** (`1,0` vs `1` alone) and ~2x the CPC. Choosing-frame
  searchers — which is also where the equipment and gourmet-ingredient blocks belong.
* **Ratio tracks a KNOWN FAILURE MODE.** Dry pork chop, grey prime rib, weeping deviled
  eggs, gluey mashed potato, grainy mac sauce. Every LOW-ratio term is a FORMAT, not a
  dish: `air fryer recipes` 1.2%, `instant pot recipes` 0.7%, `dinner ideas` 0.7%,
  `recipes` 0.4% — there is no single best CATEGORY.
  **Rule: harvest dishes with a contested technique, not appliances or occasions.**

Three dishes where `best X` OUTDRAWS `X`: **ramen 272%, sushi rice 149%, burger 100%.**

### The gap list (docs/harvest-gap-best-intent.md)

66 uncovered keywords at ratio >= 10%, base >= 10k — **715,900 'best' searches/month**,
against 542,500 across the 155 existing dishes. More addressable comparison demand than
the entire current catalogue.

Softest doors: `grilled cheese recipe` (36% ratio, **KD 37**), `pork chop` (37%, KD 42),
`prime rib roast` (24%, KD 47), `deviled egg` (20%, KD 48).

**Cross-referenced against `dishes.queries`, NOT titles** — the curator's correction, and
it fixed two errors: title-matching paired *Broccoli* with `broccoli cheddar soup` and
missed the 1.22M-volume singular `chocolate chip cookie recipe` the dish already targets.
Queries are what people type; titles are labels.

### The spec (docs/dish-candidates-from-keywords.md)

Automating dish selection from keyword data. **PROPOSE, NEVER CREATE** — a dish decides
what gets harvested at all and mints an immutable join key, the same objection the curator
raised about invented product categories ([[project_curate_staff_inputs]]: *"nothing
automated should be inventing them"*). Writes to a candidates table + review surface;
approval creates the dish.

**THE TRAP recorded so nobody re-makes it:** do NOT put `best X recipe` into
`dishes.queries`. That SERP returns roundups and listicles — exactly what the harvest's
collection/listicle filter discards. The head term is how we FIND recipes; the comparison
term is what our resulting PAGE ranks for. Two different fields; the spec proposes
`target_keyword`.

Dedupe must be SEMANTIC via `dishes_vec` / `find_similar_dishes`, not string matching —
string matching already produced two wrong answers in this very analysis.

### The pattern

Yesterday: contaminated comparisons and a documented correction re-broken. Today the
errors were smaller (two false restart claims) and the value came from the curator
reframing the question three times — reviews site, then membership club, then match on
queries not titles. Each reframe dissolved a question I had been answering the hard way.

---

## Session log — 2026-08-10 (evening) — the ramen re-run, a percentile that was really zero, and the AI editor named

### The ramen pass, judged then re-run (jobs 794 → 795)

Reviewed 794 and the funnel ratio flagged as suspect was **not** the problem. The
binding constraint was the min-OU floor: 48 candidates cut to 26, then 20 taken from
26. Two of the four queries were the trap recorded that morning — `Best Ramen Recipes`
fed a SERP the listicle filter is built to discard (it caught food.com's "29 Best Ramen
Recipes", foodandwine's "17 Cozy Chewy Ramen Recipes", justonecookbook's own hub). In
fairness `Best Ramen Broth Recipe` behaved WELL — the "broth" qualifier made it a
component query, and it delivered the tonkotsu pages.

The verdict on 794: **OU measures link-earning exceptionalism, not whether a recipe is
the best version of the dish.** #1 was `thesaltymarshmallow` sesame-garlic instant
noodles (6 ingredients, 6 steps) — DA 51/PA 50 punching above weight. Adam Liaw's Ramen
School, Serious Eats' miso butter, 101cookbooks' vegan ramen and tasteofhome's
from-scratch NOODLES were all below the floor.

Queries replaced with head + component + three variants (`Ramen Recipe`, `Ramen Broth`,
`Tonkotsu`, `Miso`, `Shoyu`), `top_n_serpapi` 30 → 25. Result:

    union      66 -> 106        the 4 old queries overlapped 45%
    to Moz     50 -> 78
    survivors  26 -> 43         20 chosen from 43, not from 26
    #1         instant-noodle toss -> seriouseats Tonkotsu Broth
    #2                             -> justonecookbook Miso Ramen

Shortcut bowls fell from ~10 of 20 to 4. New entrants are the authentic tier —
mealsbymolly (DA **19**, top exceptionalism 91.2), sudachirecipes, gastroplant's vegan
tonkotsu, honestcooking (35 ingredients / 20 steps), Serious Eats' pressure-cooker
chintan shoyu.

**But the floor still discards good work**, now demonstrated on a clean query set:
epicurious tonkotsu (-1.49), doobydobap (-2.75), joshuaweissman best-tonkotsu (-3.75),
seriouseats miso-butter again (-3.62). 35 of 78 dropped. That is the case for §AI editor.

### A percentile that was really zero (26 rows)

Chased the log line claiming a row was "saved outside a dish batch" while sitting inside
one. Not a message bug — data loss. `_sanitize_scoring` strips 0.0 as proof-of-unmeasured,
but `PERCENT_RANK` gives **exactly 0.0 to the bottom-ranked row of a cohort**. 26 master
rows had lost a true percentile, each the lowest-power row of its own dish, each stamped
with a false explanation. `fieldN` now decides: no cohort → strip; cohort → the zero is
real. Fixed BEFORE the re-run so 795 wrote correct data. (795 did not exercise it — its
minimum was 13.1; the unit test covers all three cases.)

### The embedding moved to the write

Curator: *"shouldn't the embedding logic run right thru the sql update logic path so
nothing gets missed"* — correct, and it had already been missed. The refresh lived in the
two HTTP endpoints, so a script, a job or the jobs CLI could change `queries` and leave
`dishes.embedding` + `dishes_vec` describing the old ones, silently. Now inside
`create_dish`/`update_dish`. Cheap unconditionally — `ensure_dish_embedding` is
content-addressed.

### THE AI EDITOR — docs/ai-editor-mediation.md

Curator's design: statistical first pass, then hand the kept set AND the rejections to an
AI editor for mediation both ways, thumbs up / thumbs down, mediation log kept for
verification. Cost is not a constraint (low volume, every run). Four findings constrain it:

1. **The reject pool is not persisted.** `dish_rejects` took 1 of 39 drops on 794 and 1
   of 61 on 795. The rest live only in a log file. **Phase 0 is the ledger, not the AI.**
2. **Editor's Choice is candidacy, NOT override** — `_pinned` is written twice and read
   NOWHERE, so a pin must clear every gate again including the floor we want overturned.
   Thumbs-up has to be an override with a recorded reason. Affects curator pins too.
3. **Capture at run time** — a reject's Moz score and content are in NEITHER cache; free
   at the moment of the drop, paid for afterwards.
4. **`public_scoring.py` already settles the stars.** 3.0–5.0 half steps (never below 3 —
   the index IS the filter), cuts on the blend VALUE not rank, frozen when stored. So the
   AI judges WITHIN the cohort but emits an ABSOLUTE rubric band, and thumbs-down means
   removal from the set, not one star.

Rule: **overturn judgments, not facts.** Rubric axes: dish fidelity (grounded in the
identity card — it alone would have caught the ramen #1), method completeness, craft
specificity, source trust, and failure-mode coverage — the comparison ratio tracks a KNOWN
failure mode, so the property that makes a dish worth ranking is what the ranking should
reward.

### Also

- **SerpApi cancelled**, verified safe: `serp_provider=scaleserp`, no code calls the
  Shopping/Amazon engines the $25/mo was held for. Fixed a stale guard —
  `detect_recipe_path` gated on `SERPAPI_KEY` and would have silently assumed `/recipes`
  for every publisher once that key left `.env` (Milk Street: 45 pages found vs 0).
- **Pagination measured** — `/recipes` 4,829 rows, ~0.8s server, 4.8 MB with `summary=1`
  and **83.2 MB** without (that is the default). `/dishes` and `/domains` have no limit at
  all but are 156/321 rows. The list DOM is uncapped. Not a one-liner: search and sort are
  client-side over the full cache.

---

## Session log — 2026-08-11/12 — the candidate ledger, the AI editor's first verdicts, and three bugs wearing one symptom

### Phase 0 + 1 of the AI editor (docs/ai-editor-mediation.md)

**The candidate ledger SHIPPED** (`run_candidates`, `input/pipeline/candidate_ledger.py`).
Nothing new had to be computed: `build_batch` already returned every dropped entry as a
full dict and threw away all but the counts. The timing is the point — at the moment of a
drop we hold url/title/rank/DA/PA/OU, and afterwards it costs money, because the
OU-dropped URLs are in NEITHER the Moz cache nor the extract cache.

A new table rather than a wider `dish_rejects`, because the grain differs: that is a
curator worklist (dish-scoped, current-state, status lifecycle); this is a run audit
(run-scoped, immutable, winners included). **`overturnable` encodes the mediation rule as
data at write time** — the editor may argue with an inference (the OU floor, a detection
failure, a missing measurement) and never with an observation (blocklist, listicle,
skip-thin). Unknown reasons default to NOT overturnable. `rank_cut` names the class that
cleared every gate and merely missed `top_n_final` — the cheapest promotions, previously
invisible. Wired for BOTH dish and publisher runs; the publisher half needed less, because
`collection_members` already keeps the scored losers (`selected=0`) — what was missing
there is only the pre-scoring drops.

**Shadow mediation SHIPPED** (`input/pipeline/ai_editor.py`, `run_mediations`,
`ai_mediation` job). First real run on Ramen/795, opus-4-8, **$0.44**, 20 verdicts:
demoted all six shortcut bowls with cited evidence ("no broth at all"; "700ml store
chicken stock + soy + Worcestershire"), moved honestcooking's 12-hour tonkotsu **#19 -> #1**
and Serious Eats' chintan shoyu **#18 -> #3**, nominated exactly the drops that looked
wrong by hand, and **flagged glebekitchen for a reason nobody here had noticed**: it calls
for 8 cups of tonkotsu broth and links out rather than making it.

**The design decision that matters: there is no `promote` verdict.** A kept row has been
extracted; a dropped row has not, so all we hold is a title and three numbers. The editor
can argue convincingly that something kept is WRONG and cannot argue that something
dropped is RIGHT. Hence hold/demote/flag on winners and **`nominate`** on a drop, meaning
"worth paying to fetch and judge". `applied=0` throughout; `apply=True` RAISES.

**Open question for the curator: 19 of 20 ranks moved and 6 of 20 were demoted.** Whether
that is a real finding or miscalibration is the question shadow mode exists to answer.

### Three bugs that all looked like "the bookmarklet is broken"

1. **The JSON-LD fast lane accepted a recipe that wasn't there.** Barefoot Contessa saved
   with ONE ingredient — a newline-joined lemon/oil/seasoning string — and one instruction:
   the salad VINAIGRETTE. The site renders ingredients client-side. `jsonld_to_recipe`
   already promised to return None on thin markup; its bar was "non-empty", so one
   ingredient passed. Now >=2/>=2, the SAME thresholds the cache uses — and the cache was
   already refusing this exact recipe while the extract path called it a success. Fixed in
   `_has_required_fields`, so all FIVE lanes inherit it (three in save_recipe_api, two in
   enrich/api — the batch path, which is why it failed twice). Blast radius: 2 of 4,850.
   VERIFIED LIVE 2026-08-12: `not eligible (only 1 ingredient(s) in the markup) -> fall
   back to LLM`, full recipe recovered.
2. **The screenshot was 60-75% of every interactive extract** (22s of 27s; worst 31s of
   43s; 6s when cached). Now deferred to a background thread on the bookmarklet path, with
   `/screenshot-status` for the form to poll and a save-time backstop. Nothing is stamped
   optimistically — 45 of 45 captures failed in one recent refresh job, so a URL pointing
   at a blob that may never arrive asserts something false.
3. **An unusable pop-up swallowed a finished capture.** THIS was the actual egg foo young
   hang, not the screenshot. Reproduced in a real browser: script and server are healthy.
   The early hand-off is wrapped, so its failure is silent; then the else-branch
   `popup.location.href = ...` was NOT wrapped, threw, and skipped `uploadScreenshot` —
   which is exactly why the log showed stage-markdown 200 and then nothing at all. Now
   wrapped, with a rescue banner carrying the token (a plain anchor: nothing to block, and
   the source page is not navigated away). **Root cause of the dead pop-up still unknown.**

### The screenshot was photographing the wrong page

Curator, on a paywalled ATK recipe they were signed in to: the saved screenshot showed the
paywall. The server captures with headless Chromium, which fetches anonymously — it was
never photographing the curator's page at all.

**The right image was already being taken and thrown away.** The bookmarklet renders the
page with html2canvas IN THE USER'S OWN BROWSER and uploads it to `/stage-image`, where it
was used only as a fallback input for vision extraction. `/stage-image` now also adopts it
as the page screenshot. This is the principle the HERO image has followed all along —
`stage_markdown_endpoint`'s own comment says it uploads hero bytes "from inside the user's
authenticated session (paywall-aware)".

`crop_above_fold()` frames it to the same window a headless capture produces (derived from
`VIEWPORT_W`/`CAPTURE_HEIGHT`, so both paths move together); every shape lands on 800x427.
The crop is a SEPARATE derived copy — `entry["image_b64"]` is untouched, so vision
extraction still gets the full-length capture. Consequences: the bookmarklet path launches
**no headless Chromium at all**, and the deferred capture stands down when a
browser-rendered one arrived first, so it cannot finish late and overwrite the good image.

VERIFIED LIVE on a real ATK recipe: `adopted the browser-rendered capture`, no deferred
capture line. Blobs overwrite in place (deterministic key), so a re-grab fixes an existing
row's screenshot without even saving.

### Also

- **`_sanitize_scoring` was deleting real zeros.** `PERCENT_RANK` gives exactly 0.0 to the
  bottom-ranked row of a cohort; 26 master rows had lost a true percentile, each stamped
  with a note blaming "saved outside a dish batch" while sitting inside one. `fieldN` now
  decides. **The 26 damaged rows are NOT repaired.**
- **The dish embedding moved into `create_dish`/`update_dish`.** It lived in the two HTTP
  endpoints, so a script or job could change `queries` and leave `dishes_vec` describing
  the old ones. Curator's catch.
- **SerpApi cancelled**, verified nothing called it; fixed a stale `SERPAPI_KEY` guard in
  `detect_recipe_path` that would have silently assumed `/recipes` for every publisher.
- **Ramen re-run (795)** with sourcing queries instead of the top-4 SEMrush keywords:
  union 66 -> 106, survivors 26 -> 43, #1 went from an instant-noodle toss to Serious Eats'
  tonkotsu. **The queries were the curator's top-volume keywords — which is the right
  selector for `target_keyword` and the wrong one for `dishes.queries`.**

### The pattern

Three separate causes wore one symptom ("the bookmarklet is broken"), and I attributed the
hang to the screenshot on circumstantial timing before reproducing it. The reproduction —
driving the real bookmarklet against the live server in a browser — settled in one step
what two rounds of log-reading had guessed at. **Reproduce before attributing.**

---

## Session log — 2026-08-12 (afternoon) — the paywall correction rebuilt on the bar not the page, and a migration that ate the data it was protecting

Started from a curator question about the bookmarklet: a paid site's recipe gets no paywall
adjustment on the interactive path, so the best recipes sink. It ended up replacing the
whole correction, and the replacement was arrived at by the curator overruling me three
times running.

### The old correction was wrong, and the curator caught it before I did

The shipped remap rewrote PA to a "free-equivalent":
`free_mean + (PA − paid_mean) · (free_std / paid_std)`. Preview said a Boston Globe pilaf
would go from OU **+7.96 → +31.70**. The curator's reaction — *"that would be the highest
in the whole system… I can't imagine that's the world's best recipe"* — was exactly right:
measured, the maximum OU across 4,896 master rows is **+25.38**, and **zero** rows sat
above 31.66.

Two faults. The slope `free_std/paid_std` divided a **pooled** free sigma (gathered across
a DA±8 window, so carrying BETWEEN-site variance) by a single publisher's **within-site**
sigma — apples to oranges. It pinned at its 2.0 cap for 3 of 5 publishers and doubled every
page's distance from its publisher's mean. And it was unbounded: PA ≥ 70 on a DA-91
publisher would have emitted an impossible PA > 100.

There was already a σ-floor guard in `calibrate_paid_pa.py` naming this exact case
("Boston Globe at raw σ 1.8 → slope 3.24 → #1 of 249, an over-correction"). It fired. It
just didn't cut far enough.

### The curator's diagnosis beat mine: fix the BAR, not the page

My instinct was to tune the slope. The curator's was better: *"we might just adjust the DA
on these sites down some amount to estimate the impact of the pages being behind a paywall
while the domain itself is not."*

That is the right frame. DA is measured across the WHOLE domain — bostonglobe.com is DA 91
on the strength of its free news — while the recipes sit behind the wall, so OU judges a
gated page against an ungated domain's expectations. **Discount the DA; leave PA alone.**
PA is the one thing actually measured.

`pa_gap_v1` (`input/pipeline/paywall_calibration.py`):
`gap = mean_free_PA(DA) − mean_publisher_PA`, then
`adjusted_DA = ou_bar_inverse(ou_bar(DA) − gap)`. One measured quantity, one bounded
transform, nothing extrapolated. `gap ≤ 0` → no adjustment at all.

### Three corrections from the curator, each of which changed the answer

**"Why should it change… it is still evidence."** I had decided master_recipes was a
survivors' pool (dish runs keep pages averaging PA 47.6 and drop pages averaging 34.4 —
a +13.2 selection gap) and rewired the calibration to read the candidate ledger instead.
The curator pushed back. Testing it settled it against me: the ledger is ~75% rejects, so
it compares a gated publisher's fresh top-traffic harvest against **other runs' discards** —
free-peer pools of n=38 and n=23 averaging PA 45.3/50.3, versus master's n=268/n=114 at
54.6/62.2. It reported cooking.nytimes.com OUTSCORING its peers by 12.7, which is
composition, not signal. Reverted to master_recipes. The requirement was never
"unselected" — it is that **both sides pass through the same selection regime.**

**"The 6-point band IS meaningful for ranking."** I twice called Boston Globe's PA spread
near-noise. The by-DA table killed it: the Globe's σ is 2.75 over a 16-point range, which
is *mid-pack* — free cohorts at DA 84, 71 and 69 discriminate LESS. σ 2–5 is simply what
PA discrimination looks like everywhere here. The same table exposed the real defect: no
single-DA row is anywhere near the 5.98 "free sigma" the old method used, confirming it was
pooled.

**"Persist it — I might want a report writer to just query it."** I had carried forward the
old code's "derived on read, never stored" rule. The curator's two reasons both land: a
value that only exists inside a Python call is invisible to SQL, and recomputing erases
which adjustment was in force when a page was scored. Staleness is handled by **re-stamping
on recalibration**, not by refusing to store. Now `restamp_recipes()` plus five generated
columns (`adjusted_da`, `adjusted_ou_score`, `paywall_discount_pct`, `paywall_adj_method`,
`effective_ou_score` — the last COALESCEs to raw OU so a query can rank on one column
without knowing which publishers are gated). Written up as
memory/feedback_persist_derived_values.

### Two gating attempts of mine that were wrong

An `n` floor didn't catch NYT. A standard-error test was worse — SE shrinks with √n, so
NYT's meaningless 2.21 gap scored z = 3.32 and sailed through, while I had *written the
comment* arguing effect size and then implemented precision-of-the-mean. The gate that
works measures the gap against the ordinary page-to-page spread (Cohen's-d style) **and**
requires the sign to survive the peer-window choice — the check that actually caught NYT,
which reads +2.21 (starved) at exact-DA and −0.80 (not starved) at DA±2.

### Gated is not the same as penalized

cooking.nytimes.com cannot be fetched without the unblocker, yet averages PA 59.8 against
free DA-95 peers at 58.9. It is linked heavily enough to overcome its own wall. So
`paywall` stays the FACT (is it gated) and `paywall_da_discount_pct` is the measured
JUDGMENT (is it penalized, and how much) — **neither derived from the other.**
eatingwell.com's flag was pulled on the fact that it is not gated at all.

Final state: **2 of 7 adjusted.** ATK −56.5% (gap 16.2, effect 5.17), Boston Globe −27.0%
(gap 8.0, effect 1.17). Milk Street low_confidence (n=6), NYT + LA Times inconclusive,
cookscountry + capecodtimes no_rows. Max effective OU corpus-wide stays **+25.38** — no
manufactured record. The pilaf lands **+15.92** with PA untouched at 54. ATK goes from
**0 → 3** rows inside the corpus top-500.

### A join that matched zero rows

`_scoring.rootDomain` holds the APEX (`nytimes.com`); `domains` is canonical at FULL-HOST
grain (`cooking.nytimes.com`); there is no domains row for the apex. So the paywall flag
matched **0 of NYT's 89 master rows**. `adjustment_for_url` now walks up the label chain.
I had also told the curator NYT had "0 master rows" — it has 89, and they were right that
those came from dish runs, not a publisher extract.

### Follow-ups, all built the same afternoon

Monthly cadence was the wrong trigger — a publisher can sit at `no_rows` for weeks after
the harvest that would settle it. A **gated publisher now recalibrates at the end of its own
refresh** (no Moz/SERP spend; the corpus is the sample), timer dropped 720h → 168h as a
backstop. A **curator override** (`paywall_adj_source='manual'`) that the job refuses to
overwrite — without that marker a hand-set value would survive until the next scheduled run
and vanish with no trace. **Status + a one-line reason** written for every flagged publisher,
because "no discount" had four causes that all rendered as a blank field.

And the job was still named `paid_pa_calibration`, with a stored `purpose` describing the
deleted shift-and-scale remap in confident detail — a curator reading the Job Monitor would
have been **actively misinformed**. Renamed `paywall_calibration`, purpose rewritten, old
job_type kept registered as an alias so historical rows still resolve. The monthly timer was
also still wired to the deleted script and would have resurrected the old method on its next
tick.

### The SEMrush URL generator, deleted — and the near-miss

The curator settled a long-parked question: for awkward publishers they build the query in
SEMrush, copy the URL, paste it, and uncouple. *"It's too complicated for you to create the
domain url — we should just go with either the default or use uncouple with a copy paste."*

That closes project_semrush_filter_codes without doing the parked work: decoding SEMrush's
undocumented `fld`/`cri` codes only ever existed to let the generator reach what copy-paste
already reaches. Worse, the generator **re-derived the URL on every read** while the form
posted back what it displayed, so hand-built links silently reverted — eleven rows carried
`db=gr` while every row's `semrush_db` said `us`.

**Then I did the same thing to the data, in the step meant to protect it.** Before removing
generation I "materialized" the derived URL into the column for all 322 rows — writing the
DERIVED value over the STORED one. That flattened **27 hand-built URLs** (Greek `db=gr`,
`/recipe` and `/syntagh` paths, `searchType=subfolder`, a `fid=` parameter) to a bare
`?db=us&q=domain&searchType=domain`. Recovered byte-identical from the pre-change dump at
3b07a07 — 90/90 rows with a stored URL match their prior state — but only because a same-day
dump existed and because a spot-check I nearly skipped printed `epicurious.com → db=us`.
The rule is `COALESCE(stored, derived)`, and print the rows a bulk UPDATE would change
before running it: memory/feedback_materialize_stored_not_derived.

Six DB columns dropped (with the vec extension loaded — `DROP COLUMN` revalidates every
trigger, including the vec0 ones). `semrush_url_uncoupled` kept and pinned to 1 on all 322
rows as a latch, at the curator's suggestion, so a re-introduced generator finds every row
already opted out.

### Also worth remembering

`restamp_recipes` reported "0 rows" and looked like a clean no-op. `json` was not imported
in the module, so `json.loads` raised NameError on all 4,898 rows and a bare
`except Exception: continue` swallowed every one. **A silent zero is what a broken loop
looks like.** Narrowed to `(ValueError, TypeError)`.

Three commits pushed: caef3fa, 10c8fdb, 825c4a5.

---

## Session 2026-08-13 — "why didn't it score?" was four bugs, and one of them was five copies

Started as R7 from `docs/acquisition-logic-study.md` and turned into a run at the whole
"a value is missing and nothing says why" family. Everything below is measured, not read.

### R7 — a fetch failure now says what it saw

`_fetch_for_filter` returned `Optional[tuple]`: timeout, 404, captcha and parse error all
collapsed into one `None`, so the caller could only write a bare `"fetch-failed"`. Replaced
with a **`FilterFetch` NamedTuple** whose `ok=False` branch always carries `failure`, a
human phrase that reaches the run log, `_dropped_reason`, and the ledger in one move
(`classify()`'s longest-prefix match on `fetch-failed` still fires, so the suffix is free).
The render escalation had the same defect one level down — a rendered challenge stub scored
0 and was filed `no-recipe-structure`. Both verdict paths now refuse before scoring.

**The part that was nearly wrong.** `_looks_blocked`'s own docstring said it was safe *only*
for deciding whether to spend a credit. Reusing it unchanged as a VERDICT violated that: a
false positive there discards a real recipe. So two thresholds —
`_THIN_SPEND_CHARS = 15000` (eager, credit at risk) and `_THIN_VERDICT_CHARS = 2000`
(strict, recipe at risk), selected by `strict=`.

**The part that WAS wrong, caught by measuring.** The first strict build refused 4 of 40
previously-kept recipes; two were **767 KB real jamieoliver.com pages**. Cloudflare injects
its passive JSD probe (`/cdn-cgi/challenge-platform/scripts/jsd/main.js`) into pages it
serves normally, so the marker list had been treating vendor plumbing as proof of a block.
Markers are now two tiers: **HARD** (`px-captcha`, `just a moment...`, `verify you are
human` — only appear ON a challenge page, sufficient alone) and **AMBIENT**
(`challenge-platform`, `datadome`, `incapsula` — corroborating only; they name WHO once a
thin body has established THAT). `_BLOCK_MARKERS` kept as the union for existing importers.

| set | n | refused |
|---|---|---|
| previously-kept recipes (random `master_recipes`) | 40 | 1 — genuinely blocked today (1,115 b Cloudflare) |
| known blocks (tiffycooks 213 b, bostonchefs 1,142 b, kalofagas 836 b) | 3 | 3 |

Two live bugs the restructure surfaced: `_fetch_text` did `result[0]`, which under the
NamedTuple is `ok` — it would have returned `True` as page text; and the Phase-A salvage
filter tested `_dropped_reason == "fetch-failed"` **exactly**, so the newly-labelled blocks
would have been excluded from the recovery path R7 exists to feed.

### The sun-sentinel swordfish grab: no score, no reason

Nothing was broken — the article was published THAT DAY, Moz had not crawled it, and
`score_url_via_moz` correctly returned None. But the chain that explains this was broken in
four places, so the only way to answer "why" was to read the database:

1. **The reason was computed and thrown away.** `_moz_lookup` knows the code (0 = "answered,
   no data"); `score_url_via_moz` returns `[0]` only and `get_or_create_url_metadata` did
   `if scores:` … then nothing. Now `_moz_score_and_record` records BOTH outcomes.
2. **It was re-billing.** With nothing written, `moz_last_scored` stayed NULL and
   `_is_moz_stale(None)` is True — **every re-extract re-ran the full 4-variant probe**.
   49 rows were in that state. Uncrawled now stamps `moz_http_code=0` + a timestamp and
   retries on `MOZ_UNCRAWLED_RETRY_DAYS = 3` (waiting on a crawl, not on a score going
   stale). Verified: second lookup went **4 rows → 0**.
3. **`_sanitize_scoring` returned before writing the note** — it only fires on values
   arriving as `0.0`, and the form now sends `null` (the absent-not-zero fix working). The
   trigger is now the STATE (no page authority), not the repair.
4. **Two silent drops of the same shape already fixed once here.** `_row_to_dict` omitted
   `moz_http_code`, so `_attach_moz_scoring`'s `is not None` check could never be true —
   1,277 metabase rows carried a code while only 77 recipe rows had ever received one. And
   `scoringNote` was undeclared on `ScoringMetadata`, so pydantic dropped it on every
   round-trip — the identical loss the comment three lines above documents for
   `mozHttpCode`.

**And the form never displayed it at all** — zero references to `scoringNote` in any HTML.
Added a "No score — why" box under the scoring strip, shown only when the authority chips
are actually empty.

### Wayback is a transport, never an identity

`web.archive.org` URLs were being scored as themselves. All 97 scored archive rows in
`metabase_url` carried the IDENTICAL **PA 47 / DA 94 / OU 0.049** — archive.org's own
authority — and 16 recipe rows were ranked on it. Another 28 never scored at all and were
re-probed on every access.

- `unwrap_wayback()` / `is_wayback()` are now canonical in `url_utils`. There were **three**
  independent copies (`html_to_markdown`, an inline `find("/http")` scan in
  `save_recipe_api`, the migration script) and the one place it cost money had none.
- `scoring_url()` = unwrap + normalize, applied at the Moz probe AND the metabase key, read
  and write, so a score and the row it lands on cannot disagree about which page they mean.
- **`_save_recipe_core` was still minting archive identities** — repairing rows without this
  just schedules the next repair. It now unwraps and preserves the snapshot on
  `_source.archiveUrl`.
- Migration (`scripts/unwrap_wayback_urls.py`, extended to both tables + a second failure
  mode): **15 unwrapped, 28 re-scored, 35 Moz rows.** Of the 16 bad rows only 3 still had an
  archive `originalUrl`; the other 13 had been unwrapped by the earlier run with their
  **scores never corrected** — invisible to any scan for archive URLs.
- `rootDomain` was stale on **54** rows (still `archive.org` beside a correct publisher PA),
  because the Moz result has no `root_domain` key. Not cosmetic —
  `stamp_paywall_adjustment` falls back to it. The writer now derives it from the identity
  URL; 53 corrected.

**DECIDED, do not re-litigate: the unwrap does NOT go inside `normalize_url`.** Its
docstring claims "one canonical form across the system" and it isn't, which is why three
copies grew around it — but the transport URL must stay representable or "fetch this
snapshot" becomes inexpressible. Two named concepts is the correct model, not one.

### The `_scoring` dict has ONE writer now

`apply_moz_scores(scoring, scores, *, url, stamp_paywall)` + `clear_moz_scores()` +
`MOZ_SCORING_FIELDS` in `url_scoring.py`. Five hand-rolled constructors —
`process_batch`, `_attach_moz_scoring`, `backfill_url_scoring`, `unwrap_wayback_urls`,
`_sanitize_scoring` (which now derives its key list from the shared tuple) — all delegate.
A grep for `["pageAuthority"] =` returns exactly one hit: inside the writer.

What the duplication was hiding:

| | recipes | master_recipes |
|---|---|---|
| had PA but **no** `power` | 201 / 351 | 1,481 / 5,017 |
| `power != DA+PA` (stale) | 0 | 57 |

The live extract path — every bookmarklet grab — **never wrote `power` at all**, so those
rows carried a measured PA that no power-blend ranking could see. The stale ones came from
a writer refreshing PA and leaving `power` computed from the old value. **1,739 rows
re-derived** through the new writer at zero Moz cost, plus one orphan `power: 72.0` beside
a null PA/DA. Invariant now holds corpus-wide: `missing=0 stale=0 orphan=0`.

Rules that were restated differently in each copy, now stated once: absent-is-not-zero;
`power` DERIVED from what is in the block (not carried), so a PA-only refresh moves it;
`mozHttpCode` provenance rides with the PA; `rawTitle` is fill-only; a real score clears an
explanatory note.

### Coquilles Saint Jacques — merged, not deleted

Row 7236 was described (by me) as a redundant duplicate of 932 and approved for deletion.
It was not: **7236 was a `kind=top` dish winner**, one of 10, and the only one unscoreable
because it had been "scored" as archive.org. 932 was the unstamped older copy of the same
publisher page holding the real PA 35/DA 37 — which is exactly why the migration refused to
re-key 7236 onto that URL. Merged instead: **932 deleted, 7236 re-keyed to the publisher and
scored** (PA 35 · DA 37 · OU 8.251 · power 72), now 4th of 10 by OU instead of unranked.
Delete ran with `enable_vec` loaded; **5029 vec entries = 5029 master rows**, rowid-map
orphans 0. Backup: `docs/reports/coquilles-dedupe-backup.json`.

### Process notes that cost time today

- **ASCII only in run-log lines.** `└─` raises `UnicodeEncodeError` on this host's cp1252
  stdout and would have killed a harvest mid-run. Em dashes are fine (cp1252 has one). The
  sub-line is `why:`.
- **A value-based detector needs a discriminator.** Flagging PA 47/DA 94 by value alone
  swept in 7 legitimate washingtonpost.com rows every run — WaPo really is DA 94 and its
  thin `/recipes/` pages really do measure PA 47. Tightened to require
  `rootDomain = archive.org`; the migration is now idempotent (0 / 0 / 1 skipped).
- **Test the suspicion before "fixing" it.** Those 7 rows looked like placeholders. Scoring
  fabricated WaPo URLs returned http_code 0 (correctly rejected) while two real ones came
  back 47 and 55 — real data. `usable = bool(code)` is right; nothing needed changing.
- **`recipes` has 19 duplicate `url_normalized` groups and that is CORRECT** — every group's
  distinct-user count equals its row count. Multi-tenant, not a defect. Do not "fix" it.

---

## Session 2026-08-13 (afternoon) — the paywall discount finally computed, and a screenshot rabbit hole

The morning entry above covers R7 and the "why didn't it score?" chain. This is the
afternoon: R8/R6/R4, the calibration paying off, a feature deleted, and a long failure
of method at the end that is worth reading before touching the screenshot path again.

### R8 — meta-tag JSON-LD, and the teaser trap

177milkstreet publishes every recipe's schema.org Recipe in a
`<meta name="application/ld+json" content="…">` tag. extruct reads only the `<script>`
form, so a **180 KB page carrying a complete Recipe declaration scored ZERO structure** —
8 of 10 candidates dropped as `no-recipe-structure`. R7 is what made this diagnosable: the
run logged **zero FETCH-FAIL**, so the residual really was a structure problem.

The recommended fix ("a contained addition to `extract_recipe_jsonld`") would have made the
corpus WORSE, because what is in that tag is a **paywall teaser**: 3 of N ingredients plus
the literal string *"… and more. Sign up for full access to all ingredients and
instructions."* and one step. So it became two functions —

| function | question | gated teaser |
|---|---|---|
| `page_declares_recipe()` | IS this a recipe? | **counts** (the candidate filter asks this) |
| `extract_recipe_jsonld()` | GIVE me the recipe | **refuses** (whatever it returns is ingested) |

`jsonld_declares_gated()` reads schema.org's own `isAccessibleForFree` on the node and on
any `hasPart`. Milk Street sets it honestly.

### Milk Street is NOT crackable — settled, do not re-derive

Measured: `1 of 9 extracted via unblocker_render (11% yield)`. The unblocker gets title,
hero, author, times and headnote, then *"To access this recipe, you need to be a member."*
**There are no ingredients and no method in the response.** Verified against two pages held
in FULL (austrian-potato-salad 12 ing, gateau-basque 14 ing): both return the paywall
notice today. Nothing regressed — it was always this way. **The nine complete Milk Street
recipes in the corpus came from the curator's signed-in browser.** The Jordanian flatbread
is the 11% because its short method sits above the paywall; generalising that one page into
"Milk Street is crackable" is the mistake that re-opened this and cost hours.
See [[project_milkstreet_gated]] and docs/acquisition-logic-study.md §6d.

### R4 — "Gated · human capture only"

New `domains.human_capture_only`. Deliberately NOT a reuse of existing flags:
`paywall` is a SCORING fact, `harvestable` skips the publisher entirely, `score_only` is the
right behaviour but a **per-run checkbox** — forgetting it costs a render per URL to
rediscover a paywall already measured. R4 is a property of the publisher, enforced
server-side in the harvest AND at `/domains/{d}/process-selected` (409). It is the CURATED
twin of the measured `content_obtainable='never'` latch. 177milkstreet is set to it.

**ATK is NOT gated** — measured, not assumed: two ATK pages fetch complete, free, with
Recipe JSON-LD and no paywall words. A publisher can be link-poor (ATK's paywall tax is the
largest in the corpus) while its HTML stays readable. That distinction is the whole point of
keeping the two flags separate.

### THE DISCOUNT COMPUTED

Captured 6 more Milk Street recipes by hand to clear `MIN_N = 12`, then ran the calibration:

| publisher | n | gap | effect | verdict |
|---|---|---|---|---|
| americastestkitchen.com | 23→33 | 16.26 | **5.19** | **−56.7% → −52.6%** |
| 177milkstreet.com | 13 | 12.37 | 3.06 | **−51.4%** |
| bostonglobe.com | 32 | 7.98 | 1.17 | −27.0% (just clears the 1.0 floor) |
| cooking.nytimes.com | 176 | **−1.5** | −0.41 | `no_penalty` — gated pages score ABOVE free peers |
| latimes.com | 52 | 3.03 | 0.42 | `inconclusive` |

Milk Street's rows moved from OU −4.08…+7.92 to **+8.30…+20.30** — nine of thirteen were at
or below zero, structurally unable to win a dish cohort. ATK's top rows went ~2 → ~18.
NYT clearing as `no_penalty` is the gate working: not every paywall is taxed.

`calibrate(persist=True)` re-stamps internally, so a follow-up `restamp_recipes` correctly
reports 0. That is not the silent-zero bug from the morning.

### ATK's first publisher refresh (job 829)

Never harvested before — its 33 rows had arrived one or two at a time across 15 dates from
dish batches and bookmarklet grabs. First run: 20 discovered → 14 kept → **10 extracted**,
131s, ~13s per recipe. All 5 `no-struct` drops were category pages (`/recipes/chicken`,
`/recipes/all`, `/recipes/paleo`) — the filter working. Obtainability now measured at
`direct — 10 of 10 (100%)`.

### The userscript capture queue is GONE

3 runs over 7 weeks (jobs 358, 825, 826), **0 recipes ever**. Worse than inert: it reported
"Userscript launched" when the pop-up had been blocked, and left a `running` job nothing
could cancel, holding the publisher's entity lock. Removed from code, forms AND db.
Its job is done by the manual queue (**↗↗ Open all N** → click the bookmarklet), which
works. Kept from the effort: `markdown_from_html()`, `jsonld_declares_gated()`,
`page_declares_recipe()`.

### Capture always targets master

The cohort queue nudged the form via `localStorage['sidebar:user_id']='0'` — which a
bookmarklet press OUTSIDE the queue never set (a Milk Street capture landed in user 5's
library) and which, once written, PERSISTED to mis-target the next personal grab. Replaced
with `_bcc_master=1` on the opened tab, reusing the SAME hint channel as `#_bcc_dish=…`.
A first pass invented a separate marker; that was a parallel pipeline for a question the
codebase had already answered, and it was removed.

### Two landmines fixed

- **jobs CLI `--param` had no type coercion.** `score_only=False` arrived as the STRING
  `"False"` — truthy — silently turning a full harvest into a score-only run that reported
  a green "done … stored=10" having fetched nothing.
- **`reset_interrupted_jobs` assumed every 'running' row was dead.** Jobs run out of
  process, so merely IMPORTING the app wiped a healthy job's status. It killed job 784
  before, and job 822 during this session. Now `mark_running` stamps `os.getpid()` and the
  reset only touches rows whose process is genuinely gone (Windows: OpenProcess +
  GetExitCodeProcess — `os.kill(pid,0)` TERMINATES on Windows, it is not a probe).

### Domains page redesign

Five ordered steps — ① Mode (a real 2×2; auto-fit produced a 3+1 orphan) → ② Scope & limits
(**above** the sources, which is what says they apply to both) → ③ Discovery source
(**SEMrush first and default**; SERP is barely used now) → ④ Publisher settings (one
scannable list; a 7-line paragraph had been serving as a checkbox label) → ⑤ Run, with
nothing after it. New in components.css: `.cfg-panel`, `.hm-grid`, `.opt-list`,
`.opt-status`.

### Process notes — the screenshot rabbit hole

Four hours went into a picture, and the method was wrong, not just the fixes.

- **Four independent faults stacked on one symptom**: a 12.5%-per-side display crop
  (`aspect-ratio:3/2` on a 1.875:1 capture); a long-edge cap that destroyed WIDTH on a
  portrait ribbon (65×35 phone captures); `windowWidth` from an inflated
  `document.body.scrollWidth`, so DESKTOP media queries ran inside a phone-width capture;
  and `bvee`, a **millisecond timestamp** in the URL that minted a new recipe identity on
  every newsletter click.
- **I fixed them one at a time without confirming which one the curator was looking at**,
  so each fix moved the symptom instead of closing it. The rule that would have saved the
  evening: *get the artefact first* — pull the bytes, measure them, and only then change
  code. Two phone captures from 01:59 and 02:05 were 73×39 and 72×38, which proved the path
  had been broken before the session started; that measurement existed the whole time.
- **I resized the curator's browser window to phone dimensions and left it that way**, then
  spent a round diagnosing a symptom I had caused.
- **I deleted the 622×332 phone capture as an "orphan" before understanding it** — the best
  evidence available, destroyed.
- **A blanket revert took a CORRECT fix down with it.** The next phone capture came back
  67×36 within minutes, proving the width cap had been right. Re-applied the two capture
  fixes; deliberately left the display `aspect-ratio` change out, because that is a change
  to a page the curator needs stable.
- **Cache-buster churn is a real cost.** `components.css` was bumped three times in one
  session against no-cache HTML, so reloads landed on different CSS/markup combinations —
  "it seems to jump around". Bump once, at the end.

---

## Session log — 2026-08-14 — a pure harvest day: five publishers, two new, and three things the runs quietly told us

**No code changed today.** `git status` shows only `recipes.sql.gz` and four SEMrush exports.
This was an operations session — nine jobs, +202 master rows — and the value in writing it
down is not the row count, it is the three standing conditions the runs exposed.

### What ran

| job | target | discovered → kept → extracted | note |
|---|---|---|---|
| 830 | Oatmeal Cookies | 86 → 69 → **10** | delete-and-replace, 10 prior rows dropped |
| 831 | Egg Foo Young | 88 → 51 → **20** | new dish |
| 832 | Chinese BBQ Pork | 87 → 60 → **20** | new dish |
| 833 | redhousespice.com | 125 → 114 → **25** | new publisher |
| 834 | Dan Dan Noodles(担担面) | 25 → 8 → **6** | new dish, thin cohort |
| 835 | screenshot_refresh | scanned 5,549 | captured 6, **failed 45** |
| 836/837 | tasteatlas.com | 20 → 20 → **5** | 836 cancelled, 837 re-ran on the unblocker |
| 838 | mygreekdish.com | 160 → 159 → **40** | ran on a **June** export |
| 839 | sallysbakingaddiction.com | 160 → 152 → **40** | ran on a **June** export |
| 840 | latimes.com | 160 → 157 → **40** | recalibration still `inconclusive` |

The 202 new master rows split 151 publisher / 51 dish. All 199 that carry a kind are `top`.
New publishers: **redhousespice.com** (28 rows, DA unscored, `plain`) and **tasteatlas.com**
(5 rows, DA 70, `unblocker`). `woocancook.com` was created as a discovery side-effect with
0 rows.

### tasteatlas needed the unblocker — and the cancel cost 22 seconds

Job 836 ran `plain` and was killed at 6 of 20 candidates. Job 837 re-ran the identical
params with `unblocker: true` and completed 20 of 20, and the domain is now stamped
`fetch_strategy='unblocker'`. This is the diagnosis loop working exactly as it should:
notice the yield, cancel, change ONE thing, re-run. Total cost of the wrong first guess was
22 seconds, because the cancel landed before the expensive extraction phase. Worth naming
because the alternative — letting a bad-strategy run finish "successfully" with a quarter of
the recipes — is the failure mode that produces a publisher who looks harvested and isn't.

### latimes stayed inconclusive — but the reason was the reference pool

Job 840 re-ran the recalibration after the harvest and returned `inconclusive` again
(78 master + 33 personal rows restamped, `discount_pct` null).

**Do not read publisher sample size off `pa_cal_n`.** That column belongs to the SUPERSEDED
shift-and-scale method and is retained read-only; it shows latimes at 246 while the live
`pa_gap_v1` sample is **n=42**. Reading 246 as "the sample grew 5×" was wrong on 2026-08-14
and the mistake is easy to repeat, because both numbers sit on the same domain row.

The verdict itself turned out NOT to be a property of latimes at all — see the mixed-media
entry below. Once PA-starved general-interest domains are removed from the free reference
pool, latimes' gap goes 6.32 at effect 1.25 and it flips to **`adjusted`, −21.4%**. It was
inconclusive because the yardstick was contaminated, not because it isn't taxed.

### Two harvests ran on seven-week-old exports — flag, not a failure

`mygreekdish.com` used an export dated **2026-06-22** and `sallysbakingaddiction.com` one
dated **2026-06-28**, both pulled from `~/Downloads` rather than `input/`. redhousespice and
tasteatlas ran same-day exports; latimes ran a 9-day-old one.

This matters because of [[project_two_stage_selection]]: **harvest SELECTS on last month's
traffic, and OU only RANKS inside the pool that selection produced.** A June export selects
on May traffic. The 80 rows those two runs contributed are real recipes and the extraction
is sound — nothing needs undoing — but they were chosen by a signal two months stale, which
is a different pool than a fresh export would have offered. Not worth re-running today;
worth knowing when either publisher's numbers get read.

Related: the two fresh exports were written to `input/` at run time and now sit in
`input/semrush/`. The ATK (08-13) and NYT (08-12) exports in that folder were already
consumed by jobs 829 and 810/811 — they are untracked files, not pending work.

### The screenshot refresh has been retrying the same 45 rows for six days

| job | date | scanned | no-shot | captured | failed |
|---|---|---|---|---|---|
| 778 | 08-09 | 5,186 | 51 | 6 | **45** |
| 791 | 08-10 | 5,207 | 45 | 0 | **45** |
| 799 | 08-11 | 5,265 | 45 | 0 | **45** |
| 806 | 08-12 | 5,330 | 45 | 0 | **45** |
| 817 | 08-13 | 5,455 | 45 | 0 | **45** |
| 835 | 08-14 | 5,549 | 45 | 6 | **45** |

Identical every night: 45 attempted, 45 failed, 0% success, six runs running. The scanned
column grows and that number does not move. These are a fixed set of rows the capture path
cannot handle, and nothing marks them as unrecoverable — so the nightly job pays for 45
doomed captures forever. (The 6 `captured` on 08-14 are the separate `no-blob` bucket, rows
whose record existed but whose bytes were missing.) Two honest options: latch them the way
`content_obtainable='never'` latches, or find out what the 45 have in common. Nobody has
looked at the list yet, and it should be looked at before it is latched.

### `mixed_media` — the PA haircut was never really about paywalls (job 841)

Chasing why a Chinese forum recipe was culled at OU −7.64 ended somewhere much bigger. The
curator's question was the pivot: *the domain is really more like a newspaper, shouldn't it
be scored like the Globe?*

It should. `pa_gap_v1` corrects a page measured against an expectations bar it did not
build. A paywall causes that. So does a domain whose authority is earned by NON-recipe
content — and only the first was eligible, because the trigger was `WHERE paywall = 1`.

New curated `domains.mixed_media`, OR'd into that trigger. Six flagged, all `adjusted`:

| domain | what it is | n | gap | effect | discount |
|---|---|---|---|---|---|
| ab.gr | supermarket chain | 20 | 12.72 | 3.31 | **−56.7%** |
| andrewzimmern.com | TV personality | 30 | 10.86 | 2.44 | **−45.0%** |
| bostonchefs.com | restaurant directory | 17 | 8.62 | 2.08 | **−40.9%** |
| marthastewart.com | lifestyle portal | 107 | 10.81 | 2.21 | **−35.6%** |
| jamieoliver.com | chef brand (TV/books) | 41 | 9.31 | 2.12 | **−33.1%** |
| washingtonpost.com | newspaper | 32 | 8.67 | 3.07 | **−28.7%** |

**THE FREE POOL WAS THE CONTAMINATED PART.** Flagging a publisher also removes it from the
free reference, and the yardstick got materially cleaner: **bostonglobe 27.1% → 36.3%**, and
**latimes flipped `inconclusive` → `adjusted` −21.4%**. latimes was never untaxed; it was
being compared against a peer pool that included starved general-interest domains. Anyone
re-reading the 08-13 calibration table should know its free baseline was low.

Job 841: flagged 13, adjusted 10, restamped **321 master + 18 personal**. Effect on rows:

| host | mean OU before | after | rows above 0 |
|---|---|---|---|
| marthastewart.com | −0.30 | **+10.51** | 19/107 → **107/107** |
| ab.gr | −1.68 | +11.04 | 1/20 → 20/20 |
| bostonchefs.com | −1.98 | +6.63 | 4/17 → 17/17 |
| bostonglobe.com | −0.85 | +10.11 | 7/32 → 32/32 |

~152 rows crossed from at-or-below zero to above it — rows that were structurally unable to
win a dish cohort.

**Checked for overcorrection, and it is not one.** Every flagged publisher landing 100%
above zero looks alarming until you measure the free pool: **97.8% of the 4,592 unflagged
rows are already above zero** (mean +11.05, median +11.65). The adjusted rows land at mean
+11.40 / median +10.66 — on top of the free distribution, not above it. Being above zero is
the NORM here; the flagged publishers were the anomaly. The one honest caveat: the
correction is a per-publisher CONSTANT, so it lifts a weak page and a strong one equally —
adjusted p10 is +7.46 against the free pool's +4.66, a compressed lower tail.

**Not flagged, deliberately:** epicurious.com (gap 2.96 → would earn −10.6%). Its authority
really is food-and-recipe authority, which is the case the flag is NOT for. Also
bbs.wenxuecity.com — gap **22.0**, effect **5.77**, the most starved thing measured in this
corpus, but n=1 against MIN_N=12. The non-Latin query fix is what actually saves that row.

**A pure recipe blog running below its DA cohort must NEVER be flagged** — that is a weaker
site, and discounting it manufactures a permanent bonus for mediocrity. The first sweep of
this got it wrong by testing all 18 unflagged domains with n≥12 and reporting them as one
list; budgetbytes, wellplated, toriavey and the Greek pure-recipe cluster were in there. The
curator's "it should only be mixed media domains" is the correct cut.

**OPEN — the live server holds a stale copy.** `_PAYWALL_ADJ_CACHE` in `url_scoring` is
per-process with NO TTL. Job 841 ran out of process and cleared only its own. Stored
`adjustedOuScore` values are correct (they were restamped), so anything reading stored data
is fine; only live recomputation inside the server process uses the old table. **Restart the
BCC service to clear it** — needs admin, see [[project_restart_zombie_port]].

### Dan Dan Noodles is a thin cohort

25 SERP results, **17 dropped as not-a-recipe**, 8 survived to Moz, 6 saved. `ou_fit` came
back `below_min_n` so no cohort curve was fitted at all — the OU numbers on those 6 rows are
raw, not fitted. One query, `serpapi_per_query: 25`. The other three dish runs pulled 50-60
per query across 2 queries and landed 51-69 in the fitted pool. If Dan Dan Noodles is meant
to be a real dish page it needs more queries, per the sourcing-instrument rule
([[project_dish_variants_membership]], and the `dishes.queries` entry under Settled).

Standing across all four dish runs: `fetch-failed (likely anti-bot)` accounted for 4, 5, 5
and 0 rejects respectively — the [[project_fetchfail_salvage]] Phase B backlog, still unbuilt.

---

## Session log — 2026-08-14 (evening) — one Chinese recipe, nine bugs, and every one of them reported success

Started as "why is this recipe's title still in Chinese?" and turned into the most productive
bug day of the month. **The thread through all of it: nine separate defects, and not one of
them raised an error.** Harvests said `stored=1`. Extracts said `translated=True`. Saves
returned 200. The system was lying politely in nine different places.

### The nine, and what each one was really doing

| # | commit | what it claimed | what was true |
|---|---|---|---|
| 1 | `84504cd` | run is domestic | query was `担担面`; ccTLD-only detector missed it |
| 2 | `f759d9c` | locale inferred from query | locale is a DISH fact, not a string property |
| 3 | `16f66fe` | `translated=True` | recipe built from pre-translation JSON-LD |
| 4 | `c92e176` | `discovered=2` | 10,000-row export collapsed to 2 by a dedupe key |
| 5 | `74f4e2b` | domain saved fine | `language='zh'` silently destroyed by a `<select>` |
| 6 | `d8692e9` | hero image stored | remote URL that 403s in the browser, at 300×200 |
| 7 | `9a4df46` | screenshot captured | pure white, 1 unique colour |
| 8 | `f333799` | hero fetch failed | our own `credentials:'include'` broke CORS |
| 9 | `2da3181` | page is English | 14% Han, diluted by JSON-LD scaffolding |

### The two worth reading twice

**#3 — the fast lane ate the translation.** The page's Recipe JSON-LD is parsed once, ABOVE
the language handling, to build the cache fingerprint — correct, since the fingerprint must
be source-to-source. That same `_src_rec` object was then reused as the EXTRACTION result
further down. Translation cleared `md_result['jsonld']` intending to force the LLM path, but
the lane selection reads `_src_rec`, so clearing `md_result` did nothing. The split was
exact: rows whose JSON-LD had ≥2 ingredients and ≥2 steps shipped in Chinese; rows logging
`not eligible (only 1 instruction step)` fell through to the LLM and came out English.
**The better-structured the page, the worse the result.** And `skip_jsonld_fast_lane` — the
flag that exists to express precisely this — is set in three places and read in none.

**#4 — one dedupe key erased a publisher.** `_recipe_path_key` drops numeric path segments
so `/recipe/21014/slug/` and `/recipe/slug/` are one recipe. Correct when a slug survives.
On xiachufang, where URLs are `/recipe/100634884/` with no slug, NOTHING survives the strip
and all 10,000 rows key to `('xiachufang.com','recipe')`. The fix detects the collapse by
RESULT rather than maintaining a list of numeric-id publishers, and logs loudly when it
fires. 2 → 25 URLs; redhousespice unaffected.

### xiachufang.com — the new publisher, and what it cost to learn

Largest Chinese recipe site by a wide margin (Semrush: ~50× the runner-up's organic traffic;
the only one of six candidates that is a pure recipe site by URL structure). Harvested as
**`m.xiachufang.com`** on purpose — `www` serves a slide-captcha (滑动验证) to a plain fetch,
the mobile host returns the full page WITH Recipe JSON-LD. `robots.txt` sets `Crawl-delay: 10`
and **our fetch path honours no crawl delay at all** — keep runs small.

Job 844 (post-fix): 25 discovered → 25 passed → **8 extracted, all 0% CJK**. Titles came out
in the corpus's house style unprompted — `Taiwanese Braised Pork Rice (台湾卤肉饭)`,
`Xiaolongbao (Shanghai Soup Dumplings, 小笼包)`.

**Chrome auto-translate is a trap on the bookmarklet path.** The morning's wenxuecity capture
looked perfect because the CURATOR'S BROWSER had translated the page — the tell was
`Xia Chu Fang (Download Kitchen)`, Google rendering 下厨房 with 下 as *download*. Our Haiku
translator has a culinary prompt and left it alone. Free translation, but we inherit Google's
errors, lose the original, and the output depends on a browser setting. **Test with translate
OFF.**

### Process notes

- **Three cousins of the same bug class** turned up in one day: a flag set-but-never-read
  (`skip_jsonld_fast_lane`), state cleared in one variable while a second still held it
  (`_src_rec`), and a control that could not represent its own stored value (the language
  `<select>`, which silently destroyed `zh` on an unrelated save). All three reported success.
- **I killed my own job** by wrapping it in `timeout 900`; it died mid-save at 8 of 10 and
  left an orphaned `running` row holding the publisher lock. The PID-aware reset from 08-13
  cleaned it correctly. Don't wrap a long job in a timeout — background it.
- **The curator's bookmarklet test ran on OUR OWN form** (`localhost:8009/r/<uuid>`), which
  produced a plausible-looking capture with a method list and no ingredients. The bookmarklet
  should refuse to run on our own origin — UNBUILT, and it would have saved a round.
- **A dropdown is state you can forget; a button is a decision.** My first pass at the enrich
  control was a `auto|always|never` select. The curator replaced it with two buttons and was
  right: a mode you set once and forget can fire the expensive path on a save you never
  thought about.

### Money — the first real accounting

| period | API spend |
|---|---|
| 2026-08-14 | **$4.97** |
| August (13 days) | **$62.46** |
| since 2026-05-15 (journal start) | **$215.29** |

~$4.80/day, ≈$145/mo at current pace. Haiku 4.5 is **$141 of the $215** — the right shape
($1/$5 per MTok vs Opus at $5/$25). Sonnet 4.6 $50, Sonnet 5 $15, Opus 4.8 $9.72 (44 calls,
the AI editor). Also in the journal: `gpt-4o`/`gpt-4o-mini`, 354 calls — a **third bill**, on
OpenAI, not priced here.

**API billing is entirely separate from the $200/mo Claude subscription** — different system,
different invoice, at platform.claude.com. Console check is unfinished: the browser had no
authenticated session and signing in is the curator's to do. Suspected explanation is a
prepaid credit balance with auto-reload, which is silent by design.

**The line that grows:** `translate_markdown` ran 34 times today at 183k in / 96k out. Every
foreign-language page costs a full-page Haiku translation ON TOP of extraction (~40s, about
half the per-recipe wall clock). That is the cost of harvesting non-English publishers.

### Enrichment policy — settled

**Auto-enrich was never asked for.** It arrived 2026-05-21 inside a five-theme `EOD` commit
and is not listed among that commit's own highlights. It also never re-enriched on every save
— it has always been idempotent, firing only when `classification.story` is empty (measured:
199 master saves, 75 fires, 124 skips). The reason it deserved a control is **latency, not
money**: median **9.7s**, max **30.4s** added to a save.

Where it landed (`7c674fc`, `613b7c8`):

- **MASTER** — `[Save]` enriches a row with no story (unchanged); `[Save & Re-Enrich]` is the
  one thing saving does NOT do, refresh an existing story. The plain Enrich button is HIDDEN
  there, because saving already does it.
- **PERSONAL** — no auto-enrich at all. The Enrich button is the only route, and it now shows
  only on a tier that includes it (`users.subscription_tier`, mirroring `_AUTO_ENRICH_TIERS`).
- Two things to settle **before** selling it: a per-user opt-in (a Premium user must be able
  to decline ~10s on every save — wants a nullable `users.auto_enrich`), and **metering**.
  Spend is journaled per `user_id` but nothing enforces a ceiling, and enrichment is the most
  expensive per-save operation we have. Decide the cap before the first paying user.

### Free vs paid — decided, do not re-argue

**Master enrichment stays visible to everyone, including free tier.** It follows from the
existing thesis rather than adding to it: *free to browse, membership to KEEP*; the conversion
moment is CAPTURE (layer 2), and *the free layer is not generosity, it is the channel*.
Gating layer 1 would wall off the shop window and hide the editorial content from crawlers
on the same surface that carries `ItemList` + `Review` JSON-LD. The clean line is ownership,
not quality:

| | whose content | who pays | who sees |
|---|---|---|---|
| master enrichment | ours | us, once per recipe | **everyone** |
| personal enrichment | theirs | them (paid tier) | the owner |

**COOK MODE IS NOT A CONVERSION DRIVER ON ITS OWN** (curator, 2026-08-14). It is layer-4
icing and cannot carry the offer by itself — the membership needs several feature checkboxes
that together read as "why I want to be a member of this club." Do not plan a paywall around
cook view alone. What the other checkboxes are is **OPEN** and is the next product question.

## Session log — 2026-08-14 (late) — the redaction pass, and two designs written down

After the multilingual bug day, three threads: a security pass on what we tell
users, screenshot policy now that it is a display asset, and two designs recorded
rather than built.

### End users were being shown our plumbing

Two leaks, then a systemic one.

- The unscored-save note said *"Moz has not crawled this URL yet…"*, and
  `/url-metadata` said *"Row exists; Moz scoring not yet run (set MOZ creds and
  run refresh script)."* The second names a vendor AND tells a customer to run a
  script only we can run.
- Worse: **31 endpoints an ordinary member reaches echo the raw exception**.
  Demonstrated live as a Free-tier member — `POST /images/fetch` returned
  `ConnectionError: HTTPSConnectionPool(host=…, port=443)`. A sqlite error names
  our columns; an SDK error names the model and provider.

Fixed with ONE `HTTPException` handler rather than 31 edits, on the same
principle as the unscored-note redaction: **a rule applied in one place cannot be
forgotten by the 32nd endpoint.** 5xx only — 4xx details are written for the user
and are the actionable half of the API. Staff keep the full text; the check fails
CLOSED. Non-staff get "Something went wrong on our end. Your work wasn't lost."

**Audit result:** the two end-user pages (`recipe_form_styled.html`, `cook.html`)
are clean in SHIPPED text; remaining vendor mentions there are HTML/JS comments.
Admin surfaces are deliberately untouched — `domains.html` carries 39 SEMrush and
18 Moz references and should. Standing rule: [[feedback_no_vendor_names_to_users]].

### The screenshot is a DISPLAY asset now, not a provenance signal

The curator wants it as blurry card wallpaper. That reframing changes what the
failures cost, and the refresh job turned out to already be the design he
described — `screenshot_refresh`, nightly, `limit` as the real control.

- `max_age_days` was **365**, so the age-refresh had NEVER fired (`aged: 0` every
  run). Set to **30**, `limit` 200 → **100**: a 56-day rolling cycle at ~7 min of
  Chromium nightly. Deliberately half the throughput of the 200 option — the host
  has confirmed Intel 13th-gen degradation with an RMA pending
  ([[project_host_thermal_shutdowns]]), and this is permanent nightly load.
- **24 blank blobs deleted** so they re-shoot. As provenance a white rectangle was
  merely useless; as wallpaper it renders a blank card.
- **The 45 nightly failures are latched** (2 strikes, 90-day retry, a success
  clears the counter). At limit=100 they would have eaten HALF the budget forever.

**Why 45 rows have no screenshot — it is four different things, and half are not
failures:** 36 point at `bestcooksclub.com` (OUR OWN domain — the URL currently
resolves to the edit form, so shooting it is circular; **this self-resolves when
the production display page ships**, and those rows will need their latch cleared),
12 have no URL at all (typed / photo / PDF imports — these need a display
FALLBACK, and they are a growing population), 33 washingtonpost (times out at the
HTTP level), 10 edibleboston (**SSLError, not a block** — possibly recoverable).

**Would the unblocker help? Mostly no.** It returns HTML; screenshots come from a
Playwright child process with no proxy plumbed in. For WaPo you would render the
paywall (the Milk Street lesson). For edibleboston's TLS failure a proxy might
work — but that is 10 rows of 5,594, and the cheaper test is whether Playwright
already tolerates a cert `requests` rejects.

### Two designs written down, nothing built

**`docs/recipe-activity-and-engagement.md`.** Two systems that must not be one
table: a per-record ACTIVITY log (what the system did) and an ENGAGEMENT log
(what people did). The activity log answers the day's recurring failure — the
system acts and the record does not show it. Its key decision: **keyed on
`url_normalized`, in its own table, NOT in the record**, because publisher and
dish refreshes are delete-and-replace and an in-record log dies exactly when the
interesting thing happens. `metabase_url` is the existence proof — 7,102 URLs
against 5,594 recipes, already outliving row churn.

**The profile is wanted, and the doc says so plainly.** Curator: *"we are after
the user profile that's created out of those collected activities."* The
resolution of that against years of arguing about data collection is that the
objection was never to KNOWING things — it is to a profile that is secret, serves
someone else, is inescapable, follows you across sites, is hoarded, or is sold.
Each is separable. The test recorded: **would we show the user their own profile,
in full, without embarrassment?** Most of the machinery exists — identity cards,
embeddings, chapters — so a taste profile is a centroid of what someone kept,
weighted **cooked > captured > viewed**. Unresolved: cross-user aggregation, and
`cook_complete` is the strongest available signal and is not recorded at all.

### Also shipped

- **`domains.extract_notes` is finally READ** — appended to the extraction prompt
  as publisher-specific guidance (the curator raised this repeatedly and was right;
  the field existed since the table was created, described as "capture hints", and
  was wired to nothing). Seeded on m.xiachufang.com and confirmed firing:
  `[EXTRACT] publisher hint applied (319 chars)`.
- **DA now refreshes from the harvest's own Moz rows** (median of the run), which
  also makes `da_last_scored` real — the form has always rendered a "DA scored"
  pill from a column nothing wrote.
- **2 orphan columns dropped**; `obtainable_streak` KEPT after checking — it is
  actively written and reads 0 only because no publisher has hit the streak.
  "Never differs from default" is not the same as dead.
- **Hero images now fetched SERVER-SIDE from a URL** (~122 bytes over the phone
  instead of a download-and-re-upload), because mobile is spotty. 300x200 → 900x1600.

### Open

- **The server has not been restarted since `464f2de`.** Nothing in the last third of the
  day is live.
- **The admin Refresh button is DEFERRED, not rejected.** Reprocess the stored URL as if
  delete-and-add, recompute everything, redisplay, wait for save. It is currently the only
  way to fix one bad record without a bulk job. Held so the redaction shipped clean.
- **36 latched URLs point at `bestcooksclub.com`** and will be shootable the moment a
  production display page exists. Clear their latch then.
- **12 recipes have no URL at all** (typed / photo / PDF). They can never have a screenshot
  and need a display FALLBACK — a growing population, not an edge case.
- **edibleboston's 10 failures are an SSLError, not a block.** Cheapest test is whether
  Playwright already tolerates a cert `requests` rejects — before reaching for a proxy.
- **`cook_complete` is not recorded anywhere.** It is the single strongest engagement
  signal in the system and the design doc depends on it.
- **Cross-user aggregation is the unresolved privacy question** — recorded, not decided.
- **DEFERRED to the display work (2026-08-15, curator): raw PA/DA/OU on
  `/url-metadata`.** The endpoint hands the raw trio to any caller, and it is on the
  public-host allowlist. `/public-score` exists precisely so stars are quantised
  server-side — "a page that receives the raw numbers has published the ranking method
  to anyone who opens devtools." The two are in tension for a MEMBER. Gating the trio to
  staff costs the curator nothing but removes the score strip for members, so it is a
  product decision and waits for the production display surface. The vendor-named KEYS
  (`moz_last_scored`, `moz_http_code`) were fixed separately and are staff-only now.
- **Cook mode is not a conversion driver by itself** (curator, explicit). It needs other
  checkboxes beside it before the membership pitch closes.

---

## Session log — 2026-08-15/16 — pricing found, a bug class closed

Two threads that turned out to be the same thread: what we sell, and why the code
keeps failing quietly.

### The tier design, argued down to two tiers

Wrote **The Membership Ladder** (artifact; source at `temp/tier-design.html`) and
then rewrote it three times as the curator knocked out my assumptions. Worth
recording the ARGUMENTS, because the conclusions moved a long way.

- **Cook mode is not the anchor.** Curator: *"the single source for all recipes
  with easy and powerful (vector) search, and a curated library from the bigs AND
  the long tail including foreign sourced sites... to me it pales."* Correct, and
  the data agrees — cook mode exists on **16 of 5,169** master recipes (0.3%). It
  is a metered feature inside Member now, not a tier.
- **No Household tier.** I had lifted a 5-seat family plan from Duolingo, where it
  works because four children each learn separately. *"i don't think there are a
  lot of people who need 5 memberships in a family... like nobody."* A kitchen has
  one cook. Two tiers, deliberately.
- **Price higher.** The curator's fixed-asset accounting product went $600 to
  $2,500 and from a pittance to thousands of sales. The subscription data agrees:
  high-priced apps convert download-to-paid at **2.7% vs 1.5%**, trial-start 9.8%
  vs 4.3%. Member moved from $4.99 to **$8.99/mo, $79/yr**.
- **Search free, save paid.** The curator's line, and it is the original thesis
  stated exactly. Everything that helps you FIND is free — that is the Google half,
  and Google is free because ads pay, a bargain we decline. Everything that follows
  from KEEPING is the membership. **Free is the whole product with a number on it:
  20 recipes, nothing crippled.**
- **Commerce funds the free tier**, and it is tier-independent — a free user who
  buys a Dutch oven (~$5.40 at kitchen's 4.5%) is worth more than a member who buys
  nothing. That inverts freemium logic and is why free got generous. **Display ads
  declined** despite $12-30 RPM: the whole positioning is that publisher pages are
  ad-choked and we are the clean alternative.

**Two corrections inside the document, both from bad first queries.**
`LIKE '%"_cook"%'` matched 4,922 rows; a real parse found **16**. Editorial blocks
are on **190** rows, not 5,169. The 3.7% enrichment rate is NOT a coverage failure
— auto-enrich on master saves is about a week old, so it measures elapsed time.
**All six accounts are fictitious**, so every behavioural number in the doc is
unevidenced and now says so.

**The corpus capital finding:** do NOT pre-buy cook rework. It is one-time per
recipe, cached in the row, and master rows are shared — so on-demand turns
**$1,091 into roughly $21** for the first hundred recipes anyone actually cooks.

### The synthesis experiment — it works, and it counts badly

Built `scripts/export_markdown.py` to test a claim the tier design leans on and
nothing had verified. `--format md|json` share one `project()` so they cannot
disagree about facts; `--dish` / `--domain` filter on the INDEXED columns
(`dish_key`, `source_host` — the json_extract equivalent is a full scan, 163 ms
against 1.5 ms).

Fed 30 crab cake recipes (29 publishers) to NotebookLM. The output ranked them on
**procedural structure** — technical precision vs parallel coordination vs
sub-recipe partitioning — an axis nobody publishes. It caught that the TV Dinner
interleaves four dishes chronologically, that Mansaf quarantines bread-making into
steps 8-13, and that Serious Eats has zero filler INSIDE while being breaded
outside.

**Then I verified every falsifiable claim, and the split is clean:**

| | |
|---|---|
| Judgments | **all sound** — including "Cookie Rookie: 1 tbsp baking powder", confirmed exactly: a real error in a published recipe |
| Counts | **all wrong** — "egg in 20 of the recipes" (actually 30/30), "exactly half ban vegetables" (17/30), saltine camp 6 named (10 actual) |

**The model does judgment; code must do arithmetic.** A synthesis feature should
compute field statistics in SQL and hand them over — the same boundary the
cook-view work already draws.

**The standardization is half-built**, which the same test exposed:
**59,366 ingredient lines, ZERO parsed** into amount/unit/item; equipment has
**3,004 distinct names across 33,729 mentions, 261 ways to write "skillet"**.
Roles are strong; quantities do not exist. That is why "rank by crab-to-filler
ratio" cannot be answered, and it is the measured blocker on the best queries.

### A bug class, closed

`lidiasitaly.com` looked hung. It was **failing every save** —
`NameError: skip_auto_enrich`, left behind by my own rename. That branch only runs
when a caller explicitly opts OUT, which is exactly what the three batch paths do,
so every interactive save was fine and every harvest save died. The cost shape is
the bad part: it fires AFTER the extract, identity card and screenshot are paid
for. Job 856: 10 failures, 0 saves.

Then ran `ruff check --select F821` — **already installed, never run** — and it
found **three more of the identical bug in under a second**: `cal_by_host`, a
missing `import llm` (so `/url-words/sweep` has always 500'd), and a missing
`extract_og_image` sitting inside `except Exception: return ""`.

**Asked whether it was time for a deep refactor. It is not.** 61,418 lines, 11,299
in `save_recipe_api.py`, **3 test files**. All four bugs were single undefined
names in branches ordinary use does not reach — a linter problem, not an
architecture problem — and restructuring without tests reproduces this exact
failure mode at scale. Order: linter, then a smoke test on the save path, then
carve one bounded piece at a time.

- **`scripts/hooks/pre-commit` installed** (`git config core.hooksPath scripts/hooks`).
  Blocks F821/F822/F811/E9 on STAGED files; everything else advisory. Deliberately
  no style enforcement — 319 style findings exist, and a hook that argues about
  style gets bypassed within a week and then protects nothing.
- **All 152 B904 sites fixed** — every raise inside an except now names its cause.
  AST transform, not regex, because multi-line raises, nested handlers and closures
  each defeat a regex. One real bug on the way worth remembering: **`ast` col_offset
  is a UTF-8 BYTE offset, not a character index** — an em-dash in a user-facing
  string pushed ` from e` onto the next line and broke the file.

### Also shipped

- **topsecretrecipes.com skipped entirely** — paid gate, and unlike Milk Street
  there is no bookmarklet path either, because the curator holds no subscription.
  `harvestable=0` plus `system_config.disallowed_domains` (the live blocking
  mechanism; `domains.allowed=0` is retired). Row KEPT as the record of the
  measurement, so a future run cannot rediscover and re-pay for it.
- **`/url-metadata` leaked vendor names in its FIELD KEYS** (`moz_last_scored`,
  `moz_http_code`) to any member — the previous sweep caught prose and missed
  structured data. Renamed for non-staff; staff keep both.
- **Persona chip** on the recipe form: user_id, tier and role from `/auth/me` only.
  Written because "am I admin right now?" was unanswerable without devtools.
- **Signup wrote a NULL tier** on every self-signup. Now explicit `'Free'`.
- **lidiasitaly.com harvested** (40 rows) and anthonymichaelcontrino.com (11).

### Open

- **The server is STALE** — nothing from 2026-08-15 onward is in the running
  process. Jobs are unaffected; they launch fresh.
- **Ingredient quantity parsing is the recommended next build.** Roughly $21 for
  the whole corpus at identity-card rates, and it unblocks scaling, shopping lists,
  "what can I make from what I have", and every numeric comparison.
- **The structured-vs-plain A/B was never run.** `temp/crab-md` against a
  `--style plain` export. The crab answer leaned on method text; roles and
  equipment barely featured, so the front matter has not yet earned its keep.
- **Click ledger and engagement log wait for real users** — with six fictitious
  accounts there is no traffic to measure.
- **Advisory lint debt:** 57 unused imports, 21 E702, 20 E402.
- **`users.status` and `subscription_tier` still overlap**, and account 8
  (`comped`) has a NULL tier on purpose — Free is probably wrong, Premium may be
  right.

---

## Session log — 2026-08-17/20 — a rubric designed and parked, a regression closed

### The dish rubric — designed, then parked

`docs/dish-rubrics.md` (a65d5d9, a39d6bf, 21a39b4). **Design only; the curator
parked it 2026-08-20 with nothing built.** Do not resume unless asked. The
reasoning is worth keeping because it cost real analysis:

- **The Bolognese experiment worked and then proved why it could not be trusted.**
  A closed-world notebook tool given 30 Bolognese recipes produced a genuine
  evaluation framework and rejected the majority on garlic — correctly. But it was
  **luck**: it held because Hazan, Lidia and Vincenzo happened to be among the 30.
  Against the Accademia recipe deposited with the Bologna Chamber of Commerce
  (20 April 2023, read through our own `to_markdown/pdf_to_markdown.py`), its
  master recipe agreed on garlic and **contradicted canon on six other points**,
  including recommending veal the registry explicitly forbids.
- **The useful reading is not "it was wrong".** It synthesised Hazan, who really
  does milk-first. There are competing authorities; a closed-world read cannot say
  which one it landed on. So dimensions can be **contested with named camps**.
- **Three kinds of authority, three questions.** Definitional (Accademia — thin BY
  DESIGN, it states a boundary), instructional (Hazan, ATK, Serious Eats — rich,
  explains why), de facto (what people cook). The curator's point that the Italian
  sources read thin is explained by the first: boundaries are short.
- **Reach is already in the set.** I had argued positions must be traffic-weighted.
  Wrong — members survived the two-stage selection, so counting positions across a
  dish's members ALREADY is the de facto axis. What selection has never selected
  for is definitional canon and tested method.
- **Coherence, measured across all 164 dishes** (`temp/dish_coherence.tsv`):
  66 dish-shaped (>=0.80), 61 mixed, **37 are collections, not dishes** — Blood
  Orange 0.10 across 18 distinct dishes, Swordfish, Tagliatelle, Hoisin Sauce.
  Caveat: Kolokithopita and Gemista score low because members disagree about the
  NAME, not because they are collections. Gate must flag for review, not exclude.

### Serious Eats authority harvest

Tested-authority coverage of the 66 dish-shaped dishes: **28 uncovered -> 11**.
18 recipes saved across 17 dishes. Throwaway scripts in `temp/`.

Three things it surfaced that outlive the rubric:

- **A `site:` SERP returns the NEAREST page, not the right dish.** It offered a
  savory crinkle pie for Galaktoboureko, baked ziti for Pastitsio, a Spanish bean
  salad for Gigantes Plaki. Gate any targeted harvest on the extractor's own
  `_identity.likelyDish`, NOT on URL tokens — a token screen wrongly rejects
  Schwarma->shawarma and Tabouleh->tabbouleh, which are just transliterations.
  One still slipped: Shopska Salata -> "Salata Falahiyeh" scored 0.5 on the shared
  token "salata". Deleted by hand (id 8236). The real fix is semantic similarity
  over embeddings we already have.
- **6 duplicate dish entries** share a modal likelyDish: Spaghetti alla Nerano ~
  alla Nerano · Pastitsio ~ Pastitcio (Greece) · Cacciatore ~ Italian Braised
  Chicken · Meatloaf ~ Turkey Meatloaf · Chicken Thighs Bone In ~ Boneless ·
  Shrimp with Tomatoes and Feta ~ Saganaki. Some true dupes, some deliberate
  variants — a curator call, untouched. Merging the true ones closes 3 of the 11
  remaining coverage gaps for free.
- **My own bug, caught by my own gate.** `extract_recipe_from_url` returns the
  ENDPOINT shape `{success, recipe_id, recipe, ...}`; the recipe is nested under
  `["recipe"]`. I passed the wrapper, so the identity gate found no `_identity`
  and rejected all 23. It failed CLOSED — without the gate, 23 near-empty rows
  would have been written with dish stamps. Argument for gates that verify content
  rather than trusting the pipeline.

### The enrich button regression (3da9f75, 7f7e61b)

The Enrich button had **vanished from the user recipe form for 4 of 6 accounts**,
including every Free one — the accounts used to test the member experience.

Cause was mine: on 2026-08-15 I gave the form its own
`ENRICH_TIERS = ['premium','admin']` while the server's `_AUTO_ENRICH_TIERS` is
`frozenset()` — "paid tier not sold yet". The two drifted and the client won, so
the UI withheld a feature nobody is charged for.

**Hiding it was never a gate.** `/enrich-recipe` requires only `own_recipes`,
which every role including member holds. Visibility only decided what our own UI
does — exactly what that endpoint's comment warns about client checks.

`/auth/me` now returns **`enrich_available`**, computed by `_enrich_available()`
beside the tier policy it reads: true for any resolved user while enrichment is
unsold, narrowing automatically the moment `_AUTO_ENRICH_TIERS` is populated, with
no client change. Verified both ways, and live on the restarted server — uid 2,
tier Free, role member -> `enrich_available: True`.

**This is the same shape as the `skip_auto_enrich` NameError:** one policy
duplicated in two places that silently diverged. The lint hook cannot catch it —
both halves are valid code. The entitlements table from the tier design is what
would.

### Data changes

- **+~99 master rows** from three publisher harvests: panlasangpinoy.com (31),
  shop.legalseafoods.com (30), bakewithzoha.com (20), plus the 18 Serious Eats.
- **`shop.legalseafoods.com` has 30 rows and NO domain row**; the domain row is
  `legalseafoods.com` with 0 rows. The recipes are genuine (`/blogs/recipes/`,
  3-22 ingredients). Bookkeeping only: counts will not roll up and a future
  refresh of `legalseafoods.com` will look for the wrong host. The domains table
  is full-host grain, so this wants a `shop.` row or a redirect rule. NOT fixed.

### Open

- **Ingredient quantity parsing remains the recommended next build.** 59,366
  ingredient lines, ZERO parsed into amount/unit/item. ~$21 for the corpus at
  identity-card rates. Blocks scaling, shopping lists, "what can I make from what
  I have", and every ratio question — including the one the rubric work wanted.
- The server is current through `3da9f75`; `7f7e61b` (cosmetic, anonymous branch
  of /auth/me) lands on the next restart.

---

---

## Session log — 2026-08-20 (afternoon/evening) — the recipe list stops loading the whole corpus, and search becomes search

The day started as "the sidebar is slow on the iPad" and ended with the list paged, faceted
and full-text searchable. Sixteen commits, `fbefce3` through `f2d7d9d`.

### The performance problem, measured before touching anything

`GET /recipes?user_id=0&summary=1` was **6.89 MB and 2.2s**, and `renderRecipes` built
**~75,600 DOM elements** in one synchronous pass. The slim projection's own comment still
claimed "~700KB" — true at the 702 rows it was audited against in June, against 5,435 now.
The projection had not regressed; the corpus grew 7.7x underneath it.

Three separate costs, fixed three separate ways:

- **No compression existed at all.** The server returned identical bytes whether or not the
  client sent `Accept-Encoding`. `GZipMiddleware` plus trimming four fields the cards never
  read (`classification.story` and `editorial.opinion` fed a dead `isEnriched()`; `image[]`
  loses to `previewImage` on 97% of rows; `exceptionalism.basis` carries match provenance no
  tooltip prints) took it to **4.40MB raw / 0.66MB gzipped**.
- **The DOM.** A card FACTORY plus a windowed renderer: 75,600 elements to 880.
- **Search ran on every keystroke** over the whole cache. Now submitted — Enter or the
  magnifier — at the curator's request.

### Then the reframe: the trigger moved, the cost did not

Defaulting the sidebar to the user's own collection took boot from 660KB/2.0s to 19KB/0.25s
— but the curator caught the framing: *"the same load problem... it doesn't go away, it's
just triggered by a button and not the init default."* Every Master click still paid in
full. That correction is why the rest of the day happened, and it is saved as
`feedback_deferred_is_not_fixed`.

### Sorting moved into SQL, and the verification found three things

Each of the 9 sort orders was run against the browser's own comparator over the same 5,435
rows, counting inversions. None of what it found was predicted:

1. **Ties are the normal case, so neither side was deterministic.** Rows in a tie group:
   page_authority 99.9%, power 99.9%, chapter 100%, ou_score 92.9% — and created_at **0%**.
   created_at was also the only sort that matched, which is the experiment proving the rest
   was tie-ordering rather than wrong ordering. Every sort now ends in a total order,
   breaking ties on traffic then id per `0e2be0a`.
2. **"Quality blend" had been silently broken.** Its second key is `recipeScore`, which
   `_recipe_list_data` never shipped — so the client read `undefined` on every row and the
   sort degraded to OU-then-date. Present on 5,434 of 5,435 rows in the DB.
3. **The collation gap was punctuation, not accents.** Accents were the predicted problem
   (NOCASE reorders 1,083 positions) but the residual was straight-vs-curly apostrophes in
   "Chef John's" — the corpus holds both. Four foldings measured: accent-only 45 inversions,
   plus-typographic-to-ASCII **26**, plus-drop-apostrophes 42, plus-drop-ALL-punctuation
   **114**. The intuitive move is the worst of the four.

`bcc_sortkey` is registered on the connection factory and deliberately NEVER in a generated
column or index: SQLite accepts it there, and then every connection that has not registered
it — `backup_db`, the jobs, DB Browser — fails with "no such function".

### Facets, and the rule that settles them

**Anything the UI can filter or sort on gets a real indexed SQL surface, never a
json_extract at query time.** `DISTINCT cuisine + counts` cost 471ms through JSON and
**0.4ms** through an indexed generated column — the difference between a dropdown you can
rebuild on every selection and one you cannot.

Source is `_identity`, chosen on COVERAGE: `_identity.cuisine`/`.ethnicity` are on **100%**
of rows in both tables, against `provenance.ethnicity` at 6.2% and site-declared
`recipeCuisine` at 86% of publisher-typed free text. 151 cuisines, 281 ethnicities. They are
not synonyms and both are kept — ethnicity carries Roman, Sicilian, Venetian, Southern.

`GET /recipes/search` returns one page AND every dropdown's options in a single request.
Each facet is counted with every OTHER filter applied but not its own, which is what makes a
zero-result selection unreachable rather than merely unlikely. Verified across every
combination tried: no option is ever offered with a count of zero.

### Search was never search

It was `LIKE` on a contiguous substring. "shrimp and corn chowder" could not match "Corn and
Shrimp Chowder", and "corn" matched Pop-corn Shrimp because a substring has no word
boundaries. 31 master rows have shrimp AND corn in their ingredients and were invisible
entirely.

FTS5 over name + dish + ingredients + cuisine, `porter unicode61 remove_diacritics 2`.
Index builds in 0.4s (~10MB), queries run 7-35ms against 380-440ms:

    'chowders'      21   STEMMED — the FREETEXT half SQL Server gives you
    'bechamel'       9   incl. Bechamel Sauce (accented in the data)
    'risotto'       26   via likelyDish, whatever the title says
    "Chef John's"    5   incl. BOTH apostrophe variants
    'chowder -clam'  4   exclusion
    '"clam chowder"' 15  phrase

**Why both engines.** The identity vector deliberately excludes the title — measured
2026-06-11, adding it nearly doubled intra-dish L2 (0.35 to 0.68). The curator spotted the
oddity: *"the weird part is we don't embed the name."* Demonstrated: "Chef John's" has 4
literal title matches and the vector returns NONE of them; "Ina Garten" returns Arnold
Palmer at 0.35, the noise floor. Conversely "something brothy and warming for a cold night"
contains no recipe words and the vector nails it. So they are not redundant engines with one
weaker — **the corpus has two retrieval surfaces**, the name a publisher gave it and the
dish it actually is. `likelyDish` is the one field BOTH index.

Semantic runs ONLY on a lexical miss, in its own response field, never merged into rows.
Two gates found by watching it misbehave: `((((` was embedding successfully and returning
"test" and Tonkotsu Ramen at 700ms — the slowest path in the endpoint — so a query yielding
no usable FTS expression is now skipped; and a **0.45 similarity floor**, because a nearest
neighbour always exists (real hits at 0.69 and 0.49; the junk run topped out at 0.46).

Also settled: raw input NEVER reaches `MATCH`. FTS5 has its own grammar, so an unbalanced
quote or a bare `-` is a syntax error — a 500 from the one control where users type
anything. Two grammar traps found by testing rather than reading: `NOT` is BINARY, so the
natural-looking "AND NOT" is invalid; and a leading `NOT` has no left operand, so an
all-exclusions query cannot be expressed and is refused rather than guessed at.

### Regressions I introduced and fixed the same day

- **GZip swallowed the live job log.** `/jobs/<id>/stream` is SSE, and compression is a
  buffering operation. A publisher-refresh ran to completion, wrote a 127KB log and 30
  recipes, and BOTH viewers showed nothing. `_NoGzipForSSE` drops `Accept-Encoding` on
  `Accept: text/event-stream`, registered AFTER GZip because Starlette builds the stack in
  reverse.
- **A TDZ ReferenceError at boot** — the top-level `sidebar` const is declared ~700 lines
  below the first render, and `if (sidebar)` does not guard that: the guard is the throwing
  read.

### UX reversals, all in the same direction

The curator's framing: keep what the user is looking at.

- The criteria dialog was **closing the sidebar behind it** — a modal `<dialog>` is in the
  TOP LAYER, so `sidebar.contains(e.target)` is false for every control in it, and "Show
  recipes" filtered the list and hid it in one gesture.
- **Clear all only reset the draft**, leaving the list and badge filtered — it read as a
  button that did nothing.
- `defaultOpenSidebar()` **never worked**: it calls `LibraryShell.openSidebar()`, which reads
  `state.sidebar`, populated only by `LibraryShell.init()` — and this page calls `initNav()`
  and never `init()`.
- **Saving no longer closes the recipe** (reversing 2026-05-29). The "Saved until a change is
  detected" half was already built and simply never visible, because clearing the form wiped
  its content out from under `paintRecipeSaveState()`.
- **The dialogs were never centred.** `* { margin: 0 }` overrides the UA's `margin: auto`,
  which is what centres a modal — the existing error dialog had it too.

### Data cleanups

`scripts/normalize_recipe_fields.py` (dry-run by default), 3,415 rows: **3,371 timestamps
with no timezone** — proven UTC, not assumed (master #3512 has created_at naive 18:23:14 and
updated_at aware 18:25:40+00:00, so a local reading would have it created four hours after
it was updated); 43 names with stray whitespace incl. non-breaking spaces; 1 row with the
literal "<UNKNOWN>" as its cuisine, ethnicity AND technique, now scrubbed at the extractor.

**Embeddings: 0 failures, 0 warnings** for the first time. 1,426 rows re-embedded (~$0.002).
The dry-run said 1,426 rather than the 48 reported stale because a NULL hash never matches —
which reconciles both tools exactly (46 stale + 1,348 unstamped = 1,394 master). Root cause
fixed: the save path wrote the embedding and never recorded what text produced it, so every
new row arrived unverifiable and would be redone on every pass. `dishes` stamped
model/hash/timestamp from the start; the recipe tables never did.

### Numbers that summarise the day

| | start | end |
|---|---|---|
| boot payload, master | 6.89 MB uncompressed | **15.7 KB** |
| boot time | ~2.2 s | **0.042 s** |
| DOM elements | ~75,600 | ~1,400 |
| text search | 380-440 ms, substring | **7-35 ms**, stemmed full-text |
| default sort, LIMIT 200 | 187 ms | **0.0 ms** |

## Session log — 2026-08-21 — three hashes that never matched, a dish match that never ran on master, and a threshold that was letting in Pumpkin Spice Latte

Nine commits, `e2aa17f` through `e1393d1`. Two restarts (10:13, 10:45); a third is owed.

### The morning's two verifications, one of which was a bug

`f2d7d9d`'s two gates went live and were checked against the running server, not by
reading code: `((((` now returns **0 suggestions** (was noise), `atk` returns 0 (nothing
clears the 0.45 floor), and `something brothy and warming` still returns six real
near-misses. Worth noting `chowdr` returns 0 FTS matches but the semantic leg surfaces
**Creamy Corn Chowder** at #4 — the typo gap is partly covered already, just not ranked.

The `embedding_text_hash` check looked fine and wasn't. Both of the curator's morning
saves stamped a hash — but `check_embeddings` went from 0 failures at end of yesterday to
2, reporting 21 stale rows. The cause:

    save_recipe_api      sha256(txt).hexdigest()        -> 64 chars
    check_embeddings     sha256(txt).hexdigest()[:16]   -> 16 chars
    reembed_identity     sha256(txt).hexdigest()[:16]   -> 16 chars

A 64-char hash can never equal a 16-char one, so **every row saved through the app read as
STALE forever** — the same failure the stamp was added to fix (a NULL hash never matches),
inverted. Measured, not inferred: 16 master + 5 personal rows carried a 64-char hash, and
those were exactly the 21 the checker called stale.

Fixed as ONE definition (`embeddings.text_hash`) that all three call, rather than patching
the writer to match — three copies is how it drifted. The 21 rows restamped in place;
every one was a pure truncation (`stored[:16]` equalled the freshly recomputed hash),
which PROVES the vectors were current all along and only the stamp format was wrong. No
re-embedding, no spend.

### The suggestions that did nothing were two bugs wearing one symptom

The "You might mean" entries didn't open. Both causes produced identical silence:

- **The handler never closed the sidebar.** The recipe loaded UNDERNEATH it. Result cards
  have always closed it; this path was written without that step. On the iPad that alone
  is the whole bug.
- **It opened the row from the collection being BROWSED.** Suggestions always come from
  `master_recipes` (the vector index is the master one) even while you are in your own
  recipes, so the click asked for a master id out of the personal table. Measured: same
  id, `200` at `user_id=0`, `404` at `user_id=5`. `if (r.ok)` turned that into silence.

Suggestions now carry `user_id: 0` — rows have always carried their own; suggestions did
not. A failed open warns to the console instead of vanishing.

### "Does user 5 + admin unlock equal master?" — yes, and that was the wrong suspect

Minted both credentials locally and compared `/auth/me`:

| identity | role | permissions |
|---|---|---|
| uid 0 + master token | owner | all 9 |
| uid 5 + master token | owner | **identical 9** |
| uid 5, no master token | member | `own_recipes`, `staff_locked: true` |

Server-side parity is exact and the nav is permission-driven (`perm: 'edit_master'`),
never `user_id === 0`. **The real asymmetry is the STORE TARGET, not the role.** Signing
in as user 5 points the sidebar at your own collection; the recipe form's enrich controls
key off that, so master shows "Save & Re-Enrich" and personal shows "Enrich". Same
permissions, different button. Ticking the Master checkbox as user 5 restores it.

Two findings fell out of the audit:

- **`/domains/{d}/deep-enrich` had NO permission check.** It took no `request` parameter,
  so there was nothing to check with — the gate added to its sibling `/enrich` on
  2026-07-29 missed it. It is the *more* expensive of the two (~16 Moz rows AND a Sonnet
  call). Now `edit_master`.
- **77 of 108 write endpoints have no permission check at all** — product/review/collection
  CRUD, `/chapters`, `/scheduled-jobs`, `/domains/rescore`, `/cook-kb`, `/ws-categories`.
  Some are correctly open (`/auth/login`, `/stage-markdown`). NOT swept: which surfaces
  members may touch is a decision, not a refactor. **Open.**

### The dish match had never run on a master row. Not once.

The curator's report was "the recipes process on the master should get embedding by
default". The recipes *were* embedded. What they never got was the dish match derived from
the embedding, because the save path gated it on WHICH TABLE the row was in:

    if user_id == 0:   # master: store vector + KNN index      <- no match, ever
    else:              # user recipe: store vector AND match a dish

The assumption was that master rows carry `_master.dish` because a dish refresh curates
them FOR a dish. True of harvested rows; false of every interactive capture. So a
bookmarklet capture computed a vector, stored it, and threw away the one thing the vector
was for — the form's "Matched dish" chip read an em-dash on a row that WAS embedded, which
reads as "never embedded".

    master rows              5483
      no _master.dish        3253  (59%)
      carrying _match           0  <- the branch had never run there

The rule is now **"infer a dish when the row doesn't know one"**, not "user rows only".

### The backfill, and two bugs of my own

`scripts/backfill_dish_match.py`. Free by construction — every row already has its
embedding stored and the dish index is local, so it is a sqlite-vec KNN per row: **14s for
3,223 rows**, no embedding call, nothing billable. Writes `data` only, NEVER `updated_at`
(derived metadata is not an edit, and bumping it would reorder the sidebar's default sort).

First run reported **16 failures, all `UnicodeEncodeError`** — raised by the PROGRESS LINE,
not the database. The Windows console is cp1252 and the titles were Greek and Chinese, and
because the print ran before the write, the `except` swallowed the whole row. Fixed both
halves (UTF-8 stdout AND write-before-report, since either alone leaves the class open).
Re-run recovered all 16, every one confident: 6 Dan Dan Noodles, 5 Greek Chickpea Soup, 2
Galaktoboureko, Baklava, Beef Stew, Shrimp with Tomatoes and Feta — precisely the
population the console choked on.

Second: the script only re-stamped the vec index on a *confident* match. Fine on a first
pass (the index already holds None), wrong under `--rematch`, where a demoted row would
keep a stale dish in the index while `data` said otherwise. Closed before the threshold
change made demotions common.

### The threshold was letting in Pumpkin Spice Latte

Sanity-checking the weakest matches into the new dishes found real errors at the boundary:
Pumpkin Soup → Pumpkin Pie (0.799), **Pumpkin Spice Latte → Pumpkin Pie** (0.799), Fortune
Cookies → Sugar Cookies (0.792), Biscuits → Sugar Cookies (0.786). So the whole
distribution got measured — does the row's own identity-card `likelyDish` agree with the
dish it matched?

| L2 band | agree | disagree | % wrong |
|---|---:|---:|---:|
| 0.0-0.3 | 194 | 7 | 3% |
| 0.3-0.4 | 104 | 17 | 14% |
| 0.4-0.5 | 87 | 21 | 19% |
| 0.5-0.6 | 75 | 30 | 29% |
| 0.6-0.7 | 55 | 100 | **65%** |
| 0.7-0.75 | 27 | 104 | **79%** |
| 0.75-0.8 | 33 | 191 | **85%** |

The metric was validated rather than trusted: the low-band "disagreements" are mostly
artefacts of comparing names — `Pastitsio`/`Pastitcio (Greece)`,
`Shrimp Saganaki`/`Shrimp with Tomatoes and Feta`, `Eggplant Parmesan`/`Parmigiana` are the
same dish twice — so the low bands are BETTER than shown, while the high bands were
confirmed by eye. **`dish_match_max_distance` 0.8 → 0.6**, set through the API so
`set_setting` invalidated the running server's cache in-process (no restart needed).

    confident matches   1045 -> 511
    card disagrees       45% -> 14%

508 rows moved, overwhelmingly demotions to `None`; every one keeps its distance and top-3
candidates, so nothing was destroyed, only reclassified. The dishes created that morning
were themselves the biggest over-claimers at 0.8 — adding them did not cause that, it
exposed it.

### Signal 1 and Signal 2: what the backfill said to build next

Two independent signals, answering different questions.

**Signal 1 — recipes held that no dish claims** (2,271 uncovered rows, 1,757 distinct
`likelyDish`), crossed against `dish_keywords` demand. Top by both: Chili (7 recipes /
1.29M traffic), Cheesecake (7 / 706k), Mashed Potatoes (12 / 413k), Chocolate Cake
(10 / 350k), Hummus (6 / 325k), Pizza Dough (10 / 316k). **The trap:** the dish with the
MOST recipes held is Melomakarona — 13 recipes, **3,618** traffic. Supply rank and demand
rank are close to inverted at the tail. (Melomakarona and Roast Turkey are seasonal; a
snapshot out of season undercounts them.)

**Signal 2 — existing dishes absorbing recipes that aren't theirs.** Cheaper and more
urgent, because it corrupts cohorts already ranking. **Cream Pie was functioning as a
generic pie bucket** — pulling in Pumpkin Pie, Pecan Pie, Chicken Pot Pie and bare Pie
Crust (13 rows). Ten dishes created: Chicken Pot Pie, Pumpkin Pie, Pie Crust, Pecan Pie,
Pot Roast, Marinara Sauce, Waffles, Sugar Cookies, Roast Potatoes, Bougatsa. The last two
are deliberately demand-NEGATIVE (13k and 241) — built to clean cohorts, not chase traffic.

Skipped with reasons: **Vegetable Lasagna** (a variant of Lasagna, an M2M question not a
new record), **Potato Latkes** (latkes *are* potato pancakes — the "mis-filing" may be
correct), **Galatopita** (91 traffic, 2 recipes).

The curator then ran dish refreshes on all ten. Each now holds a **full 20-recipe cohort**
from ground-truth `_master.dish`, and **30 previously-unclaimed rows were adopted** — the
mis-filing fixed properly rather than merely re-labelled.

### The sweep becomes a nightly job, and the rewrite test becomes the default

The curator's correction — *"CAN'T YOU TEST TO SEE IF THE REWRITE IS NECESSARY.. THAT
SHOULD BE THE DEFAULT"* — was a prerequisite for scheduling, not a nicety. The old version
stamped `data` on every row it scanned, so a nightly run would have dirtied 3,223 JSON
blobs for ~0 real changes, inflated the WAL, and made every night's `recipes.sql` diff the
size of the table — against a dump already at 53MB of GitHub's 100MB limit.

`same_verdict()` compares dish/confident/distance and skips the write. `matched_at` is
excluded ON PURPOSE — it changes every run, so including it would make every row look
changed and silently defeat the test. Verified: a second consecutive run scans **3,223 rows
and writes 0**.

`input/pipeline/dish_match.py` is now the single implementation (save path, script, job).
The copies had already drifted on whether to stamp the vec index.

**What the sweep is FOR** — the premise needed correcting. New recipes are matched AT SAVE,
so this is not how they get a dish. It is for the CATALOG moving underneath rows already
matched: ~45-60 new dishes a month plus query/description edits that move a dish's own
vector. *Creating "Pumpkin Pie" does not move the pumpkin pies out of Cream Pie; only a
re-score does.* Frequency chosen from that rate — the catalog changes most days, cost is
14s and ~0 writes, and the day's pattern was create-then-refresh-same-day. **`dish_rematch`
registered at 24h.**

### The two dishes that were not dishes

`Chefs: Legal Seafood` and `Julia Child Best Recipes` were collections wearing a dish
record, acting as match ATTRACTORS — a Beef Bourguignon matched "Julia Child Best Recipes"
at **0.141**, the tightest distance in the corpus. The curator approved deleting them.

`DELETE /dishes/{name}` **cascades**: it also deletes the dish's `kind='top'` master rows —
10 real recipes, including three Legal Seafood clam chowder captures. So the membership was
cleared first and the recipes kept.

Two intentions reversed by looking rather than assuming:

- **`retire_master_membership` was the wrong tool.** It is the shared function for exactly
  this, and it DELETES the row when no other typed block remains. These ten carry only a
  dish block and no publisher block, so it would have destroyed what we were protecting.
- **`exceptionalism` was kept, not dropped.** The plan was to drop it as a grade earned in
  a bogus cohort. Checking the basis on all ten showed every grade came from
  `chapter-fallback` or an embedding-match against a REAL dish (Soups & Stews, Poultry,
  Baked Stuffed Shrimp) — never the fake cohort. Independent, so it stays.

`dish=None` was stamped into the vec index for all ten BEFORE the delete, so the KNN filter
never pointed at a dish about to stop existing. Both deletes returned
`cascaded_master_rows: 0`; `dishes` and `dishes_vec` went 179 → 177 in step.

Six of the ten immediately found real homes: three clam chowders → **New England Clam
Chowder** (0.168-0.194), two → **Crab Cakes** (0.254, 0.327), one → **Baked Stuffed
Shrimp** (0.255). Four correctly stayed unclaimed — Baked Scallops (0.845), Key Lime Pie
(0.782), Supremes de Volaille (0.856), Coq au Vin (0.882). **Key Lime Pie at 0.782 is a
real catalog gap**; it had previously been graded against *Apple Pie*.

### Process notes

- **`system_config.get_setting()` IS the job-reset landmine's trigger.** With no `db_path`
  it lazily imports `save_recipe_api` purely to read `DB_PATH`, and that import runs the
  app's startup, which resets in-flight jobs. Tripped once today (nothing was running).
  `jobs/__main__.py` calls it without one too. Pass `db_path` explicitly in any script.
- **A display concern must never gate a data write.** The cp1252 crash cost 16 rows because
  the progress line ran before the UPDATE.
- **Validate the metric before trusting the finding.** The name-comparison "disagreement"
  rate would have overstated the low bands badly; checking the low-distance disagreements
  showed they were `Pastitsio`/`Pastitcio` renames, not errors.
- **Read the shared helper before reusing it.** `retire_master_membership` does the right
  thing for refreshes and the catastrophic thing here.


## Session log — 2026-08-22 — a pre-filter that dropped the word it searched for, a fetch tier that called a challenge a success, and three thresholds that disagree

Eight commits, `a71cbf7`..`c7d5da8`, on top of the 2026-08-21 nine.

### The Tiramisu harvest was dropping 80% of its own results

`URL-SKIP 38 of 47`. The url pre-filter keeps a URL only if its path names a known
food word, and **`tiramisu` was not among the 2,085 learned words**, so every
`/tiramisu/` URL was dropped BEFORE the fetch. Waffles was also 80%, Sugar Cookies 33%.

The fix is not the missing word. **A run must never pre-filter away the word it
searched for** — whatever the dish is called is, for that run, a food word by
definition. `url_lacks_recipe_signal` takes `extra_food`; the dish batch passes the
dish name plus its query words. Re-run: **38 skips -> 0, scored pool 47 -> 80**, same
20 saved but from a 70% larger field.

30 words from EXISTING dish names were unknown to the list, so this was waiting for
Bougatsa, Shopska, Tabouleh, Scoglio and the rest. 14 unambiguous ones added to the DB
list; `betty`, `bee`, `young`, `foo`, `norma`, `lok`, `lak` deliberately NOT — measured
that only **6 of 179 dishes have no known food token**, so the rest already pass on a
sibling ("Bee Sting Cake" on `cake`) and adding them globally buys nothing.

**And a bug in the fix itself:** it tokenised the dish NAME with the same `[^a-z]+`
split the URL tokeniser uses — right for a slug, wrong for the name. `Fricassée` gave
`fricass`, `Ragù` gave `rag`, neither able to match the slug it was meant to rescue.
`name_tokens()` folds accents first. Third time accent-blindness bit in two days.

### memoriediangelina: the fetch tier was calling a challenge a success

Every URL fell through to Wayback; 49 of 119 candidates were lost outright. Diagnosis
went through two wrong answers before the right one:

1. *"The site is anti-bot"* — no. `curl` got 200/583KB with no UA at all.
2. *"Our Chrome/120 UA has rotted into a block"* — partly true (Chrome/140 got the full
   article once) but NOT the cause, and it never reproduced.
3. **The real one:** `fetch_with_ua_fallback` stopped at the first 2xx, and this WAF
   answers the bot UA with **`202` and a 198-byte stub**. The chain "succeeded" on UA #1
   and never tried the browser UA behind it. Every fallback in that list was dead code
   against exactly the sites the fallback exists for.

Worse, and separately: the soft-block check was gated on `unblocker`, which is off by
default — so a block was returned as a successful direct fetch and handed to the
extractor. Both fixed: block-check always runs, `unblocker` only decides whether we can
escalate, and a stub no longer ends the UA search.

**Settled by measurement:** one probe through the unblocker returned 200/583KB live on
the URL that had failed twice. Run 920 with `fetch_strategy=unblocker`:

    fetch failures  49 -> 0      scored pool  67 -> 116      winners  30 -> 30
    9 of the 30 winners are NEW — Parmigiana di Melanzane, Carciofi alla Giudia,
    Focaccia Genovese — invisible before because their pages had no snapshot.

Two traps had to be cleared first or the re-run would have lied: all 70 cached pages
for the host were `source='wayback'` (the cache would have served them back and the
unblocker would never have been called), and the domain had recorded
`content_obtainable='direct', 30 of 30, 100% yield` on a run where **zero** direct
fetches succeeded.

That last one was a real bug: the verdict came from the CONFIGURED flag, not the
transport used. Now derived from `_source.archiveUrl`, which the save path already
stamps, with `wayback` added to the enum — a materially different state from `direct`,
because the content is a dated snapshot and any URL without one is lost.

**Process note:** hammering a live publisher during diagnosis tripped its rate limiter
and cost the clean measurement. One probe first, then reason.

### Dish coverage became a page, and "possible split" became three answers

`GET /dish-coverage` + Dishes/Coverage in the admin nav. It reports the gap between
`_identity.likelyDish` (free LLM text, 2,808 names with no dish record) and `_match.dish`
(a CLOSED set — a KNN over `dishes_vec`, so it can only ever return a dish that already
exists; verified 125 distinct values, 0 outside the catalog).

"Possible split" was one label over three cases wanting opposite actions, so it now
classifies: **split** (14 — different dishes sharing a word, create it), **variant**
(121 — same identity narrower, a JUDGEMENT since `docs/collections.md` §11 says variants
are not the M2M case), **alias** (6 — same dish another spelling, a synonym not a dish).
Rules earned by testing against the real 143 pairs; equal token sets were the miss in the
first cut.

**Search is fed by `likelyDish`, not the dish record** — proven by `elote` returning 19
recipes all titled "Mexican Street Corn". So uncovered dishes are already findable; the
coverage gap costs cohorts and ranking, NOT discoverability.

### The git-side backup had not been restorable for two days

Regenerating `recipes.sql.gz` and then actually REPLAYING it found the backup broken:
`SQL logic error` at statement 275,023, an INSERT into `master_recipes_fts` carrying the
hidden `rank` and table-name columns an fts5 table will not accept. The search work of
2026-08-20 added that index, so **every dump since had been unrestorable** — and nobody
noticed, because nobody replayed one.

**Second time.** Generated columns did the same on 2026-07-22 and that fix did not
generalise, so this one is by CLASS: the exclusion query matches `USING vec0` OR
`USING fts5` (catching `recipes_fts` as well) and will catch the next virtual table with
nobody remembering to come back. Both rebuild free from tables that ARE in the dump —
vec0 from the embedding BLOBs, fts5 from `master_recipes` by its own triggers.

**The real defect was not fts5** — it was that a dump got written, committed and trusted
three times without being replayed once. `backup_db.py --verify` now restores the gz into
a throwaway db and compares every table's row count, refusing the dump if anything
differs. Verified: RESTORE OK, all counts match, 5,851 embedding BLOBs intact. Dropping
the fts tables also took it 70.3 -> 66.7 MB.

### The page cache had no delete path and had reached 1.28 GB

The TTL was READ-ONLY: `get()` refuses a page older than `page_cache_ttl_days` (5) and
nothing anywhere issued a DELETE, so the file grew ~100KB per candidate fetched with no
ceiling. Found at **1.28 GB across 12,253 pages, 65% never served once**, on a host with
an RMA pending.

`page_cache.purge()` + nightly `page_cache_purge`. Retention is a SEPARATE setting from
the TTL — `page_cache_retain_days` (30, six times the serving TTL) — because the TTL says
how long a page is SERVED and retention how long it is KEPT; the margin means raising the
TTL later cannot silently discard pages that would then have been serveable. VACUUM only
above 20% free pages. First run: 3,894 rows, **1.28 -> 0.85 GB**. Steady state is 0
deleted.

(Two 0-byte files, `cache.db` and `pages.db`, were MINE — a diagnostic that called
`sqlite3.connect()` on guessed filenames, which creates the file. Deleted.)

### Prepared for the scoring session: §14 of the scoring design

**79% of embedding-graded master rows are graded against a dish their own `_match`
rejects**, because "is this recipe that dish?" is asked in three places with three
answers — save path 0.60 L2, similar-master 0.60 L2, **grading 0.949 L2**. The first two
were tied together on 2026-08-06 with a comment about not letting them drift; grading was
never brought onto that bar. 59% of the mismatch predates the 0.80 -> 0.60 change.
Full measurement, worked examples and the three unknowns are in
`docs/recipe-scoring-design.md` §14.

## Session log — 2026-08-22 (evening) — the defect pass is born, a page that was only slow, and pizza splits six ways

Eleven commits, `15f18f8`..`f555f80` plus this one. The scoring-enhancement session the
START HERE pointed at — and it went somewhere better than planned.

### The ChatGPT dialog, and what was refused

The curator brought a ChatGPT thread proposing a quantified finalist score:
`BEST = wQ·Q + wU·U + ...` over seven 0-100 dimensions with invented weights.
Verdict, now recorded in `docs/soundness-defect-pass.md` §7: the two-score
architecture and the near-zero marginal cost insight were right (it re-derived our
own three-stage shape); the BEST formula was the cancelled AI-editor role wearing a
formula, the weights validated by nothing, and `outcome_potential`/`dish_fit` are
taste-laundering (§13: "an AI cannot know what tastes better"). STOLEN: piggybacked
structured output, pairwise-comparison idea (parked), repeatability testing.
REFUSED: every scalar. The curator's own words named the risk: "the problem is
it's a slippery slope." The answer that held: structure, not discipline — a thing
you cannot ORDER BY cannot become a selector.

### Measured before designed: the thinness pass

The curator's motivating experience — winners "extraordinarily thin, missing steps,
bizarre order" — was measured over all 5,657 winners before anything was built:

    steps median 6; <=3 steps 10.9%; 12 rows <=2 steps (read in full)
    naive thin flag (<=3 steps OR <100 words): 20.1% — but judgment samples: mostly INNOCENT
    ghost-ingredient flag >=2: 18.5% — top offenders ALL the "combine all the X
    ingredients" idiom; every one a fine recipe my token-matcher couldn't resolve

The 12-row band split three ways: 8 legit-terse (tabbouleh, cocktails, NYT pie
crust), 2 OUR extraction truncations, 1-2 simplistic-but-complete (Mississippi Pot
Roast — structurally sound, demand-selected, editorially weak: THE COMMENTARY
LAYER'S CASE, not a defect). Conclusion: nothing in the pipeline reads the
instructions; code can't separate thin-bad from terse-good; that separation is
judgment — the defect pass.

### Two extraction truncations found and fixed

* **#7545 Barefoot Contessa Parmesan Chicken** — 1 ingredient / 1 step (a mid-recipe
  vinaigrette line). Stale row predating the 2026-08-11 fast-lane fix; the cache
  already held the good extraction. Re-ingested via process-selected: 5 steps/13 ings.
  `n_ingredients <= 2` = a perfect-precision truncation marker (1 hit corpus-wide).
* **#8582 Sour Cream Coconut Cake** — "mix all ingredients except coconut" twice over
  one undivided list. Root cause: the page's JSON-LD `HowToSection name="Frosting
  Instructions"` and our flattener DISCARDED the section name; second bug under it:
  WPRM sets every step's `name` equal to its own text, so the first fix's "only if no
  name" guard skipped. `15f18f8`: echo-names stripped, section names land on child
  steps. THREE cache traps in the repair: the llm_extract_cache row (deleted), the
  REVALIDATE reuse path serving the cached recipe keyed on the WAYBACK url (why a
  delete on the publisher url matched 0), and my own too-broad LIKE that took 16
  other coconut-cake cache rows with it (regenerable, but sloppy).

### The soundness defect pass — spec'd, built, calibrated (shadow only)

`docs/soundness-defect-pass.md` + `extract/defect_pass.py` + `scripts/defect_pass_shadow.py`.
The boundary: NO scalar of any kind; defects with VERBATIM quotes; disqualify-for-cause
feeding the EXISTING reserve-backfill path (never reordering); taste out of scope by
construction. The honesty layer is mechanical: code verifies every quote is a substring
of the RENDERED user prompt (models quote steps as displayed — "4. [label] text" — a
reassembled-fields haystack rejected honest citations twice); disqualify is honored only
when a critical defect SURVIVES validation. Review scales per prompt_version (one ~30-row
audit per hash), never per row — the curator's explicit requirement.

**The calibration arc settled the model tier.** Haiku invented defects through two
rounds of targeted fixes (demanded a grinding step against "seeds removed and ground";
a cooling step against "Spread over cooled cake") and inflated severity. Sonnet: zero
false positives on the six traps AND full recall on two synthetically broken recipes
(deleted meatball step -> critical+disqualify; injected time contradiction -> major).
Also settled: Claude 5 rejects `temperature` (only sent to Haiku now); a resolved
reference cannot also be flagged; prep stated in an ingredient line needs no step;
notes ride along and are citable. The validator killed a fabricated "[Assemble and
serve]" label unaided, twice, from both models.

**First 50-winner shadow run: 0 disqualifications**, 8 rows with defects, majors 3/3
verified TRUE against stored text (Trofie: list "½ tbsp Parmesan" vs step "6
tablespoons"; Gogges: ingredient list flour/water/salt, steps use myzithra+oil).
Minors mixed; they never gate. Base rate confirms: truly broken winners are rare, so
a for-cause gate is review-by-exception cheap.

### "The coverage page is empty" — it was 68 seconds of LIKE

The new /dish-coverage endpoint ran two `LIKE '%name%'` scans over 171,503
dish_keywords rows for EACH of 568 uncovered names, ~68s per page load; "Loading…"
read as empty, and the tunnel sometimes gave up. `54b225f`: one scan builds a
word-posting index, candidates from the name's rarest token, full-name
substring-verified — 0.4s, same matches minus tokens never appearing as their own
word. The data was never missing.

### budgetbytes: keep=50 in the recipe_path field, three "successful" zero-runs

`recipe_path='50'` on the domain record path-scoped every run to `/50/` and dropped
all 197 URLs — three runs REPORTED SUCCESS at 0 extracted. Cleared: 197 discovered,
187 recipe_pass, 50 winners into master, 94% direct yield. `f555f80` makes the class
impossible to repeat silently: **a path that drops ALL discovered URLs now raises**
(the tiramisu rule again — a run must not filter away the thing it came for).
Curator-directed form rework rode along: recipe_path + harvest TTL moved INTO each
discovery-source box (duplicated `_bf`/`_serp` ids over the same stored columns; the
active box wins via `srcVal`), shared panel keeps keep-top-N + exclude-words, and
every `serp` fallback became `backlinks_file` — SEMrush is the default source, and a
missing file fails loudly rather than silently spending SERP credits.

### Pizza became six dishes, 60/60 saved

Curator asked how to build the pizza query; demand data said don't: generic `pizza`
(69k) is chains-and-listicles, while the real signals are specific — dough 177k,
deep dish 75k, Detroit 39k, NY 19k. Styles are SPLITS (different procedure sharing a
word), not variants. Created + refreshed: **Pizza Dough** (10/10, fit r²=0.77,
n=41), **Detroit-Style Pizza** (10/10, zero rejects), **Deep Dish Pizza** (10/10,
one reserve-backfill — the exact mechanism the defect gate will reuse),
**New York-Style Pizza** (10/10, bottom OU 8.5, strongest), **Pizza Margherita**
(10/10), **Greek Pizza** (10/10 — curator add; the pool answered the two-identity
question: feta-topped dominates, with Ladenia the traditional outlier).
Substitution niches (no-yeast, gluten-free) deliberately NOT seeded — variant
demand, not sourcing terms.

### Do next

1. **RESTART OWED** — the domains-form rework + refresh-top default (`f555f80`)
   landed AFTER the mid-session restart that served the coverage fix. One more
   restart picks them up. (The zero-drop guard is already live — jobs run current
   code.)
2. **Unfile "Greek Yogurt Pizza Dough" from Greek Pizza** (rank #2) — a Pizza Dough
   recipe matching on the word "Greek". One click in the editor; read
   `retire_master_membership` first (it DELETES when no other block remains).
3. **Defect pass toward teeth:** accumulate ~30 major/critical flags (larger shadow
   sweep), curator audits them, then wire `disqualify` into the reserve-backfill
   path. `soundness_reports` table exists; `applied=0` everywhere.
4. **Pizza Dough fetch-fail rescues** via bookmarklet: 177milkstreet (gated),
   pizzaeveryfriday.substack.com, brianlagerstrom.com.
5. The 2026-08-22 START HERE's standing items (§14 cohort experiment, Pie Crust
   chapter, Key Lime Pie, xiachufang, ingredient quantities) remain open.

## Session log — 2026-08-23/24 — enrichment was invisible not broken, names beat distances, and every header becomes one header

Fifteen commits, `15f18f8`(prior log)… then `e9d6a81`..`1458fbf`. A morning scare that
wasn't a regression, one real matcher upgrade, an image sweep, and a pile of UI debts
paid.

### "No enrichment" was three different truths, none a regression

The curator reported enrichment gone (no button, no auto-enrich) after a 15-publisher
harvest morning. Runtime data said otherwise on every layer:

* **Interactive master saves WERE enriching** — 266 SAVE-ENRICH log lines, ~9s stories
  all morning. What was broken was the DISPLAY: the post-save handler deliberately
  "reloads nothing (the form is by definition what the DB holds)" — a premise
  save-enrich falsifies, since the server just wrote a story the form never had. The
  curator watched blanks while the DB filled. `e9d6a81`: after a story-less master
  save, fetch the stored row and populate what enrichment wrote.
* **Batch rows arrive un-enriched BY DESIGN** — `_skip_auto_enrich` on the publisher
  path, and dishes' `auto_enrich` off on 187 of 189. Decision: LEAVE AS-IS,
  enrich-on-touch.
* **The hidden Enrich button was the old master/personal split.** Curator call:
  one button. `4b9d5bd`: Save & Re-Enrich REMOVED (with its force-flag plumbing);
  the plain Enrich button now always shows on master, enriches the form in place,
  Save persists. Verified live on Flammekueche (1,762-char story).

Also that morning: the 2026-08-22 zero-drop guard caught its second real typo within
12 hours (`recipe_path='/recipies/'` on sugarologie — loud error, fixed, re-run
succeeded), and the "hung on image update" reports traced to the harvest phase's
UNTIMESTAMPED log lines making the stream look frozen — no actual stall in any log.

### The advanced search grew a Site filter — then earned its polish

`4b6eb95`: `/recipes/search` takes `domain` (two sargable prefix LIKEs on
url_normalized — scheme kept, www stripped); the dialog gets a type-ahead over
"Display Name — host" option text; the applied value is always the HOST.
`d2b905c` after curator feedback: the option counts were `domains.master_recipe_count`
— a stored counter that neither cascades nor matches the predicate ("numbers that
don't correlate"). Replaced with LIVE cascaded counts from the same query the filter
runs (host-extraction GROUP BY as `facets.domain`); zero-count sites hidden; missing
facet data reads as unknown, never all-zero. Plus an ✕ clear button.

### Name evidence now beats the distance verdict (`0498fa5`)

The akispetretzikis lasagna: likelyDish 'Lasagna Bolognese', nearest dish BOLOGNESE
THE SAUCE at 0.6186, Lasagna behind it at 0.68 — unassigned, wrongly-neighboured, and
a dozen plain lasagnas stranded at 0.60-0.69 with likelyDish saying 'Lasagna'.
**When `_identity.likelyDish` exactly equals a catalog dish (name / display_name /
alias, accent-folded), that literal identity claim overrides the embedding in BOTH
directions** — claims stranded rows AND corrects confident-but-wrong neighbours.
Distance + candidates stay recorded, `method='name-exact'`.

* **Token-subset matching measured and REJECTED the same day**: 179 hits included
  Boston Cream Pie → Cream Pie and Greek Pasta Salad → Greek Salad. Exact only.
* **`dishes.aliases` (JSON) shipped** — the minimal start of
  docs/dish-alias-normalization.md, seeded with the Lasagna family. No editor UI yet.
* First sweep: 34 corrected (incl. two confident Bolognese mis-claims — Argiro and
  Bon Appétit — and five 'Pastitcio (Greece)' renames). After the curator added new
  dishes, a manual `dish_rematch` (Jobs form ▶ Run now) moved **270 rows**.
* Grading's own matcher (§14's 0.949) deliberately NOT touched.

### The image sweep: 164 imageless → 46, and a new extractor fallback

lidiasitaly publishes NO og:image and no `<img>` hero — the photo lives in a
recipe-classed container's inline background-image and the share buttons' data-image.
`738b8d7`: extract_og_meta falls back to those two, only when the meta tags are
absent, skipping svg/theme chrome (baked-ziti, genuinely photo-less, still returns
none). Then the corpus-wide sweep, measured first, run on approval:

    69 fixed from cached pages (joyofbaking 39 — same hiding pattern)
    30 fixed by polite direct fetch (Greek sites, pauladeen, spainonafork…)
    19 fixed via unblocker (bostonchefs came through)
    ~35 remain: page genuinely has no photo (weekendbakery 25)
    5 gated (NYT/Globe), handful of strays

Two lessons paid for: `fetch_via_unblocker` returns a TUPLE (first wave burned zero
credits failing on that), and the backfill import tripped the import-resets-jobs
landmine again (harmless — nothing in flight — but the guard item stays open).

### UI debts, each its own commit

* **Coverage page had no header/burgers** (`1b7ee6c`) — markup was there, nobody
  called `LibraryShell.initNav`. One line.
* **Dish page couldn't cancel a running refresh** (`6aaca9c`+`4f5f436`) — the domains
  form's cooperative-cancel pattern, wired to the stream so the attach path gets it.
* **Search-first list panels** (`535aae9`) — dishes + domains: the search box now
  permanently in the sidebar head, autofocused; magnifier demoted to Sort & tools;
  page title moved into the header brand line.
* **THE SHELL-WIDTH CONTRACT** (`b76a229` then corrected by `1458fbf`): Job Monitor
  ran a 1200px body under a 960px header, five more pages likewise. First fix made
  the header follow the page — which preserved the OTHER bug the curator then named:
  headers varied form to form. Final shape: **`--shell-w: 1200px` in tokens.css is
  the ONE header width, read by both shells; content sizes from `--shell-max`
  (default 960, raisable per page, never wider than the rail, never a bare pixel
  width).** Recorded in the page-shell contract memory.

### Standing state

* master ≈ 6,760; the defect pass remains shadow-only (50-run: 0 disqualifications,
  majors 3/3 true); soundness gate wiring + ~30-flag audit still the next step there.
* RESTARTS: all served as of 2026-08-24 14:43 EXCEPT the Site-picker live counts +
  search-domain facet (`d2b905c`+) — one restart owed when convenient.
* recipes.sql.gz at 71MB — GitHub warns every push; LFS-or-split decision pending.
* Greek Pizza still holds the "Greek Yogurt Pizza Dough" impostor at rank #2.

## Session log — 2026-08-24/25 — the repo goes on a diet, the backups earn an offsite, BAILEY passes the drill, and the harvest learns French

Roughly thirty commits, `738b8d7`..`07d8db6`. The DR arc dominated the 24th; the
25th was harvest intelligence and the recipe form.

### The two time bombs, both defused

* **`recipes.sql.gz` left git.** 29 committed dump versions had made `.git` 1.6GB
  with the file itself at 73MB marching toward GitHub's 100MB hard reject. Step A
  untracked it; step B `git filter-repo` purged `recipes.sql.gz` + `recipes.sql` +
  `recipes.db` from all 917 commits and force-pushed both branches: **1.6GB →
  31MB**. Backups are ADAM's timestamped, restore-verified copies — git is code
  again. (Old clones must re-clone; commit hashes changed across history.)
* **The import-resets-jobs landmine's main path**: `get_setting()` with no
  db_path lazy-imported the whole app (2s of startup) to read the hardcoded
  string `"recipes.db"`. Now defaults to the literal. The jobs-wipe half was
  already defanged by the PID-liveness check (jobs 784/822).

### Backups: verify nightly, then go offsite (Google Drive, 5TiB)

* The 3 AM task now runs `--verify` — both historical unrestorable-dump incidents
  went unnoticed for days because only hand-run backups replayed. Look for
  RESTORE OK in backup.log every morning.
* **rclone → Drive** (`drive.file` scope, authorized 2026-08-24):
  `BCC-Backups/recipes-db` (14 days of verified dumps), `forms-mirror/` (full
  project incl. `generated/` hero uploads + `.git`), `db-latest/`
  (media_latest + freshest training). `env.backup` stays off the cloud by
  policy — the offsite key copy is the password manager.
* **DR audit found two assets protected NOWHERE**: `media.db` (357MB
  screenshots/TTS) and `generated/` (2.1GB incl. irreplaceable user hero
  uploads). Both now in the nightly (media via rolling consistent snapshot;
  generated via robocopy /MIR project mirror to ADAM + Drive).
* **`docs/disaster-recovery.md`** — the whole scheme: inventory, tiers, four
  playbooks, rebuild list, gaps (G1 ADAM retention still open; G4 rclone's
  shared client_id retires during 2026).

### G5 closed: the restore drill was real, onto BAILEY — and it caught three defects

Playbook C executed over SSH onto BAILEY (bare Win11 + git): mirror stream 5:00,
DBs 0:47, deps ~4:00, server answering, **RESTORE OK on the restored db itself**
(6,742 BLOBs). ~35 min wall including the finds; ~20 clean. The finds — the whole
point of drills:
1. **`recipe-core`** — a sibling EDITABLE pip dependency outside forms/, not a
   git repo, backed up NOWHERE; the app would never have booted on a real
   recovery. Now nightly-mirrored.
2. `requirements-frozen.txt`'s `-e c:\users\...` backslash path — modern pip
   eats the backslashes as escapes. Forward slashes now.
3. A task launched from an SSH session dies with the session → `/RU SYSTEM`
   (task `BCC-Drill` runs the staging server; NSSM is the cutover answer).
4. (And: BAILEY has no SMB creds for ADAM — a cutover prerequisite, in the doc.)

**BAILEY soaks as a verified parallel instance; cutover deliberately separate.**
`bcc_sync_bailey.ps1` = one-way incremental refresh (rclone-over-SFTP; `-WithDbs`
stops/reloads/restarts; `-FreshBackup` snapshots first). Curator confirmed the
instance works. The 2026-08-25 (this) session wired the sync into the nightly.

### Harvest intelligence, three pieces

* **Phase B — bounded unblocker salvage** (`salvage_unblocker_max`, default 3):
  a fetch-fail whose authority clears the cut bar gets ONE paid attempt through
  the same `_is_recipe_filter` (cache-aware; R1 honored). First production run
  (Brussels sprouts): 2 of 3 walls fell; second fix same day — **rescues insert
  by MERIT** (an OU-8.03 rescue had queued at rank 37 behind an OU-2.26 winner).
  Moules run: rescue saved at rank 2. Rejects no longer misreport a saved rescue
  as fetch-failed.
* **Foreign dishes: locale follows source_language.** Moules en Cassolette was
  querying the AMERICAN Google (`gl=us/hl=en` silently defaulted on French
  queries) → thin SERP, 9 xlate-bad rejects. `apply_locale_defaults` now stamps
  gl/hl from the dish's language at every queries write AND when the language is
  set (el→gr class mapped; explicit rows respected; also closes the
  textarea-resets-locales trap). fr/fr re-run: keeps 15→22, saved 10/10, zero
  fetch-fail rejects.
* **French publisher end-to-end**: cookingjulia.fr onboarded with exactly two
  fields (Language=fr + an FR-database SEMrush export — the filename regex takes
  any db code). Every candidate kept via json-ld [fr]; winners saved with
  English canonical + original French title preserved. PA saturation observed
  (16/18/20 bands); traffic-primary ranking was shipped then REVERTED same hour
  — **authority stays primary, traffic the tiebreaker** (curator call; on
  saturated sites they converge anyway).

### The Hotlist, and trafficPct grows up

`_scoring.trafficPct` (SEMrush share-of-site, verbatim) got a generated column,
a **Share of site** chip in the scoring strip, and a sidebar sort that became a
SELECTION twice over (curator corrections): first `WHERE traffic >= 1000 AND
pct IS NOT NULL`, then **ONE page per publisher** via a window function — 95
rows, each measured publisher's flagship. Facets and counts cascade under it.

### The recipe form, reshaped (and the pill hunted down)

* Header: title featured then calmed (1.15em/400, wraps via auto-grow textarea —
  as does site name; shared `wrap-field` + one newline guard), source identity
  under the title, description|image balanced, times compact (30m / 1h 30m —
  corpus backfilled 6,600 rows at the sanitize choke point), category 2×2.
  Scoring strip now labeled **Scoring**, first block inside Metadata.
* **The mobile pill saga**: the ugly stacked identity was the recipe form's OWN
  `personaChip`, not the shared badge (two browsers showing it ruled out cache).
  Removed; the shared badge is ONE quiet name (staff-elevated = the one colored
  state); email/unlock/lock/signout live in the ⋮ menu's identity section.
  **Two-line mobile header verified at real 390px** via an iframe sim
  (`/generated/_iphone_sim.html`, kept): brand+burgers row one (brand at
  flex-basis 0 — flex wraps on CONTENT size before shrinking, the actual bug),
  identity row two, left-aligned, never truncated.
* Site picker: live cascaded counts (facets.domain from the same query the
  filter runs — the stored domain counter never matched), ✕ clear, and a custom
  suggestion panel replacing `<datalist>` (iPadOS renders datalist options in
  the keyboard QuickType bar).
* Dish page got the domains-form Cancel button; the coverage page got its
  missing `LibraryShell.initNav` call.
* **Shell-width contract**: ONE header rail (`--shell-w: 1200px` in tokens.css,
  both shells read it); page content sizes from `--shell-max` (≤ rail). Fixed
  the Job Monitor's 1200px-body-under-960px-header AND headers varying page to
  page.

### Open / next

1. **Tab interface for the recipe form** — curator noodling on Main / Story /
   Data / Cook. Two non-negotiables written down: tabs are visibility over ONE
   DOM (no field moves), and actions announce their tab (Enrich → Story,
   Rework → Cook).
2. **RESTART owed** on MARLEY: hotlist one-per-publisher selection, rescue-reject
   fix, sanitize-times for interactive saves, dishes.py locale-defaulting for
   editor saves.
3. **Cutover to BAILEY in a few days**: write-freeze → final `-WithDbs` sync →
   NSSM + scheduled tasks + rclone + ADAM creds on BAILEY → tunnel move. All
   prerequisites documented in docs/disaster-recovery.md §5.
4. Standing: defect pass toward teeth (~30-flag audit), §14 cohort experiment,
   G1 ADAM retention policy, Greek Yogurt Pizza Dough unfiling, ingredient
   quantities.

## START HERE — state of play as of 2026-08-31 (start of day)

Read the three 2026-08-30 logs (bottom of file: morning, evening, late) —
it was a landmark day. **The commerce funnel is now demonstrable end-to-end
on Apple Cake with every hop clickable**: cohort signals (tube pan 151.7×)
→ proposed class chip with cited evidence → its curated collection
(?collection= deep-link) → catalog product records (?product= deep-link,
RealRank/offers attached). Only the render side (public ad block + /go/ +
EV bandit) remains unbuilt past the impressions/clickstream plumbing.

**Classes** (say "Classes", never "the registry") is now a first-class
surface: `product_classes` = 256 rows, all embedded, 220 with `signals`
(trigger phrases ON the class record — curator's design: 'Apple Peeler'
never embeds near 'granny smith apples', so term-to-term is the matchable
surface; implications like 'egg yolks'→Egg Separators encode once, match
free forever). Full ACDV editor at admin → Commerce → Classes: Add
(staff-supplied, embeds immediately), rename/MERGE (re-keys chips+products+
collections, approvals never downgraded), guarded delete. Gourmet curated:
vegetables purged, 39 spices/dried-herbs added with signals.

**NEXT BUILD (spec agreed, awaiting nothing):** the mechanical matcher
pre-pass in dish_class_propose — dish signals × class signal-phrases by
embedding distance, identity candidates seated deterministically BEFORE the
LLM (which keeps implications/served_with/tiering); calibrate the distance
bar on live pairs and show the table before fixing it. Then: Apple Peeler
loop test (run collection → re-propose Apple Cake → approve), the GREY LIST
(fresh herbs/fruits as classes — Granny Smith is T1-proposed, decide),
equipment dupe merges (Pie/Meatloaf/Loaf pairs).

Standing items: per-publisher cap at selection (the concentration report's
second edition made it overdue — NYT holds 115/200; the slot-reservation
seating is the machinery); Pistou rerun with a fr-line Reserve (per-line
`keep` + per-line OU relax shipped); the dish↔recipe M2M migration map;
the three twin merges (Pastitsio/Matzo/Strawberries — prior attempt was
auth-blocked, the session watchdog now makes that loud); orphan hygiene
report. Ops notes: restarts KILL in-flight jobs; propose ~30s, curated
collection ~5min, EN refresh ~2.5-7min, foreign now fast (it/fr phrase
packs); the jobs CLI --dish flag is refresh-only.

## Prior START HERE — state of play as of 2026-08-30

Read the 2026-08-30 log (bottom of file) first. The commerce pilot remains
complete end-to-end (see 08-29); this session hardened the DISH LAYER under
it: **matching now honors lexical qualifier evidence** (bone-in/boneless
families — derived from catalog names, guard in the refresh, all four chicken
cohorts clean), **dish delete RELEASES recipes to re-match instead of deleting
them** (refresh keeps delete-and-replace), **aliases are editable in the dish
editor** (and claim recipes as strongly as names), signals carry a **cohort
authority profile** (DA/PA lead, OU with the selection caveat, Cohort scores
block + 📊 Measure button), and **Coverage & Holes** (admin menu → Corpus) maps
catalog gaps from BOTH sides with one ➕ create flow — the weakest-link half is
the new dish_gap_report job. Five duplicate dish twins found by embedding
distance; curator merging (alla Nerano keeps the name, spaghetti spellings are
aliases). BIG NAMED GAP: dish↔recipe M2M was never migrated onto
collection_members (publishers only) — needs a migration map, identity
(dish_effective) versus membership kept separate. Admin menu now grouped
Corpus/Commerce/Jobs/System.

EVENING (same day, read that log too): per-line **slot reservation** (`keep`
on query rows — the language-tax fix) + per-line min-OU relax; the **info-dot
convention** (field explanations pop a card; everywhere from now on); the
**concentration report's second edition** says the top-200 is now an NYT/BBC
monoculture (115/200 NYT, first food blog at rank 145) → a **per-publisher
cap is overdue** and the reservation seating is its machinery; SEMrush inbox
removed; a **session watchdog** re-verifies the displayed user against
/auth/me (focus/interval) after the iPhone spent an evening as a phantom
user 5; cancelled/failed-early refreshes preserve the prior run's
count/fit/schedule. Twins (Pastitsio/Matzo/Strawberries) STILL pending —
the earlier attempt was silently auth-blocked.

## Prior START HERE — state of play as of 2026-08-28

Read the 2026-08-29 log + the four 2026-08-28 logs (bottom of file), then
the prior START HEREs below — the 2026-08-22 scoring-session framing and
standing items remain valid. **Dish-anchored product matching
(docs/dish-product-matching.md) is the standing build and the PILOT IS
COMPLETE end-to-end:** every recipe resolves a dish (dish_effective, source
'assigned'|'matched'|'nearest') → cohort signals corpus-wide (247/247,
sortable tables in the dish editor) → class registry canonical + snap
(38 seeded, calibrated) → pattern-aware Sonnet proposals (junction rows,
evidence-cited) → approve chips in the dish editor (instant-save, gated,
first 5 classes approved on Chocolate Cream Pie). The all-dishes proposal
sweep may still be finishing — check jobs. NEXT: curator approval passes +
provisional-class cleanup, then the RENDER side (block + impressions log +
/go/ + EV bandit — the metric and layer-5 funnel are designed in the doc).
Cart-builder parked as its own subsystem (docs/shopping-cart-subsystem.md).

**Where things stand:**

* **Coverage → creation is one click** — dish-coverage rows carry a ➕ that
  opens the dish editor prefilled (the 40/15/180 + "<name> Recipe" profile
  lives in dish_coverage's createHref; the editor reads generic params);
  curator is actively working the coverage backlog with it.
* **ONE save gate** (`input/pipeline/save_gate.py`) shared by the API and the
  batch pre-filter. No-cook/mix-only recipes pass: ≥1 step + ≥5 ingredients
  beats the prose floor (measured twice — 10 historical flips + 10/11 Cajun
  one-step blends, 0 junk). Reruns proved it: Coleslaw 15/15, Greek Salad
  20/20, Cajun Seasoning 10/10 clean.
* **Saves can't silently eat a URL-mate anymore** — `_url_conflict`
  ask/adopt/new; the form asks, batch adopts. Born from real data loss
  (three of four dressings from one multi-recipe page overwritten).
* **The partial-index trap is closed** — plain url_normalized indexes on both
  recipe tables (save dedup was a scan per save; /domains/{d}/top was 24.4s →
  0.22s). If a url_normalized query is ever slow again, check the plan FIRST.
* **Domains list surfaces the untouched backlog** — never_extracted derived
  LIVE (stored counters lie at zero for 69/392 rows): 60 domains, yellow dot
  + "no extracts yet first" sort. RESTART OWED for this endpoint change.
* **Postgres is PARKED by curator call** — docs/postgres-migration-inventory.md
  is the decision record (recipes.db only, big-bang + BAILEY rehearsal, NOT
  dual-write). Don't reopen until more system functionality lands. Step 0
  (one connection factory, `e78ae66`) is in.
* **Dish search lines are DONE end-to-end** ({q, n, gl, hl} rows; null =
  follow the dish, resolved at refresh; structured row editor with examples;
  harvest honors per-row count + Google locale). 224 dishes migrated.
* **Dish form audited + fixed** (12 findings; sortable score headers with the
  blend legend; `LibraryShell.hostOf` is now THE hostname derivation — 4 other
  forms still carry local copies, migrate as touched).
* **Unblocker spend leaks CLOSED and rerun-verified** (Quiche 26 paid → 3,
  all genuine blocks). The free direct probe always runs first; domain flags
  only choose HOW to escalate. Gressingham data reset. If unblocker use looks
  high again, check `[fetch-summary]` lines FIRST — the log now tells the
  whole story per run.

**Menu for next session (curator picks):**

* BAILEY cutover drill (still pending from 08-25).
* known_for → recipe-space embedding link ([[project_domain_known_for]]).
* Domains/dishes advanced search design ([[project_recipe_table_backed_lists]]).
* The three-stage grading layer (2026-08-22 framing below — still the
  standing big-ticket item; cook_validators' 13 soundness checks are the seed).
* Cookbooks end-user surface ([[project_cookbooks]]) if the curator wants a
  visible-product day instead.

The system runs: **MARLEY (production, RMA pending) + BAILEY (verified warm
standby) + ADAM (backup tiers) + Google Drive (offsite)**. Nightly 3 AM chain
unchanged. Corpus ≈ 6,900 master rows.

## Prior START HERE — state of play as of 2026-08-25

Read the 2026-08-24/25 log above, then the 2026-08-22 START HERE below (its
scoring-session framing and standing items remain valid). The system now runs:
**MARLEY (production, RMA pending) + BAILEY (verified warm standby, task
BCC-Drill) + ADAM (backup tiers) + Google Drive (offsite)**. Nightly 3 AM:
dump → verify-restore → ADAM copies → Drive sync → project mirrors → BAILEY
sync. Git is 31MB of code only. The recipe corpus ≈ 6,860 master rows.

## Prior START HERE — state of play as of 2026-08-22 (end of day)

**NEXT SESSION IS A SCORING ENHANCEMENT — the shape, stated by the curator 2026-08-22:**
*use what we have now to select the top 20/30/40, then apply a much more intelligent,
AI-assisted analysis to the SELECTED recipes and grade them on a different, not-so-SEO-
centric system.*

That is the two-stage gospel extended to **three**: harvest SELECTS on traffic -> OU RANKS
within that pool -> **NEW: an intelligent layer grades the survivors on non-SEO criteria.**
Four things bind it before any design starts:

* **This is NOT the cancelled AI-editor role, and the boundary must hold.**
  [[project_ai_editor_mediation]] cancelled the AI *selection* role on 2026-08-12 because
  demand data inverted its ranking (its #1 had 1,075 traffic against two floor-demotes at
  96,775 and 77,242). Its stated future was *"commentary on the algorithmic top-10, never
  re-ranking"* — and grading an already-selected set IS that future. The AI grades what the
  algorithm chose; it does not choose. Do not let stage 3 quietly become stage 1 again.
* **It narrows what §14 has to be right about.** If a second layer judges quality, the SEO
  grade's job shrinks to "produce a defensible candidate set". Cohort mismatch still decides
  WHICH rows reach stage 3, so it is not moot — but it stops being the last word on quality,
  which is the more forgiving requirement.
* **A non-SEO grading vocabulary already exists in the repo and is thrown away.**
  `cook_validators.py` has 13 validators grading instruction SOUNDNESS — unit consistency,
  mise completeness, appearance order, no-lookback, sub-steps, bundling. Cognitive-design
  checks, not SEO. But they run only on cook-reworked rows: **16 of 5,851 master (0.3%)**,
  and §13 Open already records *"the cook-rework validators already grade soundness and the
  result is discarded."* That is the nearest existing thing to what is being asked for.
* **It is the stated product pillar.** [[project_marketing_differentiation]] — cognitively-
  grounded instruction design — and the thesis line *"the algos already ran; here are the
  best, ranked."*

**Standing caution for the design:** [[project_two_stage_selection]] says never judge a
ranking without the selection stage in front of it, and the AI-editor cancellation is what
happens when a model's taste is scored against real demand. Any stage-3 grade needs a way
to be checked against something outside itself, or it is a guess wearing a number —
`docs/recipe-scoring-design.md` §13 closes with exactly that warning: *"This still ranks
provenance and demand, never the dish. An AI cannot know what tastes better; anything
claiming otherwise is laundering a guess."* Stage 3 is an attempt on that problem and
should be held to it.

**Read, in this order:
`docs/recipe-scoring-design.md` **§14 first** (the three-thresholds finding, measured
2026-08-22), then **§12 Disproved** (it exists because these error messages describe the
wrong cause — three claims have been re-made from scratch after being disproved), then
**§13 Open**. The memories that bind: [[project_two_stage_selection]] (GOSPEL — harvest
SELECTS on traffic, OU RANKS within that pool; never judge OU without the selection stage
in front), [[project_ou_power_blend]], [[project_traffic_exceptionalism]],
[[project_paid_pa_calibration]], [[feedback_verify_with_runtime_data]].

**The open question §14 leaves, and the cheap experiment that should precede any change:**
nobody has re-graded the same row against both its `explicit`/`embedding` cohort and its
`chapter-fallback` cohort and compared the grades. Both cohorts already exist, so it costs
a script, not a harvest. Tightening grading to 0.60 may just move 79% of rows onto
chapter-fallback, which could be more honest (a chapter is a real population) or less
informative (n is huge, so everything regresses to the mean). Measure before deciding.

**NO RESTART OWED.** Restarted 2026-08-22 16:43; everything through `140f371` is live.
Both new handlers verified registered by POSTing their `job_type` to `/scheduled-jobs/…`,
which REJECTS an unknown type — so a 200 is the proof, and it does not need a fresh route
to test against. (Careful: that endpoint upserts, so probing it overwrites `purpose`.
Send the real body or restore it after.)

**Three nightly jobs now, all 24h:** `chapter_rollups`, `dish_rematch`, `page_cache_purge`
(plus `screenshot_refresh`). **Check each has actually fired** — none has yet reached a
first scheduled run.

**Verify a restart by PID START TIME, never by hitting an endpoint that already existed:**

    Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Select ProcessId, CreationDate

(A brand-new route is the one honest exception: `/dish-coverage` answering 401 rather than
404 proved the new code was loaded.)

**`dish_match_max_distance` is 0.6** (was 0.8, changed 2026-08-22 on measured data — see
the band table in that day's log). It governs the save path and `/recipes/similar-master`.
It does NOT govern grading, which is the whole point of §14. Change it through
`POST /system-config`, never by SQL: the cache is a process-global that only `set_setting`
clears, so a direct write leaves the running server on the old value.

**`dish_rematch` runs nightly at 24h**, writing only rows whose verdict changed (verified:
a second consecutive run scans 3,223 and writes 0). It exists because the CATALOG moves,
not because recipes arrive — new recipes are matched at save. Check it actually fired.



**Branch `split/enrichment-api`, nine commits today `e2aa17f`..`e1393d1`.** The server was
restarted twice (10:13, 10:45) and is current through `739740c`. **A THIRD RESTART IS
OWED:** `e1393d1` registers the `dish_rematch` job handler, and its 24h schedule row is
already in `scheduled_jobs` waiting for it. Until that restart the schedule references a
handler the server does not know. Everything else from today is live. **Jobs always run
current code** (out of process via `Popen`) — only the server and the forms lag a restart.

**Verify a restart by PID START TIME, never by hitting an endpoint that already existed:**

    Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Select ProcessId, CreationDate

**`dish_match_max_distance` is now 0.6 (was 0.8).** Changed against measured data — see the
band table in today's log. It governs the live save path AND personal-recipe matching, not
just the sweep. Set through `POST /system-config` so `set_setting` invalidated the running
server's cache in-process; a direct SQL write would NOT have (the cache is a process-global
that only `set_setting` clears).

**THE ONE THING TO UNDERSTAND ABOUT DISH MATCHING.** `_match.dish` is a CLOSED set — it is
a KNN over `dishes_vec`, built from the `dishes` table, so it can only ever return a dish
that already exists (verified: 125 distinct values, 0 outside the catalog). The nearest
neighbour ALWAYS exists, so distance says how far, never whether it belongs — that is what
the threshold is for. `_identity.likelyDish` is the opposite: free LLM text, 2,940 distinct
values of which 2,814 have no dish record. That gap IS the coverage signal, and the two
disagreeing is a free accuracy check.

**`check_embeddings` is clean** — 0 failures / 0 warnings across 5,767 master, 445
personal, 178 dishes, 56 products; no orphans in either direction.

### Do first when you return

1. **Restart** (see above), then confirm `python -m jobs run dish_rematch` resolves.
2. **`Pie Crust`'s chapter auto-derived to `Breads`** — probably wants `Pies & Pastries`.
   One edit in the dish editor. (`Chicken Pot Pie` → `Sandwiches, Pizza & Savory Pastry`
   looks odd but is defensible.)
3. **Key Lime Pie is a real catalog gap** — `Legal Seafood's Key Lime Pie` sits unclaimed
   at 0.782 and had previously been graded against *Apple Pie*.
4. **77 of 108 write endpoints have NO permission check** (see today's log). Which surfaces
   members may touch is a decision, not a refactor — do not sweep it blind.
5. **`m.xiachufang.com/recipe/107744561` has still never completed a save** (outstanding
   since 2026-08-14). Re-capture with Chrome translate OFF.
6. **Parse ingredient quantities** — still the standing recommendation. 59,366 lines, zero
   parsed, ~$21 for the corpus.

### Dish coverage — what to build next (measured 2026-08-21)

Two signals, from the dish-match backfill. **Signal 1: recipes held that no dish claims**,
crossed against `dish_keywords` demand — Chili (7 recipes / 1.29M traffic), Cheesecake
(7 / 706k), Mashed Potatoes (12 / 413k), Chocolate Cake (10 / 350k), Hummus (6 / 325k),
Pizza Dough (10 / 316k). **The trap:** the dish with the MOST recipes held is Melomakarona
— 13 recipes, 3,618 traffic. Supply and demand rank near-inverted at the tail.

**Signal 2 — existing dishes absorbing recipes that are not theirs — was DONE today**
(10 dishes created, refreshed by the curator, each now holding a 20-recipe cohort).
Still open from it: **Vegetable Lasagna** (variant of Lasagna — an M2M question),
**Potato Latkes** (latkes ARE potato pancakes; the "mis-filing" may be correct),
**Galatopita** (91 traffic, 2 recipes — too thin).


### The product thesis (read this first — unchanged, still settled)

The membership club IS the product; the review is not.

    1  curated access   the algos already ran; here are the best, ranked
    2  capture          it becomes YOUR book        <- THE CONVERSION MOMENT
    3  your own stuff   selections, creations
    4  optimizations    cook view, voice, tips

**Free to browse, membership to KEEP.** Capture happens on our page BEFORE the click-out,
so sending traffic to the source is a step in the funnel, not the end of it.

**Do not gate the ranking** — gated pages earn 12-16 fewer PA points because they collect
no links, which is the tax the paywall calibrator exists to undo. The free layer is the
channel. **JSON-LD: `ItemList` + `Review`, NEVER `Recipe`** on a master.

### What shipped 2026-08-11/12

- **The candidate ledger** — `run_candidates` + `input/pipeline/candidate_ledger.py`.
  Every URL a run considered, dish AND publisher, captured AT the drop. `overturnable`
  encodes the fact/judgment boundary as data. LIVE and proven: 7 runs, 700 rows.
- **The AI editor, shadow** — `input/pipeline/ai_editor.py`, `run_mediations`,
  `ai_mediation` job. Verdicts only; `applied=0`; `apply=True` RAISES.
- **JSON-LD fast lane requires >=2 ingredients / >=2 steps** — fixed in
  `_has_required_fields`, so all five lanes inherit it. Verified live on Barefoot Contessa.
- **Page screenshot = the user's own browser capture.** `/stage-image` adopts the
  bookmarklet's html2canvas image (signed in, paywall-free) as the page screenshot,
  cropped by `crop_above_fold()` to the same 800x427 the headless path produced. The
  bookmarklet path now launches NO headless Chromium. Verified live on ATK.
- **Screenshot deferred** on the interactive path + `/screenshot-status` poll + save-time
  backstop. (Largely moot on the bookmarklet path now — the browser capture wins first.)
- **Bookmarklet rescue banner** when the pop-up cannot be driven.
- **`_sanitize_scoring` keeps a real 0** — a bottom-of-cohort percentile is a measurement.
- **Dish embedding moved into `create_dish`/`update_dish`**, not the HTTP endpoints.
- **SerpApi cancelled**; `detect_recipe_path` now asks the ACTIVE provider for its key.

### Do first

0. **`m.xiachufang.com/recipe/107744561` has still never completed a save.** Re-capture it
   with Chrome translate OFF — the cheapest way to exercise a large batch of multilingual
   fixes at once. Outstanding since 2026-08-14.
0b. **Parse ingredient quantities** — the recommended build. `recipeIngredient` is 59,366
   plain strings and `_identity.ingredientRoles` already tags each one, so the parser has a
   scaffold: add amount / unit / item alongside the role. Unblocks scaling, combined shopping
   lists, "what can I make from what I have", and any ratio question. Verify against the crab
   cake corpus, where "rank by crab-to-filler ratio" currently cannot be answered.
1. **The activity log** — `docs/recipe-activity-and-engagement.md` §7. One table keyed on
   `url_normalized` (NOT `recipe_id` — publisher/dish refreshes are delete-and-replace) and
   four chokepoint writes. It would have caught six of the nine bugs found on 2026-08-14,
   and it has no privacy surface. The engagement half waits for the production display page.
   **The AI editor's SELECTION role stays cancelled** (2026-08-12: demand data inverted its
   ranking). Its future is commentary on the algorithmic top-10, never re-ranking.
2. **`docs/dish-candidates-from-keywords.md`** — spec written, nothing built. A
   `dish_candidate_scan` job + candidates table + review surface. PROPOSE, NEVER CREATE.
3. **Add high-ratio keywords as QUERIES to existing dishes** — clearest is the singular
   `chocolate chip cookie recipe` (1.22M base) against the plural dish. But see the
   sourcing-vs-target rule under "Settled" before touching `dishes.queries`.
4. **Fresh Top-Pages exports** for `thekitchn.com`, `themediterraneandish.com`,
   `edibleboston.com` — they now fail loudly rather than harvesting on a stale signal.
5. **A public-voice commentary variant** — the card cannot use `scoreCommentary`; it names
   PA/DA/OU.

### Current numbers (measured 2026-08-14)

- master_recipes **5,166** · personal **428** · dishes **163** · domains **326**
  (was 4,921 / 423 / 159 / 322 on 08-12). The evening's net is small because the
  publisher refresh is DELETE-AND-REPLACE — job 843 wiped job 842's rows.
- candidate ledger **700 rows across 7 runs** · mediations **25** (Ramen only)
- cook-reworked: **16** rows with a non-empty `_cook` (0.3%)
- SEMrush API units **~26,100** — a ONE-TIME grant. Smallest purchasable package is
  2 MILLION units. Spend deliberately.
- scheduled jobs: `chapter_rollups`, `semrush_ranks_refresh`, `domain_scoring`,
  `screenshot_refresh`, `paid_pa_calibration`

### Settled — do not re-argue

- **`dishes.queries` is a SOURCING instrument, not an SEO target.** Judged by the quality
  of the candidate pool it surfaces, NOT by search volume. Ramen's queries were the
  curator's top-4 SEMrush keywords — the right selector for `target_keyword` and the wrong
  one for this field. Replacing them with head + component + variants took the union
  66 -> 106 and moved #1 from an instant-noodle toss to Serious Eats' tonkotsu.
- **Capture the page from the USER'S browser, not ours.** A server fetch is anonymous, so
  on a subscription site it photographs the paywall. The hero image already worked this
  way; the screenshot now does too.
- **Reproduce before attributing.** Three separate causes wore one symptom ("the
  bookmarklet is broken") on 2026-08-11, and the hang was attributed to the screenshot on
  circumstantial timing before anyone drove the real bookmarklet in a browser. The
  reproduction settled in one step what two rounds of log-reading had guessed wrong.


- **Harvest dishes, not formats.** Comparison ratio tracks a KNOWN FAILURE MODE.
  `air fryer recipes` 1.2%, `dinner ideas` 0.7%, `recipes` 0.4% — no single best CATEGORY.
- **Match keywords against `dishes.queries`, not titles.** Titles are labels. Title
  matching paired *Broccoli* with `broccoli cheddar soup` and missed a 1.22M-volume term.
- **Never put `best X recipe` in `dishes.queries`** — that SERP is roundups and listicles,
  which the harvest filter discards. Head term FINDS; comparison term is the page's SEO
  target (spec proposes a separate `target_keyword`).
- **Exceptionalism needs a varying baseline.** In a PUBLISHER harvest DA is constant, so
  OU and power both reduce to PA ordering (spearman 1.0000). OU does real work only in a
  DISH cohort. PA is a good within-site ranker (a 10-pt swing = 7.2x median traffic).
- **DA predicts inventory among recipe sites** (spearman +0.711) — the keep-formula
  premise holds. Earlier claims otherwise were contaminated by newspapers and -gr exports.
- **Skim depth: 6-12% of an export gets 90% of the best 10.** Current configs land 60-90%,
  worst on big sites (allrecipes 3.6% -> 60%).
- **Do NOT ship an OU floor for keep-sizing** until the PA remap covers URL MIGRATION as
  well as paywall — it would delete 98 of marthastewart's 100 for re-platforming, and gut
  ATK for being gated.
- **`display_filter` WORKS** on `domain_organic_unique` (and history is `url_rank_history`,
  not `url_ranks`+`display_date` — that 403 is a wrong type name, disproved THREE times
  now). Together these make replacing the manual export dance affordable: ~500k units for
  a full ~96-publisher refresh at a 100/mo floor.

### Known-open

- **A backup nobody restores is a hypothesis.** `recipes.sql.gz` has silently failed to
  restore TWICE — generated columns (2026-07-22), the fts5 index (2026-08-20, found
  08-22 after two days of unusable backups). Run `python backup_db.py --verify`, which
  replays the gz and compares row counts. Any NEW virtual table is excluded automatically
  now (`USING vec0` / `USING fts5` pattern), but a genuinely new failure MODE would not
  be — so verify, do not assume.
- **`recipes.sql.gz` is 66.7 MB** and GitHub warns on every push (hard limit 100 MB). It
  only grows. Git LFS, splitting the dump, or excluding more rebuildable tables — a
  decision that is coming, not urgent.
- **`page_cache.db` holds ~0.85 GB steady** after the nightly purge. Watch that the job
  actually fires; before 2026-08-22 nothing had ever deleted from it.

- **`system_config.get_setting()` IS the trigger for the import-resets-jobs landmine.**
  With no `db_path` it lazily imports `save_recipe_api` purely to read `DB_PATH`, and that
  import runs the app's startup, which resets in-flight jobs. This is the concrete way in
  that the long-standing "MUST BUILD a real guard" item below keeps getting tripped — pass
  `db_path` explicitly in any script. `jobs/__main__.py` calls it without one.
- **77 of 108 write endpoints have no permission check** (audited 2026-08-21): product /
  review / collection CRUD, `/chapters`, `/scheduled-jobs`, `/domains/rescore`,
  `/cook-kb`, `/ws-categories`, `/ingredient-synonyms`, `/images`. Some are correctly open
  (`/auth/login`, `/auth/signup`, `/stage-markdown` for the bookmarklet, `/recipes/{id}/claim`).
  `/domains/{d}/deep-enrich` was fixed the day it was found. The rest needs a DECISION about
  which surfaces members may touch, not a blind sweep.
- **370 confident matches sat in the 0.70-0.80 band before the threshold moved** — that band
  is now non-confident. It was a signal about CATALOG COVERAGE, not match quality: a thin
  178-dish catalog forces the KNN to return the least-bad neighbour. Revisit as the catalog
  grows.
- **Two non-dish rows were deleted from `dishes`** (2026-08-21): `Chefs: Legal Seafood` and
  `Julia Child Best Recipes`. They were collections wearing a dish record and acted as match
  ATTRACTORS. Their 10 recipes were preserved by clearing membership first — `DELETE
  /dishes/{name}` CASCADES to `kind='top'` master rows. If more collection-shaped dishes
  turn up, that is the typed-collections model not being fully applied.
- **`retire_master_membership` DELETES the row when no other typed block remains.** It is
  right for a refresh and catastrophic for "unfile this but keep the recipe". Read it before
  reusing it.

- **AUDIT EVERY USER-FACING STRING FOR VENDOR NAMES.** A member who bookmarklets a recipe is
  a customer of the product, not an operator of our pipeline — they must never be shown which
  data vendors we buy from, nor told to perform an operator action. Caught 2026-08-14: an
  unscored save surfaced *"Moz has not crawled this URL yet … retried every 3 days"*, and
  `/url-metadata` rendered *"Row exists; Moz scoring not yet run (set MOZ creds and run
  refresh script)"*. Both replaced; non-staff now get `GENERIC_UNSCORED_NOTE`
  ("Score not yet available for this page — it usually appears within a few days"), redacted
  at the **/recipes response boundary** on `auth_lib.is_staff`, failing CLOSED to the generic
  text. Client-side redaction is not enough — it still ships the string.
  **Audit as of 2026-08-14:** the two end-user pages (`recipe_form_styled.html`, `cook.html`)
  are clean in SHIPPED text; remaining hits are HTML/JS comments only. Admin surfaces
  (`domains.html` 39 SEMrush / 18 Moz, `dishes_v2.html`, `dish-keywords.html`,
  `product_collections.html`) are curator tools and are deliberately left alone. Re-run the
  grep before shipping any new user-facing message. See
  [[feedback_no_vendor_names_to_users]].
- **`skip_jsonld_fast_lane` is still dead code** — set in three places in `intake/translate.py`,
  read in none. `16f66fe` fixed the SYMPTOM (nulling `_src_rec`); the flag that was meant to
  express the rule is still unwired, so the next path that reaches for JSON-LD can repeat it.
- **The bookmarklet will happily capture our own app.** Fired on `/r/<uuid>` it produced a
  plausible-looking recipe — method list, screenshot, no ingredients — and cost a debugging
  round. It should refuse to run on our own origin.
- **html2canvas returns a BLANK capture on m.xiachufang.com** — 640×341, stddev 0.00, twice.
  The guard now refuses it and the deferred server capture fills in, so the outcome is
  correct, but the cause is unknown. Needs the browser console on a real capture; the
  bookmarklet already logs its own diagnostics.
- **24 blank screenshots are already in media.db** across 10 publishers (timoleondiamantis 8,
  southernliving 6, marthastewart 3, …) plus 22 in the warn band. `9a4df46` guards new writes
  only. Look at the list before latching them — the commonality is the useful part.
- **`scripts/backfill_coopt_images.py` needs the same remote-vs-ours logic** as `d8692e9`, or
  it will keep skipping rows whose `previewImage` is a remote URL that 403s in a browser.
- **The nightly `screenshot_refresh` fails the same 45 rows every run** — 45 attempted, 45
  failed, six consecutive nights (jobs 778→835), while `scanned` climbed 5,186→5,549. A
  fixed unrecoverable set that nothing latches, so the job pays for 45 doomed captures in
  perpetuity. LOOK AT THE LIST before latching it — the commonality is the useful part.
- **Two publishers were harvested on June exports** (mygreekdish 06-22,
  sallysbakingaddiction 06-28, both from `~/Downloads`). Their 80 rows were SELECTED on a
  two-month-stale traffic signal — see [[project_two_stage_selection]]. Extraction is sound;
  the pool is what's suspect. Re-run on fresh exports before reading either publisher's
  numbers.
- **26 master rows are still missing a real percentile.** `_sanitize_scoring` used to
  delete a bottom-of-cohort 0.0 as if it were unmeasured; the bug is fixed going forward
  but those rows lost the value. Recomputable from their cohorts — not done, because it
  touches data.
- **Why the bookmarklet pop-up was unusable is still unknown.** 2026-08-11's fix makes the
  case recoverable and VISIBLE (rescue banner) rather than silent. If the banner appears
  again, that confirms the pop-up is the failing part; the console lines to look for are
  `early hand-off failed` and `popup navigation failed`.
- **`recipes.sql.gz` is 53 MB and past GitHub's 50 MB warning threshold** (hard limit is
  100 MB, and the file only grows). Options when it matters: Git LFS, splitting the dump,
  or excluding more rebuildable tables. Also: use targeted `git add <paths>`, never
  `git add -A` — on 2026-08-12 that swept four 55 MB dumps (209 MB) into three code
  commits, collapsed to one before pushing.
- **The batch/publisher path still captures screenshots synchronously** (5-31s each). Fine
  for correctness — nobody waits on it, and cached rows need the screenshot — but it is
  several minutes of a 20-recipe dish refresh. Deferring needs care there for that reason.
- **Publisher harvests still screenshot anonymously**, so a paywalled publisher shows its
  paywall. There is no user session to borrow on that path; a real limitation of
  harvesting, not something the browser-capture fix covers.

- **[[project_traffic_exceptionalism]]** — TU is a real third axis (corr to OU +0.166) but
  blocked on supply: traffic exists only on publisher-harvested rows, and 71% of its
  variance is between-site (use site TU and site-centred TU separately).
- **No PWA `share_target`** — Android users cannot capture from the native share sheet.
  The bookmarklet is the whole capture story on mobile and iOS is its only handled case.
- **cook-rework covers 15 of 4,811 rows (0.3%)** — that is the gate on publishing method
  at all, since stored `recipeInstructions` are the publisher's verbatim prose.
- **MUST BUILD: a real guard against the import-resets-jobs landmine.** Importing
  `collections_lib` (or anything that pulls in `save_recipe_api`) runs the app's startup,
  which RESETS in-flight jobs. It killed job 784 mid-run, and was tripped again on
  2026-08-10 while testing the serp guard (harmless that time — nothing was running).
  Today this exists ONLY as a warning in this file, which is not a guard. Options: make
  the startup job-reset refuse to run when the importing process is not the service (a
  PID/entrypoint check), or gate the reset behind an explicit env flag the service sets.
  Until then it will keep being re-tripped by exactly the people who read this file.
- **Pagination — the lists read the whole table** (see 2026-08-10 measurements): `/recipes`
  has `limit`/`offset` but they DEFAULT TO 0 = unbounded, and the only GET caller does not
  pass them; `/dishes` and `/domains` have no limit/offset at all. Not a one-line fix —
  search/sort/chapter filtering all live CLIENT-side over the full cache, so adding LIMIT
  without moving filter+sort server-side would silently make search only search page 1.
- `harvest_source='backlinks_file'` should become `'semrush_export'`.
- `pageScreenshot` defaults to `""` in `recipe_model` ([[feedback_absent_not_zero]]).
- Blur must be baked server-side when the card is built; the card must store the
  `/screenshot/<id>` URL, never bytes, or publisher opt-out is unenforceable.
- The header-less INGREDIENTS fallback; `power_blend_weight` in `bcc_config.json`;
  provenance backfill (~4,150 NULL `mozHttpCode`); a scoring counterpart to
  `check_embeddings.py`; snapshot capture; `user_api_keys`.
  (The archive-47 rows are DONE — see 2026-08-13.)

- **DEFERRED — tab explosion / page stacking.** Every bookmarklet capture and every
  "new" spawns a tab, so they pile up. Design settled in discussion 2026-08-13, NOT built:
  * **One tab per PAGE TYPE is browser-native** — `window.open(url, 'bcc-recipe')` reuses
    the tab already holding that name. Per type (recipe / dish / domain / review), never
    per record, or the explosion just returns. A tab opened manually has no name and
    can't be adopted; that's the known gap.
  * **Do NOT build a prev/next stack.** Give records real URLs and let history BE the
    stack: `pushState` + `popstate` buys back/forward, the back gesture, reopen-closed-tab
    and session restore. A JS-held stack dies on refresh and can't be shared or bookmarked.
  * Cheapest shape needing zero server work: query params on the existing static pages
    (`domains.html?domain=…`, `dishes_v2.html?dish=…`), read from `location.search` on
    load. Pretty paths need a catch-all route and can come later. **The recipe form
    already does this** — it serves `/r/<uuid>` and rewrites the URL on Clear.
  * **The hazard is unsaved work.** Many tabs is messy but SAFE; reuse is destructive —
    capture #20 lands where #19 is still unsaved. Reuse must be gated on a real dirty
    check. That was the blocker on the recipe form and it is now cleared (2026-08-13:
    real snapshot-diff dirty flag). `dishes_v2.html` still has no guard at all.
  * Keep an escape hatch: honour ctrl/cmd-click for a genuine new tab — comparing two
    recipes side by side is sometimes the point.
  * Verify before building: how the bookmarklet's STAGING step behaves when it lands in
    a tab that already holds a staged capture, and whether the recipe form's
    localStorage store-context fights a URL-supplied record.
- **R9 is still open** (`docs/acquisition-logic-study.md`): MEASURE
  microdata/microformat/rdfa across a real sample of `no-struct` rejects before adding any
  of them. R7 and R8 are shipped, and between them they changed what that sample means —
  reject reasons now separate "we never got the page" from "the page has no recipe
  structure", and meta-tag JSON-LD no longer counts as missing. The measurement is finally
  worth taking.
- **Still open from R1-R6:** R5 only (acquisition rows in the ledger). R4 shipped as
  `domains.human_capture_only`; R6 shipped as `_render_retry_would_help`.
- **The unsaved-work guard is done for domains, NOT for `dishes_v2.html`** (identical
  structure, the fix lifts directly) or the recipe form (which needs a real dirty flag
  first — `saveBtn.disabled` there means "has content", not "is clean").
- **~4,150 rows still have NULL `mozHttpCode`** = scored before 2026-08-04 and therefore
  unverified. Now that `_row_to_dict` carries the code and the uncrawled case persists it,
  this backlog drains on natural re-score; a deliberate pass would cost Moz rows.
- **Milk Street partial capture is undecided** — store 3-of-10 ingredients marked gated,
  or refuse the row entirely?

### Process notes that cost time this week

- **Verify a restart by PID start time vs file mtime.** `bcc_restart.bat` self-elevates and
  silently no-ops when run non-interactively; polling an endpoint that already existed
  proves nothing. I reported a restart as done twice when it had not happened.
- **Read the Disproved table in `docs/recipe-scoring-design.md` §12 BEFORE probing an
  API.** It exists because these error messages describe the wrong cause.
- **Check populations are comparable before drawing a cross-site conclusion** — and
  sanity-read whether a number is plausible. bonappetit showing ZERO recipes above 1k/mo
  should have stopped an analysis; instead it was printed in a table and reasoned from.
- **Do not import `collections_lib` (or anything pulling in `save_recipe_api`) while a job
  runs** — the app's startup resets in-flight jobs. It killed job 784 mid-run.

## Session log — 2026-08-26 — the co.uk lie, unblocker becomes the default everywhere, publishers arrive researched, and the coverage list learns to read its own queries

Roughly twenty-five commits, `b20956e`..`dc81e87`. One root-cause hunt in the
morning grew into a systemic fetch-tier rework; the evening was publisher
onboarding at scale and a string of curator-spotted UI truths.

### The DA=52 mystery → root_domain was lying about ccTLDs

* deliciousmagazine.co.uk showed DA 52 in the form while the harvest measured 66.
  Every store was eliminated until the deep-enrich path confessed:
  `root_domain()` took the last two labels, so the host became **`co.uk`** and
  Moz was asked for the DA of the suffix itself — which is exactly 52.
  bbc.co.uk had been quietly poisoned the same way (stored 52, real 95).
* Fix `b20956e`: public-suffix-aware `root_domain()` (curated two-part-suffix
  set). Both rows re-stamped with real Moz values. 18 modules consume the
  function; all inherit the fix.
* Follow-on sweep: displayed DA vs calc DA across all 245 covered domains — only
  6 mismatches, all ±2-3 Moz drift. No other 52-class corruption.

### Unblocker: from per-domain opt-in to tier-1 fallback EVERYWHERE

The pesto run leaned on Wayback; 12tomatoes died at candidate #2; two giallo
runs launched cold because a stale form tab kept re-PATCHing `plain` over the
fresh opt-in (the pre-run PATCH inside doRefreshTop, not Save, was the writer).
Closed at every level, direct-first so credits only spend on failures:

* **Dish batches** (`6c57f05`): `_is_recipe_filter` gets `unblocker=True` via
  `dish_unblocker_fallback` (default on). Phase B salvage skips (with a log
  line) when the inline tier already tried this run's fetch-fails. Every filter
  pass now prints `[fetch-summary] direct=N unblocker=N wayback=N`.
* **Publisher enqueue** (`48cd81f`): visible "🛡 Unblocker fallback" checkbox in
  the run section, sent explicitly — what you see at click time is what runs.
  Server resolver: explicit payload > strategy opt-in > config default ON.
* **Extract phase** (`905d799`): the per-URL policy resolver defaults to the
  same fallback (`extract_unblocker_fallback`, on) — it had sent 38/40
  giallozafferano.com winners to Wayback while the filter fetched the same
  pages live.
* Proof: 12tomatoes rerun 139/139 fetched, 130 keeps, 35/35 extracted live.

### Publishers arrive researched: enrich rework + known_for

* ✨ cheap Enrich REMOVED (button+endpoint+JS) — deep enrich is the single path
  (`61be9d4`). Deep enrich now runs AUTOMATICALLY on domain create (server-side,
  best-effort, ~15s); button for re-runs.
* NEW `domains.known_for`: the ranking-keyword pills distilled into 3-6
  demand-ranked "best known for" phrases ("Slow-cooker and Crock-Pot recipe
  authority"), displayed as the headline over the chips (retitled "Demand
  evidence"). Planned: its own embedding in recipe space → recipe commentary
  can cite the publisher's identity (memory project_domain_known_for).
* Onboarded with full research: **food52** (DA 86 stamped), **marmiton.org**
  (fr), **giallozafferano.it/.com** (it, challenge-platform → unblocker), plus
  the 13 cohort-evidenced publishers (bettycrocker, greatbritishchefs, nigella,
  iambaker, howsweeteats, ...). myrecipes.com flagged harvestable=0 (passthru).
  Giallo runs: .it 39 stored/10 winners, .com 151 stored/40 winners; marmiton
  30/5 via the French pipeline.
* Quick-and-dirty DA refresh across all 375 domain rows via batched Moz V2
  (135 updated). DA≥60-no-extract flag list + not-in-table suggestions
  produced the 13 above.

### Paywall calibration coverage pass

* Recalibrated all flagged publishers from the corpus (milkstreet 47.3, ATK
  56.2, deliciousmagazine 33.1 new). NYT + bonappetit measured no_penalty —
  the guards working. 17 STRUCTURAL mixed-media flags added (brands,
  broadcasters, aggregators: mccormick, barilla, tastemade, today.com, ...);
  southernliving calibrated instantly at 20.1 (n=28). Rule recorded: a flag
  needs an identifiable external cause, never mere underperformance — dish
  runs match adjustments in REAL TIME (ATK selected #1 on Pesto proves it).

### Curator-spotted fixes, each with a lesson

* **KA images**: 11 of 50 legacy recipes carry DEAD image URLs in KA's own
  JSON-LD and og:image (404 at origin). SERP-image fallback repaired them;
  corpus-wide sweep fixed 303 of 321 missing previews (94%), three-tier:
  own-URL re-coopt (224) → og re-fetch → SERP images (33).
* **Dish coverage** (`929a03f`): "covered" was dish.name only. Now name +
  display_name + aliases + SEARCH PHRASES (folded, order-blind, dressing
  stripped) — "if we're searching for it, it's covered." 68 false gaps closed;
  subset-only overlap gets a "searched by" badge (Boston-Cream-Pie trap).
* **Site filter**: suggest panel anchored to the dialog, not the input
  (`78c4d7c`); domain filter was exact-host so giallozafferano.it found
  nothing on ricette.* (`4d34c18` — WHERE + count roll-up now subdomain-aware).
* **Identity card XML leak** (`49e947e`): ethnicity='French</ethnicity><param
  ...' surfaced as a markup facet option. Source guard strips '<'-onward;
  the Garlic Aioli row repaired (technique text recovered into its field).
* **Cook view** (`dc81e87`): a saved recipe without _cook now SAYS "not built
  yet — press Rework" instead of omitting the surface. Open: auto-rework on
  first interactive save (LLM cost per save — curator to decide).
* **Shell width**: all forms now 1040 via the one `--shell-w` token; container
  padding matched to header inset (the 4px lie); two-layer button shadows;
  the admin chip finally shadowed.
* **bcc_restart.bat**: stray "Rres" keystrokes broke the port-verify step —
  restart itself was never broken.

### Tomorrow

* **Explore migrating to Postgres.** Inventory the SQLite-specific surface
  first: sqlite-vec (4 vector tables), FTS5 (2), generated facet columns,
  WAL/busy_timeout patterns, json_extract everywhere, AFTER DELETE vec
  triggers, PRAGMA optimize, the backup/verify chain (dump format changes),
  BAILEY sync, and `_load_bcc_config`'s direct sqlite read. pgvector +
  tsvector are the natural landing spots; the jobs table's polling pattern
  gets LISTEN/NOTIFY options. Decide staged (dual-write?) vs big-bang restore.
* BAILEY cutover still pending; known_for embedding link; domains/dishes
  advanced search design.

## Session log — 2026-08-27 — search lines grow up, the form gets audited, and the unblocker stops billing us for pages we already had

### Postgres: inventoried, then parked

* Full SQLite-surface inventory written to **docs/postgres-migration-inventory.md**
  (four DB files not one; what DISSOLVES — WAL/busy_timeout, vec0 sidecars +
  triggers, the 5KB source_host CASE monster, FTS triggers, the disabled job
  runner — vs what's a REWRITE — ~30 modules of placeholders, 106 json_extract
  sites, the whole backup/DR chain). Recommendation on file: recipes.db only,
  big-bang ETL with BAILEY rehearsal, NOT dual-write. Curator call: **HOLD**
  until more system functionality lands. Nothing is on fire; it's an
  investment, not a rescue.

### Dish search lines: the stored-but-dead feature gets finished

* The 2026-08 query-ROWS design ({q, n, gl, hl}) was ~40% built: storage +
  validators existed, but the UI was a bare-strings textarea and the harvest
  ignored every per-row value (gl=us/hl=en hardcoded, one uniform top_n).
  Deferred-is-not-fixed, textbook case.
* **Null = follow the dish**, now for locale too (same contract n already had):
  gl/hl stored as null, resolved at REFRESH time from source_language by
  `resolve_query_locales`. Save-time stamping (apply_locale_defaults) is dead —
  it made an explicit us/en line on a Greek dish inexpressible, and the moules
  fr/fr stamp was found already WIPED in prod by a later textarea save.
  Migration: all 224 dishes → null-locale canonical rows (zero explicit
  locales existed to preserve).
* Harvest honors rows end-to-end: `_multi_query_lookup` runs each line at its
  own `n` (blank → dish default, renamed "Default results per line" on the
  form) and its own gl/hl; per-row n clamps to System→Limits; an explicit
  non-base row hl marks the batch foreign for the OU-floor relax. Dan Dan
  Noodles now queries CHINESE Google; moules queries French — both had been
  silently querying US.
* UI: structured row editor (query | results | language | country selects,
  country auto-derived, GL_FOR_LANG mirrored in JS — keep in sync) with
  worked EXAMPLES on the form, per curator: teach the syntax where it's used.
  Query box autosizes (wraps, never scrolls hidden); Enter = new line row.

### The dish form audit (curator asked for a full one)

* 12 findings, all fixed: refresh button re-arming mid-run (duplicate
  EventSource), dirty-tracker lighting Save from Editor's-Choice typing,
  boot hang on dead server (now retry link), /system-config re-render
  stomping typing (in-place max update), stream lifecycle (ONE tail,
  resync-not-duplicate, give-up after 5 errors), silent half-filled-row drop,
  create/edit validation parity, alert()→flash, human error messages,
  hostname derivation → **LibraryShell.hostOf** (was 6 copies in 2 variants;
  4 other forms still carry copies — migrate as touched), EC purple + row
  tints → single-source classes, /dishes list slimmed (no embedding_text,
  identity_card → boolean; 222 × multi-KB saved per load).
* Winners + cohort lists: **sortable stat headers** (score/ou%/pwr%) and the
  score finally DEFINED in place — "score = 70% OU pct + 30% power pct",
  weight live from the payload (power_blend_weight), tooltip explains the
  in-cohort percentile blend.

### Unblocker: two leaks were billing us for pages that fetch fine

* Curator flagged heavy unblocker use. Verified with live fetches, not
  code-reads: **gressinghamduck.co.uk 74/74 paid RENDER fetches** on a site
  whose static HTML carries complete recipe JSON-LD (200 in 0.7s); Quiche
  dish run paid 26× including jamieoliver — the exact false-positive the
  code itself documents.
* Leak 1 — flag-trusting render-first: render_required=1 (or the dish
  batch's _allow_render from fetch_strategy='unblocker') SKIPPED the free
  direct fetch entirely. Gressingham got both flags from the domains form's
  **"blocked" mode preset** — one click, and the curator had no way to know
  ("unclear when to check it"). Fix: the free direct probe now runs FIRST
  unconditionally; accepted iff unblocked AND structured (ld+json/<article>).
  True JS-shells pay the same as before +~1s; wrong flags now cost $0.
* Leak 2 — ambient-marker spend false positive: Cloudflare's PASSIVE
  'challenge-platform' script on full healthy pages triggered paid
  escalation. Fix: ambient markers spend only when the body ALSO lacks
  structure.
* Data: gressingham reset to plain/render=0. Form: strategy fields now say
  the truth — flags choose HOW to escalate, never whether to try direct.
* **Verified by rerun**: Quiche old `direct=85 unblocker=26` → new
  `direct=105 unblocker=3` (kitchenaid/southernfellow/hiteonricecouple —
  genuine blocks). ~88% of the paid filter spend was waste, now gone.
  Ops note: spawned jobs import fresh code (fixed immediately); only the
  server's live-extract path needed the restart.

### Open

* BAILEY cutover; known_for embedding link; domains/dishes advanced search;
  postgres decision parked (inventory doc is the decision record); 4 forms
  still carrying local hostname copies → LibraryShell.hostOf as touched.

## Session log — 2026-08-28 — coverage grows a create button, and the save gate learns that coleslaw has two steps

Three commits: `54b9fbb` (coverage ➕ deep-link), `2593055` (save-gate fix), plus this log.
Service restarted by curator 10:15; everything below is live and rerun-verified.

* **Dish coverage → prefilled dish creation (54b9fbb).** Every coverage row gets a
  **➕ dish** button → `dishes_v2.html?create=<name>`: the New Dish form lands prefilled
  with the name, TWO search lines (`<name>` and `<name> Recipe`), default results/line
  **40**, kept winners **15**, TTL **180d** — all editable before Create. Prefill is
  one-shot (Cancel → + gives a blank form); coverage page stays read-only (the button
  only deep-links; creation happens on the editor's Create). Verified in Chrome both
  ends. Curator drove it immediately: Coleslaw, Bruschetta, Buffalo Chicken Dip, Hummus
  runs same morning.
* **"Demand" column question answered by code-read:** coverage traffic = SUM of
  `dish_keywords.traffic` over rows whose keyword CONTAINS the name (substring), i.e.
  SEMrush est. monthly organic traffic to tracked pages, ~117 exported publishers only.
  Directional ordering, not search volume: substring over-catch, coverage bias,
  seasonality all documented at `save_recipe_api.py` `_kw_demand`.
* **The coleslaw "weird rejects" → a real save-gate class bias, measured then fixed.**
  Run 1087's Rejects held spendwithpennies' coleslaw — the run's **rank=1 candidate**
  (OU 12.39) — rejected `skip-thin: fewer than 3 instructions (2)`. Root cause: save gate
  needs ≥3 steps; the <3 fallback needs ≥150 chars of method prose. The publisher's OWN
  JSON-LD ships 2 steps / 9 ingredients / ~145 chars — **rejected by 5 characters**. Same
  run saved a DIFFERENT 2-step coleslaw whose steps were merely wordier: keep-vs-reject
  was verbosity. History (dish_rejects): 173/716 rejects are skip-thin; biggest bucket
  "fewer than 3 instructions (2)" ×86; top dishes = Cajun Seasoning ×17, Greek Salad,
  Green Salad, Hoisin, dressings — the gate was structurally biased against
  no-cook/mix-only dishes.
* **Measurement before the fix** (replayed the proposed rule against the EXACT cached
  extractions in `llm_extract_cache`, no re-fetch): 126 distinct skip-thin instruction
  rejects, 88 still cached, **10 flip** under `(≥2 real steps AND ≥5 real ingredients)`
  — all 10 genuine (Cajun seasoning ×4, horiatiki, coleslaw, ginger dressing, overnight
  oats), **0 junk admitted**. The guarded failure modes (paywall stub / 404 / wrong-node
  carousel) cannot show 5+ real ingredients; the ingredient floor still runs first.
* **Fix (2593055):** `RICH_INGREDIENT_MIN_INGS = 5`; in `_is_cacheable`'s thin-steps
  branch, ≥2 steps + ≥5 ingredients accepts BEFORE the prose floor. One-step rows still
  face the 150-char (CJK 40) floor unchanged. Verified by module import + 4 gate probes
  (coleslaw passes with the new reason string; 1-ing stub, 4-ing/2-step, long-paragraph
  all unchanged).
* **Rerun proof, post-restart:** Coleslaw (job 1089) saved 15/15, spendwithpennies KEPT
  (OU 19.07, PA 57/DA 66), **bottom_ou 4.66 → 5.50** (weakest keeper displaced), sole
  reject = saltsearsavor anti-bot fetch-fail (legit, Phase-A flagged). Bruschetta (1092)
  10/10, zero rejects. Spend clean: coleslaw direct=66/unblocker=3, bruschetta 28/2.

### Open

* saltsearsavor.com coleslaw (OU 19.4, "would've qualified") — bookmarklet recovery if
  the curator wants it.
* The 9 other historical skip-thin flips self-correct as their dishes' TTLs come due —
  or a targeted re-refresh of Cajun Seasoning / Greek Salad / Horiatiki whenever wanted.
* Standing menu unchanged: BAILEY cutover drill, known_for embedding link,
  domains/dishes advanced search, three-stage grading layer, cookbooks surface.

## Session log — 2026-08-28 (afternoon) — a URL that ate three dressings, one save gate instead of two, and a 24-second join

Commits `d04a97b`..`189a789`. Server restarted through `93397d4`; ONE RESTART
OWED (see end).

* **Recipe form gets its sidebar ＋; footer Clear retired (d04a97b, 93397d4).**
  Curator's call, and correct: Clear never cleared the saved row — it dropped
  identity and initialized a fresh form, which is "New". The routine is now
  `startNewRecord()` (sidebar ＋ + the post-delete teardown call it); the
  misleading footer button is gone.
* **DATA LOSS found and root-caused: one URL, four recipes, one survivor.**
  Curator entered four dressing recipes from ONE loirekitchen technique page;
  every save carried the same source URL, so the server's dedup-by-
  (url_normalized, user_id) ADOPTED the existing row each time and overwrote
  it — uvicorn_stdout.log shows each fresh UUID being discarded. Three
  dressings unrecoverable (extract cache had nothing); only the last save
  survives. Fix: `_url_conflict: "adopt" | "ask" | "new"` on the save payload
  ("adopt" default keeps batch/harvest semantics; the form sends "ask" → a
  would-be adoption 422s with the existing row's name → curator picks
  OVERWRITE or SAVE AS NEW). Keep-both rows store url_normalized='' (the
  established claimed-row sentinel; partial unique index ignores ''), page
  link survives on _source.originalUrl; a kept-both row re-saves through the
  same path automatically because the check reads the INVARIANT (stored
  url=='') not row existence.
* **Save gate consolidated to ONE function (f92c7ec, /simplify pass with 4
  review agents).** `input/pipeline/save_gate.py` now owns `is_cacheable`;
  save_recipe_api imports it and intake/process_batch's "mirror" (which had
  silently missed EVERY escape hatch — its lockstep claim was false) now
  delegates. Rich-ingredient rule widened to **>=1 step + >=5 ingredients**
  after the first post-fix Cajun run rejected 11 one-step spice blends —
  JSON-LD probes showed 10/11 are real 7-9-ingredient recipes. Client: 422
  envelope unwrapped once, postSave takes one extra-keys param, coverage ➕
  profile (40/15/180 + "<name> Recipe" line) moved into dish_coverage's
  createHref query params — dishes_v2 reads generic create/q/serp/final/ttl.
* **Rerun proofs:** Coleslaw 15/15 (spendwithpennies kept, OU 19.07,
  bottom_ou 4.66→5.50) · Bruschetta 10/10 · Greek Salad 20/20 (horiatiki
  kept) · Cajun Seasoning 9/10 + 11 skip-thins BEFORE the widening → **10/10,
  zero drops** after.
* **The partial-index trap, found twice, fixed at depth (f92c7ec, 189a789).**
  The tables' only url_normalized indexes were the PARTIAL unique ones
  (WHERE != ''), which SQLite uses only when the query restates that
  predicate. ~10 call sites didn't: the save dedup lookup was a full scan per
  save, and the publisher-ledger LEFT JOIN (get_collection_top) re-scanned
  master_recipes per ledger row — `/domains/{d}/top` measured **24.4s**
  (cooking.nytimes.com), the curator-visible "harvest panel pops in late with
  no warning". Plain url_normalized indexes on both tables fix every call
  site with zero query edits: **0.22s on the live server** (plan-time, no
  restart). Also in init_db. Async panels now paint a shared `.ed-loading`
  spinner at fetch start (editor-shell.css).
* **Domains list: never-extracted surfacing (b22bfd9).** The stored
  refresh-on-access recipe counters disagree with reality at the zero
  boundary for 69/392 domains, so `list_domains` now batch-counts LIVE per
  domain (GROUP BY generated source_host, both tables) and derives
  `never_extracted` = zero kept rows from any flow AND no SEMrush ingest
  stamped (a run that kept nothing still ran). 60 domains flag. Form:
  "no extracts yet first" sort + yellow `.ed-item-flag.pending` dot + meta
  bit. Shared `.ed-item-meta` now WRAPS instead of truncating (curator
  request — applies to every editor list).

### Open

* **RESTART OWED** for: `never_extracted`/live counts in /domains (b22bfd9)
  and the init_db index DDL (inert — the indexes already exist in the DB).
  Everything else is live.
* Re-enter the three lost dressings from the loirekitchen page (answer
  "save as new" on each) — the form now makes that safe.
* saltsearsavor coleslaw (OU 19.4) still recoverable via bookmarklet.
* Historical one-step skip-thin victims (therecipecritic, culinaryhill,
  barefeetinthekitchen, delish, chilipeppermadness cajun pages…) return on
  their dishes' next refresh; Cajun Seasoning itself is already clean.

## Session log — 2026-08-28 (evening) — the product flow demystified, and every recipe learns its dish

Commits `9c8b73d`..`6b4b9af` + this log. All restarts done — NOTHING owed;
everything below is live.

* **Domains sorts + dots refined (9c8b73d).** Grouped sorts (SEMrush due,
  no-extracts) order DA-desc inside groups — most popular site first, curator's
  call. `never_extracted` requires allowed=1: blocked reference domains
  (youtube, facebook — 9 rows) will never be extracted BY DESIGN and lost
  their to-do dots. Live count: 50 flagged (GBC + acouplecooks harvested off
  the list same day).
* **Product curation pages joined the shared component (2c6fa17).** Both
  collection sidebars were classless li's with an UNDEFINED .ed-sub — bare
  unstyled text. Now ed-item rows like every other editor, and curated rows
  deduplicate ("Fine Mesh Strainer · Fine Mesh Strainer · whole class · 3
  picks · 3 products" → "3 picks · 31d ago").
* **Product flow re-education (no code).** The curator's Vitamix mystery
  traced: curated-run class string IS the research prompt ("Recycling" class
  picked a trash can + compostable cutlery as "Food Recyclers"); the
  bookmarklet importer INVENTS class names ("Food Recyclers (5 l)" — third
  spelling of one concept); reruns research reviews, never the catalog, so an
  imported product is only adopted if the authorities' evidence picks it
  (then linked by ASIN/brand+title). Class/category ARE editable: Products →
  Edit. `product_classes`/`product_categories` tables EXIST (1 row each) —
  products.product_class free text never joins them; that's the fragmentation
  root and step 2's target.
* **THE BIG ONE — dish-anchored product matching, design + step 1 SHIPPED
  (1dd481a, aa03cb7, 6863692; docs/dish-product-matching.md;
  [[project_dish_product_matching]]).** Equipment is too coarse to match
  products (52.9k items, 4,180 names, 5% sized; Tool = name+size only, no
  material/coating) — so matching anchors on the DISH. Resolution ladder:
  _master.dish (curated) → _match.dish (confident) → _match.candidates[0]
  (NEAREST, no gate — already stored by every sweep). Exposed as
  `dish_effective` + `dish_effective_source` GENERATED columns on BOTH
  recipe tables, indexed. **8,376/8,376 recipes resolve a dish** (master:
  3,258 curated · 935 matched · 3,731 nearest; user: 218/234). The 0.6
  membership threshold untouched. rematch sweeps BOTH tables now (nightly
  handler + CLI); user rows skip the vec upsert (matched AGAINST dishes_vec,
  never KNN targets). The one embedding-less row (Granola, user 5 — save's
  best-effort embed had failed) backfilled + matched at 0.307. Form's
  scoring-strip dish chip now says the nearest dish muted ("nearest (loose) ·
  distance 0.85 · then …") instead of "no confident match". Next steps in
  the doc: canonical class registry + embed-snap imports, dish→classes
  junction seeded from cohort equipment (curator signs the money join —
  fuzzy proposes, curator disposes).
* **Ladder proven live twice.** Pumpkin Pie already existed (08-21) — sweep
  correctly moved nothing. Chocolate Cream Pie CREATED (2×60=120 fetch, keep
  20, 180d) → rematch minutes later moved 3 rows OUT of Cream Pie into it
  (the generic-bucket migration, exactly as designed) + 40 nearest inherits.
  No refresh run yet — zero curated winners until the curator fires one.
* **Fetch-total hint (6b4b9af).** "Default results per line" × 3 lines = 360
  nearly bit twice; a live hint under the field now does the arithmetic
  ("3 lines × 40 ≈ 120 candidates fetched per run", per-line overrides
  summed). Pumpkin Pie reconfigured 40/line×3=120, keep 20.

### Open

* Chocolate Cream Pie refresh (120 fetch/keep 20) when the curator wants its
  own top-20.
* Dish→product next steps: class registry (step 2), junction + proposals
  (step 3) — open curator calls listed in docs/dish-product-matching.md.
* saltsearsavor coleslaw bookmarklet recovery; remaining lost dressings.
* Optional: nightly re-embed pass for embedding-less rows (self-heal the
  Granola failure mode; check_embeddings is the current backstop).

## Session log — 2026-08-28 (late) — the design closes the loop: yolks to egg separators, signals to money

Commits `0e5c609`..`4a170d2` + this log. ONE restart owed (the /materialize
endpoint — everything else live; today's materialization already ran
in-process).

* **Multi-dish membership: ALREADY BUILT, proved live (0e5c609).** The
  curator remembered right and the agent reasoned wrong: the 2026-06-14
  ledger fix made `dish_run_data_points` the membership junction — top lists
  derive from per-dish selected+rank, 79 URLs are winners under 2+ dishes.
  First Chocolate Cream Pie refresh (20/20) selected a recipe that REMAINS
  selected in Cream Pie: top-20 of parent AND child. Pass-by-nearest-dish
  alternative rejected on record (leftover-bucket parents, lists that rewrite
  when a sibling is born, paid runs gated on fuzzy distances). Residuals
  noted: _master.dish = last-claiming-batch label only; grading may want the
  most-specific cohort.
* **Design rounds into docs/dish-product-matching.md (3911e06, c24902a,
  bb2f8c6):** render chain (most-specific membership dish → nearest dish
  with an approved set → chapter/category floor); a specific dish
  differentiates by INGREDIENT classes ("the product associated with
  CHOCOLATE") and those lead the block; class ordering = relevance GATES,
  money SORTS (price×commission cold-start → measured EPC from the planned
  /go/ clickstream), curator override wins; THREE product families —
  equipment / gourmet / TRAVEL — each seeded from its own corpus signal
  (equipment list / recipeIngredient / provenance).
* **Pilot step 1 SHIPPED: dishes.cohort_signals (a02bb39, 4e225db).**
  input/pipeline/dish_signals.py — per-dish term document-frequency + LIFT
  vs corpus, per family, example lines carrying form detail; ranked
  df×ln(lift) (pure lift headlined df=3 fragments at 756x over chocolate's
  65%@13x). `dish_signals` job (one dish or sweep; free). Dish editor grew a
  lazy "Cohort signals" accordion (GET /dishes/{name}/signals; lift≥10x
  bold, <3x muted). Chocolate Cream Pie stamped: chocolate 65%/13x, oreo
  67x, ghirardelli 54x, vanilla-extract 78%-of-vanilla-lines, 9-inch pie
  dish. Qualifier mining on raw lines recovers FORM (bar vs chips vs cocoa;
  extract vs pod) — the precision commerce needs.
* **The egg-separator proof: signal → sellable, end to end.** "Egg yolks
  44%/14x" isn't a product — it implies a TOOL. Ran the Amazon path live:
  created "Egg Separators" collection (auto-taxonomy hit Breakfast & Egg
  Tools), refresh screened 48→10 (1 credit), curator medaled gold=The
  Original Egg Tool ($14.95, rr 90.1 — the premium one leads, as predicted),
  silver=CAMKYDE, bronze=Chef Craft. OXO screened OUT at 4.4★/5,122 —
  Wilson working.
* **The Amazon path's missing half BUILT (4a170d2).** No medaled candidate
  had EVER materialized into products (Dutch Ovens' golds included).
  `materialize_medals`: find-or-create by ASIN then brand+title,
  medal→bcc_pick only when blank, placement via set_curation, product_id
  stamped back; POST /materialize + "⬇ Medalists → catalog" button +
  "in catalog" chip. Run live: 3 egg separators CREATED; Dutch Ovens' 2
  golds MERGED into their existing curated-path rows by ASIN — the two
  selection techniques now converge on one catalog record.

### Open

* RESTART for POST /product-collections/{name}/materialize (UI button 404s
  until then; data already materialized).
* Products editor: approve the 3 egg-separator placements; optional W-S
  premium separator via bookmarklet into the class.
* Pilot step 2 next: product_classes registry (family column + embeddings +
  embed-snap imports), then junction + proposals (steps 3-4), approve chips
  in the dish editor (step 5). All specced in docs/dish-product-matching.md.
* dish_signals sweep for all dishes when wanted (free, minutes).

## Session log — 2026-08-29 — the commerce pilot completes: signals → registry → proposals → approve chips

Commits `53bbf3d`..`d0459f4` + this log. All restarts done; the all-dishes
PROPOSAL SWEEP is IN FLIGHT as this is written (~247 Sonnet calls, per-dish
try/except; occasional JSONDecodeError failures expected and logged — retry
candidates for a second pass).

* **Cobb Salad match forensics.** All 15 kept recipes resolve Cobb Salad
  (14 assigned + 1 that lost its _master.dish to the known interactive
  re-save re-grade — resolution survives via the matched rung). "Greek
  Salad" appears ONLY as a runner-up candidate, never a verdict.
* **Terminology + display made honest (44da08b).** The single interpretive
  "Matched dish" chip became THREE RAW chips — Dish (_master.dish) · Match
  (_match.dish + distance) · Nearest (candidates[0] + distance), no
  nomenclature. And 'curated' → **'assigned'** in dish_effective_source
  (the run assigned it; no human judged) — generated columns rebuilt live.
* **Log honesty (53bbf3d, bbec7a3):** rematch logs BOTH match fields
  before→after (nearest-only flips were invisible while re-aiming gear
  inheritance); dish_signals logs FIRST STAMP / delta / unchanged.
* **Signals swept corpus-wide:** 247/247 dishes carry cohort_signals
  (blue cheese 155x on Cobb, tahini 121x on Hummus). Dish-editor tables:
  fixed 1-decimal columns + sortable headers (4cf2ef3).
* **Commerce design finished in discussion** (all in
  docs/dish-product-matching.md): THREE matching patterns
  (identity/implication/passthrough) × FOUR derivation routes (contains/
  does/from/SERVED-WITH — the pairing route: wine for every dish, geo-gated
  Total-Wine pickup, editorial degrade); families extend to books +
  alcohol; display metric = **EV = P(click|shown,dish) × P(purchase|click)
  × payout**, Thompson-sampled descending (relevance = the cold-start
  prior INSIDE contextual P); layer 5 = measurement funnel
  (shown→clicked→purchased→paid) feeding EV, curation triage, class
  pruning. Instacart cart-builder OUT-SCOPED to its own subsystem
  (docs/shopping-cart-subsystem.md): recipe-grain checklist, staples
  default-unchecked (corpus DF = the staples detector), push the SELECTED
  subset.
* **Pilot steps 2-4 BUILT:**
  - Step 2 (4cf4059): product_classes = canonical registry — 38 in-use
    names seeded + embedded, family column, `snap()` calibrated (true
    matches ≤0.50, new classes ≥0.75, bar 0.60).
  - Step 3 (c9df8f4): dish_product_classes junction + pattern-aware Sonnet
    proposal job. CCP pilot: 11 proposals — Baking Chocolate/Oreo T1 as
    hand-predicted, Tawny Port via served_with, and **Egg Separators
    arrived as an implication and SNAPPED to the registry class the Amazon
    run created the day before — the loop closed by itself.**
  - Step 4 (0433e4f, 2bec086): "Product classes" approve-chips accordion —
    evidence cards (cited signals w/ measured pct/lift), tier selector,
    approve/reject/revoke/restore surviving re-proposal, edit_master-gated.
    Chip decisions save INSTANTLY and now SAY so (the quiet Save button
    read as "didn't take"; the curator's 5 CCP approvals had all landed).
* **First real approvals:** Chocolate Cream Pie — Baking Chocolate (T1),
  Cocoa Powder, Egg Separators, Stand Mixers, Tawny Port approved.
* **Noted for later ([[project_dish_story_ethnicity]]):** dish records get
  full story+ethnicity like recipes; recipes inherit unless
  overridden/appended.

### Open

* Proposal sweep finishing in background — then: retry failures, curator
  approval passes dish by dish, provisional-class cleanup in the registry.
* Render side when wanted: the block, impressions log (day-one mandatory),
  /go/ rail, bandit. Cart subsystem parked in its own doc.
* Dish story/ethnicity build; the W-S egg separator bookmarklet import.

## Session log — 2026-08-30 — the dish layer grows judgment: qualifier evidence, mercy on delete, and a map of its own holes

Commits `65a315b`..`70706d5` + this log. (The unlogged 2026-08-29-evening
commits — `/go/` rail + impressions log, affiliates ACDV editor, proposal-
parser quote repair — belong to the prior session's render-side start.)

* **SEMrush round, curator-driven from the iPad.** Five exports (thecountrycook,
  barefeetinthekitchen, tastemade/recipes, jamesbeard/recipes, tastingtable)
  downloaded AND harvested remotely via the tunnel before the session started;
  archives committed (`65a315b`). tastingtable note: 125/198 candidates had NO
  Moz data (not API errors) — ledgered as reconsiderable. ~200 new master rows.
* **Signals get a per-dish button + an authority profile.** 📊 Measure in the
  dish editor (`0d6f07e`, POST /dishes/{name}/signals/run, entity-locked). The
  stamp now carries `authority` — DA/PA/OU mean/median/n from the generated
  columns, one free fetch (`4fee12c`) — surfaced as a Cohort scores block
  (`85400db`). The reading that sold it: Chili avg DA 71.4 (big-site dish) vs
  Lok Lak 53.2 (long-tail blogs); ~40-point spread corpus-wide. OU displayed
  LAST with the two-stage caveat: cohorts are selected from different pools,
  so cross-dish OU deltas mostly echo the DA/PA mix (Lahanodolmades DA 35 has
  HIGHER avg OU than Italian Braised Chicken DA 76 — exceptionalism, not size).
* **Bone-in/boneless FIXED, the general way (`31f34dd`).** Curator caught
  'Baked Bone In Chicken Breast' stamped Boneless. Measured: 21 contradictions
  across the breast/thigh sibling pairs; embeddings CANNOT split them (0.8794
  vs 0.8796 — the attribute is a token, not a vector). Machinery: qualifier
  families derived from catalog NAMES ('Base - Qualifier', ≥2 sharing a base);
  build_match(text=title+ingredients) reorders siblings when exactly ONE is
  evidenced (hedged text does nothing, name-exact still wins); the dish
  refresh grew a QUALIFIER GUARD that drops (ledgers) contradicting
  candidates instead of blind-stamping. All four chicken cohorts re-refreshed:
  0 contradictions, guard fired 8 live drops.
* **Five duplicate dish twins found by dish↔dish embedding distance**
  (alla Nerano 0.010, Minestrone 0.026, Pastitsio 0.030, Matzo 0.071,
  Strawberries 0.184) — each splitting one cohort in half. Curator merging
  manually. Nerano call: keep `alla Nerano` (curator's alla/alle convention +
  demand: 'pasta alla nerano' 26,250 traffic vs ~2,400 for the spaghetti
  phrase) — the spaghetti spellings became aliases.
* **Dish delete stops destroying corpus (`26dd4fa`, `ad62f97`).** The fixed
  warning lied in both directions; now a preflight names the three fates. And
  the policy flipped, curator's call: stamped dish-only rows are RELEASED
  (whole _master block stripped, row kept, next rematch re-homes to nearest
  surviving dish) — never deleted. Verified by rollback simulation (Matzo: 16
  released, 0 deleted). The dish REFRESH keeps delete-and-replace. Threshold
  AUTO-delete rejected (distances move with the catalog) → hygiene report
  parked instead.
* **Discovery, the 'good lord' moment: dish↔recipe M2M was never migrated.**
  `collection_members` exists and even says `-- 'publisher' (later 'dish',…)`
  — publishers moved onto it (22k rows), dishes still ride the single
  `_master.dish` stamp. The vegan-AND-Greek junction remains design. Now a
  named big-ticket item; needs a migration map (identity vs membership must
  stay separate or cohort signals pollute).
* **dish_gap_report — the catalog maps its own holes (`6f0435d`, `5db6aa0`).**
  Un-anchored rows beyond the confidence bar, clustered by identity-card
  likelyDish, EXCLUDING clusters that already name a dish/alias. First run:
  3,830 weak links; Sourdough Bread 24 (curator created + refreshed it within
  the hour), Mashed Potatoes 16, Melomakarona 13, Roast Turkey 12, deep Greek
  tail (Tsoureki, Loukoumades, Souvlaki…). Rendered as a 'Catalog holes'
  section on the coverage page with the SAME ➕ create chip; browser-verified
  live — which caught the endpoint answering signed-out → same admin_ui gate
  as /dish-coverage (`70706d5`, restart owed). Watch for the alias-pattern
  rows ('Roasted Potatoes' leaning on Roast Potatoes = missing ALIAS, not dish).
* **Aliases become editable (`1a77cb9`).** They were read-side-complete (an
  alias claims recipes as strongly as the name, via name_index) with NO write
  path. Now: editor field + PATCH + read-back; applied live to alla Nerano.
* **Admin menu grouped (`023fb20`):** Corpus / Commerce / Jobs / System
  headers; slash prefixes dropped where the header carries them;
  Dishes/Coverage → 'Coverage & Holes'; System → 'Settings'; shell asset
  versions bumped on all 25 pages.

### Open

* Curator's remaining twin merges (Pastitsio, Matzo, Strawberries pairs) —
  released rows re-home on the next rematch; run one after.
* Holes backlog: create dishes from Coverage & Holes (Mashed Potatoes next);
  alias-pattern rows get aliases, not dishes.
* THE M2M migration map (dish membership onto collection_members; identity
  stays dish_effective) — design doc before any code.
* Hygiene report: orphans with no dish within ~0.75 → curator review surface.
* Restart owed: gap-report admin_ui gate. Render side continues per 08-29.

## Session log — 2026-08-30 (evening) — the language tax gets seats, the corpus meets its landlords, and every page learns to doubt its own login

Commits `a8d4bf4`..`ed341ea` + this log. The curator drove dish creation off
the holes list all evening (Sourdough Bread, Apple Cake, Chicken Salad,
alla colatura proposals) while the tooling grew around them.

* **Per-line slot reservation SHIPPED (`1edb1b2`)** — the language-tax fix.
  Pistou measured it: the fr/fr line's marmiton (DA 83) lost every winner
  seat to anglophone pages on PA alone (link-graph geography, not merit).
  Query rows carry `keep` = reserved winner seats for that line's own
  candidates (floor not cap; unfillable reserves return to the pool via a
  top-up pass; seats consume on SAVE success so failures backfill in-line;
  no reserves = exact prior behavior, simulation-verified). Reserve column
  in the row editor; form blocks reserves > Winners.
* **Per-LINE min-OU relax (`f23821b`).** The foreign-locale floor relax was
  all-or-nothing per run — one French line switched the negative-OU roundup
  protection off for the English candidates too. Mixed batches (foreign
  verdict from row locales) now relax per candidate; dish-declared or
  query-inferred foreign stays run-wide.
* **ⓘ info-dot convention (`1edb1b2`, sweep `584673e`)** — curator's call:
  field explanations behind a small dot popping a centered card, never
  inline prose walls; EVERYWHERE from now on. Shared component in
  library-shell (delegated listener + LibraryShell.showInfo). Sweep fixed
  real staleness — dishes winners and chapters top-10 both still said "by
  OU" (blend since weeks), Sources-in predated row locales AND the relax
  scope — and migrated dishes/domains/coverage explainers (the coverage
  paragraph walls were what buried the holes table; also `a8d4bf4` capped
  both tables at 62vh, `955904e` put holes rows on the coverage layout).
* **Corpus-concentration report, SECOND EDITION (`955aa1e`,
  docs/reports/corpus-concentration.html).** Same method, 2.5x corpus:
  top-200 publishers 34 → **13**, HHI 667 → **3,753**, top-3 share 34% →
  **84%**. The publisher harvests seated the DA-95 giants in a block —
  NYT holds 115/200, ranks 1–77 are exclusively NYT+BBC, the first food
  blog appears at 145; Serious Eats/Martha/BBC-Good-Food (100+ rows each)
  place ZERO. July's webmd 1-for-1 persists. 'Cap the publisher' is now
  overdue — and the slot-reservation seating is the machinery a per-
  publisher cap needs.
* **SEMrush inbox REMOVED (`603ae4f`)** — never used; the harvest reads
  exports directly. Kept: semrush_inbox_dir (search dir), upload-export,
  worklist. Also `e4df3f0`: a stale form's blank save can't clobber
  semrush_report_url (splendidtable lost its seeded link minutes after the
  21-domain backfill; same guard family as the paywall-discount blank).
* **Session watchdog (`eadd53d`)** — identity was fetched once and believed
  forever; the iPhone showed its persisted picker ('user 5') over an
  anonymous session all evening. The shell re-validates /auth/me on focus/
  visibility/5-min interval/post-boot; lapse → red banner + badge/nav
  re-render; pages get `bcc:identity-changed` (recipe form re-resolves
  Enrich). The trail that led here (`7e771d2`): a master grab on the phone
  had NO Enrich — boot hid it (stale picker + anonymous), then the harvest
  lock's early return froze it hidden; visibility now applies pre-lock.
* **Run-record honesty (`ed341ea`, `b646d36`).** A cancelled Apple Cake
  refresh (0/65) stamped count=0 over 15 standing winners AND bumped the
  schedule a full TTL. record_run_result gains preserve_outcome/
  preserve_schedule (cancel keeps both, pre-save error keeps outcome);
  Apple Cake restored from job #1209's stored result. And ✨ Propose says
  "model reply unusable — run again" instead of a silent empty panel
  (alla colatura #1213 failed inside a 'success' job; rerun gave 12).
* Misc: dishes-list sort always visible (`29ca5e3`); Reserve spinner edits
  arm Save (`7e406a0`); gap-report gated admin_ui (`70706d5`).

### Open

* Rerun Pistou with a Reserve on the fr line — first live use of the seats.
* **Per-publisher cap at selection** — promoted by the report from "worth
  doing" to overdue; design = the reservation seating keyed on host.
* Twins still pending: Pastitsio / Matzo / Strawberries pairs (delete →
  alias → rematch); the earlier attempt was silently blocked by the
  expired session the watchdog now catches.
* M2M migration map; orphan hygiene report (~0.75+); render side per 08-29.

## Session log — 2026-08-30/31 (late) — the commerce chain closes every link, and the Classes table gets a throne room

Commits `9fa18a8`..`746305a` + DB curation + this log. Apple Cake served as
the end-to-end test case all night, curator-driven.

* **Phrase packs it+fr (`9fa18a8`).** The 63-minute alla colatura refresh
  decomposed: Italian had NO phrase pack → ~85s full-page translation per
  candidate ×43, THEN a restart killed it at 43/50 ("owning process is
  gone" — restarts kill in-flight jobs; let long runs land first). One
  offline call each: it.json (133 phrases), fr.json (152). Italian/French
  refreshes now filter at English speed. Runtime cheat-sheet: propose ~30s,
  curated collection ~5min, EN dish refresh 2.5-7min.
* **The chain became clickable end-to-end.** Products page honors the
  ?product= deep-link the picks always sent (`4be68fe`, UUID not row id);
  curated runs REGISTER their class at completion + chips carry the supply
  side — 'collection: Tube Pan ↗ · 3 products' with a ?collection= deep-link
  (`a9403ba`). Root cause of the Tube Pan mystery: the collection created
  products under a class the class list never learned, so the proposer
  (told to reuse known names) took Springform (26×) over tube pan (151.7×).
  Re-proposed with the class known: Tube Pan seated, T2, evidence cited.
  Apple Cake now demonstrates signals→class→collection→products with every
  hop a link.
* **jobs CLI --dish hijack (`92a6fe2`) — disclosure:** 'run dish_class_propose
  --dish X' silently launched a PAID dish_refresh (the sugar branch ignored
  the type). Two unintended Apple Cake refreshes were mine. --dish now
  refresh-only; other types get params.dish_name.
* **Classes editor SHIPPED (`145eec2`) — vocabulary per curator: "Classes",
  never "the registry".** Full ACDV on the affiliates template: search +
  family chips, Used-by panel (collection/product/dish links), staff Add
  (embedded immediately), family/category edit, RENAME/MERGE re-keying
  chips+products+collections transactionally (approvals never downgraded),
  guarded delete (409 with reasons; fixed a row_factory 500). Admin menu →
  Commerce → Classes.
* **Class `signals` (`761c94d`) — curator's design insight:** 'Apple Peeler'
  the NAME embeds nowhere near 'granny smith apples' the phrase, so the
  matchable surface lives ON the class record: a signals list (trigger
  phrases), edited in the Classes editor, matched term-to-term. Encoding an
  implication once ('egg yolks' on Egg Separators) = a free mechanical
  match forever. SEEDED from junction evidence: 192 classes. The mechanical
  pre-pass (distance candidates before the LLM, spec agreed) is the
  NEXT BUILD — calibrate the distance bar on live pairs first.
* **ⓘ redesigned (`746305a`):** standard Material outlined-i via CSS mask,
  injected from library-shell.js because the styles lived in a css file the
  EDITOR pages never load (the real reason it looked ugly); spacing +
  middle alignment fixed; comma-delimited signal chips; deeper textarea.
* **Gourmet curation (DB, via the new editor's endpoints):** 11 fresh
  vegetables deleted (Asparagus…Zucchini — not shoppable ad categories),
  Colossal→Jumbo Shrimp merged, Pasta Pots + Rice Cooker re-familied to
  equipment, and **39 spice/dried-herb classes created with signals**
  (Black Peppercorns…Za'atar; Dried Oregano…Dried Mint) — curator: every
  spice and dried herb is a class. Now 256 classes / 160 gourmet, all
  embedded, 220 with signals.

### Open

* GREY LIST for curator: fresh herbs/fruits as classes (Fresh Basil/
  Cilantro/Ginger/Lime/Parsley/Sage, berries, Ripe Bananas, Roma Tomatoes,
  Granny Smith Apples) — same fresh-produce logic as the deleted
  vegetables, but Granny Smith is T1-proposed on Apple Cake. Decide, then
  the same delete/merge pass. Equipment dupes too (Pie Pans pair, Meatloaf
  pair, Loaf pair).
* Mechanical matcher pre-pass (spec agreed; signals surface now exists).
* Apple Peeler test: run the collection → re-propose Apple Cake → approve.
* Rerun Pistou with fr-line Reserve; per-publisher cap; M2M map; twins.

## Session log — 2026-08-31 — signals learn subsumption, deletes get a doctrine, and the class registry is repossessed

Commits: this one. NOTHING owed — both restarts happened in-session; jobs run
out-of-process, so every pipeline change was live on landing. The v3 signals
SWEEP was in flight (237/283, clean) as this was written.

* **Signals mining v2/v3 (`dish_signals.py`, method df-lift-v3).** Curator
  caught 'smith' ranking on Apple Cake ("smith is useless"). Two textbook
  treatments: nested-term SUBSUMPTION (approximately-closed patterns, cf.
  C-value — a term whose df is explained by a longer phrase dies; 'granny
  smith apples' retires smith/granny/'smith apples'; 4-grams mined only to
  subsume; n-grams can't start/end on function words) + ATTESTED PLURAL
  FOLDING ('apples'+'apple' = one lemma, folded only when the singular is
  corpus-attested — molasses safe; display = the cohort's most-written
  surface of the WHOLE term, never a per-token Frankenstein like "eggs
  yolks"). Apple Cake before/after: fragments dead, 'granny smith apples'
  seated, freed slots surfaced nutmeg/allspice/dark rum. A staple filter
  (leaveners/sugars/flours) was built, shipped, and REVERTED the same hour —
  curator settled the principle: **signals = measurement, kept FULL; classes
  = sellable inventory; the sellability rule lives at the JOIN (the proposal
  prompt), never in the measurement.**
* **Delete audit → doctrine → six gaps fixed** (curator: "no way to delete a
  collections search item"; agent audit of all 25 pages; memory/
  feedback_delete_exclusion_pattern). On delete-and-replace lists a bare row
  delete resurrects next run, so removal = persistent EXCLUSION: candidates
  (`excluded` col, survives replace, blocks re-entry by ASIN, Restore strip
  + ⓘ; medal-clear '×'→'–' — it read as row-delete), curated picks (ban by
  ASIN→brand+title, slot re-key on collision, materialize skips), products
  offers (full offers-list editor replaces the offers[0]-only form), domains
  top list (per-row ✕, junction spot-fix — candidate filters stay the
  durable ban), rejected class proposals (🗑 rejected-only hard delete), and
  the dead rejects PATCH wired (per-row triage select). Armed two-click =
  the sub-item standard.
* **Medal "doesn't work" forensics:** the gold click had LANDED and
  persisted — the awarded state was an invisible bold 'G'. Medal buttons now
  paint in medal palette + "✓ gold — saved" status; failures surface
  (handler had ignored the response entirely). Cohort default order →
  RealRank (screened first); "screen score" renamed Wilson + ⓘ. Both
  collection pages' intros grew the ⓘ explaining the two supply lines
  (owners-say vs authorities-say, converging in ONE catalog by ASIN).
* **Mechanical matcher PUNTED, on evidence** (scripts/
  calibrate_class_match.py + match_one_dish.py, kept + rerunnable, cache
  gitignored). Calibration on 578 junction-labeled pairs: separation is huge
  (hard negs p10 1.05) BUT positives are ~exact-lookup (signals were seeded
  from the same evidence — circular), embeddings CANNOT bridge synonyms
  (bundt→tube 0.906; curator hand-encoded bundt/angel-food/fluted onto Tube
  Pan instead), name-surface is median +0.75 worse than signal-surface (the
  founding claim, quantified), and hygiene dominates (identity terms leaked
  onto implication classes made Baking Soda rank #1 under an exclusivity
  discount). Verdict: matching = lookup over curated trigger phrases; the
  LLM is the implication engine.
* **THE MATCHER IS THE LLM — `dish_class_propose` rebuilt.** Prompt now
  carries the FULL cohort signals, the class roster WITH trigger phrases,
  and the METHOD TEXT of the top-3 winners (prep implications live in
  instructions the miner doesn't read; method-quoted evidence = anecdotal,
  tier-capped T3). Output = descending ORDER OF NEED → junction `sort`;
  re-propose REPLACES the proposed tier (approved/rejected untouched);
  sellability fence in the rules (no produce/leavener/sugar/flour classes —
  "identity fruit is IDENTITY, not a product we sell"); **NO
  auto-registration** — a new-class chip mints its registry row only on
  curator APPROVAL (set_status). Prompt template moved to system_config
  (`dish_class_propose_prompt`, [[TOKEN]] substitution, curator-editable,
  no restart — code constant is seed only). Apple Cake proof run: Apple
  Corer #1 / Vegetable Peeler #2 (evidence-honest — the cohort SAYS
  vegetable peeler), Loaf Pans arrived via method text, Applesauce proposed
  as an unregistered chip.
* **Registry repossessed (260 → 90).** The 2026-08-29 "sweep" was exposed by
  its own logs as 26/247 + the B-prefix batch — 38 dishes, alphabet-biased,
  pre-fence, auto-registering: that's where the junk came from. Deleted the
  3 named staples (Granny Smith Apples/Baking Soda/Powdered Sugar) + 167
  sweep-provisional unused classes; KEPT everything curator-created (39
  spices, Tube Pan, Loaf Pans…), everything in-use, everything
  curator-approved. 286 proposed chips now reference unregistered names —
  valid by design (approval re-mints). 'Piec Plate' typo removed the
  correct way: rename→'Pie Plate', then MERGED Pie Pans + Pie Pans (9- in) +
  Pie Dishes (9 in) onto it (signals union + 'pie pan' triggers, junction
  moved, curated collection re-pointed; 7 products, one Emile Henry dup
  noted for a products-editor merge).
* **docs/m2m-migration-map.md** — the junction idiom owed to TWO patients:
  dish↔recipe (the known stamp problem) AND product↔class/collection (the
  Piec Plate incident: class = free-text stamp, collection links half-die
  on delete). Verdict recorded: bite the bullet on the PRODUCT side before
  the impressions/EV layer bakes class strings into append-only logs;
  design pending.

### Open

* v3 signals sweep finishing (in flight at write). Proposal runs stay
  PER-DISH, curator-triggered — no proposal sweep, ever, without asking.
* Products editor: merge the Emile Henry pie-dish duplicate rows.
* In-use junk classes await their products/collections cleanup first:
  Recycling, Food Recyclers ×2, Flour (1 kg), Bread Slicers, Apple Corers/
  Nutmeg oddities, Meatloaf/Loaf pairs, Bundt Pan (shadowed by Tube Pan).
* Product-side M2M migration design (class-by-key + placement junction +
  delete preflight) — next big build, before the render/EV layer.
* Carried: Pistou fr-line Reserve rerun; per-publisher cap; twins merges;
  render side per 08-29; grey-list produce classes now moot (fence).

## Session log — 2026-08-31 (later) — picks get faces and part numbers, Amazon leads, and Shopify names the prospects

Commits: this one (follows ea710c2 same day). No restarts owed — verify/
materialize/propose all run out-of-process; page changes are reload-only.

* **CCP re-propose proof run:** approvals untouchable and ranked top by the
  model; merged Pie Plate seated at #3 citing the union signals; Fine Mesh
  Strainer arrived via method text (anecdotal T3); Port Wine proposed as an
  UNREGISTERED chip (gate working). Two findings: the model shoehorned Oreos
  into 'Chocolate Chunks (8 oz)' after the purge deleted Oreo Cookies
  (reject it or soften the reuse-verbatim prompt line — curator's call, the
  prompt is Settings-editable now); and the 08-29 Tawny Port APPROVAL was
  already gone before the purge (revoked or lost — the purge only deleted
  approval-less classes).
* **signal-terms report** (docs/reports/signal-terms.html +
  scripts/report_signal_terms.py): all 2,182 unique v3 terms, deduped,
  alpha, filter box + click-sortable columns. v3 proof corpus-wide: zero
  bare fragments ('smith'/'purpose'/'yolks' survive only inside phrases).
* **Model number = the sibling disambiguator (KitchenAid 7-speed case).**
  Research prompt rule 2b: `model_number` sourced FROM THE REVIEWS FIRST
  (the model the reviewer TESTED is the recommendation; a listing's number
  can name an untested sibling), manufacturer page fallback, blank-never-
  guessed. Stored on picks (+ migration), shown in the row, materializes
  into specs.model_number (strengthens _mfg_key dedupe).
* **Pick images.** The old AsinImage widget endpoint is DEAD (DNS gone) —
  that was the "no images" report. verify's identity call (az.product_
  ratings) already carried the listing photo + model_number and dropped
  both; now kept on the pick (image, model blank-fill). Thumbnails render
  from pick.image, hotlinked m.media-amazon.com, ASIN text links to a CLEAN
  /dp/ built from the ASIN (never the harvested link) on BOTH collection
  pages incl. excluded strips.
* **Blank-ASIN recovery (Nutmeg run: 3 picks, all blank — the only prior
  recovery was our review corpus, no spice coverage).** New verify rung 1b:
  ONE EasyParser Amazon search per blank pick (brand + review-stated model
  + title + capacity), FORM-GUARDED both ways (whole nutmeg never resolves
  to the ground jar), step-2 identity still re-verifies. Re-run Nutmeg to
  fill; also decide its class family (registered 'Nutmeg' equipment vs the
  spice batch's 'Ground Nutmeg').
* **Amazon-first offer policy (curator: Prime makes Amazon the preferred
  buy path).** _offers_for sorts Amazon first at storage; audit showed the
  ASIN⇒Amazon-offer invariant already held (0 violations); 33 existing
  products reordered. affiliate_url stays blank — /go/ mints at click,
  which is what makes "always available" true without storing tags.
* **Shopify = the prospect classifier** (intake/products/shopify_detect.py
  + retailer_hosts table — the evidence cache, 90d re-probe). THE TEST:
  GET /products.json?limit=1 -> 200 {"products":[...]} (headers unreliable
  through CDNs; page markers corroborate). 54 catalog hosts probed: 13
  Shopify stores incl. McCormick, Made In (json gated, markers caught it),
  Emile Henry USA, Our Place, Milk Street, Taza, Burlap & Barrel. Bonus:
  the .json payload carries per-variant SKUs — a free structured feed.
  OXO/W-S 403 and Mercer/Norpro 202 = anti-bot walls, not clean negatives.
* **The 13 seeded as PROSPECT programs in the affiliates ledger** (the
  status tier existed for exactly this), hosts wired into affiliate_
  program_hosts so activation needs zero plumbing. No universal Shopify
  code exists — consolidation is at the network tier (ShareASale/Impact/
  CJ/Rakuten; apply-not-negotiate) or aggregators (Skimlinks/Sovrn — one
  deal, haircut rates). Ladder: aggregator floor -> networks for proven
  earners -> direct for concentrated volume.
* **goto.* tracking links cleaned:** two offers carried a reviewer's Impact
  redirects (goto.walmart/goto.target, publisher 2204542) — destinations
  decoded from the u= param, clean walmart/target product URLs stored.
* **Affiliates editor links clickable:** hosts list links to the stores;
  Dashboard URL field now uses the shared urlField control.

### Open

* Re-run Nutmeg + the KitchenAid mixer collection (ASIN recovery + models
  + images); decide Nutmeg class family. Emile Henry pie-dish product merge.
* Prospect research pass: which network carries each of the 13.
* Rejected 'Chocolate Chunks' shoehorn: soften reuse-verbatim prompt line?
* Carried: product-side M2M design; in-use junk classes; per-publisher cap;
  Pistou Reserve; twins; render/EV layer (now with Amazon-first + /go/
  routing groundwork laid).

## Session log — 2026-08-31 (evening) — store ≠ network ≠ program, and research before schema

No code since 18481c3 — this entry records a DECISION and work in flight.

* **Curator caught the conflation in the affiliate seeding:** the 13 Shopify
  prospects were seeded as PROGRAM rows, but a store (Made In: hosts,
  platform, contact) is not a monetization relationship (Made In × Impact:
  publisher id, rate, link template). Flat `affiliate_programs` would
  repeat network credentials per merchant and can't express one merchant
  reachable via two relationships. Agreed target: THREE entities — stores
  (absorbing retailer_hosts), networks (our accounts), programs (the join,
  time/status-carrying) — with /go/ resolving click host → store → best
  ACTIVE program (priority), which is the fullest expression of the
  codes-minted-at-click rule. Timing argument accepted: the subsystem is
  two days old (1 active program, no click history) — restructure is
  cheapest NOW, same logic as the M2M map.
* **Research BEFORE schema (curator's call, feedback_research_before_design):**
  three parallel web-research agents in flight — (1) affiliate link-
  management platforms' data models (Affilimate/Trackonomics/Skimlinks/
  Sovrn/Geniuslink APIs), SubID architecture, conversion-reversal
  reconciliation, link rot, Amazon ToS constraints; (2) metasearch
  monetization architecture (Kayak/Trivago/TripAdvisor/Skyscanner: partner
  layers, click-out tracking, what transfers to small scale); (3) content-
  commerce publishers (Wirecutter/Dotdash/BuzzFeed: offers-service pattern,
  editorial/monetization firewall, revenue-per-page attribution). Findings
  synthesize into docs/affiliate-programs-and-clicks.md as the proposed
  schema for curator sign-off BEFORE migration code.

### Open

* Synthesize research -> schema design -> curator sign-off -> build the
  three-entity migration (stores/networks/programs + /go/ + editor).
* Everything carried from the two earlier 2026-08-31 entries.

## Session log — 2026-08-31 (night) — three research agents report, and the affiliate layer gets its real shape

Commits: this one. RESTART OWED for the new /affiliate-stores, /affiliate-
networks, /affiliate-connections endpoints (the migration itself already ran
LIVE — a curator-triggered job loaded the new code from disk and executed it
mid-rehearsal; verified complete and correct after the fact).

* **Research round (3 parallel agents; condensed into design doc §9).**
  Sources: link-management platforms' public APIs (Strackr/Affilimate/
  Skimlinks/Sovrn/Trackonomics/Wecantrack), trivago 20-F + TripAdvisor 10-K
  + Skyscanner partner docs, Wirecutter/Dotdash/BuzzFeed/Future-Hawk
  reporting. All three converge on the same model AND independently
  validate decisions already taken (codes-minted-at-click, own click
  ledger, /go/ redirect, Amazon-first at the offer layer, commission data
  never near ranking — the Wirecutter firewall, complete with NYT's 2019
  breach as the cautionary tale).
* **The five entities (flat table → researched shape):** stores (merchant:
  hosts, platform; a store with no program IS a prospect) · networks (the
  rails + their subid MECHANICS: param name + safe max, seeded from
  research: Impact subId1/255, Awin clickref/50, Rakuten u1/72, Amazon
  ascsubtag/restricted) · connections (OUR account per network — the entity
  every flat model misses; Strackr and Trackonomics both carry it) ·
  programs (the store×connection JOIN: status ladder, priority, template) ·
  immutable ledgers.
* **Phase 1 BUILT + live-migrated:** amazon → store 'amazon' + connection
  'amazon-associates' (tag carried) + program re-keyed; the 13 Shopify
  prospects became STORES (platform stamped from retailer_hosts), their
  program rows deleted — a prospect is a store without a program, by
  definition now. /go/ resolution: host → store → best ACTIVE program by
  status-ladder+priority, merged with connection creds + network subid
  metadata. Minted Amazon link verified byte-identical pre/post.
* **Click ledger upgraded:** clicks stamp `store` + `rate` AT CLICK TIME
  (trivago: price fixed in advance of the click; history survives rate
  edits). Conversions REPLACED (empty) by `affiliate_conversion_events` —
  immutable event log, reversal = new event, click_id NULLABLE with
  match_status (25-30% attribution loss is structural, per metasearch).
* **Editor = three panes** (affiliates.html): Programs / Stores (platform
  chip, PROSPECT state, clickable hosts, guarded delete) / Networks (subid
  mechanics + inline connection upsert). Program form gained store +
  connection selects (connection fills credential blanks).
* **Deliberately NOT built (research: wrong scale or wrong side):**
  auctions/bidding, live price scraping, multi-touch attribution,
  merchant-of-record. Phase 2 = conversion polling + reconciliation diff
  on first non-Amazon activation; Phase 3 = link-health jobs + payout
  reconciliation.

### Open

* RESTART for the new endpoints; then eyeball the three-pane editor.
* Network research per prospect (which of ShareASale/Impact/CJ/Rakuten
  carries each of the 13); aggregator (Skimlinks/Sovrn) as the floor.
* Everything carried from the earlier 2026-08-31 entries (Nutmeg re-run,
  KitchenAid mixer re-run, Emile Henry merge, product-side M2M design…).

## Session log — 2026-08-31 (late night) — dishes get their story, picks get faces confirmed, and the prospects get named

Commits: this one (follows 2063908 + c89d365 same day). RESTART OWED for:
/dishes/{name}/story-draft, the dish PATCH whitelist (story fields), the
prospect thin-sample retry + exactly-6 prompt, and the auto-rematch-on-
create. Pages are reload-only.

* **Affiliates editor finished to spec:** sidebar sections became TABS
  (accordions tried first, curator called tabs — one clean list at a time,
  active tab sticks per browser, cross-type links switch tabs); ＋ is now a
  chooser (Program | Store — a prospect is a store without a program, by
  definition); phone layout fixed (page grids + kv collapse ≤640px; the
  shell drawer already handled the sidebar); stores carry THEIR
  affiliate-program URL (shown on store AND program panes) + contact/
  phone/email (mailto:/tel: links).
* **Prospect research pass (3 parallel web agents, sourced, blank-beats-
  guess) filled all 13 Shopify stores.** Actionable now: Burlap & Barrel
  (agency-run, partnerships@; ~10%/14d), Made In (Impact, ~8%/30d), Our
  Place (Impact CID 4218057), Emile Henry (own page, courts food bloggers,
  info@eh-usa.com, Awin), Graza (email-first). Stubs/none: Flavortown
  (Refersion pixel, blank page), GIR (Affiliatery portal half-deployed),
  McCormick/Taza/Milk Street/Cangshan/Abioto/Life&Home (none/discontinued
  — outreach or aggregator). **LOAD-BEARING: ShareASale was SHUT DOWN by
  Awin 2025-10-06** (post-cutoff; the research caught it) — network row
  marked CLOSED, Awin marked successor; stale shareasale links on merchant
  pages (Emile Henry's own!) resolve to Awin now. The agent also caught MY
  seeded B&B URL 404ing (/pages/affiliate-program → /pages/affiliate).
  Verdict: Impact = the highest-value single signup.
* **The holes/empty-dish mystery resolved END TO END.** Baked Chicken
  Breast (created off the holes list) measured cohort=0 → panel said "not
  measured yet" → curator hunted a phantom bug. Three fixes: (1) panel now
  distinguishes measured-EMPTY from never-measured, with the alias-vs-
  refresh guidance; (2) the missing hop was REMATCH — run live: 43 rows
  flowed in (2 matched/41 nearest, incl. strays off Baked Stuffed Shrimp
  and Chicken Wings; siblings' 30+30 curated winners untouched) — the hole
  was REAL; (3) dish CREATE now auto-enqueues the free rematch (entity-
  deduped) so hole-born dishes populate in ~1 min instead of overnight.
* **Proposer breadth + ordering fixed.** Apple Brown Betty returned ONE
  proposal against apple-corer-at-55x signals — model variance (rerun gave
  9). Thin-sample retry added (<3 → one auto-retry, keep larger), then
  curator: "tell it how many" → EXACTLY 6. And the curator caught
  cinnamon/nutmeg outranking the corer: NEED redefined in the prompt as
  BUY-LIKELIHOOD (ownership-gap × dish-demand — a tool most kitchens lack
  beats a staple they own); TIER regraded as evidence-confidence,
  independent of order. Rerun: Apple Corer #1, Apple Peeler #2, spices
  demoted to upgrade slots. Purge collateral repaired: Apple Corer
  restored (renamed from the junk 'Apple Corers', implication-only
  signals), Ground Nutmeg re-familied gourmet + signals back.
* **Dish story + cook's notes BUILT** (project_dish_story_ethnicity, at
  last): dishes.story/ethnicity/origin_region/cook_notes ({tips,
  techniques, landmines} JSON), PATCHable with validation, in every read.
  ✨ Draft endpoint = the domains deep-enrich pattern GROUNDED in the
  dish's own cohort (signals + top-3 winner method text; "where recipes
  disagree, that disagreement IS a landmine") — drafts fill the FORM,
  never the row. New accordion in the dish editor with auto-growing
  textareas (size to content on open/type/draft — no inner scrolling).
  Recipes INHERIT at render time = later phase, on record.
* **Curated pick images confirmed working** — the "still no images" report
  was a pre-capture collection (Stand Mixer) + a frozen screenshot red
  herring; tonight's runs (blenders, Ground Nutmeg ×3 via the NEW
  amazon-search ASIN recovery — whole-vs-ground resolved with distinct
  ASINs + models — and Apple Corers) all carry m.media-amazon photos +
  model numbers, verified rendering live. Old collections fill on re-run,
  or via an offered image-only backfill (~1 credit/pick).

### Open

* RESTART, then: Apple Brown Betty ✨ Draft as the story demo; re-run
  Stand Mixer (KitchenAid model disambiguation NEEDS the new review-first
  model_number rule); decide image backfill vs re-runs for old collections.
* Baked Chicken Breast: keep as parent vs alias the siblings — curator
  call now that it holds 43 rows. Holes page: show nearest-existing-dish
  + distance per cluster (alias-holes vs real holes at a glance).
* Impact signup (Made In + Our Place); Burlap & Barrel application;
  Emile Henry email. 'Nutmeg' class merge (4 rows). Rejected junk chips
  (Vanilla Extract on ABB). Recipes-inherit-story render phase.
* Carried: product-side M2M design; per-publisher cap; twins; Pistou
  Reserve; render/EV layer.

## Session log — 2026-09-01 — four channels of evidence, filters before fetch, and search learns what a dish is

Commits: this one (follows c6b0a0a). RESTART OWED for: the dish-aware
search, the candidate-filter PATCH validation + /domains/{domain}/
filter-compile endpoint, and the four-channel proposer (propose runs
in-server). Pages are reload-only (CSS bumped v=20260901a).

* **The proposer became the four-channel matcher.** After the equipment
  experiment (verb-object pairs across ABB + Baked Chicken Breast —
  docs/reports/equipment-llm-experiment.md) and the curator's ChatGPT
  design handoff, PATTERNS moved to direct/functional/inferred/affinity
  (legacy identity/implication/passthrough stay valid on stored rows);
  TIER now DERIVES from channel (direct=1, functional/inferred=2,
  affinity=3) — the channels ARE the evidence grades. The prompt was
  rebuilt around the recipes as primary evidence with DISTINCTIVE_TERMS
  demoted to "context, not a constraint", and affinity is grounded in
  DISH IDENTITY per the curator's amendment ("the dish would dictate
  affinity") — provenance tallies, not recipe-tag votes. Count loosened
  from exactly-6 to "at most 10, typically 6-8". Operations extraction
  (ACTION+OBJECT+QUALIFIER) persists to dishes.operations. Proven live:
  Beef and Broccoli inferred the santoku from "thin strips against the
  grain"; Pastitsio proposed Kefalotiri (new chip) and Greek tours/
  cookbooks off dish identity. Prompt synced to system_config
  (dish_class_propose_prompt; DB canonical, code seed).
* **ROOT CAUSE of every "thin/empty model reply": thinking ate the
  budget.** stop=max_tokens with ONLY a thinking block = zero text.
  Proposer 3000→14000, story-draft 1500→6000, experiments 9000.
* **B&B 15-vs-2 solved twice over.** (a) The proposer sampled by OU
  alone, so 51 UNGATED nearest-rung strays outranked the 15 assigned
  winners — _gather_recipes now orders rung-first (assigned → matched →
  nearest), THEN OU; re-propose: 8 proposals, zero contaminants; all 15
  winners verified intact (NOT last-run-wins). The strays' true homes
  are catalog holes: Fried Rice, Chow Mein, Mongolian Beef, Pepper
  Steak. The matcher surfaces cohort contamination FOR FREE. (b) The
  recipe search found 2 of 70 because FTS ANDs the literal word "with" —
  _materialise_text_match grew a dish-aware layer: connector-normalized
  query (and/with/&/w/n) against the fold+alias name_index → all
  dish_effective rows injected ABOVE text hits. beef with broccoli → 70;
  "boston cream pie" (no dish) stays text-only — no Cream-Pie collapse.
  Title embeddings deferred; identity vectors are the phase-2 reserve.
* **Candidate filters SHIPPED** (docs/candidate-filters.md → real).
  input/pipeline/candidate_filter.py: {keep,drop} condition rules
  (SEMrush-filter-shaped; url/url_path/title strings, url_depth/traffic/
  traffic_pct/rank numbers), evaluated PRE-FETCH; author provenance
  (curator final, llm overturnable) → ledger reasons filter-curator/
  filter-llm; ✨ Compile = plain English → rule via ONE Sonnet call,
  never per-candidate. recipe_path + exclude_words REPLACED (27 domains
  migrated as curator rules; loud fail if a rule drops everything).
  Barilla vindication: 18 dropped pre-fetch, 21 stored — no more
  unblocker credits burned on category pages.
* **Signals verdict: LEAVE IT.** The blob is 100% derived (recreatable
  by one free sweep — verified: no curator state inside) but it is NOT
  just the ugly term lists: cohort authority stats + the provenance
  tallies live there, and provenance currently FEEDS the affinity
  channel while dishes.ethnicity/origin_region sit empty (~280 dishes).
  Teardown milestone: when the story/ethnicity fields are populated,
  slim the sweep to authority-only. Curator: "just leave it".
* Menu scroll fixed (library-shell nav-menu max-height + overflow-y) —
  the grown admin menu was unreadable; CSS version bumped on 14 pages.

### Open

* RESTART, then verify: beef-with-broccoli search, a filter-compile on
  some domain, a re-propose. B&B chips await approval: Oyster Sauce,
  Toasted Sesame Oil, Riesling — and Soy Sauce (staple-boundary call).
  Pastitsio: Kefalotiri chip. Create the hole dishes (Fried Rice, Chow
  Mein, Mongolian Beef, Pepper Steak) — auto-rematch drains the strays
  on create.
* Carried: Baked Chicken Breast keep-vs-alias; Nutmeg class merge;
  Stand Mixer re-run; Impact signup / B&B application / Emile Henry
  email; product-side M2M design; per-publisher cap; twins; Pistou
  Reserve; recipes-inherit-story render; render/EV layer.

## Session log — 2026-09-01 (night) — the holes get their dishes, and 89 strays go home

Post-restart close-out. The four catalog holes the B&B contamination
exposed were created via dishes_lib (auto-describe + embed on create),
then one dish_rematch sweep drained the strays: 89 master rows + 4
local moved, zero failures.

* Fried Rice (Rice & Grains) — 33 rows (10 matched / 23 nearest)
* Chow Mein (Pasta & Noodles) — 16 rows (4 / 12)
* Pepper Steak (Meat) — 25 rows (3 / 22)
* Mongolian Beef (Meat) — 15 rows (3 / 12)

Beef and Broccoli slimmed 70 → 47 with its 15 assigned winners intact;
rows magnetized from Kimchi / Cabbage / Chicken Noodle Soup found their
real homes. B&B still carries 28 nearest — mix of genuine variants and
possibly one more hole; glance at the holes page.

### START HERE tomorrow (clear morning)

1. **Spin the four-channel matcher on a virgin dish:** cohort measure +
   propose on Fried Rice (then the other three). None of the four new
   dishes are measured or proposed yet.
2. **Chip approvals waiting:** B&B — Oyster Sauce, Toasted Sesame Oil,
   Riesling, and the Soy Sauce staple-boundary call; Pastitsio —
   Kefalotiri. (Chips mint classes only on approval.)
3. **Verify the restart took:** search "beef with broccoli" (expect the
   full dish cohort ranked first), try a ✨ Compile on a domain filter.
4. Then the carried queue: Baked Chicken Breast keep-vs-alias · Nutmeg
   merge · Stand Mixer re-run · Impact signup / B&B application /
   Emile Henry email · holes-page nearest-dish display · product-side
   M2M design.

## Session log — 2026-09-02 — sources get honest, the curator gets a pinned slot, and the revenue path gets its map

RESTART not owed on MARLEY (user restarted after each server-side change;
the last batch of edits — source accounting, on-class filter, editors
choice — went in before the immersion-blender tests). BAILEY: see incident.

* **BestReviews joined BOTH inventories** — the Tribune ask exposed that
  "review sites" live in two places: review_sources/ (bookmarklet
  decoders; new bestreviews.py covers bestreviews.com + every newspaper
  white-label via the reader-supported header signature; pressure-canners
  page ingested clean: 5 products, tripled carousel deduped, cross-sell
  excluded) AND realrank_research SOURCE_SITES (curate authorities; added
  as seat #9). Both canner collections re-run with it seated — Stovetop
  drew the first-ever 9/9 source full house.
* **"Why always Serious Eats?" answered with data, then fixed three ways.**
  The cache showed every canner run fetching ATK/Wirecutter/CR pressure-
  COOKER pages (0 canner mentions) — byte counts made off-topic look like
  answers. Now: (1) fetch log prints each page's own title + on-class hit
  count; (2) the research reply must file a source_report (page_covers /
  relevance / used_in_ranking — "off-topic honestly reported is a GOOD
  answer"); (3) it lands in job log, summary JSON, and a SOURCES
  CONSULTED brief section.
* **Review sources got their candidate filter** (the recipe-filter analog
  the curator asked for): pre-fetch, SERP candidates are title-ordered;
  post-fetch, a page must mention the class ≥2× (plural/possessive/
  knife→knives/canner→canning fuzz) or it's rejected off-class with the
  title named. Max 3 fetches/site. Proven live both directions on ATK.
  Plus the FALLBACK LADDER: curated_collections.search_terms (editor
  field) — aliases tried in order when the class name flunks, and they
  WIDEN the on-class accept set ("Multicooker"). Cache is keyed by class
  alone — tick re-fetch after editing terms.
* **Editor's choice slot BUILT**: curator pins a product on the
  collection (free-text field); the run must return an editors_choice row
  (requested-but-missing = shape error), analyzed with full rigor via
  rows_of parity — identity, owners, offers — in its own labeled section
  in picks + brief. Provenance firewall in the prompt: never displaces
  the ranking; no-source-tested must be said aloud; edge_over_next
  becomes an honest comparison to overall #1. Margin note: point it at
  high-rate D2C stores (Made In ~8% vs Amazon ~3-4%).
* Pick-row polish: brand said once (pkName mirrors render._name), one
  body font size, capacity prose (>60ch / "N/A…") off the title line,
  The catch ABOVE Ahead-of-the-next (both surfaces).
* **RealRank decomposed for the curator** (the "more than NPS+Wilson"
  question): the missing piece is the −1..+1 → 0..100 rescale (50 =
  promoters equal detractors); plus Agresti-Coull pseudo-obs (the
  13-ratings-scores-100 fix) and 4★-passive. Braun 84.7 reproduced
  stage by stage. Multi-retailer pooling: plumbing real, NEVER fed —
  zero products carry pooled_from; Amazon-only today by design gap.
* **Class vs collection settled**: class = demand-side join hub (M2M,
  283 named, most uncurated) and the proposer's controlled vocabulary
  (roster carries name+family+6 trigger phrases — signals ARE used, as
  prompt hints); collection = supply-side workproduct. The one true
  redundancy = the name-string join → FK is the fix (m2m map), not
  deleting either.
* **Revenue playbook shipped as artifact** (Sink-or-Swim Playbook,
  live DB numbers): funnel 9,033 recipes → 292 dishes → 8 with approved
  needs → 52 approved briefs → 1 active program → ZERO recipe pages
  showing products. Gate 1 = render layer (engineering ask verbatim,
  FK first). Corrected after curator's "what's a chip": ~305 of 342
  proposed chips are STALE Aug-29-era (same vintage as the repossessed
  registry, many name deleted classes) — re-propose over them, don't
  triage. Batch of 36 re-proposes RUNNING now (resumable by design).
* **Render/EV layer design ON PAPER** (docs/render-ev-layer.md +
  memory): curator asked for ad-style dynamic selection — decomposed as
  eligibility (chip gate = ad review, human, later auto-approve-above-
  confidence) vs serving (EV = p(click)×p(buy)×price×rate). Phases:
  FK first → deterministic EV + impressions log → Thompson sampling
  seeded from need order → learned p(click)+taste profile at volume.
  Curator amendment ON RECORD: "noise can be valuable" — noisy SERVING
  is a feature at every volume (exploration floor never zero); only
  noisy ESTIMATES wait for volume. Firewall at every phase: EV picks
  classes and offers, never reorders picks inside a class.
* **BAILEY incident**: "failed to fetch" on the clone = server down
  since ~Aug 26 AND recipes.db there MALFORMED (dies in init_db before
  binding 8009 — drill task spawns pythons that never listen; pages the
  curator saw were browser cache). bcc_restart.bat can't work there
  (NSSM "BCC" is MARLEY-only; BAILEY = schtasks BCC-Drill). Remedy
  RUNNING: bcc_sync_bailey.ps1 -WithDbs -FreshBackup. Durable fix on
  the list: install the NSSM service on BAILEY so death is loud.

### Open

* In flight: 36-dish re-propose batch (relaunch until stale=0; then the
  APPROVAL pass is the curator's queue) · BAILEY -WithDbs sync (verify
  health check at end; then consider NSSM install on BAILEY).
* Editors-choice live test still pending: pin was never saved (stale
  browser page) — hard reload, Edit → Editor's choice → Save → verify
  survives reopen → Run.
* Gate 2 actions: Impact signup, B&B application, Emile Henry email.
* Carried: render/EV build (after FK) · Nutmeg merge · Stand Mixer
  re-run · product-side M2M design · per-publisher cap · twins ·
  Pistou Reserve · recipes-inherit-story.

## Session log — 2026-09-02 (night) — BAILEY's real killer found in four blank names, and the chip queue is reborn

Close-out of the two in-flight arcs. Working tree committed through 10f467d
before this entry; MARLEY server current (no restart owed).

* **BAILEY closed end to end.** The corrupt DB wasn't the disease — it was
  the symptom: `bcc_sync_bailey.ps1 -WithDbs` had NEVER delivered a file.
  The $copies table's `,@(src,dst)` rows double-wrapped, so $c[1] (the
  destination) was $null on every row — rclone failed all four copies
  ("can't use empty string as a path"), and the failure banner printed
  four BLANK names, which is what finally gave it away. The 08-26 "13MB
  short" truncation and every stale day since trace to this. Fixed
  (plain nested arrays, post-mortem in the comment, 10f467d), re-run
  clean: all four copies size-verified, sidecars cleared, BCC-Drill
  restarted, health 200. VERIFIED on BAILEY itself: integrity_check ok,
  292 dishes, Fried Rice present (= tonight's backup set is live).
  Also learned: clearing stale WAL/SHM alone let the OLD db boot — the
  "malformed" at startup was partly sidecar pairing.
* **Re-propose batch: 36/36 ok, 0 skipped, 0 failed.** Stale Aug-29-era
  chips now ZERO; the proposed tier is 300 chips, all four-channel,
  need-ordered, across ~44 dishes. The approval queue is real again.

### START HERE (post-/clear morning)

1. **Chip approvals — the queue is finally worth your time.** 300
   current-quality proposals across ~44 dishes (Dishes → dish → Product
   classes). Approve sellable, reject junk; approvals feed Gate 4
   (curate what accumulates demand). This is Gate 3 of the playbook:
   https://claude.ai/code/artifact/f3a4ea52-5c5b-40f2-b960-7718364fe7ed
2. **Editors-choice live test** (never completed — the pin never saved
   off a stale page): hard-reload curated collections, Edit Immersion
   Blender → "Editor's choice" field → Save → REOPEN to confirm it
   stuck → Run. Expect an EDITOR'S CHOICE section in picks + brief.
3. **Gate 2 money actions — PARKED (curator decision 2026-09-03):**
   no affiliate signups (Impact/Made In/Our Place, Burlap & Barrel,
   Emile Henry) until there is a DEMONSTRABLE user product to show
   in the applications. Amazon-only until then — which makes the
   render layer (item 5) the unblock for this item too.
4. **BAILEY durable fix:** install the NSSM "BCC" service there (mirror
   MARLEY's) so bcc_restart.bat works and a dead server is loud; until
   then it's `schtasks /Run /TN BCC-Drill`.
5. Engineering next-big-build unchanged: class↔collection FK, then the
   render/EV layer (docs/render-ev-layer.md — Phase 1 deterministic EV
   + impressions log).
6. Carried: propose the ~250 never-proposed dishes (biggest cohorts
   first) · Nutmeg merge · Stand Mixer re-run · product-side M2M ·
   per-publisher cap · twins · Pistou Reserve · recipes-inherit-story.

## Session log — 2026-09-03 — the pesto kept its chunks: technique becomes identity, and Chef gets a memory

The curator cooked John's Pesto (recipe 863) last night and caught the cook
rework red-handed: it had silently turned the source's "add the Pecorino in
chunks and pound" into "finely grate" (mouthfeel = the recipe's identity),
duplicated ingredient mentions, and Ask Chef INSISTED the recipe said
shredded. All three traced, all fixed, service restarted + smoke-tested.

* **Root causes.** (1) The rework prompt's own license: "Result fidelity
  matters; source fidelity does not" + the mise mandate pushed cheese to
  grated-ahead; the change wasn't declared in technique_changes and NO
  gauntlet gate checks meaning. (2) Prep lived twice — mise bundle labels
  AND steps 1–2; amounts re-listed at prep step and use step (basil 70g
  ×2, garlic ×2). (3) cook_ask.build_context grounds ONLY on `_cook` when
  present — Chef was a confident witness for the corruption; and every ask
  was a single-turn call, nothing persisted (the "shredded" answer was
  unrecoverable — token journal had counts, nobody had words).
* **Prompt v2.3** (`cook-rework-v2.3-2026-09-03`): TECHNIQUE IS IDENTITY —
  stated technique/tool/ingredient-form survives the rework exactly; add
  only where the source is silent; "a change you would not write down
  [in technique_changes] is a change you must not make." Plus SINGLE
  DEPLOYMENT (an ingredient enters the method once; prep in mise OR step).
* **Fidelity gate** (cook_fidelity.py NEW, sonnet ~$0.008/run): post-
  gauntlet LLM audit of source vs plan; undeclared technique substitution
  = failure → one repair pass → unrepaired fails the rework. Recorded in
  validators.ran as "source-fidelity".
* **`single-deployment` validator** (cook_validators.py): deterministic —
  flags an ingredient entering via a step AND a bundle (exempt: layered
  staples/to_taste, refs pointing back via reused_from_step/reserved).
  PROVEN: run against last night's corrupt _cook it flags exactly the
  basil and garlic double-mentions.
* **Chef grounds on BOTH docs** (cook_ask.build_context): `_cook` +
  `authors_original_steps/ingredients`; CHEF_SYSTEM now orders: on
  conflict, never insist — say the plan and the author disagree, quote
  the original, let the cook choose.
* **Chef memory** (cook_chat.py NEW): cook_chat table (recipe_id, user_id,
  surface cook|cook-voice|notes, role, text); all three ask paths (typed,
  streaming voice, notes chat) persist every exchange and pass the LAST 5
  exchanges as real conversation turns (curator accepted token cost;
  history = bare Q/A text, only the final message carries the recipe
  context). Also the audit trail that was missing last night.
* **Pesto re-reworked** (job 1362, $0.53): step 5 = the source verbatim
  ("Add the Pecorino in chunks and pound to incorporate, then add the
  Parmigiano in chunks and do the same"), mise keeps cheeses "broken into
  chunks", model self-declared the preservation in technique_changes,
  fidelity gate passed, basil enters once (step 4 references step-1 basil
  as `reserved`). Live smoke test: the exact question that failed last
  night now answers chunks + quotes the author; follow-up "what did I just
  ask you" recalled correctly; 4 rows in cook_chat.
* **Also this morning:** Gate-2 affiliate signups PARKED (curator: no
  applications until a demonstrable user product; Amazon-only) — recorded
  in the 09-02 START HERE + memory; render layer is now the unblock for
  BOTH revenue gates.

### Open

* 52 existing `_cook`s predate v2.3 (34 user + 19 master, minus the
  re-run pesto — CORRECTED from "~2,200", which was the equipment
  backfill count, curator caught it) — any may carry silent technique
  drift; detectable by prompt_version < v2.3. Batch fidelity-audit
  (sonnet-only, ~$0.008/recipe ≈ $0.50 total) triages which need
  re-reworking. Not launched — curator's call.
* Voice path history is wired but untested live (cook-voice surface).
* Chunked mise-bundle labels still say "washed and thoroughly dried"
  while step 1 washes — cosmetic tension, single-deployment is satisfied
  via the reserved back-ref; left alone.
* START HERE queue from 09-02 unchanged: chip approvals (300) ·
  editors-choice live test · BAILEY NSSM · FK → render/EV.

## Session log — 2026-09-03 (cont.) — the iPad wait: 900ms → 30ms, and the thumbnail learns to be stored

Curator: "when i first click dishes the wait was noticeable… i suspect we
don't have an index on that view." Measured first (no index was missing —
the sort is client-side): the /dishes load was ~900ms server-side =
275ms re-deriving every card thumbnail (JSON-parsing 4,114 master rows
PER PAGE LOAD) + a 500KB all-editor-fields payload + ~400ms of
middleware body-shuttling that scales with size.

* **dishes.preview_image is now a STORED column** (curator call mid-
  investigation: "why are we recomputing the thumbnail every time...we
  should just store it" — beats the TTL cache I was circling, which
  wouldn't fix first load). refresh_preview_images() derives at WRITE
  time: end of dish_refresh (that dish, via dish_key index), after the
  nightly dish_rematch sweep, and an init_db catch-all (heals BAILEY
  restores). Prints every row it changes. Backfilled all 293.
* **GET /dishes is a SLIM projection** — the 11 fields the sidebar +
  sorters + attention count read (last_ou_fit collapsed to {used}).
  The editor pane fetches the full shape via GET /dishes/{name} on
  first select (~10-30ms, cached on the row, refetched after save
  because loadDishes rebuilds the map). renderMain went async.
* **Result: ~30ms / 15KB on the wire** (was ~900ms / 93KB gzipped).
  Verified live post-restart; detail fetch verified full.
* **Audited the sibling lists on request:** recipes ALREADY has scroll
  paging (08-20 rebuild — IntersectionObserver → loadMoreRecipes, 100/
  page via /recipes/search = 36ms/16KB; the bare GET /recipes is
  API-only, no UI calls it unpaged). domains = 133ms, fine. Recipe
  rows are ~5KB each in the paged fetch — a future slim-projection
  win, not today's pain.
* Lesson repeated: localhost vs 127.0.0.1 on Windows — urllib paid a
  ~2s IPv6-first stall that curl didn't; measure against 127.0.0.1.

## Session log — 2026-09-03 (afternoon) — one job ran twice, and the queue learns to claim

Curator: "sidebar says 10 records but detail form shows only 8." Not a
too-few-steps problem — job #1350 (Guacamole refresh) EXECUTED TWICE,
concurrently (two log files, one id; second exec started 06:18 ET while
the first ran to 06:37). Two delete-and-replace refreshes interleaved →
15 master rows with colliding ranks, ledger showing 8 selected under one
model_version, sidebar 10 from the last finisher. Likely trigger: the
dishes page Run flow's drain fallback after a slow tunnel spawn
response (iPad). Nothing refused: /jobs/{id}/spawn accepted running
jobs, _run_job_id accepted running jobs, mark_running was a blind
UPDATE.

* **mark_running is now an ATOMIC CLAIM** (jobs.py): UPDATE gated on
  status='queued' (or 'running' with the observed DEAD pid in the
  WHERE — single-winner takeover of a crashed executor). A loser
  raises JobAlreadyClaimed; _run_one_job aborts WITHOUT mark_finished
  (stomping the live run's row is the exact torn state this prevents).
  Unit-tested all three paths. pid added to the job row projection;
  /jobs/{id}/spawn 409s on a live-pid running job (live at next
  restart; the executor-side claim is already active).
* **Eggplant Caponata was torn the same way** (22 rows / top-20; scan
  found only these two). Both re-run clean: Guacamole 10=10=10=10=10,
  Caponata 20 everywhere across master rows / ranks / sidebar /
  ledger — and the new preview_image job hook refreshed both stored
  thumbnails in production.
* **Fidelity audit of all 52 pre-v2.3 cooks ($0.50): 24 clean, 20 real
  defects** after triage. Two auditor defects found and fixed by the
  audit itself: (1) sonnet padded 8 reports with items whose own text
  says "not a violation" — gate prompt now orders OMIT on
  self-dismissal; (2) v_single_deployment flagged legitimate DIVIDED
  USE (Galaktoboureko's split sugar) — now silent when an ingredient
  splits across bundles in DIFFERENT amounts; same-amount double-
  mention and unmarked direct re-entry still flag (verified both
  directions). Standouts: Gravlax drops the cure-on-skin application,
  Paella pre-crumbles saffron + deletes "stir in" option, Broccoli
  soup invents florets.
* **20-recipe re-rework batch RUNNING** (~$11, sequential, canonical
  jobs path; manifest + driver in scratchpad). 12 personal + 8 master.

### Open

* Verify the batch: 20/20 success, spot-check Gravlax cure-on-skin and
  Paella saffron survive; re-audit is free (validator) + cheap (gate
  runs inside each rework now).
* /jobs/{id}/spawn 409 guard + dishes-page drain fallback belt: live
  at next server restart (claim already protects).

## Session log — 2026-09-03 (evening) — the extract gets an honest progress bar, the dots learn commerce, and 19 of 20 reworks land

* **Bookmarklet wait analyzed** (curator: "recipe gen has been taking a
  long time"): the 38s Jamaican extracts = ~28s streamed haiku
  markdown_to_recipe (26K in / 3.2K out — jamdownfoodie has JSON-LD but
  NO Recipe object, so the 4s fast lane legitimately can't fire; no
  harvest bug) + ~7s sequential tail. Tail (chapter + Moz + identity)
  now runs PARALLEL via copy_context threads (~5s saved); the rest is
  model-bound.
* **Real status + progress bar SHIPPED** (curator: "real status updates
  ... maybe even a progress bar"): extract_progress.py in-memory
  registry; extract-from-markdown takes progress_token, stamps every
  phase; the ALREADY-streamed LLM call now counts its deltas → true
  within-call pct (chars/11K expected); GET /extract-progress/{token};
  form polls ~1/s → bar + English phase line, funny rotation demoted to
  flavor below. Smoke-tested live end-to-end (temp API key, deleted).
* **Dish commerce dots SHIPPED**: /dishes slim rows carry
  classes_proposed/approved (one GROUP BY on dish_product_classes);
  dot = amber/hollow for attention (still wins), full green = has
  approved classes, half green = proposals waiting, accent = fresh but
  no classes; meta line says it in English ("2 approved · 3 to
  review"); "Sort: incomplete first". editor-shell.css carries the two
  new flag states.
* **Similar-recipes panel bug FIXED** (curator report): results
  lingered across recipe loads + no close. resetSimilarResults() on
  loadForm + populateFormFromRecipe; panel got a header + ✕.
* **Re-rework campaign: 19/20 landed on v2.3** (~$14 total). The
  gates earned three refinements along the way, each from a real hold:
  (1) fidelity auditor drops blank-quote junk rows; (2) divided use
  fully understood — different amounts pass however deployed, equal
  splits pass when the later portion's label says so ("remaining
  dill"), same-whole-amount-twice still flags (regression-tested);
  (3) prep TIMING is never a violation (mise pre-slicing ≠ technique
  change) unless the source depends on it. Prompt teaches the
  split-label convention. STILL HELD: Tandoori Chicken with Raita
  (transient appearance-order miss; v2.2 cook remains live — one real
  fidelity defect outstanding there).

### Open

* Tandoori Chicken re-rework: one more attempt someday (~$0.55), or
  cook it and let the next edit trigger it.
* Extract progress: extract-from-url path still has no phase stamps
  (bookmarklet/markdown path was the ask); wire if the URL box feels
  slow too.
* Recipes list rows are ~5KB each in the paged fetch — slim-projection
  win available if it ever matters.

## Session log — 2026-09-03 (night) — the class boundary: collections learn what to reject

Curator: Water Bath Canner run ranked an electric multi-cooker #2 and a
wire-basket-imaged Norpro #3 — "maybe a hint as to what the llm should
expect or reject?" Built as TWO layers:

* **curated_collections.class_criteria** (new column + editor textarea):
  the curator's binding definition — what counts, what to reject — fed
  VERBATIM into the research prompt, explicitly outranking the model's
  own judgment. Plus an ALWAYS-ON "CLASS BOUNDARY" prompt section for
  every run: no neighboring class, no different power source/working
  principle, no bigger appliance that merely CAN do the job, no
  accessories/racks/lids — exclusions named in methodology_note.
* **Deterministic on-class title gate** (curator's suggestion,
  V.flag_offclass_titles): pick titles must name the class or a fallback
  search_term (singular/plural fuzz) or get a visible identity_warning.
  FLAG, never delete — the Presto contains "Canner" and is off-class;
  the Norpro omits "water bath" and IS on-class — titles can't convict
  or acquit alone. Proven on the stored canner result: flags exactly
  the two bad picks, clears Granite Ware.
* Wiring: prompt.build_prompt(class_criteria=) → pipeline.research/run →
  job handler passes coll.class_criteria; editor + save in
  curated_collections.html. Restart needed only for SAVING the field
  (update endpoint); runs pick everything up per-spawn.

### Open

* Curator: write the Water Bath Canner class_criteria + re-run to
  confirm ("stovetop pot with jar rack; reject electric/digital
  canners, pressure canners, bare racks/baskets, multi-cookers").
* Consider surfacing identity_warning more loudly in the picks UI/brief.
