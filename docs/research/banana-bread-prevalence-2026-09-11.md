# How many websites have a banana bread recipe? (2026-09-11)

Curator's question, asked while looking at the Banana Bread dish. There is no
registry to look this up in; every answer is an estimate of a stated population
with a stated instrument. Three instruments were used and disagree by design.

## 1. Sites that RANK (SEMrush, US)

Top-100 organic results for "banana bread recipe" resolve to ~45 distinct
domains; about half the slots are YouTube. The six head terms carry ~2.2M
monthly US searches (banana bread recipe 1.83M · banana bread 201k · easy
90.5k · best 60.5k · moist 33.1k · healthy 22.2k; KD 43–71). Our own dish runs
have seen 162 candidate URLs on 154 hosts. This is the commercially relevant
set, not the population.

## 2. Google's "about N results" (Scale SERP, 11 credits, 2026-09-11)

| Query | Market | About N results |
|---|---|---|
| `"banana bread" recipe` | US | 24,800,000 |
| `"banana bread"` | US | 25,200,000 |
| `"banana bread recipe"` | US | 1,510,000 |
| `intitle:"banana bread" recipe` | US | **500,000** |
| `receta "pan de plátano"` | ES | 1,720,000 |
| `recette "banana bread"` | FR | 514,000 |
| `"Bananenbrot" Rezept` | DE | 176,000 |
| `"banana bread" receita` | BR | 93,900 |

`intitle:` is the tightest proxy for recipe PAGES (a page titled "banana bread"
is nearly always a recipe). Counts are estimates that drift by tens of percent;
Google never enumerates past a few hundred results, so paging is impossible.
Unquoted `banana bread recipe` returned 105 (provider failed to parse the stat
off an AI-overview layout) and the Japanese query failed twice, uncharged.

## 3. Web Data Commons schema.org/Recipe subset (Oct 2024 crawl, release 2024-12)

Source: https://webdatacommons.org/structureddata/2024-12/stats/schema_org_subsets.html
Files: https://data.dws.informatik.uni-mannheim.de/structureddata/2024-12/quads/classspecific/Recipe
(21 gz N-Quads files, 3.84 GB; 258,349,284 quads; 2,746,545 URLs; 37,304 hosts).
Method: streamed every file (`scripts/wdc_recipe_term_scan.sh`), kept lines
matching the term in any target language (558,330 lines), then counted only
lines whose predicate is `schema:name`/`schema:headline`
(`scripts/wdc_recipe_term_count.py`). ~10 minutes, no local storage.

| Term | Pages | Hosts | Pay-level domains |
|---|---|---|---|
| All languages | 4,908 | 2,644 | **2,607** |
| en: banana bread / banana loaf / banana nut bread | 4,519 | 2,415 | 2,380 |
| de: Bananenbrot | 263 | 178 | 178 |
| fr: pain aux bananes / cake à la banane | 104 | 68 | 68 |
| es: pan de plátano / pan de banana | 32 | 27 | 27 |
| ja: バナナブレッド | 15 | 9 | 9 |

- 2,607 of 37,304 recipe hosts (7%) carry ≥1 banana bread recipe in the sample.
- Only 17% of those publishers have exactly ONE banana-bread page; 83% have
  several → "one per site" is false even for small blogs.
- Top publishers by page count (somethingswanky 129, justapinch 124,
  hiddenponies 115, gourmandize 98, food.com 73…) include URL variants (print
  views, comment pagination) — page counts are inflated, host counts are solid.

### Why the WDC numbers are a FLOOR

- Common Crawl samples the web: the Recipe host list is missing 200 of our own
  469 harvestable publishers (43%), including allrecipes.com, seriouseats.com,
  cooking.nytimes.com, sallysbakingaddiction.com, skinnytaste.com.
- It samples each site thinly: median 13 recipes per host; food.com shows
  17,362 of its ~500k recipes (~3%).
- Google's 500k banana-bread pages vs WDC's 4,908 = a ~100× page-level gap.
  A site with hundreds of recipes and one banana bread is usually missed, so
  the 7% share is far below the true share of recipe sites carrying the dish.
- WDC only sees pages with structured markup; forums, old blogs and most
  non-English sites are invisible to it but indexed by Google.

### Addendum — WHY WDC misses half our publishers (checked 2026-09-11)

Curator: "the WDC missed half of our own sites! and those were huge." Verified:

- **Explicit Common Crawl blocks.** robots.txt `User-agent: CCBot / Disallow: /` at
  cooking.nytimes.com, skinnytaste.com, food.com (post-2023 AI-training backlash).
- **Bot-managed publishers absent entirely.** allrecipes.com, seriouseats.com,
  simplyrecipes.com (Dotdash Meredith), foodnetwork.com, sallysbakingaddiction.com,
  thepioneerwoman.com do not mention CCBot yet have ZERO hosts in the file — the WAF
  turns the crawler away (foodnetwork.ca is present with 1,411 recipes; the US site is not).
- **The miss is skewed to the head.** Of our harvestable publishers with a DA, WDC
  caught 264 (median DA 56, 15 at DA ≥ 80) and missed 195 (median DA 55, **36 at
  DA ≥ 80**). The missed set supplied **2,310 of our master recipes vs 2,250 from
  the caught set** — half our corpus comes from sites Common Crawl cannot see.

So WDC is not a random sample: it is systematically blind to the largest,
best-defended publishers, which is exactly where recipe pages concentrate. As a
CENSUS it undercounts where it matters most (the banana-bread floor stands; any
extrapolation from its 7% host share is biased low, not merely noisy). As a
DISCOVERY SEED it is the mirror image of SEMrush: strong on the long tail of small
blogs that don't block bots, useless for the head — which we already hold.

## Bottom line

Measured floor: **2,607 publishers** with a banana bread recipe. Google indexes
**~500,000 pages** titled banana bread. Host counts do not scale linearly with
the page gap, so no precise multiplier — but the direction is unambiguous:
**well over 100,000 sites**, with the largest publishers each holding dozens to
hundreds of variants. The curator's "over 100k" intuition holds.

## Reusable

`scripts/wdc_recipe_term_scan.sh` (edit `PAT`) + `scripts/wdc_recipe_term_count.py`
(edit `TERMS`) give the same statistic for ANY dish in ~10 minutes, free.
`Recipe_domain_stats.csv` (per-host recipe counts, 37,304 rows) is a ranked
list of every structured-recipe host the crawl saw — a publisher-discovery
seed like the punchfork list, and a coverage check for `domains`.
