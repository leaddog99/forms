"""Move the stored thumbnails to the bucket as WebP and point the rows at them.

    python scripts/backfill_images_to_s3.py                 # dry run: report only
    python scripts/backfill_images_to_s3.py --apply         # do it (resumable)
    python scripts/backfill_images_to_s3.py --apply --limit 200

What it covers (2026-10-01, curator: "run the 11k jpeg to webp backfill with
the other transformations"):

  generated/og-thumbs/<hash>.jpg      -> bucket og-thumbs/<hash>.webp   (re-encoded)
  generated/og-thumbs/<hash>.webp     -> bucket og-thumbs/<hash>.webp   (as-is)
  generated/recipe-screens/<name>.jpg -> bucket recipe-screens/<name>.webp (re-encoded)

and every row that names one of those files by its local address:
  recipes.data        (_source.previewImage, _source.pageScreenshot)
  master_recipes.data (_source.previewImage)
  dishes.preview_image

The row gets the store's address for the new key (the direct S3 URL when S3
is on - the curator's choice over a key-plus-redirect: browsers and Google
fetch straight from the bucket, no hop through this machine or the tunnel).

What it does NOT cover: the uploaded / AI-generated heroes at the top of
generated/ (absolute tunnel-host URLs in `image` arrays - their own pass),
and deleting the local files afterwards (a separate step, once the result
has been looked at).

Honesty about the pixels: the originals are gone. This re-encodes an
already-lossy JPEG (q85) into WebP (q82) at its existing size - a second
generation. Measured on 30 real thumbnails: 242 KB -> 158 KB (65%), no
visible difference on a card. It is not the same as encoding from source.

Safety: a consistent copy of recipes.db is taken first (sqlite's page-level
backup API, into logs/); every row rewrite is recorded in an undo file as
(table, id, [old, new] pairs); rows are rewritten by exact string
substitution of the local address inside the JSON text, in batches of 500 in
short transactions, so the service stays up. A file is uploaded only if the
bucket does not already have the key, so a stopped run resumes by re-running.
A row is rewritten only after its file's upload has been confirmed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

from imagekit import derive, inspect as probe  # noqa: E402
from input.pipeline.db import connect as db_connect  # noqa: E402
from input.pipeline.image_store import get_image_store  # noqa: E402

DB = ROOT / "recipes.db"
GEN = ROOT / "generated"
QUALITY = 82
CLASSES = ("og-thumbs", "recipe-screens")
LOCAL_RE = re.compile(r"/generated/(og-thumbs|recipe-screens)/([^\"'\s?]+)")
# (table, column, key column)
TARGETS = (("recipes", "data", "id"), ("master_recipes", "data", "id"),
           ("dishes", "preview_image", "name"))


def new_key(cls: str, name: str) -> str:
    stem, _, _ext = name.rpartition(".")
    return f"{cls}/{stem or name}.webp"


def encode(path: Path) -> tuple[bytes, str]:
    """The bytes to upload: WebP at its own size for a JPEG, as-is for WebP."""
    raw = path.read_bytes()
    if path.suffix.lower() == ".webp":
        return raw, "as-is"
    p = probe(raw)
    d = derive(raw, width=p.width, height=p.height, fit="contain", fmt="WEBP",
               quality=QUALITY, keep_alpha=False)
    return d.data, f"{p.width}x{p.height}"


def referenced(conn: sqlite3.Connection) -> tuple[dict[str, set[tuple]], Counter]:
    """local path -> {(table, key)} for every row naming a local thumbnail."""
    refs: dict[str, set[tuple]] = defaultdict(set)
    per_field: Counter = Counter()
    for table, col, keycol in TARGETS:
        for key, val in conn.execute(
                f"SELECT {keycol}, {col} FROM {table} WHERE {col} LIKE '%/generated/%'"):
            for m in LOCAL_RE.finditer(str(val)):
                refs[m.group(0)].add((table, key))
                per_field[(table, m.group(1))] += 1
    return refs, per_field


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="files to process this run")
    ap.add_argument("--sample", type=int, default=20, help="dry run: conversions to measure")
    args = ap.parse_args()

    store = get_image_store()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    print(f"store: {store!r}")

    with db_connect(str(DB), timeout=30) as conn:
        refs, per_field = referenced(conn)

    # The files, by class, with their reference status.
    on_disk: dict[str, Path] = {}
    for cls in CLASSES:
        d = GEN / cls
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file():
                    on_disk[f"/generated/{cls}/{p.name}"] = p
    ref_paths = set(refs)
    todo = sorted(p for p in on_disk if p in ref_paths)          # referenced AND on disk
    unreferenced = sorted(p for p in on_disk if p not in ref_paths)
    missing = sorted(p for p in ref_paths if p not in on_disk)   # referenced, no file
    print(f"\nfiles on disk: {len(on_disk)} | referenced by a row: {len(todo)} | "
          f"on disk but unreferenced (left alone): {len(unreferenced)} | "
          f"referenced but missing on disk (rows left alone): {len(missing)}")
    print("by class:", dict(Counter(p.split('/')[2] for p in todo)))
    print("rows per table/field:", dict(per_field))
    rows_touched = set()
    for p in todo:
        rows_touched |= refs[p]
    print("rows that would be rewritten:", dict(Counter(t for t, _ in rows_touched)))
    if missing[:5]:
        print("missing examples:", missing[:5])

    if args.limit:
        todo = todo[:args.limit]

    if not args.apply:
        print(f"\nDRY RUN - measuring {min(args.sample, len(todo))} conversions:")
        tot_in = tot_out = 0
        for p in todo[:args.sample]:
            src = on_disk[p]
            data, how = encode(src)
            tot_in += src.stat().st_size; tot_out += len(data)
            print(f"  {p[11:]:<48} {src.stat().st_size//1024:>5} KB -> {len(data)//1024:>5} KB  "
                  f"({how})  key={new_key(*p.split('/')[2:4])}")
        if tot_in:
            print(f"  sample total {tot_in//1024} KB -> {tot_out//1024} KB ({100*tot_out//tot_in}%)")
        example = todo[0] if todo else None
        if example:
            print(f"\nexample row rewrite: {example}\n"
                  f"                  -> {store.url_for(new_key(*example.split('/')[2:4]))}")
        print("\nnothing written. Re-run with --apply.")
        return 0

    # ---- apply ---------------------------------------------------------------
    bk = ROOT / "logs" / f"recipes_before_image_backfill_{stamp}.db"
    src = sqlite3.connect(str(DB)); dst = sqlite3.connect(str(bk))
    src.backup(dst); dst.close(); src.close()
    print(f"\nDB backup: {bk} ({bk.stat().st_size // 1_000_000} MB)")
    undo_path = ROOT / "logs" / f"image_backfill_undo_{stamp}.json"
    undo = {"store": repr(store), "rows": []}

    # 1. files -> bucket (idempotent: skip keys the bucket already has)
    mapping: dict[str, str] = {}      # local path -> new URL, for confirmed uploads
    t0 = time.time(); n_up = n_skip = n_fail = 0; bytes_out = 0
    for i, p in enumerate(todo, 1):
        cls, name = p.split("/")[2:4]
        key = new_key(cls, name)
        try:
            if store.exists(key):
                n_skip += 1
            else:
                data, _ = encode(on_disk[p])
                store.put(key, data, content_type="image/webp")
                n_up += 1; bytes_out += len(data)
            mapping[p] = store.url_for(key)
        except Exception as e:  # noqa: BLE001
            n_fail += 1
            print(f"  FAIL {p}: {e}")
        if i % 250 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"  [{i}/{len(todo)}] uploaded {n_up} skipped {n_skip} failed {n_fail} "
                  f"| {bytes_out // 1_000_000} MB | {el:.0f}s", flush=True)

    # 2. rows -> new addresses (only for confirmed uploads)
    print("\nrewriting rows...")
    n_rows = 0
    with db_connect(str(DB), timeout=30) as conn:
        for table, col, keycol in TARGETS:
            rows = conn.execute(
                f"SELECT {keycol}, {col} FROM {table} WHERE {col} LIKE '%/generated/%'").fetchall()
            batch = []
            for key, val in rows:
                text = str(val); pairs = []
                for m in LOCAL_RE.finditer(text):
                    old = m.group(0)
                    if old in mapping and old not in [a for a, _ in pairs]:
                        pairs.append([old, mapping[old]])
                if not pairs:
                    continue
                new_text = text
                for old, new in pairs:
                    new_text = new_text.replace(old, new)
                batch.append((new_text, key))
                undo["rows"].append({"table": table, "key": key, "pairs": pairs})
                if len(batch) >= 500:
                    conn.executemany(f"UPDATE {table} SET {col} = ? WHERE {keycol} = ?", batch)
                    conn.commit(); n_rows += len(batch); batch = []
            if batch:
                conn.executemany(f"UPDATE {table} SET {col} = ? WHERE {keycol} = ?", batch)
                conn.commit(); n_rows += len(batch)
            print(f"  {table}: {sum(1 for r in undo['rows'] if r['table'] == table)} rows")
    undo_path.write_text(json.dumps(undo, indent=1), encoding="utf-8")
    print(f"\nrows rewritten: {n_rows} | undo: {undo_path}")
    print(f"uploaded {n_up}, already there {n_skip}, failed {n_fail}; "
          f"{bytes_out // 1_000_000} MB sent in {time.time() - t0:.0f}s")
    print("local files NOT deleted - a separate step.")
    return 0 if not n_fail else 1


if __name__ == "__main__":
    sys.exit(main())
