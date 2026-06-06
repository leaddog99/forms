# Split — Phase 1: Inventory & Classification Map

**Status: PROPOSED — awaiting approval. No code has moved.**
Per `SplitSpec.md` Phase 1, this assigns every module / table / endpoint / job /
config / UI to exactly one bucket, flags the genuinely ambiguous ones, and stops.

## 0. Locked vocabulary & decisions (2026-06-05)

**Names (locked):**
- **TBOTB** = Bucket **C** — the master / best-of-the-best engine + search-engine
  destination. Crawls, scores, critiques, ranks; owns the master DB. Public
  surface emits *work-product + JSON-LD envelope + link* only — never a substitute
  recipe. ("Corpus" anywhere below = TBOTB.)
- **BCC** = Bucket **L** — the Local tool. The user's personal
  capture / possess / cook app.
- **Recipe Enrichment API** = the **third entity** (productized form of Bucket
  **S**), owned by neither BCC nor TBOTB; BCC and TBOTB are its first two
  customers (SplitSpec §43 "jointly held in a third entity"). It is the home of
  Bucket S. (`recipe-core` = the shared library it is built on / ships, owned by
  the same third entity. Renamed from `bcc-core` so it doesn't imply BCC owns it.)

**Three-entity model (locked):** a third arm's-length **Recipe Enrichment API**
sits between the two products; both *call* it.
- **What the API does:** content + params → a fully structured, enriched recipe.
  Absorbs ALL per-recipe LLM "calls as calls": extract (md/JSON-LD→RecipeModel),
  enrich (provenance/classification/editorial), identity card, embedding
  *generation*, sanitize, validate, and the parameterized **seal serializer**
  (`full | static | public`). **BYOK** — the caller passes its OWN LLM key
  (encrypted to the API's public key); the API decrypts → uses for the single
  inference call → discards. Inference is billed by the LLM vendor directly to
  the customer's account; the API never fronts compute, so its price is a
  **constant** subscription / per-transaction fee for the value-add (immune to
  token/price volatility). This is BCC's "one outbound LLM call" — to the API,
  on BCC's own key. **Stateless** (retains nothing — no content AND no keys; no
  A↔B commingling). **Does NOT fetch** (fetch stays per-customer to preserve the
  actor/rights distinction). **Does NOT score** (turning a recipe into a vector
  is a transform; comparing the vector to the corpus = grade/rank = TBOTB's moat).
  *Trade:* prompts run on the customer's LLM account, so they're visible in that
  customer's vendor logs — fine for the first two customers; the deeper moat
  (model, orchestration, seal, scoring) never traverses their account.
- **Why a service and not shared plumbing:** an arm's-length vendor both license
  *strengthens* the A/B separation (both are merely its customers) instead of
  welding them together. Each customer can run its own instance, so "BCC runs
  with TBOTB absent" still holds, and it could take a third customer.
- **The seal bites at EMIT, not at the API's output.** The API returns the FULL
  structured recipe to its authenticated customers (BCC: the user's own copy;
  TBOTB: needs the full body internally to score/match). The `public` profile
  ("what doesn't come back") is enforced downstream at **TBOTB's
  public/subscriber boundary** via the same canonical serializer. BCC never emits
  publicly, so it never seals.

```
        ┌─────────────────────────────────────────┐
        │   Recipe Enrichment API  (entity C3)     │
        │   content + params → structured recipe   │
        │   extract·enrich·identity·embed·          │
        │   sanitize·validate·seal-serializer       │
        │   (BYOK keys · stateless · no fetch)      │
        └──────────────▲────────────────▲──────────┘
           calls │ (user-session          │ calls (bot crawl
                 │  fetch stays here)     │  fetch stays here)
        ┌────────┴────────┐     ┌─────────┴────────────┐
        │  BCC (Local)    │     │  TBOTB (Corpus)      │
        │  possess + cook │     │  score·rank·publish  │
        └─────────────────┘     └──────────┬───────────┘
                  ▲   ask-TBOTB-live read API │
                  └───{vector,DA,PA} → grade ─┘
```

**Scoring relationship (locked): "Ask TBOTB live" (read API).** BCC sends
`{embedding vector + DA/PA}` UP (stateless, never stored); TBOTB computes and
returns `{matched dish, category, grade, percentile, top-N links+critique}` DOWN.
The scoring model stays server-side (the moat). BCC degrades gracefully when TBOTB
is absent (loses grading, still captures/cooks). This is SplitSpec §188-189
(`/match`, `/grade`).

**Still open (need your call):** §6-F auth/users ownership · §6-G `domains`
extraction-tips routing · §6-H config split · explicit go-ahead to cut the 4
bridges (§6 A-D).

## Buckets (from the spec)
- **L — BCC, the Local tool**: recipe-rework deterministic parts (render,
  validators/gauntlet, bundling, scheduling, unit conversion, ingredient views);
  capture + parsing; the user's own recipe store; the LLM extract/anchor calls
  *as calls* (prompts/backends).
- **C — TBOTB, the master/corpus**: dishes + matching,
  cohort/embedding, scoring (popularity/exceptionalism/authority), editorial; the
  master store, the work-product **index**, promote-to-master, ranking,
  discovery; crawl/ingest that builds the master from OUR sources.
- **S — `recipe-core`, shared (owned by neither)**: the rules/spec themselves,
  the RecipeDoc/data models + serializers, pure utilities with no I/O and no data
  allegiance.

**Hard rule:** content flows one direction only — Corpus → (read) → Local.
NEVER user content → Corpus. And the corpus emits only work-product + envelope +
link; source-expressive content is sealed.

> Legend: ✅ = clear assignment · ⚠️ = flagged, needs your decision (see §6).

---

## 1. Tables

| Table (DB) | Bucket | One-line reason |
|---|---|---|
| `recipes` (recipes.db) | **L** ✅ | The user's own saved recipes — the local store. |
| `master_recipes` (recipes.db) | **C** ✅ | The curated corpus / master tier (user_id=0). |
| `llm_extract_cache` (recipes.db) | **C** ⚠️ | The work-product **index** (spec calls it index, not cache). The *local* extract path currently READS it → that read is the no-bridge violation to cut (§6-B). |
| `dishes` (recipes.db) | **C** ✅ | Dish library + matching/refresh metadata. |
| `dish_run_data_points` (recipes.db) | **C** ✅ | Cohort (DA/PA) used for OU fit / scoring. |
| `dish_rejects` (recipes.db) | **C** ✅ | Curator reject log per dish refresh. |
| `chapters` (recipes.db) | **C** ✅ | Chapter-level OU fit + top-10 snapshot (editorial). |
| `metabase_url` (recipes.db) | **C** ⚠️ | Moz PA/DA/OU authority scores. Read at *local* extract time today — but these are numbers, not content; post-split Local gets DA via the read API (`/grade`), not this table. |
| `domains` (recipes.db) | **C** ⚠️ | Publisher master (editorial). BUT carries `fetch_strategy`/`extract_notes` — capture hints the *Local* extractor needs. The editorial fields are C; the extraction tips may need to be S or mirrored to L. |
| `page_screenshots` (media.db) | **L** ⚠️ | The user's captured view of the source page (his copy). A full-page screenshot is source-expressive, so any corpus-side copy is **sealed-internal**, never emitted. |
| `dishes_vec`, `recipes_master_vec` (recipes.db) | **C** ✅ | sqlite-vec KNN indexes over corpus embeddings. |
| `product_categories` / `product_classes` / `products` | **C** ✅ | Product-review catalog — same "discover & judge" content product as recipes. |
| `bcc_token_journal` (recipes.db) | **infra → split** ⚠️ | Records LLM usage for both L (extract) and C (batch). Each product journals its own spend post-split (two stores). |
| `jobs` (recipes.db) | **C** ⚠️ | Durable async queue. The runner is generic infra, but every current job type is Corpus (dish_refresh, batch, cache refresh). Goes to C unless Local grows its own async needs. |
| `users` (recipes.db) | **infra** ⚠️ | Local has *users*; Corpus has *subscribers*. Likely each product owns its own identity store; or auth is a thin shared lib. Needs a decision (§6-F). |
| `status_messages` (recipes.db) | **infra** ✅ | Rotating UI wait-messages — travels with whichever product's UI uses it (mostly L form). |

---

## 2. Python modules

| Module | Bucket | One-line reason |
|---|---|---|
| `recipe_model.py` | **S** ✅ | THE shared recipe data model + `static_subset` serializer. |
| `sanitize_recipe_data.py` | **S** ✅ | Pure recipe-data normalization. |
| `product_model.py` | **S** ✅ | Shared product data models (commerce analog of recipe_model). |
| `input/pipeline/url_utils.py` | **S** ✅ | Pure URL normalization, no I/O. |
| `input/pipeline/blend.py` | **S** ✅ | The OU/power ranking math — shared so batch and display agree (a *rule*). |
| `input/pipeline/validators.py` | **S** ⚠️ | The is_recipe/gauntlet *rules* are pure logic (the spec calls the gauntlet defs "the real engine" → S). Batch (C) and form (L) both apply them. |
| `extract/jsonld_to_recipe.py` | **S** ✅ | Deterministic JSON-LD→recipe reshaping, no LLM. |
| `to_markdown/markdown_passthrough.py` | **S** ✅ | Pure passthrough adapter. |
| `input/pipeline/image_store.py` | **S** ✅ | Backend-agnostic blob storage (local/S3) — shared mechanism. |
| `recipe_anchor/models.py` | **S** ✅ | The RecipeDoc/step-anchored data spec. |
| `extract/markdown_to_recipe.py` | **L** ✅ | THE extraction LLM call (a "call as call"). |
| `extract/enrich_recipe.py` | **L** ✅ | Enrichment LLM calls. |
| `extract/identity_card.py` | **L** ✅ | Identity-card LLM call (corpus uses the *output* for matching, not the call). |
| `extract/chapter_classifier.py` | **L** ✅ | Chapter classification call at capture time. |
| `extract/dish_signal.py` | **L** ✅ | Lightweight matching-key LLM call. |
| `extract/domain_enrich.py` | **L** ⚠️ | Domain-profile LLM call — but its output feeds the `domains` editorial master (C). Call=L, target=C. |
| `to_markdown/html_to_markdown.py` | **L** ✅ | Web capture → markdown (user/bookmarklet path). |
| `to_markdown/pdf_to_markdown.py` | **L** ✅ | PDF capture (vision). |
| `to_markdown/image_to_markdown.py` | **L** ✅ | Image/screenshot capture (vision). |
| `input/pipeline/screenshot_pipeline.py` | **L** ⚠️ | Captures the user's view; the BLOB store mechanism is shared, but corpus-side use is sealed-internal only. |
| `recipe_anchor/{app,pipeline,render,products,prompts}.py` | **L** ✅ | The step-anchored cook-view tool — pure Local rework engine. |
| `input/pipeline/dishes.py` | **C** ✅ | Dish library CRUD. |
| `input/pipeline/chapters.py` | **C** ✅ | Chapter cohort/fit scoring. |
| `input/pipeline/embeddings.py` | **C** ✅ | Recipe↔dish semantic matching. |
| `input/pipeline/vector_store.py` | **C** ✅ | sqlite-vec KNN infrastructure for corpus. |
| `input/pipeline/grading.py` | **C** ✅ | Exceptionalism grading against OU fit. |
| `input/pipeline/url_scoring.py` | **C** ✅ | Moz authority scoring + metabase_url. |
| `input/pipeline/domains_lib.py` | **C** ⚠️ | Domain editorial master CRUD — but see `domains` table flag (extraction tips needed by L). |
| `input/pipeline/extract_cache.py` | **C** ⚠️ | The index read/write layer. Local must stop reading it (§6-B). |
| `input/pipeline/image_pipeline.py` | **C** ⚠️ | og:image coopt. Spec §149: hosting our own copy is the *Local/user* policy; the *Corpus* public display is thumbnail-and-link. Same mechanism, opposite policy → split by caller. |
| `input/pipeline/refresh_url_metadata.py` | **C** ✅ | Corpus authority-cache maintenance. |
| `intake/build_query_batch.py` | **C** ✅ | The corpus CRAWL front-end (SerpAPI→filter→Moz→rank). |
| `intake/process_batch.py` | **C** ✅ | Corpus batch ingest. |
| `intake/products/*` | **C** ✅ | Product-review corpus ingest. |
| `input/pipeline/auth.py` | **infra** ⚠️ | Permission/role logic over `users`; L-users vs C-subscribers (§6-F). |
| `input/pipeline/config.py` + `bcc_config.json` | **split** ⚠️ | Batch tuning (SerpAPI/Moz/blocklists/RECIPE_PHRASES) = C; save-gate thresholds + recipe phrases = S rule used by L. Needs splitting. |
| `input/pipeline/jobs.py` | **C** ⚠️ | Generic runner, Corpus-only job types today. |
| `input/pipeline/token_journal.py` | **infra → split** ⚠️ | Usage ledger for both; splits per product. |
| `input/pipeline/site_names.py` | **S** ⚠️ | Display utility, but reads the `domains` table (C). Pure-logic part is S; the domains read is a C dependency. |
| `admin_models.py` | **S** ⚠️ | Generic CRUD framework; today only Corpus editors consume it. Framework=S, consumers=C. |

---

## 3. HTTP endpoints

| Endpoint | Bucket | One-line reason |
|---|---|---|
| `POST /extract-from-url` / `-image` / `-pdf` / `-markdown` | **L** ⚠️ | User capture + extraction LLM call. Currently reads `llm_extract_cache` (C) → cut that read (§6-B). |
| `POST /stage-markdown`, `GET /staged-markdown/{t}`, `/stage-image`, `/staged-image` | **L** ✅ | Bookmarklet handoff. |
| `POST /recipes` (save), `GET /recipes`, `GET /recipes/{id}`, `DELETE /recipes/{id}` | **L** ✅ | User recipe store CRUD (user_id>0). |
| `POST /enrich-recipe` | **L** ✅ | On-demand enrichment call on an unsaved recipe. |
| `POST /images`, `POST /images/fetch`, `POST /recipes/{id}/generate-image` | **L** ✅ | User image upload / coopt / AI-gen for his copy. |
| `GET /screenshot/{id}` | **L** ✅ | Serves the user's captured page view from media.db. |
| `GET /r/{id}`, `GET /url-metadata` | **L** ⚠️ | Redirect is L; `/url-metadata` reads Moz scores (C) — post-split Local gets these via the read API. |
| `GET/POST/PATCH/DELETE /dishes/*` | **C** ✅ | Dish library + curation. |
| `GET/POST/PATCH /chapters/*` | **C** ✅ | Chapter editorial + scoring. |
| `GET/POST/PATCH/DELETE /domains/*`, `POST /domains/{d}/enrich` | **C** ✅ | Publisher editorial master. |
| `POST /recipes/similar-master`, `GET /dishes/suggestions`, `GET /dishes/{n}/top-recipes`, `GET /chapters/{n}/top-recipes` | **C** ⚠️ | Discovery — MUST emit only `corpus_public_view()` (work-product + envelope + link), never the source-expressive body (§6-D). |
| `POST /recipes/{id}/promote-to-master` | **C** ⛔⚠️ | **The user→corpus content pipe. Spec §4/§57: CUT.** Replace with an editorial/crawl-only origin. |
| `POST /recipes/{id}/claim` | **C→L bridge** ⚠️ | Copies a full master recipe (static_subset = the body) DOWN to the user. That's the corpus serving source-expressive content down → rework so the user re-extracts, not receives the stored body (§6-C). |
| `GET/POST/PATCH/DELETE /admin/{model}`, `/admin/models`, `/status-messages/active` | **C / infra** ✅ | Generic admin scaffold over Corpus tables. |
| `GET /jobs`, `/jobs/{id}`, `/jobs/{id}/stream`, `POST /jobs/run-queued` | **C** ✅ | Corpus batch/refresh job control. |
| `GET /auth/me`, `GET/POST/PATCH/DELETE /users`, `GET /branding`, `GET /` (health) | **infra** ⚠️ | Auth/identity (§6-F), branding, health. |
| `recipe_anchor` `/recipe/from-url`, `/from-text`, `/render-url`, `/render-doc` | **L** ✅ | Separate Local rework app. |

---

## 4. Jobs / config / scripts

| Item | Bucket | Reason |
|---|---|---|
| `input/pipeline/jobs.py` runner + `jobs` table | **C** ⚠️ | Corpus-only job types today. |
| `intake/build_query_batch.py` + `process_batch.py` | **C** ✅ | The corpus crawl + ingest. |
| `scripts/refresh_expiring_cache.py` | **C** ✅ | Refreshes the corpus index. |
| `scripts/backfill_*` (chapters, grading, identity_cards, master_enrichment, url_scoring, coopt_images, page_screenshots, master_embedding_blob, site_names) | **C** ✅ | All target corpus tables (master_recipes/dishes/embeddings/scores). |
| `scripts/migrate_chapters.py`, `migrate_master_recipes.py`, `populate_domains.py` | **C** ✅ | Corpus migrations. |
| `scripts/run_next_job.py`, `_capture_screenshot_worker.py` | **infra** ✅ | Job-drain helper; screenshot subprocess. |
| `bcc_backup*.bat`, `bcc_start/restart.bat`, `log_config.json` | **infra** ✅ | Ops. Note: recipes.db backup will split into two DB backups (§5). |
| `.env` | **infra (secrets)** ✅ | Each product gets its own keys (spec: "host brings own keys"). |
| `extract/chapter_shortcuts.json`, `domain_display_names.json` | **C** ✅ | Editorial/classification seeds. |
| `bcc_config.json` + `input/pipeline/config.py` | **split** ⚠️ | Batch tuning=C; save-gate + recipe-phrases=S/L. |

---

## 5. UI

| File | Bucket | Reason |
|---|---|---|
| `forms/recipe_form_styled.html`, `bookmarklet.js`, `install.html` | **L** ✅ | Capture/extract/edit form + bookmarklet. |
| `recipe_anchor/` rendered cook view | **L** ✅ | Step-anchored cook view (Local). |
| `forms/dishes.html`, `dishes_v2.html`, `chapters.html`, `domains.html` | **C** ✅ | Corpus curation/discovery editors. |
| `forms/admin.html` | **C / infra** ✅ | Generic admin over corpus tables. |
| `forms/users.html` | **infra** ⚠️ | User admin — which product? (§6-F). |
| `library-shell.{css,js}`, `editor-shell.css`, `forms.css`, `list_control.js` | **chrome (S-ish)** ⚠️ | Shared UI chrome. NOT part of `bcc-core` (which is Python); post-split each product's UI may diverge or share a small UI kit. |

---

## 6. Flagged decisions (these are the load-bearing calls — your approval)

**A. `promote-to-master` ⛔ — the user→corpus pipe.** Spec §4/§57 is absolute:
cut it. Promotion to master must originate ONLY from our editorial/crawl. **Proposed:
delete the endpoint + the user-origin path; master rows come only from the batch.**

**B. The extract cache read by the local tool — the no-bridge trap (this is our
recent work).** `llm_extract_cache` is the **Corpus index** (C). The local
`/extract-from-url` reading it = the corpus serving source-expressive content down
to the user. Spec §171-175: re-extraction is intentional, the redundancy IS the
legal boundary. **Proposed: the index stays in Corpus; the Local extractor stops
reading it and always re-extracts. The speculative fast-path we just built is
removed from the Local side** (the index/screenshot/durability work transfers to
Corpus). Net: cache hits go away for the *user tool*; they remain a Corpus-internal
optimization.

**C. `claim` copies the corpus body down.** It hands the user `static_subset(master)`
— the full ingredients/instructions. Same emit/seal violation. **Proposed: claim
returns work-product + envelope + link and triggers a *local re-extract* of the
source URL to build the user's own copy — never ships the stored body.**

**D. Discovery endpoints must go through one whitelist serializer.** `similar-master`,
`dishes/*/top-recipes`, `suggestions` must emit only `corpus_public_view()` =
work-product ∪ rich-result envelope ∪ mandatory source link; never the sealed body.
**Proposed: build `corpus_public_view()` (whitelist) as the single chokepoint, mirror
of `static_subset`.**

**E. Datastore split (Phase 4, the load-bearing step).** Today `recipes.db` holds
BOTH the L store (`recipes`) AND the entire C corpus (`master_recipes`, `dishes`,
`chapters`, `domains`, scoring, vec). **Proposed: physically separate — a Local DB
(user `recipes` + `media.db` screenshots) and a Corpus DB (everything C). No shared
handle. The only L→C contact is the read API.**

**F. Identity/auth: who owns `users`?** Local has users; Corpus has subscribers.
**Options:** (1) each product owns its own identity store (cleanest separation);
(2) `auth` becomes a thin shared lib but each side has its own user table;
(3) Ghost (already planned) is the shared IdP. **Needs your call.**

**G. `domains` carries both editorial (C) and extraction tips (L needs).** The
`fetch_strategy`/`extract_notes`/`custom_extractor` fields are capture hints the
Local extractor uses; the rest is corpus editorial. **Options:** keep domains in C
and expose the extraction tips via the read API, OR move just the extraction-hint
fields to S/L. **Needs your call.**

**H. Config split.** `bcc_config.json` mixes batch tuning (C) with save-gate +
recipe-phrase rules (S/L). **Proposed: split the file — `corpus.config` vs the
shared rule thresholds in `bcc-core`.**

---

## 7. What `bcc-core` (Bucket S) will contain (Phase 2 preview — not yet built)

`recipe_model.py`, `sanitize_recipe_data.py`, `product_model.py`,
`recipe_anchor/models.py` (RecipeDoc), `url_utils.py`, `blend.py`, `validators.py`
(gauntlet rules), `jsonld_to_recipe.py`, `markdown_passthrough.py`, the shared
config rule-thresholds, and a new `corpus_public_view()` whitelist spec. No I/O, no
L/C imports.

---

## 8. Proposed order of operations after approval
1. **Phase 2** — extract `bcc-core` (S) as an installable package, editable-installed into both sides.
2. **Phase 4 first cut** — separate the datastores (the real boundary) and stand up the read API skeleton.
3. **Cut the bridges** — promote-to-master (A), local cache read (B), claim (C), whitelist discovery (D).
4. **Phase 3** — split product code into two deployables with the static-analysis gate.
5. **Phase 6/7** — prove separation, then lift into two repos + `bcc-core`.

**Nothing above is executed yet. Approve / adjust the buckets and the §6 decisions,
and I'll start with Phase 2.**
