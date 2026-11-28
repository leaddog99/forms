"""A dish's chapter is read from its description by the model; the name is only the fallback."""
import sqlite3

import extract.chapter_classifier as cc
import extract.identity_card as ic
from input.pipeline import dish_describe, embeddings


def _setup(monkeypatch, answers, description="Kormos: Greek no-bake chocolate biscuit log shaped like a salami."):
    """answers: {(text, shortcut): chapter}. Returns (conn, calls)."""
    calls = []

    def fake_classify(name, ingredients=None, *, shortcut=True, **kw):
        calls.append((name, shortcut))
        return answers.get((name, shortcut), "Uncertain")

    monkeypatch.setattr(cc, "classify_chapter", fake_classify)
    monkeypatch.setattr(ic, "generate_identity_card_for_dish", lambda row: {"likelyDish": row["name"]})
    monkeypatch.setattr(dish_describe, "describe", lambda row: {"description": description})
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE dishes(name TEXT, description TEXT, chapter TEXT, identity_card TEXT, updated_at TEXT)")
    return conn, calls


def _run(conn, **row):
    row = {"name": "Kormos", **row}
    conn.execute("INSERT INTO dishes(name, description, chapter) VALUES(?,?,?)",
                 (row["name"], row.get("description"), row.get("chapter")))
    embeddings.ensure_dish_description(conn, row)
    return conn.execute("SELECT chapter FROM dishes WHERE name = ?", (row["name"],)).fetchone()[0]


DESC = "Kormos: Greek no-bake chocolate biscuit log shaped like a salami."


def test_the_description_decides_and_the_keyword_layer_is_off(monkeypatch):
    conn, calls = _setup(monkeypatch, {(DESC, False): "Candies & Confections", ("Kormos", True): "Meat"})
    assert _run(conn) == "Candies & Confections"
    assert calls == [(DESC, False)]          # the name was never asked


def test_the_name_is_the_fallback_when_the_description_gives_no_answer(monkeypatch):
    conn, calls = _setup(monkeypatch, {("Kormos", True): "Candies & Confections"})
    assert _run(conn) == "Candies & Confections"
    assert calls == [(DESC, False), ("Kormos", True)]


def test_a_stored_chapter_is_kept_but_uncertain_is_tried_again(monkeypatch):
    conn, calls = _setup(monkeypatch, {(DESC, False): "Candies & Confections"})
    assert _run(conn, chapter="Meat") == "Meat" and calls == []
    conn, calls = _setup(monkeypatch, {(DESC, False): "Candies & Confections"})
    assert _run(conn, chapter="Uncertain") == "Candies & Confections"
