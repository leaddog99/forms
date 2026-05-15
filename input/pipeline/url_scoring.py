# URL-keyed metadata: Moz scoring + first-seen / last-accessed tracking.
# Backed by the `metabase_url` SQLite table. URLs are normalized before any
# read or write so the table key stays canonical.

import base64
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv

from input.pipeline.url_utils import normalize_url, root_domain

load_dotenv()
logger = logging.getLogger("pipeline.url_scoring")

MOZ_ACCESS_ID = os.getenv("MOZ_ACCESS_ID")
MOZ_SECRET_KEY = os.getenv("MOZ_SECRET_KEY")
MOZ_API_URL = "https://lsapi.seomoz.com/v2/url_metrics"
MOZ_TIMEOUT_SECONDS = 8
# Default TTL for Moz scores. Save-time refresh kicks in when a metabase_url
# row is older than this (matches the CLI script's --days default so manual
# and interactive paths agree on what "stale" means).
MOZ_REFRESH_TTL_DAYS = 30


def _compute_ou(pa: float, da: float) -> Optional[float]:
    """Opportunity score: derived from Moz PA and DA. Lifted from the batch
    pipeline so scores stay comparable across batch and interactive flows."""
    try:
        return round(-3.0273 * (da ** 0.6034) + pa, 3)
    except Exception:
        return None


def _url_variants(url: str) -> list[str]:
    """Return [url, www-toggled variant]. Moz doesn't normalize — the
    non-www form returns only estimated PA, while the www form (the form
    most major sites canonicalize to) is the actually-crawled URL with
    real PA. Querying both lets us pick the crawled one at score time."""
    out = [url]
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            alt_host = host[4:]
        elif host:
            alt_host = "www." + host
        else:
            return out
        alt = urlunparse((p.scheme, alt_host, p.path, p.params, p.query, p.fragment))
        if alt != url:
            out.append(alt)
    except Exception:
        pass
    return out


def score_url_via_moz(url: str) -> Optional[dict]:
    """Call the Moz URL Metrics API for a single URL. Returns None on any
    failure (missing creds, network, non-200). Never raises.

    Internally queries both www and non-www variants in one batched call
    and returns the score for the variant Moz has actually crawled
    (http_code != 0). Falls back to the higher PA if neither is crawled.
    """
    if not url:
        return None
    if not MOZ_ACCESS_ID or not MOZ_SECRET_KEY:
        logger.info("Moz creds missing — skipping scoring for %s", url)
        return None

    candidates = _url_variants(url)
    auth = base64.b64encode(f"{MOZ_ACCESS_ID}:{MOZ_SECRET_KEY}".encode()).decode()
    try:
        resp = requests.post(
            MOZ_API_URL,
            headers={"Authorization": "Basic " + auth},
            json={"targets": candidates,
                  "metrics": ["title", "page_authority", "domain_authority", "http_code"]},
            timeout=MOZ_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except Exception as e:
        logger.warning("Moz scoring failed for %s: %s", url, e)
        return None

    if not results:
        return None

    # Prefer a variant Moz has actually crawled; if multiple, pick the
    # one with the highest PA (real measurement beats estimate).
    crawled = [r for r in results if r.get("http_code")]
    chosen = (max(crawled, key=lambda r: r.get("page_authority") or 0)
              if crawled else max(results, key=lambda r: r.get("page_authority") or 0))

    pa = float(chosen.get("page_authority") or 0)
    da = float(chosen.get("domain_authority") or 0)
    return {
        "page_authority": pa,
        "domain_authority": da,
        "ou_score": _compute_ou(pa, da),
        "raw_title": chosen.get("title") or "",
    }


def ensure_metabase_url_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metabase_url (
            url TEXT PRIMARY KEY,
            root_domain TEXT NOT NULL DEFAULT '',
            raw_title TEXT NOT NULL DEFAULT '',
            page_authority REAL,
            domain_authority REAL,
            ou_score REAL,
            moz_last_scored TEXT,
            first_seen TEXT NOT NULL,
            last_accessed TEXT NOT NULL
        )
        """
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "url": row["url"],
        "root_domain": row["root_domain"],
        "raw_title": row["raw_title"],
        "page_authority": row["page_authority"],
        "domain_authority": row["domain_authority"],
        "ou_score": row["ou_score"],
        "moz_last_scored": row["moz_last_scored"],
        "first_seen": row["first_seen"],
        "last_accessed": row["last_accessed"],
    }


def get_metabase_url(conn: sqlite3.Connection, url: str) -> Optional[dict]:
    """Look up a metabase row for `url` (normalized). Returns None if absent."""
    norm = normalize_url(url)
    if not norm:
        return None
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def _is_moz_stale(moz_last_scored: Optional[str], days: int) -> bool:
    """True if the row has never been scored or its score is older than `days`."""
    if not moz_last_scored:
        return True
    try:
        # SQLite stores ISO-8601 strings; fromisoformat handles "+00:00" etc.
        scored_at = datetime.fromisoformat(moz_last_scored)
    except Exception:
        return True
    if scored_at.tzinfo is None:
        scored_at = scored_at.replace(tzinfo=timezone.utc)
    return scored_at < (datetime.now(timezone.utc) - timedelta(days=days))


def _apply_moz_scores(conn: sqlite3.Connection, url: str, scores: dict, now_iso: str) -> None:
    """Write scores onto a metabase_url row. Used by both create-new and
    refresh-stale paths so the UPDATE shape stays in one place."""
    conn.execute(
        """
        UPDATE metabase_url SET
            page_authority = ?,
            domain_authority = ?,
            ou_score = ?,
            raw_title = CASE WHEN ? <> '' THEN ? ELSE raw_title END,
            moz_last_scored = ?
        WHERE url = ?
        """,
        (
            scores["page_authority"],
            scores["domain_authority"],
            scores["ou_score"],
            scores["raw_title"], scores["raw_title"],
            now_iso,
            url,
        ),
    )
    conn.commit()


def get_or_create_url_metadata(
    conn: sqlite3.Connection,
    url: str,
    fallback_title: str = "",
    score_if_new: bool = True,
    refresh_if_stale_days: int = MOZ_REFRESH_TTL_DAYS,
) -> Optional[dict]:
    """
    Ensure a metabase_url row exists for `url`. If new, score via Moz when
    creds are available and `score_if_new` is true. If existing and its
    moz_last_scored is older than `refresh_if_stale_days` (or null), re-score
    inline. Always bumps last_accessed. Returns the row as a dict.
    Pass `refresh_if_stale_days=0` to disable the staleness check.
    """
    norm = normalize_url(url)
    if not norm:
        return None

    ensure_metabase_url_table(conn)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,)).fetchone()
    if existing:
        # Bump last_accessed first so even if scoring fails the access bump lands.
        conn.execute("UPDATE metabase_url SET last_accessed = ? WHERE url = ?", (now, norm))
        conn.commit()

        if refresh_if_stale_days > 0 and _is_moz_stale(existing["moz_last_scored"], refresh_if_stale_days):
            scores = score_url_via_moz(norm)
            if scores:
                _apply_moz_scores(conn, norm, scores, now)
            # If scoring fails (creds missing, network), leave existing scores
            # intact — better stale than zeroed.
        return _row_to_dict(conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,)).fetchone())

    # New row. Insert with what we know now, then attempt Moz scoring inline.
    conn.execute(
        """
        INSERT INTO metabase_url (url, root_domain, raw_title, first_seen, last_accessed)
        VALUES (?, ?, ?, ?, ?)
        """,
        (norm, root_domain(norm), fallback_title or "", now, now),
    )
    conn.commit()

    if score_if_new:
        scores = score_url_via_moz(norm)
        if scores:
            _apply_moz_scores(conn, norm, scores, now)

    return _row_to_dict(conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,)).fetchone())
