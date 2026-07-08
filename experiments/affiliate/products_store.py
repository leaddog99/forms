"""Products store — a DB table with embeddings, mirroring master_recipes / dishes.

Each product is a `data` JSON blob (so it holds ARBITRARY N attributes — the real catalog
will mine many from review info, no fixed schema) plus a composed `embed_text` and its
`embedding` BLOB (OpenAI text-embedding-3-small, 1536-d), stored exactly like the recipe /
dish embeddings via `input.pipeline.embeddings.vec_to_bytes`.

EXPERIMENTAL: lives in its own `experiments/affiliate/products.db` to keep recipes.db clean.
The schema is the promotable part — moving it into recipes.db + adding a `products_vec`
sqlite-vec virtual table (like recipes_master_vec) is the drop-in when the catalog grows
past a numpy-cosine scan.
"""
from __future__ import annotations

import json
import os
import sqlite3

import numpy as np

from input.pipeline.embeddings import embed_text, vec_to_bytes, bytes_to_vec

HERE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_DB = os.path.join(HERE, "products.db")
SEED = os.path.join(HERE, "product_catalog_seed.json")

# Attributes NOT folded into the embedding text (ids / urls / pure-scalar metadata). The
# embedding composes from every OTHER attribute, so review-mined attributes flow in for free.
_SKIP = {"id", "affiliate_url", "price_band", "quality_tier"}


def compose_product_text(p: dict) -> str:
    """Generic: fold every string/list attribute into one 'utility register' description.
    Scales to the real N-attribute catalog with no schema change."""
    parts = []
    for k, v in p.items():
        if k in _SKIP or k.startswith("_"):
            continue
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
        elif isinstance(v, list) and v:
            parts.append(" ".join(str(x) for x in v))
    return " | ".join(parts)


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id             INTEGER PRIMARY KEY,       -- rowid (future products_vec key)
            product_id     TEXT UNIQUE NOT NULL,      -- stable slug
            data           TEXT NOT NULL,             -- full product JSON (N attributes)
            embed_text     TEXT,                      -- the text we embedded (auditable)
            embedding      BLOB,                      -- float32 vector (embeddings.vec_to_bytes)
            embedding_model TEXT,
            created_at     TEXT NOT NULL DEFAULT '',
            updated_at     TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.commit()


def upsert_product(conn: sqlite3.Connection, p: dict, *, embed: bool = True) -> None:
    txt = compose_product_text(p)
    blob = vec_to_bytes(embed_text(txt)) if embed else None
    conn.execute(
        """
        INSERT INTO products (product_id, data, embed_text, embedding, embedding_model,
                              created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(product_id) DO UPDATE SET
            data=excluded.data, embed_text=excluded.embed_text,
            embedding=excluded.embedding, embedding_model=excluded.embedding_model,
            updated_at=datetime('now')
        """,
        (p["id"], json.dumps(p, ensure_ascii=False), txt, blob,
         "text-embedding-3-small" if embed else None),
    )
    conn.commit()


def load_seed(conn: sqlite3.Connection, seed_path: str = SEED) -> int:
    ensure_table(conn)
    products = json.load(open(seed_path, encoding="utf-8"))["products"]
    for p in products:
        upsert_product(conn, p)
    return len(products)


def all_products(conn: sqlite3.Connection) -> list[dict]:
    """Every product as {**data, _vec: np.ndarray}."""
    out = []
    for pid, dj, blob in conn.execute(
            "SELECT product_id, data, embedding FROM products ORDER BY product_id"):
        d = json.loads(dj)
        d["_vec"] = bytes_to_vec(blob) if blob is not None else None
        out.append(d)
    return out


def find_similar_products(conn: sqlite3.Connection, query_vec: np.ndarray, k: int = 5) -> list[dict]:
    """Nearest products by cosine (numpy scan — fine for a small catalog; swap for a
    sqlite-vec MATCH query when the catalog is large). Returns products with `_sim`."""
    prods = all_products(conn)
    qn = np.linalg.norm(query_vec)
    scored = []
    for p in prods:
        v = p.get("_vec")
        if v is None:
            continue
        sim = float(query_vec @ v / (qn * np.linalg.norm(v))) if qn else 0.0
        p["_sim"] = sim
        scored.append(p)
    scored.sort(key=lambda p: -p["_sim"])
    return scored[:k]


if __name__ == "__main__":
    conn = sqlite3.connect(PRODUCTS_DB)
    n = load_seed(conn)
    got = conn.execute("SELECT COUNT(*), COUNT(embedding) FROM products").fetchone()
    print(f"loaded {n} products -> {PRODUCTS_DB}; rows={got[0]} embedded={got[1]}")
    conn.close()
