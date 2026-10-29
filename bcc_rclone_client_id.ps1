# =====================================================================
# bcc_rclone_client_id.ps1 - move the Google Drive remote onto OUR OWN
# OAuth client ID (2026-10-14).
#
# WHY. The nightly offsite tier used rclone's SHARED client ID. Every
# rclone user in the world draws on that one project's per-minute Drive
# quota, so the forms-mirror stage failed "403 rateLimitExceeded" most
# nights (09-19, 09-29, 10-11..10-14), and the shared ID is being RETIRED
# during 2026 (rclone prints the NOTICE on every run). Our own client ID
# gets the full per-project quota and does not retire.
#
# THE CATCH. The remote uses the `drive.file` scope on purpose (rclone
# sees only what rclone created, never the rest of the Drive). A file's
# "creating app" is the OAuth CLIENT, so a NEW client cannot see the
# BCC-Backups folder the shared client made. Switching the client_id in
# place would leave rclone staring at an empty Drive. So this script:
#
#   1. asks for the new client ID + secret (from Google Cloud Console - the
#      checklist is in docs/disaster-recovery.md G4) and creates a SECOND
#      remote, `gdrive-own`, with them (browser sign-in opens);
#   2. SEEDS gdrive-own:BCC-Backups from ADAM with the nightly's exact
#      filters (recipes-db dumps, forms-mirror, db-latest) while the old
#      remote is still intact - the offsite copy is never missing;
#   3. renames gdrive-own -> gdrive in rclone.conf (the old section is
#      kept as [gdrive-shared] for a while), so the nightly bat,
#      backup_check.py and the docs need no change.
#
# The OLD folder is NOT deleted by this script: it is invisible to the
# new client and still counts against Drive storage, so trash the older
# of the two "BCC-Backups" folders yourself in drive.google.com (sort My
# Drive by modified date; the new one is today's). Then
# `rclone config delete gdrive-shared`.
#
# Run it in a PowerShell window as john (step 1 opens a browser for the
# Google sign-in; the rest needs no hands):
#   .\bcc_rclone_client_id.ps1                                  # prompts for ID + secret
#   .\bcc_rclone_client_id.ps1 -ClientJson "$env:USERPROFILE\Downloads\client_secret_*.json"
#                                          # reads both from the console's "Download JSON"
#   .\bcc_rclone_client_id.ps1 -SeedOnly                        # stop after step 2
#   .\bcc_rclone_client_id.ps1 -Yes                             # no cut-over prompt
# Safe to re-run: an existing gdrive-own is reused, syncs are incremental.
# Output is transcript-safe: the secret and OAuth tokens are masked (2026-10-29,
# after a first run's failure message echoed rclone's token into a log).
# Keep the client JSON in the password manager (G3 policy), not in Downloads.
# rclone.conf is backed up beside itself first.
# =====================================================================
# -ClientJson: the client_secret_*.json that the console's "Download JSON"
# button saves (2026-10-29). Skips the two paste prompts; the secret is read
# by this script only and never echoed.
# -Yes: answer the cut-over prompt (step 3) without asking.
param([switch]$SeedOnly, [string]$ClientJson, [switch]$Yes)
$ErrorActionPreference = "Stop"
$rc   = "C:\Users\john\bin\rclone.exe"
$conf = Join-Path $env:APPDATA "rclone\rclone.conf"
$adam = "\\Adam\tbotb\Backups"
$new  = "gdrive-own"
$old  = "gdrive"
$park = "gdrive-shared"

function Fail($msg) { Write-Host "`n!! $msg" -ForegroundColor Red; exit 1 }
function Run([string[]]$cmdArgs) {
  # The secret must not reach a transcript: mask it in the echoed command.
  Write-Host ("> rclone " + (($cmdArgs | ForEach-Object { $_ -replace '^client_secret=.*', 'client_secret=<hidden>' }) -join " ")) -ForegroundColor DarkGray
  # `| Out-Host`: without it rclone's stdout becomes part of the function's
  # RETURN VALUE (PowerShell returns every unconsumed output), so the caller's
  # `$r -ne 0` saw an array and a SUCCESSFUL `config create` read as a failure
  # (2026-10-29, first run on the real client ID). Only the exit code returns.
  $ErrorActionPreference = "Continue"   # rclone's stderr must not terminate the script
  & $rc @cmdArgs 2>&1 | ForEach-Object { "$_" -replace '"(access_token|refresh_token)":"[^"]+"', '"$1":"<hidden>"' -replace 'client_secret = .*', 'client_secret = <hidden>' } | Out-Host
  $ErrorActionPreference = "Stop"
  return $LASTEXITCODE
}

if (-not (Test-Path $rc))   { Fail "rclone not found at $rc" }
if (-not (Test-Path $conf)) { Fail "rclone.conf not found at $conf" }
if (-not (Test-Path "$adam\recipes-db")) { Fail "ADAM share not reachable ($adam) - run this on MARLEY as john" }

$remotes = (& $rc listremotes) -replace ":$", ""
if ($remotes -notcontains $old) { Fail "remote '$old' missing - nothing to migrate" }
if ($remotes -contains $park)   { Fail "remote '$park' already exists - the cut-over already ran" }

# ---- 1. the new remote ------------------------------------------------
if ($remotes -contains $new) {
  Write-Host "== remote '$new' already exists - reusing it =="
} else {
  Write-Host "== new remote '$new' =="
  if ($ClientJson) {
    if (-not (Test-Path $ClientJson)) { Fail "client JSON not found: $ClientJson" }
    $j = Get-Content $ClientJson -Raw | ConvertFrom-Json
    $c = if ($j.installed) { $j.installed } elseif ($j.web) { $j.web } else { $j }
    $cid = [string]$c.client_id; $secPlain = [string]$c.client_secret
    Write-Host "client ID from $(Split-Path $ClientJson -Leaf): $cid"
  } else {
    Write-Host "Paste the OAuth client from Google Cloud Console (Desktop app, project published)."
    $cid = (Read-Host "Client ID (ends in .apps.googleusercontent.com)").Trim()
    $sec = Read-Host "Client secret" -AsSecureString
    $secPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
      [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
  }
  if ($cid -notmatch '^[0-9]+-[a-z0-9]+\.apps\.googleusercontent\.com$') { Fail "that does not look like a client ID: '$cid'" }
  if ($secPlain.Length -lt 10) { Fail "client secret looks empty" }
  Copy-Item $conf "$conf.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
  Write-Host "rclone.conf backed up. A browser window opens now - sign in with the SAME Google account as before."
  $r = Run @("config", "create", $new, "drive", "client_id=$cid", "client_secret=$secPlain", "scope=drive.file")
  if ($r -ne 0) { Fail "rclone config create failed (exit $r)" }
}
$r = Run @("lsd", "${new}:")
if ($r -ne 0) { Fail "'$new' cannot list the Drive root - the token is not working" }

# ---- 2. seed the new folder (nightly's filters, from ADAM) --------------
Write-Host "`n== seeding ${new}:BCC-Backups from ADAM (incremental; first run uploads ~7 GB) =="
$seeds = @(
  @("$adam\recipes-db",   "${new}:BCC-Backups/recipes-db",   @("--include", "recipes_*.sql.gz", "--max-age", "14d", "--delete-excluded")),
  @("$adam\forms-mirror", "${new}:BCC-Backups/forms-mirror", @("--exclude", "generated/og-thumbs/**")),
  @("$adam\recipes-db",   "${new}:BCC-Backups/db-latest",    @("--include", "media_latest.db", "--include", "training_*.db", "--max-age", "2d", "--delete-excluded"))
)
foreach ($s in $seeds) {
  $r = Run (@("sync", $s[0], $s[1]) + $s[2] + @("--transfers", "8", "--retries", "3", "--stats", "30s", "--stats-one-line"))
  if ($r -ne 0) { Fail "seed of $($s[1]) failed (exit $r) - re-run; syncs are incremental" }
}
Write-Host "`n== new side =="
& $rc size "${new}:BCC-Backups"
Write-Host "== old side (may hit the shared-ID quota; informational) =="
# Informational only. Under $ErrorActionPreference=Stop a redirected stderr line
# (the shared-client NOTICE) is a terminating NativeCommandError — it killed run 2
# right after a complete seed (2026-10-29). Stderr is dropped here on purpose.
$ErrorActionPreference = "Continue"
& $rc size "${old}:BCC-Backups" --retries 1 2>$null | Select-Object -First 3
$ErrorActionPreference = "Stop"
if ($SeedOnly) { Write-Host "`n-SeedOnly: stopping before the cut-over. Re-run without it to finish."; exit 0 }

# ---- 3. cut over: rename the remotes --------------------------------------
$go = if ($Yes) { Write-Host "`n-Yes: cutting over."; "y" } else { Read-Host "`nMake '$new' the nightly's '$old' (old section parked as '$park')? [y/N]" }
if ($go -ne "y") { Write-Host "left as is: both remotes exist, nightly still uses '$old'."; exit 0 }
Copy-Item $conf "$conf.bak-cutover-$(Get-Date -Format yyyyMMdd-HHmmss)"
$text  = Get-Content $conf -Raw
$parts = $text -split '(?m)^(?=\[)'
$parts = $parts | ForEach-Object { $_ -replace "^\[$old\]", "[$park]" }
$parts = $parts | ForEach-Object { $_ -replace "^\[$new\]", "[$old]" }
Set-Content -Path $conf -Value (($parts -join "") -replace "(\r?\n)+$", "`r`n") -NoNewline -Encoding UTF8

Write-Host "`n== verify =="
$show = & $rc config show $old
if (($show | Select-String "^client_id = .+apps\.googleusercontent\.com") -eq $null) { Fail "'$old' does not carry the new client_id - restore from the cut-over backup beside rclone.conf" }
$r = Run @("lsd", "${old}:BCC-Backups")
if ($r -ne 0) { Fail "'$old' cannot list BCC-Backups after the rename" }
& $rc size "${old}:BCC-Backups"
Write-Host "`n== done: '$old' now runs on our own client ID. Tonight's 03:00 run is the proof (no NOTICE, 'cloud mirror exit code: 0'). ==" -ForegroundColor Green
Write-Host "Now trash the OLDER 'BCC-Backups' folder in drive.google.com, then: rclone config delete $park"
