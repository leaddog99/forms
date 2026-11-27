"""Fixes from the review of dish refreshes 3324-3339 (2026-11-27)."""
from intake import build_query_batch as b
from intake import youtube_source as ys
from input.pipeline.save_gate import is_cacheable


def test_single_recipes_are_not_collections():
    for t in ("Toad in the hole in 4 easy steps recipe | Good Food",
              "Toad In The Hole With Onion Gravy Recipe － Tesco Recipes",
              "Toad in the Hole / Fresh Eggs Daily® Recipes",
              "Authentic Tzatziki Recipe"):
        assert not b._looks_like_recipe_collection(t), t


def test_real_collections_still_caught():
    for t in ("30 Greek Recipes", "10 Easy Weeknight Dinners | Serious Eats", "Best Dinner Recipes - Allrecipes"):
        assert b._looks_like_recipe_collection(t), t


def _recipe(ings, steps):
    return {"name": "x", "recipeIngredient": ings, "recipeInstructions": [{"text": s} for s in steps]}


def test_two_ingredients_with_a_real_method_pass():
    r = _recipe(["7 lemons", "kosher salt"],
                ["Scrub the lemons and cut each into quarters without cutting through the base.",
                 "Pack salt into every cut, press the lemons into a jar and cover them with juice.",
                 "Seal and leave for four weeks, turning the jar every few days."])
    ok, why = is_cacheable(r, min_ings=3, min_steps=3)
    assert ok and "two-ingredient" in why


def test_two_ingredients_without_a_method_stay_out():
    assert not is_cacheable(_recipe(["cream", "sugar"], ["Whip."]), min_ings=3, min_steps=3)[0]
    assert not is_cacheable(_recipe(["cream"], ["Whip the cream " * 20, "Serve it " * 20]), min_ings=3, min_steps=3)[0]


def test_link_that_names_the_dish_beats_a_section_page():
    desc = ("More: https://www.jamieoliver.com/nutrition\n"
            "Shop: https://www.amazon.in/shop/homecookingshow\n"
            "Recipe: https://www.jamieoliver.com/recipes/pork/classic-toad-in-the-hole/\n")
    links = ys.recipe_links(desc, about="Toad in the Hole | Jamie Oliver")
    assert links[0].endswith("/classic-toad-in-the-hole/")
    assert links[-1].endswith("/nutrition")
    assert not any("amazon" in u for u in links)
