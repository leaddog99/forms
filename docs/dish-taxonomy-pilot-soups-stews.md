# Dish taxonomy pilot: Soups & Stews built by DPI's Taxonomy Factory

*Plan, 2026-11-19, written at the curator's request. Nothing here is built. The DPI side
of this work needs its own plan approval in a DPI session on BAILEY; the BCC side is one
read-only export script. Commands the curator runs are headed with the machine.*

## 0. Decisions this plan rests on (curator, 2026-11-19)

1. **Charter change: DPI becomes a categorization service with substitutable taxonomy
   instances.** What DPI built for products — a tree of consideration-set leaves with
   three texts each, an item reader, a margin-based matcher with a judge, aliases,
   merges, dated verdicts, and the factory that generates and critiques the tree — is
   generic. Products are one instance; dishes (with recipes as the items placed in
   leaves) are the second. RealRank, stores and affiliate services stay on the product
   instance. The DPI charter text ("product intelligence, independent of its clients")
   is to be amended accordingly; the independence principle stays (DPI never writes a
   client's database).
2. **The tree is drawn from culinary knowledge directly**, the way the spice and pantry
   trees were: the generator is asked for the soups-and-stews taxonomy a knowledgeable
   cook would recognise, under the Consideration-Set Rule. The current 50 dishes are
   REFERENCES the tree must account for, not the outline it is built from. The corpus is
   evidence, not the source of truth.
3. **Alias before new leaf.** "If a new dish appears with a new name it needs to be
   determined whether it is an alias before it is declared a new dish." Every door that
   can create a leaf — the generator, a judge "none", the curator's add — checks the
   existing leaves' names and aliases first and offers "alias of X" before "new leaf".
4. **Who decides a leaf?** The generator proposes under the rule and must show its work
   (a distinctiveness rationale per node, a rejected-candidates list with the rule
   applied); the critic checks; the curator approves; the data votes afterwards (near
   ties at the judge, recurring "none" names, search demand and publisher count).

## 0a. The seven calls on the draft tree (2026-11-25)

The draft tree (`docs/research/soups-stews-knowledge-tree-2026-11-20.*`, Part 6) left seven
judgement calls open. The curator delegated them on 2026-11-25 ("you make them... i trust
your judgement"), so these are Claude's calls made on that authority, and the curator can
overturn any of them. The dated draft files are left as they were written; these changes are
to be applied when the tree is next built or handed to DPI. Net leaf count stays 241.

1. **Noodle soups stay in this chapter** as their own group. A cook looking for pho thinks
   "soup"; no separate Noodle Soups tree.
2. **Turkey Chili is its own dish.** This sets the rule for the variant line: a name cooks
   search for in its own right is a dish, even when it is strictly another dish on a
   different protein. The same test applies to Chicken Chili and the like.
3. **Shrimp and Corn Chowder becomes a variant of Corn Chowder** (leaf removed). It fails
   the test Turkey Chili passes: nobody searches for it as a dish of its own.
4. **Shrimp Gumbo and Seafood Gumbo are siblings** (Shrimp Gumbo restored as a leaf, removed
   from Seafood Gumbo's aliases); Chicken and Sausage Gumbo stays as the third. This follows
   the alias check of 11-19, which was made against real recipes. Of the seven, this is the
   one that rests most on the curator's own knowledge of Louisiana cooking.
5. **Pumpkin Soup and Butternut Squash Soup stay two dishes.** Each is searched by name; the
   alias rule handles recipes that could be either.
6. **Thai Red Curry moves to a Curries tree with Massaman**, but only once that tree exists.
   Until then its 19 recipes and 22 weak leaners stay where they are.
7. **Étouffée stays under Gumbo and Louisiana.** It is a stew by method and cooks look for
   it beside gumbo, though it is served over rice.

## 1. What the pilot must answer

The curator's question: how does a generated tree match the current dishes? Concretely:

| Question | Measured by |
|---|---|
| Do the 50 current dishes survive, and as what? | the **mapping table**: each dish → same leaf / renamed / alias of a leaf / merged with another / split into several / dropped, with the generator's reason |
| Where do the chapter's recipes land? | per recipe: clear lead / judge pick / none, and whether that agrees with BCC's current dish |
| Does the tree absorb the holes? | the 279 names recipes call themselves that are not catalog dishes (Zuppa Toscana ×7, Pumpkin Soup ×5, Cream of Mushroom Soup ×4, Miso Soup ×4, White Bean Soup ×4, Tom Kha Gai, Tom Yum, Borscht, Potato Leek Soup, Cioppino, Ribollita…): leaf, alias, or still missing |
| Are pre-generated aliases used? | the 107 vetted aliases for chapter dishes: on a leaf, rejected with reason, or ignored |
| Does the judge beat the bar? | a 100-recipe sample of disagreements graded by the curator |

## 2. The chapter as it stands (BCC, measured 2026-11-19)

50 dishes; 950 recipes curated or confidently matched into the chapter; 330 more lean on
a chapter dish without reaching the bar; 279 distinct hole names. Only 4 dishes carry
aliases (Beef Bourguignon, Lentil Soup, Matzo Ball Soup, Minestrone Soup).

Cases the tree will have to decide, and that the mapping table should show explicitly:

- **Duplicates:** `Shorabet Adas` and `Shorabet Adas (Lentil Soup)`; `Matzo Ball Soup`
  and `Matzoh Ball Soup`. Also `Lentil Soup` (alias Red Lentil Soup) against the Shorabet
  pair: one leaf with a regional alias, or a Lentil Soup group with a Levantine leaf
  (the intersection rule: cuisine × form, where enough recipes exist at the crossing).
- **Siblings the judge already flagged:** Seafood Gumbo ×7 filed under Shrimp Gumbo; Clam
  Chowder ×5 under New England Clam Chowder (Manhattan exists in the world and not in the
  catalog); American Goulash ×3 under Hungarian Goulash — a different dish.
- **The Chili family:** Chili, White Chicken Chili, Turkey Chili, and the hole names
  Vegetarian Chili, Chilli con Carne, Red Lentil Chili, Chile Verde. Which are leaves
  (a cook on white chicken chili does not want chili con carne) and which are attributes
  (turkey for beef in the same red chili)? This is the rule applied visibly, and the
  rejected-candidates list is where the answer must appear.
- **Ingredient names standing where dishes should be:** Vegetable Soup (32 weak leaners),
  Potato Soup, Tomato Soup, Cauliflower Soup. The judge called "Roasted Tomato Soup" and
  "Baked Potato Soup" attributes; "Creamy Chicken Noodle Soup" a variant. Are these
  leaves or groups with leaves under them?
- **Chapter boundary:** Thai Red Curry (22 weak leaners), Ramen (20), Tonkotsu Ramen, Niu
  Rou Mian, Pho, Hot and Sour Soup — curries and noodle soups sit in this chapter today.
  The generator's excludes decide whether they belong here or in a neighbouring tree, and
  where recipes that lean on them go meanwhile.
- **Name hygiene:** `Étouffée Recipes`, `Soupe au Pistou (French Minestrone)`, `Greek
  Chickpea Soup (Ρεβιθόσουπα)` — catalog names that are not what a cook calls the dish.
  DPI separates the stable key from the name; BCC uses the name as the join key. The
  mapping table records the new name beside the old key.

## 3. Inputs: what BCC exports (read-only)

One script, `scripts/export_dish_chapter.py --chapter "Soups & Stews"`, writes a JSON
file the DPI session reads. Nothing in BCC changes.

| Section | Content | Use in DPI |
|---|---|---|
| `dishes` | the 50: name, display_name, aliases, description, identity_card, chapter; counts curated / confident / weak-leaning | the references the tree must account for (§4 step 3), and the mapping table's left column |
| `recipes` | ~1,280 chapter recipes (curated, confident, and weak-leaning): id, title, identity card (likelyDish, cuisine, ethnicity, primaries, technique), current dish and method, nearest distance | the items to classify; the comparison baseline |
| `holes` | 279 names with counts and the dish they lean on | evidence for the generator's brief ("recipes in the corpus call themselves…"); the absorption check |
| `aliases_vetted` | 107 rows for chapter dishes (tier A 11, B 30, C 66) with evidence counts | brief lines ("also called…"); the alias-usage check |
| `aliases_rejected` | 37 names the alias judge said are NOT the same dish (sibling/child/attribute/component) with reasons | candidate leaves and attributes for the generator to rule on |
| `demand` | search demand per dish name where BCC has it (keyword pills, competitiveness) | the rule's "a name people search for" test |

## 4. Method, in DPI on BAILEY, against `dpi_test`

**Step 0 — the instance plumbing (the charter change made concrete).** The smallest
change that lets the factory and matcher run on dishes: a per-domain `instance_kind`
(`product` | `dish`) with prompt partials selected by kind. The words that are
product-specific in the prompts become partials: who the consideration set is for
("a shopper looking at this product" → "a cook looking at this recipe"), what an item
is, how an item is read (the describe prompt: a recipe's identity card is already the
two texts DPI wants — what the dish IS: form, base, key ingredients, technique; and
its role: course, occasion, what it stands in for), and the examples in the
distinctiveness rule (capability → form: soup / stew / chowder / bisque / broth;
mechanism → technique: braised / simmered / blended / raw like gazpacho; segment → a
dish cooks name and search for; situation → dietary or occasion the recipe itself
states, vegetarian chili against beef chili; intersection → cuisine × form, Portuguese
Kale Soup, Avgolemono). The schema (`NodeSpec`: definition, includes/excludes,
identity_text, capability_text, aliases, rationale, examples; `RuleSpec`: fallback and
step-up rules) needs no change. The product instance must behave exactly as before:
run the existing regression (548 of 554) after the change.

**Step 1 — the alias gate (new, small).** In `match.py`'s "none" path and in the
curator's add-leaf door: before a name becomes a leaf proposal, the judge is asked
whether it is an alias of an existing leaf (shown the nearest leaves' names and
aliases), and the answer is recorded with the verdict. This is the curator's rule
from §0.3, and it is not in DPI today: the generator emits `aliases` per node from its
own knowledge, but nothing checks a new name against the tree first, and nothing
pre-generates aliases from evidence. The BCC-side vetting (`alias_vetted.json`) is the
evidence-based half; the gate is the runtime half.

**Step 2 — explode the domain from knowledge.** Domain `soups_stews`, "Soups & Stews".
Scope: soups, stews, chowders, bisques, broths and braised one-pot dishes eaten as a
course. Includes: the 50 current dish names and the hole names with ≥2 recipes, as
"clearly in scope" lines. Excludes: to be stated by the generator and reviewed (curries
and noodle soups are the open question). The generator runs on its own culinary
knowledge under the rule; the brief lines tell it what exists in our corpus, not what
to draw. It returns nodes with definitions and aliases, and the rejected-candidates
list. Then the existing critique → revise loop, then the curator's read on the tree
page. Cost: the pantry trees ran about a dollar a tree; this chapter is perhaps 60–120
leaves, a few dollars with critique.

**Step 3 — the mapping table (new script, DPI side).** For each of the 50 current
dishes: match its name, display name, aliases and identity card against the generated
leaves (name/alias equality first, then the matcher's judge with the dish's card as the
item). Outcome per dish: `same` (a leaf with this name), `renamed` (same leaf, better
name), `alias` (the dish is an alias of a leaf), `merged` (two dishes → one leaf),
`split` (one dish → several leaves, e.g. Clam Chowder), `dropped` (no leaf; reason).
The curator reads this table before anything else; it is the pilot's main deliverable.

**Step 4 — embed and classify the recipes.** Embed the leaves; classify the ~1,280
exported recipes through the matcher with the identity card as the item's words (title
read: the card text as it is; described read only when the card is thin). Record per
recipe: leaf or none, score, margin, how decided (clear lead / judge / none / named),
and the comparison with BCC's current dish through the mapping table (agree / moved /
newly placed / none). Report the distribution and the full disagreement list.

**Step 5 — the alias check.** Which of the 107 vetted aliases landed on a leaf, which
the generator or gate rejected and why, which were ignored. Which "none" names the gate
resolved as aliases, and which stand as proposals for new leaves.

**Step 6 — the curator's grade.** 100 recipes sampled from the disagreements, graded by
the curator: DPI right / BCC right / both wrong / both acceptable. This is the only
measurement that settles whether the judge beats the bar.

**Step 7 — write-up and decision.** In `dpi-state.md` and this file: the mapping
table, the distributions, the grades, cost and time, and the go/no-go for the other
chapters. If go: the BCC migration design (a leaf id as the dish key, the membership
junction already decided for dish variants, the rematch and gap jobs replaced by DPI's
matcher and "none" list) is a separate plan.

## 5. Success criteria

- Every one of the 50 dishes has a mapping outcome with a reason; no more than 5 are
  `dropped`, and each drop is one the curator agrees with on reading.
- The duplicates and flagged siblings in §2 are resolved explicitly in the tree or the
  rejected list, not silently.
- Of the recipes, at least 85% land by clear lead or judge; "none" is under 10% and
  the "none" names are genuine holes or chapter-boundary cases, not misses.
- On the 100-recipe graded sample, DPI is right or acceptable at least 90% of the time,
  and beats BCC's current dish on the disagreements.
- At least two thirds of the vetted aliases are on leaves or rejected with a reason the
  curator accepts; the top hole names (Zuppa Toscana, Pumpkin Soup, Cream of Mushroom
  Soup, Miso Soup, White Bean Soup, Borscht, Potato Leek Soup) are leaves or aliases.
- The product instance's regression is unchanged after step 0.

## 6. Risks and how the plan handles them

- **The tree is better than the catalog and the catalog is the join key.** The pilot is
  read-only on BCC; collections, stories, SERP queries and cohort signals keep their
  dish names. Migration is its own plan, through the junction idiom, with the mapping
  table as the crosswalk.
- **Chapter scope.** Curries and noodle soups may leave the chapter. Recipes that lean
  on them are reported as boundary cases, not counted as misses.
- **Thin identity cards.** A recipe whose card lacks primaries or technique gets the
  described read, at a model call each; count them.
- **The generator over-splits** (every protein a leaf) or **under-splits** (Chili as one
  leaf). The rejected-candidates list and the critic exist for this; the curator's read
  of the Chili and Chowder families is the test.
- **Judge drift.** The alias judge was over-strict today (Bolognese Sauce as a
  component of Bolognese) and arguable elsewhere (Belgian Waffles for Waffles). The
  graded sample measures it; the prompt partials for the dish instance are where it is
  tuned.

## 7. Cost and time

Factory run with critique: a few dollars. Classification: embeddings for ~1,300 cards
(cents) plus judge calls on perhaps a third of them (under a dollar). Model work: the
instance plumbing and regression is the bulk, about a day in a DPI session; the export
script an hour in BCC; the mapping and comparison scripts half a day; the curator's
reads and the graded sample an evening.

## 8. Order of work and who does it

| # | Where | What | Needs |
|---|---|---|---|
| 1 | BCC (this repo) | `scripts/export_dish_chapter.py`, read-only, writes the JSON of §3 | nothing; can be done now |
| 2 | DPI session on BAILEY | plan approval for steps 0–1 (charter text, instance partials, alias gate), then build against `dpi_test` with the product regression green | the curator's go in that session |
| 3 | DPI session | steps 2–5 on the exported JSON; the mapping table first | the export file on BAILEY (the nightly mirror carries this repo; or copy the JSON) |
| 4 | Curator | read the tree and the mapping table; grade the 100-recipe sample | an evening |
| 5 | DPI session + this repo | step 7 write-up; go/no-go; if go, the BCC migration plan | — |
