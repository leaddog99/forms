"""serp_search.py — the single, provider-agnostic Google SERP fetch chokepoint.

One canonical fetcher behind a config-selected provider, so we can A/B vendors and
switch without touching callers ([[single-path]]). Default = **SerpApi**, byte-for-
byte the current behavior. **Scale SERP** (Traject Data) is staged for the planned
switch — cheaper at our future scale + vendor consolidation with Rainforest (the
product-data source). Flip the `serp_provider` setting to "scaleserp" only AFTER a
fidelity A/B passes (our rankings trust Google's exact result set + order — the bar
DataForSEO failed). See memory project_serp_provider; A/B harness: scripts/serp_ab.py.

Contract: callers pass a VERBATIM `q` (we never construct the query) + how many
10-result pages deep. Returns normalized [{link, title, rank}] aggregated across
pages, deduped, in rank order. Empty list if the active provider's key is absent
(every caller already no-ops on []). Mirrors the prior _serp_links behavior:
filter=0 (no Google similar-page collapsing), optional early-stop at `want`.
"""
from __future__ import annotations

import os
import requests

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SCALESERP_ENDPOINT = "https://api.scaleserp.com/search"
_PAGE_SIZE = 10  # Google organic results per page (protocol constant)

_SCALESERP_ALIASES = {"scaleserp", "scale_serp", "scale-serp", "scale"}


def _scaleserp_key() -> str | None:
    """Scale SERP key, accepting the common env-var spellings."""
    return (os.getenv("SCALESERP_KEY") or os.getenv("SCALE_SERP_API_KEY")
            or os.getenv("SCALESERP_API_KEY"))


def active_provider() -> str:
    """Config-selected provider (default serpapi). system_config is optional —
    fall back to the SERP_PROVIDER env var, then serpapi."""
    try:
        from input.pipeline import system_config
        return (system_config.get_setting("serp_provider", "") or
                os.getenv("SERP_PROVIDER") or "serpapi").lower()
    except Exception:
        return (os.getenv("SERP_PROVIDER") or "serpapi").lower()


def has_key(provider: str | None = None) -> bool:
    prov = (provider or active_provider()).lower()
    if prov in _SCALESERP_ALIASES:
        return bool(_scaleserp_key())
    return bool(os.getenv("SERPAPI_KEY"))


def serp_search(query: str, pages: int = 7, *, want: int | None = None,
                gl: str | None = None, hl: str | None = None,
                provider: str | None = None, timeout: int = 30) -> list[dict]:
    """Verbatim Google search. `pages` = how many 10-result pages to fetch; `want`
    early-stops once that many results are collected (truncates). `provider` forces
    a specific vendor (for the A/B); otherwise the configured one. Returns
    [{link, title, rank}] deduped in rank order."""
    prov = (provider or active_provider()).lower()
    if prov in _SCALESERP_ALIASES:
        return _scaleserp(query, pages, want, gl, hl, timeout)
    return _serpapi(query, pages, want, gl, hl, timeout)


def serp_image_search(query: str, *, want: int = 5, gl: str | None = None,
                      hl: str | None = None, provider: str | None = None,
                      timeout: int = 30) -> list[dict]:
    """Google Images via the active SERP vendor — FETCH-FREE, the vendor absorbs
    the target site's anti-bot. For thumbnailing recipes on sites we can't fetch
    (thekitchn etc.): the og:image direct fetch is blocked, but Google already has
    the image. Returns up to `want` normalized
    [{image, thumbnail, title, source_link, source_domain, rank}] in result order.
    1 credit/call. Empty list if the active provider's key is absent."""
    prov = (provider or active_provider()).lower()
    if prov in _SCALESERP_ALIASES:
        return _scaleserp_images(query, want, gl, hl, timeout)
    return _serpapi_images(query, want, gl, hl, timeout)


def _img_host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").replace("www.", "")
    except Exception:
        return ""


def _serpapi_images(query, want, gl, hl, timeout) -> list[dict]:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return []
    params = {"engine": "google_images", "q": query, "api_key": key}
    if gl:
        params["gl"] = gl
    if hl:
        params["hl"] = hl
    try:
        res = requests.get(SERPAPI_ENDPOINT, params=params, timeout=timeout).json()
        imgs = res.get("images_results") or []
    except Exception as e:
        print(f"  [serpapi-images] failed: {type(e).__name__}: {e}")
        return []
    out = []
    for it in imgs[:want]:
        image = it.get("original") or it.get("thumbnail") or ""
        if not image:
            continue
        link = it.get("link") or ""
        out.append({"image": image, "thumbnail": it.get("thumbnail") or image,
                    "title": it.get("title") or "", "source_link": link,
                    "source_domain": (it.get("source") or _img_host(link)),
                    "rank": len(out) + 1})
    return out


def _scaleserp_images(query, want, gl, hl, timeout) -> list[dict]:
    key = _scaleserp_key()
    if not key:
        return []
    params = {"api_key": key, "q": query, "output": "json", "search_type": "images"}
    if gl:
        params["gl"] = gl
    if hl:
        params["hl"] = hl
    try:
        data = requests.get(SCALESERP_ENDPOINT, params=params, timeout=timeout).json()
        imgs = data.get("image_results") or []
    except Exception as e:
        print(f"  [scaleserp-images] failed: {type(e).__name__}: {e}")
        return []
    if not imgs:
        ri = data.get("request_info") or {}
        if not ri.get("success"):
            print(f"  [scaleserp-images] {ri.get('message') or data.get('error')}")
        return []
    out = []
    for it in imgs[:want]:
        image = it.get("image") or it.get("original") or ""
        if not image:
            continue
        link = it.get("link") or it.get("source") or ""
        out.append({"image": image, "thumbnail": it.get("thumbnail") or image,
                    "title": it.get("title") or "", "source_link": link,
                    "source_domain": (it.get("domain") or _img_host(link)),
                    "rank": len(out) + 1})
    return out


def _serpapi(query, pages, want, gl, hl, timeout) -> list[dict]:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for p in range(max(1, pages)):
        params = {"engine": "google", "q": query, "num": _PAGE_SIZE,
                  "start": p * _PAGE_SIZE, "filter": 0, "api_key": key}
        if gl:
            params["gl"] = gl
        if hl:
            params["hl"] = hl
        try:
            org = requests.get(SERPAPI_ENDPOINT, params=params, timeout=timeout).json().get("organic_results") or []
        except Exception:
            break
        for it in org:
            link = it.get("link") or ""
            if link and link not in seen:
                seen.add(link)
                out.append({"link": link, "title": it.get("title") or "", "rank": len(out) + 1})
        if not org or (want is not None and len(out) >= want):
            break
    return out[:want] if want is not None else out


def _scaleserp(query, pages, want, gl, hl, timeout) -> list[dict]:
    """Scale SERP (Traject Data). LOOP the 1-based `page` param (like SerpApi's
    `start`), one page per request, with early-stop at `want`. We don't use
    `max_page` — it's hard-capped at 10 (400 above) AND a 10-page server-side fetch
    risks the request timeout (the silent-0 bug). Page-looping has no depth cap, is
    uniform with the SerpApi path, and early-stops cheaply. Same verbatim `q`;
    response: organic_results[] with link/title/position. 1 credit per page."""
    key = _scaleserp_key()
    if not key:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for p in range(1, max(1, pages) + 1):
        params = {"api_key": key, "q": query, "output": "json", "page": p}
        if gl:
            params["gl"] = gl
        if hl:
            params["hl"] = hl
        try:
            data = requests.get(SCALESERP_ENDPOINT, params=params, timeout=timeout).json()
            org = data.get("organic_results") or []
        except Exception as e:
            print(f"  [scaleserp] page {p} failed: {type(e).__name__}: {e}")
            break
        if not org:
            ri = data.get("request_info") or {}
            if not ri.get("success"):  # surface a real error (e.g. out of credits), not a silent 0
                print(f"  [scaleserp] page {p}: {ri.get('message') or data.get('error')}")
            break
        for it in org:
            link = it.get("link") or it.get("url") or ""
            if link and link not in seen:
                seen.add(link)
                out.append({"link": link, "title": it.get("title") or "", "rank": len(out) + 1})
        if want is not None and len(out) >= want:
            break
    return out[:want] if want is not None else out
