"""Calibrate the mechanical dish-signal -> product-class matcher.

The matcher's design (docs/dish-product-matching.md, class `signals` shipped
761c94d): a dish cohort signal TERM matches a class through the TRIGGER
PHRASES stored on the class record — score = min embedding distance over the
class's signal phrases. This script measures whether a single distance bar
separates real pairs from junk BEFORE the matcher is built, on live labels:

  positives      every evidence term cited on an approved/proposed
                 dish_product_classes junction row -> that class
  hard negatives the same terms against OTHER classes proposed on the same
                 dish (the confusable set the matcher must not cross), plus
                 same-family classes
  random         random term x class pairs

Also reports name-distance beside signal-distance for every pair — the
quantified test of the founding claim ("Apple Peeler" the NAME embeds
nowhere near "granny smith apples" the PHRASE; the signals surface is what
makes the match possible).

Embeddings: text-embedding-3-small, BARE text both sides (term vs phrase —
symmetric, same register), cached in scratch so re-runs are free.
Read-only against recipes.db; writes nothing but the cache + report.

Run:  python scripts/calibrate_class_match.py
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "recipes.db"
CACHE = ROOT / "scripts" / ".class_match_embed_cache.json"
BARS = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]
random.seed(11)


def _load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                import os
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def embed_all(texts: list) -> dict:
    """text -> unit vector, disk-cached. Batched 100/request."""
    cache = {}
    if CACHE.exists():
        cache = json.loads(CACHE.read_text(encoding="utf-8"))
    missing = [t for t in dict.fromkeys(texts) if t not in cache]
    if missing:
        _load_env()
        from input.pipeline.embeddings import _get_client, EMBED_MODEL
        client = _get_client()
        for i in range(0, len(missing), 100):
            batch = missing[i:i + 100]
            resp = client.embeddings.create(model=EMBED_MODEL, input=batch)
            for t, d in zip(batch, resp.data):
                cache[t] = d.embedding
            print(f"  embedded {min(i + 100, len(missing))}/{len(missing)}")
        CACHE.write_text(json.dumps(cache), encoding="utf-8")
    out = {}
    for t in dict.fromkeys(texts):
        v = np.array(cache[t], dtype="float32")
        out[t] = v / (np.linalg.norm(v) or 1.0)
    return out


def dist(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.linalg.norm(a - b))       # L2 on unit vectors (matches snap/dish match)


def pct(vals, q):
    return round(float(np.percentile(vals, q)), 3) if vals else None


def main():
    conn = sqlite3.connect(DB)

    # ---- classes: signals + names -------------------------------------
    classes = {}
    for name, family, sig in conn.execute(
            "SELECT name, family, signals FROM product_classes"):
        try:
            phrases = [p.strip().lower() for p in json.loads(sig or "[]") if p and p.strip()]
        except Exception:
            phrases = []
        classes[name] = {"family": family, "signals": phrases}

    # ---- labeled pairs from the junction ------------------------------
    rows = conn.execute(
        "SELECT dish_name, class_name, status, route, evidence "
        "FROM dish_product_classes WHERE status IN ('approved','proposed')").fetchall()
    by_dish = defaultdict(set)
    positives = []          # (term, class, status, route)
    for dish, cls, status, route, ev in rows:
        if cls not in classes:
            continue
        by_dish[dish].add(cls)
        try:
            terms = [(e.get("term") or "").strip().lower() for e in json.loads(ev or "[]")]
        except Exception:
            terms = []
        for t in terms:
            if t:
                positives.append((t, cls, status, route))
    positives = list(dict.fromkeys(positives))

    # ---- hard negatives: same term vs the dish's OTHER classes --------
    hard = []
    pos_set = {(t, c) for t, c, _, _ in positives}
    for dish, cls, status, route, ev in rows:
        if cls not in classes:
            continue
        try:
            terms = [(e.get("term") or "").strip().lower() for e in json.loads(ev or "[]")]
        except Exception:
            continue
        others = [c for c in by_dish[dish] if c != cls]
        for t in terms:
            for c in others:
                if t and (t, c) not in pos_set:
                    hard.append((t, c))
    hard = list(dict.fromkeys(hard))

    # ---- random negatives ---------------------------------------------
    all_terms = list(dict.fromkeys(t for t, _, _, _ in positives))
    signal_classes = [c for c, d in classes.items() if d["signals"]]
    rnd = []
    while len(rnd) < min(3000, len(all_terms) * 12):
        t, c = random.choice(all_terms), random.choice(signal_classes)
        if (t, c) not in pos_set:
            rnd.append((t, c))
    rnd = list(dict.fromkeys(rnd))

    # ---- embed everything ---------------------------------------------
    texts = list(all_terms)
    for c, d in classes.items():
        texts.append(c.lower())
        texts.extend(d["signals"])
    print(f"embedding {len(set(texts))} unique texts "
          f"({len(positives)} positives, {len(hard)} hard negs, {len(rnd)} random)")
    E = embed_all(texts)

    def score(term, cls):
        d = classes[cls]
        ds = min((dist(E[term], E[p]) for p in d["signals"]), default=None)
        dn = dist(E[term], E[cls.lower()])
        return ds, dn

    def collect(pairs):
        sig, nam, skipped = [], [], 0
        detail = []
        for t, c in pairs:
            ds, dn = score(t, c)
            if ds is None:
                skipped += 1          # class has no signals — matcher can't see it
                continue
            sig.append(ds); nam.append(dn); detail.append((t, c, ds, dn))
        return sig, nam, skipped, detail

    p_pairs = [(t, c) for t, c, _, _ in positives]
    ps, pn, pskip, pdet = collect(p_pairs)
    hs, hn, hskip, hdet = collect(hard)
    rs, rn, rskip, rdet = collect(rnd)

    print("\n== distance distributions (signal-phrase surface | class-name surface) ==")
    for label, s, n in (("positives", ps, pn), ("hard negatives", hs, hn), ("random", rs, rn)):
        print(f"  {label:15s} n={len(s):5d}  signal p10/50/90: "
              f"{pct(s,10)}/{pct(s,50)}/{pct(s,90)}   name p10/50/90: "
              f"{pct(n,10)}/{pct(n,50)}/{pct(n,90)}")
    print(f"  (skipped — class has no signals: pos {pskip}, hard {hskip}, rnd {rskip})")

    print("\n== bar sweep on the SIGNAL surface (accept when min-dist <= bar) ==")
    print("  bar    pos recall   hard-neg FPR   random FPR")
    for b in BARS:
        rec = sum(1 for d in ps if d <= b) / len(ps) if ps else 0
        fh = sum(1 for d in hs if d <= b) / len(hs) if hs else 0
        fr = sum(1 for d in rs if d <= b) / len(rs) if rs else 0
        print(f"  {b:.2f}   {rec:8.1%}     {fh:8.1%}      {fr:8.1%}")

    print("\n== name-vs-signal: how much the signals surface buys (positives) ==")
    gain = [dn - ds for _, _, ds, dn in pdet]
    print(f"  name-dist minus signal-dist on true pairs: p10/50/90 = "
          f"{pct(gain,10)}/{pct(gain,50)}/{pct(gain,90)}  (positive = signals closer)")

    print("\n== smoke tests ==")
    smoke = [("egg yolks", "Egg Separators"), ("yolks", "Egg Separators"),
             ("tube pan", "Tube Pan"), ("bundt pan", "Tube Pan"),
             ("12-cup bundt pan", "Tube Pan"),
             ("granny smith apples", "Granny Smith Apples"),
             ("granny smith apples", "Apple Peeler"),
             ("apples peeled", "Apple Peeler"),
             ("chocolate", "Baking Chocolate"), ("cocoa", "Cocoa Powder")]
    for t, c in smoke:
        if c not in classes:
            print(f"  {t!r} -> {c}: class not in registry"); continue
        E2 = embed_all([t.lower()])
        E.update(E2)
        ds, dn = score(t.lower(), c)
        print(f"  {t!r} -> {c}: signal={ds if ds is None else round(ds,3)} "
              f"name={round(dn,3)} signals={classes[c]['signals'][:4]}")

    print("\n== worst offenders at bar 0.60 ==")
    fp = sorted([x for x in hdet + rdet if x[2] <= 0.60], key=lambda x: x[2])[:15]
    for t, c, ds, dn in fp:
        print(f"  FALSE ACCEPT  {t!r} -> {c}  signal={ds:.3f}")
    fn = sorted([x for x in pdet if x[2] > 0.60], key=lambda x: -x[2])[:15]
    for t, c, ds, dn in fn:
        print(f"  MISSED TRUE   {t!r} -> {c}  signal={ds:.3f}")

    conn.close()


if __name__ == "__main__":
    main()
