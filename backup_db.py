r"""backup_db.py — back up recipes.db to the ADAM data disk (and refresh the
local .sql dump that lives in git).

What it does, in order:
  1. Writes a fresh logical dump to ./recipes.sql — every real table as
     CREATE + INSERT text. The vec0 virtual tables (dishes_vec,
     recipes_master_vec) are EXCLUDED: they're a DERIVED index and their
     shadow tables can't be dumped/restored cleanly. Both rebuild for
     free/offline from their source-of-truth BLOB columns — dishes.embedding
     and master_recipes.embedding — via vector_store.rebuild_master_vec_from_blobs
     (and the dish equivalent). Those BLOB columns ARE in the dump, so the
     git fallback no longer loses any vectors. The dump is the diffable,
     binary-independent fallback that gets committed to git.
  2. Copies recipes.db AND recipes.sql to ADAM (\\Adam\tbotb, mapped Z:)
     under Backups\recipes-db\ with a timestamp in the filename.
  3. Verifies the copied .db with PRAGMA integrity_check before trusting it.

Run it any time you want an off-machine snapshot — especially BEFORE a risky
batch run. Reads the live DB read-only, so it's safe while the server is up.

    C:\\Users\\john\\PyCharm\\venv\\Scripts\\python.exe backup_db.py

Override the destination with --dest if ADAM is mapped elsewhere, or skip the
copy entirely (just refresh recipes.sql) with --no-adam.
"""
import argparse
import datetime
import shutil
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "recipes.db"
SQL = HERE / "recipes.sql"
DEFAULT_ADAM = Path(r"Z:\Backups\recipes-db")


def write_sql_dump(db_path: Path, out_path: Path) -> list[str]:
    """Logical dump of every real table, vec0 virtual tables excluded.
    Returns the list of excluded vec table names."""
    import sqlite_vec  # only needed to drop the vec0 vtables on the copy

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    mem = sqlite3.connect(":memory:")
    mem.enable_load_extension(True)
    sqlite_vec.load(mem)
    mem.enable_load_extension(False)
    src.backup(mem)  # page-level copy; no module needed for the copy itself
    vts = [
        r[0]
        for r in mem.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND sql LIKE '%USING vec0%'"
        )
    ]
    for vt in vts:  # dropping a vec0 vtable also drops its shadow tables
        mem.execute(f'DROP TABLE IF EXISTS "{vt}"')
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("-- recipes.db logical dump\n")
        f.write(
            f"-- vector index tables excluded ({', '.join(vts) or 'none'}); "
            "rebuilt from dishes.embedding via input/pipeline/vector_store.py\n"
        )
        for line in mem.iterdump():
            f.write(line + "\n")
    src.close()
    mem.close()
    return vts


def integrity_ok(db_path: Path) -> bool:
    c = sqlite3.connect(str(db_path))
    try:
        return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Back up recipes.db to ADAM.")
    ap.add_argument("--dest", type=Path, default=DEFAULT_ADAM,
                    help=f"backup folder (default {DEFAULT_ADAM})")
    ap.add_argument("--no-adam", action="store_true",
                    help="only refresh local recipes.sql; skip the ADAM copy")
    args = ap.parse_args()

    if not DB.exists():
        print(f"ERROR: {DB} not found", file=sys.stderr)
        return 1

    vts = write_sql_dump(DB, SQL)
    print(f"recipes.sql refreshed ({SQL.stat().st_size / 1e6:.1f} MB); "
          f"excluded vec tables: {vts or 'none'}")

    if args.no_adam:
        print("--no-adam: skipped ADAM copy.")
        return 0

    dest = args.dest
    if not dest.parent.exists():  # e.g. Z:\ not mounted
        print(f"ERROR: {dest.parent} not reachable — is ADAM (Z:) mounted?",
              file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    db_dst = dest / f"recipes_{ts}.db"
    sql_dst = dest / f"recipes_{ts}.sql"
    shutil.copy2(DB, db_dst)
    shutil.copy2(SQL, sql_dst)
    print(f"copied -> {db_dst}")
    print(f"copied -> {sql_dst}")

    if integrity_ok(db_dst):
        print("integrity_check: ok")
        return 0
    print("integrity_check: FAILED on the backup copy!", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
