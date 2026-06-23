@echo off
REM ============================================================
REM  kernel_power_check.bat
REM  Lists Kernel-Power Event 41 (abrupt-shutdown) events.
REM
REM  BugcheckCode=0 + PowerButton=0  = the bad signature
REM      (instant power-cut, no BSOD, not a clean shutdown).
REM  PowerButton=1                   = you held the power button
REM      (deliberate) -> ignore.
REM
REM  Context: MARLEY_SVR / Intel i7-13700 13th-Gen instability.
REM  BIOS flashed F.40 -> F.45 on 2026-06-23, AFTER the last
REM  crash (6/23 9:44 AM). Any Bugcheck=0/PowerButton=0 event
REM  dated clearly AFTER the flash means F.45 did NOT hold ->
REM  escalate to Intel (CPU defect warranty, 5-yr window to
REM  ~early 2028; HP system warranty is expired but separate).
REM ============================================================

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$flash = Get-Date '2026-06-23 10:00:00';" ^
  "Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41} -MaxEvents 25 -ErrorAction SilentlyContinue |" ^
  "  Select-Object TimeCreated, Id," ^
  "    @{N='BugcheckCode';E={$_.Properties[1].Value}}," ^
  "    @{N='PowerButton';E={$_.Properties[5].Value}}," ^
  "    @{N='Phase';E={ if ($_.TimeCreated -gt $flash) {'>>> POST-FLASH'} else {'pre-flash'} }} |" ^
  "  Format-Table -AutoSize"

echo.
pause
