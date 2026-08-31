# M2M Migration Map — identity vs membership, everywhere

Status: **map, not built.** Opened 2026-08-30 when the dish↔recipe gap was
discovered; product side added 2026-08-31 after the "Piec Plate" incident.
Design doc precedes any code (curator's call, 08-30 session log).

The one idiom this map drives toward: **an entity owns its identity; its
group memberships are junction rows.** `collection_members` is the proven
implementation — publishers migrated onto it (~22k rows), editors-choice
pins ride it, and `dish_run_data_points` already gives dish top-lists
per-dish membership semantics. The comment in its DDL
(`-- 'publisher' (later 'dish', …)`) was the plan all along.

Related reading: docs/collections.md (the junction), 
docs/dish-variants-membership.md (the 06-03 one-to-one design this
supersedes IN PART — its rejection reasons for auto-routing still stand),
docs/dish-product-matching.md (the commerce chain that consumes classes).

---

## Patient A — dish ↔ recipe (discovered 2026-08-30)

**Today:** a recipe's dish is a single `_master.dish` stamp (last-claiming
batch wins), exposed via the `dish_effective` resolution ladder. The
vegan-AND-Greek case (one recipe in two dish slices) is unrepresentable.
Publishers got migrated onto `collection_members`; dishes never did.

**Target:** dish membership rows on `collection_members`
(`collection_type='dish'`), one per (dish, url) with rank/selected —
`dish_run_data_points` already carries the per-run selection facts to seed
from. **Identity stays `dish_effective`** — the stamp remains the *primary*
dish for grading and cohort work.

**Pollution guard (the load-bearing constraint):** cohort signals, cohort
authority stats, qualifier guards, and the grader read **identity**, not
membership. If they ever read membership, a recipe seated in both Cream Pie
and Chocolate Cream Pie double-counts and the signals blur. Membership
feeds *lists and rendering*; identity feeds *measurement*.

**Delete semantics:** already settled 2026-08-30 — dish delete RELEASES
stamped rows (strip `_master`, keep the row, next rematch re-homes).
Membership rows are junction rows: they just get cleared.

## Patient B — product ↔ class / collection (added 2026-08-31)

**The incident:** curator created a "Piec Plate" (typo) search collection,
medaled + materialized 3 products, deleted the collection, recreated it
spelled right. The products survived (correct — catalog records outlive
selection tools, same release-don't-destroy policy as dishes) but were left
stranded under a ghost "Piec Plate" class header, and their link to any
collection was half-destroyed: `candidates.product_id` died with the
collection; the curation placements survive only inside a JSON blob on the
product. Cleanup requires the Classes editor's rename/merge machinery
because nothing smaller exists.

**Today:**
- `products.product_class` = free-text stamp — the exact analog of
  `_master.dish`. Products join the `product_classes` registry BY NAME
  STRING; rename/merge re-keys strings transactionally (shipped 145eec2)
  because there is no key to hold still.
- product ↔ collection linkage is split across `candidates.product_id`
  (dies with the collection) and `products` curation JSON placements
  (survives, but not SQL-queryable — violates the persist-derived rule).

**Target:**
1. **Class by key, not string:** products reference the class registry by
   a stable key (registry id, or the name once names stop being load-
   bearing). Rename becomes a one-row registry edit; the ghost-header
   failure mode disappears.
2. **Product membership junction:** (source_type, source_key, product_id,
   placement/medal/basis, run_at) — one row per collection→product
   placement, surviving or clearing on collection delete EXPLICITLY, and
   queryable ("which products did this collection stock?", "which sources
   stocked this product?"). The curation JSON placements are the seed data.
3. **Delete preflight parity:** collection delete names its fates the way
   dish delete does ("N materialized products remain in the catalog under
   class X").

## Sequencing

- The two migrations share the idiom but not a deadline. A is bigger
  (grader/signals adjacency) and blocks the vegan-AND-Greek feature; B is
  smaller and mostly removes cleanup friction.
- Both interact with the Postgres migration decision — if the big-bang is
  near, do these ON Postgres, not twice.
- Nothing here changes `dish_effective`, cohort signals, or the resolution
  ladder. Measurement stays on identity.
