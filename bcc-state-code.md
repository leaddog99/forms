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
