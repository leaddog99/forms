# BCC → BAILEY migration plan — DRAFT 2026-11-10

*Companion to `docs/new-host-cutover-playbook.md` (10-17, "move first, convert
later") and `docs/postgres-etl.md` (11-09, the ETL). That playbook was written
for a fresh box named MARLEY2; this plan is for BAILEY, which is not fresh: it
already runs BCC as the warm standby (NSSM service `BCC`, port 8009, synced
nightly from MARLEY_SVR), and it already hosts f2n, mbg and dpi on PostgreSQL 18.
Everything below is a proposal. Nothing changes on either box until the
curator approves; the BAILEY half is reviewed by DPI's session (dpi-06). Facts
were read off MARLEY_SVR and, read-only over SSH, off BAILEY on 2026-11-10.*

Every command block is marked with its machine: **▶ ON BAILEY** or
**▶ ON MARLEY_SVR**. Commands are what we would run, not what has been run.

---

## 0. The decision in one paragraph

Move first, convert later — the same order the 10-17 playbook chose, for the
same reason: a host move with identical software is a copy you can verify
byte-for-byte against the frozen source, while a Postgres conversion and a
3,072-dimension re-embed each change behaviour on purpose. Doing the three at
once leaves no way to tell which one broke a search result. BAILEY is already
most of the way through the "move" half (code, venv, service, data copy,
tunnel connector all exist), so the move is a short cut-over (§4) followed by a
one-week soak with BCC still on SQLite (§5). Only then does the Postgres step
run (§6), on BAILEY, from BAILEY's own SQLite copy, with MARLEY_SVR's frozen
file as the comparison engine — and the re-embed is the last stage of that
step, not the first (§7), because the SQLite side cannot hold a 3,072-vector
in its vec0 tables and the thresholds that depend on the embedding space have
to be re-measured against a working Postgres KNN.

---

## 1. Where the two boxes stand today

| | MARLEY_SVR (primary) | BAILEY (standby → target) |
|---|---|---|
| address | 192.168.1.196, Ethernet | 192.168.1.183, Ethernet (cable was out on 11-09/10; Wi-Fi .180 now off) |
| Windows | 11 Home 26300.9818 | 11, 26300.9818 |
| Python | 3.13.1, venv `C:\Users\john\PyCharm\venv` | 3.13.17, same venv path |
| forms repo | `C:\Users\john\PycharmProjects\forms`, `split/enrichment-api` @ 4d90f82 | same path, same branch @ ba3fc32 (11-09; code sync has been failing on three git objects since 10-30, cleared today) |
| siblings | recipe-core (no git), imagekit, llmkit (editable from GitHub) | all three present (BAILEY owns the kits and pushes them) |
| BCC service | NSSM `BCC`, LocalSystem, `-m uvicorn save_recipe_api:app --host 127.0.0.1 --port 8009`, RUNNING | NSSM `BCC`, RUNNING, Automatic (standby instance, port 8009 — **8009 is BCC's own, not free and not a conflict**; f2n 8765, mbg 8766, dpi 8767, all NSSM services needing no logged-in session) |
| tunnel | `Cloudflared` service, tunnel `recipes` → recipes.tbotb.com | `Cloudflared` service RUNNING, tunnel `bestcooksclub` (bailey.tbotb.com; currently 530, connector not registering) |
| data | recipes.db 1.00 GiB · training.db 1.16 GiB · media.db 0.64 GiB · page_cache.db 0.44 GiB | recipes.db (11-10 copy failed; 11-09 set present) · training.db 11-08 · media.db 11-08 · no page_cache |
| scheduled tasks | BCC Recipes DB Backup 02:00 (wake, 2 h) · BCC Backup Check 06:00 (was 07:00 under DST) · BCC Dish Schedule hourly from 00:00 (3 h) · BCC Lock After Autologon | **none of the four** (my first SSH read mis-attributed a row); only `BCC-Drill` (Disabled, the 08-24 drill's cmd-spawned uvicorn — delete it, NSSM owns serving). DPI's tasks: DPI DB Backup 02:30 (~25 min, mostly the Drive upload; catches up after a missed start), DPI Backup Check 07:00. No autologon (`AutoAdminLogon` not set); BAILEY rebooted 18:13 today and came up serving with nobody logged on |
| Playwright | chromium installed | chromium-1243 + headless shell installed |
| PostgreSQL | 17 (unused by the app; scratch cluster only) | **18**, service `postgresql-x64-18`, :5432, pgvector 0.8.7, databases f2n/f2n_test, mbg/mbg_test, dpi/dpi_test |
| psycopg in venv | 3.3.8 (added 11-09, not in requirements-frozen) | **missing** |
| OpenSSH server | **none** (no sshd, nothing on :22) | sshd RUNNING, inbound rule Private |
| disk | — | 136 GB free of 456 |
| .env | 30 keys | present (copied by the nightly sync; last good 11-09) |
| rclone | `C:\Users\john\bin\rclone.exe`; remotes `gdrive:` (own OAuth client, drive.file scope) + `bailey:` | `bin\rclone.exe` present; `rclone.conf` holds `[gdrive]` = **DPI's remote, still on rclone's shared client_id** (retires during 2026) and `[bailey]` (points at itself). BCC's own-client token must not overwrite `[gdrive]` — see §9.o |
| ADAM (`\\Adam\tbotb`) | mapped Z:, cmdkey present, nightly writes | `dir \\Adam\tbotb\Backups` works as john **while logged in**; `cmdkey` shows NO entry for Adam, so a task running with john logged off may fail — see §9.q |

Two things from this table shape the plan. First, BAILEY is already the
rehearsed standby: the 2026-08-24 bare-metal drill built it and the nightly has
kept it fresh; the cut-over is "promote the standby", not "build a box".
Second, BAILEY is a shared host now. The cut-over must not touch f2n, mbg or
dpi, their databases, their ports (dpi on 8767) or their backup window.

---

## 2. Postgres landing zone on BAILEY (§6 uses it; §4 does not)

**Databases and role.** BCC gets its own role `bcc` and two databases, `bcc`
and `bcc_test`, both owned by `bcc`, nothing shared. The reason is mechanical:
`python -m pgmigrate run --drop` executes `DROP SCHEMA public CASCADE` on its
target, so any database it points at is wiped to the schema. `bcc_test` is
where every verify-by-restore lands (the same convention as `dpi_test`), so
the production `bcc` is never the target of a replay.

**Extensions.** pgmigrate needs `vector`, `unaccent` and `pg_trgm`; it issues
`CREATE EXTENSION IF NOT EXISTS` for each at run time and silently falls back
to `real[]` (no index) if `vector` is not creatable. Creating them takes the
superuser once per database. Follow DPI's pattern: a bootstrap script that
takes `POSTGRES_UID`/`POSTGRES_PWD` from the environment or a prompt, never
stores them, and is idempotent. **BCC wants its own bootstrap**
(`scripts/bootstrap_pg.py` in forms), not a shared one: the two apps have
different extension sets and different owners, and a shared script would make
one repo's deploy depend on another repo's file. Keep DPI's convention of
installing extensions into schema `extensions` and set the role's search path
so unqualified `vector` / `unaccent()` in pgmigrate's DDL resolve:

**▶ ON BAILEY** (what the bootstrap does; superuser, once)
```
CREATE ROLE bcc LOGIN PASSWORD '<from the password manager>';
CREATE DATABASE bcc      OWNER bcc ENCODING 'UTF8';
CREATE DATABASE bcc_test OWNER bcc ENCODING 'UTF8';
-- in each of bcc and bcc_test:
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION IF NOT EXISTS vector   SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS unaccent SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS pg_trgm  SCHEMA extensions;
GRANT USAGE ON SCHEMA extensions TO bcc;
ALTER ROLE bcc SET search_path = public, extensions;
```
After this, pgmigrate's own `CREATE EXTENSION IF NOT EXISTS` lines are
no-ops and it never needs the superuser. The DSN goes in BAILEY's `.env` as
`BCC_PG_DSN=postgresql://bcc:…@127.0.0.1:5432/bcc` (and `BCC_PG_TEST_DSN` for
`bcc_test`). pgmigrate must be checked for one assumption: it creates
extensions without a `SCHEMA` clause, which would try `public` and fail for a
non-superuser if the bootstrap had not already installed them — fine with the
bootstrap in place, but `load.py` should tolerate the "already exists in
another schema" case explicitly rather than by accident.

**Nothing in §2 happens before the soak is over.** It is listed here because
the bootstrap can be written and reviewed now, offline.

---

## 3. What moves in the "move" half (and what is already there)

Compared with the 10-17 inventory, only the deltas matter on BAILEY:

- **Code**: `git pull` in forms; copy recipe-core once more (no git); kits are
  BAILEY's own and are **not** mirrored (DR doc, 10-28 rule).
- **Data**: the four `.db` files from a frozen MARLEY_SVR (§4.2–4.3);
  `generated/` (2.7 GB, user-uploaded heroes, irreplaceable) and `input/` by
  robocopy or the existing SFTP path. page_cache.db is optional but worth
  carrying (paid fetches, 5-day TTL).
- **Secrets**, by hand, never through the repo: `.env` (the 30 keys — the
  sync already copies it, but verify the copy is current at the freeze);
  `OPENAI_API_KEY` as a User env var; `%APPDATA%\rclone\rclone.conf` (the
  `gdrive` remote's own-client OAuth token — the Drive client JSON goes into
  the password manager first, still owed from 10-29); `C:\Users\john\.ssh\`
  (the `bailey:` remote is useless on BAILEY itself; the key is still wanted
  for whatever stands by next); `cmdkey /add:adam` for `\\Adam\tbotb`; the
  **recipes tunnel token** from Zero Trust → Networks → Tunnels → `recipes` →
  Configure.
- **Runtime**: `pip install "psycopg[binary]"` into BAILEY's venv (3.3.8 on
  MARLEY; add it to requirements-frozen.txt in the same commit); nssm is
  already there (the service exists); Playwright browsers are already there;
  `pre-commit install` if BAILEY will be where commits happen.
- **Tasks**: the four BCC tasks (§4.6), created fresh; delete the drill's
  `BCC-Drill` task (its cmd-spawned uvicorn would fight the service for 8009
  if ever re-enabled).
- **rclone on BAILEY**: BAILEY's `[gdrive]` is DPI's remote on the shared
  client_id. BCC's bat names the remote `gdrive` by string, so either DPI moves
  onto BCC's own OAuth client (same Drive account, one `[gdrive]`, and the
  shared-client retirement stops being DPI's problem too) or BCC's remote is
  added as `gdrive-bcc` and the bat's remote name becomes a variable. Curator
  decision (§9.o); do not overwrite `[gdrive]` either way.
- **Autologon**: needed only because the three john tasks are Interactive and
  the capture walker / Playwright want a desktop. Decision for the curator
  (§9.g): keep the Home-edition autologon + lock-after-logon dance, or
  re-create the tasks as `/RU john /RL HIGHEST` with "run whether user is
  logged on or not" and accept that headed captures need a human to log in.

---

## 4. Cut-over day — promote the standby (one to two hours)

Pick a quiet hour, no batch dishes running (`/jobs` on MARLEY_SVR). DPI's and
the other apps' services on BAILEY are not touched at any step. Each step is
reversible until step 5.

1. **Freeze MARLEY_SVR.**
   **▶ ON MARLEY_SVR**
   ```
   schtasks /Change /TN "BCC Dish Schedule"     /DISABLE
   schtasks /Change /TN "BCC Recipes DB Backup" /DISABLE
   schtasks /Change /TN "BCC Backup Check"      /DISABLE
   powershell -c "Stop-Service BCC; Set-Service BCC -StartupType Disabled"
   ```
   Nothing writes to recipes.db now.
2. **Final consistent set, with a dated rollback copy on ADAM.**
   **▶ ON MARLEY_SVR**
   ```
   python -c "import sqlite3; sqlite3.connect('recipes.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
   python backup_db.py --verify --dest \\Adam\tbotb\Backups\recipes-db
   ```
   Wait for `RESTORE OK`.
3. **Final delta to BAILEY** — the existing script, pointed at BAILEY, with
   the databases and a fresh backup:
   **▶ ON MARLEY_SVR**
   ```
   .\bcc_sync_standby.ps1 -Standby BAILEY -WithDbs -FreshBackup
   ```
   It stops BAILEY's `BCC`, lays in the newest ADAM set with size-verified
   copies, syncs code + assets (now that the three locked git objects are
   cleared it reaches its delete pass), copies recipe-core, restarts `BCC`.
   Then robocopy `generated\` and `input\` once more and copy
   `page_cache.db` (server stopped both sides).
4. **Compare.** Row counts of recipes, master_recipes, dishes, domains, jobs,
   users must be equal between MARLEY_SVR's frozen file and BAILEY's; then the
   playbook §2.8 proof on BAILEY (`/auth/me`, form loads, search returns rows,
   `/dish-coverage`, save-and-delete one recipe, `backup_db.py --verify`,
   `scripts\check_embeddings.py` clean, `test_fts_query.py` passes). Expected
   on 2026-11-10: master_recipes 14,239 · recipes 487 · dishes 525 · domains
   529 · jobs 2,976.
5. **Tunnel flip — the point of no easy return.**
   **▶ ON MARLEY_SVR**
   ```
   powershell -c "Stop-Service Cloudflared; Set-Service Cloudflared -StartupType Disabled"
   ```
   **▶ ON BAILEY** (elevated)
   ```
   cloudflared service uninstall
   cloudflared service install <recipes tunnel token>
   ```
   Same tunnel `recipes`, same hostname, no DNS change: within a minute
   `https://recipes.tbotb.com/auth/me` is served by BAILEY (confirm by the
   worker PID in BAILEY's `uvicorn_stdout.log`, and by content, never by a
   200 alone — the 09-12 lesson). The `bestcooksclub` tunnel that carried
   bailey.tbotb.com is then free: keep it as the staging back door pointed at
   whatever stands by next, or delete it. `bcc_link_domain` in system_config
   stays `recipes.tbotb.com`; links already minted keep working.
6. **Give BAILEY the jobs.** Recreate the four tasks (run as john, times as
   MARLEY runs them today; see §8 for the collision check with DPI):
   **▶ ON BAILEY**
   ```
   schtasks /Create /TN "BCC Recipes DB Backup"    /TR "C:\Users\john\PycharmProjects\forms\bcc_backup_scheduled.bat" /SC DAILY  /ST 02:00 /RU john
   schtasks /Create /TN "BCC Backup Check"         /TR "C:\Users\john\PycharmProjects\forms\bcc_backup_check.bat"     /SC DAILY  /ST 06:00 /RU john
   schtasks /Create /TN "BCC Dish Schedule"        /TR "C:\Users\john\PycharmProjects\forms\bcc_schedule.bat"         /SC HOURLY /ST 00:00 /RU john
   schtasks /Create /TN "BCC Lock After Autologon" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\john\PycharmProjects\forms\bcc_lock_if_idle.ps1" /SC ONLOGON /RU john /DELAY 0001:00
   schtasks /Delete /TN BCC-Drill /F
   ```
   (The lock task only if the curator chooses autologon, §9.f; otherwise the
   first three are created with `/RP` so they run whether john is logged on
   or not.) Then in Task Scheduler: Backup → wake the computer, 2 h limit;
   Check → 20 min; Dish Schedule → 3 h limit. **Run the backup task once with
   john logged OFF** — that is the test of the ADAM credential (§9.q) and of
   the non-interactive choice. **Before enabling the backup task, edit
   `bcc_backup_scheduled.bat` on BAILEY so the standby-sync line no longer
   targets `bailey:`** (a host syncing to itself); point it at the next
   standby or comment it out until there is one. Run the backup task by hand
   once and read `backup.log`: `RESTORE OK`, ADAM copies, `cloud mirror exit
   code: 0`.
7. **Prove the morning after**: the 06:00 check sends no mail; `/jobs` on
   BAILEY shows the hourly schedule running; a dish refresh by name
   (`python -m jobs run dish_refresh <name>`) completes.

---

## 5. After the flip: the soak on SQLite (one week) and what MARLEY_SVR becomes

MARLEY_SVR is the frozen rollback for seven days: `BCC`, `Cloudflared` and
the tasks disabled, box on. Rollback = stop `Cloudflared` on BAILEY, start it
and `BCC` on MARLEY_SVR, re-enable its tasks; any recipe saved on BAILEY in
between is copied back by hand, so the honest rollback window is the first day
or two. MARLEY_SVR is not to run the long ETL or any unattended batch — it
rebooted six times on 11-09 for reasons not yet found.

After the week there are two options for MARLEY_SVR, and the curator chooses
(§9.e): **(1) the reverse standby** — install OpenSSH Server on it (it has none
today), add BAILEY's key to `administrators_authorized_keys`, create an rclone
remote `marley:` on BAILEY, and let BAILEY's nightly run
`bcc_sync_standby.ps1 -Standby MARLEY` so MARLEY_SVR becomes what BAILEY was;
the SQLite file copy keeps working as a standby mechanism right up to §6, after
which the standby needs the Postgres design in §8; or **(2) decommission** —
the 10-29 decommission list (tunnel token, tasks, ssh key, rclone.conf, .env)
and power down, leaving ADAM + Drive + GitHub as the recovery tiers, which the
DR doc's playbooks C and D already cover.

The soak's purpose is narrow: prove BAILEY under production load (the hourly
dish schedule, the 02:00 backup with its ADAM and Drive legs, the bookmarklet,
the iPad, the cook voice loop against recipes.tbotb.com) with no behavioural
change to attribute to the engine. Watch the drill's four lessons plus the
fifth: FTS and vec tables rebuild on first start only from the dump path; a
`.db` copy carries them.

---

## 6. Convert: recipes.db → PostgreSQL 18 on BAILEY (after the soak)

The ETL is built and verified at 1,536 dimensions (749,486 rows, 61 tables,
0 mismatches, 52 s on a scratch PG 17). What remains is the sequence, on
BAILEY, with BCC still serving from SQLite throughout until the last step:

1. **Bootstrap** (§2) → `bcc`, `bcc_test`, role, extensions. Curator runs it
   with the superuser credentials; nothing stored.
2. **Dependencies.** **▶ ON BAILEY** `pip install "psycopg[binary]"` (and
   `pgvector` the Python helper is not required — pgmigrate speaks the text
   form).
3. **Full ETL into `bcc_test` first**, from a frozen copy of BAILEY's own
   recipes.db (take the 02:00 set from ADAM, never the live file):
   **▶ ON BAILEY**
   ```
   python -m pgmigrate plan --sqlite \\Adam\tbotb\Backups\recipes-db\recipes_<ts>.db --ddl pg-ddl.sql
   python -m pgmigrate run  --sqlite ...\recipes_<ts>.db --dsn %BCC_PG_TEST_DSN% --drop
   python -m pgmigrate verify --sqlite ...\recipes_<ts>.db --dsn %BCC_PG_TEST_DSN%
   ```
   Expect "vector extension: available" (the first time it has ever said so),
   the HNSW indexes built, and the KNN top-5 overlap 5/5 on the sampled
   dishes — that is the pgvector proof the 11-09 run could not give.
4. **The app port branch** (inventory REWRITE 1–7, `DB_BACKEND` switch behind
   `input/pipeline/db.connect`): `?`→`%s`, `lastrowid`→`RETURNING`,
   `json_extract`→`#>>`, `INSERT OR REPLACE`→`ON CONFLICT`, jsonb rows arrive
   as dicts, fts5 MATCH → tsquery (`test_fts_query.py` re-pinned), bm25 tiers
   → weight classes, `temp.q_match` → CTE, PRAGMA census. Developed and tested
   against `bcc_test` on BAILEY while production keeps serving SQLite. This is
   the long pole of the whole plan; nothing in §4–§5 waits on it.
5. **Backup chain redesign** (§8) must be in place before step 6.
6. **Engine cut-over on BAILEY** (an evening): stop `BCC`, checkpoint, run the
   ETL into `bcc` with `--drop`, `verify`, flip `DB_BACKEND=postgres` in
   `.env`, start `BCC`, run the §4.4 proof plus the FTS hit-set comparison
   from the ETL doc (lemon chicken, shrimp, béchamel, kenji, banana bread,
   nytimes). recipes.db stays on disk, untouched, as the rollback: flipping
   `DB_BACKEND` back is the whole rollback for the first days, with the same
   "hand-copy anything saved since" caveat.

---

## 7. Embeddings: text-embedding-3-small → text-embedding-3-large (3,072 d)

**When.** As the last stage of §6, after the app serves from Postgres at
1,536 d and the KNN check has passed. Not during the move (§4) and not during
the SQLite soak (§5): the vec0 tables are declared `float[1536]` and the whole
SQLite path (`vector_store.py`, `check_embeddings.py`, the three vec0 DDLs)
would have to be rewritten for a dimension it is about to stop using. Doing
the re-embed once, in Postgres, against a working `<=>` KNN, is the only order
that lets each change be checked on its own.

**Storage and index.** `embedding vector(3072)` on the base table (pgvector
stores up to 16,000 d) and, because pgvector's HNSW/IVFFlat index types stop
at 2,000 d for `vector`, the index goes on a half-precision expression:

```
CREATE INDEX master_recipes_embedding_hnsw
  ON master_recipes USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);
-- queries must use the same expression to hit it:
ORDER BY (embedding::halfvec(3072)) <=> ($1::vector(3072))::halfvec(3072)
```
pgmigrate changes: `EMBED_DIM` becomes a measurement (blob length ÷ 4: 6,144 B
→ 1,536, 12,288 B → 3,072) instead of a constant; `hnsw_sql()` emits the
halfvec expression form when the dimension exceeds 2,000; `verify.py`'s KNN
uses the same expression. Row cost: 12,288 B per vector, so recipes.db's
15,713 vectors are ~190 MB of vectors plus the halfvec index (~100 MB); the
SQLite-side BLOBs would double to 12 KB per row if they were ever kept, which
is one more reason not to re-embed before Postgres.

**Which surfaces, in one pass, in this order** (all through the one
chokepoint `input/pipeline/embeddings.embed_text`, the only
`embeddings.create` call in runtime code; `EMBED_MODEL`/`EMBED_DIM` change
there, in `vector_store.py`, in `check_embeddings.py` and in pgmigrate):

| surface | rows | embed text | tokens/row (≈) |
|---|---|---|---|
| dishes | 525 | `compose_identity_text(identity_card)` | 60 |
| master_recipes | 14,239 (all have `_identity`) | same composer, recipe side | 62 |
| recipes | 487 (484 embedded today) | same | 62 |
| products | 396 | catalog composer | ~40 |
| product_classes | 347 | `embedding_text` column | ~35 |
| ws_categories | 217 | headline + description + products sample | ~150 |

`scripts/reembed_identity.py --force` is already the one-pass tool for the
first three (resumable by text hash; stamps model + hash + timestamp per row);
`--products` covers products; the class and ws tables need the same `--force`
path added. The composer is unchanged: identity-card `likelyDish` first, then
cuisine/ethnicity, primaries ranked by `INGREDIENT_ROLES` position and
normalised through `ingredients_lib`, then technique; **title stays OUT**
(2026-06-11 measurement: including it doubled intra-dish spread). Only the
model id and the dimension move.

**Cost and time.** ≈ 1.0 M tokens for all of recipes.db at $0.13 / M tokens
for 3-large ≈ **$0.13**. Wall time is call-bound, not token-bound:
`embed_text` sends one text per request, which the re-embed script measured at
10–25 min per 4.8k rows, so 15.7k rows is 35–80 min as-is; batching 100 texts
per request (the API takes up to 2,048) brings it under five minutes and is a
ten-line change worth making first.

**training.db — explicit decision (§9.c): do NOT re-embed in this move.**
`is_recipe_samples` holds 101,510 rows; 6,932 carry a 3-small vector (stamped
`embedding_model`), 94,578 have none, and **no code reads them yet** — the
Tier-B "embedding + logistic head" classifier in `docs/corpus-ml-strategy.md`
is unbuilt. Re-embedding the whole corpus would be ~275 M tokens (avg 10.6k
chars of page content per row) ≈ **$36** and hours; the 6,932 alone ≈ $2.50.
The right move is to leave the column as it is (the model stamp makes the old
vectors identifiable), carry training.db as a file, and embed the corpus with
3-large when Tier B is actually built — at which point it may want a different
text (page content, not identity cards) anyway.

**The thresholds are the real risk.** `DEFAULT_MATCH_THRESHOLD = 0.55` in
embeddings.py was tuned on 3-small's cosine distribution ("same-dish pairs
0.55–0.85, unrelated below 0.3"); 3-large's distribution is tighter and
higher, so the same number means something else. Every cosine floor must be
re-measured on the new vectors before the re-embedded tables go live: the dish
cohort match floor, the product-class match calibration
(`scripts/calibrate_class_match.py` exists for exactly this), and the
recommender's cut-offs. The exact-alias override (name-exact beats distance
both ways) is unaffected. Plan: re-embed into `bcc_test`, re-run the
calibration scripts and the KNN overlap against the 1,536-d answers on a
sample of 50 dishes, pick the new floors, then re-embed `bcc`.

---

## 8. Backups on BAILEY — the file chain now, the Postgres chain for §6

**During §4–§5 nothing changes in the design**, only the host: the same
`bcc_backup_scheduled.bat` runs at 02:00 on BAILEY (`backup_db.py --verify`
→ ADAM dated sets + dump replayed and row-counted; prune; robocopy mirror;
rclone → Drive `recipes-db/` + `db-latest/` + `forms-mirror/`), and
`bcc_backup_check.bat` at 06:00 judges the outcomes (A–E) and mails the
curator. BCC's times, for the collision check with DPI's nightly:

| BCC task | start | typical duration | touches |
|---|---|---|---|
| BCC Recipes DB Backup | 02:00 | 15–25 min (backup+verify ~5, ADAM copies, prune, mirror, rclone ~10) | CPU for the dump replay, `\\Adam\tbotb`, Drive upload bandwidth, (standby sync ~10 min if one exists) |
| BCC Backup Check | 06:00 | < 1 min | reads ADAM, Drive, the standby URL |
| BCC Dish Schedule | hourly at :00 | 0 – 3 h, only when dishes are due | SERP + fetch spend, LLM calls, CPU |

DPI DB Backup runs at 02:30 for ~25 min (dump + verify into dpi_test a few
minutes, the rest the Drive upload) and catches up after a missed start; DPI
Backup Check at 07:00. BCC at 02:00 finishes before or near 02:30, and the two
touch different databases and different ADAM folders, so there is no
collision — **01:30 for BCC** buys a clean margin at no cost and is what §4.6
should use if the curator agrees. The hourly dish schedule is the one to keep
an eye on because a long refresh can overlap anything.

**For §6 the chain is redesigned once, before the engine cut-over:** nightly
`pg_dump -Fc bcc` → ADAM dated set, verified by `pg_restore --clean` into
`bcc_test` and row-counted (the same shape as DPI's verify-into-dpi_test);
training.db / media.db / page_cache.db keep the file path; `backup_check.py`
gets a Postgres branch for checks A–C; Drive keeps receiving the dumps. The
SQLite standby mechanism (file copy over SFTP) ends with §6; a Postgres standby
is either a nightly `pg_restore` on the standby host or streaming replication
— that is a §9 decision, not needed for the move.

---

## 9. Open items and risks — owner and the decision the curator must make

| # | item | owner | decision for the curator |
|---|---|---|---|
| a | **SSH both ways.** BAILEY→MARLEY_SVR: MARLEY_SVR has no OpenSSH Server. MARLEY_SVR→BAILEY: works again since the cable was reseated (11-10 18:36); the three locked git objects are cleared; tonight's 02:00 run is the proof | BAILEY side: dpi-06 (done). MARLEY_SVR side: curator (Settings → Optional features → OpenSSH Server, then BAILEY's key into `administrators_authorized_keys`) | install OpenSSH Server on MARLEY_SVR now (needed for option 5.1) or only if it becomes the reverse standby |
| b | **3-large is new to this side.** pgmigrate builds vector(1536); no doc or code mentions 3,072. §7 is the first write-up | BCC session | approve the §7 order (re-embed last, in Postgres) and the batching change |
| c | **training.db** | BCC session | accept "carry as a file, do not re-embed now" (§7) |
| d | **Thresholds** tuned on 3-small (0.55 dish floor, class calibration, recommender) | BCC session | approve re-calibration in `bcc_test` before the re-embedded tables serve |
| e | **What MARLEY_SVR becomes** after the week | curator | reverse standby (needs OpenSSH + a `marley:` remote) or decommission (10-29 list, power down) |
| f | **Autologon on BAILEY** — Interactive tasks + headed Playwright want a desktop; BAILEY also serves three other apps, all as services needing no session (it rebooted 18:13 today and came up serving with nobody logged on). dpi-06's view: non-interactive tasks, human-attended headed captures | curator | non-interactive tasks (recommended, matches the host's other apps) vs. autologon + lock-after-logon |
| g | **Port 8009 on BAILEY is BCC's own** standby service — CONFIRMED by dpi-06; f2n 8765, mbg 8766, dpi 8767 | — | none |
| h | **BCC's own PostgreSQL database + role + bootstrap script** (§2) | BCC session writes `scripts/bootstrap_pg.py`; curator runs it | own bootstrap (recommended) vs. a shared one |
| i | **MARLEY_SVR's six unexplained reboots** (11-09) — the ETL and any long batch run on BAILEY, never on MARLEY_SVR | curator (household, Logitech receiver, HP leftovers) | none for the move; it is a reason to move sooner |
| j | **`bcc_backup_scheduled.bat` on BAILEY must not sync to `bailey:`** (itself) | BCC session edits the bat before step 4.6 | which host is the next standby, if any |
| k | **The `bestcooksclub` tunnel** and bailey.tbotb.com after the flip | curator | keep as the staging door for the next standby, or delete; bestcooks.club itself stays a separate, parked decision |
| l | **Backup window overlap with "DPI DB Backup"** (§8) | dpi-06 reports DPI's window | move BCC's 02:00 if they collide |
| m | **Secrets off-repo**: Drive client JSON → password manager (owed since 10-29); `.env` copy verified at the freeze | curator | — |
| n | **DR doc §7 and the memory rule** "restart = service + host + script" name MARLEY; after the flip "restart BCC" means BAILEY | BCC session, after the flip | — |
| o | **Two `gdrive` remotes, one name.** BAILEY's `[gdrive]` is DPI's, on the retiring shared client_id; BCC's bat names `gdrive` by string | curator | DPI adopts BCC's own OAuth client under the one `[gdrive]` (recommended: one Drive account, one client, retirement solved for both), or BCC gets `gdrive-bcc` and the bat's remote name becomes a variable |
| p | **A service restarts on whatever is in its working tree.** f2n broke on today's 18:13 reboot because uncommitted work (migration 0049) loaded against its 0048 database | BCC session: before every restart on BAILEY the forms tree is on a committed revision; the state-log rule "commit before restart" | — |
| q | **ADAM credential on BAILEY** rides on the session login (no cmdkey entry); a task with john logged off may fail silently | curator: `cmdkey /add:Adam /user:… /pass:…` as john, then the logged-off test run in §4.6 | — |
| r | **BCC's backup at 01:30 instead of 02:00** for margin ahead of DPI's 02:30 | curator | 01:30 (recommended) or keep 02:00 |

---

## 10. Order of work, as a list

1. Tonight: confirm the 02:00 sync to BAILEY is green (a).
2. Offline, no box changes: write `scripts/bootstrap_pg.py` (h); batch
   `embed_text` (§7); edit the bat's standby line for BAILEY (j); add psycopg
   to requirements-frozen.txt.
3. Curator decides e, f, o, q, r (g and l are settled: 8009 is BCC's, DPI's
   window is 02:30; BAILEY-side review by dpi-06 received 11-10 19:15, order
   and landing zone agreed).
4. Cut-over day (§4) → one-week soak (§5).
5. Bootstrap on BAILEY (§2) → ETL into `bcc_test` with pgvector (§6.3) → app
   port branch (§6.4) → backup chain (§8) → engine cut-over (§6.6).
6. Re-embed to 3-large in `bcc_test`, re-calibrate (d), then `bcc` (§7).
7. Paper: DR doc §7, playbook, memory rule, state log (n).
