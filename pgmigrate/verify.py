"""Compare the two engines after a load. Numbers, not reassurance.

  counts      every base table, SQLite vs Postgres
  generated   every generated column on recipes/master_recipes, row by row
  sortkey     bcc_sortkey ordering of master recipe names: inversions between
              the SQLite order and the Postgres order (the inventory's step 3)
  fts         a few search terms: SQLite MATCH hit set vs Postgres @@ hit set
  vectors     non-null embeddings per table; for a few dishes, the top-5 cosine
              neighbours from the SQLite BLOBs vs the Postgres <=> order
"""
from __future__ import annotations

import math
import sqlite3
import struct
from dataclasses import dataclass, field
from typing import Callable

import psycopg

from .sqlite_schema import Schema, introspect
from .translate import EMBED_DIM, FTS_TABLES, is_embedding, q

Log = Callable[[str], None]


@dataclass
class Report:
    count_mismatches: list[tuple[str, int, int]] = field(default_factory=list)
    generated_mismatches: dict[str, tuple[int, list]] = field(default_factory=dict)   # "table.col" -> (n, examples)
    sortkey_inversions: int | None = None
    sortkey_rows: int = 0
    fts: list[tuple[str, int, int, int]] = field(default_factory=list)    # term, sqlite hits, pg hits, overlap
    vectors: list[tuple[str, int, int]] = field(default_factory=list)     # table, sqlite non-null, pg non-null
    knn_overlap: list[tuple[str, int]] = field(default_factory=list)      # dish, overlap of top-5
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.count_mismatches and not any(n for n, _ in self.generated_mismatches.values())


def _close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, bool) or isinstance(b, bool):
            return a == b
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    return str(a) == str(b)


def _counts(src, pg, schema: Schema, rep: Report, log: Log) -> None:
    for t in schema.base_tables:
        a = src.execute(f'SELECT count(*) FROM "{t.name}"').fetchone()[0]
        b = pg.execute(f"SELECT count(*) FROM {q(t.name)}").fetchone()[0]
        if a != b:
            rep.count_mismatches.append((t.name, a, b))
    log(f"counts: {len(schema.base_tables)} tables, {len(rep.count_mismatches)} mismatches")
    for t, a, b in rep.count_mismatches:
        log(f"  {t}: sqlite {a} vs postgres {b}")


def _generated(src, pg, schema: Schema, rep: Report, log: Log) -> None:
    for tname in FTS_TABLES:
        t = schema.tables.get(tname)
        if not t or t.kind != "base":
            continue
        gen = [c.name for c in t.generated_columns]
        pk = t.integer_pk or t.pk[0]
        cols = ", ".join(f'"{c}"' for c in gen)
        a = {r[0]: r[1:] for r in src.execute(f'SELECT "{pk}", {cols} FROM "{tname}"')}
        b = {r[0]: r[1:] for r in pg.execute(f"SELECT {q(pk)}, {', '.join(q(c) for c in gen)} FROM {q(tname)}")}
        for i, col in enumerate(gen):
            n = 0
            ex = []
            for k, va in a.items():
                vb = b.get(k)
                if vb is None:
                    continue
                if not _close(va[i], vb[i]):
                    n += 1
                    if len(ex) < 3:
                        ex.append((k, va[i], vb[i]))
            rep.generated_mismatches[f"{tname}.{col}"] = (n, ex)
        bad = {k: v for k, v in rep.generated_mismatches.items() if k.startswith(tname + ".") and v[0]}
        log(f"generated columns on {tname}: {len(gen)} checked over {len(a)} rows, {len(bad)} with mismatches")
        for k, (n, ex) in bad.items():
            log(f"  {k}: {n} rows differ, e.g. {ex}")


def _inversions(order_a: list, order_b: list) -> int:
    """Pairs ordered one way in A and the other way in B (merge-sort count)."""
    pos = {k: i for i, k in enumerate(order_b)}
    seq = [pos[k] for k in order_a if k in pos]

    def sort_count(arr):
        if len(arr) <= 1:
            return arr, 0
        mid = len(arr) // 2
        left, a = sort_count(arr[:mid])
        right, b = sort_count(arr[mid:])
        merged, i, j, inv = [], 0, 0, a + b
        while i < len(left) and j < len(right):
            if left[i] <= right[j]:
                merged.append(left[i]); i += 1
            else:
                merged.append(right[j]); j += 1; inv += len(left) - i
        merged.extend(left[i:]); merged.extend(right[j:])
        return merged, inv

    return sort_count(seq)[1]


def _sortkey(src, pg, schema: Schema, rep: Report, log: Log) -> None:
    if "master_recipes" not in schema.tables:
        return
    try:
        import unicodedata
        typo = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                "−": "-", " ": " "}

        def fold(s):
            if s is None:
                return None
            return "".join(typo.get(ch, ch) for ch in unicodedata.normalize("NFKD", str(s))
                           if not unicodedata.combining(ch)).casefold()
        src.create_function("bcc_sortkey", 1, fold, deterministic=True)
    except Exception as e:  # pragma: no cover
        rep.notes.append(f"sortkey check skipped: {e}")
        return
    a = [r[0] for r in src.execute("SELECT id FROM master_recipes WHERE recipe_name IS NOT NULL ORDER BY bcc_sortkey(recipe_name), id")]
    b = [r[0] for r in pg.execute("SELECT id FROM master_recipes WHERE recipe_name IS NOT NULL ORDER BY bcc_sortkey(recipe_name), id")]
    rep.sortkey_rows = len(a)
    rep.sortkey_inversions = _inversions(a, b)
    log(f"sortkey: {rep.sortkey_rows} names, {rep.sortkey_inversions} inversions between the two orders")


def _fts(src, pg, schema: Schema, rep: Report, log: Log) -> None:
    if "master_recipes_fts" not in schema.tables:
        return
    terms = ["lemon chicken", "béchamel", "kenji", "shrimp", "banana bread", "nytimes"]
    for term in terms:
        try:
            a = {r[0] for r in src.execute("SELECT rowid FROM master_recipes_fts WHERE master_recipes_fts MATCH ?", (term,))}
        except sqlite3.OperationalError as e:
            rep.notes.append(f"fts sqlite {term!r}: {e}")
            continue
        b = {r[0] for r in pg.execute("SELECT id FROM master_recipes WHERE fts @@ plainto_tsquery('bcc', %s)", (term,))}
        rep.fts.append((term, len(a), len(b), len(a & b)))
    log("fts (term: sqlite hits / postgres hits / overlap): " +
        "; ".join(f"{t}: {x}/{y}/{z}" for t, x, y, z in rep.fts))


def _vectors(src, pg, schema: Schema, rep: Report, log: Log, vector_ok: bool) -> None:
    for t in schema.base_tables:
        for c in t.plain_columns:
            if is_embedding(c):
                a = src.execute(f'SELECT count("{c.name}") FROM "{t.name}"').fetchone()[0]
                b = pg.execute(f"SELECT count({q(c.name)}) FROM {q(t.name)}").fetchone()[0]
                rep.vectors.append((t.name, a, b))
    log("embeddings (table: sqlite / postgres): " + "; ".join(f"{t}: {a}/{b}" for t, a, b in rep.vectors))
    if not vector_ok or "dishes" not in schema.tables:
        return
    rows = src.execute("SELECT name, embedding FROM dishes WHERE embedding IS NOT NULL ORDER BY name LIMIT 400").fetchall()
    vecs = {n: struct.unpack(f"<{EMBED_DIM}f", e) for n, e in rows}

    def cos(u, v):
        dot = sum(x * y for x, y in zip(u, v))
        nu = math.sqrt(sum(x * x for x in u)); nv = math.sqrt(sum(x * x for x in v))
        return dot / (nu * nv) if nu and nv else 0.0

    for name in list(vecs)[:5]:
        u = vecs[name]
        top_a = [n for n, _ in sorted(((n, cos(u, v)) for n, v in vecs.items() if n != name), key=lambda x: -x[1])[:5]]
        top_b = [r[0] for r in pg.execute(
            "SELECT d.name FROM dishes d WHERE d.name <> %s AND d.embedding IS NOT NULL "
            "AND d.name IN (SELECT name FROM dishes WHERE embedding IS NOT NULL ORDER BY name LIMIT 400) "
            "ORDER BY d.embedding <=> (SELECT embedding FROM dishes WHERE name = %s) LIMIT 5", (name, name))]
        rep.knn_overlap.append((name, len(set(top_a) & set(top_b))))
    log("knn top-5 overlap (dish: shared of 5): " + "; ".join(f"{n}: {k}" for n, k in rep.knn_overlap))


def run(sqlite_path: str, dsn: str, *, log: Log = print) -> Report:
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    schema = introspect(src, count_rows=False, detect_json=False)
    rep = Report()
    with psycopg.connect(dsn) as pg:
        vector_ok = pg.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone() is not None
        _counts(src, pg, schema, rep, log)
        _generated(src, pg, schema, rep, log)
        _sortkey(src, pg, schema, rep, log)
        _fts(src, pg, schema, rep, log)
        _vectors(src, pg, schema, rep, log, vector_ok)
    for n in rep.notes:
        log("note: " + n)
    log("VERIFY " + ("OK" if rep.ok else "FAILED") + " (counts + generated columns must match exactly; the rest is reported)")
    return rep
