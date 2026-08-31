"""Shopify storefront detection — the affiliate-prospect classifier.

Curator policy (2026-08-31): Amazon leads every offer list (Prime), and the
best secondary prospects are retailers on affiliate-friendly platforms —
Walmart, Kohl's, and anything running Shopify. This module answers "is this
retailer host a Shopify store?" with one cheap GET.

THE TEST (measured 2026-08-31 on fromourplace/milkstreet/taza/mccormick —
all four hit): `GET https://<host>/products.json?limit=1` returns HTTP 200
with a `{"products": [...]}` body on a Shopify store and fails/404s
elsewhere. Response headers (x-shopid etc.) are NOT reliable through edge
caches; page markers (cdn.shopify.com, Shopify.theme) corroborate but the
JSON endpoint is the verdict. Bonus: that same endpoint is a structured
product feed — variants carry SKUs (manufacturer model numbers), prices and
images, no scraping.

Results persist in `retailer_hosts` (persist-derived: value + evidence +
checked_at). This table is the seed of the affiliate-programs design's hosts
ledger (docs/affiliate-programs-and-clicks.md).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Hosts that are their own thing — never probed, never prospects here.
_SKIP = ("amazon.", "walmart.", "kohls.", "target.", "wayfair.", "ebay.",
         "costco.", "homedepot.", "google.", "youtube.")


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retailer_hosts (
            host        TEXT PRIMARY KEY,   -- bare host, www stripped
            is_shopify  INTEGER,            -- 1 | 0 | NULL = probe failed
            evidence    TEXT,               -- what the probe saw
            checked_at  TEXT
        )""")
    conn.commit()


def host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def probe(host: str, *, timeout: int = 12) -> dict:
    """One live check. -> {is_shopify: 1|0|None, evidence: str}."""
    if not host or any(s in host for s in _SKIP):
        return {"is_shopify": 0, "evidence": "skipped (major retailer/platform)"}
    try:
        r = requests.get(f"https://{host}/products.json?limit=1",
                         headers={"User-Agent": _UA}, timeout=timeout,
                         allow_redirects=True)
        if r.status_code == 200:
            try:
                if isinstance(r.json().get("products"), list):
                    return {"is_shopify": 1, "evidence": "products.json live"}
            except Exception:
                pass
        # Fallback: homepage markers (some stores gate the JSON).
        h = requests.get(f"https://{host}/", headers={"User-Agent": _UA},
                         timeout=timeout, allow_redirects=True)
        body = h.text[:400_000]
        if "cdn.shopify.com" in body or "/cdn/shop/" in body or "Shopify.theme" in body:
            return {"is_shopify": 1, "evidence": "page markers (json gated)"}
        return {"is_shopify": 0, "evidence": f"products.json {r.status_code}, no markers"}
    except requests.RequestException as e:
        return {"is_shopify": None, "evidence": f"probe failed: {type(e).__name__}"}


def classify(conn: sqlite3.Connection, host: str, *, max_age_days: int = 90) -> dict:
    """Cached classification; re-probes when stale or previously failed."""
    ensure_table(conn)
    host = (host or "").lower().removeprefix("www.")
    if not host:
        return {"host": host, "is_shopify": None, "evidence": "no host"}
    row = conn.execute("SELECT is_shopify, evidence, checked_at FROM retailer_hosts "
                       "WHERE host = ?", (host,)).fetchone()
    now = datetime.now(timezone.utc)
    if row and row[0] is not None and row[2]:
        try:
            age = (now - datetime.fromisoformat(row[2])).days
            if age <= max_age_days:
                return {"host": host, "is_shopify": row[0], "evidence": row[1],
                        "cached": True}
        except ValueError:
            pass
    res = probe(host)
    conn.execute("INSERT INTO retailer_hosts(host, is_shopify, evidence, checked_at) "
                 "VALUES(?,?,?,?) ON CONFLICT(host) DO UPDATE SET "
                 "is_shopify=excluded.is_shopify, evidence=excluded.evidence, "
                 "checked_at=excluded.checked_at",
                 (host, res["is_shopify"], res["evidence"], now.isoformat()))
    conn.commit()
    return {"host": host, **res}
