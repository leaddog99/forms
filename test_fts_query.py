"""The search box grammar (_fts_query) against a real FTS5 table with the index's
own column list, so every emitted expression is proven to PARSE as well as to
mean what the docstring says. Run: python test_fts_query.py

Importing save_recipe_api runs the schema ensure on ./recipes.db like every
import of it does; the table here is an in-memory stand-in for the index.
"""
import sqlite3

import save_recipe_api as api

ROWS = [
    ("Lemon Chicken", "Lemon Chicken", "chicken lemon garlic", "Greek",
     "J. Kenji López-Alt", "seriouseats.com seriouseats com Serious Eats Serious Eats"),
    ("Butter Chicken", "Butter Chicken", "chicken lemon cream", "Indian",
     "Nagi", "recipetineats.com recipetineats com RecipeTin Eats RecipeTin Eats"),
    ("Chocolate Cake", "Chocolate Cake", "flour cocoa eggs", "American",
     "Melissa Clark", "cooking.nytimes.com cooking nytimes com NYT Cooking NYT Cooking"),
]


def _table():
    c = sqlite3.connect(":memory:")
    c.execute(f"CREATE VIRTUAL TABLE f USING fts5({', '.join(api.FTS_COLUMNS)}, "
              f"tokenize='porter unicode61 remove_diacritics 2')")
    c.executemany("INSERT INTO f VALUES (?,?,?,?,?,?)", ROWS)
    return c


def names(c, raw, about=False):
    expr = api._fts_query(raw)
    if not expr:
        return []
    if about:
        expr = "{" + " ".join(api.FTS_ABOUT_COLUMNS) + "} : (" + expr + ")"
    return sorted(r[0] for r in c.execute("SELECT name FROM f WHERE f MATCH ?", [expr]))


CASES = [
    # raw, expected names, expected tier-1 ("about") names
    ("chicken lemon", ["Butter Chicken", "Lemon Chicken"], ["Lemon Chicken"]),
    ('"butter chicken"', ["Butter Chicken"], ["Butter Chicken"]),
    ("chicken or cake", ["Butter Chicken", "Chocolate Cake", "Lemon Chicken"], None),
    ("chicken -butter", ["Lemon Chicken"], None),
    ("chicken not butter", ["Lemon Chicken"], None),
    ("chicken AND NOT butter", ["Lemon Chicken"], None),          # was a 500
    ("chicken AND OR NOT", ["Butter Chicken", "Lemon Chicken"], None),
    ("author:kenji", ["Lemon Chicken"], ["Lemon Chicken"]),
    ("chef:nagi chicken", ["Butter Chicken"], ["Butter Chicken"]),
    ("by:clark", ["Chocolate Cake"], None),
    ("-author:kenji chicken", ["Butter Chicken"], None),          # exclusion typed first
    ("-lemon chicken or cake", ["Chocolate Cake"], None),         # (chicken OR cake) NOT lemon
    ("-garlic -cake chicken", ["Butter Chicken"], None),          # (chicken) NOT garlic NOT cake
    ('site:"serious eats"', ["Lemon Chicken"], None),
    ("site:nytimes", ["Chocolate Cake"], None),
    ("publisher:recipetineats.com", ["Butter Chicken"], None),
    ("serious eats", ["Lemon Chicken"], ["Lemon Chicken"]),       # publisher words, unprefixed
    ("nyt cooking chocolate", ["Chocolate Cake"], ["Chocolate Cake"]),
    ("ingredient:cream", ["Butter Chicken"], []),                 # contains, not about
    ("author: kenji", [], None),                                  # space after the colon: plain words
    ("re:chicken", [], None),                                     # unknown prefix becomes a word
    ("chowders", [], None),
    ("-clam", [], None),                                          # nothing to subtract from
    ("((((", [], None),
]


def test_grammar():
    c = _table()
    for raw, want, want_about in CASES:
        got = names(c, raw)
        assert got == want, f"{raw!r}: {got} != {want}  (expr {api._fts_query(raw)!r})"
        if want_about is not None:
            got_about = names(c, raw, about=True)
            assert got_about == want_about, f"{raw!r} about: {got_about} != {want_about}"


def test_stemming_and_accents():
    c = _table()
    assert names(c, "lopez") == ["Lemon Chicken"]
    assert names(c, "cakes") == ["Chocolate Cake"]


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e}")
    print("all passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
