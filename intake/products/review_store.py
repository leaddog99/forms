"""Reviews store — reviews as a first-class, curator-editable object (ACDV), TWO-TABLE model.

PRODUCT-FIRST architecture (memory/project_monetization_pipeline, the 2026-07-08 pivot):
a review does NOT select a product — the recipe -> product_class -> rank commerce join does.
A review is the AUTHORITY LAYER that SUPPORTS a product (rank-within-class + trust copy).

Normalized, per the curator's design (reviews own their items; the link to a catalog SKU is
DYNAMIC, not stamped at ingest):

    reviews            one row per review page (the header: reviewer / title / url / dates)
    review_products    one row per item AS REVIEWED  <-- SOURCE OF TRUTH for tier/verdict/price/
                       specs + the item's retail identity (asin / model / url)
            |
            v  DYNAMIC link (review_products.product_id) resolved by identity match — recomputable,
               or set manually; NOT predetermined. One catalog product can be supported by MANY
               review_products (ATK + Serious Eats + …); a review_product can be UNLINKED.
    products           the canonical catalog SKU (product_model.Product)

Reconciliation: `products.verdicts[]` is no longer authored — it is a DERIVED PROJECTION rebuilt
from whatever review_products currently link to that product (`_reproject_product`). So the
product block + tier-ranking keep working while review_products stays the single source of truth
(no dual authoring). Projection is a direct data write (verdicts don't feed the embedding), so it
costs nothing and never re-embeds.

Migration: the pre-existing raw `products.verdicts[]` (the 13 ATK loaf-pan verdicts) are lifted
into review_products once, linked back to their SKUs by identity, then reprojected (idempotent).
Delete leaves the catalog products intact (memory/feedback_no_silent_removal) — it removes the
review + its review_products and reprojects the affected products (which empties their verdicts if
nothing else links).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from intake.products import catalog_store, review_facts

# Verdict tiers the editor offers (ordered best -> worst); '' = "ungraded".
TIERS = ["Winner", "Co-Winner", "Highly Recommended", "Recommended",
         "Recommended with Reservations", "Not Recommended", "Discontinued"]

# ReviewSource metadata the curator may edit (reviewer + product_class are the identity/join key).
_EDITABLE_META = ("title", "url", "category", "last_updated", "captured_at",
                  "rating_scale", "screenshot_id", "notes")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
#  Tables + one-time seed/migration
# ============================================================================
def ensure_reviews_table(conn: sqlite3.Connection) -> None:
    """Create both review tables (idempotent), seed review headers from
    product_classes.data.sources, and one-time-migrate legacy products.verdicts[] into
    review_products."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            id             INTEGER PRIMARY KEY,
            review_id      TEXT UNIQUE,
            reviewer       TEXT,          -- "America's Test Kitchen" (join key w/ product_class)
            product_class  TEXT,          -- "Loaf Pans (1 lb)" (join key)
            category       TEXT,
            title          TEXT,
            url            TEXT,          -- provenance only — NEVER an outbound link
            last_updated   TEXT,
            captured_at    TEXT,
            rating_scale   TEXT,
            screenshot_id  TEXT,
            notes          TEXT,
            data           TEXT,          -- JSON = full ReviewSource + extras
            created_at     TEXT,
            updated_at     TEXT
        )""")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_key "
        "ON reviews(reviewer, product_class, url)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_products (
            id                 INTEGER PRIMARY KEY,
            review_product_id  TEXT UNIQUE,
            review_id          TEXT,       -- FK -> reviews.review_id
            name               TEXT,
            brand              TEXT,
            tier               TEXT,
            summary            TEXT,       -- the reviewer's verdict prose (verbatim-ish)
            price_at_test      REAL,
            data               TEXT,       -- JSON {specs, retailer_offers, ratings, asin, url}
            product_id         TEXT,       -- DYNAMIC link to a catalog product (nullable)
            linked_by          TEXT,       -- 'asin'|'model'|'gtin'|'url'|'manual'|'migrated'|''
            created_at         TEXT,
            updated_at         TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rp_review ON review_products(review_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rp_product ON review_products(product_id)")
    # ASIN as a FIRST-CLASS column, not a key inside the JSON blob. It is the strongest
    # identity we get from a review — the reviewer's own buy link naming the exact product
    # they tested — and it drives the catalog match, the variant-family lookup, and the
    # "which sources actually reviewed this" evidence. Buried in `data` it could not be
    # queried, indexed, displayed or corrected. Additive + idempotent.
    rp_have = {r[1] for r in conn.execute("PRAGMA table_info(review_products)")}
    if "asin" not in rp_have:
        conn.execute("ALTER TABLE review_products ADD COLUMN asin TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rp_asin ON review_products(asin)")
    # WS-taxonomy classification (the curator's single type-ahead pick): the review's place in the
    # 4-level ws_categories tree (headline > section > subcategory > leaf), stored as the id + cached
    # path. Additive columns (idempotent).
    have = {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}
    if "ws_category_id" not in have:
        conn.execute("ALTER TABLE reviews ADD COLUMN ws_category_id INTEGER")
    if "ws_path" not in have:
        conn.execute("ALTER TABLE reviews ADD COLUMN ws_path TEXT")
    if "buying_guide" not in have:
        # The review's editorial header (lede + what-to-look-for/avoid + considerations) — the rich
        # raw material for writing OUR own summaries; the per-product verdicts are sparse.
        conn.execute("ALTER TABLE reviews ADD COLUMN buying_guide TEXT")
    conn.commit()
    _seed_from_sources(conn)
    _migrate_verdicts(conn)
    _backfill_ws_taxonomy(conn)


def _resolve_ws(conn: sqlite3.Connection, ws_category_id) -> tuple:
    """(ws_path, headline) for a ws_categories id, or ('', '')."""
    if not ws_category_id:
        return ("", "")
    row = conn.execute(
        "SELECT ws_path, headline FROM ws_categories WHERE id=?", (ws_category_id,)).fetchone()
    return (row[0] or "", row[1] or "") if row else ("", "")


def _backfill_ws_taxonomy(conn: sqlite3.Connection) -> int:
    """One-time: give reviews with no WS pick a sensible default from product_class_ws_map (the
    recipe->product commerce bridge already maps each product_class to a ws_category). Idempotent —
    only fills NULLs, so a curator's later pick is never overwritten."""
    try:
        rows = conn.execute(
            "SELECT review_id, product_class FROM reviews WHERE ws_category_id IS NULL").fetchall()
    except sqlite3.OperationalError:
        return 0
    n = 0
    for rid, cls in rows:
        m = conn.execute(
            "SELECT ws_category_id, ws_path FROM product_class_ws_map WHERE product_class=?",
            (cls,)).fetchone()
        if not m or not m[0]:
            continue
        _, headline = _resolve_ws(conn, m[0])
        conn.execute(
            "UPDATE reviews SET ws_category_id=?, ws_path=?, category=COALESCE(NULLIF(category,''),?),"
            " updated_at=? WHERE review_id=?",
            (m[0], m[1] or "", headline, _now(), rid))
        n += 1
    if n:
        conn.commit()
    return n


def _existing_review_id(conn: sqlite3.Connection, reviewer: str, url: str,
                        product_class: str) -> str | None:
    """Dedup identity for a review. A review PAGE is its (reviewer, url) — product_class is no longer
    a join key (items link to products by identity), so it's excluded when a url is present. Falls
    back to (reviewer, product_class) only for url-less manual reviews."""
    if url:
        row = conn.execute(
            "SELECT review_id FROM reviews WHERE reviewer=? AND url=?", (reviewer, url)).fetchone()
    else:
        row = conn.execute(
            "SELECT review_id FROM reviews WHERE reviewer=? AND product_class=? AND (url='' OR url IS NULL)",
            (reviewer, product_class)).fetchone()
    return row[0] if row else None


def _seed_from_sources(conn: sqlite3.Connection) -> int:
    """Backfill review headers from product_classes.data.sources (idempotent on the natural key).
    Seeds ONLY from recorded sources, never from verdicts, so a deleted review isn't resurrected."""
    try:
        rows = conn.execute("SELECT name, category, data FROM product_classes").fetchall()
    except sqlite3.OperationalError:
        return 0
    now, seeded = _now(), 0
    for cls_name, cls_cat, dj in rows:
        try:
            sources = (json.loads(dj) if dj else {}).get("sources") or []
        except Exception:
            sources = []
        for s in sources:
            if not isinstance(s, dict):
                continue
            reviewer = (s.get("reviewer") or "").strip()
            url = (s.get("url") or "").strip()
            if not reviewer:
                continue
            if _existing_review_id(conn, reviewer, url, cls_name):
                continue
            conn.execute(
                "INSERT OR IGNORE INTO reviews(review_id, reviewer, product_class, category, "
                "title, url, last_updated, captured_at, rating_scale, screenshot_id, notes, "
                "data, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), reviewer, cls_name, cls_cat or s.get("category", ""),
                 s.get("title", ""), url, s.get("last_updated", ""), s.get("captured_at", ""),
                 s.get("rating_scale", ""), s.get("screenshot_id", ""), "",
                 json.dumps(s), now, now))
            seeded += 1
    if seeded:
        conn.commit()
    return seeded


def _migrate_verdicts(conn: sqlite3.Connection) -> int:
    """One-time lift of legacy products.verdicts[] into review_products. Per review, runs only
    when that review has 0 review_products AND products in its class still carry a raw verdict for
    its reviewer — so it never resurrects rows the curator has since deleted (delete reprojects the
    products, emptying their verdicts). Returns rows created."""
    made = 0
    for rid, reviewer, cls in conn.execute(
            "SELECT review_id, reviewer, product_class FROM reviews"):
        if conn.execute("SELECT 1 FROM review_products WHERE review_id=?", (rid,)).fetchone():
            continue
        touched = set()
        for pid, brand, name, data in conn.execute(
                "SELECT product_id, brand, name, data FROM products WHERE product_class=?", (cls,)):
            try:
                d = json.loads(data) or {}
            except Exception:
                continue
            verd = next((v for v in (d.get("verdicts") or [])
                         if (v.get("reviewer") or "") == reviewer), None)
            if verd is None:
                continue
            _insert_review_product(conn, rid, {
                "name": name or "", "brand": brand or "",
                "tier": verd.get("tier") or "", "summary": verd.get("summary") or "",
                "price_at_test": verd.get("price_at_test"),
                "ratings": verd.get("ratings") or {},
                "specs": d.get("specs") or {},
                "retailer_offers": d.get("retailer_offers") or [],
            }, product_id=pid, linked_by="migrated")
            touched.add(pid); made += 1
        for pid in touched:
            _reproject_product(conn, pid)
    if made:
        conn.commit()
    return made


# ============================================================================
#  Dynamic identity link  (review_product <-> catalog product)
# ============================================================================
def _identity_keys(d: dict) -> set:
    """Retail-identity keys for a product-shaped dict (a catalog product blob OR a review_product
    data blob). Strong keys (model/mpn/gtin/asin) + normalized offer/plain URLs."""
    ids = set()
    specs = d.get("specs") or {}
    for k in ("model_number", "mpn", "gtin"):
        v = (specs.get(k) or "").strip().lower()
        if v:
            ids.add(f"{k}:{v}")
    for o in (d.get("retailer_offers") or []):
        if not isinstance(o, dict):
            continue
        a = (o.get("asin") or "").strip().upper() or review_facts.asin_from(o.get("source_url") or "")
        if a:
            ids.add(f"asin:{a}")
        nu = review_facts._norm(o.get("source_url") or "")
        if nu:
            ids.add(f"url:{nu}")
    if d.get("asin"):
        ids.add("asin:" + d["asin"].strip().upper())
    if d.get("url"):
        nu = review_facts._norm(d["url"])
        if nu:
            ids.add("url:" + nu)
    return ids


def _rp_data(rp_row_data: str) -> dict:
    try:
        return json.loads(rp_row_data) if rp_row_data else {}
    except Exception:
        return {}


def _find_product_for(conn: sqlite3.Connection, rp_ident: set) -> tuple:
    """(product_id, matched_key_kind) for the first catalog product whose identity intersects
    `rp_ident`, or (None, '')."""
    if not rp_ident:
        return (None, "")
    for pid, data in conn.execute("SELECT product_id, data FROM products"):
        try:
            d = json.loads(data) or {}
        except Exception:
            continue
        hit = rp_ident & _identity_keys(d)
        if hit:
            kind = next((k.split(":", 1)[0] for k in sorted(hit)
                         if k.startswith(("model_number", "mpn", "gtin", "asin"))), "url")
            return (pid, "model" if kind == "model_number" else kind)
    return (None, "")


def resolve_links(conn: sqlite3.Connection, review_id: str | None = None,
                  *, force: bool = False) -> dict:
    """(Re)compute review_products.product_id by identity match. By default only fills UNLINKED
    rows and leaves manual links; force=True re-resolves everything except manual links. Reprojects
    every product whose link set changed. Returns {linked, cleared, total}."""
    ensure_reviews_table(conn)
    q = "SELECT review_product_id, review_id, data, product_id, linked_by FROM review_products"
    params: tuple = ()
    if review_id:
        q += " WHERE review_id=?"; params = (review_id,)
    linked = cleared = 0
    affected: set = set()
    for rpid, _rid, data, cur_pid, linked_by in conn.execute(q, params).fetchall():
        if linked_by == "manual":
            continue
        if cur_pid and not force:
            continue
        new_pid, kind = _find_product_for(conn, _identity_keys(_rp_data(data)))
        if new_pid == cur_pid:
            continue
        conn.execute(
            "UPDATE review_products SET product_id=?, linked_by=?, updated_at=? WHERE review_product_id=?",
            (new_pid, (kind or "") if new_pid else "", _now(), rpid))
        if new_pid:
            linked += 1; affected.add(new_pid)
        else:
            cleared += 1
        if cur_pid:
            affected.add(cur_pid)
    conn.commit()
    for pid in affected:
        _reproject_product(conn, pid)
    total = conn.execute(
        "SELECT COUNT(*) FROM review_products" + (" WHERE review_id=?" if review_id else ""),
        params).fetchone()[0]
    return {"linked": linked, "cleared": cleared, "total": total}


# ============================================================================
#  Projection: rebuild products.verdicts[] from linked review_products
# ============================================================================
def _reproject_product(conn: sqlite3.Connection, product_id: str) -> None:
    """Rebuild ONE product's derived verdicts[] + sources[] from the review_products currently
    linked to it. Direct data write (verdicts don't feed the embedding — no re-embed, no vec)."""
    row = conn.execute("SELECT data FROM products WHERE product_id=?", (product_id,)).fetchone()
    if not row:
        return
    try:
        d = json.loads(row[0]) if row[0] else {}
    except Exception:
        d = {}
    verds = []
    for rev_er, tier, summary, price, rp_data in conn.execute(
            "SELECT rv.reviewer, rp.tier, rp.summary, rp.price_at_test, rp.data "
            "FROM review_products rp JOIN reviews rv ON rv.review_id = rp.review_id "
            "WHERE rp.product_id=?", (product_id,)):
        verds.append({
            "reviewer": rev_er or "", "tier": tier or "", "summary": summary or "",
            "ratings": (_rp_data(rp_data).get("ratings") or {}),
            "price_at_test": price,
        })
    verds.sort(key=lambda v: catalog_store._tier_rank({"verdicts": [{"tier": v["tier"]}]}))
    d["verdicts"] = verds
    d["sources"] = sorted({v["reviewer"] for v in verds if v["reviewer"]})
    conn.execute("UPDATE products SET data=?, updated_at=? WHERE product_id=?",
                 (json.dumps(d), _now(), product_id))
    conn.commit()


# ============================================================================
#  review_products CRUD
# ============================================================================
def _insert_review_product(conn: sqlite3.Connection, review_id: str, item: dict,
                           *, product_id: str | None = None, linked_by: str = "") -> str:
    rpid, now = str(uuid.uuid4()), _now()
    asin = resolve_asin(item)
    data = {
        "specs": item.get("specs") or {},
        "retailer_offers": clean_offers(item),
        "ratings": item.get("ratings") or {},
        "asin": asin,
        "url": (item.get("url") or "").strip(),
    }
    price = item.get("price_at_test")
    try:
        price = None if price in (None, "") else float(price)
    except (TypeError, ValueError):
        price = None
    conn.execute(
        "INSERT INTO review_products(review_product_id, review_id, name, brand, tier, summary, "
        "price_at_test, data, asin, product_id, linked_by, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rpid, review_id, item.get("name", ""), item.get("brand", ""), item.get("tier", ""),
         item.get("summary", ""), price, json.dumps(data), asin, product_id, linked_by,
         now, now))
    return rpid


def clean_offers(item: dict) -> list:
    """Normalize a reviewed item's buy links into PUBLISHABLE offers.

    A review's buy link is evidence of WHICH product was tested — and it is also, usually,
    the reviewer's own affiliate link (69 of the 139 we hold carry tags like
    `tag=atkequipland-20`). Republishing one pays that publisher on our sale, so:

      source_url  the link EXACTLY as the review carried it — kept, untouched, as provenance
      buy_url     a clean destination with all tracking stripped — what we may publish
      affiliate_url  stays EMPTY; our codes are applied at click time (see buy_links)

    An offer whose link is an opaque redirector we cannot unwrap offline gets buy_url = ""
    and is simply not publishable — we keep the row as evidence but never render it.
    """
    from intake.products.buy_links import clean_url, retailer_name
    out = []
    for o in (item.get("retailer_offers") or []):
        if not isinstance(o, dict):
            continue
        o = dict(o)
        raw = (o.get("source_url") or "").strip()
        o["buy_url"] = clean_url(raw or o.get("affiliate_url") or "")
        o["affiliate_url"] = ""          # never store a pre-tagged link
        if not (o.get("retailer") or "").strip() and o["buy_url"]:
            o["retailer"] = retailer_name(o["buy_url"])
        out.append(o)
    return out


def resolve_asin(item: dict) -> str:
    """The ASIN for one reviewed item — stated, or recovered from its buy links.

    Reviews almost always carry an affiliate link to the exact product tested, so the ASIN is
    usually THERE even when the extractor didn't lift it into a field. `asin_from` decodes
    redirector wrappers (ATK routes through clicks.trx-hub.com with the Amazon URL
    percent-encoded), which is why this recovers ASINs a plain field read misses.
    """
    from intake.products.review_facts import asin_from
    direct = (item.get("asin") or "").strip().upper()
    if len(direct) == 10:
        return direct
    for o in (item.get("retailer_offers") or []):
        if not isinstance(o, dict):
            continue
        for key in ("asin", "source_url", "affiliate_url"):
            a = asin_from(str(o.get(key) or ""))
            if a:
                return a
    return asin_from(str(item.get("url") or ""))


def recover_asins_via_redirects(conn: sqlite3.Connection, *, limit: int = 200,
                                timeout: int = 25) -> dict:
    """Second pass: follow OPAQUE affiliate redirects to recover the ASIN behind them.

    Publishers wrap buy links two ways. ATK embeds the Amazon URL percent-encoded in a query
    param, so `asin_from` reads it for free. Wirecutter (and others) issue an opaque server
    redirect — `nytimes.com/wirecutter/out/link/7492/22112/4/112744?merchant=Amazon` — where
    the ASIN exists only at the far end: 4 hops later it lands on /dp/B000N501BK.

    Kept OUT of the ingest path deliberately. It is network I/O per item, and a review grab
    should not hang or double in length because a publisher's redirector is slow. Run it as
    a maintenance pass over rows still missing an ASIN.

    Rows whose buy links point somewhere other than Amazon (Williams-Sonoma, a reviewer's own
    site) have no ASIN to find — they are skipped, not retried.
    """
    import requests
    from intake.products.review_facts import asin_from
    ensure_reviews_table(conn)
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    rows = conn.execute(
        "SELECT review_product_id, data FROM review_products "
        "WHERE COALESCE(asin,'') = '' LIMIT ?", (limit,)).fetchall()
    checked = found = 0
    for rpid, dj in rows:
        d = json.loads(dj or "{}")
        urls = [str(o.get(k) or "") for o in (d.get("retailer_offers") or [])
                if isinstance(o, dict) for k in ("source_url", "affiliate_url")]
        # Only chase links that plausibly END at Amazon; anything else is not an ASIN miss.
        cands = [u for u in urls if u and ("amazon" in u.lower() or "/out/link/" in u.lower())]
        for u in cands:
            checked += 1
            try:
                r = requests.get(u, timeout=timeout, allow_redirects=True,
                                 headers={"User-Agent": ua})
                a = asin_from(r.url)
            except Exception:
                continue
            if a:
                d["asin"] = a
                conn.execute("UPDATE review_products SET asin=?, data=?, updated_at=? "
                             "WHERE review_product_id=?", (a, json.dumps(d), _now(), rpid))
                found += 1
                break
    conn.commit()
    return {"rows_missing": len(rows), "links_followed": checked, "asins_recovered": found}


def add_review_product(conn: sqlite3.Connection, review_id: str, item: dict) -> dict | None:
    """Add one reviewed item to a review; auto-resolve its link + reproject the linked product."""
    ensure_reviews_table(conn)
    if not conn.execute("SELECT 1 FROM reviews WHERE review_id=?", (review_id,)).fetchone():
        return None
    rpid = _insert_review_product(conn, review_id, item)
    conn.commit()
    resolve_links(conn, review_id)   # links the new row + reprojects
    return get_review(conn, review_id)


def upsert_review_product(conn: sqlite3.Connection, review_id: str, item: dict) -> str | None:
    """Idempotent add for INGEST: within a review, match an existing item by ASIN (strong) or
    exact name and UPDATE it, else INSERT. Lets a review page be re-ingested without duplicating
    items. Does NOT resolve/reproject — the caller does that once after the batch."""
    if not conn.execute("SELECT 1 FROM reviews WHERE review_id=?", (review_id,)).fetchone():
        return None
    # Resolve from the buy links too, so a re-ingest matches on the SAME identity the row was
    # stored under — matching on a raw (often empty) field would insert a duplicate instead.
    asin = resolve_asin(item)
    if asin:
        item = {**item, "asin": asin}
    existing = None
    for rpid, data in conn.execute(
            "SELECT review_product_id, data FROM review_products WHERE review_id=?", (review_id,)):
        if asin and (_rp_data(data).get("asin") or "").strip().upper() == asin:
            existing = rpid; break
    if existing is None and item.get("name"):
        row = conn.execute(
            "SELECT review_product_id FROM review_products WHERE review_id=? AND lower(name)=lower(?)",
            (review_id, item["name"])).fetchone()
        existing = row[0] if row else None
    if existing:
        update_review_product(conn, existing, item)   # note: reprojects the linked product too
        return existing
    return _insert_review_product(conn, review_id, item)


def update_review_product(conn: sqlite3.Connection, rpid: str, patch: dict) -> bool:
    """Edit one review_product (verdict tier/summary/price + optional identity fields). Re-resolves
    the link if identity changed; reprojects the affected product(s)."""
    ensure_reviews_table(conn)
    row = conn.execute(
        "SELECT review_id, data, product_id FROM review_products WHERE review_product_id=?",
        (rpid,)).fetchone()
    if not row:
        return False
    review_id, data_json, old_pid = row
    data = _rp_data(data_json)
    sets, params, ident_changed = [], [], False
    for f in ("name", "brand", "tier", "summary"):
        if f in patch:
            sets.append(f"{f}=?"); params.append(patch.get(f) or "")
    if "price_at_test" in patch:
        pr = patch.get("price_at_test")
        try:
            pr = None if pr in (None, "") else float(pr)
        except (TypeError, ValueError):
            pr = None
        sets.append("price_at_test=?"); params.append(pr)
    for f in ("asin", "url", "specs", "retailer_offers", "ratings"):
        if f in patch:
            # Offers are cleaned on the way in, never on the way out — so a link is safe to
            # publish wherever it is read from, not only where someone remembered to strip it.
            data[f] = clean_offers(patch) if f == "retailer_offers" else patch[f]
            if f in ("asin", "url", "retailer_offers", "specs"):
                ident_changed = True
    if "asin" in patch:
        # Keep the first-class column in step with the blob — it's the field the curator
        # edits when a match is wrong, and the one every lookup reads.
        sets.append("asin=?")
        params.append(str(patch.get("asin") or "").strip().upper() or None)
    sets.append("data=?"); params.append(json.dumps(data))
    sets.append("updated_at=?"); params.append(_now())
    params.append(rpid)
    conn.execute(f"UPDATE review_products SET {', '.join(sets)} WHERE review_product_id=?", params)
    conn.commit()
    if ident_changed:
        resolve_links(conn, review_id, force=True)
    if old_pid:
        _reproject_product(conn, old_pid)   # verdict text change flows to the product block
    return True


def delete_review_product(conn: sqlite3.Connection, rpid: str) -> bool:
    """Delete one reviewed item; reproject the product it was linked to."""
    ensure_reviews_table(conn)
    row = conn.execute(
        "SELECT product_id FROM review_products WHERE review_product_id=?", (rpid,)).fetchone()
    if not row:
        return False
    old_pid = row[0]
    conn.execute("DELETE FROM review_products WHERE review_product_id=?", (rpid,))
    conn.commit()
    if old_pid:
        _reproject_product(conn, old_pid)
    return True


def link_review_product(conn: sqlite3.Connection, rpid: str, product_id: str | None) -> bool:
    """Manually (re)link a reviewed item to a catalog product (product_id=None auto-resolves).
    Reprojects both the old and new products."""
    ensure_reviews_table(conn)
    row = conn.execute(
        "SELECT review_id, data, product_id FROM review_products WHERE review_product_id=?",
        (rpid,)).fetchone()
    if not row:
        return False
    review_id, data, old_pid = row
    if product_id is None:
        resolve_links(conn, review_id, force=True)
        return True
    if not conn.execute("SELECT 1 FROM products WHERE product_id=?", (product_id,)).fetchone():
        return False
    conn.execute(
        "UPDATE review_products SET product_id=?, linked_by='manual', updated_at=? "
        "WHERE review_product_id=?", (product_id, _now(), rpid))
    conn.commit()
    for pid in {old_pid, product_id}:
        if pid:
            _reproject_product(conn, pid)
    return True


# ============================================================================
#  reviews (header) CRUD + read
# ============================================================================
def _review_products(conn: sqlite3.Connection, review_id: str) -> list:
    """The reviewed items under a review, each with its dynamic-link status (linked product name)."""
    out = []
    for rpid, name, brand, tier, summary, price, data, pid, linked_by in conn.execute(
            "SELECT review_product_id, name, brand, tier, summary, price_at_test, data, "
            "product_id, linked_by FROM review_products WHERE review_id=?", (review_id,)):
        linked_name = ""
        if pid:
            r = conn.execute("SELECT brand, name FROM products WHERE product_id=?", (pid,)).fetchone()
            if r:
                linked_name = f"{(r[0]+' · ') if r[0] else ''}{r[1] or ''}".strip()
        dd = _rp_data(data)
        out.append({
            "review_product_id": rpid, "name": name or "", "brand": brand or "",
            "tier": tier or "", "summary": summary or "", "price_at_test": price,
            "asin": dd.get("asin", ""), "url": dd.get("url", ""),
            # The item's retailer offers = its "product sources" (where to buy).
            # Surfaced per-item in the reviews editor so each summary links out to
            # its sources; buy-links are the ONLY outbound links (brand-safe).
            "retailer_offers": dd.get("retailer_offers") or [],
            "product_id": pid, "linked_by": linked_by or "", "linked_name": linked_name,
        })
    out.sort(key=lambda p: (catalog_store._tier_rank({"verdicts": [{"tier": p["tier"]}]}), p["name"]))
    return out


def list_reviews(conn: sqlite3.Connection) -> list:
    """Roster of reviews (sidebar), each with its review_products count + linked count."""
    ensure_reviews_table(conn)
    out = []
    for rid, reviewer, cls, cat, title, updated, captured, ws_id, ws_path in conn.execute(
            "SELECT review_id, reviewer, product_class, category, title, last_updated, "
            "captured_at, ws_category_id, ws_path FROM reviews"):
        n = conn.execute("SELECT COUNT(*) FROM review_products WHERE review_id=?", (rid,)).fetchone()[0]
        linked = conn.execute(
            "SELECT COUNT(*) FROM review_products WHERE review_id=? AND product_id IS NOT NULL",
            (rid,)).fetchone()[0]
        out.append({
            "review_id": rid, "reviewer": reviewer or "", "product_class": cls or "",
            "category": cat or "", "title": title or "", "last_updated": updated or "",
            "captured_at": captured or "", "ws_category_id": ws_id, "ws_path": ws_path or "",
            "products": n, "linked": linked,
        })
    out.sort(key=lambda x: (x["category"], x["reviewer"], x["product_class"]))
    return out


def get_review(conn: sqlite3.Connection, review_id: str) -> dict | None:
    """Full review metadata + its review_products (with dynamic-link status)."""
    ensure_reviews_table(conn)
    row = conn.execute(
        "SELECT review_id, reviewer, product_class, category, title, url, last_updated, "
        "captured_at, rating_scale, screenshot_id, notes, ws_category_id, ws_path, "
        "buying_guide, data FROM reviews WHERE review_id=?", (review_id,)).fetchone()
    if not row:
        return None
    cols = ("review_id", "reviewer", "product_class", "category", "title", "url",
            "last_updated", "captured_at", "rating_scale", "screenshot_id", "notes",
            "ws_category_id", "ws_path", "buying_guide", "_data")
    d = dict(zip(cols, row))
    d["buying_guide"] = d.get("buying_guide") or ""
    try:
        d["criteria"] = (json.loads(d.pop("_data") or "{}") or {}).get("criteria") or []
    except Exception:
        d.pop("_data", None); d["criteria"] = []
    d["products"] = _review_products(conn, review_id)
    return d


def create_review(conn: sqlite3.Connection, patch: dict) -> dict:
    """Create a review header (Add). reviewer + product_class are the identity; items attach via
    add_review_product / ingest. Returns the existing review if the natural key already exists."""
    ensure_reviews_table(conn)
    reviewer = (patch.get("reviewer") or "").strip()
    url = (patch.get("url") or "").strip()
    ws_id = patch.get("ws_category_id") or None
    ws_path_in, headline = _resolve_ws(conn, ws_id)
    # product_class is now optional (the WS taxonomy pick is the classification). Accept an explicit
    # one, else derive a label from the picked WS path's leaf/subcategory.
    cls = (patch.get("product_class") or "").strip() or (ws_path_in.split(" > ")[-1] if ws_path_in else "")
    if not reviewer:
        raise ValueError("reviewer is required")
    if not cls and not ws_id:
        raise ValueError("a taxonomy (WS category) or product_class is required")
    existing = _existing_review_id(conn, reviewer, url, cls)
    if existing:
        return get_review(conn, existing)
    rid, now = str(uuid.uuid4()), _now()
    meta = {k: (patch.get(k) or "") for k in _EDITABLE_META}
    category = meta["category"] or headline
    buying_guide = patch.get("buying_guide") or ""
    data = {"reviewer": reviewer, **meta, "url": url}
    if patch.get("criteria"):
        data["criteria"] = patch["criteria"]
    conn.execute(
        "INSERT INTO reviews(review_id, reviewer, product_class, category, title, url, "
        "last_updated, captured_at, rating_scale, screenshot_id, notes, ws_category_id, ws_path, "
        "buying_guide, data, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, reviewer, cls, category, meta["title"], url, meta["last_updated"],
         meta["captured_at"], meta["rating_scale"], meta["screenshot_id"], meta["notes"],
         ws_id, ws_path_in, buying_guide,
         json.dumps(data), now, now))
    conn.commit()
    return get_review(conn, rid)


def update_review(conn: sqlite3.Connection, review_id: str, patch: dict) -> dict | None:
    """Edit a review header's metadata. Verdict edits are per review_product via
    `product_verdicts` = [{review_product_id, tier?, summary?, price_at_test?, ...}]."""
    ensure_reviews_table(conn)
    row = conn.execute("SELECT data FROM reviews WHERE review_id=?", (review_id,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0]) if row[0] else {}
    except Exception:
        data = {}
    sets, params = [], []
    # WS-taxonomy pick (the single type-ahead): resolve id -> path + headline; the headline seeds
    # `category` (for sidebar grouping) unless the curator set category explicitly.
    if "ws_category_id" in patch:
        ws_id = patch.get("ws_category_id") or None
        ws_path_in, headline = _resolve_ws(conn, ws_id)
        sets += ["ws_category_id=?", "ws_path=?"]; params += [ws_id, ws_path_in]
        data["ws_category_id"] = ws_id; data["ws_path"] = ws_path_in
        if "category" not in patch and headline:
            sets.append("category=?"); params.append(headline); data["category"] = headline
    if "product_class" in patch:
        sets.append("product_class=?"); params.append(patch.get("product_class") or "")
        data["product_class"] = patch.get("product_class") or ""
    if "buying_guide" in patch:
        sets.append("buying_guide=?"); params.append(patch.get("buying_guide") or "")
    if "criteria" in patch:
        data["criteria"] = patch.get("criteria") or []
    for f in _EDITABLE_META:
        if f in patch:
            val = patch.get(f) or ""
            sets.append(f"{f}=?"); params.append(val); data[f] = val
    if sets:
        sets.append("data=?"); params.append(json.dumps(data))
        sets.append("updated_at=?"); params.append(_now())
        params.append(review_id)
        conn.execute(f"UPDATE reviews SET {', '.join(sets)} WHERE review_id=?", params)
        conn.commit()
    for item in (patch.get("product_verdicts") or []):
        if isinstance(item, dict) and item.get("review_product_id"):
            update_review_product(conn, item["review_product_id"], item)
    return get_review(conn, review_id)


def delete_review(conn: sqlite3.Connection, review_id: str) -> bool:
    """Delete a review + its review_products + strip the provenance source entry. Reprojects the
    affected catalog products (empties their verdicts if nothing else links). Catalog products are
    NOT deleted (memory/feedback_no_silent_removal)."""
    ensure_reviews_table(conn)
    row = conn.execute(
        "SELECT reviewer, product_class, url FROM reviews WHERE review_id=?", (review_id,)).fetchone()
    if not row:
        return False
    reviewer, cls, url = row
    affected = {r[0] for r in conn.execute(
        "SELECT DISTINCT product_id FROM review_products WHERE review_id=? AND product_id IS NOT NULL",
        (review_id,))}
    conn.execute("DELETE FROM review_products WHERE review_id=?", (review_id,))
    conn.execute("DELETE FROM reviews WHERE review_id=?", (review_id,))
    crow = conn.execute("SELECT data FROM product_classes WHERE name=?", (cls,)).fetchone()
    if crow and crow[0]:
        try:
            cdata = json.loads(crow[0]) or {}
            srcs = cdata.get("sources") or []
            kept = [s for s in srcs if not (isinstance(s, dict)
                    and (s.get("reviewer") or "") == reviewer and (s.get("url") or "") == url)]
            if len(kept) != len(srcs):
                cdata["sources"] = kept
                conn.execute("UPDATE product_classes SET data=?, updated_at=? WHERE name=?",
                             (json.dumps(cdata), _now(), cls))
        except Exception:
            pass
    conn.commit()
    for pid in affected:
        _reproject_product(conn, pid)
    return True


def distinct_reviewers(conn: sqlite3.Connection) -> list:
    """Reviewer names present (datalist source for the Add form)."""
    ensure_reviews_table(conn)
    return sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT reviewer FROM reviews WHERE reviewer <> ''") if r[0]})