"""Listing identity — does an Amazon listing name THIS product?

One scorer for every path an ASIN can enter a pick (the research model's own
memory, a corpus buy link, a search result), and one candidate finder that
asks a web search instead of Amazon.

THE LOGIC LIVES IN THE SHARED PACKAGE `searchkit` (sibling repo, since
2026-11-21; it is also the basis of DPI's search for named products). This
module is the seam: BCC's names and row shapes on top of the package. A rule
change is made THERE, once. The package's `searchkit/identity.py` carries the
rules and the wrong listing that taught each one.

Why it exists (2026-09-07 audit, 223 live picks): every path checked brand plus
a coarse type noun and nothing else, so a same-brand SIBLING sailed through —
Pastene San Marzano DOP resolved to Pastene "Kitchen Ready" ground tomatoes,
ChefAlarm to the ThermoWorks DOT, a 2-piece carving set to a 6-piece BBQ set.
Amazon's own search was the fill of last resort and ranks a brand's bestseller,
not the named product; a web search scoped to amazon.com returned the right
listing for all six (rank 1 for four).

Verdicts: verified (>= VERIFIED) · weak (>= WEAK, listed with a warning) ·
reject. A blank ASIN is a correct answer; a guess is not.
"""
from __future__ import annotations

import re

import searchkit
from searchkit import identity as _kit

VERIFIED = searchkit.KITCHEN.verified
WEAK = searchkit.KITCHEN.weak

clean_model_number = _kit.clean_model_number

_ASIN_IN_URL = re.compile(r"/(?:dp|gp/product|clp|gp/aw/d)/([A-Z0-9]{10})(?:[/?#]|$)")


def identity_score(pick: dict, listing_title: str, listing_brand: str = "",
                   listing_model: str = "") -> dict:
    """-> {score, verdict, method, brand_ok, why}. `pick` carries manufacturer,
    product_title, capacity, model_number, product_class as the research model
    wrote them. Scored with the kitchen word lists (searchkit.KITCHEN)."""
    return _kit.identity_score(pick, listing_title, listing_brand, listing_model)


def asin_from_url(url: str) -> str:
    m = _ASIN_IN_URL.search(url or "")
    return m.group(1) if m else ""


def _search(query: str, *, pages: int = 1, want: int | None = None, **_ignored) -> list:
    """The package's finder, on OUR search chokepoint (provider setting, job
    cancel, page-loss alerts all apply)."""
    from input.pipeline.serp_search import serp_search
    return serp_search(query, pages=pages, want=want)


def google_candidates(pick: dict, *, retailer: str = "amazon.com", want: int = 10) -> list:
    """Web search, scoped to the retailer -> [{asin, title, slug, rank, link}] in
    result order. (The name is historical: it runs on whichever provider is
    active.) The ASIN comes free from the result URL; the result title is what
    the scorer reads. One search, two when the first finds too little. Empty on
    any failure — the caller leaves the ASIN blank, it never guesses."""
    store = searchkit.AMAZON if retailer == "amazon.com" else searchkit.AmazonStore(domain=retailer)
    try:
        cands = searchkit.candidates(pick, store=store, want=want, search=_search)
    except Exception as e:
        if type(e).__name__ == "JobCancelled":
            raise
        print(f"[identity] listing search failed for {pick.get('product_title')!r}: {e}")
        return []
    return [{**c, "asin": c["id"]} for c in cands]


def best_candidate(pick: dict, candidates: list) -> tuple:
    """Score every candidate — its result title AND its URL slug, best of the
    two — -> (candidate, score_info) for the best one that is not a reject,
    else (None, None). Ties go to the earlier rank."""
    return searchkit.best_candidate(pick, candidates)
