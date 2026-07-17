"""Back-compat shim — the per-source review decoders moved into the `review_sources` package.

Kept so the documented path (product_model.py / catalog_store.py reference "review_parsers.py")
and any external caller keep working. New code should import `intake.products.review_sources`.
"""
from __future__ import annotations

from intake.products.review_sources import (  # noqa: F401
    detect_source, parse_review, ingest_review, supported, SOURCES,
)
from intake.products.review_sources.atk import parse as parse_atk  # noqa: F401
from intake.products.review_sources.base import (  # noqa: F401
    amazon_asin as _amazon_asin, retailer as _retailer,
)