# Jobs as Executables — Design Note

Status: **design, not built.** Worked out in discussion on 2026-06-04.
No code has changed. Two decisions are still open (§7).

Audience: anyone about to touch `input/pipeline/jobs.py`,
`scripts/run_next_job.py`, the `/jobs/*` endpoints in `save_recipe_api.py`,
or the dishes-form Run button.

This note pivots the job system from the original "in-process asyncio
scheduler" plan (see memory `job-system`, Layer 2) toward **each run being
its own command-line executable** — schedulable from Task Scheduler/cron or
run ad hoc — that writes a **reviewable log into the DB**. The pivot
*simplifies* the architecture rather than complicating it; §2 explains why.

---

## 1. What exists today

The bones are already here — we are extending, not rebuilding.

- **`jobs` table** (`input/pipeline/jobs.py:41`) — durable queue + audit
  trail. One row per job: `id`, `type`, `params` (JSON), `entity_ref`
  (`"dish:Pastitcio (Greece)"`), `status`
  (`queued|running|success|error|cancelled`), `scheduled_at`,
  `created_at`, `started_at`, `finished_at`, `log_filename`, `result`
  (JSON), `error_detail`.
- **`_run_one_job`** (`jobs.py:290`) — the one canonical path: open log
  file → tee stdout/stderr to it → `mark_running` → call the registered
  handler → `mark_finished` with result/error → restore stdout. Every run,
  whoever triggers it, goes through here.
- **Handler registry** (`jobs.py:240`) — `register_handler("dish_refresh",
  fn)`. Handlers are `async fn(job) -> dict|None`.
- **`scripts/run_next_job.py`** — *already* a standalone CLI that imports
  the handlers (importing `save_recipe_api` registers them without starting
  a server) and runs jobs via the exact `_run_one_job` path. This is the
  proof-of-concept that runs can happen **out of process, no server**.
- **Triggers today**: the dishes form `POST /dishes/{name}/refresh`
  enqueues a row (`save_recipe_api.py:2979`); `POST /jobs/run-queued`
  (`save_recipe_api.py:3180`) drains the backlog on demand from inside the
  server; an SSE endpoint tails the **log file** for the live panel.

What's missing vs. the vision:
1. A command that *is* a run — enqueue + execute + exit in one shot with
   params on the command line.
2. Logs that live in the **DB**, not scattered files under `logs/`.
3. A first-class scheduling story.

---

## 2. The insight: out-of-process dissolves the scars

The current design carries three scars. Moving execution into a separate
process removes all three:

1. **The poll-runner is disabled** (memory `job-runner-disabled`) because a
   2s blocking `sqlite3.connect` poll on the asyncio event loop stalled
   request handling. A separate process never touches the server's event
   loop → **the server stops being the thing that blocks.**
2. **`_run_one_job` had to be serial** because the stdout-tee
   (`jobs.py:248`) is process-global; concurrency would interleave logs. A
   per-run process owns its own stdout → no interleave → **the
   serialization constraint evaporates.** Parallel runs become free.
3. **Cloudflare's 100s origin timeout** — the original reason the whole job
   system exists — is irrelevant to a CLI process. No HTTP in the loop.

So the user's instinct (each run = its own executable) is not a detour from
the job system; it's the cleaner deployment the job system was reaching for.

---

## 3. Target architecture — one canonical path, two triggers

Per memory `single-path`: **not** a parallel pipeline. The CLI and the form
funnel through ONE entrypoint.

```
python -m jobs run dish_refresh --dish "Pastitcio (Greece)" --top-n-final 10
```

That entrypoint:
1. Writes a `jobs` row from the CLI params — so there is **always** a
   durable record, and `find_in_flight_for_entity` (`jobs.py:218`) still
   guards against a double-run of the same entity.
2. Runs it inline via the existing `_run_one_job`.
3. **Exits 0 on success / 1 on error**, read from the terminal status — so
   Task Scheduler / cron sees pass/fail and surfaces it in their own
   last-run-result column.

The two triggers, same executable underneath:

- **Ad hoc** — you type the command.
- **Scheduled** — Task Scheduler's action *is* that command (per dish), or
  one `python -m jobs drain --all` task on an interval that picks up
  whatever the form enqueued. This is the cron-equivalent that retires the
  in-process Layer-2 scheduler from memory `job-system`.
- **Form Run button** — keeps enqueuing, but the server now **spawns the
  CLI as a subprocess** (never runs the handler inline) and the live panel
  tails the log. "I clicked Run and want to watch it now" still works; the
  server just never does the heavy work.

`scripts/run_next_job.py` is the seed of `python -m jobs`; this is mostly
promoting it to a first-class module entrypoint with a `run <type>
--param` subcommand alongside the existing `drain`/`list`.

### 3.1 CLI args carry dish IDENTITY, never the SERP query

A hard rule for the entrypoint: **the command line passes the dish name/id,
not its Google query.** The verbatim SERP queries deliberately carry embedded
straight quotes — `"banana bread" | "banana nut bread"` (the curly-quote bug,
2026-06-02; Google only honors straight `"`). If such a string were ever an
argv argument, the shell would consume/mangle the embedded quotes before our
code saw them, and PowerShell vs cmd vs cron quote-handling all differ — the
query would silently run loose, re-admitting exactly the junk the quoting was
added to exclude.

The design sidesteps this entirely: `--dish "Pastitcio (Greece)"` passes the
dish *identity*; the handler reads that dish's row and gets `google_query`
(quotes intact) straight from the DB. **The query never traverses the shell.**
The only quoting on the command line is around the dish name, which contains no
embedded quotes. Keep it that way — never add a `--query` flag that puts a SERP
string on argv; if a one-off query is ever needed, route it through a DB field
or a file, not the command line.

### 3.2 The dish-level scheduler (a third trigger, same executable)

Requirement (2026-06-08): keep running jobs from the **dish UI Run button**
*and* add a **scheduler that fires dishes by a per-dish next-run date**. These
are not two systems — they are the ad-hoc and unattended triggers of the one
executable from §3.

The scheduling primitives already exist in `input/pipeline/dishes.py`:
- `refresh_ttl_days` per dish (NULL = manual-only) — the cadence.
- `last_refreshed` — the anchor.
- `is_due(ttl, last_refreshed)` — derived due-ness.
- `next_run_at(ttl, last_refreshed)` — **derived** ISO date the dish is next
  due (added 2026-06-08; mirrors `is_due`, surfaced in `row_to_dict` as
  `next_run_at`). NOT stored → can't drift from the cadence. Manual pinning
  ("run next Tuesday regardless") would be a later opt-in `next_run_override`
  column; deferred until actually wanted.
- `find_due_dishes(conn)` — returns due dishes, ordered, ready to drive a run.
- index on `refresh_ttl_days` so the scan is cheap.

What's missing is only the **executable** (the docstring's long-referenced but
never-written `refresh_due_dishes.py`):

```
python -m jobs schedule           # examine every dish's next_run_at; run the due ones
```

It calls `find_due_dishes`, and for each due dish runs `dish_refresh` through
the **same `_run_one_job` path** as the Run button (entity-locked via
`find_in_flight_for_entity`, so a manual run and the scheduler can't double-fire
the same dish). A successful run stamps `last_refreshed = now`, which rolls
`next_run_at` forward automatically. Task Scheduler fires `python -m jobs
schedule` on an interval (hourly/daily); each due dish becomes its own run.

The three triggers, one executable, one canonical path:
- **Dish Run button** — interactive; server spawns the CLI subprocess, panel
  tails the live log. Stays exactly as the user knows it.
- **Scheduler** — `python -m jobs schedule` examines `next_run_at`, runs due
  dishes unattended.
- **Ad hoc** — `python -m jobs run dish_refresh --dish "…"` typed by hand.

### 3.3 Live + latest log from the dish across process boundaries

Requirement (2026-06-08): from the dish UI, **watch the log in real time while
a run is in progress**, and **pull the latest saved log from the dish anytime**.

Both already work today (the `#dishLogPanel`, 2026-06-03: live SSE tail during a
run + the latest saved log via `dishes.last_run_log_filename` →
`/logs/<file>`). The catch is that today's live tail works *because the job runs
inside the server*, writing a file the server tails. Once the job is a separate
process (§3's executable, the scheduler, or an unattended Task Scheduler run),
the live tail needs a **cross-process channel** — and a file is the brittle one
(server must know the live path + share the FS; partial-line reads; a run nobody
watched leaves only a loose file). **This requirement is what decides §7.1 →
L1**: the DB is the clean cross-process channel. The job process writes
`job_logs(job_id, seq, …)` rows (WAL — now enabled — makes the concurrent writes
safe); the dish UI's SSE reads `WHERE job_id=? AND seq>last`, and "latest log"
is `WHERE job_id = dishes.last_job_id`. A 3am scheduled refresh nobody watched is
then fully reviewable from the dish afterward, and rides the `.sql` backup. The
dish gains a `last_job_id` pointer (alongside or replacing `last_run_log_filename`)
so the UI can find both the live run (via `find_in_flight_for_entity`) and the
last completed one.

---

## 4. Handler = pipeline OR agent

The load-bearing framing from the 2026-06-04 discussion. The question was:
"aren't dish jobs basically AI agents — should we use agent architecture?"

**A dish job is a *pipeline that calls models*, not an *agent*.**

- **Pipeline** = fixed control flow (search → score → translate → extract →
  save); models are *steps*. Deterministic, reproducible, cheap,
  debuggable, **schedulable**. `dish_refresh` is exactly this — the
  KEEP/DROP funnel is deterministic filtering, not reasoning.
- **Agent** = an LLM *in the loop holding the steering wheel* — it picks
  the next tool and decides when it's done. Powerful, but nondeterministic,
  pricier, harder to audit, and awkward to put behind a cron job.

The CLI-executable + schedule + log-to-DB direction is **batch/pipeline
thinking, and that's correct for `dish_refresh`** — you want a Pastitsio
run to come out the same way twice and to be reviewable in a logs table. An
agent loop fights both of those. So **we do not make `dish_refresh` an
agent.**

Where agent architecture *does* earn its place — inside this world, not
instead of it:

1. **Messy-leaf recovery** — deciding "try wayback / AMP url / printable
   view" when one extraction fails. Note the funnel *already encodes these
   fallbacks deterministically* (wayback snapshots, json-ld vs xlate,
   length-ratio guard), capturing most of the value without an agent's
   cost.
2. **Judgment calls** — "is this *really* the canonical Pastitsio?", cohort
   matching, the dish-variant tie-resolution in
   `docs/dish-variants-membership.md`. Genuine reasoning-with-tools tasks
   where an agent beats a heuristic.

**The synthesis: the job system is the substrate agents run on, not a rival
to them.** Memory `job-system` already anticipated this — *"future
agentic-AI workflows will trigger long-running tasks… they need a clean
job/poll API."* Concretely:

> **An agent is just a job whose handler happens to be an LLM-in-a-loop.**

The durable row, the executable wrapper, the log table, the schedule, the
entity-lock — all shared, whether `JOB_HANDLERS[type]` is a deterministic
pipeline (`dish_refresh`) or an agent (a future `curate_dish`,
`resolve_variant`, `recover_extraction`). Adding an agent is adding a job
*type*, nothing more. The executable/logs/schedule architecture is the
right call **regardless of how agentic any single handler is** — it's the
layer underneath both.

---

## 5. Logs in the DB — OPEN DECISION (§7.1)

Today logs are files under `logs/job_<type>_<id>_<ts>.log` with a
`log_filename` pointer on the job row; the SSE endpoint tails the file. The
goal is a log **reviewable in the DB** and carried in the `recipes.sql`
dump backup (memory `db-backup`) instead of scattered, un-backed-up files.

Two shapes (pick in §7):

- **L1 — line-grained** `job_logs(job_id, seq, ts, stream, line)`. Fully
  queryable; the SSE live tail reads **from the DB** (`WHERE job_id=? AND
  seq > last`), retiring the file tail; rides the `.sql` dump. Cost: a
  write per log line — mitigated by WAL (§6) + batching the flush (every
  ~1s or N lines). Best if the DB is to be the single source of truth for
  logs.
- **L2 — one blob per job**. Store the full captured text as one TEXT
  column (on `jobs.result_log` or a `job_logs(job_id, text)` row), written
  on completion; keep the file only for the live tail *during* the run.
  Simplest, least disruption, still reviewable in the DB after the fact.

Recommendation: **L1** if we want files gone and the DB authoritative; L2
if we want minimal disruption now and can revisit. Leaning L1 because it
also folds the live-tail and the backup story into one mechanism.

---

## 6. Hard prerequisite: WAL

`recipes.db` is currently `journal_mode=delete`, `busy_timeout=5000`
(verified 2026-06-04). The moment a job *process* writes while the server
*also* writes, two writers on a non-WAL DB collide and you get "database is
locked" past the 5s timeout. **Out-of-process execution requires switching
`recipes.db` to WAL first** — non-negotiable, not a nice-to-have. `recipes.db`
is local and the backup is the `.sql` dump + ADAM disk (memory `db-backup`),
so WAL's side files are not a backup concern here. This also makes the
L1 per-line log writes safe under concurrency.

---

## 6.5 Job timeout + warning (requirement, 2026-06-04)

A job must not be able to run unbounded with nobody noticing. Today there is
**no timeout and no alert** — job #93 happened to finish in ~26 min, but a
single un-timeout'd fetch (we logged a wayback 10s read-timeout; other `.gr`
sources can hang) could leave a job `running` forever, and the operator has
no signal. Two layers:

1. **Hard per-job deadline.** Each job carries a max wall-clock (per type, or
   a `timeout_seconds` column with a default). On expiry the run is killed and
   marked `error: timed out after Ns`. In the per-run-executable model this is
   clean: a watchdog (or the OS — Task Scheduler "Stop the task if it runs
   longer than…") bounds the child process; in-process it needs an
   `asyncio.wait_for` around the handler. Every external fetch the handler
   makes must also carry its own request timeout so the deadline is reachable.
2. **Warning when it matters.** Emit a notification on (a) timeout/error and
   (b) optionally a soft threshold ("still running after N min"). Channel TBD —
   log marker + a row the dishes form already polls is the cheap first cut; a
   push/email is the richer version. The crash-recovery reset
   (`reset_interrupted_jobs`, `jobs.py:80`) covers process death but NOT a live
   hang — the timeout is the missing half.

This interacts with §7.2: a scheduled Task Scheduler run gets the OS-level
"stop if longer than" for free; an in-server drain needs the `wait_for`
wrapper. Decide alongside the execution-trigger fork.

## 7. Open decisions

1. **Logs table shape** — **DECIDED 2026-06-08: L1** (line-grained
   `job_logs(job_id, seq, ts, stream, line)`). Forced by the §3.3
   requirement: live tail in the dish UI while the job runs *out of process* +
   latest-log-from-the-dish for unattended scheduled runs. The DB is the clean
   cross-process streaming channel (WAL-safe); files are the brittle path once
   jobs leave the server. SSE reads `WHERE job_id=? AND seq>last`; the write is
   batched (~1s / N lines).
2. **Execution trigger** — **DECIDED 2026-06-08: three triggers, one
   executable** (was option (a), now generalized by §3.2). The dish Run button
   spawns the CLI subprocess (interactive, live panel); `python -m jobs
   schedule` runs due dishes unattended off `next_run_at` (Task Scheduler on an
   interval); `python -m jobs run …` is the ad-hoc hand-typed form. All three
   funnel through `_run_one_job`, entity-locked.

### 7.3 Still open

- **Server-spawn vs. daemon** — should the dish Run button shell out a fresh
  `python -m jobs run` per click (simple, cold-start per run), or should a
  single long-lived **worker daemon** own all execution while the Run button /
  scheduler merely enqueue? See §9.

---

## 8. Phasing

1. **WAL migration** (§6) — **DONE 2026-06-08** (`init_db` sets
   `PRAGMA journal_mode=WAL`; persistent in the header, inherited by every
   process).
2. **`python -m jobs run <type> --param`** — promote `run_next_job.py` to a
   module entrypoint with enqueue+run+exit-code. Pure addition. (§3.1: args =
   identity only, never the query.)
3. **L1 logs table** (§7.1) — `job_logs(job_id, seq, ts, stream, line)`; write
   path (batched) in `_run_one_job`, SSE read path `WHERE job_id=? AND
   seq>last`; add `dishes.last_job_id`. Keep file-write during transition
   (no-silent-removal).
4. **Form Run → subprocess spawn** — replace the inline `/jobs/run-queued`
   drain with a `Popen` of the executable; dish panel tails the L1 log.
5. **Dish scheduler** — `python -m jobs schedule` over `find_due_dishes`
   (`next_run_at` already derived + surfaced, 2026-06-08); Task Scheduler fires
   it on an interval (cron-tick model, §9-A). Document the exact
   `venv\Scripts\python -m jobs …` command line. Surface `next_run_at` in the
   dish UI.
6. **(Later, optional)** `python -m jobs worker` daemon (§9-B) if sub-minute
   pickup or central pause/kill is wanted.

Nothing here removes a working capability without the on-demand drain
staying available throughout (memory `no-silent-removal`).

---

## 9. "Continually looking for something to do" — cron tick vs. daemon

Asked 2026-06-08: *do we have a daemon that continually looks for work?*

**Today: no — and on purpose.** The old in-process poll-runner (a 2s queue
poll) is disabled (memory `job-runner-disabled`) because its blocking
`sqlite3.connect` ran on the server's asyncio event loop and stalled requests.
Nothing currently looks for work on its own; jobs run only on an explicit
trigger (`/jobs/run-queued` drain, `run_next_job.py`). **The event-loop reason
that banned a daemon evaporates once execution is its own process (§2) — so a
daemon is viable again, it just must live OUTSIDE uvicorn.** WAL (§6, now on)
makes its concurrent writes safe.

Two ways to provide "continual":

- **A — External cron tick (no daemon).** Task Scheduler IS the watcher: it
  fires `python -m jobs schedule` every N minutes; that process reads each
  dish's `next_run_at`, runs the due ones, exits. "Continual" = the interval.
  ✅ nothing to keep alive, no event-loop risk, survives reboot for free, dead
  simple. ❌ pickup granularity = the tick; no single home for kill/pause/
  concurrency controls.
- **B — Worker daemon.** One long-lived out-of-process loop: check queue + due
  dishes → run → repeat. This is literally "continually looking for something
  to do." ✅ instant pickup, warm process, the natural home for a concurrency
  cap + queue kill/pause. ❌ a process to keep alive (Task Scheduler "restart
  if it stops", NSSM, or a Windows service) + must be crash-resilient.

**Recommendation: ship A, design for B as a drop-in.** The Run button already
covers instant *interactive* runs; the scheduler only needs "sometime soon,"
which a 1–5 min tick satisfies without babysitting a daemon. Keep the loop body
(`find_due_dishes` → `_run_one_job`) trigger-agnostic so promoting it to
`python -m jobs worker` later is a wrapper, not a queue redesign. Adopt B when
sub-minute pickup or central pause/kill is actually wanted.
