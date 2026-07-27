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
    mpn: str = ""                            # manufacturer part number — VENDOR-AGNOSTIC identity
    gtin: str = ""                           # GTIN / UPC / EAN — global vendor-agnostic identity
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


class RatingSource(BaseModel):
    """ONE retailer's star histogram for this product. Kept per-retailer even after
    pooling, because a combined number is only honest if the split stays visible."""
    source: str = ""                         # amazon | bestbuy | walmart | …
    listing_id: str = ""                     # the ASIN/SKU we actually scored
    url: str = ""
    avg_rating: Optional[float] = None
    count: Optional[int] = None
    histogram: List[int] = Field(default_factory=list)   # 5..1 COUNTS
    fetched_at: str = ""


class OwnerRatings(BaseModel):
    """Owner sentiment as arithmetic, never as prose. `histogram`/`review_count` are
    POOLED across `sources` (counts summed, not scores averaged — see
    realrank_index.pool_histograms); an empty or zero histogram is skipped, not counted."""
    avg_rating: Optional[float] = None
    review_count: Optional[int] = None       # pooled n
    histogram: List[int] = Field(default_factory=list)   # pooled 5..1 counts
    sources: List[RatingSource] = Field(default_factory=list)
    polarization: dict = Field(default_factory=dict)     # {label, j_shaped, one_star_pct, …}


class ExpertFinding(BaseModel):
    """What ONE source said, as gathered by an automated RealRank run.

    Deliberately NOT merged into `Product.verdicts`: a verdict comes from a page a curator
    chose and ingested through the review store; a finding comes from the automated sweep.
    Keeping them apart preserves which is which. `via` records the rung of the fetch ladder
    that served it (unblocker / wayback / search / bookmarklet) — a finding read from a
    years-old snapshot should not read as current."""
    name: str = ""
    url: str = ""
    type: str = "expert"                     # expert | owner
    verdict_or_award: str = ""
    key_facts: List[str] = Field(default_factory=list)
    short_quote: str = ""
    via: str = ""
    fetched_at: str = ""


class RealRank(BaseModel):
    """**The number.** Owner-sentiment arithmetic and the evidence it was computed from.

    Split from RealStory deliberately: these two halves have different trust bases and
    different lifetimes. A score is reproducible from a histogram and goes stale monthly as
    ratings move; a written assessment is editorial and barely ages. One name over both hid
    that, and forced a cheap ratings refresh to re-run an expensive narrative.

    `score` is NPS-from-stars with a confidence penalty, and is DISTINCT from
    `Product.rank_score` (expert consensus × rating × value). They can legitimately
    disagree — America's Test Kitchen ranks the Lodge skillet mid-pack while 145,000 owners
    score it 86.8 — and conflating them would destroy the distinction.
    """
    score: Optional[float] = None
    score_basis: str = ""
    owner: OwnerRatings = Field(default_factory=OwnerRatings)
    computed_at: str = ""
    job_id: Optional[int] = None


class RealStory(BaseModel):
    """**The words.** Our written assessment of the product, and the attributed evidence
    behind it.

    Clearly OUR voice — that is the safe side of the brand-safety line: `findings` are facts
    attributed to sources we actually read, while verdict/one_liner/summary/pros/cons are
    BCC's opinion and need no third-party attribution. Carries its own approval because a
    human should read prose before it earns anything; a score needs no sign-off, it needs
    arithmetic.
    """
    verdict: str = ""                        # Top Pick | Highly Recommended | …
    one_liner: str = ""
    summary: str = ""
    aspects: List[dict] = Field(default_factory=list)     # [{name, sentiment}]
    pros: List[str] = Field(default_factory=list)
    cons: List[str] = Field(default_factory=list)
    cheaper_alternative: Optional[dict] = None
    findings: List[ExpertFinding] = Field(default_factory=list)
    coverage: List[dict] = Field(default_factory=list)    # [{name, status, note}]
    generated_at: str = ""
    model: str = ""
    job_id: Optional[int] = None
    files: dict = Field(default_factory=dict)             # {json, md, html}
    # Staff gate: nothing earns affiliate revenue off an unreviewed automated write-up.
    approved_by: str = ""
    approved_at: str = ""


class CuratedPlacement(BaseModel):
    """Where this product placed in ONE section of a curated class ranking, and why.

    A product holds several of these at once: overall #2 and "Best value" #1 are separate
    judgments about the same object, made in the same run, and each carries its own reasoning.

    `edge_over_next` is the field that makes a placement auditable. Ranking here is model
    judgment against the stated weights, not arithmetic — no weighted score is computed — so
    the stated reason IS the audit trail. Without it a rank can only be accepted, never
    challenged. For the last place in a section it names the strongest product that did NOT
    make the list, and what kept it out.

    Prices are recorded per placement because they are the EVIDENCE FOR THIS RANKING: the
    ranking input is `typical_price` (a sale is news, not a ranking signal), and a later price
    change does not retroactively change what was ranked on.
    """
    collection: str = ""                     # the curated_collections run that placed it
    section: str = ""                        # "" = overall; otherwise the category name
    place: Optional[int] = None              # 1 | 2 | 3 within the section
    best_for: str = ""
    why_it_ranks_here: str = ""              # why this product is good, on its own
    edge_over_next: str = ""                 # why it beat the one BELOW it — names that product
    important_tradeoff: str = ""             # the required "here is the catch"
    typical_price: Optional[float] = None    # what it was RANKED on
    current_price: Optional[float] = None    # perishable
    price_type: str = ""                     # regular | sale
    source_links: List[str] = Field(default_factory=list)   # what was actually read for this row
    ranked_at: str = ""


class Curation(BaseModel):
    """The class-ranking half of our editorial: where this product stands AGAINST THE OTHERS.

    Deliberately not folded into RealStory. RealStory judges one product on its own evidence
    and stays true whatever else exists; a placement is meaningless without the competing set
    and is invalidated the moment that set changes. They also refresh on different triggers —
    a new rival re-ranks everything and rewrites nothing.
    """
    placements: List[CuratedPlacement] = Field(default_factory=list)
    collection: str = ""                     # the run that last placed this product
    product_class: str = ""                  # the class it was ranked within
    ranked_at: str = ""
    job_id: Optional[int] = None
    # Staff gate, same as RealStory: an automated ranking must not earn until a human has read
    # it. A re-run resets this — new evidence does not inherit sign-off on the old.
    approved_by: str = ""
    approved_at: str = ""


class Product(BaseModel):
    """One product — the master_recipe analog. Holds each source's verdict,
    specs, where-to-buy, and BCC's own homogenized editorial."""
    product_class: str = ""                  # ≈ _master.dish — the class it belongs to
    category: str = ""                       # ≈ classification.chapter
    brand: str = ""
    name: str = ""
    description: str = ""                     # LLM-normalized FACTUAL product copy (material,
                                             # construction, size, key features) — de-marketed,
                                             # NOT our opinion (that's bcc_blurb). Human-facing AND
                                             # the primary semantic signal for matching/embedding.
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
    # The automated analysis, in two halves that age and are trusted differently:
    # RealRank = the computed score (refreshes cheaply, monthly);
    # RealStory = our written assessment + the attributed findings (rarely changes, needs a
    # human read before it earns). Both separate from rank_score above.
    realrank: Optional[RealRank] = None
    realstory: Optional[RealStory] = None
    # Where it placed when the whole class was ranked from the expert reviews. Third of the
    # trio and the only RELATIVE one: RealRank scores the owners' verdict, RealStory writes
    # ours, Curation says where it came in against its rivals.
    curation: Optional[Curation] = None


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
