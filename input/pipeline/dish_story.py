"""dish_story.py — dish-level editorial (story, ethnicity, origin region, cook's
notes), the domains deep-enrich pattern applied to a dish.

ONE grounded Sonnet call per dish: the measured cohort signals (ingredient /
equipment terms with lift) plus the method text of the dish's strongest
winners, returned as four fields. Until 2026-10-16 this lived only behind the
editor's ✨ button and filled the FORM for the curator to save, so 10 of 461
dishes had a story while 226 of 508 domains did. Now the same call also FILLS
BLANKS on its own — after a dish refresh (the cohort has just been rebuilt),
on a dish save that leaves the fields empty, and as a one-off sweep — under
one rule:

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


_LINES = {"type": "string", "description": "One item per line. No bullets, no numbering, no tags."}
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
            # Flat strings, not arrays: with array parameters the model wrapped
            # the list in <item>/<parameter> text (Soutzoukakia, 2026-10-16).
            "tips": _LINES,
            "techniques": _LINES,
            "landmines": _LINES,
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
- tips: 3-5 lines, each short, concrete, grounded in the methods
- techniques: 2-4 lines, each "Technique name: one sentence on why the dish turns on it"
- landmines: 2-4 lines, each a way this dish actually goes wrong — prefer points
  where the cohort's recipes disagree or the methods warn
One item per line in tips / techniques / landmines; no bullets, numbering or tags."""


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
    return {
        "story": (out.get("story") or "").strip(),
        "ethnicity": (out.get("ethnicity") or "").strip(),
        "origin_region": (out.get("origin_region") or "").strip(),
        "cook_notes": {k: _as_list(out.get(k)) for k in ("tips", "techniques", "landmines")},
    }


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
    has none yet but does have a cohort (no LLM, one corpus scan)."""
    row = conn.execute("SELECT cohort_signals FROM dishes WHERE name = ?", (name,)).fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0]).get("cohort_n")
        except Exception:
            return None
    n = conn.execute("SELECT COUNT(*) FROM master_recipes WHERE dish_effective = ?",
                     (name,)).fetchone()[0]
    if not n:
        return 0
    from input.pipeline import dish_signals
    sig = dish_signals.stamp_signals(conn, name)
    return sig.get("cohort_n")


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
