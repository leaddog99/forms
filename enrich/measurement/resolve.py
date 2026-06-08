"""Deterministic ingredient-name resolution — map a parsed name onto a canonical
King Arthur registry key.

Strategy (cheapest first; stop at the first hit):
  1. exact registry hit (the registry already indexes names + curated aliases)
  2. singular/plural nudge
  3. unit-aware candidates for count items ("garlic" + clove -> "garlic clove")
  4. fuzzy match (difflib) over registry keys, conservative cutoff

A miss returns None — the recipe pass then routes the line to the LLM fallback.
No LLM here; this is the free path that should catch the bulk of well-formed
names. Tune the cutoff up if false matches appear, down if too many real
ingredients fall through to the (paid) LLM.
"""

from __future__ import annotations

import difflib
from typing import Optional

from .convert import Ingredient, registry, lookup, COUNT_UNITS
from .parse import clean_name


_FUZZY_CUTOFF = 0.86


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 3:
        return word[:-3] + "y"
    if word.endswith("es") and len(word) > 3:
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _candidates(name: str, unit: Optional[str]) -> list[str]:
    """Ordered candidate keys to try before fuzzy matching."""
    name = name.strip().lower()
    words = name.split()
    sing = None
    if words:
        # singularize the last word ("eggs" -> "egg", "berries" -> "berry")
        s = " ".join(words[:-1] + [_singular(words[-1])])
        if s != name:
            sing = s
    cands: list[str] = []
    # Count context FIRST: with a count unit, "garlic" + clove should prefer the
    # count item "garlic clove" over a de-parenthesized density row also keyed
    # "garlic". (bare "egg" is already a count key, so it still resolves.)
    if unit and unit.lower().rstrip("s") in COUNT_UNITS:
        u = unit.lower().rstrip("s")
        cands += [f"{name} {u}", f"{u} {name}"]
    cands.append(name)
    if sing:
        cands.append(sing)
    return cands


def resolve_canonical(
    name: str, unit: Optional[str] = None
) -> Optional[Ingredient]:
    """Best deterministic match for `name` (None on miss). `unit` gives count
    context so a bare "garlic" with a clove unit can find "garlic clove"."""
    if not name or not name.strip():
        return None

    # Try the name as given, then a prep-stripped variant ("powdered sugar,
    # sifted" -> "powdered sugar"), so callers that pass a raw string (e.g. the
    # /convert endpoint) get the same robustness the recipe pass gets from parse.
    variants = [name.strip().lower()]
    cleaned = clean_name(name)
    if cleaned and cleaned not in variants:
        variants.append(cleaned)

    for nm in variants:
        for cand in _candidates(nm, unit):
            ing = lookup(cand)
            if ing is not None:
                return ing

    # Fuzzy over the registry keyspace (names + aliases). Conservative cutoff to
    # avoid "cream" -> "cream of tartar" style false hits. Use the cleaned form.
    keys = list(registry().keys())
    match = difflib.get_close_matches(variants[-1], keys, n=1, cutoff=_FUZZY_CUTOFF)
    if match:
        return registry()[match[0]]
    return None
