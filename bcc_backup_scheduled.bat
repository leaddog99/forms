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
endlocal
