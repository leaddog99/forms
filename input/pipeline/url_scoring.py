# URL-keyed metadata: Moz scoring + first-seen / last-accessed tracking.
# Backed by the `metabase_url` SQLite table. URLs are normalized before any
# read or write so the table key stays canonical.

import base64
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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


# Query keys that are TRACKING, never page identity. Deliberately an ALLOW-LIST:
# plenty of sites use the query string as the page's real address (WordPress
# `?p=123`, `?recipeId=`), and stripping one of those would ask Moz about a
# DIFFERENT page — usually a listing or the homepage, which scores far higher.
# An unknown key is therefore always kept.
_TRACKING_KEYS = {
    "fbclid", "gclid", "msclkid", "dclid", "yclid", "igshid", "mc_cid", "mc_eid",
    "ref", "ref_src", "ref_url", "referrer", "source", "share", "shared",
    # nytimes share links: ?smid=ck-recipe-iOS-share&unlocked_article_code=…
    "smid", "unlocked_article_code", "sgrp", "algo", "impression_id",
    # patreon "instant access" grants — an access token, not the page's address
    "token", "amp",
}
_TRACKING_PREFIXES = ("utm_",)


def _canonical_form(url: str) -> Optional[str]:
    """The URL with MOBILE/AMP/TRACKING decoration removed, or None when the
    input is already canonical (nothing to add).

    Moz indexes the canonical page. We store whatever the bookmarklet grabbed,
    which on a phone is a mobile path and from a share sheet carries tracking
    params — so we were asking Moz about strings it has never seen and reading
    the empty answer as "this page has no authority". Measured 2026-08-06, all
    three recovered immediately in canonical form:

        cooking.nytimes.com/recipes/1025200-…?unlocked_article_code=…  NO DATA
        cooking.nytimes.com/recipes/1025200-…                          pa=57 da=95
        williams-sonoma.com/m/recipe/stanley-tucci-chicken-cacciatore  NO DATA
        williams-sonoma.com/recipe/stanley-tucci-chicken-cacciatore    pa=41 da=82
        travel-gourmet.com/…/stufato-di-pesce-italian-fish-stew/amp    NO DATA
        travel-gourmet.com/…/stufato-di-pesce-italian-fish-stew/       pa=21 da=30

    This is only ever used to ADD a candidate; the stored URL is untouched, and
    `url_normalized` (the dedup + cache key) is deliberately not involved.
    """
    try:
        p = urlparse(url)
        path, query = p.path or "/", p.query
        # /amp suffix or an /amp/ segment — same document, AMP rendering.
        if path.rstrip("/").endswith("/amp"):
            path = path.rstrip("/")[: -len("/amp")] or "/"
        path = path.replace("/amp/", "/")
        # LEADING /m/ only. A deeper /m/ is a real path segment on plenty of
        # sites, and rewriting it would point at some other page entirely.
        if path.startswith("/m/"):
            path = path[2:]
        if query:
            kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
                    if k.lower() not in _TRACKING_KEYS
                    and not k.lower().startswith(_TRACKING_PREFIXES)]
            query = urlencode(kept)
        cleaned = urlunparse((p.scheme, p.netloc, path, p.params, query, ""))
        if cleaned == url or not cleaned:
            return None
        # NEVER fall back to the site root. Stripping decoration off a page and
        # landing on "/" means we guessed wrong about what was decoration, and
        # the homepage's much higher PA would be silently attributed to a recipe.
        if (urlparse(cleaned).path or "/") in ("", "/"):
            return None
        return cleaned
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
    # The stored URL first, then its canonical form when they differ. Only a
    # DECORATED url (mobile path / amp / tracking params) adds anything here, so
    # an ordinary url still probes the same 4 variants and costs the same Moz
    # rows; canonical-variant learning then collapses the domain to 1.
    bases = [url]
    canon = _canonical_form(url)
    if canon:
        bases.append(canon)
    for base in bases:
        try:
            p = urlparse(base)
            host = (p.netloc or "").lower()
            if not host:
                continue
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


def _moz_lookup(url: str) -> tuple[Optional[dict], Optional[int]]:
    """The shared Moz probe. Returns (scores, http_code).

    `scores` is None whenever there is no score to hand out — including the
    case where Moz answered but has no data for the URL. `http_code` is what
    Moz reported for the chosen variant, and is meaningful EVEN WHEN scores is
    None: 0 means "never crawled", which is the whole point of the provenance
    flag. It is None only when we never got an answer at all (no creds,
    network, non-200 from the API).

    Two public wrappers sit on this: `score_url_via_moz` (the score, gated) and
    `moz_http_status` (the code, for provenance/audit). They are kept separate
    on purpose — a single function that returned a dict for an uncrawled URL is
    exactly the bug documented at the gate below, and no caller should be able
    to opt back into it.
    """
    if not url:
        return None, None
    if not MOZ_ACCESS_ID or not MOZ_SECRET_KEY:
        logger.info("Moz creds missing — skipping scoring for %s", url)
        return None, None

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
        return None, None

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
        # ANY non-zero code means Moz HAS data for that variant, and such a
        # variant must outrank a code-0 one even when PA ties.
        #
        # This tier list was the (200,301,302,402) allow-list all over again.
        # Those four rank the KNOWN tiers; they are not the definition of "has
        # data", and Moz returns others — 1, 3 and 5 all observed with real
        # metrics. A non-standard code fell through to the generic `paired`
        # pool, where `max(..., key=PA)` broke a tie by list order and could
        # hand back a code-0 variant, which then failed the usable gate below
        # and the URL was reported as having no data at all.
        #
        # Seen on travel-gourmet.com/…/stufato-di-pesce-italian-fish-stew/,
        # where all 8 variants returned pa=21 and only the trailing-slash form
        # carried code=3: the /amp form won the tie and the page scored nothing.
        has_data = [(u, r) for u, r in paired if r.get("http_code")]
        pool = crawled or estimated or has_data or paired
        if not pool:
            return None, None, False
        cu, cr = max(pool, key=lambda ur: ur[1].get("page_authority") or 0)
        # ANY non-zero http_code means Moz HAS data for this variant. 0 is its
        # "never crawled / nothing here" sentinel, and that is the only thing
        # this gate may reject.
        #
        # This was briefly an allow-list of (200, 301, 302, 402) and that was
        # wrong: those four codes exist above to RANK variants (prefer crawled
        # over estimated), never to reject them. Moz also returns other real
        # codes — pinchofyum.com comes back http_code 5 with pa 49 / da 75, plain
        # measured data — and the allow-list discarded every one of them. A
        # publisher_refresh over pinchofyum billed 496 Moz rows and scored 0 URLs,
        # with the canonical-variant learning saving nothing, because learning
        # only records from a probe this call considers usable.
        code = cr.get("http_code")
        usable = bool(code)
        return cr, cu, usable

    chosen, chosen_url, usable = _pick(candidates, results)

    # Self-heal: a learned single-variant probe that comes back with NO real Moz data
    # (http_code 0) means the learned pattern is stale/wrong for this URL — re-expand to
    # all variants + re-learn.
    if pattern and not usable:
        candidates = _url_variants(url)
        results = _moz_url_metrics(candidates, auth)
        if results is None:
            return None, None
        chosen, chosen_url, usable = _pick(candidates, results)
        pattern = None   # treat as an unlearned call below so we (re)learn from the probe

    if chosen is None:
        return None, None

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
    code = int(chosen.get("http_code") or 0)
    if not usable:
        logger.info("Moz has no data for %s (http_code=%s) — dropping, not scoring",
                    chosen_url or url, code)
        _note_moz_uncrawled()
        return None, code

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
        # PROVENANCE. Stored alongside the score so a PA is self-describing:
        # a reader can tell a measured value from a placeholder without
        # re-querying Moz. See moz_http_status() for the three states.
        "moz_http_code": code,
    }, code


def score_url_via_moz(url: str) -> Optional[dict]:
    """Call the Moz URL Metrics API for a single URL. Returns None on any
    failure (missing creds, network, non-200) AND whenever Moz has no data for
    the URL. Never raises.

    Internally queries both www and non-www variants in one batched call
    and returns the score for the variant Moz has actually crawled
    (http_code != 0). Falls back to the higher PA if neither is crawled.
    """
    return _moz_lookup(url)[0]


def moz_http_status(url: str) -> Optional[int]:
    """Moz's http_code for a URL, WITHOUT producing a score. The provenance probe.

    Three states, and they are the vocabulary the `moz_http_code` column speaks:

        None  we never got an answer (no creds / network / API error) — unknown
        0     Moz answered and has NO data: any PA on this row is the
              domain-derived PLACEHOLDER, i.e. fabricated
        >0    Moz has real data; the PA was measured

    Costs the same Moz rows as scoring, so use it deliberately. It exists for
    auditing rows scored before 2026-08-04, when the gate below was missing and
    placeholders were written as if measured.
    """
    return _moz_lookup(url)[1]


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
            moz_http_code INTEGER,
            first_seen TEXT NOT NULL,
            last_accessed TEXT NOT NULL
        )
        """
    )
    # Provenance for the PA above. NULL on every row written before 2026-08-04,
    # which is exactly the "unverified" worklist — see moz_http_status().
    try:
        conn.execute("ALTER TABLE metabase_url ADD COLUMN moz_http_code INTEGER")
    except sqlite3.OperationalError:
        pass  # already present


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
            moz_last_scored = ?,
            moz_http_code = ?
        WHERE url = ?
        """,
        (
            scores["page_authority"],
            scores["domain_authority"],
            scores["ou_score"],
            scores["raw_title"], scores["raw_title"],
            now_iso,
            scores.get("moz_http_code"),
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


# ── corpus calibration for the editorial commentary ─────────────────────────
# The scoreCommentary block was handed raw numbers and no reference, so the
# model judged them against its own prior — "the scale runs to 100, so 65 is
# middling" — and called a DA-65 publisher a "marginal site". Measured
# 2026-08-09 over 4,769 scored master rows, DA 65 is the EXACT MEDIAN of the
# corpus (p10 33 · p25 47 · median 64 · p75 82 · p90 92) and the 74th percentile
# of curated publishers. "Marginal" was wrong by any reading available.
#
# That is a calibration bug, not a wording bug: tightening the prose alone just
# moves the error. So the prompt gets the percentile, not just the number.
#
# NOTE the reference population. This is a SELECTED corpus — harvest selects on
# traffic, then OU ranks within that pool — so these are NOT web-wide norms.
# A DA is being placed among recipes we already judged worth keeping, which is
# the right frame for commentary about our own shelf and the wrong one for a
# general claim about the web.
_AUTH_BANDS = (
    (33, "bottom tenth of our corpus — genuinely small / obscure"),
    (47, "small publisher (10-25th percentile)"),
    (64, "modest, mid-sized (25-50th)"),
    (82, "well-established (50-75th)"),
    (92, "major publisher (75-90th)"),
    (1e9, "household name (top tenth)"),
)

_CORPUS_DA_CACHE: dict = {}


def _corpus_da_values(db_path: str = "recipes.db") -> list:
    """Sorted DA values across the corpus, cached per process.

    Cached because this feeds a per-recipe prompt: re-reading ~4.8k rows on
    every enrich would be a silly cost for a distribution that moves slowly.
    A restart picks up drift, which is often enough for calibration bands.
    """
    import sqlite3
    key = str(db_path)
    if key not in _CORPUS_DA_CACHE:
        try:
            with sqlite3.connect(key, timeout=5) as conn:
                rows = conn.execute(
                    "SELECT domain_authority FROM master_recipes "
                    "WHERE domain_authority IS NOT NULL"
                ).fetchall()
            _CORPUS_DA_CACHE[key] = sorted(float(r[0]) for r in rows)
        except Exception:
            _CORPUS_DA_CACHE[key] = []
    return _CORPUS_DA_CACHE[key]


def authority_corpus_context(da, db_path: str = "recipes.db") -> dict:
    """Where a DA sits IN OUR CORPUS: {pct, band, n}. Empty dict when unknown.

    Best-effort — a missing DB or an unscored corpus returns {} and the caller
    simply omits the calibration line rather than failing an enrichment.
    """
    try:
        da = float(da)
    except (TypeError, ValueError):
        return {}
    vals = _corpus_da_values(db_path)
    if not vals:
        return {}
    below = sum(1 for v in vals if v < da)
    pct = round(100.0 * below / len(vals), 1)
    band = next(label for edge, label in _AUTH_BANDS if da < edge)
    return {"pct": pct, "band": band, "n": len(vals)}


# ── paywall PA-remap, for DISPLAY surfaces ──────────────────────────────────
# The selectors already remap: the dish path via build_query_batch.
# _apply_paywall_remap, the publisher path via domain_scoring.score_members.
# The recipe FORM did not — it reads recipe._scoring, which carries the RAW pa
# and an ouScore computed from it. So a gated publisher showed as under-
# performing on a page the selector had actually rated well: measured
# 2026-08-09, an ATK recipe displayed PA 36 / OU -5.0 while selection scored the
# same page at an adjusted PA 50.9 / OU +10.0. A 15-point disagreement, and the
# form's version is the one a curator reads — and the one scoreCommentary was
# writing its verdict from.
#
# DERIVED, NEVER STORED. The calibration is a snapshot of two moving
# distributions and is re-run monthly (the paid_pa_calibration job), so a stored
# adjustedPageAuthority would go stale exactly the way the calibration itself
# just did. Computing on read means the form always reflects the CURRENT
# calibration. Cached per process; a restart picks up a re-calibration.
_PAYWALL_CAL_CACHE: dict = {}


def _paywall_cals(db_path: str = "recipes.db") -> dict:
    key = str(db_path)
    if key not in _PAYWALL_CAL_CACHE:
        try:
            import sqlite3
            from input.pipeline import domains_lib
            with sqlite3.connect(key, timeout=5) as conn:
                cals = domains_lib.get_paywall_calibrations(conn)
            _PAYWALL_CAL_CACHE[key] = {
                (c.get("domain") or "").lower().replace("www.", ""): c
                for c in cals if c.get("pa_std") and c["pa_std"] > 0
            }
        except Exception:
            _PAYWALL_CAL_CACHE[key] = {}
    return _PAYWALL_CAL_CACHE[key]


def adjusted_pa_for(host: str, pa, db_path: str = "recipes.db"):
    """Free-equivalent PA for a gated publisher's page, or None when no remap applies.

    Returns None — not the raw pa — when the publisher isn't calibrated, so a
    caller can tell "no remap" from "remap that happened to change nothing"
    (memory/feedback_absent_not_zero). One-directional, matching every other
    consumer: `max(pa, free_mean + (pa - paid_mean) * (free_std/paid_std))`.
    """
    try:
        pa = float(pa)
    except (TypeError, ValueError):
        return None
    h = (host or "").lower().replace("www.", "")
    c = _paywall_cals(db_path).get(h)
    if not c:
        return None
    try:
        ps = float(c["pa_std"])
        if ps <= 0:
            return None
        adj = float(c["free_mean"]) + (pa - float(c["pa_mean"])) * (float(c["free_std"]) / ps)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return round(max(pa, adj), 1)
