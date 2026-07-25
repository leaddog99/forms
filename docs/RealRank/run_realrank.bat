@echo off
setlocal enabledelayedexpansion

REM folder this .bat lives in (your RealRank folder)
set "HERE=%~dp0"

REM --- load KEY=VALUE lines from the .env in this folder ---
if exist "%HERE%.env" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%HERE%.env") do (
        set "line=%%a"
        if not "!line!"=="" if not "!line:~0,1!"=="#" set "%%a=%%b"
    )
)

REM --- default the model if the .env didn't set one ---
if "%REALRANK_MODEL%"=="" set "REALRANK_MODEL=claude-sonnet-5"

REM --- your PyCharm venv python ---
set "PY=C:\Users\john\PyCharm\venv\Scripts\python.exe"

REM --- run, passing the product name through as the argument ---
"%PY%" "%HERE%realrank_research.py" %*

echo.
pause