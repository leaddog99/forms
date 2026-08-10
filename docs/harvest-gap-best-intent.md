# Harvest gap list — dishes with high 'best' comparison intent, not yet covered

Generated 2026-08-10 from `docs/Recipe_all-keywords_us_2026-08-10.xlsx` (top 10,003 US keywords) cross-referenced against
`dishes.queries` — the actual Google phrases each dish searches, NOT the dish title.
Titles are labels; queries are what people type, and matching on titles produced two
wrong answers (it paired *Broccoli* with `broccoli cheddar soup` and missed the
1.22M-volume singular `chocolate chip cookie recipe` that the dish already targets).

## What 'ratio' means, and why it is the selection signal

`ratio = volume('best X') / volume('X')` — the share of searchers who want to be told
WHICH ONE, not just given a recipe. That is the only query class this product answers
better than Google does, so it is the demand signal that matters here.

Measured across 263 matched pairs in this file the overall ratio is **8.1%**. Every
high-ratio dish below shares one property: **a known failure mode**. Dry pork chop,
grey prime rib, weeping deviled eggs, gluey mashed potato, grainy mac sauce. People
who have been burned want a verdict. Every LOW-ratio term is a format rather than a
dish — `air fryer recipes` 1.2%, `instant pot recipes` 0.7%, `dinner ideas` 0.7%,
`recipes` 0.4% — because there is no single best *category*. Formats are excluded here.

**Rule: harvest dishes with a contested technique, not appliances or occasions.**

## The list

66 keywords · ratio >= 10% · base >= 10,000/mo · not covered by any dish query
· combined 'best' volume **715,900/mo**

| keyword | base/mo | 'best'/mo | ratio | KD | intent |
|---|---:|---:|---:|---:|---|
| ramen recipes | 40,500 | 110,000 | 272% | 45 | Informational, Commercial |
| sushi rice recipe | 40,500 | 60,500 | 149% | 51 | Informational |
| burger recipe | 12,100 | 12,100 | 100% | 44 | Informational, Commercial |
| hamburger recipe | 14,800 | 9,900 | 67% | 43 | Informational |
| baked chicken recipe | 12,100 | 5,400 | 45% | 46 | Informational |
| pork chop recipe | 27,100 | 9,900 | 37% | 42 | Informational, Commercial |
| green bean recipe | 14,800 | 5,400 | 36% | 47 | Informational |
| quiche recipes | 14,800 | 5,400 | 36% | 52 | Informational |
| chicken thigh recipe | 18,100 | 6,600 | 36% | 53 | Informational |
| grilled cheese recipe | 18,100 | 6,600 | 36% | 37 | Informational, Commercial |
| mashed potato recipe | 27,100 | 8,100 | 30% | 71 | Informational, Commercial |
| hard boiled egg recipe | 12,100 | 3,600 | 30% | 54 | Informational |
| pickled onions recipe | 12,100 | 3,600 | 30% | 30 | Informational |
| pasta salad recipes | 22,200 | 6,600 | 30% | 47 | Informational, Commercial |
| cake recipes | 14,800 | 4,400 | 30% | 36 | Informational, Commercial |
| baked potato recipe | 49,500 | 12,100 | 24% | 55 | Informational, Commercial |
| mac n cheese recipe | 40,500 | 9,900 | 24% | 65 | Informational, Commercial |
| prime rib roast recipe | 22,200 | 5,400 | 24% | 47 | Informational, Commercial |
| beef brisket recipe | 14,800 | 3,600 | 24% | 46 | Informational, Commercial |
| sweet potato recipe | 18,100 | 4,400 | 24% | 50 | Informational, Commercial |
| chuck roast recipe | 12,100 | 2,900 | 24% | 48 | Informational, Commercial |
| waffle recipe | 135,000 | 27,100 | 20% | 59 | Informational, Commercial |
| turkey recipe | 33,100 | 6,600 | 20% | 57 | Informational |
| pizza crust recipe | 27,100 | 5,400 | 20% | 50 | Informational, Commercial |
| banana bread recipe moist | 18,100 | 3,600 | 20% | 56 | Informational, Commercial |
| steak marinade recipe | 18,100 | 3,600 | 20% | 47 | Informational, Commercial |
| burger recipes | 22,200 | 4,400 | 20% | 51 | Informational, Commercial |
| chicken parm recipe | 14,800 | 2,900 | 20% | 51 | Informational, Commercial |
| chocolate cookie recipe | 14,800 | 2,900 | 20% | 50 | Informational |
| buttercream frosting recipe | 90,500 | 14,800 | 16% | 59 | Informational, Commercial |
| chicken soup recipe | 90,500 | 14,800 | 16% | 50 | Informational, Commercial |
| spaghetti recipe | 33,100 | 5,400 | 16% | 52 | Informational |
| cherry pie recipe | 27,100 | 4,400 | 16% | 50 | Informational, Commercial |
| homemade bread recipe | 27,100 | 4,400 | 16% | 42 | Informational |
| chili recipes | 22,200 | 3,600 | 16% | 52 | Informational, Commercial |
| chili recipe with beans | 18,100 | 2,900 | 16% | 42 | Informational, Commercial |
| mashed potatoes recipe | 201,000 | 27,100 | 13% | 72 | Informational |
| mac and cheese recipe | 246,000 | 33,100 | 13% | 54 | Informational |
| chili recipe | 550,000 | 74,000 | 13% | 67 | Informational, Commercial |
| salmon recipe | 110,000 | 14,800 | 13% | 53 | Informational, Commercial |
| chocolate cake recipe | 135,000 | 18,100 | 13% | 71 | Informational |
| fried chicken recipe | 74,000 | 9,900 | 13% | 45 | Informational, Commercial |
| red velvet cake recipe | 49,500 | 6,600 | 13% | 52 | Informational, Commercial |
| chicken wings recipe | 33,100 | 4,400 | 13% | 46 | Informational |
| bread machine recipes | 27,100 | 3,600 | 13% | 35 | Informational, Commercial |
| short rib recipe | 27,100 | 3,600 | 13% | 59 | Informational |
| hollandaise sauce recipe | 22,200 | 2,900 | 13% | 54 | Informational |
| mocktail recipes | 22,200 | 2,900 | 13% | 56 | Informational, Commercial |
| cookie recipes | 110,000 | 12,100 | 11% | 48 | Informational, Commercial |
| carrot cake recipe | 135,000 | 14,800 | 11% | 62 | Informational, Commercial |
| soup recipes | 135,000 | 14,800 | 11% | 56 | Informational, Commercial |
| prime rib recipe | 60,500 | 6,600 | 11% | 59 | Informational, Commercial |
| turkey brine recipe | 60,500 | 6,600 | 11% | 49 | Informational, Commercial |
| casserole recipes | 49,500 | 5,400 | 11% | 40 | Informational, Commercial |
| fluffy pancake recipe | 49,500 | 5,400 | 11% | 56 | Informational, Commercial |
| salad recipes | 49,500 | 5,400 | 11% | 54 | Informational, Commercial |
| smash burger recipe | 49,500 | 5,400 | 11% | 40 | Informational |
| smoothie recipes | 49,500 | 5,400 | 11% | 47 | Informational, Commercial |
| moist banana bread recipe | 33,100 | 3,600 | 11% | 42 | Informational |
| oxtail recipe | 33,100 | 3,600 | 11% | 44 | Informational, Commercial |
| stuffed pepper recipe | 33,100 | 3,600 | 11% | 59 | Informational, Commercial |
| asparagus recipe | 40,500 | 4,400 | 11% | 43 | Informational |
| broccoli cheddar soup recipe | 40,500 | 4,400 | 11% | 55 | Informational, Commercial |
| turkey burger recipe | 40,500 | 4,400 | 11% | 50 | Informational, Commercial |
| alfredo recipe | 27,100 | 2,900 | 11% | 48 | Informational, Commercial |
| beef short ribs recipe | 27,100 | 2,900 | 11% | 50 | Informational |

## Where to start — high ratio, soft door

Sort by KD ascending among ratio >= 15%; those are real comparison intent behind a
door the big publishers have not optimised for. They out-optimise everyone on the head
term (`X recipe`) and largely ignore `best X recipe`, which is why KD is often 10-20
points lower on the comparison variant.

| keyword | ratio | 'best'/mo | KD |
|---|---:|---:|---:|
| pickled onions recipe | 30% | 3,600 | 30 |
| cake recipes | 30% | 4,400 | 36 |
| grilled cheese recipe | 36% | 6,600 | 37 |
| pork chop recipe | 37% | 9,900 | 42 |
| homemade bread recipe | 16% | 4,400 | 42 |
| chili recipe with beans | 16% | 2,900 | 42 |
| hamburger recipe | 67% | 9,900 | 43 |
| burger recipe | 100% | 12,100 | 44 |
| ramen recipes | 272% | 110,000 | 45 |
| baked chicken recipe | 45% | 5,400 | 46 |

## Caveats — read before acting

* **This file is the top 10,003 US keywords.** A dish absent from it is not a dish
  without demand; it is a dish below the cut. The whole Greek set is invisible here,
  and so is anything under roughly 5,000/mo. Do NOT read absence as a verdict.
* **Ratio needs both variants present.** A dish whose `best X` form did not make the
  top 10k has no measurable ratio, not a zero one.
* **A few near-duplicates survive** (`mac and cheese` vs `mac n cheese`, singular vs
  plural). Treat them as one target; the higher-volume form is usually the real one.
* **Volume is not value.** `chocolate chip cookie recipe` is 246,000 'best'/mo at KD 59;
  `pork chop recipe` is 9,900 at KD 42. The second may be the better first move.
* **AI Overviews target exactly this query class.** Comparison intent is what Google
  now answers inline. Being the citable ranking is worth something; the durable value
  is what sits downstream of the click (save, cook view, book).

Related: [[project_two_stage_selection]], [[project_dish_catalog_table]],
`docs/recipe-scoring-design.md` §9 (demand and capture).
