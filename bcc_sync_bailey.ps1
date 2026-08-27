# =====================================================================
# bcc_sync_bailey.ps1 — ONE-WAY incremental sync MARLEY -> BAILEY.
#
# Keeps the BAILEY staging instance fresh during the soak without a full
# reload (the 2026-08-24 restore drill built it; this maintains it).
# rclone over SFTP (remote 'bailey', same SSH key as the drill) diffs by
# size+modtime, so an unchanged tree transfers nothing.
#
#   .\bcc_sync_bailey.ps1              # code + assets only (server stays up)
#   .\bcc_sync_bailey.ps1 -WithDbs     # + databases: STOPS Bailey's server,
#                                      #   copies the newest ADAM backup set,
#                                      #   restarts the BCC-Drill task
#   .\bcc_sync_bailey.ps1 -WithDbs -FreshBackup
#                                      # runs backup_db.py first so the DBs
#                                      #   are to-the-minute, then as above
#
# ONE-WAY means one-way: anything saved on BAILEY since the last sync is
# overwritten (that is the point of a staging copy). Excluded either side:
# live sqlite files (consistent copies come from backup_db), page cache,
# logs (each machine keeps its own), venv/pycache noise, the local dump.
# =====================================================================
param([switch]$WithDbs, [switch]$FreshBackup)

$rc    = "C:\Users\john\bin\rclone.exe"
$src   = "C:/Users/john/PycharmProjects/forms"
$dst   = "bailey:/C:/Users/john/PycharmProjects/forms"
$adam  = "\\Adam\tbotb\Backups\recipes-db"

Write-Host "== code + assets (incremental) =="
& $rc sync $src $dst --stats-one-line `
  --exclude "recipes.db" --exclude "recipes.db-wal" --exclude "recipes.db-shm" `
  --exclude "page_cache.db" --exclude "media.db" --exclude "training.db" `
  --exclude "recipes.sql.gz" --exclude "recipes.sqbpro" --exclude "identifier.sqlite" `
  --exclude "__pycache__/**" --exclude ".venv/**" --exclude "logs/**" `
  --exclude "uvicorn_std*.log" --exclude "backup.log" --exclude "jobs_schedule.log"
& $rc sync "C:/Users/john/PycharmProjects/recipe-core" "bailey:/C:/Users/john/PycharmProjects/recipe-core" `
  --exclude "__pycache__/**" --exclude "*.egg-info/**" --stats-one-line

if ($WithDbs) {
  if ($FreshBackup) {
    Write-Host "== fresh backup first =="
    Push-Location "C:\Users\john\PycharmProjects\forms"
    python backup_db.py | Select-Object -Last 2
    Pop-Location
  }
  Write-Host "== stopping BAILEY server =="
  ssh -o BatchMode=yes john@BAILEY "powershell -NoProfile -Command ""Stop-Process -Name python -Force -ErrorAction SilentlyContinue; 'stopped'"""
  # WAIT for python to actually exit and release recipes.db — on 2026-08-26 the
  # copy started immediately, hit 'rename failed: permission denied' x3, and the
  # run still reported success. Poll up to 30s for zero python processes.
  $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline) {
    $left = ssh -o BatchMode=yes john@BAILEY "powershell -NoProfile -Command ""(Get-Process python -ErrorAction SilentlyContinue | Measure-Object).Count"""
    if ("$left".Trim() -eq "0") { break }
    Start-Sleep 3
  }
  Start-Sleep 2   # let the OS release file handles after process exit
  $newestDb = Get-ChildItem "$adam\recipes_*.db"  | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  $newestTr = Get-ChildItem "$adam\training_*.db" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  Write-Host "== DBs: $($newestDb.Name) / $($newestTr.Name) / media_latest / env =="
  # Each copy checked; one retry after 10s; failures collected and FAILED LOUDLY
  # at the end (exit 1) — the 08-26 run buried three rclone errors under exit 0.
  $failed = @()
  $copies = @(
    ,@($newestDb.FullName,       "$dst/recipes.db"),
    ,@($newestTr.FullName,       "$dst/training.db"),
    ,@("$adam\media_latest.db",  "$dst/media.db"),
    ,@("$adam\env.backup",       "$dst/.env"))
  # SIZE-VERIFIED copies: on 2026-08-26 rclone delivered recipes.db 13MB SHORT
  # of the source (503,853,056 vs 517,066,752) and still exited 0 — BAILEY then
  # failed startup with 'database disk image is malformed'. Trust nothing:
  # after each copy, compare the remote byte count to the local file.
  function Copy-Verified([string]$src, [string]$dstPath) {
    & $rc copyto $src $dstPath --stats-one-line
    if ($LASTEXITCODE -ne 0) { return $false }
    $want = (Get-Item $src).Length
    $j = & $rc lsjson $dstPath 2>$null | ConvertFrom-Json
    $got = if ($j) { ($j | Select-Object -First 1).Size } else { -1 }
    if ($got -ne $want) {
      Write-Host "  SIZE MISMATCH $dstPath : remote $got vs source $want"
      return $false
    }
    return $true
  }
  foreach ($c in $copies) {
    if (-not (Copy-Verified $c[0] $c[1])) {
      Write-Host "  retrying $($c[1]) after 10s..."
      Start-Sleep 10
      if (-not (Copy-Verified $c[0] $c[1])) { $failed += $c[1] }
    }
  }
  # A REPLACED db must never pair with the previous run's WAL/SHM — that pairing
  # reads as 'database disk image is malformed' at startup.
  ssh -o BatchMode=yes john@BAILEY "powershell -NoProfile -Command ""Remove-Item C:\Users\john\PycharmProjects\forms\*.db-wal, C:\Users\john\PycharmProjects\forms\*.db-shm -Force -ErrorAction SilentlyContinue; 'sidecars cleared'"""
  Write-Host "== restarting BAILEY server =="
  ssh -o BatchMode=yes john@BAILEY "schtasks /Run /TN BCC-Drill"
  Start-Sleep 15
  ssh -o BatchMode=yes john@BAILEY "powershell -NoProfile -Command ""try{(Invoke-WebRequest -Uri http://127.0.0.1:8009/auth/me -UseBasicParsing -TimeoutSec 5).StatusCode}catch{'NOT ANSWERING'}"""
  if ($failed.Count -gt 0) {
    Write-Host ("!" * 70)
    Write-Host "!! BAILEY DB SYNC FAILED for: $($failed -join ', ')"
    Write-Host "!! BAILEY is serving STALE data. Re-run: .\bcc_sync_bailey.ps1 -WithDbs"
    Write-Host ("!" * 70)
    exit 1
  }
}
Write-Host "== sync done =="
