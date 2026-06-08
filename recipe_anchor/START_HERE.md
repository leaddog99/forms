# Recipe Optimization — Claude Code starting point

You are evolving the **step-anchored / bundling** recipe capability inside the existing
PyCharm application. This folder is a prototype + specification, NOT the app. Adapt every
idea to the app's real models, renderer, and product source; where they disagree, the app
wins. Carry over the *rules*, not this file layout.

## Read in this order
1. `recipe-optimization-handoff.md` — the integration plan, data-model requirements,
   validator set, and cost architecture. Start here.
2. `skills/recipe-rework/SKILL.md` — the callable operating procedure (phases + gates).
   This is what should seed the in-app skills store. The condensed, enforceable spec.
3. `recipe-rework-playbook.md` — the long-form rationale behind every rule.

## Prototype code (reference implementation of the data→render split)
- `models.py`      — RecipeDoc and friends (the single source of truth)
- `prompts.py`     — the two LLM passes (extract, anchor) + forced-tool schemas
- `pipeline.py`    — fetch → clean → extract → anchor → assemble
- `render.py` + `templates/recipe.html.j2` — deterministic render
- `products.py`    — category→product lookup (LLM never sees e-commerce data)
- `app.py`         — FastAPI surface
- `example_vongole.py` / `sample_output.html` — runnable fixture + proof

## Rendered examples of the target output (open in a browser)
- `examples/lasagna.html`           — most evolved: ingredient *views* (Shopping / In-order
  / Bundles), left checkboxes, autoscroll on instructions only, form pickers, pre-portion step
- `examples/pastitsio.html`         — bundling + form variants + dose-to-taste
- `examples/spaghetti-vongole-v2.html` — the simpler reference

## The rules that emerged (all detailed in handoff + SKILL)
- No lookback; every measure real; no measuring mid-cook (definite-article: method refers
  back, never introduces); the page reflects decisions already made.
- Section order: Ingredients → Equipment → Bundles → Instructions.
- Ingredients are ONE list; Shopping / In-order / Bundles are VIEWS of it. Bundles is a
  view, never a step (a "do the bundles" step reintroduces lookback).
- Ingredient vs USE: an ingredient (purchasable) has one or more uses (deployments). Cook
  views list uses at each moment; Shopping GROUPS uses under the ingredient and shows each
  use's "amount — what it's for" — do NOT auto-sum (the cook decides; e.g. fresh vs bagged
  mozzarella). Max two-three uses; the math is theirs.
- Bundling: pre-combine co-added ingredients; keep separate when combining early harms.
- Form-variant carries name, amount, prep_verb, AND aisle (dry basil=Pantry, fresh=Produce).
- Two kinds of prep: regrouping lives in the Bundles view; standing tasks (heat oven, press
  ricotta, boil water) ARE steps, ordered by lead time (oven usually first).
- Units: lb/oz/cup/qt/in/°F convert and stay single-system per line; tsp/Tbsp/pinch and
  COUNTS are unit-neutral, shown identically in both systems (don't turn 1 tsp into 0.9 g).
- Variable-count items (garlic cloves, shallots, ginger): measure the PREPARED amount
  (2 Tbsp / 10 g minced) as the real quantity; count is a shopping hint. Reference: a
  medium clove ≈ 1 tsp minced ≈ 4–5 g. Count leads only when the piece-form matters.
- Encoding: store data as plain text (bare "&"); encode only at the HTML boundary; never
  put HTML entities into textContent.

## Validators to build as code (gate the build; don't rely on eyeballing)
unit-consistency · definite-article / no-introduce · every-measure-numeric · no-lookback ·
mise-complete · size-where-it-matters · form-resolves-forward · to-taste-staged ·
concurrency-honest · reuse-referenced · bundling · encoding (no entity in textContent).
