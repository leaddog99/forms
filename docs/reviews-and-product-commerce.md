# Reviews & Product Commerce — Architecture

*Last updated 2026-10-28 (§13 added; §3 and §12 describe the June 2026 bookmarklet path and are superseded by the curated run in §13).*

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

---

## 13. The curated run, step by step (as of 2026-10-28)

What actually happens when a curated collection goes out to find reviews. Read
from the code that runs today: the `curated_collection_run` job handler in
`save_recipe_api.py`, `intake/products/curate/{pipeline,prompt,verify,
to_products,amazon_owners}.py`, and the fetcher in
`docs/RealRank/realrank_research.py`. One sentence per step.

### Kick-off

1. Staff presses Run on a curated collection, and the server enqueues a
   `curated_collection_run` job carrying only the collection name, so everything
   else is read from the collection row at run time.
2. The worker loads that row and pulls out the product class name (what gets
   researched), the optional categories (sub-sections like "Best budget"), the
   fallback search terms, any pinned Editor's Choice, the class-boundary
   criteria, and whether Amazon owner reviews are switched on.
3. If Amazon owner reviews are on, the job links the class to an Amazon search
   pool (creating one from the class name if none exists) and refreshes that pool
   inline when it is older than `amazon_pool_ttl_days`, which runs the saved
   Amazon search lines, unions the listings by ASIN, screens them, and scores
   each one on its owner-rating histogram (`_collection_refresh_sync`).

### Fetch: getting the publishers' own pages

4. The run checks `cache/curate/<class>.docs.json` first, and if a cached fetch
   exists and refresh was not requested, it reuses those pages rather than
   paying for the fetch again (the cache is keyed by class alone, so edited
   search terms need a refresh to matter).
5. Otherwise it walks a fixed list of nine named authorities in order
   (America's Test Kitchen, Serious Eats, Wirecutter, The Kitchn, Reviewed,
   Tom's Guide, TechGearLab, Consumer Reports, BestReviews), four at a time in
   parallel (`SOURCE_SITES`, `fetch_source_docs`).
6. For each publisher it runs one Google query through the SERP provider shaped
   as `site:<publisher> <class name> review`, trying the class name first and
   then each fallback term in order, and dropping model numbers for a second
   broader query when the name contains one.
7. An empty SERP result is retried once after four seconds, because the
   provider sometimes serves a blank page and a silent miss would mean a
   source-less run.
8. Every hit is checked against the host it claims (`_host_ok`), because a
   `site:` query that matches nothing quietly returns unrestricted results, and
   the system once labelled KitchenAid's homepage as three different reviewers.
9. Surviving candidates are sorted so pages whose title names the class go
   first, since the fetch is the expensive step and should be spent on the
   likeliest page.
10. At most three pages per publisher are fetched through the same tiered ladder
    the recipe pipeline uses: direct request with the user-agent chain, then the
    paid unblocker, then a Wayback snapshot, with the rung that served it
    recorded as `via`.
11. The fetched page must mention a class term at least twice or it is rejected
    as off-class (`_class_pattern` / `_on_class_hits`), which is what stops an
    ATK pressure-cooker roundup standing in for a pressure-canner answer.
12. A page that passes is trimmed to about 18,000 characters around the
    paragraphs that mention the product's distinctive words
    (`_trim_for_prompt`), because a 100k-character roundup buries the relevant
    verdict.
13. A publisher where every rung failed or every page was off-class is recorded
    as missing with the reason and the best URL found, and is never replaced by
    a mirror or a different site; the last rung is human (capture it with the
    review bookmarklet).
14. The fetch results are written to the cache, and then bookmarklet captures
    from the `reviews` store are overlaid on top (`_overlay_captured_reviews`):
    a captured page replaces a failed or thinner fetched page for the same
    reviewer, a captured reviewer not in the list is appended, and a structured
    digest never overwrites real page text.
15. If owner reviews are on, the Amazon pool's top ten listings by RealRank are
    fetched one by one through Rainforest (cached per ASIN under
    `cache/curate/asins/`) and assembled into a single document labelled
    "Amazon (owner reviews)" carrying each listing's star arithmetic, histogram,
    Amazon's AI summary, attribute sentiment, and three top reviews
    (`amazon_owners.owner_doc`).

### Research: one grounded model call

16. The prompt is built from the curator's original research rules plus the
    explicit ranking weights, the class boundary, the criteria and pinned pick
    if any, and every retrieved document inlined verbatim under its publisher's
    name, with the missing publishers listed under "could not retrieve"
    (`prompt.build_prompt`).
17. The prompt tells the model the Amazon document is owner voice that may be
    quoted as "what owners say" but can never count as the independent
    editorial source a rank requires (rule 12b), and that a product appearing
    only there may not be ranked.
18. One streaming call goes to the research model through the LLM gateway
    (`operation=curate_research`), and the raw reply is saved to
    `cache/curate/<class>.raw.txt` before any parsing, because the call is the
    expensive artifact and a parse failure must not cost a second call
    (`reuse_raw` replays it).
19. A reply cut off at `max_tokens` is reported as truncated rather than
    malformed, and a reply with an unescaped quote inside a string is repaired
    before parsing (`repair_json_quotes`).
20. The parsed record carries three overall picks, three per requested
    category, each with `why_it_ranks_here`, `edge_over_next`,
    `important_tradeoff`, the `source_links` actually used, a `typical_price`
    separate from `current_price`, plus `source_report` (what each supplied
    document covered and whether it was used) and `omitted_slots` (the honest
    gap).
21. A deterministic title gate (`verify.flag_offclass_titles`) then flags any
    pick whose title names neither the class nor a fallback term with a visible
    `identity_warning`, flagged rather than deleted.

### Verify: the part only we can do

22. The record is shape-checked first (`validate_shape`: places 1 to 3 each
    filled or declared omitted, ASINs ten characters, source links present, and
    at least one source link per pick that is not the maker's site, Amazon, or
    the pick's own buy link per `check_independent_sources`), and a shape
    failure refuses to build rather than emitting a plausible-but-broken brief.
23. For a pick with no ASIN, `resolve_asin` looks for one in our own review
    corpus (buy links other reviewers already put on that brand's products),
    then in Google scoped to amazon.com, scoring every candidate against the
    pick's brand, type noun, model number and title words
    (`identity.identity_score`), and leaves it blank when nothing passes.
24. Every ASIN is then checked against the live Amazon listing through
    Rainforest (`_verify_score_row`), and a listing that scores as a different
    product gets its ASIN rejected and re-resolved once, with two failures
    leaving the ASIN blank and the warning on the row.
25. A verified listing contributes its photo, its model number if the research
    left one blank, and Amazon's AI review summary and attribute themes onto the
    pick (`owner_summary`, `owner_themes`).
26. Buy links are gathered from every retailer the reviews we hold link for that
    product (`buy_links.offers_from_reviews`) plus the model's own link, with
    each URL stripped of tracking parameters by `clean_url`, Wirecutter-style
    opaque redirectors dropped entirely because they cannot be cleaned, and
    `affiliate_url` left blank to be minted at click time.
27. The free Amazon widget supplies the real five-to-one star histogram
    (`amazon_widget.rating_histogram`), from which the RealRank score and
    polarization shape are computed onto the pick.
28. The written brief is rendered from the record and the verification report
    (`render.render`), including a per-source accounting line for each
    authority saying what its page covered and whether the model used it
    (`_source_accounting`).

### Store and materialize

29. The run's placements are stored on the collection with delete-and-replace
    (`replace_picks`: a run re-ranks the whole class), the brief and record are
    saved by `set_run_result`, and any previous approval is reset because new
    evidence must be signed off again.
30. Each placement is then turned into a catalog product row
    (`to_products.materialize`) keyed by ASIN when one exists and by brand plus
    title otherwise, with one product holding several placements, Amazon offers
    sorted first, and a pick whose listing was flagged as suspect getting its
    Amazon offer dropped rather than published.
31. A failure in that last step does not lose the run, since the brief and
    picks are already stored and the error is reported in the job summary.
32. The curator's Approve button (`curated_collections.approve`) is the gate
    between this automated ranking and anything that earns affiliate revenue
    from it.

Two things worth knowing. Wirecutter and the others are never searched by the
model itself; the system fetches their pages and hands them over, because a
browsing model silently substituted Forbes for ATK when blocked (measured
2026-07-25). And Amazon plays two roles: as the owner-voice document in the
research step when switched on, and as the identity and histogram authority in
the verify step for every pick regardless.
