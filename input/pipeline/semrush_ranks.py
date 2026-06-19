"""SEMrush Rank — a reference dataset of the top-N domains by organic search
traffic, used as an independent *traffic-authority* signal on the domain master.

SEMrush's "Rank" is a global ordinal by Organic Traffic (rank 1 = most traffic).
It complements the link-authority signals we already carry (Moz DA/PA, referring
domains): traffic is harder to game and reflects real audience reach.

The bulk web export is **top-N + per-region** (the file we ingest is e.g.
``ranks.semrushranks-us-YYYYMMDD-…​.xlsx`` = the top 10,000 US domains). So:
  - small / paywalled / long-tail domains are simply ABSENT → null rank (normal).
  - foreign domains need their own region file (this is the US list).

Stored verbatim at root-domain grain (the file's grain). A domain on the
``domains`` master gets its rank by matching ``root_domain`` against this table;
the per-domain ``semrush_rank`` is stamped from here, and the corpus-relative
"rank the rank" is DERIVED (see domains_lib).

Multi-region by design (PRIMARY KEY ``(domain, region)``); re-import is a
delete-and-replace for that region so a fresh file cleanly supersedes the old.
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_DEFAULT_DB = "recipes.db"

# Tolerant header → column map (SEMrush has shuffled these before; match by name).
_COL_ALIASES = {
    "rank": ("Rank", "SEMrush Rank"),
    "organic_keywords": ("Organic Keywords", "Organic keywords"),
    "organic_traffic": ("Organic Traffic", "Organic traffic"),
    "organic_cost": ("Organic Cost", "Organic cost"),
    "adwords_keywords": ("Adwords Keywords", "Ads Keywords", "Paid Keywords"),
    "adwords_traffic": ("Adwords Traffic", "Ads Traffic", "Paid Traffic"),
    "adwords_cost": ("Adwords Cost", "Ads Cost", "Paid Cost"),
}
_DOMAIN_ALIASES = ("Domain", "domain")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon_host(s: str) -> str:
    """Lowercase, drop scheme/path/query/port and a leading ``www.``. Mirrors
    domains_lib._canon_host so both sides key identically."""
    h = (s or "").strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    h = h.split(":", 1)[0].lstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h


def _two_part_root(host: str) -> str:
    """Naive registrable root = last two labels (consistent with the rest of the
    system's url_utils.root_domain). Wrong for multi-part ccTLDs (co.uk) — handled
    by also trying the exact host as a lookup key."""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def lookup_keys(host: str) -> list[str]:
    """Candidate keys to find a host's rank, most-specific first: the exact canon
    host (catches ``bbc.co.uk`` and bare roots), then the two-part root (so a
    subdomain like ``cooking.nytimes.com`` resolves to ``nytimes.com``'s rank)."""
    h = _canon_host(host)
    if not h:
        return []
    root = _two_part_root(h)
    return [h] if root == h else [h, root]


def ensure_semrush_ranks_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS semrush_ranks (
            domain            TEXT NOT NULL,
            region            TEXT NOT NULL DEFAULT 'us',
            rank              INTEGER,
            organic_keywords  INTEGER,
            organic_traffic   INTEGER,
            organic_cost      INTEGER,
            adwords_keywords  INTEGER,
            adwords_traffic   INTEGER,
            adwords_cost      INTEGER,
            file_date         TEXT,
            imported_at       TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (domain, region)
        )
        """
    )
    # PK(domain, region) already indexes the domain lookup (leftmost prefix).
    # Add a traffic index for corpus-wide sorts/joins by reach.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_semrush_ranks_traffic "
        "ON semrush_ranks(organic_traffic DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_semrush_ranks_rank "
        "ON semrush_ranks(region, rank)"
    )
    conn.commit()


def parse_region_and_date(path: str) -> tuple[str, Optional[str]]:
    """Pull region + data-date from the SEMrush export filename, e.g.
    ``ranks.semrushranks-us-20260618-2026-06-19T….xlsx`` → ('us', '20260618').
    Falls back to ('us', None) if the name doesn't match."""
    name = os.path.basename(path)
    m = re.search(r"semrushranks-([a-z]{2})-(\d{8})", name, re.I)
    if m:
        return m.group(1).lower(), m.group(2)
    return "us", None


_PROJECT_INPUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # <project>/input
_RANKS_GLOB = "*semrushranks*.xlsx"


def find_newest_ranks_file(extra_dirs: Optional[list[str]] = None) -> Optional[str]:
    """Newest SEMrush Rank export across input/ ∪ ~/Downloads (∪ extra_dirs).
    The export is MANUAL (no API), so the batch refresh re-imports whatever fresh
    file the admin dropped — same no-daemon pattern as the backlinks file
    (docs/semrush-harvest-scheduling.md §2 Fix B). Newest mtime wins."""
    import glob
    dirs = [_PROJECT_INPUT, os.path.join(os.path.expanduser("~"), "Downloads")]
    dirs += list(extra_dirs or [])
    hits = []
    for d in dirs:
        try:
            hits += glob.glob(os.path.join(d, _RANKS_GLOB))
        except Exception:
            pass
    return max(hits, key=os.path.getmtime) if hits else None


def import_ranks_file(conn: sqlite3.Connection, path: str,
                      region: Optional[str] = None) -> dict:
    """Ingest a SEMrush Rank export (.xlsx) into ``semrush_ranks``, delete-and-
    replace for its region. Returns {region, file_date, rows, path}."""
    import openpyxl

    if not os.path.exists(path):
        raise FileNotFoundError(path)
    parsed_region, file_date = parse_region_and_date(path)
    region = (region or parsed_region or "us").lower()

    ws = openpyxl.load_workbook(path, read_only=True).active
    it = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(it)]

    def col(aliases):
        for a in aliases:
            if a in header:
                return header.index(a)
        return None

    di = col(_DOMAIN_ALIASES)
    if di is None:
        raise ValueError(f"No Domain column in SEMrush ranks file: {header}")
    idx = {field: col(aliases) for field, aliases in _COL_ALIASES.items()}

    def num(row, field):
        i = idx.get(field)
        if i is None or i >= len(row):
            return None
        v = row[i]
        if v is None or v == "":
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    ensure_semrush_ranks_table(conn)
    now = _now()
    rows = []
    seen = set()
    for r in it:
        dom = _canon_host(str(r[di] or ""))
        if not dom or dom in seen:
            continue
        seen.add(dom)
        rows.append((
            dom, region,
            num(r, "rank"), num(r, "organic_keywords"), num(r, "organic_traffic"),
            num(r, "organic_cost"), num(r, "adwords_keywords"),
            num(r, "adwords_traffic"), num(r, "adwords_cost"),
            file_date, now,
        ))

    conn.execute("DELETE FROM semrush_ranks WHERE region = ?", (region,))
    conn.executemany(
        "INSERT INTO semrush_ranks (domain, region, rank, organic_keywords, "
        "organic_traffic, organic_cost, adwords_keywords, adwords_traffic, "
        "adwords_cost, file_date, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return {"region": region, "file_date": file_date, "rows": len(rows), "path": path}


def get_rank(conn: sqlite3.Connection, host: str,
             region: str = "us") -> Optional[dict]:
    """Look up a host's SEMrush rank row (most-specific match wins). Returns the
    row dict or None when the domain isn't in the (top-N) file."""
    keys = lookup_keys(host)
    if not keys:
        return None
    ensure_semrush_ranks_table(conn)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" * len(keys))
    rows = {
        r["domain"]: dict(r)
        for r in conn.execute(
            f"SELECT * FROM semrush_ranks WHERE region = ? AND domain IN ({placeholders})",
            (region, *keys),
        ).fetchall()
    }
    for k in keys:                       # honor candidate order (exact host first)
        if k in rows:
            return rows[k]
    return None


def region_stats(conn: sqlite3.Connection) -> list[dict]:
    """Per-region summary for the admin UI: row count, file date, last import."""
    ensure_semrush_ranks_table(conn)
    rows = conn.execute(
        "SELECT region, COUNT(*) n, MAX(file_date) file_date, MAX(imported_at) "
        "imported_at FROM semrush_ranks GROUP BY region ORDER BY region"
    ).fetchall()
    return [{"region": r[0], "rows": r[1], "file_date": r[2], "imported_at": r[3]}
            for r in rows]
