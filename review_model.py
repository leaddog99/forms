"""Review model — the schema.org Review counterpart to recipe_model.RecipeModel.

A *review* is a FIRST-CLASS entity: a source's assessment of a product (an editorial
roundup verdict from ATK/Serious Eats/Wirecutter, a retailer customer review, a magazine
test). It is mined from ANY page by the review bookmarklet the way a recipe is mined from a
recipe page, and it can stand on its own (be stored, embedded, browsed) OR attach to a
product (Product.review). Reviews are where a product's matchable ATTRIBUTES come from —
so besides the faithful schema.org capture (reviewBody / rating / author) a review carries
a `_mined` block: the structured product attributes it evidences, which roll up into the
product's `_attributes`.

This SUBSUMES the old embedded `ProductVerdict`: an editorial roundup verdict is just a
Review whose `reviewRating.tier` is "Winner"/"Recommended"/etc. — no capability lost.

Mirrors recipe_model conventions: Pydantic + schema.org base, `@context`/`@type` aliases,
lenient `mode='before'` coercions, a STATIC vs USER field split, and `static_subset()`.

Brand-safety (see memory/project_affiliate_catalog): a Review only ever records what a REAL
fetched page said (verified by construction — the curator extracted the actual page). BCC's
own voice lives on the PRODUCT (bcc_pick/bcc_blurb), never as a fabricated review here.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from typing import List, Optional


def _as_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return " ".join(_as_str(x) for x in v if x is not None)
    if isinstance(v, dict):
        return _as_str(v.get("name") or v.get("value") or v.get("text") or "")
    return str(v)


def _as_str_list(v, split: bool = False) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        if split and "," in v:
            return [s.strip() for s in v.split(",") if s.strip()]
        return [v] if v.strip() else []
    if isinstance(v, dict):
        return [_as_str(v)]
    if isinstance(v, (list, tuple)):
        return [x if isinstance(x, str) else _as_str(x) for x in v if x]
    return [_as_str(v)]


class Rating(BaseModel):
    """schema.org Rating, extended with an editorial `tier` so a roundup verdict
    ("Winner"/"Recommended"/"Not Recommended") fits without a separate model."""
    model_config = {"populate_by_name": True, "extra": "allow"}
    type: Optional[str] = Field(default="Rating", alias="@type")
    ratingValue: Optional[float] = None
    bestRating: Optional[float] = 5
    worstRating: Optional[float] = 0
    tier: Optional[str] = ""      # editorial verdict tier (roundup reviews); "" for star-only
    ratingScale: Optional[str] = ""   # e.g. "3-star (Good/Fair/Poor)"

    @field_validator("ratingValue", "bestRating", "worstRating", mode="before")
    @classmethod
    def _num(cls, v):
        try:
            return float(str(v).split("/")[0].strip()) if v not in (None, "") else None
        except (ValueError, TypeError):
            return None


class ReviewAuthor(BaseModel):
    """The reviewer or the publication (schema.org Person | Organization)."""
    model_config = {"populate_by_name": True, "extra": "allow"}
    type: Optional[str] = Field(default="Organization", alias="@type")
    name: Optional[str] = ""

    @field_validator("name", mode="before")
    @classmethod
    def _n(cls, v):
        return _as_str(v)


class ReviewSource(BaseModel):
    """Provenance of one mined review (mirrors recipe `_source`; unions the old
    product_model.ReviewSource fields so the roundup path keeps every field). `originalUrl`
    is kept for re-extraction/audit only — per policy it is NEVER rendered as an outbound
    link (we don't send users to the source)."""
    model_config = {"populate_by_name": True, "extra": "allow"}
    originalUrl: Optional[str] = ""
    origin: Optional[str] = ""            # host / publication slug
    type: Optional[str] = "review"        # review | roundup | retailer | forum
    capturedAt: Optional[str] = ""
    authors: Optional[List[str]] = []
    title: Optional[str] = ""
    lastUpdated: Optional[str] = ""
    screenshotId: Optional[str] = ""      # media.db page screenshot — visual proof


class ReviewModel(BaseModel):
    model_config = {"populate_by_name": True, "extra": "allow"}

    context: Optional[str] = Field(default="https://schema.org", alias="@context")
    type: Optional[str] = Field(default="Review", alias="@type")
    id: Optional[str] = ""
    # What this review is OF — a product name and (once resolved) the product_id it maps to.
    itemReviewed: Optional[str] = ""
    productId: Optional[str] = ""
    productClass: Optional[str] = ""       # the class it belongs to (e.g. "Loaf Pans (1 lb)")
    name: Optional[str] = ""               # review headline/title
    author: Optional[ReviewAuthor] = None  # reviewer / publication
    publisher: Optional[str] = ""
    datePublished: Optional[str] = ""
    reviewBody: Optional[str] = ""         # the faithful review text (verbatim-ish)
    reviewRating: Optional[Rating] = None  # stars and/or editorial tier
    positiveNotes: Optional[List[str]] = []   # pros
    negativeNotes: Optional[List[str]] = []   # cons
    image: Optional[List[str]] = []
    source: Optional[ReviewSource] = Field(default=None, alias="_source")
    user_id: Optional[int] = None
    # OUR value-add: structured PRODUCT ATTRIBUTES this review evidences (flexible — the
    # "N attributes from review info"). Rolled up into the product's `_attributes`.
    mined: Optional[dict] = Field(default=None, alias="_mined")

    @field_validator("name", "itemReviewed", "productId", "productClass", "publisher",
                     "datePublished", "reviewBody", mode="before")
    @classmethod
    def _coerce_text(cls, v):
        return _as_str(v)

    @field_validator("image", "positiveNotes", "negativeNotes", mode="before")
    @classmethod
    def _coerce_lists(cls, v):
        return _as_str_list(v)


STATIC_REVIEW_FIELDS = frozenset({
    "@context", "@type", "itemReviewed", "productId", "productClass", "name", "author",
    "publisher", "datePublished", "reviewBody", "reviewRating", "positiveNotes",
    "negativeNotes", "image", "_source", "_mined",
})
USER_REVIEW_FIELDS = frozenset({"id", "user_id", "current_status", "_access"})


def static_subset(review: dict) -> dict:
    """A copy carrying only static/platonic fields — for cross-owner / product-attach flows.
    Mirrors recipe_model.static_subset."""
    return {k: v for k, v in (review or {}).items() if k in STATIC_REVIEW_FIELDS}
