"""Key names — the ONE cleaner for any name that becomes a join key AND a URL segment.

Collections, curated collections and product classes are addressed as
`/things/{name}`. A slash in the name can never reach such a route: the server
decodes %2F to "/" BEFORE routing, so the path splits and every GET / PUT /
DELETE answers 404 — the row exists and nothing can open, edit or delete it
("Cast Iron Griddle / Grill", 2026-09-18). The name is cleaned ONCE, where it is
born, rather than teaching a dozen routes to tolerate it: the same canonical
form then reaches the URL, the cache filename and the search query.

A slash between words means "and/or", so it becomes the word, not a symbol —
"Cast Iron Griddle / Grill" -> "Cast Iron Griddle and Grill". Cosmetic spelling
belongs in display_name.
"""
from __future__ import annotations

import re

_SLASHES = re.compile(r"\s*[/\\]+\s*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_key_name(value) -> str:
    """Canonical form of a user-typed key name. Idempotent; '' stays ''."""
    s = _CONTROL.sub(" ", str(value or ""))
    s = _SLASHES.sub(" and ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # a leading/trailing slash leaves a dangling joiner: "/ Grill" -> "and Grill"
    s = re.sub(r"^(?:and\s+)+|(?:\s+and)+$", "", s, flags=re.I).strip()
    return s
