@echo off
setlocal

REM ====================================================================
REM bcc_restart.bat — restart the BCC FastAPI server.
REM
REM The server now runs as the Windows SERVICE "BCC" (NSSM wrapper:
REM   nssm.exe -> python -m uvicorn save_recipe_api:app --port 8009).
REM Restarting the SERVICE is the correct way now. Do NOT kill the python
REM PID: NSSM/the Service Control Manager immediately respawns it, so a
REM Stop-Process no-ops and you keep serving the same (stale) code.
REM
REM Controlling a service needs administrator rights, so this self-elevates
REM via UAC if you didn't start it from an elevated prompt.
REM
REM (NSSM also auto-restarts the worker on crash; this is only for picking
REM up code changes — uvicorn runs WITHOUT --reload, so edits aren't live
REM until the service is bounced.)
REM ====================================================================

set "SVC=BCC"
set "PORT=8009"

REM --- ensure admin (SCM stop/start needs it); re-launch elevated if not ---
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator elevation...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Restarting service %SVC% ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Restart-Service -Name '%SVC%' -Force; Start-Sleep -Seconds 3"

REM --- verify the listener came back on %PORT% ---
echo Waiting for http://localhost:%PORT% to listen ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "for($i=0;$i -lt 25;$i++){ $c=Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue; if($c){ $p=($c.OwningProcess|Select-Object -First 1); $pi=Get-CimInstance Win32_Process -Filter ('ProcessId='+$p); Write-Host ('  listening: pid='+$p+' started '+$pi.CreationDate); exit 0 }; Start-Sleep -Seconds 1 }; Write-Host '  ***** PORT %PORT% DID NOT COME UP — check: nssm status %SVC% / Get-Service %SVC% *****'; exit 1"

echo.
echo Service %SVC% restarted. Manage it with:  nssm status %SVC%  ^|  nssm edit %SVC%  (logs/cmdline)
echo                                          Get-Service %SVC%  ^|  Restart-Service %SVC%
pause
endlocal
