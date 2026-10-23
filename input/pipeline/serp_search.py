"""serp_search.py — the single, provider-agnostic Google SERP fetch chokepoint.

One canonical fetcher behind a config-selected provider, so we can A/B vendors and
switch without touching callers ([[single-path]]). Default = **SerpApi**, byte-for-
byte the current behavior. **Scale SERP** (Traject Data) is staged for the planned
switch — cheaper at our future scale + vendor consolidation with Rainforest (the
product-data source). Flip the `serp_provider` setting to "scaleserp" only AFTER a
fidelity A/B passes (our rankings trust Google's exact result set + order — the bar
DataForSEO failed). See memory project_serp_provider; A/B harness: scripts/serp_ab.py.

Contract: callers pass a VERBATIM `q` (we never construct the query) + how many
10-result pages deep. Returns normalized [{link, title, rank}] aggregated across
pages, deduped, in rank order. Empty list if the active provider's key is absent
(every caller already no-ops on []). Mirrors the prior _serp_links behavior:
filter=0 (no Google similar-page collapsing), optional early-stop at `want`.
"""
from __future__ import annotations

import os
import re
import time
import requests

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
SCALESERP_ENDPOINT = "https://api.scaleserp.com/search"
_PAGE_SIZE = 10  # Google organic results per page (protocol constant)

_SCALESERP_ALIASES = {"scaleserp", "scale_serp", "scale-serp", "scale"}


def _scaleserp_key() -> str | None:
    """Scale SERP key, accepting the common env-var spellings."""
    return (os.getenv("SCALESERP_KEY") or os.getenv("SCALE_SERP_API_KEY")
            or os.getenv("SCALESERP_API_KEY"))


def _serp_retry_cfg() -> tuple[int, float]:
    """(attempts, backoff_seconds) for transient per-page SERP retries. Config-overridable
    (system_config `serp_page_retries` / `serp_page_retry_backoff`); defaults 3 / 2.0s."""
    attempts, backoff = 3, 2.0
    try:
        from input.pipeline import system_config as _cfg
        attempts = int(_cfg.get_setting("serp_page_retries", attempts) or attempts)
        backoff = float(_cfg.get_setting("serp_page_retry_backoff", backoff) or backoff)
    except Exception:
        pass
    return max(1, attempts), max(0.0, backoff)


def _serp_dry_cfg() -> int:
    """Consecutive pages that add NO new usable result (dead page, all blocked hosts,
    dropped off-topic) before a line stops paging. Config-overridable (system_config
    `serp_dry_pages`); default 3. Why: 'Sauce Jean Vignard' #2788 (2026-10-23) - a thin
    dish whose Google answer is mostly Pinterest/YouTube paged to p19 on a line whose
    usable count sat at 15 from p9, dead pages never two in a row; the curator
    cancelled at 13 min and the 32 usable URLs went with the job."""
    n = 3
    try:
        from input.pipeline import system_config as _cfg
        n = int(_cfg.get_setting("serp_dry_pages", n) or n)
    except Exception:
        pass
    return max(1, n)


# Pages that exhausted their retries this process — each one is ~10 lost
# candidates. Harvest runs pop this at the end and ALERT (input.pipeline.alerts)
# instead of letting the loss hide in a mid-log line (curator, 2026-08-26).
_page_losses: list[dict] = []


def pop_page_losses() -> list[dict]:
    """Return-and-clear the losses recorded since the last pop."""
    global _page_losses
    out, _page_losses = _page_losses, []
    return out


# Words that say nothing about WHICH dish a query is after.
_GENERIC_Q = {"recipe", "recipes", "best", "easy", "how", "make", "the", "and", "with",
              "for", "homemade", "classic", "simple", "quick", "authentic", "traditional"}


def _off_topic_page(query: str, org: list) -> bool:
    """Is this result page about something ELSE?

    Since ~2026-09-17 the provider's localized deep pages intermittently come back
    as a full, successful page for a DIFFERENT query: page 2 of "Chicken Cordon
    Bleu Recipe" (gl=us hl=en) returned "1 Hour of Lazy Recipes", "The Recipe
    Critic: Best Recipes", "The Best Neapolitan Pizza" - 0 of 10 about the dish,
    while pages 1 and 3 of the same query were 6/6 and 9/10. Those strangers are
    real recipes on strong sites, so they pass is-recipe, outrank honest pages on
    authority and get SAVED as winners: an easy-kimchi and a banana pudding became
    Apple Crumble and Chicken Cordon Bleu winners (jobs #2129/#2132, 2026-09-18).

    A page is off-topic when NOT ONE result mentions any distinctive query word in
    its title, link or snippet. Exclusion operators, site: scopes and generic words
    ("recipe", "best") are not distinctive. Non-ASCII queries are never judged (a
    Chinese title need not repeat the query string), nor are queries left with no
    distinctive word - when unsure, the page stands."""
    words = [w for w in re.findall(r"[a-z]{3,}", re.sub(r"(^|\s)-\S+|\bsite:\S+", " ", query.lower()))
             if w not in _GENERIC_Q]
    if not words or not query.isascii() or len(org) < 4:
        return False
    # On-topic = a result carrying MOST of the distinctive words, not just one:
    # the strangers served for "Best Chicken Kiev" were chicken parmesan, chicken
    # adobo and chicken salad - every one says "chicken", none says "kiev".
    stems = list(dict.fromkeys(w[:5] for w in words))
    for it in org:
        hay = " ".join(str(it.get(k) or "") for k in ("title", "link", "snippet")).lower()
        if sum(st in hay for st in stems) * 2 > len(stems):
            return False
    return True


def _cancel_check(should_cancel, label: str, page) -> None:
    """Cooperative cancel INSIDE the paging loop (2026-10-13). A cancelled dish run
    kept searching for four minutes - seven pages per line, each with up to three
    retries and a back-off sleep - because the flag was only read at the fetch loop
    that comes after the search. Checked before every page request and before
    every retry sleep; raises JobCancelled like the fetch loop does."""
    if should_cancel and should_cancel():
        from input.pipeline.jobs import JobCancelled
        raise JobCancelled(f"cancelled during search ({label} page {page})")


def _backoff(seconds: float, should_cancel, label: str, page) -> None:
    """Retry sleep that a cancel can cut short (checked before the sleep)."""
    _cancel_check(should_cancel, label, page)
    time.sleep(seconds)


def _serp_get_json(endpoint, params, timeout, *, label: str, page,
                   should_cancel=None) -> dict | None:
    """GET + parse JSON for one SERP page, with retries on TRANSIENT network errors
    (Timeout / ConnectionError). Returns the parsed dict, or None if the page exhausted
    its retries or hit a non-transient error — in which case the caller stops paging.

    Why: a single page-level ReadTimeout used to `break` the whole pagination loop,
    silently truncating a deep query (e.g. target 75) to whatever had already arrived
    (seen as '20 URLs (target 75)'). Retrying the page rides out a blip instead."""
    attempts, backoff = _serp_retry_cfg()
    for attempt in range(1, attempts + 1):
        try:
            return requests.get(endpoint, params=params, timeout=timeout).json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < attempts:
                print(f"  [{label}] page {page} {type(e).__name__} "
                      f"(attempt {attempt}/{attempts}) — retrying in {backoff}s")
                _backoff(backoff, should_cancel, label, page)
            else:
                print(f"  [{label}] page {page} failed after {attempts} attempts: "
                      f"{type(e).__name__}: {e}")
                _page_losses.append({"label": label, "page": page,
                                     "query": str(params.get("q") or params.get("search_term") or "?"),
                                     "error": f"{type(e).__name__}: {e}"})
        except Exception as e:   # non-transient (bad JSON, etc.) — don't spin, stop paging
            print(f"  [{label}] page {page} failed: {type(e).__name__}: {e}")
            return None
    return None


def active_provider() -> str:
    """Config-selected provider (default serpapi). system_config is optional —
    fall back to the SERP_PROVIDER env var, then serpapi."""
    try:
        from input.pipeline import system_config
        return (system_config.get_setting("serp_provider", "") or
                os.getenv("SERP_PROVIDER") or "serpapi").lower()
    except Exception:
        return (os.getenv("SERP_PROVIDER") or "serpapi").lower()


def has_key(provider: str | None = None) -> bool:
    prov = (provider or active_provider()).lower()
    if prov in _SCALESERP_ALIASES:
        return bool(_scaleserp_key())
    return bool(os.getenv("SERPAPI_KEY"))


# Google's `hl` codes are NOT bare ISO-639 for Chinese: the vendors validate
# against Google's list (zh-cn / zh-tw), so a query row written as hl=zh was
# rejected outright ("'hl' parameter is invalid", Xiamen Chow Mei Fun, job #2008,
# 2026-09-13) and the whole Chinese-language search silently contributed zero
# results. Normalize ONCE here, at the chokepoint both providers share; the
# query rows keep their 2-letter form (the rest of the pipeline compares hl[:2]).
_HL_TRADITIONAL_GL = {"tw", "hk", "mo"}
_HL_ALIASES = {"zh-hans": "zh-cn", "zh_cn": "zh-cn", "zh-hant": "zh-tw", "zh_tw": "zh-tw"}


def google_hl(hl: str | None, gl: str | None = None) -> str | None:
    """Map a query row's language to a code Google (and both SERP vendors) accept."""
    if not hl:
        return hl
    h = hl.strip().lower()
    h = _HL_ALIASES.get(h, h)
    if h == "zh":
        h = "zh-tw" if (gl or "").lower() in _HL_TRADITIONAL_GL else "zh-cn"
    return h


def serp_search(query: str, pages: int = 7, *, want: int | None = None,
                gl: str | None = None, hl: str | None = None,
                provider: str | None = None, timeout: int = 60,
                skip_hosts: set[str] | frozenset[str] | None = None,
                should_cancel=None) -> list[dict]:
    """Verbatim Google search. `pages` = how many 10-result pages to fetch; `want`
    early-stops once that many results are collected (truncates). `provider` forces
    a specific vendor (for the A/B); otherwise the configured one. Returns
    [{link, title, rank}] deduped in rank order.

    `skip_hosts` (root domains, 2026-10-12): results on these hosts are still
    returned - flagged `blocked: True`, so the caller's filter can record them -
    but they do NOT count toward `want`, and paging continues until `want` USABLE
    results are in hand (or `pages` runs out). Why: since the `-site:` exclusions
    were switched off (2026-09-18, provider fault) Google fills 5-8 of every 7-9
    organic slots with YouTube/Facebook videos; a 40-result dish line stopped at
    page 5 with ~12 real pages (Panna Cotta #2481: 66 unique, 47 blocked). Google
    ignores `-site:` for video results and udm=14 has no effect via the provider
    (tested x4, 2026-10-12), so the budget has to be kept on OUR side."""
    prov = (provider or active_provider()).lower()
    hl = google_hl(hl, gl)
    skip = {h.lower() for h in (skip_hosts or ())}
    if prov in _SCALESERP_ALIASES:
        return _scaleserp(query, pages, want, gl, hl, timeout, skip, should_cancel=should_cancel)
    return _serpapi(query, pages, want, gl, hl, timeout, skip, should_cancel=should_cancel)


# Google's result-link wrapper (rolled out 2026-08-26, confirmed by Google as an
# anti-abuse measure): organic `link`s arrive as google.com/goto?url=<opaque token>
# instead of the publisher URL, and our SERP provider passes them through verbatim.
# Intermittent since 09-08, then EVERY link of every dish refresh from Tzatziki
# #2662 (2026-10-16 07:13Z) on - the French 'Poulet à la Moutarde' #2672 got 40/40.
# Downstream a wrapper is worthless: the URL-word pre-filter sees no words and
# skips it (448 of 556 that morning), the fetcher follows the redirect and KEEPs
# the page but Moz has no row for a google.com URL (the rest), and the ledger files
# the candidate under google.com. The token does not decode; Google answers the
# wrapper with a 302 whose Location IS the publisher URL, so one HEAD-sized request
# (no page body) recovers it. The legacy google.com/url?q=<url> form carries the
# URL in the clear.
# /goto and /url are redirect wrappers (resolved by a HEAD-less GET below).
# translate.google.com/translate?u=<page> is Google's TRANSLATION PROXY: a Hindi
# SERP for Butter Chicken (2026-10-17) returned two English blogs behind it, and
# they reached Moz as translate.google.com URLs, which Moz has never crawled.
# The publisher page is the `u` parameter; no request needed.
_GOOGLE_WRAPPER = re.compile(
    r"^https?://(?:(?:www\.)?google\.[a-z.]{2,8}/(?:goto|url)\?|translate\.google\.[a-z.]{2,8}/translate\?)", re.I)
_RESOLVE_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
)


def is_google_wrapper(link: str) -> bool:
    return bool(link) and bool(_GOOGLE_WRAPPER.match(link))


def unwrap_google_redirect(link: str, timeout: float = 10.0) -> str | None:
    """The publisher URL behind a google.com/goto (or /url) wrapper, or None when
    Google would not say (non-3xx, or a Location that is not an http(s) URL off
    google.com). Reads the redirect's Location WITHOUT following it. Two user
    agents, because Google's edge answers the odd request with a 403 for a given
    UA and 302 for the next (seen in the fetch ladder's escalations on these links)."""
    from urllib.parse import parse_qs, unquote, urlsplit
    if not is_google_wrapper(link):
        return link
    sp = urlsplit(link)
    if sp.path.rstrip("/").endswith("/url") or sp.netloc.lower().startswith("translate.google."):
        q = parse_qs(sp.query)
        for k in ("q", "url", "u"):
            v = (q.get(k) or [""])[0]
            if v.startswith(("http://", "https://")):
                return unquote(v)
        if sp.netloc.lower().startswith("translate.google."):
            return None              # a translate URL with no page behind it: nothing to fetch
    for ua in _RESOLVE_UAS:
        try:
            r = requests.get(link, allow_redirects=False, timeout=timeout,
                             headers={"User-Agent": ua, "Accept-Language": "en"})
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            continue
        loc = (r.headers.get("Location") or "").strip()
        if (300 <= r.status_code < 400 and loc.startswith(("http://", "https://"))
                and not is_google_wrapper(loc)
                and "google." not in urlsplit(loc).netloc.lower()):
            return loc
        if r.status_code in (301, 302, 303, 307, 308):
            break                    # a redirect to somewhere useless - another UA won't change it
    return None


def _unwrap_page(org: list[dict]) -> tuple[list[str | None], int, int]:
    """Resolve a page's wrapped links in parallel (one small request each). Returns
    the per-item link list (None = wrapper Google would not resolve) plus the
    (wrapped, unresolved) counts for the page line."""
    from concurrent.futures import ThreadPoolExecutor
    links = [(it.get("link") or it.get("url") or "") for it in org]
    idx = [i for i, l in enumerate(links) if is_google_wrapper(l)]
    if not idx:
        return links, 0, 0
    with ThreadPoolExecutor(max_workers=min(6, len(idx))) as ex:
        resolved = list(ex.map(unwrap_google_redirect, [links[i] for i in idx]))
    out: list[str | None] = list(links)
    for i, r in zip(idx, resolved):
        out[i] = r
    return out, len(idx), sum(1 for r in resolved if r is None)


def _collect_page(org: list[dict], out: list[dict], seen: set[str], skip: set[str],
                  *, label: str, page: int) -> int:
    """Append one page's organic results to `out` (deduped, ranked in arrival order),
    flagging `skip`-host results `blocked`. Returns the number of USABLE results in
    `out` so far. Prints the page's usable/blocked split when anything was blocked,
    and the wrapper split when Google wrapped any link (see _GOOGLE_WRAPPER)."""
    from input.pipeline.url_utils import root_domain
    n_blocked, hosts = 0, {}
    links, n_wrapped, n_unresolved = _unwrap_page(org)
    if n_wrapped:
        print(f"  [{label}] page {page}: {n_wrapped} of {len(org)} links came as google.com/goto "
              f"wrappers - {n_wrapped - n_unresolved} resolved to the publisher URL"
              + (f", {n_unresolved} Google would not resolve (dropped)" if n_unresolved else ""))
    for it, link in zip(org, links):
        if not link or link in seen:
            continue
        seen.add(link)
        row = {"link": link, "title": it.get("title") or "", "rank": len(out) + 1}
        host = root_domain(link) if skip else ""
        if host and host.lower() in skip:
            row["blocked"] = True
            n_blocked += 1
            hosts[host] = hosts.get(host, 0) + 1
        out.append(row)
    usable = sum(1 for r in out if not r.get("blocked"))
    if n_blocked:
        top = ", ".join(f"{h} x{n}" for h, n in sorted(hosts.items(), key=lambda kv: -kv[1])[:3])
        print(f"  [{label}] page {page}: {len(org)} organic, {n_blocked} on blocked hosts "
              f"({top}) - {usable} usable so far")
    return usable


def _trim(out: list[dict], want: int | None) -> list[dict]:
    """Cut `out` after the `want`-th USABLE result (blocked rows between stay)."""
    if want is None:
        return out
    n = 0
    for i, r in enumerate(out):
        if not r.get("blocked"):
            n += 1
            if n >= want:
                return out[: i + 1]
    return out


def serp_image_search(query: str, *, want: int = 5, gl: str | None = None,
                      hl: str | None = None, provider: str | None = None,
                      timeout: int = 60) -> list[dict]:
    """Google Images via the active SERP vendor — FETCH-FREE, the vendor absorbs
    the target site's anti-bot. For thumbnailing recipes on sites we can't fetch
    (thekitchn etc.): the og:image direct fetch is blocked, but Google already has
    the image. Returns up to `want` normalized
    [{image, thumbnail, title, source_link, source_domain, rank}] in result order.
    1 credit/call. Empty list if the active provider's key is absent."""
    prov = (provider or active_provider()).lower()
    hl = google_hl(hl, gl)
    if prov in _SCALESERP_ALIASES:
        return _scaleserp_images(query, want, gl, hl, timeout)
    return _serpapi_images(query, want, gl, hl, timeout)


def _img_host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").replace("www.", "")
    except Exception:
        return ""


def _serpapi_images(query, want, gl, hl, timeout) -> list[dict]:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return []
    params = {"engine": "google_images", "q": query, "api_key": key}
    if gl:
        params["gl"] = gl
    if hl:
        params["hl"] = hl
    try:
        res = requests.get(SERPAPI_ENDPOINT, params=params, timeout=timeout).json()
        imgs = res.get("images_results") or []
    except Exception as e:
        print(f"  [serpapi-images] failed: {type(e).__name__}: {e}")
        return []
    out = []
    for it in imgs[:want]:
        image = it.get("original") or it.get("thumbnail") or ""
        if not image:
            continue
        link = it.get("link") or ""
        out.append({"image": image, "thumbnail": it.get("thumbnail") or image,
                    "title": it.get("title") or "", "source_link": link,
                    "source_domain": (it.get("source") or _img_host(link)),
                    "rank": len(out) + 1})
    return out


def _scaleserp_images(query, want, gl, hl, timeout) -> list[dict]:
    key = _scaleserp_key()
    if not key:
        return []
    params = {"api_key": key, "q": query, "output": "json", "search_type": "images"}
    if gl:
        params["gl"] = gl
    if hl:
        params["hl"] = hl
    try:
        data = requests.get(SCALESERP_ENDPOINT, params=params, timeout=timeout).json()
        imgs = data.get("image_results") or []
    except Exception as e:
        print(f"  [scaleserp-images] failed: {type(e).__name__}: {e}")
        return []
    if not imgs:
        ri = data.get("request_info") or {}
        if not ri.get("success"):
            print(f"  [scaleserp-images] {ri.get('message') or data.get('error')}")
        return []
    out = []
    for it in imgs[:want]:
        image = it.get("image") or it.get("original") or ""
        if not image:
            continue
        link = it.get("link") or it.get("source") or ""
        out.append({"image": image, "thumbnail": it.get("thumbnail") or image,
                    "title": it.get("title") or "", "source_link": link,
                    "source_domain": (it.get("domain") or _img_host(link)),
                    "rank": len(out) + 1})
    return out


def _serpapi(query, pages, want, gl, hl, timeout, skip: set[str] = frozenset(),
             should_cancel=None) -> list[dict]:
    key = os.getenv("SERPAPI_KEY")
    if not key:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    empty_streak = 0   # see _scaleserp: Google leaves dead pages mid-results
    dry_limit = _serp_dry_cfg()
    dry_streak, last_usable = 0, 0   # pages in a row that added no usable result
    for p in range(max(1, pages)):
        _cancel_check(should_cancel, "serpapi", p + 1)
        params = {"engine": "google", "q": query, "num": _PAGE_SIZE,
                  "start": p * _PAGE_SIZE, "filter": 0, "api_key": key}
        if gl:
            params["gl"] = gl
        if hl:
            params["hl"] = hl
        data = _serp_get_json(SERPAPI_ENDPOINT, params, timeout, label="serpapi", page=p + 1,
                              should_cancel=should_cancel)
        if data is None:   # page exhausted retries / non-transient error → stop paging
            break
        org = data.get("organic_results") or []
        if not org:
            empty_streak += 1
            dry_streak += 1
            if empty_streak >= 2:
                print(f"  [serpapi] page {p + 1}: second empty page in a row — end of results "
                      f"({len(out)} collected)")
                break
            if dry_streak >= dry_limit:
                print(f"  [serpapi] page {p + 1}: empty, and {dry_streak} pages in a row added "
                      f"nothing usable — stopping with {last_usable} usable")
                break
            print(f"  [serpapi] page {p + 1}: empty page — Google's dead page, stepping "
                  f"over it ({len(out)} collected)")
            continue
        empty_streak = 0
        usable = _collect_page(org, out, seen, skip, label="serpapi", page=p + 1)
        if want is not None and usable >= want:
            break
        if usable > last_usable:
            dry_streak, last_usable = 0, usable
        else:
            dry_streak += 1
            if dry_streak >= dry_limit:
                print(f"  [serpapi] page {p + 1}: {dry_streak} pages in a row added nothing "
                      f"usable — stopping with {usable} usable")
                break
    return _trim(out, want)


def _scaleserp(query, pages, want, gl, hl, timeout, skip: set[str] = frozenset(),
               should_cancel=None) -> list[dict]:
    """Scale SERP (Traject Data). LOOP the 1-based `page` param (like SerpApi's
    `start`), one page per request, with early-stop at `want`. We don't use
    `max_page` — it's hard-capped at 10 (400 above) AND a 10-page server-side fetch
    risks the request timeout (the silent-0 bug). Page-looping has no depth cap, is
    uniform with the SerpApi path, and early-stops cheaply. Same verbatim `q`;
    response: organic_results[] with link/title/position. 1 credit per page."""
    key = _scaleserp_key()
    if not key:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    empty_streak = 0   # consecutive empty pages; Google leaves DEAD pages mid-results
    dry_limit = _serp_dry_cfg()
    dry_streak, last_usable = 0, 0   # pages in a row that added no usable result
    for p in range(1, max(1, pages) + 1):
        _cancel_check(should_cancel, "scaleserp", p)
        # filter=0 = Google's "show omitted similar results". The SerpApi path
        # always sent it; this one never did, and without it Google ends a dish
        # query at ~page 7 (Panna Cotta 2026-10-12: p8 empty, no `next`; with
        # filter=0 pages 8-11 are full). Invisible while blocked hosts were not
        # spending the budget; decisive now that lines page deeper.
        params = {"api_key": key, "q": query, "output": "json", "page": p, "filter": "0"}
        if gl:
            params["gl"] = gl
        if hl:
            params["hl"] = hl
        data = _serp_get_json(SCALESERP_ENDPOINT, params, timeout, label="scaleserp", page=p, should_cancel=should_cancel)
        if data is None:   # page exhausted retries / non-transient error → stop paging
            break
        org = data.get("organic_results") or []
        if not org:
            ri = data.get("request_info") or {}
            if not ri.get("success"):
                # A DELIVERED failure can still be transient (rate limit, engine
                # overloaded, temporary) — the ATK parchment case (2026-09-04)
                # accepted one such page instantly and a whole named source
                # vanished from the run. Retry those through the same budget the
                # network errors get; only a non-transient message (credits,
                # bad request) stops on first sight.
                msg = str(ri.get("message") or data.get("error") or "")
                # "unable to fulfil your request at this time, please retry. You
                # have not been charged" (Strawberry Cake #2185, 2026-09-20) matched
                # none of these words and ended the query at 7 of 40 URLs.
                if re.search(r"rate|limit|overload|temporar|try again|timeout|busy|"
                             r"please retry|unable to fulfil|not been charged",
                             msg, re.I):
                    attempts, backoff = _serp_retry_cfg()
                    for extra in range(2, attempts + 1):
                        print(f"  [scaleserp] page {p}: transient provider error "
                              f"({msg[:60]}) — retry {extra}/{attempts} in {backoff}s")
                        _backoff(backoff, should_cancel, "scaleserp", p)
                        data = _serp_get_json(SCALESERP_ENDPOINT, params, timeout,
                                              label="scaleserp", page=p, should_cancel=should_cancel)
                        org = (data or {}).get("organic_results") or []
                        if org:
                            break
                        ri = (data or {}).get("request_info") or {}
                        msg = str(ri.get("message") or (data or {}).get("error") or "")
                if not org:
                    if not ri.get("success"):  # real error (e.g. out of credits), not a silent 0
                        print(f"  [scaleserp] page {p}: {msg or 'unsuccessful response'}")
                    break
            else:
                # A SUCCESSFUL page with no organic results is the END of the
                # results. This branch used to fall through and keep paging: a
                # query whose page 2 came back empty went on to request pages
                # 3..20, one billed credit each, for nothing (Pasta al Forno,
                # 2026-09-18: 19 wasted credits per search line). Say so, stop.
                # ...or the provider's intermittent empty success (the 09-17 fault),
                # which the same request a moment later answers in full. Measured
                # 2026-10-12 on 'Panna Cotta': three runs ended "no results" at p6,
                # p7 and p8 while direct probes of p7-p9 minutes apart were full and
                # p12 was the real end. `pagination.next` is NOT a reliable tell
                # (the false ends came without one). So: an empty page is retried
                # through the same budget transient errors get, and only an empty
                # page on every try is an ending. Bounded waste: `attempts` credits
                # per line at the true end, against half the candidates lost. Now
                # that blocked hosts no longer spend the budget, lines page deeper
                # and meet this more often.
                has_next = bool((data.get("pagination") or {}).get("next"))
                attempts, backoff = _serp_retry_cfg()
                for extra in range(2, attempts + 1):
                    print(f"  [scaleserp] page {p}: empty page (provider lists a next page: "
                          f"{'yes' if has_next else 'no'}) — retry {extra}/{attempts} in {backoff}s")
                    _backoff(backoff, should_cancel, "scaleserp", p)
                    data = _serp_get_json(SCALESERP_ENDPOINT, params, timeout,
                                          label="scaleserp", page=p, should_cancel=should_cancel) or {}
                    org = data.get("organic_results") or []
                    if org:
                        break
                    has_next = has_next or bool((data.get("pagination") or {}).get("next"))
                if not org:
                    # Google leaves DEAD pages in the middle of a result set: 'Panna
                    # Cotta' p8 is empty on BOTH providers, twice each, while p9 and
                    # p11 are full (2026-10-12). One dead page is stepped over (not a
                    # loss to alert on — it is Google's answer); two in a row is the
                    # end. Bounded waste: one extra credit per line at the true end.
                    empty_streak += 1
                    dry_streak += 1
                    if empty_streak >= 2:
                        print(f"  [scaleserp] page {p}: second empty page in a row — end of "
                              f"results ({len(out)} collected)")
                        break
                    if dry_streak >= dry_limit:
                        print(f"  [scaleserp] page {p}: empty, and {dry_streak} pages in a row "
                              f"added nothing usable — stopping with {last_usable} usable")
                        break
                    print(f"  [scaleserp] page {p}: empty on every try — Google's dead "
                          f"page, stepping over it ({len(out)} collected)")
                    continue
        empty_streak = 0
        if _off_topic_page(query, org):
            # A stranger's results. Ask again; a page that stays off-topic is
            # DROPPED (its ten URLs never enter the pool) and recorded as a lost
            # page so the run alerts - then paging continues, the next page is
            # usually fine.
            # A GLITCH changes when asked again; a REAL Google page does not. The
            # one honest page the guard flags in 730 replayed pages was
            # '"Macaroni and Cheese"' page 2, all "Mac and Cheese" titles - the
            # same ten links every time. Same links back = Google's real answer:
            # keep it, as before, and let the downstream filters judge it.
            attempts, backoff = _serp_retry_cfg()
            first = {it.get("link") for it in org if it.get("link")}
            stable = False
            for extra in range(2, attempts + 1):
                print(f"  [scaleserp] page {p}: OFF-TOPIC results (none mention the query) "
                      f"- retry {extra}/{attempts} in {backoff}s")
                _backoff(backoff, should_cancel, "scaleserp", p)
                data = _serp_get_json(SCALESERP_ENDPOINT, params, timeout,
                                      label="scaleserp", page=p, should_cancel=should_cancel)
                org = (data or {}).get("organic_results") or []
                if org and not _off_topic_page(query, org):
                    break
                again = {it.get("link") for it in org if it.get("link")}
                if first and len(first & again) * 10 >= len(first) * 7:
                    stable = True
                    print(f"  [scaleserp] page {p}: same results on retry - Google's real "
                          f"page, kept")
                    break
            if stable:
                pass
            elif not org or _off_topic_page(query, org):
                print(f"  [scaleserp] page {p}: still off-topic - page DROPPED")
                _page_losses.append({"label": "scaleserp", "page": p, "query": query,
                                     "error": "off-topic results (provider served another query)"})
                dry_streak += 1
                if dry_streak >= dry_limit:
                    print(f"  [scaleserp] page {p}: {dry_streak} pages in a row added nothing "
                          f"usable — stopping with {last_usable} usable")
                    break
                continue
        usable = _collect_page(org, out, seen, skip, label="scaleserp", page=p)
        if want is not None and usable >= want:
            break
        if usable > last_usable:
            dry_streak, last_usable = 0, usable
        else:
            dry_streak += 1
            if dry_streak >= dry_limit:
                print(f"  [scaleserp] page {p}: {dry_streak} pages in a row added nothing "
                      f"usable — stopping with {usable} usable")
                break
    return _trim(out, want)
