"""HTML -> canonical markdown adapter.

Fetches a URL, pulls any schema.org Recipe JSON-LD via extruct, and emits
markdown shaped for extract.markdown_to_recipe:

    # <title>

    URL: <source_url>

    ## STRUCTURED RECIPE DATA (JSON-LD)
    ```json
    {...}
    ```

    ## PAGE CONTENT

    <body-converted-to-markdown>

The JSON-LD section is omitted when no Recipe block is found. Page chrome
(nav, footer, aside, script, style) is stripped before markdownify runs.
"""
import copy
import json
import random
import re
import threading
import time
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

import extruct
from w3lib.html import get_base_url


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; recipe-forms/0.1; +https://example.com)"
)
# Browser-style fallback. Some sites do "normal" anti-bot — block our
# recipe-forms UA, allow real browsers. We try BOT_UA first (broadly
# accepted, including by quirky sites like thekitchn.com that actively
# 403 Chrome UAs); on failure we retry with this. Order matters: more
# sites accept the bot UA than reject it, so this minimizes wasted
# fetches.
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
USER_AGENT_CHAIN = [DEFAULT_USER_AGENT, FALLBACK_USER_AGENT]
DEFAULT_TIMEOUT_SECONDS = 20

# Tags whose content is never useful for recipe extraction.
STRIP_TAGS = ["script", "style", "noscript", "iframe", "svg", "form"]

# Page-chrome selectors dropped before markdown conversion. Conservative
# on purpose — we'd rather include a sidebar than drop an ingredient list
# living inside an unexpected wrapper.
DROP_SELECTORS = [
    "nav", "footer", "aside", "header",
    "[role='navigation']",
    ".ads", ".advertisement", ".related-posts", ".related",
    ".comments", "#comments", ".social-share",
]


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

    Used by:
      - fetch_html (this module): step 7 canonical extract
      - intake.build_query_batch._fetch_text: step 3 is_recipe filter
    Keeping both on this code path means a URL the extract can fetch
    will always survive the filter (no more silent step-3 drops).
    """
    uas = user_agents or USER_AGENT_CHAIN
    last_exc: Optional[Exception] = None
    last_status: Optional[int] = None
    for ua in uas:
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": ua})
            if 200 <= resp.status_code < 300:
                _fix_response_encoding(resp)
                return resp, ua
            last_status = resp.status_code
            # Don't retry 404 — page genuinely doesn't exist, swapping
            # UA won't conjure it. 410 (gone) similarly.
            if resp.status_code in (404, 410):
                resp.raise_for_status()
        except requests.HTTPError as e:
            # 404/410 above — terminal
            raise
        except Exception as e:
            last_exc = e
            continue
    # Every UA in the chain failed. Raise the most informative thing
    # we have: a real exception if we caught one, else a synthetic
    # HTTPError carrying the last status code.
    if last_exc is not None:
        raise last_exc
    raise requests.HTTPError(
        f"All UAs in chain returned non-2xx (last status: {last_status}) for {url}"
    )


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


# --- PAID "web unblocker" tier (opt-in per domain) -----------------------
# Some publishers (thekitchn = PerimeterX press-and-hold) block on the FIRST
# request via browser-fingerprint + JS challenge — no UA rotation or delay beats
# that, and Wayback only yields a stale snapshot. A managed "web unblocker"
# (residential IPs + real-browser render + CAPTCHA/challenge solving) fetches the
# LIVE page. It costs ~$1–5 / 1,000 requests, so it is NEVER a blanket fallback —
# a domain opts in via fetch_strategy='unblocker' and the caller passes
# unblocker=True. BYOK: key in the UNBLOCKER_API_KEY env, NEVER the DB (portable-
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

# Per-RUN circuit breaker (process-level; resets each job process). Some publishers
# (Dotdash / People Inc. — seriouseats) burst-block the ENTIRE unblocker exit pool once
# a harvest ramps up, so every fresh-IP retry still 402s. After N consecutive origin
# blocks on a host, stop attempting the (slow) unblocker for that host for the rest of
# the run and go straight to the Wayback fallback — retries can't beat a fully-blocked
# pool, so they're pure wasted latency. A single non-block response (2xx or a real 404)
# proves the pool works again for that host and resets the counter.
_UNBLOCKER_HOST_BLOCKS: dict = {}


def _unblocker_cfg() -> dict:
    """BYOK + provider config. GET-style key in UNBLOCKER_API_KEY; proxy-style creds
    in UNBLOCKER_PROXY_USER / UNBLOCKER_PROXY_PASS (all env, NEVER the DB). Provider +
    optional host/port/endpoint overrides from system_config (no-data-in-code)."""
    import os
    cfg = {
        "key": os.getenv("UNBLOCKER_API_KEY"),
        "proxy_user": os.getenv("UNBLOCKER_PROXY_USER"),
        "proxy_pass": os.getenv("UNBLOCKER_PROXY_PASS"),
        "endpoint": None, "proxy_host": None, "proxy_port": None,
        "block_retries": 2, "circuit_trip": 3,
    }
    try:
        from input.pipeline import system_config
        cfg["provider"] = (system_config.get_setting("unblocker_provider", "")
                           or os.getenv("UNBLOCKER_PROVIDER") or "scraperapi").lower()
        cfg["endpoint"] = system_config.get_setting("unblocker_endpoint", "") or None
        cfg["proxy_host"] = system_config.get_setting("unblocker_proxy_host", "") or None
        cfg["proxy_port"] = system_config.get_setting("unblocker_proxy_port", 0) or None
        # Extra fresh-IP retries on an ORIGIN anti-bot block (see _fetch_via_proxy_unblocker).
        # A proxy provider passes any target 4xx straight through WITHOUT rotating (Oxylabs
        # only auto-retries 5xx / AI-detected blocks), so a People-Inc-style 402 needs a
        # client-side retry — each attempt gets a fresh exit IP. 0 disables.
        cfg["block_retries"] = int(system_config.get_setting("unblocker_block_retries", 2) or 0)
        # Per-run circuit breaker: consecutive origin blocks on a host before we stop
        # attempting the unblocker for that host (see _UNBLOCKER_HOST_BLOCKS). 0 disables.
        cfg["circuit_trip"] = int(system_config.get_setting("unblocker_circuit_trip", 3) or 0)
    except Exception:
        cfg["provider"] = (os.getenv("UNBLOCKER_PROVIDER") or "scraperapi").lower()
        cfg["block_retries"] = 2
        cfg["circuit_trip"] = 3
    return cfg


def unblocker_available() -> bool:
    """True only when THIS provider's BYOK credentials are present (else no-op):
    proxy providers need user+pass, GET providers need the api key."""
    cfg = _unblocker_cfg()
    if cfg["provider"] in _UNBLOCKER_PROXY_PROVIDERS:
        return bool(cfg["proxy_user"] and cfg["proxy_pass"])
    return bool(cfg["key"])


def _circuit_host(url) -> str:
    from urllib.parse import urlparse
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def _circuit_open(url, trip: int) -> bool:
    """True when this host has hit `trip` consecutive origin blocks this run (breaker
    open → skip the unblocker, go straight to the fallback). trip<=0 disables."""
    if trip and trip > 0:
        return _UNBLOCKER_HOST_BLOCKS.get(_circuit_host(url), 0) >= trip
    return False


def _circuit_note_block(url, trip: int) -> None:
    host = _circuit_host(url)
    n = _UNBLOCKER_HOST_BLOCKS.get(host, 0) + 1
    _UNBLOCKER_HOST_BLOCKS[host] = n
    if trip and n == trip:
        print(f"[unblocker] circuit OPEN for {host} after {n} consecutive origin blocks "
              f"— skipping the unblocker for this host for the rest of the run (→ fallback)")


def _circuit_note_success(url) -> None:
    _UNBLOCKER_HOST_BLOCKS.pop(_circuit_host(url), None)


def _fetch_via_proxy_unblocker(url, provider, cfg, timeout, render):
    """Oxylabs / Bright Data: GET the TARGET through their unblocking proxy. The
    zone solves the challenge + renders; we just route through it. They intercept
    TLS, so verify=False (and we silence the resulting warning)."""
    from urllib.parse import quote
    host, port, render_hdr = _UNBLOCKER_PROXY_PROVIDERS[provider]
    host = cfg.get("proxy_host") or host
    port = cfg.get("proxy_port") or port
    user, pwd = cfg["proxy_user"], cfg["proxy_pass"]
    proxy = f"http://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}"
    proxies = {"http": proxy, "https": proxy}
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    if render and render_hdr:
        headers[render_hdr[0]] = render_hdr[1]
    try:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except Exception:
        pass
    # Origin anti-bot statuses worth a fresh-IP retry. A proxy provider GETs the target
    # directly and passes any 4xx straight through WITHOUT rotating (Oxylabs only auto-
    # retries 5xx / AI-detected blocks), so a People-Inc-style 402 needs a client retry —
    # each attempt draws a fresh exit IP + fingerprint, and the block is IP-intermittent.
    _BLOCK = (401, 402, 403, 429, 503)
    retries = max(0, int(cfg.get("block_retries", 0) or 0))
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, proxies=proxies, headers=headers,
                                timeout=timeout, verify=False)
        except Exception as e:
            print(f"[unblocker] {provider} transport/account error for {url}: {e}")
            return None
        if 200 <= resp.status_code < 300:
            _fix_response_encoding(resp)   # resp.url is already the target (we GET it directly)
            _circuit_note_success(url)     # pool works for this host → reset the breaker
            tag = f" (attempt {attempt + 1})" if attempt else ""
            print(f"[unblocker] {provider} fetched {url} LIVE{tag}")
            return resp, {"source": "unblocker", "provider": provider}
        # Non-2xx = the TARGET ORIGIN's status relayed through the proxy (a provider/account
        # failure raises above), confirmed when X-Oxylabs-Content-Status-Code echoes it — that
        # header's presence means the request reached the origin, so retrying draws a new IP
        # rather than hammering an Oxylabs-side 429. seriouseats/People Inc. serve 402 here,
        # which read like "account unpaid" under the old "{provider} returned" wording.
        origin_echo = resp.headers.get("x-oxylabs-content-status-code")
        is_origin_block = resp.status_code in _BLOCK and (
            origin_echo is None or origin_echo == str(resp.status_code))
        if is_origin_block and attempt < retries:
            print(f"[unblocker] {provider} origin {resp.status_code} anti-bot block "
                  f"for {url} — retrying with a fresh IP ({attempt + 1}/{retries})")
            continue
        # A non-block origin response (real 404/410, etc.) proves the exit pool reaches
        # the origin fine — reset the breaker; only an actual origin block counts against it.
        if is_origin_block:
            _circuit_note_block(url, cfg.get("circuit_trip", 0))
        else:
            _circuit_note_success(url)
        hint = " — origin anti-bot block" if resp.status_code in _BLOCK else ""
        print(f"[unblocker] {provider} proxied OK; target ORIGIN returned "
              f"{resp.status_code}{hint} for {url}"
              + (f" (gave up after {retries + 1} tries)" if is_origin_block else ""))
        return None


def _fetch_via_get_unblocker(url, provider, cfg, timeout, render):
    """ScraperAPI / ScrapingBee / Zyte: one GET to the vendor API → page HTML."""
    spec = _UNBLOCKER_GET_PROVIDERS.get(provider)
    endpoint = cfg.get("endpoint")
    if spec:
        ep, key_param, url_param, render_param, render_val = spec
        endpoint = endpoint or ep
    elif endpoint:                          # custom endpoint → assume scraperapi shape
        key_param, url_param, render_param, render_val = "api_key", "url", "render", "true"
    else:
        print(f"[unblocker] unknown provider {provider!r} and no unblocker_endpoint set")
        return None
    params = {key_param: cfg["key"], url_param: url}
    if render:
        params[render_param] = render_val
    try:
        resp = requests.get(endpoint, params=params, timeout=timeout)
        if not (200 <= resp.status_code < 300):
            print(f"[unblocker] {provider} returned {resp.status_code} for {url}")
            return None
    except Exception as e:
        print(f"[unblocker] {provider} failed for {url}: {e}")
        return None
    resp.url = url                          # base-url must be the TARGET, not the API
    _fix_response_encoding(resp)
    print(f"[unblocker] {provider} fetched {url} LIVE")
    return resp, {"source": "unblocker", "provider": provider}


def fetch_via_unblocker(url: str, *, timeout: int = UNBLOCKER_TIMEOUT_SECONDS,
                        render: bool = True) -> Optional[tuple[requests.Response, dict]]:
    """Fetch `url` LIVE through the configured web-unblocker — PROXY-style (Oxylabs,
    Bright Data) or GET-style (ScraperAPI, ScrapingBee, Zyte). Returns (response,
    meta) with response.url = the TARGET, or None on no-creds / failure."""
    if not unblocker_available():
        return None
    cfg = _unblocker_cfg()
    provider = cfg["provider"]
    if provider in _UNBLOCKER_PROXY_PROVIDERS:
        # Per-run circuit breaker: if this host has already burst-blocked the whole exit
        # pool this run, skip the (slow, futile) unblocker call and let the caller fall
        # through to Wayback. Retries can't beat a fully-blocked pool.
        if _circuit_open(url, cfg.get("circuit_trip", 0)):
            print(f"[unblocker] circuit open for {_circuit_host(url)} — skipping unblocker, "
                  f"using fallback for {url}")
            return None
        return _fetch_via_proxy_unblocker(url, provider, cfg, timeout, render)
    return _fetch_via_get_unblocker(url, provider, cfg, timeout, render)


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

# Union — preserved under its original name so existing importers keep working.
_BLOCK_MARKERS = _HARD_BLOCK_MARKERS + _AMBIENT_BLOCK_MARKERS


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
    try:
        body = resp.text or ""
    except Exception:
        return None
    low = body.lower()
    for m in _HARD_BLOCK_MARKERS:
        if m in low:
            return f"blocked — challenge page, marker {m!r} ({len(body)} bytes)"
    limit = _THIN_VERDICT_CHARS if strict else _THIN_SPEND_CHARS
    looks_unstructured = "ld+json" not in low and "<article" not in low
    thin = len(body) < limit and looks_unstructured
    if thin:
        ambient = [m for m in _AMBIENT_BLOCK_MARKERS if m in low]
        # Name the vendor when we can — "blocked by datadome" is actionable in a
        # way "thin body" is not. The thin body is what makes it a block; the
        # ambient marker only says WHO.
        by = f", served by {ambient[0]!r}" if ambient else ""
        return (f"blocked — thin body, no JSON-LD and no <article>{by} "
                f"({len(body)} bytes < {limit}; too small to be the page)")
    if not strict and any(m in low for m in _AMBIENT_BLOCK_MARKERS):
        # SPEND path only: an ambient vendor marker on a full page is weak evidence,
        # but escalating costs one credit and this preserves the long-standing
        # behaviour. Never reached on the verdict path, where it would be a false
        # accusation against a page we actually received.
        hit = next(m for m in _AMBIENT_BLOCK_MARKERS if m in low)
        return f"possible anti-bot vendor {hit!r} present ({len(body)} bytes)"
    return None


def _looks_blocked(resp: requests.Response) -> bool:
    """Boolean form of `blocked_reason`, kept for the escalation call sites that
    only need yes/no. Used to decide whether to escalate an unblocker-ENABLED
    domain to the paid tier, so a slightly eager match merely spends one credit."""
    return blocked_reason(resp) is not None


def fetch_with_full_fallback(url: str, *,
                              timeout: int = DEFAULT_TIMEOUT_SECONDS,
                              try_wayback: bool = True,
                              unblocker: bool = False,
                              render: bool = False,
                              ) -> tuple[requests.Response, dict]:
    """Tiered fetch with an OPT-IN raw-page cache layered on top.

    The actual fetching is `_fetch_with_full_fallback_uncached`. When a caller has
    turned the page cache ON (the publisher harvest does, via `page_cache.enabled()`),
    a fresh cached page is returned without hitting the network, and a successful
    non-stub fetch is written back — so the harvest's is-recipe filter and its
    winner-extract share ONE fetch (no double-fetch / double unblocker credit), and
    re-harvests within the TTL skip the network entirely. Default-off: every other
    caller behaves exactly as before."""
    from input.pipeline import page_cache
    if page_cache.is_enabled():
        cached = page_cache.get(url, render)
        if cached is not None:
            return cached, {"source": "page-cache"}
    resp, meta = _fetch_with_full_fallback_uncached(
        url, timeout=timeout, try_wayback=try_wayback, unblocker=unblocker, render=render)
    if page_cache.is_enabled():
        try:
            if 200 <= getattr(resp, "status_code", 0) < 300 and not _looks_blocked(resp):
                page_cache.put(url, render, resp, meta)
        except Exception:
            pass
    return resp, meta


def _fetch_with_full_fallback_uncached(url: str, *,
                              timeout: int = DEFAULT_TIMEOUT_SECONDS,
                              try_wayback: bool = True,
                              unblocker: bool = False,
                              render: bool = False,
                              ) -> tuple[requests.Response, dict]:
    """Tiered fetch: direct UA chain → [paid unblocker, opt-in] → Wayback.

    Returns (response, meta) where meta["source"] is "direct" | "unblocker" |
    "wayback". `unblocker=True` (set by the caller when the domain's
    fetch_strategy='unblocker') means the domain is KNOWN to block direct fetches, so we
    go STRAIGHT to the PAID web-unblocker — skipping the wasted direct probe that would
    only return a 200 challenge stub. Direct + Wayback remain as backstops if the
    unblocker no-ops (no BYOK key) or fails. 404/410 stay terminal.
    render=False: the unblocker still solves the challenge (residential IP + session) but
    skips the slow real-browser JS render — recipe content/JSON-LD is in the static HTML,
    so it's ~3x faster (~5s vs ~17-30s) AND ~3x cheaper. (A JS-only recipe site would need
    render=True — none seen yet; revisit per-domain if so.)
    """
    err: Optional[Exception] = None
    # RENDER-FIRST for known JS-rendered domains (render_required): the static HTML is
    # only a nav shell — the article body is injected client-side — so the plain probe
    # would return useless chrome (and _looks_blocked can't catch a big, structurally-
    # normal-but-empty page). Go STRAIGHT to a real-browser render via the unblocker.
    # Falls through to plain/Wayback backstops if the render attempt no-ops/fails.
    if render and unblocker and unblocker_available():
        ub = fetch_via_unblocker(url, timeout=max(timeout, UNBLOCKER_TIMEOUT_SECONDS), render=True)
        if ub is not None:
            return ub
    # PLAIN FIRST (credit-conscious): try the free direct fetch; only escalate to the PAID
    # unblocker if the page comes back as an anti-bot stub (or the fetch fails). So a page
    # that loads fine plain costs ZERO unblocker credits — the unblocker is paid only when
    # actually needed. (unblocker=True just ENABLES the escalation tier for this domain.)
    try:
        resp, ua_used = fetch_with_ua_fallback(url, timeout=timeout)
        if unblocker and unblocker_available() and _looks_blocked(resp):
            err = requests.HTTPError(f"Soft-block challenge for {url} — escalating to unblocker")
        else:
            return resp, {"source": "direct", "ua_used": ua_used}
    except requests.HTTPError as e:
        # 404/410 came from a real response.raise_for_status() → terminal.
        status = getattr(e.response, "status_code", None) if e.response is not None else None
        if status in (404, 410):
            raise
        err = e
    except Exception as e:
        err = e

    # PAID unblocker tier — only reached when the plain fetch was blocked or failed.
    if unblocker and unblocker_available():
        ub = fetch_via_unblocker(url, timeout=max(timeout, UNBLOCKER_TIMEOUT_SECONDS), render=False)
        if ub is not None:
            return ub

    if not try_wayback:
        raise err if err is not None else requests.HTTPError(f"Direct fetch failed for {url}")
    wb = fetch_via_wayback(url, timeout=timeout)
    if wb is None:
        raise requests.HTTPError(
            f"Direct fetch failed and no Wayback snapshot available for {url}"
        )
    resp, ts = wb
    return resp, {"source": "wayback", "timestamp": ts}


def fetch_html(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS,
               user_agent: Optional[str] = None,
               try_wayback: bool = True,
               unblocker: bool = False,
               render: bool = False,
               ) -> tuple[str, str, dict]:
    """Return (html_text, final_url, meta) after redirects + fallbacks.

    Default behavior:
      - UA fallback chain (bot UA → Chrome UA)
      - On failure, Wayback Machine snapshot fallback
    Pass `user_agent=...` to force one UA + skip Wayback (used by tests).
    Pass `try_wayback=False` to keep the UA chain but disable Wayback.

    `meta` is a small dict describing the fetch:
      {"source": "direct", "ua_used": "..."}
      {"source": "wayback", "timestamp": "20260128152348"}

    Callers that don't care about meta can ignore the third tuple
    element — backward-compatible call sites continue to work because
    Python tuple unpacking is positional and the prior signature was
    `(html_text, final_url)`. Updated call sites pull the meta to
    stamp `_source.via = wayback:<ts>` provenance.
    """
    if user_agent is not None:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        resp.raise_for_status()
        return resp.text, resp.url, {"source": "direct", "ua_used": user_agent}
    resp, meta = fetch_with_full_fallback(url, timeout=timeout, try_wayback=try_wayback,
                                          unblocker=unblocker, render=render)
    return resp.text, resp.url, meta


def _is_recipe_type(node_type: Any) -> bool:
    if node_type == "Recipe":
        return True
    if isinstance(node_type, list) and "Recipe" in node_type:
        return True
    return False


# Leading <?xml ... encoding="..."?> declaration. lxml (under extruct) refuses
# to parse a *str* that carries one ("Unicode strings with encoding declaration
# are not supported"); some Joomla/older-CMS pages emit it inside what is served
# as text/html (e.g. e-sofia.gr). We strip it and retry.
_XML_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>", re.IGNORECASE)

# Un-wrap a Wayback Machine URL to the original publisher URL it archived.
# Wayback is a fetch *transport* for dead live pages; provenance, cache-key, and
# Moz scoring must key on the real publisher, not archive.org (DA 94). This
# handles the case where the INPUT url is already an archive URL (the batch
# stamps the working snapshot as the entry url for dead pages).
#
# The regex now lives in input.pipeline.url_utils — this was one of three
# independent copies, and the one place that most needed it (Moz scoring) had
# none of them. Thin alias so existing call sites here are untouched.
from input.pipeline.url_utils import unwrap_wayback as _unwrap_wayback_url  # noqa: E402


def extract_recipe_jsonld(html: str, base_url: str) -> list[dict]:
    """Return all JSON-LD objects whose @type is (or includes) Recipe.

    Handles the common @graph wrapper that schema.org sites use to nest
    multiple linked-data nodes inside a single script block.

    Resilient by contract: JSON-LD is an *optional* enrichment, so any parser
    failure returns [] rather than propagating — a page that can't be
    JSON-LD-parsed still converts to markdown and extracts via the LLM path.
    """
    try:
        data = extruct.extract(html, base_url=base_url, syntaxes=["json-ld"],
                               uniform=True)
    except ValueError as e:
        # Encoding-declaration case is recoverable: strip the leading <?xml ?>
        # and retry once. Anything else → JSON-LD is optional, return [].
        if "encoding declaration" in str(e).lower():
            try:
                data = extruct.extract(_XML_DECL_RE.sub("", html, count=1).lstrip(),
                                       base_url=base_url, syntaxes=["json-ld"],
                                       uniform=True)
            except Exception as e2:
                print(f"[jsonld] parse failed after decl-strip for {base_url!r}: {e2}")
                return []
        else:
            print(f"[jsonld] parse failed for {base_url!r}: {e}")
            return []
    except Exception as e:
        print(f"[jsonld] parse failed for {base_url!r}: {e}")
        return []
    out: list[dict] = []
    for item in data.get("json-ld", []) or []:
        if _is_recipe_type(item.get("@type")):
            out.append(item)
        for nested in item.get("@graph", []) or []:
            if isinstance(nested, dict) and _is_recipe_type(nested.get("@type")):
                out.append(nested)
    return out


# Selectors tried as candidate "main content" roots, in declarative priority
# order. We score every match (not just the first hit) — see pickBestRoot in
# forms/bookmarklet.js for the JS twin. KEEP THIS LIST IN SYNC with the
# bookmarklet's addAll() sequence; the two pickers should choose the same
# root for the same page so that batch-fetched and bookmarklet-captured
# markdown converge.
CANDIDATE_SELECTORS = [
    "[itemtype*='Recipe']",
    "[typeof*='Recipe']",
    ".wprm-recipe-container",
    ".tasty-recipes",
    ".mv-recipe-card",
    ".recipe-card",
    ".recipe",
    # Mediavine "create" recipe-card wrappers (cleanfoodiecravings.com and
    # other food blogs running Mediavine). The recipe lives in
    # .recipe-details, a sibling of <article>, so first-article-wins
    # picks the blog post and silently drops the recipe.
    ".recipe-details",
    "[data-slot-rendered-recipe]",
    "[class*='hrecipe']",
    "article",
    "main",
    "[role='main']",
    ".post-content",
    ".entry-content",
    ".article-content",
    ".post-body",
]

# Phrase list mirrored from bookmarklet.js RECIPE_PHRASES. Keep in sync.
RECIPE_PHRASES = [
    "teaspoon", "tablespoon", "tsp", "tbsp", "cup", "cups",
    " oz", " lb", " lbs", " ounce", " pound", "gram", " ml",
    "ingredients", "instructions", "directions", "method",
    "prep time", "cook time", "total time", "serves",
    "servings", "yield",
    "preheat", "bake", "boil", "simmer", "roast", "fry",
    "minutes", "whisk",
]


def _score_text(text: str) -> dict:
    """Score a candidate root's text. Mirrors scoreText in bookmarklet.js.

    chars + 100 * phraseHits — char count alone loses to recipe widgets
    embedded in long blog posts (the blog text inflates the wrapping
    container); phrase weighting lets a tight recipe widget outscore a
    bloated wrapper, but a wrapper that genuinely contains the recipe
    still wins over a sibling without it.
    """
    lower = (text or "").lower()
    hits = sum(1 for p in RECIPE_PHRASES if p in lower)
    chars = len(text or "")
    return {"chars": chars, "phrase_hits": hits, "score": chars + 100 * hits}


def select_main_content(soup: BeautifulSoup) -> Any:
    """Pick the most recipe-looking subtree of the page.

    Multi-candidate scoring picker, ported from forms/bookmarklet.js.
    Previous version did first-match-wins on
    ["[itemtype*='Recipe']", "article", "main", "body"]; that picked a
    blog-post <article> on sites where the recipe lived in a sibling
    .recipe-details widget (cleanfoodiecravings.com regression
    2026-05-27). Now: enumerate all candidates, clone+clean each, score,
    return winner.
    """
    candidates: list = []
    for sel in CANDIDATE_SELECTORS:
        try:
            candidates.extend(soup.select(sel))
        except Exception:
            pass
    if soup.body:
        candidates.append(soup.body)
    if not candidates:
        return soup

    seen: set[int] = set()
    unique = []
    for el in candidates:
        if id(el) in seen:
            continue
        seen.add(id(el))
        unique.append(el)

    best = None
    for el in unique:
        # Re-parse via str() gives an independent subtree so cleaning the
        # clone doesn't mutate the live tree (which would corrupt later
        # candidates that nest under it, e.g. .recipe inside body).
        # Cheap for typical recipe widgets (<5KB); body is the expensive
        # case but still <50ms in practice.
        clone_soup = BeautifulSoup(str(el), "lxml")
        clean_for_markdown(clone_soup)
        text = clone_soup.get_text(separator=" ", strip=True)
        s = _score_text(text)
        if best is None or s["score"] > best["score"]["score"]:
            best = {"el": el, "score": s}

    return best["el"]


def clean_for_markdown(node: Any) -> None:
    """Strip junk tags / chrome sections in-place from a bs4 node."""
    for tag_name in STRIP_TAGS:
        for t in node.find_all(tag_name):
            t.decompose()
    for sel in DROP_SELECTORS:
        for t in node.select(sel):
            t.decompose()


def _find_meta(soup: BeautifulSoup, *names: str) -> str:
    """Search <meta> tags for the first name/property match in the
    given list. Both `property=` and `name=` attribute forms are
    checked because sites spell og: vs twitter: vs article: tags
    inconsistently. Returns content string or empty."""
    for nm in names:
        for selector in ({"property": nm}, {"name": nm}):
            tag = soup.find("meta", attrs=selector)
            if tag and tag.get("content"):
                return tag["content"].strip()
    return ""


def extract_og_meta(soup: BeautifulSoup, base_url: str) -> dict:
    """Pull the useful Open Graph + Twitter Card + article: metadata
    from a page's <head>. Returns a dict — empty strings for any field
    the page didn't publish. Always returns the dict shape so callers
    can index by key without guarding.

    Fields (in order of preview-usefulness):
      - image         og:image (preferred) or twitter:image
                      The link-preview thumbnail URL. Cooped at extract.
      - description   og:description / twitter:description
                      The teaser sentence (typically 150-250 chars).
                      Useful for tile mouseovers, accessibility, and as
                      a fallback when our editorial.opinion isn't run.
      - imageAlt      og:image:alt / twitter:image:alt
                      Alt text for the image. Use on the cooped tile's
                      <img alt=…> for accessibility.
      - title         og:title (fallback to twitter:title)
                      Sometimes cleaner than <title> (no site-name suffix).
      - siteName      og:site_name
                      Human-readable site name ("Bon Appétit") vs the
                      raw hostname. Addresses the "friendly site-name
                      display" item that's been in the to-do list.
      - author        article:author
                      Author name OR URL — sites use both shapes.
      - publishedTime article:published_time (ISO 8601)
      - modifiedTime  article:modified_time (ISO 8601)

    Absolute-URL-resolves the image field against `base_url`; other
    fields are passed through as-is.
    """
    from urllib.parse import urljoin

    image_raw = _find_meta(soup, "og:image", "twitter:image")
    image_abs = ""
    if image_raw:
        try:
            image_abs = urljoin(base_url, image_raw)
        except Exception:
            image_abs = image_raw

    return {
        "image":         image_abs,
        "description":   _find_meta(soup, "og:description", "twitter:description"),
        "imageAlt":      _find_meta(soup, "og:image:alt", "twitter:image:alt"),
        "title":         _find_meta(soup, "og:title", "twitter:title"),
        "siteName":      _find_meta(soup, "og:site_name", "application-name"),
        "author":        _find_meta(soup, "article:author", "author"),
        "publishedTime": _find_meta(soup, "article:published_time"),
        "modifiedTime":  _find_meta(soup, "article:modified_time"),
    }


def extract_og_image(soup: BeautifulSoup, base_url: str) -> str:
    """Backward-compat shim — returns just the image URL from the
    fuller `extract_og_meta` result. Kept so existing callers that
    only care about the image field don't have to thread the dict.
    """
    return extract_og_meta(soup, base_url).get("image", "")


def html_to_markdown(url: str, timings: Optional[dict] = None, *,
                     unblocker: bool = False, render: bool = False) -> dict:
    """Fetch a URL and produce canonical markdown for recipe extraction.

    Returns dict with:
        markdown    str         ready to hand to extract.markdown_to_recipe
        source_url  str         final URL after redirects
        title       str         <title> tag text, best-effort
        has_jsonld  bool        whether a schema.org Recipe block was found
        jsonld      list[dict]  the parsed Recipe JSON-LD object(s); empty
                                list when none. Lets callers take the fast
                                JSON-LD-direct path without re-parsing the
                                markdown's fenced block.

    If `timings` is provided it is populated in place with:
        fetch_ms        time spent in requests.get
        html_parse_ms   bs4 + extruct + markdownify combined
    """
    t0 = time.perf_counter()
    html, final_url, fetch_meta = fetch_html(url, unblocker=unblocker, render=render)
    t_fetch = time.perf_counter()
    if timings is not None:
        timings["fetch_ms"] = int((t_fetch - t0) * 1000)
        timings["fetch_source"] = fetch_meta.get("source")
        if fetch_meta.get("source") == "wayback":
            timings["wayback_timestamp"] = fetch_meta.get("timestamp")

    base_url = get_base_url(html, final_url)
    # When the page was served from the Wayback Machine, `final_url` is an
    # archive.org URL — but that is only the fetch *transport*. For provenance,
    # cache-keying, and Moz scoring we must use the ORIGINAL site URL, or the
    # recipe gets stamped (and scored) as archive.org (DA 94) instead of the
    # real publisher. base_url stays the archive URL so relative links in the
    # archived HTML still resolve.
    source_url = url if fetch_meta.get("source") == "wayback" else final_url
    # Un-wrap an archive.org URL to the original publisher — covers BOTH an
    # internal Wayback fallback (final_url is archive) AND an input that is
    # already an archive snapshot (the batch passes the working snapshot for a
    # dead live page). Without this the recipe is keyed/scored as archive.org.
    source_url = _unwrap_wayback_url(source_url)
    soup = BeautifulSoup(html, "lxml")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    recipes_jsonld = extract_recipe_jsonld(html, base_url)
    og_meta = extract_og_meta(soup, base_url)
    og_image = og_meta.get("image", "")  # kept for back-compat with callers

    main = select_main_content(soup)
    clean_for_markdown(main)
    body_md = markdownify(str(main), heading_style="ATX",
                          strip=STRIP_TAGS).strip()
    if timings is not None:
        timings["html_parse_ms"] = int((time.perf_counter() - t_fetch) * 1000)

    parts: list[str] = []
    if title:
        parts.append(f"# {title}\n")
    parts.append(f"URL: {final_url}\n")

    if recipes_jsonld:
        payload = recipes_jsonld[0] if len(recipes_jsonld) == 1 else recipes_jsonld
        parts.append("## STRUCTURED RECIPE DATA (JSON-LD)\n")
        parts.append("```json")
        parts.append(json.dumps(payload, indent=2, ensure_ascii=False))
        parts.append("```\n")

    parts.append("## PAGE CONTENT\n")
    parts.append(body_md)

    # Language detection runs on the visible body text (no scripts /
    # style noise). <html lang="..."> first, then Content-Language
    # header (if present in the response — fetch_html doesn't return
    # it directly, so we only get the HTML signal here), then fasttext
    # on body text. Stamped on the return dict so extract_recipe_from_url
    # can decide whether to invoke the translation pipeline without
    # re-parsing the HTML.
    try:
        from intake.translate import detect_language
        body_text = main.get_text(separator=" ", strip=True) if hasattr(main, "get_text") else ""
        detected_lang = detect_language(html, headers=None, visible_text=body_text)
    except Exception:
        detected_lang = "en"

    return {
        "markdown": "\n".join(parts),
        "source_url": final_url,
        "title": title,
        "has_jsonld": bool(recipes_jsonld),
        "jsonld": recipes_jsonld,
        "og_image": og_image,   # kept for back-compat
        "og_meta": og_meta,     # full dict — see extract_og_meta docstring
        "language": detected_lang,  # ISO 639-1; 'en' as conservative default.
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python -m to_markdown.html_to_markdown <url>")
        sys.exit(1)
    result = html_to_markdown(sys.argv[1])
    print(f"=== source_url: {result['source_url']}")
    print(f"=== title: {result['title']}")
    print(f"=== has_jsonld: {result['has_jsonld']}")
    print("=== markdown:")
    print(result["markdown"])