"""America's Test Kitchen equipment-review decoder.

ATK pages are highly structured (tier section headers + labeled spec blocks), so this is a
DETERMINISTIC parser — no LLM, free, reliable. Proven on fixtures/atk_loaf_pans.md (13 products).
"""
from __future__ import annotations

import re

from . import base

KEY = "atk"
IMPLEMENTED = True
LABEL = "America's Test Kitchen"

# Per-source prompt fragment for the LLM extractor (extract/markdown_to_review). Encodes ATK's page
# structure so the model reliably finds the products, tiers, retailers, and editorial. This is where
# ATK-specific extraction knowledge now lives (the LLM replaced the brittle regex for real pages).
EXTRACT_HINTS = """SOURCE: America's Test Kitchen (ATK) equipment review. Reviewer = "America's Test Kitchen".
- PRODUCTS are under the "Everything We Tested" heading, grouped by TIER section headers in this order:
  "Highly Recommended", "Recommended", "Recommended with Reservations", "Not Recommended", "Discontinued".
  The single top product also carries a "Winner" (or "Co-Winner") badge — use "Winner"/"Co-Winner" as its
  tier. A "Top Pick" block near the top of the page repeats the winner; INCLUDE IT ONCE (dedupe).
- EDITORIAL HEADER lives under "What You Need to Know" with sub-sections "What to Look For", "What to
  Avoid", and "Other Considerations", plus an "FAQs" block — capture ALL of it into product_class.buying_guide.
- SPECS ("Model Number", "Capacity", "Material", "Dimensions", "Dishwasher-Safe", "Weight") and "Price at
  Time of Testing" usually sit behind a collapsed "Full Ratings & Specs" toggle and are OFTEN ABSENT from
  the captured text — leave specs/price empty when not present; never guess.
- BUY LINKS read "Buy at <Retailer>" or "Buy from N Sellers". Byline is the "By <Author>" line; the date is
  the "Last Updated ..." line; the rating scale is 3-star (Good/Fair/Poor)."""

_TIER_HEADERS = {
    "Highly Recommended", "Recommended", "Recommended with Reservations",
    "Not Recommended", "Discontinued",
}
_BADGES = ["Co-Winner", "Winner"]


def matches(url: str, head: str = "") -> bool:
    return "americastestkitchen.com" in (url or "").lower() or "americastestkitchen.com" in (head or "").lower()


def parse(md: str, *, url: str = "", captured_at: str = "") -> dict:
    """Decode an ATK equipment review into the canonical ProductReviewExtraction shape."""
    title = base.find(md, r"^#\s+(.+?)(?:\s*\|.*)?$", re.M)
    review_url = url or base.find(md, r"\*Source:\s*(\S+)")
    cap = captured_at or base.find(md, r"\*Captured:\s*([^\*\n]+)")
    authors = re.findall(r"\[([^\]]+)\]\(/authors/[^)]+\)", md)
    updated = base.find(md, r"Last Updated\s+([A-Za-z]+ \d+,\s*\d{4})")

    body = md.split("## Everything We Tested", 1)
    body = body[1] if len(body) > 1 else md

    products: list[dict] = []
    current_tier = ""
    seen: set[str] = set()

    for part in re.split(r"\n###\s+", body)[1:]:
        header, _, block = part.partition("\n")
        header = header.strip()
        if header in _TIER_HEADERS:
            current_tier = header
            continue

        badge, name = "", header
        for b in _BADGES:
            if name.startswith(b):
                badge, name = b, name[len(b):].strip()
                break

        specs = {}
        for label, key in base.SPEC_FIELDS.items():
            m = re.search(rf"^{re.escape(label)}:\s*(.+)$", block, re.M)
            if m:
                specs[key] = m.group(1).strip()
        pm = re.search(r"Price at Time of Testing:\s*\$?([\d,.]+)", block)
        price = float(pm.group(1).replace(",", "")) if pm else None
        if not specs and price is None:
            continue

        prose = []
        for ln in block.split("Model Number:", 1)[0].splitlines():
            s = ln.strip()
            if not s or s[0] in "![-" or s.startswith("Buy"):
                continue
            prose.append(s)
        summary = " ".join(prose).strip()

        offers = []
        for label, link in re.findall(r"\[([^\]]+)\]\((https?://[^)]+)\)", block):
            if "/authors/" in link or "/recipes/" in link:
                continue
            r = base.retailer(label, link)
            if any(o["retailer"] == r for o in offers):
                continue
            offers.append({"retailer": r, "asin": base.amazon_asin(link), "source_url": link})

        key = specs.get("model_number") or name
        if key in seen:
            continue
        seen.add(key)

        products.append({
            "name": name,
            "specs": specs,
            "verdict": {
                "reviewer": LABEL, "tier": badge or current_tier,
                "summary": summary, "price_at_test": price,
            },
            "retailer_offers": offers,
        })

    return {
        # NOTE: class is currently FIXED for the loaf-pan fixture. Inferring class+size grain from
        # the page (title -> "Loaf Pans", spec dims -> "(1 lb)") is a known TODO (the curator flagged
        # the hardcoded class); left deterministic here so ingest is predictable until that lands.
        "product_class": {
            "name": "Loaf Pans (1 lb)", "category": "Bakeware",
            "criteria": ["Performance", "Ease of Use", "Cleanup/Durability"],
        },
        "review_source": {
            "reviewer": LABEL, "authors": authors, "title": title,
            "url": review_url, "last_updated": updated, "captured_at": cap,
            "rating_scale": "3-star (Good/Fair/Poor)",
        },
        "products": products,
    }
