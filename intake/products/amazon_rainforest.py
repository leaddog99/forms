"""Amazon owner ratings + reviews via the Rainforest API.

The STRUCTURED half of a product's evidence. Expert reviews are prose we distill; owner
sentiment is arithmetic — an average, a count, and a 5/4/3/2/1 histogram — and arithmetic
should never be asked of a language model. Rainforest returns those fields directly, so we
read them and compute the score ourselves.

Why this exists: RealRank's index needs the histogram, and web search reliably fails to find
it — Amazon splits ratings across colour/size ASINs and renders the breakdown in a widget.
Two runs of `realrank_research` (KitchenAid, Lodge) came back "histogram not found — score
pending feed" for exactly that reason. This is the feed.

Cost: 2 credits per product (one `search` to resolve the ASIN, one `product` for the
breakdown). Pass a known `asin` to skip the search and halve it.

Key: RAINFOREST_KEY in the repo-root .env.
"""
from __future__ import annotations

import os

import requests

ENDPOINT = "https://api.rainforestapi.com/request"
DEFAULT_DOMAIN = "amazon.com"
TIMEOUT = 90

# Rainforest's key names for the histogram, in 5..1 order (the order realrank_index expects).
_STAR_KEYS = ("five_star", "four_star", "three_star", "two_star", "one_star")


def _key() -> str:
    k = os.environ.get("RAINFOREST_KEY", "").strip()
    if not k:
        raise RuntimeError("RAINFOREST_KEY not set (repo-root .env)")
    return k


def _get(params: dict) -> dict:
    r = requests.get(ENDPOINT, params={"api_key": _key(), **params}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def find_asin(product: str, *, domain: str = DEFAULT_DOMAIN,
              brand: str = "") -> dict | None:
    """Best-matching Amazon listing for a product name.

    Among listings that are PLAUSIBLY THE SAME PRODUCT, pick the one with the most ratings:
    Amazon's first result is often a newer or sponsored variant with a handful of reviews
    while the canonical listing carries the deep rating history we want to score (Lodge 12":
    the #1 hit had 21k ratings, the real listing had 145k).

    "Plausibly the same product" is the part that bit us. Ranking on ratings alone picked
    **Amazon Basics (52,003 ratings) for a Le Creuset query** and scored a $60 pot as if it
    were the $400 one (job #618). A search returns COMPETITORS, not just variants, and the
    most-reviewed competitor is usually the cheap one. So the brand must match: we take the
    brand from the caller or the leading word(s) of the query, and only consider hits whose
    brand or title carries it. No brand match => None, and the caller reports the score as
    unavailable rather than borrowing a rival's ratings.
    """
    d = _get({"type": "search", "amazon_domain": domain, "search_term": product})
    hits = [h for h in (d.get("search_results") or []) if h.get("asin")]
    if not hits:
        return None

    # Prefer the brand the CALLER knows (it comes off the catalog row). Deriving one from
    # the query is a last resort and must not use a single leading word: "Le Creuset ..."
    # yields "Le", and "le" is a substring of "Enameled" — which matched Amazon Basics and
    # is precisely how this went wrong. Require a token with enough substance to mean
    # something.
    want = (brand or "").strip().lower()
    if not want:
        head = product.split(",")[0].split()
        want = next((w.lower() for w in head if len(w) >= 4), "")
    if want:
        matched = [h for h in hits
                   if want in (h.get("brand") or "").lower()
                   or (h.get("title") or "").lower().startswith(want)]
        if not matched:
            print(f"[rainforest] no listing matching brand {want!r} for {product!r} — "
                  f"refusing to score a different manufacturer's product")
            return None
        hits = matched

    best = max(hits, key=lambda h: (h.get("ratings_total") or 0))
    return {"asin": best["asin"], "title": best.get("title", ""),
            "brand": best.get("brand", ""),
            "rating": best.get("rating"), "ratings_total": best.get("ratings_total"),
            "link": best.get("link", "")}


def product_ratings(asin: str, *, domain: str = DEFAULT_DOMAIN,
                    max_reviews: int = 8) -> dict:
    """Rating, total, 5..1 histogram and top review bodies for one ASIN.

    `histogram` is a list of COUNTS in 5..1 order — feed it straight to realrank_index.
    Empty list when Amazon didn't render a breakdown (rare, but then we score nothing
    rather than inventing a distribution).
    """
    d = _get({"type": "product", "amazon_domain": domain, "asin": asin})
    p = d.get("product") or {}
    bd = p.get("rating_breakdown") or {}
    hist = [int((bd.get(k) or {}).get("count") or 0) for k in _STAR_KEYS] if bd else []
    reviews = [{"title": (r.get("title") or "").strip(),
                "rating": r.get("rating"),
                "body": (r.get("body") or "").strip()[:1200]}
               for r in (p.get("top_reviews") or [])[:max_reviews]]
    bb = p.get("buybox_winner") or {}
    price = (bb.get("price") or {}) if isinstance(bb.get("price"), dict) else {}
    return {
        "asin": asin,
        "title": p.get("title", ""),
        "link": p.get("link", ""),
        "brand": p.get("brand", ""),
        # For the posting card: the listing photo and the current asking price.
        "image": (p.get("main_image") or {}).get("link", ""),
        # Manufacturer's model/part number when the listing states it — the
        # only unambiguous identity inside a brand's sibling line (KHM7210
        # vs the other KitchenAid 7-speeds, 2026-08-31).
        "model_number": p.get("model_number") or "",
        "price": price.get("raw") or "",
        "rating": p.get("rating"),
        "ratings_total": p.get("ratings_total"),
        "histogram": hist,                       # counts, 5..1
        "distribution_pct": {str(5 - i): (bd.get(k) or {}).get("percentage")
                             for i, k in enumerate(_STAR_KEYS)} if bd else None,
        "top_reviews": reviews,
    }


def variant_asins(asin: str, *, domain: str = DEFAULT_DOMAIN) -> dict:
    """Every ASIN in this product's variation FAMILY — all colours, all sizes.

    The identity set. A review links whichever variant its author happened to test (the White
    4.5qt, say) while our catalog row is the Cerise 5.5qt; matching on one ASIN misses. Hold
    the whole family and ANY reviewer link that lands inside it confirms the review is about
    OUR product — a factual test rather than a guess from names.

    It also rejects near-misses for free: a Le Creuset Signature Round BREAD oven sits under a
    different parent, so it simply isn't in the set, no vocabulary of type nouns required.

    Returns {asin, parent, family: set, titles: {asin: title}} — `family` always includes the
    ASIN itself so callers can use it unconditionally.
    """
    out = {"asin": asin, "parent": "", "family": {asin}, "titles": {}}
    try:
        d = _get({"type": "product", "amazon_domain": domain, "asin": asin})
    except Exception as e:
        print(f"[rainforest] variant lookup failed for {asin}: {e}")
        return out
    p = (d.get("product") or {})
    out["parent"] = p.get("parent_asin") or ""
    if out["parent"]:
        out["family"].add(out["parent"])
    for v in (p.get("variants") or []):
        a = (v.get("asin") or "").strip().upper()
        if a:
            out["family"].add(a)
            if v.get("title"):
                out["titles"][a] = v["title"]
    return out


def owner_sentiment(product: str, *, asin: str = "", domain: str = DEFAULT_DOMAIN,
                    brand: str = "") -> dict | None:
    """One call-site for "what do owners actually rate and say" — resolve the ASIN if
    needed, then pull the ratings.

    Deliberately does NOT score: this module reports what Amazon says, and whoever owns the
    scoring model (RealRank's `realrank_index`) applies it to `histogram` + `ratings_total`.
    Keeping the data layer free of the scoring layer means a change to the index doesn't
    touch the fetcher, and this stays reusable for the catalog.

    Returns None when the product can't be found on Amazon at all; the caller should then
    report the score as unavailable rather than guessing a distribution.
    """
    hit = {"asin": asin} if asin else find_asin(product, domain=domain, brand=brand)
    if not hit:
        return None
    return product_ratings(hit["asin"], domain=domain)
