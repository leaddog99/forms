"""Ingredient-measurement reference store — the canonical, editable layer.

The ingredient density/weight/alias data is Enrich's OWN curated reference
knowledge. The canonical copy is the `ingredient_measures` table in Enrich's DB
(see enrich/db.py); it is what the admin a/c/d editor maintains. The JSON seeds
(kingarthur_ingredient_weights.json + curated_liquids.json) are the SHIPPED
DEFAULT: they bootstrap the table on first boot and serve as a portable snapshot
for a fresh instance.

Layering (keeps the math engine pure and DB-free):
    seed JSON ── assemble_default_dataset() ──► table (canonical, editable)
                                                  │  to_dataset_dict()
                                                  ▼
                              load_dataset_from_obj()  (pure engine)

So the engine never learns SQLite exists; this module is the only DB-aware piece,
and `sync_engine()` is the one call that points the engine at the live table.
`export_snapshot()` is the "produce the file on demand" path — dump the table
back to a JSON a separate Enrich node can boot from.
"""
from __future__ import annotations

import json
from typing import Optional

from ..db import connect
# Import the functions directly: the package __init__ re-exports `convert` as the
# conversion FUNCTION, so `from . import convert` would shadow the module.
from .convert import assemble_default_dataset, load_dataset_from_obj

_DDL = """
CREATE TABLE IF NOT EXISTS ingredient_measures (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind           TEXT NOT NULL DEFAULT 'density',   -- density | count
    g_per_ml       REAL,                              -- volume<->mass bridge
    grams_per_item REAL,                              -- count<->mass bridge
    grams_per_cup  REAL,                              -- display / provenance
    aliases        TEXT,                              -- newline-separated (form-friendly)
    provenance     TEXT NOT NULL DEFAULT 'king_arthur', -- king_arthur|curated_reference|llm_derived|measured
    source_note    TEXT,
    notes          TEXT,
    description    TEXT,                              -- free text; also feeds the LLM estimate
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _split_aliases(text: Optional[str]) -> list[str]:
    if not text:
        return []
    return [a.strip() for a in text.replace(",", "\n").splitlines() if a.strip()]


def _join_aliases(aliases) -> str:
    return "\n".join(a for a in (aliases or []) if a)


def ensure_schema() -> None:
    with connect() as conn:
        conn.executescript(_DDL)
        # Self-upgrade older DBs created before `description` existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ingredient_measures)")}
        if "description" not in cols:
            conn.execute("ALTER TABLE ingredient_measures ADD COLUMN description TEXT")


def count_rows() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM ingredient_measures").fetchone()[0]


def seed_if_empty() -> int:
    """Populate the table from the shipped default (KA + curated) iff empty.
    Returns the number of rows inserted (0 if it was already populated)."""
    ensure_schema()
    if count_rows() > 0:
        return 0
    data = assemble_default_dataset()
    rows = []
    for r in data.get("ingredients", []):
        rows.append((
            r["name"], "density", r.get("g_per_ml"), None, r.get("grams_per_cup"),
            _join_aliases(r.get("aliases")), r.get("provenance", "king_arthur"),
            data.get("source", "") if r.get("provenance") == "king_arthur" else "curated reference",
        ))
    for r in data.get("count_items", []):
        rows.append((
            r["name"], "count", None, r.get("grams_per_item"), None,
            _join_aliases(r.get("aliases")), r.get("provenance", "king_arthur"),
            r.get("source", data.get("source", "")),
        ))
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO ingredient_measures "
            "(name, kind, g_per_ml, grams_per_item, grams_per_cup, aliases, "
            " provenance, source_note) VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def to_dataset_dict() -> dict:
    """Read the live table back into the dataset shape the engine consumes."""
    ensure_schema()
    ingredients, count_items = [], []
    with connect() as conn:
        for row in conn.execute("SELECT * FROM ingredient_measures ORDER BY name"):
            aliases = _split_aliases(row["aliases"])
            if row["kind"] == "count":
                count_items.append({
                    "name": row["name"], "grams_per_item": row["grams_per_item"],
                    "aliases": aliases, "provenance": row["provenance"],
                    "source": row["source_note"] or "",
                })
            else:
                ingredients.append({
                    "name": row["name"], "g_per_ml": row["g_per_ml"],
                    "grams_per_cup": row["grams_per_cup"], "aliases": aliases,
                    "provenance": row["provenance"], "source": row["source_note"] or "",
                })
    return {"source": "Enrich ingredient_measures table",
            "ingredients": ingredients, "count_items": count_items}


def reload_engine() -> int:
    """Point the pure engine at the CURRENT table (call after any edit so live
    conversions reflect it immediately). Returns the engine's entry count."""
    return load_dataset_from_obj(to_dataset_dict())


def sync_engine() -> int:
    """Seed the table on first boot, then point the pure engine at the live
    table. Call once at app/service startup. Returns the engine's entry count."""
    seed_if_empty()
    return reload_engine()


# --------------------------------------------------------------------------- #
# CRUD — the editable layer behind the a/c/d admin. Every mutation reloads the
# engine so a saved edit converts correctly on the very next request.
# --------------------------------------------------------------------------- #

_EDITABLE = ("name", "kind", "g_per_ml", "grams_per_item", "grams_per_cup",
             "provenance", "source_note", "notes", "description")


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["aliases"] = _split_aliases(d.get("aliases"))
    return d


def list_rows(search: Optional[str] = None) -> list[dict]:
    """All rows (id + fields, aliases as a list), optionally filtered by a
    case-insensitive substring over name/aliases. Ordered by name."""
    ensure_schema()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ingredient_measures ORDER BY name COLLATE NOCASE"
        ).fetchall()
    out = [_row_to_dict(r) for r in rows]
    if search:
        s = search.strip().lower()
        out = [r for r in out
               if s in r["name"].lower()
               or any(s in a.lower() for a in r["aliases"])]
    return out


def get_row(row_id: int) -> Optional[dict]:
    ensure_schema()
    with connect() as conn:
        r = conn.execute("SELECT * FROM ingredient_measures WHERE id=?",
                         (row_id,)).fetchone()
    return _row_to_dict(r) if r else None


def _normalize(data: dict) -> dict:
    """Pull the editable fields out of a payload, coercing types and aliases."""
    out = {k: data.get(k) for k in _EDITABLE if k in data}
    if "kind" in out and out["kind"] not in ("density", "count"):
        out["kind"] = "density"
    if "provenance" in out and not out["provenance"]:
        out["provenance"] = "curated_reference"
    for num in ("g_per_ml", "grams_per_item", "grams_per_cup"):
        if num in out:
            v = out[num]
            out[num] = float(v) if v not in (None, "") else None
    return out


def create_row(data: dict) -> int:
    """Insert a new ingredient. Returns the new id. Reloads the engine."""
    ensure_schema()
    fields = _normalize(data)
    if not (fields.get("name") or "").strip():
        raise ValueError("name is required")
    fields.setdefault("kind", "density")
    fields.setdefault("provenance", "curated_reference")
    fields["aliases"] = _join_aliases(data.get("aliases"))
    cols = list(fields.keys())
    placeholders = ",".join("?" for _ in cols)
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO ingredient_measures ({','.join(cols)}) VALUES ({placeholders})",
            [fields[c] for c in cols],
        )
        new_id = cur.lastrowid
    reload_engine()
    return new_id


def update_row(row_id: int, data: dict) -> bool:
    """Update an existing ingredient. Returns False if it doesn't exist.
    Reloads the engine so the edit takes effect immediately."""
    ensure_schema()
    fields = _normalize(data)
    if "aliases" in data:
        fields["aliases"] = _join_aliases(data.get("aliases"))
    if not fields:
        return get_row(row_id) is not None
    fields["updated_at"] = None  # set via SQL below
    sets = ", ".join(f"{c}=?" for c in fields if c != "updated_at")
    sets += ", updated_at=datetime('now')"
    vals = [fields[c] for c in fields if c != "updated_at"]
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE ingredient_measures SET {sets} WHERE id=?", [*vals, row_id])
        changed = cur.rowcount > 0
    if changed:
        reload_engine()
    return changed


def delete_row(row_id: int) -> bool:
    ensure_schema()
    with connect() as conn:
        cur = conn.execute("DELETE FROM ingredient_measures WHERE id=?", (row_id,))
        deleted = cur.rowcount > 0
    if deleted:
        reload_engine()
    return deleted


def export_snapshot(path: str) -> int:
    """Dump the live table to a JSON snapshot (the 'produce the file on demand'
    path) — what a fresh Enrich node can boot from. Returns row count written."""
    data = to_dataset_dict()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return len(data["ingredients"]) + len(data["count_items"])
