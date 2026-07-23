"""`python -m jobs` — out-of-process job runner CLI.

Subcommands:
  run <type> --dish "NAME"      enqueue a fresh job + run it + exit 0/1
  run <type> --param k=v ...    same, with arbitrary params (generic types)
  schedule                      run every dish whose next_run_at is due
  next                          run the oldest queued job (no enqueue)
  drain                         run every queued job, oldest first
  list                          list jobs (default: queued), run nothing

Design notes (docs/jobs-as-executables.md):
  - Importing `save_recipe_api` loads .env and REGISTERS the handlers
    (`register_handler("dish_refresh", ...)`) at module-import time. The
    FastAPI startup event only fires under uvicorn, so this import does NOT
    start a server — we just borrow the handler registry + the canonical
    DB_PATH / LOGS_DIR so our view of the world matches the server's.
  - Every run goes through `jobs_lib._run_one_job`, identical to the form's
    Run button: per-job log capture, status transitions, result recording.
  - §3.1: CLI args carry dish IDENTITY (--dish NAME), never the SERP query.
    The handler reads the dish's `queries` (with their embedded straight
    quotes) from the DB, so shell quoting can never mangle them. Do NOT add a
    --query flag that puts a SERP string on argv.
  - Exit code: 0 if the run(s) succeeded, 1 if any ended in error or was
    refused — so Task Scheduler / cron surface pass/fail in their own last-run
    column.

Examples:
  python -m jobs run dish_refresh --dish "Pastitcio (Greece)"
  python -m jobs schedule
  python -m jobs list --status running
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from input.pipeline.db import connect as _connect  # WAL busy_timeout — input/pipeline/db.py
import sys
from pathlib import Path

# Make the project root importable no matter where we're invoked from (the
# jobs/ package sits directly under it). Mirrors scripts/run_next_job.py.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# We are a WORKER, not the server. Importing save_recipe_api runs its init_db,
# which calls reset_interrupted_jobs — that would wipe the status of any job
# currently running in ANOTHER process to 'error'. Only the uvicorn server should
# do that cleanup (on its own startup, for jobs orphaned by a dead server). Flag it
# off for the whole jobs CLI BEFORE the import below.
import os  # noqa: E402
os.environ["BCC_SKIP_JOB_RESET"] = "1"

# Importing this registers the handlers and gives us the canonical paths the
# server uses. No server is started (no uvicorn → no startup event).
import save_recipe_api as api  # noqa: E402
from input.pipeline import jobs as jobs_lib  # noqa: E402
from input.pipeline import dishes as dishes_lib  # noqa: E402
from input.pipeline import system_config as cfg  # noqa: E402


# ============================================================
#  Shared helpers
# ============================================================

def _print_jobs(rows: list[dict]) -> None:
    if not rows:
        print("  (none)")
        return
    for j in rows:
        print(f"  #{j['id']:>4}  {j['type']:<14}  {j['status']:<9}  "
              f"{j.get('entity_ref') or ''}")


def _exit_code_for(job: dict | None) -> int:
    """0 when the job ended success, 1 otherwise (error/missing/non-terminal)."""
    return 0 if (job and job.get("status") == "success") else 1


def _run_job_id(job_id: int) -> int:
    """Run one already-enqueued job through the canonical path and return its
    exit code. Re-fetches the row so the handler sees current params."""
    with _connect(api.DB_PATH) as conn:
        job = jobs_lib.get_job(conn, job_id)
    if job is None:
        print(f"Job #{job_id} not found.")
        return 1
    if job["status"] not in ("queued", "running"):
        print(f"Job #{job_id} is already terminal (status={job['status']!r}); "
              f"refusing to re-run.")
        return 1

    asyncio.run(jobs_lib._run_one_job(job, api.DB_PATH, api.LOGS_DIR))

    with _connect(api.DB_PATH) as conn:
        final = jobs_lib.get_job(conn, job_id)
    if final:
        tail = ""
        if final["status"] == "error" and final.get("error_detail"):
            tail = f"  — {final['error_detail']}"
        elif final["status"] == "success" and final.get("result"):
            tail = f"  — {final['result']}"
        print(f"  #{final['id']}  {final['status']}{tail}")
        log = final.get("log_filename")
        if log:
            print(f"  log: {api.LOGS_DIR / log}  (UI: /logs/{log})")
    return _exit_code_for(final)


def _enqueue_dish_refresh(name: str) -> tuple[int | None, str | None]:
    """Resolve a dish to its canonical name, guard against a double-run, and
    enqueue a dish_refresh. Returns (job_id, None) on success, or
    (None, reason) when not found / already in flight."""
    with _connect(api.DB_PATH) as conn:
        dish = dishes_lib.get_dish(conn, name)
        if dish is None:
            return None, f"Dish {name!r} not found."
        if not dish.get("queries"):
            return None, f"Dish {dish['name']!r} has no queries."
        canonical = dish["name"]
        entity_ref = f"dish:{canonical}"
        existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
        if existing:
            return None, (f"Dish {canonical!r} already has job #{existing['id']} "
                          f"{existing['status']} — not double-running.")
        job_id = jobs_lib.enqueue_job(
            conn,
            type="dish_refresh",
            params={"dish_name": canonical},
            entity_ref=entity_ref,
        )
    return job_id, None


# ============================================================
#  Subcommands
# ============================================================

def cmd_run(args: argparse.Namespace) -> int:
    """Enqueue a fresh job and run it. --dish is sugar for the dish_* types
    (sets params.dish_name + entity_ref so the in-flight guard works and the
    handler can read the dish's queries from the DB)."""
    job_type = args.type

    # Dish convenience path: identity-only, entity-locked, canonical-resolved.
    if args.dish is not None:
        job_id, reason = _enqueue_dish_refresh(args.dish)
        if job_id is None:
            print(reason)
            return 1
        print(f"Enqueued job #{job_id} ({job_type}) for dish {args.dish!r}.")
        print("-" * 60)
        return _run_job_id(job_id)

    # Generic path: arbitrary --param k=v + optional --entity-ref.
    params: dict = {}
    for kv in (args.param or []):
        if "=" not in kv:
            print(f"--param must be key=value, got {kv!r}")
            return 1
        k, v = kv.split("=", 1)
        params[k] = v
    entity_ref = args.entity_ref
    with _connect(api.DB_PATH) as conn:
        if entity_ref:
            existing = jobs_lib.find_in_flight_for_entity(conn, entity_ref)
            if existing:
                print(f"Entity {entity_ref!r} already has job #{existing['id']} "
                      f"{existing['status']} — not double-running.")
                return 1
        job_id = jobs_lib.enqueue_job(
            conn, type=job_type, params=params, entity_ref=entity_ref)
    print(f"Enqueued job #{job_id} ({job_type}) params={params}.")
    print("-" * 60)
    return _run_job_id(job_id)


def cmd_schedule(args: argparse.Namespace) -> int:
    """Run every dish whose auto-refresh is due (next_run_at elapsed). The
    cron-tick model (doc §9-A): the OS heartbeat fires this hourly; the actual
    cadence + on/off live in the DB system config (memory/project_system_config),
    NOT in Task Scheduler. Entity-locked, so a dish already running (e.g. a manual
    Run in progress) is skipped, not double-fired. Exit 1 if any due dish errored.

    Config gates (bypass both with --force):
      scheduler_enabled        master on/off
      scheduler_interval_hours min hours between real passes (vs scheduler_last_tick_at)
    """
    if not args.force and not args.dry_run:
        if not cfg.get_setting("scheduler_enabled", True):
            print("Scheduler disabled in system config (scheduler_enabled=false). "
                  "Nothing to do.")
            return 0
        interval_h = cfg.get_setting("scheduler_interval_hours", 6)
        last = cfg.get_setting("scheduler_last_tick_at", None)
        if last and interval_h:
            try:
                from datetime import datetime, timezone
                last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                elapsed_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
                if elapsed_h < float(interval_h):
                    print(f"Last pass {elapsed_h:.1f}h ago; interval is {interval_h}h. "
                          f"Skipping (use --force to override).")
                    return 0
            except Exception as e:
                print(f"[schedule] interval check failed ({e}); proceeding.")

    # Stamp the pass time up front so overlapping heartbeats honor the interval
    # even while this pass is still running. Not for a dry-run preview.
    if not args.dry_run:
        from datetime import datetime, timezone
        with _connect(api.DB_PATH) as conn:
            cfg.set_setting(conn, "scheduler_last_tick_at",
                            datetime.now(timezone.utc).isoformat())

    with _connect(api.DB_PATH) as conn:
        due = dishes_lib.find_due_dishes(conn)

    worst = 0
    if due:
        print(f"{len(due)} dish(es) due:")
        for d in due:
            print(f"  - {d['name']}  (last_refreshed={d.get('last_refreshed')}, "
                  f"ttl={d.get('refresh_ttl_days')}d, next_run_at={d.get('next_run_at')})")
        print("-" * 60)
        for d in due:
            name = d["name"]
            if args.dry_run:
                print(f"[dry-run] would refresh {name!r}")
                continue
            job_id, reason = _enqueue_dish_refresh(name)
            if job_id is None:
                # Already in flight (manual run) or vanished — skip, not a failure.
                print(f"Skipping {name!r}: {reason}")
                continue
            print(f"Enqueued job #{job_id} for {name!r}; running...")
            worst = max(worst, _run_job_id(job_id))
    else:
        print("No dishes due.")

    # Recurring scheduled jobs (DB-resident registry, editable in the a/c/d Jobs
    # editor), AFTER the dish pass so data-dependent rollups see fresh data.
    if not args.dry_run:
        worst = max(worst, _run_due_scheduled_jobs())
    return worst


def _run_due_scheduled_jobs() -> int:
    """Run every enabled scheduled_jobs row whose interval has elapsed. Each goes
    through the canonical job path; last_run_at is stamped UP FRONT (so an
    overlapping heartbeat honors the interval) then the status is recorded after.
    Returns the worst exit code."""
    from input.pipeline import scheduled_jobs as sched
    with _connect(api.DB_PATH) as conn:
        due = sched.find_due_scheduled_jobs(conn)
    worst = 0
    for j in due:
        name = j["name"]
        with _connect(api.DB_PATH) as conn:
            job_id = jobs_lib.enqueue_job(
                conn, type=j["job_type"], params=j.get("params") or {},
                entity_ref=f"scheduled:{name}")
            sched.record_run(conn, name, status="running", job_id=job_id)
        print(f"Running scheduled job {name!r} (#{job_id}, type={j['job_type']})...")
        rc = _run_job_id(job_id)
        worst = max(worst, rc)
        with _connect(api.DB_PATH) as conn:
            run = jobs_lib.get_job(conn, job_id) or {}
            sched.record_run(conn, name,
                             status=run.get("status") or ("error" if rc else "success"),
                             job_id=job_id)
    return worst


def cmd_exec(args: argparse.Namespace) -> int:
    """Run an ALREADY-enqueued job by id, out-of-process. This is what the
    server's POST /jobs/{id}/spawn launches so a UI Refresh runs off the uvicorn
    event loop — the enqueue already happened in-request; we just execute it.

    STDERR is captured to logs/job_<id>.stderr.log for the duration: the server
    spawns us with stderr=DEVNULL, so an uncaught exception (outside the runner's
    own try/except) or a faulthandler traceback (segfault) would otherwise vanish —
    the reason a 'failed' job can show an empty error_detail. The file is deleted on
    a clean exit, so it only survives when there's an actual crash to read."""
    import faulthandler
    err_path = api.LOGS_DIR / f"job_{args.job_id}.stderr.log"
    prev_stderr = sys.stderr
    f = None
    try:
        api.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        f = open(err_path, "w", encoding="utf-8", errors="replace")
        sys.stderr = f
        faulthandler.enable(file=f)          # segfault/fatal-signal traceback → same file
    except Exception:
        f = None
    try:
        return _run_job_id(args.job_id)
    except BaseException:
        # An exception propagating PAST here would otherwise be printed by Python's
        # excepthook AFTER the finally restores sys.stderr (→ back to DEVNULL) and the
        # empty file is deleted — losing the traceback. Write it to the file now.
        if f is not None:
            try:
                import traceback
                traceback.print_exc(file=f)
                f.flush()
            except Exception:
                pass
        raise
    finally:
        sys.stderr = prev_stderr
        if f is not None:
            try:
                empty = f.tell() == 0
                f.close()
                if empty and err_path.exists():
                    err_path.unlink()        # clean run → no clutter; keep only real crashes
            except Exception:
                pass


def cmd_next(args: argparse.Namespace) -> int:
    """Run the single oldest ready queued job (does not enqueue)."""
    with _connect(api.DB_PATH) as conn:
        nxt = jobs_lib.find_next_ready(conn)
    if nxt is None:
        print("No queued jobs ready. Nothing to do.")
        return 0
    print(f"Running oldest queued job #{nxt['id']} ({nxt['type']}).")
    print("-" * 60)
    return _run_job_id(nxt["id"])


def cmd_drain(args: argparse.Namespace) -> int:
    """Run every queued job, oldest first. Exit 1 if any errored."""
    with _connect(api.DB_PATH) as conn:
        queued = jobs_lib.list_jobs(conn, status="queued", limit=1000)
    queued.sort(key=lambda j: j["created_at"])
    if not queued:
        print("No queued jobs. Nothing to do.")
        return 0
    print(f"Draining {len(queued)} queued job(s):")
    _print_jobs(queued)
    print("-" * 60)
    worst = 0
    for j in queued:
        worst = max(worst, _run_job_id(j["id"]))
    return worst


def cmd_list(args: argparse.Namespace) -> int:
    """List jobs and exit. Default filters to queued; --status '' for all."""
    status = None if args.status == "" else (args.status or "queued")
    with _connect(api.DB_PATH) as conn:
        rows = jobs_lib.list_jobs(conn, status=status, type=args.type, limit=args.limit)
    label = status or "all"
    print(f"{label} jobs ({len(rows)}):")
    _print_jobs(rows)
    return 0


# ============================================================
#  Argparse wiring
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m jobs", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="enqueue a fresh job and run it")
    p_run.add_argument("type", help="job type, e.g. dish_refresh")
    p_run.add_argument("--dish", default=None,
                       help='dish name/identity for dish_* types '
                            '(e.g. "Pastitcio (Greece)"). NEVER the SERP query.')
    p_run.add_argument("--param", action="append", metavar="KEY=VALUE",
                       help="generic param (repeatable); for non-dish types")
    p_run.add_argument("--entity-ref", default=None,
                       help="entity lock key for generic types (e.g. dish:Foo)")
    p_run.set_defaults(func=cmd_run)

    p_sched = sub.add_parser("schedule", help="run every dish whose next_run_at is due")
    p_sched.add_argument("--dry-run", action="store_true",
                         help="list what would run, run nothing")
    p_sched.add_argument("--force", action="store_true",
                         help="bypass scheduler_enabled + the interval gate")
    p_sched.set_defaults(func=cmd_schedule)

    p_exec = sub.add_parser("exec", help="run an already-enqueued job by id (used by the server)")
    p_exec.add_argument("--job-id", type=int, required=True)
    p_exec.set_defaults(func=cmd_exec)

    p_next = sub.add_parser("next", help="run the oldest queued job")
    p_next.set_defaults(func=cmd_next)

    p_drain = sub.add_parser("drain", help="run every queued job, oldest first")
    p_drain.set_defaults(func=cmd_drain)

    p_list = sub.add_parser("list", help="list jobs, run nothing")
    p_list.add_argument("--status", default="queued",
                        help="filter by status (default queued; '' for all)")
    p_list.add_argument("--type", default=None, help="filter by job type")
    p_list.add_argument("--limit", type=int, default=100)
    p_list.set_defaults(func=cmd_list)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
