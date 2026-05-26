@echo off
setlocal

set "VENV=C:\Users\john\PyCharm\venv"
set "PROJECT=C:\Users\john\PycharmProjects\forms"

cd /d "%PROJECT%"
call "%VENV%\Scripts\activate.bat"

REM Force UTF-8 + unbuffered stdio so prints don't crash on chars the
REM Windows console (cp1252) can't render, and so the log files update
REM in near-real-time when tailed.
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

echo.
echo Open: http://localhost:8009/forms/recipe_form_styled.html
echo Logs: uvicorn_stdout.log + uvicorn_stderr.log (tail with `Get-Content -Wait`)
echo.

REM Redirect both streams to files so closing this window can't break
REM uvicorn's stdio handles mid-flight (which was raising OSError 22
REM on every print, including [DATA] payload dumps inside /recipes).
REM
REM NOTE: --reload was dropped intentionally. On Windows it spawns a
REM separate worker process whose stdio handles are NOT inherited from
REM this bat's redirect — the worker's print() then fails with OSError
REM 22 on every request. --reload was also flaky on Windows in our
REM experience anyway (the user has been hand-restarting uvicorn for
REM code changes throughout the project). Single-process serving here.
uvicorn save_recipe_api:app --host 127.0.0.1 --port 8009 --log-config log_config.json > uvicorn_stdout.log 2> uvicorn_stderr.log

endlocal