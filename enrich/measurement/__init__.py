"""Ingredient-context-aware measurement conversion for the shared enrichment core.

Three layers:
  convert.py      deterministic engine (domains + KA densities) — the math
  parse.py        free ingredient-line parser (qty/unit/name)
  resolve.py      free name -> canonical KA ingredient
  recipe_pass.py  hybrid orchestration (deterministic + LLM fallback on misses)

The engine is deterministic, stateless, and free; only the recipe pass's
fallback touches an LLM, and only for lines the free path can't handle.
"""
from .convert import (
    Domain,
    Ingredient,
    ConversionResult,
    convert,
    resolve_unit,
    is_known_unit,
    resolve_ingredient,
    load_dataset,
    DATA_SOURCE,
)
from .parse import ParsedIngredient, parse_ingredient, clean_name
from .resolve import resolve_canonical
from .recipe_pass import convert_recipe_measurements

__all__ = [
    "Domain", "Ingredient", "ConversionResult",
    "convert", "resolve_unit", "is_known_unit", "resolve_ingredient",
    "load_dataset", "DATA_SOURCE",
    "ParsedIngredient", "parse_ingredient", "clean_name",
    "resolve_canonical",
    "convert_recipe_measurements",
]
