# Recipe scoring — design

> Status: **DESIGN / NOT BUILT.** The shipped system is `docs/domain-scoring.md` (OU/power
> blend, two-stage harvest).
> **Rewritten 2026-08-04** to be current — earlier drafts carried claims that testing has since
> killed. Those are in **§12 Disproved** rather than left in the body.
> Memories: `project_two_stage_selection` (gospel — read first), `project_selection_lens`,
> `project_domain_scoring`, `project_ou_power_blend`, `project_paid_pa_calibration`.

**Read `project_two_stage_selection` first.** Selection is last month's TRAFFIC; ranking is OU.
Judging either in isolation gives wrong answers — that is how two of the disproved claims got
written.

Every claim is marked **MEASURED** (we ran it) or **ASSUMED** (reasoning, untested).

---

## 1. Two objectives, two filters

Curator: *"I want a library of the best tried and true recipes discoverable via the pa/da
stats... and separately the up and coming exciting new recipes. Two different filters on the
extraction, flagged as such."*

Not one score with archetypes — **two pipelines**, reading different signals, disagreeing by
design, stamping which one found the row.

| | **A. LIBRARY** (tried and true) | **B. RISING** (up and coming) |
|---|---|---|
| question | has this EARNED its place? | is it ABOUT to? |
| primary | OU / excess PA — advocacy | `Or` slope — keyword breadth growth |
| supporting | durability, capture, like-month momentum | poised pool, `Ot`/`Or`, answer engines |
| floor | artifact soundness | artifact soundness |
| **EXCLUDES** | `Or` velocity — a spike is not a credential | **PA / OU** — a new page has none by construction |
| flag | `selection_lens='library'` | `selection_lens='rising'` |

**They cannot be blended.** Advocacy can never surface a three-week-old recipe; keyword velocity
demotes a stable decade-old classic.

---

## 2. Vocabulary

* **Appetite** = traffic. Something caught people's eye and they wanted to try it. Nobody
  *needs* a recipe.
* **Advocacy** = excess PA. Having found it, someone thought it worth pointing others at.
* **Breadth** = `Or`, how many queries the page ranks for.
* **Conversion** = `Ot`/`Or`, whether that breadth produces visits.

Appetite and advocacy are **orthogonal — MEASURED** (smittenkitchen, 114 pages: 30 hi/hi, 27
hi-traffic/lo-PA, 21 lo/hi, 36 lo/lo). Neither subsumes the other.

---

## 3. How a page gains keywords — MEASURED

**`Or` is not a list Google keeps.** Semrush tracks millions of queries, samples Google's top
100 for each, and counts how many your url appears in. A third party's sampling, counted from
the query side.

Porridge's 224 keywords, pulled from both ends of the ladder (2026-08-04, 2,400 units):

```
positions   1-3     25 keywords
            4-10    84
           11-20    36
           21-75   ~30
             76+     0        <- nothing beyond 75
```

**Keywords ARRIVE already ranking decently — mostly 4-20 — not at the bottom of a ladder they
climb.** Google widens the set of queries it treats the page as a good answer for and places it
reasonably on each. Semantic matching, not rank creep.

**ASSUMED (standard CTR curves, not measured here):** positions 4-20 produce very little
traffic; essentially only the top 3 pays. If so, a page can hold 200 keywords at 4-20 and show
near-zero traffic — porridge at `Or 84, Ot 13` — with traffic arriving as some cross into the
top 3. That would make `Or` and `Ot` two stages of one process: **breadth of relevance, then
winning the slot.**

The late tail is marginal by nature: `recipe for gruel` (pos 75, Nq 90), `oat gruel` (67, Nq
210).

**Two things move `Or` that say nothing about the page:** query-side seasonality (a Christmas
term leaves the tracked set in July), and Semrush changing its own keyword database.

**Do not use `url_organic`'s `Tr` column.** MEASURED 2026-08-04: it reconciles with neither the
page nor the domain as a denominator (a 12-keyword page returned 7 rows summing to 10.05, with
`Traffic(%)`=0.07 and page traffic 149), and per-row values match no CTR model. Meaning unknown;
unusable until Semrush explains it.

---

## 4. The Rising metric — MEASURED, and weak

    rising_score = Or_log_slope x R2

      slope   least-squares slope of log10(Or+1) per month
      R2      fit quality
      keep    slope > 0 AND R2 > 0.7
      prune   slope < 0 over the last 3 points
      rank    BY the score — continuous, never a threshold

```
page                          window   Or slope    R2      Ot slope   R2
porridge  (breakout)          6mo        +0.083  0.98       +0.167  0.37
shortbread (mature/seasonal)  6mo        -0.170  0.91       -0.038  0.26
```

**R2 settles `Or`-vs-`Ot` with a number.** Porridge's TRAFFIC slope is `+0.167` at **R2 0.37**
— the 1,383 -> 383 spike-and-dip, untrustworthy. Its KEYWORD slope is `+0.083` at **R2 0.98**.
Same page, same window: one is noise, one a straight line.

Slope removes every cliff-edge rule. Porridge's March traffic dip does not dent a positive `Or`
slope, so "prune on negative slope" cannot kill it two months before it takes off — a
traffic-based rule would have.

### How well it works — THE EXPERIMENT

seriouseats.com, three Organic Pages exports (Jun 21 / Jul 21 / Aug 03), **n = 5,064** pages in
all three. **Zero API units.** Rerun: `python -m scripts.keyword_lead_experiment <A> <B> <C>`.

Design avoids the obvious trap: taking known breakouts and confirming keywords rose first
conditions on the outcome. The cohort is EVERY overlapping page, so cases where keywords rose
and nothing happened are counted. And `Or` must beat a null, since traffic's own past predicts
its future.

```
corr( dOr Jun->Jul , dOt Jul->Aug )  = +0.1162     the hypothesis
corr( dOt Jun->Jul , dOt Jul->Aug )  = -0.2330     the null
PARTIAL corr( dOr | dOt )            = +0.1865     unique contribution

deciles by dOr:  worst 29% grew  ->  best 51% grew   (monotonic across all 10)
dOr > +0.10:     52% grew vs 43% baseline
```

**The hypothesis survives WEAKLY.** Ten monotonic deciles on n=5,064 is not noise, but `Or`
explains only **~3-4% of variance** and 52-vs-43 is a 1.2x lift. So `Or` slope is a **ranking
signal for a watchlist** — ordering candidates you re-check anyway — **not a confident breakout
detector.** Never promote on it alone.

**The most valuable result is the null: traffic momentum is INVERTED (-0.233).** A page that
gained traffic last month tends to lose it next. **Ranking risers by traffic change performs
worse than random.**

Bounds: one publisher; a 13-day outcome window against a 30-day predictor (attenuates the
effect); pages already ranking for something. The 21 August export gives a clean month-on-month
rerun, free.

---

## 5. The watchlist — discovery free, refresh cheap

| step | cadence | cost |
|---|---|---|
| **discover** — `Traffic == Traffic Change` | harvest schedule (90-180d) | **free** |
| **refresh** — `url_ranks` -> `Ot`, `Or` | **monthly** | **10 units/url** |
| **prune** — slope turned negative | monthly | free |
| **judge** — slope, R2, poised pool | batch | free (stored) |

**`Traffic == Traffic Change` IS the "New" filter — MEASURED.** Equality means last month was
zero. christinascucina export: 820 rows, **26 flagged new**, porridge correctly NOT flagged
(8,877 vs 7,855) because it already had traffic. The UI's New/Lost tabs compute the same thing;
**the API has no "new" flag** — that needs two months compared, which no single call does — so
the diff is ours either way. It is free either from a downloaded file or from two stored
`domain_organic_unique` pulls.

> **CORRECTED 2026-08-09.** This paragraph previously read "the API cannot (`display_filter`
> ignored on `domain_organic_unique`, `display_date` 403)". **Both halves were wrong.**
> `display_date`'s 403 was a wrong type name (already corrected in §10/§12), and
> `display_filter` **is honoured** — see §10. Only the *New* comparison is genuinely
> unavailable, because it is a two-month diff rather than a filter.

```
one publisher's new pages   ~26 urls        260 units/mo
30 publishers              ~780 urls      7,800 units/mo
300-url steady state        300 urls      3,000 units/mo
```

Self-limiting, because pruning caps the list.

**Screen new arrivals by breadth before spending:** `earl-grey-cookies` arrived with 54 keywords
on 15 traffic; `kumquat-cupcakes` with 2. Only the broad ones earn a slot.

**Build the series, don't buy it.** `url_rank_history` with `display_limit=12` is 600 units/url
(8,750 unbounded). A page caught at birth needs no retroactive history; reserve the backfill for
a MATURE page that suddenly matters.

**This decouples the clocks** — discovery at harvest cadence, watchlist monthly, the expensive
fetch/Moz/extract pipeline on its own schedule. `harvest_ttl_days=90` on 295/311 domains stops
being a blocker.

---

## 6. Execution — where a riser dies today

Curator: *"the current extract filters by pa/da... the new items aren't there, therefore there's
no recipe to process but 'old' recipes."* **Two independent gates:**

```
1. read export, sort by traffic desc, take top records=100   <-- DEAD. never a candidate
2. pre-filter taxonomy / archive urls
3. recipe filter - FETCH each candidate (unblocker), check JSON-LD
4. Moz score each survivor -> PA/DA/OU
5. domain_scoring -> rank_score
6. sort by rank_score, keep top `keep` (10-40)               <-- DEAD AGAIN. PA ~0
7. extract winners -> master_recipes
```

Porridge in Nov 2025 (`Ot=7, Or=14`) sat around row 400 of 820. Steps 3-4 SPEND on 100 pages
before selecting, so widening the funnel is not affordable.

**Four phases instead:**

1. **CAPTURE** (harvest time, free) — read the export at FULL DEPTH, append a snapshot per url:
   `(url_normalized, captured_at, traffic, or_count, traffic_change, traffic_pct,
   answer_engines, llm_prompts, top_keyword, primary_intent)`. No fetch, no Moz, no LLM. The
   top-100 pipeline is untouched. **Today those columns are parsed then DISCARDED, and
   `replace_members` is delete-and-replace, so every re-harvest destroys the history.**
2. **JUDGMENT** (scheduled job — precedent `domain_scoring`) — free SQL screen on slope and R2,
   THEN pay `url_organic` for survivors only. Must be a batch: metrics are cohort-relative,
   slope spans runs, and tuning has to be free.
3. **PROMOTION** (triggered only) — fetch, recipe filter, artifact floor, extract with
   `selection_lens='rising'` and `_rising.triggeredAt`.
4. **MAINTAIN** — update; **graduate** (flip to `library`, set `graduatedAt`); **retire**.

**Rising is CHEAPER per candidate than the current extract**, because it screens on free stored
data and pays only for survivors.

**Limit:** a page with zero rankings is invisible at any export depth. A recipe published last
week is in no export at all and stays tier 3.

---

## 7. Storage — the ledger, then a promotion gate

**No second recipe table.** `collection_members` already holds 10,276 discovered-but-not-promoted
urls, and the harvest already promotes winners.

```
collection_type='rising'  ->  ledger row, metrics only, no recipe content
          |  graduation gate
          v
master_recipes            ->  extracted recipe, selection_lens='library'
```

Master stays the library **by construction** — promotion is the gate.

**Against a second table:** a graduating recipe is the SAME recipe (a migration every time); the
same url found by both filters gives two rows for one recipe (the caponata 5515/5516 bug); the
enrichment is expensive and shared (`_cook` ~$0.44 Opus, `_identity`, `_measurements`,
screenshot); and `_master.kind` (4,040 `top`, little else) plus `collection_type` (only
`publisher`) already exist for this.

**Purity structural, not disciplinary:**

```sql
CREATE VIEW library_recipes AS
  SELECT * FROM master_recipes WHERE selection_lens = 'library';
```

with `selection_lens` as a generated column so the filter is indexable.

**Promotion gate** — a bet, not a credential: positive `Or` slope with good R2, AND a poised
pool worth having, AND `Ot`/`Or` lifting, AND artifact soundness. Non-converters stay ledger
rows costing nothing.

---

## 8. Record shape — two blocks

```jsonc
"_scoring": { ...RAW measurements, ONE copy... },

"_library": { "ouScore":6.28, "excessPA":6.3, "capture":0.0011,
              "momentumYoY":1.04, "durabilityYears":16,
              "verdict":"evergreen", "computedAt":"...", "cohort":"dish:Pasta Vongole" },

"_rising":  { "orSeries":[224,185,168,139,107,84], "orSlope":0.083, "orR2":0.98,
              "poisedPoolNq":40410, "convertedPct":0.84, "otPerOr":46.7,
              "answerEngines":["google","google-ai"], "llmPrompts":26,
              "triggeredAt":"2026-01-15", "graduatedAt":null }
```

1. **Raw in `_scoring` (one copy), derived in the lens blocks.** Two copies of `Ot` will drift —
   the failure that put a customer front door over the admin surface (2026-07-31).
2. **`triggeredAt` makes Rising honest.** Porridge qualified in January and by July was merely
   popular; without it you cannot tell a discovery from a late notice.
3. **Both blocks are COHORT-RELATIVE.** `_scoring.fieldScope` is empty on every row today —
   that problem, unresolved. Stamp the cohort or the number is unreadable later.

Build costs: virtual generated columns (else invisible to SQL) and the four-edge rule
(`feedback_db_form_sync`) across ~20 fields.

### OPEN — does the VERDICT belong on the recipe at all?

A recipe can be Rising for *Porridge* and ordinary for *Breakfast*. **Metrics** describe the page
and belong on the recipe; **verdict + lens flag** describe the page IN a collection and may
belong on `collection_members`, which already carries `rank`/`selected`/`rank_score` per
collection. Same shape as a single `_master.dish` stamp before membership became a junction
([[project_dish_variants_membership]]).

---

## 9. Demand and capture

    capture = page traffic / keyword search volume

Traffic alone conflates "big dish" with "winning page" — all MEASURED:

```
how-to-hard-boil-an-egg       Ot 24,625   head query Nq 246,000   -> tiny capture
country-chicken-stew          Ot     37   Nq 18,100 at position 30
linguine-and-clams-vongole    Nq 40,410 in reach, 0% converted, every keyword on page 2
how-to-make-shortbread        Ot 9,616 (us) + 8,328 (uk)   positions 1,2,3,1,1,3,1,1
```

Volume is keyed on the DISH, so unlike traffic it does not depend on the publisher having been
harvested.

---

## 10. What the APIs actually do — MEASURED 2026-08-04

`scripts/semrush_url_probe.py`. **Prefer Semrush over Google Ads**: same vendor, no
developer-token application, no ad-spend requirement.

| want | call | cost | status |
|---|---|---|---|
| traffic + keyword count, ANY url | **`url_ranks`** -> `Ot`, `Or` | 10 units/line | works |
| a url's keywords + volume | `url_organic` -> `Ph`, `Po`, `Nq` | 10 units/line | works |
| monthly series for a url | **`url_rank_history`** -> `Dt`, `Ot`, `Or` | 50 units/line | works |
| a domain's full page list | **`domain_organic_unique`** | 10 units/line | works |
| a domain's monthly series | `domain_rank_history` | 50 units/line | works |
| **filter a domain's page list by traffic** | `domain_organic_unique` + `display_filter` | 10 units/line, **filtered rows only** | **works — MEASURED 2026-08-09** |
| "New pages" filter | — | — | **not available** (a two-month diff, not a filter) |
| multiple domains per call | — | — | **not available** |

**`display_filter` IS honoured on `domain_organic_unique` — MEASURED 2026-08-09**, contradicting
an earlier note that said it was ignored. Test: pinchofyum.com sorted `tg_asc` (ascending by
traffic). Unfiltered the bottom rows are `0, 0, 0, 0, 0` — the dead tail. With
`display_filter=+|Tg|Gt|1000` the bottom rows become **1001, 1006, 1013, 1021, 1022**. Cost 100
units total.

This is the difference between paying for a publisher's whole page list and paying only for the
part above a traffic floor — pinchofyum 1,661 rows vs **451** at ≥100/mo; seriouseats 7,099 vs
**3,154**. Combined with `display_offset` (which pages past the export's 10,000-row cap, hiding
allrecipes' true depth) it makes the manual search/save/process export dance replaceable:
**~50,000 lines ≈ 500,000 units for a full ~96-publisher refresh at a 100/mo floor**, against a
2M-unit minimum package — roughly four cycles a year, which matches `harvest_ttl_days=90`.

The filter also turns skim depth from a judgement call into a parameter: the traffic floor IS
the `records` decision.

**Gotchas, each paid for once:**

* the type is **`url_ranks`**, NOT `url_overview` — the docs page is TITLED "URL Overview" and
  `url_overview` returns *"query type not found"*;
* history is **`url_rank_history`**, NOT `url_ranks` + `display_date` — the latter returns
  *"ERROR 403 :: History reports are not allowed"*, a wrong type name producing a permissions
  error;
* `url_rank_history` returns **175 months** unless `display_limit` is set — 8,750 units for ONE
  url;
* `url_organic` bills **per keyword line**, and its `Tr` column is unusable (see §3);
* **`url_ranks` is variant-sensitive** — our slash-stripped `url_normalized` returns NOTHING;
* an unindexed url returns `ERROR 50 :: NOTHING FOUND`, not a zero row. A three-day-old article
  is invisible to Semrush AND Moz.

**Verified equivalence:** export `Traffic` == API `Ot` (shortbread 9,616 in both; UI shows
9.6K), and `Number of Keywords` == `Or` (741 in both).

### Costs

Semrush does not publish a per-unit price (packages 2M/5M/10M/20M, Business-tier add-on).
Third-party figures cluster near $50/M; **units are exact, dollars indicative.** Units expire
monthly, no rollover. 10 req/sec.

```
current traffic (Ot), whole corpus 4,169      41,690 units
keywords + volume, top-10/url, corpus        416,900
one domain's full page list (807 rows)         8,070
12-month history, one url                         600
watchlist refresh, 300 urls                     3,000/mo
```

---

## 11. Three export fields we discard today

`Traffic Change`, `Answer Engines` (`google-ai`, `gemini`, `search-gpt`), `LLM Prompts` — plus
`Number of Keywords`, the single most important field for Rising. All parsed and dropped.
Answer-engine visibility is a distribution channel nothing in the current scoring reads.

---

## 11b. PA provenance — which authority scores are real — MEASURED 2026-08-04

Until 2026-08-04 `score_url_via_moz` computed `usable` — whether Moz held data for a URL — and
never consulted it on the way out. A URL Moz had never crawled returned the **domain-derived
placeholder PA** it ships with, and that was stored as if measured. Fixed; the fix stops new
fabrications only.

**Scope, measured, not estimated.** A 60-row sample re-scored through the corrected gate:

| | fabricated PA |
|---|---|
| random sample of the ranked corpus (n=60) | **5%** (3) |
| **top 200 by OU — exhaustive, not sampled** | **0%** (0) |

The earlier **15%** figure was measured through the *broken allow-list* version of the gate
(`http_code in (200,301,302,402)`), which rejected real Moz answers — the same defect that made a
pinchofyum harvest bill 496 rows and score none. Two-thirds of "fabricated" was that regression.
Extrapolated true scope: **~210 rows of 4,179**.

**Why the top is clean, structurally.** The placeholder is derived from DA, and OU measures PA
*against* DA — so a fabricated row lands near the **OU = 0 line** by construction:

```
id     host                  stored PA    DA   stored OU   PA for OU 0
1470   bostonglobe.com            41.0  91.0      -5.041          46.0
1481   cooking.nytimes.com        49.0  95.0      +1.749          47.3
                                       corpus median OU = 11.05
                                       top-200 floor    = 18.04
```

A fabricated row is ~18 OU points short of the top 200. It cannot climb; it parks at par.

**DECISION: do not strip.** The case for clearing these values was ranking contamination, and
there is none — they already sort to the bottom, which is where clearing would also put them.

Two limits stated honestly. The top-200 test selected *by stored OU*, so it proves no fabricated
row is wrongly **in** the top; it cannot prove none is wrongly **out**. And the wrongly-out
direction is real and visible above — Boston Globe at OU −5 against a median of 11. These hosts
are uncrawled because they **block crawlers**, not because nobody links to them, so the curator's
earlier rule ("if it can't crawl them they probably aren't popular enough to matter") does not
hold for this subset. They are the same gated-publisher population **§paid-PA calibration** exists
to rescue. Stripping would delete the rows that work needs and make the exclusion permanent.

**The defect is not that the value exists — it is that it is indistinguishable from a measured
one.** So: provenance, not deletion. `_scoring.mozHttpCode`, exposed as a generated column
`moz_http_code` on both recipe tables and a column on `metabase_url`:

| value | meaning |
|---|---|
| `NULL` | scored before the fix — provenance **unverified** |
| `0` | verified: Moz has no data, the PA **is** the placeholder |
| `> 0` | verified measured |

`moz_http_status(url)` probes the code without producing a score (kept separate from
`score_url_via_moz` so no caller can opt back into scoring an uncrawled URL). It is the one
`_scoring` field where **0 is a real measurement**, so it is deliberately excluded from
`_sanitize_scoring`'s zero-strip list.

`scripts/verify_pa_provenance.py` stamps existing rows — never touches the PA, resumable (`NULL` is
the worklist, no-answer rows stay `NULL`). **~$10 corpus-wide**: a measured URL costs 1 Moz row but
a fabricated one costs ~5, because the learned single-variant probe finds nothing and self-heals by
re-expanding to all four variants. Two round trips each makes it slow — budget minutes per hundred.

First run, bostonglobe's 32 rows: **30 measured, 2 fabricated (6.2%)** — ids 1470 and 3277, the same
two the random sample flagged. One of the 30 returned **`http_code 1`**, a third non-standard-but-real
code after pinchofyum's `5`; independent confirmation that `bool(code)` is the correct gate and the
`(200,301,302,402)` allow-list was discarding real measurements.

### 11b.1 "No Moz data" was often OUR bug, not Moz's — MEASURED 2026-08-06

Two defects made pages look uncrawled when Moz held them all along. Both inflate the
fabricated/uncrawled population above, so counts taken before this date read high.

**1. We asked about non-canonical URLs.** The stored URL is whatever the bookmarklet grabbed —
a mobile path from a phone, an AMP page, a share sheet's tracking query. Moz indexes the
canonical page and has never seen those strings:

```
cooking.nytimes.com/recipes/1025200-…?unlocked_article_code=…&smid=ck-recipe-iOS-share   NO DATA
cooking.nytimes.com/recipes/1025200-…                                    pa=57 da=95 ou=+9.75
williams-sonoma.com/m/recipe/stanley-tucci-chicken-cacciatore.html                       NO DATA
williams-sonoma.com/recipe/stanley-tucci-chicken-cacciatore.html         pa=41 da=82 ou=-2.24
```

Fixed in `_canonical_form()`, which adds the cleaned URL as an EXTRA probe candidate.
`url_normalized` — the dedup and cache key — is deliberately untouched.

Guards, because the failure mode is silent over-scoring: tracking keys are an **allow-list**
(`utm_*`, `smid`, `fbclid`, `unlocked_article_code`, `token`…), since sites use the query string
as real page identity (`?p=123`) and stripping one asks about a different page; only a **leading**
`/m/` is removed; and a canonical form that collapses to the site root is **refused outright**,
because attributing a homepage's PA to a recipe is worse than having no score. An already-canonical
URL adds nothing, so ordinary scoring still costs 4 variants.

**2. A usable variant could lose a tie-break.** `_pick` sorted candidates into `crawled`
(200/301/302) and `estimated` (402) and dropped everything else into a generic pool ranked by PA.
Moz also returns codes **1, 3 and 5** with real metrics, so those fell to the generic pool — and
when PA tied across variants, `max()` broke the tie by list order and could return a code-0 row,
which then failed the usable gate. Observed on `travel-gourmet.com/…/stufato-di-pesce-…/`: all 8
variants pa=21, only the trailing-slash form carried `code=3`, and the page scored nothing.

Fixed by adding a `has_data` tier (**any** non-zero code) above the generic pool. **This is the
`(200,301,302,402)` allow-list mistake for the third time** — first as the `usable` gate that
rejected pinchofyum's `code=5` (496 rows billed, 0 scored), then as the inflated 15% fabrication
estimate, now as a tie-break. Those four codes RANK known tiers; they have never been the
definition of "has data". That is `bool(http_code)`, and nothing else.

### 11b.2 The zeros were manufactured by the CONTRACT — fixed 2026-08-06

Curator: *"i've had zero scores many times."* Correct, and the recurrence was the tell. Zero-carrying
saves by month: **May 3% · Jun 10% · Jul 15% · Aug 94%**.

`ScoringMetadata` declared every numeric field `= 0.0`. Every extract passes through `RecipeModel`,
so the model **manufactured a full block of zeros on every recipe**, which then read as measured
everywhere downstream. `sanitize_recipe_data` re-inserted PA/DA/OU as a second manufacturer.

Three fixes had been applied at the SINK and none at the SOURCE — each deleting values the model had
just created:

| when | fix | where |
|---|---|---|
| 07-30 | `power`/percentiles written as 0 | `pre_scored_from_entry` — the writer |
| 08-03 | zeros persisted on save | `_sanitize_scoring` — the save boundary |
| 08-06 | that fix had NEVER RUN | service restart (NSSM, no `--reload`, 3 days stale) |

The August spike is the stale service; the May–July baseline is the model leaking through.

**Three distinct states were collapsed into one `0`:** NOT APPLICABLE (a handwritten recipe mints a
`/r/<uuid>` self-URL — nothing can link to it, so PA is meaningless), NOT MEASURED YET (a real page
Moz hasn't crawled), and NO COHORT (saved outside a dish batch, so the field statistics have nothing
to rank within). `None` expresses all three; `0.0` expresses none. Of 70 affected rows: 11 / 16 / 41
respectively, plus 2 non-recipe links.

Fields flipped to `Optional[... ] = None`. **Strings keep `""`** and **`recipeScore`/`recipeScoreThreshold`
keep `0`** — the validator sets them on every extract and the threshold is used in `>=` comparisons
where `None` genuinely would break.

**Audited before flipping** — every reader already tolerated absence: `setScoreChip` renders `—` for
null, `blend._power` returns None unless both operands are numbers, `enrich_recipe` guards with
`is not None`/truthiness, `chapters.py` filters `IS NOT NULL`, `dishes.py` wraps in `COALESCE`.
Nothing consumed the zeros. `exclude_none` was deliberately NOT added at the two `model_dump` sites —
it is model-wide and would drop unrelated fields; `null` reads identically to absent for every
consumer above, and `json_extract` yields SQL NULL either way.

Also caught by the same audit: **`mozHttpCode` was never declared on the model**, so pydantic silently
dropped the provenance flag on every extract that round-tripped it — written by `url_scoring`,
surviving the batch and save paths, and vanishing at validation. Now declared.

**Consumers must now exclude `moz_http_code = 0` from any fit over PA** — the paid-PA calibration
first, since fabricated rows are concentrated in exactly its population and would drag the
shift-scale remap toward the placeholder.

---

## 11c. PA ranks ENDORSEMENT, not DEMAND — and within a publisher it is the only ranker — MEASURED 2026-08-12

§12 records that `corr(PA, log external links) = +0.905` within a domain. That stands. PA is
an excellent measure of third-party endorsement. **This section is about the other question:
how well PA predicts whether anyone reads the page.**

Measured against SEMrush per-URL traffic already stored on `collection_members`, within each
publisher (so DA is held constant by construction):

| domain | n | corr(PA, traffic) |
|---|---|---|
| toriavey.com | 222 | +0.425 |
| allrecipes.com | 2,000 | +0.362 |
| bonappetit.com | 250 | +0.263 |
| bbcgoodfood.com | 319 | +0.217 |
| recipetineats.com | 953 | +0.211 |
| budgetbytes.com | 984 | +0.202 |
| eatingwell.com | 250 | +0.168 |
| skinnytaste.com | 217 | +0.158 |
| loveandlemons.com | 249 | +0.126 |
| cooking.nytimes.com | 391 | +0.104 |
| latimes.com | 246 | +0.087 |
| bostonglobe.com | 250 | +0.015 |
| dianekochilas.com | 97 | **−0.012** |

**Median +0.185 across 12 publishers and ~6,300 pages — PA explains roughly 3% of
within-site traffic variance** (r² ≈ 0.034). No publisher exceeds +0.43.

Both facts are true and compatible: PA tracks *links* almost perfectly and *demand* barely at
all. **Links are not demand.** A page can be widely cited and rarely cooked, and — far more
commonly — widely cooked and never linked, because home cooks do not run blogs.

### Why this is load-bearing rather than a curiosity

    rank_score = 0.70 · pct(OU) + 0.30 · pct(power)
    OU    = PA − bar(DA)
    power = DA + PA

Within one publisher **DA is constant.** So `OU` is `PA` plus a constant, `power` is `PA` plus
a constant, and both percentiles are monotone in PA. **Within a domain, 100% of `rank_score`
is PA** — the 70/30 blend collapses to a single axis. Publisher selection therefore orders a
site's pages by a signal that explains ~3% of their demand.

This does **not** impugn the blend between publishers, where DA varies and the two axes are
genuinely independent — that is the two-stage rule (§1) and it is not in question. The defect
is narrower and worse: a between-site metric is being asked to rank *within* a site. It is
also exactly what the traffic-exceptionalism work predicted — 71% of traffic variance is
between-SITE, so within-site is precisely where PA has least to say.

### What it costs — dianekochilas.com, 97 pages, DA 49 throughout

| page | traffic | PA | rank_score | selected |
|---|---|---|---|---|
| Greek Baklava | **698** | 31 | 0.211 | no |
| Detox water | 313 | 29 | 0.157 | no |
| Broccoli & cauliflower salad | 207 | 29 | 0.157 | no |
| Classic Dolmades | 206 | 29 | 0.157 | no |
| Greek Salad | 157 | 35 | 0.351 | no |
| Classic Moussaka | 149 | 32 | 0.243 | no |
| Octopus with Orange & Olives | **21** | 40 | 0.573 | **yes — #1** |
| Tsoureki | 20 | 40 | 0.573 | yes |
| Skordostoumbi | 11 | 40 | 0.573 | yes |

It rejected baklava, moussaka, dolmades and Greek salad — the canonical dishes of the cuisine —
and selected octopus with orange and olives at 21 visits.

**Three alternative explanations were tested and ruled out**, which is what makes the PA
reading solid rather than a story:

* *A paywall penalty.* The domain is flagged `paywall=1`; `pa_gap_v1` returned `inconclusive`
  (gap +0.6, effect 0.09 against a needed 1.0). Not starved. The flag itself looks wrong —
  the site is not gated.
* *A DA penalty.* Real but separate, and it caps the *site*, not the ordering within it: DA 49
  → power 89 → the 40th corpus percentile, so nothing on the domain can exceed ~0.80 without a
  97th-percentile OU page. That explains the low ceiling; it does not explain octopus over
  baklava.
* *Split authority — recipes syndicated to PBS.* **Falsified.** pbs.org carries no recipes of
  hers; the only *My Greek Table* page is the show landing page at 131 traffic, and
  `mygreektable.com` has no indexed presence. Broadcast authority does not become web
  authority: a TV audience does not produce backlinks.

### The signal is already in the table

`collection_members.traffic` and `.traffic_pct` are populated on all 97 of her rows, and on
every `backlinks_file` publisher — from the SEMrush export the harvest already reads. **The
demand signal sits in the same row as the endorsement signal that is being used instead.**
Nothing needs to be bought.

### Do not simply swap PA for traffic

Ranking her members by traffic puts baklava, moussaka and dolmades on top, which reads
right — and "it reads right" is exactly the reasoning that produced the mistake in
`docs/ai-editor-mediation.md`. Before changing the ranker, score both ways across several
publishers and check which ordering better predicts pages people actually open. Open
questions that a test has to answer:

* Traffic is only present on the `backlinks_file` path. A `serp`-sourced publisher has none —
  does the within-site term degrade to PA, or does the publisher not get ranked at all?
* Traffic rewards head-term targeting (`docs/ai-editor-mediation.md`), which is arguably the
  right signal for a home-cook product but is not a quality measure.
* Her top two pages by traffic are **articles, not recipes** (gyro history 4,617;
  spanakopita ingredients 1,827). A demand-ranked pipeline that only looks at recipe pages
  still cannot see what her audience actually arrives for.

### The third channel: authority that never touches the web

Both signals we have — PA (links) and traffic (organic search) — are web-native. Diane
Kochilas is a PBS series host with numerous published cookbooks, and **neither of those
produces the thing either metric counts.** A television audience does not write blog posts, and
a reader who cooks from a printed cookbook generates no page view and no backlink. Her
reputation is real and largely converts to **book sales and broadcast reach**, both invisible
here by construction.

This is not the paywall problem (a measurable suppression we can correct for) and not the DA
ceiling (a real property of the link graph). It is a **whole channel of authority the corpus
has no instrument for**, and it will systematically undervalue exactly the sources an
editorially-serious product most wants: cookbook authors, chefs, broadcasters — people whose
standing was established off the web and who treat their site as a companion rather than a
business.

Worth noting the direction of the error. Under-measuring here does not merely lose a good
source; it **inverts** the intent — a content-farm page with no author and good SEO outranks a
James Beard-winning author's own recipe, on a metric neither of them was competing on.

**A possible instrument already half-exists.** The planned `chefs` master table (a normalised
record parallel to `domains`) is the natural home for an author-authority signal, and the
product-commerce pipeline already resolves Amazon listings, ratings and review counts for
kitchen goods — the same machinery points at cookbooks. Published-title count, ratings and
awards would be an authority axis genuinely orthogonal to PA, attached to the *author* rather
than the *page*. Unbuilt, and not to be hand-waved into the ranker without the same
both-ways test demanded above.

---

## 12. Disproved — do not re-argue these

| claim | status |
|---|---|
| "the authority signal is hardly meaningful" | **FALSE.** corr(PA, log external links) = **+0.905** within a domain (smittenkitchen, n=114). PA *is* third-party endorsement. **But see §11c: the same PA correlates only +0.185 with within-site TRAFFIC (median, 12 publishers, ~6,300 pages). PA measures endorsement well and demand barely — and within a publisher it is the only thing ranking.** Both entries are true; do not use either to argue the other. |
| ranking has an age bias (old pages accumulate links) | **FALSE.** corr(publish year, PA) = **+0.530** — newer pages score higher. |
| internal linking explains that | **FALSE.** canonical-variant numbers killed it. |
| "keywords lead traffic by 2-3 months" | **OVERSTATED.** From porridge alone. At n=5,064: r=+0.12 raw, +0.19 partial. |
| "Rising = steep positive traffic arrow" | **FALSE.** Traffic momentum is INVERTED (-0.233), worse than random. |
| `Traffic Change` as a positive ranking flag | **FALSE.** Same finding. Investigate-only. |
| keywords are gained by "crossing rank 100" | **FALSE.** Porridge's keywords span 1-75, none beyond; they arrive at 4-20. |
| traffic history is blocked on this plan | **FALSE.** `url_rank_history` works; the 403 was a wrong type name. **Re-made a THIRD time on 2026-08-09** by re-running the probe and reading its 403 at face value instead of reading this table. The error message describes the wrong cause; that is the whole point of the entry. |
| `display_filter` is ignored on `domain_organic_unique` | **FALSE — MEASURED 2026-08-09.** Sorted `tg_asc`, unfiltered bottom rows are `0,0,0,0,0`; with `+\|Tg\|Gt\|1000` they are `1001,1006,1013,1021,1022`. It was written in the same sentence as the `display_date` 403 and inherited its credibility. Filtering server-side is what makes replacing the manual export workflow affordable (§10). |
| Google Trends as a page-trajectory fallback | **FALSE.** Trends is query-level only. No Google source gives third-party PAGE traffic. |
| the coverage constraint is structural | **FALSE.** It is a budget line — `url_ranks` serves any url. |
| momentum = `traffic(1mo) / (traffic(12mo)/12)` | **WRONG SHAPE.** Compares different months; for a seasonal dish it measures the calendar. Use like-month, and only for LIBRARY — a Rising page has never been through a cycle. |
| `url_organic.Tr` as "% of the page's traffic" | **UNVERIFIED and unusable.** Reconciles with neither page nor domain. |
| "15% of the corpus carries a fabricated PA" | **OVERSTATED ~3x.** Measured through the broken allow-list gate. Corrected: **5%** of the corpus, **0%** of the top 200 (§11b). |
| fabricated PAs are contaminating the ranking | **FALSE.** The placeholder parks a row at OU ≈ 0, ~18 points below the top-200 floor. It cannot reach anything visible. |

---

## 13. Open

* **Verdict on recipe vs membership** (§8).
* **Cadence** — `harvest_ttl_days=90` on 295/311 domains; the watchlist wants monthly.
* **Snapshot capture** — the free change that starts the clock. Every month it is not running is
  a month of series that cannot be bought back cheaply.
* **Rerun the experiment** on the 21 Aug export (clean 30-day window) and a second publisher.
  Everything measured is one site.
* **Structured-data prerequisite** — bostonglobe shows no keyword signal at all
  (`_source.type=article`, poor_quality_rate 0.41, `trust_extraction` override needed). A
  publisher Google cannot parse as recipes reads as "not rising" for reasons unrelated to
  quality. Systematic blind spot, unquantified.
* **Provenance backfill** — `verify_pa_provenance.py` is written and the flag is live, but only
  bostonglobe is stamped. ~4,150 rows still `NULL`. ~$8.70, non-destructive, resumable; no rush,
  since nothing visible depends on it (§11b).
* **Artifact scoring is the only cold-start signal.** The cook-rework validators already grade
  soundness and the result is discarded.
* **Within-publisher ranking is single-axis and near-blind** (§11c). `rank_score` collapses to
  PA inside a domain, and PA explains ~3% of within-site traffic variance. The demand signal
  is already stored on `collection_members.traffic`. Needs a both-ways test, not a swap.
* **Off-web authority has no instrument** (§11c). Cookbooks and broadcast produce neither
  backlinks nor organic search, so PBS hosts and published authors are undervalued by
  construction. Candidate home: the planned `chefs` table + the existing Amazon
  product-resolution pipeline pointed at published titles.
* **This still ranks provenance and demand, never the dish.** An AI cannot know what tastes
  better; anything claiming otherwise is laundering a guess.

---

## 14. THE THREE THRESHOLDS — MEASURED 2026-08-22

**Read this before touching grading.** "Is this recipe that dish?" is asked in three
places, and the three answer differently. The result is that most graded rows are
graded against a cohort the system elsewhere says they do not belong to.

| asked by | threshold | as L2 |
|---|---|---|
| save path, stamping `_match` | `dish_match_max_distance` | **0.60** |
| `/recipes/similar-master` | derives from the same setting | **0.60** |
| **grading** (`find_best_dish_match`) | `DEFAULT_MATCH_THRESHOLD` = cosine 0.55 | **0.949** |

The first two were deliberately tied together on 2026-08-06, with a comment saying
*"One question, one bar: derive it from the same setting so the two paths cannot
drift apart again."* **Grading was never brought onto that bar** and still carries
its own constant.

### What it costs, measured

2,280 master rows are graded via an embedding match. Of the 2,263 that also carry a
`_match` distance:

| rows | share | |
|---:|---:|---|
| 461 | 20.4% | inside the current match bar (<= 0.60) — genuinely matched |
| 462 | 20.4% | opened by the 0.80 -> 0.60 change on 2026-08-22 |
| **1,336** | **59.0%** | **already beyond even the OLD 0.80 bar — pre-existing** |
| 4 | 0.2% | beyond grading's own bar (> 0.949) |

**79% of embedding-graded rows are graded against a dish their own `_match` rejects,
and three quarters of that predates the threshold change.** Worked examples, all real:

    d=0.960  grade=A+   graded against  Hungarian Goulash
    d=0.955  grade=D+   graded against  Chicken Cacciatore
    d=0.951  grade=A+   graded against  Pasta & Noodles - Agnolotti
    d=0.948  grade=A+   graded against  Cobbler

At L2 0.96 in a 179-dish catalog, "matched" means *nearest neighbour*, not *belongs
to*. **The nearest neighbour always exists** — distance says how far, never whether it
belongs, which is the entire reason a threshold exists. A grade is a comparison
against a cohort; comparing a recipe to an unrelated cohort produces a number, not a
measurement.

### What is NOT yet known

* Whether tightening grading to 0.60 is right, or whether it just moves 79% of rows
  onto `chapter-fallback` — which may be **more** honest (a chapter is a real
  population) or **less** informative (n is huge, so everything regresses to the mean).
* Whether the grade actually MOVES when the cohort changes. Nobody has re-graded the
  same row against both cohorts and compared. That is the cheap experiment, and it
  should come before any change: both cohorts already exist, so it costs a script.
* Whether `explicit` (a curated `_master.dish`) and `embedding-match` grades are
  comparable at all. 2,443 rows carry an explicit dish and are graded against it;
  the rest are graded against a guess. Those two populations may not belong on one
  scale.

### The rule this is an instance of

Any threshold that answers the same question in two places must come from ONE
setting. Three copies is how this happened; the same class produced the
64-vs-16-char `embedding_text_hash` and the two copies of the dish matcher, both on
the same day.
