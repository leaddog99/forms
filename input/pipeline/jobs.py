"""Async job system — durable queue + in-process runner.

The foundational infrastructure for every long-running task: dish
refreshes (today), future cache refreshes, URL re-scoring, master
enrichment backfills, agent-spawned work. See memory/project_job_system.md
for the design rationale and layer phasing.

Architecture summary:
  - `jobs` table in SQLite is the persistent queue.
  - `runner_loop()` is an asyncio background task started on uvicorn
    startup. It polls the table every ~2s for the next ready job
    (status='queued' AND scheduled_at IS NULL OR <= now), runs it,
    repeats. Serial — one job at a time process-wide. The stdout-tee
    used for per-job log capture is global, so concurrency would
    interleave logs.
  - Handlers are pluggable: `register_handler("dish_refresh", fn)`.
  - Each job's stdout/stderr are teed to `forms/logs/job_<type>_<id>_<ts>.log`
    while it runs. The SSE endpoint in save_recipe_api.py tails that
    file + emits status events.

Crash recovery: on startup, any row left as 'running' is reset to
'error:interrupted' (the runner died mid-job in the last process).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


# ============================================================
#  Schema
# ============================================================

def ensure_jobs_table(conn: sqlite3.Connection) -> None:
    """Create the jobs table and indexes if absent. Idempotent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            type          TEXT NOT NULL,                       -- e.g. 'dish_refresh'
            params        TEXT NOT NULL DEFAULT '{}',          -- JSON, type-specific
            entity_ref    TEXT,                                -- e.g. 'dish:Beef Stew' (for cross-find)
            status        TEXT NOT NULL DEFAULT 'queued',      -- queued|running|success|error|cancelled
            scheduled_at  TEXT,                                -- ISO ts; NULL = ready immediately
            created_at    TEXT NOT NULL,
            started_at    TEXT,
            finished_at   TEXT,
            log_filename  TEXT,                                -- per-job log under forms/logs/
            result        TEXT,                                -- JSON, type-specific summary
            error_detail  TEXT
        )
        """
    )
    # Used by find_next_ready (the runner's hot path).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status_scheduled "
        "ON jobs(status, scheduled_at)"
    )
    # Used to find in-flight jobs for an entity (e.g. 'is this dish
    # currently refreshing?').
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_entity_status "
        "ON jobs(entity_ref, status)"
    )
    # Used by the future /jobs admin page's list view.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_type_created "
        "ON jobs(type, created_at DESC)"
    )
    conn.commit()


def reset_interrupted_jobs(conn: sqlite3.Connection) -> int:
    """Called at startup. Any job left as 'running' is interrupted —
    the prior process died with it in flight. Mark as
    error:interrupted so it doesn't sit forever. Returns the count
    reset for logging."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE jobs SET status='error', "
        "error_detail='interrupted by uvicorn restart', "
        "finished_at=? WHERE status='running'",
        (now,),
    )
    conn.commit()
    return cur.rowcount


# ============================================================
#  CRUD + queries
# ============================================================

_SELECT_COLS = (
    "id, type, params, entity_ref, status, scheduled_at, "
    "created_at, started_at, finished_at, log_filename, result, error_detail"
)


def _row_to_dict(row: tuple) -> dict:
    (id_, type_, params_json, entity_ref, status, scheduled_at,
     created_at, started_at, finished_at, log_filename, result_json,
     error_detail) = row
    try:
        params = json.loads(params_json) if params_json else {}
    except Exception:
        params = {}
    try:
        result = json.loads(result_json) if result_json else None
    except Exception:
        result = None
    return {
        "id": id_,
        "type": type_,
        "params": params,
        "entity_ref": entity_ref,
        "status": status,
        "scheduled_at": scheduled_at,
        "created_at": created_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "log_filename": log_filename,
        "log_url": f"/logs/{log_filename}" if log_filename else None,
        "result": result,
        "error_detail": error_detail,
    }


def enqueue_job(conn: sqlite3.Connection, *,
                type: str,
                params: Optional[dict] = None,
                entity_ref: Optional[str] = None,
                scheduled_at: Optional[str] = None) -> int:
    """Insert a new job. Returns the new job_id. The runner picks it up
    on its next poll (typically <2s)."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO jobs (type, params, entity_ref, scheduled_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (type, json.dumps(params or {}), entity_ref, scheduled_at, now),
    )
    conn.commit()
    return cur.lastrowid


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[dict]:
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def list_jobs(conn: sqlite3.Connection, *,
              type: Optional[str] = None,
              entity_ref: Optional[str] = None,
              status: Optional[str] = None,
              limit: int = 100) -> list[dict]:
    sql = f"SELECT {_SELECT_COLS} FROM jobs WHERE 1=1"
    args: list = []
    if type is not None:
        sql += " AND type = ?"
        args.append(type)
    if entity_ref is not None:
        sql += " AND entity_ref = ?"
        args.append(entity_ref)
    if status is not None:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]


def find_next_ready(conn: sqlite3.Connection) -> Optional[dict]:
    """Pick the oldest queued job whose scheduled_at has elapsed (or is
    null). Used by the runner each tick."""
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM jobs "
        f"WHERE status = 'queued' "
        f"AND (scheduled_at IS NULL OR scheduled_at <= ?) "
        f"ORDER BY created_at ASC LIMIT 1",
        (now,),
    ).fetchone()
    return _row_to_dict(row) if row else None


def mark_running(conn: sqlite3.Connection, job_id: int, log_filename: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET status='running', started_at=?, log_filename=? WHERE id=?",
        (now, log_filename, job_id),
    )
    conn.commit()


def mark_finished(conn: sqlite3.Connection, job_id: int, *,
                  status: str,
                  result: Optional[dict] = None,
                  error_detail: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE jobs SET status=?, finished_at=?, result=?, error_detail=? WHERE id=?",
        (status, now,
         json.dumps(result) if result is not None else None,
         error_detail,
         job_id),
    )
    conn.commit()


def find_in_flight_for_entity(conn: sqlite3.Connection,
                              entity_ref: str) -> Optional[dict]:
    """Is there a queued-or-running job for this entity? Used by Run
    handlers to refuse double-enqueue."""
    row = conn.execute(
        f"SELECT {_SELECT_COLS} FROM jobs "
        f"WHERE entity_ref = ? AND status IN ('queued', 'running') "
        f"ORDER BY created_at DESC LIMIT 1",
        (entity_ref,),
    ).fetchone()
    return _row_to_dict(row) if row else None


# ============================================================
#  Handler registry
# ============================================================

# type -> async fn(job: dict) -> dict|None (the result to record)
JobHandler = Callable[[dict], Awaitable[Optional[dict]]]
JOB_HANDLERS: dict[str, JobHandler] = {}


def register_handler(type: str, fn: JobHandler) -> None:
    JOB_HANDLERS[type] = fn


# ============================================================
#  Stdout tee for per-job log capture
# ============================================================

class _TeeStream:
    """Forward writes to multiple streams. Flushes after each write so
    the live SSE tail can see output in real time (and so the bcc_start
    terminal updates as the job runs, not in bursts when buffers spill).
    NOT thread-safe — protected by the runner's serial execution."""
    def __init__(self, *streams):
        self._streams = streams
    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass
    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass
    def isatty(self):
        return False


def _slug_for_log(s: str) -> str:
    import re as _re
    s = _re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-").lower()
    return s or "job"


def _build_log_filename(job: dict) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    type_slug = _slug_for_log(job.get("type") or "job")
    entity_slug = _slug_for_log((job.get("entity_ref") or "").split(":", 1)[-1])
    suffix = f"_{entity_slug}" if entity_slug else ""
    return f"job_{type_slug}_{job['id']}{suffix}_{ts}.log"


# ============================================================
#  Runner
# ============================================================

async def _run_one_job(job: dict, db_path: str, log_dir: Path) -> None:
    """Execute one job: open log file, tee stdout/stderr to it, mark
    running, call handler, record result, restore stdout/stderr.
    Failures are caught and recorded as error status; the runner loop
    continues."""
    log_filename = _build_log_filename(job)
    log_path = log_dir / log_filename
    log_file = open(log_path, "w", encoding="utf-8", buffering=1)
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(orig_stdout, log_file)
    sys.stderr = _TeeStream(orig_stderr, log_file)

    print(f"=== Job #{job['id']} ({job['type']}) starting ===")
    print(f"entity_ref: {job.get('entity_ref')}")
    print(f"params: {job.get('params')}")

    try:
        with sqlite3.connect(db_path) as conn:
            mark_running(conn, job["id"], log_filename)

        handler = JOB_HANDLERS.get(job["type"])
        if handler is None:
            raise RuntimeError(f"No handler registered for job type {job['type']!r}")

        # Pass the freshly-stamped job dict (with log_filename) to the
        # handler so it can write log_filename onto entity rows
        # (dishes.last_run_log_filename etc).
        job_with_log = dict(job)
        job_with_log["log_filename"] = log_filename
        result = await handler(job_with_log)

        with sqlite3.connect(db_path) as conn:
            mark_finished(conn, job["id"], status="success", result=result)
        print(f"=== Job #{job['id']} success ===")
    except Exception as e:
        traceback.print_exc()
        with sqlite3.connect(db_path) as conn:
            mark_finished(
                conn, job["id"],
                status="error",
                error_detail=f"{type(e).__name__}: {e}",
            )
        print(f"=== Job #{job['id']} ERROR: {type(e).__name__}: {e} ===")
    finally:
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        try:
            log_file.close()
        except Exception:
            pass


async def runner_loop(db_path: str, log_dir: Path, *,
                      poll_interval: float = 2.0,
                      stop_event: Optional[asyncio.Event] = None) -> None:
    """Background asyncio task. Polls the jobs table every poll_interval
    seconds for the next ready job, runs it, repeats. Self-restarts on
    its own exceptions so one bad job doesn't kill the loop."""
    print(f"[JOB-RUNNER] started (poll_interval={poll_interval}s, log_dir={log_dir})")
    while True:
        if stop_event is not None and stop_event.is_set():
            print("[JOB-RUNNER] stop_event set; exiting loop")
            return
        try:
            with sqlite3.connect(db_path) as conn:
                job = find_next_ready(conn)
            if job is None:
                await asyncio.sleep(poll_interval)
                continue
            await _run_one_job(job, db_path, log_dir)
        except Exception as e:
            # Catch-all so a bug in find_next_ready / connect / etc.
            # doesn't take the runner down. Log and back off briefly.
            print(f"[JOB-RUNNER] loop exception ({type(e).__name__}): {e}")
            traceback.print_exc()
            await asyncio.sleep(poll_interval * 2)
