"""Equipment -> product affiliate-match experiment.

Products live in a DB table WITH embeddings (experiments/affiliate/products.db, built by
products_store from the seed). For a few real recipes, compare THREE ways of linking
products so we can eyeball match quality (quality is the whole metric):

  (A) DETERMINISTIC alias join   — extracted equipment name/category -> product alias keys.
  (B) per-equipment VECTOR match — embed a per-item needs-string, nearest product by cosine.
  (C) whole-recipe VECTOR match  — embed the RECIPE (compose_recipe_text), top-3 products.
       The "apples & oranges" baseline: should surface ON-THEME items (a French cookbook,
       gourmet oil) rather than the tools you need — i.e. why raw recipe<->product similarity
       is the wrong signal, and why matching goes through the equipment needs instead.

Run:  python -m experiments.affiliate.run_experiment  [recipe_id ...]
(no args = a curated sample incl. one _cook recipe)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from experiments.affiliate.equipment_extract import extract_equipment            # noqa: E402
from experiments.affiliate import products_store as ps                            # noqa: E402
from input.pipeline.embeddings import embed_text, compose_recipe_text            # noqa: E402

DB = os.path.join(ROOT, "recipes.db")


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(a @ b / (na * nb)) if na and nb else 0.0


def _norm(s: str) -> str:
    return " ".join((s or "").lower().replace("-", " ").split())


def deterministic_match(eq_name: str, eq_cat: str, products: list[dict]) -> list[str]:
    """Products whose aliases match this equipment item (token/substring, both directions)."""
    en = _norm(eq_name)
    hits = []
    for p in products:
        aliases = [_norm(a) for a in p.get("equipment_aliases", [])]
        if any(a and (a in en or en in a) for a in aliases):
            hits.append(p["id"])
    return hits


def load_products():
    """Open (and lazily seed) the products DB; return the product list with `_vec`."""
    pconn = sqlite3.connect(ps.PRODUCTS_DB)
    ps.ensure_table(pconn)
    n = pconn.execute("SELECT COUNT(*) FROM products WHERE embedding IS NOT NULL").fetchone()[0]
    if n == 0:
        print("[products] empty — seeding + embedding…")
        ps.load_seed(pconn)
    prods = ps.all_products(pconn)
    pconn.close()
    print(f"[products] {len(prods)} products loaded from {os.path.basename(ps.PRODUCTS_DB)}")
    return prods


def main(recipe_ids: list[str]) -> None:
    products = load_products()
    by_id = {p["id"]: p for p in products}

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    if not recipe_ids:
        recipe_ids = []
        for kw in ("stew", "stir", "cake", "risotto", "roast chicken"):
            r = conn.execute(
                "SELECT recipe_id FROM master_recipes "
                "WHERE lower(json_extract(data,'$.name')) LIKE ? "
                "AND json_extract(data,'$.recipeInstructions') IS NOT NULL LIMIT 1",
                (f"%{kw}%",)).fetchone()
            if r:
                recipe_ids.append(r["recipe_id"])
        recipe_ids.append("e5e084ec-71e4-45cd-9741-ca0e679340c3")  # Chicken Milanese (_cook)

    for rid in recipe_ids:
        row = conn.execute("SELECT data FROM master_recipes WHERE recipe_id=?", (rid,)).fetchone() \
            or conn.execute("SELECT data FROM recipes WHERE recipe_id=?", (rid,)).fetchone()
        if not row:
            print(f"\n=== {rid}: NOT FOUND ===")
            continue
        recipe = json.loads(row["data"])
        name = recipe.get("name") or "(no name)"
        equip = extract_equipment(recipe)
        src = equip[0].get("_source") if equip else "?"
        print("\n" + "=" * 82)
        print(f"RECIPE: {name}   [{rid[:8]} · equipment source={src} · {len(equip)} items]")
        print("-" * 82)

        print("EQUIPMENT -> product   (A: alias-join   |   B: vector-nearest)")
        for e in equip:
            need = f"{e['name']} — kitchen equipment for {e.get('category','')}"
            nv = embed_text(need)
            top = ps.find_similar_products(sqlite3.connect(ps.PRODUCTS_DB), nv, k=1)
            top = top[0] if top else None
            det = deterministic_match(e["name"], e.get("category", ""), products)
            det_names = ", ".join(by_id[i]["name"] for i in det) or "—"
            ess = "★" if e.get("essential") else " "
            print(f"  {ess} {e['name']:<26} A:[{det_names}]")
            if top:
                print(f"     {'':<26} B: {top['name']}  (cos {top['_sim']:.3f})")

        rv = embed_text(compose_recipe_text(recipe))
        ranked = sorted(products, key=lambda p: -cosine(rv, p["_vec"]))[:3]
        print("WHOLE-RECIPE vector (baseline — expect on-theme, NOT the needed tools):")
        for p in ranked:
            print(f"     • {p['name']:<44} (cos {cosine(rv, p['_vec']):.3f}, {p.get('category')})")

    conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
