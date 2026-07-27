"""EasyParser — Amazon SEARCH + DETAIL, the cheap replacement for Rainforest.

One HTTP endpoint, one credit per call. `operation=SEARCH` turns a collection (a saved
Amazon search URL) into a page of ASINs with ratings; `operation=DETAIL` returns everything
about one ASIN — images, price, specs, review bodies.

Two things learned by testing it live (2026-07-26), both baked in here:

**SEARCH takes no url.** It wants `keyword` / `refinements` / `sort_by` / pages. But Amazon
already encodes exactly those in its own query string, so `params_from_url()` translates at
call time and THE CURATOR'S SAVED URL REMAINS THE ARTIFACT — a parser improvement then
applies retroactively to every stored collection, which storing decomposed params would not.

**DETAIL's `rating_breakdown` comes back ALL ZEROS** — verified on two ASINs whose `rating`
and `ratings_total` were exactly right. Zeros are the dangerous shape: they read as data
rather than absence. So `detail()` treats an all-zero breakdown as MISSING and says so, and
the histogram is sourced from `amazon_widget` instead. The parsing here is kept and wired
because EasyParser has been asked to fix it — when they do, it lights up with no code
change.

Key: `EASYPARSER` in the repo-root .env.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse, parse_qs, unquote

import requests

ENDPOINT = "https://realtime.easyparser.com/v1/request"
TIMEOUT = 150

# EasyParser's documented sort vocabulary — Amazon uses the same tokens in `s=`.
SORTS = {"featured", "price-asc-rank", "price-desc-rank", "review-rank",
         "date-desc-rank", "relevanceblender", "exact-aware-popularity-rank"}

_STAR_KEYS = ("five_star", "four_star", "three_star", "two_star", "one_star")


def _key() -> str:
    k = os.environ.get("EASYPARSER", "").strip()
    if not k:
        raise RuntimeError("EASYPARSER not set (repo-root .env)")
    return k


def _call(params: dict) -> dict:
    r = requests.get(ENDPOINT, params={"api_key": _key(), "output": "json",
                                       "platform": "AMZ", **params}, timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    info = d.get("request_info") or {}
    if not info.get("success", True):
        raise RuntimeError(f"easyparser error: {info.get('error_details') or info}")
    return d


def credits(response: dict) -> dict:
    """Credit accounting off any response — worth surfacing in job results, since a
    collection run is 1 credit per search page plus 1 per DETAIL."""
    i = response.get("request_info") or {}
    return {"used_this_request": i.get("credit_used_this_request"),
            "remaining": i.get("credits_remaining")}


# --------------------------------------------------------------------------- #
#  Collection URL -> SEARCH params
# --------------------------------------------------------------------------- #

def params_from_url(url: str, *, pages: int = 1, exclude_sponsored: bool = True) -> tuple:
    """(params, warnings) for a saved Amazon search URL.

        k     -> keyword
        rh    -> refinements   (URL-decoded; Amazon's "n:123,p_72:456" IS the format)
        s     -> sort_by       (same vocabulary)
        page  -> min_page / max_page
    """
    u = urlparse(url or "")
    q = parse_qs(u.query)
    warn = []
    if "/s" not in (u.path or ""):
        warn.append(f"path {u.path!r} is not an Amazon search path (/s) — is this a search URL?")

    keyword = (q.get("k") or q.get("keywords") or [""])[0]
    rh = unquote((q.get("rh") or [""])[0])
    sort = (q.get("s") or [""])[0]
    node = (q.get("node") or q.get("bbn") or [""])[0]
    page = int((q.get("page") or ["1"])[0] or 1)

    # A browse node given as node=/bbn= rather than inside rh still belongs in refinements.
    if node and f"n:{node}" not in rh:
        rh = f"n:{node}" + (f",{rh}" if rh else "")
    if not keyword and not rh:
        warn.append("no keyword and no refinements — EasyParser needs at least one")
    if sort and sort not in SORTS:
        warn.append(f"sort {sort!r} is not in EasyParser's documented vocabulary — verify it")

    host = (u.hostname or "www.amazon.com")
    params = {"operation": "SEARCH",
              "domain": "." + host.split("amazon.")[-1] if "amazon." in host else ".com",
              "min_page": page, "max_page": page + max(1, pages) - 1}
    if keyword:
        params["keyword"] = keyword
    if rh:
        params["refinements"] = rh
    if sort:
        params["sort_by"] = sort
    if exclude_sponsored:
        params["exclude_sponsored"] = "true"
    return params, warn


def _abs_link(link: str, asin: str, host: str) -> str:
    """A usable product URL.

    SEARCH returns links RELATIVE ("/Petromax-.../dp/B077QGHRLL/ref=sr_1_1?dib=..."), which
    a browser resolves against whatever page is displaying them — so they rendered as
    localhost links and 404'd. Fixed here rather than in the form, so every consumer gets a
    working URL from one place.

    When we have the ASIN we prefer the canonical /dp/<asin> form: the returned href carries
    a `ref=sr_1_N` search-position tag and a `dib=` blob that are noise, position-specific,
    and will not survive the next run — none of which belongs in a stored product link.
    """
    if asin:
        return f"{host}/dp/{asin}"
    link = (link or "").strip()
    if not link:
        return ""
    if link.startswith("http"):
        return link
    return host + ("" if link.startswith("/") else "/") + link


def search_url(url: str, *, pages: int = 1, exclude_sponsored: bool = True) -> dict:
    """Run a collection URL. Returns {ok, items, count, credits, warnings, params, error}.

    `items` are normalized to the few fields selection needs; the raw shape carries far
    more, but a stage-one screen should not depend on fields it doesn't use.
    """
    params, warn = params_from_url(url, pages=pages, exclude_sponsored=exclude_sponsored)
    if not params.get("keyword") and not params.get("refinements"):
        return {"ok": False, "error": "; ".join(warn) or "unusable URL", "warnings": warn}
    try:
        d = _call(params)
    except Exception as e:
        return {"ok": False, "error": str(e), "warnings": warn, "params": params}
    raw = (d.get("result") or {}).get("search_results") or []
    host = "https://www.amazon" + (params.get("domain") or ".com")
    items = [{
        "asin": p.get("asin"), "title": p.get("title", ""), "brand": p.get("brand", ""),
        "rating": p.get("rating"), "ratings_total": p.get("ratings_total"),
        "price": ((p.get("price") or {}) or {}).get("raw", ""),
        "image": p.get("image") or p.get("main_image", ""),
        "link": _abs_link(p.get("link"), p.get("asin"), host),
        "position": p.get("position"),
        "is_sponsored": bool(p.get("is_sponsored")),
        "categories": p.get("categories") or [],
    } for p in raw if p.get("asin")]
    return {"ok": True, "items": items, "count": len(items), "warnings": warn,
            "params": params, "credits": credits(d)}


# --------------------------------------------------------------------------- #
#  DETAIL
# --------------------------------------------------------------------------- #

def detail(asin: str, *, domain: str = ".com") -> dict:
    """Full product detail for one ASIN.

    `histogram` is [] and `histogram_missing` True whenever the breakdown is absent OR all
    zeros — see the module docstring. Never return zeros as though they were measurements.
    """
    try:
        d = _call({"operation": "DETAIL", "domain": domain, "asin": asin,
                   "include_html": "false"})
    except Exception as e:
        return {"ok": False, "asin": asin, "error": str(e)}
    p = (d.get("result") or {}).get("detail") or {}
    bd = p.get("rating_breakdown") or {}
    counts = [int((bd.get(k) or {}).get("count") or 0) for k in _STAR_KEYS]
    missing = not any(counts)
    bb = p.get("buybox_winner") or {}
    price = bb.get("price") if isinstance(bb.get("price"), dict) else {}
    return {
        "ok": True, "asin": asin, "error": "",
        "title": p.get("title", ""), "brand": p.get("brand", ""), "gtin": p.get("gtin", ""),
        "link": p.get("link", ""),
        "image": (p.get("main_image") or {}).get("link", ""),
        "images": [i.get("link") for i in (p.get("images") or []) if i.get("link")],
        "price": (price or {}).get("raw", ""),
        "rating": p.get("rating"),
        "ratings_total": p.get("ratings_total"),
        "histogram": [] if missing else counts,       # 5..1 counts
        "histogram_missing": missing,
        "top_reviews": [{"title": (r.get("title") or "").strip(),
                         "rating": r.get("rating"),
                         "body": (r.get("body") or "").strip()[:1200]}
                        for r in (p.get("top_reviews") or [])],
        "customer_say": p.get("customer_say") or {},
        "categories": p.get("categories") or [],
        "credits": credits(d),
    }
