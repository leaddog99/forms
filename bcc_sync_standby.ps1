# =====================================================================
# bcc_sync_standby.ps1 — ONE-WAY incremental sync MARLEY -> a standby host.
#
# Was bcc_sync_bailey.ps1 (2026-08-24 → 2026-10-17); parameterised when the
# new desktop MARLEY2 arrived, so ONE script serves every standby. Keeps a
# standby instance fresh during a soak without a full reload (the 2026-08-24
# restore drill built BAILEY; this maintains it — and builds MARLEY2's soak).
# rclone over SFTP (remote named after the host, lower-case, same SSH key as
# the drill) diffs by size+modtime, so an unchanged tree transfers nothing.
#
#   .\bcc_sync_standby.ps1                      # BAILEY, code + assets only (server stays up)
#   .\bcc_sync_standby.ps1 -Standby MARLEY2      # same, for MARLEY2
#   .\bcc_sync_standby.ps1 -WithDbs             # + databases: STOPS the standby's server,
#                                               #   copies the newest ADAM backup set, restarts it
#   .\bcc_sync_standby.ps1 -Standby MARLEY2 -WithDbs -FreshBackup
#                                               # runs backup_db.py first so the DBs are
#                                               #   to-the-minute, then as above
#
# The rclone remote must exist: one per standby, named like the host in lower
# case. Create one with (key = the drill key, host = the machine name):
#   rclone config create marley2 sftp host MARLEY2 user john key_file C:/Users/john/.ssh/id_ed25519 shell_type cmd md5sum_command none sha1sum_command none
# and the standby's C:\ProgramData\ssh\administrators_authorized_keys must
# hold that key's .pub (john is an administrator there, so the per-user
# authorized_keys is ignored by Windows OpenSSH).
#
# ONE-WAY means one-way: anything saved on the standby since the last sync is
# overwritten (that is the point of a staging copy). Excluded either side:
# live sqlite files (consistent copies come from backup_db), page cache,
# logs (each machine keeps its own), venv/pycache noise, the local dump.
#
# backup_check.py reads this script's output inside backup.log: the words
# "FAILED" / "permission denied" / "rename failed" mark a bad night and
# "== sync done ==" a clean one. Keep those strings if you edit messages.
# =====================================================================
param(
  [string]$Standby = "BAILEY",
  [switch]$WithDbs,
  [switch]$FreshBackup
)

$Standby = $Standby.ToUpper()
$remote  = $Standby.ToLower()          # rclone remote name: bailey, marley2
$who     = "john@$Standby"             # ssh target; the SSH key is the drill key
$rc    = "C:\Users\john\bin\rclone.exe"
$ssh   = "C:\Windows\System32\OpenSSH\ssh.exe"   # full path: not on PATH in every shell (2026-09-08)
$src   = "C:/Users/john/PycharmProjects/forms"
$dst   = "${remote}:/C:/Users/john/PycharmProjects/forms"
$adam  = "\\Adam\tbotb\Backups\recipes-db"

# Fail before doing anything if the remote is not configured — rclone would
# otherwise print 'didn't find section in config file' per stage and the run
# would read as a code-sync failure rather than a missing remote.
$remotes = & $rc listremotes 2>$null
if (-not ($remotes -contains "${remote}:")) {
  Write-Host ("!" * 70)
  Write-Host "!! $Standby SYNC FAILED: no rclone remote '$remote'. Create it:"
  Write-Host "!!   rclone config create $remote sftp host $Standby user john key_file C:/Users/john/.ssh/id_ed25519 shell_type cmd md5sum_command none sha1sum_command none"
  Write-Host ("!" * 70)
  exit 1
}

# 2026-10-03: the two code syncs below were never checked. On 10-03 BAILEY was
# unreachable (stale DNS after a reboot), both failed CRITICAL, and only the DB
# stage's own check made the run exit 1. Any failed stage fails the script.
$stageFailed = @()
Write-Host "== ${Standby}: code + assets (incremental) =="
# --no-update-dir-modtime (2026-09-12): Windows sftp-server refuses SetModTime on
# directories the running service holds open -> rclone reported 1 error every run
# and SKIPPED ALL DELETIONS on the standby. Directory mtimes carry nothing we need.
# 2026-10-13: git writes pack and commit-graph files READ-ONLY, and the SFTP
# copy carries that mode to the standby, where Windows then refuses to delete
# them. When MARLEY's git repacks (10-10 did), rclone fails to delete the stale
# pair and, as on 09-12, SKIPS ALL DELETIONS for the run - three nights of
# alerts and a tree that only grew. Clear the attribute under .git first.
& $ssh -o BatchMode=yes $who "cmd /c attrib -R /S C:\Users\john\PycharmProjects\forms\.git\*.*" | Out-Null
& $rc sync $src $dst --stats-one-line --no-update-dir-modtime `
  --exclude "recipes.db" --exclude "recipes.db-wal" --exclude "recipes.db-shm" `
  --exclude "page_cache.db" --exclude "media.db" --exclude "training.db" `
  --exclude "recipes.sql.gz" --exclude "recipes.sqbpro" --exclude "identifier.sqlite" `
  --exclude "__pycache__/**" --exclude ".venv/**" --exclude "logs/**" `
  --exclude "uvicorn_std*.log" --exclude "backup.log" --exclude "jobs_schedule.log" `
  --exclude "data/browser_profiles/**" --exclude "input/captures/**"
  # ^ capture walker (2026-09-08): a live Chromium profile holds LOCK files
  #   (13 rclone errors) and its cookies are a signed-in session that must
  #   stay on this machine; captures are per-machine pipeline inputs.
if ($LASTEXITCODE -ne 0) { $stageFailed += "forms code sync (rclone exit $LASTEXITCODE)" }
# recipe-core, the one editable sibling with no git repo anywhere: this copy is
# its only route to a standby. The kits (imagekit, llmkit, ...) are NOT synced:
# BAILEY owns them and commits there (imagekit README); a one-way mirror from
# MARLEY overwrote BAILEY's newer kit commits (found 2026-10-28: llmkit lost
# openai_embed, imagekit its README section). Each machine pulls kits from GitHub.
foreach ($pkg in "recipe-core") {
  & $rc sync "C:/Users/john/PycharmProjects/$pkg" "${remote}:/C:/Users/john/PycharmProjects/$pkg" --no-update-dir-modtime `
    --exclude "__pycache__/**" --exclude "*.egg-info/**" --exclude ".git/**" --stats-one-line
  if ($LASTEXITCODE -ne 0) { $stageFailed += "$pkg sync (rclone exit $LASTEXITCODE)" }
}

if ($WithDbs) {
  if ($FreshBackup) {
    Write-Host "== fresh backup first =="
    Push-Location "C:\Users\john\PycharmProjects\forms"
    python backup_db.py | Select-Object -Last 2
    Pop-Location
  }
  Write-Host "== stopping $Standby server =="
  # 2026-09-11 cut-over: the standby runs BCC as an NSSM service (AppExit=Restart),
  # so a bare Stop-Process would be undone by nssm within seconds and the db
  # copy would hit a locked file. Stop the SERVICE when it exists; fall back
  # to the old process kill for a BCC-Drill-era host.
  & $ssh -o BatchMode=yes $who "powershell -NoProfile -Command ""if (Get-Service BCC -ErrorAction SilentlyContinue) { Stop-Service BCC -Force; 'service stopped' } else { Stop-Process -Name python -Force -ErrorAction SilentlyContinue; 'stopped' }"""
  # WAIT for python to actually exit and release recipes.db — on 2026-08-26 the
  # copy started immediately, hit 'rename failed: permission denied' x3, and the
  # run still reported success. Poll up to 30s for zero python processes.
  $deadline = (Get-Date).AddSeconds(30)
  while ((Get-Date) -lt $deadline) {
    # NO PIPE in the remote command (2026-09-19). The nightly runs this file under
    # Windows PowerShell 5.1, which hands native exes the inner quotes differently
    # from pwsh 7: the standby's cmd.exe saw the bare "|" and answered "'Measure-Object)
    # .Count' is not recognized" on every poll - so $left was never "0" and this
    # loop was a blind 30s sleep, not a check. @(...).Count needs no pipe.
    $left = & $ssh -o BatchMode=yes $who "powershell -NoProfile -Command ""@(Get-Process python -ErrorAction SilentlyContinue).Count"""
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
  # Plain nested arrays: comma-separated @(src,dst) pairs inside @() stay nested.
  # The previous ",@(...)" unary-comma rows DOUBLE-wrapped each pair, so $c[1]
  # (the destination) was $null on every row — rclone then failed all four
  # copies with 'can''t use empty string as a path', and the retry/failure
  # messages printed blank names (caught 2026-09-02; -WithDbs had never
  # actually delivered a file through this loop).
  $copies = @(
    @($newestDb.FullName,       "$dst/recipes.db"),
    @($newestTr.FullName,       "$dst/training.db"),
    @("$adam\media_latest.db",  "$dst/media.db"),
    @("$adam\env.backup",       "$dst/.env"))
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
  & $ssh -o BatchMode=yes $who "powershell -NoProfile -Command ""Remove-Item C:\Users\john\PycharmProjects\forms\*.db-wal, C:\Users\john\PycharmProjects\forms\*.db-shm -Force -ErrorAction SilentlyContinue; 'sidecars cleared'"""
  Write-Host "== restarting $Standby server =="
  & $ssh -o BatchMode=yes $who "powershell -NoProfile -Command ""if (Get-Service BCC -ErrorAction SilentlyContinue) { Start-Service BCC; 'service started' } else { schtasks /Run /TN BCC-Drill }"""
  Start-Sleep 40   # 2026-09-11: 15 s printed NOT ANSWERING on a healthy start
  & $ssh -o BatchMode=yes $who "powershell -NoProfile -Command ""try{(Invoke-WebRequest -Uri http://127.0.0.1:8009/auth/me -UseBasicParsing -TimeoutSec 5).StatusCode}catch{'NOT ANSWERING'}"""
  if ($failed.Count -gt 0) {
    Write-Host ("!" * 70)
    Write-Host "!! $Standby DB SYNC FAILED for: $($failed -join ', ')"
    Write-Host "!! $Standby is serving STALE data. Re-run: .\bcc_sync_standby.ps1 -Standby $Standby -WithDbs"
    Write-Host ("!" * 70)
    exit 1
  }
}
if ($stageFailed.Count -gt 0) {
  Write-Host ("!" * 70)
  Write-Host "!! $Standby CODE SYNC FAILED: $($stageFailed -join '; ')"
  Write-Host "!! Re-run: .\bcc_sync_standby.ps1 -Standby $Standby"
  Write-Host ("!" * 70)
  exit 1
}
Write-Host "== sync done =="
