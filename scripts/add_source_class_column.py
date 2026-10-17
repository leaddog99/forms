"""One-off: add the `source_class` generated column (+ index) to master_recipes and
recipes, with sqlite-vec LOADED.

Why a one-off and not the startup ensure: ALTER TABLE on these tables re-validates
their vec cleanup triggers, which reference vec0 virtual tables; on the plain
schema-ensure connection that is "no such module: vec0" (the 2026-10-16 restart
loop). The startup loop swallows that OperationalError, so the column would
silently never appear on a live database. Run once:

    python scripts/add_source_class_column.py              # apply
    python scripts/add_source_class_column.py --check      # report only
    python scripts/add_source_class_column.py --recreate   # drop + re-add (the expression changed)

Idempotent: a table that already has the column is left alone unless --recreate.
The expression is SOURCE_CLASS_SQL in save_recipe_api.py - ONE definition; importing
the module runs its schema ensure, which is harmless here.
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from input.pipeline import vector_store  # noqa: E402
from save_recipe_api import SOURCE_CLASS_SQL as SOURCE_CLASS_EXPR  # noqa: E402

DB = os.path.join(ROOT, "recipes.db")


def main() -> int:
    check = "--check" in sys.argv
    recreate = "--recreate" in sys.argv
    conn = sqlite3.connect(DB, timeout=60)
    vector_store.enable_vec(conn)
    rc = 0
    for tbl in ("master_recipes", "recipes"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_xinfo({tbl})")]
        if "source_class" in cols and recreate and not check:
            conn.execute(f"DROP INDEX IF EXISTS idx_{tbl}_source_class")
            conn.execute(f"ALTER TABLE {tbl} DROP COLUMN source_class")
            cols.remove("source_class")
            print(f"{tbl}: source_class dropped for re-creation")
        if "source_class" in cols:
            print(f"{tbl}: source_class present")
        elif check:
            print(f"{tbl}: source_class MISSING")
            rc = 1
        else:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN source_class TEXT "
                         f"GENERATED ALWAYS AS ({SOURCE_CLASS_EXPR}) VIRTUAL")
            print(f"{tbl}: source_class added")
        if not check:
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{tbl}_source_class ON {tbl}(user_id, source_class)")
        if "source_class" in cols or not check:
            dist = conn.execute(f"SELECT source_class, COUNT(*) FROM {tbl} GROUP BY 1").fetchall()
            print(f"  {tbl} classes: {dist}")
    conn.commit()
    conn.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
