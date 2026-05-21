"""Markdown -> canonical markdown (near-identity).

When the user supplies markdown directly (paste, drop, typed, .md file
upload, bookmarklet-generated), there's nothing to convert. This adapter
exists so the intake layer can always say "pick a `to_markdown` adapter,
hand off to `extract.markdown_to_recipe`" without a special-case branch.

It also does light hygiene that we want regardless of source:
- strips a UTF-8 BOM if present
- collapses Windows / Mac line endings to \\n
- trims trailing whitespace
- leaves the body otherwise untouched so the extract prompt sees what the
  user actually wrote

And it sniffs the body for hints the saver may have stamped on top:
- `*Source: <url>*` italic line (bookmarklet/converter convention)
- embedded JSON-LD `"url"` field
- first `# H1` line as title fallback
- fenced ```json``` block under "STRUCTURED RECIPE DATA (JSON-LD)" header
  (bookmarklet convention); when present and Recipe-typed, the endpoint
  can take the same `jsonld_to_recipe` fast lane that `/extract-from-url`
  uses and skip the Claude call entirely.
"""
import json
import re
from typing import Any, Optional


_SOURCE_LINE_RE = re.compile(
    r'^\s*\*?\s*(?:Source|URL|Original URL)\s*:\s*<?(https?://\S+?)>?\s*\*?\s*$',
    re.MULTILINE | re.IGNORECASE,
)
_JSONLD_URL_RE = re.compile(r'"url"\s*:\s*"(https?://[^"]+)"')
_TITLE_RE = re.compile(r'^\s*#\s+(.+?)\s*$', re.MULTILINE)
# Matches the fenced JSON-LD block the bookmarklet writes:
#   ## STRUCTURED RECIPE DATA (JSON-LD)
#
#   ```json
#   ...
#   ```
# The heading is fixed (bookmarklet.js writes it verbatim); accept the
# heading line optionally to allow stray fenced blocks elsewhere in the
# body to be ignored.
_JSONLD_FENCE_RE = re.compile(
    r'##\s*STRUCTURED RECIPE DATA \(JSON-LD\)\s*\n+```json\s*\n(.*?)\n```',
    re.DOTALL | re.IGNORECASE,
)


def markdown_passthrough(
    markdown_text: str,
    *,
    source_url: str = "",
    title: str = "",
) -> dict:
    """Normalize whitespace and return the canonical markdown envelope.

    Output mirrors `html_to_markdown` for symmetry. Caller-supplied
    source_url/title win; otherwise we sniff the body for hints.
    """
    md = _normalize(markdown_text)
    effective_url = source_url or _sniff_source_url(md)
    effective_title = title or _sniff_title(md)
    jsonld = _sniff_jsonld(md)
    return {
        "markdown": md,
        "source_url": effective_url,
        "title": effective_title,
        "has_jsonld": bool(jsonld),
        "jsonld": jsonld,
    }


def _normalize(md: Optional[str]) -> str:
    if not md:
        return ""
    if md.startswith("﻿"):
        md = md.lstrip("﻿")
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in md.split("\n")]
    return "\n".join(lines).strip()


def _sniff_source_url(md: str) -> str:
    """Look for a source URL the saver may have stamped on top of the body."""
    m = _SOURCE_LINE_RE.search(md)
    if m:
        return m.group(1).rstrip('.,;)*]').strip()
    m = _JSONLD_URL_RE.search(md)
    if m:
        return m.group(1)
    return ""


def _sniff_title(md: str) -> str:
    """First `# H1` line, if any. Empty otherwise."""
    m = _TITLE_RE.search(md)
    return m.group(1).strip() if m else ""


def _is_recipe_type(node_type: Any) -> bool:
    """schema.org @type can be a string or a list of strings."""
    if node_type == "Recipe":
        return True
    if isinstance(node_type, list) and "Recipe" in node_type:
        return True
    return False


def _sniff_jsonld(md: str) -> list:
    """Find a fenced JSON-LD block, parse it, return all Recipe-typed
    entries (flattening @graph wrappers the same way html_to_markdown does).

    Returns [] when no fenced JSON-LD block is present, when it doesn't
    parse, or when nothing inside it has @type Recipe — caller falls back
    to the LLM path in all three cases.
    """
    m = _JSONLD_FENCE_RE.search(md)
    if not m:
        return []
    try:
        parsed = json.loads(m.group(1))
    except Exception:
        return []
    # The bookmarklet wraps multiple <script type="application/ld+json">
    # blocks in an outer list; some pages also use @graph for nesting.
    # Normalize to a flat list of dicts before filtering by @type.
    out: list = []
    candidates = parsed if isinstance(parsed, list) else [parsed]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if _is_recipe_type(item.get("@type")):
            out.append(item)
        for nested in item.get("@graph") or []:
            if isinstance(nested, dict) and _is_recipe_type(nested.get("@type")):
                out.append(nested)
    return out


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "/dev/stdin"
    with open(src, "r", encoding="utf-8") as f:
        result = markdown_passthrough(f.read())
    print(result["markdown"])