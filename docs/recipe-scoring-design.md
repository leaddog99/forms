# Recipe scoring — the composed picture

> Status: **DESIGN / NOT BUILT.** Nothing here is decided. The shipped system is
> `docs/domain-scoring.md` (OU/power blend, two-stage harvest); this is where it could go.
> Memories: `project_two_stage_selection` (gospel — read first), `project_domain_scoring`,
> `project_ou_power_blend`, `project_paid_pa_calibration`, `project_selection_lens`.

Written 2026-08-03 out of a long working session on why the ranking behaves as it does.
Read `project_two_stage_selection` first: **selection is last month's traffic, ranking is
OU**, and critiquing either in isolation produces wrong conclusions.

## The organizing insight — four questions, not one score

Stop asking "how good is this recipe" and ask four separable things:

| | question | signal | have it today? |
|---|---|---|---|
| **Demand** | do people want this dish? | keyword search **volume** | NO — see Google Ads below |
| **Capture** | do they choose *this* version? | page traffic / volume | NO — derived from the above |
| **Advocacy** | having found it, do they recommend it? | OU / excess PA | YES |
| **Trajectory** | is that rising, stable, fading? | traffic over 1/6/12 months | NO — one window only |

Today advocacy is measured well, demand only partially (traffic conflates two different
things), and trajectory not at all.

**Vocabulary, and it matters.** Traffic is **appetite** — something caught thousands of
people's eye and they wanted to try it; nobody *needs* a recipe. Excess PA is **advocacy** —
having found it, someone thought it worth pointing others at. Two different human acts, and
the gap between them is informative: heavy traffic with below-median PA means a recipe drew
people in without moving them to recommend it.

## The coverage constraint — it is a BUDGET, not a wall (read this first)

**Traffic exists only as a by-product of a SEMrush publisher harvest.** Measured 2026-08-03:

```
master_recipes            : 4,148
  with harvest traffic    : 2,181  (53%)
  with a dish keyword     : 2,324  (56%)
publishers with traffic   :    86 of 841  (10%)
```

So every traffic-derived term covers **half the corpus and a tenth of publishers**, biased
toward whichever publishers happened to be harvested.

**BUT this is how we get traffic today, not the limit of what is available.** The Semrush
Analytics API serves all three axes for an ARBITRARY url, on demand (see "Buying the missing
axes" below): `url_overview` returns `Ot` (absolute organic traffic) and `Or` (organic
keyword count) for any url at 10 units/line, with `display_date=YYYYMM15` for historical
months at 50 units/line; `url_organic` returns that url's keywords with `Nq` (search volume)
at 10 units/line.

So the constraint is **cost and cadence, not availability** — and the cost is smaller than
expected (full corpus current traffic is ~2% of the smallest unit package). Design the tiers
around what is worth buying for which recipes, not around what can be known.

## TWO OBJECTIVES, TWO FILTERS — the organizing decision

Curator, 2026-08-03: *"we have different objectives here... I want a library of the best
tried and true recipes that is discoverable via the pa/da stats... and separately I want the
up and coming exciting new recipes. There are two different filters on the extraction and
need to be flagged as such."*

This is not one score with archetypes falling out of it. It is **two selection pipelines**
that read different signals, disagree by design, and stamp the record with which one found
it. A recipe may satisfy both, either, or neither.

| | **A. THE LIBRARY** (tried and true) | **B. RISING** (up and coming) |
|---|---|---|
| question | has this EARNED its place? | is this ABOUT to? |
| primary | OU / excess PA — advocacy | `Or` velocity — keyword footprint growth |
| supporting | durability (sustained excess, years), capture, like-month momentum ~1.0 | poised pool (`Nq` at pos 11-30), `Ot`/`Or` inflection, answer-engine presence |
| floor | artifact soundness | artifact soundness |
| **explicitly EXCLUDES** | `Or` velocity — a spike is not a credential | **PA / OU / advocacy** — a new page has none BY CONSTRUCTION; ranking on it finds new recipes LAST |
| fails on | anything published recently | anything mature and stable |
| flag | `selection_lens = 'library'` | `selection_lens = 'rising'` |

**Why they must stay separate.** Blend them and each destroys the other: a single score with
advocacy in it can never surface a three-week-old recipe, and a single score with keyword
velocity in it demotes a decade-old classic that is simply stable. The two-lens split is
already anticipated in `project_selection_lens` (a "hot" page vs "hidden gems / editor's
find") — this is that idea with measurable triggers.

**Flag at EXTRACTION, not at display.** The harvest runs both filters and stamps which one
selected the row, so the surfaces read a flag rather than re-deriving thresholds, and a
recipe that qualified as Rising in March is still legible as such in September when its
numbers have moved on.

## B. The RISING detector

Ordered by how EARLY each fires. The first three are the detector; the rest confirm too late
to be useful.

**1. `Or` velocity — the earliest signal, 2-3 months ahead of traffic.**
Monthly keyword count climbing consistently. **Three consecutive months of `Or` growth is the
trigger.** Measured on christinascucina.com/porridge/, a real breakout:

```
date        Ot        Or    Ot/Or    Or chg
20260715    10,470    224    46.7      +39
20260615     1,055    185     5.7      +17
20260515       727    168     4.3      +29
20260415       549    139     3.9      +32
20260315       383    107     3.6      +23
20260215     1,383     84    16.5      +33
20260115        13     51     0.3      +20      <- Or already climbing 3 months
20251215         9     31     0.3      +17      <- traffic still noise
20251115         7     14     0.5       -2
20251015         0     16     0.0        0
```

`Or` climbed from Nov while `Ot` sat at 0, 7, 9, 13. Traffic did not move until February.

**2. The poised pool — `Nq` ranked at positions 11-30.** Demand the page has earned reach
into but not yet converted. This is a PREDICTION, not a record.

**3. `Ot`/`Or` inflection — the conversion moment.** Traffic-per-keyword went 0.3 -> 46.7 as
positions crossed the page-1 cliff. Low `Ot`/`Or` with a big poised pool = **loaded but not
fired**; rising `Ot`/`Or` = firing now.

The mechanism, breakout vs stuck (60 keywords sampled each):

```
PORRIDGE (broke out)  pos 1-3: 19   4-10: 33   11-20:  8   -> 84% of 103,710 Nq converted
VONGOLE  (stuck)      pos 1-3:  0   4-10:  0   11-20: 47   ->  0% of  40,410 Nq converted
```

Vongole is not failing for lack of demand — **40,410 monthly searches in reach, every one of
them on page two.** Porridge crossed onto page one and took 84% of a 103,710 pool. Same
publisher, same DA, opposite outcomes, and neither is visible in a traffic snapshot.

**4-6, confirmation only:** answer-engine presence (`google-ai`, `gemini`, `search-gpt` and
`LLM Prompts` — a channel nothing in the current scoring reads); head-term `Nq` x best
position, i.e. how big the ceiling is; artifact soundness as the floor.

**What this changes.** Porridge would have tripped the Rising trigger in **January 2026**, at
`Or 51` / `Ot 13`. The current scoring ranks it near zero — no links, no traffic — until
July, by which point it is not a discovery. **The existing system is structurally incapable of
finding a new recipe before everyone else has**, and that is the case for building this.

## Demand — search volume is the missing denominator

**Traffic alone conflates "big dish" with "winning page."**

    capture = page traffic / keyword search volume

That one ratio dissolves the confound. `how-to-hard-boil-an-egg` pulls **16,750** visits on
smittenkitchen — but against a query with enormous volume its capture is tiny. A niche page
taking 800 visits on a 1,200-volume keyword **owns its dish**. Same data, opposite
conclusion, and capture is the honest one.

It also makes cross-dish comparison possible, which raw traffic can never do: 5,000 visits
on banana bread and 5,000 on pastitsio are not the same achievement.

**Crucially, volume is keyed on the DISH, not the page** — so unlike traffic it does not
depend on a publisher having been harvested. It could take the demand axis from 53% to
effectively 100% coverage.

## Trajectory — momentum must compare LIKE MONTHS

**Do NOT use `traffic(1mo) / (traffic(12mo)/12)`, and do not use the export's raw
`Traffic Change`.** Both compare a month against a different month, and for a seasonal dish
that measures the calendar, not the recipe.

    momentum = traffic(this month) / traffic(SAME MONTH last year)
       >1.3 rising      ~1.0 stable      <0.7 fading

Worked case that forced this correction — christinascucina.com's shortbread, July 2026. A
month-over-month read says `-1,962` and a trailing-mean read says `0.68x`: fading, demote it.
Year over year says the opposite:

```
month     2024 Ot   2025 Ot   2026 Ot  |  2024 Or   2025 Or   2026 Or
01         13,005    10,894    16,009  |   1,127     2,322     4,479
02         11,176    10,940    14,094  |   1,223     2,494     4,600
03          7,718     8,867    17,019  |   1,173     2,573     3,337
07          6,191    10,163     9,623  |   1,155     1,665       739
```

Up on every comparable month, with the Feb-peak / summer-trough shape repeating in all three
years. It is shortbread — a Christmas bake. The "decline" was July.

**A raw `Traffic Change` cannot tell a breakout from a season.** In the same export,
porridge reads `+7,855` and shortbread `-2,182`. Porridge is genuinely breaking out (flat
ZERO until Oct 2025, then 7 -> 1,383 -> 10,470 by Jul 2026). Shortbread is simply out of
season. Ranking on the raw diff punishes seasonal dishes in exactly the months nobody cooks
them, then rewards them in December for the same non-reason.

### Reading `Or` alongside `Ot`

`Or` (organic keywords) counts terms the url ranks for **that have measurable volume**, so it
falls for two very different reasons that look identical in the number:

1. **Demand disappeared** — "christmas shortbread gift tins" is not searched in July. The
   page still ranks; the query stopped existing.
2. **Rankings were lost** — the page fell out of the top 100 on terms people still search.
   This is the real problem case.

Separate them with a control: **same month last year** (season held constant, so what remains
is structural), or **the publisher's other pages in the same window** (a domain-wide `Or` fall
is an algorithm update or a Semrush database change, not this page). A genuine loss shows `Or`
down year-over-year AND head-term positions slipping — shortbread holds 1, 2, 3, 1, so it has
not lost anything.

### Where the data comes from

* **`Traffic Change`** — already in the Organic Pages export, free, but month-over-month only.
  Use it as a flag to investigate, never as a ranking term.
* **`url_rank_history`** — the API call that makes like-for-like possible. See below; note it
  returns the FULL series (175 months) at 50 units/line, so it is a shortlist call.

Confound that remains after seasonality is handled: momentum is contaminated by the
**publisher's own trajectory** — a growing site lifts every page. Normalise within publisher,
the way OU already does.

## Durability — the time axis on ADVOCACY

Selection already proves a page is *currently* wanted; OU proves it *out-earned its
siblings*; neither proves it has done both for years. Durability is therefore **not "old and
still trafficked" but old and STILL CARRYING EXCESS PA** — survivorship of the residual
through everything the publisher has released since.

Worked example (smittenkitchen): **shakshuka, 2010, 329 traffic, PA 57 against a median of
55.** Sixteen years of newer posts published over it and it still out-endorses its siblings.
That is a claim no single month can make.

Hold demand constant by computing it **inside a dish cohort** — the dish-refresh path
already builds one. Otherwise it measures how popular the dish is. **Age alone is never
merit: the claim is sustained excess, never survival.**

## The shape — a point with an arrow

Curator's framing: *"maybe our scoring should be based on a point location in the 2x2
matrix... a poor man's vector."*

* **Position** = (capture percentile, advocacy percentile) — the plane
* **Magnitude** = `hypot(x,y)/sqrt(2)` — overall strength, 0..1
* **Angle** = `degrees(atan2(y,x))` — 0 deg pure appetite, 90 deg pure advocacy
* **Momentum** = an **arrow on the point** — where it is heading
* **Artifact soundness** = a **floor, not a rank term** — gates eligibility, adds no points

Position says what it is; the arrow says where it is going.

Measured on smittenkitchen.com (114 pages, using traffic percentile as x since capture is
not yet available). Angles span the full 0-90 with a median of 44, so the dimension is real
and well populated, not clustered on the diagonal:

| angle | reads as | example |
|---|---|---|
| ~45, high magnitude | wanted AND recommended | sidecar (traffic 99th pct, PA 96th) |
| ~0-3 | huge appetite, almost no advocacy | how-to-hard-boil-an-egg (100th / 2nd) |
| ~62-74 | quiet, disproportionately recommended | charred salt-and-vinegar cabbage (50th / 96th) |

**What is new is the ANGLE, not the magnitude.** Euclidean distance in percentile space with
equal weights ranks close to the existing weighted blend — so this is not a better scalar.
The blend's real loss is that it COLLAPSES direction: two recipes scoring identically today
can sit at 5 deg and 75 deg and mean opposite things.

**It is a lens selector as much as a score.** `project_selection_lens` (hot vs
hidden-gems/editor's-find) falls out of one computation instead of two separate builds.

Two constraints if built:

1. **Percentile-rank both axes first.** Raw traffic spans 141-16,750 and is log-distributed;
   PA spans 49-62. Un-normalised, traffic dominates the angle entirely.
2. **The angle only means anything within a COHORT** — a publisher for a within-site lens, a
   dish for cross-publisher picks. `PERCENT_RANK()` over the generated columns already does
   this.

## The archetypes it produces (which lens each belongs to)

| archetype | signature | surface |
|---|---|---|
| **Star** | high magnitude, ~45 deg, flat arrow | LIBRARY — the medal pick |
| **Evergreen** | high magnitude, flat arrow, years of it | LIBRARY — "a decade-long favourite" |
| **Rising** | 3 months `Or` growth + poised pool + `Ot`/`Or` lifting | RISING — "trending now" |
| **Hidden gem** | high angle (advocacy >> capture) | LIBRARY — editor's find |
| **Commodity** | high traffic, LOW capture, low angle | utility — do not medal it |
| **Fading** | negative arrow, past advocacy | re-review or retire |
| **Unproven** | no endorsement data yet | artifact score only |

The last row is the cold-start answer. A three-day-old recipe has no capture, no advocacy and
no trajectory — the artifact checks are the only thing that can speak for it.

## The record shape — two blocks, one per lens

Curator: *"I would envision two different blocks in the master with fields from each
separately."* Fits the existing typed-block pattern (`_master`, `_scoring`, `_cook`,
`_identity`, `_measurements`).

```jsonc
"_scoring":  { ...unchanged - RAW measurements, ONE copy... },

"_library": {                      // A. tried and true
  "ouScore":         6.28,         // advocacy, the primary
  "excessPA":        6.3,          // PA over the DA-predicted bar
  "capture":         0.0011,       // Ot / Nq in reach
  "momentumYoY":     1.04,         // like-month, ~1.0 = stable
  "durabilityYears": 16,           // years carrying excess PA
  "verdict":         "evergreen",
  "computedAt":      "2026-08-03T...",
  "cohort":          "dish:Pasta Vongole"
},

"_rising": {                       // B. up and coming
  "orSeries":        [224,185,168,139,107,84],   // newest first
  "orVelocity3mo":   0.61,         // +61% over 3 months
  "poisedPoolNq":    40410,        // Nq ranked at positions 11-30
  "convertedPct":    0.84,         // Nq at pos <=10 / total in reach
  "otPerOr":         46.7,         // conversion - the inflection
  "answerEngines":   ["google","google-ai"],
  "llmPrompts":      26,
  "triggeredAt":     "2026-01-15", // when it FIRST qualified
  "graduatedAt":     null          // set when it stops being new
}
```

**Three rules that are cheap now and expensive later.**

1. **Raw in `_scoring`, derived in the lens blocks.** `Ot`, `PA`, `DA`, `Nq` are facts about
   the page; `capture`, `momentumYoY`, `orVelocity` are interpretations. If both lens blocks
   carry their own copy of `Ot` they will drift — the same failure as the two hostname lists
   that produced a customer front door over the admin surface (2026-07-31).
2. **`triggeredAt` is what makes Rising honest.** Rising is TIME-BOUND in a way Library is
   not: porridge qualified in January and by July it is merely a popular page. Without a
   trigger date you cannot answer "was this a discovery, or did we notice late" — the only
   question that judges whether the detector works.
3. **Both blocks are COHORT-RELATIVE.** `capture` and `orVelocity` are meaningless in the
   absolute; they are relative to some population. `_scoring.fieldScope` is empty on every row
   today, which is exactly this problem already unresolved. Stamp the cohort IN the block or
   the number is unreadable in six months.

**Two build consequences to price first.**

* **Generated columns.** `_scoring` is queried through virtual generated columns (`ou_score`,
  `power`, `source_host`). These blocks want the same — `rising_triggered_at`,
  `library_verdict`, `poised_pool` — or they are invisible to SQL and every query re-parses
  JSON blobs.
* **The four-edge rule.** Per `feedback_db_form_sync`, new recipe fields need load / save /
  extract / metadata ALL audited or they drop silently. Two blocks of ~10 fields each is real
  form work, not a schema tweak.

### OPEN QUESTION — does the VERDICT belong on the recipe at all?

A recipe is one thing. "Is it rising" is a claim about a **moment and a cohort** — and a
recipe can be Rising within one dish cohort and unremarkable in another. That reads more like
`collection_members` (the ledger, which already carries `rank`, `selected`, `rank_score`,
`adjusted_pa` PER collection) than like the recipe record.

The likely split, not decided:

* **metrics** -> the recipe (`_library` / `_rising`) — they describe the page
* **verdict + lens flag** -> the membership row — it describes the page IN a collection

Deciding this wrongly is costly in the same way `_master.dish` was before dish membership
became a junction ([[project_dish_variants_membership]]): a single stamp on the recipe cannot
express "rising for Porridge, ordinary for Breakfast".

## Where Rising LIVES — the ledger, then a promotion gate

Curator's concern: *"the master is the reliable library and should not be polluted with hot
shots... but the actual recipe has to live someplace. Another recipe table?"*

**No second recipe table. The ledger is already the staging area, and promotion is the gate.**

Measured 2026-08-03:

```
master_recipes by _master.kind :  top 4,040   (none) 138   harvest 1
collection_members             :  publisher - 97 keys, 10,276 rows
```

`collection_members` already holds **10,276 discovered-but-not-promoted URLs**, and the
harvest already promotes winners into master. That is exactly the shape Rising needs: a rising
CANDIDATE is discovered, tracked and re-evaluated monthly, and only enters master once it has
EARNED it.

    collection_type='rising'  ->  ledger row, metrics only, no recipe content
              |  graduation gate
              v
    master_recipes            ->  extracted recipe, selection_lens='library'

**Master stays the library BY CONSTRUCTION** — not because rising rows are stored elsewhere,
but because the promotion gate is what admits anything at all.

### Why not a second recipe table

1. **A graduating recipe is the SAME recipe.** Porridge in January (rising, `Or 51`/`Ot 13`)
   and in July (established, `Ot 10,470`) is one page. Two tables means a migration on every
   graduation, and migrations lose things.
2. **Dedup.** The same url can be found by both filters. Two content tables = two rows for one
   recipe — exactly the caponata bug of 2026-07-31, where ids 5515 and 5516 were one Serious
   Eats recipe stored by two different paths, and one had to be deleted.
3. **The enrichment is expensive and shared.** `_cook` is a ~$0.44 Opus pass, `_identity` ~2s
   of Haiku, plus `_measurements`, page screenshot and cooped hero image. A second table either
   duplicates all of it — paying twice and drifting — or needs a join anyway.
4. **Two typing mechanisms exist and neither is exercised.** `_master.kind` is 4,040 `top` and
   almost nothing else; `collection_type` is only `publisher`. This is what they are for.

### Make purity STRUCTURAL, not disciplinary

The real risk of one store is a query that forgets to filter. Fix that with a **view**, not a
table:

```sql
-- the library: nothing can accidentally read a hot-shot from this
CREATE VIEW library_recipes AS
  SELECT * FROM master_recipes WHERE selection_lens = 'library';

CREATE VIEW rising_recipes AS
  SELECT * FROM master_recipes WHERE selection_lens = 'rising';
```

Physically one store, logically two. Pair with `selection_lens` as a **virtual generated
column** (same pattern as `ou_score` / `source_host`) so the filter is indexable rather than a
JSON re-parse.

### The promotion gate

What a rising candidate must show before it is extracted into master — deliberately a
different bar from the Library filter, because it is a bet rather than a credential:

* `Or` growth sustained across **three consecutive months** (the trigger), AND
* a **poised pool** worth having — meaningful `Nq` at positions 11-30, AND
* `Ot`/`Or` **lifting** — conversion actually starting, not just footprint, AND
* **artifact soundness passes** — the floor, and at this stage the only quality evidence that
  exists.

Candidates that never convert stay ledger rows and cost nothing: no extraction, no `_cook`, no
screenshot. **That is the point of gating at promotion rather than at display** — the expensive
work only happens for recipes that proved out.

Graduation sets `_rising.graduatedAt` and flips `selection_lens` to `library` once the
Library filter would admit it on its own merits. Until then a promoted rising recipe is
visible as `rising` and only there.

### What would change this decision

A second table would be right if rising rows needed a **different schema**, or a lifecycle
master cannot express. Neither holds: identical recipe fields, and `kind='top'` already does
delete-and-replace churn per batch refresh — the same volatility, already tolerated.

## EXECUTION — where a riser dies today, and the exact steps to catch it

Curator: *"the current extract filters by pa/da... the new items aren't there, therefore
there's no recipe to process but 'old' recipes."* Correct, and there are **TWO independent
gates**, not one.

### Why the current pipeline cannot produce a riser

Take porridge as it stood in **Nov 2025**: `Ot=7`, `Or=14`, around row 400 of
christinascucina's 820-row export.

```
1. read export, sort by traffic desc, take top records=100   <-- DEAD HERE. never a candidate
2. pre-filter taxonomy / archive urls
3. recipe filter - FETCH each candidate (unblocker), check JSON-LD
4. Moz score each survivor -> PA/DA/OU
5. domain_scoring -> rank_score
6. sort by rank_score, keep top `keep` (10-40)               <-- DEAD HERE TOO. PA ~0
7. extract winners -> master_recipes
8. also-rans stay ledger rows, never extracted
```

`_read_backlinks_file(domain, want=records)` truncates 820 rows to 100 by TRAFFIC (gate 1),
and the keep-top-N sorts on `rank_score`, which is PA-driven (gate 2). A new page fails both.
Note steps 3-4 SPEND (unblocker fetch + Moz rows) on 100 pages BEFORE selecting, so simply
widening the funnel is not affordable.

### Phase 1 — CAPTURE, at harvest time, costs nothing

1. Read the export at **FULL DEPTH** — all 820 rows, not the top 100. Same file, already
   downloaded, already parsed.
2. Append one snapshot row per url: `(url_normalized, captured_at, traffic, or_count,
   traffic_change, traffic_pct, answer_engines, llm_prompts, top_keyword, primary_intent)`.
   **No fetch, no Moz, no LLM — a workbook parse.**
3. The existing pipeline continues **UNTOUCHED** on the top-100. Library behaviour unchanged.

This is the ONLY change to the extract. Today those columns are parsed and discarded a few
lines later, and `replace_members` is delete-and-replace, so every re-harvest destroys the
history Rising needs.

### Phase 2 — JUDGMENT, a separate scheduled job reading stored data

4. For each url with **>=3 monthly snapshots**, compute from the snapshot table alone — free,
   pure SQL: `Or` velocity, `Ot`/`Or` trend, traffic trajectory.
5. **Screen**: keep only urls with three consecutive months of `Or` growth. Still free.
6. **FIRST SPEND**, survivors only: `url_organic` (10 units/line) -> positions + `Nq` ->
   poised pool, converted %.
7. Apply the trigger: `Or` velocity AND poised pool AND `Ot`/`Or` lifting.

Precedent for the shape: `domain_scoring` already runs as a scheduled job re-scoring the whole
corpus after the fact rather than inline. Judgment MUST be a batch — the metrics are
cohort-relative (percentiles need the whole population), velocity spans runs by definition,
and tuning the threshold has to be free (re-run on stored data, never re-harvest).

### Phase 3 — PROMOTION, triggered candidates only

8. Fetch the page (unblocker per domain policy) — the first expensive I/O.
9. Recipe filter — is it even a recipe?
10. Artifact soundness — the floor.
11. Extract -> `master_recipes`, `selection_lens='rising'`, `_rising` block with `triggeredAt`.
12. Add to the `collection_type='rising'` ledger.

### Phase 4 — MAINTAIN, every judgment run

13. Update metrics on existing rising rows.
14. **Graduate** — if the Library filter would now admit it on its own OU/durability, flip
    `selection_lens='library'` and set `graduatedAt`.
15. **Retire** — velocity reversed and never converted: drop from the rising collection.

### The economics — Rising is CHEAPER per candidate than the current extract

```
820 rows snapshotted        free
 ~20 pass velocity screen   free
 ~20 url_organic calls      ~2,000 units
  ~5 trigger -> fetch + extract
```

Because it screens on free stored data FIRST and only pays for survivors. The current pipeline
fetches and Moz-scores 100 pages before deciding which 10 to keep.

### The limitation, stated plainly

**A page with genuinely zero rankings is invisible even at full export depth** — Semrush only
lists pages that rank for something. Porridge first appears at `Or=16, Ot=0` in Oct 2025, so it
IS visible before the traffic arrives; a recipe published last week is in no export at all.
That case stays tier 3: artifact soundness only, no demand signal at any price.

### Cadence — this needs deciding

`harvest_ttl_days=90` on 295 of 311 domains. Rising wants MONTHLY points; a quarterly harvest
gives three points a year, so the three-month trigger would take nine months to fire. A monthly
clock is probably only worth it for the ~30 publishers worth acting on, not all 311.

## Tiers — how it degrades

Because of the coverage constraint, this is not one formula but three:

Tiers are now a **spending decision**, not a data-availability one — anything in tier 2 or 3
can be promoted by paying for the lookup (see costs below).

| tier | who is in it | score | why not just buy everything |
|---|---|---|---|
| **1** — ranked shortlist | the above + trajectory | full vector, momentum computed YEAR-OVER-YEAR | `url_rank_history` is 8,750 units/url — shortlist only |
| **2** — whole corpus | traffic + volume + advocacy | **capture — available NOW**, 41,690 units for all 4,169 | `Traffic Change` from the export flags movement, but is month-over-month so it cannot rank |
| **3** — fresh grab / brand-new URL | none of it yet | artifact soundness only | a 3-day-old page has no traffic, volume rank or endorsement to buy — nothing exists to measure |

Tier 3 is the only one that is genuinely un-purchasable, and it is the cold-start case: a
recipe published this week has no history at any price. That is what makes artifact scoring
structurally necessary rather than merely nice.

## Buying the missing axes — Semrush first, Google second

**Semrush already sells everything this design needs, per url, and it is the vendor you
already pay.** Checked 2026-08-03:

**PROBED LIVE 2026-08-03** (`scripts/semrush_url_probe.py`, 240 units of a 50k balance).
Two of the three axes work on the current plan; the third does not:

| what | endpoint / column | cost | status |
|---|---|---|---|
| absolute traffic for ANY url | **`url_ranks`** -> `Ot`, `Or` | 10 units/line | **WORKS** |
| that url's keywords + SEARCH VOLUME | `url_organic` -> `Ph`, `Po`, `Nq`, `Tr` | 10 units/line | **WORKS** |
| that url month by month | **`url_rank_history`** -> `Dt`, `Ot`, `Or` | 50 units/line | **WORKS** |
| (`url_ranks` + `display_date`) | — | — | 403 — wrong call, see below |

Measured, and it is the design's own worked example:

```
how-to-hard-boil-an-egg   Ot=24,625  Or=3,180   "hard boiled eggs"  Nq=246,000  pos 5   Tr=8.99%
country-chicken-stew      Ot=    37  Or=  137   "chicken stew"      Nq= 18,100  pos 30  Tr=32.43%
```

The hard-boiled egg page is exactly the predicted shape — enormous traffic, but capturing a
slice of a 246,000-volume query. `country-chicken-stew` takes 37 visits from an 18,100 query
at position 30. **Capture is computable today**, on the existing balance, for the whole
corpus (41,690 units for all 4,169 recipes).

**Trajectory works, via a DIFFERENT REPORT TYPE.** `url_ranks` + `display_date` returns
`ERROR 403 :: History reports are not allowed`, which reads like a plan limit and is not —
the history report is **`url_rank_history`**, and it works on this plan. (`domain_rank_history`
likewise for a whole domain; `url_ranks_history` and `domain_ranks_history` do not exist.)
Same class of mistake as `url_overview` vs `url_ranks`: a wrong type name that produces an
error message about permissions.

**But it returns the FULL series — 175 monthly rows back to 2012 — at 50 units/line, so
8,750 units for ONE url.** That is 175x a current-traffic call and would exhaust a 50k balance
in six urls. No range parameter was found (`display_date` is the one that 403s, `display_limit`
does not apply); worth asking support whether the range can be bounded, because at 12 points
it would be 600 units/url and viable for a few hundred recipes. Until then **`url_rank_history`
is a shortlist-only call.**

Four gotchas, all paid for once already:

1. **The API `type` is `url_ranks`, NOT `url_overview`.** The docs page is *titled* "URL
   Overview (one database)"; `type=url_overview` returns `query type not found`.
2. `url_organic` bills **per keyword line** — 10 keywords is 100 units for a single url.
3. Its `Tr` column is Traffic **(%)**, not absolute. `Ot` is the absolute figure, `Nq` the
   volume figure.
4. A url Semrush has not indexed returns **`ERROR 50 :: NOTHING FOUND`**, not a zero row —
   the three-day-old sun-sentinel article is unknown to Semrush exactly as it is to Moz.
   **Same cold-start hole in both vendors**, which is the strongest argument for tier 3.

**Unit accounting verified exact:** computed spend matched the balance delta on both runs
(100 units, then 120), and calls returning no rows bill nothing. The cost table below can be
trusted.

### What it costs

Semrush does **not publish a per-unit price** (confirmed in their KB). Units come in packages
of 2M / 5M / 10M / 20M, the API is an add-on to the **Business** tier, and the rate comes from
sales. Third-party sources cluster around **~$50 per million**; treat the dollars as
indicative and the **units as exact**. Units **expire monthly and do not roll over**, which
favours a regular scheduled pass over occasional bursts. Rate limit is 10 req/sec, so a
full-corpus pass is ~7 minutes minimum.

At 4,169 recipes:

| operation | units | ~$ at $50/M |
|---|---|---|
| current traffic (`Ot`), whole corpus | 41,690 | $2.08 |
| keywords + volume, top-10 kw/url, whole corpus | 416,900 | $20.85 |
| 3 time points (1/6/12mo), whole corpus | 625,350 | $31.27 |
| 12 monthly points, whole corpus | 2,501,400 | $125.07 |
| **all of the above except the 12-point history** | **1,083,940** | **$54.20** |
| 12 monthly points, 200-recipe shortlist | 120,000 | $6.00 |

**Current traffic for the entire corpus is ~2% of the smallest package.** Traffic + volume +
three time points is ~54% of one 2M package. Only the full 12-month history is expensive, and
that is precisely the thing to run on a ranked shortlist rather than everything.

Caveat that does not go away: `Ot` is Semrush's **estimate**, the same model behind the
exports — consistent with what is already held, not more truthful.

## Google Ads as the alternative denominator

Checked 2026-08-03. **Yes, per-URL is possible**, in two calls:

1. **`GenerateKeywordIdeas` with `UrlSeed`** — takes a PAGE URL as the seed and returns the
   keywords that page is about, each with `avg_monthly_searches` and competition. Also
   `KeywordAndUrlSeed` (keyword + URL) and `SiteSeed` (a whole domain, up to 250k ideas).
2. **`GenerateKeywordHistoricalMetrics`** — takes those keywords and returns
   **`monthly_search_volumes`: month-by-month for the past twelve months**, plus competition
   index and bid ranges.

So a recipe URL in, and "this page is about *pastitsio*, *greek lasagna*, here is each one's
volume month by month" out. **That is the 1/6/12-month trajectory from Google directly,
without a third SEMrush export** — and on the demand side, where coverage is not limited to
harvested publishers.

**The catch.** Basic Access needs a Google Ads manager account, a developer token and an
application review (Google piloted same-day review in July 2026; previously ~5 business
days). **Full precision requires active ad spend** — accounts without recent campaign
activity get bucketed ranges like "1K-10K" rather than exact numbers. That bucketing bites
differently per use:

* **Ranking dishes by demand** — buckets are fine, they still order correctly.
* **Capture arithmetic** — buckets are mush. `traffic / [1K-10K]` is a 10x uncertainty band,
  usable only as a coarse tier.

**Google Trends is NOT a substitute, and the distinction matters.** Trends is search-TERM
interest over time — a relative 0-100 index for a QUERY, never for a URL. It can say
"shakshuka is rising"; it can never say "this shakshuka page is rising." It is a fallback for
DISH-demand trajectory only, not for page trajectory.

**There is no Google source for third-party PAGE traffic, free or paid.** Google does not
expose other people's page analytics. Search Console gives real page-level impressions,
clicks and position — but only for properties you OWN and verify, so it is useful for
bestcooksclub.com's own pages later and useless for judging a publisher's recipe.

That is why the coverage constraint above is STRUCTURAL rather than a tooling gap: page-level
performance on sites you do not own is only available from estimator vendors (SEMrush,
Ahrefs, Similarweb), and will therefore always be a bonus tier.

**So be precise about what Google buys.** `UrlSeed` gives what a page is ABOUT and how big
those queries are — the DENOMINATOR. It does not give that page's traffic — the NUMERATOR
still comes from SEMrush. Volume alone lifts the DEMAND axis to ~100% coverage, but CAPTURE
cannot follow it: capture needs both, so it stays tier-1 at ~53% no matter what Google
provides.

**Recommendation: prefer Semrush.** Google would add a second vendor, a developer-token
application and an ad-spend requirement to obtain a denominator Semrush already sells through
an account that exists. Google is worth revisiting only if Semrush unit costs prove
prohibitive at the cadence wanted, or if a Google-native volume figure is wanted as a
cross-check on Semrush's estimates.

Sources:
[Keyword Planning overview](https://developers.google.com/google-ads/api/docs/keyword-planning/overview) ·
[GenerateKeywordIdeas](https://developers.google.com/google-ads/api/docs/keyword-planning/generate-keyword-ideas) ·
[GenerateKeywordHistoricalMetrics](https://developers.google.com/google-ads/api/docs/keyword-planning/generate-historical-metrics) ·
[Developer Token](https://developers.google.com/google-ads/api/docs/api-policy/developer-token)

## What I would flag honestly

* **Capture needs a keyword export you do not pull today**, and matching a page to *its*
  keyword is fuzzy. `dish_keywords` (114,515 rows) gives a top keyword per URL — a good
  start, not the full query set.
* **Momentum is confounded by publisher trajectory.** Normalise within publisher.
* **Three SEMrush windows means three exports per publisher**, tripling that workflow cost.
  Worth it for a curated set, probably not for 300 domains — another reason to prefer Google
  for the demand axis.
* **Volume figures are Google-centric**, and `dish_keywords.answer_engines` already shows
  pages surfaced by `google-ai`. A page surfaced in an AI answer may lose clicks while
  gaining reach, which would decouple traffic from actual influence.
* **This still ranks provenance and demand, not the dish itself.** Artifact scoring
  (soundness, completeness, fidelity — the cook-rework validators already do this and throw
  the result away) is the only axis that judges the recipe. It belongs here as the tier-3
  floor and as a complement, never as a taste judgement: an AI cannot know what tastes
  better, and any system claiming otherwise is laundering a guess.
