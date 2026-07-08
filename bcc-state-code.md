the # bcc-state-code

Running state log for the recipe forms project. Append-only style; prune as items complete.

## Interesting links

- https://claude.ai/public/artifacts/fd58ba67-876d-47fc-9610-561ada60639f — TBD context (logged 2026-05-13)
- **[docs/harvest-and-cache-explained.md](docs/harvest-and-cache-explained.md)** — plain-language (high-school level) end-to-end walkthrough of the SEMrush harvest → page cache → recipe cache → change-detecting fingerprint → cache refresh, with a full exceptions table. The gate-by-gate detail is in [docs/recipe-candidate-pipeline.md](docs/recipe-candidate-pipeline.md).

---

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
