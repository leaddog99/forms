"""Consolidate every SEMrush export into the tracked archive at input/semrush/.

WHY
---
An export is not a throwaway import artifact. It carries per-URL traffic,
keywords, positions and history that we currently read one column of, and
SEMrush will not sell the same historical snapshot back to us later. Two things
were quietly losing them:

  1. `.gitignore` has `input/*.xlsx`, which matches only the ROOT of input/. On
     2026-08-07, 35 of the 55 exports sitting there were untracked and therefore
     not backed up. The other 20 predated the rule and stayed tracked, which is
     exactly why the gap looked like it wasn't there.
  2. Exports downloaded on THIS machine were never copied into the project at
     all — the harvest reads them in place from Downloads, so 71 of them existed
     only in a folder nobody backs up.

Filing them one level down (input/semrush/) makes them tracked with no
.gitignore edit and no exception to remember.

SUPERSEDING
-----------
The dedupe key is (stem, database) — NOT the domain. A domain legitimately has
more than one current export: 101cookbooks has a -gr AND a -us run, and
thepioneerwoman has a backlinks-pages export AND an organic one. Keying on the
domain alone would delete four such files as "older versions" when they are
different data.

Superseded files are MOVED to input/semrush/_superseded/, not deleted, and
--delete is opt-in. Reason: newest is not always richest. Three real cases here
have the newer export SMALLER than the one it replaces (marthastewart 74KB ->
26KB on the same day, seriouseats 1013KB -> 667KB, tasteofhome 1024KB ->
837KB), which is what a re-run with a narrower filter looks like. Those are
flagged for a human rather than silently dropped.

Usage
-----
    python scripts/collect_semrush_exports.py                    # dry run, changes nothing
    python scripts/collect_semrush_exports.py --apply            # file them
    python scripts/collect_semrush_exports.py --apply --delete   # hard-delete superseded
    python scripts/collect_semrush_exports.py --clean-inbox      # preview inbox cleanup
    python scripts/collect_semrush_exports.py --apply --clean-inbox   # and do it

--clean-inbox deletes an inbox export ONLY when a byte-identical copy is
already in the archive, verified by sha256 rather than by filename — two
different exports have shared a name here. It runs AFTER filing, so anything
unique is archived first and only then can be considered redundant.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import os
import re
import shutil
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from input.pipeline.collections_lib import (  # noqa: E402
    semrush_inbox_dir, semrush_archive_dir, semrush_export_key, _input_dir,
)

# Anything else in the inbox is somebody's spreadsheet — Downloads holds 66
# unrelated .xlsx files — so this pattern is the ONLY thing deciding what we
# touch. The (stem, database) key itself comes from collections_lib, which is
# also what the publisher-refresh job files exports with: ONE set of rules, so
# this batch sweep and the per-harvest filing can never disagree about what
# supersedes what.
IS_EXPORT = re.compile(r"(-organic\.PagesV3-|backlinks[_-]pages)", re.I)


def parse_name(fn: str):
    """(stem, db) — the supersede key — or (None, None) if not an export."""
    return semrush_export_key(fn) or (None, None)


def scan(folder: str) -> list[dict]:
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for p in glob.glob(os.path.join(folder, "*.xlsx")):
        fn = os.path.basename(p)
        if not IS_EXPORT.search(fn):
            continue
        stem, db = parse_name(fn)
        if not stem:
            continue
        out.append({
            "path": p, "fn": fn, "stem": stem, "db": db,
            "size": os.path.getsize(p), "mtime": os.path.getmtime(p),
            "src": folder,
        })
    return out


def human(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_inbox(inbox: str, archive: str, apply: bool) -> int:
    """Remove inbox exports whose CONTENT is already in the archive.

    Keyed on the file's hash, not its name. Two different exports have shared a
    filename here (allrecipes.com-backlinks_pages.xlsx exists at 668KB and
    600KB), so a name match is not proof the bytes are safe — and this is the
    one operation in this script that cannot be undone. Anything whose hash is
    not found is left alone and reported, so a partial or odd archive state can
    never turn into data loss.

    Only ever touches the inbox; the archive is read-only here.
    """
    have = {}
    for folder in (archive, os.path.join(archive, "_superseded")):
        for p in glob.glob(os.path.join(folder, "*.xlsx")):
            have.setdefault(_sha(p), p)

    redundant, unique = [], []
    for f in scan(inbox):
        (redundant if _sha(f["path"]) in have else unique).append(f)

    freed = sum(f["size"] for f in redundant) / 1e6
    print(f"\n--- clean-inbox: {len(redundant)} redundant, {len(unique)} unique "
          f"({freed:.1f} MB reclaimable) ---")
    for f in unique:
        print(f"  KEEPING (not in archive) {f['fn'][:70]}")
    if not apply:
        print("  DRY RUN — nothing deleted. Add --apply.")
        return 0

    removed = 0
    for f in redundant:
        try:
            os.remove(f["path"])
            removed += 1
        except OSError as e:
            print(f"  could not remove {f['fn']}: {e}")
    print(f"  deleted {removed} redundant export(s) from the inbox; "
          f"{len(unique)} left in place")
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually move/copy files")
    ap.add_argument("--delete", action="store_true",
                    help="hard-delete superseded files instead of retiring them")
    ap.add_argument("--clean-inbox", action="store_true",
                    help="delete inbox exports whose exact bytes are already archived")
    args = ap.parse_args()

    archive = semrush_archive_dir()
    superseded_dir = os.path.join(archive, "_superseded")
    inbox = semrush_inbox_dir()
    loose = _input_dir()

    # Archive last so an already-filed copy wins ties over a duplicate elsewhere.
    files = scan(inbox) + scan(loose) + scan(archive)
    print(f"inbox   {inbox}")
    print(f"loose   {loose}")
    print(f"archive {archive}")
    print(f"\n{len(files)} export files found "
          f"({sum(f['size'] for f in files) / 1e6:.1f} MB)\n")

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in files:
        groups[(f["stem"], f["db"])].append(f)

    to_file, to_retire, warnings = [], [], []
    for key, grp in sorted(groups.items()):
        # Identical name in two places is the SAME export, not a version.
        by_name: dict[str, dict] = {}
        for f in sorted(grp, key=lambda x: x["src"] != archive):
            by_name.setdefault(f["fn"], f)
        versions = sorted(by_name.values(), key=lambda x: x["mtime"], reverse=True)

        # Duplicate copies of the winning file elsewhere still need retiring
        # from their old home, but they are not "older versions".
        keeper = versions[0]
        for f in grp:
            if f["fn"] == keeper["fn"] and f["path"] != keeper["path"]:
                to_retire.append((f, "duplicate copy"))

        if os.path.dirname(keeper["path"]) != archive:
            to_file.append(keeper)

        for older in versions[1:]:
            to_retire.append((older, "superseded"))
            if older["size"] > keeper["size"] * 1.15:
                warnings.append((key, keeper, older))

    print(f"--- {len(to_file)} to file into the archive ---")
    for f in to_file[:200]:
        where = "inbox" if f["src"] == inbox else "input/"
        print(f"  [{where:6s}] {f['fn'][:76]}")

    print(f"\n--- {len(to_retire)} to retire "
          f"({'DELETE' if args.delete else 'move to _superseded/'}) ---")
    for f, why in to_retire[:200]:
        print(f"  [{why:14s}] {human(f['mtime'])} {f['size']/1000:7.0f}KB  {f['fn'][:60]}")

    if warnings:
        print(f"\n!!! {len(warnings)} case(s) where the NEWER export is materially "
              f"SMALLER — check before deleting:")
        for key, keep, old in warnings:
            print(f"    {key[0]}[{key[1]}]  keep {human(keep['mtime'])} "
                  f"{keep['size']/1000:.0f}KB  <  retiring {human(old['mtime'])} "
                  f"{old['size']/1000:.0f}KB")

    if not args.apply:
        if args.clean_inbox:
            clean_inbox(inbox, archive, apply=False)
        print("\nDRY RUN — nothing changed. Re-run with --apply.")
        return 0

    os.makedirs(archive, exist_ok=True)
    if not args.delete:
        os.makedirs(superseded_dir, exist_ok=True)

    filed = retired = 0
    for f in to_file:
        dest = os.path.join(archive, f["fn"])
        if os.path.exists(dest):
            continue
        # COPY from the inbox (leave the admin's Downloads alone); MOVE from the
        # loose input/ root, which is the location being emptied.
        if f["src"] == inbox:
            shutil.copy2(f["path"], dest)
        else:
            shutil.move(f["path"], dest)
        filed += 1

    for f, why in to_retire:
        if not os.path.exists(f["path"]):
            continue
        if f["src"] == inbox:
            # Never reach into the admin's Downloads folder. An older export
            # there is still data we want kept, and Downloads is not backed up,
            # so copy it into _superseded/ and leave the original alone.
            #
            # ONLY for a genuine older version. A "duplicate copy" is the same
            # file we already filed under the same name — archiving that would
            # store a second copy of the keeper on every run, which is exactly
            # what it did before this guard (70 redundant files, 9MB).
            if why == "superseded" and not args.delete:
                dest = os.path.join(superseded_dir, f["fn"])
                if not os.path.exists(dest) and not os.path.exists(
                        os.path.join(archive, f["fn"])):
                    shutil.copy2(f["path"], dest)
                    retired += 1
            continue
        if args.delete:
            os.remove(f["path"])
        else:
            dest = os.path.join(superseded_dir, f["fn"])
            if os.path.exists(dest):
                os.remove(f["path"])
            else:
                shutil.move(f["path"], dest)
        retired += 1

    print(f"\nfiled {filed}, retired {retired} -> {archive}")
    # AFTER filing, never before — an inbox file is only redundant once its
    # bytes are demonstrably in the archive.
    if args.clean_inbox:
        clean_inbox(inbox, archive, apply=True)
    print("Remember to `git add input/semrush` — that is the whole point.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
