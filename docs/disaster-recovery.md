# Disaster Recovery — the whole scheme

**Written 2026-08-24**, the day the scheme was completed: the git-side dump was
retired (repo 1.6GB → 31MB), the offsite tier moved to Google Drive, the nightly
gained `--verify`, and the DR audit found and closed two unprotected assets
(`media.db`, `generated/`). Context that motivates all of it:
[[project_host_thermal_shutdowns]] — this host is a confirmed-degrading 13th-gen
Intel with an RMA pending. Machine death is not hypothetical here.

---

## 1. What state exists, and where it lives

Everything the system is lives in one of these places:

| asset | what it is | size | replaceable? |
|---|---|---|---|
| working tree (code, forms, docs, bats, `input/` exports) | the application | ~50MB | git |
| `recipes.db` | THE database: recipes, dishes, domains, jobs, embeddings | ~465MB | **no** |
| `training.db` | is-recipe corpus, gold human labels | ~580MB | **no** |
| `media.db` | page screenshots + TTS audio blobs | ~360MB | partly (anti-bot & user-browser captures are not) |
| `generated/` | og-thumbs, **user-uploaded hero images**, generated art | ~2.1GB | thumbs mostly; **uploads no** |
| `.env` | every API key we hold (Anthropic, OpenAI, Moz, SEMrush, ScaleSERP, unblocker, mail…) | 4KB | painfully (re-issue per vendor) |
| `page_cache.db` | raw fetched pages, 5-day serving TTL | ~950MB | yes — it is a cache, deliberately unprotected |
| `.venv` / PyCharm venv | Python environment | 51MB+ | yes — `requirements-frozen.txt` |
| logs/ | job + server logs | ~60MB | diagnostic only |

Plus configuration that is NOT files in this tree (see §5 rebuild list): the NSSM
service "BCC", the Windows scheduled task "BCC Recipes DB Backup", the rclone
remote `gdrive`, the Cloudflare tunnel (recipes.tbotb.com → :8009), and ADAM's
share `\\Adam\tbotb`.

---

## 2. The protection tiers

### Tier 0 — git + GitHub (code, continuous)
The working tree, pushed on every substantive commit to
`github.com/leaddog99/forms`. **Code only** since 2026-08-24: the dump was
untracked (step A) and all 29 historical dump blobs purged via `git filter-repo`
+ force-push (step B) — `.git` went 1.6GB → 31MB. The dump's offsite role moved
to Tier 3. `input/semrush/` exports ARE tracked (they are inputs, not artifacts).

### Tier 1 — the nightly chain (Windows Task Scheduler, 3:00 AM)
Task **"BCC Recipes DB Backup"** → `bcc_backup_scheduled.bat` → everything below,
appended to `backup.log`. Run by hand anytime: `schtasks /Run /TN "BCC Recipes DB Backup"`.

1. **`backup_db.py --verify --dest \\Adam\tbotb\Backups\recipes-db`**
   - regenerates `recipes.sql.gz` (stable-key-ordered dump; vec0/fts5 virtual
     tables excluded BY CLASS — they rebuild from tables that are in the dump)
   - copies to ADAM, timestamped: `recipes_<ts>.db` + `recipes_<ts>.sql.gz`
     (each with `PRAGMA integrity_check`), `training_<ts>.db`
   - **rolling** single copies: `media_latest.db` (added 2026-08-24) and
     `env.backup` (plaintext — ADAM is our LAN; the off-site key copy is the
     password manager, a human responsibility)
   - **`--verify` replays the fresh gz into a throwaway DB and compares every
     table's row count.** In the NIGHTLY since 2026-08-24 — the dump shipped
     unrestorable TWICE (generated columns 2026-07-22; fts5 2026-08-20) and both
     times nobody noticed for days because only hand-run backups verified.
     *A backup nobody restores is a hypothesis.* Look for `RESTORE OK` in
     `backup.log` every morning.
2. **Offsite: rclone → Google Drive**, three folders under `gdrive:BCC-Backups/`:
   - `recipes-db/` — `recipes_*.sql.gz`, rolling 14-day window (~1.8GB)
   - `forms-mirror/` — the full project mirror (code + `generated/` incl. hero
     uploads + `input/`), rolling
   - `db-latest/` — `media_latest.db` + the freshest `training_*.db` (2-day window)
   `env.backup` stays OFF the cloud by policy — the offsite key copy is the
   password manager. Remote `gdrive` = `drive.file` scope (rclone sees only its
   own files), authorized 2026-08-24, token in `%APPDATA%\rclone\rclone.conf`.
   Total offsite ≈ 5GB against a 5TiB quota.
3. **Project mirror: robocopy `/MIR` → `\\Adam\tbotb\Backups\forms-mirror`.**
   Everything the other tiers miss — `generated/` (the irreplaceable hero
   uploads), `input/`, bats, docs, the working tree AND `.git`. Live SQLite
   files are excluded (their consistent copies come from step 1; a robocopy of
   an open WAL database can be torn), as are `page_cache.db`, `__pycache__`,
   `.venv`. Seeded 2026-08-24 (2.3GB, 77s over LAN); nightly incremental.

### What each tier answers
- *bad code change* → Tier 0 (git revert)
- *corrupted / mis-migrated DB* → Tier 1 ADAM copies (or dump replay)
- *this machine dies* → Tier 1 (ADAM has everything) + Tier 0
- *machine AND ADAM die* (fire/theft) → Drive (dumps + mirror + media/training)
  + password manager keys + GitHub code. Since 2026-08-24 this loses only the
  page cache and up to one day of everything else.

---

## 3. Recovery playbooks

### A. Roll back a bad DB state (machine fine)
```
Stop-Service BCC        # (admin; or bcc_restart.bat knows the dance)
copy \\Adam\tbotb\Backups\recipes-db\recipes_<ts>.db  C:\Users\john\PycharmProjects\forms\recipes.db
Start-Service BCC
```
Timestamped copies exist for every nightly (03:00) plus any hand-run. The `.db`
copy is byte-identical and instant. Verify: PID start time, then
`scripts/check_embeddings.py`.

### B. Restore from the dump instead (no .db copy trusted / cross-checking)
```
gunzip -c recipes_<ts>.sql.gz | sqlite3 recipes.db
```
The dump has no vec0/fts5 tables — **on first server start** `ensure_vec_tables`
rebuilds the vector indexes from the embedding BLOBs (which ARE in the dump) and
`master_recipes_fts` rebuilds via its own triggers. Then run
`python backup_db.py --verify` against the restored DB as a sanity loop.

### C. This machine dies (ADAM alive) — full rebuild
1. New Windows box; install Python 3.13, git.
2. `git clone http://github.com/leaddog99/forms` into `C:\Users\<user>\PycharmProjects\forms`
   — or copy `\\Adam\tbotb\Backups\forms-mirror` wholesale (it includes `.git`
   AND `generated/` + `input/`, which the clone does not).
3. Databases from ADAM: newest `recipes_<ts>.db` → `recipes.db`; newest
   `training_<ts>.db` → `training.db`; `media_latest.db` → `media.db`.
   (`page_cache.db` — don't: it refills itself.)
4. `env.backup` → `.env`.
5. `pip install -r requirements-frozen.txt` (into a venv; the service bat
   activates `C:\Users\john\PyCharm\venv`, adjust paths or recreate there).
6. Recreate the moving parts from §5.
7. Prove it: server answers on :8009, `/dish-coverage` loads, save one recipe,
   `python backup_db.py --verify`, `python scripts/check_embeddings.py`.

### D. Machine AND ADAM die — the fire scenario
What survives: GitHub (code), Google Drive (14 days of verified dumps), the
password manager (keys — if maintained, see G3).
1. Steps C.1–C.2 via GitHub clone.
2. `rclone` re-auth (2 minutes, any browser) → pull the newest dump →
   playbook B replay.
3. Recreate `.env` from the password manager.
4. Pull the rest of Drive: `forms-mirror/` (restores `generated/` incl. hero
   uploads, `input/`, the working tree), `db-latest/` (`media_latest.db` →
   `media.db`, newest `training_*.db` → `training.db`).
5. Losses: `page_cache.db` (refills itself) and up to one day of deltas.
   The RECIPES — the thing the product is — come back whole.

---

## 4. Verification — why we believe any of this
- **Nightly `RESTORE OK`** in backup.log (dump replayed, all row counts match).
- `PRAGMA integrity_check` on every timestamped copy at copy time.
- `scripts/check_embeddings.py` — the on-demand data-rot sweep (vectors ↔ rows).
- The mirror and Drive sync log their outcomes to backup.log with exit codes.
- History says verification is the whole game: both dump failures were silent
  for days precisely because nothing replayed them.

---

## 5. The moving parts a rebuild must recreate

| part | recreation |
|---|---|
| NSSM service **BCC** | nssm binary at `C:\tools\nssm\...\win64\nssm.exe`; `nssm install BCC` pointing at the venv python + uvicorn on :8009 (inspect current config anytime: `nssm dump BCC`); StartMode=Auto. `bcc_restart.bat` / `bcc_start.bat` in the repo encode the runtime invocation. |
| Task **"BCC Recipes DB Backup"** | `schtasks /Create /TN "BCC Recipes DB Backup" /TR "C:\...\forms\bcc_backup_scheduled.bat" /SC DAILY /ST 03:00` |
| rclone remote **gdrive** | `rclone config create gdrive drive scope drive.file` + browser OAuth (2 min). Binary: winget `Rclone.Rclone`, copy at `C:\Users\john\bin\rclone.exe`. |
| Cloudflare tunnel | `cloudflared` service mapping recipes.tbotb.com → localhost:8009 ([[project_cloudflare_tunnel]]); config lives with cloudflared, NOT in this repo. |
| ADAM share | `\\Adam\tbotb` reachable (Z: mapping is per-session; the bat uses UNC on purpose). |
| Windows watchdog bits | `kernel_power_check.bat`, `bcc_schedule.bat` — see docs/host-stability-and-watchdog.md. |

---

## 6. Known gaps and standing decisions

- **G1 — ADAM has no retention.** `Backups\recipes-db` was 58.8GB at audit time,
  growing ~1.1GB/night (a 465MB db + 580MB training + 70MB dump per run, forever).
  ADAM will fill. Needs a pruning policy (e.g. keep dailies 30d, then weeklies) —
  **a decision, not yet built.**
- **G2 — CLOSED 2026-08-24**, the day it was found: media.db, generated/ and the
  freshest training.db now ride to Drive nightly (`forms-mirror/` +
  `db-latest/`). Residual: the cloud holds ONE rolling copy of each — dated
  history offsite exists only for the 14 dump days.
- **G3 — `.env` off-site = the password manager**, by declared policy. That copy
  is maintained by a human, not by any job. Keep it true.
- **G4 — rclone's shared Google client_id retires "during 2026".** The nightly
  sync will start failing with a clear NOTICE when it happens; fix = personal
  OAuth client_id (5 min in Google Cloud Console) + `rclone config update`.
- **G5 — restore drills.** Playbook A/B have been exercised (dump replay runs
  nightly); playbook C/D never end-to-end. The honest version of this document
  is one practiced bare-metal restore away.
