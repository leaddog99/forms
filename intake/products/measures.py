"""Product size-string normalization — dimensions, capacity, weight, class tokens.

Standardizes the messy size syntax that arrives from retailer pages / JSON-LD:
    8⅝ × 4½ × 2¾ in  |  8.5 x 4.5 x 2.75 in  |  10.25"  |  6-7 quarts  |  2 Qt
into ONE canonical form (unicode fractions -> decimals, ×/by -> x, unit token ->
the shared preferred symbol). It owns only product-string PARSING; the unit
VOCABULARY is the single source of truth in enrich.measurement.convert
(feedback_single_path) — this module never re-lists units.

Canonical output:
    dimensions -> "8.5 x 4.5 x 2.75 in"   (space-x-space, one unit at the end)
    capacity/weight -> "2 qt" / "1 lb" / "6-7 qt"  (ranges preserved with a hyphen)
"""
from __future__ import annotations

import re

from enrich.measurement.convert import preferred_symbol, is_known_unit

# Unicode vulgar fractions -> decimal value.
_UNICODE_FRAC = {
    "¼": 0.25, "½": 0.5, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3,
    "⅕": 0.2, "⅖": 0.4, "⅗": 0.6, "⅘": 0.8,
    "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 0.125, "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
    "⅐": 1 / 7, "⅑": 1 / 9, "⅒": 0.1,
}
_FRAC_CLASS = "".join(_UNICODE_FRAC)
_FRAC_RE = re.compile(r"(\d*)\s*([" + _FRAC_CLASS + r"])")
_SEP_RE = re.compile(r"\s*(?:×|✕|✖|x|X|by)\s*")
_TRAIL_UNIT_RE = re.compile(r'([A-Za-z]+|")\s*$')
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_RANGE_RE = re.compile(r"\s*(?:-|–|—|to)\s*")


def _fmt(v: float) -> str:
    """Trim a float to a clean string: 4.50 -> '4.5', 3.0 -> '3', 0.625 -> '0.625'."""
    return f"{round(v, 4):g}"


def _defraction(s: str) -> str:
    """8⅝ -> 8.625, ½ -> 0.5 (a bare fraction with no leading integer -> its decimal)."""
    def repl(m: re.Match) -> str:
        whole = m.group(1)
        val = (float(whole) if whole else 0.0) + _UNICODE_FRAC[m.group(2)]
        return _fmt(val)
    return _FRAC_RE.sub(repl, s)


def _canon_trailing_unit(t: str):
    """Split a trailing unit token off `t`. Returns (body, symbol) — symbol is '' when
    the trailing token isn't a known unit (so non-measure parens survive untouched)."""
    m = _TRAIL_UNIT_RE.search(t.strip())
    if m and (m.group(1) == '"' or is_known_unit(m.group(1))):
        sym = preferred_symbol(m.group(1))
        return t[: m.start()].strip(), sym
    return t.strip(), ""


def normalize_dimensions(s) -> str:
    """'8⅝ × 4½ × 2¾ in' / '10.25\"' -> '8.625 x 4.5 x 2.75 in' / '10.25 in'."""
    if not s:
        return ""
    t = _defraction(str(s))
    t = _SEP_RE.sub(" x ", t)
    body_src, unit = _canon_trailing_unit(t)
    nums = _NUM_RE.findall(body_src)
    if not nums:
        return str(s).strip()
    body = " x ".join(_fmt(float(n)) for n in nums)
    return f"{body} {unit}".strip() if unit else body


def normalize_measure(s) -> str:
    """'2 Qt' -> '2 qt', '6 to 7 quarts' -> '6-7 qt', '1 lb' -> '1 lb'. Ranges kept."""
    if not s:
        return ""
    t = _defraction(str(s)).strip()
    num_src, unit = _canon_trailing_unit(t)
    if not unit:
        return t
    num = _RANGE_RE.sub("-", num_src).strip()
    return f"{num} {unit}".strip()


def normalize_class_size(name) -> str:
    """Normalize the size token inside a product_class: 'Dutch Ovens (6-7 Quarts)'
    -> 'Dutch Ovens (6-7 qt)'. Non-measure parens (e.g. '(fresh)') pass through."""
    if not name:
        return ""
    return re.sub(r"\(([^)]*)\)",
                  lambda m: "(" + (normalize_measure(m.group(1)) or m.group(1)) + ")",
                  str(name))


def normalize_product(p: dict) -> dict:
    """In-place canonicalize a product dict's size fields. Idempotent."""
    specs = p.get("specs") or {}
    if specs.get("dimensions_in"):
        specs["dimensions_in"] = normalize_dimensions(specs["dimensions_in"])
    if specs.get("capacity"):
        specs["capacity"] = normalize_measure(specs["capacity"])
    if specs.get("weight"):
        specs["weight"] = normalize_measure(specs["weight"])
    p["specs"] = specs
    if p.get("product_class"):
        p["product_class"] = normalize_class_size(p["product_class"])
    return p
