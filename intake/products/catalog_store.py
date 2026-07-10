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

# Embedding for the match-or-create save path (product<->product similarity). Lazy-friendly:
# the module imports fine without numpy/embeddings; only save_product/find_matches need them.
try:
    import numpy as np
    from input.pipeline.embeddings import embed_text, vec_to_bytes, bytes_to_vec
    _EMBED_OK = True
except Exception:  # pragma: no cover
    _EMBED_OK = False

# sqlite-vec (vec0) index for product<->product KNN — the scalable replacement for the
# in-Python numpy scan. Best-effort import so the module still loads without the extension.
try:
    from input.pipeline import vector_store
    _VEC_OK = True
except Exception:  # pragma: no cover
    _VEC_OK = False

from intake.products.measures import normalize_product as _normalize_product

# Confident product<->product match distance (L2 on normalized OpenAI vectors). Below this
# the two extractions are almost certainly the same product from different wording; the form
# still lets the curator confirm before merging vendors.
PRODUCT_MATCH_MAX_DIST = 0.55

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
    # vec0 index + delete trigger — after `products` exists (best-effort; the BLOB
    # column stays the source of truth, so a missing extension just disables KNN).
    if _VEC_OK:
        try:
            vector_store.ensure_product_vec_tables(conn)
            # One-time backfill: index products that predate the vec table (they
            # carry embedding BLOBs, the source of truth). Guarded to the empty-index
            # case so it runs once, not on every ensure_* call.
            if conn.execute("SELECT COUNT(*) FROM products_vec").fetchone()[0] == 0:
                if conn.execute("SELECT COUNT(*) FROM products WHERE embedding IS NOT NULL").fetchone()[0]:
                    n = vector_store.rebuild_products_vec_from_blobs(conn)
                    print(f"[VEC] products_vec backfilled {n} vectors from BLOBs")
        except Exception as e:  # pragma: no cover
            print(f"[VEC] products_vec setup skipped: {e}")


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


# Verdict tiers, best -> worst, for ranking product cards within a class.
_TIER_ORDER = [
    "Co-Winner", "Winner", "Highly Recommended", "Recommended",
    "Recommended with Reservations", "Not Recommended", "Discontinued",
]


def _tier_rank(product: dict) -> int:
    verds = product.get("verdicts") or []
    tier = (verds[0].get("tier") if verds else "") or ""
    try:
        return _TIER_ORDER.index(tier)
    except ValueError:
        return len(_TIER_ORDER)


def list_catalog(conn: sqlite3.Connection) -> dict:
    """Read-only catalog tree for the demo viewer: categories -> classes -> products,
    products sorted best-tier-first. Everything rich comes straight from each product's
    `data` JSON blob (the master_recipes pattern)."""
    ensure_product_tables(conn)
    categories = [r[0] for r in conn.execute(
        "SELECT name FROM product_categories ORDER BY name")]
    classes = []
    for name, category, criteria, guide, data in conn.execute(
            "SELECT name, category, criteria, buying_guide, data FROM product_classes "
            "ORDER BY category, name"):
        try:
            crit = json.loads(criteria) if criteria else []
        except Exception:
            crit = []
        try:
            sources = (json.loads(data) or {}).get("sources", []) if data else []
        except Exception:
            sources = []
        classes.append({"name": name, "category": category or "", "criteria": crit,
                        "buying_guide": guide or "", "sources": sources})
    products = []
    for pid, dj, rank in conn.execute(
            "SELECT product_id, data, rank_score FROM products"):
        try:
            d = json.loads(dj)
        except Exception:
            continue
        d["product_id"] = pid
        d["rank_score"] = rank
        products.append(d)
    products.sort(key=lambda p: (p.get("product_class", ""), _tier_rank(p), p.get("name", "")))
    return {"categories": categories, "classes": classes, "products": products}


# ============================================================================
#  Match-or-create save path — the product-first workflow (one product, many vendors).
#  A product is the entity (our product_id); a vendor is one RetailerOffer. Re-extracting
#  the same item from a different vendor MERGES the offer onto the existing product instead
#  of duplicating: match on the manufacturer key (brand + mpn/gtin/model_number) — which,
#  unlike an ASIN, is vendor-agnostic — else fall back to an embedding-nearest SUGGESTION the
#  curator confirms.
# ============================================================================

def compose_product_text(p: dict) -> str:
    """Utility-register text we embed for product<->product matching (brand, name, class,
    key specs). Kept generic so review-mined attributes fold in later."""
    specs = p.get("specs") or {}
    parts = [p.get("brand", ""), p.get("name", ""), p.get("product_class", ""),
             p.get("category", "")]
    for k in ("material", "capacity", "dimensions_in"):
        if specs.get(k):
            parts.append(str(specs[k]))
    # The LLM-normalized factual description is the richest semantic signal — it
    # carries material/construction/use that the structured fields miss.
    if p.get("description"):
        parts.append(str(p["description"]))
    if p.get("bcc_blurb"):
        parts.append(p["bcc_blurb"])
    return " | ".join(x for x in parts if x)


def _mfg_key(p: dict) -> str:
    """Vendor-agnostic identity: brand + the strongest manufacturer id present (GTIN > MPN >
    model_number). '' when none — then matching falls back to the embedding."""
    specs = p.get("specs") or {}
    brand = (p.get("brand") or "").strip().lower()
    for k in ("gtin", "mpn", "model_number"):
        v = str(specs.get(k) or "").strip().lower()
        if v:
            return f"{brand}|{k}:{v}"
    return ""


def _offer_key(o: dict) -> tuple:
    """De-dupe key for one vendor offer: (retailer, its own id). Re-extracting a vendor
    UPDATES its offer (price/availability), never adds a duplicate row."""
    ident = (o.get("asin") or o.get("sku") or "").strip().lower() or \
            (o.get("source_url") or "").split("?", 1)[0].rstrip("/").lower()
    return ((o.get("retailer") or "").strip().lower(), ident)


def _merge_offers(existing: dict, new_offers: list) -> None:
    offers = existing.get("retailer_offers") or []
    idx = {_offer_key(o): i for i, o in enumerate(offers)}
    for no in new_offers or []:
        k = _offer_key(no)
        if k in idx:
            offers[idx[k]].update({kk: vv for kk, vv in no.items() if vv not in (None, "", [])})
        else:
            offers.append(no)
            idx[k] = len(offers) - 1
    existing["retailer_offers"] = offers


def _fill_missing(existing: dict, incoming: dict) -> None:
    """A later vendor may carry specs/image the first didn't — fill only what's blank."""
    for f in ("image_url", "category", "product_class", "description"):
        if not existing.get(f) and incoming.get(f):
            existing[f] = incoming[f]
    es, ins = existing.setdefault("specs", {}), (incoming.get("specs") or {})
    for k, v in ins.items():
        if v and not es.get(k):
            es[k] = v


def find_matches(conn: sqlite3.Connection, product: dict, *, k: int = 3) -> dict:
    """Suggest whether an incoming product is one we already have (a new vendor of it).
    Returns {'exact': product_id|None (manufacturer-key match), 'candidates': [{product_id,
    name, distance, confident}]} (embedding-nearest). The form uses this to offer
    'add as a vendor to X?' before saving."""
    ensure_product_tables(conn)
    _normalize_product(product)   # embed the SAME canonical text the save path stores
    key = _mfg_key(product)
    exact = None
    rows = conn.execute("SELECT product_id, data, embedding FROM products").fetchall()
    if key:
        for pid, dj, _ in rows:
            try:
                if _mfg_key(json.loads(dj)) == key:
                    exact = pid
                    break
            except Exception:
                continue
    candidates = []
    if _EMBED_OK:
        try:
            vec = embed_text(compose_product_text(product))
            if _VEC_OK:
                # vec0 KNN — the engine does the scan (SIMD), we only fetch names.
                hits = vector_store.find_similar_products(conn, vec, k=k)
                ids = [h["product_id"] for h in hits]
                names = {}
                if ids:
                    ph = ",".join("?" * len(ids))
                    names = dict(conn.execute(
                        f"SELECT product_id, name FROM products WHERE product_id IN ({ph})", ids))
                candidates = [{"product_id": h["product_id"], "name": names.get(h["product_id"], ""),
                               "distance": round(h["distance"], 3),
                               "confident": h["distance"] <= PRODUCT_MATCH_MAX_DIST}
                              for h in hits]
            else:
                # Fallback: in-Python numpy scan over the BLOB column.
                scored = []
                for pid, dj, blob in rows:
                    if blob is None:
                        continue
                    d = float(np.linalg.norm(vec - bytes_to_vec(blob)))
                    nm = ""
                    try:
                        nm = json.loads(dj).get("name", "")
                    except Exception:
                        pass
                    scored.append((d, pid, nm))
                scored.sort()
                candidates = [{"product_id": pid, "name": nm, "distance": round(d, 3),
                               "confident": d <= PRODUCT_MATCH_MAX_DIST}
                              for d, pid, nm in scored[:k]]
        except Exception:
            pass
    return {"exact": exact, "candidates": candidates}


def save_product(conn: sqlite3.Connection, product: dict, *,
                 merge_into: str = None, embed: bool = True) -> dict:
    """Match-or-create. If `merge_into` (a product_id) is given, append this vendor's offer
    to that product; else auto-merge on an exact manufacturer key; else CREATE a new product
    (minting product_id) and embed it. Returns {product_id, action, vendors}. The embedding-
    nearest SUGGESTION is surfaced by find_matches (pre-save), so a create here means the
    curator declined any suggested merge."""
    ensure_product_tables(conn)
    now = _now()
    p = Product.model_validate(product).model_dump()
    _normalize_product(p)   # canonicalize dimensions/capacity/class size (idempotent)

    def _embed(rec: dict):
        """Return (blob, vec) for a product record, or (None, None) when embedding is off."""
        if not (embed and _EMBED_OK):
            return None, None
        v = embed_text(compose_product_text(rec))
        return vec_to_bytes(v), v

    def _index(pid_: str, rec: dict, vec) -> None:
        """Keep products_vec in lockstep with the BLOB (best-effort)."""
        if vec is None or not _VEC_OK:
            return
        try:
            vector_store.upsert_product_vector(
                conn, pid_, vec,
                product_class=(rec.get("product_class") or None),
                category=(rec.get("category") or None))
        except Exception as e:  # pragma: no cover
            print(f"[VEC] products_vec upsert failed for {pid_}: {e}")

    target = merge_into
    if target is None:
        key = _mfg_key(p)
        if key:
            for pid, dj in conn.execute("SELECT product_id, data FROM products"):
                try:
                    if _mfg_key(json.loads(dj)) == key:
                        target = pid
                        break
                except Exception:
                    continue

    if target:
        row = conn.execute("SELECT data FROM products WHERE product_id = ?", (target,)).fetchone()
        if row:
            existing = json.loads(row[0])
            _merge_offers(existing, p.get("retailer_offers") or [])
            _fill_missing(existing, p)
            _normalize_product(existing)   # in case the earlier row predates normalization
            blob, vec = _embed(existing)
            if blob is not None:
                conn.execute("UPDATE products SET data=?, brand=?, name=?, embedding=?, updated_at=? "
                             "WHERE product_id=?",
                             (json.dumps(existing), existing.get("brand", ""),
                              existing.get("name", ""), blob, now, target))
            else:
                conn.execute("UPDATE products SET data=?, brand=?, name=?, updated_at=? WHERE product_id=?",
                             (json.dumps(existing), existing.get("brand", ""),
                              existing.get("name", ""), now, target))
            conn.commit()
            _index(target, existing, vec)
            return {"product_id": target, "action": "merged",
                    "vendors": [o.get("retailer") for o in existing.get("retailer_offers", [])]}

    pid = str(uuid.uuid4())
    blob, vec = _embed(p)
    conn.execute(
        "INSERT INTO products(product_id, product_class, category, brand, name, data, "
        "rank_score, embedding, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, p.get("product_class", ""), p.get("category", ""), p.get("brand", ""),
         p.get("name", ""), json.dumps(p), p.get("rank_score"), blob, now, now))
    conn.commit()
    _index(pid, p, vec)
    return {"product_id": pid, "action": "created",
            "vendors": [o.get("retailer") for o in p.get("retailer_offers", [])]}


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
