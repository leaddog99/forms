"""The is-recipe verdict: phrase score first, extracted structure as the
rescue. Run: python test_recipe_verdict.py"""
from input.pipeline import validators as v


def _validation(score, threshold=7):
    return {"accepted": score >= threshold, "score": score, "threshold": threshold,
            "timestamp": "2026-10-17T00:00:00+00:00",
            "reason": f"Recipe-phrase score {score} (threshold {threshold})"}


def _recipe(name="Chicken Fricassee", n_ing=19, n_step=9):
    return {"name": name,
            "recipeIngredient": [f"ingredient {i}" for i in range(n_ing)],
            "recipeInstructions": [{"@type": "HowToStep", "text": f"step {i}"} for i in range(n_step)]}


def test_phrase_pass_is_untouched():
    r = _recipe()
    v.stamp_validation_on_recipe(r, _validation(13))
    assert r["current_status"]["value"] == "accepted"
    assert r["current_status"]["reason"] == "Recipe-phrase score 13 (threshold 7)"
    assert "recipeVerdictBasis" not in r["_scoring"]
    assert r["_scoring"]["recipeScore"] == 13


def test_transcript_video_accepted_on_structure():
    r = _recipe()
    v.stamp_validation_on_recipe(r, _validation(6))
    assert r["current_status"]["value"] == "accepted"
    assert r["current_status"]["reason"] == \
        "Recipe-phrase score 6 (threshold 7); accepted on structure: 19 ingredients, 9 steps"
    assert r["_scoring"]["recipeVerdictBasis"] == "structure"
    assert r["_scoring"]["recipeScore"] == 6          # the measurement stays


def test_thin_result_stays_rejected():
    r = _recipe(n_ing=2, n_step=1)
    v.stamp_validation_on_recipe(r, _validation(3))
    assert r["current_status"]["value"] == "rejected"
    assert "recipeVerdictBasis" not in r["_scoring"]


def test_no_name_stays_rejected():
    r = _recipe(name="")
    v.stamp_validation_on_recipe(r, _validation(3))
    assert r["current_status"]["value"] == "rejected"


def test_structure_at_the_minimums():
    assert v.structure_accepts(_recipe(n_ing=5, n_step=3)) == "accepted on structure: 5 ingredients, 3 steps"
    assert v.structure_accepts(_recipe(n_ing=4, n_step=3)) is None
    assert v.structure_accepts(_recipe(n_ing=5, n_step=2)) is None


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
