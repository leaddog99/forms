# App playbook — how we build apps (distilled from the recipes app)

Written 2026-09-14 so a SECOND app (f2n, the face-to-name memory aid) can start
with the house style instead of rediscovering it. This is the reference the new
project's CLAUDE.md points at. Paths below are in `C:\Users\john\PycharmProjects\forms`.

## 1. The shape of an app

- **One FastAPI process, uvicorn, port-bound to 127.0.0.1.** Static HTML pages in a
  `forms/` folder are the UI; the API serves them and the JSON they call. No SPA
  framework, no build step. Vanilla JS, one shared CSS shell.
- **SQLite in WAL mode is the database**, one file, opened per request. Domain data
  lives in tables, never in code constants (code constants are SEED only). Derived
  values are persisted as columns with their method id and inputs — never
  computed on read.
- **Runs as a Windows service under NSSM** (`nssm install <NAME> <venv python>
  -m uvicorn app:app --host 127.0.0.1 --port <PORT>`, AppExit=Restart, stdout/err
  to files). Restarted by a self-elevating `*_restart.bat`. Reached from outside
  through a **Cloudflare tunnel** (token-run `cloudflared` service; hostnames are
  Published application routes on the tunnel, DNS follows). A **host gate** lets the
  same process serve a public hostname and an admin hostname differently.
- **Long work runs out of process** as `python -m jobs exec --job-id N` children
  spawned by the API; the `jobs` table is the queue and the child writes its own
  final status. The UI watches a per-job log over SSE. Cancel is cooperative.
- **Every LLM call goes through one gateway** (`llm.py`) that stamps an ambient
  context and journals tokens per operation and per user. Accounting is
  structural, not per call site.
- **Every tunable is a DB setting** (`system_config`: key, JSON value, type,
  category, label, description) with an admin editor; a JSON file is only the seed.

## 2. Conventions that cost us something to learn

Each of these is a memory file (`feedback_*.md`) with the incident behind it.
The one-line versions:

- **Single canonical path.** Convert exotic inputs to the canonical form; never a
  parallel pipeline. One chokepoint per external call (search, fetch, LLM).
- **Absent is not zero.** Uncomputed = NULL. Fix at the source. A real 0 must survive.
- **Persist derived values; materialize stored, not derived.** Keep the stored value;
  generate only where blank; print the rows a bulk UPDATE touches before running it.
- **No data in code.** Domain lists, categories, vocab: DB tables. Code seeds them once.
- **Page shell contract.** Every admin page loads `library-shell.css` (or
  `editor-shell.css`) then `tokens.css` LAST, uses the shared header; no page-local
  `:root`. Reuse the layout component; don't drift to per-page UI.
- **Info dots, not prose.** Field explanations sit behind an ⓘ (`showInfo`), never inline.
- **Unsaved state lives ON the save button.** Snapshot-diff dirty flag; prompt only on leave.
- **Delete = exclusion pattern.** Sub-item removal on delete-and-replace lists is a
  persistent `excluded` flag with an armed two-click.
- **No surprise file pickers.** Hide the button rather than mislabel it.
- **No vendor names to end users.** Redact on `is_staff`; fail closed.
- **i18n as we go.** New user-facing strings → `t(key)` + JSON catalogs from day one.
- **Editors are templates, not runtime.** Clone and specialize the editor page; no
  metadata-driven form engine.
- **Never silently remove a working capability.** Explicit approval first.
- **Deferred is not fixed.** Rerouting off a slow path ≠ fixing it; measure both paths.
- **Verify with runtime data.** Impact claims come from logs and rows, not code reads.
- **Research before design.** Look up the established pattern before a binary choice.
- **Don't pause mid-task.** Apply obvious fixes; ask only at real forks.
- **Lint before refactor.** ruff pre-commit (F821/F822/F811/E9) → tests → incremental carve.
- **Post-crash catch-up = `git diff` + `compileall` FIRST** (a stray keystroke once
  landed in line 1 of a module).
- **Update the state log.** Every substantial session ends with a Session log entry in
  the project's state file, a START HERE for next time, commit + push.
- **Use paragraphs.** Two or three real paragraphs beat bullet blobs; bullets only for
  genuinely enumerable things.

## 3. What to LIFT (copy, then let it diverge)

| piece | file(s) | what it gives you |
|---|---|---|
| auth + roles + permissions | `input/pipeline/auth.py` | role taxonomy, permission map in code, `resolve_user`, `_require_perm` gate, header identity with the master-user rule |
| host gate | `input/pipeline/host_gate.py` | public vs admin hostname served by one process |
| page shell | `forms/library-shell.css`, `forms/library-shell.js`, `forms/editor-shell.css`, `forms/tokens.css` | list-to-detail admin shell, sliding sidebar, action footer, iOS-safe, `showInfo`, tokens |
| jobs | `input/pipeline/jobs.py`, `jobs/__main__.py` | durable queue, out-of-process runner CLI, pid-aware reaper, cooperative cancel, per-job log files |
| LLM gateway | `llm.py` (+ `token_journal.py`, `docs/llm-gateway.md`) | one `create`/`stream` with ambient context and token journaling |
| system config | `input/pipeline/system_config.py` | DB-resident settings + cached `get_setting` + admin editor contract |
| curator alerts | `input/pipeline/alerts.py` | loud banner + email for losses a log line can't carry |
| logging | `log_config.json` | uvicorn log config with timestamps |
| ops kit | `backup_db.py`, `bcc_backup_scheduled.bat`, `bcc_restart.bat`, `bcc_sync_bailey.ps1` | verified nightly dump + offsite tiers, NSSM restart, warm-standby sync |
| ops docs | `docs/disaster-recovery.md`, `docs/host-stability-and-watchdog.md`, `docs/jobs-as-executables.md` | the why behind the kit |

Copy the file, rename the tables and the service name, delete what the new app
does not need. Do NOT import across repos and do NOT extract a shared package on
day one — extract only after both apps run and you can see what stayed identical
(`recipe-core` is the existing precedent for a sibling editable package).

## 4. What NOT to copy

- `save_recipe_api.py` as a shape. It is a 10k-line monolith; the new app starts
  with a package layout (`api/`, `pipeline/`, `forms/`) from the first commit.
- Anything recipe-shaped: `recipe_model.py`, the extract/enrich layers, dish and
  domain libraries, the harvest ladder, the product/commerce stack.
- The in-process job runner loop (`runner_loop`). It is intentionally OFF; the
  out-of-process CLI is the path.

## 5. Working across the two repos with Claude Code

- Launch in the new project with `claude --add-dir C:\Users\john\PycharmProjects\forms`
  (or `/add-dir` in-session) so the reference code is readable while writing the
  new one.
- The new project's CLAUDE.md names this document and the lift table.
- Auto-memory is per project directory. The generic `feedback_*` memories were
  copied into the new project's memory folder at setup; project memories were not.
