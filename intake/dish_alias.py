"""dish_alias.py — canonical-dish normalization from the native/transliterated anchor.

The reported "savory Greek pie shows up as pumpkin pie" is a DISH-IDENTITY problem, not a
translation one: the rows are English-sourced (the author titled it "Greek Pumpkin Pie …
Kolokithopita"), but the transliterated native name — `Kolokithopita` — is right there in
the title and is a STABLE anchor. This module resolves a recipe to its canonical dish off
that anchor, regardless of how the English wobbles (pumpkin / squash / zucchini), so the
dish identity + chapter normalize the same way across languages.

CONSERVATIVE by design: we match ONLY the unambiguous native/transliterated anchor (see
intake/dish_aliases.json) — never loose English like "pumpkin pie", which would grab the
real American dessert. We DON'T rewrite the author's title (provenance); we normalize the
dish IDENTITY and CHAPTER.

Seed file now; folds into the dish catalog (project_dish_catalog_table) later. See
docs/dish-alias-normalization.md.
"""
from __future__ import annotations

import json
import os
import unicodedata
from typing import Optional

_SEED_PATH = os.path.join(os.path.dirname(__file__), "dish_aliases.json")
_CACHE: Optional[list[dict]] = None


def _norm(s: str) -> str:
    """Lowercase + strip diacritics so 'Kolokithópita'/'kolokithopita' and
    'Κολοκυθόπιτα'/'κολοκυθοπιτα' compare equal within their own script. (Greek
    letters stay Greek — we keep BOTH the Greek and Latin alias forms in the seed.)"""
    if not s:
        return ""
    nfd = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in nfd if not unicodedata.combining(c))


def _dishes() -> list[dict]:
    global _CACHE
    if _CACHE is None:
        try:
            with open(_SEED_PATH, encoding="utf-8") as f:
                raw = json.load(f).get("dishes", [])
        except Exception:
            raw = []
        for d in raw:
            d["_alias_norms"] = [_norm(a) for a in d.get("aliases", []) if a]
        _CACHE = raw
    return _CACHE


def resolve(*texts: str) -> Optional[dict]:
    """Return the canonical-dish entry whose native/transliterated anchor appears in ANY
    of the given texts (recipe name, _source.originalTitle, current _master.dish), else
    None. Substring match on the diacritic-stripped, lowercased forms. First dish whose
    alias matches wins (the seed lists distinct anchors, so collisions are unlikely)."""
    hay = "  ".join(_norm(t) for t in texts if t)
    if not hay:
        return None
    for d in _dishes():
        for a in d["_alias_norms"]:
            if a and a in hay:
                return d
    return None


def canonical_chapter(*texts: str) -> Optional[str]:
    """The authoritative chapter for the canonical dish matched in `texts`, or None."""
    d = resolve(*texts)
    return d.get("chapter") if d else None
