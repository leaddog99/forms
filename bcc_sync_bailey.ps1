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
  $newestDb = Get-ChildItem "$adam\recipes_*.db"  | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  $newestTr = Get-ChildItem "$adam\training_*.db" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  Write-Host "== DBs: $($newestDb.Name) / $($newestTr.Name) / media_latest / env =="
  & $rc copyto $newestDb.FullName        "$dst/recipes.db"  --stats-one-line
  & $rc copyto $newestTr.FullName        "$dst/training.db" --stats-one-line
  & $rc copyto "$adam\media_latest.db"   "$dst/media.db"    --stats-one-line
  & $rc copyto "$adam\env.backup"        "$dst/.env"        --stats-one-line
  Write-Host "== restarting BAILEY server =="
  ssh -o BatchMode=yes john@BAILEY "schtasks /Run /TN BCC-Drill"
  Start-Sleep 15
  ssh -o BatchMode=yes john@BAILEY "powershell -NoProfile -Command ""try{(Invoke-WebRequest -Uri http://127.0.0.1:8009/auth/me -UseBasicParsing -TimeoutSec 5).StatusCode}catch{'NOT ANSWERING'}"""
}
Write-Host "== sync done =="
