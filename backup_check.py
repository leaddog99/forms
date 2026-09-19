"""backup_check.py — did last night's backup and BAILEY sync ACTUALLY work? Email if not.

Runs as its OWN scheduled task ("BCC Backup Check", 07:00), four hours after the 03:00
backup, NOT as the backup's last line: on 2026-09-19 Windows killed the backup task at
its 30-minute limit (result 0x41306) and nothing said so — a check at the end of a
killed script never runs. For the same reason it trusts OUTCOMES, not the exit codes the
backup wrote about itself:

  A. ADAM      newest recipes .db / .sql.gz / training .db / media are < 26h old, not
               shrunken, and the recipes copy OPENS and holds the recipes.
  B. the run   the last block of backup.log is from last night, reached its FINAL stage
               (a killed run stops short), and no stage reported failure. The dump
               verifier's row-count mismatch is a note, not an alarm, when it is tiny:
               jobs writing at 03:00 made it fire on 09-13 and 09-16 (1-19 rows).
  C. the task  Task Scheduler's last result for the backup task is 0.
  D. BAILEY    answers 200 AND serves exactly the recipe count of the snapshot it was
               meant to load. "bailey sync exit code: 0" proves the script ended, not
               that BAILEY has the data.
  E. offsite   Google Drive holds a recipes_*.sql.gz from last night.

Any failure -> one email via input.pipeline.alerts.alert_curator (setting `alert_email`).
    python backup_check.py                # check; email only on failure
    python backup_check.py --always-email # email the report even when all is well
    python backup_check.py --no-email     # print only
Exit code 0 = all good, 1 = something failed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADAM = Path(r"\\Adam\tbotb\Backups\recipes-db")
LOG = HERE / "backup.log"
TASK = "BCC Recipes DB Backup"
BAILEY = "https://bailey.tbotb.com"
RCLONE = r"C:\Users\john\bin\rclone.exe"
CLOUD = "gdrive:BCC-Backups/recipes-db"
MAX_AGE_H = 26
STAGES = [  # (marker in backup.log, acceptable exit codes; None = robocopy 0-7)
    ("exit code:", {0, 1}),                      # 1 is judged separately (verify mismatch)
    ("cloud sync exit code:", {0}),
    ("project mirror exit code:", None),
    ("recipe-core mirror exit code:", None),
    ("bailey sync exit code:", {0}),
    ("cloud mirror exit code:", {0}),
    ("cloud media/training exit code:", {0}),
]


class Report:
    def __init__(self):
        self.lines, self.fails = [], []

    def ok(self, msg):
        self.lines.append(f"  ok    {msg}")

    def note(self, msg):
        self.lines.append(f"  note  {msg}")

    def fail(self, msg):
        self.lines.append(f"  FAIL  {msg}")
        self.fails.append(msg)


def _age_h(p: Path) -> float:
    return (dt.datetime.now().timestamp() - p.stat().st_mtime) / 3600


def check_adam(r: Report) -> int | None:
    """-> the master recipe count inside the newest snapshot (for the BAILEY check)."""
    if not ADAM.exists():
        r.fail(f"ADAM share unreachable: {ADAM}")
        return None
    for pattern, label in (("recipes_*.db", "recipes database"), ("recipes_*.sql.gz", "SQL dump"),
                           ("training_*.db", "training database")):
        files = sorted(ADAM.glob(pattern), key=lambda p: p.stat().st_mtime)
        if not files:
            r.fail(f"ADAM has no {label} at all ({pattern})")
            continue
        new = files[-1]
        age = _age_h(new)
        if age > MAX_AGE_H:
            r.fail(f"newest {label} is {age:.0f}h old: {new.name}")
            continue
        if len(files) > 1 and new.stat().st_size < 0.9 * files[-2].stat().st_size:
            r.fail(f"{label} SHRANK: {new.name} is {new.stat().st_size / 1e6:.0f} MB, "
                   f"previous {files[-2].name} was {files[-2].stat().st_size / 1e6:.0f} MB")
            continue
        r.ok(f"{label}: {new.name}, {new.stat().st_size / 1e6:.0f} MB, {age:.1f}h old")
    media = ADAM / "media_latest.db"
    if not media.exists() or _age_h(media) > MAX_AGE_H:
        r.fail("media_latest.db missing or stale on ADAM")
    else:
        r.ok(f"media_latest.db, {media.stat().st_size / 1e6:.0f} MB, {_age_h(media):.1f}h old")
    dbs = sorted(ADAM.glob("recipes_*.db"), key=lambda p: p.stat().st_mtime)
    if not dbs:
        return None
    try:
        # Read-only URI. A UNC path needs FOUR slashes ("file:////Adam/share/...");
        # with two, SQLite reads "Adam" as a URI authority and refuses.
        posix = dbs[-1].as_posix()
        uri = "file:" + ("//" + posix if posix.startswith("//") else posix) + "?mode=ro"
        c = sqlite3.connect(uri, uri=True, timeout=30)
        n = c.execute("SELECT COUNT(*) FROM master_recipes WHERE user_id = 0").fetchone()[0]
        personal = c.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
        c.close()
        if n < 1000:
            r.fail(f"snapshot {dbs[-1].name} opens but holds only {n} master recipes")
        else:
            r.ok(f"snapshot opens: {n:,} master recipes, {personal:,} personal")
        return n
    except Exception as e:
        r.fail(f"snapshot {dbs[-1].name} will not open: {type(e).__name__}: {e}")
        return None


def check_log(r: Report) -> None:
    if not LOG.exists():
        r.fail("backup.log does not exist")
        return
    text = LOG.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"=+ scheduled backup (.+?) =+", text)
    if len(blocks) < 3:
        r.fail("backup.log has no scheduled-backup block")
        return
    stamp, body = blocks[-2].strip(), blocks[-1]
    m = re.search(r"(\d\d)/(\d\d)/(\d{4})\s+(\d+):(\d\d)", stamp)
    if m:
        mo, d, y, hh, mm = map(int, m.groups())
        started = dt.datetime(y, mo, d, hh, mm)
        age = (dt.datetime.now() - started).total_seconds() / 3600
        if age > MAX_AGE_H:
            r.fail(f"the last backup run STARTED {age:.0f}h ago ({stamp}) — it did not run last night")
            return
        r.ok(f"last run started {stamp}")
    for marker, okcodes in STAGES:
        hit = re.search(r"^" + re.escape(marker) + r"\s*(-?\d+)", body, re.M)
        if marker == "bailey sync exit code:" and "bailey sync skipped" in body:
            r.note("BAILEY sync skipped on this host")
            continue
        if not hit:
            r.fail(f"stage never finished: no '{marker}' line — the run was cut off before or during it")
            continue
        code = int(hit.group(1))
        good = (0 <= code <= 7) if okcodes is None else (code in okcodes)
        if not good:
            r.fail(f"stage failed: '{marker}' {code}")
    first = re.search(r"^exit code:\s*(\d+)", body, re.M)
    if first and int(first.group(1)) == 1:
        mism = re.findall(r"ROW COUNT MISMATCH (\w+): restored (\d+) vs live (\d+)", body)
        worst = max((abs(int(a) - int(b)) / max(int(b), 1) for _, a, b in mism), default=1.0)
        if mism and worst < 0.005 and "integrity_check: ok" in body:
            r.note("dump verifier saw " + ", ".join(f"{t} {a}/{b}" for t, a, b in mism)
                   + " — rows written while the backup ran, not corruption")
        else:
            r.fail("backup step exited 1 and it is NOT a small row-count race: "
                   + (", ".join(f"{t} restored {a} vs live {b}" for t, a, b in mism) or "see backup.log"))
    sync = body.split("== code + assets", 1)[-1].split("bailey sync exit code", 1)[0]
    bad = re.findall(r"(?im)^.*(?:permission denied|rename failed|FAILED|not recognized as).*$", sync)
    if bad:
        r.fail(f"BAILEY sync log has {len(bad)} error line(s), e.g. {bad[0].strip()[:110]}")
    elif "== sync done ==" in sync:
        r.ok("BAILEY sync section clean, reached 'sync done'")


def check_task(r: Report) -> None:
    try:
        out = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST", "/V"],
                             capture_output=True, text=True, timeout=60).stdout
        res = re.search(r"Last Result:\s*(-?\d+)", out)
        when = re.search(r"Last Run Time:\s*(.+)", out)
        code = int(res.group(1)) if res else None
        if code == 0:
            r.ok(f"Task Scheduler: last result 0 ({when.group(1).strip() if when else '?'})")
        elif code == 267014:
            r.fail("Task Scheduler TERMINATED the backup task (0x41306) — it hit its time limit or was stopped")
        elif code == 267009:
            r.note("backup task is running right now")
        else:
            r.fail(f"Task Scheduler last result {code} (0x{(code or 0) & 0xFFFFFFFF:X})")
    except Exception as e:
        r.fail(f"could not query Task Scheduler: {type(e).__name__}: {e}")


def check_bailey(r: Report, snapshot_count: int | None) -> None:
    try:
        url = f"{BAILEY}/recipes/search?user_id=0&sort=updated_desc&limit=1&facets=0"
        with urllib.request.urlopen(url, timeout=45) as resp:
            d = json.load(resp)
        total = int(d.get("total") or 0)
        newest = ((d.get("rows") or [{}])[0].get("updated_at") or "")[:19]
    except Exception as e:
        r.fail(f"BAILEY is not answering ({BAILEY}): {type(e).__name__}: {e}")
        return
    if snapshot_count is None:
        r.note(f"BAILEY answers ({total:,} recipes) but there is no snapshot count to compare with")
    elif total == snapshot_count:
        r.ok(f"BAILEY serves last night's snapshot exactly: {total:,} recipes (newest change {newest})")
    else:
        r.fail(f"BAILEY is NOT on last night's snapshot: it serves {total:,} recipes, "
               f"the snapshot holds {snapshot_count:,} (BAILEY's newest change: {newest})")


def check_cloud(r: Report) -> None:
    try:
        out = subprocess.run([RCLONE, "lsf", CLOUD, "--include", "recipes_*.sql.gz"],
                             capture_output=True, text=True, timeout=180)
        names = sorted(n.strip() for n in out.stdout.splitlines() if n.strip())
        if out.returncode != 0 or not names:
            r.fail(f"offsite listing failed or empty: {(out.stderr or '').strip()[:160]}")
            return
        m = re.search(r"recipes_(\d{4})-(\d\d)-(\d\d)_(\d\d)(\d\d)", names[-1])
        age = (dt.datetime.now() - dt.datetime(*map(int, m.groups()))).total_seconds() / 3600 if m else 999
        if age > MAX_AGE_H:
            r.fail(f"newest OFFSITE dump is {age:.0f}h old: {names[-1]}")
        else:
            r.ok(f"offsite: {names[-1]} ({len(names)} dumps on Google Drive)")
    except Exception as e:
        r.fail(f"offsite check failed: {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--always-email", action="store_true")
    ap.add_argument("--no-email", action="store_true")
    a = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    r = Report()
    for title, fn in (("A. ADAM backup files", lambda: check_adam(r)), ("B. last night's run", None),
                      ("C. scheduled task", None), ("D. BAILEY", None), ("E. offsite", None)):
        r.lines.append(title)
        if fn:
            snap = fn()
        elif title.startswith("B"):
            check_log(r)
        elif title.startswith("C"):
            check_task(r)
        elif title.startswith("D"):
            check_bailey(r, snap)
        else:
            check_cloud(r)
    head = (f"BACKUP CHECK {dt.datetime.now():%Y-%m-%d %H:%M} on {__import__('socket').gethostname()}: "
            + (f"{len(r.fails)} PROBLEM(S)" if r.fails else "all good"))
    body = head + "\n\n" + "\n".join(r.lines)
    if r.fails:
        body = head + "\n\nWHAT FAILED\n" + "\n".join(f"  - {f}" for f in r.fails) + "\n\nFULL REPORT\n" + "\n".join(r.lines)
    print(body)
    if not a.no_email and (r.fails or a.always_email):
        sys.path.insert(0, str(HERE))
        from input.pipeline.alerts import alert_curator
        alert_curator(("backup/BAILEY sync: " + r.fails[0][:70]) if r.fails
                      else "backup check: all good", body)
    return 1 if r.fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
