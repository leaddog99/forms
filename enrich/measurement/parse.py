"""Deterministic ingredient-line parser — the cheap half of the hybrid.

Turns a raw recipe ingredient string into (quantity, unit, name) WITHOUT an LLM.
Handles the common shapes: decimals, vulgar fractions (½), mixed numbers
("1 1/2"), ASCII fractions ("1/2"), ranges ("2-3", "2 to 3"), a leading unit, a
parenthetical aside, and "no quantity" markers ("to taste", "a pinch").

This is intentionally conservative: when it can't confidently parse, it sets
`parseable=False` and the recipe pass hands the line to the LLM fallback
(`recipe_pass.py`). Better to defer a messy line than to mis-parse it silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .convert import (
    is_known_unit, resolve_unit, canonical_unit, Domain, COUNT_UNITS,
)


# Unicode vulgar fractions -> ASCII "a/b" so one numeric parser handles them all.
_VULGAR = {
    "½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
    "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5",
    "⅙": "1/6", "⅚": "5/6", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
    "⅐": "1/7", "⅑": "1/9", "⅒": "1/10",
}
_VULGAR_RE = re.compile("([" + "".join(_VULGAR) + "])")

# "no real quantity" phrases — convertible=False, qty stays None.
_NO_QTY = re.compile(
    r"\b(to taste|as needed|for (serving|garnish|dusting|drizzling|frying|greasing)|"
    r"a pinch|pinch of|a dash|dash of|a handful|handful of|splash|optional)\b",
    re.I,
)

# Prep / quality descriptors stripped during NAME cleanup (resolution-friendly).
# Kept here (parser owns the name remainder) but used by resolve.py too.
_PREP_WORDS = {
    "sifted", "melted", "softened", "packed", "chopped", "minced", "diced",
    "sliced", "grated", "shredded", "ground", "fresh", "freshly", "dried",
    "large", "small", "medium", "ripe", "cold", "warm", "beaten", "whisked",
    "divided", "finely", "coarsely", "roughly", "lightly", "well", "cooked",
    "raw", "peeled", "seeded", "cored", "halved", "quartered", "cubed",
    "crushed", "whole", "boneless", "skinless", "drained", "rinsed", "toasted",
    "room", "temperature", "plus", "more", "extra", "approximately", "about",
    "heaping", "scant", "level", "packed", "loosely", "firmly", "unsalted",
    "salted", "granulated", "pure", "good", "quality", "organic",
}

_NUM = r"\d+(?:\.\d+)?"
# A single quantity token: mixed number, fraction, or decimal/integer.
_QTY_TOKEN = re.compile(
    rf"^\s*(?P<a>{_NUM})\s+(?P<b>\d+/\d+)"   # mixed number "1 1/2"
    rf"|^\s*(?P<frac>\d+/\d+)"               # fraction "1/2"
    rf"|^\s*(?P<dec>{_NUM})",                # decimal/integer
)
# Range glue between two quantities: "2-3", "2 – 3", "2 to 3". A dash needs no
# surrounding space (handles "2-3"); the word forms require a trailing space so
# we don't swallow "to" out of a following word.
_RANGE = re.compile(r"^\s*(?:[-–—]\s*|(?:to|or)\s+)", re.I)


@dataclass
class ParsedIngredient:
    raw: str
    qty: Optional[float]      # numeric quantity (None when not parseable / no qty)
    unit: Optional[str]       # canonical unit token, or None (bare count -> "each")
    name: str                 # ingredient name remainder, lightly cleaned (for resolution)
    name_raw: str = ""        # remainder BEFORE cleanup (keeps prep: "flour, sifted")
    note: str = ""            # parenthetical / prep aside, for provenance
    parseable: bool = False   # qty present AND (unit known OR bare-count)
    domain: Optional[str] = None  # 'volume' | 'mass' | 'count' when unit resolved


def _frac(tok: str) -> float:
    n, d = tok.split("/")
    return float(n) / float(d)


# A "NUMBER UNIT" weight/volume inside a parenthetical: "15-ounce", "14.5 oz",
# "28 ounce", "400 g". The number and unit may be hyphen- or space-joined.
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[-\s]?\s*([a-zA-Z.]+)")


def _parse_size(note: str) -> Optional[tuple[float, str]]:
    """Pull a real measure out of a parenthetical size, e.g. the '15-ounce' in
    '1 (15-ounce) can'. Returns (qty, canonical_unit) only when the unit is a
    known mass or volume unit (so we can actually weigh it); else None."""
    if not note:
        return None
    for m in _SIZE_RE.finditer(note):
        tok = m.group(2).strip().rstrip(".").lower()
        if is_known_unit(tok):
            dom, _f = resolve_unit(tok)
            if dom in (Domain.MASS, Domain.VOLUME):
                return float(m.group(1)), canonical_unit(tok)
    return None


def _read_quantity(text: str) -> tuple[Optional[float], str]:
    """Read a leading quantity (with optional range) and return (value, rest)."""
    m = _QTY_TOKEN.match(text)
    if not m:
        return None, text
    if m.group("a") is not None:          # mixed number
        val = float(m.group("a")) + _frac(m.group("b"))
    elif m.group("frac") is not None:     # bare fraction
        val = _frac(m.group("frac"))
    else:                                 # decimal / int
        val = float(m.group("dec"))
    rest = text[m.end():]

    # Optional range upper bound -> average the two (closest single value).
    rm = _RANGE.match(rest)
    if rm:
        after = rest[rm.end():]
        m2 = _QTY_TOKEN.match(after)
        if m2:
            if m2.group("a") is not None:
                hi = float(m2.group("a")) + _frac(m2.group("b"))
            elif m2.group("frac") is not None:
                hi = _frac(m2.group("frac"))
            else:
                hi = float(m2.group("dec"))
            val = (val + hi) / 2.0
            rest = after[m2.end():]
    return val, rest


def clean_name(name: str) -> str:
    """Lightly normalize an ingredient name remainder for resolution: drop a
    trailing prep clause after a comma, strip prep/quality words, collapse
    spaces. Keeps the core noun(s)."""
    n = name.strip().strip(",")
    # Drop everything after the first comma — almost always a prep clause
    # ("garlic, minced", "flour, sifted").
    if "," in n:
        n = n.split(",", 1)[0]
    n = re.sub(r"\([^)]*\)", " ", n)          # any leftover parentheticals
    n = re.sub(r"[^\w\s'-]", " ", n)          # punctuation -> space
    words = [w for w in n.lower().split() if w not in _PREP_WORDS]
    return " ".join(words).strip()


def parse_ingredient(raw: str) -> ParsedIngredient:
    """Parse one raw ingredient line. Never raises."""
    s = (raw or "").strip()
    if not s:
        return ParsedIngredient(raw=raw, qty=None, unit=None, name="", parseable=False)

    # Pull a parenthetical aside out of the line (kept as note).
    note = ""
    pm = re.search(r"\(([^)]*)\)", s)
    if pm:
        note = pm.group(1).strip()
        s = (s[:pm.start()] + " " + s[pm.end():]).strip()

    # Normalize vulgar fractions, inserting a space if glued to a leading int
    # ("1½" -> "1 1/2", "½" -> "1/2").
    s_norm = _VULGAR_RE.sub(lambda m: " " + _VULGAR[m.group(1)], s)
    s_norm = re.sub(r"(\d)\s+(\d+/\d+)", r"\1 \2", s_norm).strip()

    no_qty = bool(_NO_QTY.search(s_norm))
    qty, rest = _read_quantity(s_norm)
    rest = rest.strip()

    # Try a leading unit token on the remainder.
    unit: Optional[str] = None
    domain: Optional[str] = None
    if rest:
        first, _, tail = rest.partition(" ")
        cand = first.strip().lower().rstrip(".")
        if is_known_unit(cand):
            dom, _f = resolve_unit(cand)
            unit = canonical_unit(cand)   # 'cups' -> 'cup'
            domain = dom.value
            rest = tail.strip()
            # "of" filler after a unit: "1 cup of flour"
            if rest.lower().startswith("of "):
                rest = rest[3:].strip()

    # Parenthetical size wins for containers: in "1 (15-ounce) can tomato sauce"
    # the 15 oz IS the real measure (a mass — converts with no density at all).
    # Apply when the line counts containers/items (domain COUNT) or has no usable
    # unit; leave real volume/mass units ("1 cup (240 ml) milk") untouched.
    size = _parse_size(note)
    if size and (unit is None or domain == Domain.COUNT.value):
        sz_qty, sz_unit = size
        qty = (qty if qty is not None else 1.0) * sz_qty
        unit = sz_unit
        dom, _f = resolve_unit(sz_unit)
        domain = dom.value

    name_raw = rest.strip().strip(",").strip()
    name = clean_name(rest)

    # Decide parseability:
    #   - explicit no-qty phrase  -> not convertible
    #   - qty + known unit        -> parseable
    #   - qty + no unit + a name  -> bare COUNT ("3 eggs"): unit defaults to "each"
    if no_qty or qty is None:
        return ParsedIngredient(raw=raw, qty=qty, unit=unit, name=name,
                                name_raw=name_raw, note=note, parseable=False,
                                domain=domain)
    if unit is None and name:
        unit = "each"
        domain = Domain.COUNT.value
    parseable = qty is not None and unit is not None
    return ParsedIngredient(raw=raw, qty=qty, unit=unit, name=name,
                            name_raw=name_raw, note=note, parseable=parseable,
                            domain=domain)
