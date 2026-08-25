@echo off
REM ====================================================================
REM bcc_backup_scheduled.bat — NON-interactive daily backup for the
REM Windows Task Scheduler. Same job as bcc_backup.bat but:
REM   - no `pause` (a scheduled run has no console to read a keypress;
REM     pause would hang the task forever),
REM   - writes the ADAM copy via the UNC path \\Adam\tbotb (the Z: drive
REM     mapping is per-interactive-session and may be absent here),
REM   - appends all output to backup.log so failures are diagnosable.
REM
REM Registered as scheduled task "BCC Recipes DB Backup". To run by hand:
REM   schtasks /Run /TN "BCC Recipes DB Backup"
REM Interactive one-off backups should use bcc_backup.bat instead.
REM ====================================================================
setlocal
cd /d "C:\Users\john\PycharmProjects\forms"
call "C:\Users\john\PyCharm\venv\Scripts\activate.bat"
echo ==================== scheduled backup %DATE% %TIME% ==================== >> backup.log
REM --verify replays the fresh gz into a throwaway db and compares every
REM table's row count (added to the NIGHTLY 2026-08-24 — the dump had
REM shipped unrestorable twice, and both times nobody noticed because
REM only hand-run backups ever verified). Worth the ~1 min at 3 AM.
python backup_db.py --verify --dest "\\Adam\tbotb\Backups\recipes-db" >> backup.log 2>&1
echo exit code: %ERRORLEVEL% >> backup.log
REM Offsite tier (2026-08-24, replaces the retired git-side dump): the
REM restore-VERIFIED .sql.gz dumps, and only those, go to Google Drive —
REM rolling 14 days (~1GB). The fat .db copies and env.backup (PLAINTEXT
REM API keys — must never leave ADAM unencrypted) are excluded by the
REM include filter. rclone remote 'gdrive' = drive.file scope, authorized
REM 2026-08-24; config in %%APPDATA%%\rclone\rclone.conf.
"C:\Users\john\bin\rclone.exe" sync "\\Adam\tbotb\Backups\recipes-db" "gdrive:BCC-Backups/recipes-db" --include "recipes_*.sql.gz" --max-age 14d --delete-excluded --stats-one-line >> backup.log 2>&1
echo cloud sync exit code: %ERRORLEVEL% >> backup.log
REM Project-directory MIRROR (2026-08-24 DR audit): everything the other
REM tiers miss — generated\ (2.1GB of images incl. IRREPLACEABLE user
REM hero uploads), input\ exports, bats, working tree + .git. Rolling
REM mirror (/MIR), not timestamped. Live SQLite files are EXCLUDED: their
REM consistent copies come from backup_db.py above (a robocopy of an open
REM WAL db can be torn); page_cache.db is a regenerable fetch cache and
REM __pycache__ is noise. robocopy exit codes 0-7 = success.
robocopy "C:\Users\john\PycharmProjects\forms" "\\Adam\tbotb\Backups\forms-mirror" /MIR /R:1 /W:2 /NFL /NDL /NP ^
  /XF recipes.db recipes.db-wal recipes.db-shm page_cache.db media.db training.db recipes.sqbpro identifier.sqlite ^
  /XD __pycache__ .venv >> backup.log 2>&1
echo project mirror exit code: %ERRORLEVEL% (0-7 = ok) >> backup.log
REM recipe-core: a sibling EDITABLE dependency (requirements-frozen.txt
REM line '-e c:\users\john\pycharmprojects\recipe-core') that lives
REM OUTSIDE forms\ — found by the 2026-08-24 restore drill when pip on
REM the target machine could not resolve it. Not a git repo; this mirror
REM line is its ONLY backup.
robocopy "C:\Users\john\PycharmProjects\recipe-core" "\\Adam\tbotb\Backups\recipe-core-mirror" /MIR /R:1 /W:2 /NFL /NDL /NP /XD __pycache__ *.egg-info >> backup.log 2>&1
echo recipe-core mirror exit code: %ERRORLEVEL% (0-7 = ok) >> backup.log
REM BAILEY warm-standby refresh (2026-08-25): the -WithDbs sync rides the
REM nightly, ORDERED AFTER the backup so it ships the copies made minutes
REM ago. Stops BAILEY's staging server, lays in the fresh set, restarts
REM the BCC-Drill task, health-checks. Best-effort: BAILEY being off must
REM not fail the backup run (the sync script exits nonzero on its own).
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\john\PycharmProjects\forms\bcc_sync_bailey.ps1" -WithDbs >> backup.log 2>&1
echo bailey sync exit code: %ERRORLEVEL% >> backup.log
REM Mirror + media + freshest training copy go OFFSITE too (2026-08-24,
REM closes DR gap G2 — the fire scenario previously lost all three).
REM env.backup stays OFF the cloud by policy (plaintext keys; the offsite
REM key copy is the password manager). Training uses a 2-day age window
REM so the cloud holds the newest copy or two, not the whole dated trail.
"C:\Users\john\bin\rclone.exe" sync "\\Adam\tbotb\Backups\forms-mirror" "gdrive:BCC-Backups/forms-mirror" --stats-one-line >> backup.log 2>&1
echo cloud mirror exit code: %ERRORLEVEL% >> backup.log
"C:\Users\john\bin\rclone.exe" sync "\\Adam\tbotb\Backups\recipes-db" "gdrive:BCC-Backups/db-latest" --include "media_latest.db" --include "training_*.db" --max-age 2d --delete-excluded --stats-one-line >> backup.log 2>&1
echo cloud media/training exit code: %ERRORLEVEL% >> backup.log
endlocal
