# Dish-anchored product matching

*Written 2026-08-28, from the curator design discussion. Step 1 built the same
day; later steps are design only.*

## The problem

Recipe equipment is too coarse to match products directly: 52,906 equipment
items across the corpus, 4,180 distinct free-text names, **5% carry a size**,
and the `Tool` model holds only name+size — material/coating ("non-stick
needed?") are not representable, and publishers mostly don't state them anyway
("a saucepan"). Attribute-level matching starves on day one.

## The design: anchor on the dish, fuzz everywhere except the money join

The commerce model already mirrors recipes (Category ↔ chapter, **product
class ↔ dish**, product ↔ recipe — docs/reviews-and-product-commerce.md §2).
This design completes the mirror:

```
recipe ──(nearest dish, ALWAYS, embedding KNN)──▶ dish
dish   ──(curated junction, LLM-proposed)───────▶ product classes
class  ──(curated collection runs)──────────────▶ ranked product picks
recipe equipment ──(embedding match, later)─────▶ class   [customization]
```

Embeddings do **recall and routing** everywhere; a human signs the ONE join
that puts affiliate blocks in front of readers (dish→classes). That division
is deliberate — the AI-editor selection role was cancelled 2026-08-12 when
demand data inverted its ranking; fuzzy proposes, curator disposes.

## Step 1 (BUILT 2026-08-28) — every recipe resolves a dish

The resolution ladder, one value per master row:

1. `_master.dish` — curated membership (a dish refresh selected this row).
2. `_match.dish` — confident inference (KNN distance ≤ threshold, or the
   likelyDish name-exact override).
3. `_match.candidates[0].dish` — the NEAREST dish, no gate. Already stored by
   `dish_match.build_match` on every swept row; distance rides along.

Exposed as **generated columns** on `master_recipes` (persist-derived rule:
SQL-queryable, no compute-on-read):

- `dish_effective` — `COALESCE` of the three paths above.
- `dish_effective_source` — `'curated' | 'matched' | 'nearest'` — which rung
  answered, so surfaces can badge a loose inherit ("gear suggested from Beef
  Stew, loose match"). Distance itself stays in `_match` JSON.

Measured at build time: 7,924 master rows = 3,258 curated + 4,662 swept + 4
never-swept (fixed by one `dish_rematch` run) + 0 embedding-less. Coverage is
total.

The THRESHOLD IS UNCHANGED for catalog membership — `_match.dish` still gates
at 0.6 (the Pumpkin-Spice-Latte lesson, measured 2026-08-21). Rung 3 is a new
consumer of already-stored data, not a loosening.

## Step 2 (design) — canonical classes + embeddings

`product_classes` ALREADY EXISTS as a table (name, category, criteria,
buying_guide, data, embedding) — but holds 1 row while `products.product_class`
is free text that never joins to it. That free text is how one concept became
"Recycling" / "Food Recyclers" / "Food Recyclers (5 l)" in one afternoon.

Step 2: make `product_classes` the canonical registry. Every class gets an
embedding; **imports and curation runs embed-match their class guess against
the registry and snap to the nearest existing class** instead of inventing a
name. Low-confidence → create-new-class proposal for curator review.

## Step 3 (design) — the dish→classes junction

`dish_product_classes(dish_name, class_name, role, note, approved_by,
approved_at)` — the curated money join. Seeded cheaply: aggregate each dish's
cohort equipment by frequency → LLM proposes classes → curator approves in a
dish-editor block. ~224 dishes ≈ one review session. Open design choices
(curator's): role grain (essential/optional vs flat), whether the dish page
becomes an editorial "what you need for X" surface, add-only vs suppress
customization in v1.

## Step 4 (design) — per-recipe customization

Recipe's own equipment text embedding-matched to classes: ADD what the recipe
demands beyond the dish baseline (slow cooker variant), later SUPPRESS what it
replaces. Requires step 2. The technique-implied attributes (non-stick for
crêpes) belong to the cook-rework layer, which actually reasons about method —
stamped as *implied*, never invented.

## Multi-dish membership (settled 2026-08-28, verified live)

A recipe CAN belong to several dishes — this was already built (2026-06-14,
docs/collections.md): `dish_run_data_points` IS the membership junction, the
top lists are ledger-derived (`selected=1` per dish, each at its own rank),
and 79 URLs are selected winners under 2+ dishes today. Verified live: the
first Chocolate Cream Pie refresh selected a recipe that REMAINS selected in
Cream Pie's top list — top-20 of the parent AND top-20 of the child, exactly
the intended shape. The alternative (refresh passes by any candidate whose
nearest dish is another dish) was considered and REJECTED: it turns parent
dishes into leftovers buckets, makes membership unstable under catalog growth
(creating a sibling dish would rewrite existing lists' meaning), and couples
paid harvest decisions to fuzzy distances.

Two residuals:
- `_master.dish` (single-valued) = "the last batch that claimed the row",
  used for delete-and-replace cleanup only — display never trusts it. For a
  multi-membership row, `dish_effective` rung 1 therefore reports whichever
  batch stamped last. Fine in practice (the more specific dish usually runs
  later and is also the nearest), but a gear surface that cares can resolve
  the row's ledger memberships and prefer the most specific. Refinement, not
  a bug.
- Cohort GRADING may still want the most-specific cohort (pooling variants
  mis-grades the minority — the Trapanese/Genovese problem). Membership and
  grading-cohort are separate choices.

## Lifecycle / downstream impacts

- **Recipe delete** — `_match` lives inside the row's JSON and dies with it;
  the vec index is cleaned by the AFTER DELETE triggers (established). The
  dish→classes junction is dish-grain, untouched. Nothing to do.
- **Dish delete/rename** — `dish_effective` may briefly point at a dish that
  no longer exists. It is a SOFT pointer by design: consumers resolve it
  against `dishes`/junction at read time and fall back a tier (chapter →
  category) when dangling; the nightly `dish_rematch` sweep re-scores against
  the changed catalog and heals rung 2/3 within a day. Rung 1 (`_master.dish`)
  is membership and already has its own lifecycle (delete-and-replace,
  retire_master_membership). Dish RENAME is already forbidden by design (name
  is the immutable join key; delete+recreate).
- **Class delete/rename** — junction rows for that class are removed with it
  (the junction FKs the registry). `products.product_class` text labels keep
  their value but lose linkage → the products editor flags "class not in
  registry" rather than silently orphaning. Rename = registry UPDATE cascaded
  to junction + products in one migration statement; classes are few and
  curated, so this is an admin action, not a flow.
- **Dish catalog growth** — ~45-60 new dishes/month move the KNN answers;
  the existing write-only-on-change sweep already handles re-scoring, and
  `dish_effective` follows automatically (generated).
- **User recipes** — SAME ladder, same columns, same sweep (curator call,
  built same day): `rematch_unclaimed(table=...)` covers both tables and the
  nightly job runs both; user rows differ only in having no vec-index row to
  keep in step (they are matched AGAINST dishes_vec, never KNN targets).
  At build: 451/452 user rows resolve (217 matched · 234 nearest); the one
  holdout has no embedding.
