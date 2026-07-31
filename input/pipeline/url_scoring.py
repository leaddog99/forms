# URL-keyed metadata: Moz scoring + first-seen / last-accessed tracking.
# Backed by the `metabase_url` SQLite table. URLs are normalized before any
# read or write so the table key stays canonical.

import base64
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests
from dotenv import load_dotenv

from input.pipeline.url_utils import normalize_url, root_domain

load_dotenv()
logger = logging.getLogger("pipeline.url_scoring")

MOZ_ACCESS_ID = os.getenv("MOZ_ACCESS_ID")
MOZ_SECRET_KEY = os.getenv("MOZ_SECRET_KEY")
MOZ_API_URL = "https://lsapi.seomoz.com/v2/url_metrics"
MOZ_TIMEOUT_SECONDS = 8
# Default TTL for Moz scores. Save-time refresh kicks in when a metabase_url
# row is older than this (matches the CLI script's --days default so manual
# and interactive paths agree on what "stale" means).
MOZ_REFRESH_TTL_DAYS = 30

# --- Per-domain canonical-variant learning (Moz ROW reduction) ---------------
# Moz bills PER TARGET URL in the batch. score_url_via_moz probes up to 4 variants
# (www/non-www × trailing-slash) per URL to find the CANONICAL form — the one the
# site actually serves, which concentrates the link graph; the others score ~15 PA
# points lower. That's 4 Moz rows per URL. But the canonical pattern is CONSTANT per
# domain, so we learn it ONCE (first URL of a domain does the full 4-variant probe)
# and then query only that single variant for the rest of the domain's URLs = 1 row.
# ~4x fewer Moz rows on the dominant case (a publisher harvest is all one domain).
# Self-healing: if a learned single-variant probe returns un-crawled, we re-expand +
# re-learn. Kill switch: system_config `moz_canonical_learning` (default on).
_CANON: Optional[dict] = None   # domain -> (use_www: bool, trailing_slash: bool); None = unloaded

# Moz ROW meter (per-process) — every url_metrics target = 1 billed row. Lets a harvest
# report actual rows spent + what the canonical-variant learning saved. reset per batch.
_MOZ_ROWS = 0
_MOZ_CALLS = 0
# URLs dropped because Moz has no data for them (http_code 0) — counted apart from
# credential/network failures, which drop URLs for the same "moz-unavailable" reason
# but mean something entirely different. 23% of one run being uncrawled is a fact
# about the publisher; 23% being unreachable is an outage.
_MOZ_UNCRAWLED = 0


def reset_moz_row_stats() -> None:
    global _MOZ_ROWS, _MOZ_CALLS, _MOZ_UNCRAWLED
    _MOZ_ROWS = 0
    _MOZ_CALLS = 0
    _MOZ_UNCRAWLED = 0


def _note_moz_uncrawled() -> None:
    global _MOZ_UNCRAWLED
    _MOZ_UNCRAWLED += 1


def moz_row_stats() -> dict:
    """(rows, calls, urls_scored). `rows` = billed Moz targets; `calls` = scored URLs.
    `saved` = rows the canonical learning avoided vs the old flat 4-variant probe."""
    return {"rows": _MOZ_ROWS, "calls": _MOZ_CALLS,
            "uncrawled": _MOZ_UNCRAWLED,
            "saved_vs_4x": max(0, _MOZ_CALLS * 4 - _MOZ_ROWS)}


def _compute_ou(pa: float, da: float) -> Optional[float]:
    """Opportunity score: derived from Moz PA and DA. Lifted from the batch
    pipeline so scores stay comparable across batch and interactive flows."""
    try:
        return round(-3.0273 * (da ** 0.6034) + pa, 3)
    except Exception:
        return None


def _url_variants(url: str) -> list[str]:
    """Return all reasonable URL variants Moz might score differently:
    {host, alt-host (www-toggled)} × {path, alt-path (trailing-slash-toggled)}.

    Moz scores variants independently — the canonical (the form the
    site actually serves) gets the real PA, others get an alternate /
    estimated PA. Empirically, for many sites the trailing-slash form
    is canonical and the slash-stripped form scores ~15 points lower
    PA. `normalize_url` (used internally for cache keys) strips the
    trailing slash, so we MUST re-add it as a candidate here or we'll
    cache the under-scored variant. `score_url_via_moz` picks the
    highest-PA crawled result, so adding more variants only helps.
    """
    out = [url]
    seen = {url}
    try:
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if not host:
            return out
        # Host toggle
        alt_host = host[4:] if host.startswith("www.") else "www." + host
        # Path toggle (trailing slash)
        path = p.path or "/"
        if path.endswith("/") and len(path) > 1:
            alt_path = path.rstrip("/")
        else:
            alt_path = path + "/" if path else "/"
        for h in (host, alt_host):
            for pa in (path, alt_path):
                v = urlunparse((p.scheme, h, pa, p.params, p.query, p.fragment))
                if v not in seen:
                    seen.add(v)
                    out.append(v)
    except Exception:
        pass
    return out


def _canon_learning_on() -> bool:
    """Kill switch for the per-domain canonical-variant learning (default ON)."""
    try:
        from input.pipeline import system_config
        return bool(system_config.get_setting("moz_canonical_learning", True))
    except Exception:
        return True


def _canon_domain(url: str) -> str:
    """Domain key for the canonical cache: lowercased host, www stripped (so www.x.com
    and x.com share one learned pattern)."""
    try:
        host = (urlparse(url).netloc or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _canon_load() -> None:
    """Lazily load the learned per-domain canonical patterns into the process cache."""
    global _CANON
    if _CANON is not None:
        return
    _CANON = {}
    try:
        from input.pipeline import db
        conn = db.connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS moz_domain_canonical ("
                "domain TEXT PRIMARY KEY, use_www INTEGER NOT NULL, "
                "trailing_slash INTEGER NOT NULL, learned_at TEXT NOT NULL)")
            conn.commit()
            for dom, w, s in conn.execute(
                    "SELECT domain, use_www, trailing_slash FROM moz_domain_canonical"):
                _CANON[dom] = (bool(w), bool(s))
        finally:
            conn.close()
    except Exception as e:   # DB unreachable → in-process-only learning still works
        logger.info("moz canonical cache load skipped: %s", e)


def _canonical_variant(url: str, pattern: tuple) -> str:
    """Rebuild `url` into the domain's learned canonical form (use_www, trailing_slash)."""
    use_www, trailing = pattern
    p = urlparse(url)
    host = (p.netloc or "").lower()
    base = host[4:] if host.startswith("www.") else host
    host2 = ("www." + base) if use_www else base
    path = p.path or "/"
    if trailing:
        path2 = path if path.endswith("/") else path + "/"
    else:
        path2 = path.rstrip("/") if len(path) > 1 else path
    return urlunparse((p.scheme, host2, path2, p.params, p.query, p.fragment))


def _canon_learn(url: str, crawled_url: str) -> None:
    """Record the canonical (www?, trailing-slash?) pattern for this URL's domain,
    derived from the variant Moz actually crawled. Persists + updates the cache."""
    global _CANON
    dom = _canon_domain(url)
    if not dom or not crawled_url:
        return
    try:
        p = urlparse(crawled_url)
        host = (p.netloc or "").lower()
        use_www = host.startswith("www.")
        path = p.path or "/"
        trailing = path.endswith("/") and len(path) > 1
    except Exception:
        return
    if _CANON is None:
        _CANON = {}
    _CANON[dom] = (use_www, trailing)
    try:
        from input.pipeline import db
        conn = db.connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO moz_domain_canonical "
                "(domain, use_www, trailing_slash, learned_at) VALUES (?,?,?,?)",
                (dom, int(use_www), int(trailing),
                 datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass   # in-process cache still holds it for the rest of this run


def _moz_url_metrics(candidates: list[str], auth: str) -> Optional[list]:
    """One Moz url_metrics call for the given target URLs. Returns the results list
    (Moz preserves target order) or None on failure."""
    global _MOZ_ROWS, _MOZ_CALLS
    _MOZ_ROWS += len(candidates)   # every target = 1 billed row
    _MOZ_CALLS += 1
    try:
        resp = requests.post(
            MOZ_API_URL,
            headers={"Authorization": "Basic " + auth},
            json={"targets": candidates,
                  "metrics": ["title", "page_authority", "domain_authority", "http_code"]},
            timeout=MOZ_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return resp.json().get("results") or []
    except Exception as e:
        logger.warning("Moz scoring failed for %s: %s", candidates[:1], e)
        return None


def score_url_via_moz(url: str) -> Optional[dict]:
    """Call the Moz URL Metrics API for a single URL. Returns None on any
    failure (missing creds, network, non-200). Never raises.

    Internally queries both www and non-www variants in one batched call
    and returns the score for the variant Moz has actually crawled
    (http_code != 0). Falls back to the higher PA if neither is crawled.
    """
    if not url:
        return None
    if not MOZ_ACCESS_ID or not MOZ_SECRET_KEY:
        logger.info("Moz creds missing — skipping scoring for %s", url)
        return None

    auth = base64.b64encode(f"{MOZ_ACCESS_ID}:{MOZ_SECRET_KEY}".encode()).decode()

    # Per-domain canonical learning: if we've learned this domain's canonical form,
    # query ONLY that variant (1 Moz row). Otherwise probe all ~4 variants (learn call).
    learning_on = _canon_learning_on()
    pattern = None
    if learning_on:
        _canon_load()
        pattern = _CANON.get(_canon_domain(url))
    candidates = [_canonical_variant(url, pattern)] if pattern else _url_variants(url)

    results = _moz_url_metrics(candidates, auth)
    if results is None:
        return None

    def _pick(cands, res):
        """Pair each result with the target we sent (Moz preserves order), then pick
        the best-tier crawled/estimated variant by PA. Returns (chosen_result,
        chosen_target_url, usable) where `usable` = the chosen variant has REAL Moz
        data (http_code 200/301/302 crawled OR 402 estimated — NOT 0/missing). The
        canonical form is the highest-PA variant with real data; the 0-code variants
        are the wrong/uncrawled forms that score ~15 PA lower or nothing."""
        paired = list(zip(cands, res)) if len(res) == len(cands) else [(None, r) for r in res]
        crawled = [(u, r) for u, r in paired if r.get("http_code") in (200, 301, 302)]
        estimated = [(u, r) for u, r in paired if r.get("http_code") == 402]
        pool = crawled or estimated or paired
        if not pool:
            return None, None, False
        cu, cr = max(pool, key=lambda ur: ur[1].get("page_authority") or 0)
        usable = cr.get("http_code") in (200, 301, 302, 402)
        return cr, cu, usable

    chosen, chosen_url, usable = _pick(candidates, results)

    # Self-heal: a learned single-variant probe that comes back with NO real Moz data
    # (http_code 0) means the learned pattern is stale/wrong for this URL — re-expand to
    # all variants + re-learn.
    if pattern and not usable:
        candidates = _url_variants(url)
        results = _moz_url_metrics(candidates, auth)
        if results is None:
            return None
        chosen, chosen_url, usable = _pick(candidates, results)
        pattern = None   # treat as an unlearned call below so we (re)learn from the probe

    if chosen is None:
        return None

    # NO REAL MOZ DATA => NO SCORE. `usable` was computed above and then never
    # consulted on the way out, so a URL Moz has never crawled (http_code 0) still
    # returned the domain-derived placeholder PA it ships with — and that flowed
    # into ou_score and the blend as if it had been measured. On a 2026-07-31
    # healthline harvest that was 23% of the run, every one of them handed exactly
    # pa=44, sitting indistinguishable beside the real 44s from crawled pages.
    #
    # Same rule as the _scoring.power fix (2026-07-30): a value we could not
    # compute must be ABSENT, never a number that reads as "measured, and it's
    # nothing." Callers already handle None — _moz_score drops the URL into
    # rejects, the cache path leaves any existing score intact.
    #
    # Curator's call on dropping rather than keeping-with-absent-PA: "if it can't
    # crawl them they probably aren't popular enough to matter." The absence is
    # itself the signal.
    if not usable:
        logger.info("Moz has no data for %s (http_code=%s) — dropping, not scoring",
                    chosen_url or url, chosen.get("http_code"))
        _note_moz_uncrawled()
        return None

    # Learn the domain's canonical form from the chosen (highest-PA, has-data) variant,
    # but only on a FULL probe (pattern is None) so the single-variant re-query reproduces
    # the same pick next time.
    if learning_on and pattern is None and usable and chosen_url:
        _canon_learn(url, chosen_url)

    pa = float(chosen.get("page_authority") or 0)
    da = float(chosen.get("domain_authority") or 0)
    return {
        "page_authority": pa,
        "domain_authority": da,
        "ou_score": _compute_ou(pa, da),
        "raw_title": chosen.get("title") or "",
    }


def ensure_metabase_url_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metabase_url (
            url TEXT PRIMARY KEY,
            root_domain TEXT NOT NULL DEFAULT '',
            raw_title TEXT NOT NULL DEFAULT '',
            page_authority REAL,
            domain_authority REAL,
            ou_score REAL,
            moz_last_scored TEXT,
            first_seen TEXT NOT NULL,
            last_accessed TEXT NOT NULL
        )
        """
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "url": row["url"],
        "root_domain": row["root_domain"],
        "raw_title": row["raw_title"],
        "page_authority": row["page_authority"],
        "domain_authority": row["domain_authority"],
        "ou_score": row["ou_score"],
        "moz_last_scored": row["moz_last_scored"],
        "first_seen": row["first_seen"],
        "last_accessed": row["last_accessed"],
    }


def get_metabase_url(conn: sqlite3.Connection, url: str) -> Optional[dict]:
    """Look up a metabase row for `url` (normalized). Returns None if absent."""
    norm = normalize_url(url)
    if not norm:
        return None
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def _is_moz_stale(moz_last_scored: Optional[str], days: int) -> bool:
    """True if the row has never been scored or its score is older than `days`."""
    if not moz_last_scored:
        return True
    try:
        # SQLite stores ISO-8601 strings; fromisoformat handles "+00:00" etc.
        scored_at = datetime.fromisoformat(moz_last_scored)
    except Exception:
        return True
    if scored_at.tzinfo is None:
        scored_at = scored_at.replace(tzinfo=timezone.utc)
    return scored_at < (datetime.now(timezone.utc) - timedelta(days=days))


def _apply_moz_scores(conn: sqlite3.Connection, url: str, scores: dict, now_iso: str) -> None:
    """Write scores onto a metabase_url row. Used by both create-new and
    refresh-stale paths so the UPDATE shape stays in one place."""
    conn.execute(
        """
        UPDATE metabase_url SET
            page_authority = ?,
            domain_authority = ?,
            ou_score = ?,
            raw_title = CASE WHEN ? <> '' THEN ? ELSE raw_title END,
            moz_last_scored = ?
        WHERE url = ?
        """,
        (
            scores["page_authority"],
            scores["domain_authority"],
            scores["ou_score"],
            scores["raw_title"], scores["raw_title"],
            now_iso,
            url,
        ),
    )
    conn.commit()


def get_or_create_url_metadata(
    conn: sqlite3.Connection,
    url: str,
    fallback_title: str = "",
    score_if_new: bool = True,
    refresh_if_stale_days: int = MOZ_REFRESH_TTL_DAYS,
) -> Optional[dict]:
    """
    Ensure a metabase_url row exists for `url`. If new, score via Moz when
    creds are available and `score_if_new` is true. If existing and its
    moz_last_scored is older than `refresh_if_stale_days` (or null), re-score
    inline. Always bumps last_accessed. Returns the row as a dict.
    Pass `refresh_if_stale_days=0` to disable the staleness check.
    """
    norm = normalize_url(url)
    if not norm:
        return None

    ensure_metabase_url_table(conn)
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,)).fetchone()
    if existing:
        # Bump last_accessed first so even if scoring fails the access bump lands.
        conn.execute("UPDATE metabase_url SET last_accessed = ? WHERE url = ?", (now, norm))
        conn.commit()

        if refresh_if_stale_days > 0 and _is_moz_stale(existing["moz_last_scored"], refresh_if_stale_days):
            scores = score_url_via_moz(norm)
            if scores:
                _apply_moz_scores(conn, norm, scores, now)
            # If scoring fails (creds missing, network), leave existing scores
            # intact — better stale than zeroed.
        return _row_to_dict(conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,)).fetchone())

    # New row. Insert with what we know now, then attempt Moz scoring inline.
    conn.execute(
        """
        INSERT INTO metabase_url (url, root_domain, raw_title, first_seen, last_accessed)
        VALUES (?, ?, ?, ?, ?)
        """,
        (norm, root_domain(norm), fallback_title or "", now, now),
    )
    conn.commit()

    if score_if_new:
        scores = score_url_via_moz(norm)
        if scores:
            _apply_moz_scores(conn, norm, scores, now)

    return _row_to_dict(conn.execute("SELECT * FROM metabase_url WHERE url = ?", (norm,)).fetchone())
