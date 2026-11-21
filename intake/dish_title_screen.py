"""dish_title_screen.py - is this search result even the dish? Asked BEFORE any fetch.

A dish refresh trusted the search results until the very end. The only wrong-dish
check (dish_match.off_dish) runs at seating, after the page has been fetched,
scored by Moz and extracted by the model, and it only rejects a page that is both
FAR from the dish and never names it - so a near twin sails through. Veal
Cacciatore #3064 (2026-11-21) seated 12 of 15 winners whose titles plainly named
another dish (ten chicken cacciatore, a venison one, vitello tonnato); Butter
Chicken #3070 fetched, scored and extracted eight Sally's Baking pages (bagels,
cinnamon rolls) before the seating guard threw them out; Veal Parmigiana #3063
seated three veal ROASTS with parmesan. The search provider was serving a page of
chicken cacciatore for the veal query while Google itself, in a browser, served
veal.

One model call per refresh reads every surviving result's TITLE and URL and
answers same / different / unclear. Only `different` is dropped - a title that
itself rules the dish out, or is not a recipe for food (dog food, a shoe, a list
of many recipes). `unclear` stays: when the title does not say, the page gets its
fetch. Editor's Choice pins are never screened.

Measured before it was allowed to drop anything, on the six runs of 2026-11-21
(363 candidates): every one of the wrong winners above was flagged, no page that
used the dish's own name was, and Revithia - a clean run - lost no winner.

THE DISH NAME IS THE AUTHORITY. The dish's stored description is passed as a
note and the model is told it can be wrong: Licurdia's read "confection made with
licorice root" that day (written from a mistranslated search line), and with the
name leading the screen still kept all seven real licurdia pages.

The larger model on purpose. The small one, given the same list, called nearly
every real licurdia page "different: onion soup" - it did not know the dish. This
is one call of a few thousand tokens per refresh, against a fetch, a Moz row and
an extraction for every page it stops.

Setting `dish_title_screen` (System -> Search): off | report | drop.
  report  runs the screen and prints what it would drop; drops nothing.
  drop    drops the `different` rows (reason `off-dish-title: <what it is>`,
          an overturnable inference in the candidate ledger).
Any failure - no key, a reply that does not parse - leaves every candidate in.
"""
from __future__ import annotations

import json
import re
from typing import Optional

MODEL = "claude-sonnet-4-6"
SETTING = "dish_title_screen"
MODES = ("off", "report", "drop")
DEFAULT_MODE = "drop"
REASON = "off-dish-title"

SYSTEM = (
    "You screen web search results for ONE dish before a recipe site spends money fetching them. "
    "For each numbered result (page title + URL) decide whether the page is a recipe for THAT dish.\n"
    "  same      - the dish itself: any spelling, translation, regional name or household variant of it "
    "(a slow-cooker, quick, or 'best' version is still the dish).\n"
    "  different - the title plainly names ANOTHER dish (a different main ingredient or a different "
    "preparation: chicken cacciatore is not veal cacciatore, veal roast is not veal parmigiana, "
    "vitello tonnato is not veal cacciatore), or it is not a recipe for food at all (a product, a shop, "
    "pet food, a restaurant, a list of many recipes).\n"
    "  unclear   - the title does not say enough to tell (a bare site name, a generic or truncated title, "
    "a name that could be this dish).\n"
    "The DISH NAME is the authority. The WHAT IT IS line is a machine-written note and can be wrong; "
    "if it contradicts what you know the named dish to be, go by the name. A title that uses the dish's "
    "own name is the dish, whatever else it adds. "
    "Be strict about 'different': use it only when the title itself rules the dish out. When in doubt, "
    "unclear. Answer with ONLY a JSON array, one object per result in order: "
    '{"n": <number>, "v": "same"|"different"|"unclear", "is": "<what it is, 2-5 words, only when different>"}'
)


def mode() -> str:
    try:
        from input.pipeline.system_config import get_setting
        m = str(get_setting(SETTING, DEFAULT_MODE) or "").strip().lower()
        return m if m in MODES else DEFAULT_MODE
    except Exception:
        return DEFAULT_MODE


def _about(dish_name: str) -> str:
    """The dish's own note for the judge: stored description + aliases. '' when
    the name is not a catalog dish (a single-query batch)."""
    try:
        from input.pipeline.db import connect
        from input.pipeline import acquisition
        conn = connect(acquisition.DB_PATH)
        try:
            row = conn.execute("SELECT description, display_name, aliases FROM dishes WHERE name = ?",
                               (dish_name,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return ""
    if not row:
        return ""
    desc, display, aliases = row
    parts = [str(desc or "").strip()[:300]]
    names = [str(display or "").strip()]
    try:
        al = json.loads(aliases) if aliases else []
        names += [a for a in (al if isinstance(al, list) else [str(aliases)]) if isinstance(a, str)]
    except Exception:
        names.append(str(aliases or ""))
    names = [n for n in dict.fromkeys(n.strip() for n in names) if n and n.lower() != dish_name.lower()]
    if names:
        parts.append("also called: " + ", ".join(names)[:200])
    return " | ".join(p for p in parts if p)


def judge(dish_name: str, rows: list[tuple[str, str]], *, about: Optional[str] = None,
          _create=None) -> dict[int, dict]:
    """rows = [(title, url)] -> {1-based index: {"v": same|different|unclear, "is": str}}.
    Raises on an API error or a reply that is not the asked-for JSON."""
    if not rows:
        return {}
    about = _about(dish_name) if about is None else about
    lines = [f"{i}. {(t or '(no title)').strip()[:160]} | {u}" for i, (t, u) in enumerate(rows, 1)]
    user = (f"DISH: {dish_name}\n" + (f"WHAT IT IS: {about}\n" if about else "")
            + "\nRESULTS:\n" + "\n".join(lines))
    if _create is None:
        import llm
        _create = llm.create
    msg = _create(operation="dish_title_screen", model=MODEL,
                  max_tokens=min(16000, 400 + 45 * len(rows)),
                  system=SYSTEM, messages=[{"role": "user", "content": user}])
    txt = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    m = re.search(r"\[.*\]", txt, re.S)
    if not m:
        raise ValueError(f"dish_title_screen: no JSON array in the reply ({txt[:80]!r})")
    out: dict[int, dict] = {}
    for d in json.loads(m.group(0)):
        if isinstance(d, dict) and isinstance(d.get("n"), int) and d.get("v") in ("same", "different", "unclear"):
            out[d["n"]] = {"v": d["v"], "is": str(d.get("is") or "").strip()[:60]}
    return out


def screen(dish_name: str, entries: list[dict], *, _create=None) -> tuple[list[dict], list[dict], dict]:
    """-> (kept, dropped, summary). Pins are never screened. In `report` mode
    nothing is dropped; the entries a `drop` run would have lost are still
    stamped `_title_verdict` and listed in summary["would_drop"]."""
    m = mode()
    summary = {"mode": m, "judged": 0, "same": 0, "different": 0, "unclear": 0, "would_drop": []}
    if m == "off" or not entries:
        return entries, [], summary
    todo = [e for e in entries if not e.get("_pinned")]
    verdicts = judge(dish_name, [(e.get("title") or "", e.get("url") or "") for e in todo], _create=_create)
    kept, dropped = [], []
    drop_ids = set()
    for i, e in enumerate(todo, 1):
        v = verdicts.get(i)
        if not v:
            continue                          # the model skipped it: it stays
        summary["judged"] += 1
        summary[v["v"]] += 1
        e["_title_verdict"] = v["v"]
        if v["v"] == "different":
            what = v["is"] or "another dish"
            summary["would_drop"].append((e.get("title") or e.get("url") or "", what))
            if m == "drop":
                e["_dropped_reason"] = f"{REASON}: {what}"
                drop_ids.add(id(e))
    for e in entries:
        (dropped if id(e) in drop_ids else kept).append(e)
    return kept, dropped, summary
