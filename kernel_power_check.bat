@echo off
REM ============================================================
REM  kernel_power_check.bat
REM  Lists Kernel-Power Event 41 (abrupt-shutdown) events.
REM
REM  BugcheckCode=0 + PowerButton=0  = the bad signature
REM      (instant power-cut, no BSOD, not a clean shutdown).
REM  BugcheckCode=0x101              = CLOCK_WATCHDOG_TIMEOUT,
REM      a CPU core stopped answering interrupts. This is the
REM      Raptor Lake Vmin-shift signature.
REM  PowerButton nonzero             = you held the power button
REM      (deliberate) -> ignore.
REM
REM  Context: MARLEY_SVR / Intel i7-13700 13th-Gen instability.
REM  BIOS flashed F.40 -> F.45 on 2026-06-23, AFTER the last
REM  crash (6/23 9:44 AM). Any Bugcheck=0/PowerButton=0 event
REM  dated clearly AFTER the flash means F.45 did NOT hold ->
REM  escalate to Intel. Purchased 2023-11-21, so the 5-yr CPU
REM  defect window is open to 2028-11-21. HP system warranty
REM  and Best Buy Protection are both already expired.
REM
REM  FIELD ORDER -- fixed 2026-07-29. Both columns were off by
REM  one, which hid the 0x101 bugchecks behind BugcheckParameter1
REM  and reported SleepInProgress as a power-button press (the
REM  4/14 crash looked deliberate and would have been discarded).
REM      Properties[0] = BugcheckCode
REM      Properties[1] = BugcheckParameter1
REM      Properties[5] = SleepInProgress
REM      Properties[6] = PowerButtonTimestamp
REM  Verify with:  ([xml]$e.ToXml()).Event.EventData.Data
REM ============================================================

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$flash = Get-Date '2026-06-23 10:00:00';" ^
  "Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41} -MaxEvents 25 -ErrorAction SilentlyContinue |" ^
  "  Select-Object TimeCreated, Id," ^
  "    @{N='BugcheckCode';E={ $c=[int]$_.Properties[0].Value; if ($c -eq 0) {'0 (none)'} else {'0x{0:X}' -f $c} }}," ^
  "    @{N='Meaning';E={ switch ([int]$_.Properties[0].Value) { 0 {'hard hang - no dump written'} 257 {'CLOCK_WATCHDOG_TIMEOUT'} default {'see bugcheck reference'} } }}," ^
  "    @{N='PowerButton';E={$_.Properties[6].Value}}," ^
  "    @{N='Phase';E={ if ($_.TimeCreated -gt $flash) {'>>> POST-FLASH'} else {'pre-flash'} }} |" ^
  "  Format-Table -AutoSize"

echo.
pause
