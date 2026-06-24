# Dish-alias / canonical-name normalization (SCOPE — #2)

> Status: **SCOPE** (2026-06-24). The real fix for "savory Greek pies show up as pumpkin
> pie." Builds on the glossary (#1, `intake/glossary/el.json`) and the "native title as
> anchor" idea. Related: `project_dish_catalog_table`, `project_dish_library`,
> `project_identity_card`, `project_multilingual_extraction`, `feedback_no_data_in_code`.

## The problem #1 does NOT fix

The corpus scan (2026-06-24) showed the mislabeled rows are **English-sourced**, not
translated — `_source.originalTitle` is empty, and the source author themselves titled it
"Greek Pumpkin Pie … Kolokithopita". So the glossary (Greek→English) never touches them.

```
'Greek Savory Pumpkin Pie with Feta Cheese-Kolokithopita'   originalTitle=''
'Greek Pumpkin and Feta Cheese Pie (Kolokithopita)'          originalTitle=''
'Greek Pumpkin Hand Pies - Kolokithopita (Vegan...)'         originalTitle=''
```

But the **transliterated native name is right there in every title** — `Kolokithopita`. That
is the stable anchor. The English description wobbles (pumpkin / squash / zucchini / hand
pies); `kolokithopita` does not. The fix is to key **dish identity** off that anchor.

## The fix

A **canonical-dish alias map**: recognize a dish's native/transliterated name (and its
spelling variants) and resolve to ONE canonical dish, regardless of how the English
description renders it. So `kolokithopita | kolokythopita | κολοκυθόπιτα | "Greek pumpkin
pie" | "squash pie"` all collapse to the canonical dish **Kolokithopita (savory zucchini/
squash phyllo pie)**.

This is the **dish catalog** work (`project_dish_catalog_table`): a canonical dish carries
- `canonical_name` (English): "Kolokithopita" / "Zucchini Phyllo Pie"
- `native_name(s)`: κολοκυθόπιτα
- `aliases` (transliterations + common mislabels): kolokithopita, kolokythopita, "greek pumpkin pie", "squash pie"
- `note` / classification: savory phyllo pie (chapter = Pies & Pastries, NOT Desserts)

## Where the anchor comes from (resolution order)

1. **`_source.originalTitle`** (native title) — present for *translated* recipes (stable key).
2. **Transliteration in the English title** — `kolokithopita` present even in English sources
   (regex / alias-substring match). This is what catches the reported rows.
3. **Ingredients** — a "pumpkin pie" with feta + phyllo + zucchini is structurally savory;
   a sanity signal (the identity card already has facts).

Match alias → canonical dish → stamp the canonical **dish identity** (and chapter).

## What to normalize (decision)

- **Dish IDENTITY / chapter: YES, normalize.** This is what fixes matching, the dish library,
  search, and the wrong-chapter ("Desserts") cascade. Key the dish off the canonical, not the
  wobbly English name.
- **Display NAME: keep the source title** (provenance) but optionally show the canonical
  alongside — "Greek Pumpkin & Feta Pie *(Kolokithopita — savory zucchini phyllo pie)*".
  Don't silently rewrite the author's title; tag it.
- **Auto vs review:** auto-resolve on a CONFIDENT alias hit (exact transliteration in title);
  flag low-confidence for review. Never guess.

## Build outline

1. **Alias store** — start the **dish catalog** (or a minimal `dish_aliases(alias, canonical,
   lang, note)` it absorbs later; a standalone table risks duplicating the catalog, so prefer
   seeding the catalog). Seed with the Greek phyllo-pie family + transliteration variants
   (kolokithopita / spanakopita / tyropita / hortopita / kreatopita / bougatsa / galaktoboureko)
   and their common English mislabels. **Data in the DB**, seeded from JSON (per
   `feedback_no_data_in_code`); reuse the editor template for curation.
2. **Resolver** — `resolve_canonical_dish(recipe) -> canonical | None`: try originalTitle →
   title transliteration → ingredient sanity. Pure function, unit-tested on the 5 known rows.
3. **Wire at save/extract** — stamp the canonical dish identity + chapter when confident
   (mirror how `_master.dish` / `_identity` are set today; audit all four edges per
   `feedback_db_form_sync`).
4. **Backfill** — re-resolve existing rows; the 5 "pumpkin … kolokithopita" master rows →
   canonical Kolokithopita, chapter Pies/Pastries. Report counts; don't auto-rewrite titles.
5. **Surface** — dish library + recipe form show canonical + native; search indexes the
   transliteration so "kolokithopita" finds them.

## Why this is the durable fix (vs the glossary)

- The glossary improves the **translation derivation** (Greek→English) — necessary but only
  for translated sources.
- The alias map fixes **dish identity across ALL sources** (English included) via the stable
  native/transliterated anchor — and folds into the dish catalog you're building anyway.
- Together: native name = source of truth; English = derived view; canonical dish = the
  identity both resolve to.

## Open questions for the build

- Catalog grain: one row per canonical dish with an `aliases` array, vs a normalized
  `dish_aliases` child table? (Lean: aliases array on the catalog row; query with a
  build-time index of alias→dish.)
- Confidence threshold for auto-stamp; what gets flagged vs auto-applied.
- Cross-cuisine collisions (does any non-Greek dish transliterate to a Greek alias? unlikely
  for these, but the resolver should be language-aware).
