"""Ingredient-context-aware culinary measurement conversion — the engine.

Carved from the starter kit (Desktop Claude, 2026-05-31) into the shared core.
Densities load from `kingarthur_ingredient_weights.json`, built from the King
Arthur Baking Company Ingredient Weight Chart
(https://www.kingarthurbaking.com/learn/ingredient-weight-chart). Every density
traces to a published KA value (facts, not guesses; KA cited as source).

Model
-----
Three domains: VOLUME (base mL), MASS (base g), COUNT (base item). Same-domain
conversions are pure unit math, ingredient-independent. Cross-domain conversions
route through grams (the hub):
    VOLUME <-> MASS  via density (g/mL)
    COUNT  <-> MASS  via grams-per-item
    VOLUME <-> COUNT chained through MASS
Ingredient context enters ONLY at a bridge crossing. An unknown density on a
cross-domain request raises explicitly rather than guessing (the stated
preference) — callers catch ValueError and degrade to a partial conversion.

This module is deterministic, stateless, and free (no LLM). It belongs in the
shared enrichment core; the fuzzy ingredient-name resolution that feeds it lives
in `resolve.py`, and the recipe-level orchestration in `recipe_pass.py`.

Note on KA values: KA rounds water/milk to 227 g/cup (the 8 fl-oz = 8 oz
convention); physically a cup of water is ~236 g. Flour reflects the
spoon-and-level method (120 g/cup). KA publishes a single density per
ingredient, so prep-state variants (e.g. scooped flour) are not modeled.
"""

from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
# Domains and unit tables
# --------------------------------------------------------------------------- #

class Domain(Enum):
    VOLUME = "volume"
    MASS = "mass"
    COUNT = "count"


CUP_ML = 236.5882365

UNITS: dict[str, tuple[Domain, float]] = {
    # volume -> mL
    "ml": (Domain.VOLUME, 1.0), "milliliter": (Domain.VOLUME, 1.0),
    "cc": (Domain.VOLUME, 1.0),
    "cl": (Domain.VOLUME, 10.0), "dl": (Domain.VOLUME, 100.0),
    "l": (Domain.VOLUME, 1000.0), "liter": (Domain.VOLUME, 1000.0),
    "litre": (Domain.VOLUME, 1000.0),
    "tsp": (Domain.VOLUME, 4.92892159), "teaspoon": (Domain.VOLUME, 4.92892159),
    "t": (Domain.VOLUME, 4.92892159),
    "tbsp": (Domain.VOLUME, 14.7867648), "tablespoon": (Domain.VOLUME, 14.7867648),
    "tbs": (Domain.VOLUME, 14.7867648), "tbl": (Domain.VOLUME, 14.7867648),
    "fl_oz": (Domain.VOLUME, 29.5735296), "fluid_ounce": (Domain.VOLUME, 29.5735296),
    "cup": (Domain.VOLUME, CUP_ML), "c": (Domain.VOLUME, CUP_ML),
    "pint": (Domain.VOLUME, 473.176473), "pt": (Domain.VOLUME, 473.176473),
    "quart": (Domain.VOLUME, 946.352946), "qt": (Domain.VOLUME, 946.352946),
    "gallon": (Domain.VOLUME, 3785.411784), "gal": (Domain.VOLUME, 3785.411784),
    "stick": (Domain.VOLUME, CUP_ML / 2.0),  # 1 stick butter = 1/2 cup
    # mass -> g
    "mg": (Domain.MASS, 0.001),
    "g": (Domain.MASS, 1.0), "gram": (Domain.MASS, 1.0), "gr": (Domain.MASS, 1.0),
    "kg": (Domain.MASS, 1000.0), "kilogram": (Domain.MASS, 1000.0),
    "oz": (Domain.MASS, 28.349523125), "ounce": (Domain.MASS, 28.349523125),
    "lb": (Domain.MASS, 453.59237), "pound": (Domain.MASS, 453.59237),
    "lbs": (Domain.MASS, 453.59237),
    # count -> item
    "each": (Domain.COUNT, 1.0), "item": (Domain.COUNT, 1.0),
    "clove": (Domain.COUNT, 1.0), "piece": (Domain.COUNT, 1.0),
    "whole": (Domain.COUNT, 1.0), "head": (Domain.COUNT, 1.0),
    "can": (Domain.COUNT, 1.0), "package": (Domain.COUNT, 1.0),
    "pkg": (Domain.COUNT, 1.0),
}

# Units that are really "count of a thing" — used by the parser to decide a
# bare quantity ("3 eggs") means COUNT, not an error.
COUNT_UNITS = {u for u, (d, _) in UNITS.items() if d is Domain.COUNT}


def resolve_unit(u: str) -> tuple[Domain, float]:
    key = u.strip().lower().replace(" ", "_").rstrip(".")
    if key in UNITS:
        return UNITS[key]
    if key.endswith("s") and key[:-1] in UNITS:
        return UNITS[key[:-1]]
    raise ValueError(f"Unknown unit: {u!r}")


def is_known_unit(u: str) -> bool:
    try:
        resolve_unit(u)
        return True
    except ValueError:
        return False


def canonical_unit(u: str) -> str:
    """The registry key for a unit token ('cups'/'Cups.' -> 'cup'). Returns the
    normalized input unchanged if unknown (callers gate on is_known_unit first)."""
    key = u.strip().lower().replace(" ", "_").rstrip(".")
    if key in UNITS:
        return key
    if key.endswith("s") and key[:-1] in UNITS:
        return key[:-1]
    return key


# --------------------------------------------------------------------------- #
# Ingredient model + registry (loaded from KA JSON)
# --------------------------------------------------------------------------- #

@dataclass
class Ingredient:
    name: str
    g_per_ml: Optional[float] = None        # for volume<->mass
    grams_per_item: Optional[float] = None  # for count<->mass
    grams_per_cup: Optional[float] = None   # provenance / display
    aliases: tuple[str, ...] = ()
    source: str = ""


_REGISTRY: dict[str, Ingredient] = {}
DATA_SOURCE = ""


def load_dataset(path: Optional[str] = None) -> int:
    """(Re)load the ingredient density dataset. Returns entry count."""
    global DATA_SOURCE
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "kingarthur_ingredient_weights.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    DATA_SOURCE = data.get("source", "")
    _REGISTRY.clear()

    def register(ing: Ingredient):
        _REGISTRY[ing.name.lower()] = ing
        for a in ing.aliases:
            _REGISTRY[a.lower()] = ing

    for row in data["ingredients"]:
        register(Ingredient(
            name=row["name"],
            g_per_ml=row["g_per_ml"],
            grams_per_cup=row["grams_per_cup"],
            aliases=tuple(row.get("aliases", [])),
            source=DATA_SOURCE,
        ))
    for row in data.get("count_items", []):
        register(Ingredient(
            name=row["name"],
            grams_per_item=row["grams_per_item"],
            aliases=tuple(row.get("aliases", [])),
            source=row.get("source", DATA_SOURCE),
        ))
    return len(_REGISTRY)


def registry() -> dict[str, Ingredient]:
    """The live name/alias -> Ingredient map (read-only use by resolve.py)."""
    return _REGISTRY


def lookup(key: str) -> Optional[Ingredient]:
    """Direct registry hit (no fuzzy). None on miss — does NOT raise."""
    return _REGISTRY.get(key.strip().lower())


def resolve_ingredient(name: str) -> Ingredient:
    ing = lookup(name)
    if ing is not None:
        return ing
    raise KeyError(
        f"Unknown ingredient: {name!r}. Not in the King Arthur dataset. "
        f"Pass density_g_per_ml=/grams_per_item= explicitly, or resolve the "
        f"name to a known ingredient first."
    )


# --------------------------------------------------------------------------- #
# Conversion core
# --------------------------------------------------------------------------- #

@dataclass
class ConversionResult:
    amount: float
    to_unit: str
    grams: Optional[float]
    path: list[str] = field(default_factory=list)


def convert(
    amount: float,
    from_unit: str,
    to_unit: str,
    ingredient: Optional[str] = None,
    *,
    density_g_per_ml: Optional[float] = None,
    grams_per_item: Optional[float] = None,
) -> ConversionResult:
    from_dom, from_factor = resolve_unit(from_unit)
    to_dom, to_factor = resolve_unit(to_unit)
    path: list[str] = []

    src_canon = amount * from_factor
    path.append(f"{amount} {from_unit} = {src_canon:.4g} {from_dom.value}-base")

    ing = resolve_ingredient(ingredient) if ingredient else None
    if density_g_per_ml is None and ing is not None:
        density_g_per_ml = ing.g_per_ml
    if grams_per_item is None and ing is not None:
        grams_per_item = ing.grams_per_item

    if from_dom == to_dom:
        out = src_canon / to_factor
        path.append(f"same domain -> {out:.4g} {to_unit}")
        grams = src_canon if from_dom == Domain.MASS else None
        return ConversionResult(out, to_unit, grams, path)

    grams = _to_grams(src_canon, from_dom, density_g_per_ml, grams_per_item, path)
    out_canon = _from_grams(grams, to_dom, density_g_per_ml, grams_per_item, path)
    out = out_canon / to_factor
    path.append(f"-> {out:.4g} {to_unit}")
    return ConversionResult(out, to_unit, grams, path)


def _to_grams(canon, dom, density, per_item, path) -> float:
    if dom == Domain.MASS:
        return canon
    if dom == Domain.VOLUME:
        if density is None:
            raise ValueError("Volume->mass needs a density (g/mL). "
                             "Pass ingredient= or density_g_per_ml=.")
        g = canon * density
        path.append(f"x density {density:.4g} g/mL -> {g:.4g} g")
        return g
    if dom == Domain.COUNT:
        if per_item is None:
            raise ValueError("Count->mass needs grams_per_item. "
                             "Pass ingredient= or grams_per_item=.")
        g = canon * per_item
        path.append(f"x {per_item:.4g} g/item -> {g:.4g} g")
        return g
    raise ValueError(f"Unhandled domain {dom}")


def _from_grams(g, dom, density, per_item, path) -> float:
    if dom == Domain.MASS:
        return g
    if dom == Domain.VOLUME:
        if density is None:
            raise ValueError("Mass->volume needs a density (g/mL). "
                             "Pass ingredient= or density_g_per_ml=.")
        ml = g / density
        path.append(f"/ density {density:.4g} g/mL -> {ml:.4g} mL")
        return ml
    if dom == Domain.COUNT:
        if per_item is None:
            raise ValueError("Mass->count needs grams_per_item. "
                             "Pass ingredient= or grams_per_item=.")
        n = g / per_item
        path.append(f"/ {per_item:.4g} g/item -> {n:.4g} items")
        return n
    raise ValueError(f"Unhandled domain {dom}")


# load on import
load_dataset()


# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    print(f"Loaded dataset: {DATA_SOURCE}\n")
    cases = [
        dict(amount=1, from_unit="cup", to_unit="ml"),
        dict(amount=2, from_unit="lb", to_unit="g"),
        dict(amount=1, from_unit="cup", to_unit="g", ingredient="flour"),
        dict(amount=1, from_unit="cup", to_unit="g", ingredient="sugar"),
        dict(amount=1, from_unit="cup", to_unit="g", ingredient="brown sugar"),
        dict(amount=1, from_unit="cup", to_unit="g", ingredient="honey"),
        dict(amount=1, from_unit="cup", to_unit="g", ingredient="cocoa powder"),
        dict(amount=250, from_unit="g", to_unit="cup", ingredient="flour"),
        dict(amount=3, from_unit="each", to_unit="g", ingredient="egg"),
        dict(amount=100, from_unit="g", to_unit="each", ingredient="egg"),
        dict(amount=4, from_unit="clove", to_unit="g", ingredient="garlic clove"),
        dict(amount=2, from_unit="cup", to_unit="oz", ingredient="butter"),
    ]
    for c in cases:
        r = convert(**c)
        label = c.get("ingredient", "-")
        print(f"{c['amount']:>4} {c['from_unit']:<5} {label:>13} "
              f"= {r.amount:8.2f} {c['to_unit']}")
