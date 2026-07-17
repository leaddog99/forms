"""Wirecutter (NYT) review decoder — STUB.

Detects the source so the pipeline recognizes it, but the decoder isn't built yet. Wirecutter
prose is far less structured than ATK (narrative "our pick / runner-up / also great" + inline
spec callouts), so this one will likely need an LLM-assisted extract rather than pure regex.
"""
from __future__ import annotations

KEY = "wirecutter"
IMPLEMENTED = False
LABEL = "Wirecutter"

# Per-source prompt fragment for the LLM extractor. Wirecutter has no deterministic parser (prose is
# unstructured), but the LLM extracts it well with this guidance.
EXTRACT_HINTS = """SOURCE: Wirecutter (NYT) product review. Reviewer = "Wirecutter".
- PRODUCTS are labeled by ROLE rather than a tier ladder: "Our pick", "Runner-up", "Also great", "Budget
  pick", "Upgrade pick", "Best for <use>". Map that role into the tier field verbatim. Each pick's name is
  usually a heading; its verdict is the prose beneath it.
- EDITORIAL HEADER: capture the "Why you should trust us", "Who this is for", "How we picked", and "How we
  tested" sections (what to look for, tradeoffs, criteria) into product_class.buying_guide.
- BUY LINKS are per-retailer buttons; capture retailer + a real /dp/ ASIN when present."""


def matches(url: str, head: str = "") -> bool:
    u = (url or "").lower() + " " + (head or "").lower()
    return "wirecutter" in u or "nytimes.com/wirecutter" in u


def parse(md: str, *, url: str = "", captured_at: str = "") -> dict:
    raise NotImplementedError("Wirecutter decoder not built yet (LLM-assisted extract planned).")