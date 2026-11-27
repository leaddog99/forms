"""A dish's description, written from evidence instead of from the name.

The description is one sentence saying what the dish IS. It is not cosmetic:
the dish's identity card (cuisine, main ingredients, technique) is generated
from it, and that card is what recipes are matched against.

Until 2026-11-27 it was one small-model call given only the dish NAME. For a
dish the model did not know, that produced a confident invention: "Sauce Jean
Vignard" (a white-wine, mustard and cream sauce) was described as a French
brown sauce with red wine, shallots and mushrooms. A web search for the same
name described it correctly, because the search engine's summary READ pages.

So this module does the same, in this order:

  1. SEARCH   the dish's first search line, one results page (titles + snippets).
  2. DESCRIBE the dish from that evidence (extract.dish_signal.compose_dish_description).

and, after a refresh has gathered the dish's recipes:

  3. CHECK    the stored description against those recipes; a material
              disagreement is filed as a SUGGESTION for the curator. Nothing is
              overwritten: the description may be the curator's own words, and
              a description rewritten from the recipes can drift toward
              whatever is filed there.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

SEARCH_SETTING = "dish_description_search"
CHECK_SETTING = "dish_description_check"
SEARCH_RESULTS = 10


def _setting(key: str, default: bool = True) -> bool:
    try:
        from input.pipeline.system_config import get_setting
        return bool(get_setting(key, default))
    except Exception:
        return default


def _label(dish_row: dict) -> str:
    return (dish_row.get("display_name") or "").strip() or (dish_row.get("name") or "").strip()


def _alternates(dish_row: dict) -> list:
    """Other names + search phrases, minus anything spelled like a typo'd key."""
    key, label = (dish_row.get("name") or "").strip(), _label(dish_row)
    out = [str(a) for a in (dish_row.get("aliases") or [])]
    out += [str(q) for q in (dish_row.get("queries") or [])]
    if key and label and key.lower() != label.lower():
        out = [a for a in out if key.lower() not in a.lower()]
    return out


def search_evidence(dish_row: dict, *, search=None, log=print) -> list:
    """[{title, snippet, host}] for the dish's first search line. One results
    page. Empty on any failure, or when the setting is off: the caller then
    describes from the name alone."""
    if not _setting(SEARCH_SETTING, True):
        return []
    rows = dish_row.get("query_rows") or []
    first = next((r for r in rows if isinstance(r, dict) and (r.get("q") or "").strip()), None)
    q = (first or {}).get("q") or next(
        (str(x) for x in (dish_row.get("queries") or []) if str(x).strip()), "") or _label(dish_row)
    if not q:
        return []
    try:
        if search is None:
            from input.pipeline.serp_search import serp_search as search
        hits = search(q, pages=1, want=SEARCH_RESULTS,
                      gl=(first or {}).get("gl"), hl=(first or {}).get("hl"))
    except Exception as e:
        log(f"[DISH-DESCRIBE] search failed ({type(e).__name__}: {e}); describing from the name")
        return []
    out = []
    for h in hits or []:
        title = " ".join(str(h.get("title") or "").split())
        snippet = " ".join(str(h.get("snippet") or "").split())
        if not (title or snippet):
            continue
        host = urlsplit(str(h.get("link") or "")).netloc.lower().removeprefix("www.")
        out.append({"title": title[:140], "snippet": snippet[:320], "host": host})
        if len(out) >= SEARCH_RESULTS:
            break
    log(f"[DISH-DESCRIBE] {_label(dish_row)!r}: {len(out)} search result(s) for {q!r}")
    return out


def recipes_evidence(conn: sqlite3.Connection, dish_name: str, limit: int = 12) -> list:
    """[{name, cuisine, ingredients, method}] from the recipes filed under the dish."""
    def _t(x):
        if isinstance(x, str):
            return x
        if isinstance(x, dict):
            return str(x.get("text") or x.get("name") or "")
        return ""
    out = []
    for (data,) in conn.execute(
            "SELECT data FROM master_recipes WHERE json_extract(data, '$._master.dish') = ? "
            "ORDER BY id LIMIT ?", (dish_name, limit)):
        try:
            d = json.loads(data)
        except Exception:
            continue
        out.append({
            "name": d.get("name"),
            "cuisine": d.get("recipeCuisine") or (d.get("_identity") or {}).get("cuisine") or "",
            "ingredients": [_t(i) for i in (d.get("recipeIngredient") or [])],
            "method": " ".join(_t(s) for s in (d.get("recipeInstructions") or [])[:3]),
        })
    return out


def describe_new(dish_row: dict, *, search=None, create=None, log=print) -> str:
    """Search first, then describe. For a dish with no description yet (creation,
    or a description the curator cleared). '' on failure: the caller falls back
    to the name-only call."""
    from extract.dish_signal import compose_dish_description
    results = search_evidence(dish_row, search=search, log=log)
    out = compose_dish_description(
        _label(dish_row), alternates=_alternates(dish_row), search_results=results,
        notes=str(dish_row.get("notes") or ""), create=create)
    return out.get("description") or ""


# --- the after-refresh check -------------------------------------------------

def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dish_description_suggestions (
            dish_name   TEXT PRIMARY KEY COLLATE NOCASE,
            basis       TEXT NOT NULL,     -- the description that was judged
            suggestion  TEXT NOT NULL,
            why         TEXT,
            recipes     INTEGER,           -- how many of the dish's recipes were read
            method      TEXT,
            model       TEXT,
            status      TEXT NOT NULL DEFAULT 'open',   -- open | dismissed
            created_at  TEXT NOT NULL
        )""")


def get_suggestion(conn: sqlite3.Connection, dish_row: dict) -> Optional[dict]:
    """The open suggestion for this dish, only while it still refers to the
    description the dish has now."""
    try:
        ensure_table(conn)
        r = conn.execute(
            "SELECT basis, suggestion, why, recipes, created_at FROM dish_description_suggestions "
            "WHERE dish_name = ? AND status = 'open'", (dish_row.get("name"),)).fetchone()
    except Exception:
        return None
    if not r or (r[0] or "").strip() != (dish_row.get("description") or "").strip():
        return None
    return {"suggestion": r[1], "why": r[2], "recipes": r[3], "created_at": r[4]}


def dismiss(conn: sqlite3.Connection, dish_name: str) -> bool:
    ensure_table(conn)
    cur = conn.execute("UPDATE dish_description_suggestions SET status = 'dismissed' "
                       "WHERE dish_name = ?", (dish_name,))
    conn.commit()
    return cur.rowcount > 0


def check_after_refresh(conn: sqlite3.Connection, dish_name: str, *, create=None,
                        log=print) -> Optional[dict]:
    """Judge the dish's stored description against the recipes it now holds.
    Files a suggestion when they materially disagree; clears a stale one when
    they agree. One model call; skipped when this exact description was already
    judged (open or dismissed), when there are too few recipes, or when off."""
    if not _setting(CHECK_SETTING, True):
        return None
    from input.pipeline import dishes as dishes_lib
    from extract.dish_signal import judge_dish_description
    dish = dishes_lib.get_dish(conn, dish_name)
    if not dish or not (dish.get("description") or "").strip():
        return None
    ensure_table(conn)
    basis = dish["description"].strip()
    prior = conn.execute("SELECT basis FROM dish_description_suggestions WHERE dish_name = ?",
                         (dish["name"],)).fetchone()
    if prior and (prior[0] or "").strip() == basis:
        return None                      # this wording was already judged
    recipes = [r for r in recipes_evidence(conn, dish["name"]) if r.get("ingredients")]
    if len(recipes) < 2:
        return None
    v = judge_dish_description(_label(dish), description=basis, recipes=recipes,
                               alternates=_alternates(dish), notes=str(dish.get("notes") or ""),
                               create=create)
    if not v:
        return None
    if v["agrees"] or not v.get("description"):
        conn.execute("DELETE FROM dish_description_suggestions WHERE dish_name = ?", (dish["name"],))
        conn.commit()
        log(f"[DISH-DESCRIBE] {dish['name']!r}: description agrees with its {v['recipes_used']} recipes")
        return None
    conn.execute(
        "INSERT OR REPLACE INTO dish_description_suggestions "
        "(dish_name, basis, suggestion, why, recipes, method, model, status, created_at) "
        "VALUES (?,?,?,?,?,?,?, 'open', ?)",
        (dish["name"], basis, v["description"], v.get("why") or "", v["recipes_used"],
         v["method"], v["model"], datetime.now(timezone.utc).isoformat()))
    conn.commit()
    log(f"[DISH-DESCRIBE] {dish['name']!r}: description DISAGREES with its recipes "
        f"({v.get('why')}); suggestion filed for the curator")
    return v
