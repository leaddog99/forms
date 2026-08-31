"""Affiliate entities — stores, networks, connections, programs — and link building.

The table behind the revenue path. `buy_links.affiliate_url()` was hard-wired to Amazon:
every other retailer got the clean destination and earned nothing. Measured 2026-07-28,
that was **35 of 58 held offers (60%)**.

FIVE-ENTITY MODEL (2026-08-31 restructure, research round in design note §9 —
curator caught the flat table conflating store with relationship):

    affiliate_stores        the MERCHANT as displayed (hosts, platform, contact) —
                            exists whether or not monetized; prospects live here
    affiliate_store_hosts   host -> store, so an outbound URL resolves in one lookup
    affiliate_networks      the rails (impact/awin/cj/...) + their subid MECHANICS
                            (param name, max length) — link building reads metadata
    affiliate_connections   OUR account on a network (publisher id, region, creds ref)
    affiliate_programs      the JOIN: store x connection — status ladder, priority,
                            link template, rate. /go/ resolves click host -> store ->
                            best ACTIVE program; the applicable rate is STAMPED ON THE
                            CLICK (clickstream) so history survives rate edits.

Design note: docs/affiliate-programs-and-clicks.md (§9 = the researched model).

THE RULE THIS MODULE EXISTS TO ENFORCE: **never emit a half-built affiliate link.** A
clean URL that works beats a tagged one that is malformed — the malformed one loses the
sale AND the attribution, and looks broken to the reader. So every path that cannot
produce a complete, credentialled link falls back to the clean destination:

    * status is not 'active'          — publishing an unapproved id breaches most terms
    * a placeholder has no value      — no publisher_id means no attribution anyway
    * the template cannot carry dest  — a deep link with nowhere to put the destination
    * supports_deeplink = 0           — homepage-only program, and sending a reader to a
                                        homepage instead of the product is worse than
                                        sending them to the product unattributed

Commission data lives here and MUST NOT reach the ranking stage (design note §7.2). This
module is imported by the buy-link layer, never by curate/pipeline.
"""
from __future__ import annotations

import json
import os
import sqlite3
from input.pipeline.db import connect as db_connect
import threading
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
DB_PATH = os.path.join(_ROOT, "recipes.db")

STATUSES = ("prospect", "applied", "approved", "active", "paused", "rejected", "closed")
STRATEGIES = ("template", "amazon_tag", "none")

# Placeholders that must be filled for a link to be worth emitting. Everything else a
# template can reference (dest, subtag, click_id) is either always present or optional.
_CREDENTIALS = ("publisher_id", "tracking_id", "campaign_id", "account_ref")

EDITABLE = ("display_name", "merchant", "network", "status", "store", "connection",
            "publisher_id", "tracking_id",
            "campaign_id", "account_ref", "login_url", "dashboard_url", "contact",
            "link_strategy", "link_template", "subtag_param", "supports_deeplink",
            "default_rate", "commission_note", "cookie_days", "priority", "notes")

STORE_EDITABLE = ("display_name", "merchant", "platform", "contact", "notes")
CONNECTION_EDITABLE = ("network", "publisher_id", "tracking_id", "account_ref", "region",
                       "login_url", "dashboard_url", "contact", "status", "notes")

# Network rows seed the subid MECHANICS the research measured (design note §9):
# param name + safe max length. Link building reads these; nothing hardcodes them.
NETWORK_SEED = [
    # (name, display, subid_param, subid_max, notes)
    ("amazon", "Amazon Associates", "ascsubtag", 100,
     "tag= is the credential (per-site tracking ids under one account); ascsubtag "
     "reporting is restricted/invite-only — own click ledger is the durable half. "
     "PA-API gated: 3 sales/180d, revoked after 30 idle days."),
    ("impact", "Impact", "subId1", 255,
     "subId1-3 private to us; SharedId visible to the brand — nothing strategic in subids."),
    ("shareasale", "ShareASale", "afftrack", 255, "Awin-owned."),
    ("awin", "Awin", "clickref", 50, "clickref..clickref6; 50 chars."),
    ("cj", "CJ Affiliate", "sid", 64, ""),
    ("rakuten", "Rakuten Advertising", "u1", 72, ""),
    ("partnerize", "Partnerize", "pubref", 100, ""),
    ("skimlinks", "Skimlinks", "xcust", 50, "Aggregator — one deal, haircut rates."),
    ("sovrn", "Sovrn Commerce", "cuid", 50, "Aggregator."),
    ("direct", "Direct (merchant's own program)", "", 0,
     "Refersion/UpPromote/GoAffPro etc — per-store apps; param varies per store."),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dicts(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
    """Rows as dicts without mutating the caller's connection (the server's `_db()` hands
    out a bare connection with no row_factory, and a store must not change it)."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(sql, args).fetchall()]


# --------------------------------------------------------------------------- #
#  Schema
# --------------------------------------------------------------------------- #

def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS affiliate_programs (
            name            TEXT PRIMARY KEY,   -- immutable join key: 'walmart'
            display_name    TEXT,
            merchant        TEXT NOT NULL,      -- 'Walmart' — as a shopper knows it
            network         TEXT,               -- impact | cj | rakuten | shareasale | amazon | direct
            status          TEXT DEFAULT 'prospect',
            publisher_id    TEXT,               -- our affiliate/partner id at the network
            tracking_id     TEXT,               -- per-property channel (Amazon: tracking id)
            campaign_id     TEXT,               -- the network's program/campaign id
            account_ref     TEXT,               -- account/store id, reference only
            login_url       TEXT,
            dashboard_url   TEXT,
            contact         TEXT,
            link_strategy   TEXT DEFAULT 'template',
            link_template   TEXT,
            subtag_param    TEXT,               -- which param carries our click id
            supports_deeplink INTEGER DEFAULT 1,
            default_rate    REAL,
            commission_note TEXT,
            cookie_days     INTEGER,
            priority        INTEGER DEFAULT 100,
            notes           TEXT,
            applied_at      TEXT,
            approved_at     TEXT,
            created_at      TEXT,
            updated_at      TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS affiliate_stores (
            name         TEXT PRIMARY KEY,      -- immutable slug: 'made-in'
            display_name TEXT,
            merchant     TEXT NOT NULL,         -- 'Made In Cookware' — as a shopper knows it
            platform     TEXT,                  -- shopify | marketplace | '' unknown
            contact      TEXT,
            notes        TEXT,
            created_at   TEXT,
            updated_at   TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS affiliate_store_hosts (
            host       TEXT PRIMARY KEY,        -- 'walmart.com', matched as host or *.host
            store      TEXT NOT NULL,
            kind       TEXT DEFAULT 'merchant', -- merchant | network_redirector
            created_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ash_store "
                 "ON affiliate_store_hosts(store)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS affiliate_networks (
            name         TEXT PRIMARY KEY,      -- 'impact'
            display_name TEXT,
            subid_param  TEXT,                  -- the click-id parameter this network takes
            subid_max    INTEGER,               -- safe length for it
            notes        TEXT,
            created_at   TEXT,
            updated_at   TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS affiliate_connections (
            name         TEXT PRIMARY KEY,      -- 'amazon-associates', 'impact-main'
            network      TEXT NOT NULL,
            publisher_id TEXT,
            tracking_id  TEXT,                  -- Amazon: the tag
            account_ref  TEXT,
            region       TEXT,
            login_url    TEXT,
            dashboard_url TEXT,
            contact      TEXT,
            status       TEXT DEFAULT 'active',
            notes        TEXT,
            created_at   TEXT,
            updated_at   TEXT
        )""")
    now = _now()
    have_nets = {r[0] for r in conn.execute("SELECT name FROM affiliate_networks")}
    for name, disp, param, mx, notes in NETWORK_SEED:
        if name not in have_nets:
            conn.execute("INSERT INTO affiliate_networks(name, display_name, subid_param, "
                         "subid_max, notes, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                         (name, disp, param, mx, notes, now, now))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(affiliate_programs)")}
    if "store" not in cols:
        conn.execute("ALTER TABLE affiliate_programs ADD COLUMN store TEXT")
        conn.execute("ALTER TABLE affiliate_programs ADD COLUMN connection TEXT")
        conn.commit()
        _migrate_to_stores(conn)
    conn.commit()
    _seed_amazon(conn)


def _migrate_to_stores(conn: sqlite3.Connection) -> None:
    """One-time: flat programs -> stores + connections + re-keyed hosts.

    - Every program row gets a STORE row (slug from name, merchant carried).
    - Credentialed rows (amazon) also get a CONNECTION carrying the account.
    - Prospect rows with no credentials and no network (the 13 Shopify seeds
      of 2026-08-31) were stores mislabeled as programs: their store row is
      created and the PROGRAM row deleted — a prospect is a store without a
      program, by definition now.
    - affiliate_program_hosts rows move to affiliate_store_hosts.
    Platform comes from retailer_hosts (the Shopify probe cache) when known.
    """
    now = _now()
    shopify_hosts = set()
    try:
        shopify_hosts = {r[0] for r in conn.execute(
            "SELECT host FROM retailer_hosts WHERE is_shopify = 1")}
    except sqlite3.OperationalError:
        pass
    progs = _dicts(conn, "SELECT * FROM affiliate_programs")
    hosts = []
    try:
        hosts = _dicts(conn, "SELECT * FROM affiliate_program_hosts")
    except sqlite3.OperationalError:
        pass
    hosts_by_prog: dict = {}
    for h in hosts:
        hosts_by_prog.setdefault(h["program"], []).append(h)

    for p in progs:
        slug = p["name"]
        my_hosts = [h["host"] for h in hosts_by_prog.get(slug, [])]
        platform = ("marketplace" if slug == "amazon" else
                    "shopify" if any(h in shopify_hosts for h in my_hosts) else "")
        conn.execute(
            "INSERT OR IGNORE INTO affiliate_stores(name, display_name, merchant, platform, "
            "contact, notes, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (slug, p.get("display_name") or "", p.get("merchant") or slug, platform,
             p.get("contact") or "", "", p.get("created_at") or now, now))
        for h in hosts_by_prog.get(slug, []):
            conn.execute("INSERT OR REPLACE INTO affiliate_store_hosts(host, store, kind, "
                         "created_at) VALUES(?,?,?,?)",
                         (h["host"], slug, h.get("kind") or "merchant",
                          h.get("created_at") or now))
        has_creds = any((p.get(k) or "").strip() for k in
                        ("publisher_id", "tracking_id", "campaign_id", "account_ref"))
        if has_creds or (p.get("status") or "") == "active":
            cname = f"{p.get('network') or 'direct'}-{slug}" if slug != "amazon" \
                else "amazon-associates"
            conn.execute(
                "INSERT OR IGNORE INTO affiliate_connections(name, network, publisher_id, "
                "tracking_id, account_ref, login_url, dashboard_url, contact, status, "
                "notes, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (cname, p.get("network") or "direct", p.get("publisher_id") or "",
                 p.get("tracking_id") or "", p.get("account_ref") or "",
                 p.get("login_url") or "", p.get("dashboard_url") or "",
                 p.get("contact") or "", "active", "migrated 2026-08-31", now, now))
            conn.execute("UPDATE affiliate_programs SET store=?, connection=? WHERE name=?",
                         (slug, cname, slug))
        elif (p.get("status") or "") == "prospect" and not (p.get("network") or "").strip():
            # A store mislabeled as a program: keep the store, drop the program.
            conn.execute("DELETE FROM affiliate_programs WHERE name = ?", (slug,))
        else:
            conn.execute("UPDATE affiliate_programs SET store=? WHERE name=?", (slug, slug))
    try:
        conn.execute("DROP TABLE affiliate_program_hosts")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    _invalidate()


def _seed_amazon(conn: sqlite3.Connection) -> None:
    """Bootstrap the Amazon row from the system record, once.

    The Associates ids lived in `system_config` (Affiliate section) because there was no
    programs table to hold them. They are per-program membership, so this is where they
    belong — but the system_config rows are left in place as the SEED and as the fallback
    (feedback_no_data_in_code: code/config is bootstrap, the table is canonical once
    seeded). Retire those keys only after this row is confirmed in use.
    """
    if conn.execute("SELECT 1 FROM affiliate_programs WHERE name='amazon'").fetchone():
        return
    tag, store = "", ""
    try:
        from input.pipeline.system_config import get_setting
        tag = (get_setting("amazon_tracking_id", "") or "").strip()
        store = (get_setting("amazon_store_id", "") or "").strip()
    except Exception:
        pass
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO affiliate_stores(name, display_name, merchant, platform, "
        "created_at, updated_at) VALUES('amazon','Amazon','Amazon','marketplace',?,?)",
        (now, now))
    conn.execute(
        "INSERT OR IGNORE INTO affiliate_connections(name, network, tracking_id, account_ref, "
        "status, notes, created_at, updated_at) VALUES('amazon-associates','amazon',?,?,"
        "'active','Seeded from system_config.',?,?)", (tag, store, now, now))
    conn.execute(
        "INSERT INTO affiliate_programs(name, display_name, merchant, network, status, "
        "store, connection, tracking_id, account_ref, link_strategy, subtag_param, "
        "supports_deeplink, default_rate, commission_note, cookie_days, priority, notes, "
        "created_at, updated_at) "
        "VALUES('amazon','Amazon Associates','Amazon','amazon','active',"
        "'amazon','amazon-associates',?,?,'amazon_tag',"
        "'ascsubtag',1,0.045,'Kitchen & Dining 4.50% (SiteStripe, 2026-07-27)',1,10,"
        "'Seeded from system_config. `tag` is the only parameter Amazon requires.',?,?)",
        (tag, store, now, now))
    conn.execute("INSERT OR IGNORE INTO affiliate_store_hosts(host, store, kind, created_at) "
                 "VALUES('amazon.com','amazon','merchant',?)", (now,))
    conn.commit()
    _invalidate()


# --------------------------------------------------------------------------- #
#  Resolver — cached, because it sits on the click path
# --------------------------------------------------------------------------- #

_cache_lock = threading.Lock()
_host_cache: dict | None = None          # host -> program dict, merchants only


def _invalidate() -> None:
    global _host_cache
    with _cache_lock:
        _host_cache = None


def _open() -> sqlite3.Connection:
    return db_connect(DB_PATH, timeout=10)


_LADDER = {"active": 0, "approved": 1, "applied": 2, "prospect": 3,
           "paused": 4, "rejected": 5, "closed": 6}


def _merge_program(p: dict, connections: dict, networks: dict, store: dict) -> dict:
    """Program row + its connection's credentials (fill blanks only) + the
    network's subid metadata + store identity. What link building consumes."""
    out = dict(p)
    c = connections.get((p.get("connection") or "").strip()) or {}
    for k in ("publisher_id", "tracking_id", "account_ref", "login_url",
              "dashboard_url", "contact"):
        if not (out.get(k) or "").strip() and (c.get(k) or "").strip():
            out[k] = c[k]
    net = networks.get((c.get("network") or p.get("network") or "").strip()) or {}
    if not (out.get("subtag_param") or "").strip() and (net.get("subid_param") or "").strip():
        out["subtag_param"] = net["subid_param"]
    out["subid_max"] = net.get("subid_max")
    out["store"] = store.get("name") or out.get("store")
    out["store_platform"] = store.get("platform") or ""
    return out


def _load_cache(conn: sqlite3.Connection | None = None) -> dict:
    """host -> merged program dict (host -> STORE -> best program by status
    ladder then priority, joined with its connection + network metadata).
    A store with no program at all resolves to nothing — clean link.

    Mirrors system_config.get_setting: process-wide, invalidated on write. The click path
    must not pay a query per outbound link.
    """
    global _host_cache
    with _cache_lock:
        if _host_cache is not None:
            return _host_cache
    cache: dict = {}
    own = conn is None
    c = conn or _open()
    try:
        ensure_tables(c)
        stores = {s["name"]: s for s in _dicts(c, "SELECT * FROM affiliate_stores")}
        hosts = _dicts(c, "SELECT * FROM affiliate_store_hosts "
                          "WHERE COALESCE(kind,'merchant') = 'merchant'")
        programs = _dicts(c, "SELECT * FROM affiliate_programs")
        connections = {r["name"]: r for r in _dicts(c, "SELECT * FROM affiliate_connections")}
        networks = {r["name"]: r for r in _dicts(c, "SELECT * FROM affiliate_networks")}
        by_store: dict = {}
        for p in programs:
            by_store.setdefault((p.get("store") or "").strip(), []).append(p)
        for h in hosts:
            host = (h.get("host") or "").strip().lower()
            store = stores.get(h.get("store"))
            if not host or not store:
                continue
            progs = by_store.get(store["name"]) or []
            if not progs:
                continue                      # a prospect store: clean links until a program
            best = min(progs, key=lambda p: (
                _LADDER.get((p.get("status") or "").strip().lower(), 9),
                p.get("priority") if p.get("priority") is not None else 100))
            cache[host] = _merge_program(best, connections, networks, store)
    except Exception:
        cache = {}
    finally:
        if own:
            c.close()
    with _cache_lock:
        _host_cache = cache
    return cache


def program_named(name: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Look a program up by NAME off the same cache the URL resolver uses.

    The cache is keyed by host, so `cache.get('amazon')` silently misses and the caller
    falls through to whatever fallback it has — which is exactly what happened to
    `buy_links.amazon_tag()`: editing the program row changed the minted link but not the
    tag, because one path resolved by URL and the other by name.
    """
    want = (name or "").strip().lower()
    if not want:
        return None
    for row in (_load_cache(conn) or {}).values():
        if (row.get("name") or "").strip().lower() == want:
            return row
    return None


def program_for_url(url: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """The program that pays us for this destination, or None.

    Matches on host or any parent domain (`shop.walmart.com` -> `walmart.com`), so a
    program is registered once rather than per subdomain. Only `kind='merchant'` hosts
    resolve — a network redirector is a WRAPPER, not a place to send a reader.
    """
    host = ""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    cache = _load_cache(conn)
    parts = host.split(".")
    for i in range(len(parts) - 1):
        hit = cache.get(".".join(parts[i:]))
        if hit:
            return hit
    return None


# --------------------------------------------------------------------------- #
#  Link construction
# --------------------------------------------------------------------------- #

def _placeholders(program: dict, dest: str, subtag: str, click_id: str) -> dict:
    return {
        "dest": dest,
        "dest_enc": quote(dest, safe=""),
        "publisher_id": (program.get("publisher_id") or "").strip(),
        "tracking_id": (program.get("tracking_id") or "").strip(),
        "campaign_id": (program.get("campaign_id") or "").strip(),
        "account_ref": (program.get("account_ref") or "").strip(),
        "subtag": subtag or "",
        "click_id": click_id or "",
    }


def link_fault(program: dict, dest: str, subtag: str = "", click_id: str = "") -> str:
    """Why this program cannot produce a link for this destination, or "" if it can.

    Returned rather than raised: the caller's correct response is always the same — emit
    the clean destination — and a fault is a worklist entry ("Walmart is approved but has
    no publisher_id"), not an error.
    """
    if not program:
        return "no program"
    status = (program.get("status") or "").strip().lower()
    if status != "active":
        return f"status is {status or 'unset'}, not active"
    strategy = (program.get("link_strategy") or "template").strip().lower()
    if strategy == "none":
        return "link_strategy is none"
    if strategy == "amazon_tag":
        return "" if (program.get("tracking_id") or program.get("account_ref")) \
            else "no tracking_id or account_ref"
    if strategy != "template":
        return f"unknown link_strategy {strategy!r}"

    tpl = (program.get("link_template") or "").strip()
    if not tpl:
        return "no link_template"
    if "{dest}" not in tpl and "{dest_enc}" not in tpl:
        return "link_template carries no {dest}/{dest_enc} — cannot deep-link"
    if not program.get("supports_deeplink", 1):
        return "program does not support deep links"
    vals = _placeholders(program, dest, subtag, click_id)
    for key in _CREDENTIALS:
        if "{" + key + "}" in tpl and not str(vals.get(key) or "").strip():
            # A template that renders with an empty CREDENTIAL produces a live link that
            # earns nothing and looks broken. Refuse it.
            #
            # `subtag` and `click_id` are deliberately NOT checked: they are per-click
            # attribution detail, not credentials, and are legitimately empty when a link
            # is rendered for preview. Treating them as required made every link
            # unbuildable outside a real click.
            return f"link_template needs {{{key}}} but it is blank"
    try:
        tpl.format(**vals)
    except (KeyError, IndexError, ValueError) as e:
        return f"link_template placeholder error: {e}"
    return ""


def build_link(program: dict, dest: str, *, subtag: str = "", click_id: str = "") -> str:
    """Render the affiliate URL, or "" when this program cannot produce one.

    "" means "use the clean destination" — the caller decides, and every caller decides
    the same way. Amazon is NOT handled here: its `tag`/`ascsubtag` rules live with the
    rest of the Amazon knowledge in `buy_links`, and this module stays generic.
    """
    if link_fault(program, dest, subtag, click_id):
        return ""
    tpl = (program.get("link_template") or "").strip()
    return tpl.format(**_placeholders(program, dest, subtag, click_id))


# --------------------------------------------------------------------------- #
#  CRUD
# --------------------------------------------------------------------------- #

def list_programs(conn: sqlite3.Connection) -> list:
    ensure_tables(conn)
    rows = _dicts(conn,
        "SELECT p.*, (SELECT COUNT(*) FROM affiliate_store_hosts h "
        "  WHERE h.store = p.store) AS host_count "
        "FROM affiliate_programs p ORDER BY p.priority, p.name")
    for r in rows:
        r["hosts"] = [x["host"] for x in _dicts(
            conn, "SELECT host FROM affiliate_store_hosts WHERE store = ? ORDER BY host",
            (r.get("store") or r["name"],))]
    return rows


def get_program(conn: sqlite3.Connection, name: str) -> dict | None:
    ensure_tables(conn)
    rows = _dicts(conn, "SELECT * FROM affiliate_programs WHERE name = ?", (name,))
    if not rows:
        return None
    p = rows[0]
    p["hosts"] = _dicts(conn, "SELECT host, kind FROM affiliate_store_hosts "
                              "WHERE store = ? ORDER BY host", (p.get("store") or name,))
    return p


def create_program(conn: sqlite3.Connection, patch: dict) -> dict:
    ensure_tables(conn)
    merchant = (patch.get("merchant") or patch.get("name") or "").strip()
    name = (patch.get("name") or "").strip() or merchant.lower().replace(" ", "-")
    if not name:
        raise ValueError("name (or merchant) is required")
    if not merchant:
        raise ValueError("merchant is required")
    if get_program(conn, name):
        raise ValueError(f"affiliate program {name!r} already exists")
    _validate(patch)
    now = _now()
    # The program is a JOIN — it must hang off a store. Reuse a named one or
    # auto-create from the merchant (a program implies its store exists).
    store = (patch.get("store") or "").strip() or merchant.lower().replace(" ", "-")
    conn.execute(
        "INSERT OR IGNORE INTO affiliate_stores(name, display_name, merchant, platform, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (store, (patch.get("display_name") or "").strip(), merchant, "", now, now))
    conn.execute(
        "INSERT INTO affiliate_programs(name, display_name, merchant, network, status, "
        "store, connection, link_strategy, supports_deeplink, priority, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, (patch.get("display_name") or "").strip(), merchant,
         (patch.get("network") or "").strip(),
         (patch.get("status") or "prospect").strip().lower(),
         store, (patch.get("connection") or "").strip(),
         (patch.get("link_strategy") or "template").strip().lower(),
         1 if patch.get("supports_deeplink", True) else 0,
         int(patch.get("priority") or 100), now, now))
    conn.commit()
    _invalidate()
    # `hosts` rides along in the same patch — it is not in EDITABLE (it lives in its own
    # table), so it has to be passed explicitly or a create silently produces a program
    # nothing resolves to.
    rest = {k: v for k, v in patch.items() if k in EDITABLE or k == "hosts"}
    if rest:
        return update_program(conn, name, rest)
    return get_program(conn, name)


def _validate(patch: dict) -> None:
    st = (patch.get("status") or "").strip().lower()
    if st and st not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    ls = (patch.get("link_strategy") or "").strip().lower()
    if ls and ls not in STRATEGIES:
        raise ValueError(f"link_strategy must be one of {', '.join(STRATEGIES)}")


def update_program(conn: sqlite3.Connection, name: str, patch: dict) -> dict | None:
    ensure_tables(conn)
    if not get_program(conn, name):
        return None
    _validate(patch)
    sets, vals = [], []
    for f in EDITABLE:
        if f not in patch:
            continue
        v = patch[f]
        if f == "supports_deeplink":
            v = 1 if v else 0
        elif f in ("status", "link_strategy", "network"):
            v = (v or "").strip().lower()
        elif f in ("default_rate",):
            v = float(v) if str(v).strip() not in ("", "None") else None
        elif f in ("cookie_days", "priority"):
            v = int(v) if str(v).strip() not in ("", "None") else None
        sets.append(f"{f} = ?")
        vals.append(v)
    if "status" in patch:
        st = (patch["status"] or "").strip().lower()
        col = {"applied": "applied_at", "approved": "approved_at"}.get(st)
        if col:
            sets.append(f"{col} = COALESCE({col}, ?)")
            vals.append(_now())
    if sets:
        sets.append("updated_at = ?")
        vals.extend([_now(), name])
        conn.execute(f"UPDATE affiliate_programs SET {', '.join(sets)} WHERE name = ?", vals)
        conn.commit()
    if "hosts" in patch:
        set_hosts(conn, name, patch["hosts"])
    _invalidate()
    return get_program(conn, name)


def delete_program(conn: sqlite3.Connection, name: str) -> bool:
    """Deletes the RELATIONSHIP only — the store, its hosts and its click
    history all survive (release, don't destroy)."""
    ensure_tables(conn)
    cur = conn.execute("DELETE FROM affiliate_programs WHERE name = ?", (name,))
    conn.commit()
    _invalidate()
    return cur.rowcount > 0


def set_hosts(conn: sqlite3.Connection, name: str, hosts) -> int:
    """Replace the hosts of the STORE behind this program (or store name
    directly). Accepts ['walmart.com'] or [{host, kind}] or a delimited
    string, so the editor and a direct call converge on one shape."""
    ensure_tables(conn)
    prog = _dicts(conn, "SELECT store FROM affiliate_programs WHERE name = ?", (name,))
    store = (prog[0]["store"] if prog and prog[0].get("store") else name)
    if isinstance(hosts, str):
        hosts = [h for h in __import__("re").split(r"[;,\s\n]+", hosts) if h.strip()]
    items = []
    for h in hosts or []:
        if isinstance(h, dict):
            host, kind = (h.get("host") or "").strip(), (h.get("kind") or "merchant").strip()
        else:
            host, kind = str(h).strip(), "merchant"
        host = host.lower().replace("https://", "").replace("http://", "").split("/")[0]
        if host.startswith("www."):
            host = host[4:]
        if host:
            items.append((host, kind))
    conn.execute("DELETE FROM affiliate_store_hosts WHERE store = ?", (store,))
    now = _now()
    for host, kind in items:
        # A host belongs to exactly one store; last writer wins, loudly in the editor.
        conn.execute("INSERT OR REPLACE INTO affiliate_store_hosts(host, store, kind, "
                     "created_at) VALUES(?,?,?,?)", (host, store, kind, now))
    conn.commit()
    _invalidate()
    return len(items)


# --------------------------------------------------------------------------- #
#  Stores / networks / connections CRUD
# --------------------------------------------------------------------------- #

def list_stores(conn: sqlite3.Connection) -> list:
    """Every store, with hosts and program coverage — a store with no program
    is a PROSPECT by definition."""
    ensure_tables(conn)
    rows = _dicts(conn, "SELECT * FROM affiliate_stores ORDER BY name")
    progs = _dicts(conn, "SELECT name, store, status, network FROM affiliate_programs")
    by_store: dict = {}
    for p in progs:
        by_store.setdefault((p.get("store") or "").strip(), []).append(p)
    for r in rows:
        r["hosts"] = [x["host"] for x in _dicts(
            conn, "SELECT host FROM affiliate_store_hosts WHERE store = ? ORDER BY host",
            (r["name"],))]
        r["programs"] = by_store.get(r["name"]) or []
    return rows


def create_store(conn: sqlite3.Connection, patch: dict) -> dict:
    ensure_tables(conn)
    merchant = (patch.get("merchant") or patch.get("name") or "").strip()
    name = (patch.get("name") or "").strip() or merchant.lower().replace(" ", "-")
    if not name or not merchant:
        raise ValueError("name (or merchant) is required")
    if conn.execute("SELECT 1 FROM affiliate_stores WHERE name=?", (name,)).fetchone():
        raise ValueError(f"store {name!r} already exists")
    now = _now()
    conn.execute("INSERT INTO affiliate_stores(name, display_name, merchant, platform, "
                 "contact, notes, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                 (name, (patch.get("display_name") or "").strip(), merchant,
                  (patch.get("platform") or "").strip(), (patch.get("contact") or "").strip(),
                  (patch.get("notes") or "").strip(), now, now))
    conn.commit()
    if patch.get("hosts"):
        set_hosts(conn, name, patch["hosts"])
    _invalidate()
    return next((s for s in list_stores(conn) if s["name"] == name), None)


def update_store(conn: sqlite3.Connection, name: str, patch: dict) -> dict | None:
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM affiliate_stores WHERE name=?", (name,)).fetchone():
        return None
    sets, vals = [], []
    for f in STORE_EDITABLE:
        if f in patch:
            sets.append(f"{f} = ?")
            vals.append((patch[f] or "").strip() if isinstance(patch[f], str) else patch[f])
    if sets:
        sets.append("updated_at = ?")
        vals.extend([_now(), name])
        conn.execute(f"UPDATE affiliate_stores SET {', '.join(sets)} WHERE name = ?", vals)
        conn.commit()
    if "hosts" in patch:
        set_hosts(conn, name, patch["hosts"])
    _invalidate()
    return next((s for s in list_stores(conn) if s["name"] == name), None)


def delete_store(conn: sqlite3.Connection, name: str) -> bool:
    """Guarded: a store with programs cannot be deleted (delete the programs
    first — an approval-carrying relationship must never vanish as a cascade)."""
    ensure_tables(conn)
    used = conn.execute("SELECT COUNT(*) FROM affiliate_programs WHERE store = ?",
                        (name,)).fetchone()[0]
    if used:
        raise ValueError(f"store {name!r} has {used} program(s) — delete them first")
    cur = conn.execute("DELETE FROM affiliate_stores WHERE name = ?", (name,))
    conn.execute("DELETE FROM affiliate_store_hosts WHERE store = ?", (name,))
    conn.commit()
    _invalidate()
    return cur.rowcount > 0


def list_networks(conn: sqlite3.Connection) -> list:
    """Networks with their connections nested — the small fixed roster."""
    ensure_tables(conn)
    nets = _dicts(conn, "SELECT * FROM affiliate_networks ORDER BY name")
    conns = _dicts(conn, "SELECT * FROM affiliate_connections ORDER BY name")
    for n in nets:
        n["connections"] = [c for c in conns if c.get("network") == n["name"]]
    return nets


def upsert_connection(conn: sqlite3.Connection, patch: dict) -> dict:
    ensure_tables(conn)
    name = (patch.get("name") or "").strip()
    network = (patch.get("network") or "").strip()
    if not name:
        raise ValueError("name is required")
    exists = conn.execute("SELECT 1 FROM affiliate_connections WHERE name=?",
                          (name,)).fetchone()
    now = _now()
    if not exists:
        if not network:
            raise ValueError("network is required for a new connection")
        conn.execute("INSERT INTO affiliate_connections(name, network, status, created_at, "
                     "updated_at) VALUES(?,?, 'active', ?, ?)", (name, network, now, now))
    sets, vals = [], []
    for f in CONNECTION_EDITABLE:
        if f in patch:
            sets.append(f"{f} = ?")
            vals.append((patch[f] or "").strip() if isinstance(patch[f], str) else patch[f])
    if sets:
        sets.append("updated_at = ?")
        vals.extend([now, name])
        conn.execute(f"UPDATE affiliate_connections SET {', '.join(sets)} WHERE name = ?", vals)
    conn.commit()
    _invalidate()
    return _dicts(conn, "SELECT * FROM affiliate_connections WHERE name = ?", (name,))[0]
