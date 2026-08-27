"""Affiliate programs — who pays us, and how a link to them is built.

The table behind the revenue path. `buy_links.affiliate_url()` was hard-wired to Amazon:
every other retailer got the clean destination and earned nothing. Measured 2026-07-28,
that was **35 of 58 held offers (60%)** — Sur La Table 8, Williams Sonoma 8, Walmart 7.
The olive-oil run made it concrete: Carapelli Original ranked #1 and could only be bought
at Walmart, which has a perfectly good program we simply had not wired.

Two tables:

    affiliate_programs        one per merchant relationship: our membership, how a link
                              is built, the commission, and the STATUS that gates it
    affiliate_program_hosts   host -> program, so an outbound URL resolves in one lookup

Design note: docs/affiliate-programs-and-clicks.md.

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

EDITABLE = ("display_name", "merchant", "network", "status", "publisher_id", "tracking_id",
            "campaign_id", "account_ref", "login_url", "dashboard_url", "contact",
            "link_strategy", "link_template", "subtag_param", "supports_deeplink",
            "default_rate", "commission_note", "cookie_days", "priority", "notes")


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
        CREATE TABLE IF NOT EXISTS affiliate_program_hosts (
            host       TEXT PRIMARY KEY,        -- 'walmart.com', matched as host or *.host
            program    TEXT NOT NULL,
            kind       TEXT DEFAULT 'merchant', -- merchant | network_redirector
            created_at TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aph_program "
                 "ON affiliate_program_hosts(program)")
    conn.commit()
    _seed_amazon(conn)


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
        "INSERT INTO affiliate_programs(name, display_name, merchant, network, status, "
        "tracking_id, account_ref, link_strategy, subtag_param, supports_deeplink, "
        "default_rate, commission_note, cookie_days, priority, notes, created_at, updated_at) "
        "VALUES('amazon','Amazon Associates','Amazon','amazon','active',?,?,'amazon_tag',"
        "'ascsubtag',1,0.045,'Kitchen & Dining 4.50% (SiteStripe, 2026-07-27)',1,10,"
        "'Seeded from system_config. `tag` is the only parameter Amazon requires; "
        "SiteStripe''s linkCode/linkId/language/ref_ are its own reporting residue.',?,?)",
        (tag, store, now, now))
    conn.execute("INSERT OR IGNORE INTO affiliate_program_hosts(host, program, kind, created_at) "
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


def _load_cache(conn: sqlite3.Connection | None = None) -> dict:
    """host -> program row, for ACTIVE merchant hosts only.

    Mirrors system_config.get_setting: process-wide, invalidated on write. The click path
    must not pay a query per outbound link.
    """
    global _host_cache
    with _cache_lock:
        if _host_cache is not None:
            return _host_cache
    own = conn is None
    c = conn or _open()
    try:
        ensure_tables(c)
        rows = _dicts(c,
            "SELECT h.host, p.* FROM affiliate_program_hosts h "
            "JOIN affiliate_programs p ON p.name = h.program "
            "WHERE COALESCE(h.kind,'merchant') = 'merchant'")
    except Exception:
        rows = []
    finally:
        if own:
            c.close()
    cache = {r["host"].strip().lower(): r for r in rows if (r.get("host") or "").strip()}
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
        "SELECT p.*, (SELECT COUNT(*) FROM affiliate_program_hosts h "
        "  WHERE h.program = p.name) AS host_count "
        "FROM affiliate_programs p ORDER BY p.priority, p.name")
    for r in rows:
        r["hosts"] = [x["host"] for x in _dicts(
            conn, "SELECT host FROM affiliate_program_hosts WHERE program = ? ORDER BY host",
            (r["name"],))]
    return rows


def get_program(conn: sqlite3.Connection, name: str) -> dict | None:
    ensure_tables(conn)
    rows = _dicts(conn, "SELECT * FROM affiliate_programs WHERE name = ?", (name,))
    if not rows:
        return None
    p = rows[0]
    p["hosts"] = _dicts(conn, "SELECT host, kind FROM affiliate_program_hosts "
                              "WHERE program = ? ORDER BY host", (name,))
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
    conn.execute(
        "INSERT INTO affiliate_programs(name, display_name, merchant, network, status, "
        "link_strategy, supports_deeplink, priority, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (name, (patch.get("display_name") or "").strip(), merchant,
         (patch.get("network") or "").strip(),
         (patch.get("status") or "prospect").strip().lower(),
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
    ensure_tables(conn)
    cur = conn.execute("DELETE FROM affiliate_programs WHERE name = ?", (name,))
    conn.execute("DELETE FROM affiliate_program_hosts WHERE program = ?", (name,))
    conn.commit()
    _invalidate()
    return cur.rowcount > 0


def set_hosts(conn: sqlite3.Connection, name: str, hosts) -> int:
    """Replace this program's hosts. Accepts ['walmart.com'] or [{host, kind}] or a
    delimited string, so the editor and a direct call converge on one shape."""
    ensure_tables(conn)
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
    conn.execute("DELETE FROM affiliate_program_hosts WHERE program = ?", (name,))
    now = _now()
    for host, kind in items:
        # A host belongs to exactly one program; last writer wins, loudly in the editor.
        conn.execute("INSERT OR REPLACE INTO affiliate_program_hosts(host, program, kind, "
                     "created_at) VALUES(?,?,?,?)", (host, name, kind, now))
    conn.commit()
    _invalidate()
    return len(items)
