@echo off
REM ====================================================================
REM bcc_backup_check.bat - NON-interactive morning verification of the
REM 03:00 backup + BAILEY sync. Scheduled task "BCC Backup Check", 07:00.
REM
REM Its OWN task on purpose: on 2026-09-19 Windows killed the backup task
REM at its time limit and nothing reported it - a check placed at the end
REM of the backup bat would have died with it. backup_check.py emails the
REM curator (setting alert_email) when anything failed; silent when all is
REM well. Output appends to backup_check.log.
REM   By hand:  python backup_check.py --no-email
REM ====================================================================
setlocal
cd /d "C:\Users\john\PycharmProjects\forms"
call "C:\Users\john\PyCharm\venv\Scripts\activate.bat"
echo ==================== backup check %DATE% %TIME% ==================== >> backup_check.log
python backup_check.py >> backup_check.log 2>&1
echo exit code: %ERRORLEVEL% >> backup_check.log
endlocal
