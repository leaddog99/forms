"""Shared helpers for the per-source review parsers.

Each source module (atk, wirecutter, williams_sonoma, wsj, …) is a dedicated DECODER — the review
analog of recipe standardization's per-source methods ([[feedback_single_path]]): review sites are
structured wildly differently, so each gets custom code that converts its page into ONE canonical
shape (`ProductReviewExtraction` — see product_model). The dispatcher in __init__ routes a captured
page to the right decoder by URL, then the ingest bridge writes the canonical result into the
two-table store (reviews + review_products, [[project_reviews_architecture]]).

Canonical shape a `parse()` returns:
    {
      "product_class":  {"name", "category", "criteria": [...]},
      "review_source":  {"reviewer", "authors": [...], "title", "url", "last_updated",
                         "captured_at", "rating_scale"},
      "products": [ {"name", "brand"?, "specs": {...}, "verdict": {"reviewer","tier","summary",
                    "price_at_test"}, "retailer_offers": [{"retailer","asin","source_url"}]} ],
    }
"""
from __future__ import annotations

import re

# ATK-style labeled spec block -> ProductSpecs field. Reused by any source that labels specs.
SPEC_FIELDS = {
    "Model Number": "model_number",
    "Capacity": "capacity",
    "Material": "material",
    "Dimensions": "dimensions_in",
    "Dishwasher-Safe": "dishwasher_safe",
    "Weight": "weight",
}

# Retailer canonicalization (label/URL -> a stable retailer name). Extend as sources add vendors.
_RETAILERS = [
    ("Amazon", ("amazon",)), ("Williams Sonoma", ("williams", "williams-sonoma")),
    ("Sur La Table", ("sur la table", "surlatable")), ("Walmart", ("walmart",)),
    ("Target", ("target",)), ("OXO", ("oxo",)), ("Cuisinart", ("cuisinart",)),
    ("Crate & Barrel", ("crateandbarrel", "crate")), ("Le Creuset", ("lecreuset", "le creuset")),
    ("Wayfair", ("wayfair",)), ("Best Buy", ("bestbuy", "best buy")),
]


def amazon_asin(url: str) -> str:
    m = re.search(r"/dp/([A-Z0-9]{10})", url or "")
    return m.group(1) if m else ""


def retailer(label: str, url: str) -> str:
    lab, u = (label or "").strip().lower(), (url or "").lower()
    for name, keys in _RETAILERS:
        if any(k in lab or k in u for k in keys):
            return name
    return (label or "").strip()


def find(md: str, pat: str, flags: int = 0, default: str = "") -> str:
    m = re.search(pat, md, flags)
    return m.group(1).strip() if m else default