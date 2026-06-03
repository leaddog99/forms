"""Product catalog DB store — the commerce-side analog of dishes_lib +
chapters + the master_recipes persistence. Three tables mirror the recipe
trio:

    product_categories  ≈ chapters        (name PK)
    product_classes     ≈ dishes           (name PK; criteria, buying_guide, embedding…)
    products            ≈ master_recipes    (id/product_id/data JSON/embedding…)

`persist_extraction()` takes one ProductReviewExtraction (the output of a
per-source decoder in review_parsers.py) and writes the category + class +
products. Products are upserted by (product_class, name) so re-ingesting the
same review updates rather than duplicates. Cross-source merge of the SAME
product (ATK's "USA Pan 1 lb Small Loaf Pan" vs another site's wording) is the
later HOMOGENIZATION step, not done here.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from product_model import Product, ProductSpecs, ProductVerdict, RetailerOffer

_KNOWN_BRANDS = [
    "USA Pan", "Williams Sonoma", "Chicago Metallic", "Le Creuset",
    "Emile Henry", "Simply Calphalon", "OXO Good Grips", "OXO", "Pyrex",
    "Cuisinart", "Trudeau", "Wilton", "Calphalon",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guess_brand(name: str) -> str:
    n = (name or "").lower()
    for b in sorted(_KNOWN_BRANDS, key=len, reverse=True):
        if n.startswith(b.lower()):
            return b
    return name.split(" ")[0] if name else ""


def ensure_product_tables(conn: sqlite3.Connection) -> None:
    """Create the three product tables (idempotent). Mirrors the
    ensure_* migrations for chapters/dishes."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_categories (
            name        TEXT PRIMARY KEY,
            created_at  TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_classes (
            name          TEXT PRIMARY KEY,
            category      TEXT,
            criteria      TEXT,          -- JSON list
            buying_guide  TEXT,
            data          TEXT,          -- JSON (sources, etc.)
            embedding     BLOB,
            created_at    TEXT,
            updated_at    TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id             INTEGER PRIMARY KEY,
            product_id     TEXT UNIQUE,   -- uuid, like recipe_id
            product_class  TEXT,
            category       TEXT,
            brand          TEXT,
            name           TEXT,
            data           TEXT,          -- JSON = the full Product model
            rank_score     REAL,
            embedding      BLOB,
            created_at     TEXT,
            updated_at     TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_class ON products(product_class)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)")
    conn.commit()


def _build_product(pc: dict, p: dict) -> Product:
    """Adapt one parser product dict into the validated Product model."""
    return Product(
        product_class=pc["name"],
        category=pc.get("category", ""),
        brand=_guess_brand(p.get("name", "")),
        name=p.get("name", ""),
        specs=ProductSpecs(**(p.get("specs") or {})),
        verdicts=[ProductVerdict(**p["verdict"])] if p.get("verdict") else [],
        retailer_offers=[RetailerOffer(**o) for o in (p.get("retailer_offers") or [])],
        sources=[p["verdict"]["reviewer"]] if p.get("verdict") else [],
    )


def persist_extraction(conn: sqlite3.Connection, ext: dict) -> dict:
    """Write one decoded review (ProductReviewExtraction shape) to the
    catalog. Returns counts. Idempotent per (product_class, name)."""
    ensure_product_tables(conn)
    now = _now()
    pc = ext["product_class"]
    src = ext.get("review_source") or {}

    conn.execute("INSERT OR IGNORE INTO product_categories(name, created_at) VALUES (?, ?)",
                 (pc.get("category", ""), now))

    # Upsert the class; append this review source to its source list.
    row = conn.execute("SELECT data FROM product_classes WHERE name = ?", (pc["name"],)).fetchone()
    sources = []
    if row and row[0]:
        try:
            sources = (json.loads(row[0]) or {}).get("sources", [])
        except Exception:
            sources = []
    if src and not any(s.get("url") == src.get("url") for s in sources):
        sources.append(src)
    conn.execute("""
        INSERT INTO product_classes(name, category, criteria, buying_guide, data, created_at, updated_at)
        VALUES (:name, :category, :criteria, :guide, :data, :now, :now)
        ON CONFLICT(name) DO UPDATE SET
            category=excluded.category, criteria=excluded.criteria,
            buying_guide=excluded.buying_guide, data=excluded.data, updated_at=:now
    """, {
        "name": pc["name"], "category": pc.get("category", ""),
        "criteria": json.dumps(pc.get("criteria", [])),
        "guide": pc.get("buying_guide", ""),
        "data": json.dumps({"sources": sources}),
        "now": now,
    })

    inserted = updated = 0
    for p in ext.get("products", []):
        product = _build_product(pc, p)
        data = json.dumps(product.model_dump())
        existing = conn.execute(
            "SELECT product_id FROM products WHERE product_class = ? AND lower(name) = lower(?)",
            (pc["name"], product.name),
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE products SET category=?, brand=?, data=?, updated_at=? WHERE product_id=?
            """, (product.category, product.brand, data, now, existing[0]))
            updated += 1
        else:
            conn.execute("""
                INSERT INTO products(product_id, product_class, category, brand, name, data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(uuid.uuid4()), product.product_class, product.category,
                  product.brand, product.name, data, now, now))
            inserted += 1
    conn.commit()
    return {"class": pc["name"], "inserted": inserted, "updated": updated,
            "products": inserted + updated}
