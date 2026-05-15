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


def fetch_html(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS,
               user_agent: str = DEFAULT_USER_AGENT) -> tuple[str, str]:
    """Return (html_text, final_url) after redirects."""
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
    resp.raise_for_status()
    return resp.text, resp.url


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


def select_main_content(soup: BeautifulSoup) -> Any:
    """Pick the most recipe-looking subtree of the page."""
    for sel in ["[itemtype*='Recipe']", "article", "main", "body"]:
        node = soup.select_one(sel)
        if node:
            return node
    return soup


def clean_for_markdown(node: Any) -> None:
    """Strip junk tags / chrome sections in-place from a bs4 node."""
    for tag_name in STRIP_TAGS:
        for t in node.find_all(tag_name):
            t.decompose()
    for sel in DROP_SELECTORS:
        for t in node.select(sel):
            t.decompose()


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
    html, final_url = fetch_html(url)
    t_fetch = time.perf_counter()
    if timings is not None:
        timings["fetch_ms"] = int((t_fetch - t0) * 1000)

    base_url = get_base_url(html, final_url)
    soup = BeautifulSoup(html, "lxml")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    recipes_jsonld = extract_recipe_jsonld(html, base_url)

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