"""Foreign-language publishers (2026-11-27, lacuisinedegeraldine.fr job 3347):
structured-lane translation, no saving in the source language, one seat per
recipe across language editions, and titles for title-less harvest candidates."""
import json

import pytest

from intake import translate as T
from intake.build_query_batch import _page_title_and_alternates, _looks_like_recipe_collection
from input.pipeline.collections_lib import collapse_language_twins
from input.pipeline import candidate_ledger

quiet = lambda *_a, **_k: None  # noqa: E731

FR = {
    "@type": "Recipe", "name": "Ratatouille, recette traditionnelle",
    "image": "https://x.test/r.jpg", "author": {"name": "Géraldine"},
    "recipeIngredient": ["3 c. à soupe d'huile d'olive", "1 aubergine"],
    "recipeInstructions": [{"@type": "HowToStep", "text": "Faire revenir."},
                           {"@type": "HowToStep", "text": "Laisser mijoter."}],
    "review": [{"reviewBody": "Délicieux " * 500}],
}


def _reply(obj, truncated=False):
    def fake(markdown, src_lang):
        fake.sent = markdown
        return T.TranslationResult("```json\n" + json.dumps(obj) + "\n```", None,
                                   src_lang, "French", truncated=truncated)
    return fake


def test_structured_lane_translates_text_and_keeps_the_rest(monkeypatch):
    en = {"name": "Ratatouille, Traditional Recipe",
          "recipeIngredient": ["3 tbsp olive oil", "1 eggplant"],
          "recipeInstructions": [{"@type": "HowToStep", "text": "Sauté."},
                                 {"@type": "HowToStep", "text": "Simmer."}]}
    fake = _reply(en)
    monkeypatch.setattr(T, "translate_markdown", fake)
    out, meta = T.translate_recipe_jsonld(FR, "fr", log=quiet)
    assert out["name"] == "Ratatouille, Traditional Recipe"
    assert out["image"] == FR["image"] and out["author"] == FR["author"]
    assert meta["originalTitle"] == "Ratatouille, recette traditionnelle" and meta["translated"]
    assert "Délicieux" not in fake.sent          # reader reviews are never sent


def test_structured_lane_refuses_a_changed_ingredient_count_or_a_cut_off_reply(monkeypatch):
    short = {"name": "Ratatouille", "recipeIngredient": ["1 eggplant"],
             "recipeInstructions": FR["recipeInstructions"]}
    monkeypatch.setattr(T, "translate_markdown", _reply(short))
    assert T.translate_recipe_jsonld(FR, "fr", log=quiet) == (None, None)
    full = {"name": "Ratatouille", "recipeIngredient": ["a", "b"],
            "recipeInstructions": FR["recipeInstructions"]}
    monkeypatch.setattr(T, "translate_markdown", _reply(full, truncated=True))
    assert T.translate_recipe_jsonld(FR, "fr", log=quiet) == (None, None)


def test_a_failed_page_translation_raises_instead_of_returning_the_original(monkeypatch):
    calls = []

    def bad(markdown, src_lang):
        calls.append(1)
        return T.ExtractionTranslationResult("x", None, src_lang, "French",
                                             plausibility_ok=False,
                                             plausibility_reason="length ratio 0.38 < 0.4")
    monkeypatch.setattr(T, "translate_extraction_markdown", bad)
    with pytest.raises(T.TranslationFailed) as ei:
        T.translate_markdown_checked("# Ratatouille\n" + "texte " * 200, "fr", log=quiet)
    assert len(calls) == 2 and "0.38" in str(ei.value)


def test_page_title_and_hreflang_links():
    html = ('<html><head><title>25 idées de salades - La cuisine</title>'
            '<meta property="og:title" content="25 id&eacute;es de salades compos&eacute;es">'
            '<link rel="alternate" hreflang="fr" href="https://s.fr/ratatouille">'
            "<link rel='alternate' hreflang='en-US' href='/en/ratatouille-recipe'>"
            '<link rel="alternate" hreflang="x-default" href="https://s.fr/ratatouille">'
            '<link rel="alternate" type="application/rss+xml" href="/feed"></head></html>')
    title, alts = _page_title_and_alternates(html, "https://s.fr/ratatouille")
    assert title == "25 idées de salades composées"
    assert alts == {"fr": "https://s.fr/ratatouille", "en": "https://s.fr/en/ratatouille-recipe"}
    assert _looks_like_recipe_collection("25 Composed Salad Ideas (Easy and Original)")


def test_one_seat_per_recipe_across_language_editions():
    fr = {"url": "https://s.fr/ratatouille-recette", "pa": 32}
    en = {"url": "https://s.fr/en/ratatouille-recipe", "pa": 34}
    solo = {"url": "https://s.fr/granola", "pa": 30}
    en_only = {"url": "https://s.fr/en/onion-soup", "pa": 29}
    ranked = [en, fr, solo, en_only]
    alt = {fr["url"]: {"en": "https://www.s.fr/en/ratatouille-recipe/", "fr": fr["url"]},
           en_only["url"]: {"fr": "https://s.fr/soupe-a-loignon"}}     # twin not in the pool
    lang = {fr["url"]: "fr", en["url"]: "en", solo["url"]: "fr", en_only["url"]: "en"}
    kept, dropped = collapse_language_twins(ranked, alt, lang, "en")
    assert kept == [en, solo, en_only]
    assert dropped == [(fr, en)]
    # The French edition ranks first: the English one still takes that seat.
    kept2, dropped2 = collapse_language_twins([fr, solo, en], alt, lang, "en")
    assert kept2 == [en, solo] and dropped2 == [(fr, en)]
    # A French instance keeps the French edition.
    kept3, _ = collapse_language_twins(ranked, alt, lang, "fr")
    assert kept3 == [fr, solo, en_only]


def test_language_twin_is_ledgered_as_a_fact():
    assert candidate_ledger.classify("language-twin: https://s.fr/en/x") == ("rank_cut", False)
