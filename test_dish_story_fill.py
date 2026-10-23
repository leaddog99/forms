"""dish_story: the automatic half of the editorial.

Run: python test_dish_story_fill.py

Proves (on a throwaway SQLite, model and corpus scan stubbed):
  1. a stale signals stamp (cohort_n 0, 61 live rows) is re-stamped, not trusted
     — the Baked Chicken Breast case, story-less for seven weeks;
  2. a stamp that matches the live count is left alone (no scan);
  3. blank_fields: curator fields are never offered; blank enrich/None fields are;
     cook_notes stored as JSON with empty lists is blank;
  4. fill_where_blank: only dishes with a cohort AND a blank field reach the
     model; a dish with nothing blank is never drafted; a dish with no rows
     is skipped with a reason.
"""
from __future__ import annotations

import json
import sqlite3
import sys

from input.pipeline import dish_story, dish_signals
from input.pipeline import dishes as dishes_lib

FAILS = 0


def check(label, got, want):
    global FAILS
    ok = got == want
    FAILS += 0 if ok else 1
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"\n       got  {got!r}\n       want {want!r}"))


def db():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE dishes (name TEXT PRIMARY KEY COLLATE NOCASE,
        story TEXT, story_source TEXT, ethnicity TEXT, ethnicity_source TEXT,
        origin_region TEXT, origin_region_source TEXT, cook_notes TEXT, cook_notes_source TEXT,
        cohort_signals TEXT, editorial_enriched_at TEXT, updated_at TEXT)""")
    c.execute("CREATE TABLE master_recipes (id INTEGER PRIMARY KEY, dish_effective TEXT, "
              "dish_effective_source TEXT)")
    return c


def seed(c, name, rows, signals=None, nearest=0, **fields):
    """`rows` member recipes (matched) plus `nearest` rows that only GUESS the dish."""
    cols = ["name", "cohort_signals"] + list(fields)
    vals = [name, json.dumps(signals) if signals is not None else None] + list(fields.values())
    c.execute(f"INSERT INTO dishes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    c.executemany("INSERT INTO master_recipes (dish_effective, dish_effective_source) VALUES (?, ?)",
                  [(name, "matched")] * rows + [(name, "nearest")] * nearest)


# --- stubs: get_dish reads the real schema, stamp_signals scans the corpus, draft bills ---
def _get_dish(conn, name):
    row = conn.execute("SELECT * FROM dishes WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    d = dict(zip([x[0] for x in conn.execute("SELECT * FROM dishes LIMIT 0").description], row))
    if isinstance(d.get("cook_notes"), str) and d["cook_notes"]:
        d["cook_notes"] = json.loads(d["cook_notes"])
    return d


STAMPED, DRAFTED = [], []


def _stamp(conn, name, **kw):
    n = conn.execute("SELECT COUNT(*) FROM master_recipes WHERE dish_effective = ?", (name,)).fetchone()[0]
    sig = {"cohort_n": n, "ingredients": []}
    conn.execute("UPDATE dishes SET cohort_signals = ? WHERE name = ?", (json.dumps(sig), name))
    STAMPED.append(name)
    return sig


def _draft(conn, name):
    DRAFTED.append(name)
    return {"story": f"The story of {name}.", "ethnicity": "Test", "origin_region": "Here",
            "cook_notes": {"tips": ["one"], "techniques": [], "landmines": []}}


dishes_lib.get_dish = _get_dish
dish_signals.stamp_signals = _stamp
dish_story.draft = _draft

# 1 + 2: the stale stamp
c = db()
seed(c, "Baked Chicken Breast", 61, signals={"cohort_n": 0, "computed_at": "2026-08-31"})
seed(c, "Buttercream Frosting", 58, signals={"cohort_n": 58})
check("stale stamp (0 vs 61 live) is re-stamped", dish_story._ensure_signals(c, "Baked Chicken Breast"), 61)
check("  ...and stamp_signals ran for it", STAMPED, ["Baked Chicken Breast"])
check("matching stamp is trusted", dish_story._ensure_signals(c, "Buttercream Frosting"), 58)
check("  ...with no scan", STAMPED, ["Baked Chicken Breast"])
seed(c, "Empty Dish", 0)
check("no rows -> 0, no scan", (dish_story._ensure_signals(c, "Empty Dish"), len(STAMPED)), (0, 1))

# 3: blank_fields
c = db()
seed(c, "Curated", 5, story="curator wrote", story_source="curator", ethnicity=None,
     ethnicity_source="curator", origin_region="", origin_region_source=None,
     cook_notes=json.dumps({"tips": [], "techniques": [], "landmines": []}), cook_notes_source="enrich")
check("curator fields never offered; blank region + empty-list notes are",
      dish_story.blank_fields(c, "Curated"), ["origin_region", "cook_notes"])
seed(c, "Full", 5, story="s", story_source="enrich", ethnicity="e", ethnicity_source="enrich",
     origin_region="r", origin_region_source="enrich",
     cook_notes=json.dumps({"tips": ["t"], "techniques": [], "landmines": []}), cook_notes_source="enrich")
check("nothing blank -> []", dish_story.blank_fields(c, "Full"), [])
check("unknown dish -> []", dish_story.blank_fields(c, "Nope"), [])
seed(c, "Broccoli", 35, story="Broccoli is...", story_source="enrich", ethnicity=None, ethnicity_source=None,
     origin_region=None, origin_region_source=None, cook_notes=json.dumps({"tips": ["t"], "techniques": [], "landmines": []}),
     cook_notes_source="enrich", editorial_enriched_at="2026-10-17T00:00:00+00:00")
check("drafted before, story present: the model's blanks are its answer -> []",
      dish_story.blank_fields(c, "Broccoli"), [])
seed(c, "Storyless After Draft", 9, story=None, story_source=None, ethnicity="e", ethnicity_source="enrich",
     origin_region="r", origin_region_source="enrich", cook_notes=None, cook_notes_source=None,
     editorial_enriched_at="2026-10-17T00:00:00+00:00")
check("drafted before but no story: every dish owes a story -> retried",
      dish_story.blank_fields(c, "Storyless After Draft"), ["story", "cook_notes"])

# 4: fill_where_blank
STAMPED.clear(); DRAFTED.clear()
seed(c, "Hole Born", 61, signals={"cohort_n": 0})          # the real case: everything blank, stale stamp
seed(c, "No Rows", 0)                                       # blank but nothing to ground in
out = dish_story.fill_where_blank(c, log=lambda m: None)
check("drafted exactly the dishes with a cohort and an owed blank", sorted(DRAFTED),
      ["Curated", "Hole Born", "Storyless After Draft"])
check("Full was never read by the model", "Full" in DRAFTED, False)
check("No Rows has no cohort -> not even selected", "No Rows" in out["skipped"] or "No Rows" in out["filled"], False)
check("Hole Born: all four filled", out["filled"].get("Hole Born"), ["story", "ethnicity", "origin_region", "cook_notes"])
check("Curated: only its two blanks filled", out["filled"].get("Curated"), ["origin_region", "cook_notes"])
row = c.execute("SELECT story, story_source, cook_notes_source FROM dishes WHERE name = 'Curated'").fetchone()
check("Curated's story untouched, stays curator", row[:2], ("curator wrote", "curator"))
check("stale stamp re-stamped on the way", "Hole Born" in STAMPED, True)
out2 = dish_story.fill_where_blank(c, log=lambda m: None)
check("second pass: nothing left to fill, model not called", (out2["filled"], len(DRAFTED)), ({}, 3))

# 5: guesses are not members (Sauce Jean Vignard, 2026-10-23: four nearest guesses, no member)
STAMPED.clear(); DRAFTED.clear()
seed(c, "Sauce Jean Vignard", 0, nearest=4)
check("member_count ignores nearest guesses", dish_story.member_count(c, "Sauce Jean Vignard"), 0)
out3 = dish_story.fill_where_blank(c, log=lambda m: None)
check("guess-only dish is not selected by the sweep", "Sauce Jean Vignard" in DRAFTED, False)
r = dish_story.fill(c, "Sauce Jean Vignard", log=lambda m: None)
check("direct fill (refresh / save hook) skips it with a reason, no model, no scan",
      (r["filled"], "needed" in (r["skipped"] or ""), DRAFTED, STAMPED), ([], True, [], []))
seed(c, "Thin But Real", 1, nearest=6)
r = dish_story.fill(c, "Thin But Real", log=lambda m: None)
check("one real member is enough at the default", r["filled"], ["story", "ethnicity", "origin_region", "cook_notes"])

print("-" * 60)
print("ALL PASSED" if not FAILS else f"{FAILS} FAILED")
sys.exit(1 if FAILS else 0)
