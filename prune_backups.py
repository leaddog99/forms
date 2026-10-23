r"""prune_backups.py — retention for the dated DB sets on ADAM (DR gap G1, closed 2026-10-22).

The nightly backup_db.py writes a timestamped SET per run into Backups\recipes-db:
    recipes_<date>_<time>.db (+ .db-shm / .db-wal sidecars), recipes_<date>_<time>.sql.gz
    training_<date>_<time>.db
and nothing ever removed them: 169 GB / 671 files by 2026-10-22, growing ~1 GB a night.

Policy (curator's choice "A", 2026-10-22) — decided per SET (one run), never per file:
    recipes   : every set from the last 14 days
                then ONE set per ISO week (the latest of the week) up to 90 days
                then ONE set per month (the latest of the month), forever
    training  : every set from the last 7 days
                then ONE set per month, forever
Always kept, regardless of age: the newest set of each kind, env.backup, media_latest.db,
and any file whose name is not a dated set (unknown = untouched).

Dry run by default — prints what WOULD go and the totals. `--apply` deletes.

    python prune_backups.py                      # dry run against \\Adam\tbotb\Backups\recipes-db
    python prune_backups.py --apply              # delete
    python prune_backups.py --dest Z:\Backups\recipes-db --today 2026-10-22
"""
import argparse
import datetime as dt
import re
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_DEST = Path(r"\\Adam\tbotb\Backups\recipes-db")
SET_RE = re.compile(r"^(recipes|training)_(\d{4}-\d{2}-\d{2})_(\d{6})\.")

RULES = {
    #          keep-all days, weekly until (days), monthly after
    "recipes":  dict(all_days=14, weekly_until=90, monthly=True),
    "training": dict(all_days=7,  weekly_until=0,  monthly=True),
}


def scan(dest: Path):
    """{(kind, stamp): [(Path, bytes)]} for every dated set; other files are ignored."""
    sets = defaultdict(list)
    for p in dest.iterdir():
        m = SET_RE.match(p.name)
        if not m or not p.is_file():
            continue
        sets[(m[1], f"{m[2]}_{m[3]}")].append((p, p.stat().st_size))
    return sets


def decide(sets, today: dt.date):
    """Return the set of (kind, stamp) keys to KEEP."""
    keep = set()
    for kind, rule in RULES.items():
        keys = sorted(k for k in sets if k[0] == kind)
        if not keys:
            continue
        keep.add(keys[-1])                       # the newest set of each kind, always
        by_day = defaultdict(list)
        for k in keys:
            by_day[dt.date.fromisoformat(k[1][:10])].append(k)
        weekly, monthly = {}, {}
        for day in sorted(by_day):
            age = (today - day).days
            latest = by_day[day][-1]
            if age <= rule["all_days"]:
                keep.update(by_day[day])
            elif age <= rule["weekly_until"]:
                weekly[day.isocalendar()[:2]] = latest   # last set of the ISO week wins
            elif rule["monthly"]:
                monthly[(day.year, day.month)] = latest  # last set of the month wins
        keep.update(weekly.values())
        keep.update(monthly.values())
    return keep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    ap.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    ap.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    ap.add_argument("--quiet", action="store_true", help="totals only, no per-set lines")
    a = ap.parse_args(argv)

    if not a.dest.is_dir():
        print(f"prune: {a.dest} not reachable — nothing done", file=sys.stderr)
        return 2
    sets = scan(a.dest)
    keep = decide(sets, a.today)
    doomed = sorted(k for k in sets if k not in keep)
    gb = lambda n: n / 1024 ** 3

    kept_b = sum(b for k in keep for _, b in sets[k]);   kept_n = sum(len(sets[k]) for k in keep)
    gone_b = sum(b for k in doomed for _, b in sets[k]); gone_n = sum(len(sets[k]) for k in doomed)
    mode = "DELETING" if a.apply else "would delete"
    if not a.quiet:
        for k in doomed:
            print(f"  {mode}: {k[0]}_{k[1]}  ({len(sets[k])} files, {gb(sum(b for _, b in sets[k])):.2f} GB)")
    print(f"prune {a.dest}: keep {len(keep)} sets / {kept_n} files / {gb(kept_b):.1f} GB; "
          f"{mode} {len(doomed)} sets / {gone_n} files / {gb(gone_b):.1f} GB  (as of {a.today})")

    if not a.apply:
        return 0
    failed = 0
    for k in doomed:
        for p, _ in sets[k]:
            try:
                p.unlink()
            except OSError as e:
                failed += 1
                print(f"  FAILED {p.name}: {e}", file=sys.stderr)
    print(f"prune: deleted {gone_n - failed} files, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
