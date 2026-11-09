"""CLI — see the package docstring."""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import load, verify
from .sqlite_schema import introspect
from .translate import build_plan, plan_text


def _dsn(args) -> str:
    dsn = args.dsn or os.environ.get("BCC_PG_DSN")
    if not dsn:
        sys.exit("no DSN: pass --dsn or set BCC_PG_DSN (e.g. postgresql://bcc@127.0.0.1:5432/bcc)")
    return dsn


def cmd_plan(args) -> int:
    src = load.open_sqlite(args.sqlite)
    schema = introspect(src)
    plan = build_plan(schema, vector_ok=not args.no_vector)
    print(plan_text(plan))
    if args.ddl:
        with open(args.ddl, "w", encoding="utf-8") as f:
            f.write("-- functions\n" + load.FUNCTIONS_SQL + "\n")
            for s in plan.create_tables:
                f.write(s + ";\n\n")
            for s in plan.create_indexes:
                f.write(s + ";\n")
            f.write("\n" + load.FTS_SETUP_SQL + "\n")
            for s in plan.post_load:
                f.write(s + ";\n")
        print(f"DDL written to {args.ddl}")
    return 0


def cmd_run(args) -> int:
    dsn = _dsn(args)
    t0 = time.perf_counter()
    only = set(args.tables.split(",")) if args.tables else None
    plan, counts = load.run(args.sqlite, dsn, drop=args.drop, only=only)
    print(f"loaded {sum(counts.values())} rows into {len(counts)} tables in {time.perf_counter() - t0:.0f}s")
    for w in plan.warnings:
        print("WARNING " + w)
    if args.skip_verify:
        return 0
    rep = verify.run(args.sqlite, dsn)
    return 0 if rep.ok else 2


def cmd_verify(args) -> int:
    rep = verify.run(args.sqlite, _dsn(args))
    return 0 if rep.ok else 2


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m pgmigrate", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="print what would be built; --ddl writes the SQL")
    sp.add_argument("--sqlite", default="recipes.db")
    sp.add_argument("--ddl", help="write the full DDL to this file")
    sp.add_argument("--no-vector", action="store_true", help="plan for a target without pgvector")
    sp.set_defaults(fn=cmd_plan)

    sr = sub.add_parser("run", help="create schema, copy data, index, verify")
    sr.add_argument("--sqlite", default="recipes.db")
    sr.add_argument("--dsn")
    sr.add_argument("--drop", action="store_true", help="DROP SCHEMA public CASCADE on the target first")
    sr.add_argument("--tables", help="comma list: copy only these (schema is still built in full)")
    sr.add_argument("--skip-verify", action="store_true")
    sr.set_defaults(fn=cmd_run)

    sv = sub.add_parser("verify", help="compare SQLite and Postgres")
    sv.add_argument("--sqlite", default="recipes.db")
    sv.add_argument("--dsn")
    sv.set_defaults(fn=cmd_verify)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
