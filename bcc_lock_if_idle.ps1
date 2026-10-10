# bcc_lock_if_idle.ps1 — lock the desktop after an unattended autologon.
#
# Why: MARLEY_SVR autologons at boot (Sysinternals Autologon, password in the
# LSA secret) so the john/Interactive scheduled tasks — nightly backup, backup
# watcher, hourly dish schedule — run after a crash-reboot instead of waiting at
# the sign-in screen (crash #17, 2026-10-06: four backups silently skipped).
# An autologon leaves the desktop unlocked, so this task fires at logon and locks
# the workstation if nobody has touched the keyboard or mouse. A human logging
# on by hand moves the mouse within the window and keeps the desktop.
#
# Task: "BCC Lock After Autologon" — AtLogOn MARLEY_SVR\john, delay PT60S,
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File <this file>
# Log:  logs\lock_after_logon.log

param([int]$IdleSeconds = 45)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir 'lock_after_logon.log'

function Write-Log([string]$msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Add-Content -Path $log
}

Add-Type -Namespace Win32 -Name Idle -MemberDefinition @'
[StructLayout(LayoutKind.Sequential)]
public struct LASTINPUTINFO { public uint cbSize; public uint dwTime; }
[DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
[DllImport("user32.dll")] public static extern bool LockWorkStation();
'@

$lii = New-Object Win32.Idle+LASTINPUTINFO
$lii.cbSize = [System.Runtime.InteropServices.Marshal]::SizeOf($lii)
if (-not [Win32.Idle]::GetLastInputInfo([ref]$lii)) {
    Write-Log "GetLastInputInfo failed; locking defensively"
    [void][Win32.Idle]::LockWorkStation()
    exit 0
}

$idleMs = ([uint32][Environment]::TickCount - $lii.dwTime)
$idle = [math]::Round($idleMs / 1000)

if ($idle -ge $IdleSeconds) {
    $ok = [Win32.Idle]::LockWorkStation()
    Write-Log "idle ${idle}s >= ${IdleSeconds}s -> LockWorkStation returned $ok"
} else {
    Write-Log "idle ${idle}s < ${IdleSeconds}s -> someone is here, leaving the desktop unlocked"
}
