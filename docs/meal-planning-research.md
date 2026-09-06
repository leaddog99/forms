# Menus, shopping lists, and the dinner-party problem — field research

*2026-09-05. Research only — nothing designed final, nothing built. Curator ask:
flag recipes for tomorrow's dinner party, retrieve them as a group for prep and
cooking, get a market-run shopping list, and start staging Thursday — informed
by what cooking sites and family-display devices do. Memory:
`project_menus_shopping`.*

## The field: four product archetypes

**1. Recipe-manager-first (Paprika 3, Plan to Eat, Samsung Food/ex-Whisk).**
Your recipe collection is the center; a calendar of dated "meals" hangs off it,
and the grocery list is generated from a date range or a set of planned meals.
Paprika is the benchmark for the flag→group→list loop: pick recipes onto a day,
one tap builds the list, **pantry items auto-uncheck** (it knows what you
already have), and ingredients merge. Plan to Eat is the calendar-first
variant (shared family menus, manually maintained pantry); Samsung Food's
"Add Plan to Shopping List" combines ingredients across the plan and dedupes.

**2. List-first (AnyList).** The inverse: a world-class *shared grocery list*
with recipes bolted on. Items auto-categorize by aisle as you type, the
household syncs in real time, voice entry and store-location reminders. The
lesson: the list is a first-class object people add NON-recipe items to (wine,
ice, flowers) — a list generated only from recipes and closed to hand additions
fails the actual market run.

**3. Family displays (Skylight Calendar, Hearth, Cozi).** Wall-mounted shared
screen: color-coded family calendar, chores with star rewards, and a *meal
plan surface* whose real job is visibility — the kids check "what's for
dinner" without asking. Recipes are shallow (manual entry, light import); the
meal plan is a **read-only projection of a plan made elsewhere**. Lesson for
us: the family-planner layer is a rendering surface, not a planning engine —
exactly the one-table→many-projections idiom (cook-KB-as-product). Build the
menu object right and a Skylight-style projection is a later view, not a
rebuild.

**4. Event-execution niche (Time To Plate, Deglaze, multi-dish timers).** The
dinner-party-specific insight the mainstream apps lack: a MULTI-RECIPE PREP
TIMELINE. Time To Plate schedules across "appliance lanes" (your one oven is a
resource; turkey, gratin, and dessert contend for it) with rest times, so
everything lands together; Deglaze merges like ingredients across dishes and
manages several recipes simultaneously; the timer apps compute reverse
schedules from a single serve time. This is the archetype closest to "dinner
party tomorrow" — and the one where OUR cook-view assets (step anchors,
validated per-step times, voice) are an unfair advantage.

## Mechanics worth stealing (and one failure to avoid)

- **The flag is a dated group, not a tag.** Every good implementation makes
  "tomorrow's dinner" an object holding recipes — retrievable as a unit for
  prep, cooking, and listing. Thursday is just a second object.
- **Merge rules are conservative.** Plan to Eat merges only compatible units
  (cups/tbsp/tsp scale together; "pinch" never merges into "teaspoons");
  merging keys on canonical title + unit family + category. Over-merging is
  worse than under-merging — a wrong total poisons trust in the whole list.
- **Aisle grouping is the walk order.** Produce/dairy/pantry sections so the
  shopper never backtracks. (Our `ingredient_synonyms.category` column is
  this, already seeded.)
- **Pantry-awareness is a check-off, not an inventory.** The winning UX is
  Paprika's: the list is complete but staples come pre-checked "have it";
  serious inventory-keeping (Plan to Eat's hand-maintained pantry) is chore-y
  and mostly abandoned. Ship "uncheck what you have," not stock-keeping.
- **Lists accept hand additions** and survive check-off state across the trip.
- **The NYT failure to avoid:** NYT Cooking's grocery list famously does NOT
  combine the same ingredient across recipes — three recipes needing shallots
  = three shallot lines. Users notice immediately. Aggregation is the whole
  point.
- **Scaling at flag time.** Party cooking is 1.5×/2× cooking; the group's list
  must scale per-recipe multipliers before merging.

## Three alternatives for weaving into our system

**Option A — Menus as typed collections (minimal; serviceable this week).**
A `menu` is a typed collection with a date (and later a serve time) — the
existing ledger-junction membership and the shared recipe-table-backed list
component do the flag/retrieve half with almost no new machinery ("Tomorrow's
dinner party" and "Thursday" are two rows). The shopping list is generated
on demand: ONE LLM call (llm.py gateway, journaled) parses the group's raw
ingredient lines into {qty, unit, canonical item, category}, aggregates with
the conservative merge rules, groups by `ingredient_synonyms.category`, and
persists as a checklist (check-off state + hand-added items + "have it"
pre-check). Persist the parsed result per the persist-derived-values rule.
Cost: pennies per menu. Weakness: the parse is per-menu, not per-recipe — the
same recipe re-parses in every menu it joins.

**Option B — Structured ingredients at the source (the durable investment).**
Add the qty/unit/canonical-item parse to the ENRICHMENT pass (the one
JSON-LD-aware call already reads every ingredient line), persist it per
recipe as columns/JSON, and backfill the corpus once. Shopping lists then
become deterministic aggregation — instant, free, and identical on every
regeneration — and the same structure unlocks scaling, pantry pre-check,
per-ingredient nutrition later, and better dish signals. Option A's list
generator collapses into a pure SQL+merge step. This is the
materialize-stored-not-derived path; A without B is a permanent
compute-on-read.

**Option C — The event execution layer (the differentiator; design-only).**
The menu carries a serve time; the system compiles a cross-recipe PREP
TIMELINE: day-before tasks, morning tasks, the last-90-minutes interleave,
with the oven as a contended lane — Time To Plate's idea, but grounded in
step-anchored cook-view data (validated per-step times, sub-steps, voice)
that the niche apps fake with hand-entered durations. This is the
cognitively-grounded-instructions differentiation applied to the multi-recipe
case, and the natural growth of cook-view v2.1. Not now — but Option A's menu
object should carry `serve_at` from day one so C attaches without migration.

**The family-display note.** Don't build the calendar/chores layer. But a menu
with a date is exactly the join point a Skylight-class surface consumes: a
read-only "what's for dinner (and who's cooking)" projection later, the same
way cook-KB projects one table to three surfaces.

## Recommendation

A now, B as the immediate follow-on (the backfill makes A's lists deterministic
and free), C designed when cook-view v2.1 lands. Sequence-wise A is small:
menu type + date on collections, the group view via the existing list
component, one list-builder call, one checklist surface.

## Sources

Paprika/AnyList/Plan to Eat comparisons: foodieprep.ai 2026 side-by-sides,
foodieflow.app, weeklymealsplanner.app · Samsung Food shopping-list docs
(support.samsungfood.com) · NYT Cooking recipe box + grocery-list reviews
(App Store/Play listings; aggregation complaint in user reviews) · Skylight
vs Hearth: thequalityedit.com, tasteofhome.com, myskylight.com · Event
execution: timetoplate.com, deglaze.app, multi-dish timer apps · Merge
mechanics: learn.plantoeat.com (manual merge rules), docs.mealie.io
(shopping-list survey), fond.kitchen glossary.
