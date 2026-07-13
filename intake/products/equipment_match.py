"""intake/products/equipment_match.py — map a recipe's equipment to Williams-Sonoma
product categories (the `product_class`) via the `ws_categories` embeddings.

The commerce bridge: recipe equipment mention -> nearest WS category (cosine over the
category embeddings built by scripts/build_ws_taxonomy.py) -> that category's product
class + sample products. See docs/equipment-product-linking.md and
memory/project_equipment_standardization.

Small-N by design: ~186 WS categories, so a plain in-Python cosine scan is microseconds
(no sqlite-vec needed yet; migrate if the taxonomy ever balloons). One OpenAI embed call
per DISTINCT equipment name — deduped within a recipe so a repeated tool costs once.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from input.pipeline.embeddings import embed_text, bytes_to_vec, cosine

# Below this cosine the nearest category isn't a real match — a generic/unknown "tool"
# shouldn't get force-mapped to whatever is least-far. Tuned from the validation set
# (real equipment terms scored well above this against their correct category).
MATCH_MIN_COSINE = 0.28


def load_ws_categories(conn: sqlite3.Connection) -> list:
    """[(id, headline, subcategory, ws_path, products_sample, vector)] for embedded rows."""
    rows = conn.execute(
        "SELECT id, headline, subcategory, ws_path, products_sample, embedding "
        "FROM ws_categories WHERE embedding IS NOT NULL"
    ).fetchall()
    out = []
    for rid, hl, sub, path, samp, emb in rows:
        v = bytes_to_vec(emb)
        if v is not None:
            out.append((rid, hl, sub, path, samp, v))
    return out


def _row(name: str, size, score: float, cat) -> dict:
    rid, hl, sub, path, samp, _ = cat
    return {
        "equipment": name,
        "size": size,
        "ws_category_id": rid,
        "headline": hl,
        "subcategory": sub,
        "ws_path": path,                 # the product_class, e.g. "Cookware > Fry Pans & Skillets"
        "score": round(float(score), 3),
        "matched": score >= MATCH_MIN_COSINE,
        "products_sample": samp,         # real WS product names (core categories) or None
    }


def match_name(name: str, cats: list, *, k: int = 1) -> list:
    """Top-k (score, category) for one equipment name against preloaded `cats`."""
    q = embed_text(name)
    scored = sorted(((cosine(q, c[5]), c) for c in cats), key=lambda x: -x[0])
    return scored[:k]


def match_equipment_name(name: str, conn: sqlite3.Connection, *, k: int = 1) -> list:
    """Convenience single-name lookup (loads categories each call)."""
    cats = load_ws_categories(conn)
    return [_row(name, None, s, c) for s, c in match_name(name, cats, k=k)]


def match_recipe_equipment(recipe: dict, conn: sqlite3.Connection) -> list:
    """For each of the recipe's `equipment` items, the single best WS category.
    Dedupes by lowercased name so a repeated tool is embedded once. Preserves order."""
    cats = load_ws_categories(conn)
    if not cats:
        return []
    out, cache = [], {}
    for e in (recipe.get("equipment") or []):
        if not isinstance(e, dict):
            continue
        name = (e.get("name") or "").strip()
        if not name:
            continue
        key = name.lower()
        best = cache.get(key)
        if best is None:
            res = match_name(name, cats, k=1)
            best = cache[key] = (res[0] if res else None)
        if best is None:
            continue
        score, cat = best
        out.append(_row(name, e.get("size"), score, cat))
    return out
