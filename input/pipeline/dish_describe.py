"""A dish's description, written from evidence instead of from the name.

The description is one sentence saying what the dish IS. It is not cosmetic:
the dish's identity card (cuisine, main ingredients, technique) is generated
from it, and that card is what recipes are matched against.

Until 2026-11-27 it was one small-model call given only the dish NAME. For a
dish the model did not know, that produced a confident invention: "Sauce Jean
Vignard" (a white-wine, mustard and cream sauce) was described as a French
brown sauce with red wine, shallots and mushrooms. A web search for the same
name described it correctly, because the search engine's summary READ pages.

This module does the same. `describe` is the one way a description is written:

  1. SEARCH   the dish's first search line, one results page (titles + snippets).
  2. DESCRIBE the dish from that evidence, plus any recipes filed under it and
              the curator's notes (extract.dish_signal.compose_dish_description).

It runs when a dish has no description (creation, or one the curator cleared)
and when the curator presses Retry on the dish editor.

After a refresh has gathered the dish's recipes:

  3. CHECK    the stored description against those recipes. A material
              disagreement is filed as a SUGGESTION for the curator. Nothing is
              overwritten: the description may be the curator's own words, and
              one rewritten from the recipes alone can drift toward whatever is
              filed there.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from extract.dish_signal import (
    MAX_SEARCH_RESULTS, compose_dish_description, judge_dish_description,
)

SEARCH_SETTING = "dish_description_search"
CHECK_SETTING = "dish_description_check"
MIN_RECIPES_TO_CHECK = 2


def _setting(key: str) -> bool:
    try:
        from input.pipeline.system_config import get_setting
        return bool(get_setting(key, True))
    except Exception:
        return True


def label(dish_row: dict) -> str:
    """What the curator calls the dish. `name` is the immutable key and can carry
    a typo the display name has since corrected ("Chicken Marenga")."""
    return (dish_row.get("display_name") or "").strip() or (dish_row.get("name") or "").strip()


def alternates(dish_row: dict) -> list:
    """Other names + search phrases, minus anything spelled like a typo'd key."""
    key, shown = (dish_row.get("name") or "").strip(), label(dish_row)
    out = [str(a) for a in (dish_row.get("aliases") or [])]
    out += [str(q) for q in (dish_row.get("queries") or [])]
    if key and key.lower() != shown.lower():
        out = [a for a in out if key.lower() not in a.lower()]
    return out


def search_evidence(dish_row: dict, *, search=None, log=print) -> list:
    """[{title, snippet, host}] for the dish's first search line, one results
    page. Empty when the setting is off or the search fails: the description is
    then written without it."""
    if not _setting(SEARCH_SETTING):
        return []
    first = next((r for r in (dish_row.get("query_rows") or [])
                  if isinstance(r, dict) and (r.get("q") or "").strip()), {})
    query = first.get("q") or next(
        (str(q) for q in (dish_row.get("queries") or []) if str(q).strip()), "") or label(dish_row)
    if not query:
        return []
    try:
        if search is None:
            from input.pipeline.serp_search import serp_search as search
        hits = search(query, pages=1, want=MAX_SEARCH_RESULTS,
                      gl=first.get("gl"), hl=first.get("hl"))
    except Exception as e:
        log(f"[DISH-DESCRIBE] search failed ({type(e).__name__}: {e}); describing without it")
        return []
    out = []
    for h in hits or []:
        title = " ".join(str(h.get("title") or "").split())
        snippet = " ".join(str(h.get("snippet") or "").split())
        if title or snippet:
            host = urlsplit(str(h.get("link") or "")).netloc.lower().removeprefix("www.")
            out.append({"title": title[:140], "snippet": snippet[:320], "host": host})
    out = out[:MAX_SEARCH_RESULTS]
    log(f"[DISH-DESCRIBE] {label(dish_row)!r}: {len(out)} search result(s) for {query!r}")
    return out


def recipes_evidence(conn: sqlite3.Connection, dish_name: str, limit: int = 12) -> list:
    """[{name, cuisine, ingredients, method}] from the recipes filed under the dish."""
    def text(x) -> str:
        if isinstance(x, dict):
            return str(x.get("text") or x.get("name") or "")
        return x if isinstance(x, str) else ""
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
            "ingredients": [text(i) for i in (d.get("recipeIngredient") or [])],
            "method": " ".join(text(s) for s in (d.get("recipeInstructions") or [])[:3]),
        })
    return out


def describe(dish_row: dict, *, recipes: Optional[list] = None, notes: Optional[str] = None,
             previous: Optional[str] = None, search=None, create=None, log=print) -> dict:
    """Search first, then describe. `notes` / `previous` default to what is
    stored on the dish; the editor passes what is in its boxes instead. Returns
    compose_dish_description's dict ({description: ''} when the call failed)."""
    return compose_dish_description(
        label(dish_row), alternates=alternates(dish_row), recipes=recipes,
        search_results=search_evidence(dish_row, search=search, log=log),
        notes=(dish_row.get("notes") or "") if notes is None else notes,
        previous=(dish_row.get("description") or "") if previous is None else previous,
        create=create)


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


def _stored(conn: sqlite3.Connection, dish_name: str) -> Optional[tuple]:
    """(basis, suggestion, why, recipes, created_at, status), or None. A missing
    table reads as no row, so a page load never has to create it."""
    try:
        return conn.execute(
            "SELECT basis, suggestion, why, recipes, created_at, status "
            "FROM dish_description_suggestions WHERE dish_name = ?", (dish_name,)).fetchone()
    except sqlite3.OperationalError:
        return None


def get_suggestion(conn: sqlite3.Connection, dish_row: dict) -> Optional[dict]:
    """The open suggestion for this dish, only while it still refers to the
    description the dish has now."""
    row = _stored(conn, dish_row.get("name") or "")
    if (not row or row[5] != "open"
            or (row[0] or "").strip() != (dish_row.get("description") or "").strip()):
        return None
    return {"suggestion": row[1], "why": row[2], "recipes": row[3], "created_at": row[4]}


def dismiss(conn: sqlite3.Connection, dish_name: str) -> bool:
    """The curator kept their wording. It is not judged again until it changes."""
    ensure_table(conn)
    cur = conn.execute("UPDATE dish_description_suggestions SET status = 'dismissed' "
                       "WHERE dish_name = ?", (dish_name,))
    conn.commit()
    return cur.rowcount > 0


def check_after_refresh(conn: sqlite3.Connection, dish_name: str, *, create=None,
                        log=print) -> Optional[dict]:
    """Judge the dish's stored description against the recipes it now holds.
    A material disagreement is filed (or refreshed) as an open suggestion; an
    agreement clears one. One model call per refresh. Skipped when the setting
    is off, the dish has too few recipes, or the curator already dismissed a
    suggestion for this exact wording. Returns the verdict when one was made."""
    if not _setting(CHECK_SETTING):
        return None
    from input.pipeline import dishes as dishes_lib
    dish = dishes_lib.get_dish(conn, dish_name)
    basis = ((dish or {}).get("description") or "").strip()
    if not basis:
        return None
    prior = _stored(conn, dish["name"])
    if prior and prior[5] == "dismissed" and (prior[0] or "").strip() == basis:
        return None
    recipes = [r for r in recipes_evidence(conn, dish["name"]) if r.get("ingredients")]
    if len(recipes) < MIN_RECIPES_TO_CHECK:
        return None
    verdict = judge_dish_description(label(dish), description=basis, recipes=recipes,
                                     alternates=alternates(dish),
                                     notes=str(dish.get("notes") or ""), create=create)
    if not verdict:
        return None
    ensure_table(conn)
    if verdict["agrees"] or not verdict["description"]:
        conn.execute("DELETE FROM dish_description_suggestions WHERE dish_name = ?", (dish["name"],))
        log(f"[DISH-DESCRIBE] {dish['name']!r}: description agrees with its "
            f"{verdict['recipes_used']} recipes")
    else:
        conn.execute(
            "INSERT OR REPLACE INTO dish_description_suggestions "
            "(dish_name, basis, suggestion, why, recipes, method, model, status, created_at) "
            "VALUES (?,?,?,?,?,?,?, 'open', ?)",
            (dish["name"], basis, verdict["description"], verdict["why"],
             verdict["recipes_used"], verdict["method"], verdict["model"],
             datetime.now(timezone.utc).isoformat()))
        log(f"[DISH-DESCRIBE] {dish['name']!r}: description DISAGREES with its recipes "
            f"({verdict['why']}); suggestion filed for the curator")
    conn.commit()
    return verdict
