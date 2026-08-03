# System-wide domain scoring

> Status: **SHIPPED** 2026-06-23 on `split/enrichment-api`.
> Code: `input/pipeline/domain_scoring.py` · wired in `collections_lib.harvest_publisher_top`
> · job `domain_scoring` (`save_recipe_api._handle_domain_scoring_job`) · weekly schedule
> · `POST /domains/rescore` · `GET /collections/leaderboard` · Rescore button on `domains.html`.
> Memories: `project_domain_scoring`, `project_ou_power_blend`, `project_paid_pa_calibration`, `feedback_single_path`.

## The problem

A publisher harvest used to rank a publisher's recipes by **raw PA**
(`collections_lib.harvest_publisher_top` set `rank_score = m["pa"]`). Two defects:

1. **Raw PA ignores DA context.** A PA-40 page on a DA-90 domain is *under*-performing;
   PA-40 on a DA-50 domain *over*-performs. Raw-PA ranking quietly rewards big domains.
2. **Not comparable across publishers.** Two publishers' `rank_score`s couldn't be put on
   one axis — there was no "best recipes anywhere" number.

## The score

The corpus-grain generalization of the per-dish OU/power blend
(`project_ou_power_blend`). One **system-wide** fit, not per-dish/per-cohort:

- Fit **one** quadratic `PA = a₀·DA² + a₁·DA + a₂` over the **whole scored population**
  (every `dish_run_data_points` row UNION every `collection_members` row, deduped by URL).
- `OU = adjPA − predicted_PA(DA)` — over-performance vs the global curve.
- `power = DA + adjPA` — raw authority.
- `rank_score = ((100−w)·pct(OU) + w·pct(power)) / 100`, where each percentile is taken
  **against the whole-system distribution** and `w = POWER_BLEND_WEIGHT` (default 30 →
  OU 70 / power 30). Same blend weight as dishes.

`adjPA` is the **paywall PA-remap** (`project_paid_pa_calibration`): for flagged gated
publishers, the link-starved page PA is lifted to its free-equivalent
(`max(pa, free_mean + (pa − paid_mean)·(free_std/paid_std))`) *before* scoring, so OU
stops penalizing them for the paywall instead of for quality. The curve is fit on **raw**
PA~DA (it should reflect the organic link landscape); the remap only relocates gated pages
onto that curve at scoring time — exactly how `score_data_points_for_dish` does it.

### Why single system-wide (not per-field)

One comparable score → a cross-publisher "best recipes anywhere" leaderboard; most data →
the most robust fit; simplest v1. Per-chapter/cuisine fits are a later refinement *if*
fields skew it. (Dishes went per-cohort because each SERP's competitive field differs;
for a global domain score that cross-field comparability is the whole point.)

### The safety property — winner picks don't churn

Inside a single publisher, **DA is ~constant**, so OU and power are both monotonic in PA,
so the blended percentile is monotonic in PA too. Therefore switching from `rank_score = pa`
to the system-wide score **does not change which recipes a publisher keeps** (the `selected`
top-N) — it only turns `rank_score` into a cross-publisher-comparable number. Low regression
risk by construction. (Verified live: 177milkstreet's stored ranking stayed monotonic in PA
after the rescore.)

## Single-path reuse

- Global fit reuses `chapters._fit_da_pa` — the **same pinned-quadratic** shape every other
  OU fit uses (no model-flip jitter).
- Paywall remap reuses `domains_lib.get_paywall_calibrations` (the calibration on the
  domains master, `n ≥ 15` guard).
- The percentile blend is the same OU/power idea as `blend.rank_by_blend`, just percentiled
  against a **stored whole-system distribution** instead of within the passed list.

## The fit store

The fit is a **computed artifact** (the corpus analog of `dishes.last_ou_fit`), *not* a
`system_config` setting — so it lives in its own single-row table `domain_score_fit`:

```
domain_score_fit(id=1, fit TEXT, computed_at TEXT)
```

`fit` JSON carries: `coefficients [a₀,a₁,a₂]`, `r2_chosen`, `sigma_*`, `weight`, `n`,
`n_paywall_calibrated`, and **101-point quantile sketches** `ou_quantiles` /
`power_quantiles`. The sketches are a compact, persistable summary of the whole-system OU
and power distributions: percentile lookups interpolate against them (`np.interp`), so the
**live harvest** and the **scheduled batch** percentile through the *same* stored
distribution — a freshly harvested publisher is immediately on the same scale the batch uses,
without storing every raw point.

## Two entry points (one math)

- **Live harvest** — `domain_scoring.score_members(members)` stamps each member with
  `adjusted_pa`, `ou`, `power`, `ou_pct`, `power_pct`, `rank_score` using the stored fit.
  No fit yet → `rank_score` falls back to raw PA (byte-identical to the old behavior), so the
  harvest never hard-depends on a prior batch run.
- **Scheduled batch** — `domain_scoring.recompute_and_rescore(conn)`:
  `compute_global_fit` (refit + rebuild sketches + store) → `rescore_all_members` (recompute
  `adjusted_pa` + `rank_score` for every member and re-rank within each publisher; `selected`
  is left untouched per the safety property). Runs weekly via `scheduled_jobs` (the corpus
  drifts slowly); also on demand via `POST /domains/rescore` and the **Rescore** button on
  the domains page.

## Two stages, two signals — why this is not an age contest

**Selection and ranking are deliberately different signals.** Candidates come from the
SEMrush Organic Top-Pages export, which is ordered by the **previous month's organic
traffic** (`collections_lib` line ~600: the Top-Pages shape ranks by `Traffic`, the
backlinks shape by referring domains) — so the pool is what readers are cooking *now*,
not an all-time archive. **OU then ranks within that pool**, asking whether a page earned
disproportionate third-party endorsement relative to its own domain's baseline; traffic
breaks ties where PA saturates across a publisher's pages.

**Why both are needed:** traffic alone rewards whatever is merely hot, and OU alone would
reward whatever has been online longest accumulating links. Feeding a *current-demand*
pool into an *earned-authority* ranking is what keeps either failure mode from taking
over — the corpus never sees the stale long tail, and the hot-but-shallow page still has
to show endorsement to win. Judging the OU formula on its own, without the traffic-ranked
selection stage in front of it, will produce wrong conclusions about age bias.

Measured, for the record (smittenkitchen.com, 114 scored pages, one domain so DA is
constant): **correlation(PA, log10 external links) = +0.905** across the full PA range —
PA 50-52 pages carry 141-1,409 external links, PA 61-62 pages carry 5,232-18,534. Within a
domain, PA *is* third-party endorsement, which is what makes OU a page-level quality
signal rather than a publisher-size proxy.

## Next step (NOT BUILT) — the durability dimension

Traffic and excess-PA are **orthogonal**, and each covers the other's blind spot. Measured
on smittenkitchen.com (114 pages, one domain, medians traffic 380 / PA 55):

| quadrant | n | example |
|---|---|---|
| hi traffic / hi PA | 30 | sidecar 5,944 · pumpkin bread 2,489 |
| hi traffic / lo PA | 27 | how-to-hard-boil-an-egg **16,750**, PA 49 |
| lo traffic / hi PA | 21 | shakshuka (2010) 329 traffic, **PA 57** |
| lo traffic / lo PA | 36 | strawberry milk · broccoli pizza |

**Traffic is appetite** — something caught thousands of people's eye and they wanted to try
it. **Excess PA is advocacy** — having found it, someone thought it worth pointing others
at. They are not the same act, and the gap between them is informative: a page with heavy
traffic and below-median PA drew people in without moving them to recommend it. The
hard-boiled egg page at 16,750 visits and PA 49 is being read correctly, not mis-ranked.

**What is missing is the time axis.** Selection already proves a page is *currently* wanted
(the pool is last month's traffic), and OU already proves it *out-earned its siblings*. The
untapped signal is a page that has done **both, for years**: not merely "old and still
trafficked", but **old and still carrying excess PA** — survivorship of the residual through
everything the publisher has released since. A 2010 shakshuka still out-endorsing sixteen
years of newer posts is a stronger claim than any single-month measurement can make.

Sketch, using only data already held:

* Publish date is available (URL date segment for most publishers, else `datePublished`).
* Traffic and PA are already on `collection_members` per harvest.
* Hold demand constant by computing it **inside a dish cohort** — the dish-refresh path
  already builds one. An old recipe still out-trafficking and out-endorsing newer rivals
  *for the same dish* is durability of the artifact; raw traffic alone would only be
  measuring how popular the dish is.

Open: whether durability is a third ranked dimension, a multiplier on the blend, or simply
a display/editorial signal ("a decade-long favourite"). Do not treat age alone as merit —
the claim is sustained *excess*, never survival.

## Missing PA / DA

DA is domain-constant and Moz returns DA+PA together, so in a live harvest every scored
member carries both (`MOZ-FAIL` candidates are dropped before scoring). The missing-signal
paths matter mainly for the **batch rescore**, which reads every existing `collection_members`
row including any legacy/anomalous ones:

- **Fit population** — rows with NULL DA or PA are excluded from the regression *and* the
  quantile sketches (`_population`'s `WHERE … IS NOT NULL`). They can't skew the curve.
- **No global fit yet (bootstrap)** — every member, everywhere, falls back to `rank_score =
  raw PA`. The scale is uniform across the whole corpus, so ordering stays consistent. This
  is the documented old behavior until the first batch run.
- **Fit exists but a member lacks PA or DA** — `rank_score = NULL` (sorts last, excluded from
  the leaderboard). **Deliberately not raw PA:** a 0–100 PA value mixed into the 0–1 system
  scale would outrank every properly-scored recipe on the cross-publisher leaderboard. A
  missing signal can only fail to lift a page, never lift it. (Earlier draft fell back to raw
  PA here — fixed, it was a latent leaderboard-corruption bug.)

## Surfaces

- `collection_members.rank_score` / `.adjusted_pa` are written by both paths — already shown
  by `get_collection_top` (the domains top-recipes list + scored-cohort panel), so the new
  meaning flows through with no UI change.
- `GET /collections/leaderboard?limit=&selected_only=` — the payoff: publisher recipes across
  **all** collections ordered by the comparable `rank_score`, LEFT-JOINed to `master_recipes`
  for real name/grade/thumbnail. The basis for a future "best recipes anywhere" surface.

## First real run (2026-06-23)

Population **5,587** deduped (url, da, pa) points; quadratic R² **0.71**; **3** paywall-
calibrated publishers; rescored **729** members across **21** publishers. Milk Street (gated)
correctly lifted PA 43 → adjPA 62.9, its best recipe to `ou_pct = 100` — the calibration
letting a paywalled publisher's top page compete on the global scale.

## Not done / later

- **Per-field (chapter/cuisine) refinement** if the single global fit proves too coarse for
  some categories.
- Surface the leaderboard as an actual page (the endpoint exists; no UI yet).
- The dish `score_data_points_for_dish` SQL scorer stays canonical for the **dish** ledger;
  this module is the **publisher** ledger's scorer. They share the math and the paywall remap
  but write different tables.
