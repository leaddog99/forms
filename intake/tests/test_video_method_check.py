"""The video method check's own logic (the model call is faked)."""
from types import SimpleNamespace

from input.pipeline import video_method_check as vmc

RECIPE = {"name": "Sweet Potato Casserole",
          "recipeIngredient": ["3 lb sweet potatoes", "1/2 cup butter"],
          "recipeInstructions": [{"@type": "HowToStep", "text": "Boil the potatoes 20 minutes."},
                                 {"@type": "HowToStep", "text": "Mash with the butter and bake at 350°F for 30 minutes."}]}


def _reply(**over):
    data = {k: {"ok": True, "note": "fine"} for k in vmc.CHECKS}
    data["reason"] = "sound"
    data.update(over)
    return lambda **_kw: SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=data)],
                                         stop_reason="tool_use")


def test_all_five_yes_is_a_pass():
    r = vmc.check(RECIPE, "boil the potatoes about twenty minutes", "3 lb sweet potatoes", create=_reply())
    assert r["verdict"] == "pass" and r["failed"] == [] and r["method"] == vmc.METHOD_ID
    assert set(r["checks"]) == set(vmc.CHECKS) and "recipe_lesson" in vmc.CHECKS


def test_one_no_is_a_fail_and_names_it():
    r = vmc.check(RECIPE, "so we went to this street stall", "",
                  create=_reply(recipe_lesson={"ok": False, "note": "a street-food travelogue"}, reason=""))
    assert r["verdict"] == "fail" and r["failed"] == ["recipe_lesson"]
    assert r["reason"] == "a street-food travelogue"          # falls back to the failed check's note


def test_no_captions_and_bad_replies_are_errors_not_passes():
    assert vmc.check(RECIPE, "", "", create=_reply())["verdict"] == "error"
    boom = lambda **_kw: (_ for _ in ()).throw(RuntimeError("down"))        # noqa: E731
    assert vmc.check(RECIPE, "words", "", create=boom, log=lambda *_: None)["verdict"] == "error"
    half = lambda **_kw: SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"reason": "x"})],  # noqa: E731
                                         stop_reason="max_tokens")
    assert vmc.check(RECIPE, "words", "", create=half, log=lambda *_: None)["verdict"] == "error"


def test_a_recipe_with_no_steps_fails_without_a_call():
    called = []
    r = vmc.check({"name": "x", "recipeIngredient": ["a"]}, "words", "", create=lambda **k: called.append(1))
    assert r["verdict"] == "fail" and not called
