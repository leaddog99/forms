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
        return bool(os.getenv("SCALE_SERP_API_KEY") or os.getenv("SCALESERP_API_KEY"))
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
    """Scale SERP (Traject Data). `max_page` server-side-paginates + concatenates in
    one request (1 credit/page). Same verbatim `q` passthrough; response shape:
    organic_results[] with link/title/position."""
    key = os.getenv("SCALE_SERP_API_KEY") or os.getenv("SCALESERP_API_KEY")
    if not key:
        return []
    params = {"api_key": key, "q": query, "output": "json", "max_page": max(1, pages)}
    if gl:
        params["gl"] = gl
    if hl:
        params["hl"] = hl
    try:
        org = requests.get(SCALESERP_ENDPOINT, params=params, timeout=timeout * 2).json().get("organic_results") or []
    except Exception:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for it in org:
        link = it.get("link") or it.get("url") or ""
        if link and link not in seen:
            seen.add(link)
            out.append({"link": link, "title": it.get("title") or "", "rank": len(out) + 1})
    return out[:want] if want is not None else out
