# Experiment: LLM-derived equipment-with-context vs signal mining

2026-09-01. Curator's question: the equipment signals emit "knife" but the
catalog will hold many knife CLASSES — can reading the method text supply the
disambiguating context ("chop the onion" → chef's/santoku, "slice the bread"
→ bread knife)? And given <300 dishes and infrequent updates, should the LLM
just do more of the matching work directly?

## Step 1 — mechanical verb–object reconnaissance (free)

Regex over cohort instructions, fuzzy-deduped pairs, df per dish. Finding:
real cross-cohort regularity EXISTS — `pound + chicken` 7/53 (meat mallet),
`pat + dry` (paper towels), `cut + butter` "cut in butter" (pastry cutter!),
`line + baking sheet`, `toss + apples`. But the noise ceiling is low:
"brown + sugar" topped ABB (ingredient misread as verb), object extraction
leaks determiners, and a `bake until the salmon` line exposed nearest-rung
cohort contamination. `chop`/`slice` pairs specifically were BELOW df=3 in
both test dishes. Verdict: raw material is present; parsing is the wrong tool.

## Step 2 — Sonnet, Baked Chicken Breast, top-5 methods (~3¢)

Asked for specific purchasable classes, evidence-quoted, essential-vs-helpful
with workarounds, disagreements flagged, contaminants excluded. Result:

- **Boning or paring knife** (stuffed variant: "cut a slit or pocket…") vs
  **chef's knife demoted to helpful** ("any sharp knife can slice rested
  chicken") — the exact knife-context answer the signals can never give.
- Meat mallet, instant-read thermometer (3 quoted evidence lines),
  toothpicks (stuffed variant), basting brush w/ workaround.
- **Found the Baked Salmon contaminant unprompted** and excluded it.
- **Disagreement as information**: baking dish vs rimmed sheet vs
  stovetop-skillet-with-lid, flagged per recipe.
- Weakness: variant-only tools marked ESSENTIAL (no dish-wide/variant split
  in the prompt), and top-5 gives no prevalence.

## Step 3 — Sonnet, Apple Brown Betty, top-10, adjusted prompt (~4¢)

Added: scope = core|variant|helpful (variant NAMED), counted prevalence k/N,
same evidence/disagreement/contaminant rules. Result:

- **Paring knife [core] 8/10** ("peel and core apples then slice thinly") —
  the knife answer again, correctly paring not chef's for this dish.
- **Food processor [variant: crumb-topping recipes] 3/10** vs "crumble the
  bread by hand"; **pastry blender [variant: solid-butter crumble] 2/10**
  ("cut butter into dry ingredients until pebbly") — the step-1 `cut+butter`
  pair, now properly contextualized and scoped.
- **Foil cover [variant: covered-then-uncovered steam bake] 4/10**;
  **citrus reamer [helpful] 7/10** with the hand-squeeze workaround.
- Vessel disagreement enumerated: 8x8, 11x7, 13x9, 9x9, 9-in round, 1.5-qt
  casserole — matches the pan-variance seen everywhere.
- **Found TWO contaminants unprompted**: Hash Brown Patties and a Bushwacker
  COCKTAIL sitting in the ABB cohort (nearest-rung strays) — free cohort
  hygiene as a side effect.

## Operational finding (load-bearing)

`stop=max_tokens` with ONLY a thinking block: the model can burn the entire
max_tokens budget on extended thinking and return NO text. This was the real
cause of the intermittent "model reply unusable" / empty replies — including,
retroactively, some proposer thinness. Fixed: proposer MAX_TOKENS 3000→8000,
story-draft 1500→6000; experiments run at 9000. RULE: counting/analysis
prompts need generous budgets because thinking shares them.

## Verdict

The LLM path wins decisively on context, scoping, disagreement, and free
contaminant detection; the mechanical side's only residual advantage
(prevalence counts) is absorbed by asking the model to count over 10-15
supplied recipes. Cost at catalog scale: ~$10-15 for all dishes, refreshed
rarely. Proposed next build: a per-dish `equipment_profile` job (persist:
value + method + inputs on the dish row) storing this output, feeding or
replacing the equipment half of dish_class_propose.
