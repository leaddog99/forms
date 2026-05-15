# URL-keyed metadata: Moz scoring + first-seen / last-accessed tracking.
# Backed by the `metabase_url` SQLite table. URLs are normalized before any
# read or write so the table key stays canonical.

import base64
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv

from input.pipeline.url_utils import normalize_url, root_domain

load_dotenv()
logger = logging.getLogger("pipeline.url_scoring")

MOZ_ACCESS_ID = os.getenv("MOZ_ACCESS_ID")
MOZ_SECRET_KEY = os.getenv("MOZ_SECRET_KEY")
MOZ_API_URL = "https://lsapi.seomoz.com/v2/url_metrics"
MOZ_TIMEOUT_SECONDS = 8


def _compute_ou(pa: float, da: float) -> Optional[float]:
    """Opportunity score: derived from Moz PA and DA. Lifted from the batch
    pipeline so scores stay comparable across batch and interactive flows."""
    try:
        return round(-3.0273 * (da ** 0.6034) + pa, 3)
    except Exception:
        return None


def score_url_via_moz(url: str) -> Optional[dict]:
    """Call the Moz URL Metrics API for a single URL. Returns None on any
    failure (missing creds, network, non-200). Never raises."""
    if not url:
        return None
    if not MOZ_ACCESS_ID or not MOZ_SECRET_KEY:
        logger.info("Moz creds missing — skipping scoring for %s", url)
        return None

    auth = base64.b64encode(f"{MOZ_ACCESS_ID}:{MOZ_SECRET_KEY}".encode()).decode()
    try:
        resp = requests.post(
            MOZ_API_URL,
            headers={"Authorization": "Basic " + auth},
            json={"targets": [url], "metrics": ["title", "page_authority", "domain_authority"]},
            timeout=MOZ_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        result = (resp.json().get("results") or [{}])[0]
    except Exception as e:
        logger.warning("Moz scoring failed for %s: %s", url, e)
        return None

    pa = float(result.get("page_authority") or 0)
    da = float(result.get("domain_authority") or 0)
    return {
        "page_authority": pa,
        "domain_authority": da,
        "ou_score": _compute_ou(pa, da),
        "raw_title": result.get("title") or "",
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


def get_or_create_url_metadata(
    conn: sqlite3.Connection,
    url: str,
    fallback_title: str = "",
    score_if_new: bool = True,
) -> Optional[dict]:
    """
    Ensure a metabase_url row exists for `url`. If new, score via Moz when
    creds are available and `score_if_new` is true. Always bumps last_accessed.
    Returns the row as a dict (normalized URL form).
    """
    norm = normalize_url(url)
    if not norm:
        return None

    ensure_metabase_url_table(conn)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,)).fetchone()
    if existing:
        conn.execute("UPDATE metabase_url SET last_accessed = ? WHERE url = ?", (now, norm))
        conn.commit()
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
                    now,
                    norm,
                ),
            )
            conn.commit()

    return _row_to_dict(conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,)).fetchone())
