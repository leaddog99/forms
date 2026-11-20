"""Export one dish chapter as the JSON the DPI taxonomy pilot reads.

    python -m scripts.export_dish_chapter --chapter "Soups & Stews"
    python -m scripts.export_dish_chapter --chapter "Soups & Stews" --out path.json

READ-ONLY on recipes.db (opened with mode=ro). Nothing in BCC changes. This is
step 1 of docs/dish-taxonomy-pilot-soups-stews.md §3: the DPI session on BAILEY
builds the chapter's tree from culinary knowledge and uses this file for the
references the tree must account for, the recipes to classify, and the evidence
lines in the generator's brief.

Sections (every count is measured here, the same way the plan's §2 numbers were):

  dishes            the chapter's catalog rows: name (the join key), display_name,
                    aliases, description, identity card, demand, and how many
                    recipes sit on each dish — curated (_master.dish), confident
                    (_match.confident), weak (nearest candidate, under the bar).
  recipes           every recipe that is curated, confident, or weak-leaning on a
                    chapter dish: id, title, url, identity card, the composed
                    identity text the matcher embeds (title excluded), and the
                    current placement (dish, basis, method, distance, candidates).
  holes             identity-card names (likelyDish) that are NOT a catalog name,
                    display name or alias, counted over the weak leaners, with the
                    dish each leans on; `settled_rows` counts the same name on
                    curated/confident rows (those are the alias candidates).
  aliases_vetted    the approved rows of the alias pre-generation (tiers A/B/C)
                    whose dish is in the chapter — "also called" lines.
  aliases_rejected  the alias judge's NOT-same rows (tier R) for chapter dishes:
                    sibling / child / attribute / component names with the reason,
                    i.e. candidate leaves and attributes for the generator to rule on.
  demand            keyword demand per name (dish names, aliases, hole names) from
                    dish_keywords: matching keyword count, summed traffic, top
                    keyword — the distinctiveness rule's "a name people search for".

The alias files default to the latest docs/research/dish-alias-vetted-*.json and
dish-alias-proposals-*.md (the committed review file; its R section is parsed).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from input.pipeline import dish_match  # noqa: E402
from input.pipeline.embeddings import compose_identity_text  # noqa: E402

fold = dish_match._fold_name


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _loads(s, default):
    try:
        v = json.loads(s) if s else default
        return v if v is not None else default
    except (ValueError, TypeError):
        return default


def _rel(p: str | None) -> str | None:
    return os.path.relpath(p, ROOT).replace("\\", "/") if p else None


def _latest(pattern: str) -> str | None:
    hits = sorted(glob.glob(os.path.join(ROOT, pattern)))
    return hits[-1] if hits else None


# ── alias review file: the R section ─────────────────────────────────────────
_ROW = re.compile(r"^- \*\*(?P<alias>.+?)\*\* → (?P<dish>.+?)\s+×(?P<rows>\d+)"
                  r"(?:\s+— (?P<kind>[\w-]+): (?P<why>.*))?$")


def parse_rejected(review_path: str) -> list[dict]:
    """Rows of the '## R — NOT the same dish' section of the proposals file."""
    out, in_r = [], False
    with open(review_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("## "):
                in_r = line.startswith("## R ")
                continue
            if not in_r:
                continue
            m = _ROW.match(line)
            if m:
                out.append({"alias": m["alias"], "dish": m["dish"], "rows": int(m["rows"]),
                            "kind": m["kind"] or "", "why": (m["why"] or "").strip()})
    return out


# ── keyword demand (the coverage page's posting-index shape, one table scan) ──
class Demand:
    def __init__(self, conn: sqlite3.Connection):
        self.rows = conn.execute("SELECT keyword, traffic FROM dish_keywords").fetchall()
        self.low = [(k or "").lower() for k, _ in self.rows]
        self.posting: dict[str, list[int]] = defaultdict(list)
        for i, k in enumerate(self.low):
            for w in set(re.findall(r"[a-z]+", k)):
                self.posting[w].append(i)

    def lookup(self, name: str) -> dict:
        nl = name.lower()
        toks = re.findall(r"[a-z]+", nl)
        if not toks:
            return {"keywords": 0, "traffic": 0, "top_keyword": None}
        cands = min((self.posting.get(t, []) for t in toks), key=len)
        n, total, top_kw, top_tr = 0, 0.0, None, -1.0
        for i in cands:
            if nl in self.low[i]:
                tr = float(self.rows[i][1] or 0)
                n += 1
                total += tr
                if tr > top_tr:
                    top_tr, top_kw = tr, self.rows[i][0]
        return {"keywords": n, "traffic": round(total, 1), "top_keyword": top_kw}


# ── the export ────────────────────────────────────────────────────────────────
def export(conn: sqlite3.Connection, chapter: str, vetted_path: str | None,
           review_path: str | None) -> dict:
    cols = ("name", "display_name", "aliases", "description", "identity_card",
            "chapter", "competitiveness_pct", "field_clout", "ethnicity",
            "origin_region", "created_at", "updated_at")
    dishes: dict[str, dict] = {}
    for r in conn.execute(f"SELECT {','.join(cols)} FROM dishes WHERE chapter=? ORDER BY name",
                          (chapter,)):
        d = dict(zip(cols, r))
        d["aliases"] = _loads(d["aliases"], [])
        d["identity_card"] = _loads(d["identity_card"], None)
        d["counts"] = {"curated": 0, "confident": 0, "weak": 0}
        dishes[d["name"]] = d
    if not dishes:
        raise SystemExit(f"no dishes in chapter {chapter!r}")
    chapter_of = dict(conn.execute("SELECT name, chapter FROM dishes"))
    names_idx = dish_match.name_index(conn)

    recipes: list[dict] = []
    holes: dict[str, dict] = {}
    settled_names: Counter = Counter()
    for rid, url, data in conn.execute(
            "SELECT recipe_id, url_normalized, data FROM master_recipes"):
        d = _loads(data, {})
        md = (d.get("_master") or {}).get("dish")
        m = d.get("_match") or {}
        cands = m.get("candidates") or []
        nearest = (cands[0].get("dish") if cands else None) or m.get("dish")
        if md in dishes:
            basis, dish = "curated", md
        elif m.get("confident") and m.get("dish") in dishes:
            basis, dish = "confident", m["dish"]
        elif not m.get("confident") and nearest in dishes:
            basis, dish = "weak", nearest
        else:
            continue
        dishes[dish]["counts"][basis] += 1
        card = d.get("_identity") or {}
        likely = (card.get("likelyDish") or "").strip()
        lf = fold(likely)
        if lf and lf not in names_idx:
            if basis == "weak":
                h = holes.setdefault(lf, {"name": likely, "rows": 0, "leans_on": Counter(),
                                          "settled_rows": 0})
                h["rows"] += 1
                h["leans_on"][dish] += 1
            else:
                settled_names[lf] += 1
        recipes.append({
            "id": rid,
            "title": d.get("name") or "",
            "url": url,
            "identity": {
                "likelyDish": likely,
                "cuisine": card.get("cuisine"),
                "ethnicity": card.get("ethnicity"),
                "primaryIngredients": card.get("primaryIngredients") or [],
                "technique": card.get("technique"),
                "servingForm": card.get("servingForm"),
            },
            "identity_text": compose_identity_text(card),
            "current": {
                "dish": dish,
                "basis": basis,
                "method": m.get("method"),
                "distance": m.get("distance"),
                "confident": bool(m.get("confident")),
                "curated_dish": md,
                "candidates": [{"dish": c.get("dish"), "distance": c.get("distance"),
                                "chapter": chapter_of.get(c.get("dish"))}
                               for c in cands[:3]],
            },
        })
    for lf, h in holes.items():
        h["settled_rows"] = settled_names.get(lf, 0)
        h["leans_on"] = dict(h["leans_on"].most_common())
    holes_out = sorted(holes.values(), key=lambda h: (-h["rows"], h["name"]))

    vetted = [o for o in _loads(open(vetted_path, encoding="utf-8").read(), [])
              if o.get("dish") in dishes] if vetted_path else []
    rejected = [o for o in parse_rejected(review_path) if o["dish"] in dishes] if review_path else []

    dem = Demand(conn)
    demand: dict[str, dict] = {}
    for n, d in dishes.items():
        demand[n] = {**dem.lookup(d["display_name"] or n), "kind": "dish",
                     "competitiveness_pct": d["competitiveness_pct"], "field_clout": d["field_clout"]}
        for a in d["aliases"]:
            demand.setdefault(a, {**dem.lookup(a), "kind": "alias", "dish": n})
    for o in vetted:
        demand.setdefault(o["alias"], {**dem.lookup(o["alias"]), "kind": "alias_vetted", "dish": o["dish"]})
    for h in holes_out:
        demand.setdefault(h["name"], {**dem.lookup(h["name"]), "kind": "hole"})

    meta = {
        "chapter": chapter,
        "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "BCC recipes.db (read-only)",
        "plan": "docs/dish-taxonomy-pilot-soups-stews.md §3",
        "match_bar": dish_match.max_distance(),
        "alias_files": {"vetted": _rel(vetted_path), "review": _rel(review_path)},
        "counts": {
            "dishes": len(dishes),
            "dishes_with_aliases": sum(1 for d in dishes.values() if d["aliases"]),
            "recipes": len(recipes),
            "recipes_curated": sum(d["counts"]["curated"] for d in dishes.values()),
            "recipes_confident": sum(d["counts"]["confident"] for d in dishes.values()),
            "recipes_weak": sum(d["counts"]["weak"] for d in dishes.values()),
            "holes": len(holes_out),
            "aliases_vetted": len(vetted),
            "aliases_vetted_by_tier": dict(Counter(o["tier"] for o in vetted)),
            "aliases_rejected": len(rejected),
        },
    }
    return {"meta": meta, "dishes": list(dishes.values()), "recipes": recipes,
            "holes": holes_out, "aliases_vetted": vetted, "aliases_rejected": rejected,
            "demand": demand}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chapter", required=True, help='e.g. "Soups & Stews"')
    ap.add_argument("--db", default=os.path.join(ROOT, "recipes.db"))
    ap.add_argument("--out", help="output JSON (default docs/research/dish-chapter-export-<slug>-<date>.json)")
    ap.add_argument("--aliases-json", default=_latest("docs/research/dish-alias-vetted-*.json"))
    ap.add_argument("--aliases-review", default=_latest("docs/research/dish-alias-proposals-*.md"))
    a = ap.parse_args(argv)

    out = a.out or os.path.join(ROOT, "docs", "research",
                                f"dish-chapter-export-{_slug(a.chapter)}-{_dt.date.today():%Y-%m-%d}.json")
    uri = "file:" + os.path.abspath(a.db).replace("\\", "/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        doc = export(conn, a.chapter, a.aliases_json, a.aliases_review)
    finally:
        conn.close()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    c = doc["meta"]["counts"]
    print(f"{a.chapter}: {c['dishes']} dishes ({c['dishes_with_aliases']} with aliases); "
          f"recipes {c['recipes']} = curated {c['recipes_curated']} + confident {c['recipes_confident']} "
          f"+ weak {c['recipes_weak']}; holes {c['holes']}; aliases vetted {c['aliases_vetted']} "
          f"{c['aliases_vetted_by_tier']}, rejected {c['aliases_rejected']}")
    print("wrote", out, f"({os.path.getsize(out) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
