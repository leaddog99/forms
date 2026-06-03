# Reviews & Product Commerce — Architecture

*Last updated 2026-06-02.*

How Best Cooks Club turns trusted equipment/ingredient **reviews** into a
ranked **product catalog** that earns affiliate revenue — without hallucinated
recommendations.

---

## 1. Goal

A curated, ranked catalog of kitchen equipment and gourmet goods, recommended
**in context** ("this banana-bread recipe needs a loaf pan → here are the best
ones, with where to buy"). Two non-negotiables:

- **Trust.** Every recommendation traces to a *real review we actually
  extracted* — never the LLM's say-so. (See §4.)
- **We keep the user.** We show *who* rated a product and *what they said*, but
  we **do not link out to the review site** — only **buy** links leave.

---

## 2. The core insight: products mirror recipes

The product side reuses the recipe machinery at three levels:

| Recipe side | Product side | Table |
|---|---|---|
| **Chapter** (Breads) | **Category** (Bakeware) | `product_categories` |
| **Dish** (Banana Bread) — curated, ranked, embedded, refreshable | **Product class** (Loaf Pans) | `product_classes` |
| **master_recipe** (a saved recipe) | **Product** (USA Pan 1 lb) | `products` |

**The review site is *provenance*, not a tier.** ATK / Serious Eats /
Wirecutter map to what a recipe's *source domain* is — not a level above
category. The one real difference: a recipe has **one** source, but a product
**aggregates many** sources → that's the *homogenization* step (§5). The
**ingestion unit** is "one bookmarkleted review" = one source's take on one
class → many products (analogous to "one dish batch run → many recipes").

---

## 3. Ingestion: from the site to the catalog

Curator-driven, reusing the recipe bookmarklet rails:

1. **Curator visits a trusted review** (ATK, Serious Eats, Wirecutter…). A
   human picks authoritative sources.
2. **Bookmarklet extracts the page** — markdown **+** screenshot (same capture
   the recipe flow uses; the screenshot is also visual proof / provenance).
3. **`detect_source(url)` → a per-source decoder** in
   `intake/products/review_parsers.py`. Review sites are structured wildly
   differently, so each gets **custom code**:
   - **ATK is highly structured** (labeled `Model Number:` / `Capacity:` /
     tier headers) → `parse_atk()` is **deterministic, no LLM, free**.
   - Less-structured sources fall back to an **LLM extract** with a
     source-aware prompt.
4. Output = one **`ProductReviewExtraction`** `{product_class, review_source,
   products[]}`.
5. **`persist_extraction()`** (`intake/products/catalog_store.py`) writes the
   category + class + products. Products upsert by `(product_class, name)` so
   re-ingesting a review updates rather than duplicates.

**Why curator-driven extraction solves verification:** the review is real *by
construction* — we extracted the actual page the curator chose. There's no
"did ATK really say that?" problem, because we have ATK's page.

---

## 4. Trust / brand-safety model

Two clearly-separated voices, both safe:

- **Verified third-party** — a `ProductVerdict` records only what a *real,
  fetched* review said (reviewer name + tier + quote + optional stars). Real by
  construction.
- **BCC's own** — `bcc_blurb` + `critique` + `bcc_pick` are *our* editorial
  opinion. Clearly ours; needs no verification (it's opinion, not a fabricated
  attribution).

Rules:
- The review **URL is stored for provenance** (re-extract / audit) but is
  **never rendered as an outbound link** — we show the reviewer's *name* + the
  quote, not a link. Don't send users away.
- **Affiliate links are OURS.** A source's tagged URL (e.g. ATK's
  `tag=akotrx02554-20`) is captured as *identity only* (retailer + ASIN) and
  **never reused**; `RetailerOffer.affiliate_url` is minted with our own tags.
- The LLM is a **lead generator, never a source of record** (an unprompted
  "best loaf pans" answer had fabricated/unverifiable rating attributions and
  zero real links — the cautionary example that drove this whole design).

---

## 5. Homogenization (many sources → one BCC recommendation)

When 2+ sources review the same class, an LLM **condenses their verdicts** into:
- a per-product **BCC pick** (`bcc_pick`: "Best Overall" / "Best Value" /
  "Premium") + `bcc_blurb`/`critique`, and
- a class-level **buying guide**.

Consensus across reputable sources is the strongest ranking signal.

---

## 6. Ranking — the product "OU"

`Product.rank_score` = **review consensus × rating stats × value** (price-band
fit). The product analog of the recipe OU/power blend. Tier (Co-Winner →
Discontinued) and how many trusted sources endorsed it are the dominant inputs.

---

## 7. Buy enrichment (the perishable layer)

A **separate pass** turns retailer *identity* (ASIN + retailer) into our own
affiliate links + **current price/savings**, via SerpAPI **Shopping** (or
Amazon PA-API). Up to three sources: **Williams Sonoma / Sur La Table / Amazon
/ manufacturer (if affiliate)**. Refreshed periodically — prices go stale, the
verdicts don't (so they live in separate write paths, like a dish refresh).

---

## 8. The recipe ↔ product link

A recipe/dish's **`equipment`** is a list of **`product_class` references**
(precise — "Loaf Pans (1 lb)", not free text). The chain:

> recipe → `equipment` → `product_class` → ranked `products` → display

Equipment needs themselves are *dish-invariant* and LLM-derived once per dish
(same "derive once" pattern as ethnicity/story).

---

## 9. Display

A **"we think you'd like"** popup / mobile page (separate from the admin master
form). Each product card: image, name, **BCC pick badge**, our blurb, a
**verified review quote + reviewer name (no link)**, key specs, and the **top-3
buy options** with price/savings. Reachable from a recipe via §8.

> **Master form ≠ display.** The admin **product master form** (a 4th clone of
> the a/c/d/v editor template) holds *all* fields for curation; the public card
> is a separate, lean surface.

---

## 10. Data model & tables

**`product_model.py`** (Pydantic, recipe-model analog):
`ProductCategory`-implied · `ProductClass` · `Product` · plus `ReviewSource`,
`ProductVerdict`, `ProductSpecs`, `RetailerOffer`. A `Product` carries a list of
`verdicts` (one per source), our editorial, and `retailer_offers`.

**Tables** (`intake/products/catalog_store.py → ensure_product_tables`):
- `product_categories(name PK, created_at)`
- `product_classes(name PK, category, criteria JSON, buying_guide, data JSON, embedding, …)`
- `products(id, product_id uuid, product_class, category, brand, name, data JSON, rank_score, embedding, …)`

Everything rich lives in the `data` JSON blob (the master_recipes pattern), with
a few indexed columns + an `embedding` BLOB for similarity.

---

## 11. Pipeline parallel (what we reuse)

| Recipe pipeline | Product pipeline |
|---|---|
| `dish.queries` → SerpAPI → Moz DA/PA → OU/power rank → `master_recipes` | review bookmarklet → per-source decoder → verdicts → homogenize + rank → `products` |
| dish refresh job | product-class refresh (re-extract review / re-price) |
| `recipes_master_vec` "we think you'd like" recommender | `products_vec` recommender |
| chapters / dishes / recipe-form editors | category / class / product-master editors |

---

## 12. Status & roadmap (2026-06-02)

**Built + proven tonight:**
- `product_model.py` — the model.
- `intake/products/review_parsers.py` — `detect_source` + deterministic
  `parse_atk` (proven on a real ATK loaf-pan review → 13 products).
- `intake/products/catalog_store.py` — the 3 tables + `persist_extraction`.
- **End-to-end:** ATK review → 13 `products` rows in the DB under
  `Loaf Pans (1 lb)` / `Bakeware`.

**Roadmap (in order):**
1. **Homogenization** — 2+ sources → one BCC pick/blurb per product.
2. **Buy enrichment** — our affiliate links + live prices.
3. **Ingestion endpoint + bookmarklet** — `/extract-product-review` so the
   curator bookmarklets a review and the product appears.
4. **Product master form** (4th a/c/d/v editor) + class/category editors.
5. **Display** — the "we think you'd like" popup / mobile page.
6. **`equipment` → `product_class`** link on dishes/recipes.
7. **More source decoders** (Serious Eats, Wirecutter).
8. **`products_vec`** for product-to-product similarity / cross-sell.

Related memory: `project_affiliate_catalog`, `project_dish_catalog_table`,
`project_ou_power_blend`.
