# Host stability (MARLEY_SVR) + auto-recovery watchdog

**Status:** diagnosis complete and measured; two mitigations applied; BIOS change and
watchdog hardware still TODO. Written 2026-07-29 after the host sat dead for ~12 hours
following a 1:28 AM crash.

Related: `memory/project_host_thermal_shutdowns.md`, `memory/project_restart_zombie_port.md`,
`memory/project_db_backup.md`, `memory/feedback_stray_keystroke_corruption.md`.

---

## 1. The host

| | |
|---|---|
| Name | `MARLEY_SVR` |
| IP (wired) | 192.168.1.177 · Ethernet |
| IP (wifi) | 192.168.1.250 · `VerizonWifI-250` |
| Gateway | 192.168.1.1 |
| CPU | 13th Gen Intel Core i7-13700 — Family 6 **Model 183 Stepping 1** (Raptor Lake) |
| Board / BIOS | HP 8B3B · AMI **F.45** (2025-10-14) |
| Microcode | **0x12F** (`Update Revision` = `2F 01 00 00`) |
| App | FastAPI behind NSSM Windows service **`BCC`**, Automatic, `127.0.0.1:8009` |
| Tunnel | `Cloudflared` service, Automatic → recipes.tbotb.com |
| Backup target | **ADAM** = Synology NAS, 192.168.1.235, `\\Adam\tbotb\Backups\recipes-db` |

---

## 2. Root cause — CPU degradation, not software

The 2026-07-29 01:28:01 crash was bugcheck **`0x101` CLOCK_WATCHDOG_TIMEOUT**
(`0x8, 0x0, 0xffffc481f1817180, 0x17`) — a CPU core stopped responding to interrupts.

Everything points one way:

- The i7-13700 is in the **Raptor Lake Vmin-shift** family (Model 183).
- Microcode **0x12F is already loaded** — Intel's *latest* mitigation, released
  specifically for *"systems continuously running for multiple days with low-activity
  and lightly-threaded workloads."* That is exactly this host's duty cycle.
- It crashed **awake and idle**: zero System-log events between 00:30 and 01:35.
- **The mitigation is applied and it still fails** → the silicon has already degraded.
  Microcode prevents *further* degradation; it does not reverse what has happened.
  A newer BIOS will not help — F.45 is what carries 0x12F.

**Ruled out:**

- *Sleep / modern standby* — S0 Low Power Idle is **not supported** on this board,
  AC standby timeout = 0, hibernate timeout = 0, `powercfg /lastwake` history empty.
  (`Kernel-Power 41` reports `ConnectedStandbyInProgress = true`, but that field is
  meaningless here given S0ix is unavailable. Do not chase it again.)
- *Power outage* — this was a bugcheck, not a loss of AC. "Restore on AC power loss"
  would not have helped **this** event (it is still required for the watchdog, §5).
- *The application* — see §4.

### Frequency, and it is accelerating

11 spontaneous shutdowns between 2026-05-10 and 2026-07-29. The log holds 13
Kernel-Power Event 41 records; 2026-04-14 and 2026-05-21 carry a real
`PowerButtonTimestamp` (the power button was pressed) and are excluded:

```
4/14 16:19 · 5/10 13:12 · 5/21 12:02 · 5/30 12:51 · 6/8 00:29 · 6/12 01:34
6/16 20:34 · 6/22 21:36 · 6/23 04:18 · 6/25 14:06 · 7/10 11:25 · 7/24 22:04 · 7/29 01:28
```

Only **two** recorded a bugcheck code (6/12 and 7/29, both `0x101`). The other eleven
logged `BugcheckCode = 0` — a total hang with nothing written at all.

Run `kernel_power_check.bat` for the live table; the generated exhibit is
`warranty-evidence/crash-evidence.txt`.

### Why it never restarted by itself

`HKLM\SYSTEM\CurrentControlSet\Control\CrashControl\AutoReboot = 1`, so Windows *is*
configured to restart after a bugcheck. But `0x101` halts the cores — the
crash-dump-and-reset path itself wedges, leaving nothing running to perform the reboot.
**No Windows setting can recover from this.** Recovery has to come from outside the OS.

---

## 3. Diagnostic commands (re-run these after the next crash)

```powershell
# Shutdown / bugcheck history
Get-WinEvent -FilterHashtable @{LogName='System'; Id=41,1001,6008} -MaxEvents 40 |
  Select-Object TimeCreated, Id, Message | Format-Table -Wrap

# Was it a real bugcheck, or a silent hang? (BugcheckCode 0 = hang, nothing recorded)
Get-WinEvent -FilterHashtable @{LogName='System'; Id=41} -MaxEvents 4 | ForEach-Object {
  $x=[xml]$_.ToXml(); "--- $($_.TimeCreated) ---"
  $x.Event.EventData.Data | Where-Object { $_.Name -like 'Bugcheck*' } |
    ForEach-Object { "  $($_.Name) = $($_.'#text')" } }

# Microcode revision actually loaded (bytes are little-endian: 2F 01 00 00 = 0x12F)
$p = Get-ItemProperty 'HKLM:\HARDWARE\DESCRIPTION\System\CentralProcessor\0'
($p.'Update Revision' | ForEach-Object { '{0:X2}' -f $_ }) -join ' '

# Sleep states actually available
powercfg /a ; powercfg /lastwake
```

### Post-crash integrity checklist

Run in this order — the first two have caught real damage before:

1. `git status --short` + `git diff` — a stray keystroke saved into the wrong PyCharm
   window once put `like ` into line 1 of `cook_ask.py`
   (`memory/feedback_stray_keystroke_corruption.md`).
2. `python -m compileall -q .` — must exit 0.
3. DB: `PRAGMA quick_check` / `integrity_check`, confirm `journal_mode = wal`, spot-check
   row counts (`recipes`, `products`, `reviews`, `review_products`, `dishes`, `jobs`).
4. **Verify the backup dump** — `gzip -t recipes.sql.gz`. See §6; this is the one that
   was actually broken on 2026-07-29.
5. App: `Invoke-WebRequest http://localhost:8009/` and `Get-Service BCC, Cloudflared`.
6. `git status -sb` — check how many commits are unpushed. On a host that hangs every
   few days, unpushed work is real exposure.

---

## 4. The application side is blameless

Verified 2026-07-29 after the manual power-on: `BCC` service Automatic and Running,
app answering HTTP 200 on `127.0.0.1:8009`, `Cloudflared` Running. Once the machine
POSTs, **everything comes back on its own.** The only failure mode is the box never
POSTing. That is what the watchdog in §5 exists to solve.

---

## 5. The watchdog — design

### 5.1 The linchpin is a BIOS setting, not the plug

A smart plug can only cut and restore power. If the BIOS does not boot when power
returns, the whole scheme does nothing.

> **HP BIOS → Advanced → Power-On Options → "After Power Loss" = Power On**

**Set this first and test it by pulling the cord.** If the host does not come back by
itself, stop — no plug will help.

### 5.2 Second gotcha, same shape

The plug itself must default to **ON** when *it* loses power (Shelly calls this
"Power on default"). If a real outage hits and the plug returns OFF, the host stays
dark and you are strictly worse off than today.

### 5.3 Which plug

It must have a **local API**. Cloud-only plugs (Amazon Smart Plug, most Wyze) fail
exactly when needed — if the internet or the vendor cloud is down, so is the watchdog.

**Shelly Plug US (Gen3)**, ~$20–25 — fully local HTTP, no account:

```
http://<plug-ip>/rpc/Switch.Set?id=0&on=false     # cut
http://<plug-ip>/rpc/Switch.Set?id=0&on=true      # restore
http://<plug-ip>/rpc/Switch.GetStatus?id=0        # includes apower (watts)
```

Power metering is a real bonus: the watcher can *confirm* the outlet cut and came back
rather than assume. TP-Link Kasa KP125M via `python-kasa` is an acceptable second
choice, but TP-Link keeps drifting toward cloud-required setup.

Draw is a non-issue — this host peaks well under 300W against a 1875W plug.

### 5.4 Where the watcher runs

**On ADAM** (Synology, 192.168.1.235). DSM **Control Panel → Task Scheduler →
Create → Scheduled Task → User-defined script**, every 1 minute. DSM's built-in
notification system handles the alert email. No extra hardware beyond the plug.

### 5.5 The logic that actually matters

The naive version — "ping fails, cut power" — will cause damage. Three guards:

**Discriminate hung from merely-unhealthy.** Ping the host *and* check port 8009.
ICMP answering but the port dead is an *app* problem; restarting the service is the fix,
not a power cut. Only network-layer silence earns a power cycle. Check **both** IPs
(.177 and .250) — alive on either means alive.

**Do not false-fire on your own network.** Before declaring the host dead, confirm the
gateway (192.168.1.1) still answers. Otherwise a router hiccup power-cycles a healthy
machine.

**Circuit-break, do not loop.** ~5 consecutive failures at 60s → cut 20s → restore →
**hard 20-minute cooldown** while it boots. If a second cycle is needed inside an hour,
**stop and email instead**. Without this, a host that cannot POST gets hammered with
power cuts all night.

A Windows Update reboot is normally under the 5-minute threshold and will not trip it.

### 5.6 Honest limits

- It is a hard power cut. `recipes.db` survives — SQLite WAL is crash-safe, and the
  2026-07-29 crash proved it (`quick_check ok`). But an in-flight curate / RealRank job
  dies mid-run, and repeated hard cuts are wear on the SSD.
- If the CPU degrades to where it will not POST, power-cycling accomplishes nothing.
- **This buys uptime. It does not fix anything.** Pursue the RMA in parallel (§7).

### 5.7 Alternative: USB watchdog card

Technically cleaner. A card wired to the motherboard **reset header**; a daemon on
MARLEY pets it every few seconds, and when the CPU hangs the petting stops and the card
pulses reset. No power cut, no dependence on the BIOS AC setting, no second machine.
The catch is opening the case and finding the front-panel reset header on an HP
prebuilt, which is sometimes proprietary.

A **PiKVM** wired to the power header adds remote console — you would actually *see*
the next BSOD instead of inferring it from the event log. Bigger project, much better
diagnostics.

---

## 6. Collateral damage pattern — the backup dump

`recipes.db` is untracked; **`recipes.sql.gz` is the git-side backup**
(`memory/project_db_backup.md`). On 2026-07-29 it was found **truncated** — 31.3 MB,
`gzip: unexpected end of file`.

Cause: the daily 03:00 task missed its slot (host was dead), fired as a catch-up at
13:46:05 after boot, and was **killed 15s in** — `backup.log` ends in `^C`, scheduled-task
result `3221225786` (`0xC000013A`, STATUS_CONTROL_C_EXIT). Rare but not unique: 2 such
kills in 60 logged runs.

Nothing was lost — HEAD's committed copy was intact (36.9 MB) and ADAM's newest good
copy was 2026-07-28 03:01 — but there was a window where a corrupt dump sat in the
working tree looking like a backup. **A truncated dump is a silent data-loss trap;
always `gzip -t` it after a crash.**

Repaired by re-running the backup → 39.2 MB, verifies clean, ends with `COMMIT;`, fresh
ADAM copy `recipes_2026-07-29_135240`.

> **Gotcha:** `bcc_backup_scheduled.bat` does **not** survive being invoked from Git Bash
> — the venv `activate.bat` swallows the rest of the script and Python never runs
> (exit code is still 0, so it looks like it worked). Call the interpreter directly:
>
> ```powershell
> & "C:\Users\john\PyCharm\venv\Scripts\python.exe" backup_db.py --dest "\\Adam\tbotb\Backups\recipes-db"
> ```

---

## 7. Actions

### Applied 2026-07-29

- **Minimum processor state 5% → 100% on AC.** Keeps cores out of the low-voltage
  P-states where a degraded Raptor Lake wedges. Costs watts and idle heat.
  ```powershell
  powercfg /setacvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN 100
  powercfg /setactive SCHEME_CURRENT
  # verify: powercfg /q SCHEME_CURRENT SUB_PROCESSOR PROCTHROTTLEMIN  → 0x64
  ```
- Truncated `recipes.sql.gz` repaired and re-verified (§6).

### TODO — needs admin

- **Disable Fast Startup.** For an always-on server it turns shutdown into a hibernate
  and can wedge recovery. Requires an elevated shell (the agent cannot elevate; see
  `memory/project_restart_zombie_port.md`):
  ```powershell
  Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' `
    -Name HiberbootEnabled -Value 0
  ```

### TODO — needs a reboot into BIOS

- **"After Power Loss" = Power On** (§5.1) — mandatory before the plug is worth buying.

### TODO — hardware

- Buy the Shelly Plug US, set its power-on default to ON, write the ADAM watcher (§5).

### The actual fix — claim it. Best Buy FIRST.

**Purchase — from the Best Buy receipt (authoritative):**

| | |
|---|---|
| Purchase date | **2023-11-21** (picked up 11-22, Framingham MA) |
| Order number | `BBY01-806817898714` |
| Product | HP Envy Desktop i7 / 16GB / 1TB SSD — $749.99 (total $934.98) |
| Model / BBY SKU | `7H0U6AA#ABA` / `6532244` |
| Serial | **`2MO334319K`** |
| Protection purchased | Monthly Best Buy Protection, **up to 24 mo.**, SKU `6420466` |

> **Correction:** an earlier revision of this doc put the purchase at 2025-01 based on
> the `john` profile creation time (2025-01-11). That was a **Windows reinstall**, not
> first setup — which is why the factory image dates 2024-04-01, *after* the real
> purchase. Do not date this machine from the filesystem; use the receipt.

Coverage, measured from 2023-11-21:

| route | window | status |
|---|---|---|
| HP system warranty (1 yr) | → 2024-11 | expired |
| Best Buy Protection (up to 24 mo.) | → **2025-11-21** | **EXPIRED ~8 months ago** |
| **Intel Vmin-shift extended (5 yr)** | → **2028-11-21** | **OPEN — the only live route** |

**Best Buy is out.** The 24-month cap ran out 2025-11. There is also no
"fault arose during coverage" argument available: the documented crash history starts
2026-04-14, after the window closed. Worth one phone call to confirm the exact expiry on
the `Monthly Best Buy Protection` line, but do not plan around it.

Note the My Best Buy Total membership (~$200/yr) still covers **future** purchases for
24 months — relevant if this ends in buying a replacement machine, not in saving this one.

⚠️ **The RAM is not as-sold — 64 GB (2×32 GB Crucial) against the 16 GB on the
receipt.** Expect it to be raised as a cause, and close the door first:

- Magnuson-Moss means a user-installed part cannot void coverage unless it caused the
  failure — but it is the easiest available deflection, and crashes are the exact
  symptom people pin on memory.
- **Best fix: refit the original HP sticks** so the machine presents as-sold.
- Otherwise run **MemTest86** overnight and keep a clean pass on file. Caveat: a degraded
  CPU can crash MemTest itself, and that result would be misread as a RAM fault — do not
  submit a test that died mid-run.
- The RAM is almost certainly not the cause: `0x101` is a *core hang*. Real memory
  faults surface as `0x1A` / `0x50` / `0x4E` / WHEA `0x124`, none of which appear here,
  and the Crucial runs at JEDEC 3200 on a locked HP board (no XMP).

### The only live route — RMA the CPU via Intel (open to 2028-11-21)

**The F.45 microcode test has failed, and that is the whole argument.** BIOS was
flashed F.40 → F.45 (microcode ≥0x12B, now 0x12F) on **2026-06-23** specifically to see
whether the idle-shutdown cadence would stop. It did not — **four** further crashes
followed: 6/25, 7/10, 7/24, 7/29, one of them a recorded `0x101`. Per the plan recorded
at the time, that outcome escalates to a hardware claim.

Intel extended the 13th/14th-Gen warranty on the **processor** by 2 years
(3 → cumulative **5 years**), and the extension explicitly covers **boxed, tray, AND
OEM/system-integrator** chips. Purchased 2023-11-21 → **open until 2028-11-21**, about
two years and four months of runway. The clock runs from purchase, and the Best Buy
receipt (order `BBY01-806817898714`) is the proof — the single document this claim most
needs, already in hand.

**Routing — verified 2026-07-29. This corrects an earlier note in
`memory/project_host_thermal_shutdowns.md` that said to go to Intel directly.**
Intel's published routing is by processor type:

| type | where to file |
|---|---|
| Boxed (retail) | Intel directly |
| Tray | place of purchase |
| **OEM / system integrator ← this host** | **the system manufacturer = HP** |

So the order is **HP first, Intel second** — and HP's expired *system* warranty does not
end it, because Intel's remediation clause is the second step:

> *"If customers have experienced instability symptoms on their 13th and/or 14th Gen
> desktop processors but were **unsuccessful in prior RMAs**, Intel asks that they reach
> out to Intel Customer Support for further assistance and remediation."*

A documented HP refusal is therefore not a dead end — it is the **entry ticket** to
Intel escalation. Get it in writing. HP's own forum carries a thread for exactly this
case ("RMA Request - Intel 13th Gen CPU Degradation (Vmin Shift Instability / Intel
Extended Warranty Program)"), so their support org has seen it before.

**Frame the HP call as the Intel defect program, not "my PC crashes."** Front-line
support will close a crash ticket on the expired system warranty; the Vmin-shift
extension is a separate, CPU-level program.

**Evidence to attach** — this case is unusually strong:

- The `0x101 CLOCK_WATCHDOG_TIMEOUT` bugchecks and the full Event 41 list (§2).
- **The BIOS before/after.** The strongest single exhibit: the official mitigation was
  applied on a recorded date (F.40→F.45, microcode 0x12F) and the crash cadence did not
  stop. `kernel_power_check.bat` in the project root tags each Event 41 pre-flash vs
  `>>> POST-FLASH` against the 2026-06-23 baseline — export it before starting.
- **Overclocking cannot be alleged.** Locked non-K chip in a locked OEM prebuilt on
  stock HP firmware. Intel's standing deflection ("run Intel Default Settings") is
  unavailable against this system. Say so explicitly.

**Expect IPDT to pass.** Intel may ask for the Intel Processor Diagnostic Tool; it
frequently passes on degraded chips, and Intel has itself acknowledged having no
reliable detection tool for this defect. A passing IPDT is not evidence of a healthy
CPU — do not accept it as grounds for denial.

**Ask for Advance/Rapid Replacement.** This host runs the app; standard RMA means
shipping the CPU first and sitting dark for the round trip. Advance replacement ships
first against a card hold, with 30 days to return the defective part.

**Physical swap:** Intel replaces the chip, not the system — someone has to pull the
i7-13700 and fit the replacement (LGA1700, plus thermal paste; HP coolers are often
proprietary mounts). A shop does this in under an hour if you would rather not.

Reported RMA experience is mixed — some smooth, some slow with "failed validation"
retentions. **Document everything before shipping anything.**

Everything above is a stopgap.

---

## References

- [Tom's Hardware — Raptor Lake instability, 0x12F](https://www.tomshardware.com/pc-components/cpus/raptor-lake-instability-saga-continues-as-intel-releases-0x12f-update-to-fix-vmin-instability)
- [VideoCardz — Intel addresses Vmin Shift Instability with 0x12F](https://videocardz.com/newz/intel-addresses-13-14th-gen-core-raptor-lake-vmin-shift-instability-with-new-0x12f-microcode-update)
