"""Deterministic tests for the measurement subsystem (engine + parse + resolve +
recipe pass). No LLM, no network — the recipe-pass tests run with
use_llm_fallback=False. Run from the repo root:

    python -m pytest recipe_enrichment/measurement/tests/ -q
    python -m recipe_enrichment.measurement.tests.test_measurement   # no-pytest fallback
"""
from contextlib import contextmanager

from recipe_enrichment.measurement import (
    convert, parse_ingredient, resolve_canonical, convert_recipe_measurements,
)


@contextmanager
def _raises(exc):
    try:
        yield
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} to be raised")


def _close(a, b, tol=0.5):
    return abs(a - b) <= tol


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

def test_same_domain_volume():
    assert _close(convert(1, "cup", "ml").amount, 236.59)


def test_same_domain_mass():
    assert _close(convert(2, "lb", "g").amount, 907.18)


def test_food_aware_density_differs():
    # The whole point: same cup, different weights.
    flour = convert(1, "cup", "g", "flour").amount
    sugar = convert(1, "cup", "g", "sugar").amount
    honey = convert(1, "cup", "g", "honey").amount
    assert _close(flour, 120) and _close(sugar, 198) and _close(honey, 336)
    assert flour < sugar < honey


def test_reverse_mass_to_volume():
    assert _close(convert(250, "g", "cup", "flour").amount, 2.08, 0.05)


def test_count_to_mass():
    assert _close(convert(3, "each", "g", "egg").amount, 150)


def test_mass_to_count():
    assert _close(convert(100, "g", "each", "egg").amount, 2.0, 0.05)


def test_missing_density_raises():
    # Cross-domain with no ingredient AND no explicit density -> explicit failure.
    with _raises(ValueError):
        convert(1, "cup", "g")


def test_unknown_ingredient_raises_keyerror():
    with _raises(KeyError):
        convert(1, "cup", "g", "definitely not an ingredient 9000")


def test_explicit_density_override():
    # 1 cup water @ 1 g/mL ~= 236 g
    assert _close(convert(1, "cup", "g", density_g_per_ml=1.0).amount, 236.59)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

def test_parse_mixed_number_and_unit():
    p = parse_ingredient("1 1/2 cups all-purpose flour")
    assert p.parseable and p.qty == 1.5 and p.unit == "cup"
    assert "flour" in p.name


def test_parse_vulgar_fraction():
    p = parse_ingredient("½ cup sugar")
    assert p.qty == 0.5 and p.unit == "cup"


def test_parse_glued_vulgar():
    p = parse_ingredient("1½ cups milk")
    assert p.qty == 1.5 and p.unit == "cup"


def test_parse_range_averages():
    p = parse_ingredient("2-3 tablespoons olive oil")
    assert p.qty == 2.5 and p.unit == "tablespoon"


def test_parse_bare_count():
    p = parse_ingredient("3 eggs")
    assert p.parseable and p.qty == 3 and p.unit == "each"


def test_parse_unit_of_filler():
    p = parse_ingredient("1 cup of flour")
    assert p.unit == "cup" and p.name == "flour"


def test_parse_to_taste_not_convertible():
    p = parse_ingredient("Salt, to taste")
    assert not p.parseable and p.qty is None


def test_parse_parenthetical_note():
    p = parse_ingredient("1 cup butter (2 sticks), melted")
    assert p.qty == 1 and p.unit == "cup" and "2 sticks" in p.note
    assert "melted" not in p.name  # prep word stripped


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #

def test_resolve_alias():
    ing = resolve_canonical("ap flour")
    assert ing and ing.name == "All-Purpose Flour"


def test_resolve_prep_stripped():
    # parse first, then resolve the cleaned name
    p = parse_ingredient("2 cups flour, sifted")
    ing = resolve_canonical(p.name, p.unit)
    assert ing and "Flour" in ing.name


def test_resolve_count_name_plus_unit():
    ing = resolve_canonical("garlic", "clove")
    assert ing and ing.grams_per_item == 3


def test_resolve_miss_returns_none():
    assert resolve_canonical("unobtainium dust") is None


# --------------------------------------------------------------------------- #
# Recipe pass (deterministic only — LLM fallback OFF)
# --------------------------------------------------------------------------- #

def test_recipe_pass_aligned_and_converts():
    recipe = {"recipeIngredient": [
        "1 cup all-purpose flour",
        "1/2 cup sugar",
        "3 eggs",
        "Salt, to taste",
    ]}
    meta = convert_recipe_measurements(recipe, use_llm_fallback=False)
    ms = recipe["_measurements"]
    assert len(ms) == 4                       # 1:1 alignment
    flour, sugar, eggs, salt = ms
    assert _close(flour["grams"], 120) and flour["resolved_by"] == "deterministic"
    assert _close(sugar["grams"], 99, 1)      # 1/2 cup sugar ~ 99 g
    assert _close(eggs["grams"], 150)
    assert salt["convertible"] is False
    assert meta["counts"]["deterministic"] == 3
    # metric display: food-aware weight, preserving the ingredient text
    assert flour["metric"] == "120 g all-purpose flour"
    assert eggs["metric"] is None             # counts unchanged in metric
    assert salt["metric"] is None             # unconvertible -> show raw


def test_recipe_pass_leaves_ingredient_strings_untouched():
    recipe = {"recipeIngredient": ["1 cup flour"]}
    convert_recipe_measurements(recipe, use_llm_fallback=False)
    assert recipe["recipeIngredient"] == ["1 cup flour"]   # source strings intact
    assert recipe["_measurements"][0]["ml"] is not None     # volume mL still computed


def test_recipe_pass_carries_forward_prior():
    # Deterministic-only recompute (the save path) must NOT lose an exotic
    # ingredient a prior pass already resolved — it's carried forward by raw.
    recipe = {"recipeIngredient": ["2 tbsp gochujang"]}
    prior = [{"raw": "2 tbsp gochujang", "metric": "40 g gochujang",
              "grams": 40, "ml": 30, "convertible": True, "resolved_by": "llm"}]
    meta = convert_recipe_measurements(recipe, use_llm_fallback=False, prior=prior)
    e = recipe["_measurements"][0]
    assert e["metric"] == "40 g gochujang" and e["convertible"] is True
    assert meta["counts"]["carried"] == 1


def test_recipe_pass_recompute_picks_up_edits():
    # A manually-typed common ingredient gets converted on recompute, even with
    # no prior and no LLM — the save path covers edits/additions for free.
    recipe = {"recipeIngredient": ["2 cups all-purpose flour"]}
    convert_recipe_measurements(recipe, use_llm_fallback=False)
    assert recipe["_measurements"][0]["metric"] == "240 g all-purpose flour"


def test_recipe_pass_empty_safe():
    recipe = {}
    meta = convert_recipe_measurements(recipe, use_llm_fallback=False)
    assert recipe["_measurements"] == [] and meta["counts"]["total"] == 0


if __name__ == "__main__":
    import sys
    fns = sorted(
        (v for k, v in dict(globals()).items()
         if k.startswith("test_") and callable(v)),
        key=lambda f: f.__name__,
    )
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
            passed += 1
        except Exception as e:  # noqa: BLE001
            print("FAIL", fn.__name__, "->", repr(e))
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
