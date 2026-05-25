"""Dish library — the dishes table + helpers.

A dish is the unit of curated top-recipe collection. Each row in this
table maps a canonical dish name (e.g. "Spaghetti and Meat Sauce") to
a set of SerpAPI queries that populate it, plus tuning + refresh
metadata.

The dish name is the IMMUTABLE primary key — every recipe in
master_recipes that came from a batch refresh stamps `_master.dish`
with this name, and the delete-and-replace logic uses it as the join
key. Renaming would orphan all those rows; that's why we forbid it
(callers delete + recreate to "rename" — which also deletes the
master rows, intentionally).

Both the admin form-driven refresh button AND the cron-fired
`refresh_due_dishes.py` agent operate on this table:
  - Form: list + create + edit + delete + manual refresh
  - Agent: scans for due dishes (refresh_ttl_days elapsed since
           last_refreshed) and runs each through the same in-process
           build_batch + delete-and-replace as the form.

See memory/project_dish_library.md for the broader design.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


def ensure_dishes_table(conn: sqlite3.Connection) -> None:
    """Create the dishes table and its indexes if absent. Idempotent —
    safe to call on every startup. Also runs lightweight ALTER TABLE
    migrations for columns added after the initial schema."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dishes (
            name              TEXT PRIMARY KEY COLLATE NOCASE,
            queries           TEXT NOT NULL,           -- JSON array of strings
            top_n_serpapi     INTEGER NOT NULL DEFAULT 25,
            top_n_final       INTEGER NOT NULL DEFAULT 10,
            refresh_ttl_days  INTEGER DEFAULT 30,      -- NULL = manual-only (agent skips)
            last_refreshed    TEXT,                    -- ISO-8601 UTC; null if never
            last_run_status   TEXT,                    -- 'success' | 'error:<reason>'
            last_run_count    INTEGER,                 -- rows landing in master after refresh
            notes             TEXT,                    -- curator's free-form note
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
        """
    )
    # Migration (2026-05-24): add last_run_log_filename. Idempotent —
    # check existing columns and ADD only when absent.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dishes)")}
    if "last_run_log_filename" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN last_run_log_filename TEXT")
    # Index on refresh_ttl_days so the agent's "find due" query is cheap.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dishes_ttl "
        "ON dishes(refresh_ttl_days) WHERE refresh_ttl_days IS NOT NULL"
    )
    conn.commit()


def row_to_dict(row: tuple) -> dict:
    """Convert a SELECT * row into the dict shape every endpoint returns.

    `queries` is stored as a JSON string in SQLite; we materialize it to
    a list here so the API surfaces a real array. Adds a derived
    `is_due` field based on refresh_ttl_days + last_refreshed, and a
    derived `last_run_log_url` for the form's "View latest log" link.
    """
    name, queries_json, top_n_serpapi, top_n_final, ttl_days, \
        last_refreshed, last_run_status, last_run_count, notes, \
        created_at, updated_at, last_run_log_filename = row
    try:
        queries = json.loads(queries_json) if queries_json else []
    except Exception:
        queries = []
    return {
        "name": name,
        "queries": queries,
        "top_n_serpapi": top_n_serpapi,
        "top_n_final": top_n_final,
        "refresh_ttl_days": ttl_days,
        "last_refreshed": last_refreshed,
        "last_run_status": last_run_status,
        "last_run_count": last_run_count,
        "notes": notes,
        "created_at": created_at,
        "updated_at": updated_at,
        "is_due": is_due(ttl_days, last_refreshed),
        "last_run_log_filename": last_run_log_filename,
        "last_run_log_url": f"/logs/{last_run_log_filename}" if last_run_log_filename else None,
    }


_SELECT_ALL_COLS = (
    "name, queries, top_n_serpapi, top_n_final, refresh_ttl_days, "
    "last_refreshed, last_run_status, last_run_count, notes, "
    "created_at, updated_at, last_run_log_filename"
)


def list_dishes(conn: sqlite3.Connection) -> list[dict]:
    """Return all dishes, newest first by created_at."""
    rows = conn.execute(
        f"SELECT {_SELECT_ALL_COLS} FROM dishes ORDER BY created_at DESC"
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def get_dish(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    """Look up by name (case-insensitive — table uses COLLATE NOCASE)."""
    row = conn.execute(
        f"SELECT {_SELECT_ALL_COLS} FROM dishes WHERE name = ?",
        (name,),
    ).fetchone()
    return row_to_dict(row) if row else None


def validate_create_payload(payload: dict) -> tuple[str, list[str], int, int, Optional[int], Optional[str]]:
    """Validate a POST /dishes body. Returns (name, queries, top_n_serpapi,
    top_n_final, refresh_ttl_days, notes). Raises ValueError on any
    problem; the endpoint converts that to a 400."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required and must be non-empty")
    queries_raw = payload.get("queries")
    if not isinstance(queries_raw, list) or not queries_raw:
        raise ValueError("queries must be a non-empty array of strings")
    queries = [str(q).strip() for q in queries_raw if str(q).strip()]
    if not queries:
        raise ValueError("queries must contain at least one non-empty string")

    top_n_serpapi = int(payload.get("top_n_serpapi", 25))
    if top_n_serpapi <= 0:
        raise ValueError("top_n_serpapi must be positive")
    top_n_final = int(payload.get("top_n_final", 10))
    if top_n_final <= 0:
        raise ValueError("top_n_final must be positive")

    ttl_raw = payload.get("refresh_ttl_days", 30)
    if ttl_raw is None or ttl_raw == "":
        ttl: Optional[int] = None
    else:
        ttl = int(ttl_raw)
        if ttl <= 0:
            raise ValueError("refresh_ttl_days must be positive or null")

    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be a string or null")
    notes = (notes or None) if notes is None else notes.strip() or None

    return name, queries, top_n_serpapi, top_n_final, ttl, notes


def create_dish(conn: sqlite3.Connection, *,
                name: str,
                queries: list[str],
                top_n_serpapi: int = 25,
                top_n_final: int = 10,
                refresh_ttl_days: Optional[int] = 30,
                notes: Optional[str] = None) -> dict:
    """Insert a new dish. Raises sqlite3.IntegrityError on name
    collision (caller maps to 409). Returns the created dict."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO dishes (name, queries, top_n_serpapi, top_n_final, "
        "refresh_ttl_days, notes, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, json.dumps(queries), top_n_serpapi, top_n_final,
         refresh_ttl_days, notes, now, now),
    )
    conn.commit()
    return get_dish(conn, name)  # round-trip so we return the canonical shape


# The fields PATCH is allowed to update. `name` is intentionally absent —
# it's the join key into master_recipes._master.dish, so renaming would
# orphan recipes. To "rename", caller deletes + recreates (which deletes
# the master rows too — intentional cascade).
_PATCHABLE = {
    "queries", "top_n_serpapi", "top_n_final",
    "refresh_ttl_days", "notes",
}


def update_dish(conn: sqlite3.Connection, name: str, patch: dict) -> Optional[dict]:
    """Apply a partial update to a dish row. Returns the updated dict, or
    None if the row doesn't exist. Raises ValueError on field-validation
    failures."""
    existing = get_dish(conn, name)
    if existing is None:
        return None

    sets: list[str] = []
    params: list = []

    if "queries" in patch:
        q = patch["queries"]
        if not isinstance(q, list) or not q:
            raise ValueError("queries must be a non-empty array of strings")
        q_clean = [str(x).strip() for x in q if str(x).strip()]
        if not q_clean:
            raise ValueError("queries must contain at least one non-empty string")
        sets.append("queries = ?")
        params.append(json.dumps(q_clean))

    if "top_n_serpapi" in patch:
        v = int(patch["top_n_serpapi"])
        if v <= 0:
            raise ValueError("top_n_serpapi must be positive")
        sets.append("top_n_serpapi = ?")
        params.append(v)

    if "top_n_final" in patch:
        v = int(patch["top_n_final"])
        if v <= 0:
            raise ValueError("top_n_final must be positive")
        sets.append("top_n_final = ?")
        params.append(v)

    if "refresh_ttl_days" in patch:
        raw = patch["refresh_ttl_days"]
        if raw is None or raw == "":
            sets.append("refresh_ttl_days = NULL")
        else:
            v = int(raw)
            if v <= 0:
                raise ValueError("refresh_ttl_days must be positive or null")
            sets.append("refresh_ttl_days = ?")
            params.append(v)

    if "notes" in patch:
        n = patch["notes"]
        if n is None:
            sets.append("notes = NULL")
        else:
            if not isinstance(n, str):
                raise ValueError("notes must be a string or null")
            stripped = n.strip()
            if stripped:
                sets.append("notes = ?")
                params.append(stripped)
            else:
                sets.append("notes = NULL")

    extras = set(patch.keys()) - _PATCHABLE
    if extras:
        raise ValueError(f"non-patchable fields in body: {sorted(extras)}")

    if not sets:
        # No updatable fields supplied — return the existing row unchanged.
        return existing

    sets.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(name)

    conn.execute(
        f"UPDATE dishes SET {', '.join(sets)} WHERE name = ?",
        params,
    )
    conn.commit()
    return get_dish(conn, name)


def delete_dish(conn: sqlite3.Connection, name: str) -> bool:
    """Delete a dish row. Returns True if a row was removed.

    NOTE: this does NOT yet cascade-delete the dish's top-kind rows in
    master_recipes — that's done by the in-process refresh logic when
    #3 lands. For now the dish row goes; any existing master rows
    stamped with `_master.dish == name` (if/when they exist) stay
    until the next batch refresh. Tracked in project_dish_library.md.
    """
    cur = conn.execute("DELETE FROM dishes WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def is_due(refresh_ttl_days: Optional[int], last_refreshed: Optional[str]) -> bool:
    """True when an auto-refresh agent should pick this dish up.

    Rules:
      - refresh_ttl_days is None → manual-only; never due automatically.
      - last_refreshed is None → never run; always due.
      - now - last_refreshed >= ttl_days → due.
    """
    if refresh_ttl_days is None:
        return False
    if not last_refreshed:
        return True
    try:
        last = datetime.fromisoformat(last_refreshed.replace("Z", "+00:00"))
    except Exception:
        return True  # malformed timestamp → safer to refresh
    age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
    return age_days >= refresh_ttl_days


def find_due_dishes(conn: sqlite3.Connection) -> list[dict]:
    """Return all dishes whose auto-refresh is due. Used by the cron-fired
    refresh_due_dishes.py agent. Filters out refresh_ttl_days IS NULL
    (manual-only) at the SQL layer; final due check is in Python."""
    rows = conn.execute(
        f"SELECT {_SELECT_ALL_COLS} FROM dishes "
        f"WHERE refresh_ttl_days IS NOT NULL "
        f"ORDER BY last_refreshed IS NULL DESC, last_refreshed ASC"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = row_to_dict(r)
        if d["is_due"]:
            out.append(d)
    return out


def delete_master_rows_for_dish(conn: sqlite3.Connection, dish_name: str,
                                kind: str = "top") -> int:
    """Delete every row in master_recipes whose `_master.dish` matches
    `dish_name` AND `_master.kind` matches `kind`. Returns the
    deletion count. Used by the refresh-dish path to clear the prior
    top-N before re-populating; editors_choice and legacy rows are
    untouched because the kind filter excludes them.
    """
    cur = conn.execute(
        "DELETE FROM master_recipes "
        "WHERE json_extract(data, '$._master.dish') = ? "
        "AND json_extract(data, '$._master.kind') = ?",
        (dish_name, kind),
    )
    conn.commit()
    return cur.rowcount


def record_run_result(conn: sqlite3.Connection, name: str, *,
                      status: str, count: Optional[int] = None,
                      log_filename: Optional[str] = None) -> None:
    """Stamp a refresh run's outcome on the dish row. Called by both the
    /dishes/<name>/refresh endpoint and the agent. `status` is
    'success' or 'error:<short-reason>'. `log_filename` is the basename
    of the per-run log file under forms/logs/; the form turns it into
    a /logs/<filename> link."""
    now = datetime.now(timezone.utc).isoformat()
    if log_filename is not None:
        conn.execute(
            "UPDATE dishes SET last_refreshed = ?, last_run_status = ?, "
            "last_run_count = ?, last_run_log_filename = ?, updated_at = ? "
            "WHERE name = ?",
            (now, status, count, log_filename, now, name),
        )
    else:
        conn.execute(
            "UPDATE dishes SET last_refreshed = ?, last_run_status = ?, "
            "last_run_count = ?, updated_at = ? WHERE name = ?",
            (now, status, count, now, name),
        )
    conn.commit()
