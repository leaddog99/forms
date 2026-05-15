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
"""
from typing import Optional


def markdown_passthrough(
    markdown_text: str,
    *,
    source_url: str = "",
    title: str = "",
) -> dict:
    """Normalize whitespace and return the canonical markdown envelope.

    Output mirrors `html_to_markdown` for symmetry, minus the JSON-LD
    detection (markdown sources don't have structured data we can mine).
    """
    md = _normalize(markdown_text)
    return {
        "markdown": md,
        "source_url": source_url,
        "title": title,
        "has_jsonld": False,
    }


def _normalize(md: Optional[str]) -> str:
    if not md:
        return ""
    if md.startswith("﻿"):
        md = md.lstrip("﻿")
    md = md.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in md.split("\n")]
    return "\n".join(lines).strip()


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "/dev/stdin"
    with open(src, "r", encoding="utf-8") as f:
        result = markdown_passthrough(f.read())
    print(result["markdown"])