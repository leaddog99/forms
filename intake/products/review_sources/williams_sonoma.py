"""Williams Sonoma "expert review / buying guide" decoder — STUB.

Detects the source; decoder not built yet. WS is also a RETAILER (its taxonomy is our category
spine — see ws_categories), so a WS review often doubles as a product page: the decoder will need
to separate editorial verdict from the sellable offer.
"""
from __future__ import annotations

KEY = "williams_sonoma"
IMPLEMENTED = False
LABEL = "Williams Sonoma"

# Per-source prompt fragment for the LLM extractor.
EXTRACT_HINTS = """SOURCE: Williams Sonoma expert review / buying guide. Reviewer = "Williams Sonoma".
- WS is ALSO the retailer, so buy links point to williams-sonoma.com — set retailer "Williams Sonoma"; a WS
  guide usually recommends a small number of its own products with editorial reasoning.
- EDITORIAL HEADER: capture any "How to choose", "What to look for", or buying-guide prose into
  product_class.buying_guide.
- TIER: WS guides rarely use a formal tier ladder; use the page's own emphasis ("our favorite", "best
  for...") as the tier, else leave it "" for the curator to rank."""


def matches(url: str, head: str = "") -> bool:
    return "williams-sonoma.com" in ((url or "").lower() + " " + (head or "").lower())


def parse(md: str, *, url: str = "", captured_at: str = "") -> dict:
    raise NotImplementedError("Williams Sonoma decoder not built yet.")