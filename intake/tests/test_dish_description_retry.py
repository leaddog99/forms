"""A dish's description written and checked from evidence
(extract/dish_signal.py prompts + input/pipeline/dish_describe.py), faked model and search."""
import sqlite3
from types import SimpleNamespace

from extract import dish_signal as ds
from input.pipeline import dish_describe as dd

quiet = lambda *_a, **_k: None  # noqa: E731


def _says(reply, seen):
    def create(**kw):
        seen.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])
    return create


def _verdict(agrees, seen=None, **more):
    def create(**kw):
        if seen is not None:
            seen.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"agrees": agrees, **more})])
    return create


def test_the_description_is_written_from_notes_search_results_and_recipes():
    seen = {}
    out = ds.compose_dish_description(
        "Sauce Jean Vignard", alternates=["Sauce Jean Vignard Recipe", "sauce jean vignard"],
        search_results=[{"title": "Sauce Jean Vignard (Pierre Gagnaire)", "snippet": "white wine, cream",
                         "host": "papillesetpupilles.fr"}],
        recipes=[{"name": "Sauce Jean Vignard", "cuisine": "French",
                  "ingredients": ["white wine", "tomato paste", "Dijon mustard", "cream"],
                  "method": "Reduce the wine."},
                 {"name": "no ingredients, skipped", "ingredients": []}],
        notes="Not a brown sauce.", previous="French brown sauce enriched with red wine.",
        create=_says('"Sauce Jean Vignard: French cream sauce."', seen))
    assert out == {"description": "Sauce Jean Vignard: French cream sauce.", "recipes_used": 1,
                   "results_used": 1, "model": ds.DESCRIBE_MODEL, "method": ds.DESCRIBE_METHOD_ID}
    prompt = seen["messages"][0]["content"]
    for must in ("Dijon mustard", "Not a brown sauce.", "brown sauce enriched",
                 "Sauce Jean Vignard (Pierre Gagnaire) (papillesetpupilles.fr) - white wine, cream",
                 "Other names and search phrases: Sauce Jean Vignard Recipe"):
        assert must in prompt
    assert seen["model"] == ds.DESCRIBE_MODEL


def test_missing_evidence_is_stated_and_a_failed_call_returns_no_text():
    seen = {}
    ds.compose_dish_description("Kokkinisto", create=_says("Kokkinisto: x.", seen))
    assert "No search results and no recipes" in seen["messages"][0]["content"]
    ds.compose_dish_description("Kokkinisto", search_results=[{"title": "Greek beef stew", "snippet": ""}],
                                create=_says("Kokkinisto: x.", seen))
    assert "No recipes are filed" in seen["messages"][0]["content"]

    def boom(**_kw):
        raise RuntimeError("no network")
    assert ds.compose_dish_description("Kokkinisto", create=boom)["description"] == ""


def test_describe_searches_the_first_search_line_then_describes():
    seen, asked = {}, {}

    def search(q, **kw):
        asked.update(q=q, **kw)
        return [{"title": "Licurdia: Calabrian Onion Soup", "snippet": "Tropea onions",
                 "link": "https://www.lacucinaitaliana.com/x"}]
    dish = {"name": "Licurdia", "queries": ["Licurdia"],
            "query_rows": [{"q": "Licurdia", "gl": "it", "hl": "it"}], "notes": "from Cosenza"}
    out = dd.describe(dish, search=search, create=_says("Licurdia: Calabrian onion soup.", seen), log=quiet)
    assert out["description"] == "Licurdia: Calabrian onion soup." and out["results_used"] == 1
    assert asked == {"q": "Licurdia", "pages": 1, "want": ds.MAX_SEARCH_RESULTS, "gl": "it", "hl": "it"}
    prompt = seen["messages"][0]["content"]
    assert "Calabrian Onion Soup (lacucinaitaliana.com)" in prompt and "from Cosenza" in prompt


def test_a_failed_search_still_describes_and_a_typod_key_is_never_shown():
    seen = {}

    def search(q, **kw):
        raise RuntimeError("search is down")
    dish = {"name": "Chicken Marenga", "display_name": "Chicken Marengo",
            "queries": ["Chicken Marenga", "Chicken Marengo Recipe"], "query_rows": [{"q": "Chicken Marengo"}]}
    out = dd.describe(dish, search=search, create=_says("Chicken Marengo: x.", seen), log=quiet)
    assert out["description"] == "Chicken Marengo: x."
    assert "Marenga" not in seen["messages"][0]["content"]


def test_the_judge_reports_a_real_disagreement():
    seen = {}
    recipes = [{"name": "Melopita", "ingredients": ["mizithra", "honey", "eggs"], "method": "Bake."}] * 2
    v = ds.judge_dish_description("Melopita", description="Melopita: phyllo pie.", recipes=recipes,
                                  create=_verdict(False, seen, why="no phyllo", description="Melopita: custard pie."))
    assert v["agrees"] is False and v["description"] == "Melopita: custard pie." and v["recipes_used"] == 2
    assert "Stored description to judge: Melopita: phyllo pie." in seen["messages"][0]["content"]
    assert seen["tool_choice"] == {"type": "tool", "name": "description_verdict"}
    assert ds.judge_dish_description("Melopita", description="x", recipes=recipes,
                                     create=_verdict(True))["agrees"] is True


def test_the_after_refresh_check_suggests_never_overwrites_and_respects_a_dismissal(monkeypatch):
    conn = sqlite3.connect(":memory:")
    dish = {"name": "Melopita", "description": "Melopita: phyllo pie.", "notes": ""}
    from input.pipeline import dishes as dishes_lib
    monkeypatch.setattr(dishes_lib, "get_dish", lambda _c, _n: dict(dish))
    monkeypatch.setattr(dd, "recipes_evidence", lambda _c, _n: [{"name": "m", "ingredients": ["honey"]}] * 3)
    monkeypatch.setattr(dd, "_setting", lambda _k: True)

    assert dd.get_suggestion(conn, dish) is None                      # no table yet: a page load is safe
    disagree = _verdict(False, why="no phyllo", description="Melopita: custard pie.")
    assert dd.check_after_refresh(conn, "Melopita", create=disagree, log=quiet)["agrees"] is False
    got = dd.get_suggestion(conn, dish)
    assert got["suggestion"] == "Melopita: custard pie." and got["why"] == "no phyllo" and got["recipes"] == 3
    # The curator edits the description: the old suggestion no longer applies.
    assert dd.get_suggestion(conn, {**dish, "description": "Melopita: something else."}) is None
    # "Keep mine": gone from the page, and this wording is not judged again.
    assert dd.dismiss(conn, "Melopita") is True and dd.get_suggestion(conn, dish) is None

    def must_not_run(**_kw):
        raise AssertionError("a dismissed wording was judged again")
    assert dd.check_after_refresh(conn, "Melopita", create=must_not_run, log=quiet) is None
    # A new wording is judged; agreement clears the row.
    dish["description"] = "Melopita: custard pie."
    assert dd.check_after_refresh(conn, "Melopita", create=_verdict(True), log=quiet)["agrees"] is True
    assert conn.execute("select count(*) from dish_description_suggestions").fetchone()[0] == 0
