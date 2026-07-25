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


def find_asin(product: str, *, domain: str = DEFAULT_DOMAIN) -> dict | None:
    """Best-matching Amazon listing for a product name.

    Picks the hit with the MOST ratings rather than the top-ranked one: Amazon's first
    result is often a newer or sponsored variant with a handful of reviews, while the
    canonical listing carries the deep rating history we actually want to score
    (Lodge 12": the #1 hit had 21k ratings, the real classic listing had 145k).
    """
    d = _get({"type": "search", "amazon_domain": domain, "search_term": product})
    hits = [h for h in (d.get("search_results") or []) if h.get("asin")]
    if not hits:
        return None
    best = max(hits, key=lambda h: (h.get("ratings_total") or 0))
    return {"asin": best["asin"], "title": best.get("title", ""),
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
    return {
        "asin": asin,
        "title": p.get("title", ""),
        "link": p.get("link", ""),
        "rating": p.get("rating"),
        "ratings_total": p.get("ratings_total"),
        "histogram": hist,                       # counts, 5..1
        "distribution_pct": {str(5 - i): (bd.get(k) or {}).get("percentage")
                             for i, k in enumerate(_STAR_KEYS)} if bd else None,
        "top_reviews": reviews,
    }


def owner_sentiment(product: str, *, asin: str = "", domain: str = DEFAULT_DOMAIN) -> dict | None:
    """One call-site for "what do owners actually rate and say" — resolve the ASIN if
    needed, then pull the ratings.

    Deliberately does NOT score: this module reports what Amazon says, and whoever owns the
    scoring model (RealRank's `realrank_index`) applies it to `histogram` + `ratings_total`.
    Keeping the data layer free of the scoring layer means a change to the index doesn't
    touch the fetcher, and this stays reusable for the catalog.

    Returns None when the product can't be found on Amazon at all; the caller should then
    report the score as unavailable rather than guessing a distribution.
    """
    hit = {"asin": asin} if asin else find_asin(product, domain=domain)
    if not hit:
        return None
    return product_ratings(hit["asin"], domain=domain)
