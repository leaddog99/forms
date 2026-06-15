# Collections — design note

**Status:** design (one brick shipped). Captures the architecture for generalizing
"dishes" into typed, many-to-many **collections**. Written 2026-06-14 off a long
design thread; see the Session log of the same date in `bcc-state-code.md`.

Related: [docs/dish-variants-membership.md](dish-variants-membership.md) (a *narrower*
problem — see "Relationship to prior notes" below), `memory/project_dish_library.md`,
`memory/project_dish_variants_membership.md`, `memory/project_master_recipes_ui.md`.

---

## 1. The reframe: "Dish" was never the real noun — "Collection" is

Everything we want to curate is the same shape: **a named set of recipes, assembled
by some sourcing method, scored, ranked, and displayed.** A "dish" is just one kind
of collection. The output is *always* a ranked recipe set; only the **sourcing**
differs:

| type | how URLs are gathered | example |
|---|---|---|
| `dish` | SerpAPI query for a dish | "chocolate chip cookies" |
| `chef` | SerpAPI query for a person | "Gordon Ramsay best recipes" |
| `method` | SerpAPI query for a technique | "best braised recipes" |
| `ingredient` | SerpAPI query for an ingredient | "blood orange" |
| `list` | admin pastes URLs, no SERP | a textbox of URLs |

`editors_choice` is **not** a type — it's an **overlay** (curator pins) that rides on
any collection (see §7). And note we already have collections that aren't really
"dishes" — `zucchini`, `blood orange` are ingredient collections living in the
`dishes` table today.

So: a `collections` table is the `dishes` table **plus a `type` column** plus a
per-type sourcing strategy. The literal table/route/field rename (`dishes` →
`collections`, ~800 occurrences) is **pure cosmetics and is NOT required** — `dishes`
becomes a legacy table name the way `master_recipes` is really "the dish library."
Add `type`; branch the sourcing; done. (See §9.)

---

## 2. The core insight: membership is a JUNCTION, and we already built it

`dish_run_data_points` (the scoring ledger) is **already a membership junction**:
`(dish_name × url × rank_score × selected × model_version)`. A recipe that appears in
two collections already has two rows there — one per `dish_name`. The ledger is
many-to-many **today**.

The ONLY thing that makes the relationship look one-to-one is the **denormalized
single `_master.dish` stamp** on the shared `master_recipes` row. So the whole M2M
problem reduces to one move:

> **Membership + per-collection rank/grade lives in the junction (N rows per recipe).
> The recipe row holds only intrinsic content. `_master.dish` drops from "the truth"
> to a vestigial hint (or is retired).**

Rename the junction's key column `dish_name → collection_id`, and a recipe is natively
in as many collections as it has junction rows. **Display already reads the junction**
(see §5). Many-to-many, for free.

---

## 3. Data model (target)

```
collections                      master_recipes                collection_members
-----------                      --------------                (= dish_run_data_points,
id / name (PK, immutable)        recipe_id / url_normalized       generalized)
type  {dish,chef,method,         data (intrinsic content:      ----------------------
       ingredient,list}            recipe, _identity, image,   collection_id  (FK)
queries / urls (per type)         _source, _measurements…)     url_normalized (FK→master)
fit / scoring config            ── NO _master.dish stamp ──    rank_score / ou / power
ttl / refresh metadata          ── NO single grade stamp ──    percentiles
last_run_fit (cohort σ)                                        grade  (PER-COLLECTION)
                                                               selected / pinned
                                                               model_version (the run)
```

Key properties:
- **One master row per URL** (UNIQUE on `url_normalized`) — shared content.
- **Membership + rank + grade are on `collection_members`**, one row per
  `(collection, recipe)`. A recipe in 5 collections = 5 member rows.
- **Grade is cohort-relative, so it belongs on the member row.** A recipe can be A+
  among *chocolate chip cookies* and a B among *all cookies*. The single
  `_master.exceptionalism` stamp is the deeper "latest wins"; it moves here.

---

## 4. The "latest wins" weakness — precisely located (traced 2026-06-14)

Scenario: recipe X is a winner in collection `zucchini`, then collection
`stuffed zucchini` also selects it.

What happens in **today's** code:
1. `stuffed zucchini` refresh first runs `delete_master_rows_for_dish('stuffed
   zucchini','top')` — deletes master rows whose `_master.dish='stuffed zucchini'`.
   X still says `zucchini`, so it survives this step.
2. The save adopts the **shared** master row (UNIQUE url) and **overwrites** `_master`:
   `dish='stuffed zucchini'`, `kind='top'`, `exceptionalism=`(stuffed-zucchini grade).
3. `zucchini`'s ledger row for X is **untouched** (`replace_data_points_for_dish` only
   replaces the running dish's rows).

Result **after today's ledger-derived display fix**:
- **Membership survives.** `zucchini`'s top-10 query joins *its* ledger row
  (`selected=1`) to the shared master content → **X still shows under zucchini.** ✅
- **But two holes remain on the shared row:**
  - **Grade is latest-wins** — under zucchini, X shows the *stuffed-zucchini* grade
    (wrong cohort), because `_master.exceptionalism` is one stamp.
  - **The shared row is deletable out from under zucchini** — the *next* stuffed-
    zucchini refresh runs `delete_master_rows_for_dish('stuffed zucchini','top')`,
    which now matches X (its stamp says `stuffed zucchini`) and **deletes it**. It's
    recreated only if X is still a stuffed-zucchini winner; if X drops out of
    stuffed-zucchini's top-N, **X vanishes from zucchini** until zucchini re-runs.

**Summary: membership is M2M-safe; the shared content row + its grade are still
single-owner ("latest wins").**

### The two fixes that close it
1. **Delete-and-replace operates on the junction, not master rows.** A refresh clears
   *this collection's* member rows (selection), never the shared content. A master row
   is deleted only when **no** collection points at it (a GC pass / refcount), not when
   one collection re-runs.
2. **Grade moves onto the member row** — each collection shows its own cohort grade;
   `_master.exceptionalism` / `_master.dish` are retired (or `_master.dish` becomes a
   convenience "primary collection" hint, never load-bearing).

---

## 5. Display reads the junction (SHIPPED 2026-06-14)

`/dishes/{name}/top-recipes` now derives the top-N from the ledger
(`dish_run_data_points`: `selected=1` at the latest `model_version`, ordered by
`rank_score`) JOINed to master content — **not** the `_master.kind='top'` label.

Why it matters here: this is fix #5-of-the-M2M-story already in place. Display no
longer trusts the single-owner label, so a re-save or a cross-collection adoption
can't demote a winner from the collection it legitimately ranks in. (It's also what
made today's NYT-chocolate-chip bug — a 100/100 winner dropped out of the top-10
because a re-save wiped its label — impossible going forward.)

---

## 6. Scoring model (the cost question — "is a new recipe scored against every
collection?")

**No.** Scoring is **lazy, per-membership, and batch-driven.** A recipe is scored
against a collection **only when the membership is formed** — i.e., that collection's
batch run included it, or an admin pinned it. There is **no creation-time fan-out**.

- **A plain recipe save** gets at most **one** convenience grade — its single best-
  matched dish (`_grade_recipe_on_save`), for the user's own badge. No collection
  scoring.
- **Collection membership + rank/grade** is computed inside *that collection's* run
  (one cheap SQL `UPDATE` over the cohort — `score_data_points_for_dish`).
- Cost is bounded: `(collections it's actually in) × (re-scored when that collection
  refreshes)`. Five memberships = five member rows, spread across five independent
  runs. A save never touches more than one collection.

The only thing that would cause a blow-up is **eagerly** deciding "which collections
does this new recipe belong to, and score all of them" on save. We don't — membership
comes from the curation flows (a collection's SERP run, an admin pin, or an *optional*
async "suggest collections" job), never synchronously on save.

---

## 7. Editor's Choice — the first junction brick (SHIPPED 2026-06-14)

`dish_editors_choice (dish_name, url_normalized, note, …)` — a curator pins a recipe
URL to a collection. The collection's next refresh adds pinned URLs to the candidate
pool (`build_batch(extra_urls=…)`), so they're **scored alongside the SERP results**
and surface in the top-N **if they rank** ("if it scores high enough it appears").

This is deliberately a `(collection, url)` **membership row**, NOT a `_master.kind`
stamp on the recipe — so the same recipe pins to several collections. It's the model
of §2/§3 in miniature, shipped and live.

v2 add-ons (clean, deferred):
- **`pinned` force-show** — display honors a pin even below the rank cut.
- **Score-on-add** — score against the collection's stored fit + insert one member row
  so a pin shows *immediately*, without waiting for a full refresh.

---

## 8. Migration path (incremental, non-breaking)

1. **Display reads the junction.** ✅ Done (`/dishes/{name}/top-recipes`).
2. **Editor's Choice pins as junction membership.** ✅ Done.
3. **Add `type` to `dishes`** (default `'dish'`); branch URL-sourcing per type
   (chef/method/ingredient queries; `list` = stored URL set, no SERP). Browse/search
   by type in the admin nav.
4. **Grade on the member row.** Write `grade`/`exceptionalism` onto
   `dish_run_data_points` at scoring time; display reads it from there. Stop trusting
   `_master.exceptionalism` (keep writing it as a "primary" convenience during
   transition).
5. **Delete-and-replace on the junction.** Refresh clears *this collection's* member
   rows; master rows are GC'd only when no collection references their url
   (refcount/orphan sweep), never by a single-collection delete.
6. **Retire `_master.dish` as load-bearing.** Becomes an optional "primary collection"
   hint or is dropped. The junction is the truth.
7. (Optional, never required) the cosmetic `dishes → collections` rename.

Steps 1–2 are shipped. 3–6 are the collections build proper; each is independently
shippable and backward-compatible (the legacy `kind='top'`/`_master.dish` machinery
keeps working until step 6 removes it).

---

## 9. The rename question

`dishes → collections` is ~800 occurrences (3 tables, the `/dishes` route prefix, the
`_master.dish` JSON key, `dishes.py`, `dishes_v2.html`, hundreds of vars). **It is not
required for any of §8** — typed, browsable collections only need a `type` column on
the existing table. Treat the literal rename as optional cosmetics; the table name
`dishes` is harmless legacy (like `master_recipes`). Recommendation: **don't.**

---

## 10. Open decisions

- **Which grade does the recipe page show?** A recipe in several collections has
  several grades. Options: best-across-collections; the collection you navigated in
  from; or a small "A+ in Chocolate Chip Cookies · B in All Cookies" list.
- **How do non-dish types score?** `chef`/`method` collections have no single-dish OU
  cohort. Candidates: rank by raw authority (power), by each recipe's *own* dish-grade,
  or editorial/SERP order. `rank_score` becomes type-aware.
- **`list` type ingestion** — no SERP; score "as pasted" or by each recipe's intrinsic
  grade; dedup by `url_normalized`.
- **Orphan GC** — when do shared master rows die? A refcount over the junction, swept
  lazily (a recipe in zero collections AND not a personal save → eligible).

---

## 11. Relationship to prior notes

`docs/dish-variants-membership.md` (2026-06-03) walked through and **rejected** an M2M
junction in favor of **one-to-one membership + curator tie-resolution**. That decision
was about a **narrower, orthogonal** problem: *variants of the same dish* — Greek vs US
vs vegan **pastitsio** — where you genuinely want ONE winner per identity and pooling
the cohorts mis-grades the minority cuisine.

**Collections are a different axis.** A recipe is genuinely *Gordon Ramsay* **and**
*pan-fried* **and** *Beef Wellington* at the same time — there is no "tie" to resolve;
all memberships are simultaneously true. So the variants decision does **not** bind
here; collections are the case that actually forces M2M, and this note supersedes the
variants note *on the membership axis* (variants remain a within-collection concern:
how to pick/segment winners inside one collection's cohort).
