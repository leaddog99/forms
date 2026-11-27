"""The dish editor's Retry button (extract/dish_signal.compose_dish_description), faked model."""
from types import SimpleNamespace

from extract import dish_signal as ds


def _fake(reply, seen):
    def create(**kw):
        seen.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])
    return create


def test_retry_is_grounded_in_the_dishs_recipes_and_the_curators_notes():
    seen = {}
    out = ds.compose_dish_description(
        "Sauce Jean Vignard", alternates=["Sauce Jean Vignard Recipe"],
        recipes=[{"name": "Sauce Jean Vignard", "cuisine": "French",
                  "ingredients": ["white wine", "tomato paste", "Dijon mustard", "cream"],
                  "method": "Reduce the wine."},
                 {"name": "no ingredients, skipped", "ingredients": []}],
        notes="Not a brown sauce.", previous="French brown sauce enriched with red wine.",
        create=_fake('"Sauce Jean Vignard: French cream sauce."', seen))
    assert out["description"] == "Sauce Jean Vignard: French cream sauce."
    assert out["recipes_used"] == 1 and out["model"] == ds.RETRY_MODEL and out["method"] == ds.RETRY_METHOD_ID
    prompt = seen["messages"][0]["content"]
    assert "Dijon mustard" in prompt and "Not a brown sauce." in prompt and "brown sauce enriched" in prompt
    assert seen["model"] == ds.RETRY_MODEL


def test_retry_says_so_when_no_recipes_are_filed_and_never_raises():
    seen = {}
    ds.compose_dish_description("Kokkinisto", create=_fake("Kokkinisto: x.", seen))
    assert "No recipes are filed" in seen["messages"][0]["content"]

    def boom(**_kw):
        raise RuntimeError("no network")
    assert ds.compose_dish_description("Kokkinisto", create=boom)["description"] == ""
