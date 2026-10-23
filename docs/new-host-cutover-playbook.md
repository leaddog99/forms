# New host cut-over playbook — cloning MARLEY onto the new desktop

*2026-10-17. Sequence approved by the curator: **move first, convert later.**
The new box becomes an exact SQLite clone of MARLEY, soaks beside it, takes
over, runs for a week, and only then gets PostgreSQL 18 and the migration
(`docs/postgres-migration-inventory.md`, whose step 4 "cutover on MARLEY" is
superseded by this document). Every fact below was read off MARLEY on
2026-10-17; the BAILEY restore drill of 2026-08-24 (`docs/disaster-recovery.md`
§6 G5) is the rehearsal this playbook is built on.*

Why this order: a host move with identical software is a copy you can verify
exactly; a Postgres conversion changes behaviour on purpose. One variable at a
time, and the sick box (sixteen thermal shutdowns) never runs the hours-long
ETL. MARLEY stays alive as the frozen SQLite reference until the new engine's
results have been diffed against it.

---

## 0. Decisions to make before touching the new box

1. **Same user name, same paths.** Eleven scripts hard-code
   `C:\Users\john\PycharmProjects\forms` and `C:\Users\john\PyCharm\venv`
   (bcc_backup*.bat, bcc_schedule.bat, bcc_start.bat, bcc_sync_standby.ps1,
   bcc_lock_if_idle.ps1, bcc_rclone_client_id.ps1, kernel_power_check.bat,
   backup_check.py, bcc_install_service_bailey.ps1). Create the Windows account
   as **john** with a local profile at `C:\Users\john`, or spend a day editing
   paths. Do not let Windows name the profile folder after a Microsoft-account
   email.
2. **Host name: MARLEY2** (named HESTIA 2026-10-17, renamed by the curator 2026-10-21; built as `MARLEY2`, no underscore, confirmed 2026-10-22 — it registers on the LAN as `MARLEY2` / 192.168.1.196). Soak hostname `marley2.tbotb.com`, rclone remote `marley2`. It is the rclone remote (`marley2`),
   the soak tunnel hostname, and the memory rule "restart = service + host +
   script" after the flip.
3. **Edition.** MARLEY is Windows 11 Home. Pro is nicer (no Home autologon
   gymnastics for scheduled tasks are needed either way, but Pro has gpedit and
   Remote Desktop). Either works; the steps are the same.
4. **Power.** This is a server. Plugged in: never sleep, never hibernate, wake
   timers allowed (the 03:00 backup task wakes the machine), BIOS "power on
   after power loss" on, fast startup off. MARLEY runs an "HP Recommended" plan;
   do not copy it.

---

## 1. What has to move — the inventory

Measured on MARLEY, 2026-10-17.

| asset | where on MARLEY | size | how it moves |
|---|---|---|---|
| working tree + `.git` | `C:\Users\john\PycharmProjects\forms` | ~130 MB | `git clone` + the untracked bits (`generated/`, `input/`, `.env`) by copy; or robocopy the whole tree |
| `recipes.db` | same | 948 MB | **frozen copy** (server stopped) or `backup_db.py` output; WAL mode — never copy an open file |
| `training.db` | same | 1,130 MB | frozen copy |
| `media.db` | same | 611 MB | frozen copy |
| `page_cache.db` | same | 695 MB | optional, but copy it at the final freeze: it is paid fetches, 5-day TTL |
| `generated/` | same | 2,731 MB | robocopy; **user-uploaded heroes live here, irreplaceable** |
| `input/` | same | 125 MB | robocopy (SEMrush exports, pipeline inputs; `input/captures/` is per-machine) |
| `logs/` | same | 904 MB | optional history; not needed to run |
| `.env` | same | 27 keys | copy the file; also `OPENAI_API_KEY` exists as a **User** environment variable on MARLEY, recreate it |
| `recipe-core` | `C:\Users\john\PycharmProjects\recipe-core` | tiny | **not a git repo, backed up only to ADAM `recipe-core-mirror`** — copy the folder |
| `imagekit`, `llmkit` | `C:\Users\john\PycharmProjects\…` | tiny | `git clone` from github.com/leaddog99/imagekit and /llmkit (check both are pushed first) |
| Python venv | `C:\Users\john\PyCharm\venv` | 41 MB | rebuild: Python 3.13.1 + `requirements-frozen.txt` (154 lines, three `-e` editable siblings) |
| Playwright browsers | `%LOCALAPPDATA%\ms-playwright` | — | `playwright install chromium` (headed capture walker + screenshots) |
| nssm | `C:\tools\nssm\nssm-2.24-101-g897c7ad\win32\nssm.exe` | — | copy the folder (not on PATH; `tools\nssm.exe` in the repo is absent on MARLEY) |
| rclone | winget `Rclone.Rclone` **and** a copy at `C:\Users\john\bin\rclone.exe` | — | the backup bats call the `bin` copy by path — put one there |
| rclone config | `%APPDATA%\rclone\rclone.conf` | — | copy: remotes `gdrive` (Drive token) and `bailey` (SFTP, key file) |
| SSH key | `C:\Users\john\.ssh\id_ed25519` (+ `.pub`, `known_hosts`) | — | copy; BAILEY's `authorized_keys` already trusts it |
| SMB credential for ADAM | Credential Manager, target `adam` | — | `cmdkey /add:adam /user:… /pass` on the new box; `\\Adam\tbotb` must be writable or the nightly fails silently |
| Cloudflare tunnel | service `Cloudflared`, `"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel run --token …` | — | token from the Zero Trust dashboard; see §5. The `C:\Windows\System32\cloudflared.exe` on MARLEY is a wrong-platform binary — do not copy it |
| Claude Code memory | `C:\Users\john\.claude\projects\C--Users-john-PycharmProjects-forms\memory\` | small | copy `C:\Users\john\.claude` wholesale (memory, settings); BAILEY got the same seed |
| PyCharm settings, Claude Code, Chrome + Claude-in-Chrome extension | per-app | — | reinstall; export PyCharm settings if wanted |

**Not moving:** PostgreSQL 17 + pgagent (MARLEY's instance is unused by the app;
the new box gets **18**, fresh, in the migration phase), Cloudflare WARP client
(not needed by the app), the capture-walker browser profile
(`data/browser_profiles/`, signed-in sessions are per machine — log in again),
`identifier.sqlite`, `recipes.sqbpro`.

Services and tasks that must exist on the new box at the end (all read off
MARLEY):

| moving part | MARLEY configuration |
|---|---|
| NSSM service **BCC** | `C:\Users\john\PyCharm\venv\Scripts\python.exe -m uvicorn save_recipe_api:app --host 127.0.0.1 --port 8009`, AppDirectory = the project, stdout/stderr → `uvicorn_stdout.log` / `uvicorn_stderr.log` in the project, auto start, LocalSystem |
| service **Cloudflared** | token-run tunnel, auto start |
| task **BCC Recipes DB Backup** | daily 03:00, `bcc_backup_scheduled.bat`, run as john (Interactive), wake to run, 2 h limit |
| task **BCC Backup Check** | daily 07:00, `bcc_backup_check.bat`, john |
| task **BCC Dish Schedule** | every 1 h from 01:00, `bcc_schedule.bat`, john, 3 h limit |
| task **BCC Lock After Autologon** | at logon of john, 60 s delay, `bcc_lock_if_idle.ps1` |
| autologon | Sysinternals Autologon (the john tasks are Interactive: without a logged-on session they wait at the sign-in screen after a reboot) |

---

## 2. Build the new box (day 1, nothing touches MARLEY)

Expect two to three hours including downloads. BAILEY's bare-metal restore took
35 minutes with three surprises; this is the same work plus the dev tooling.

1. **Windows.** Account **john**, local profile. Updates to current. Name the
   machine `MARLEY2`. Power settings from §0.4. Turn on **OpenSSH Server**
   (Settings → Optional features), set it to Automatic, and add MARLEY's
   `id_ed25519.pub` to `C:\ProgramData\ssh\administrators_authorized_keys`
   (john is an administrator, so the per-user `authorized_keys` is ignored).
   This is how the sync from MARLEY will reach the box, exactly as it reaches
   BAILEY.
2. **Install:** Python 3.13.1 (python.org, "for this user", so it lands at
   `C:\Users\john\AppData\Local\Programs\Python\Python313` like MARLEY), Git for
   Windows, Node.js (the lint uses `node --check`), rclone via winget plus a copy
   at `C:\Users\john\bin\rclone.exe`, cloudflared (the official MSI, lands in
   `C:\Program Files (x86)\cloudflared`), PyCharm, Claude Code, Chrome. Copy the
   nssm folder to `C:\tools\nssm\…` from MARLEY.
3. **Code.**
   ```
   mkdir C:\Users\john\PycharmProjects
   cd C:\Users\john\PycharmProjects
   git clone http://github.com/leaddog99/forms
   git clone https://github.com/leaddog99/imagekit
   git clone https://github.com/leaddog99/llmkit
   ```
   `recipe-core` has no repo: copy `C:\Users\john\PycharmProjects\recipe-core`
   from MARLEY (or `\\Adam\tbotb\Backups\recipe-core-mirror`).
   In `forms`, check out `split/enrichment-api` (the working branch).
4. **Environment.**
   ```
   python -m venv C:\Users\john\PyCharm\venv
   C:\Users\john\PyCharm\venv\Scripts\python -m pip install -r C:\Users\john\PycharmProjects\forms\requirements-frozen.txt
   C:\Users\john\PyCharm\venv\Scripts\python -m playwright install chromium
   ```
   The three `-e c:/users/john/pycharmprojects/…` lines resolve only because
   the paths match (§0.1). Install `pre-commit` hooks: `pre-commit install` in
   the repo (ruff F821/F822/F811/E9 gate).
5. **Secrets and credentials.** Copy `.env` from MARLEY into the project root
   (or `env.backup` from ADAM). Set the User env var `OPENAI_API_KEY`. Copy
   `C:\Users\john\.ssh\` and `%APPDATA%\rclone\rclone.conf`. `cmdkey /add:adam
   /user:<adam user> /pass:<adam password>`, then prove it: `dir \\Adam\tbotb\Backups`.
   Copy `C:\Users\john\.claude\`.
6. **First data load (a soak copy, not the final one).** Run on MARLEY
   `python backup_db.py` to get a consistent set, then copy from ADAM's
   newest set onto the new box: `recipes_<ts>.db → recipes.db`,
   `training_<ts>.db → training.db`, `media_latest.db → media.db`. Robocopy
   `generated\` and `input\` from MARLEY (SMB or the SFTP path in §3).
7. **Service.** From an **elevated** PowerShell, run the install script,
   which is MARLEY's NSSM parameters verbatim:
   ```
   powershell -ExecutionPolicy Bypass -File C:\Users\john\PycharmProjects\forms\bcc_install_service_bailey.ps1
   ```
   It expects nssm at `forms\tools\nssm.exe`; copy it there or edit `$nssm`.
   Then `bcc_restart.bat` works on this box.
8. **Prove it** (same proof as the drill): `http://127.0.0.1:8009/auth/me`
   answers; the recipe form loads and search returns rows; `/dish-coverage`
   loads; save one test recipe and delete it; `python backup_db.py --verify`
   prints `RESTORE OK`; `python scripts\check_embeddings.py` is clean;
   `python -m intake.tests.test_youtube_source` and `python test_fts_query.py`
   pass. Expected row counts on 2026-10-17: master_recipes 13,217 · recipes 473
   · dishes 467 · domains 514 · users 7 · 91 tables; the FTS index must hold
   13,217 rows after first start.

Do **not** yet create the scheduled tasks on the new box. Two hosts running
`bcc_schedule.bat` means every due dish is refreshed twice and paid for twice.

---

## 3. Soak (one to two weeks, MARLEY stays live)

1. **Reach it from outside** without touching production: in the Zero Trust
   dashboard create a second tunnel (`marley2`) with public hostname
   `marley2.tbotb.com → http://localhost:8009`, install its token as the
   Cloudflared service on the new box. This mirrors `bailey.tbotb.com`. Use
   the bookmarklet, the iPad and the voice loop against that hostname.
   `bcc_link_domain` in system_config stays `recipes.tbotb.com`, so links the
   soak instance mints still point at production — correct.
2. **Keep it fresh** from MARLEY. Create the rclone remote once:
   ```
   rclone config create marley2 sftp host MARLEY2 user john key_file C:/Users/john/.ssh/id_ed25519 shell_type cmd md5sum_command none sha1sum_command none
   ```
   then `.\bcc_sync_standby.ps1 -Standby MARLEY2` (code, assets and the three
   sibling packages; the server stays up) or `-WithDbs` (stops MARLEY2's
   service, lays in the newest ADAM set with size-verified copies, restarts).
   The nightly only syncs BAILEY; add a MARLEY2 line to
   `bcc_backup_scheduled.bat` once MARLEY2's sshd and remote exist, not before,
   or every morning's check reports a failed sync.
3. **Watch for the drill's four lessons:** editable package missing (boot
   fails at import), backslashes in requirements, a server started from an
   SSH session dying with the session (hence NSSM), and ADAM credentials
   absent (backups fail silently). Add a fifth: the FTS and vec tables rebuild
   on first start from the dump path only; a `.db` copy carries them.
4. **Thermal baseline.** Run a dish refresh batch on the new box
   (`python -m jobs run dish_refresh <name>` by dish name, never by query) and
   watch temperatures; this is the load that killed MARLEY.

---

## 4. Cut-over day (one to two hours, mostly waiting on copies)

Pick a quiet hour; no batch dishes running (check `/jobs`). Steps in order,
each reversible until step 6.

1. **Freeze MARLEY.** Disable the three recurring tasks (`schtasks /Change
   /TN "BCC Dish Schedule" /DISABLE`, likewise "BCC Recipes DB Backup" and
   "BCC Backup Check"). Stop the service: `Stop-Service BCC` elevated, then
   `Set-Service BCC -StartupType Disabled`. Nothing writes to recipes.db now.
2. **Final consistent set.** On MARLEY run `python backup_db.py --verify
   --dest \\Adam\tbotb\Backups\recipes-db`; wait for `RESTORE OK`. This also
   leaves a dated rollback copy on ADAM.
3. **Final delta to the new box.** Stop BCC on the new box. Copy
   `recipes.db`, `training.db`, `media.db`, `page_cache.db` (the server is
   stopped on both sides, so the live files are safe to copy; checkpoint first
   with `python -c "import sqlite3; sqlite3.connect('recipes.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"`
   on MARLEY). Robocopy `generated\`, `input\`, `.env` once more. `git pull` in
   forms, imagekit, llmkit on the new box; copy recipe-core again if it changed.
4. **Start and compare.** Start BCC on the new box. Compare row counts of
   recipes, master_recipes, dishes, domains, jobs, users between the two files
   (they must be equal), then run the §2.8 proof again.
5. **Tunnel flip.** Stop and disable the `Cloudflared` service on MARLEY.
   On the new box, replace the soak tunnel with production: `cloudflared
   service uninstall`, then `cloudflared service install <recipes tunnel token>`
   (the token is in Zero Trust → Networks → Tunnels → the recipes tunnel →
   Configure). Same tunnel, same hostname, no DNS change: within a minute
   `https://recipes.tbotb.com/auth/me` is served by the new box (confirm with
   the worker PID in `uvicorn_stdout.log`). Keep `marley2.tbotb.com` on the
   second tunnel if you want a permanent back door; otherwise delete it.
6. **Give the new box the jobs.** Create the four tasks (run as john,
   Interactive, "run with highest privileges" not needed):
   ```
   schtasks /Create /TN "BCC Recipes DB Backup" /TR "C:\Users\john\PycharmProjects\forms\bcc_backup_scheduled.bat" /SC DAILY /ST 03:00 /RU john
   schtasks /Create /TN "BCC Backup Check"      /TR "C:\Users\john\PycharmProjects\forms\bcc_backup_check.bat"     /SC DAILY /ST 07:00 /RU john
   schtasks /Create /TN "BCC Dish Schedule"     /TR "C:\Users\john\PycharmProjects\forms\bcc_schedule.bat"         /SC HOURLY /ST 01:00 /RU john
   schtasks /Create /TN "BCC Lock After Autologon" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\john\PycharmProjects\forms\bcc_lock_if_idle.ps1" /SC ONLOGON /RU john /DELAY 0001:00
   ```
   Then in Task Scheduler set: Backup → wake the computer, 2 h limit; Dish
   Schedule → 3 h limit. Configure Sysinternals Autologon for john. Run the
   backup task by hand once (`schtasks /Run /TN "BCC Recipes DB Backup"`) and
   read `backup.log`: `RESTORE OK`, ADAM copies written, `cloud mirror exit
   code: 0` (if the shared rclone client is rate-limited that night, that is
   G4, not the move).
7. **The BAILEY sync now runs from the new box.** Same script, same `bailey`
   remote, same key. Run it once by hand.
8. **Finish the rclone client-ID job (G4) here, not on MARLEY:**
   `bcc_rclone_client_id.ps1` in your own PowerShell window on the new box.

---

## 5. After the flip

- **MARLEY is the frozen rollback for seven days.** BCC disabled, Cloudflared
  disabled, tasks disabled, box on. Rollback = stop Cloudflared on the new
  box, start it and BCC on MARLEY; any recipe saved on the new box in the
  meantime must be copied back by hand, so the honest rollback window is the
  first day. After the week, power MARLEY down; it has sixteen crashes of
  evidence.
- **Paper that names the host:** `docs/disaster-recovery.md` §5 (service and
  tasks table), the memory rule "restart = service + host + script" (BCC on
  MARLEY2), `bcc_lock_if_idle.ps1` header, `bcc-state-code.md` START HERE,
  the default `-Standby` in `bcc_sync_standby.ps1` (BAILEY → whatever stands by next). `docs/host-stability-and-
  watchdog.md` gets its closing entry.
- **Then, and only then, Postgres 18** on the new box: install, `pip install
  psycopg[binary]`, run the ETL from the inventory against the live SQLite
  with MARLEY's frozen copy and BAILEY as the comparison engines. The search
  grammar tests (`test_fts_query.py`) and the row-count table above are the
  first two diffs the port must pass.

---

## 6. Checklist (print this)

```
[ ] account john, profile C:\Users\john, host named, power plan = server
[ ] OpenSSH Server on, MARLEY key in administrators_authorized_keys
[ ] Python 3.13.1 · Git · Node · rclone (+ C:\Users\john\bin\rclone.exe) · cloudflared · nssm folder · PyCharm · Claude Code · Chrome
[ ] forms / imagekit / llmkit cloned, recipe-core copied, branch split/enrichment-api
[ ] venv at C:\Users\john\PyCharm\venv, requirements-frozen.txt, playwright chromium, pre-commit install
[ ] .env · OPENAI_API_KEY (User) · .ssh · rclone.conf · cmdkey adam · .claude
[ ] soak DBs + generated + input laid in; NSSM BCC installed; §2.8 proof passes
[ ] soak tunnel marley2.tbotb.com; bookmarklet, iPad, voice tried against it
[ ] nightly sync MARLEY → MARLEY2 for the soak; thermal baseline under a dish batch
--- cut-over ---
[ ] MARLEY tasks disabled, BCC stopped + disabled, backup_db --verify RESTORE OK
[ ] final copy of the four DBs (checkpointed), generated, input, .env; git pull x3
[ ] new box started; row counts equal; proof passes
[ ] Cloudflared: MARLEY stopped + disabled, production token installed on MARLEY2, recipes.tbotb.com answers from MARLEY2
[ ] four tasks created, limits + wake set, autologon on, backup run by hand = RESTORE OK
[ ] BAILEY sync run from MARLEY2; rclone client-ID script run on MARLEY2
[ ] docs + memory renamed to MARLEY2; MARLEY frozen one week, then off
```
