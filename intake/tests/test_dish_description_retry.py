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
    assert "No search results and no recipes" in seen["messages"][0]["content"]
    out = ds.compose_dish_description(
        "Kokkinisto", search_results=[{"title": "Kokkinisto: Greek beef stew", "snippet": "simmered in tomato",
                                       "host": "x.test"}],
        create=_fake("Kokkinisto: Greek stew.", seen))
    assert out["results_used"] == 1 and "Greek beef stew" in seen["messages"][0]["content"]
    assert "No recipes are filed" in seen["messages"][0]["content"]

    def boom(**_kw):
        raise RuntimeError("no network")
    assert ds.compose_dish_description("Kokkinisto", create=boom)["description"] == ""


# --- search first, then describe; and the after-refresh check ---------------

def test_a_new_dish_is_described_from_its_search_results():
    from input.pipeline import dish_describe as dd
    seen, asked = {}, {}

    def search(q, **kw):
        asked["q"], asked["kw"] = q, kw
        return [{"title": "Licurdia: Calabrian Onion Soup", "snippet": "Tropea onions",
                 "link": "https://www.lacucinaitaliana.com/x"}]
    row = {"name": "Licurdia", "queries": ["Licurdia"], "query_rows": [{"q": "Licurdia", "gl": None, "hl": None}]}
    text = dd.describe_new(row, search=search, create=_fake("Licurdia: Calabrian onion soup.", seen),
                           log=lambda *_a: None)
    assert text == "Licurdia: Calabrian onion soup."
    assert asked["q"] == "Licurdia" and asked["kw"]["pages"] == 1
    assert "Calabrian Onion Soup (lacucinaitaliana.com)" in seen["messages"][0]["content"]


def test_a_failed_search_still_describes_from_the_name():
    from input.pipeline import dish_describe as dd
    seen = {}

    def search(q, **kw):
        raise RuntimeError("search is down")
    row = {"name": "Moussaka", "queries": ["Moussaka"], "query_rows": [{"q": "Moussaka"}]}
    assert dd.describe_new(row, search=search, create=_fake("Moussaka: x.", seen),
                           log=lambda *_a: None) == "Moussaka: x."


def test_the_check_files_a_suggestion_only_on_a_real_disagreement():
    seen = {}

    def tool(agrees, **more):
        def create(**kw):
            seen.update(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"agrees": agrees, **more})])
        return create
    recipes = [{"name": "Melopita", "ingredients": ["mizithra", "honey", "eggs"], "method": "Bake."}] * 2
    v = ds.judge_dish_description("Melopita", description="Melopita: phyllo pie.", recipes=recipes,
                                  create=tool(False, why="no phyllo", description="Melopita: custard pie."))
    assert v["agrees"] is False and v["description"] == "Melopita: custard pie." and v["recipes_used"] == 2
    assert "Stored description to judge: Melopita: phyllo pie." in seen["messages"][0]["content"]
    assert ds.judge_dish_description("Melopita", description="x", recipes=recipes, create=tool(True))["agrees"] is True
