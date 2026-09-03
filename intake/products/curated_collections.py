"""Curated collections — a product class, ranked from the expert reviews.

The SECOND way products get selected, built beside the first rather than replacing it:

    collections_store       a saved Amazon SEARCH URL -> the cohort it returns -> screened on
                            owner ratings (Wilson, then real histograms). Bottom-up from the
                            marketplace: what is selling, filtered by what owners think.

    curated_collections     a product CLASS NAME -> the reviews the named authorities
    (this module)           published about it -> ranked picks with their reasoning. Top-down
                            from authority: what the testers concluded, verified against our
                            own data.

Both end in the same place — product records — which is the point of having two. They answer
different questions and disagree usefully: expert consensus ranks, owner arithmetic rides
alongside, and neither is reconciled into the other (the same split as `rank_score` vs
`realrank.score` on the product row).

Two tables, mirroring the pair next door:

    curated_collections        the saved class (name + the product class + the categories)
    curated_collection_picks   every PLACEMENT a run produced, with its evidence

The second keeps the reasoning, not just the winners: `why_it_ranks_here` justifies a pick in
isolation, `edge_over_next` says why it beat the one below it, and `important_tradeoff` is the
required catch. Ranking here is model judgment against stated weights rather than arithmetic,
so that prose IS the audit trail — a stored rank without it could only be accepted, never
challenged.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dicts(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
    """Rows as dicts without mutating the caller's connection (the server's `_db()` hands out
    a bare connection with no row_factory, and a store has no business changing it)."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(sql, args).fetchall()]


EDITABLE = ("display_name", "product_class", "categories", "ws_category_id", "ws_path",
            "notes", "use_network", "search_terms", "editors_choice", "class_criteria")

# Columns holding JSON, decoded on read so callers never json.loads by hand.
_JSON_PICK_FIELDS = ("source_links", "offers", "owner_histogram")


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS curated_collections (
            name            TEXT PRIMARY KEY,   -- identity/join key, immutable
            display_name    TEXT,               -- cosmetic, editable
            product_class   TEXT NOT NULL,      -- "Loaf pans" — what gets researched
            categories      TEXT,               -- JSON list; [] = rank the whole class
            ws_category_id  INTEGER,            -- WS taxonomy leaf (auto-classified)
            ws_path         TEXT,               -- denormalized for display/sort
            notes           TEXT,
            use_network     INTEGER DEFAULT 1,  -- enrichment may call out (identity/ratings)
            search_terms    TEXT,               -- JSON list: FALLBACK terms tried after the
                                                -- class name when a publisher's page flunks
                                                -- the on-class filter ("Multicooker" for
                                                -- Electric Pressure Cooker); also widen the
                                                -- filter's accept set
            editors_choice  TEXT,               -- curator-pinned product (name/ASIN, free
                                                -- text): analyzed by the run with full rigor
                                                -- in its own labeled slot — NEVER mixed into
                                                -- the review-sourced ranking (provenance
                                                -- firewall)
            last_run_at     TEXT,
            last_job_id     INTEGER,
            last_pick_count INTEGER,
            last_error      TEXT,
            result_json     TEXT,               -- the validated record, as researched
            brief_text      TEXT,               -- the rendered brief
            report_json     TEXT,               -- verification report (verified/filled/…)
            approved_by     TEXT,
            approved_at     TEXT,
            created_at      TEXT,
            updated_at      TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS curated_collection_picks (
            collection      TEXT NOT NULL,
            slot            TEXT NOT NULL,      -- "overall.1" | "best-value.2" — stable key
            section         TEXT,               -- '' = overall, else the category name
            place           INTEGER,
            product_title   TEXT,
            manufacturer    TEXT,
            capacity        TEXT,
            model_number    TEXT,               -- manufacturer's part no — the disambiguator
            image           TEXT,               -- listing photo (from verify's identity call)
            typical_price   REAL,               -- the RANKING input
            current_price   REAL,               -- perishable
            price_type      TEXT,
            best_for        TEXT,
            why_it_ranks_here   TEXT,
            edge_over_next      TEXT,           -- why it beat the one below — the audit trail
            important_tradeoff  TEXT,
            buy_link        TEXT,
            amazon_link     TEXT,
            asin            TEXT,
            asin_source     TEXT,               -- how a blank ASIN was recovered
            verified_title  TEXT,               -- the listing we matched, as Amazon states it
            identity_warning TEXT,              -- set when the listing looks like another product
            source_links    TEXT,               -- JSON: what was read for THIS row
            offers          TEXT,               -- JSON: retailers, tracking stripped
            owner_rating    REAL,
            owner_count     INTEGER,
            owner_histogram TEXT,               -- JSON [5..1] counts
            realrank_score  REAL,
            rating_shape    TEXT,
            product_id      TEXT,               -- the catalog row it materialized
            product_action  TEXT,               -- created | merged | skipped
            run_at          TEXT,
            excluded        INTEGER DEFAULT 0,  -- curator: rejected pick, stays out (see set_pick_excluded)
            PRIMARY KEY (collection, slot)
        )""")
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(curated_collections)")}
    if "search_terms" not in ccols:
        conn.execute("ALTER TABLE curated_collections ADD COLUMN search_terms TEXT")
    if "editors_choice" not in ccols:
        conn.execute("ALTER TABLE curated_collections ADD COLUMN editors_choice TEXT")
    if "class_criteria" not in ccols:
        # Water-Bath-Canner lesson (2026-09-03): the class NAME alone let an
        # electric multi-cooker rank #2 and an accessory rack rank #3. Curator-
        # written boundary prose — what this class IS and IS NOT ("stovetop pot
        # with jar rack; reject electric/digital canners, pressure canners, bare
        # racks/baskets") — fed verbatim into the research prompt as binding.
        conn.execute("ALTER TABLE curated_collections ADD COLUMN class_criteria TEXT")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(curated_collection_picks)")}
    if "excluded" not in cols:
        conn.execute("ALTER TABLE curated_collection_picks "
                     "ADD COLUMN excluded INTEGER DEFAULT 0")
    if "model_number" not in cols:
        # KitchenAid-7-speed lesson (2026-08-31): within a brand's sibling
        # line only the manufacturer's model number names ONE product.
        conn.execute("ALTER TABLE curated_collection_picks ADD COLUMN model_number TEXT")
    if "image" not in cols:
        conn.execute("ALTER TABLE curated_collection_picks ADD COLUMN image TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ccp_collection "
                 "ON curated_collection_picks(collection, section, place)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ccp_asin "
                 "ON curated_collection_picks(asin)")
    conn.commit()


# --------------------------------------------------------------------------- #
#  Collections
# --------------------------------------------------------------------------- #

def _decode(c: dict) -> dict:
    for f, default in (("categories", []), ("search_terms", []),
                       ("result_json", None), ("report_json", None)):
        raw = c.get(f)
        if raw:
            try:
                c[f] = json.loads(raw)
            except Exception:
                c[f] = default
        else:
            c[f] = default
    return c


def list_collections(conn: sqlite3.Connection) -> list:
    ensure_tables(conn)
    rows = _dicts(conn,
        "SELECT c.name, c.display_name, c.product_class, c.categories, c.ws_path, "
        "       c.last_run_at, c.last_job_id, c.last_pick_count, c.last_error, "
        "       c.approved_by, c.approved_at, c.updated_at, "
        " (SELECT COUNT(*) FROM curated_collection_picks p WHERE p.collection = c.name) "
        "   AS pick_count, "
        " (SELECT COUNT(*) FROM curated_collection_picks p WHERE p.collection = c.name "
        "   AND COALESCE(p.product_id,'') <> '') AS product_count "
        "FROM curated_collections c ORDER BY c.name")
    return [_decode(r) for r in rows]


def get_collection(conn: sqlite3.Connection, name: str) -> dict | None:
    ensure_tables(conn)
    rows = _dicts(conn, "SELECT * FROM curated_collections WHERE name = ?", (name,))
    return _decode(rows[0]) if rows else None


def create_collection(conn: sqlite3.Connection, patch: dict) -> dict:
    """`name` is the immutable join key; `product_class` is what actually gets researched.

    They default to each other so the common case is one field: typing "Loaf pans" gives the
    class verbatim and a slug to hang picks off.
    """
    ensure_tables(conn)
    pclass = (patch.get("product_class") or patch.get("name") or "").strip()
    name = (patch.get("name") or "").strip() or pclass
    if not name:
        raise ValueError("name (or product_class) is required")
    if not pclass:
        raise ValueError("product_class is required")
    if get_collection(conn, name):
        raise ValueError(f"curated collection {name!r} already exists")
    now = _now()
    conn.execute(
        "INSERT INTO curated_collections(name, display_name, product_class, categories, "
        "ws_category_id, ws_path, notes, use_network, search_terms, editors_choice, "
        "class_criteria, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, (patch.get("display_name") or "").strip(), pclass,
         json.dumps(_clean_categories(patch.get("categories"))),
         patch.get("ws_category_id"), (patch.get("ws_path") or ""),
         (patch.get("notes") or ""), 1 if patch.get("use_network", True) else 0,
         json.dumps(_clean_search_terms(patch.get("search_terms"))),
         (patch.get("editors_choice") or "").strip(),
         (patch.get("class_criteria") or "").strip(), now, now))
    conn.commit()
    return get_collection(conn, name)


def _clean_categories(value) -> list:
    """Staff input, normalized through the ONE gate the prompt uses, so what is stored is
    exactly what will be asked for. Empty is a real answer: rank the whole class."""
    from intake.products.curate.prompt import normalize_categories
    return normalize_categories(value)


def _clean_search_terms(value) -> list:
    """Fallback search terms — a list or a `;`/`,`/newline-delimited string. The class name
    is NOT stored here: it is always the ladder's first rung at run time, so the stored list
    is only the aliases tried after it ("Multicooker", "pressure canning"). Empty is the
    common case. Deduped case-insensitively, order preserved (order = ladder order)."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    parts = [p.strip() for v in value for p in re.split(r"[;,\n]", str(v))]
    out, seen = [], set()
    for p in parts:
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def update_collection(conn: sqlite3.Connection, name: str, patch: dict) -> dict | None:
    ensure_tables(conn)
    if not get_collection(conn, name):
        return None
    sets, vals = [], []
    for f in EDITABLE:
        if f not in patch:
            continue
        v = patch[f]
        if f == "categories":
            v = json.dumps(_clean_categories(v))
        elif f == "search_terms":
            v = json.dumps(_clean_search_terms(v))
        elif f in ("editors_choice", "class_criteria"):
            v = (v or "").strip()
        elif f == "use_network":
            v = 1 if v else 0
        elif f == "product_class":
            v = (v or "").strip()
            if not v:
                raise ValueError("product_class cannot be blank — it is what gets researched")
        sets.append(f"{f} = ?")
        vals.append(v)
    if not sets:
        return get_collection(conn, name)
    sets.append("updated_at = ?")
    vals.extend([_now(), name])
    conn.execute(f"UPDATE curated_collections SET {', '.join(sets)} WHERE name = ?", vals)
    conn.commit()
    return get_collection(conn, name)


def delete_collection(conn: sqlite3.Connection, name: str) -> bool:
    """Removes the collection and its picks. Catalog products created by a run are NOT
    deleted — they are their own records by then, referenced elsewhere, and a selection run
    is not their owner."""
    ensure_tables(conn)
    cur = conn.execute("DELETE FROM curated_collections WHERE name = ?", (name,))
    conn.execute("DELETE FROM curated_collection_picks WHERE collection = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def set_run_result(conn: sqlite3.Connection, name: str, *, record: dict | None,
                   report: dict | None, brief_text: str, job_id: int | None,
                   pick_count: int) -> None:
    """Store what a completed run produced. A re-run RESETS approval — new evidence must not
    inherit a human's sign-off on the old."""
    ensure_tables(conn)
    conn.execute(
        "UPDATE curated_collections SET result_json=?, report_json=?, brief_text=?, "
        "last_run_at=?, last_job_id=?, last_pick_count=?, last_error=NULL, "
        "approved_by='', approved_at='', updated_at=? WHERE name=?",
        (json.dumps(record) if record is not None else None,
         json.dumps(report) if report is not None else None,
         brief_text or "", _now(), job_id, pick_count, _now(), name))
    conn.commit()


def set_run_error(conn: sqlite3.Connection, name: str, message: str,
                  job_id: int | None = None) -> None:
    """A failed run leaves a RECORD, not a shrug. The previous result stays put: a failure
    says nothing about the picks that were already verified."""
    ensure_tables(conn)
    conn.execute("UPDATE curated_collections SET last_error=?, last_run_at=?, last_job_id=?, "
                 "updated_at=? WHERE name=?",
                 (str(message)[:2000], _now(), job_id, _now(), name))
    conn.commit()


def approve(conn: sqlite3.Connection, name: str, who: str) -> dict | None:
    """Staff sign-off on the brief. The gate between an automated ranking and anything that
    earns affiliate revenue off it."""
    ensure_tables(conn)
    if not get_collection(conn, name):
        return None
    conn.execute("UPDATE curated_collections SET approved_by=?, approved_at=?, updated_at=? "
                 "WHERE name=?", (who or "staff", _now(), _now(), name))
    conn.commit()
    return get_collection(conn, name)


# --------------------------------------------------------------------------- #
#  Picks
# --------------------------------------------------------------------------- #

def replace_picks(conn: sqlite3.Connection, name: str, picks: list) -> int:
    """Store a run's placements. Delete-and-replace: a run re-ranks the whole class, so the
    previous placements are superseded rather than merged.

    `product_id` is carried over by ASIN, then by brand+title, then by SLOT — the catalog link
    is a fact we recorded, not an output of this run, and losing it orphans the product the
    last run created and then materializes a duplicate of it.

    Slot is the WEAKEST of the three and comes last on purpose: a product that moves from
    "Best value #2" to "#1" between runs is the same product, while whatever now occupies its
    old slot is a different one.
    """
    ensure_tables(conn)
    prior = _dicts(conn,
        "SELECT slot, asin, manufacturer, product_title, product_id "
        "FROM curated_collection_picks "
        "WHERE collection = ? AND COALESCE(product_id,'') <> ''", (name,))
    prior_slot = {r["slot"]: r["product_id"] for r in prior}
    prior_asin = {(r["asin"] or "").upper(): r["product_id"] for r in prior if r["asin"]}
    prior_name = {_name_key(r["manufacturer"], r["product_title"]): r["product_id"]
                  for r in prior}
    # Curator-excluded picks SURVIVE the wipe and BAN their product from
    # re-entry (by ASIN, then brand+title): the authorities keep recommending
    # it, so a rejection has to outlive the run that placed it.
    excl = _dicts(conn,
        "SELECT slot, asin, manufacturer, product_title FROM curated_collection_picks "
        "WHERE collection = ? AND excluded = 1", (name,))
    ban_asin = {(r["asin"] or "").upper() for r in excl if r["asin"]}
    ban_name = {_name_key(r["manufacturer"], r["product_title"]) for r in excl}
    excl_slots = {r["slot"] for r in excl}
    conn.execute("DELETE FROM curated_collection_picks "
                 "WHERE collection = ? AND excluded = 0", (name,))
    now = _now()
    banned = 0
    for p in picks:
        slot = (p.get("_slot") or "").strip()
        if not slot:
            continue
        asin = (p.get("amazon_asin") or "").strip().upper()
        if (asin and asin in ban_asin) \
                or _name_key(p.get("manufacturer"), p.get("product_title")) in ban_name:
            banned += 1
            continue
        if slot in excl_slots:      # a surviving excluded row holds this slot — re-key it
            new_slot = f"x:{slot}"
            while new_slot in excl_slots:
                new_slot += "'"
            conn.execute("UPDATE curated_collection_picks SET slot = ? "
                         "WHERE collection = ? AND slot = ?", (new_slot, name, slot))
            excl_slots.discard(slot)
            excl_slots.add(new_slot)
        pid = (prior_asin.get(asin) if asin else None) \
            or prior_name.get(_name_key(p.get("manufacturer"), p.get("product_title"))) \
            or prior_slot.get(slot)
        conn.execute(
            "INSERT INTO curated_collection_picks(collection, slot, section, place, "
            "product_title, manufacturer, capacity, model_number, image, "
            "typical_price, current_price, price_type, "
            "best_for, why_it_ranks_here, edge_over_next, important_tradeoff, buy_link, "
            "amazon_link, asin, asin_source, verified_title, identity_warning, source_links, "
            "offers, owner_rating, owner_count, owner_histogram, realrank_score, rating_shape, "
            "product_id, run_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, slot, p.get("_section", ""), p.get("place"),
             p.get("product_title", ""), p.get("manufacturer", ""), p.get("capacity", ""),
             (p.get("model_number") or "").strip(), p.get("image", ""),
             _num(p.get("typical_price")), _num(p.get("current_price")),
             p.get("price_type", ""), p.get("best_for", ""), p.get("why_it_ranks_here", ""),
             p.get("edge_over_next", ""), p.get("important_tradeoff", ""),
             p.get("buy_link", ""), p.get("amazon_link", ""), asin, p.get("asin_source", ""),
             p.get("verified_title", ""), p.get("identity_warning", ""),
             json.dumps(p.get("source_links") or []), json.dumps(p.get("offers") or []),
             p.get("owner_rating"), p.get("owner_count"),
             json.dumps(p.get("owner_histogram") or []), p.get("realrank_score"),
             p.get("rating_shape", ""), pid, now))
    if banned:
        print(f"[CURATE] {banned} pick(s) skipped — match a curator-excluded product")
    kept = len(picks) - banned
    conn.execute("UPDATE curated_collections SET last_pick_count = ?, updated_at = ? "
                 "WHERE name = ?", (kept, now, name))
    conn.commit()
    return kept


def _name_key(manufacturer, title) -> str:
    """Identity for a pick with no ASIN. Exact on brand+title, whitespace- and case-folded —
    the same strength as catalog_store.find_by_name, deliberately not fuzzy."""
    return (" ".join((manufacturer or "").split()).lower() + "|"
            + " ".join((title or "").split()).lower())


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def list_picks(conn: sqlite3.Connection, name: str) -> list:
    """Every placement, overall first then categories — publication order."""
    ensure_tables(conn)
    rows = _dicts(conn,
        "SELECT * FROM curated_collection_picks WHERE collection = ? "
        "ORDER BY excluded ASC, "
        "CASE WHEN COALESCE(section,'') = '' THEN 0 ELSE 1 END, section, place",
        (name,))
    for d in rows:
        for f in _JSON_PICK_FIELDS:
            if d.get(f):
                try:
                    d[f] = json.loads(d[f])
                except Exception:
                    d[f] = []
            else:
                d[f] = []
    return rows


def set_pick_excluded(conn: sqlite3.Connection, name: str, slot: str,
                      excluded: bool) -> bool:
    """Curator rejection of ONE pick — a persistent per-product ban, not a row
    delete. The row stays flagged (its research/reasoning kept for the record);
    replace_picks skips any future pick matching its ASIN or brand+title, and
    the run's materialize step never sees it. Restore lifts the ban — it does
    NOT re-rank the list; the next run decides placements. Excluding does not
    touch a catalog row the pick already materialized."""
    ensure_tables(conn)
    cur = conn.execute(
        "UPDATE curated_collection_picks SET excluded = ? "
        "WHERE collection = ? AND slot = ?", (1 if excluded else 0, name, slot))
    conn.commit()
    return cur.rowcount > 0


def set_pick_product(conn: sqlite3.Connection, name: str, slot: str, product_id: str,
                     action: str = "") -> None:
    """Link a placement to the catalog row it materialized."""
    ensure_tables(conn)
    conn.execute("UPDATE curated_collection_picks SET product_id = ?, product_action = ? "
                 "WHERE collection = ? AND slot = ?", (product_id, action, name, slot))
    conn.commit()
