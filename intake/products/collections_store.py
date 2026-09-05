"""Product collections — a named Amazon search URL, and the cohort it returns.

A *collection* is the unit of product selection: a NAME plus a saved Amazon search URL that
the curator built by hand in Amazon's own UI until the result set was right. Amazon encodes
every criterion in the URL, so the URL is the query. Unrestricted on purpose — it might be
a product class ("12-inch cast iron skillets") or an occasion ("top 20 holiday gifts"). The
only thing every collection has in common is that **it yields a list of ASINs**.

Two tables:

  product_collections            the saved search (the dish-equivalent — name + url)
  product_collection_candidates  EVERY ASIN a run returned, kept

The second one is the point. We keep the whole cohort — winners flagged, also-rans retained
— not just the survivors, so a curator can see what the URL actually returned rather than
only what came out the far end, and so a rubric can be re-tuned and re-scored against the
same pool without paying to fetch it again.

`wilson_score` and `realrank_score` are deliberately SEPARATE columns. The first is a cheap
screen over an average and a count (`p = (mean-1)/4`, Wilson lower bound); the second is the
real index computed from an actual star histogram. They answer different questions with
different evidence and must never be read as the same number.

Namespaced away from `collection_members`, which is the RECIPE side (publisher/dish/chef
membership) and unrelated.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dicts(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
    """Rows as dicts WITHOUT touching the caller's connection.

    The server's `_db()` deliberately returns a bare connection with no row_factory, and a
    store has no business mutating a shared connection to suit itself — so we set the factory
    on our own cursor. (Getting this wrong is how the first collection run died: `dict(row)`
    on a plain tuple.)
    """
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(sql, args).fetchall()]


EDITABLE = ("url", "ws_category_id", "keep_top_n", "pages", "notes", "display_name")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_collections (
            name            TEXT PRIMARY KEY,   -- identity/join key, immutable
            display_name    TEXT,               -- cosmetic, editable
            url             TEXT NOT NULL,      -- the Amazon search URL = the query
            ws_category_id  INTEGER,            -- WS taxonomy leaf (auto-classified)
            ws_path         TEXT,               -- denormalized for display/sort
            keep_top_n      INTEGER DEFAULT 10, -- how many reach the bake-off
            pages           INTEGER DEFAULT 1,  -- EasyParser pages, 1 credit each
            notes           TEXT,
            last_run_at     TEXT,
            last_job_id     INTEGER,
            last_count      INTEGER,            -- candidates returned by the last run
            created_at      TEXT,
            updated_at      TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_collection_candidates (
            collection      TEXT NOT NULL,
            asin            TEXT NOT NULL,
            run_at          TEXT,
            position        INTEGER,            -- Amazon's own order, kept for comparison
            title           TEXT,
            brand           TEXT,
            price           TEXT,
            image           TEXT,
            link            TEXT,
            rating          REAL,
            ratings_total   INTEGER,
            wilson_score    REAL,               -- stage 1: cheap screen, average+count only
            histogram       TEXT,               -- stage 2: JSON [5..1] counts, when screened
            realrank_score  REAL,               -- stage 2: the real index
            polarization    TEXT,               -- stage 2: shape label
            screened        INTEGER DEFAULT 0,  -- did we spend a histogram fetch on it
            selected        INTEGER DEFAULT 0,  -- survived to the shortlist
            medal           TEXT,               -- gold | silver | bronze (curator-confirmed)
            product_id      TEXT,               -- set once it materializes a catalog row
            excluded        INTEGER DEFAULT 0,  -- curator: junk, and STAYS out (see below)
            PRIMARY KEY (collection, asin)
        )""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(product_collection_candidates)")}
    if "excluded" not in cols:
        # Curator-removed candidate. A bare row delete would only last until the
        # next refresh re-returned the ASIN (the cohort is delete-and-replace),
        # so removal is a persistent per-ASIN EXCLUSION: the row stays, flagged,
        # is skipped by replace/screening/selection, and can be restored.
        conn.execute("ALTER TABLE product_collection_candidates "
                     "ADD COLUMN excluded INTEGER DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pcc_collection "
                 "ON product_collection_candidates(collection, wilson_score DESC)")
    conn.commit()


def wilson_from_mean(mean, n, z: float = 1.96) -> float:
    """Stage-1 screen: a Wilson lower bound on the positive share implied by a star average.

    Without a histogram you cannot compute NPS-from-stars — promoters and detractors don't
    exist yet. Mapping the mean onto a proportion (`p = (mean-1)/4`) and taking the lower
    bound at n gives the property stage 1 actually needs: 4.8★ from 22,754 ratings beats
    4.9★ from 111, which Amazon's own "review-rank" ordering does not.
    """
    try:
        mean = float(mean or 0)
        n = int(n or 0)
    except (TypeError, ValueError):
        return 0.0
    if mean <= 0 or n <= 0:
        return 0.0
    p = (mean - 1.0) / 4.0
    p = min(max(p, 0.0), 1.0)
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return round(100.0 * (centre - margin) / denom, 1)


# --------------------------------------------------------------------------- #
#  Collections
# --------------------------------------------------------------------------- #

def list_collections(conn: sqlite3.Connection) -> list:
    ensure_tables(conn)
    return _dicts(conn,
        "SELECT c.*, "
        " (SELECT COUNT(*) FROM product_collection_candidates x WHERE x.collection = c.name "
        "   AND x.excluded = 0) AS candidate_count, "
        " (SELECT COUNT(*) FROM product_collection_candidates x WHERE x.collection = c.name "
        "   AND x.selected = 1) AS selected_count "
        "FROM product_collections c ORDER BY c.name")


def get_collection(conn: sqlite3.Connection, name: str) -> dict | None:
    ensure_tables(conn)
    rows = _dicts(conn, "SELECT * FROM product_collections WHERE name = ?", (name,))
    return rows[0] if rows else None


def create_collection(conn: sqlite3.Connection, patch: dict) -> dict:
    ensure_tables(conn)
    name = (patch.get("name") or "").strip()
    url = (patch.get("url") or "").strip()
    if not name:
        raise ValueError("name is required")
    if not url:
        raise ValueError("url is required")
    if get_collection(conn, name):
        raise ValueError(f"collection {name!r} already exists")
    now = _now()
    conn.execute(
        "INSERT INTO product_collections(name, display_name, url, ws_category_id, ws_path, "
        "keep_top_n, pages, notes, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (name, (patch.get("display_name") or "").strip(), url,
         patch.get("ws_category_id"), (patch.get("ws_path") or ""),
         int(patch.get("keep_top_n") or 10), int(patch.get("pages") or 1),
         (patch.get("notes") or ""), now, now))
    conn.commit()
    return get_collection(conn, name)


def update_collection(conn: sqlite3.Connection, name: str, patch: dict) -> dict | None:
    """Edit the editable fields. `name` is the immutable join key — candidates hang off it."""
    ensure_tables(conn)
    if not get_collection(conn, name):
        return None
    sets, vals = [], []
    for f in EDITABLE:
        if f in patch:
            sets.append(f"{f} = ?")
            vals.append(patch[f])
    if "ws_path" in patch:
        sets.append("ws_path = ?")
        vals.append(patch["ws_path"])
    if not sets:
        return get_collection(conn, name)
    sets.append("updated_at = ?")
    vals.extend([_now(), name])
    conn.execute(f"UPDATE product_collections SET {', '.join(sets)} WHERE name = ?", vals)
    conn.commit()
    return get_collection(conn, name)


def delete_collection(conn: sqlite3.Connection, name: str) -> bool:
    ensure_tables(conn)
    cur = conn.execute("DELETE FROM product_collections WHERE name = ?", (name,))
    conn.execute("DELETE FROM product_collection_candidates WHERE collection = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
#  Candidates (the cohort)
# --------------------------------------------------------------------------- #

def replace_candidates(conn: sqlite3.Connection, name: str, items: list) -> int:
    """Store a run's cohort. Delete-and-replace per run: the collection's URL defines the
    pool, so a refreshed pool IS the answer — but a candidate's curator-set `medal` and its
    `product_id` are carried over, because those are human decisions and a catalog link,
    not search output."""
    ensure_tables(conn)
    keep = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT asin, medal, product_id FROM product_collection_candidates "
        "WHERE collection = ? AND (medal IS NOT NULL OR product_id IS NOT NULL)",
        (name,)).fetchall()}
    # Excluded rows SURVIVE the wipe and block re-entry: the search will keep
    # returning the same junk ASIN forever; the exclusion has to outlive the run.
    excluded = {r[0] for r in conn.execute(
        "SELECT asin FROM product_collection_candidates "
        "WHERE collection = ? AND excluded = 1", (name,)).fetchall()}
    conn.execute("DELETE FROM product_collection_candidates "
                 "WHERE collection = ? AND excluded = 0", (name,))
    now = _now()
    for it in items:
        asin = (it.get("asin") or "").strip()
        if not asin or asin in excluded:
            continue
        medal, pid = keep.get(asin, (None, None))
        conn.execute(
            "INSERT INTO product_collection_candidates(collection, asin, run_at, position, "
            "title, brand, price, image, link, rating, ratings_total, wilson_score, medal, "
            "product_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, asin, now, it.get("position"), it.get("title", ""), it.get("brand", ""),
             it.get("price", ""), it.get("image", ""), it.get("link", ""),
             it.get("rating"), it.get("ratings_total"),
             wilson_from_mean(it.get("rating"), it.get("ratings_total")), medal, pid))
    kept_n = conn.execute(
        "SELECT COUNT(*) FROM product_collection_candidates "
        "WHERE collection = ? AND excluded = 0", (name,)).fetchone()[0]
    conn.execute("UPDATE product_collections SET last_run_at = ?, last_count = ?, "
                 "updated_at = ? WHERE name = ?", (now, kept_n, now, name))
    conn.commit()
    return kept_n


def set_screen_result(conn: sqlite3.Connection, name: str, asin: str, *,
                      histogram: list | None, realrank_score: float | None,
                      polarization: str = "", selected: bool = True) -> None:
    """Record a stage-2 rescore for one candidate. `screened` marks that we spent a fetch on
    it, which is what distinguishes 'not good enough to screen' from 'screened and weak'."""
    ensure_tables(conn)
    conn.execute(
        "UPDATE product_collection_candidates SET histogram = ?, realrank_score = ?, "
        "polarization = ?, screened = 1, selected = ? WHERE collection = ? AND asin = ?",
        (json.dumps(histogram) if histogram else None, realrank_score, polarization,
         1 if selected else 0, name, asin))
    conn.commit()


def list_candidates(conn: sqlite3.Connection, name: str, *, order: str = "wilson") -> list:
    """The whole cohort — screened winners AND the also-rans we didn't spend on."""
    ensure_tables(conn)
    col = {"wilson": "wilson_score DESC", "realrank": "realrank_score DESC, wilson_score DESC",
           "position": "position ASC", "ratings": "ratings_total DESC",
           "rating": "rating DESC"}.get(order, "wilson_score DESC")
    rows = _dicts(conn,
        f"SELECT * FROM product_collection_candidates WHERE collection = ? "
        f"ORDER BY excluded ASC, selected DESC, {col}", (name,))
    out = []
    for d in rows:
        if d.get("histogram"):
            try:
                d["histogram"] = json.loads(d["histogram"])
            except Exception:
                d["histogram"] = None
        out.append(d)
    return out


def set_excluded(conn: sqlite3.Connection, name: str, asin: str, excluded: bool) -> bool:
    """Curator removal of one candidate — a persistent exclusion, not a row
    delete (the cohort is delete-and-replace; the search would just return
    the ASIN again). Excluding also clears the medal and shortlist flag: an
    excluded row is a junk verdict, and materialize/selection must never see
    it. Restore re-seats the row with its scores; the next refresh
    re-evaluates it like any candidate."""
    ensure_tables(conn)
    if excluded:
        cur = conn.execute(
            "UPDATE product_collection_candidates SET excluded = 1, medal = NULL, "
            "selected = 0 WHERE collection = ? AND asin = ?", (name, asin))
    else:
        cur = conn.execute(
            "UPDATE product_collection_candidates SET excluded = 0 "
            "WHERE collection = ? AND asin = ?", (name, asin))
    conn.commit()
    return cur.rowcount > 0


def set_medal(conn: sqlite3.Connection, name: str, asin: str, medal: str) -> bool:
    """Curator-confirmed gold/silver/bronze. Survives a refresh (see replace_candidates)."""
    ensure_tables(conn)
    medal = (medal or "").strip().lower()
    if medal not in ("gold", "silver", "bronze", ""):
        raise ValueError("medal must be gold, silver, bronze or empty")
    cur = conn.execute(
        "UPDATE product_collection_candidates SET medal = ? "
        "WHERE collection = ? AND asin = ? AND excluded = 0",
        (medal or None, name, asin))
    conn.commit()
    return cur.rowcount > 0


def materialize_medals(conn: sqlite3.Connection, name: str) -> dict:
    """Medaled candidates -> catalog product rows. The missing half of the
    Amazon-search path (built 2026-08-28): the run screens and the curator
    medals, but until this, nothing ever became a `products` row — even Best
    Dutch Ovens' golds sat with product_id NULL while the catalog's Dutch
    Ovens came from the curated-reviews sibling.

    Mirrors curate/to_products.materialize semantics: find-or-create by ASIN
    then brand+title (no duplicates), placement recorded via set_curation
    (approval-gated like every placement), candidate stamped with the
    product_id. The medal is a CURATOR decision, so mapping it onto bcc_pick
    (gold->Best Overall, silver->Best Value) honors the "bcc_pick is a human
    field" rule — but only fills a BLANK pick, never overwrites one.
    Idempotent: re-running updates offers/curation on the same rows.
    """
    from intake.products import catalog_store
    ensure_tables(conn)
    coll = get_collection(conn, name)
    if coll is None:
        raise ValueError(f"collection {name!r} not found")
    klass = (coll.get("display_name") or coll["name"]).strip()
    cands = _dicts(conn,
        "SELECT * FROM product_collection_candidates WHERE collection = ? "
        "AND medal IS NOT NULL AND medal != ''", (name,))
    MEDAL_PICK = {"gold": "Best Overall", "silver": "Best Value"}
    out = []
    for cand in cands:
        title = (cand.get("title") or "").strip()
        if not title:
            out.append({"asin": cand.get("asin"), "skipped": "no title"})
            continue
        asin = (cand.get("asin") or "").strip().upper()
        # Brand: candidate's own, else guessed from the title — a blank brand
        # made the McCormick Culinary paprika invisible under its brand in the
        # products list (2026-09-05). Search providers often omit brand.
        from intake.products.catalog_store import _guess_brand
        brand = (cand.get("brand") or "").strip() or _guess_brand(title)
        # Product NAME: the listing title CLEANED, not the raw 40-word Amazon
        # blob ("…, 18 oz - One 18 Ounce Container of…, Perfect with Chicken…")
        # — that blob is why the curator didn't recognize their own pick.
        title = re.split(r"\s+-\s+|\s*\|\s*", title)[0].strip()
        title = re.sub(r",\s*\d[\d./]*\s*(?:oz|ounces?|lbs?|pounds?|count|pack|ct|g|kg|ml)\b.*$",
                       "", title, flags=re.IGNORECASE).strip().rstrip(",")
        # Trailing parenthetical size groups, nesting included: "(8.8 ounce (250g))".
        while title.endswith(")"):
            depth, i = 0, len(title) - 1
            while i >= 0:
                depth += 1 if title[i] == ")" else (-1 if title[i] == "(" else 0)
                if depth == 0:
                    break
                i -= 1
            inner = title[i + 1:-1] if i >= 0 else ""
            if i >= 0 and re.search(r"\d", inner) and re.search(
                    r"oz|ounce|g\b|kg|lb|pound|ml|count|pack", inner, re.IGNORECASE):
                title = title[:i].strip().rstrip(",")
            else:
                break
        price = None
        try:
            price = float(str(cand.get("price") or "").replace("$", "").replace(",", "")) or None
        except ValueError:
            pass
        existing = (catalog_store.find_by_asin(conn, asin)
                    or catalog_store.find_by_name(conn, brand, title))
        product = {
            "product_class": klass,
            "brand": brand,
            "name": title,
            "image_url": cand.get("image") or "",
            "retailer_offers": [{
                "retailer": "Amazon", "asin": asin,
                "source_url": cand.get("link") or "", "price": price,
            }],
        }
        res = catalog_store.save_product(conn, product, merge_into=existing)
        pid, action = res["product_id"], res["action"]
        medal = cand["medal"]
        pick = MEDAL_PICK.get(medal, "")
        if pick:
            row = catalog_store.get_product(conn, pid)
            if row and not (row.get("bcc_pick") or "").strip():
                catalog_store.update_product(conn, pid, {"bcc_pick": pick})
        catalog_store.set_curation(conn, pid, {
            "collection": name,
            "placements": [{
                "collection": name, "label": medal.capitalize(),
                "basis": "amazon-owner-screen",
                "wilson_score": cand.get("wilson_score"),
                "realrank_score": cand.get("realrank_score"),
                "rating": cand.get("rating"),
                "ratings_total": cand.get("ratings_total"),
            }],
        })
        conn.execute(
            "UPDATE product_collection_candidates SET product_id = ? "
            "WHERE collection = ? AND asin = ?", (pid, name, cand["asin"]))
        conn.commit()
        out.append({"asin": asin, "medal": medal, "product_id": pid, "action": action})
    return {"collection": name, "materialized": out,
            "created": sum(1 for r in out if r.get("action") == "created"),
            "merged": sum(1 for r in out if r.get("action") == "merged")}
