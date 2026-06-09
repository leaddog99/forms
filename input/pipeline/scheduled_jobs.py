"""Scheduled jobs — the DB-resident registry of recurring jobs.

DISTINCT from the `jobs` table (which holds job RUNS / the queue). This table
holds job DEFINITIONS: what recurring tasks exist, their purpose, cadence, and
on/off — editable via the a/c/d admin editor and obeyed by `python -m jobs
schedule`. Same portable-package spirit as system_config: the schedule lives in
the DB, not in code (memory/project_system_config, project_jobs_as_executables).

A row's `job_type` is a registered handler key (jobs_lib.register_handler), e.g.
"chapter_rollups". The scheduler runs a row when it's `enabled` and at least
`interval_hours` have elapsed since `last_run_at`. `params` (JSON) is passed to
the handler. The per-DISH refreshes are NOT here — those are driven by each
dish's own TTL; this registry is for singleton recurring jobs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


# Bootstrap seed — code constant is seed-only; the table is canonical once seeded
# (new rows only INSERT if absent, so a curator's edits are never clobbered).
SCHEDULED_DEFAULTS: list[dict] = [
    {
        "name": "chapter_rollups",
        "job_type": "chapter_rollups",
        "purpose": "Recompute each dish's competitiveness percentile within its "
                   "chapter (a cross-dish rollup that drifts as siblings refresh, "
                   "so it runs on a schedule rather than on every refresh).",
        "interval_hours": 24,
        "params": {},
        "enabled": 1,
    },
]


def ensure_scheduled_jobs_table(conn: sqlite3.Connection) -> None:
    """Create + seed the table. Idempotent; safe on every startup."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            name           TEXT PRIMARY KEY,
            job_type       TEXT NOT NULL,
            purpose        TEXT,
            interval_hours REAL NOT NULL DEFAULT 24,
            params         TEXT,                 -- JSON
            enabled        INTEGER NOT NULL DEFAULT 1,
            last_run_at    TEXT,
            last_status    TEXT,
            last_job_id    INTEGER,
            created_at     TEXT,
            updated_at     TEXT
        )
        """
    )
    now = datetime.now(timezone.utc).isoformat()
    existing = {r[0] for r in conn.execute("SELECT name FROM scheduled_jobs")}
    for d in SCHEDULED_DEFAULTS:
        if d["name"] in existing:
            continue
        conn.execute(
            "INSERT INTO scheduled_jobs (name, job_type, purpose, interval_hours, "
            "params, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (d["name"], d["job_type"], d.get("purpose"),
             float(d.get("interval_hours", 24)), json.dumps(d.get("params") or {}),
             1 if d.get("enabled", 1) else 0, now, now),
        )
    conn.commit()


def _row_to_dict(row: tuple) -> dict:
    (name, job_type, purpose, interval_hours, params_json, enabled,
     last_run_at, last_status, last_job_id, created_at, updated_at) = row
    try:
        params = json.loads(params_json) if params_json else {}
    except Exception:
        params = {}
    return {
        "name": name,
        "job_type": job_type,
        "purpose": purpose,
        "interval_hours": interval_hours,
        "params": params,
        "enabled": bool(enabled),
        "last_run_at": last_run_at,
        "last_status": last_status,
        "last_job_id": last_job_id,
        "created_at": created_at,
        "updated_at": updated_at,
    }


_COLS = ("name, job_type, purpose, interval_hours, params, enabled, "
         "last_run_at, last_status, last_job_id, created_at, updated_at")


def list_scheduled_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"SELECT {_COLS} FROM scheduled_jobs ORDER BY name").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_scheduled_job(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    row = conn.execute(f"SELECT {_COLS} FROM scheduled_jobs WHERE name = ?", (name,)).fetchone()
    return _row_to_dict(row) if row else None


def upsert_scheduled_job(conn: sqlite3.Connection, name: str, fields: dict) -> dict:
    """Create or update a row. Only the editable columns are touched
    (job_type/purpose/interval_hours/params/enabled). Returns the row."""
    now = datetime.now(timezone.utc).isoformat()
    existing = get_scheduled_job(conn, name)
    job_type = fields.get("job_type", existing["job_type"] if existing else "")
    purpose = fields.get("purpose", existing["purpose"] if existing else "")
    interval_hours = float(fields.get("interval_hours",
                                      existing["interval_hours"] if existing else 24))
    if interval_hours <= 0:
        raise ValueError("interval_hours must be positive")
    params = fields.get("params", existing["params"] if existing else {})
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    enabled = 1 if fields.get("enabled", existing["enabled"] if existing else True) else 0
    if not job_type:
        raise ValueError("job_type is required")
    if existing:
        conn.execute(
            "UPDATE scheduled_jobs SET job_type=?, purpose=?, interval_hours=?, "
            "params=?, enabled=?, updated_at=? WHERE name=?",
            (job_type, purpose, interval_hours, json.dumps(params), enabled, now, name),
        )
    else:
        conn.execute(
            "INSERT INTO scheduled_jobs (name, job_type, purpose, interval_hours, "
            "params, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, job_type, purpose, interval_hours, json.dumps(params), enabled, now, now),
        )
    conn.commit()
    return get_scheduled_job(conn, name)


def delete_scheduled_job(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM scheduled_jobs WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def record_run(conn: sqlite3.Connection, name: str, *, status: str,
               job_id: Optional[int] = None) -> None:
    """Stamp last_run_at/status/job_id after the scheduler runs a row."""
    conn.execute(
        "UPDATE scheduled_jobs SET last_run_at=?, last_status=?, last_job_id=? WHERE name=?",
        (datetime.now(timezone.utc).isoformat(), status, job_id, name),
    )
    conn.commit()


def find_due_scheduled_jobs(conn: sqlite3.Connection) -> list[dict]:
    """Enabled rows whose interval_hours have elapsed since last_run_at
    (never-run rows are always due)."""
    now = datetime.now(timezone.utc)
    due = []
    for j in list_scheduled_jobs(conn):
        if not j["enabled"]:
            continue
        last = j.get("last_run_at")
        if not last:
            due.append(j)
            continue
        try:
            last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            elapsed_h = (now - last_dt).total_seconds() / 3600.0
            if elapsed_h >= float(j["interval_hours"]):
                due.append(j)
        except Exception:
            due.append(j)
    return due
