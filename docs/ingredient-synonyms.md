# Ingredient synonym dictionary — and the future substitution layer

**Status:** the synonym dictionary is BUILT + live (2026-06-11). The substitution
layer below is FUTURE (design note only). See `input/pipeline/ingredients_lib.py`,
`forms/ingredients.html`, and [[project_named_embeddings]].

## What's built (the synonym dictionary)

A curated, ACD-managed map of variant ingredient phrasings → one **canonical**
term, applied in `compose_identity_text` **before embedding** so same-dish
recipes don't scatter on phrasing (measured: "arborio rice" vs "risotto-style
rice" sat ~0.42 L2 apart; normalizing collapses it). Table `ingredient_synonyms`,
cached `normalize()`, git-tracked seed, a/c/d editor at `/forms/ingredients.html`.

**It normalizes the embedding INPUT only — never the stored recipe.** "1½ cups
arborio rice" stays verbatim for display/fidelity; only the vector sees the
canonical.

### Curation discipline (the synonym / variety / substitution distinction)

Three different relationships; do NOT bucket them together. The `alias_type`
column records which:

- **synonym** — the SAME thing, another name/spelling (aubergine = eggplant,
  garbanzo = chickpea, risotto-style rice = arborio). Always safe to merge.
- **regional** — regional name (rocket = arugula, coriander leaf = cilantro).
- **misspelling** / **brand** — typo'd or brand-name variants.
- **variety** — a DIFFERENT ingredient that merely behaves similarly (arborio vs
  carnaroli vs vialone nano). **Kept as its OWN canonical, NOT merged.** Reasons:
  (1) arborio also appears in rice pudding/arancini — collapsing it to a
  "risotto rice" role would mislabel those dishes; (2) we measured that the
  **dish identity already clusters them** — with the title dropped, "Risotto"
  alone put all 8 recipes at 0.0 distance, so the rice variety adds only a sliver
  swamped by the shared dish/cuisine/technique anchors. So merging varieties is
  both unsafe and unnecessary.

### Seeding

Public ontologies are aimed at the wrong problem (nutrition/labels, not culinary
equivalence) but are useful SEED data for the easy label-synonyms: **Open Food
Facts** and **Wikidata aliases** would bootstrap aubergine/eggplant,
courgette/zucchini, rocket/arugula, garbanzo/chickpea cheaply — then curate.
FoodOn/USDA skip (nutrition-oriented). Culinary equivalence stays our layer.

## Future: the substitution table (NOT built)

A separate, richer relationship — "what can I swap, and is it OK **in this
context**?" — is its own table, justified by a future **recipe-adaptation /
"what can I substitute?"** feature, not retrieval normalization. Sketch:

```
ingredient_substitution(
  canonical_id, substitute_id,
  strength    TEXT,   -- exact | near | acceptable | poor
  context     TEXT,   -- 'risotto' | 'sushi' | 'baking' | 'thickening' | ...
  note
)
```

The key field is **context**: short-grain rice subs in risotto but not sushi /
pudding / paella; vectors flatten that distinction unless explicitly modelled.
This is a knowledge layer like the cook-KB moat — curated, ACD-managed, used by a
feature that reasons about swaps. Don't build until that feature exists; the flat
synonym→canonical map is the right scope for the embedding-normalization job we
have today.
