"""Getting a page: the fetch ladder.

    direct (free)  ->  paid unblocker (opt-in)  ->  Wayback snapshot (free)

One public entry point, `fetch_with_full_fallback(url, ...)`, which every
recipe/page fetch in the system goes through. It returns `(response, meta)`
where `meta["source"]` is "direct" | "unblocker" | "wayback" | "page-cache",
or raises when no rung produced a page.

How a URL moves through it:

  1. Page cache (only when a caller switched it on) answers without a fetch.
  2. A host this run already gave up on goes straight to Wayback.
  3. A domain the curator marked `paid_first` skips direct: unblocker, Wayback.
  4. Everything else is direct-first. A 429 pauses and retries before anything
     is paid for. A page that comes back blocked, or fails, escalates to the
     paid unblocker when the caller enabled it. A parked domain never pays.
  5. Wayback is the last rung for all of them.

What the ladder remembers is per PROCESS, so per job (`_HostRun`): how often a
host rate-limited us, whether it blocked the unblocker's exit pool, and how
many of its URLs in a row lost every live rung. Nothing here persists; the
durable record is the acquisition ledger (input/pipeline/acquisition.py), one
row per rung tried.

The per-domain answer to "how may we fetch this?" is ONE function,
`domains_lib.fetch_policy(url, run=...)`. Callers pass its `unblocker` and
`render` in; the ladder reads its `paid_first` itself. Tunable numbers are system_config
settings read through `_setting`. Tests: to_markdown/tests/test_fetch_ladder.py
replays the scenarios against a golden record with no network.
"""
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; recipe-forms/0.1; +https://example.com)"
)
# Browser-style fallback. Some sites do "normal" anti-bot — block our
# recipe-forms UA, allow real browsers. We try BOT_UA first (broadly
# accepted, including by quirky sites like thekitchn.com that actively
# 403 Chrome UAs); on failure we retry with this. Order matters: more
# sites accept the bot UA than reject it, so this minimizes wasted
# fetches.
# KEEP THIS VERSION CURRENT. It is not cosmetic: WAFs increasingly treat a
# STALE browser version as a bot signal, so a pinned UA silently rots into a
# block. memoriediangelina.com (2026-08-22) served 403 to Chrome/120 and the
# full 558KB article to Chrome/140 — same IP, same headers, same second. The
# harvest read that as "site is unfetchable", fell through to Wayback for every
# URL, and lost outright the ones with no snapshot.
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
USER_AGENT_CHAIN = [DEFAULT_USER_AGENT, FALLBACK_USER_AGENT]
DEFAULT_TIMEOUT_SECONDS = 20


def _fix_response_encoding(resp: requests.Response) -> None:
    """Override requests' ISO-8859-1 default when no charset is declared
    in the HTTP Content-Type. Without this, non-UTF-8 East-Asian pages
    (Shift-JIS, GB2312, EUC-KR) decode as mojibake even though the body
    bytes are intact — `requests` only trusts the HTTP header and falls
    back to Latin-1 when absent, which is wrong for ~every non-Latin
    script. We let chardet sniff the body when the header is silent
    or defaulted, which is the same logic `resp.text` would apply if
    we deferred encoding choice — but doing it here makes the fix
    visible across all downstream callers (BeautifulSoup, json-ld
    parser, translator) instead of only the ones that re-read .text.

    Cheap: chardet runs a few KB sample, not the full body.
    """
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "latin-1"):
        sniffed = resp.apparent_encoding
        if sniffed and sniffed.lower() not in ("ascii",):
            resp.encoding = sniffed


# --- reading a response: is this the page? -------------------------------------

# A page that carries one of these has real structure: it is the article, not
# a challenge stub or an unrendered JS shell.
_STRUCTURE_RE = re.compile(r"ld\+json|<article")


def _page_text(resp) -> Optional[tuple[str, str]]:
    """(text, lowercased text) of a response, decoded ONCE per response.

    `resp.text` re-decodes the whole body on every access, and one fetch asks
    for a verdict on the same response three or four times (the UA chain, the
    ladder, the ledger row, the page cache). On a 500KB page that was several
    megabytes of decode-and-lowercase per URL for an answer that cannot change.
    Keyed on the encoding, so a caller that corrects it gets a fresh decode.
    None when the body cannot be read at all."""
    try:
        cached = getattr(resp, "_page_text", None)
        if cached is not None and cached[0] == resp.encoding:
            return cached[1], cached[2]
        body = resp.text or ""
    except Exception:
        return None
    low = body.lower()
    try:
        resp._page_text = (resp.encoding, body, low)
    except Exception:
        pass
    return body, low


def _has_structure(resp) -> bool:
    page = _page_text(resp)
    return bool(page and _STRUCTURE_RE.search(page[1]))


# Anti-bot challenge / interstitial signatures. A site like a Hearst property
# (thepioneerwoman.com) answers a bot with HTTP **200** carrying a tiny JS-challenge
# stub instead of the page — so the fetch "succeeds" but has no content. These markers
# (+ the small-body heuristic) detect that so an unblocker-enabled domain escalates
# to the paid tier instead of returning the useless stub.
#
# TWO TIERS, because they are not equally good evidence — measured 2026-08-13:
# jamieoliver.com served a REAL 767KB recipe page (HTTP 200) that contained
# "challenge-platform", because Cloudflare injects its passive JSD probe
# (/cdn-cgi/challenge-platform/scripts/jsd/main.js) into pages it serves normally.
# Treating that as proof of a block would have discarded every Jamie Oliver recipe.

# HARD — text that appears only ON a challenge/denial page. Sufficient on its own.
_HARD_BLOCK_MARKERS = (
    "px-captcha", "pardon our interruption",
    "access to this page has been denied", "just a moment...",
    "are you a robot", "verify you are human",
)

# AMBIENT — vendor script/cookie names that ride along on SUCCESSFULLY served
# pages too. Corroborating only: meaningful when the body is also not a real page.
_AMBIENT_BLOCK_MARKERS = (
    "perimeterx", "_pxhd", "captcha-delivery", "datadome",
    "challenge-platform", "incapsula", "_incapsula_",
    "enable javascript and cookies",
)


# Thin-body thresholds. Two, because the same signal answers two questions whose
# cost of being wrong is wildly different:
#   SPEND (escalate to the paid tier?) — a false positive costs one credit, so be
#       eager. 15KB: a real recipe page with its nav/head/footer clears this easily.
#   VERDICT (refuse the response outright?) — a false positive DISCARDS A REAL
#       RECIPE, so demand near-certainty. A challenge stub is a few hundred bytes;
#       even a minimal hand-written recipe page carries >2KB of head + markup.
# Splitting them is the point: the eager number was safe only for the spend
# question, and it was never intended to decide what a page IS.
_THIN_SPEND_CHARS = 15000
_THIN_VERDICT_CHARS = 2000


# PARKED / EXPIRED domain. Not a bot challenge: the publisher is gone and a
# registrar or ad-parking lander answers every path. Measured 2026-09-11,
# aprettylifeinthesuburbs.com: every URL (homepage included) returned a
# 114-byte page whose only content was `window.location.href="/lander"`; the
# lander was an openresty ad page (X-Adblock-Key). blocked_reason() read the
# stub as a soft-block and the ladder escalated EVERY one of 44 URLs to the
# paid unblocker, which fetched the same stub. Nothing on a parked domain is
# recoverable at any price, so this is checked FIRST and never escalates.
_PARKED_REDIRECT_RE = re.compile(
    r"""(?:location(?:\.href)?\s*=\s*|http-equiv=["']refresh["'][^>]*url=)["']?/lander""", re.I)
_PARKED_MARKERS = (
    "sedoparking", "parkingcrew", "bodis.com", "hugedomains", "afternic",
    "dan.com/buy-domain", "godaddy.com/forsale", "this domain may be for sale",
    "this domain is for sale", "buy this domain", "domain is parked",
    "parked free, courtesy of", "the domain owner is offering",
)
_PARKED_MAX_CHARS = 4000   # landers and their stubs are tiny; a real page is not


def parked_reason(resp: requests.Response) -> Optional[str]:
    """WHY this response looks like a parked/expired domain — or None.

    Two signatures, both requiring a tiny body (a real article never fits):
      - a JS/meta redirect to `/lander` — the ad-parking stub pattern
      - a known registrar/parking marker in the body
    A parked verdict is TERMINAL for the paid rungs: the unblocker fetches
    the same lander. Wayback (free) may still hold the real page."""
    page = _page_text(resp)
    if page is None or len(page[0]) > _PARKED_MAX_CHARS:
        return None
    body, low = page
    if _PARKED_REDIRECT_RE.search(body):
        return f"parked domain — redirect to /lander stub ({len(body)} bytes)"
    for m in _PARKED_MARKERS:
        if m in low:
            return f"parked domain — parking marker {m!r} ({len(body)} bytes)"
    return None


def blocked_reason(resp: requests.Response, *, strict: bool = False) -> Optional[str]:
    """WHY this response looks like a challenge/interstitial rather than the page —
    or None if it looks real.

    Returns a short, specific, loggable phrase. A caller that refuses a response
    must be able to say what it saw: "fetch-failed" with no reason is the defect
    this replaces. Measured 2026-08-13, tiffycooks.com returned HTTP 202 / 213
    bytes / a captcha stub and was filed `no-recipe-structure` — a statement about
    page content we had never received.

    Two signals, reported separately because they mean different things and call
    for different remedies:
      - a KNOWN challenge marker  → the origin actively blocked us (escalate/unblocker)
      - a thin body with no structure → an interstitial or an unrendered JS shell

    `strict=True` for callers deciding a candidate's FATE (see the threshold note
    above); leave it False for callers deciding only whether to spend a credit.
    """
    page = _page_text(resp)
    if page is None:
        return None
    body, low = page
    for m in _HARD_BLOCK_MARKERS:
        if m in low:
            return f"blocked — challenge page, marker {m!r} ({len(body)} bytes)"
    limit = _THIN_VERDICT_CHARS if strict else _THIN_SPEND_CHARS
    looks_unstructured = not _STRUCTURE_RE.search(low)
    thin = len(body) < limit and looks_unstructured
    if thin:
        ambient = [m for m in _AMBIENT_BLOCK_MARKERS if m in low]
        # Name the vendor when we can — "blocked by datadome" is actionable in a
        # way "thin body" is not. The thin body is what makes it a block; the
        # ambient marker only says WHO.
        by = f", served by {ambient[0]!r}" if ambient else ""
        return (f"blocked — thin body, no JSON-LD and no <article>{by} "
                f"({len(body)} bytes < {limit}; too small to be the page)")
    if not strict and looks_unstructured and any(m in low for m in _AMBIENT_BLOCK_MARKERS):
        # SPEND path only, and only when the body ALSO lacks structure. An ambient
        # vendor marker on a structured page is Cloudflare's passive probe riding a
        # page we fully received (the jamieoliver case above) — 2026-08-28 it was
        # still escalating those: foodnouveau (611KB, full recipe JSON-LD) and
        # blog.thermoworks (357KB, same) each burned a paid unblocker credit on
        # 'challenge-platform' alone. A page with ld+json or <article> is the page;
        # never pay to re-fetch it. Never reached on the verdict path, where it
        # would be a false accusation against a page we actually received.
        hit = next(m for m in _AMBIENT_BLOCK_MARKERS if m in low)
        return f"possible anti-bot vendor {hit!r} present ({len(body)} bytes, unstructured)"
    return None


# --- settings, per-run memory, ledger ------------------------------------------

_GONE = (404, 410)      # the PAGE is missing; says nothing about the host


def _setting(key: str, default):
    """A system_config value, or `default` when the settings cannot be read
    (a fetch must never fail because a tuning knob was unreachable)."""
    try:
        from input.pipeline import system_config
        return system_config.get_setting(key, default)
    except Exception:
        return default


def _int_setting(key: str, default: int) -> int:
    try:
        return int(_setting(key, default) or 0)
    except (TypeError, ValueError):
        return default


def _host(url) -> str:
    """Lower-cased host without a leading www. — the grain all run memory is kept at."""
    from urllib.parse import urlparse
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _status_of(exc) -> Optional[int]:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None) if resp is not None else None


@dataclass
class _HostRun:
    """What this run has learned about one host. Per process, so per job; a new
    job starts with a clean slate (reset_run_state does the same for tests).

    unblocker_blocks  consecutive ORIGIN blocks relayed by the unblocker. Some
                      publishers (Dotdash / People Inc. — seriouseats) burst-block
                      the whole exit pool once a harvest ramps up, so every
                      fresh-IP retry still 402s. At `unblocker_circuit_trip` the
                      unblocker is skipped for this host; any non-block answer
                      (a 2xx, a real 404) proves the pool works and resets it.
    rate_pauses       429 pauses taken on this host (drives the doubling).
    rate_spent        one URL waited out EVERY paused retry and was still 429,
                      so later URLs do not wait again (cooks.com, 2026-11-21:
                      60s, 120s, 240s, 480s per URL, 205 of 205 direct fetches
                      429). A clean direct fetch clears it.
    live_lost         consecutive URLs that lost every LIVE rung (direct, and
                      the unblocker where it applies). At `host_giveup_after`
                      the run stops trying this host live and sends its
                      remaining URLs straight to Wayback (free, a few seconds,
                      and the only route that has ever worked on some hosts).
                      Any live success resets it.
    unblocker_timed_out  a STATIC paid fetch of this host ran out its whole
                      timeout. The site is not answering through the pool, and
                      every further paid try costs the same 70 s to learn the
                      same thing (iamgreek.nl, 2026-11-27: static, rendered and
                      static again, 3.5 minutes on one candidate page). Paid
                      fetches are skipped for the host for the rest of the run;
                      a paid fetch that gets through clears it. A RENDERED
                      fetch timing out does not set it: a render is ~3x slower
                      and the static try after it can still get through.
    """
    unblocker_blocks: int = 0
    unblocker_timed_out: bool = False
    rate_pauses: int = 0
    rate_spent: bool = False
    live_lost: int = 0


_RUN: dict = {}                    # host -> _HostRun
_RUN_LOCK = threading.Lock()       # the harvest fetches on a thread pool


def _host_run(host: str) -> _HostRun:
    with _RUN_LOCK:
        return _RUN.setdefault(host, _HostRun())


def reset_run_state() -> None:
    """Forget everything this process learned about hosts (and close the
    Wayback breaker). A job process never needs it; tests do."""
    global _wb_fails, _wb_open_until
    with _RUN_LOCK:
        _RUN.clear()
    with _wb_lock:
        _wb_fails = 0
        _wb_open_until = 0.0


def _given_up(host: str) -> bool:
    trip = _int_setting("host_giveup_after", 3)
    return bool(host and trip > 0 and _host_run(host).live_lost >= trip)


def _note_live(host: str, ok: bool, *, page_gone: bool = False) -> None:
    """Record how the live rungs ended for one URL of `host`."""
    if not host:
        return
    st = _host_run(host)
    with _RUN_LOCK:
        if ok:
            st.live_lost = 0
            return
        if page_gone:
            return
        st.live_lost += 1
        n = st.live_lost
    if n == _int_setting("host_giveup_after", 3):
        print(f"[fetch] GIVING UP on {host} for the rest of this run: {n} URLs in a row lost "
              f"every live fetch — further URLs go straight to Wayback")


class _Trace:
    """One URL's trip down the ladder: numbers the rungs, times each one, and
    writes its acquisition-ledger row. Best-effort — a ledger problem never
    costs a fetch."""

    def __init__(self, url: str):
        self.url = url
        self.rung = 0
        self._t0 = time.perf_counter()

    def step(self) -> None:
        self.rung += 1

    def record(self, technique: str, *, ok: bool, resp=None, reason: str = "",
               notes: str = "") -> None:
        now = time.perf_counter()
        ms, self._t0 = int((now - self._t0) * 1000), now
        _ledger(self.url, technique, ok=ok, rung=self.rung, resp=resp, reason=reason,
                ms=ms, notes=notes)


def _ledger(url: str, technique: str, *, ok: bool, rung: int, resp=None,
            reason: str = "", ms: Optional[int] = None, notes: str = "") -> None:
    try:
        from input.pipeline import acquisition
        body = getattr(resp, "content", None) if resp is not None else None
        acquisition.record(url, technique, ok=ok, rung=rung, reason=reason,
                           status_code=getattr(resp, "status_code", None) if resp is not None else None,
                           nbytes=len(body) if body is not None else None, ms=ms, notes=notes)
    except Exception:
        pass


# --- rung 1: direct ----------------------------------------------------------

def fetch_with_ua_fallback(url: str, *,
                            timeout: int = DEFAULT_TIMEOUT_SECONDS,
                            user_agents: Optional[list[str]] = None
                            ) -> tuple[requests.Response, str]:
    """Canonical HTTP-level fetcher. Tries each UA in order until one
    succeeds (2xx response, no network exception). Returns
    (response, ua_used) so callers can log which UA worked.

    Why a chain: source sites have inconsistent UA policies. Most
    recipe blogs accept both. Some — thekitchn.com is the canonical
    example — actively 403 Chrome-style UAs and accept our bot
    string. Other sites do "normal" anti-bot (block bots, allow
    Chrome). One UA can't satisfy both. We try bot UA first (broader
    acceptance + zero deception cost) and fall back to Chrome on
    failure. Last error re-raised if every UA fails.

    Every direct fetch in the system comes through here (the ladder below,
    the harvest's is-recipe filter, the domain pre-flight), so a URL the
    extract can fetch always survives the filter.
    """
    uas = user_agents or USER_AGENT_CHAIN
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    last_resp = None             # last non-2xx response — carried on the synthetic error
    blocked_resp = None          # best 2xx-but-blocked answer seen, as a last resort
    blocked_ua = None
    for ua in uas:
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": ua})
            if 200 <= resp.status_code < 300:
                _fix_response_encoding(resp)
                # A CHALLENGE STUB IS NOT A SUCCESS, SO IT MUST NOT END THE SEARCH.
                # The chain exists to find a UA the site accepts, and it used to
                # stop at the first 2xx — but a WAF challenge is frequently 2xx.
                # memoriediangelina.com (2026-08-22) answers the bot UA with
                # `202` and a 198-byte stub, so the chain "succeeded" on UA #1 and
                # NEVER TRIED the browser UA behind it. The site was reachable the
                # whole time; we just stopped looking. Keep the stub aside and try
                # the next UA; only fall back to it if every UA is blocked, so the
                # caller still gets a response to inspect rather than an exception
                # that loses the evidence.
                if blocked_reason(resp) is not None:
                    if blocked_resp is None:
                        blocked_resp, blocked_ua = resp, ua
                    last_status = resp.status_code
                    continue
                return resp, ua
            last_status = resp.status_code
            last_resp = resp
            # Don't retry 404 — page genuinely doesn't exist, swapping
            # UA won't conjure it. 410 (gone) similarly.
            if resp.status_code in (404, 410):
                resp.raise_for_status()
        except requests.HTTPError:
            # 404/410 above — terminal
            raise
        except Exception as e:
            last_exc = e
            continue
    # Every UA in the chain failed. Raise the most informative thing
    # we have: a real exception if we caught one, else a synthetic
    # HTTPError carrying the last status code.
    if blocked_resp is not None:
        # Every UA was challenged. Hand back the stub rather than raising: the
        # caller's own block check sees it and routes to unblocker/Wayback, and
        # a response carries the diagnosis where an exception does not.
        return blocked_resp, blocked_ua
    if last_exc is not None:
        raise last_exc
    raise requests.HTTPError(
        f"All UAs in chain returned non-2xx (last status: {last_status}) for {url}",
        response=last_resp,   # lets the ladder see a 429 (rate limit) and PAUSE, not pay
    )


# --- 429 = "slow down", not "go away" -------------------------------------
# Measured 2026-09-11 (ledger): connoisseurusveg.com took 106 direct fetches in
# 23 s (~270/min), answered 429 for the next 60 s, then served direct again. The
# ladder escalated every URL inside that window to the PAID unblocker — 18 units
# for pages the origin would have handed over a minute later. Same shape on
# healthyfitnessmeals, 40aprons, chewoutloud, babaganosh (67 units in two days).
# So a 429 on the direct rung now PAUSES (Retry-After if sent, else the setting,
# doubling per further 429 in this run) and retries direct before any paid rung.
def _retry_after_s(resp) -> Optional[int]:
    try:
        v = (resp.headers or {}).get("Retry-After") if resp is not None else None
        return int(float(v)) if v and str(v).strip().replace(".", "", 1).isdigit() else None
    except Exception:
        return None


def _fetch_direct_paced(url: str, timeout: int, *, on_429=None):
    """fetch_with_ua_fallback, but a 429 pauses and retries instead of failing
    straight into the paid tier. Raises the last error when retries run out.
    `on_429(pause_s, attempt, resp)` lets the caller ledger each pause."""
    pause_s = _int_setting("direct_429_pause_s", 60)
    retries = _int_setting("direct_429_retries", 2)
    host = _host(url)
    st = _host_run(host)
    attempt = 0
    while True:
        try:
            got = fetch_with_ua_fallback(url, timeout=timeout)
        except requests.HTTPError as e:
            if _status_of(e) != 429 or pause_s <= 0:
                raise
            if attempt >= retries:
                if retries > 0:
                    st.rate_spent = True        # waited the full ladder and still 429
                raise
            if st.rate_spent:
                print(f"[direct] 429 from {host} — already waited out the retries on this host "
                      f"this run and it stayed 429: not pausing again")
                raise
            with _RUN_LOCK:
                n = st.rate_pauses
                st.rate_pauses = n + 1
            wait = _retry_after_s(e.response) or pause_s * (2 ** min(n, 3))
            wait = max(1, min(wait, 600))
            attempt += 1
            print(f"[direct] 429 from {host} — rate limit, not a block: pausing {wait}s, "
                  f"then retrying direct (attempt {attempt}/{retries}) before any paid fetch")
            if on_429:
                try:
                    on_429(wait, attempt, e.response)
                except Exception:
                    pass
            time.sleep(wait)
        else:
            st.rate_spent = False
            return got


# --- rung 3: Wayback ---------------------------------------------------------

WAYBACK_AVAILABILITY_URL = "https://archive.org/wayback/available"
WAYBACK_RAW_URL_FMT = "https://web.archive.org/web/{ts}id_/{url}"


# --- Wayback politeness: jittered delay + global circuit-breaker ---------
# archive.org rate-limits an IP that bursts requests (a publisher harvest fetches
# ~87 blocked URLs, each falling to Wayback). The symptom: connect-timeouts /
# "actively refused" (WinError 10061) that slow the run AND risk a longer ban on
# our LEGIT Wayback use elsewhere. Two fixes:
#   1. a small JITTERED delay before each Wayback hit so we don't burst, and
#   2. a CIRCUIT-BREAKER — once archive.org refuses us N times in a row it's
#      throttling our IP *globally* (not per-target), so we stop trying Wayback
#      for a cooldown and fast-fail instead of timing out 80 more times.
# Safe degradation: an open circuit only means "no Wayback this window" → fewer
# blocked-page recoveries, never wrong data. Self-resets on the next success.
_WB_JITTER_S = (0.4, 1.5)        # random pause before each Wayback request
_WB_FAIL_THRESHOLD = 4           # consecutive connection failures → open the circuit
_WB_COOLDOWN_S = 120.0           # how long to skip Wayback once open
_wb_lock = threading.Lock()
_wb_fails = 0
_wb_open_until = 0.0             # monotonic deadline; 0 = closed


def _wb_circuit_open() -> bool:
    return time.monotonic() < _wb_open_until


def _wb_record(ok: bool) -> None:
    """Feed a Wayback outcome to the breaker. `ok` = archive.org answered (any HTTP
    response, connection works); not-ok = a connection/timeout failure."""
    global _wb_fails, _wb_open_until
    with _wb_lock:
        if ok:
            _wb_fails = 0
            _wb_open_until = 0.0
        else:
            _wb_fails += 1
            if _wb_fails >= _WB_FAIL_THRESHOLD and not _wb_circuit_open():
                _wb_open_until = time.monotonic() + _WB_COOLDOWN_S
                print(f"[wayback] circuit OPEN — {_wb_fails} consecutive connection "
                      f"failures; skipping Wayback for {int(_WB_COOLDOWN_S)}s "
                      f"(archive.org is throttling us)")


def fetch_via_wayback(url: str, *,
                      timeout: int = DEFAULT_TIMEOUT_SECONDS,
                      ) -> Optional[tuple[requests.Response, str]]:
    """Last-resort fallback: fetch the page from Internet Archive's
    Wayback Machine when the live site refuses our UA chain. Returns
    (response, wayback_timestamp) on success, None when no snapshot
    exists or the fetch itself fails.

    Why this exists: aggressive Cloudflare-fronted sites (Kitchn, NYT
    Cooking, WaPo) increasingly 403 every UA we can rotate through.
    The user's directive is "no manual harvest, no Playwright" — so
    when direct fetch fails we ask Wayback for the most recent
    snapshot and parse that instead. Recipe content is essentially
    static; a snapshot from days/weeks/months ago is fine for
    cohort matching, grading, and most user-facing extraction.

    Snapshots may be missing (publisher excludes via robots.txt, very
    new URLs not yet crawled) — caller treats None as "no fallback,
    fail as before." Provenance crumb: the wayback timestamp is
    returned so the extractor can stamp `_source.via = wayback:<ts>`
    and the UI can surface "snapshot from YYYY-MM-DD."

    The `id_` flag in the raw URL strips the Wayback toolbar so the
    response body is the original page content byte-for-byte (modulo
    any rewriting Wayback does to inline assets, which the recipe
    JSON-LD survives cleanly).
    """
    # Skip entirely while the breaker is open — archive.org is throttling us, so
    # another attempt just times out + delays the run.
    if _wb_circuit_open():
        return None
    # Polite jitter so a big harvest doesn't burst archive.org into rate-limiting.
    time.sleep(random.uniform(*_WB_JITTER_S))
    try:
        avail = requests.get(
            WAYBACK_AVAILABILITY_URL,
            params={"url": url},
            timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        _wb_record(True)   # got an HTTP response → the connection works
        if not (200 <= avail.status_code < 300):
            return None
        data = avail.json()
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        _wb_record(False)  # connection/timeout → feed the breaker
        print(f"[wayback] availability check failed for {url!r}: {e}")
        return None
    except Exception as e:
        print(f"[wayback] availability check failed for {url!r}: {e}")
        return None

    snap = ((data.get("archived_snapshots") or {}).get("closest") or {})
    ts = snap.get("timestamp")
    snap_url = snap.get("url")
    if not ts or not snap_url:
        return None

    raw_url = WAYBACK_RAW_URL_FMT.format(ts=ts, url=url)
    try:
        resp = requests.get(
            raw_url,
            timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        _wb_record(True)
        if not (200 <= resp.status_code < 300):
            return None
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        _wb_record(False)
        print(f"[wayback] fetch failed for {raw_url!r}: {e}")
        return None
    except Exception as e:
        print(f"[wayback] fetch failed for {raw_url!r}: {e}")
        return None
    _fix_response_encoding(resp)
    print(f"[wayback] hit snapshot {ts} for {url}")
    return resp, ts


# --- rung 2: the PAID "web unblocker" (opt-in per domain) -----------------
# Some publishers (thekitchn = PerimeterX press-and-hold) block on the FIRST
# request via browser-fingerprint + JS challenge — no UA rotation or delay beats
# that, and Wayback only yields a stale snapshot. A managed "web unblocker"
# (residential IPs + real-browser render + CAPTCHA/challenge solving) fetches the
# LIVE page. It costs ~$1–5 / 1,000 requests, so it is NEVER a blanket fallback —
# the caller passes unblocker=True for a domain that opted in
# (fetch_strategy='unblocker', or the harvest's configured fallback). BYOK: key in the UNBLOCKER_API_KEY env, NEVER the DB (portable-
# package secret discipline). See docs/unblocker-fetch-strategy.md.
UNBLOCKER_TIMEOUT_SECONDS = 70   # a real-browser render is slow; give it room

# GET-style unblockers: one request with key + target url (+ a render flag) → HTML.
# (provider -> endpoint, key_param, url_param, render_param, render_value)
_UNBLOCKER_GET_PROVIDERS = {
    "scraperapi":  ("https://api.scraperapi.com/",         "api_key", "url", "render",     "true"),
    "scrapingbee": ("https://app.scrapingbee.com/api/v1/",  "api_key", "url", "render_js",  "true"),
    "zyte":        ("https://api.zyte.com/v1/extract",      "apikey",  "url", "browserHtml", "true"),
}

# PROXY-style unblockers (Oxylabs, Bright Data): route the request THROUGH their
# proxy (creds = user:pass); the zone does the unblocking + JS render. We GET the
# TARGET url directly, so response.url is already correct. They MITM TLS → verify=False.
# (provider -> default host, default port, optional (render_header, value))
_UNBLOCKER_PROXY_PROVIDERS = {
    "oxylabs":    ("unblock.oxylabs.io", 60000, ("x-oxylabs-render", "html")),
    "brightdata": ("brd.superproxy.io",  33335, None),   # Web Unlocker zone renders by default
}

# Origin anti-bot statuses worth a fresh-IP retry. A proxy provider GETs the target
# directly and passes any 4xx straight through WITHOUT rotating (Oxylabs only auto-
# retries 5xx / AI-detected blocks), so a People-Inc-style 402 needs a client retry —
# each attempt draws a fresh exit IP + fingerprint, and the block is IP-intermittent.
_ORIGIN_BLOCK_STATUSES = (401, 402, 403, 429, 503)


def _unblocker_cfg() -> dict:
    """BYOK + provider config. GET-style key in UNBLOCKER_API_KEY; proxy-style creds
    in UNBLOCKER_PROXY_USER / UNBLOCKER_PROXY_PASS (all env, NEVER the DB). Provider +
    optional host/port/endpoint overrides from system_config (no-data-in-code)."""
    import os
    return {
        "key": os.getenv("UNBLOCKER_API_KEY"),
        "proxy_user": os.getenv("UNBLOCKER_PROXY_USER"),
        "proxy_pass": os.getenv("UNBLOCKER_PROXY_PASS"),
        "provider": (_setting("unblocker_provider", "")
                     or os.getenv("UNBLOCKER_PROVIDER") or "scraperapi").lower(),
        "endpoint": _setting("unblocker_endpoint", "") or None,
        "proxy_host": _setting("unblocker_proxy_host", "") or None,
        "proxy_port": _setting("unblocker_proxy_port", 0) or None,
        # Extra fresh-IP tries on an ORIGIN anti-bot block. 0 disables.
        "block_retries": _int_setting("unblocker_block_retries", 2),
        # Consecutive origin blocks on a host before the unblocker is skipped
        # for it (see _HostRun.unblocker_blocks). 0 disables.
        "circuit_trip": _int_setting("unblocker_circuit_trip", 3),
    }


def _has_creds(cfg: dict) -> bool:
    if cfg["provider"] in _UNBLOCKER_PROXY_PROVIDERS:
        return bool(cfg["proxy_user"] and cfg["proxy_pass"])
    return bool(cfg["key"])


def unblocker_available() -> bool:
    """True only when THIS provider's BYOK credentials are present (else no-op):
    proxy providers need user+pass, GET providers need the api key."""
    return _has_creds(_unblocker_cfg())


def _breaker_note(host: str, blocked: bool, trip: int) -> None:
    st = _host_run(host)
    with _RUN_LOCK:
        st.unblocker_blocks = st.unblocker_blocks + 1 if blocked else 0
        n = st.unblocker_blocks
    if blocked and trip and n == trip:
        print(f"[unblocker] circuit OPEN for {host} after {n} consecutive origin blocks "
              f"— skipping the unblocker for this host for the rest of the run (→ fallback)")


def _fetch_via_proxy_unblocker(url, provider, cfg, timeout, render):
    """Oxylabs / Bright Data: GET the TARGET through their unblocking proxy. The
    zone solves the challenge + renders; we just route through it. They intercept
    TLS, so verify=False (and we silence the resulting warning).

    Returns (hit, origin_status): `hit` is (response, meta) or None; `origin_status`
    is the non-2xx status the TARGET answered with, None when the request never
    reached it (transport or account failure)."""
    from urllib.parse import quote
    host, port, render_hdr = _UNBLOCKER_PROXY_PROVIDERS[provider]
    host = cfg.get("proxy_host") or host
    port = cfg.get("proxy_port") or port
    proxy = (f"http://{quote(cfg['proxy_user'], safe='')}:{quote(cfg['proxy_pass'], safe='')}"
             f"@{host}:{port}")
    proxies = {"http": proxy, "https": proxy}
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if render and render_hdr:
        headers[render_hdr[0]] = render_hdr[1]
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    target = _host(url)
    retries = max(0, cfg["block_retries"])
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, proxies=proxies, headers=headers,
                                timeout=timeout, verify=False)
        except Exception as e:
            print(f"[unblocker] {provider} transport/account error for {url}: {e}")
            if isinstance(e, requests.exceptions.Timeout) and not render:
                st = _host_run(target)
                with _RUN_LOCK:
                    st.unblocker_timed_out = True
                print(f"[unblocker] {target} timed out on a static paid fetch — skipping "
                      f"paid fetches for this host for the rest of the run (fallback next)")
            return None, None
        if 200 <= resp.status_code < 300:
            _fix_response_encoding(resp)   # resp.url is already the target (we GET it directly)
            _breaker_note(target, False, 0)     # pool works for this host → reset the breaker
            st = _host_run(target)
            with _RUN_LOCK:
                st.unblocker_timed_out = False
            tag = f" (attempt {attempt + 1})" if attempt else ""
            print(f"[unblocker] {provider} fetched {url} LIVE{tag}")
            return (resp, {"source": "unblocker", "provider": provider, "render": bool(render)}), None
        # Non-2xx = the TARGET ORIGIN's status relayed through the proxy (a provider/account
        # failure raises above), confirmed when X-Oxylabs-Content-Status-Code echoes it — that
        # header's presence means the request reached the origin, so retrying draws a new IP
        # rather than hammering an Oxylabs-side 429. seriouseats/People Inc. serve 402 here,
        # which read like "account unpaid" under the old "{provider} returned" wording.
        origin_echo = resp.headers.get("x-oxylabs-content-status-code")
        is_origin_block = resp.status_code in _ORIGIN_BLOCK_STATUSES and (
            origin_echo is None or origin_echo == str(resp.status_code))
        if is_origin_block and attempt < retries:
            print(f"[unblocker] {provider} origin {resp.status_code} anti-bot block "
                  f"for {url} — retrying with a fresh IP ({attempt + 1}/{retries})")
            continue
        # A non-block origin response (real 404/410, etc.) proves the exit pool reaches
        # the origin fine — reset the breaker; only an actual origin block counts against it.
        _breaker_note(target, is_origin_block, cfg["circuit_trip"])
        hint = " — origin anti-bot block" if resp.status_code in _ORIGIN_BLOCK_STATUSES else ""
        print(f"[unblocker] {provider} proxied OK; target ORIGIN returned "
              f"{resp.status_code}{hint} for {url}"
              + (f" (gave up after {retries + 1} tries)" if is_origin_block else ""))
        return None, resp.status_code


def _fetch_via_get_unblocker(url, provider, cfg, timeout, render):
    """ScraperAPI / ScrapingBee / Zyte: one GET to the vendor API → page HTML.
    Same return shape as the proxy fetcher; these APIs do not relay the
    origin's status, so the second element is always None."""
    spec = _UNBLOCKER_GET_PROVIDERS.get(provider)
    endpoint = cfg.get("endpoint")
    if spec:
        ep, key_param, url_param, render_param, render_val = spec
        endpoint = endpoint or ep
    elif endpoint:                          # custom endpoint → assume scraperapi shape
        key_param, url_param, render_param, render_val = "api_key", "url", "render", "true"
    else:
        print(f"[unblocker] unknown provider {provider!r} and no unblocker_endpoint set")
        return None, None
    params = {key_param: cfg["key"], url_param: url}
    if render:
        params[render_param] = render_val
    try:
        resp = requests.get(endpoint, params=params, timeout=timeout)
        if not (200 <= resp.status_code < 300):
            print(f"[unblocker] {provider} returned {resp.status_code} for {url}")
            return None, None
    except Exception as e:
        print(f"[unblocker] {provider} failed for {url}: {e}")
        return None, None
    resp.url = url                          # base-url must be the TARGET, not the API
    _fix_response_encoding(resp)
    print(f"[unblocker] {provider} fetched {url} LIVE")
    return (resp, {"source": "unblocker", "provider": provider, "render": bool(render)}), None


def _unblocker_attempt(url: str, timeout: int, render: bool):
    """One paid fetch. (hit, origin_status) as the provider fetchers return it;
    (None, None) with no credentials or an open breaker."""
    cfg = _unblocker_cfg()
    if not _has_creds(cfg):
        return None, None
    provider = cfg["provider"]
    if provider not in _UNBLOCKER_PROXY_PROVIDERS:
        return _fetch_via_get_unblocker(url, provider, cfg, timeout, render)
    # Per-run breaker: this host has already burst-blocked the whole exit pool this
    # run, so skip the (slow, futile) call. Retries can't beat a fully-blocked pool.
    trip = cfg["circuit_trip"]
    if trip > 0 and _host_run(_host(url)).unblocker_blocks >= trip:
        print(f"[unblocker] circuit open for {_host(url)} — skipping unblocker, "
              f"using fallback for {url}")
        return None, None
    if _host_run(_host(url)).unblocker_timed_out:
        print(f"[unblocker] {_host(url)} timed out earlier this run — skipping unblocker, "
              f"using fallback for {url}")
        return None, None
    return _fetch_via_proxy_unblocker(url, provider, cfg, timeout, render)


def fetch_via_unblocker(url: str, *, timeout: int = UNBLOCKER_TIMEOUT_SECONDS,
                        render: bool = True) -> Optional[tuple[requests.Response, dict]]:
    """Fetch `url` LIVE through the configured web-unblocker — PROXY-style (Oxylabs,
    Bright Data) or GET-style (ScraperAPI, ScrapingBee, Zyte). Returns (response,
    meta) with response.url = the TARGET, or None on no-creds / failure.

    For callers that want the paid fetch and nothing else (images, screenshots,
    product pages). A PAGE fetch belongs on `fetch_with_full_fallback`, which
    tries the free route first, ledgers every rung and shares the page cache."""
    return _unblocker_attempt(url, timeout, render)[0]


# --- the ladder ----------------------------------------------------------------

def _paid_first_host(host: str) -> bool:
    """Is this host's domain marked paid_first (domains master, curated)? Falls
    CLOSED (False = the normal direct-first ladder) on any lookup problem."""
    try:
        from input.pipeline import domains_lib
        return bool(host) and domains_lib.fetch_policy(host).paid_first
    except Exception:
        return False


def _paid_rung(t: _Trace, timeout: int, *, render: bool, notes: str = ""):
    """One paid fetch, ledgered. Returns (hit, why_lost, origin_status). A hit can
    still be a blocked page (`why_lost` says so): it is handed back as it is,
    because the eager block check here answers "worth a credit?", and the
    caller's strict check is the one that decides what the page IS."""
    t.step()
    hit, origin = _unblocker_attempt(t.url, max(timeout, UNBLOCKER_TIMEOUT_SECONDS), render)
    why = "unblocker returned nothing" if hit is None else (blocked_reason(hit[0]) or "")
    t.record("unblocker_render" if render else "unblocker", ok=not why,
             resp=hit[0] if hit is not None else None, reason=why, notes=notes)
    return hit, why, origin


def _wayback_rung(t: _Trace, timeout: int, lost: str):
    t.step()
    wb = fetch_via_wayback(t.url, timeout=timeout)
    if wb is None:
        t.record("wayback", ok=False, reason="no wayback snapshot")
        raise requests.HTTPError(f"{lost} and no Wayback snapshot available for {t.url}")
    resp, ts = wb
    why = blocked_reason(resp) or ""
    t.record("wayback", ok=not why, resp=resp, reason=why, notes=f"snapshot {ts}")
    return resp, {"source": "wayback", "timestamp": ts}


def _direct_then_paid(t: _Trace, host: str, timeout: int, *, paid: bool, render: bool):
    """The direct-first live rungs. Returns (hit, error): a page, or why there is
    none. Raises on a 404/410 — the page is gone and no rung will find it.

    `paid`   the caller enabled the unblocker for this URL AND credentials exist.
    `render` the domain is render-eligible (JS-rendered, learned or curated). The
             direct fetch is still made first, because it is free: 2026-08-28 the
             old render-FIRST skip trusted the flag blindly and paid 74 render
             credits on gressinghamduck.co.uk, whose static HTML carries the
             complete recipe JSON-LD. On a render-eligible domain the direct page
             is accepted only when it plainly IS the page (structure: ld+json or
             <article>); a big-but-empty nav shell goes on to the rendered fetch.
    """
    url = t.url
    probing = render and paid
    notes = "probe" if probing else ""
    page = None                 # a direct response that is a real page, however thin
    parked = None
    t.step()
    try:
        resp, ua = _fetch_direct_paced(
            url, timeout,
            on_429=lambda wait, attempt, r: t.record(
                "direct", ok=False, resp=r,
                reason=f"429 rate-limited — paused {wait}s, retrying direct ({attempt})"))
    except requests.HTTPError as e:
        t.record("direct", ok=False, resp=e.response, reason=str(e), notes=notes)
        if _status_of(e) in _GONE:
            raise
        err: Exception = e
    except Exception as e:
        t.record("direct", ok=False, reason=f"{type(e).__name__}: {e}", notes=notes)
        err = e
    else:
        # BLOCK-CHECK ALWAYS, ESCALATE ONLY IF WE CAN. With the paid tier off a
        # soft block must still not be returned as a success — it is frequently
        # 2xx (2026-08-22: a WAF answering 202 with a 197-byte challenge stub).
        # A block we cannot escalate falls through to Wayback, which is a real
        # page; returning the stub is silent corruption, worse than a clean failure.
        parked = parked_reason(resp)
        why = parked or blocked_reason(resp)
        if not why and probing and not _has_structure(resp):
            page = (resp, {"source": "direct", "ua_used": ua})
            why = "no ld+json / <article> — looks like a JS shell"
        if not why:
            t.record("direct", ok=True, resp=resp, notes=notes)
            _note_live(host, True)
            return (resp, {"source": "direct", "ua_used": ua}), None
        t.record("direct", ok=False, resp=resp, reason=why, notes=notes)
        if parked:
            # Dead publisher, not a block: never pay to re-fetch a lander
            # (2026-09-11, aprettylifeinthesuburbs.com: 44 URLs escalated).
            err = requests.HTTPError(f"{parked} for {url} — domain is dead; not escalating")
        elif paid:
            err = requests.HTTPError(f"{why} for {url} — escalating to unblocker")
        else:
            err = requests.HTTPError(f"Soft-block challenge for {url} ({why}) — no unblocker; trying Wayback")

    origin = None
    if paid and not parked:
        # SAY WHY before spending, so the run log distinguishes a genuine block
        # (403s, challenge pages — money well spent) from a detector false
        # positive at a glance.
        print(f"[unblocker] {'render-' if render else ''}escalating (direct lost: {str(err)[:140]})")
        hit, why, origin = _paid_rung(t, timeout, render=render)
        if hit is None and render and origin is None:
            # The rendered fetch never reached the site (a render can time out
            # where the static fetch, ~3x faster, gets through). One static try.
            # Not after an origin block: the site refused the pool, and asking
            # again without the browser only repeats the refusal.
            hit, why, origin = _paid_rung(t, timeout, render=False)
        if hit is not None:
            _note_live(host, not why)
            return hit, None
    if page is not None:
        # The direct page was real, only unstructured, and rendering did not
        # improve on it: it is still the best answer there is.
        t.step()
        t.record("direct", ok=True, resp=page[0], notes="probe kept — render lost")
        _note_live(host, True)
        return page, None
    _note_live(host, False, page_gone=origin in _GONE)
    return None, err


def _fetch_ladder(url: str, *, timeout: int, try_wayback: bool, unblocker: bool,
                  render: bool) -> tuple[requests.Response, dict]:
    """The rungs, in order, for one URL (see the module docstring)."""
    host = _host(url)
    t = _Trace(url)
    have_paid = unblocker_available()
    if _given_up(host):
        # Earlier URLs this run lost every live rung in a row. Do not pay the
        # same minutes (and paid fetches) again per URL — Wayback or nothing.
        lost = f"Gave up on live fetches from {host} this run"
        err: Exception = requests.HTTPError(f"{lost}: {url}")
    elif have_paid and _paid_first_host(host):
        # Curated: direct is skipped outright and never revisited. For a host
        # whose direct answer is always a 429 the pause-and-retry costs minutes
        # per URL to learn nothing. Without credentials the mark is inert.
        print(f"[unblocker] paid-first domain ({host}) — skipping the direct fetch")
        hit, why, origin = _paid_rung(t, timeout, render=render, notes="paid-first")
        _note_live(host, not why, page_gone=origin in _GONE)
        if hit is not None:
            return hit
        lost = "Paid-first fetch failed"
        err = requests.HTTPError(f"{lost} for {url} ({why})")
        if try_wayback:
            print(f"[unblocker] paid-first fetch lost ({why}) — trying Wayback, not direct")
    else:
        hit, err = _direct_then_paid(t, host, timeout, paid=unblocker and have_paid, render=render)
        if hit is not None:
            return hit
        lost = "Direct fetch failed"
    if not try_wayback:
        raise err
    return _wayback_rung(t, timeout, lost)


def fetch_with_full_fallback(url: str, *,
                             timeout: int = DEFAULT_TIMEOUT_SECONDS,
                             try_wayback: bool = True,
                             unblocker: bool = False,
                             render: bool = False,
                             ) -> tuple[requests.Response, dict]:
    """Get a page: page cache, then the ladder (direct → paid unblocker → Wayback).

    Returns (response, meta); `meta["source"]` is "direct" | "unblocker" |
    "wayback" | "page-cache". Raises when no rung produced a page; a 404/410 is
    raised as it is.

    unblocker   ENABLES the paid tier as an escalation. The free direct fetch is
                still tried first, so a page that loads plain costs nothing.
    render      the domain needs a real browser (render_required). Only has an
                effect together with `unblocker`.
    try_wayback False ends the ladder at the live rungs.

    The page cache is opt-in (the publisher harvest switches it on with
    `page_cache.enabled()`): a fresh cached page is returned without touching
    the network, and a good fetch is written back, so the harvest's is-recipe
    filter and its winner-extract share ONE fetch."""
    from input.pipeline import page_cache
    caching = page_cache.is_enabled()
    if caching:
        cached = page_cache.get(url, render)
        if cached is not None:
            _ledger(url, "cache", ok=True, rung=0, resp=cached, ms=0,
                    notes="render" if render else "static")
            return cached, {"source": "page-cache"}
    resp, meta = _fetch_ladder(url, timeout=timeout, try_wayback=try_wayback,
                               unblocker=unblocker, render=render)
    if caching:
        try:
            if 200 <= getattr(resp, "status_code", 0) < 300 and not blocked_reason(resp):
                page_cache.put(url, render, resp, meta)
        except Exception:
            pass
    return resp, meta
