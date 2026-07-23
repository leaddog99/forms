# The domain form, field by field — what each option actually does

A plain-language reference for every control on the **Domains** editor (`forms/domains.html`),
in the order the form shows them. For each field: **what it does**, **when to set it**, and a tag —

- 🟢 **LIVE** — some pipeline / harvest / scoring / extraction code branches on the value.
- 🟡 **CURATOR TOOL** — it does something, but only builds a link/hint a *human* acts on; no
  automated step reads it.
- ⚪ **INERT** — stored and shown back, but **no code reads it to change behavior**. Safe to ignore;
  a candidate for removal (see the last section).

> For *what happens to a candidate URL* once a harvest runs, read
> [recipe-candidate-pipeline.md](recipe-candidate-pipeline.md) (every gate, in order). This doc is
> about the **form knobs**, not the flow. Verified against the code 2026-07-23.

---

## The one thing to understand first: the harvest **Mode** cards

The "Top recipes" section leads with three mode cards — **🟢 Auto·open**, **🔴 Auto·blocked**,
**✋ Curate**. They are *not* separate fields. Each card is a **preset that flips four low-level
switches** for you (`domains.html` `HARVEST_MODES`):

| Mode | fetch_strategy | render_required | check_recipe (verify) | score_only |
|---|---|---|---|---|
| 🟢 Auto · open site   | plain     | off | on  | off |
| 🔴 Auto · blocked site | unblocker | on  | on  | off |
| ✋ Curate · score & pick | unblocker | on  | **off** | **on** |

So if you ever wonder "what does this mode do?" — it does exactly those four things, and you can see
(and override) each one in the controls below the cards. **`check_recipe`** (the *"Verify each is a
recipe"* checkbox) and the mode itself are **per-run** choices sent with the harvest request — only
`fetch_strategy`, `render_required`, and `score_only` persist on the domain row.

---

## Section: Editorial

| Field | Tag | What it does / when to set |
|---|---|---|
| **Display name** | 🟢 LIVE | The friendly publisher name stamped as source attribution on saved recipes (`get_display_map` → `friendly_site_name`). Set it to how you want the byline to read (e.g. "NYT Cooking"). |
| **Story** | ⚪ INERT | A 1–2 sentence editorial bio. Displayed in the form only — nothing downstream reads it today. (Reserved for a future publisher surface.) |
| **Profile (deep research)** | ⚪ INERT | The long researched bio written by **🔬 Deep enrich**. Display-only; no pipeline consumer reads it back. Useful as curator reference. |

## Section: Authority & reach (Moz)

All three are **written** by 🔬 Deep enrich (from Moz V3) and shown here for reference. **None is
read back to drive scoring or selection** — scoring authority comes from `url_scoring`/Moz PA-DA at
harvest time, not these columns.

| Field | Tag | Note |
|---|---|---|
| **Brand Authority (0-100)** | ⚪ INERT | Moz V3 Brand Authority. Reference/display only. |
| **Referring domains** | ⚪ INERT | Moz V3 referring-domain count. Reference/display only. |
| **Ranking keywords** | ⚪ INERT | Top keywords the site ranks for. Fed into the enrich LLM prompt *at write time*, never read afterward. |

## Section: Provenance & extraction

| Field | Tag | What it does / when to set |
|---|---|---|
| **Country** | ⚪ INERT | Shown as a badge only. No pipeline consumer. |
| **Language (ISO 639-1)** | 🟢 LIVE | **Authoritative** for is-recipe scoring on a publisher harvest — it fixes the language the candidate text is scored against (`domain_lang` → the structural/phrase gate). Set it for non-English publishers (e.g. `el` for a Greek site) so the gate uses the right phrase pack instead of per-page guessing. |
| **Cuisine (searchable)** | ⚪ INERT | Despite the "searchable" label, it's a free-text UI filter field only — no scoring/pipeline consumer. |
| **Ethnicity** | ⚪ INERT | No consumer. Recipe ethnicity is derived per-dish from the recipe's own provenance, never from the domain row. |
| **Fetch strategy** | 🟢 LIVE | `plain` vs `unblocker`. `unblocker` routes this host's fetches through the paid Oxylabs unblocker (`get_render_eligible_hosts`, `_fetch_render`). Set via the Mode cards; override here for anti-bot sites. |
| **JS-rendered (render_required)** | 🟢 LIVE | Fetch with a full browser (unblocker `render=True`) — for sites that ship an empty JS shell. Also **auto-learned**: if a harvest has to render-escalate to find a recipe, it sets this for you. |
| **Domain authority (DA)** | ⚪ INERT | Populated + displayed, and even returned by `get_paywall_calibrations`, **but the paywall remap math never uses it** — live DA for scoring is sourced from `url_scoring`, not this column. Reference only. |
| **DA scored (date)** | ⚪ INERT | Timestamp for the above. No consumer. |
| **Extraction notes** | ⚪ INERT | A human hint ("JSON-LD reliable", "needs www for Moz"). Form-only; nothing dispatches on it. |

*(**Custom extractor** and the **fetch-fails** status pill were removed 2026-07-23 — see the INERT-fields section below. There was never a per-domain custom-extractor mechanism wired up.)*

## Section: Top recipes — publisher refresh

The harvest controls. (Mode cards explained at the top.)

| Field | Tag | What it does / when to set |
|---|---|---|
| **Verify each is a recipe (check_recipe)** | 🟢 LIVE *(per-run)* | Runs the is-recipe filter on each candidate. Uncheck for paywalled publishers where a fetch can't see the recipe (the fetch would wrongly fail verify). Set by the Mode; per-run, not persisted. |
| **Skip this publisher (harvestable=0)** | 🟢 LIVE | Blocks the refresh entirely (endpoint 403s). Set when there's no mechanical way to list the site's recipes. |
| **Gated / premium (paywall)** | 🟢 LIVE | Two effects: the harvest **trusts** the site (skips fetch-verify), and a system rescore **PA-calibrates** its link-starved pages up to free-equivalent so they aren't penalized. Set for hard paywalls (NYT, ATK). |
| **Trust extraction** | 🟢 LIVE | Keeps this host's candidates **past the structure gate + the LLM catch** so the full extractor decodes them — for publishers whose recipes have an unconventional layout the cheap gate wrongly drops (e.g. Boston Globe story-format). **Pair with a SEMrush URL filter** that already narrows to recipe pages. See [recipe-candidate-pipeline.md](recipe-candidate-pipeline.md) Stage 2. |
| **Score only** | 🟢 LIVE | Moz-rank the URLs, ingest **nothing**; you pick winners later from the cohort. Set by the Curate mode; persists. |
| **Exclude sections (exclude_words)** | 🟢 LIVE | Space-separated URL path sections to drop outright (`restaurant chef news`) — the publisher's own "not a recipe" taxonomy. Applied before any fetch (`url_excluded_by_domain`). |
| **Recipe URL path (recipe_path)** | 🟢 LIVE | A single path-prefix KEEP filter applied to **every** source (Google + SEMrush file). E.g. `recipes` keeps only `domain.com/recipes/…`. Leave blank for sites with no clean prefix. |
| **Keep top N (keep_top_n)** | 🟢 LIVE | How many winners to keep/ingest per refresh. |

### Discovery source: Google — site: search

| Field | Tag | What it does |
|---|---|---|
| **Query (verbatim, serp_query)** | 🟢 LIVE | The exact Google query for discovery (e.g. `site:bostonglobe.com recipe`). Overrides `recipe_path` for *finding* candidates. |
| **Depth — pages (search_pages)** | 🟢 LIVE | SERP pages to fetch (~10 results each). |

### Discovery source: SEMrush Top-Pages export

| Field | Tag | What it does |
|---|---|---|
| **Records to pull (harvest_records)** | 🟢 LIVE | How many rows to read from the SEMrush `.xlsx`, ranked by traffic/referring-domains (`_read_backlinks_file(want=…)`). *(The agent audit first flagged this dead — it isn't; it flows through the refresh payload exactly like Keep-top-N.)* |
| **Refresh every (days) (harvest_ttl_days)** | 🟢 LIVE | Refresh cadence → drives `next_harvest_at` and the due-today worklist. |
| **Export folder / file path (backlinks_dir)** | 🟢 LIVE | Per-domain override of where the harvest looks for the `.xlsx` (blank = auto-detect newest in the inbox/Downloads). |

### SEMrush deep-link + advanced filter — all 🟡 CURATOR TOOL

These fields build the one-click SEMrush deep-link (`build_semrush_pages_url`) that a **human** clicks
to open SEMrush pre-filtered, then Exports. **No automated harvest step reads them** — the harvest
ingests the exported *file*, not these settings. They exist to make the export step one click.

| Field | Note |
|---|---|
| **Country (semrush_db)** | SEMrush country database (`us`, `gr`, …). |
| **Analyze by (semrush_search_type)** | domain / subdomain / subfolder / url. |
| **Filter / Field / Condition / Word** (`semrush_filter_include` / `semrush_filter_field` / `semrush_filter_criterion` / `semrush_filter_word`) | The advanced-filter row, e.g. *Include · URL · Containing · "recipe"*. Blank Word = no filter. |
| **Uncouple (semrush_url_uncoupled)** | Stop auto-generating the deep-link; use the pasted URL as-is (greys the filter fields). |
| **Deep-link (semrush_report_url)** | The generated (or pasted) SEMrush URL. Derived unless Uncoupled. |

---

## ⚪ INERT fields

These are stored + shown but **no code reads them to change behavior** — the "options that don't do
anything". They split into *aspirational* (plausibly wanted for a future publisher page or scoring
input) and *truly vestigial*.

**Removed 2026-07-23** — the two truly-vestigial ones were dropped from the form + `EDITABLE_FIELDS`
+ `CREATE TABLE` (existing DBs keep the now-ignored columns; harmless):

- ~~**custom_extractor**~~ — deceptive name, zero dispatch. Gone.
- ~~**failure_count**~~ — never even written; was only a display pill. Gone.

**Still present (aspirational — kept on purpose):**

- **story**, **profile** — editorial/reference text, no consumer yet (future publisher page).
- **brand_authority**, **referring_domains**, **ranking_keywords** — Moz enrich output, write-only (future scoring input).
- **country**, **cuisine_focus**, **ethnicity** — display/UI-filter only, no pipeline consumer.
- **extract_notes** — human hint, no dispatch.
- **domain_authority**, **da_last_scored** — displayed, but live scoring DA comes from `url_scoring`.
- **notes** — ops/display.
