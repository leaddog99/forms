"""Cohort signals — what a dish's recipes USE, measured, with lift.

Step 1 of the dish->product-class pipeline (docs/dish-product-matching.md
§3.5): before any class is proposed, stamp the EVIDENCE on the dish record —
term frequencies across the dish's cohort, each with its lift against the
corpus baseline, plus example lines carrying the form/kind qualifiers
("8 oz bittersweet chocolate, chopped") that turn a term into a precise
product class downstream.

Lift = (share of cohort recipes containing the term) / (share of ALL recipes
containing it). Plain frequency answers "what do these recipes use?" (sugar,
butter, a whisk — same as everything); lift answers "what do they use that
OTHERS don't?" — the commerce question. Measured on Chocolate Cream Pie
2026-08-28: sugar 87% of cohort but 2.7x lift (commodity); chocolate 65% at
27x, oreo 14% at 286x (the identities). Rule of thumb: <3x commodity, >10x
identity.

Cohort = master rows whose dish_effective resolves to the dish (the
resolution ladder, all three rungs — a loose nearest is still evidence).
Stored on dishes.cohort_signals per the persist-derived rule (value + method
+ inputs, re-stamped when recomputed; never compute-on-read).
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

METHOD = "df-lift-v1"

# Quantity/unit/prep noise stripped from ingredient lines before term mining.
_NOISE = re.compile(
    r"\b(cups?|tablespoons?|tbsp|teaspoons?|tsp|ounces?|oz|pounds?|lbs?|grams?|g|kg|"
    r"ml|l|liters?|quarts?|qt|pints?|sticks?|cloves?|cans?|packages?|pkg|envelopes?|"
    r"large|small|medium|extra|finely|coarsely|roughly|freshly|thinly|chopped|sliced|"
    r"diced|minced|grated|melted|softened|cooled|divided|packed|heaping|level|plus|"
    r"more|about|approximately|optional|to taste|as needed|for (?:garnish|serving|dusting)|"
    r"at room temperature|room temperature|cold|warm|hot|whole|half|halved|cut into|"
    r"pieces?|inch|inches)\b")
_NON_ALPHA = re.compile(r"[\d/¼½¾⅓⅔⅛⅜⅝⅞.,;:()\[\]{}*#%\"'’‘“”–—\-]+")
_WS = re.compile(r"\s+")
# Unigrams too generic to ever be a class on their own; bigrams keep them
# ("heavy cream", "egg yolks" survive via their bigram).
_STOP = {
    "and", "or", "of", "the", "a", "an", "with", "for", "to", "into", "in", "on",
    "if", "your", "you", "such", "as", "like", "very", "few", "bit", "them",
    "cream", "sugar", "salt", "water", "flour", "butter", "milk", "eggs", "egg",
    "oil", "fresh", "ground", "pure", "unsalted", "salted", "granulated",
    "powdered", "confectioners", "light", "dark", "sweet", "temperature",
}


def _clean(line: str) -> str:
    s = _NON_ALPHA.sub(" ", str(line).lower())
    s = _NOISE.sub(" ", s)
    return _WS.sub(" ", s).strip()


def _terms(cleaned: str) -> set:
    toks = [t for t in cleaned.split() if len(t) > 2]
    out = set()
    for t in toks:
        if t not in _STOP:
            out.add(t)
    for a, b in zip(toks, toks[1:]):
        out.add(f"{a} {b}")
    return out


def _row_texts(data_json: str) -> tuple[list, list]:
    """(cleaned ingredient lines, equipment names) for one recipe row."""
    try:
        d = json.loads(data_json)
    except Exception:
        return [], []
    ings = [_clean(x) for x in (d.get("recipeIngredient") or [])]
    eq = [str(t.get("name") or "").strip().lower()
          for t in (d.get("equipment") or []) if isinstance(t, dict)]
    return [x for x in ings if x], [x for x in eq if x]


def compute_signals(conn: sqlite3.Connection, dish_name: str, *,
                    min_df: int = 3, max_ingredients: int = 25,
                    max_equipment: int = 15, max_examples: int = 4) -> dict:
    """Measure the dish's cohort and return the signals block (no write)."""
    cohort = conn.execute(
        "SELECT data FROM master_recipes WHERE dish_effective = ?",
        (dish_name,)).fetchall()
    cn = len(cohort)
    if not cn:
        return {"method": METHOD, "computed_at": _now(), "cohort_n": 0}

    ing_df: Counter = Counter()          # term -> cohort doc frequency
    eq_df: Counter = Counter()
    examples: defaultdict = defaultdict(Counter)   # term -> cleaned line -> count
    prov_eth: Counter = Counter()
    prov_reg: Counter = Counter()
    for (dj,) in cohort:
        lines, eq_names = _row_texts(dj)
        seen = set()
        for ln in lines:
            for t in _terms(ln):
                if t not in seen:
                    ing_df[t] += 1
                    seen.add(t)
                examples[t][ln] += 1
        for name in set(eq_names):
            eq_df[name] += 1
        try:
            d = json.loads(dj)
            p = d.get("provenance") or {}
            if (p.get("ethnicity") or "").strip():
                prov_eth[p["ethnicity"].strip()] += 1
            if (p.get("originRegion") or "").strip():
                prov_reg[p["originRegion"].strip()] += 1
        except Exception:
            pass

    ing_cand = {t for t, n in ing_df.items() if n >= min_df}
    eq_cand = {t for t, n in eq_df.items() if n >= min_df}

    # Authority profile of the cohort — free (generated columns, one fetch).
    # DA/PA are the headline: absolute Moz scales, comparable across dishes
    # ("Chili lives on DA-71 sites; Lok Lak on DA-53 blogs"). OU is stored too
    # (effective_ou_score = paywall-adjusted) but read it with the two-stage
    # caveat: each cohort was SELECTED from a different traffic pool, so
    # cross-dish OU deltas mostly re-express the DA/PA mix. NULLs are skipped
    # per metric (absent-is-not-zero), n counted per metric.
    def _stats(vals: list) -> dict | None:
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        mid = len(vals) // 2
        med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        return {"n": len(vals), "mean": round(sum(vals) / len(vals), 1),
                "median": round(med, 1)}
    _auth_rows = conn.execute(
        "SELECT domain_authority, page_authority, effective_ou_score "
        "FROM master_recipes WHERE dish_effective = ?", (dish_name,)).fetchall()
    authority = {
        "da": _stats([r[0] for r in _auth_rows]),
        "pa": _stats([r[1] for r in _auth_rows]),
        "ou": _stats([r[2] for r in _auth_rows]),
    }

    # Corpus baseline: ONE scan, doc frequency for exactly the candidate terms.
    gn = 0
    g_ing: Counter = Counter()
    g_eq: Counter = Counter()
    for (dj,) in conn.execute("SELECT data FROM master_recipes"):
        gn += 1
        lines, eq_names = _row_texts(dj)
        text = " " + " ".join(lines) + " "
        for t in ing_cand:
            if f" {t} " in text or text.strip().startswith(t) or text.strip().endswith(t):
                g_ing[t] += 1
        eqset = set(eq_names)
        for t in eq_cand:
            if t in eqset:
                g_eq[t] += 1

    def _rank(cdf: Counter, gdf: Counter, cand: set, cap: int, with_examples: bool):
        rows = []
        for t in cand:
            cp = cdf[t] / cn
            gp = max(gdf.get(t, 0), 0.5) / gn      # floor: absent-from-corpus ≠ infinite
            rows.append({
                "term": t, "df": cdf[t],
                "pct": round(100 * cp, 1),
                "corpus_pct": round(100 * gdf.get(t, 0) / gn, 2),
                "lift": round(cp / gp, 1),
            })
        # Support-weighted: pure lift-sort headlines df=3 line fragments
        # ("homemade store" at 756x) over the dish's actual identity
        # (chocolate: 13x but in 41 recipes); raw lift*df still let the
        # 756x outlier win. df * ln(lift) makes support dominant with
        # distinctiveness as the multiplier.
        import math
        rows.sort(key=lambda r: -(r["df"] * math.log(max(r["lift"], 1.01))))
        rows = rows[:cap]
        if with_examples:
            for r in rows:
                r["examples"] = [ln for ln, _ in examples[r["term"]].most_common(max_examples)]
        return rows

    return {
        "method": METHOD,
        "computed_at": _now(),
        "cohort_n": cn,
        "corpus_n": gn,
        "min_df": min_df,
        "authority": authority,
        "ingredients": _rank(ing_df, g_ing, ing_cand, max_ingredients, True),
        "equipment": _rank(eq_df, g_eq, eq_cand, max_equipment, False),
        "provenance": {
            "ethnicities": dict(prov_eth.most_common(5)),
            "regions": dict(prov_reg.most_common(5)),
        },
    }


def stamp_signals(conn: sqlite3.Connection, dish_name: str, **kw) -> dict:
    """Compute + persist on dishes.cohort_signals. Returns the signals."""
    sig = compute_signals(conn, dish_name, **kw)
    conn.execute("UPDATE dishes SET cohort_signals = ? WHERE name = ?",
                 (json.dumps(sig, ensure_ascii=False), dish_name))
    conn.commit()
    return sig


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
