# Dish candidates from keyword data — spec

Turning the keyword export into a ranked, explained queue of dishes worth harvesting.
Written 2026-08-10 off the `docs/harvest-gap-best-intent.md` analysis.

## The one rule that shapes everything: PROPOSE, NEVER CREATE

`memory/project_curate_staff_inputs` records the curator's objection when an automated
run invented its own product categories:

> **Categories decide what gets recommended at all — nothing automated should be
> inventing them.**

A dish is the same shape with higher stakes: it decides what gets harvested, what gets
extracted, what enters master, and it mints an **immutable join key** (`dishes.name`,
see [[project_dish_display_name]]). So this pipeline writes to a **candidates table**
and a review surface. A human approves; approval is what creates the dish.

It also follows the curator's stated preference on ranking (same memory): **rank AND
explain, do not hide the reasoning in a composite score.** They explicitly rejected
per-criterion numeric scoring for products — "it might ask more questions than it
answers" — and chose explanation instead. Same here: show ratio, volume and KD as
themselves, with a one-line reason, not a blended 0-100.

## What makes a candidate

Measured over 263 matched pairs in the top-10k US keyword file (2026-08-10):

| signal | what it is | why it matters |
|---|---|---|
| **ratio** = `vol('best X') / vol('X')` | share of searchers who want to be TOLD WHICH | the only query class this product answers better than Google. Overall 8.1%; the useful band is >= 10% |
| **base volume** | market size | a 300% ratio on 500 searches is noise |
| **KD of the `best X` term** | difficulty of the comparison query | publishers optimise the head term and ignore the comparison — KD is often 10-20 points lower there. That gap IS the opportunity |
| **not a format** | dish vs appliance/occasion | `air fryer recipes` 1.2%, `instant pot recipes` 0.7%, `dinner ideas` 0.7%, `recipes` 0.4%. There is no single best CATEGORY |

**The pattern behind high ratio: a known failure mode.** Dry pork chop, grey prime rib,
weeping deviled eggs, gluey mashed potato, grainy mac sauce, soggy fried chicken. People
who have been burned want a verdict. That is a better plain-language filter than any
threshold, and it is what a reviewer should sanity-check each candidate against.

Three dishes where `best X` OUTDRAWS `X` — ramen (272%), sushi rice (149%), burger
(100%). For those the choosing IS the search.

## Dedupe must be SEMANTIC, not string

`dishes_vec` + `vector_store.find_similar_dishes` already exist. Use them: embed the
candidate keyword, KNN against the dish index, and suppress anything inside the existing
match bar.

String matching demonstrably fails here — the first pass at this analysis matched on
dish TITLES and produced two wrong answers:

* paired **Broccoli** with `broccoli cheddar soup recipe` (a different dish)
* reported `mac n cheese recipe` and `chocolate chip cookie recipe` as GAPS although
  *Macaroni and Cheese* and *Chocolate Chip Cookies* both exist

Matching against `dishes.queries` fixed both, because queries are the phrases people
actually type and titles are only labels. The vector index is the stronger version of
the same idea and handles abbreviation ("mac n cheese"), plural, and paraphrase.

**Candidate states:** `new` · `duplicate` (KNN hit — record WHICH dish, so the reviewer
can instead add the keyword as a query to the existing one) · `approved` · `rejected`
(with a reason, so the same keyword does not resurface every scan).

## THE TRAP: the query you SEARCH is not the query you RANK FOR

Do NOT put `best X recipe` into `dishes.queries`.

A SERP for `best pork chop recipe` returns **roundups and listicles** — exactly what the
harvest's collection/listicle pre-filter is built to discard. Searching the comparison
term would spend SERP credits to retrieve pages we then throw away.

The split:

* **`queries`** = the HEAD term (`pork chop recipe`) — how we FIND individual recipes
* **the comparison term** (`best pork chop recipe`) = what our resulting ranked PAGE
  targets in search. It is an SEO target, not a discovery input.

That argues for a new dish field — `target_keyword` (+ its volume/KD/ratio at capture
time) — separate from `queries`. Without it the distinction lives only in someone's head
and the first person to "helpfully" add the best-variant to `queries` will quietly poison
the funnel.

## Proposed queries and select size

For an approved candidate, seed `queries` with the head term and its close variants,
using the phase-1 row shape `{q, n, gl, hl}`:

    [{"q": "pork chop recipe",  "n": null, "gl": "us", "hl": "en"},
     {"q": "pork chops recipe", "n": null, "gl": "us", "hl": "en"}]

`n: null` means "use the dish's `top_n_serpapi`". **Do not invent a volume-to-`n`
formula.** The 6-12%-of-export depth finding (2026-08-09) was measured on FILE exports
for publisher harvests; there is no equivalent measurement for SERP dish batches, and
inventing one would be exactly the kind of plausible-but-unmeasured rule that has cost
this project time repeatedly. Leave `n` at the dish default until someone measures it.

Locale stays `us/en` — [[project_traffic_exceptionalism]] notes the curator's decision
to nail US before touching foreign locales.

## Where it runs

A job (`dish_candidate_scan`), per [[project_jobs_as_executables]] — schedulable,
DB-logged, visible in the Job Monitor:

1. read the keyword source
2. pair each `X` with `best X`, compute ratio
3. drop formats, drop below thresholds
4. KNN-dedupe against `dishes_vec`
5. upsert into `dish_candidates`, preserving `rejected` so nothing resurfaces
6. report counts + the reason for every suppression

Surface it in an ACDV editor cloned from the dish editor
([[feedback_editor_template_not_runtime]]), with Approve creating the dish and seeding
`queries` + `target_keyword`.

**Data source, today:** the manual xlsx export (`docs/Recipe_all-keywords_us_*.xlsx`).
Free, already on disk, and the whole gap analysis ran off it.

**Later:** the Semrush API — `phrase_these` batches up to 100 keywords per call at 10
units/line, so pairing 500 dish candidates with their best-variants is ~1,000 units.
Cheap. But note `SEMRUSH_KEY`'s balance is a ONE-TIME grant, not a monthly refresh
(smallest purchasable package is 2 MILLION units), so spend deliberately —
[[project_traffic_exceptionalism]] has the accounting.

## Open / unmeasured — do not treat these as settled

* **Is ratio stable over time?** Measured once, on one file. A dish whose ratio moves
  seasonally (cookies in December) would look different in another month. Nothing yet
  says how much it drifts.
* **Does high ratio actually convert?** The whole thesis is that comparison intent is
  the winnable, monetisable slice. That is reasoning, not measurement — there is no
  public surface yet, so no conversion data exists. Ship the queue, then check.
* **KD as a gate is borrowed judgement.** Semrush's difficulty index is a vendor model.
  It is a reasonable prior, not a measurement of our ability to rank.
* **AI Overviews target exactly this query class.** Comparison intent is what Google now
  answers inline. Being the citable ranking is worth something; the durable value is
  downstream of the click (save, cook view, book).
* **The source file is the top 10,003 US keywords**, so absence is not a verdict. The
  entire Greek set is invisible in it, as is anything under roughly 5,000/mo.

## Why this is worth building

66 uncovered keywords at >= 10% ratio and >= 10k base carry **715,900 'best' searches a
month**, and the curator's 155 existing dishes cover 542,500. So the queue is not a
marginal top-up — it is more addressable comparison demand than the entire current
catalogue, and it is selectable on evidence rather than intuition.

Related: `docs/harvest-gap-best-intent.md` (the current list),
`docs/recipe-scoring-design.md` §9 (demand and capture),
[[project_two_stage_selection]], [[project_dish_catalog_table]].
