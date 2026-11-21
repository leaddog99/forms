"""Place a chapter export's recipes in a knowledge-drawn dish tree and compare with today's dish.

Read-only on the database (never opened): inputs are the tree JSON and the chapter export.
Retrieve-then-judge, the shape the 2026-11-19 verification pass used: embed every leaf and
every recipe's identity text, take the nearest leaves (plus any leaf the card names outright),
and ask Haiku to pick one or NONE. Verdicts are cached per recipe in the results JSON, so a
re-run only pays for rows it has not judged.

    python scripts/classify_chapter_against_tree.py \
        --tree   docs/research/soups-stews-knowledge-tree-2026-11-20.json \
        --export docs/research/dish-chapter-export-soups-stews-2026-11-19.json \
        --out    docs/research/soups-stews-tree-corpus-eval-2026-11-20
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

import llm  # noqa: E402
from input.pipeline import embeddings as emb  # noqa: E402

MODEL = "claude-haiku-4-5"
OPERATION = "dish_tree_classify"
TOP_K = 10
SYSTEM = (
    "You file one recipe in a tree of dishes. A dish is the set of recipes a cook would accept "
    "as the same thing. A change of method or richness (roasted, creamy, slow cooker, pressure "
    "cooker, vegan version) or a minor ingredient swap is still the same dish. A different "
    "defining ingredient, a different form, or a different named tradition is a different dish.\n"
    "You are shown the recipe and a short list of candidate dishes with their definitions. Pick "
    "the ONE candidate that is this recipe's dish, copying its name exactly, or answer NONE when "
    "no candidate is. A wrong dish is worse than NONE: do not pick the nearest relative.\n"
    "When you answer NONE, give the name a cook would use for the dish, and say whether it is a "
    "soup, stew, chowder, chili or similar bowl dish at all (in_chapter)."
)
TOOL = {
    "name": "file_recipe",
    "description": "File the recipe under one candidate dish, or NONE.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pick": {"type": "string", "description": "A candidate's name copied exactly, or NONE."},
            "suggested_name": {"type": "string", "description": "Only when pick is NONE: what a cook calls this dish."},
            "in_chapter": {"type": "boolean", "description": "Is this a soup/stew/chowder/chili-type dish?"},
            "why": {"type": "string", "description": "One short clause."},
        },
        "required": ["pick", "in_chapter", "why"],
    },
}


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def embed_many(texts: list[str]) -> np.ndarray:
    out = []
    client = emb._get_client()
    for i in range(0, len(texts), 200):
        resp = client.embeddings.create(model=emb.EMBED_MODEL, input=texts[i:i + 200])
        out.extend(d.embedding for d in resp.data)
    return np.array(out, dtype="float32")


def judge(recipe: dict, cands: list[dict]) -> dict:
    idn = recipe.get("identity") or {}
    lines = [f"RECIPE: {recipe.get('title') or ''}",
             f"The recipe's own card calls it: {idn.get('likelyDish') or '?'}",
             f"Cuisine: {idn.get('cuisine') or '?'}",
             f"Main ingredients: {', '.join(idn.get('primaryIngredients') or []) or '?'}",
             f"Technique: {idn.get('technique') or '?'}",
             f"Served as: {idn.get('servingForm') or '?'}", "", "CANDIDATES:"]
    for c in cands:
        aka = f" (also: {', '.join(c['aka'])})" if c["aka"] else ""
        lines.append(f"- {c['name']}{aka} [{c['origin']}]: {c['what']}")
    resp = llm.create(operation=OPERATION, model=MODEL, max_tokens=250, temperature=0,
                      system=SYSTEM, messages=[{"role": "user", "content": "\n".join(lines)}],
                      tools=[TOOL], tool_choice={"type": "tool", "name": "file_recipe"})
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use":
            return dict(block.input)
    return {"pick": "NONE", "in_chapter": True, "why": "no tool output"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tree", required=True)
    ap.add_argument("--export", required=True)
    ap.add_argument("--out", required=True, help="path stem; writes <stem>.json and <stem>.md")
    ap.add_argument("--limit", type=int, default=0, help="judge only the first N recipes (trial run)")
    ap.add_argument("--threads", type=int, default=8)
    a = ap.parse_args()
    tree = json.loads(Path(a.tree).read_text(encoding="utf-8"))
    export = json.loads(Path(a.export).read_text(encoding="utf-8"))
    out_json, out_md = Path(a.out + ".json"), Path(a.out + ".md")

    leaves, group_of = [], {}
    for g in tree["groups"]:
        for name, aka, origin, what in g["leaves"]:
            leaves.append({"name": name, "aka": aka, "origin": origin, "what": what})
            group_of[name] = g["title"]
    by_name = {lf["name"]: lf for lf in leaves}
    name_index = {}
    for lf in leaves:
        for label in [lf["name"]] + lf["aka"]:
            name_index.setdefault(fold(label), lf["name"])
    expected = {}                                   # catalog dish -> set of leaves it maps to
    for dish, (outcome, target, _n) in tree["catalog_map"].items():
        expected[dish] = set() if outcome == "out" else set(target.split(" + "))
    in_catalog = set().union(*expected.values())

    recipes = export["recipes"][: a.limit] if a.limit else export["recipes"]
    done = {}
    if out_json.exists():
        done = {r["id"]: r for r in json.loads(out_json.read_text(encoding="utf-8"))["rows"]}
    todo = [r for r in recipes if r["id"] not in done]
    print(f"{len(leaves)} leaves, {len(recipes)} recipes, {len(todo)} to judge", flush=True)

    if todo:
        leaf_vecs = embed_many([f"{lf['name']}. {', '.join(lf['aka'])}. {lf['origin']}. {lf['what']}" for lf in leaves])
        rec_vecs = embed_many([r.get("identity_text") or r.get("title") or "?" for r in todo])
        sims = rec_vecs @ leaf_vecs.T

        def work(i: int) -> dict:
            r = todo[i]
            order = list(np.argsort(-sims[i])[:TOP_K])
            cands = [leaves[j] for j in order]
            named = name_index.get(fold((r.get("identity") or {}).get("likelyDish") or ""))
            if named and all(c["name"] != named for c in cands):
                cands.append(by_name[named])
            try:
                v = judge(r, cands)
            except Exception as ex:                  # one bad call must not lose the run
                return {"id": r["id"], "error": f"{type(ex).__name__}: {ex}"[:200]}
            pick = (v.get("pick") or "").strip()
            if pick not in {c["name"] for c in cands}:
                pick = "NONE" if pick.upper() == "NONE" else name_index.get(fold(pick), "NONE")
            return {"id": r["id"], "title": r.get("title"), "url": r.get("url"),
                    "card": (r.get("identity") or {}).get("likelyDish"),
                    "current": r["current"]["dish"], "basis": r["current"]["basis"],
                    "pick": pick, "suggested": (v.get("suggested_name") or "").strip() if pick == "NONE" else "",
                    "in_chapter": bool(v.get("in_chapter", True)), "why": v.get("why") or "",
                    "top1": cands[0]["name"], "top1_sim": round(float(sims[i][order[0]]), 4),
                    "named": named or ""}

        with ThreadPoolExecutor(max_workers=a.threads) as pool:
            for n, row in enumerate(pool.map(work, range(len(todo))), 1):
                if "error" in row:
                    print("  ERROR", row["id"], row["error"], flush=True)
                else:
                    done[row["id"]] = row
                if n % 100 == 0 or n == len(todo):
                    print(f"  judged {n}/{len(todo)}", flush=True)
                    out_json.write_text(json.dumps({"rows": list(done.values())}, ensure_ascii=False, indent=0), encoding="utf-8")

    rows = [done[r["id"]] for r in recipes if r["id"] in done]
    for r in rows:
        exp = expected.get(r["current"], set())
        if r["pick"] == "NONE":
            r["verdict"] = "none"
        elif r["basis"] == "weak":
            r["verdict"] = "placed"
        else:
            r["verdict"] = "agree" if r["pick"] in exp else "moved"
    out_json.write_text(json.dumps({"rows": rows}, ensure_ascii=False, indent=0), encoding="utf-8")

    # ---- report -------------------------------------------------------------
    n = len(rows)
    by_basis = defaultdict(Counter)
    for r in rows:
        by_basis[r["basis"]][r["verdict"]] += 1
    per_leaf = Counter(r["pick"] for r in rows if r["pick"] != "NONE")
    landed_new = sum(c for lf, c in per_leaf.items() if lf not in in_catalog)
    md = [f"# Soups & Stews: our recipes placed in the knowledge-drawn tree ({n} recipes)", "",
          f"*{MODEL} as judge over the embedding's {TOP_K} nearest leaves (plus any leaf the card names). "
          "Not graded by a person. Nothing in the database was changed.*", "",
          "## Result by how the recipe is filed today", "",
          "| Filed today | Recipes | Same dish | Different dish | Newly placed | No dish in the tree |",
          "|---|---|---|---|---|---|"]
    for basis, label in (("curated", "Curated / run-assigned"), ("confident", "Confident nearest match"), ("weak", "Weak leaner (no real home)")):
        c = by_basis[basis]
        t = sum(c.values()) or 1
        def pct(k, c=c, t=t):
            return f"{c[k]} ({100 * c[k] / t:.0f}%)" if c[k] else "-"
        md.append(f"| {label} | {sum(c.values())} | {pct('agree')} | {pct('moved')} | {pct('placed')} | {pct('none')} |")
    placed_total = sum(per_leaf.values())
    md += ["", f"Placed in a leaf: {placed_total} of {n} ({100 * placed_total / n:.0f}%). "
               f"Of those, {landed_new} landed in a leaf our catalog does not have. "
               f"Leaves that received at least one recipe: {len(per_leaf)} of {len(leaves)}; empty: {len(leaves) - len(per_leaf)}.", "",
           "## Leaves our catalog lacks that received recipes", "",
           "| Leaf | Group | Recipes | Filed today under |", "|---|---|---|---|"]
    for lf, c in per_leaf.most_common():
        if lf in in_catalog:
            continue
        src = Counter(r["current"] for r in rows if r["pick"] == lf)
        md.append(f"| {lf} | {group_of[lf]} | {c} | {', '.join(f'{d} {k}' for d, k in src.most_common(3))} |")
    md += ["", "## Moves: filed with confidence today, judged a different dish", "",
           "| Today | Tree leaf | Recipes | Example |", "|---|---|---|---|"]
    moves = defaultdict(list)
    for r in rows:
        if r["verdict"] == "moved":
            moves[(r["current"], r["pick"])].append(r)
    for (cur, lf), rs in sorted(moves.items(), key=lambda kv: -len(kv[1])):
        md.append(f"| {cur} | {lf} | {len(rs)} | {rs[0]['title']} |")
    md += ["", "## No dish in the tree", "",
           "| What the judge called it | In chapter? | Recipes | Filed today under | Example |", "|---|---|---|---|---|"]
    nones = defaultdict(list)
    for r in rows:
        if r["verdict"] == "none":
            nones[(fold(r["suggested"]) or "?", r["in_chapter"])].append(r)
    for (_k, inch), rs in sorted(nones.items(), key=lambda kv: (-len(kv[1]), kv[0][0])):
        src = Counter(r["current"] for r in rs)
        md.append(f"| {rs[0]['suggested'] or '?'} | {'yes' if inch else 'no'} | {len(rs)} | "
                  f"{', '.join(f'{d} {k}' for d, k in src.most_common(2))} | {rs[0]['title']} |")
    md += ["", "## Recipes per leaf (all leaves that received any)", "", "| Leaf | Recipes |", "|---|---|"]
    md += [f"| {lf} | {c} |" for lf, c in per_leaf.most_common()]
    out_md.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({b: dict(c) for b, c in by_basis.items()}))
    print(f"placed {placed_total}/{n}; in non-catalog leaves {landed_new}; leaves used {len(per_leaf)}/{len(leaves)}")
    print(f"wrote {out_json} and {out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
