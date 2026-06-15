the # bcc-state-code

Running state log for the recipe forms project. Append-only style; prune as items complete.

## Interesting links

- https://claude.ai/public/artifacts/fd58ba67-876d-47fc-9610-561ada60639f — TBD context (logged 2026-05-13)

---

> 📦 **Earlier session logs (2026-05-13 … 2026-05-31) are archived** in [bcc-state-archive.md](bcc-state-archive.md) to keep this tracker lean. Recent logs (2026-06-01 →) remain below.

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
