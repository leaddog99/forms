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
