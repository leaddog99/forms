"""ingredients_lib.py — the ingredient synonym dictionary (canonical-data-in-DB).

A curated map of ingredient SYNONYMS -> a CANONICAL term, so the embedding
composition (and anything else that wants consistent ingredient names) can
normalize before it embeds. Motivation: text-embedding-3-small places
"arborio rice" and "risotto-style rice" ~0.42 L2 apart in short identity text
even though they're the same thing — that phrasing variance scatters same-dish
recipes. Normalizing to one canonical term removes it.

Same ACD shape as the other masters (chapters / domains / cook_tips_kb): the DB
table is canonical; `ingredient_synonyms_seed.json` (git-tracked) bootstraps a
fresh install and is the version-controlled backup; a curator edits via the a/c/d
form. Code constants are seed only ([[feedback_no_data_in_code]]).

Lookup is via a cached, lowercased map so `normalize()` is pure + fast (no
per-call DB hit) — loaded at startup, invalidated on edit.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_SEED_FILE = os.path.join(os.path.dirname(__file__), "ingredient_synonyms_seed.json")

# The relationship an alias has to its canonical — curation discipline so we don't
# bury distinct relationships in one bucket (the ChatGPT/own taxonomy point):
#   synonym  — the SAME thing, another name/spelling (aubergine = eggplant). Always safe.
#   regional — regional name (rocket = arugula, coriander leaf = cilantro).
#   misspelling — a typo'd variant.
#   brand — a brand name standing in for the generic.
#   variety — a DIFFERENT ingredient that behaves similarly (arborio vs carnaroli).
#             Kept as its OWN canonical, NOT merged — recorded here only as a
#             cross-reference; true substitution/context belongs in a future
#             substitution table (see docs).
ALIAS_TYPES = ("synonym", "regional", "misspelling", "brand", "variety")

# Cached {lowercased synonym -> canonical}. None = not loaded yet.
_MAP: Optional[dict] = None

_WS = re.compile(r"\s+")


def _clean(term: str) -> str:
    """Light surface-normalize: lowercase, collapse whitespace, strip. The
    floor every term gets even when it isn't in the dictionary."""
    return _WS.sub(" ", (term or "").strip().lower())


def ensure_ingredient_synonyms_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingredient_synonyms (
            synonym    TEXT PRIMARY KEY,   -- variant phrase (lowercased)
            canonical  TEXT NOT NULL,      -- the canonical term it maps to
            alias_type TEXT DEFAULT 'synonym',  -- synonym|regional|misspelling|brand|variety
            category   TEXT,               -- optional grouping (rice, cheese, ...)
            note       TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ingsyn_canonical ON ingredient_synonyms(canonical)")
    if "alias_type" not in {r[1] for r in conn.execute("PRAGMA table_info(ingredient_synonyms)")}:
        conn.execute("ALTER TABLE ingredient_synonyms ADD COLUMN alias_type TEXT DEFAULT 'synonym'")
    if conn.execute("SELECT COUNT(*) FROM ingredient_synonyms").fetchone()[0] == 0:
        _seed_from_file(conn)
    conn.commit()


def _seed_from_file(conn: sqlite3.Connection) -> int:
    """Seed a FRESH table from the git-tracked seed. Shape: a list of
    {canonical, synonyms:[...], category?, note?} groups — expanded to one row
    per (synonym -> canonical), plus a canonical->canonical identity row."""
    try:
        with open(_SEED_FILE, "r", encoding="utf-8") as f:
            groups = json.load(f)
    except (FileNotFoundError, ValueError):
        return 0
    if not isinstance(groups, list):
        return 0
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for g in groups:
        canon = _clean(g.get("canonical") or "")
        if not canon:
            continue
        # synonyms: list of strings (type 'synonym') OR {alias, type} objects.
        typed = {}
        for s in (g.get("synonyms") or []):
            if isinstance(s, dict):
                a = _clean(s.get("alias") or "")
                if a:
                    typed[a] = (s.get("type") or "synonym")
            else:
                a = _clean(s)
                if a:
                    typed.setdefault(a, "synonym")
        typed[canon] = "canonical"  # identity row
        for s, atype in typed.items():
            conn.execute(
                "INSERT OR REPLACE INTO ingredient_synonyms "
                "(synonym, canonical, alias_type, category, note, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (s, canon, atype, g.get("category"), g.get("note"), now, now),
            )
            n += 1
    return n


def load_map(conn: sqlite3.Connection) -> dict:
    """(Re)load the cached synonym map from the DB. Call at startup and after
    any edit so `normalize()` sees fresh data."""
    global _MAP
    ensure_ingredient_synonyms_table(conn)
    _MAP = {s: c for s, c in conn.execute(
        "SELECT synonym, canonical FROM ingredient_synonyms")}
    return _MAP


def invalidate() -> None:
    global _MAP
    _MAP = None


def normalize(term: str) -> str:
    """Map an ingredient phrase to its canonical term. Pure + fast (cached map).
    Unknown terms fall back to the light surface-clean — never raises, never
    drops a term. If the cache isn't loaded yet, behaves as surface-clean."""
    cleaned = _clean(term)
    if not cleaned:
        return ""
    if _MAP and cleaned in _MAP:
        return _MAP[cleaned]
    return cleaned


# --- CRUD for the a/c/d editor -------------------------------------------- #

def list_groups(conn: sqlite3.Connection) -> list[dict]:
    """Synonyms grouped by canonical term — the editor's row model. Each synonym
    is {alias, type} so the editor can show + edit the relationship."""
    rows = conn.execute(
        "SELECT canonical, synonym, alias_type, category, note FROM ingredient_synonyms "
        "ORDER BY canonical, synonym").fetchall()
    by_canon: dict = {}
    for canon, syn, atype, cat, note in rows:
        g = by_canon.setdefault(canon, {"canonical": canon, "synonyms": [],
                                        "category": cat, "note": note})
        if syn != canon:
            g["synonyms"].append({"alias": syn, "type": atype or "synonym"})
    return list(by_canon.values())


def upsert_group(conn: sqlite3.Connection, canonical: str, synonyms: list,
                 category: Optional[str] = None, note: Optional[str] = None) -> dict:
    """Replace a canonical group. `synonyms` items may be strings (type 'synonym')
    or {alias, type} objects. Idempotent."""
    canon = _clean(canonical)
    if not canon:
        raise ValueError("canonical is required")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM ingredient_synonyms WHERE canonical = ?", (canon,))
    typed = {}
    for s in (synonyms or []):
        if isinstance(s, dict):
            a = _clean(s.get("alias") or "")
            if a:
                t = s.get("type") or "synonym"
                typed[a] = t if t in ALIAS_TYPES else "synonym"
        else:
            a = _clean(s)
            if a:
                typed.setdefault(a, "synonym")
    typed[canon] = "canonical"
    for s, atype in typed.items():
        conn.execute(
            "INSERT OR REPLACE INTO ingredient_synonyms "
            "(synonym, canonical, alias_type, category, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (s, canon, atype, category, note, now, now))
    conn.commit()
    invalidate()
    return {"canonical": canon,
            "synonyms": [{"alias": a, "type": t} for a, t in typed.items() if a != canon],
            "category": category, "note": note}


def delete_group(conn: sqlite3.Connection, canonical: str) -> bool:
    cur = conn.execute("DELETE FROM ingredient_synonyms WHERE canonical = ?",
                       (_clean(canonical),))
    conn.commit()
    invalidate()
    return cur.rowcount > 0
