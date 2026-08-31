"""One-dish mechanical matcher experiment — Apple Cake only.

The aggregation question (unarchitected as of 2026-08-31): a dish has ~40
weighted signal terms, a class has a handful of trigger phrases — how do
term-level distances aggregate into a CLASS ranking? This script runs one
transparent candidate scheme so we can look at real output and argue about
it:

  term weight   = df * ln(lift)   (the mining rank key — support-dominant)
  tier factor   = 1.0 exact (d=0) | 0.7 close (d<=0.45) | 0.3 fuzzy (d<=0.65)
  class score   = sum over dish terms of weight * factor, using each term's
                  BEST phrase on that class; a term credits a class ONCE.

Prints every class that scored, with the term->phrase evidence lines.
Read-only; reuses the calibration harness's embedding cache.

Run: python scripts/match_one_dish.py "Apple Cake"
"""
from __future__ import annotations

import json
import math
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.calibrate_class_match import embed_all, dist   # noqa: E402

DB = ROOT / "recipes.db"


def main(dish: str):
    conn = sqlite3.connect(DB)
    sig = json.loads(conn.execute(
        "SELECT cohort_signals FROM dishes WHERE name = ?", (dish,)).fetchone()[0])

    # dish terms with weights (ingredients + equipment; equipment rows carry
    # df/lift too, same weighting)
    terms = []
    for fam_key in ("ingredients", "equipment"):
        for r in sig.get(fam_key) or []:
            w = r["df"] * math.log(max(r["lift"], 1.01))
            terms.append((r["term"].lower(), round(w, 2), fam_key[:3]))

    classes = {}
    for name, family, s in conn.execute(
            "SELECT name, family, signals FROM product_classes "
            "WHERE signals IS NOT NULL AND signals != '[]' AND signals != ''"):
        try:
            phrases = [p.strip().lower() for p in json.loads(s) if p and p.strip()]
        except Exception:
            continue
        if phrases:
            classes[name] = (family, phrases)

    texts = [t for t, _, _ in terms]
    for _, (_, ph) in classes.items():
        texts.extend(ph)
    E = embed_all(texts)

    results = []
    for cname, (family, phrases) in classes.items():
        score, hits = 0.0, []
        for t, w, kind in terms:
            best_d, best_p = min(((dist(E[t], E[p]), p) for p in phrases),
                                 key=lambda x: x[0])
            if best_d <= 0.001:
                f = 1.0
            elif best_d <= 0.45:
                f = 0.7
            elif best_d <= 0.65:
                f = 0.3
            else:
                continue
            score += w * f
            hits.append((t, w, best_p, round(best_d, 3), f, kind))
        if hits:
            results.append((round(score, 1), cname, family, hits))

    results.sort(key=lambda x: -x[0])
    print(f"=== {dish} — {len(terms)} weighted terms vs {len(classes)} "
          f"signal-bearing classes ===\n")
    for score, cname, family, hits in results:
        print(f"{score:8.1f}  {cname} [{family}]")
        for t, w, p, d, f, kind in sorted(hits, key=lambda h: -h[1]):
            print(f"           {kind} {t!r} -> {p!r}  d={d} x{f}  (w={w})")

    # ---- variant 2: exclusivity discount ------------------------------
    # A term that hits N classes is generic BETWEEN classes even when it's
    # distinctive for the dish — 'apples' crediting Granny Smith Apples,
    # Apple Corer AND Apple Peeler at full weight is triple-counted
    # identity. Divide each term's credit by the number of classes it hit.
    hit_count = {}
    for _, _, _, hits in results:
        for t, *_ in hits:
            hit_count[t] = hit_count.get(t, 0) + 1
    v2 = []
    for _, cname, family, hits in results:
        s = sum(w * f / hit_count[t] for t, w, _, _, f, _ in hits)
        excl = [t for t, *_ in hits if hit_count[t] == 1]
        v2.append((round(s, 1), cname, family, excl))
    v2.sort(key=lambda x: -x[0])
    print("\n=== variant 2: same hits, credit / n-classes-hit ===")
    for s, cname, family, excl in v2[:15]:
        print(f"{s:8.1f}  {cname} [{family}]"
              + (f"  exclusive: {excl}" if excl else "  (no exclusive terms)"))
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "Apple Cake")
