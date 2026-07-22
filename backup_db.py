r"""backup_db.py — back up recipes.db to the ADAM data disk (and refresh the
local .sql dump that lives in git).

What it does, in order:
  1. Writes a fresh logical dump to ./recipes.sql — every source-of-truth table
     as CREATE + INSERT text, rows in STABLE-KEY ORDER (not rowid) so the git
     diff stays small: an unchanged row keeps its position even when a table is
     delete-and-replaced (master_recipes, dish_run_data_points). EXCLUDED:
       - vec0 virtual tables (dishes_vec, recipes_master_vec) — a DERIVED index;
         shadow tables don't dump/restore cleanly. Both rebuild for free/offline
         from the source-of-truth BLOB columns (dishes.embedding,
         master_recipes.embedding) via vector_store.rebuild_master_vec_from_blobs.
         Those BLOB columns ARE in the dump, so no vectors are lost.
       - rebuildable CACHE tables (llm_extract_cache, metabase_url) — they rewrite
         on every extract / Moz lookup and dominated the diff; they're not
         source-of-truth and stay in the full ADAM .db copy below.
     The dump is the diffable, binary-independent fallback committed to git.
  2. Copies recipes.db AND recipes.sql to ADAM (\\Adam\tbotb, mapped Z:)
     under Backups\recipes-db\ with a timestamp in the filename.
  3. Verifies the copied .db with PRAGMA integrity_check before trusting it.
  4. Also copies the git-ignored training.db (the is-recipe corpus / gold human
     labels) to ADAM — best-effort, its only off-machine backup.

Run it any time you want an off-machine snapshot — especially BEFORE a risky
batch run. Reads the live DB read-only, so it's safe while the server is up.

    C:\\Users\\john\\PyCharm\\venv\\Scripts\\python.exe backup_db.py

Override the destination with --dest if ADAM is mapped elsewhere, or skip the
copy entirely (just refresh recipes.sql) with --no-adam.
"""
import argparse
import datetime
import shutil
import gzip
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "recipes.db"
# Gzip-compressed logical dump (~5x smaller). Plain text blows past GitHub's
# 100 MB per-file limit once the corpus grows; the .gz stays well under it and
# has no LFS quota. Restore: `gunzip -c recipes.sql.gz | sqlite3 new.db`.
SQL = HERE / "recipes.sql.gz"
# The is-recipe training corpus — git-ignored (never in the .sql dump), so the ADAM
# copy is its ONLY backup. It holds the curator's human_label CORRECTIONS: the gold
# signal that teaches where the heuristic is wrong. Worth off-machine copies.
TRAINING = HERE / "training.db"
DEFAULT_ADAM = Path(r"Z:\Backups\recipes-db")


# Rebuildable CACHE tables — excluded from the git dump (they rewrite on every
# extract / Moz lookup, so they dominate the diff and aren't source-of-truth).
# The full ADAM .db copy still has them; the git dump is for diffable SoT data.
_EXCLUDE_TABLES = ("llm_extract_cache", "metabase_url")

# Stable sort key per table so the dump is DETERMINISTIC and delete-and-replace
# stops reshuffling rows. Default is the primary key; the override is for tables
# whose INTEGER pk (`id`) is an autoincrement that's reassigned on reinsert —
# order by the row's durable identity instead so unchanged rows hold their place.
_ORDER_OVERRIDE = {
    "recipes": '"recipe_id"',
    "master_recipes": '"recipe_id"',
}


def _sql_literal(v) -> str:
    """Format one value as a SQLite literal (the four storage classes)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):           # before int (bool is an int subclass)
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)                # round-trippable full precision
    if isinstance(v, (bytes, bytearray)):
        return "X'" + v.hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def _order_by(mem: sqlite3.Connection, table: str) -> str | None:
    if table in _ORDER_OVERRIDE:
        return _ORDER_OVERRIDE[table]
    pk = [c[1] for c in mem.execute(f'PRAGMA table_info("{table}")') if c[5]]
    pk.sort(key=lambda name: next(c[5] for c in mem.execute(
        f'PRAGMA table_info("{table}")') if c[1] == name))  # by pk seq
    return ", ".join(f'"{c}"' for c in pk) if pk else None


def write_sql_dump(db_path: Path, out_path: Path) -> list[str]:
    """Deterministic, stable-ordered logical dump of every source-of-truth table.
    EXCLUDED: vec0 virtual tables (derived index, rebuilt from the embedding
    BLOBs) and the rebuildable cache tables (_EXCLUDE_TABLES). Rows are emitted
    in stable-key order (not rowid) so an unchanged row keeps its position across
    delete-and-replace batches — that's what keeps the git diff small. Returns the
    list of excluded vec table names."""
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

    # Real tables in creation order; their CREATE comes with each, indexes/
    # triggers/views go at the end so cross-table references resolve.
    tables = [
        (r[0], r[1]) for r in mem.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY rowid")
    ]
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        f.write("-- recipes.db logical dump (deterministic, stable-ordered)\n")
        f.write(
            f"-- excluded vec index tables ({', '.join(vts) or 'none'}) — "
            "rebuilt from the embedding BLOBs via input/pipeline/vector_store.py\n"
        )
        f.write(
            f"-- excluded rebuildable caches ({', '.join(_EXCLUDE_TABLES)}) — "
            "kept in the full ADAM .db copy, not in this diffable SoT dump\n"
        )
        f.write("PRAGMA foreign_keys=OFF;\nBEGIN TRANSACTION;\n")
        for name, create_sql in tables:
            if name in _EXCLUDE_TABLES or create_sql is None:
                continue
            f.write(create_sql + ";\n")
            # Only NON-generated columns are insertable. SELECT * / bare VALUES(...)
            # would also emit GENERATED ALWAYS (...) VIRTUAL|STORED columns (e.g.
            # master_recipes.dish_key/publisher_key) — which can't be INSERTed, so the
            # row would carry more values than the table accepts and the dump would
            # FAIL to restore. Name the insertable columns explicitly (xinfo.hidden
            # 2=VIRTUAL, 3=STORED generated).
            cols = [r[1] for r in mem.execute(f'PRAGMA table_xinfo("{name}")')
                    if r[6] not in (2, 3)]
            collist = ",".join(f'"{c}"' for c in cols)
            ob = _order_by(mem, name)
            q = f'SELECT {collist} FROM "{name}"' + (f" ORDER BY {ob}" if ob else "")
            for row in mem.execute(q):
                f.write(f'INSERT INTO "{name}" ({collist}) VALUES('
                        + ",".join(_sql_literal(v) for v in row) + ");\n")
        # indexes / triggers / views (skip auto-indexes, which have sql IS NULL)
        for name, sql in mem.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type IN ('index','trigger','view') AND sql IS NOT NULL "
                "ORDER BY type, name"):
            f.write(sql + ";\n")
        f.write("COMMIT;\n")
    src.close()
    mem.close()
    return vts


def integrity_ok(db_path: Path) -> bool:
    c = sqlite3.connect(str(db_path))
    try:
        return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        c.close()


def run_backup(dest: Path = DEFAULT_ADAM, no_adam: bool = False) -> dict:
    """Refresh ./recipes.sql and (unless no_adam) copy recipes.db + .sql to `dest`
    with a PRAGMA integrity_check on the copy. Returns a result dict; RAISES on a
    hard failure (missing DB, ADAM unreachable, integrity fail) so a scheduled-job
    handler records the error. The recipes.sql refresh happens BEFORE the ADAM copy,
    so the git-side dump is current even if ADAM is down."""
    if not DB.exists():
        raise FileNotFoundError(f"{DB} not found")
    vts = write_sql_dump(DB, SQL)
    out = {"sql_mb": round(SQL.stat().st_size / 1e6, 1), "excluded": vts, "adam": False}
    if no_adam:
        return out
    if not dest.parent.exists():  # e.g. Z:\ not mounted / UNC unreachable
        raise RuntimeError(f"{dest.parent} not reachable — is ADAM mounted?")
    dest.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    db_dst = dest / f"recipes_{ts}.db"
    sql_dst = dest / f"recipes_{ts}.sql.gz"
    shutil.copy2(DB, db_dst)
    shutil.copy2(SQL, sql_dst)
    if not integrity_ok(db_dst):
        raise RuntimeError(f"integrity_check FAILED on the backup copy {db_dst}")
    out.update({"adam": True, "db_dst": str(db_dst), "sql_dst": str(sql_dst),
                "integrity": "ok"})
    # The git-ignored training corpus (gold human labels) — its only off-machine copy.
    # Best-effort: a missing/locked training.db must not fail the recipes.db backup.
    if TRAINING.exists():
        try:
            train_dst = dest / f"training_{ts}.db"
            shutil.copy2(TRAINING, train_dst)
            out["training_dst"] = str(train_dst)
            out["training_integrity"] = "ok" if integrity_ok(train_dst) else "FAILED"
        except Exception as e:  # noqa: BLE001
            out["training_error"] = f"{type(e).__name__}: {e}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Back up recipes.db to ADAM.")
    ap.add_argument("--dest", type=Path, default=DEFAULT_ADAM,
                    help=f"backup folder (default {DEFAULT_ADAM})")
    ap.add_argument("--no-adam", action="store_true",
                    help="only refresh local recipes.sql; skip the ADAM copy")
    args = ap.parse_args()
    try:
        r = run_backup(dest=args.dest, no_adam=args.no_adam)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"{SQL.name} refreshed ({r['sql_mb']} MB); "
          f"excluded vec tables: {r['excluded'] or 'none'}")
    if r.get("adam"):
        print(f"copied -> {r['db_dst']}")
        print(f"copied -> {r['sql_dst']}")
        print("integrity_check: ok")
        if r.get("training_dst"):
            print(f"copied -> {r['training_dst']}  (training corpus, integrity: {r.get('training_integrity')})")
        elif r.get("training_error"):
            print(f"training.db NOT copied: {r['training_error']}", file=sys.stderr)
    elif args.no_adam:
        print("--no-adam: skipped ADAM copy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
