"""Acquisition ledger + technique registry (docs/acquisition-ledger.md, phase 2).

One row per attempt to obtain a page, by any technique; one registry row per
technique, seeded from docs/acquisition-techniques.md so the admin page and
the ⓘ on the domain form show the same text the doc holds.

Phase 2 is WRITE-ONLY: nothing reads the ledger to make a decision. It is the
BEFORE measurement the ladder carve (phase 3) and the policy job (phase 4)
are judged against. The one thing it does stamp is the domain's LAST
successful technique (`domains.acquire_last_technique`), so the domain form
and the extract record can say how a publisher's pages are actually being
obtained today — the curator asked for exactly that (2026-09-10).

Hooks (all best-effort, never raise into the pipeline):
  html_to_markdown ladder      -> record() per rung (cache/direct/unblocker/
                                  unblocker_render/wayback)
  intake.capture.walk          -> record() per page (walker)
  build_query_batch filter     -> gate() back-fills the structure verdict
  save_recipe endpoint         -> mark_saved() flips `saved`
  jobs runner                  -> set_context() so rows carry job_id/run_kind
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.path.join(_ROOT, "recipes.db")
TECHNIQUES_DOC = os.path.join(_ROOT, "docs", "acquisition-techniques.md")

TECHNIQUES = ("cache", "direct", "unblocker", "unblocker_render", "wayback", "walker", "human")
# Credits per attempt. The unblocker's render is one RENDER credit, priced
# several× a static one by every provider; recorded as 1 unit of its own kind.
COST_UNITS = {"cache": 0.0, "direct": 0.0, "unblocker": 1.0, "unblocker_render": 1.0,
              "wayback": 0.0, "walker": 0.0, "human": 0.0}

# ------------------------------------------------------------------ context
_ctx: contextvars.ContextVar = contextvars.ContextVar("acq_ctx", default=None)


def set_context(*, job_id: Optional[int] = None, run_kind: str = "",
                domain: str = "") -> None:
    _ctx.set({"job_id": job_id, "run_kind": run_kind or "", "domain": _host(domain) if domain else ""})


def context() -> dict:
    return dict(_ctx.get() or {"job_id": None, "run_kind": "", "domain": ""})


# ------------------------------------------------------------------ helpers
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host(url_or_host: str) -> str:
    h = (url_or_host or "").strip().lower()
    if "://" in h:
        h = urlsplit(h).netloc
    return h[4:] if h.startswith("www.") else h


def _root(host: str) -> str:
    try:
        from input.pipeline.url_utils import root_domain
        return (root_domain(host) or host).lower()
    except Exception:
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host


def norm_url(url: str) -> str:
    try:
        from input.pipeline.url_utils import normalize_url
        return normalize_url(url) or url
    except Exception:
        return (url or "").split("#", 1)[0].rstrip("/")


def _connect() -> sqlite3.Connection:
    try:
        from input.pipeline.db import connect
        return connect(DB_PATH)
    except Exception:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout=10000")
        return conn


# ------------------------------------------------------------------ failure classes
# Deterministic first; free text never. docs/acquisition-ledger.md §4.
_CLASS_RULES = (
    (r"soft-block|challenge stub|\b202\b", "block:soft"),
    (r"sgcaptcha|just a moment|verify you are human|captcha|interstitial|did not clear", "block:captcha"),
    (r"\b429\b|too many requests|rate.?limit", "block:ratelimit"),
    (r"\b(403|503)\b|forbidden", "block:hard"),
    (r"parked domain|parking marker|/lander stub|domain is dead", "dead:parked"),
    (r"thin body|js shell|no json-ld|no <article>|too small", "shell:js"),
    (r"aggregator|content lives elsewhere|see full directions|see original recipe|visit the original", "aggregator:elsewhere"),
    (r"members|membership|sign in|log in|try the club|subscribe", "wall:membership"),
    (r"paywall|subscriber", "wall:paywall"),
    (r"\b(404|410)\b|not found|gone", "gone:404"),
    (r"no wayback snapshot|no snapshot", "gone:no-snapshot"),
    (r"circuit open|circuit-open", "net:circuit-open"),
    (r"timeout|timed out|read timed", "net:timeout"),
    (r"\b402\b|pay.?per.?crawl", "vendor:402"),
    (r"no unblocker|no creds|not configured", "vendor:unavailable"),
)


def technique_from_timings(timings: Optional[dict]) -> str:
    """The technique key for an extract's `_source.acquiredVia`, from the
    fetch meta html_to_markdown leaves in `timings` (fetch_source +
    fetch_render). '' when the extract did not come through the ladder
    (a bookmarklet stage, a PDF, an image)."""
    if not timings:
        return ""
    src = str(timings.get("fetch_source") or "").lower()
    if src == "page-cache":
        return "cache"
    if src == "unblocker":
        return "unblocker_render" if timings.get("fetch_render") else "unblocker"
    if src in ("direct", "wayback"):
        return src
    return ""


def classify(reason: str, status_code: Optional[int] = None) -> str:
    """Map a free-text failure phrase (blocked_reason, an exception message)
    onto a named class. Unknown → 'other', which the report surfaces so a
    new class can be named rather than lost."""
    if status_code in (404, 410):
        return "gone:404"
    if status_code == 429:
        return "block:ratelimit"      # "slow down": the direct rung pauses + retries
    if status_code in (403, 503):
        return "block:hard"
    if status_code == 402:
        return "vendor:402"
    low = (reason or "").lower()
    for pat, cls in _CLASS_RULES:
        if re.search(pat, low):
            return cls
    return "net:error" if low else ""


# ------------------------------------------------------------------ schema + seed
_TABLES = """
CREATE TABLE IF NOT EXISTS acquisition_techniques (
    key           TEXT PRIMARY KEY,
    label         TEXT NOT NULL,
    description   TEXT NOT NULL,      -- the doc section, verbatim (markdown)
    cost_note     TEXT,
    latency_note  TEXT,
    failure_classes TEXT,              -- JSON list
    enabled       INTEGER NOT NULL DEFAULT 1,
    sort          INTEGER NOT NULL DEFAULT 0,
    seeded_from   TEXT,                -- doc path + mtime the text came from
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS acquisition_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    job_id        INTEGER,
    run_kind      TEXT,                -- job type: dish_refresh | publisher_refresh | capture | ...
    domain        TEXT NOT NULL,
    root_domain   TEXT,
    url_normalized TEXT NOT NULL,
    technique     TEXT NOT NULL,       -- FK acquisition_techniques.key
    rung          INTEGER,             -- 1-based position in the sequence tried for this URL
    ok            INTEGER NOT NULL,    -- a body came back that was not a recognised block
    reason        TEXT,                -- failure class when not ok (docs §4)
    detail        TEXT,                -- the free-text phrase behind the class
    status_code   INTEGER,
    bytes         INTEGER,
    ms            INTEGER,
    cost_units    REAL NOT NULL DEFAULT 0,
    gate          TEXT,                -- jsonld | phrase | trust | none  (back-filled)
    gate_score    REAL,
    usable        INTEGER,             -- passed the structure gate (back-filled); NULL = not judged
    saved         INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);
CREATE INDEX IF NOT EXISTS idx_acq_domain_ts ON acquisition_attempts(domain, ts);
CREATE INDEX IF NOT EXISTS idx_acq_url ON acquisition_attempts(url_normalized, ts);
CREATE INDEX IF NOT EXISTS idx_acq_job ON acquisition_attempts(job_id);
"""

_SECTION = re.compile(r"^## \d+\. `([a-z_]+)` — (.+?)\s*$", re.M)


def _parse_doc(text: str) -> list:
    """[{key, label, description, cost_note, latency_note, failure_classes}]
    from the techniques doc. Section = from its heading to the next '## '."""
    out = []
    heads = list(_SECTION.finditer(text))
    for i, m in enumerate(heads):
        key, label = m.group(1), m.group(2).strip()
        end = heads[i + 1].start() if i + 1 < len(heads) else text.find("\n## 8.")
        if end < 0:
            end = len(text)
        body = text[m.end():end].strip()
        cost = re.search(r"\*\*Cost\.\*\*\s*(.+?)(?:\s\*\*Typical latency\.\*\*|\n\n)", body, re.S)
        lat = re.search(r"\*\*Typical latency\.\*\*\s*(.+?)(?:\n\n|$)", body, re.S)
        fails = re.search(r"\*\*How it fails\.\*\*\s*(.+?)(?:\n\n|$)", body, re.S)
        classes = re.findall(r"`([a-z]+:[a-z0-9-]+|signed-out|challenge|no-recipe|error)`", fails.group(1)) if fails else []
        out.append({"key": key, "label": label, "description": body,
                    "cost_note": (cost.group(1).strip() if cost else ""),
                    "latency_note": (lat.group(1).strip() if lat else ""),
                    "failure_classes": sorted(set(classes))})
    return out


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_TABLES)
    seed_techniques(conn)
    conn.commit()


def seed_techniques(conn: sqlite3.Connection, *, force: bool = False) -> int:
    """Seed/refresh the registry from the doc. A row an admin has edited in
    place (updated_at newer than the seed) is left alone unless `force`.
    Returns rows written."""
    if not os.path.exists(TECHNIQUES_DOC):
        return 0
    stamp = f"{os.path.relpath(TECHNIQUES_DOC, _ROOT)}@{int(os.path.getmtime(TECHNIQUES_DOC))}"
    text = open(TECHNIQUES_DOC, encoding="utf-8").read()
    rows = _parse_doc(text)
    written = 0
    have = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT key, seeded_from, updated_at FROM acquisition_techniques")}
    for i, t in enumerate(rows):
        prior = have.get(t["key"])
        if prior and not force:
            if prior[0] == stamp:
                continue                               # already seeded from this doc version
            if prior[1]:
                continue                               # admin edited the row in place — theirs wins
        conn.execute(
            "INSERT INTO acquisition_techniques(key, label, description, cost_note, latency_note, "
            "failure_classes, enabled, sort, seeded_from, updated_at) VALUES(?,?,?,?,?,?,?,?,?,NULL) "
            "ON CONFLICT(key) DO UPDATE SET label=excluded.label, description=excluded.description, "
            "cost_note=excluded.cost_note, latency_note=excluded.latency_note, "
            "failure_classes=excluded.failure_classes, sort=excluded.sort, seeded_from=excluded.seeded_from",
            (t["key"], t["label"], t["description"], t["cost_note"], t["latency_note"],
             json.dumps(t["failure_classes"]), 1, i, stamp))
        written += 1
    conn.commit()
    return written


def list_techniques(conn: sqlite3.Connection) -> list:
    ensure_tables(conn)
    cur = conn.execute("SELECT * FROM acquisition_techniques ORDER BY sort")
    cols = [c[0] for c in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        try:
            d["failure_classes"] = json.loads(d.get("failure_classes") or "[]")
        except Exception:
            d["failure_classes"] = []
        out.append(d)
    return out


def update_technique(conn: sqlite3.Connection, key: str, patch: dict) -> bool:
    sets, vals = [], []
    for f in ("label", "description", "cost_note", "latency_note", "enabled"):
        if f in patch:
            sets.append(f"{f} = ?")
            vals.append(int(bool(patch[f])) if f == "enabled" else patch[f])
    if not sets:
        return False
    sets.append("updated_at = ?")
    vals += [_now(), key]
    cur = conn.execute(f"UPDATE acquisition_techniques SET {', '.join(sets)} WHERE key = ?", vals)
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------ the ledger
_ensured = False


def _ensure_once(conn: sqlite3.Connection) -> None:
    global _ensured
    if not _ensured:
        ensure_tables(conn)
        _ensured = True


def record(url: str, technique: str, *, ok: bool, rung: int = 1, reason: str = "",
           status_code: Optional[int] = None, nbytes: Optional[int] = None,
           ms: Optional[int] = None, cost_units: Optional[float] = None,
           notes: str = "", stamp_domain: bool = True) -> Optional[int]:
    """Write one attempt row. Never raises — a ledger failure must not cost a
    fetch. `reason` is free text; it is classified here and both are kept."""
    try:
        if technique not in TECHNIQUES:
            technique = "direct" if technique == "direct" else technique
        ctx = context()
        host = _host(url)
        cls = "" if ok else classify(reason, status_code)
        cost = COST_UNITS.get(technique, 0.0) if cost_units is None else float(cost_units)
        conn = _connect()
        try:
            _ensure_once(conn)
            cur = conn.execute(
                "INSERT INTO acquisition_attempts(ts, job_id, run_kind, domain, root_domain, "
                "url_normalized, technique, rung, ok, reason, detail, status_code, bytes, ms, "
                "cost_units, notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), ctx.get("job_id"), ctx.get("run_kind") or "", host, _root(host),
                 norm_url(url), technique, rung, 1 if ok else 0, cls, (reason or "")[:300],
                 status_code, nbytes, ms, cost, notes[:300] if notes else None))
            rid = cur.lastrowid
            if ok and stamp_domain:
                _stamp_domain(conn, host, technique)
            conn.commit()
            return rid
        finally:
            conn.close()
    except Exception as e:                                       # pragma: no cover
        print(f"[ACQ] ledger write skipped: {type(e).__name__}: {e}")
        return None


def _stamp_domain(conn: sqlite3.Connection, host: str, technique: str) -> None:
    """The domain remembers how its pages were last obtained (curator ask,
    2026-09-10: the technique must be visible on the domain record and form).
    Stored, not derived; only for a domain we already know."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(domains)")}
        if "acquire_last_technique" not in cols:
            return
        conn.execute("UPDATE domains SET acquire_last_technique = ?, acquire_last_at = ? "
                     "WHERE domain = ?", (technique, _now(), host))
    except Exception:
        pass


def gate(url: str, *, gate: str, score: Optional[float], usable: bool) -> None:
    """Back-fill the structure verdict onto the most recent OK attempt for this
    URL (same job when one is set). `usable` is the strict success."""
    try:
        ctx = context()
        conn = _connect()
        try:
            _ensure_once(conn)
            if ctx.get("job_id"):
                row = conn.execute(
                    "SELECT id FROM acquisition_attempts WHERE url_normalized = ? AND job_id = ? "
                    "AND ok = 1 ORDER BY id DESC LIMIT 1", (norm_url(url), ctx["job_id"])).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM acquisition_attempts WHERE url_normalized = ? AND ok = 1 "
                    "AND ts >= ? ORDER BY id DESC LIMIT 1",
                    (norm_url(url), datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat())).fetchone()
            if row:
                conn.execute("UPDATE acquisition_attempts SET gate = ?, gate_score = ?, usable = ? "
                             "WHERE id = ?", (gate, score, 1 if usable else 0, row[0]))
                conn.commit()
        finally:
            conn.close()
    except Exception as e:                                       # pragma: no cover
        print(f"[ACQ] gate back-fill skipped: {type(e).__name__}: {e}")


def mark_saved(url: str, conn: Optional[sqlite3.Connection] = None) -> None:
    """A recipe was saved from this URL: flip `saved` on its latest OK attempt
    (within a day — a bookmarklet save hours after a harvest still counts).

    `conn`: the SAVE's own connection when called from inside its transaction.
    Found 2026-09-10 (job 1928): opening a second connection here while the
    save's connection held its uncommitted INSERT made this wait the full
    30 s busy timeout, fail with "database is locked", and cost EVERY save on
    every path 30 s — the flip never landed either. On a borrowed connection
    the UPDATE rides the caller's transaction and commits with the recipe."""
    try:
        own = conn is None
        if own:
            conn = _connect()
        try:
            _ensure_once(conn)
            since = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat()
            row = conn.execute(
                "SELECT id FROM acquisition_attempts WHERE url_normalized = ? AND ok = 1 AND ts >= ? "
                "ORDER BY id DESC LIMIT 1", (norm_url(url), since)).fetchone()
            if row:
                conn.execute("UPDATE acquisition_attempts SET saved = 1, usable = 1 WHERE id = ?", (row[0],))
                if own:
                    conn.commit()
        finally:
            if own:
                conn.close()
    except Exception as e:                                       # pragma: no cover
        print(f"[ACQ] saved flip skipped: {type(e).__name__}: {e}")


class timed:
    """`with acquisition.timed() as t: ...; t.ms` — wall-clock for one rung."""
    def __enter__(self):
        self._t0 = time.perf_counter()
        self.ms = 0
        return self

    def __exit__(self, *exc):
        self.ms = int((time.perf_counter() - self._t0) * 1000)
        return False


# ------------------------------------------------------------------ report
def report(conn: sqlite3.Connection, *, days: int = 90, domain: Optional[str] = None) -> dict:
    """Per technique, and per domain×technique: attempts, ok rate, usable
    rate, mean cost, mean ms, cost-to-first-usable (Σcost / usable). This is
    the BEFORE measurement; phase 4 derives the policy from the same numbers."""
    ensure_tables(conn)
    since = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).isoformat()
    where = "ts >= ?" + (" AND domain = ?" if domain else "")
    args = (since, _host(domain)) if domain else (since,)
    q = ("SELECT {grp} technique, COUNT(*) n, SUM(ok) ok_n, SUM(COALESCE(usable,0)) usable_n, "
         "SUM(saved) saved_n, SUM(cost_units) cost, AVG(ms) ms, "
         "SUM(CASE WHEN reason='' OR reason IS NULL THEN 0 ELSE 1 END) fails "
         "FROM acquisition_attempts WHERE " + where + " GROUP BY {grp} technique ORDER BY {grp} technique")

    def rows(grp):
        cur = conn.execute(q.format(grp=grp), args)
        cols = [c[0] for c in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["ok_rate"] = round(d["ok_n"] / d["n"], 3) if d["n"] else None
            d["usable_rate"] = round(d["usable_n"] / d["n"], 3) if d["n"] else None
            d["cost_per_usable"] = round(d["cost"] / d["usable_n"], 2) if d["usable_n"] else None
            d["ms"] = int(d["ms"]) if d["ms"] is not None else None
            out.append(d)
        return out

    reasons = conn.execute(
        "SELECT technique, reason, COUNT(*) n FROM acquisition_attempts WHERE " + where +
        " AND ok = 0 GROUP BY technique, reason ORDER BY n DESC LIMIT 40", args).fetchall()
    return {"days": days, "domain": _host(domain) if domain else None,
            "by_technique": rows(""), "by_domain": rows("domain,"),
            "failure_classes": [{"technique": t, "reason": r, "n": n} for t, r, n in reasons]}


def print_report(rep: dict) -> None:
    print(f"[ACQ] acquisition report — last {rep['days']} days"
          + (f" — {rep['domain']}" if rep.get("domain") else ""))
    print(f"  {'technique':18s} {'n':>5s} {'ok':>6s} {'usable':>7s} {'saved':>6s} {'cost':>7s} {'$/usable':>9s} {'ms':>7s}")
    for d in rep["by_technique"]:
        print(f"  {d['technique']:18s} {d['n']:>5d} {d['ok_rate'] if d['ok_rate'] is not None else '-':>6} "
              f"{d['usable_rate'] if d['usable_rate'] is not None else '-':>7} {d['saved_n']:>6d} "
              f"{d['cost']:>7.1f} {d['cost_per_usable'] if d['cost_per_usable'] is not None else '-':>9} "
              f"{d['ms'] if d['ms'] is not None else '-':>7}")
    if rep["failure_classes"]:
        print("  failure classes:")
        for f in rep["failure_classes"][:15]:
            print(f"    {f['technique']:18s} {f['reason'] or 'other':18s} {f['n']}")
    doms = rep["by_domain"]
    if doms:
        print(f"  domains with attempts: {len({d['domain'] for d in doms})}")
