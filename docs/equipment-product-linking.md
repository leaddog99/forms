# Equipment standardization → product linking — design note

**Status:** design + research (2026-07-12). Captures the gap between today's free-form recipe
`equipment` and the long-standing `equipment -> product_class` commerce goal, and how to close
it: a canonical **tool dictionary** + a **cross-reference table** anchored to the **Google
Product Taxonomy**, built **offline** from our own corpus.

Related: `memory/project_affiliate_catalog.md`, `memory/project_enrich_equipment.md`,
`memory/project_product_commerce_build.md`, `memory/project_dish_catalog_table.md` (the parallel
"build a canonical dictionary offline" pattern), `memory/feedback_single_path.md`,
`memory/feedback_no_data_in_code.md`. Size tooling: `intake/products/measures.py`.

---

## 1. Current state — measured from our corpus (2026-07-12)

- **16,270** equipment items across recipes; **1,815 distinct names**; **250 distinct size strings**.
- **No standardization is applied.** The only processing is dedupe-by-lowercased-name in
  `enrich/equipment.derive_equipment` + `save_recipe_api._recipe_equipment_from_cook`.
- Names are a heavy variant tail: `whisk` (639), `large bowl` (576), `bowl` (519), … plus
  `wooden spoon` vs `wooden spoon or spatula`, `loaf tin` vs `loaf pan`, `baking sheet` vs
  `rimmed sheet pan`. **Head nouns cluster cleanly** — bowl (2,109), knife (829), pan (741),
  spoon (735), whisk (690), pot (683), skillet (522) — the natural canonical-class backbone.
- Sizes are messy: `9x13 in` **and** `13x9 in` (transposed duplicates), `medium`/`large`
  (qualitative, not dimensional), and a **bug**: stringified dicts like
  `{'imperial': '4 qt', 'metric': '4 l', 'convertible': true}` leaked in when a `_cook` size
  **dict** was stored raw instead of a string (see §6).

Every `equipment -> product_class` reference in the code (`enrich/equipment.py`,
`recipe_model.py`, `save_recipe_api.py`) is an **aspirational comment** — the mapping doesn't
exist yet.

## 2. The target: a 3-layer link

```
 recipe equipment mention            canonical tool                 product_class          products
 "loaf tin" / "loaf pan (9x5)"  →   loaf_pan (9x5 in)      →   GPC 668… Bakeware …  →   affiliate SKUs
        (1,815 raw)                (~150–250 canonical)          (Google taxonomy)        (our catalog)
```

- **Layer A — mention:** the raw string as it appears on a recipe (the long tail).
- **Layer B — canonical tool:** the resolved tool + normalized size; carries the `product_class`.
- **Layer C — products:** the affiliate catalog rows (already have `product_class`/`category` +
  `products_vec` KNN from the product-commerce build).

The **cross-reference** is A→B (alias resolution) and B→`product_class`→C.

## 3. Anchor `product_class` to the Google Product Taxonomy (GPC)

Rather than invent a taxonomy, adopt **Google Product Category** — the ~6,000-node public taxonomy
that Google Shopping / Merchant Center / affiliate feeds already use. It has exactly our leaves under
**Home & Garden > Kitchen & Dining**:

- **Kitchen Tools & Utensils (668)** — Slotted Spoons, Flour Sifters, Kitchen Shears, Ricers,
  Carving Forks, Whisks, Graters, Spatulas, Colanders, Cutting Boards, Mixing Bowls, …
- **Cookware & Bakeware** (+ Cookware Accessories 4424) — pans, pots, skillets, saucepans,
  Dutch ovens, baking dishes, sheet pans, loaf/cake pans.
- **Kitchen Appliances** — stand mixers, food processors, blenders, deep fryers.
- **Tableware (672)** — plates, bowls-as-serving, cups.

Why GPC: (a) it's free + downloadable as one text file (`taxonomy-with-ids.en-US.txt`, ~6k lines);
(b) affiliate/retailer feeds are already tagged with it, so **canonical tool → GPC → products** is
the natural, zero-glue bridge to the catalog; (c) it's a stable external standard, not code-owned
data ([[feedback_no_data_in_code]]). `size` stays the intra-class grain ("Saucepans (2 qt)").

## 4. The cross-reference tables

Three tables (the classic mention/canonical/product split from entity-resolution practice):

```sql
-- B: the canonical tool dictionary (offline-built, human-reviewed, ~150–250 rows)
CREATE TABLE canonical_tools (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,          -- canonical label, e.g. "loaf pan" (most-frequent variant)
    head_noun     TEXT,                   -- "pan" — the blocking key
    gpc_id        INTEGER,                -- Google Product Category leaf id  <- the product_class
    gpc_path      TEXT,                   -- "Home & Garden > Kitchen & Dining > … > Bakeware"
    description   TEXT,                   -- one line, powers embedding + disambiguation
    embedding     BLOB,                   -- for matching NEW mentions at ingest (sqlite-vec)
    is_appliance  INTEGER DEFAULT 0
);

-- A→B: THE cross-reference. Every raw mention we've seen → its canonical tool.
CREATE TABLE tool_aliases (
    alias         TEXT PRIMARY KEY,       -- lowercased raw mention, e.g. "loaf tin"
    canonical_id  INTEGER NOT NULL REFERENCES canonical_tools(id),
    confidence    REAL,                   -- cluster/LLM confidence
    source        TEXT                    -- 'cluster' | 'llm' | 'manual'
);

-- B(+size)→C: canonical tool + size grain → catalog products.
CREATE TABLE tool_products (
    canonical_id  INTEGER NOT NULL REFERENCES canonical_tools(id),
    product_id    INTEGER NOT NULL,       -- catalog row
    size_grain    TEXT,                   -- normalized, e.g. "9x5 in" (NULL = size-agnostic)
    rank          INTEGER,                -- TBOTB pick order
    PRIMARY KEY (canonical_id, product_id, size_grain)
);
```

Live path stays a **cheap lookup**: mention → `tool_aliases` → `canonical_tools.gpc_id` →
`tool_products`. A brand-new mention not in `tool_aliases` falls back to a `canonical_tools`
embedding KNN (sqlite-vec) + threshold, and gets appended as a new alias (self-learning, mirrors
the URL-word pre-filter's learn loop).

## 5. Build it externally (offline batch) — the recipe

Entity-resolution best practice = **block → embed → cluster → canonical label → classify**. All
offline, one-time (then incremental):

1. **Extract vocab** (done) — 1,815 names + frequencies.
2. **Block by head noun** — group by last token (bowl/pan/pot/…). Keeps clustering O(block²), not
   O(1815²), and prevents cross-class merges.
3. **Cluster within blocks** — embed normalized mentions (OpenAI `text-embedding-3-small`, already
   our embedder), pairwise cosine, **greedy agglomerative merge at ≥0.90–0.93** (conservative, per
   the literature, to avoid merging "small bowl" into "large bowl" — size adjectives handled in §6,
   not as separate tools). Canonical label = **most-frequent variant** in the cluster (we have the
   counts).
4. **Classify each canonical tool → GPC leaf** — one LLM pass over the ~150–250 canonicals (cheap,
   ~$1), forced-choice against the downloaded GPC kitchen subtree, with confidence. **Human-review**
   the result (small table, an afternoon — this is the moat, like the dish dictionary).
5. **Normalize sizes** — reuse `intake/products/measures.py` (single-path, [[feedback_single_path]]):
   unicode fractions → decimals, canonical unit symbols, **sort dimensions** so `9x13` == `13x9`,
   and split **qualitative** (`small/medium/large`) into a separate `size_qualifier` field vs the
   dimensional `size_grain`.
6. **Load** `canonical_tools` + `tool_aliases`; wire `tool_products` off the existing catalog's
   `product_class`.

**Feasibility: yes, cleanly external.** 1,815 mentions → ~150–250 canonicals; embedding is cents;
the LLM GPC-mapping is ~$1; the only real labor is reviewing ~200 canonical rows once. It's the same
"derive a canonical dictionary once, offline, free" pattern as [[project_dish_catalog_table]].

## 6. Data-quality fixes this surfaced (do alongside)

- **Stringified size-dict bug.** `_recipe_equipment_from_cook` (and the batch mirror) store
  `_cook` equipment `size` **verbatim**; when that size is a `{imperial, metric, convertible}`
  dict it lands as a raw dict → serialized to a `"{'imperial': …}"` string. Fix: coerce to the
  imperial face string on mirror (and one-time repair of existing rows).
- **Transposed dimensions** (`9x13` vs `13x9`) → canonical-order in `measures.py`.
- **Qualitative sizes** (`medium`) are not dimensions → separate field, don't feed them to
  `product_class` size-grain.

## 7. Sources

- Google Product Taxonomy — [Kitchen & Dining (638) / Kitchen Tools & Utensils (668)](https://productcategory.net/finder/home-and-garden/kitchen-and-dining/kitchen-tools-and-utensils/); full downloadable list `taxonomy-with-ids.en-US.txt` (~6k categories).
- Entity resolution / canonicalization — [The Rise of Semantic Entity Resolution (Towards Data Science)](https://towardsdatascience.com/the-rise-of-semantic-entity-resolution/); [Entity Resolution: Top Techniques (Spot Intelligence)](https://spotintelligence.com/2024/01/22/entity-resolution/) — block → embed → agglomerative-merge (cosine ≥0.93) → most-frequent-name canonical → LLM for hard matches.
