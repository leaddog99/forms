# Recipe-table-backed lists (dishes & domains) — phased plan

**Status:** design (2026-07-12). The near-term trigger was a small bug — the **dishes
grid cards show no image** — but the right fix is directional, not a denormalized
`dishes.image` column. This note captures the target: make the **dishes** list and the
**domains** list *first-class lists of recipes* rendered from the **recipe table**,
with a **batch stamp** so the delete-and-replace refresh only removes its *own* prior
output and leaves differently-sourced members (Editor's Choice, manual, promoted
personal recipes) untouched.

Parent design: [docs/collections.md](collections.md) (the junction/membership reframe).
Related: [docs/dish-variants-membership.md](dish-variants-membership.md),
`memory/project_collections.md`, `memory/project_dish_library.md`,
`memory/feedback_reuse_layout_components.md`, `memory/project_domain_master.md`.

---

## 1. The target (curator's words)

> "I want those lists in dishes and domains to be eventually first-class lists of
> recipes… we should be going after the **recipe table**, not individual tables of
> selected items."

So the dish/domain page's list = **query the recipe table for the recipes that belong
to this dish/domain**, ranked, rendered by **one shared recipe-list component**. The
side "tables of selected items" (`dish_run_data_points`, `dish_editors_choice`, …)
stop being the *source of truth for what's in the list*. They may remain as scoring
provenance/history, but display no longer reads them.

Corollary for the image bug: the card image is **derived from the list's recipes**
(the top recipe's hero, straight from the recipe table). No `dishes.image` data column
is required. A nullable column may exist **only** as an optional curator "pin this
image" override — it is not the default source.

---

## 2. The problem the batch stamp solves

If the list is "all recipes belonging to dish X," then a **refresh** (which today is a
*delete-and-replace* of that dish's batch winners) must not blow away members that got
there another way:

- **Editor's Choice** pins (curator overlay)
- **manual** adds
- a **promoted personal** recipe
- (later) members shared with the dish from another collection

A blanket "delete all recipes for dish X, re-insert the new batch" would destroy those.
So the refresh delete must be **scoped to the batch's own output**.

---

## 2b. What ALREADY exists — verified 2026-07-12 (domains do this safely today)

The domain (publisher) refresh **already** does a source-scoped, non-destructive
delete-and-replace. It does **not** need the same fix the dishes grid does. Confirmed in
`save_recipe_api._handle_publisher_refresh_job` and `dishes.retire_master_membership`:

**The mechanism = a typed-block "inline reference count" on `_master`.** A master recipe
row carries up to two membership blocks:
- **dish block** — `_master.dish` (+ `kind`, `rank`)
- **domain block** — `_master.publisher` (+ `refreshed_at`)

`retire_master_membership(marker, value, other_marker, remove_fields, also_match?)`
(`input/pipeline/dishes.py:918`) retires ONE owner's claim: for every row where
`_master.<marker> == value`, it removes that owner's fields; **if `_master.<other_marker>`
is still set it KEEPS the row (now owned by the other type), else it DELETES it.** The vec
AFTER-DELETE trigger cleans the vector. One copy of this logic, called by BOTH refreshes.

- **Domain refresh** (`save_recipe_api.py:4059`): before extracting new winners, calls
  `retire_master_membership(marker="publisher", value=host, other_marker="dish",
  remove_fields=["publisher","refreshed_at"])` → clears the publisher block; a row **also
  claimed by a dish is kept**; publisher-only rows are dropped. Then the new winners are
  re-extracted and stamped `_master={kind:'top', publisher:host, …}` (`:4249`). So a
  manually-added or dish-extracted recipe is **never** deleted by a domain refresh — it
  has no publisher block, or it has a dish block that spares it. **Your hope is confirmed.**
- **Dish refresh** uses the mirror call (`delete_master_rows_for_dish` → `marker="dish",
  other_marker="publisher", also_match=('kind','top')`) — spares publisher-owned rows AND
  spares `kind != 'top'` (Editor's Choice / legacy).
- **Full domain DELETE** (`:4748`) adds a host-sweep for legacy rows with no publisher
  stamp, still sparing any row with a dish block.

There is ALSO a real junction — **`collection_members`** (`collection_type` ∈
{publisher, dish}, `collection_key`, `url_normalized`, `selected`) — the selection ledger,
re-flagged to match the actually-saved winners (`:4094`). So membership/selection is
already junction-tracked; the `_master` block is the *content-ownership* stamp on top of it.

### So how this changes the plan
The source-scoping is **already built** — the "source" is encoded as *which block*
(`dish` vs `publisher`), and `kind` spares pinned/legacy. Two gaps vs the target:
1. It's a **whole-block reinit** (no `batch_id`): each refresh clears the *entire*
   publisher/dish block and rebuilds. Fine for domains (they fully reinitialize each run);
   `batch_id` only matters if we ever want *incremental* replace instead of whole-block.
2. The ownership stamp lives in **`_master` JSON** (the denormalized single-stamp
   `collections.md` §2 flags), not the `collection_members` junction. The generalization
   is to make `collection_members` carry `source` (+ optional `batch_id`) and become the
   display truth, letting `_master.dish`/`publisher` demote to vestigial (Phase 4).

Net: **domains need no delete/refresh change** — reuse the existing typed-block retire.
The dishes work is the image derivation (Phase 0) + eventually reading the junction for
display (Phase 3). The `source`/`batch_id` design below is the *generalization* of what
`retire_master_membership` already does, not a new safety mechanism to bolt on.

## 3. The stamp — TWO fields, not one (generalizing what §2b already does)

Stamp each **membership** (not the intrinsic recipe content) with:

| field | meaning | when set |
|---|---|---|
| `source` | *why* it's a member: `batch` \| `editors_choice` \| `manual` \| `harvest` \| … | always |
| `batch_id` | *which run* added it | **only when `source = batch`** (else null) |

`batch_id` answers "which run put this here"; `source` answers "is this even eligible
for a batch refresh to delete." **Both are needed** — Editor's Choice is protected by
`source`, not by `batch_id`.

Dish/domain record gains **`last_batch_id`** — the pointer to the current (latest
successful) batch.

### Origin of `batch_id`
It already exists in spirit: `dish_run_data_points.model_version` is the per-run stamp,
and `selected=1 + _master.kind='top'` is effectively `source='batch'`. This plan
**moves that stamp onto the membership** so the recipe table (via the junction) becomes
the truth.

---

## 4. The surgical swap (idempotent, non-destructive)

On refresh, land the new set **first**, then delete only prior batches:

```sql
-- 1. new run produces winners, inserted as memberships with source='batch',
--    batch_id = :current_batch_id
-- 2. THEN remove the previous batch's winners:
DELETE FROM <membership>
 WHERE collection = :dish
   AND source = 'batch'
   AND batch_id <> :current_batch_id;
-- 3. dishes.last_batch_id = :current_batch_id
```

Why this shape:
- **Editor's Choice / manual** rows have `source <> 'batch'` → never touched.
- A recipe that is **both** a batch winner **and** an Editor's Choice pin survives (its
  Editor's Choice membership has `source='editors_choice'`).
- An **interrupted or re-run** batch can't nuke the good set — the delete runs only
  after the new set is in place and only against *older* `batch_id`s.
- Fully **idempotent** — re-running the same `batch_id` is a no-op delete.

---

## 5. Where the membership physically lives — decision needed

Two options; **junction table is recommended** (it's the collections model already in
motion):

**(A) Junction table** `collection_members(collection, recipe_id, source, batch_id,
rank_score, added_at)` — a recipe belongs to many dishes with different sources; the
stamp is per-membership. Clean, M2M-correct, display queries the recipe table JOINed to
this. *Recommended.*

**(B) Stamp `_master` on the recipe JSON** (`_master.source`, `_master.batch_id`) —
lighter to add, but re-collapses M2M to one-membership-per-recipe and re-creates the
"single `_master.dish` stamp" problem `collections.md` §2 warns about. Only viable as a
throwaway stepping stone.

Note: `dish_editors_choice` already **is** a junction with `source` semantics baked in;
(A) can generalize it rather than add a parallel table.

---

## 6. Phases

**Phase 0 — image now, no new data (unblocks the visible bug).**
Dish/domain card image derives from the list's top recipe hero, read from the recipe
table. Reuse the existing top-recipe preview logic (`_master_result_row` /
`list_dish_top_recipes` already compute `preview_image` = `previewImage || image[0]`).
No `dishes.image` column. Ships the fix while the rest is designed.

**Phase 1 — introduce the stamp.**
Add `source` + `batch_id` to the membership (junction per §5A) and `last_batch_id` to
`dishes`/`domains`. Backfill: existing `selected=1` ledger rows → `source='batch'`,
`batch_id = model_version`; existing Editor's Choice → `source='editors_choice'`.

**Phase 2 — refresh writes the stamp + surgical swap (§4).**
The dish/domain refresh job inserts new members with the new `batch_id`, then runs the
scoped delete, then sets `last_batch_id`. Replaces today's `kind='top'` delete-and-
replace. Editor's Choice/manual now provably survive.

**Phase 3 — display reads the recipe table.**
The dish page and domain page list = recipe table JOIN membership, ranked. One shared
recipe-list component renders both (satisfies the "match dishes top-recipes list" note
for domains). The side ledger is no longer the display source of truth.

**Phase 4 — retire / demote the denormalized stamps.**
`_master.dish`/`kind` drop from "the truth" to vestigial hints per `collections.md`.

---

## 7. Open questions

- **Junction vs `_master` stamp** (§5) — recommend the junction; confirm before Phase 1.
- **Ranking on read:** rank lives on the membership (`rank_score`) or recomputed? Batch
  winners have a ledger `rank_score`; Editor's Choice/manual need a defined slot (pin to
  top? separate section? interleave by score?).
- **Domains parity:** domain harvest members get `source='harvest'`; confirm domains
  wants the same Editor's-Choice-style overlay.
- **Does the scoring ledger stay** as provenance/history after display stops reading it?
  (Likely yes — it holds the rejected cohort + fit data the dish form shows.)
