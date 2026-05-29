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
    try:
        avail = requests.get(
            WAYBACK_AVAILABILITY_URL,
            params={"url": url},
            timeout=timeout,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        )
        if not (200 <= avail.status_code < 300):
            return None
        data = avail.json()
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
        if not (200 <= resp.status_code < 300):
            return None
    except Exception as e:
        print(f"[wayback] fetch failed for {raw_url!r}: {e}")
        return None
    print(f"[wayback] hit snapshot {ts} for {url}")
    return resp, ts


def fetch_with_full_fallback(url: str, *,
                              timeout: int = DEFAULT_TIMEOUT_SECONDS,
                              try_wayback: bool = True,
                              ) -> tuple[requests.Response, dict]:
    """Tiered fetch: direct UA chain → Wayback fallback.

    Returns (response, meta) where meta is:
      {"source": "direct", "ua_used": "<ua>"}
      {"source": "wayback", "timestamp": "20260128152348"}

    Wayback is consulted only when the live UA chain raised. 404/410
    are terminal (page doesn't exist now and didn't exist either) —
    we don't fall to Wayback for those.
    """
    try:
        resp, ua_used = fetch_with_ua_fallback(url, timeout=timeout)
        return resp, {"source": "direct", "ua_used": ua_used}
    except requests.HTTPError as e:
        # 404/410 came from a real response.raise_for_status() above
        # → terminal, don't try Wayback.
        status = getattr(e.response, "status_code", None) if e.response is not None else None
        if status in (404, 410):
            raise
        if not try_wayback:
            raise
    except Exception:
        if not try_wayback:
            raise

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
    resp, meta = fetch_with_full_fallback(url, timeout=timeout, try_wayback=try_wayback)
    return resp.text, resp.url, meta


def _is_recipe_type(node_type: Any) -> bool:
    if node_type == "Recipe":
        return True
    if isinstance(node_type, list) and "Recipe" in node_type:
        return True
    return False


def extract_recipe_jsonld(html: str, base_url: str) -> list[dict]:
    """Return all JSON-LD objects whose @type is (or includes) Recipe.

    Handles the common @graph wrapper that schema.org sites use to nest
    multiple linked-data nodes inside a single script block.
    """
    data = extruct.extract(html, base_url=base_url, syntaxes=["json-ld"],
                           uniform=True)
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


def html_to_markdown(url: str, timings: Optional[dict] = None) -> dict:
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
    html, final_url, fetch_meta = fetch_html(url)
    t_fetch = time.perf_counter()
    if timings is not None:
        timings["fetch_ms"] = int((t_fetch - t0) * 1000)
        timings["fetch_source"] = fetch_meta.get("source")
        if fetch_meta.get("source") == "wayback":
            timings["wayback_timestamp"] = fetch_meta.get("timestamp")

    base_url = get_base_url(html, final_url)
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

    return {
        "markdown": "\n".join(parts),
        "source_url": final_url,
        "title": title,
        "has_jsonld": bool(recipes_jsonld),
        "jsonld": recipes_jsonld,
        "og_image": og_image,   # kept for back-compat
        "og_meta": og_meta,     # full dict — see extract_og_meta docstring
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