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

Getting the page is `to_markdown.fetch` (direct -> paid unblocker -> Wayback);
this module only turns HTML into markdown.
"""
import html as html_lib
import json
import re
import time
from typing import Any, Optional

from bs4 import BeautifulSoup
from markdownify import markdownify

import extruct
from w3lib.html import get_base_url

from to_markdown.fetch import fetch_with_full_fallback


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
        # and retry once. Anything else → the LENIENT block-by-block reader
        # below; only if that finds nothing is JSON-LD treated as absent.
        if "encoding declaration" in str(e).lower():
            try:
                data = extruct.extract(_XML_DECL_RE.sub("", html, count=1).lstrip(),
                                       base_url=base_url, syntaxes=["json-ld"],
                                       uniform=True)
            except Exception as e2:
                print(f"[jsonld] parse failed after decl-strip for {base_url!r}: {e2}")
                data = {"json-ld": _lenient_jsonld_blocks(html, base_url, str(e2))}
        else:
            data = {"json-ld": _lenient_jsonld_blocks(html, base_url, str(e))}
    except Exception as e:
        data = {"json-ld": _lenient_jsonld_blocks(html, base_url, str(e))}
    out: list[dict] = []
    for item in data.get("json-ld", []) or []:
        if _is_recipe_type(item.get("@type")):
            out.append(item)
        for nested in item.get("@graph", []) or []:
            if isinstance(nested, dict) and _is_recipe_type(nested.get("@type")):
                out.append(nested)
    if not out:
        # Meta-tag JSON-LD, but NEVER a gated teaser. This function feeds the
        # CONTENT path: whatever it returns is ingested as the recipe. Milk
        # Street's teaser would arrive as 3 ingredients plus the literal string
        # "... and more. Sign up for full access…" and one step, replacing the
        # full method the markdown path reads correctly today. A teaser must not
        # be allowed to outrank the real page. `page_declares_recipe()` exists
        # for the callers that only need to know IF this is a recipe.
        meta_nodes = _recipe_jsonld_in_meta(html, base_url)
        usable = [n for n in meta_nodes if not jsonld_declares_gated([n])]
        if meta_nodes and not usable:
            print(f"[jsonld] meta Recipe declares isAccessibleForFree=false "
                  f"(teaser) — not used as content: {base_url}")
        elif usable:
            print(f"[jsonld] recovered {len(usable)} Recipe block(s) from a "
                  f"<meta name='application/ld+json'> tag: {base_url}")
        out.extend(usable)
    return out


_LDJSON_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\btype\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _lenient_jsonld_blocks(html: str, base_url: str, why: str) -> list[dict]:
    """Block-by-block JSON-LD when the strict parser refuses the PAGE.

    extruct parses every <script type=application/ld+json> and raises on the
    first bad one, so one malformed block hides every good one. pauladeen.com
    (2026-10-12, every recipe page): the WordPress recipe plugin closes its
    Recipe block with `};` — a stray semicolon after valid JSON — so 179 recipe
    pages lost their structured data and the whole harvest fell back to the
    phrase scorer. Here each block is read with `raw_decode`, which takes the
    first complete JSON value and ignores whatever trails it; a block that is not
    JSON at all is skipped, not fatal. Returns the same shape extruct's
    uniform=True gives: a list of top-level dicts (an array block contributes
    its dict members)."""
    out: list[dict] = []
    dec = json.JSONDecoder()
    n_blocks = 0
    for m in _LDJSON_SCRIPT_RE.finditer(html):
        raw = html_lib.unescape(m.group(1)).strip()
        if not raw:
            continue
        n_blocks += 1
        try:
            obj, _end = dec.raw_decode(raw)
        except Exception:
            # Common commented-out form: <!-- {...} -->
            inner = raw.strip("<!->").strip()
            try:
                obj, _end = dec.raw_decode(inner)
            except Exception:
                continue
        if isinstance(obj, dict):
            out.append(obj)
        elif isinstance(obj, list):
            out.extend(x for x in obj if isinstance(x, dict))
    if out:
        print(f"[jsonld] strict parse failed ({why[:70]}) — lenient reader recovered "
              f"{len(out)} block(s) of {n_blocks} for {base_url}")
    else:
        print(f"[jsonld] parse failed for {base_url!r}: {why}")
    return out


def page_declares_recipe(html: str, base_url: str) -> bool:
    """Does this page declare itself a Recipe in structured data — even a gated
    teaser? The IS-IT-A-RECIPE question, which is not the same question as
    "give me the recipe".

    The candidate filter wants this one: a paywalled Milk Street page IS a
    recipe, and dropping it as `no-recipe-structure` was a false statement about
    the page. Whether we can READ it is decided later, by the extractor working
    on the full markdown, which for this publisher usually succeeds.
    """
    # Cheap check first: the meta variant is a regex over the raw HTML, while
    # extract_recipe_jsonld runs extruct over the whole document. Ordering it
    # this way also keeps the harvest log quiet — the content-path messages
    # belong to the content path, not to a yes/no question asked per candidate.
    if _recipe_jsonld_in_meta(html, base_url):
        return True
    return bool(extract_recipe_jsonld(html, base_url))


# `<meta name="application/ld+json" content="{...}">` — the JSON-LD payload in a
# meta tag's content attribute instead of a <script> block. Not what the spec
# says, but real: 177milkstreet.com publishes every recipe this way, and extruct
# (correctly) only reads <script type="application/ld+json">, so a 180 KB page
# carrying a complete schema.org Recipe scored ZERO structure and was rejected
# as "no recipe structure" — a statement about the page that was simply false.
_META_JSONLD_RE = re.compile(
    r'<meta\b[^>]*\bname\s*=\s*["\']application/ld\+json["\'][^>]*>',
    re.IGNORECASE,
)
_META_CONTENT_RE = re.compile(r'\bcontent\s*=\s*"([^"]*)"|\bcontent\s*=\s*\'([^\']*)\'',
                              re.IGNORECASE | re.DOTALL)


def jsonld_declares_gated(nodes: list[dict]) -> bool:
    """Does this page's own structured data say the recipe is behind a paywall?

    schema.org has an explicit flag for it, and 177milkstreet.com sets it
    honestly: `isAccessibleForFree: false` plus a `hasPart` WebPageElement
    naming the gated selector (`.paywalled-content`). Publishers do this so
    search engines don't index a teaser as the article.

    It matters because the teaser is DESIGNED to look like a recipe. Milk
    Street's Tarte Tatin ships 3 real ingredients followed by the literal
    string "... and more. Sign up for full access to all ingredients and
    instructions." as a fourth, plus one instruction step. Ingested naively
    that is a recipe row with an advert in its ingredient list.

    Reading the flag turns a guess into the publisher's own statement.
    """
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        free = node.get("isAccessibleForFree")
        if free is False or str(free).strip().lower() in ("false", "no"):
            return True
        for part in node.get("hasPart", []) or []:
            if not isinstance(part, dict):
                continue
            pfree = part.get("isAccessibleForFree")
            if pfree is False or str(pfree).strip().lower() in ("false", "no"):
                return True
    return False


def _recipe_jsonld_in_meta(html: str, base_url: str) -> list[dict]:
    """Recipe JSON-LD hidden in a meta tag's `content` attribute.

    A FALLBACK, tried only when the conforming <script> path found nothing, so a
    page that publishes both is still read the standard way. The content
    attribute is HTML-escaped (&quot; for every JSON quote), which is why it
    survives inside an attribute at all — unescape before parsing.
    """
    found: list[dict] = []
    for tag in _META_JSONLD_RE.findall(html or ""):
        m = _META_CONTENT_RE.search(tag)
        if not m:
            continue
        raw = html_lib.unescape(m.group(1) if m.group(1) is not None else m.group(2))
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as e:
            print(f"[jsonld] meta-tag JSON-LD unparseable for {base_url!r}: {e}")
            continue
        for node in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(node, dict):
                continue
            if _is_recipe_type(node.get("@type")):
                found.append(node)
            for nested in node.get("@graph", []) or []:
                if isinstance(nested, dict) and _is_recipe_type(nested.get("@type")):
                    found.append(nested)
    return found


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
    if not image_raw:
        # Some themes publish NO og:image/twitter:image at all (lidiasitaly:
        # 39 of 41 harvested rows imageless, 2026-08-24). The hero is still in
        # the page, just not where a meta scan looks: an inline
        # background-image on a recipe-classed container, and/or the share
        # buttons' data-image attribute. Fire ONLY when the meta tags are
        # absent; skip svg/theme chrome (logos).
        def _plausible(u):
            u = (u or "").strip()
            return u if u and ".svg" not in u.lower() \
                and "/themes/" not in u.lower() else ""
        for el in soup.select('[class*="recipe"][style*="background-image"]'):
            m = re.search(r"background-image\s*:\s*url\(\s*['\"]?([^'\")]+)",
                          el.get("style") or "")
            if m and _plausible(m.group(1)):
                image_raw = m.group(1).strip()
                break
        if not image_raw:
            for el in soup.find_all(attrs={"data-image": True}):
                got = _plausible(el.get("data-image"))
                if got:
                    image_raw = got
                    break
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


def markdown_from_html(html: str, source_url: str = "") -> str:
    """Canonical markdown from HTML we ALREADY HAVE — no fetch.

    Same three steps `html_to_markdown` runs (select_main_content →
    clean_for_markdown → markdownify) so a browser-supplied page and a
    server-fetched one are reduced identically. Factored out when a caller needs
    to reduce HTML it already holds — a browser-supplied capture, a cached body —
    rather than re-fetching a page the server may not even be able to reach.

    Not a parallel pipeline — one converter, two sources of HTML.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    main = select_main_content(soup)
    clean_for_markdown(main)
    body_md = markdownify(str(main), heading_style="ATX", strip=STRIP_TAGS).strip()
    parts: list[str] = []
    if title:
        parts.append(f"# {title}\n")
    if source_url:
        parts.append(f"URL: {source_url}\n")
    parts.append("## PAGE CONTENT\n")
    parts.append(body_md)
    return "\n".join(parts).strip()


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
    resp, fetch_meta = fetch_with_full_fallback(url, unblocker=unblocker, render=render)
    html, final_url = resp.text, resp.url
    t_fetch = time.perf_counter()
    if timings is not None:
        timings["fetch_ms"] = int((t_fetch - t0) * 1000)
        timings["fetch_source"] = fetch_meta.get("source")
        timings["fetch_render"] = bool(fetch_meta.get("render"))
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
    # header (not available here: only the HTML text reaches this
    # function, so we only get the HTML signal), then fasttext
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