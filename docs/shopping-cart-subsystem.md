# Shopping-cart subsystem (Instacart et al.)

*Split out of docs/dish-product-matching.md 2026-08-29 — curator: "a
separate subsystem; it doesn't fit well here." Design only; nothing built.*

## Why it's its own subsystem

Everything in dish-product matching is DISH-grain, rendered, and curated
(classes approved per dish, picks medaled per class). The cart is none of
those:

- **Recipe-grain**: the unit is one recipe's ingredient list, not a dish's
  class set.
- **Interactive, not rendered**: the reader BUILDS state (checks/unchecks)
  before anything monetizes. The commerce block is read-only.
- **A program, not a curation**: there are no picks. What gets curated is
  the integration itself — which marketplace, the line-item mapping, the
  affiliate terms — once, on the /go/ rail
  (docs/affiliate-programs-and-clicks.md).

## The design (settled in discussion, 2026-08-29)

NOT "whole ingredient list -> cart" (curator: "odds are I don't need
everything"). The flow:

1. On the recipe (site-side, eventually cook view too), the ingredient list
   renders as a **checklist** — pantry staples default-UNCHECKED (salt,
   flour, butter, oil…; the staples list is itself curatable, and the
   corpus-wide ingredient document-frequency from dish_signals is a ready
   seed: very high corpus % ≈ staple).
2. The reader assembles the shortlist IN OUR SURFACE.
3. **Push the selected subset** to Instacart (their cart/deep-link API)
   when ready to shop. Money = per-order affiliate.

## What it shares with the commerce stack

- The **/go/ program rail** (click-time codes, program terms) — same
  infrastructure, different payload (a cart, not a link).
- **Layer-5 measurement**: the check/uncheck pattern is data — what readers
  actually buy vs already own, per ingredient, per dish. Feeds the staples
  list, the gourmet class priors, and the taste-profile work
  (memory: project_recipe_activity_engagement).

## Boundaries

- Renders as a UTILITY on the recipe ("🛒 shop this recipe"), never inside
  the ranked product block, never competes in the EV bandit.
- Line-item resolution (our ingredient text -> their SKUs) is the
  marketplace's job via their API; we don't curate groceries.
