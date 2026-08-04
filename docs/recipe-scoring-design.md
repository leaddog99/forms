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
**the API cannot** (`display_filter` ignored on `domain_organic_unique`, `display_date` 403), so
the diff is ours either way — free, from a file already downloaded.

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
| "New pages" filter | — | — | **not available** |
| multiple domains per call | — | — | **not available** |

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

**Consumers must now exclude `moz_http_code = 0` from any fit over PA** — the paid-PA calibration
first, since fabricated rows are concentrated in exactly its population and would drag the
shift-scale remap toward the placeholder.

---

## 12. Disproved — do not re-argue these

| claim | status |
|---|---|
| "the authority signal is hardly meaningful" | **FALSE.** corr(PA, log external links) = **+0.905** within a domain (smittenkitchen, n=114). PA *is* third-party endorsement. |
| ranking has an age bias (old pages accumulate links) | **FALSE.** corr(publish year, PA) = **+0.530** — newer pages score higher. |
| internal linking explains that | **FALSE.** canonical-variant numbers killed it. |
| "keywords lead traffic by 2-3 months" | **OVERSTATED.** From porridge alone. At n=5,064: r=+0.12 raw, +0.19 partial. |
| "Rising = steep positive traffic arrow" | **FALSE.** Traffic momentum is INVERTED (-0.233), worse than random. |
| `Traffic Change` as a positive ranking flag | **FALSE.** Same finding. Investigate-only. |
| keywords are gained by "crossing rank 100" | **FALSE.** Porridge's keywords span 1-75, none beyond; they arrive at 4-20. |
| traffic history is blocked on this plan | **FALSE.** `url_rank_history` works; the 403 was a wrong type name. |
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
* **This still ranks provenance and demand, never the dish.** An AI cannot know what tastes
  better; anything claiming otherwise is laundering a guess.
