"""Introspect a SQLite database into a plain model the translator reads.

Everything here is read-only against SQLite. The model is deliberately dumb:
tables, columns (with their generated-column expressions recovered from the
CREATE TABLE text, because PRAGMA table_xinfo only flags them), table-level
UNIQUE constraints, CHECKs, indexes split into parts, and a classification of
each table as base / fts / vec / shadow so the translator knows what to carry
and what dissolves.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Column:
    name: str
    decl: str               # declared type, as written ('' when none)
    notnull: bool
    default: Optional[str]  # SQL literal text as SQLite reports it
    pk: int                 # 0 = not in PK, else 1-based position
    hidden: int             # PRAGMA table_xinfo: 0 plain, 2 virtual generated, 3 stored generated
    gen_expr: Optional[str] = None

    @property
    def generated(self) -> bool:
        return self.hidden in (2, 3)

    @property
    def base_type(self) -> str:
        return (self.decl or "").upper().split("(")[0].strip()


@dataclass
class Index:
    name: str
    table: str
    unique: bool
    sql: str
    parts: list[str]          # column names or expressions, each may end in ' DESC'
    where: Optional[str]


@dataclass
class Table:
    name: str
    sql: str
    columns: list[Column]
    kind: str                 # base | fts | vec | virtual | shadow
    uniques: list[list[str]] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    autoincrement: bool = False
    json_columns: set[str] = field(default_factory=set)
    row_count: int = 0

    def col(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"{self.name}.{name}")

    @property
    def pk(self) -> list[str]:
        return [c.name for c in sorted((c for c in self.columns if c.pk), key=lambda c: c.pk)]

    @property
    def integer_pk(self) -> Optional[str]:
        """The rowid-alias column (single INTEGER PRIMARY KEY), if any."""
        pk = self.pk
        if len(pk) == 1 and self.col(pk[0]).base_type == "INTEGER":
            return pk[0]
        return None

    @property
    def plain_columns(self) -> list[Column]:
        return [c for c in self.columns if not c.generated]

    @property
    def generated_columns(self) -> list[Column]:
        return [c for c in self.columns if c.generated]


@dataclass
class Schema:
    tables: dict[str, Table]
    indexes: list[Index]
    triggers: list[tuple[str, str, str]]   # (name, table, sql)

    @property
    def base_tables(self) -> list[Table]:
        return [t for t in self.tables.values() if t.kind == "base"]


# --------------------------------------------------------------------------- parsing helpers

def _balanced(s: str, open_idx: int) -> int:
    """Index just past the ')' matching the '(' at open_idx. Skips quoted strings."""
    assert s[open_idx] == "(", s[open_idx:open_idx + 20]
    depth = 0
    i = open_idx
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "'":
            i += 1
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
        elif ch == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced parentheses")


def _split_top_level(s: str) -> list[str]:
    """Split on commas that are not inside parentheses or quotes."""
    parts, depth, cur, i, n = [], 0, [], 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _unquote(ident: str) -> str:
    ident = ident.strip()
    if len(ident) >= 2 and ident[0] in "\"`[" and ident[-1] in "\"`]":
        return ident[1:-1]
    return ident


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _generated_exprs(create_sql: str, columns: list[Column]) -> None:
    for c in columns:
        if not c.generated:
            continue
        pat = re.compile(
            r"(?<![\w\"])" + re.escape(c.name) + r"\"?\s+\w+(?:\s*\([^)]*\))?\s+GENERATED\s+ALWAYS\s+AS\s*\(",
            re.I,
        )
        m = pat.search(create_sql)
        if not m:
            raise ValueError(f"generated column {c.name}: expression not found in CREATE TABLE")
        open_idx = m.end() - 1
        end = _balanced(create_sql, open_idx)
        c.gen_expr = _norm(create_sql[open_idx + 1:end - 1])


def _checks(create_sql: str) -> list[str]:
    out = []
    for m in re.finditer(r"\bCHECK\s*\(", create_sql, re.I):
        open_idx = m.end() - 1
        end = _balanced(create_sql, open_idx)
        out.append(_norm(create_sql[open_idx + 1:end - 1]))
    return out


def parse_index(sql: str) -> Optional[tuple[str, str, bool, list[str], Optional[str]]]:
    s = _norm(sql)
    m = re.match(r"CREATE\s+(UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\S+)\s+ON\s+(\S+?)\s*\(", s, re.I)
    if not m:
        return None
    unique = bool(m.group(1))
    name = _unquote(m.group(2))
    table = _unquote(m.group(3))
    open_idx = m.end() - 1
    end = _balanced(s, open_idx)
    parts = [_unquote(p) if re.fullmatch(r'"[^"]+"|\w+', p) else p for p in _split_top_level(s[open_idx + 1:end - 1])]
    rest = s[end:].strip()
    where = None
    wm = re.match(r"WHERE\s+(.*)$", rest, re.I)
    if wm:
        where = wm.group(1).rstrip(";").strip()
    return name, table, unique, parts, where


# --------------------------------------------------------------------------- JSON detection

def detect_json_columns(conn: sqlite3.Connection, table: Table) -> set[str]:
    """TEXT columns whose every non-null value is a JSON object or array.

    Scalars-as-JSON ('12', '"x"') are excluded on purpose: a column of plain
    numbers written as text is not a document column. Empty strings count as
    non-JSON, so a column that mixes '' with documents stays text and is
    reported by the plan for a cleanup before it can become jsonb.
    """
    out = set()
    for c in table.plain_columns:
        if c.base_type != "TEXT":
            continue
        q = (
            f'SELECT count("{c.name}"), '
            f'sum(CASE WHEN "{c.name}" IS NOT NULL AND NOT (json_valid("{c.name}") '
            f'AND substr(ltrim("{c.name}"),1,1) IN (\'{{\',\'[\')) THEN 1 ELSE 0 END) '
            f'FROM "{table.name}"'
        )
        nonnull, bad = conn.execute(q).fetchone()
        if nonnull and not bad:
            out.add(c.name)
    return out


# --------------------------------------------------------------------------- entry point

def introspect(conn: sqlite3.Connection, *, count_rows: bool = True, detect_json: bool = True) -> Schema:
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    tables: dict[str, Table] = {}
    virtual_names: list[str] = []
    for typ, name, _tbl, sql in rows:
        if typ != "table" or name.startswith("sqlite_"):
            continue
        sql = sql or ""
        up = sql.upper()
        if up.startswith("CREATE VIRTUAL TABLE"):
            kind = "fts" if "USING FTS5" in _norm(up) else "vec" if "USING VEC0" in _norm(up) else "virtual"
            virtual_names.append(name)
        else:
            kind = "base"
        # A vec0/fts5 table's PRAGMA needs the module loaded; we never read their columns.
        cols = [] if kind != "base" else [
            Column(name=r[1], decl=r[2] or "", notnull=bool(r[3]), default=r[4], pk=r[5], hidden=r[6])
            for r in conn.execute(f'PRAGMA table_xinfo("{name}")').fetchall()
        ]
        t = Table(name=name, sql=sql, columns=cols, kind=kind)
        if kind == "base":
            _generated_exprs(sql, cols)
            t.checks = _checks(sql)
            t.autoincrement = "AUTOINCREMENT" in up
            for il in conn.execute(f'PRAGMA index_list("{name}")').fetchall():
                if il[3] == "u":   # table-level UNIQUE (autoindex), not an explicit CREATE INDEX
                    t.uniques.append([x[2] for x in conn.execute(f'PRAGMA index_info("{il[1]}")').fetchall()])
        tables[name] = t
    for t in tables.values():
        if t.kind == "base" and any(t.name.startswith(v + "_") for v in virtual_names):
            t.kind = "shadow"
    for t in tables.values():
        if t.kind != "base":
            continue
        if count_rows:
            t.row_count = conn.execute(f'SELECT count(*) FROM "{t.name}"').fetchone()[0]
        if detect_json:
            t.json_columns = detect_json_columns(conn, t)

    indexes: list[Index] = []
    for typ, name, tbl, sql in rows:
        if typ != "index" or not sql:
            continue
        parsed = parse_index(sql)
        if not parsed:
            raise ValueError(f"could not parse index: {sql}")
        iname, itable, unique, parts, where = parsed
        if tables.get(itable, Table(itable, "", [], "shadow")).kind != "base":
            continue
        indexes.append(Index(name=iname, table=itable, unique=unique, sql=sql, parts=parts, where=where))

    triggers = [(name, tbl, sql) for typ, name, tbl, sql in rows if typ == "trigger"]
    return Schema(tables=tables, indexes=indexes, triggers=triggers)
