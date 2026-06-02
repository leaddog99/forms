"""BCC product catalog data model — the commerce-side analog of
recipe_model.py. Three levels mirror the recipe side exactly:

    product_categories  ≈ chapters       e.g. "Bakeware"
    product_classes      ≈ dishes          e.g. "Loaf Pans (1 lb)"  (curated, ranked, embedded, refreshable)
    products             ≈ master_recipes  e.g. "USA Pan 1 lb Small Loaf Pan" (the SKU)

The one structural difference from recipes: a recipe has ONE source, but a
product aggregates MANY review sources (ATK, Serious Eats, Wirecutter…) — so a
Product carries a list of `verdicts`, and a later HOMOGENIZATION step condenses
them into BCC's own pick + blurb. The "review site" is therefore provenance
(like a recipe's source domain), NOT a hierarchy tier.

Ingestion: the curator bookmarklets a review page → source is detected →
intake/products/review_parsers.py decodes it (deterministic per-source code)
→ one ProductReviewExtraction (a class + that source's products).

Brand-safety rule (see memory/project_affiliate_catalog): a `verdict` only ever
records what a REAL fetched review said (verified by construction — the curator
extracted the actual page). BCC's own `bcc_blurb`/`critique` are clearly our
voice — opinion, never a fabricated third-party attribution. Affiliate links in
`RetailerOffer.affiliate_url` are OURS; a source's tagged URL is kept only as
identity, never reused or rendered as an outbound review link.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class ReviewSource(BaseModel):
    """Provenance of one extracted review (≈ a recipe's `_source`). `url` is
    kept for re-extraction/audit only — per policy it is NEVER rendered as an
    outbound link (we don't send users away)."""
    reviewer: str = ""                       # "America's Test Kitchen"
    authors: List[str] = Field(default_factory=list)
    title: str = ""
    url: str = ""                            # provenance only — not displayed
    last_updated: str = ""
    captured_at: str = ""                    # when the curator bookmarkleted it
    rating_scale: str = ""                   # "3-star (Good/Fair/Poor)"
    screenshot_id: str = ""                  # media.db page screenshot — visual proof


class ProductSpecs(BaseModel):
    model_number: str = ""
    material: str = ""
    dimensions_in: str = ""
    capacity: str = ""
    weight: str = ""
    dishwasher_safe: str = ""                # "No" | "Yes" | "Yes, hand wash rec"


class ProductVerdict(BaseModel):
    """What ONE reviewer concluded (a product has one per source)."""
    reviewer: str = ""
    tier: str = ""                           # Co-Winner | Winner | Highly Recommended | Recommended | Recommended with Reservations | Not Recommended | Discontinued
    summary: str = ""                        # the reviewer's prose (verbatim-ish)
    ratings: dict = Field(default_factory=dict)   # {"Performance":3,...} — vision-filled from the page image; optional
    price_at_test: Optional[float] = None


class RetailerOffer(BaseModel):
    """Where to buy. We capture identity (retailer + ASIN) and mint OUR OWN
    affiliate link — a source's tagged URL is identity only, never reused."""
    retailer: str = ""                       # Amazon | Williams Sonoma | Sur La Table | OXO | manufacturer…
    asin: str = ""                           # Amazon ASIN when known
    source_url: str = ""                     # URL as seen in the review (their tag) — identity only
    affiliate_url: str = ""                  # OUR link (buy-enrichment pass)
    price: Optional[float] = None            # current price — perishable; buy-enrichment pass
    savings: Optional[float] = None


class Product(BaseModel):
    """One product — the master_recipe analog. Holds each source's verdict,
    specs, where-to-buy, and BCC's own homogenized editorial."""
    product_class: str = ""                  # ≈ _master.dish — the class it belongs to
    category: str = ""                       # ≈ classification.chapter
    brand: str = ""
    name: str = ""
    specs: ProductSpecs = Field(default_factory=ProductSpecs)
    verdicts: List[ProductVerdict] = Field(default_factory=list)   # one per review source (consensus)
    image_url: str = ""                      # to coopt into our own store
    retailer_offers: List[RetailerOffer] = Field(default_factory=list)
    # BCC editorial — OUR voice (homogenized from the verdicts), not attribution:
    bcc_pick: str = ""                       # "Best Overall" | "Best Value" | "Premium" | ""
    bcc_blurb: str = ""
    critique: str = ""
    # ranking ("product OU"): review consensus × rating × value (TBOTB rank)
    rank_score: Optional[float] = None
    sources: List[str] = Field(default_factory=list)   # reviewer names covering this product


class ProductClass(BaseModel):
    """A curated, rankable grouping — the dish analog (the unit a recipe's
    `equipment` maps to, the unit that gets embedded + refreshed)."""
    name: str = ""                           # "Loaf Pans (1 lb)"
    category: str = ""                       # ≈ chapter: "Bakeware"
    criteria: List[str] = Field(default_factory=list)     # ["Performance","Ease of Use","Cleanup/Durability"]
    buying_guide: str = ""                   # homogenized "what you need to know"
    sources: List[ReviewSource] = Field(default_factory=list)   # all reviews feeding this class


class ProductReviewExtraction(BaseModel):
    """One bookmarkleted review page → a class + that source's products.
    Output of a source-specific parser (parse_atk, parse_seriouseats, …)."""
    product_class: ProductClass = Field(default_factory=ProductClass)
    review_source: ReviewSource = Field(default_factory=ReviewSource)
    products: List[Product] = Field(default_factory=list)
