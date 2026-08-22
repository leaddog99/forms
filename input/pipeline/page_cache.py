"""Raw-page fetch cache for the publisher harvest.

The publisher harvest fetches each candidate page TWICE — once for the is-recipe
filter, once for the winner-extract — and re-fetches everything on every re-harvest.
On unblocker domains that's two paid credits per page, every run. This caches the
RAW fetched page (the expensive network step; the HTML->markdown conversion is cheap
CPU that re-runs from the cached page) so both phases — and re-harvests within the
TTL — reuse one fetch.

Design:
  - ONE chokepoint: `to_markdown.html_to_markdown.fetch_with_full_fallback` consults
    this cache, so the is-recipe filter, the JSON-LD-direct lane and the markdown
    conversion all share it.
  - OPT-IN via a contextvar (`enabled()`), default OFF — so only code that explicitly
    turns it on (the harvest handler) is affected; every other fetch path (dish batch,
    live extract) is byte-for-byte unchanged.
  - Stored in a SEPARATE git-ignored `page_cache.db` (like media.db) — gzip-compressed
    HTML never bloats recipes.db or the recipes.sql git dump. Fully regenerable, so
    "not in git" is safe.
  - Keyed by (url_normalized, variant) where variant = render-vs-static (render=True
    yields different HTML); the unblocker is just HOW we reach the page, not WHAT page,
    and we only ever cache a successful non-stub body, so it isn't part of the key.
  - TTL from system_config `page_cache_ttl_days` (default 5); a master `page_cache_enabled`
    switch (default on) can disable the whole feature without code.
"""
from __future__ import annotations

import contextvars
import gzip
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from input.pipeline.url_utils import normalize_url

# page_cache.db sits next to recipes.db at the project root (input/pipeline/ -> ../..).
_DB_PATH = str(Path(__file__).resolve().parents[2] / "page_cache.db")

# OFF by default. The harvest handler flips it on for the duration of a run via
# `with page_cache.enabled():`; asyncio.to_thread copies the context, so the
# filter + extract worker threads inherit it.
_ctx_enabled: contextvars.ContextVar = contextvars.ContextVar("page_cache_enabled", default=False)

# Never cache an absurdly large body (defensive; keeps the cache lean).
_MAX_CACHE_BYTES = 6_000_000


class enabled:
    """Context manager turning the page cache on for the enclosed work."""
    def __enter__(self):
        self._token = _ctx_enabled.set(True)
        return self

    def __exit__(self, *exc):
        _ctx_enabled.reset(self._token)
        return False


def is_enabled() -> bool:
    """True iff a caller turned the cache on AND the config master switch allows it."""
    if not _ctx_enabled.get():
        return False
    try:
        from input.pipeline import system_config as _cfg
        return bool(_cfg.get_setting("page_cache_enabled", True))
    except Exception:
        return True


def _ttl_days() -> float:
    try:
        from input.pipeline import system_config as _cfg
        return float(_cfg.get_setting("page_cache_ttl_days", 5))
    except Exception:
        return 5.0


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS page_fetch_cache (
               url_normalized TEXT NOT NULL,
               variant        TEXT NOT NULL,
               status_code    INTEGER,
               final_url      TEXT,
               headers_json   TEXT,
               encoding       TEXT,
               source         TEXT,
               html_gz        BLOB NOT NULL,
               created_at     TEXT NOT NULL,
               last_used_at   TEXT,
               hit_count      INTEGER NOT NULL DEFAULT 0,
               PRIMARY KEY (url_normalized, variant)
           )"""
    )
    return conn


def _variant(render: bool) -> str:
    # render=True is a real-browser JS render → materially different HTML than the
    # static fetch, so it's a separate cache entry. unblocker/direct/wayback all return
    # the same target page, so they share one entry (we only cache good bodies).
    return "render" if render else "static"


# Headers the downstream consumers actually read off the response (lean storage).
_KEEP_HEADERS = ("content-language", "content-type")


class CachedResponse:
    """Minimal stand-in for requests.Response covering the attributes the fetch
    consumers touch: .text/.url/.status_code/.headers/.encoding/.content/
    .raise_for_status()/.json(). The body is already decoded, so re-setting
    .encoding (the encoding-fix pass does) is a harmless no-op."""
    def __init__(self, *, text, url, status_code, headers, encoding):
        self.text = text or ""
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.encoding = encoding

    @property
    def content(self) -> bytes:
        try:
            return self.text.encode(self.encoding or "utf-8", "replace")
        except Exception:
            return self.text.encode("utf-8", "replace")

    def raise_for_status(self):
        return None

    def json(self):
        return json.loads(self.text)


def get(url: str, render: bool) -> Optional[CachedResponse]:
    """Return a fresh-within-TTL cached page as a CachedResponse, or None."""
    if not is_enabled():
        return None
    norm = normalize_url(url)
    variant = _variant(render)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_ttl_days())).isoformat()
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT html_gz, final_url, status_code, headers_json, encoding "
                "FROM page_fetch_cache WHERE url_normalized=? AND variant=? AND created_at>=?",
                (norm, variant, cutoff),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE page_fetch_cache SET last_used_at=?, hit_count=hit_count+1 "
                "WHERE url_normalized=? AND variant=?",
                (datetime.now(timezone.utc).isoformat(), norm, variant),
            )
        html = gzip.decompress(row[0]).decode("utf-8", "replace")
        headers = {}
        try:
            headers = json.loads(row[3]) if row[3] else {}
        except Exception:
            headers = {}
        return CachedResponse(text=html, url=row[1] or url, status_code=row[2],
                              headers=headers, encoding=row[4])
    except Exception as e:
        print(f"[page-cache] get failed ({url}): {e}")
        return None


def put(url: str, render: bool, resp, meta: Optional[dict] = None) -> None:
    """Store a SUCCESSFUL, non-stub fetched page. No-op when disabled, on a non-2xx
    status, an empty/oversized body, or any error (best-effort — never breaks a fetch)."""
    if not is_enabled():
        return
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
        text = getattr(resp, "text", "") or ""
        if not (200 <= status < 300) or not text:
            return
        raw = text.encode("utf-8", "replace")
        if len(raw) > _MAX_CACHE_BYTES:
            return
        hdrs_in = dict(getattr(resp, "headers", {}) or {})
        headers = {k: v for k, v in hdrs_in.items() if k.lower() in _KEEP_HEADERS}
        norm = normalize_url(url)
        variant = _variant(render)
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO page_fetch_cache "
                "(url_normalized, variant, status_code, final_url, headers_json, encoding, "
                " source, html_gz, created_at, last_used_at, hit_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,COALESCE("
                "  (SELECT hit_count FROM page_fetch_cache WHERE url_normalized=? AND variant=?),0))",
                (norm, variant, status, getattr(resp, "url", url),
                 json.dumps(headers), getattr(resp, "encoding", None),
                 (meta or {}).get("source"), gzip.compress(raw),
                 datetime.now(timezone.utc).isoformat(), None, norm, variant),
            )
    except Exception as e:
        print(f"[page-cache] put failed ({url}): {e}")


def purge(retain_days: Optional[float] = None, vacuum_at_pct: int = 20) -> dict:
    """Delete cached pages older than `retain_days` and reclaim the space.

    WHY THIS HAD TO EXIST. The TTL was read-only: `get()` refuses a row older
    than `page_cache_ttl_days` (5) and nothing ever DELETED one, so the file grew
    by ~100KB per candidate fetched, forever. Found 2026-08-22 at 1.28 GB across
    12,253 pages, of which 65% had never been served once and 32% were older than
    30 days — on a host with an RMA pending for confirmed CPU degradation.

    Retention defaults to 30 days, SIX TIMES the serving TTL. By the cache's own
    contract anything past 5 days is already dead, so a 6x margin costs a little
    disk and survives someone raising the TTL later without silently throwing
    away pages that would then have been serveable. Note the deleted rows include
    paid `unblocker` fetches — that is correct, because `get()` would not have
    returned them anyway, but it is the reason not to purge aggressively.

    VACUUM only above `vacuum_at_pct` free pages: it rewrites the whole file and
    briefly needs double the disk, which is not something to do nightly for a few
    hundred rows. Returns a summary dict for the job log.
    """
    if retain_days is None:
        try:
            from input.pipeline import system_config as _cfg
            retain_days = float(_cfg.get_setting("page_cache_retain_days", 30))
        except Exception:
            retain_days = 30.0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
    out = {"retain_days": retain_days, "deleted": 0, "vacuumed": False,
           "bytes_before": 0, "bytes_after": 0}
    try:
        out["bytes_before"] = os.path.getsize(_DB_PATH)
    except Exception:
        pass
    try:
        with _connect() as conn:
            out["deleted"] = conn.execute(
                "DELETE FROM page_fetch_cache WHERE created_at < ?", (cutoff,)).rowcount
            conn.commit()
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
            total = conn.execute("PRAGMA page_count").fetchone()[0] or 1
            pct = 100.0 * free / total
        out["free_pct"] = round(pct, 1)
        if pct >= vacuum_at_pct:
            # VACUUM needs its own connection with no open transaction.
            vc = sqlite3.connect(_DB_PATH, isolation_level=None, timeout=120)
            vc.execute("PRAGMA busy_timeout=120000")
            vc.execute("VACUUM")
            vc.close()
            out["vacuumed"] = True
        try:
            out["bytes_after"] = os.path.getsize(_DB_PATH)
        except Exception:
            pass
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        print(f"[page-cache] purge failed: {e}")
    return out
