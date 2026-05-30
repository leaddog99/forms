@echo off
REM ====================================================================
REM bcc_backup.bat — one-click off-machine backup of recipes.db to ADAM.
REM
REM Refreshes ./recipes.sql (the diffable dump committed to git) and copies
REM recipes.db + recipes.sql to \\Adam\tbotb\Backups\recipes-db\ with a
REM timestamp, then verifies the copy with PRAGMA integrity_check.
REM
REM Safe to run while the server is up (reads the DB read-only). Run it
REM before any risky batch run. See backup_db.py for flags (--dest, --no-adam).
REM ====================================================================
setlocal
set "VENV=C:\Users\john\PyCharm\venv"
cd /d "C:\Users\john\PycharmProjects\forms"
call "%VENV%\Scripts\activate.bat"
python backup_db.py %*
echo.
pause
endlocal
