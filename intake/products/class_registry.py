"""The product-class REGISTRY — canonical class names, embedded, snappable.

Step 2 of the dish->product pipeline (docs/dish-product-matching.md). The
`product_classes` table existed with 1 row while 38 distinct free-text class
strings lived on products/collections — four spellings of Dutch Ovens, two
Food Recyclers, three travel variants. This module makes the table the
canonical list and gives every writer a `snap()`: embed a candidate name,
return the nearest registered class, so imports / curation runs / dish-class
proposals stop inventing new spellings.

Families (curator, 2026-08-28): equipment | gourmet | travel — each seeded
from a different corpus signal and rendered differently (gourmet leads,
travel closes). The seed guess here is a HEURISTIC for bootstrap only; the
curator owns the value.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

import numpy as np

FAMILIES = ("equipment", "gourmet", "travel")

_TRAVEL = re.compile(r"\b(tour|tours|travel|hiking|walking|experience|class|classes|cruise)\b", re.I)
_GOURMET = re.compile(
    r"\b(oil|flour|rice|chocolate|cocoa|seasoning|spice|spices|extract|vanilla|vinegar|"
    r"salt|honey|sauce|paste|chunks|chips|coffee|tea|pasta|cheese|butter|syrup|jam|"
    r"preserves|mustard|tahini)\b", re.I)


def guess_family(name: str) -> str:
    if _TRAVEL.search(name or ""):
        return "travel"
    if _GOURMET.search(name or ""):
        return "gourmet"
    return "equipment"


def ensure_registry(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(product_classes)")}
    if "family" not in cols:
        conn.execute("ALTER TABLE product_classes ADD COLUMN family TEXT NOT NULL DEFAULT 'equipment'")
    if "embedding_text" not in cols:
        conn.execute("ALTER TABLE product_classes ADD COLUMN embedding_text TEXT")
    if "signals" not in cols:
        # Curator-editable TRIGGER PHRASES (JSON array) — what a cohort signal
        # can look like when it means this class ("peeled apples" -> Apple
        # Peeler; "egg yolks" -> Egg Separators). The class NAME embeds poorly
        # against ingredient phrasing (curator's insight, 2026-08-31: 'Apple
        # Peeler' is nowhere near 'granny smith'); these phrases are the
        # matchable surface, term-to-term. Encoding an implication here ONCE
        # makes it a free distance match forever after.
        conn.execute("ALTER TABLE product_classes ADD COLUMN signals TEXT")
    conn.commit()


def seed_from_catalog(conn: sqlite3.Connection) -> dict:
    """Register every class name already in use (products + curated
    collections). Variants and junk names are seeded TOO — they are real join
    values today; canonicalization is a curator pass, aided by snap
    suggestions, not a silent merge here."""
    ensure_registry(conn)
    now = _now()
    have = {r[0] for r in conn.execute("SELECT name FROM product_classes")}
    in_use = set()
    for sql in ("SELECT DISTINCT product_class FROM products WHERE product_class != ''",
                "SELECT DISTINCT product_class FROM curated_collections"):
        in_use.update(r[0] for r in conn.execute(sql) if (r[0] or "").strip())
    added = []
    for name in sorted(in_use - have):
        conn.execute(
            "INSERT INTO product_classes(name, category, criteria, buying_guide, data, "
            "family, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (name, "", "[]", "", "{}", guess_family(name), now, now))
        added.append(name)
    conn.commit()
    return {"added": added, "total": len(have) + len(added)}


def ensure_embeddings(conn: sqlite3.Connection) -> int:
    """Embed every registry row whose embedding is missing or whose embed
    text drifted (content-addressed, same discipline as dishes)."""
    from input.pipeline.embeddings import embed_text, vec_to_bytes
    ensure_registry(conn)
    done = 0
    for name, family, blob, etext in conn.execute(
            "SELECT name, family, embedding, embedding_text FROM product_classes").fetchall():
        text = f"{name}. product class."
        if blob is not None and etext == text:
            continue
        vec = embed_text(text)
        conn.execute("UPDATE product_classes SET embedding = ?, embedding_text = ?, "
                     "updated_at = ? WHERE name = ?",
                     (vec_to_bytes(vec), text, _now(), name))
        done += 1
    conn.commit()
    return done


def snap(conn: sqlite3.Connection, candidate: str, *, max_dist: float = 0.60) -> dict:
    """Nearest registered class for a candidate name.

    Returns {name, family, distance, snapped} — `snapped` False when nothing
    is within max_dist (caller should treat the candidate as a NEW class
    proposal, not silently register it). Brute-force scan: the registry is
    tens of rows, an index would be ceremony.

    max_dist 0.60, measured 2026-08-29 on the seeded registry: true matches
    landed at <=0.58 ("Dutch Oven"->"Dutch Ovens (5-6 qt)" 0.579, "Egg Yolk
    Separator"->"Egg Separators" 0.569) and genuinely-new classes at >=0.79
    ("Baking Chocolate" vs "Chocolate Chunks", "Blue Cheese" vs anything) —
    a wide margin. Registry and query embed the SAME text shape
    ("<name>. product class.") so the distance isn't inflated by asymmetry.
    """
    from input.pipeline.embeddings import embed_text, bytes_to_vec
    ensure_registry(conn)
    rows = conn.execute(
        "SELECT name, family, embedding FROM product_classes WHERE embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        return {"name": None, "family": None, "distance": None, "snapped": False}
    q = embed_text(f"{candidate}. product class.")
    qn = q / (np.linalg.norm(q) or 1.0)
    best_name, best_family, best_d = None, None, 1e9
    for name, family, blob in rows:
        v = bytes_to_vec(blob)
        vn = v / (np.linalg.norm(v) or 1.0)
        d = float(np.linalg.norm(qn - vn))       # L2 on unit vectors (matches dish match)
        if d < best_d:
            best_name, best_family, best_d = name, family, d
    return {"name": best_name, "family": best_family,
            "distance": round(best_d, 4), "snapped": best_d <= max_dist}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
