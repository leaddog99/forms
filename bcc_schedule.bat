@echo off
setlocal

REM ===================================================================
REM  bcc_schedule.bat — the unattended dish-refresh tick.
REM
REM  Runs `python -m jobs schedule`, which examines every dish's
REM  next_run_at (= last_refreshed + refresh_ttl_days) and refreshes the
REM  ones that are DUE, then exits. When nothing is due it's a cheap
REM  read + exit, so a frequent tick costs almost nothing — real work
REM  (SerpAPI -> Moz -> extract -> save) only fires when a dish crosses
REM  its TTL.
REM
REM  Wire this into Windows Task Scheduler on an interval (see
REM  docs/jobs-as-executables.md §9-A). Set the task's "Stop the task if
REM  it runs longer than" so a hung fetch can't run forever (§6.5).
REM
REM  Out-of-process by design: this never touches the uvicorn event loop
REM  (memory/project_job_runner_disabled.md). recipes.db is WAL, so this
REM  process and the server can write concurrently.
REM
REM  Exit code is propagated from `python -m jobs schedule` (0 = all due
REM  dishes succeeded / none due; 1 = at least one errored) so Task
REM  Scheduler surfaces pass/fail in its Last Run Result column.
REM ===================================================================

set "VENV=C:\Users\john\PyCharm\venv"
set "PROJECT=C:\Users\john\PycharmProjects\forms"

cd /d "%PROJECT%"
call "%VENV%\Scripts\activate.bat"

REM UTF-8 + unbuffered so prints don't crash on the Windows console and
REM the log updates in near-real-time when tailed (same as bcc_start.bat).
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

echo. >> jobs_schedule.log
echo ==================== schedule tick %DATE% %TIME% ==================== >> jobs_schedule.log
python -m jobs schedule >> jobs_schedule.log 2>&1
set "RC=%ERRORLEVEL%"
echo ---- exit %RC% (%DATE% %TIME%) ---- >> jobs_schedule.log

endlocal & exit /b %RC%
