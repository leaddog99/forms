"""Wall Street Journal (Buy Side) review decoder — STUB.

Detects the source; decoder not built yet. WSJ Buy Side roundups are editorial prose with
labeled "Pros/Cons" and a retail link block — likely a light regex + LLM-assisted extract.
"""
from __future__ import annotations

KEY = "wsj"
IMPLEMENTED = False
LABEL = "WSJ Buy Side"

# Per-source prompt fragment for the LLM extractor.
EXTRACT_HINTS = """SOURCE: WSJ Buy Side product roundup. Reviewer = "WSJ Buy Side".
- PRODUCTS carry an editorial label (e.g. "Best Overall", "Best Value", "Best Splurge") — use it as the
  tier — plus a "Pros"/"Cons" block and a verdict paragraph. Capture the verdict prose into verdict.summary.
- EDITORIAL HEADER: capture the intro + any "What to consider"/"How we chose" prose into
  product_class.buying_guide.
- BUY LINKS lead to retailers; capture retailer + a real /dp/ ASIN when present."""


def matches(url: str, head: str = "") -> bool:
    u = (url or "").lower() + " " + (head or "").lower()
    return "wsj.com" in u or "buyside" in u


def parse(md: str, *, url: str = "", captured_at: str = "") -> dict:
    raise NotImplementedError("WSJ Buy Side decoder not built yet.")