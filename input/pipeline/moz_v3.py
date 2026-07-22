"""moz_v3.py — Moz API V3 (JSON-RPC) client.

The V3 API (https://api.moz.com/jsonrpc) is a superset of the v2 Links API and
exposes DOMAIN-level data the v2 url_metrics endpoint does NOT: Brand Authority,
ranking keywords (what a publisher is known/found for, with search volume + rank),
and referring-domain counts. We use it to enrich the `domains` master record.

Auth: V3 uses the `x-moz-token` HEADER (NOT `Authorization: Basic`). The token is
the base64 of `MOZ_ACCESS_ID:MOZ_SECRET_KEY` (same creds as v2 — no separate token
needed). Every request is JSON-RPC 2.0 with an `id` >= 24 chars and a `data.`-prefixed
method (`data.site.metrics.fetch`, etc.). Billed per ROW returned — the single-site
`*.metrics.*` / `brand.authority` calls are 1 row each; `ranking.keywords.list` is 1
row PER keyword (so keep `limit` small). See project_moz_scoring_cost.

Never raises — every function degrades to None/[] so a Moz hiccup can't break a refresh.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("pipeline.moz_v3")

MOZ_V3_URL = "https://api.moz.com/jsonrpc"
_TIMEOUT = 15
# JSON-RPC ids must be >= 24 chars; a fixed pad keeps every call valid.
_ID_PAD = "moz-v3-req-000000000000000000"


def _token() -> Optional[str]:
    aid, sec = os.getenv("MOZ_ACCESS_ID"), os.getenv("MOZ_SECRET_KEY")
    if not aid or not sec:
        return None
    return base64.b64encode(f"{aid}:{sec}".encode()).decode()


def available() -> bool:
    return bool(_token())


def _call(method: str, params: dict) -> Optional[dict]:
    """One JSON-RPC call. Returns the `result` object, or None on any error."""
    tok = _token()
    if not tok:
        logger.info("Moz V3 creds missing — skipping %s", method)
        return None
    body = {"jsonrpc": "2.0", "id": _ID_PAD, "method": method, "params": params}
    try:
        r = requests.post(MOZ_V3_URL, headers={"x-moz-token": tok,
                                               "Content-Type": "application/json"},
                          json=body, timeout=_TIMEOUT)
        j = r.json()
    except Exception as e:
        logger.warning("Moz V3 %s failed: %s", method, e)
        return None
    if "error" in j:
        logger.warning("Moz V3 %s error: %s", method, j["error"].get("message"))
        return None
    return j.get("result")


def _site_query(domain: str) -> dict:
    return {"data": {"site_query": {"query": domain, "scope": "domain"}}}


def brand_authority(domain: str) -> Optional[int]:
    """Brand Authority (0-100) — authority from BRANDED search demand, independent of
    the link graph. 1 Moz row. None if unavailable."""
    res = _call("data.site.metrics.brand.authority.fetch", _site_query(domain))
    if not res:
        return None
    ba = (res.get("site_metrics") or {}).get("brand_authority_score")
    return int(ba) if ba is not None else None


def site_metrics(domain: str) -> Optional[dict]:
    """Domain-level authority + link facts in ONE row: domain_authority, page_authority,
    spam_score, referring domains (root_domains_to_root_domain), inbound links
    (pages_to_root_domain), last_crawled. None if unavailable."""
    res = _call("data.site.metrics.fetch", _site_query(domain))
    if not res:
        return None
    m = res.get("site_metrics") or {}
    return {
        "domain_authority": m.get("domain_authority"),
        "page_authority": m.get("page_authority"),
        "spam_score": m.get("spam_score"),
        "referring_domains": m.get("root_domains_to_root_domain"),
        "inbound_links": m.get("pages_to_root_domain"),
        "outbound_domains": m.get("root_domains_from_root_domain"),
        "last_crawled": m.get("last_crawled"),
    }


def ranking_keywords(domain: str, *, limit: int = 10, locale: str = "en-US") -> list[dict]:
    """Top keywords the domain ranks for (what it's known/found for), each with
    search volume + rank position + difficulty. Billed 1 ROW PER KEYWORD — keep `limit`
    small. Returns [] if unavailable. Ordered by the API (roughly by relevance/volume)."""
    params = {"data": {
        "target_query": {"query": domain, "scope": "domain", "locale": locale},
        "serp_query": {"engine": "google", "locale": locale},
        "limit": max(1, int(limit)),
    }}
    res = _call("data.site.ranking.keywords.list", params)
    if not res:
        return []
    out = []
    for k in (res.get("ranking_keywords") or []):
        out.append({
            "keyword": k.get("keyword"),
            "volume": k.get("volume"),
            "rank": k.get("rank_position"),
            "difficulty": k.get("difficulty"),
            "ranking_page": k.get("ranking_page"),
        })
    return out


def quota_rows() -> Optional[dict]:
    """Account data-row quota for the current period. Free/introspective (no row cost).
    Returns {allotted, used, remaining, reset, overage} or None."""
    res = _call("quota.lookup", {"data": {"path": "api.limits.data.rows"}})
    q = (res or {}).get("quota") if res else None
    if not q:
        return None
    allotted = q.get("allotted")
    used = q.get("used")
    remaining = (allotted - used) if (allotted is not None and used is not None) else None
    return {"allotted": allotted, "used": used, "remaining": remaining,
            "reset": q.get("reset"), "overage": q.get("overage")}
