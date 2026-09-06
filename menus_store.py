"""Menus — dated, per-user groups of the user's own recipes, with a shopping list.

Option A of docs/meal-planning-research.md (curator go-ahead 2026-09-05): the
"flag recipes for tomorrow's dinner party" object. A menu is a small personal
junction (menu_recipes) over the user's saved recipes — the possess side, never
the corpus — plus a persisted, checkable shopping list.

The shopping list build:
  1. PARSE each recipe's raw ingredient lines into {qty, unit, item, canonical,
     category} — one LLM call for all UNCACHED recipes, cached per recipe by a
     hash of its lines (recipe_ingredient_parse), so rebuilding a menu is free
     when nothing changed and every future menu reuses the parse. This cache is
     the down-payment on Option B (structured ingredients at the source).
  2. SCALE by the per-recipe multiplier (party cooking is 1.5x cooking).
  3. MERGE conservatively (the Plan-to-Eat rule): same canonical item merges
     within the volume family or the weight family or on an EXACT unit match —
     "pinch" never merges into "teaspoons". Wrong totals poison trust.
  4. PERSIST as menu_shopping_items, preserving check-off state ("checked" =
     in the cart, "have" = already in the pantry) across rebuilds by canonical
     name, and never touching hand-added (manual) rows.

Aisle categories come from the parse against a FIXED vocabulary (produce, meat
& seafood, dairy & eggs, bakery, pantry, spices, frozen, beverages, other) —
the walk order through the store. ingredient_synonyms.category is ingredient
CLASS taxonomy (allium, legume), not aisles, so it is not used here.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

CATEGORIES = ["produce", "meat & seafood", "dairy & eggs", "bakery", "pantry",
              "spices", "frozen", "beverages", "other"]
CATEGORY_ORDER = {c: i for i, c in enumerate(CATEGORIES)}

# Unit families for the conservative merge. Everything converts to the family
# BASE for summing (tsp / gram); display re-simplifies. A unit in neither
# family merges only with its exact self.
_VOLUME_TSP = {"tsp": 1.0, "teaspoon": 1.0, "tbsp": 3.0, "tablespoon": 3.0,
               "cup": 48.0, "pint": 96.0, "quart": 192.0, "gallon": 768.0,
               "fl oz": 6.0, "ml": 0.202884, "l": 202.884}
_WEIGHT_G = {"g": 1.0, "gram": 1.0, "kg": 1000.0, "oz": 28.3495, "ounce": 28.3495,
             "lb": 453.592, "pound": 453.592}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS menus (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT NOT NULL,
            menu_date   TEXT,               -- YYYY-MM-DD (the party day)
            serve_at    TEXT,               -- HH:MM — carried from day one so the
                                            -- prep-timeline layer (Option C) can
                                            -- attach without a migration
            notes       TEXT,
            created_at  TEXT,
            updated_at  TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS menu_recipes (
            menu_id     INTEGER NOT NULL,
            recipe_id   TEXT NOT NULL,
            multiplier  REAL DEFAULT 1,     -- servings scaling, applied pre-merge
            position    INTEGER DEFAULT 0,
            added_at    TEXT,
            PRIMARY KEY (menu_id, recipe_id)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS menu_shopping_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id     INTEGER NOT NULL,
            canonical   TEXT,               -- merge/carry-over key (lowercase)
            display     TEXT,               -- "1 3/4 cups long-grain white rice"
            category    TEXT,
            have        INTEGER DEFAULT 0,  -- already in the pantry (pre-check)
            checked     INTEGER DEFAULT 0,  -- in the cart
            manual      INTEGER DEFAULT 0,  -- hand-added; rebuilds never touch it
            sources     TEXT,               -- JSON [recipe_id, ...]
            position    INTEGER DEFAULT 0,
            created_at  TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recipe_ingredient_parse (
            recipe_id   TEXT NOT NULL,
            user_id     INTEGER NOT NULL,
            lines_hash  TEXT,               -- sha1 of the raw lines; stale = re-parse
            parsed      TEXT,               -- JSON [{qty, unit, item, canonical, category}]
            model       TEXT,
            created_at  TEXT,
            PRIMARY KEY (recipe_id, user_id)
        )""")
    conn.commit()


# --------------------------------------------------------------------------- #
#  Menus + membership
# --------------------------------------------------------------------------- #

def list_menus(conn: sqlite3.Connection, user_id: int) -> list:
    ensure_tables(conn)
    rows = conn.execute(
        "SELECT m.id, m.name, m.menu_date, m.serve_at, m.notes, m.updated_at, "
        " (SELECT COUNT(*) FROM menu_recipes r WHERE r.menu_id = m.id) AS recipe_count, "
        " (SELECT COUNT(*) FROM menu_shopping_items s WHERE s.menu_id = m.id) AS item_count "
        "FROM menus m WHERE m.user_id = ? "
        "ORDER BY COALESCE(m.menu_date, '9999') , m.name", (user_id,)).fetchall()
    cols = ["id", "name", "menu_date", "serve_at", "notes", "updated_at",
            "recipe_count", "item_count"]
    return [dict(zip(cols, r)) for r in rows]


def get_menu(conn: sqlite3.Connection, user_id: int, menu_id: int) -> dict | None:
    ensure_tables(conn)
    row = conn.execute("SELECT id, name, menu_date, serve_at, notes, created_at, "
                       "updated_at FROM menus WHERE id = ? AND user_id = ?",
                       (menu_id, user_id)).fetchone()
    if not row:
        return None
    m = dict(zip(["id", "name", "menu_date", "serve_at", "notes", "created_at",
                  "updated_at"], row))
    m["recipes"] = _menu_recipes(conn, user_id, menu_id)
    m["shopping"] = list_items(conn, menu_id)
    return m


def _menu_recipes(conn: sqlite3.Connection, user_id: int, menu_id: int) -> list:
    """Membership joined to the user's recipe rows for display facts. Reads the
    intrinsic content only — title, image, yield, ingredient count."""
    out = []
    rows = conn.execute(
        "SELECT mr.recipe_id, mr.multiplier, mr.position, r.data "
        "FROM menu_recipes mr LEFT JOIN recipes r "
        "  ON r.recipe_id = mr.recipe_id AND r.user_id = ? "
        "WHERE mr.menu_id = ? ORDER BY mr.position, mr.added_at", (user_id, menu_id))
    for rid, mult, pos, data in rows:
        d = {}
        try:
            d = json.loads(data) if data else {}
        except Exception:
            pass
        img = d.get("image")
        if isinstance(img, list):
            img = img[0] if img else ""
        if isinstance(img, dict):
            img = img.get("url") or ""
        y = d.get("recipeYield")
        if isinstance(y, list):
            y = ", ".join(str(x) for x in y[:2])
        out.append({"recipe_id": rid, "multiplier": mult, "position": pos,
                    "title": d.get("name") or "(missing recipe)",
                    "image": img or "", "recipe_yield": y or "",
                    "ingredient_count": len(d.get("recipeIngredient") or []),
                    "missing": not d})
    return out


def create_menu(conn: sqlite3.Connection, user_id: int, patch: dict) -> dict:
    ensure_tables(conn)
    name = (patch.get("name") or "").strip()
    if not name:
        raise ValueError("a menu needs a name")
    now = _now()
    cur = conn.execute(
        "INSERT INTO menus(user_id, name, menu_date, serve_at, notes, created_at, "
        "updated_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, name, (patch.get("menu_date") or "").strip(),
         (patch.get("serve_at") or "").strip(), (patch.get("notes") or "").strip(),
         now, now))
    conn.commit()
    return get_menu(conn, user_id, cur.lastrowid)


def update_menu(conn: sqlite3.Connection, user_id: int, menu_id: int,
                patch: dict) -> dict | None:
    ensure_tables(conn)
    if not get_menu(conn, user_id, menu_id):
        return None
    sets, vals = [], []
    for f in ("name", "menu_date", "serve_at", "notes"):
        if f in patch:
            v = (patch.get(f) or "").strip()
            if f == "name" and not v:
                raise ValueError("a menu needs a name")
            sets.append(f"{f} = ?")
            vals.append(v)
    if sets:
        sets.append("updated_at = ?")
        vals.extend([_now(), menu_id, user_id])
        conn.execute(f"UPDATE menus SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
                     vals)
        conn.commit()
    return get_menu(conn, user_id, menu_id)


def delete_menu(conn: sqlite3.Connection, user_id: int, menu_id: int) -> bool:
    ensure_tables(conn)
    cur = conn.execute("DELETE FROM menus WHERE id = ? AND user_id = ?",
                       (menu_id, user_id))
    conn.execute("DELETE FROM menu_recipes WHERE menu_id = ?", (menu_id,))
    conn.execute("DELETE FROM menu_shopping_items WHERE menu_id = ?", (menu_id,))
    conn.commit()
    return cur.rowcount > 0


def add_recipe(conn: sqlite3.Connection, user_id: int, menu_id: int,
               recipe_id: str) -> bool:
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM menus WHERE id = ? AND user_id = ?",
                        (menu_id, user_id)).fetchone():
        return False
    if not conn.execute("SELECT 1 FROM recipes WHERE recipe_id = ? AND user_id = ?",
                        (recipe_id, user_id)).fetchone():
        raise ValueError("recipe not found in your collection")
    pos = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM menu_recipes "
                       "WHERE menu_id = ?", (menu_id,)).fetchone()[0]
    conn.execute("INSERT OR IGNORE INTO menu_recipes(menu_id, recipe_id, multiplier, "
                 "position, added_at) VALUES(?,?,1,?,?)",
                 (menu_id, recipe_id, pos, _now()))
    conn.commit()
    return True


def remove_recipe(conn: sqlite3.Connection, user_id: int, menu_id: int,
                  recipe_id: str) -> bool:
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM menus WHERE id = ? AND user_id = ?",
                        (menu_id, user_id)).fetchone():
        return False
    cur = conn.execute("DELETE FROM menu_recipes WHERE menu_id = ? AND recipe_id = ?",
                       (menu_id, recipe_id))
    conn.commit()
    return cur.rowcount > 0


def set_multiplier(conn: sqlite3.Connection, user_id: int, menu_id: int,
                   recipe_id: str, multiplier: float) -> bool:
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM menus WHERE id = ? AND user_id = ?",
                        (menu_id, user_id)).fetchone():
        return False
    m = max(0.25, min(10.0, float(multiplier or 1)))
    cur = conn.execute("UPDATE menu_recipes SET multiplier = ? "
                       "WHERE menu_id = ? AND recipe_id = ?", (m, menu_id, recipe_id))
    conn.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
#  Ingredient parse (cached per recipe — the Option-B down-payment)
# --------------------------------------------------------------------------- #

_PARSE_MODEL = "claude-haiku-4-5"


def _lines_hash(lines: list) -> str:
    return hashlib.sha1(json.dumps(lines, sort_keys=True).encode()).hexdigest()


def _parse_prompt(batch: dict) -> str:
    cats = ", ".join(CATEGORIES)
    listing = []
    for rid, lines in batch.items():
        listing.append(f'RECIPE {rid}:')
        listing += [f"- {ln}" for ln in lines]
    return (
        "Parse these recipe ingredient lines for a grocery shopping list. For EVERY line "
        "of every recipe return one object:\n"
        '{"qty": number or null, "unit": "", "item": "", "canonical": "", "category": ""}\n'
        "- qty: the leading amount as a decimal (\"1 3/4\" -> 1.75); null when there is "
        "none (\"salt to taste\").\n"
        "- unit: normalized singular, lowercase (tsp, tbsp, cup, fl oz, ml, l, g, kg, oz, "
        "lb, clove, can, bunch, sprig, slice, piece, ...); \"\" for a bare count "
        "(\"2 eggs\" -> qty 2, unit \"\").\n"
        "- item: the ingredient as written, minus amount and prep (\"finely diced\" off).\n"
        "- canonical: the generic shopping name, lowercase singular (\"long-grain white "
        "rice\" -> \"long-grain white rice\", \"finely diced shallot\" -> \"shallot\") — "
        "the SAME ingredient must get the SAME canonical across recipes.\n"
        f"- category: exactly one of: {cats}.\n"
        "Reply as JSON only: {\"recipes\": {\"<recipe id>\": [ ...one object per line, "
        "in order... ]}}\n\n" + "\n".join(listing))


def _parse_uncached(conn: sqlite3.Connection, user_id: int, need: dict) -> None:
    """One LLM call parses every uncached recipe's lines; results cached per
    recipe keyed by a hash of the lines (stale lines re-parse)."""
    if not need:
        return
    import llm
    resp = llm.create(operation="menu_ingredient_parse", model=_PARSE_MODEL,
                      max_tokens=8000,
                      messages=[{"role": "user", "content": _parse_prompt(need)}])
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("ingredient parse returned no JSON")
    parsed = (json.loads(m.group(0)).get("recipes")) or {}
    now = _now()
    for rid, lines in need.items():
        items = parsed.get(rid)
        if not isinstance(items, list):
            print(f"[MENU] parse missing recipe {rid} — skipped")
            continue
        conn.execute(
            "INSERT OR REPLACE INTO recipe_ingredient_parse(recipe_id, user_id, "
            "lines_hash, parsed, model, created_at) VALUES(?,?,?,?,?,?)",
            (rid, user_id, _lines_hash(lines), json.dumps(items), _PARSE_MODEL, now))
    conn.commit()


def _parsed_for(conn: sqlite3.Connection, user_id: int, menu_id: int) -> dict:
    """{recipe_id: (multiplier, [parsed items])} for the menu, parsing what the
    cache lacks in one batched call."""
    rows = conn.execute(
        "SELECT mr.recipe_id, mr.multiplier, r.data, p.lines_hash, p.parsed "
        "FROM menu_recipes mr "
        "JOIN recipes r ON r.recipe_id = mr.recipe_id AND r.user_id = ? "
        "LEFT JOIN recipe_ingredient_parse p "
        "  ON p.recipe_id = mr.recipe_id AND p.user_id = ? "
        "WHERE mr.menu_id = ?", (user_id, user_id, menu_id)).fetchall()
    need, have, lines_by_rid = {}, {}, {}
    for rid, mult, data, cached_hash, cached in rows:
        try:
            lines = [str(x) for x in (json.loads(data).get("recipeIngredient") or [])]
        except Exception:
            lines = []
        lines_by_rid[rid] = (mult, lines)
        if not lines:
            continue
        if cached and cached_hash == _lines_hash(lines):
            have[rid] = json.loads(cached)
        else:
            need[rid] = lines
    _parse_uncached(conn, user_id, need)
    for rid in need:
        row = conn.execute(
            "SELECT parsed FROM recipe_ingredient_parse WHERE recipe_id = ? AND "
            "user_id = ?", (rid, user_id)).fetchone()
        if row:
            have[rid] = json.loads(row[0])
    return {rid: (lines_by_rid[rid][0], items) for rid, items in have.items()}


# --------------------------------------------------------------------------- #
#  Merge + build
# --------------------------------------------------------------------------- #

def _unit_key(unit: str) -> tuple:
    """(family, factor-to-base) — 'volume'/'weight' families merge internally;
    anything else merges only with its exact self."""
    u = (unit or "").strip().lower()
    if u in _VOLUME_TSP:
        return ("volume", _VOLUME_TSP[u])
    if u in _WEIGHT_G:
        return ("weight", _WEIGHT_G[u])
    return (f"unit:{u}", 1.0)


_FRACTIONS = [(0.125, "1/8"), (0.25, "1/4"), (0.333, "1/3"), (0.375, "3/8"),
              (0.5, "1/2"), (0.625, "5/8"), (0.666, "2/3"), (0.75, "3/4"),
              (0.875, "7/8")]


def _nice_qty(q: float) -> str:
    whole = int(q)
    frac = q - whole
    for v, s in _FRACTIONS:
        if abs(frac - v) < 0.04:
            return f"{whole} {s}" if whole else s
    if abs(frac) < 0.04:
        return str(whole)
    return f"{q:.2f}".rstrip("0").rstrip(".")


def _display_volume(tsp: float) -> str:
    if tsp >= 48:
        return f"{_nice_qty(tsp / 48)} cup(s)"
    if tsp >= 3:
        return f"{_nice_qty(tsp / 3)} tbsp"
    return f"{_nice_qty(tsp)} tsp"


def _display_weight(g: float) -> str:
    if g >= 453.592:
        return f"{_nice_qty(g / 453.592)} lb"
    if g >= 28.3495:
        return f"{_nice_qty(g / 28.3495)} oz"
    return f"{_nice_qty(g)} g"


def build_shopping_list(conn: sqlite3.Connection, user_id: int, menu_id: int) -> dict:
    """(Re)build the generated rows. Manual rows are never touched; have/checked
    carry over by canonical name."""
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM menus WHERE id = ? AND user_id = ?",
                        (menu_id, user_id)).fetchone():
        raise ValueError("menu not found")
    parsed = _parsed_for(conn, user_id, menu_id)

    merged: dict = {}   # (canonical, family) -> {qty_base, units_exact, sources, category, item}
    for rid, (mult, items) in parsed.items():
        for it in items:
            canonical = (it.get("canonical") or it.get("item") or "").strip().lower()
            if not canonical:
                continue
            family, factor = _unit_key(it.get("unit") or "")
            qty = it.get("qty")
            key = (canonical, family)
            g = merged.setdefault(key, {"qty": 0.0, "any_null": False, "sources": [],
                                        "category": (it.get("category") or "other"),
                                        "unit": (it.get("unit") or "").strip().lower()})
            if isinstance(qty, (int, float)):
                g["qty"] += float(qty) * factor * float(mult or 1)
            else:
                g["any_null"] = True
            if rid not in g["sources"]:
                g["sources"].append(rid)

    prev = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT canonical, have, checked FROM menu_shopping_items "
        "WHERE menu_id = ? AND manual = 0", (menu_id,))}
    conn.execute("DELETE FROM menu_shopping_items WHERE menu_id = ? AND manual = 0",
                 (menu_id,))
    now, n = _now(), 0
    ordered = sorted(merged.items(),
                     key=lambda kv: (CATEGORY_ORDER.get(kv[1]["category"], 99), kv[0][0]))
    for (canonical, family), g in ordered:
        if family == "volume" and g["qty"] > 0:
            amount = _display_volume(g["qty"])
        elif family == "weight" and g["qty"] > 0:
            amount = _display_weight(g["qty"])
        elif g["qty"] > 0:
            unit = family[5:] if family.startswith("unit:") else g["unit"]
            amount = f"{_nice_qty(g['qty'])} {unit}".strip()
        else:
            amount = ""
        display = f"{amount} {canonical}".strip()
        if g["any_null"] and amount:
            display += " (+ some to taste)"
        have, checked = prev.get(canonical, (0, 0))
        conn.execute(
            "INSERT INTO menu_shopping_items(menu_id, canonical, display, category, "
            "have, checked, manual, sources, position, created_at) "
            "VALUES(?,?,?,?,?,?,0,?,?,?)",
            (menu_id, canonical, display, g["category"], have, checked,
             json.dumps(g["sources"]), n, now))
        n += 1
    conn.commit()
    return {"menu_id": menu_id, "generated": n,
            "recipes_parsed": len(parsed), "items": list_items(conn, menu_id)}


def list_items(conn: sqlite3.Connection, menu_id: int) -> list:
    rows = conn.execute(
        "SELECT id, canonical, display, category, have, checked, manual, sources, "
        "position FROM menu_shopping_items WHERE menu_id = ? "
        "ORDER BY manual, position, id", (menu_id,)).fetchall()
    cols = ["id", "canonical", "display", "category", "have", "checked", "manual",
            "sources", "position"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        try:
            d["sources"] = json.loads(d["sources"]) if d["sources"] else []
        except Exception:
            d["sources"] = []
        out.append(d)
    return out


def add_item(conn: sqlite3.Connection, user_id: int, menu_id: int,
             display: str, category: str = "other") -> dict | None:
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM menus WHERE id = ? AND user_id = ?",
                        (menu_id, user_id)).fetchone():
        return None
    display = (display or "").strip()
    if not display:
        raise ValueError("empty item")
    if category not in CATEGORIES:
        category = "other"
    conn.execute(
        "INSERT INTO menu_shopping_items(menu_id, canonical, display, category, "
        "manual, sources, position, created_at) VALUES(?,?,?,?,1,'[]',9999,?)",
        (menu_id, display.lower(), display, category, _now()))
    conn.commit()
    return {"added": display}


def set_item_state(conn: sqlite3.Connection, user_id: int, menu_id: int,
                   item_id: int, patch: dict) -> bool:
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM menus WHERE id = ? AND user_id = ?",
                        (menu_id, user_id)).fetchone():
        return False
    sets, vals = [], []
    for f in ("have", "checked"):
        if f in patch:
            sets.append(f"{f} = ?")
            vals.append(1 if patch[f] else 0)
    if not sets:
        return True
    vals.extend([menu_id, item_id])
    cur = conn.execute(f"UPDATE menu_shopping_items SET {', '.join(sets)} "
                       f"WHERE menu_id = ? AND id = ?", vals)
    conn.commit()
    return cur.rowcount > 0


def delete_item(conn: sqlite3.Connection, user_id: int, menu_id: int,
                item_id: int) -> bool:
    ensure_tables(conn)
    if not conn.execute("SELECT 1 FROM menus WHERE id = ? AND user_id = ?",
                        (menu_id, user_id)).fetchone():
        return False
    cur = conn.execute("DELETE FROM menu_shopping_items WHERE menu_id = ? AND id = ?",
                       (menu_id, item_id))
    conn.commit()
    return cur.rowcount > 0
