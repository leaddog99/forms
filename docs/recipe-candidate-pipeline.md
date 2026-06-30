# The recipe-candidate pipeline — every gate, in plain language

What happens to a single candidate URL from discovery to ingestion, and **every if-test that
changes the flow**. Written 2026-06-26 after the non-English filtering rework.

> **New to this?** Start with [harvest-and-cache-explained.md](harvest-and-cache-explained.md) —
> a high-school-level walkthrough of the *whole* SEMrush→cache journey (page cache, recipe cache,
> the change-detecting fingerprint, and the exceptions). This doc is the gate-by-gate detail.

## TL;DR — does the Google harvest run the same gates as the SEMrush-file harvest?

**Yes.** A publisher harvest has two *discovery sources* — **Google `site:` search** (`source='serp'`)
and a **local SEMrush export** (`source='backlinks_file'`). They differ ONLY in how the candidate
URL list is produced (Stage 0). From Stage 1 on — the cheap pre-filters, the `_is_recipe_filter`
gates, Moz scoring, ranking, keep, and extract-to-master — **both run the identical code**
(`harvest_publisher_top` → `_is_recipe_filter`). So the structural is-recipe gate, the bilingual
phrase scoring, the domain-language resolution, JSON-LD trust, render-escalation, etc. apply the
same to a Google-discovered URL and a SEMrush-discovered URL.

The **dish batch** (a different feature — multi-query Google search for ONE dish across MANY sites)
*also* shares `_is_recipe_filter`, with three differences, called out in §5.

---

## The three entry points

1. **Publisher / domain harvest** — the "Top recipes" refresh on the domain form (and the SEMrush
   inbox scan). Discovers a publisher's notable recipes, filters, ranks, keeps top-N, ingests them
   into `master_recipes`. Sources: `serp` (Google) or `backlinks_file` (SEMrush export).
2. **Dish batch** — multi-query Google search for a specific dish across the whole web; filters,
   scores, ranks a cohort. Shares the filter; different discovery + ranking.
3. **Live single-URL extract** — the form/bookmarklet on one URL. NOT a filter pipeline; is-recipe
   is a *warning/stamp*, not a hard gate (see §6).

---

## Stage 0 — DISCOVERY (produce the candidate URL list)

**Publisher harvest** (`harvest_publisher_top`), by source:
- `backlinks_file` → read the SEMrush `.xlsx`; keep 2xx/3xx rows; rank by **referring Domains**
  (backlinks export) or **Traffic** (Top-Pages export, auto-detected); dedupe URL aliases; take the
  top `records`. **Side effect:** each row's "Top Keyword" is captured into `dish_keywords`.
- `serp` + a verbatim `query` (e.g. `site:bostonglobe.com recipe`) → run it as-is; keep only
  same-domain results.
- `serp`, no query → detect the recipe path (`/recipes`) and crawl `site:domain/path`.

**Dish batch** → union of several verbatim SERP queries (paginated) + any Editor's-Choice pinned URLs.

---

## Stage 1 — CHEAP PRE-FILTERS (no fetch yet)

Applied to the discovered list before any network spend:

- **Archive/taxonomy drop** (`_looks_like_archive`): `/tag/`, `/category/`, `/page/N/`, feeds, admin,
  bare root, date-only archives → dropped.
- **Collection/listicle TITLE drop** (`_looks_like_recipe_collection`): a title whose leading part is
  "… Recipes" or "N Dinners/Projects" (with a unit/time stoplist so "15 Minutes" survives) → dropped.
  English-title fast-path only here; non-English titles are caught later (post-fetch, translated).
- **[if `url_prefilter` on]** *tee-up learn*: this batch's unknown URL tokens are classified once
  (1 Haiku call) into the food/stop word lists, so the food-word skip below knows this site's words.

---

## Stage 2 — `_is_recipe_filter` (the is-recipe gates, per URL)

### Before the loop
- Resolve **base language** = the instance's configured language (`BCC_TARGET_LANGUAGE`, default `en`).
- Resolve **language mode**:
  - *Domain-context call* (publisher harvest passes `domain_lang`, a string incl. `''`): the
    curator-set `domains.language` (normalized, `gr`→`el`) is **authoritative**; `''` (unspecified) →
    base language. Per-page auto-detect is **not** used.
  - *No-domain call* (dish batch passes `domain_lang=None`): use each result's **per-page detected**
    language.
- **[if `keyword_prescreen` on — OFF by default]** one batched Haiku call classifies every
  candidate recipe/not/unsure from slug + Top Keyword; the `not` verdicts are marked to drop
  pre-fetch. (Optional fetch-saver; the structural gate below makes the same call for free after a
  fetch, so this is normally off.)

### Per URL, in order (each `if` can end this URL's journey)
1. **[if cancel requested]** → abort the whole job (`cancelled`).
2. **[if a path section is in the domain's `exclude_words`]** → DROP `domain-exclude` (curator's
   own taxonomy, e.g. `/restaurant/`, `/chef/`). No exploration.
3. **[if `url_prefilter` on AND the path has no food word]** → DROP `url-prefilter` — *but* a small
   random fraction (`url_prefilter_explore_rate`) are let through anyway to mint unbiased labels.
4. **[if the English title looks like a collection]** → DROP `collection-title`.
5. **[if `keyword_prescreen` on AND verdict == 'not']** → DROP `kw-prescreen` (same ε-exploration).
6. **FETCH** (canonical UA-chain → Wayback fallback; or the paid **unblocker** if the domain is
   flagged anti-bot). **[if fetch fails]** → DROP `fetch-failed`.
7. Detect the page language; compute **`_eff_lang`** (= the fixed domain language for a harvest,
   else the detected page language).
8. **[if non-English AND the *translated* title looks like a collection]** → DROP `collection-title`.
9. **[if the page publishes `schema.org/Recipe` JSON-LD]** → **KEEP** (score 100, trusted,
   language-agnostic — no scoring needed).
10. **[if `_eff_lang` has a phrase pack, or `_eff_lang` == base]** → score the **RAW** text against
    **both** the base-language list and the page-language list (bilingual; same language → scored
    once). Then the **STRUCTURAL GATE** decides keep/drop:
    - **[if the text has an ingredients-section marker AND a method-section marker]** (accent-
      insensitive, looking in base + page language) → **KEEP** (`struct`). A real recipe has both;
      a vocabulary-rich guide/tips article has at most one.
    - **[elif the page looks like a thin JS shell AND render is eligible]** → re-fetch once with a
      full browser (unblocker render) and re-evaluate (auto-learns `render_required`).
    - **[else]** → DROP `no-recipe-structure`. (The phrase *count* is still recorded for ranking/
      training, but it is **not** the keep decision — that's what fixed verbose guides false-keeping.)
11. **[else — a non-base language we have NO phrase pack for]** → translate the page text (capped to
    `filter_translate_max_chars` for the filter only) and phrase-**count** score it:
    **[if score ≥ threshold]** KEEP; **[elif render-rescue]** …; **[else]** DROP. **[if translate
    errors]** fall back to a raw count on the untranslated text.

> No-LLM property: for a language **with** a phrase pack (e.g. Greek `el.json`), steps 9–10 use
> zero LLM — JSON-LD check + raw-text phrase/structure scan. Translation (step 11) only happens for
> languages without a pack. With the keyword pre-screen off, a Greek harvest makes **no LLM calls**.

---

## Stage 3 — MOZ + RANK + KEEP (publisher harvest)

- **[if `check_recipe` was OFF]** (paywalled/trusted publisher — a gated page fetches as a stub that
  would wrongly fail verify): **Stage 2 is skipped entirely**. Instead an inline, no-fetch filter
  drops `exclude_words` sections and (if on) no-food-word URLs. Survivors = all candidates.
- **Moz-score** each survivor → PA/DA. **[if Moz returns nothing]** → `MOZ-FAIL`, dropped from the
  scored set.
- **`domain_scoring.score_members`** → system-wide OU/power blend (one global PA~DA fit, paywall-
  remapped PA); **[if no global fit yet]** → raw-PA fallback.
- Sort by `rank_score` desc; mark the top `keep` as `selected=1`. **[if `score_only`]** → mark
  **nothing** selected (the curator picks winners later from the cohort).
- Thumbnail the selected top-N: og:image → **[if none]** a fetch-free SERP-image lookup.

The dish batch ranks differently (per-cohort OU/power percentile blend via the chapter fit), but the
keep/score *shape* is the same.

---

## Stage 4 — EXTRACT WINNERS → `master_recipes` (publisher harvest, job handler)

- **[if `score_only`]** → skip (nothing is ingested; the curator runs "Process selected" later).
- Else, walk selected winners + reserve also-rans (for backfill), and for each until `keep` are
  ingested: `extract_recipe_from_url` → save-gate (`_is_cacheable`, thin-recipe checks) →
  **[if a thin JS page]** render-retry once → `_save_recipe_core` with `_master.kind="top"`,
  `_master.publisher=host` (no `_master.dish`, so it never leaks into a dish top-N).
- **[if a winner fails the gate]** (roundup/thin/paywalled) → replaced by the next reserve; the
  ledger's `selected` flags are re-synced to the actually-saved set.

---

## §5 — How the DISH BATCH differs (also a Google path)

Shares Stage 2 (`_is_recipe_filter`) exactly, but:
1. **`url_prefilter`** defaults **on** for the dish batch (`url_prefilter_dish_batch`, config) vs
   per-domain (default off) for the publisher harvest.
2. **No domain** → language is **per-page detected** (Stage 2 "no-domain call"), not a fixed domain
   language.
3. **Ranking** is the per-cohort OU/power blend for the dish, not the publisher Moz/keep + extract.

## §6 — The LIVE single-URL extract (separate)

The form/bookmarklet POST does **not** run the harvest filter as a hard gate. is-recipe is computed
as an informational **warning** with an optional override; the real gate is the **save-gate**
(minimum ingredients/instructions, thin-recipe 422 with "save anyway"). Translation/extraction of the
full recipe happens regardless of language. (See `project_live_is_recipe_warn`.)

## Quick reference — the flow-changing `if`s
| where | test | effect |
|------|------|--------|
| Stage 1 | archive/taxonomy URL | drop pre-fetch |
| Stage 1 | English collection title | drop pre-fetch |
| Stage 2 | `exclude_words` section match | drop `domain-exclude` |
| Stage 2 | `url_prefilter` + no food word | drop `url-prefilter` (ε-explore) |
| Stage 2 | `keyword_prescreen`(off) verdict=not | drop `kw-prescreen` (ε-explore) |
| Stage 2 | fetch failed | drop `fetch-failed` |
| Stage 2 | non-English collection title | drop `collection-title` |
| Stage 2 | has Recipe JSON-LD | **keep** (trusted) |
| Stage 2 | lang has pack / == base | structural gate (keep if ingredients+method) |
| Stage 2 | thin JS shell | render-escalate + re-check |
| Stage 2 | lang has no pack | translate + count threshold |
| Stage 3 | `check_recipe` off (paywall) | skip Stage 2; inline no-fetch filter |
| Stage 3 | Moz fail | drop from scored |
| Stage 3 | no global score fit | raw-PA fallback |
| Stage 3 | `score_only` | select nothing (curate) |
| Stage 4 | `score_only` | skip ingest |
| Stage 4 | winner fails save-gate | backfill from reserve |
