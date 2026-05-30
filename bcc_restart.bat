@echo off
setlocal

REM ====================================================================
REM bcc_restart.bat — reliable "stop the old server, start a fresh one".
REM
REM Why this exists / why the naive version failed:
REM   uvicorn runs WITHOUT --reload (flaky on Windows), so code edits do
REM   NOT go live until the process is restarted. WORSE: old --reload
REM   sessions left an orphaned multiprocessing worker that INHERITED the
REM   :8009 listening socket. netstat/Get-NetTCPConnection then blame the
REM   dead PARENT pid, so `taskkill /PID <parent>` no-ops and every new
REM   start dies with WinError 10048 (port in use) while the zombie keeps
REM   serving STALE code. That's the "I restarted but nothing changed".
REM
REM   The fix: kill the socket owner AND its child processes (the child
REM   is the one actually holding the handle), via PowerShell which can
REM   walk the parent/child tree. Then VERIFY the port is free and abort
REM   loudly if it isn't, instead of silently failing to bind.
REM
REM Safe to run when nothing is running. Run it in a NEW terminal (the
REM old one is blocked by the running uvicorn); this kills that one.
REM ====================================================================

set "VENV=C:\Users\john\PyCharm\venv"
set "PROJECT=C:\Users\john\PycharmProjects\forms"
set "PORT=8009"

cd /d "%PROJECT%"

REM --- 1. Kill the listen-socket owner AND its children, then wait for
REM        the OS to release the socket. PowerShell handles the inherited
REM        -socket zombie that taskkill cannot. (No double-quotes inside
REM        the -Command string: WMI filters are built with single-quoted
REM        concatenation so cmd quoting stays sane.) ---
echo Freeing port %PORT% (killing server + any orphaned workers)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$o=@((Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue).OwningProcess)|Where-Object{$_ -and $_ -ne 0}|Sort-Object -Unique; $k=@(); foreach($p in $o){$k+=$p; $k+=(Get-CimInstance Win32_Process -Filter ('ParentProcessId='+$p) -ErrorAction SilentlyContinue).ProcessId}; $k=$k|Where-Object{$_ -and $_ -ne 0}|Sort-Object -Unique; if($k){Write-Host ('  killing PIDs: '+($k -join ', '))}else{Write-Host '  none found'}; foreach($q in $k){Stop-Process -Id $q -Force -ErrorAction SilentlyContinue}; Start-Sleep -Seconds 2"

REM --- 2. Verify the port is actually free. If a process still holds it
REM        (e.g. it refused to die), STOP here with a clear message so we
REM        never silently start a process that fails to bind. ---
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
if errorlevel 1 (
    echo.
    echo ******************************************************************
    echo  PORT %PORT% IS STILL IN USE — did NOT start a new server.
    echo  Inspect the holder manually:
    echo     Get-NetTCPConnection -LocalPort %PORT% -State Listen
    echo     Get-CimInstance Win32_Process -Filter "ParentProcessId=<owner>"
    echo  then Stop-Process -Id <pid> -Force, and re-run this script.
    echo ******************************************************************
    echo.
    pause
    exit /b 1
)
echo Port %PORT% is free.

REM --- 3. Activate venv + force UTF-8 / unbuffered stdio (matches
REM        bcc_start.bat so prints don't crash and logs tail live). ---
call "%VENV%\Scripts\activate.bat"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

echo.
echo Starting fresh uvicorn on http://localhost:%PORT%
echo Open: http://localhost:%PORT%/forms/recipe_form_styled.html
echo Logs: uvicorn_stdout.log + uvicorn_stderr.log (tail with `Get-Content -Wait`)
echo.

REM --- 4. Start. Append (not truncate) so logs persist across restarts;
REM        a banner marks where this session begins. Single-process
REM        serving (no --reload) means NO orphan workers are created,
REM        so this zombie situation won't recur from here on. ---
echo. >> uvicorn_stdout.log
echo ==================== uvicorn restart %DATE% %TIME% ==================== >> uvicorn_stdout.log
echo. >> uvicorn_stderr.log
echo ==================== uvicorn restart %DATE% %TIME% ==================== >> uvicorn_stderr.log
uvicorn save_recipe_api:app --host 127.0.0.1 --port %PORT% --log-config log_config.json >> uvicorn_stdout.log 2>> uvicorn_stderr.log

endlocal
