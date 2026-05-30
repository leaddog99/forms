"""Manually drain the jobs queue — the runner is intentionally disabled.

The background job runner (`runner_loop` in input/pipeline/jobs.py) is left
OFF in save_recipe_api.py: its 2s poll did a blocking sqlite3.connect on the
asyncio event loop and stalled request handling. So an enqueued job (e.g. a
dish refresh kicked off from the dishes form) sits in status='queued'
forever — nothing dispatches it. This script is the "invoke jobs manually"
half of that decision.

Importing save_recipe_api loads .env and registers the job handlers (e.g.
`dish_refresh`) at module-import time; the FastAPI startup event only fires
under uvicorn, so importing here does NOT start a server. We then run the
target job through the exact same `_run_one_job` path the runner would use,
so log capture, status transitions, and result recording are identical.

Run from anywhere — the project root is added to sys.path and the DB/log
paths are resolved off save_recipe_api's own module globals so they match
the server's view of the world.

Usage:
  python -m scripts.run_next_job                 # run the oldest queued job
  python -m scripts.run_next_job --job-id 46     # run one specific job
  python -m scripts.run_next_job --all           # drain every queued job, oldest first
  python -m scripts.run_next_job --list          # just show queued jobs, run nothing
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Importing this registers the handlers (register_handler("dish_refresh", ...))
# and gives us the canonical DB_PATH / LOGS_DIR the server uses.
import save_recipe_api as api  # noqa: E402
from input.pipeline import jobs as jobs_lib  # noqa: E402


def _print_jobs(rows: list[dict]) -> None:
    if not rows:
        print("  (none)")
        return
    for j in rows:
        print(f"  #{j['id']:>4}  {j['type']:<14}  {j['status']:<9}  "
              f"{j.get('entity_ref') or ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job-id", type=int, default=None,
                    help="Run this specific job id (any non-terminal status).")
    ap.add_argument("--all", action="store_true",
                    help="Drain every queued job, oldest first.")
    ap.add_argument("--list", action="store_true",
                    help="List queued jobs and exit without running anything.")
    args = ap.parse_args()

    db_path, log_dir = api.DB_PATH, api.LOGS_DIR

    if args.list:
        with sqlite3.connect(db_path) as conn:
            queued = jobs_lib.list_jobs(conn, status="queued", limit=100)
        print(f"Queued jobs ({len(queued)}):")
        _print_jobs(queued)
        return 0

    # Build the work list.
    to_run: list[dict] = []
    with sqlite3.connect(db_path) as conn:
        if args.job_id is not None:
            job = jobs_lib.get_job(conn, args.job_id)
            if job is None:
                print(f"Job #{args.job_id} not found.")
                return 1
            if job["status"] not in ("queued", "running"):
                print(f"Job #{args.job_id} is already terminal "
                      f"(status={job['status']!r}); refusing to re-run. "
                      f"Re-enqueue from the form if you want a fresh run.")
                return 1
            to_run = [job]
        elif args.all:
            # Snapshot the queued set; _run_one_job flips each to terminal,
            # so re-querying find_next_ready in a loop also works, but a
            # snapshot is clearer and avoids surprises if a handler enqueues
            # follow-on jobs.
            to_run = jobs_lib.list_jobs(conn, status="queued", limit=100)
            to_run.sort(key=lambda j: j["created_at"])  # oldest first
        else:
            nxt = jobs_lib.find_next_ready(conn)
            if nxt is None:
                print("No queued jobs ready. Nothing to do.")
                return 0
            to_run = [nxt]

    print(f"About to run {len(to_run)} job(s):")
    _print_jobs(to_run)
    print("-" * 60)

    async def _drain() -> None:
        for job in to_run:
            # Re-fetch fresh: in --all mode an earlier job may have changed
            # state, and we want _run_one_job to see current params.
            with sqlite3.connect(db_path) as conn:
                fresh = jobs_lib.get_job(conn, job["id"])
            if fresh is None or fresh["status"] not in ("queued", "running"):
                print(f"Skipping #{job['id']} — status now "
                      f"{fresh['status'] if fresh else 'missing'!r}")
                continue
            await jobs_lib._run_one_job(fresh, db_path, log_dir)

    asyncio.run(_drain())

    # Report final statuses.
    print("-" * 60)
    print("Final statuses:")
    with sqlite3.connect(db_path) as conn:
        for job in to_run:
            j = jobs_lib.get_job(conn, job["id"])
            if j is None:
                continue
            tail = ""
            if j["status"] == "error" and j.get("error_detail"):
                tail = f"  — {j['error_detail']}"
            elif j["status"] == "success" and j.get("result"):
                tail = f"  — {j['result']}"
            print(f"  #{j['id']}  {j['status']}{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
