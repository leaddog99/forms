# Dish search lines from Keyword Magic — the domain loop, applied to keywords

**Status: DESIGN. Nothing built.** Written 2026-10-30 after the Pollo alla Romana
refresh (#2868) and a Keyword Magic pull for the same dish.

## Why

A dish refresh searches the lines a curator typed. Pollo alla Romana's were
`Pollo alla Romana`, `Pollo alla Romana Recipe` and the Italian line. Semrush's
related-keywords report for the dish, US database, says what people actually type:

| keyword | searches / month |
|---|---|
| roman chicken | 480 |
| chicken roman | 320 |
| roman style chicken | 260 |
| roman chicken recipe | 170 |
| chicken alla romana | 110 |
| pollo con i peperoni (+5 Italian variants) | 90 each |
| pollo alla romana recipe — the line we search | 50 |

The term with ten times the demand was never searched. The two-stage rule
([[project_two_stage_selection]]) says the harvest SELECTS on traffic; the lines are
where that selection starts, and today they are guesses. Keyword Magic is the
demand data for the lines, exactly as the Top-Pages export is the demand data for a
publisher's recipes.

## The model: the SEMrush domain loop, verbatim

`docs/semrush-harvest-scheduling.md` settled how we take data from Semrush: the web
subscription is flat, the API is line-metered (the two pulls behind the table above
cost 2,400 units — 1,200 per 20 rows; the account holds ~49k). So **the human presses
Export and the system does everything around the click**:

| domain loop (shipped) | keyword loop (this design) |
|---|---|
| deep link to the Top-Pages report, pre-filled from the domain | deep link to Keyword Magic, pre-filled from the dish |
| curator: open → Export → save the .xlsx | same |
| upload-export control on the domain page lands the file in `input/` | same control on the dish editor |
| `backlinks_file` harvest parses it, is-recipe → Moz → keep top-N | parser → **our** filter → proposals |
| result = recipes on the domain | result = **proposed** lines + aliases; the curator ticks |
| `last_harvested_at` stamps the domain | `keywords_pulled_at` stamps the dish |

**The hands rule (curator, 2026-10-30): every Semrush click is the admin's, and the
result is saved by the admin.** Semrush is very sensitive to client-side bots. No
part of this loop drives semrush.com — no Playwright, no Chrome extension, no
scripted download, no "open and export for me". The system's only contacts with
Semrush are the metered API (probes, and `phrase_these` later if the balance
allows) and reading files the admin saved. That is the same line the domain loop
drew in `docs/semrush-harvest-scheduling.md` §6, and it is the reason the loop is
semi-automated rather than automated.

Two modes for the link and no third ([[project_semrush_filter_codes]]): the generated
default (`https://www.semrush.com/analytics/keywordmagic/?db=<gl>&q=<dish name>`,
written once at create or on first use, thereafter the curator's), or an uncoupled
URL pasted from the Keyword Magic UI after the curator has built the filter there.
We do not generate Semrush's advanced-filter codes.

## Advanced filters: theirs in the browser, ours on the export

Keyword Magic's advanced filter (Volume, KD, Intent, CPC, Competitive density, SERP
features, Include/Exclude words, Word count, Results) is the curator's daily tool.
It is used in TWO places, by the two parties that own them:

1. **In Semrush, by the curator, before Export.** Whatever the UI can express. The
   usual cut for a dish: Intent = Informational, Include `recipe` OR the dish's
   head noun, Exclude the noise words. The uncoupled URL remembers it per dish.
2. **On the export, by us, at ingest.** The export carries every column the UI
   filters on (`Keyword, Intent, Volume, Trend, Keyword Difficulty, CPC, SERP
   Features, Number of Results` — measured on `docs/Recipe_all-keywords_us_2026-08-10.xlsx`).
   `docs/candidate-filters.md` already chose the shape: a list of
   `{include|exclude, field, criterion, value}` conditions, ANDed, that **we**
   evaluate — no undocumented codes, no LLM per row. The same evaluator, on
   keyword rows instead of URL rows.

Default keyword rule (seed; data lives in `system_config`, per [[feedback_no_data_in_code]]):

    keep: intent = Informational
    drop: keyword contains any of  near me · menu · calories · restaurant · delivery
          · nutrition · pizza hut · panera · chick fil a · kfc   (the chain list grows
          from rejections — see States)
    drop: volume < 20
    flag: keyword starts with "best " / "top " / contains " vs "   → TARGET, not a line

The `flag` line is the trap from `docs/dish-candidates-from-keywords.md`: a
comparison term is what our ranked page targets in search, never what we search —
its SERP is roundups the pre-filter discards. Proposals show it in its own column
(`target_keyword` on the dish) so nobody "helpfully" adds `best roman chicken` as a
line.

The 2026-10-30 pull shows why ours is needed even after theirs: the broad-match
report for `roman chicken` returned Panera's "chicken roma asiago bagel stack",
Chick-fil-A in Rome, Georgia, and Alison Roman's chicken, all Informational.

## Data: one table, candidate states, capture-now

`dish_keyword_candidates` (sibling of `dish_keywords`, which captures the Top
Keyword per recipe URL from Pages exports — 362,983 rows tonight — and is a FREE
second source here: the Top Keywords of the recipes already in a dish are proposals
that cost nothing to pull):

| column | |
|---|---|
| dish | FK dishes.name |
| keyword | as exported, lower-cased for the unique key |
| db | Semrush database = the line's `gl` (`us`, `it`, …); `hl` derived at run time as today |
| volume, kd, intent, cpc, serp_features, results, trend | the export's columns, as themselves |
| source | `keyword_magic` / `pages_export` (from `dish_keywords`) / `manual` |
| source_file, captured_at | provenance |
| state | `new` · `line` (became a query row) · `alias` · `target` · `rejected` · `duplicate` |
| reason | free text on reject; the duplicate's matched line or alias |

Unique on (dish, keyword, db). A re-upload refreshes volume/KD on existing rows and
never resurrects a `rejected` row — the same rule the dish-candidates spec set so a
rejected keyword does not resurface every scan.

**Duplicate detection is against the dish's lines AND aliases AND the alias
name-exact rule** ([[project_dish_aliases]]), string-normalized (case, accents,
plural-s). The semantic KNN the dish-candidates spec uses is for *dish* identity;
within one dish the question is only "do we already search this".

## Surface: a strip in the dish editor, nothing new to learn

On `dishes_v2.html`, under the existing **Google search lines** block (the shared
`QueryRows` component, untouched):

    ┌ Keyword Magic ─────────────────────────────── pulled 2026-10-30 · 47 rows ┐
    │ ↗ Open in Semrush   [Upload export…]   ☐ Uncoupled URL [………………………]      │
    │                                                                           │
    │  keyword                 vol    KD  intent  db   →                        │
    │  roman chicken           480    31  Info    us   [+ line] [+ alias] [✕]   │
    │  roman style chicken     260    28  Info    us   [+ line] [+ alias] [✕]   │
    │  chicken alla romana     110    22  Info    us   [+ line] [+ alias] [✕]   │
    │  best roman chicken        70   19  Info    us   TARGET  [set] [✕]        │
    │  chicken roma asiago …   880    —   Info    us   dropped: contains panera │
    │  ▸ 31 more (3 dropped by rule, 2 duplicates of existing lines)            │
    └───────────────────────────────────────────────────────────────────────────┘

* `+ line` appends a `{q, n: null, gl: db, hl: null}` row to the QueryRows block
  (unsaved — the dish's save button owns the dirty state,
  [[feedback_unsaved_state_ui]]). `n: null` on purpose: the spec's rule stands, no
  volume-to-n formula until someone measures one.
* `+ alias` appends to `dishes.aliases`.
* `set` writes `target_keyword` (+ its volume/KD at capture).
* `✕` asks for a one-word reason and sets `rejected`; a reason that is a brand or
  chain name is offered to the global drop list.
* Dropped and duplicate rows stay visible, folded, with the rule that dropped them —
  rank AND explain, never hide the reasoning.

Volume, KD and intent are shown as themselves, no blended score (the curator's
standing objection, recorded in [[project_curate_staff_inputs]]).

## Endpoints (clone the domain ones)

| | |
|---|---|
| `POST /dishes/{name}/upload-keywords` | the `domain_upload_export_endpoint` shape: .xlsx only, `PK` check, 25 MB, lands in `input/`, parses, applies the rule, upserts candidates, stamps `keywords_pulled_at`; returns the proposals |
| `GET /dishes/{name}/keyword-candidates` | the strip's data, grouped by state |
| `POST /dishes/{name}/keyword-candidates/{id}` | `{state, reason}` transitions; `line`/`alias`/`target` are applied by the editor's normal save, this only records the state |
| `GET /dishes/{name}/keyword-magic-url` | generated default or the uncoupled URL |

The parser is `openpyxl` over the first sheet, header-matched by name (the Pages
loader's `col(aliases)` idiom), so a Semrush column rename degrades to "column
missing", never to a wrong value.

## What this is not

* **Not the API.** 1,200 units per dish is the wrong meter when Export is free. The
  API stays what it is today: a probe for me in chat, and `phrase_these` later for
  the dish-candidates scan if the balance allows.
* **Not automatic lines.** Nothing reaches `queries` without a tick. Lines decide what
  gets harvested; the propose-never-create rule applies.
* **Not a SERP substitute.** `phrase_organic` (the ranking URLs per keyword) was
  measured the same night: 9 of the run's 113 live candidates, 38 of its 47 URLs the
  live run never saw — a stored, desktop, non-verbatim snapshot. A possible fallback
  source behind the `serp_search` chokepoint for the days Google blocks us, not this.
* **Not a Keyword Magic generator.** Two link modes, as for domains.

## Build order

1. Parser + rule evaluator as pure functions, tested on the 10,004-row export on
   disk (no DB, no UI) — half a day.
2. Table + the four endpoints + the dish editor strip — a day.
3. Seed the drop list and the default rule into `system_config`; a `Keyword Magic
   due` chip on the dish list once `keywords_pulled_at` ages past the dish's
   `refresh_ttl_days` (the harvest-strip pattern) — later.

First dish to run it on: Pollo alla Romana, then re-refresh and compare the pool
against #2868.
