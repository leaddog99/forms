# Book review curation — the cookbook answer to ACDV

*Design 2026-09-05. Status: DESIGN — nothing built. Memory: `project_book_curated_reviews`.*

## The problem, and the shape of the answer

The curated-collection (ACDV) pipeline ranks a product class by reading what
named authorities published about it. **No authority publishes equipment-style
roundups of cookbooks**, so that sources stage has nothing to read — but Amazon
book pages carry two mineable evidence sources (editorial reviews; the AI
customer-review summary), and we already hold the owner arithmetic (widget
histograms → RealRank, Wilson from the search cohort).

The answer is NOT a parallel pipeline. It is the existing ACDV pipeline with
**one stage swapped**: evidence comes from per-book Amazon product data instead
of SERP-fetched publisher pages. Everything downstream — the one research call,
shape validation, picks, the brief, the editor UI (ack, exclude, fix-ASIN,
step-up), materialize — is reused as-is or nearly so.

## Two-stage mapping (the gospel holds)

- **Harvest SELECTS**: the existing cookbook *search* collections (137, on the
  quoted-URL model) are the pool. Amazon's shelf + the off-class screen decide
  who is even considered. Pool for a run = top ~10 non-excluded candidates by
  Wilson.
- **Curation RANKS within the pool**: a curated run whose evidence is the
  pool's own Amazon data. Closed world — the model may not introduce a book
  that isn't in the pool (the no-hallucinated-brands rule, inherited).

This gives cookbook classes the same duality equipment classes already have
(e.g. Electric Hand Mixer exists as both a search collection and a curated
collection): search = cohort + arithmetic, curated = ranked picks + why-copy.

## Stage map

```
pool (product_collections)      candidates, Wilson-ordered, off-class screened
  └─ evidence fetch             per ASIN: ONE Traject product call with
                                include_summarization_attributes=true
                                → editorial_reviews[{title,body,rating}]
                                  customers_say / summarization_attributes
                                  book_description, publisher, publication_date,
                                  isbn_10/13, reading_age, bestsellers_rank
                                + widget histogram → RealRank (free, existing)
                                + captured reviews overlay (matched_captures —
                                  a bookmarklet-captured NYT cookbook roundup
                                  joins exactly like it does for equipment)
  └─ research                   ONE LLM call (llm.py gateway), book prompt
                                variant; ranks top 3 within pool, writes
                                best_for / why_it_ranks_here / edge_over_next /
                                important_tradeoff with provenance labels
  └─ verify (LIGHT)             shape validation unchanged; identity/ASIN
                                recovery SKIPPED (evidence was fetched BY ASIN
                                — identity is given, not recovered); owner
                                stats already in hand; offers = Amazon only
  └─ picks + brief              same curated_collection_picks table, same
                                brief render, same editor page
  └─ materialize                same to_products path; find-or-create by ASIN
                                is idempotent ACROSS the search collection's
                                medal path, so no duplicate products
```

## Schema deltas (small)

`curated_collections` gains two columns:

- `source_mode TEXT DEFAULT 'authorities'` — `'authorities'` (today's ACDV) |
  `'amazon_pool'` (this design). A branch in `pipeline.run`'s sources stage,
  not a new runtime.
- `pool_collection TEXT` — the `product_collections.name` supplying the
  candidate pool. **This is the first live instance of the class↔collection FK
  the render/EV build needs (START-HERE item 6)** — design it once, use it
  twice.

`curated_collection_picks` is unchanged. Book-specific display facts
(publisher, year, page count) ride in the row's existing free-text fields
(`capacity` holds "publisher · year" the way it holds "5.5 qt" today) — if the
render layer later wants them typed, that's a column addition then, not now.

## The prompt contract (defense stack, translated)

- **Class boundary**: `class_criteria` binds exactly as for equipment — this is
  what catches the Israeli-American-facsimile failure ("Amazon has no such
  shelf" → 1796 reprints ranked well by arithmetic but off-class by meaning).
- **Evidence roles are FIXED, not blended**:
  - *Owner arithmetic* (RealRank, Wilson, rating counts) is the ranking
    backbone. The model may deviate from arithmetic order **only with a stated,
    grounded reason** ("higher-rated but it's a facsimile edition / a spiral
    reprint / not on-class").
  - *customers_say / summarization_attributes* = the owner voice — quotable as
    "owners say", it is Amazon's AI summary and is labeled as such.
  - *editorial_reviews* = attributed color ("Kirkus:", "Paula Wolfert:"). The
    blurbs are publisher-SELECTED, so they are never the reason a rank exists —
    they explain a pick, they cannot carry one. (The independent-evidence rule,
    adapted: here the independent evidence is the arithmetic.)
- **Honest gap**: a fully-declared empty or partial ranking is valid (the
  Ground Nutmeg contract, d8b4a4e). A pool of facsimiles and stubs should say
  so, not rank three anyway.
- **No invention**: closed-world pool; the model SELECTs and synthesizes from
  fetched evidence, never from memory (the cook-KB discipline).
- `editors_choice` keeps its provenance firewall unchanged.

## What it costs (grounded in the token journal, 2026-09-05)

The research call dominates and the evidence vendor barely matters. Measured
over 108 real `curate_research` calls: avg 46K in / 17K out on Sonnet ($2/$10
per MTok) ≈ $0.26 — and book runs feed ~10 compact JSON evidence summaries
instead of eight fetched publisher pages, so expect LESS input: **~$0.15–0.25**.
Evidence: 10 product calls ≈ $0.015–0.46 on Traject (plan-dependent) or
**$0.005 on EasyParser Beginner** ($0.0005/req). All-in **~$0.20–0.30/class**;
the full 137-class cookbook family ≈ $30–40. Per-class evidence cache mirrors
`fetch_docs`' disk cache (keyed by ASIN) so re-runs and prompt iterations don't
re-bill; widget histograms stay free.

**PROBED 2026-09-05 (2 credits: Acadiana Table 1558328637, Salt Fat Acid Heat
1476753830 — probe JSONs in scratchpad; re-run any time):**

- `customers_say_summary.text` = the PROSE AI summary, populated on both.
  ("Customers find the cookbook's recipes authentic and easy to follow…")
- `customers_say` = per-attribute sentiment WITH mention counts
  ("Recipe quality(26): POSITIVE") — populated on both.
- `top_reviews` = 11 full review bodies — the same material that grounds
  owner themes for equipment.
- Book fields all populated: authors, publisher, publication_date, isbn_10/13,
  book_description (1K chars), format, bestsellers_rank per category
  (e.g. #294 in Cajun & Creole Cooking).
- **`editorial_reviews`: documented but DID NOT POPULATE — 0 for 2**, including
  SFAH whose Amazon page verifiably carries the section. No request parameter
  gates it (checked the parameter docs). Treat it as UNAVAILABLE from Traject
  today. Possible substitutes, untested: `include_a_plus_body` (the "From the
  publisher" A+ section often carries press quotes for books, +credits), the
  bookmarklet capture path (a curator can capture any page section manually),
  or EasyParser's schema-named field (probe after 9/24 credits reset).

The architecture survives the miss cleanly: editorial blurbs were designed as
attributed COLOR that can never carry a rank — the ranking backbone
(arithmetic + customers_say + top_reviews bodies) is fully served. Note
`include_summarization_attributes=true` bills 2 credits instead of 1; per-class
evidence is still pennies.

## Sequencing and open questions

Build after (or alongside) the class↔collection FK, since `pool_collection` IS
that FK's first instance. Before building: run ONE hand-driven probe (a single
Traject call on a known cookbook ASIN) to confirm `editorial_reviews` and
`customers_say` actually populate for books the way the docs promise — the
EasyParser rating-breakdown episode says never trust a field until it's been
measured on our own data.

Open (curator calls, none blocking the probe):

1. Does the curated run PROPOSE medals back onto the search collection
   (gold/silver/bronze pre-filled, curator confirms), or stay a separate
   surface? Proposal-not-write matches the chip-approval idiom.
2. Categories for books (sub-genres: "baking", "weeknight") — start with NONE,
   whole-class only, same as the equipment default.
3. Generalizes to any book class (per the curator: "cookbooks, or any books")
   — nothing above is cookbook-specific except the class criteria prose.
4. **Award mining (curator ask, 2026-09-05).** Book descriptions front-load
   verifiable public claims — The Wok's opens "#1 NYT Bestseller · #1 WaPo
   Bestseller · Winner of the 2023 James Beard Award · Time's 10 Most
   Anticipated · NPR Books We Love" before any prose. The prompt now carves
   these out of the marketing rule (a named award is an independent
   institution's judgment the publisher merely reports — it may support a
   rank, attributed and flagged publisher-stated). NEXT STEP when wanted:
   deterministic per-book award extraction into its own column (awards as
   structured data — a James Beard/IACP/bestseller-list dictionary), which
   would also serve render-layer badges. Also measured: category
   bestsellers_rank is strong on-class arithmetic (The Wok = #1 in Wok
   Cookery).
