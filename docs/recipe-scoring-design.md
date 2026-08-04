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

## The coverage constraint (read before designing anything)

**Traffic exists only as a by-product of a SEMrush publisher harvest.** Measured 2026-08-03:

```
master_recipes            : 4,148
  with harvest traffic    : 2,181  (53%)
  with a dish keyword     : 2,324  (56%)
publishers with traffic   :    86 of 841  (10%)
```

So every traffic-derived term covers **half the corpus and a tenth of publishers**, and is
biased toward whichever publishers happened to be harvested. Traffic-derived signals
therefore **cannot be core ranking terms** — they must be a bonus tier that degrades
gracefully (see Tiers below).

This is the single biggest constraint on everything else in this document.

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

## Trajectory — the 1/6/12-month diffs

    momentum = traffic(1mo) / (traffic(12mo) / 12)
       >1.3 rising      ~1.0 stable      <0.7 fading

**Flat-and-high over twelve months IS evergreen** — measured, not inferred from a publish
date. A 2010 recipe with `1mo ~= 6mo ~= 12mo/12` has proven durability; a 2010 recipe on a
declining curve is a fading classic.

**Seasonality becomes a feature, not noise.** A hard 1mo spike against the 12mo baseline
means the dish is *in season now* — exactly what an editorial "cook this in October" surface
wants. Use the 6mo window as the smoother when the trend is wanted without the season.

Confound to handle: momentum is contaminated by the **publisher's own trajectory** — a
growing site lifts every page's 1mo. Normalise within publisher, the way OU already does.

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

## The archetypes it produces

| archetype | signature | surface |
|---|---|---|
| **Star** | high magnitude, ~45 deg, flat arrow | the medal pick |
| **Evergreen** | high magnitude, flat arrow, years of it | "a decade-long favourite" |
| **Rising** | steep positive arrow | "trending now" |
| **Hidden gem** | high angle (advocacy >> capture) | editor's find |
| **Commodity** | high traffic, LOW capture, low angle | utility — do not medal it |
| **Fading** | negative arrow, past advocacy | re-review or retire |
| **Unproven** | no endorsement data yet | artifact score only |

The last row is the cold-start answer. A three-day-old recipe has no capture, no advocacy and
no trajectory — the artifact checks are the only thing that can speak for it.

## Tiers — how it degrades

Because of the coverage constraint, this is not one formula but three:

| tier | available | score |
|---|---|---|
| **1** — harvested publisher | volume + traffic + advocacy + trajectory | full vector, capture included (page traffic is SEMrush-only, so this tier can never exceed harvest coverage) |
| **2** — most of the corpus | volume + advocacy | dish demand x advocacy, no capture |
| **3** — fresh grab / new URL | neither | artifact soundness only |

## Getting volume — the Google Ads API

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

**Recommendation:** apply for Basic Access — the URL-seed call is well matched to this
corpus, and even bucketed volume fixes the cross-dish comparison that traffic alone cannot.
Keep capture tier-1-only and do not let it become load-bearing until the precision level is
known.

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
