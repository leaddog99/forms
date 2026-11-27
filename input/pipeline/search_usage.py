"""Count what we send the web search provider.

Every request to the search API is one row in `search_requests`: when, which job,
what was asked, what came back, and the provider's own quota headers as it sent
them. The provider bills per request, and until 2026-11-27 nothing here counted
them: a day of 85 dish refreshes could only be estimated.

`record` never raises (a counter must not cost a search). `summary()` is the read
side; `python -m input.pipeline.search_usage [days]` prints it.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from input.pipeline.db import connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_requests (
    id             INTEGER PRIMARY KEY,
    ts             TEXT NOT NULL,       -- UTC, ISO
    job_id         INTEGER,             -- the job that asked, when there is one
    provider       TEXT NOT NULL,
    status         INTEGER NOT NULL,    -- HTTP status; 0 = no answer
    query          TEXT,
    country        TEXT,
    lang           TEXT,
    page           INTEGER,             -- the provider's offset, 0-based
    results        INTEGER,             -- results in the answer
    ms             INTEGER,
    rate_limit     TEXT,                -- the provider's quota headers, verbatim
    rate_remaining TEXT
);
CREATE INDEX IF NOT EXISTS idx_search_requests_ts ON search_requests(ts);
"""
_ensured: set = set()
_lock = threading.Lock()


def ensure_table(conn: sqlite3.Connection, db_path: str = "recipes.db") -> None:
    with _lock:
        if db_path in _ensured:
            return
        conn.executescript(_SCHEMA)
        _ensured.add(db_path)


def _job_id() -> Optional[int]:
    try:
        from input.pipeline import acquisition
        return acquisition.context().get("job_id")
    except Exception:
        return None


def record(provider: str, *, status: int, query: str = "", country: str = "", lang: str = "",
           page: Optional[int] = None, results: Optional[int] = None, ms: Optional[int] = None,
           rate_limit: str = "", rate_remaining: str = "", db_path: str = "recipes.db") -> None:
    """Write one request row. Never raises."""
    try:
        conn = connect(db_path)
        try:
            ensure_table(conn, db_path)
            conn.execute(
                "INSERT INTO search_requests(ts, job_id, provider, status, query, country, lang, "
                "page, results, ms, rate_limit, rate_remaining) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), _job_id(), provider,
                 int(status or 0), (query or "")[:300], country or None, lang or None, page,
                 results, ms, rate_limit or None, rate_remaining or None))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:                                       # pragma: no cover
        print(f"[search-usage] not recorded ({type(e).__name__}: {e})")


def counting_get(provider: str, http_get):
    """Wrap a provider's `http_get(url, *, params, headers, timeout)` so each
    request is recorded. The response is handed back untouched; a request that
    raises is recorded as status 0 and the error re-raised."""
    def _get(url, *, params, headers, timeout):
        t0 = time.monotonic()
        status, n, limit, remaining = 0, None, "", ""
        try:
            resp = http_get(url, params=params, headers=headers, timeout=timeout)
            status = int(getattr(resp, "status_code", 0) or 0)
            hdrs = getattr(resp, "headers", None) or {}
            limit = str(hdrs.get("X-RateLimit-Limit") or "")
            remaining = str(hdrs.get("X-RateLimit-Remaining") or "")
            try:
                n = len(((resp.json().get("web") or {}).get("results")) or []) if status == 200 else None
            except Exception:
                n = None
            return resp
        finally:
            record(provider, status=status, query=params.get("q") or "",
                   country=params.get("country") or "", lang=params.get("search_lang") or "",
                   page=params.get("offset"), results=n,
                   ms=int((time.monotonic() - t0) * 1000), rate_limit=limit, rate_remaining=remaining)
    return _get


def summary(days: int = 30, *, db_path: str = "recipes.db") -> dict:
    """Requests per day (UTC) for the last `days`, newest first, plus the latest
    quota headers the provider sent. `answered` = status 200, the ones a
    per-request plan bills."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        ensure_table(conn, db_path)
        rows = conn.execute(
            "SELECT substr(ts, 1, 10) AS day, provider, COUNT(*), "
            "SUM(status = 200), SUM(status = 429), SUM(status NOT IN (200, 429)), "
            "COUNT(DISTINCT job_id) FROM search_requests WHERE ts >= ? "
            "GROUP BY day, provider ORDER BY day DESC, provider", (since,)).fetchall()
        last = conn.execute(
            "SELECT ts, provider, rate_limit, rate_remaining FROM search_requests "
            "WHERE rate_remaining IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return {
        "days": [{"day": d, "provider": p, "requests": n, "answered": a or 0,
                  "rate_limited": r or 0, "other": o or 0, "jobs": j} for d, p, n, a, r, o, j in rows],
        "total": sum(r[2] for r in rows),
        "answered": sum((r[3] or 0) for r in rows),
        "quota": ({"ts": last[0], "provider": last[1], "limit": last[2], "remaining": last[3]}
                  if last else None),
    }


if __name__ == "__main__":
    s = summary(int(sys.argv[1]) if len(sys.argv) > 1 else 30)
    print(f"{'day (UTC)':<12}{'provider':<10}{'requests':>9}{'answered':>10}{'429':>6}{'other':>7}{'jobs':>6}")
    for d in s["days"]:
        print(f"{d['day']:<12}{d['provider']:<10}{d['requests']:>9}{d['answered']:>10}"
              f"{d['rate_limited']:>6}{d['other']:>7}{d['jobs']:>6}")
    print(f"total {s['total']} requests, {s['answered']} answered")
    if s["quota"]:
        q = s["quota"]
        print(f"provider quota headers at {q['ts']}: limit={q['limit']} remaining={q['remaining']}")
