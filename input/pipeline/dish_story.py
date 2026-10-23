"""dish_story.py — dish-level editorial (story, ethnicity, origin region, cook's
notes), the domains deep-enrich pattern applied to a dish.

ONE grounded Sonnet call per dish: the measured cohort signals (ingredient /
equipment terms with lift) plus the method text of the dish's strongest
winners, returned as four fields. Until 2026-10-16 this lived only behind the
editor's ✨ button and filled the FORM for the curator to save, so 10 of 461
dishes had a story while 226 of 508 domains did. Now the same call also FILLS
BLANKS on its own — after a dish refresh (the cohort has just been rebuilt),
when a saved recipe joins a dish's cohort (the save core queues a
dish_story_fill job for that one dish), at the end of the nightly dish_rematch
sweep (where a hole-born dish gains its first rows; `fill_where_blank`), and as
the dish_story_fill sweep job — under one rule:

    fill what is blank, never overwrite what a curator wrote, stamp the source.

Each field carries a sibling `<field>_source` column: 'enrich' (this module
wrote it) or 'curator' (typed or accepted in the editor; update_dish stamps it
when the VALUE changes). A curator value is never replaced here, even with
force=True. The ✨ button keeps drafting into the form; a draft the curator
Saves becomes 'curator' like any other edit.

Spend is journaled under operation "dish_story_draft" (unchanged name, so the
history stays one line).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

FIELDS = ("story", "ethnicity", "origin_region", "cook_notes")
OPERATION = "dish_story_draft"
MODEL = "claude-sonnet-5"
SOURCE_ENRICH = "enrich"
SOURCE_CURATOR = "curator"
# The rungs of dish_effective that make a recipe a MEMBER of the dish. The third
# rung, 'nearest', is a rejected match's best guess: evidence for the signals,
# never a reason to write a story (see member_count).
MEMBER_SOURCES = ("assigned", "matched")


_NOTE = {"type": "string",
         "description": "One short paragraph of connected sentences, 40-80 words, spoken to the "
                        "cook. No bullets, no numbering, no line breaks, no tags."}
_TOOL = {
    "name": "submit_dish_editorial",
    "description": "Submit the dish's story, provenance and cook's notes.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["story", "ethnicity", "origin_region", "tips", "techniques", "landmines"],
        "properties": {
            "story": {"type": "string",
                      "description": "Two or three short paragraphs separated by a blank line; "
                                     "120-200 words in all. Reader-facing narrative."},
            "ethnicity": {"type": "string", "description": "One word/phrase; empty if genuinely mixed or unclear."},
            "origin_region": {"type": "string", "description": "Region/country; empty if unknown."},
            # Prose, not lists (curator, 2026-10-16: "soften them into sentences
            # rather than bullets, more of a dialog than staccato points"). Flat
            # strings either way: array parameters came back as <item>/<parameter>
            # tagged text (Soutzoukakia, 2026-10-16).
            "tips": _NOTE,
            "techniques": _NOTE,
            "landmines": _NOTE,
        },
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return not v.strip()
    if isinstance(v, dict):
        return not any(v.get(k) for k in ("tips", "techniques", "landmines"))
    return not v


def build_prompt(conn: sqlite3.Connection, dish: dict) -> str:
    """The grounded prompt — verbatim the ✨ endpoint's, now shared."""
    from intake.products.dish_class_proposals import _winner_methods
    name = dish["name"]
    sig = {}
    try:
        row = conn.execute("SELECT cohort_signals FROM dishes WHERE name = ?", (name,)).fetchone()
        sig = json.loads(row[0]) if row and row[0] else {}
    except Exception:
        pass
    methods = _winner_methods(conn, name, k=3, cap=1500)
    terms = ", ".join(f"{r['term']} ({r['pct']}%/{r['lift']}x)"
                      for r in (sig.get("ingredients") or [])[:15])
    eq = ", ".join(r["term"] for r in (sig.get("equipment") or [])[:10])
    prov = sig.get("provenance") or {}
    meth = "\n".join(f"--- {m['name']}\n{m['method']}" for m in methods) or "(none)"
    return f"""Write dish-level editorial for a cooking site. GROUND every claim in the
material below — the measured signals and the actual method text; where recipes
disagree, that disagreement IS a landmine.

VOICE. Inform, entertain, and if the material allows it, surprise. Write like a
cook who has made this fifty times talking to a friend who is about to make it
once: specific, warm, a little opinionated, never gushing. Lead with the most
interesting true thing about this dish, not with a definition. Every sentence
must carry a fact that is specific to THIS dish — if a sentence could be pasted
into another dish's story, cut it.

NOT ALLOWED (this is what makes text read as machine-written): "beloved",
"classic", "humble", "elevate", "a testament to", "perfect for", "whether
you're … or …", "the secret is", "take it to the next level", "comfort food at
its finest", "rich and flavorful", "bursting with flavor", "simple yet",
rhetorical questions, exclamation marks, a sentence that tells the reader how
they will feel, an opening that defines the dish by its name, a closing that
sums up or invites the reader to try it, and lists of three adjectives.

NUMBERS. The signals below are measurements; the reader never sees a
spreadsheet. No percentages, decimals or ratios in the prose - say "three in
four recipes", "nearly every recipe", "about half", "only a handful". At most
one such count in the whole story, and none in the notes. Never describe the
measurement itself ("cited at a ratio", "leans toward", "shows up as a
signal", "the data suggests"): talk about recipes and cooks, never about the
numbers behind them.

WHO SAYS WHAT. The story and the notes split the material; a fact lives in
one place. The story owns the why, the history and the argument between
recipes. The notes own the instruction at the stove. The story may name the
dish's single biggest landmine as its hook, in one clause, but the actionable
version of it lives only in the notes - and the notes never restate anything
the story has already said. The three notes are each ONE short paragraph in
the same voice as the story - a cook talking, not a checklist - and a fact
appears once across all three: tips say what to do, techniques say what the
dish turns on and why, landmines say how it goes wrong. If the same fact
wants to be in two of them, it belongs in the one where it is most useful.

DISH: {name} (chapter: {dish.get('chapter') or '?'})
DESCRIPTION: {(dish.get('description') or '')[:300]}
MEASURED SIGNALS ({sig.get('cohort_n') or 0} recipes): {terms or '(none)'}
EQUIPMENT: {eq or '(none)'}
PROVENANCE: ethnicities={prov.get('ethnicities') or {}} regions={prov.get('regions') or {}}
METHOD TEXT OF TOP RECIPES:
{meth}

Call submit_dish_editorial with:
- story: two or three SHORT paragraphs separated by a blank line, 120-200 words in
  all — what this dish is, where it comes from, why it works, said through the
  specifics in the material. One idea per paragraph; never one block of text
- ethnicity: one word/phrase, or empty if genuinely mixed/unclear
- origin_region: region/country, or empty
- tips: one paragraph, 40-80 words, three or four concrete things to do at the
  stove, grounded in the methods, connected as a cook would say them
- techniques: one paragraph, 40-80 words, the two or three moves the dish turns
  on and why, each named in passing
- landmines: one paragraph, 40-80 words, how this dish actually goes wrong — prefer
  points where the cohort's recipes disagree or the methods warn
Each note is prose: no bullets, numbering, line breaks or tags; nothing the story
already says; nothing another note already says."""


def draft(conn: sqlite3.Connection, name: str) -> dict:
    """Run the call and return {story, ethnicity, origin_region, cook_notes}.
    Saves nothing. Raises LookupError for an unknown dish and RuntimeError when
    the reply is unusable (the caller decides whether to retry)."""
    import llm
    from input.pipeline import dishes as dishes_lib
    d = dishes_lib.get_dish(conn, name)
    if d is None:
        raise LookupError(f"Dish {name!r} not found")
    prompt = build_prompt(conn, d)
    llm.enter(recipe_id=f"dish:{d['name']}", user_id=0)
    # Forced tool call, like the recipe enrich blocks: the reply IS the JSON,
    # schema-checked by the API. The old regex-over-prose parse broke on the
    # first apostrophe inside a string (Soutzoukakia, 2026-10-16).
    msg = llm.create(operation=OPERATION, model=MODEL, max_tokens=6000,
                     messages=[{"role": "user", "content": prompt}],
                     tools=[_TOOL], tool_choice={"type": "tool", "name": _TOOL["name"]})
    out = next((b.input for b in msg.content
                if getattr(b, "type", None) == "tool_use" and b.name == _TOOL["name"]), None)
    if not isinstance(out, dict):
        raise RuntimeError("model reply unusable (no submit_dish_editorial call) — run again")
    out = _unleak(out)
    return {
        "story": (out.get("story") or "").strip(),
        "ethnicity": (out.get("ethnicity") or "").strip(),
        "origin_region": (out.get("origin_region") or "").strip(),
        "cook_notes": {k: _as_list(out.get(k)) for k in ("tips", "techniques", "landmines")},
    }


_LEAK = re.compile(r"</?(?:story|ethnicity|origin_region|tips|techniques|landmines|parameter)\b[^>]*>")


def _unleak(out: dict) -> dict:
    """Once in five calls (Skordalia, 2026-10-16) the model ran the tool
    parameters together as tagged text inside ONE string field, e.g.
    'story text</story> <parameter name="ethnicity">Greek', and left the real
    `ethnicity` empty. Cut each string at the first tag, and give every tagged
    segment to the field it names when that field is blank."""
    props = _TOOL["input_schema"]["properties"]
    fixed = dict(out)
    for k, v in out.items():
        if not isinstance(v, str) or not _LEAK.search(v):
            continue
        parts = _LEAK.split(v)
        tags = [m.group(0) for m in _LEAK.finditer(v)]
        fixed[k] = parts[0].strip()
        for tag, seg in zip(tags, parts[1:]):
            m = re.match(r'<(?:parameter name="(\w+)"|(\w+))>', tag)
            field = (m.group(1) or m.group(2)) if m else None
            if field in props and seg.strip() and not (fixed.get(field) or "").strip():
                fixed[field] = seg.strip()
    return fixed


def _as_list(v) -> list[str]:
    """The three note fields travel as one-item-per-line strings (see _TOOL).
    Tolerant of the shapes the model has actually produced: a real list, a
    JSON-array string, '<item>…</item>' tags, leading bullets/numbers."""
    if isinstance(v, list):
        items = [str(x) for x in v]
    elif isinstance(v, str):
        t = v.strip()
        if t.startswith("[") and t.endswith("]"):
            try:
                return _as_list(json.loads(t))
            except json.JSONDecodeError:
                pass
        items = re.findall(r"<item>(.*?)</item>", t, re.S) or t.splitlines()
    else:
        return []
    out = []
    for x in items:
        x = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", str(x)).strip()
        if x:
            out.append(x)
    return out


def _ensure_signals(conn: sqlite3.Connection, name: str) -> Optional[int]:
    """The prompt is only as grounded as the signals. Stamp them when the dish
    has none yet — or when the stored stamp no longer describes the cohort
    (no LLM, one corpus scan, ~2-4 s).

    The stamp's `cohort_n` is compared with the LIVE row count, not trusted:
    Baked Chicken Breast was created from the holes report on 2026-08-31, its
    signals stamped a minute later at cohort 0, and the rematch then handed it
    61 rows. The stored 0 made every later fill (the 10-17 sweep included)
    return "no cohort" without a word, and the dish stayed story-less for seven
    weeks while 61 recipes sat under it. A stale stamp also grounds the prose in
    LAST refresh's cohort, so a changed count re-stamps before the model reads."""
    n = conn.execute("SELECT COUNT(*) FROM master_recipes WHERE dish_effective = ?",
                     (name,)).fetchone()[0]
    if not n:
        return 0
    row = conn.execute("SELECT cohort_signals FROM dishes WHERE name = ?", (name,)).fetchone()
    stored = None
    if row and row[0]:
        try:
            stored = json.loads(row[0]).get("cohort_n")
        except Exception:
            stored = None
    if stored == n:
        return n
    from input.pipeline import dish_signals
    sig = dish_signals.stamp_signals(conn, name)
    return sig.get("cohort_n")


def member_count(conn: sqlite3.Connection, name: str) -> int:
    """Recipes that ARE the dish: assigned, or confidently matched. A dish needs
    `dish_story_min_members` of them (system_config, default 1) before the
    automatic fill writes about it.

    Why not the whole cohort: Sauce Jean Vignard was created on 2026-10-23 with
    no recipes of its own; the rematch made it the NEAREST guess of four
    unmatched rows (a demi-glace, a champagne sauce, a brown sauce and a chestnut
    soup, all at distance 0.77-0.88 against a 0.6 bar) and the fill wrote its
    story from them as 'cohort=4'. The same rule the search's dish layer has kept
    since 2026-09-05: a dish means recipes that are the dish, nothing looser."""
    ph = ", ".join("?" * len(MEMBER_SOURCES))
    return conn.execute(
        f"SELECT COUNT(*) FROM master_recipes WHERE dish_effective = ? "
        f"AND dish_effective_source IN ({ph})", (name, *MEMBER_SOURCES)).fetchone()[0]


def _min_members() -> int:
    n = 1
    try:
        from input.pipeline import system_config as _cfg
        n = int(_cfg.get_setting("dish_story_min_members", n) or n)
    except Exception:
        pass
    return max(1, n)


def blank_fields(conn: sqlite3.Connection, name: str) -> list[str]:
    """The editorial fields the AUTOMATIC fill owes this dish: blank, not the
    curator's, and not already declined by the model. Cheap (one row read, no
    model) — the save path and the rematch sweep ask this before they spend a
    job on the dish. Unknown dish -> [].

    A dish the model has already drafted (`editorial_enriched_at` set) with its
    story in place owes nothing more: what the model left blank then, it left
    blank on purpose — Broccoli, Cabbage, Fried Eggs and eight more have no
    ethnicity or region to name (eleven dishes after the 10-17 sweep), and a
    nightly redraft of each would be a Sonnet call that returns the same
    silence. A blank STORY after a draft is different — every dish owes one —
    so that is retried. `fill(force=True)` and the sweep job keep their own,
    wider, meaning of blank."""
    cols = ", ".join(f"{f}, {f}_source" for f in FIELDS)
    row = conn.execute(f"SELECT {cols}, editorial_enriched_at, story FROM dishes WHERE name = ?",
                       (name,)).fetchone()
    if row is None:
        return []
    drafted_before = bool(row[2 * len(FIELDS)])
    story_src = row[1]
    if drafted_before and story_src != SOURCE_CURATOR and not _blank(row[-1]):
        return []
    if drafted_before and story_src == SOURCE_CURATOR:
        return []
    out = []
    for i, f in enumerate(FIELDS):
        v, src = row[2 * i], row[2 * i + 1]
        if f == "cook_notes" and isinstance(v, str) and v.strip():
            try:
                v = json.loads(v)
            except Exception:
                pass
        if src != SOURCE_CURATOR and _blank(v):
            out.append(f)
    return out


def fill_where_blank(conn: sqlite3.Connection, *, log=print) -> dict:
    """Fill every dish that has a cohort AND something blank. The automatic
    half of the editorial: the refresh fills its own dish inline, the save path
    queues one dish when a recipe joins a cohort, and the rematch sweep (where
    a NEW dish gains its first rows, and where rows move between dishes) calls
    this at its end. Dishes with nothing blank are never read twice."""
    ph = ", ".join("?" * len(MEMBER_SOURCES))
    names = [r[0] for r in conn.execute(
        "SELECT d.name FROM dishes d WHERE EXISTS (SELECT 1 FROM master_recipes m "
        f"WHERE m.dish_effective = d.name AND m.dish_effective_source IN ({ph})) "
        "ORDER BY d.name", MEMBER_SOURCES)]
    out = {"filled": {}, "skipped": {}, "failed": {}}
    for name in names:
        if not blank_fields(conn, name):
            continue
        try:
            r = fill(conn, name, log=log)
        except Exception as e:  # noqa: BLE001
            log(f"[DISH-STORY] {name}: FAILED {type(e).__name__}: {e}")
            out["failed"][name] = f"{type(e).__name__}: {e}"
            continue
        if r["filled"]:
            out["filled"][name] = r["filled"]
        else:
            out["skipped"][name] = r["skipped"] or "model left the fields empty"
    return out


def fill(conn: sqlite3.Connection, name: str, *, force: bool = False,
         log=print) -> dict:
    """Fill the dish's blank editorial fields from one grounded call.

    force=True also replaces fields this module wrote earlier ('enrich'),
    never a curator's. Returns {"filled": [...], "kept": [...], "skipped": str|None,
    "cohort_n": int|None}. Never raises for a missing cohort — a dish with no
    recipes has nothing to ground a story in, and says so."""
    from input.pipeline import dishes as dishes_lib
    d = dishes_lib.get_dish(conn, name)
    if d is None:
        raise LookupError(f"Dish {name!r} not found")
    name = d["name"]
    targets, kept = [], []
    for f in FIELDS:
        src = d.get(f + "_source")
        if src == SOURCE_CURATOR:
            kept.append(f)
        elif force or _blank(d.get(f)):
            targets.append(f)
        else:
            kept.append(f)
    if not targets:
        return {"filled": [], "kept": kept, "skipped": "nothing blank", "cohort_n": None}
    members, need = member_count(conn, name), _min_members()
    if members < need:
        log(f"[DISH-STORY] {name}: skipped - {members} member recipe(s), {need} needed "
            f"(nearest guesses do not count)")
        return {"filled": [], "kept": kept, "cohort_n": None,
                "skipped": f"{members} member recipe(s); {need} needed before a story is written"}
    cohort_n = _ensure_signals(conn, name)
    if not cohort_n:
        return {"filled": [], "kept": kept, "skipped": "no cohort to ground the story in",
                "cohort_n": cohort_n}
    out = draft(conn, name)
    sets, params, filled = [], [], []
    for f in targets:
        v = out.get(f)
        if _blank(v):
            continue                      # the model left it empty: stays blank, no stamp
        sets.append(f"{f} = ?")
        params.append(json.dumps(v, ensure_ascii=False) if f == "cook_notes" else v)
        sets.append(f"{f}_source = ?")
        params.append(SOURCE_ENRICH)
        filled.append(f)
    if filled:
        sets.append("editorial_enriched_at = ?")
        params.append(_now())
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(name)
        conn.execute(f"UPDATE dishes SET {', '.join(sets)} WHERE name = ?", params)
        conn.commit()
    log(f"[DISH-STORY] {name}: filled {filled or 'nothing'}"
        + (f"; kept {kept}" if kept else "") + f" (cohort={cohort_n})")
    return {"filled": filled, "kept": kept, "skipped": None, "cohort_n": cohort_n,
            "draft": out}
