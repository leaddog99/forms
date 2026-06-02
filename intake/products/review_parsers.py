"""Source-specific decoders for product-review pages.

The bookmarklet captures a review page's markdown (+ screenshot) and tells the
server which site it came from; `detect_source()` routes to a per-source
parser. Review sites are structured wildly differently, so each gets custom
code rather than one generic extractor (ATK is highly structured → a
deterministic parser is reliable + free; less-structured sources can fall back
to an LLM extract later).

Each parser returns the shape in product_model.ProductReviewExtraction:
one review page → {product_class, review_source, products[]}. Multiple
sources' extractions for the same class are HOMOGENIZED downstream into one
BCC recommendation.
"""
from __future__ import annotations

import re

# ATK verdict tiers (section headers in "Everything We Tested").
_TIER_HEADERS = {
    "Highly Recommended", "Recommended", "Recommended with Reservations",
    "Not Recommended", "Discontinued",
}
# Per-product badges glued onto the name header ("### Co-WinnerUSA Pan…").
_BADGES = ["Co-Winner", "Winner"]

_SPEC_FIELDS = {
    "Model Number": "model_number",
    "Capacity": "capacity",
    "Material": "material",
    "Dimensions": "dimensions_in",
    "Dishwasher-Safe": "dishwasher_safe",
    "Weight": "weight",
}


def detect_source(url: str) -> str | None:
    """Map a review-page URL to a parser key. Extend as sources are added."""
    u = (url or "").lower()
    if "americastestkitchen.com" in u:
        return "atk"
    if "seriouseats.com" in u:
        return "seriouseats"
    if "wirecutter" in u or "nytimes.com/wirecutter" in u:
        return "wirecutter"
    return None


def _amazon_asin(url: str) -> str:
    m = re.search(r"/dp/([A-Z0-9]{10})", url)
    return m.group(1) if m else ""


def _retailer(label: str, url: str) -> str:
    lab, u = label.strip().lower(), url.lower()
    table = [
        ("Amazon", ("amazon",)), ("Williams Sonoma", ("williams", "williams-sonoma")),
        ("Sur La Table", ("sur la table", "surlatable")), ("Walmart", ("walmart",)),
        ("Target", ("target",)), ("OXO", ("oxo",)), ("Cuisinart", ("cuisinart",)),
        ("Crate & Barrel", ("crateandbarrel", "crate")),
    ]
    for name, keys in table:
        if any(k in lab or k in u for k in keys):
            return name
    return label.strip()


def parse_atk(md: str, *, url: str = "", captured_at: str = "") -> dict:
    """Decode an America's Test Kitchen equipment review into the
    product-extraction shape. Deterministic — keys on ATK's labeled spec
    blocks + tier headers, so no LLM call needed for this source."""
    def _find(pat, flags=0, default=""):
        m = re.search(pat, md, flags)
        return m.group(1).strip() if m else default

    title = _find(r"^#\s+(.+?)(?:\s*\|.*)?$", re.M)
    review_url = url or _find(r"\*Source:\s*(\S+)")
    cap = captured_at or _find(r"\*Captured:\s*([^\*\n]+)")
    authors = re.findall(r"\[([^\]]+)\]\(/authors/[^)]+\)", md)
    updated = _find(r"Last Updated\s+([A-Za-z]+ \d+,\s*\d{4})")

    # Restrict to the per-product section so the Top-Picks duplicates up top
    # don't double-count.
    body = md.split("## Everything We Tested", 1)
    body = body[1] if len(body) > 1 else md

    products: list[dict] = []
    current_tier = ""
    seen: set[str] = set()

    for part in re.split(r"\n###\s+", body)[1:]:
        header, _, block = part.partition("\n")
        header = header.strip()

        if header in _TIER_HEADERS:          # tier section marker, not a product
            current_tier = header
            continue

        badge, name = "", header
        for b in _BADGES:
            if name.startswith(b):
                badge, name = b, name[len(b):].strip()
                break

        specs = {}
        for label, key in _SPEC_FIELDS.items():
            m = re.search(rf"^{re.escape(label)}:\s*(.+)$", block, re.M)
            if m:
                specs[key] = m.group(1).strip()
        pm = re.search(r"Price at Time of Testing:\s*\$?([\d,.]+)", block)
        price = float(pm.group(1).replace(",", "")) if pm else None
        if not specs and price is None:      # header with no spec block → skip
            continue

        # Review prose = lines before the spec block, minus images/links/buy rows.
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
            r = _retailer(label, link)
            if any(o["retailer"] == r for o in offers):
                continue
            offers.append({"retailer": r, "asin": _amazon_asin(link), "source_url": link})

        key = specs.get("model_number") or name
        if key in seen:
            continue
        seen.add(key)

        products.append({
            "name": name,
            "specs": specs,
            "verdict": {
                "reviewer": "America's Test Kitchen",
                "tier": badge or current_tier,
                "summary": summary,
                "price_at_test": price,
            },
            "retailer_offers": offers,
        })

    return {
        "product_class": {
            "name": "Loaf Pans (1 lb)", "category": "Bakeware",
            "criteria": ["Performance", "Ease of Use", "Cleanup/Durability"],
        },
        "review_source": {
            "reviewer": "America's Test Kitchen", "authors": authors,
            "title": title, "url": review_url, "last_updated": updated, "captured_at": cap,
        },
        "products": products,
    }


_PARSERS = {"atk": parse_atk}


def parse_review(md: str, *, url: str = "", captured_at: str = "") -> dict | None:
    """Route to the right source parser by URL. Returns None if no parser."""
    key = detect_source(url) or detect_source(md[:400])
    fn = _PARSERS.get(key)
    return fn(md, url=url, captured_at=captured_at) if fn else None
