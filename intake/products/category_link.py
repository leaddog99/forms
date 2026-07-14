"""intake/products/category_link.py — link the product catalog to the WS taxonomy.

The commerce join's last hop: recipe equipment → ws_category (equipment_match) → PRODUCTS.
Products carry their own `product_class` grain (e.g. "Loaf Pans (1 lb)"); we map each
DISTINCT product_class → its nearest `ws_category` by embedding (the class name is embedded
and cosine-matched against the ws_categories vectors), so all products in a class inherit the
link with one mapping. Curator can override a mapping (source='manual').

  product_class  ──map──►  ws_category  ──►  products (WHERE product_class in that map)

Validated: Loaf Pans (1 lb)→Bread & Loaf Pans (0.67), Santoku Knives→Santoku & Nakiri (0.65),
Woks→Woks (0.67). See docs/equipment-product-linking.md, memory/project_equipment_standardization.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from input.pipeline.embeddings import embed_text, bytes_to_vec, cosine


def ensure_map_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_class_ws_map (
            product_class  TEXT PRIMARY KEY,
            category       TEXT,               -- the product's own category (Bakeware…)
            ws_category_id INTEGER,
            ws_path        TEXT,
            score          REAL,               -- cosine of the auto-match (NULL if manual-only)
            source         TEXT DEFAULT 'auto',-- 'auto' | 'manual'
            updated_at     TEXT
        )""")
    conn.commit()


def _load_ws(conn):
    return [(r[0], r[1], bytes_to_vec(r[2]))
            for r in conn.execute(
                "SELECT id, ws_path, embedding FROM ws_categories WHERE embedding IS NOT NULL")
            if bytes_to_vec(r[2]) is not None]


def relink_product_classes(conn: sqlite3.Connection, *, only_missing: bool = False) -> dict:
    """Auto-map every distinct product_class → nearest ws_category by embedding. Preserves
    curator (source='manual') mappings. `only_missing`=True skips classes already mapped.
    Returns {classes, mapped, skipped_manual}."""
    ensure_map_table(conn)
    ws = _load_ws(conn)
    if not ws:
        return {"classes": 0, "mapped": 0, "skipped_manual": 0, "error": "no ws_categories embeddings"}
    manual = {r[0] for r in conn.execute(
        "SELECT product_class FROM product_class_ws_map WHERE source = 'manual'")}
    have = {r[0] for r in conn.execute("SELECT product_class FROM product_class_ws_map")}
    classes = conn.execute(
        "SELECT product_class, category FROM products "
        "WHERE product_class IS NOT NULL AND product_class <> '' "
        "GROUP BY product_class, category").fetchall()
    mapped = skipped = 0
    now = datetime.now(timezone.utc).isoformat()
    for pc, cat in classes:
        if pc in manual:
            skipped += 1
            continue
        if only_missing and pc in have:
            continue
        q = embed_text(f"{pc} ({cat})" if cat else pc)
        best_id, best_path, best = None, None, -1.0
        for cid, path, vec in ws:
            s = cosine(q, vec)
            if s > best:
                best_id, best_path, best = cid, path, s
        conn.execute(
            "INSERT OR REPLACE INTO product_class_ws_map "
            "(product_class, category, ws_category_id, ws_path, score, source, updated_at) "
            "VALUES (?,?,?,?,?, 'auto', ?)",
            (pc, cat, best_id, best_path, round(float(best), 4), now))
        mapped += 1
    conn.commit()
    return {"classes": len(classes), "mapped": mapped, "skipped_manual": skipped}


def set_manual_map(conn: sqlite3.Connection, product_class: str, ws_category_id: int) -> dict:
    """Curator override: pin a product_class to a specific ws_category (source='manual')."""
    ensure_map_table(conn)
    row = conn.execute("SELECT ws_path FROM ws_categories WHERE id = ?", (ws_category_id,)).fetchone()
    if not row:
        raise ValueError("ws_category not found")
    cat = conn.execute(
        "SELECT category FROM products WHERE product_class = ? LIMIT 1", (product_class,)).fetchone()
    conn.execute(
        "INSERT OR REPLACE INTO product_class_ws_map "
        "(product_class, category, ws_category_id, ws_path, score, source, updated_at) "
        "VALUES (?,?,?,?, NULL, 'manual', ?)",
        (product_class, (cat[0] if cat else None), ws_category_id, row[0],
         datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return {"product_class": product_class, "ws_category_id": ws_category_id, "ws_path": row[0]}


def products_for_ws_category(conn: sqlite3.Connection, ws_category_id: int, *, limit: int = 12) -> list:
    """Catalog products linked to a ws_category (via product_class_ws_map), best first.
    Ranked by the product's rank_score (verdict tier from review ingestion)."""
    ensure_map_table(conn)
    rows = conn.execute(
        """
        SELECT p.product_id, p.name, p.brand, p.product_class, p.rank_score, p.data
        FROM products p
        JOIN product_class_ws_map m ON m.product_class = p.product_class
        WHERE m.ws_category_id = ?
        ORDER BY p.rank_score IS NULL, p.rank_score DESC, p.name
        LIMIT ?
        """, (ws_category_id, limit)).fetchall()
    out = []
    for pid, name, brand, pc, rank, data in rows:
        offer = None
        try:
            import json
            d = json.loads(data) if data else {}
            offers = d.get("retailer_offers") or []
            if offers:
                o = offers[0]
                offer = {"retailer": o.get("retailer"), "url": o.get("url"), "price": o.get("price")}
        except Exception:
            pass
        out.append({"product_id": pid, "name": name, "brand": brand,
                    "product_class": pc, "rank_score": rank, "offer": offer})
    return out
