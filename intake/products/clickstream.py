"""The /go/ clickstream + the impressions log — layer 5's data plane.

docs/affiliate-programs-and-clicks.md §3.3-§6, built 2026-08-29. The
programs/hosts tables and the link builder (affiliate_programs.py,
buy_links.affiliate_url) already existed; this adds what was missing:
click capture, the redirect's bookkeeping, and the impressions denominator
the EV metric needs from day one of display
(docs/dish-product-matching.md, "impressions log must exist from day one").

Contract highlights (all from the design note):
- click_id is minted WHEN THE LINK IS RENDERED and embedded in the href;
  the row is written when it is FOLLOWED. Rendered-but-unclicked costs
  nothing. click_id doubles as the network subtag (conversion join).
- Every href is HMAC-signed over (click_id, destination) — /go/ never
  becomes an open redirector; an unsigned/forged link 404s.
- Bot filtering RECORDS rather than discards (counted=0 + suppressed_by):
  prefetch headers, scanner UAs (data-driven via system_config), HEAD,
  30s same-(session,dest) dedupe. Everything else counts, so a new
  unrecognized source shows up in the numbers instead of vanishing.
- Unmonetized hosts still redirect and still record (program NULL) — those
  rows ARE the which-program-to-join-next report.
- ip_hash only, never raw IP.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

_SECRET_KEY = "go_link_secret"
_SCANNER_KEY = "click_scanner_uas"
_DEFAULT_SCANNERS = [
    "slackbot", "whatsapp", "twitterbot", "facebookexternalhit", "linkedinbot",
    "discordbot", "telegrambot", "skypeuripreview", "bingpreview",
    "googleimageproxy", "proofpoint", "mimecast", "barracuda", "urldefense",
]
_DEDUPE_SECONDS = 30


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS affiliate_clicks (
            click_id      TEXT PRIMARY KEY,
            created_at    TEXT NOT NULL,
            day           TEXT,
            hour          INTEGER,
            program       TEXT,
            merchant_host TEXT,
            dest_url      TEXT NOT NULL,
            final_url     TEXT,
            tagged        INTEGER DEFAULT 0,
            surface       TEXT,
            product_id    TEXT,
            collection    TEXT,
            slot          TEXT,
            page_url      TEXT,
            referrer      TEXT,
            user_id       TEXT,
            session_id    TEXT,
            ip_hash       TEXT,
            country       TEXT,
            user_agent    TEXT,
            device        TEXT,
            counted       INTEGER DEFAULT 1,
            suppressed_by TEXT,
            store         TEXT,                -- resolved store (host -> store)
            rate          REAL                 -- program rate AT CLICK TIME — rates are
                                               -- time-versioned data; history must
                                               -- survive rate edits (design note §9)
        )""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(affiliate_clicks)")}
    if "store" not in cols:
        conn.execute("ALTER TABLE affiliate_clicks ADD COLUMN store TEXT")
        conn.execute("ALTER TABLE affiliate_clicks ADD COLUMN rate REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clk_time ON affiliate_clicks(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clk_program ON affiliate_clicks(program, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clk_product ON affiliate_clicks(product_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clk_counted ON affiliate_clicks(counted, created_at)")
    # Conversions = an IMMUTABLE EVENT LOG (design note §9): every network
    # report row / webhook is an event; a reversal is a NEW event, never an
    # UPDATE (networks ship corrections as deltas for 60+ days post-sale).
    # A derived current-state view sits on top when ingestion lands (Phase 2).
    # click_id is NULLABLE with match_status — 25-30% attribution loss is
    # structural; a conversion with no subid is a fact, not an error.
    try:
        old = conn.execute("SELECT COUNT(*) FROM affiliate_conversions").fetchone()
        if old and old[0] == 0:
            conn.execute("DROP TABLE affiliate_conversions")
    except sqlite3.OperationalError:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS affiliate_conversion_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            connection     TEXT,               -- which of our accounts reported it
            network_txn_id TEXT,               -- the network's transaction id (dedupe key)
            observed_at    TEXT NOT NULL,      -- when WE saw this event
            event          TEXT,               -- created | updated | reversed | paid
            status         TEXT,               -- network status verbatim (pending/locked/…)
            order_total    REAL,
            commission     REAL,
            currency       TEXT,
            click_id       TEXT,               -- NULLABLE — see match_status
            match_status   TEXT,               -- matched | no_subid | unknown_click
            program        TEXT,
            raw            TEXT                -- the report row/payload, verbatim
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ace_txn "
                 "ON affiliate_conversion_events(connection, network_txn_id, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ace_click "
                 "ON affiliate_conversion_events(click_id)")
    # One row per rendered ITEM (with its position): the EV denominator.
    # P(click|shown) has no meaning without knowing what was shown where.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commerce_impressions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT NOT NULL,
            day         TEXT,
            surface     TEXT,               -- recipe | dish | brief | cook
            page_url    TEXT,
            dish_name   TEXT,
            recipe_id   TEXT,
            class_name  TEXT,
            product_id  TEXT,
            position    INTEGER,
            session_id  TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_imp_day ON commerce_impressions(day)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_imp_class ON commerce_impressions(class_name, day)")
    conn.commit()


def _secret(conn: sqlite3.Connection) -> bytes:
    """Server-side HMAC secret, generated once and stored in system_config."""
    from input.pipeline.system_config import get_setting, set_setting
    s = get_setting(_SECRET_KEY, "")
    if not s:
        s = secrets.token_hex(32)
        set_setting(conn, _SECRET_KEY, s)
    return s.encode()


def _sig(secret: bytes, click_id: str, dest: str) -> str:
    return hmac.new(secret, f"{click_id}|{dest}".encode(), hashlib.sha256).hexdigest()[:16]


def new_click_id() -> str:
    """Time-ordered id (sortable like a ULID, cheap): ms timestamp + random."""
    return f"{int(time.time() * 1000):x}{secrets.token_hex(6)}"


def mint(conn: sqlite3.Connection, dest: str, *, surface: str = "",
         product_id: str = "", collection: str = "", slot: str = "") -> str:
    """The href a render surface embeds. Writes NOTHING — the row appears
    only if the link is followed."""
    cid = new_click_id()
    sig = _sig(_secret(conn), cid, dest)
    q = [f"u={quote(dest, safe='')}", f"sg={sig}"]
    for k, v in (("sf", surface), ("pid", product_id), ("col", collection), ("sl", slot)):
        if v:
            q.append(f"{k}={quote(str(v), safe='')}")
    return f"/go/{cid}?" + "&".join(q)


def _scanners(conn: sqlite3.Connection) -> list:
    from input.pipeline.system_config import get_setting
    try:
        v = get_setting(_SCANNER_KEY, None)
        if v:
            return [str(x).lower() for x in (v if isinstance(v, list) else json.loads(v))]
    except Exception:
        pass
    return _DEFAULT_SCANNERS


def record_click(conn: sqlite3.Connection, click_id: str, params: dict, *,
                 method: str = "GET", headers: dict | None = None,
                 client_ip: str = "", session_id: str = "") -> str | None:
    """Verify, classify, record, and return the URL to redirect to —
    or None when the signature fails (the caller 404s; a forged /go/ link
    must not become an open redirect)."""
    ensure_tables(conn)
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    dest = params.get("u") or ""
    if not dest or not click_id:
        return None
    secret = _secret(conn)
    if not hmac.compare_digest(params.get("sg") or "", _sig(secret, click_id, dest)):
        return None
    if not re.match(r"^https?://", dest):
        return None

    # Classify (§6) — record, never discard.
    ua = (headers.get("user-agent") or "")
    suppressed = ""
    if method.upper() == "HEAD":
        suppressed = "head"
    elif "prefetch" in (headers.get("sec-purpose") or "") or \
         "prefetch" in (headers.get("purpose") or "") or \
         "prerender" in (headers.get("sec-purpose") or ""):
        suppressed = "prefetch"
    elif any(s in ua.lower() for s in _scanners(conn)):
        suppressed = "scanner"
    elif session_id:
        recent = conn.execute(
            "SELECT 1 FROM affiliate_clicks WHERE session_id = ? AND dest_url = ? "
            "AND counted = 1 AND created_at > datetime('now', ?) LIMIT 1",
            (session_id, dest, f"-{_DEDUPE_SECONDS} seconds")).fetchone()
        if recent:
            suppressed = "dedupe"

    # Tag through the existing chokepoint; click_id IS the subtag.
    from intake.products.buy_links import affiliate_url, clean_url
    try:
        final = affiliate_url(dest, subtag=click_id, click_id=click_id, conn=conn)
    except Exception:
        final = dest
    tagged = 1 if (final or "") != (clean_url(dest) or dest) else 0
    host = (urlparse(dest).hostname or "").replace("www.", "").lower()
    program, store, rate = None, None, None
    try:
        from intake.products import affiliate_programs as ap
        prog = ap.program_for_url(dest, conn)
        if prog:
            program = prog.get("name")
            store = prog.get("store")
            # Rate stamped AT CLICK TIME — rates are time-versioned data and
            # this row must still say what the click was worth after edits.
            rate = prog.get("default_rate")
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    local = datetime.now()
    conn.execute(
        "INSERT OR IGNORE INTO affiliate_clicks(click_id, created_at, day, hour, program, "
        "merchant_host, dest_url, final_url, tagged, surface, product_id, collection, "
        "slot, page_url, referrer, session_id, ip_hash, user_agent, counted, suppressed_by, "
        "store, rate) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (click_id, now.isoformat(), local.strftime("%Y-%m-%d"), local.hour, program,
         host, dest, final, tagged,
         params.get("sf") or "", params.get("pid") or "", params.get("col") or "",
         params.get("sl") or "", params.get("pg") or "", headers.get("referer") or "",
         session_id,
         hashlib.sha256(secret + client_ip.encode()).hexdigest()[:32] if client_ip else "",
         ua[:300], 0 if suppressed else 1, suppressed or None, store, rate))
    conn.commit()
    return final or dest


def log_impressions(conn: sqlite3.Connection, *, surface: str, page_url: str,
                    session_id: str, items: list) -> int:
    """Batch-insert what a block render actually showed. Capped defensively."""
    ensure_tables(conn)
    now = datetime.now(timezone.utc).isoformat()
    day = datetime.now().strftime("%Y-%m-%d")
    n = 0
    for it in items[:50]:
        conn.execute(
            "INSERT INTO commerce_impressions(created_at, day, surface, page_url, "
            "dish_name, recipe_id, class_name, product_id, position, session_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (now, day, surface[:32], page_url[:500],
             (it.get("dish_name") or "")[:200], (it.get("recipe_id") or "")[:64],
             (it.get("class_name") or "")[:200], (it.get("product_id") or "")[:64],
             it.get("position"), session_id[:64]))
        n += 1
    conn.commit()
    return n
