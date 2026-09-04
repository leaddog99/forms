# bcc_install_service_bailey.ps1 — install the NSSM "BCC" service on BAILEY,
# mirroring MARLEY's config exactly, and retire the BCC-Drill scheduled task.
#
# WHY (state log 2026-09-02): BAILEY's server died silently on Aug 26 and stayed
# dead for a week — the schtasks BCC-Drill spawn has no supervision, no restart,
# and bcc_restart.bat can't reach it. A service dies LOUD and restarts itself.
#
# RUN ON BAILEY, from an ELEVATED PowerShell (the one manual step — SSH sessions
# here don't carry the admin token):
#   powershell -ExecutionPolicy Bypass -File C:\Users\john\PycharmProjects\forms\bcc_install_service_bailey.ps1
#
# Everything below is idempotent — safe to re-run.

$ErrorActionPreference = 'Stop'
$proj  = 'C:\Users\john\PycharmProjects\forms'
$nssm  = Join-Path $proj 'tools\nssm.exe'    # staged from MARLEY by the installer prep
$py    = 'C:\Users\john\PyCharm\venv\Scripts\python.exe'

# 0. Preconditions
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'ERROR: run this from an ELEVATED PowerShell.' -ForegroundColor Red; exit 1 }
if (-not (Test-Path $nssm)) { Write-Host "ERROR: $nssm missing (sync/stage it first)." -ForegroundColor Red; exit 1 }
if (-not (Test-Path $py))   { Write-Host "ERROR: $py missing." -ForegroundColor Red; exit 1 }

# 1. Stop + disable the drill task (the service replaces it; both alive = port fight)
schtasks /End /TN BCC-Drill 2>$null | Out-Null
schtasks /Change /TN BCC-Drill /DISABLE 2>$null | Out-Null
Write-Host '[1/4] BCC-Drill task stopped + disabled'

# Kill any orphan server from the drill so the service can bind 8009
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'save_recipe_api' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -Confirm:$false }

# 2. Install/refresh the service — MARLEY's parameters verbatim
& $nssm stop BCC 2>$null | Out-Null
& $nssm remove BCC confirm 2>$null | Out-Null
& $nssm install BCC $py '-m uvicorn save_recipe_api:app --host 127.0.0.1 --port 8009'
& $nssm set BCC AppDirectory $proj
& $nssm set BCC AppStdout (Join-Path $proj 'uvicorn_stdout.log')
& $nssm set BCC AppStderr (Join-Path $proj 'uvicorn_stderr.log')
& $nssm set BCC Start SERVICE_AUTO_START
Write-Host '[2/4] NSSM BCC service installed (auto-start)'

# 3. Start + verify
& $nssm start BCC | Out-Null
Start-Sleep -Seconds 20
try {
    $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8009/auth/me' -UseBasicParsing -TimeoutSec 30
    Write-Host "[3/4] server answering (HTTP $($r.StatusCode))" -ForegroundColor Green
} catch {
    Write-Host "[3/4] WARN: no HTTP answer yet — check uvicorn_stderr.log ($_)" -ForegroundColor Yellow
}

# 4. Summary
& $nssm status BCC
Write-Host '[4/4] done — BCC is a supervised service on BAILEY; bcc_restart.bat now works here.'
