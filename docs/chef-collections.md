# Chef collections — "Marcella Hazan's best recipes" as an extract type

*Design note, 2026-10-10. Status: DESIGN, not built. Companion to docs/collections.md
(which named the `chef` type in its §2 table and left its scoring open in §10).*

## 1. The ask

The curator wants a ranked set of one author's recipes. Marcella Hazan's recipes are
not on one site: they live on NYT Cooking, Food52, Smitten Kitchen, Serious Eats,
Epicurious and a long tail of blogs, each a copy or an adaptation. The question was
whether a well-written Google query on a **domain** row could gather them the way a
domain extract gathers a publisher's recipes, and if not, whether this needs a new
extract type.

It needs a new type. Both existing rows were checked against the code:

* **Domain row with a verbatim query.** `harvest_publisher_top` runs the query as typed,
  then keeps only results whose `root_domain` equals the domain row's host
  (collections_lib.py, the `elif query:` branch). A cross-web query returns a hundred
  links and keeps none. That filter is the meaning of a domain row and should stay.
* **Dish row.** The dish path has no host filter, so a dish named "Marcella Hazan" with
  the line `"Marcella Hazan" recipe` runs end to end. Two things go wrong. Every winner
  is stamped `_master.dish = "Marcella Hazan"`, which poisons dish membership and the
  matcher exactly as "Pizza Sauce from Italy" did (docs/dish-variants-membership.md).
  And a dish cohort is one recipe by definition, so the pipeline has no same-recipe
  collapse: the top ten would be six copies of the tomato sauce with onion and butter
  and three Bolognese.

So: a collection whose key is a person, sourced by SERP query rows, that reuses the dish
pipeline through scoring and adds exactly what a person needs that a dish does not.

## 2. Shape

```
chefs (NEW master table, parallel to domains)      collection_members (EXISTS)
  name            TEXT PK COLLATE NOCASE             collection_type = 'chef'
  display_name    TEXT                               collection_key  = chefs.name
  aliases         TEXT  JSON ["Hazan", ...]          url_normalized, rank, selected,
  queries         TEXT  JSON rows {q,n,gl,hl,keep,q_src}   rank_score, note (= dish group)
  top_n_serpapi, top_n_final, refresh_ttl_days
  last_refreshed, last_run_status, last_run_count,
  last_run_log_filename, notes, created_at, updated_at
  bio, bio_provenance   (PLANNED — project_chef_bio; columns reserved, not filled in v1)

master_recipes / recipes                           run_candidates (EXISTS)
  _master = {kind:'top', chef:<name>, rank, ...}    collection_type='chef' — the ledger
  NO _master.dish — the matcher stamps the real      already takes any type
  dish through the normal save path
```

**Why a separate `chefs` table rather than `dishes.type`.** docs/collections.md §8 step 3
proposed a `type` column on `dishes`. Two things have changed since. The `chefs` master
is already planned for the bio and for the off-web authority axis
(docs/recipe-scoring-design.md §11c), so a chef is a Person entity with its own fields,
not a dish with a flag. And half the `dishes` columns are dish-only (`identity_card`,
`last_ou_fit`, `last_run_bottom_ou`, chapter fit, aliases-as-dish-names); a chef row
carrying them blank would be the metadata-driven drift the editor rule forbids
([[feedback_editor_template_not_runtime]]). The **junction** is shared: a chef's members
are `collection_members` rows, same as a publisher's, so one display component reads
both. Query rows are the **same JSON shape** as `dishes.queries`, so `normalize_query_rows`,
`line_key`, `keep`, the 🌐 translation and the per-line locale relax all apply unchanged.

## 3. Pipeline

Stages 1–6 of `build_batch` reuse as they are: SERP union over the query rows (each line
keyed text + locale), URL dedup, blocklist and aggregator flag, is-recipe, Moz, the
min-OU floor with per-line relax. Two stages are new and one is type-aware.

### 3a. Attribution gate (NEW) — "does this page credit the chef?"

Google returns pages that merely name-drop ("a sauce as simple as Marcella Hazan's"). The
gate runs **after is-recipe and before Moz**, because the fetched page is already in hand
and Moz bills per target URL ([[project_moz_scoring_cost]]): drop the uncredited before
paying to score them.

Deterministic first, LLM only for the ambiguous middle:

| verdict | evidence | action |
|---|---|---|
| `by` | chef name or alias (folded) matches JSON-LD `author`, `article:author`, or the recipe's `author` field | keep |
| `adapted` | name appears in title, headline, description, or an "adapted from / recipe by / via" line within the recipe header | keep, flagged `adapted` |
| `mention` | name appears only in body prose or comments | drop, `_dropped_reason="not-credited"` |
| none | name absent from the page | drop, `_dropped_reason="not-credited"` |

The ambiguous case (name in the first paragraph but in none of the credit positions) goes
to one LLM call through `llm.create(operation="chef_attribution")` with the header text
and the question "is this recipe presented as the chef's, an adaptation of the chef's, or
a recipe that merely mentions them". Expected volume: a handful per run. The verdict and
the matched alias are stamped on the entry (`_attribution = {verdict, matched, method}`)
and reach the ledger as `reason`, classified `is_recipe` stage, **overturnable** (it is an
inference). Candidate-ledger `_REASON_MAP` gains `("not-credited", "is_recipe", True)`.

Aliases matter more than for dishes: "Hazan", "Marcella", and the Italian sites' "la
Hazan" all credit her. The chef row carries them; the gate folds and matches any.

### 3b. One-per-dish collapse (NEW) — "her Bolognese once, not six times"

A chef set wants breadth across their dishes, with the **best copy** of each. The
collapse needs each survivor's dish, which needs the recipe vector, which needs the
extract. Today the dish refresh extracts only in the save loop, after ranking. The chef
path moves extraction **before** final ranking for the scored survivors:

1. Take the scored survivors in blend order, up to `3 × top_n_final` (the same 3× the
   reserved-seat code uses for a line's carry-through).
2. Extract each (cached by `llm_extract_cache`; the save loop would have paid for the
   winners anyway, so the marginal cost is the rank-cut band, roughly two thirds of the
   extracts).
3. Match each through `dish_match.build_match` with its identity card. Group key =
   `_match.dish` when confident, else the folded recipe title.
4. Within a group keep the member with the highest `rank_score`; the rest become
   `rank_cut` with `reason="duplicate-of:<url>"` (overturnable, so the curator or editor
   can prefer a different copy). The kept member's `note` records the group.

Groups with no confident dish are legitimate (a chef's lesser-known recipe may not be a
catalog dish yet); they key on title and are reported in the run log so the curator can
seed the dish.

### 3c. Ranking (type-aware)

Across groups, order the kept copies by the existing OU/power blend. OU is Moz's
page-versus-domain measure and is not dish-cohort-specific, so it carries. Reserved seats
(`keep` per line) apply as they do for dishes, after the collapse, so an Italian line's
five seats go to five **different** dishes credited to her on Italian sites.

Known limitation, recorded rather than solved: a cookbook author's own companion site or
a small publisher's faithful copy will often score below a content farm's SEO-tuned
adaptation ([[project_recipe_scoring_design]] §11c, off-web authority has no instrument).
v1 accepts that and gives the curator pins. The `chefs` table is the future home of the
author-authority axis; building the type first is what gives that axis somewhere to live.

## 4. Persistence and display

* `collection_members` (`'chef'`, name): kept members `selected=1` with rank; duplicates
  and rank-cut `selected=0`; `note` = dish group. Refresh is delete-and-replace on this
  collection's rows only, as for publishers.
* `master_recipes`: `_master.kind='top'`, `_master.chef=<name>`, `_master.rank`,
  `_master.attribution` (`by` | `adapted`), **no `_master.dish`**. The normal save path
  runs the matcher and stamps the recipe's real dish membership; the chef recipe then
  shows up in Bolognese **and** in Marcella Hazan, which is the point of the junction.
* `run_candidates`: every considered URL with the new reasons, `collection_type='chef'`.
* Display: `/chefs/{name}/top-recipes` reads the junction through the shared
  recipe-table-backed list component ([[project_recipe_table_backed_lists]]). Each row
  shows its dish group and the attribution badge (by / adapted).

## 5. Editor and job

* **Editor**: clone `dishes_v2.html` → `chefs.html`, specialised, not metadata-driven.
  Fields: name, display_name, aliases (chip list), query rows (shared
  `forms/query-rows.js` with `translateUrl`), top_n, ttl, notes, run log link, the
  candidates ledger panel. Bio fields present but read-only until the bio job exists.
  Admin nav gets Chefs beside Dishes and Domains.
* **Job**: `chef-refresh` handler cloned from `_handle_dish_refresh_job` with 3a and 3b
  inserted and the dish-only guards (qualifier drop, off-dish drop, label-holder) removed.
  Runs out of process like every job (`python -m jobs run chef-refresh --name "Marcella
  Hazan"`, name not query, per [[feedback_cli_args_identity_not_query]]).
* **Endpoints**: `/chefs` CRUD, `/chefs/{name}/refresh`, `/chefs/{name}/top-recipes`,
  `/chefs/{name}/candidates` (ledger).

## 6. Not in v1

Bio generation and the cookbook/author-authority axis (the table reserves the columns).
`method` and `ingredient` types, although the chef path is the template: the only
chef-specific pieces are the attribution gate and the collapse, and both are optional
stages a `method` type would skip. The `corpus` partition key discussed for the Postgres
migration is orthogonal; a chef collection lives in whichever corpus its recipes do.

## 7. Open questions

1. **Does `adapted` count as hers?** Recommendation yes, badged, because the best-known
   copies (Smitten Kitchen's tomato sauce, Food52 Genius) are all adaptations. Curator can
   flip a system setting to `by` only.
2. **A credited copy on a blocklisted or aggregator host.** Stays blocked. The blocklist
   is curator fact and outranks attribution.
3. **Two chefs on one page** (a Hazan recipe retold by another named cook). The gate
   credits the chef asked about; the `chefs` table does not yet model co-attribution.
4. **Non-English lines.** "Marcella Hazan ragù" on google.it finds Italian sites that
   credit her. The 🌐 translation handles the query; the gate's alias matching is
   script-agnostic because it folds, but an alias in another script (if ever needed) is
   just another alias row.
5. **Group key when the matcher is unconfident.** Folded title is weak (two titles for
   one recipe). Accept for v1, measure duplicates in the first runs, and if it bites, use
   embedding distance between survivors as the fallback key.

## 8. Build order

1. `chefs` table + `/chefs` CRUD + editor clone with query rows. Checkpoint: a chef row
   with lines saved and translated, no refresh yet.
2. `chef-refresh` job = dish pipeline minus dish guards, plus the attribution gate.
   Checkpoint: a Hazan run whose ledger shows `not-credited` drops and whose kept set is
   all credited pages, duplicates included.
3. The collapse (extract-before-rank, match, group, best copy). Checkpoint: one entry per
   dish, duplicates in `rank_cut` with `duplicate-of`.
4. Display route + attribution badge + state log and memory update.

Rough size: two to three working days. Step 2 is the part that can surprise, since
"credited" is a judgement; its deterministic table above is the thing to review before
code.
