"""intake/products/equipment_match.py — map a recipe's equipment to a Williams-Sonoma
product category (the `product_class`), which then yields catalog products.

PRIMARY = LLM classification into the taxonomy. A recipe's equipment names are terse and
often ambiguous ("oven", "fork", "lid", "wooden spoon or spatula"); cosine over the rich
ws_category embeddings mis-fires on those (oven→Dutch Ovens, fork→Can Openers). An LLM given
the whole taxonomy REASONS about what the tool is and picks the right category (or NONE when
nothing fits). Each DISTINCT term is classified once and cached in `tool_term_map`; the cache
is cleared whenever the taxonomy changes (so added categories / curator leaves take effect).

Embedding cosine (load_ws_categories / match_name) stays as (a) the fallback when the LLM is
unavailable and (b) the candidate list shown next to the LLM pick in the taxonomy tester.

See docs/equipment-product-linking.md, memory/project_equipment_standardization.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from input.pipeline.embeddings import embed_text, bytes_to_vec, cosine

# Embedding fallback floor (used only when the LLM is unavailable).
MATCH_MIN_COSINE = 0.28
_CLASSIFY_MODEL = "claude-haiku-4-5"

_CLASSIFY_SYSTEM = (
    "You map a recipe's kitchen EQUIPMENT term to the single best Williams-Sonoma product "
    "category for BUYING that tool. You are given the full category list (paths like "
    "'Cookware > Cookware Essentials > Fry Pans & Skillets'). Reply with ONLY the exact "
    "category path, copied verbatim from the list — no other text. Reason about what the tool "
    "actually is: 'oven' is the appliance (toaster/oven), not a Dutch oven; 'lid' is a cookware "
    "accessory; a compound like 'wooden spoon or spatula' picks the category of the primary "
    "tool. Reply exactly 'NONE' only when no listed category reasonably fits."
)


# --------------------------------------------------------------------------- #
# Embedding side (fallback + candidates)
# --------------------------------------------------------------------------- #
def load_ws_categories(conn: sqlite3.Connection) -> list:
    """[(id, headline, subcategory, ws_path, products_sample, vector)] for embedded rows."""
    rows = conn.execute(
        "SELECT id, headline, subcategory, ws_path, products_sample, embedding "
        "FROM ws_categories WHERE embedding IS NOT NULL"
    ).fetchall()
    out = []
    for rid, hl, sub, path, samp, emb in rows:
        v = bytes_to_vec(emb)
        if v is not None:
            out.append((rid, hl, sub, path, samp, v))
    return out


def match_name(name: str, cats: list, *, k: int = 5) -> list:
    """Top-k (score, category) for one term by embedding cosine — the candidate list."""
    q = embed_text(name)
    return sorted(((cosine(q, c[5]), c) for c in cats), key=lambda x: -x[0])[:k]


def _cat_row(name, size, cat, *, method, matched=True) -> dict:
    rid, hl, sub, path, samp, _ = cat
    return {"equipment": name, "size": size, "ws_category_id": rid, "headline": hl,
            "subcategory": sub, "ws_path": path, "method": method, "matched": matched,
            "products_sample": samp}


def _none_row(name, size, method) -> dict:
    return {"equipment": name, "size": size, "ws_category_id": None, "headline": None,
            "subcategory": None, "ws_path": None, "method": method, "matched": False,
            "products_sample": None}


# --------------------------------------------------------------------------- #
# Term cache + LLM classification (primary)
# --------------------------------------------------------------------------- #
def ensure_term_cache(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_term_map (
            term           TEXT PRIMARY KEY,   -- lowercased equipment term
            ws_category_id INTEGER,            -- NULL = NONE (no category fits)
            ws_path        TEXT,
            method         TEXT,               -- 'llm' | 'embed:0.xx' | 'none'
            updated_at     TEXT
        )""")
    conn.commit()


def clear_term_cache(conn: sqlite3.Connection) -> int:
    """Drop all cached classifications — call whenever the taxonomy changes so added /
    edited / deleted categories (and curator leaves) are reconsidered on the next match."""
    ensure_term_cache(conn)
    n = conn.execute("SELECT COUNT(*) FROM tool_term_map").fetchone()[0]
    conn.execute("DELETE FROM tool_term_map")
    conn.commit()
    return n


import re as _re

_BATCH = 50   # terms per LLM call


def classify_terms_llm(terms: list, paths: list) -> dict:
    """ONE LLM call classifying MANY terms → {term: 'path'|'NONE'}. The taxonomy goes in a
    cache_control'd system block (Anthropic prompt caching), so across a warm pass the ~2,500
    taxonomy tokens bill once, not per term. Terms are numbered; the model replies 'N. <answer>'.
    Raises on API failure (caller falls back to embeddings)."""
    import llm
    system = [
        {"type": "text", "text": _CLASSIFY_SYSTEM},
        {"type": "text", "text": "CATEGORIES:\n" + "\n".join(paths),
         "cache_control": {"type": "ephemeral"}},   # cached prefix — reused across calls
    ]
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(terms))
    user = ("Classify EACH equipment term to its single best category path (copied verbatim "
            "from the list) or NONE. Reply with one line per term as `N. <path or NONE>` and "
            "nothing else.\n\n" + numbered)
    r = llm.create(operation="tool_classify", model=_CLASSIFY_MODEL,
                   max_tokens=min(4000, 40 * len(terms) + 100), system=system,
                   messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in r.content if getattr(b, "type", None) == "text")
    out = {}
    for line in text.splitlines():
        m = _re.match(r"\s*(\d+)[.)]\s*(.+?)\s*$", line)
        if not m:
            continue
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(terms):
            out[terms[idx]] = m.group(2).strip()
    return out


def _resolve_terms(term_keys: list, conn: sqlite3.Connection, cats: list) -> dict:
    """{key: {ws_category_id, ws_path, method}} for the given lowercased terms. Cache hits
    come free; misses are classified in BATCHED LLM calls (taxonomy sent once per batch),
    with per-term embedding fallback on any off-list / failed answer. Results are cached."""
    ensure_term_cache(conn)
    by_path = {c[3]: c for c in cats}
    resolved, misses = {}, []
    for key in dict.fromkeys(term_keys):   # de-dup, keep order
        row = conn.execute(
            "SELECT ws_category_id, ws_path, method FROM tool_term_map WHERE term = ?", (key,)).fetchone()
        if row is not None:
            resolved[key] = {"ws_category_id": row[0], "ws_path": row[1], "method": row[2] or "cache"}
        else:
            misses.append(key)

    for i in range(0, len(misses), _BATCH):
        chunk = misses[i:i + _BATCH]
        picks = {}
        try:
            picks = classify_terms_llm(chunk, [c[3] for c in cats])
        except Exception as e:
            print(f"[tool-classify] batch LLM failed ({type(e).__name__}: {e}); embedding fallback")
        for key in chunk:
            ans = (picks.get(key) or "").strip()
            if ans and ans != "NONE" and ans in by_path:
                c = by_path[ans]
                res = {"ws_category_id": c[0], "ws_path": c[3], "method": "llm"}
            elif ans == "NONE":
                res = {"ws_category_id": None, "ws_path": None, "method": "none"}
            else:   # missing / off-list -> embedding fallback for just this term
                em = match_name(key, cats, k=1)
                if em and em[0][0] >= MATCH_MIN_COSINE:
                    s, c = em[0]
                    res = {"ws_category_id": c[0], "ws_path": c[3], "method": f"embed:{s:.2f}"}
                else:
                    res = {"ws_category_id": None, "ws_path": None, "method": "none"}
            resolved[key] = res
            conn.execute(
                "INSERT OR REPLACE INTO tool_term_map (term, ws_category_id, ws_path, method, updated_at) "
                "VALUES (?,?,?,?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (key, res["ws_category_id"], res["ws_path"], res["method"]))
        conn.commit()
    return resolved


def classify_term(term: str, conn: sqlite3.Connection, *, cats: Optional[list] = None) -> dict:
    """Single-term classify (cache-aware) — thin wrapper over the batched resolver."""
    if cats is None:
        cats = load_ws_categories(conn)
    return _resolve_terms([term.strip().lower()], conn, cats)[term.strip().lower()]


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _row_from_classify(name, size, cls, cats) -> dict:
    cid = cls.get("ws_category_id")
    if not cid:
        return _none_row(name, size, cls.get("method", "none"))
    cat = next((c for c in cats if c[0] == cid), None)
    if cat is None:  # category deleted since it was cached
        return _none_row(name, size, "stale")
    return _cat_row(name, size, cat, method=cls.get("method", "llm"))


def match_equipment_name(name: str, conn: sqlite3.Connection, *, k: int = 5) -> dict:
    """Classify one term (LLM, cached) AND return the embedding candidate shortlist — for
    the taxonomy 'Test a term' box. {classification: {...}|None, candidates: [rows]}."""
    cats = load_ws_categories(conn)
    cls = classify_term(name, conn, cats=cats)
    pick = _row_from_classify(name, None, cls, cats)
    candidates = [_cat_row(name, None, c, method=f"embed:{s:.3f}",
                           matched=(s >= MATCH_MIN_COSINE)) | {"score": round(float(s), 3)}
                  for s, c in match_name(name, cats, k=k)]
    return {"classification": pick if pick.get("matched") else {**pick, "reason": "no category fits (consumable / not a tool)"},
            "candidates": candidates}


def match_recipe_equipment(recipe: dict, conn: sqlite3.Connection) -> list:
    """Each equipment item → its best WS category. ALL of the recipe's uncached terms are
    classified in ONE batched LLM call (the taxonomy is sent once), then cached. Preserves
    order; a repeated tool is classified once."""
    cats = load_ws_categories(conn)
    if not cats:
        return []
    items = [((e.get("name") or "").strip(), e.get("size"))
             for e in (recipe.get("equipment") or [])
             if isinstance(e, dict) and (e.get("name") or "").strip()]
    resolved = _resolve_terms([n.lower() for n, _ in items], conn, cats)
    return [_row_from_classify(n, sz, resolved[n.lower()], cats) for n, sz in items]


# --------------------------------------------------------------------------- #
# OUR taxonomy: equipment term → product_classes row (family 'equipment')
# --------------------------------------------------------------------------- #
# Curator 2026-11-19: "Shop the tools" shows OUR classes, not Williams-Sonoma's
# paths. Same shape as the WS path above — LLM classification into the list,
# one batched call per recipe, cached per term — but the list is the
# product_classes registry (class_registry.py) and the hop to products is
# direct (products.product_class = name). The WS code above stays for the
# taxonomy tester and as the embedding fallback's model.
#
# The cache carries a TAXONOMY SIGNATURE (row count + latest updated_at of the
# equipment classes): when the curator adds / renames / deletes a class the
# signature moves and every cached answer is re-asked, so a new class takes
# effect on the next click without a manual cache clear.

_CLASSIFY_CLASS_SYSTEM = (
    "You map a recipe's kitchen EQUIPMENT term to the single best PRODUCT CLASS for BUYING "
    "that tool, from the class list you are given (names like 'Dutch Ovens (5-6 qt)', "
    "'Chef Knife', 'Sheet Pan'). Reply with ONLY the exact class name, copied verbatim from "
    "the list — no other text. Reason about what the tool actually is: 'oven' is the kitchen "
    "appliance, not a Dutch oven; a compound like 'wooden spoon or spatula' picks the class of "
    "the primary tool; a sized class is fine for an unsized term when it is the only class for "
    "that tool. Synonyms are ONE tool: a skillet is a frying pan, a sheet tray is a sheet pan, "
    "a stockpot is a pasta pot, a silicone spatula is a spatula. Reply exactly 'NONE' when no "
    "listed class reasonably fits — a generic 'bowl', 'oven' or 'fork' with no class is NONE, "
    "never a stretch."
)


def ensure_class_term_cache(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_class_map (
            term          TEXT PRIMARY KEY,   -- lowercased equipment term
            class_name    TEXT,               -- NULL = NONE (no class fits)
            method        TEXT,               -- 'llm' | 'embed:0.xx' | 'none'
            taxonomy_sig  TEXT,               -- registry signature the answer was given against
            updated_at    TEXT
        )""")
    conn.commit()


def load_equipment_classes(conn: sqlite3.Connection) -> list:
    """[(name, family, products_count, vector)] for every embedded equipment-family class."""
    from intake.products.class_registry import ensure_registry
    ensure_registry(conn)
    rows = conn.execute(
        "SELECT c.name, c.family, c.embedding, "
        "       (SELECT COUNT(*) FROM products p WHERE p.product_class = c.name) "
        "FROM product_classes c "
        "WHERE c.family = 'equipment' AND c.embedding IS NOT NULL ORDER BY c.name").fetchall()
    out = []
    for name, fam, emb, n in rows:
        v = bytes_to_vec(emb)
        if v is not None:
            out.append((name, fam, int(n or 0), v))
    return out


def _class_taxonomy_sig(conn: sqlite3.Connection) -> str:
    n, latest = conn.execute(
        "SELECT COUNT(*), MAX(updated_at) FROM product_classes WHERE family = 'equipment'"
    ).fetchone()
    return f"{n}:{latest or ''}"


def _class_row(name, size, cls, *, method, matched=True) -> dict:
    cname, fam, n, _ = cls
    return {"equipment": name, "size": size, "class_name": cname, "family": fam,
            "products_count": n, "method": method, "matched": matched}


def _class_none_row(name, size, method) -> dict:
    return {"equipment": name, "size": size, "class_name": None, "family": None,
            "products_count": 0, "method": method, "matched": False}


def classify_terms_llm_classes(terms: list, names: list) -> dict:
    """ONE LLM call, MANY terms → {term: '<class name>'|'NONE'}; the class list rides in a
    cached system block exactly like the WS classifier."""
    import llm
    system = [
        {"type": "text", "text": _CLASSIFY_CLASS_SYSTEM},
        {"type": "text", "text": "CLASSES:\n" + "\n".join(names),
         "cache_control": {"type": "ephemeral"}},
    ]
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(terms))
    user = ("Classify EACH equipment term to its single best class name (copied verbatim "
            "from the list) or NONE. Reply with one line per term as `N. <class or NONE>` and "
            "nothing else.\n\n" + numbered)
    r = llm.create(operation="tool_classify_class", model=_CLASSIFY_MODEL,
                   max_tokens=min(4000, 40 * len(terms) + 100), system=system,
                   messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in r.content if getattr(b, "type", None) == "text")
    out = {}
    for line in text.splitlines():
        m = _re.match(r"\s*(\d+)[.)]\s*(.+?)\s*$", line)
        if m and 0 <= int(m.group(1)) - 1 < len(terms):
            out[terms[int(m.group(1)) - 1]] = m.group(2).strip()
    return out


def _resolve_terms_classes(term_keys: list, conn: sqlite3.Connection, classes: list) -> dict:
    """{key: {class_name, method}} — cache hits whose taxonomy signature still matches come
    free; everything else goes through one batched LLM call with per-term embedding fallback
    (same floor as the WS path; the registry embeds '<name>. product class.' so the query
    does too, keeping the distance symmetric)."""
    ensure_class_term_cache(conn)
    sig = _class_taxonomy_sig(conn)
    by_name = {c[0]: c for c in classes}
    resolved, misses = {}, []
    for key in dict.fromkeys(term_keys):
        row = conn.execute(
            "SELECT class_name, method, taxonomy_sig FROM tool_class_map WHERE term = ?",
            (key,)).fetchone()
        if row is not None and row[2] == sig and (row[0] is None or row[0] in by_name):
            resolved[key] = {"class_name": row[0], "method": row[1] or "cache"}
        else:
            misses.append(key)

    for i in range(0, len(misses), _BATCH):
        chunk = misses[i:i + _BATCH]
        picks = {}
        try:
            picks = classify_terms_llm_classes(chunk, [c[0] for c in classes])
        except Exception as e:
            print(f"[tool-classify-class] batch LLM failed ({type(e).__name__}: {e}); embedding fallback")
        for key in chunk:
            ans = (picks.get(key) or "").strip()
            if ans and ans != "NONE" and ans in by_name:
                res = {"class_name": ans, "method": "llm"}
            elif ans == "NONE":
                res = {"class_name": None, "method": "none"}
            else:
                q = embed_text(f"{key}. product class.")
                best = max(((cosine(q, c[3]), c) for c in classes), key=lambda x: x[0],
                           default=None)
                if best and best[0] >= MATCH_MIN_COSINE:
                    res = {"class_name": best[1][0], "method": f"embed:{best[0]:.2f}"}
                else:
                    res = {"class_name": None, "method": "none"}
            resolved[key] = res
            conn.execute(
                "INSERT OR REPLACE INTO tool_class_map (term, class_name, method, taxonomy_sig, "
                "updated_at) VALUES (?,?,?,?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (key, res["class_name"], res["method"], sig))
        conn.commit()
    return resolved


def match_recipe_equipment_classes(recipe: dict, conn: sqlite3.Connection) -> list:
    """Each equipment item → its best class in OUR registry. Uncached terms are classified
    in ONE batched LLM call, then cached against the taxonomy signature. Preserves order; a
    repeated tool is classified once."""
    classes = load_equipment_classes(conn)
    if not classes:
        return []
    by_name = {c[0]: c for c in classes}
    by_fold = {c[0].strip().lower(): c for c in classes}
    items = [((e.get("name") or "").strip(), e.get("size"), (e.get("product_class") or "").strip())
             for e in (recipe.get("equipment") or [])
             if isinstance(e, dict) and (e.get("name") or "").strip()]
    # ONE STEP (2026-11-19): a tool extracted with our key, or named exactly as a
    # class, is answered from the registry outright. Only legacy / free-text rows
    # still go through the classifier.
    direct = {}
    for n, _sz, key in items:
        if key in by_name:
            direct[n.lower()] = (by_name[key], "key")
        elif n.lower() in by_fold:
            direct[n.lower()] = (by_fold[n.lower()], "exact")
    pending = [n.lower() for n, _sz, _k in items if n.lower() not in direct]
    resolved = _resolve_terms_classes(pending, conn, classes) if pending else {}
    out = []
    for n, sz, _key in items:
        if n.lower() in direct:
            cls, method = direct[n.lower()]
            out.append(_class_row(n, sz, cls, method=method))
            continue
        r = resolved[n.lower()]
        cls = by_name.get(r.get("class_name") or "")
        out.append(_class_row(n, sz, cls, method=r.get("method", "llm")) if cls
                   else _class_none_row(n, sz, r.get("method", "none")))
    return out
