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

v2 adds nested-term subsumption (the closed-pattern rule from terminology
extraction, cf. C-value): a term whose cohort support is (nearly) fully
explained by a longer candidate containing it is a fragment, not a signal —
"granny smith apples" retires "smith", "granny", "granny smith" and
"smith apples"; "apples" survives on its independent occurrences. Trigrams
are mined so 3-word phrases exist to subsume their cross-boundary bigrams.

v3 adds attested plural folding: "apple"/"apples" are one concept whose
support was split across two terms (misranking both, and hiding
"granny smith apple" below min_df). A plural token folds to its singular
ONLY when the singular is itself observed in the corpus (>=2), so
molasses/couscous/hummus never manufacture garbage stems. Folding runs on
BOTH the cohort and the corpus-baseline text, keeping lift honest; the
displayed term uses the most frequent surface form.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

METHOD = "df-lift-v3"

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


# Function words that make an n-gram boundary ill-formed ("apples peeled
# and", "cored and"). Interior is fine. Distinct from _STOP, which also
# holds CONTENT commodities (cream, sugar) that n-grams must keep.
_FUNC = {"and", "the", "with", "for", "into", "onto", "from", "that",
         "this", "then", "your", "you", "such", "like", "very", "few",
         "bit", "them", "each", "until", "about", "over", "under", "not"}


def _terms(cleaned: str, fold: dict | None = None) -> dict:
    """{folded term: surface form as written} for all well-formed
    1..4-grams. 4-grams exist ONLY to feed the subsumption check
    (compute_signals filters them out of candidates). Folding is
    per-token so positions align and each lemma remembers the exact
    phrase it came from ("egg yolk" -> "egg yolks", never a
    token-by-token Frankenstein like "eggs yolks")."""
    fold = fold or {}
    ot = [t for t in cleaned.split() if len(t) > 2]
    ft = [fold.get(t, t) for t in ot]
    out: dict = {}
    for i, t in enumerate(ft):
        if t not in _STOP and t not in _FUNC:
            out.setdefault(t, ot[i])
    for n in (2, 3, 4):
        for i in range(len(ft) - n + 1):
            gram = ft[i:i + n]
            if gram[0] in _FUNC or gram[-1] in _FUNC:
                continue
            out.setdefault(" ".join(gram), " ".join(ot[i:i + n]))
    return out


def _prune_nested(cdf: Counter, cand: set) -> set:
    """Approximately-closed filter: drop a candidate whose cohort df is
    (nearly) matched by a longer mined phrase containing it — the fragment
    carries no independent evidence. Supers come from the FULL df table
    (sub-min_df phrases still subsume: "vegetable or canola oil" at df 2
    retires "vegetable canola" at df 3... within tolerance). Tolerance
    max(1, 10% of df) so one stray standalone doc doesn't resurrect
    "smith"."""
    by_word: defaultdict = defaultdict(set)
    for s in cdf:
        if " " in s:
            for w in set(s.split()):
                by_word[w].add(s)
    kept = set()
    for t in cand:
        pt = f" {t} "
        best = 0
        for s in by_word.get(t.split()[0], ()):
            if len(s) > len(t) and pt in f" {s} " and cdf[s] > best:
                best = cdf[s]
        if best and cdf[t] - best <= max(1, 0.1 * cdf[t]):
            continue
        kept.add(t)
    return kept


# Corpus-wide token vocabulary -> fold map, cached for the process (the
# sweep stamps ~250 dishes in one process; key = master row count so a
# grown corpus invalidates).
_FOLD_CACHE: dict = {"key": None, "fold": {}}


def _corpus_fold(conn: sqlite3.Connection) -> dict:
    key = conn.execute("SELECT COUNT(*) FROM master_recipes").fetchone()[0]
    if _FOLD_CACHE["key"] == key:
        return _FOLD_CACHE["fold"]
    vocab: Counter = Counter()
    for (dj,) in conn.execute("SELECT data FROM master_recipes"):
        lines, _ = _row_texts(dj)
        for ln in lines:
            vocab.update(ln.split())
    fold: dict = {}
    for t in vocab:
        if len(t) <= 3 or not t.endswith("s") or t.endswith(("ss", "us", "is")):
            continue
        if t.endswith("ies"):
            cands = [t[:-3] + "y"]
        else:
            cands = [t[:-1]] + ([t[:-2]] if t.endswith("es") else [])
        for c in cands:
            if vocab.get(c, 0) >= 2:
                fold[t] = c
                break
    _FOLD_CACHE.update(key=key, fold=fold)
    return fold


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

    fold = _corpus_fold(conn)

    def _fold_line(cleaned: str) -> str:
        return " ".join(fold.get(t, t) for t in cleaned.split())

    ing_df: Counter = Counter()          # folded term -> cohort doc frequency
    eq_df: Counter = Counter()
    examples: defaultdict = defaultdict(Counter)   # folded term -> original cleaned line
    term_surface: defaultdict = defaultdict(Counter)   # folded term -> as-written form
    prov_eth: Counter = Counter()
    prov_reg: Counter = Counter()
    for (dj,) in cohort:
        lines, eq_names = _row_texts(dj)
        seen = set()
        for ln in lines:
            for t, surf in _terms(ln, fold).items():
                if t not in seen:
                    ing_df[t] += 1
                    seen.add(t)
                examples[t][ln] += 1
                term_surface[t][surf] += 1
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

    ing_cand = _prune_nested(
        ing_df, {t for t, n in ing_df.items()
                 if n >= min_df and t.count(" ") <= 2})
    # NOTE (curator, 2026-08-31, same day it was tried): NO sellability
    # filtering here — signals are the dish's full measurement, and staple
    # terms are still identity/context. Sellability is enforced where classes
    # are PROPOSED (dish_class_proposals prompt), never in the measurement.
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
        text = " " + " ".join(_fold_line(ln) for ln in lines) + " "
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
                # Display face last (examples are keyed by the lemma):
                # the cohort's most-written surface form of the WHOLE term
                # ("egg yolk" shows as "egg yolks" because that is what the
                # lines say — never a per-token pick like "eggs yolks").
                sc = term_surface.get(r["term"])
                if sc:
                    r["term"] = sc.most_common(1)[0][0]
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
