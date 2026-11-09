"""Build the Postgres schema and COPY every base table across.

Idempotent by construction: --drop empties the target schema first; without it
the run fails on the first CREATE TABLE that already exists, which is the honest
behaviour for a system of record (no half-merged states).
"""
from __future__ import annotations

import sqlite3
import struct
import time
from typing import Callable

import psycopg

from .sqlite_schema import Schema, Table, introspect
from .translate import EMBED_DIM, FTS_TABLES, FUNCTIONS_SQL, FTS_SETUP_SQL, Plan, build_plan, is_embedding, q

Log = Callable[[str], None]


def open_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = None
    return conn


def try_vector(pg: psycopg.Connection) -> bool:
    try:
        pg.execute("CREATE EXTENSION IF NOT EXISTS vector")
        return True
    except psycopg.Error:
        return False


def reset_schema(pg: psycopg.Connection) -> None:
    pg.execute("DROP SCHEMA public CASCADE")
    pg.execute("CREATE SCHEMA public")


def ensure_extensions(pg: psycopg.Connection) -> bool:
    pg.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    pg.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    return try_vector(pg)


# --------------------------------------------------------------------------- preflight

def preflight(src: sqlite3.Connection, schema: Schema, log: Log) -> list[str]:
    """Facts that would make the copy lie. Returned as warnings; the caller decides."""
    warns = []
    for t in schema.base_tables:
        for c in t.generated_columns:
            if c.base_type in ("REAL", "INTEGER"):
                n = src.execute(f'SELECT count(*) FROM "{t.name}" WHERE typeof("{c.name}") = \'text\'').fetchone()[0]
                if n:
                    warns.append(f"{t.name}.{c.name}: {n} rows hold TEXT in a numeric generated column (become NULL)")
        for c in t.plain_columns:
            if is_embedding(c):
                bad = src.execute(
                    f'SELECT count(*) FROM "{t.name}" WHERE "{c.name}" IS NOT NULL AND length("{c.name}") != ?',
                    (EMBED_DIM * 4,),
                ).fetchone()[0]
                if bad:
                    warns.append(f"{t.name}.{c.name}: {bad} embeddings are not {EMBED_DIM} float32 (load will FAIL)")
            if c.base_type == "TEXT" and c.name not in t.json_columns:
                n = src.execute(f'SELECT count(*) FROM "{t.name}" WHERE instr("{c.name}", char(0)) > 0').fetchone()[0]
                if n:
                    warns.append(f"{t.name}.{c.name}: {n} values contain NUL (Postgres text rejects it)")
    for w in warns:
        log("PREFLIGHT " + w)
    return warns


# --------------------------------------------------------------------------- copy

def _vector_literal(blob: bytes, *, array: bool) -> str:
    n = len(blob) // 4
    if n != EMBED_DIM or len(blob) % 4:
        raise ValueError(f"embedding of {len(blob)} bytes is not {EMBED_DIM} float32")
    vals = struct.unpack(f"<{n}f", blob)
    body = ",".join(repr(v) for v in vals)
    return "{" + body + "}" if array else "[" + body + "]"


def copy_table(src: sqlite3.Connection, pg: psycopg.Connection, t: Table, *, vector_ok: bool, log: Log) -> int:
    cols = t.plain_columns
    names = [c.name for c in cols]
    emb = [i for i, c in enumerate(cols) if is_embedding(c)]
    sel = "SELECT " + ", ".join(f'"{n}"' for n in names) + f' FROM "{t.name}"'
    order = t.integer_pk
    if order:
        sel += f' ORDER BY "{order}"'
    cur = src.execute(sel)
    cur.arraysize = 2000
    n = 0
    t0 = time.perf_counter()
    stmt = f'COPY {q(t.name)} ({", ".join(q(c) for c in names)}) FROM STDIN'
    with pg.cursor() as pcur, pcur.copy(stmt) as cp:
        while True:
            rows = cur.fetchmany()
            if not rows:
                break
            for row in rows:
                if emb:
                    row = list(row)
                    for i in emb:
                        if row[i] is not None:
                            row[i] = _vector_literal(row[i], array=not vector_ok)
                cp.write_row(row)
                n += 1
    dt = time.perf_counter() - t0
    log(f"  {t.name}: {n} rows in {dt:.1f}s")
    return n


def reset_sequences(pg: psycopg.Connection, schema: Schema) -> None:
    for t in schema.base_tables:
        pk = t.integer_pk
        if pk:
            pg.execute(
                f"SELECT setval(pg_get_serial_sequence(%s, %s), COALESCE((SELECT max({q(pk)}) FROM {q(t.name)}), 0) + 1, false)",
                (q(t.name), pk),
            )


# --------------------------------------------------------------------------- orchestration

def run(sqlite_path: str, dsn: str, *, drop: bool, only: set[str] | None, log: Log = print) -> tuple[Plan, dict[str, int]]:
    src = open_sqlite(sqlite_path)
    log(f"introspecting {sqlite_path} ...")
    schema = introspect(src)
    counts: dict[str, int] = {}
    with psycopg.connect(dsn, autocommit=True) as pg:
        if drop:
            log("dropping target schema public ...")
            reset_schema(pg)
        vector_ok = ensure_extensions(pg)
        plan = build_plan(schema, vector_ok=vector_ok)
        log(f"vector extension: {'available' if vector_ok else 'MISSING - real[] fallback'}")
        preflight(src, schema, log)

        log("creating functions + tables ...")
        pg.execute(FUNCTIONS_SQL)
        for ddl in plan.create_tables:
            pg.execute(ddl)

        log("copying ...")
        for t in schema.base_tables:
            if only and t.name not in only:
                continue
            counts[t.name] = copy_table(src, pg, t, vector_ok=vector_ok, log=log)
        reset_sequences(pg, schema)

        log(f"creating {len(plan.create_indexes)} indexes ...")
        for ddl in plan.create_indexes:
            pg.execute(ddl)

        log("full-text columns, triggers, vector indexes ...")
        pg.execute(FTS_SETUP_SQL)
        for stmt in plan.post_load:
            t0 = time.perf_counter()
            pg.execute(stmt)
            if time.perf_counter() - t0 > 2:
                log(f"  {stmt[:70]}... {time.perf_counter() - t0:.1f}s")

        log("analyze ...")
        pg.execute("ANALYZE")
    return plan, counts
