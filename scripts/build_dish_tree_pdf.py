"""Render a knowledge-drawn dish tree as a reading PDF, reconciled against a chapter export.

Read-only on the database (it never opens it): inputs are two JSON files.

    python scripts/build_dish_tree_pdf.py \
        --tree   docs/research/soups-stews-knowledge-tree-2026-11-20.json \
        --export docs/research/dish-chapter-export-soups-stews-2026-11-19.json \
        --out    docs/research/soups-stews-knowledge-tree-2026-11-20.pdf

The tree file is the taxonomy (groups -> leaves) plus two hand-made maps: where each
catalog dish lands and where each repeated hole name lands. The script checks both maps
are complete against the export before it prints anything, writes the HTML beside the
PDF, and prints the PDF through headless Edge/Chrome.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)
OUTCOME_WORDS = {
    "same": "Stays as it is",
    "renamed": "Same dish, better name",
    "merged": "Merged into another dish",
    "split": "Split into several dishes",
    "alias": "Becomes another name for",
    "out": "Leaves this chapter",
}
HOLE_WORDS = {
    "leaf": "Becomes a dish",
    "alias": "Another name for",
    "variant": "A variant of",
    "out": "Not in this chapter",
}


def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def eval_section(rows: list[dict], leaves: dict, in_catalog: set, n_part: int) -> str:
    """The corpus placed in the tree (rows from classify_chapter_against_tree.py)."""
    from collections import Counter, defaultdict
    n = len(rows)
    by = defaultdict(Counter)
    for r in rows:
        by[r["basis"]][r["verdict"]] += 1
    per_leaf = Counter(r["pick"] for r in rows if r["pick"] != "NONE")
    placed = sum(per_leaf.values())
    new_homes = sum(k for lf, k in per_leaf.items() if lf not in in_catalog)
    settled = sum(by[b][v] for b in ("curated", "confident") for v in by[b])
    agree = by["curated"]["agree"] + by["confident"]["agree"]

    def cell(c, k):
        t = sum(c.values()) or 1
        return f"{c[k]} <span class='aka'>({100 * c[k] / t:.0f}%)</span>" if c[k] else "&ndash;"
    out = [f"""<h2 class="newpage">Part {n_part}. Our {n} recipes filed in this list</h2>
<p>Every recipe in the chapter export was shown to a model (Claude Haiku) together with the ten dishes on the list nearest to it, and the model picked one dish or said &ldquo;none of these&rdquo;. The answer was then compared with where the recipe is filed today. No person has graded these answers, and nothing was re-filed in the database.</p>
<table class="nums"><tr>
<td><div class="n">{placed}</div>of {n} recipes found<br>a dish on the list ({100 * placed / n:.0f}%)</td>
<td><div class="n">{agree}</div>of {settled} confidently filed recipes<br>stay on the same dish ({100 * agree / max(settled, 1):.0f}%)</td>
<td><div class="n">{by['weak']['placed']}</div>of {sum(by['weak'].values())} recipes with no real<br>home today now have one</td>
<td><div class="n">{new_homes}</div>recipes land on dishes<br>our catalog lacks</td>
</tr></table>
<table><tr><th style='width:30%'>How the recipe is filed today</th><th>Recipes</th><th>Same dish</th><th>A different dish</th><th>Newly placed</th><th>No dish fits</th></tr>"""]
    for basis, label in (("curated", "Assigned by the curator or a dish run"),
                         ("confident", "Matched to its nearest dish with confidence"),
                         ("weak", "Leaning on a dish without really matching")):
        c = by[basis]
        out.append(f"<tr><td>{label}</td><td>{sum(c.values())}</td><td>{cell(c, 'agree')}</td>"
                   f"<td>{cell(c, 'moved')}</td><td>{cell(c, 'placed')}</td><td>{cell(c, 'none')}</td></tr>")
    out.append("</table><p class='note'><i>Same dish</i> = the model chose the dish our catalog dish maps to in Part 2. "
               "<i>A different dish</i> = it chose another dish on the list. <i>Newly placed</i> = the recipe had no real home and now has one. "
               "<i>No dish fits</i> = none of the ten candidates was this recipe's dish.</p>")

    out.append("<h3>Dishes our catalog lacks that our recipes fill</h3>"
               "<table><tr><th style='width:30%'>Dish on the list</th><th style='width:9%'>Recipes</th><th>Where those recipes are filed today</th></tr>")
    shown = 0
    for lf, k in per_leaf.most_common():
        if lf in in_catalog or k < 2:
            continue
        src = Counter(r["current"] for r in rows if r["pick"] == lf)
        out.append(f"<tr><td><b>{e(lf)}</b></td><td>{k}</td><td class='aka'>{e(', '.join(f'{d} ({c})' for d, c in src.most_common(3)))}</td></tr>")
        shown += 1
    singles = sum(1 for lf, k in per_leaf.items() if lf not in in_catalog and k == 1)
    out.append(f"</table><p class='note'>{shown} dishes with two or more recipes shown; {singles} more received one recipe each.</p>")

    moves = defaultdict(list)
    for r in rows:
        if r["verdict"] == "moved":
            moves[(r["current"], r["pick"])].append(r)
    out.append("<h3>Recipes filed with confidence today that the model puts on a different dish</h3>"
               "<table><tr><th style='width:26%'>Filed today under</th><th style='width:26%'>Model says</th><th style='width:9%'>Recipes</th><th>Example recipe</th></tr>")
    top = sorted(moves.items(), key=lambda kv: -len(kv[1]))
    for (cur, lf), rs in top[:28]:
        out.append(f"<tr><td>{e(cur)}</td><td><b>{e(lf)}</b></td><td>{len(rs)}</td><td class='aka'>{e(rs[0]['title'])}</td></tr>")
    rest = sum(len(rs) for _k, rs in top[28:])
    out.append(f"</table><p class='note'>{sum(len(rs) for rs in moves.values())} recipes in {len(moves)} pairs; "
               f"the {rest} not shown are in the full report.</p>")

    def fold_(s):
        return fold(s) or "?"
    nones = defaultdict(list)
    for r in rows:
        if r["verdict"] == "none":
            nones[(fold_(r["suggested"]), r["in_chapter"])].append(r)
    n_none = sum(len(v) for v in nones.values())
    n_out = sum(len(v) for (k, inch), v in nones.items() if not inch)
    out.append(f"<h3>Recipes no dish on the list fits ({n_none})</h3>"
               f"<p>{n_out} of these the model says are not soups or stews at all, so they are misfiled in this chapter today. "
               f"The other {n_none - n_out} are soups or stews the list does not have: gaps in the list.</p>"
               "<table><tr><th style='width:28%'>What the model calls it</th><th style='width:12%'>A soup or stew?</th><th style='width:9%'>Recipes</th><th style='width:22%'>Filed today under</th><th>Example recipe</th></tr>")
    topn = sorted(nones.items(), key=lambda kv: (-len(kv[1]), kv[0][0]))
    for (_k, inch), rs in topn[:26]:
        src = Counter(r["current"] for r in rs)
        out.append(f"<tr><td><b>{e(rs[0]['suggested'] or '?')}</b></td><td>{'yes' if inch else 'no'}</td><td>{len(rs)}</td>"
                   f"<td class='aka'>{e(', '.join(d for d, _c in src.most_common(2)))}</td><td class='aka'>{e(rs[0]['title'])}</td></tr>")
    out.append(f"</table><p class='note'>{len(nones)} distinct names in all; the full list is in "
               "docs/research/soups-stews-tree-corpus-eval-2026-11-20.md.</p>")
    used = len(per_leaf)
    out.append(f"<p>Of the {len(leaves)} dishes on the list, {used} received at least one of our recipes and {len(leaves) - used} received none. "
               "The empty ones are the dishes we have not collected.</p>")
    return "\n".join(out)


def build(tree: dict, export: dict, evalrows: list[dict] | None = None) -> tuple[str, dict]:
    landed = None
    if evalrows is not None:
        landed = {}
        for r in evalrows:
            if r["pick"] != "NONE":
                landed[r["pick"]] = landed.get(r["pick"], 0) + 1
    leaves = {}                                   # name -> {group, aka, origin, what}
    for g in tree["groups"]:
        for name, aka, origin, what in g["leaves"]:
            if name in leaves:
                raise SystemExit(f"duplicate leaf: {name}")
            leaves[name] = {"group": g["title"], "aka": aka, "origin": origin, "what": what,
                            "catalog": [], "corpus_rows": 0, "corpus_names": []}

    dishes = {d["name"]: d for d in export["dishes"]}
    cmap, hmap = tree["catalog_map"], tree["hole_map"]

    # --- completeness checks -------------------------------------------------
    missing = sorted(set(dishes) - set(cmap))
    extra = sorted(set(cmap) - set(dishes))
    if missing or extra:
        raise SystemExit(f"catalog_map mismatch. missing={missing} extra={extra}")
    for dish, (outcome, target, _note) in cmap.items():
        if outcome not in OUTCOME_WORDS:
            raise SystemExit(f"{dish}: unknown outcome {outcome}")
        if outcome == "out":
            continue
        for t in target.split(" + "):
            if t not in leaves:
                raise SystemExit(f"{dish}: target leaf not in tree: {t}")
            leaves[t]["catalog"].append((dish, outcome))
    repeated = [h for h in export["holes"] if h["rows"] >= 2]
    missing_h = sorted(h["name"] for h in repeated if h["name"] not in hmap)
    if missing_h:
        raise SystemExit(f"hole_map is missing repeated hole names: {missing_h}")
    for hname, (kind, target) in hmap.items():
        if kind != "out" and target not in leaves:
            raise SystemExit(f"hole {hname}: target leaf not in tree: {target}")

    # --- corpus evidence per leaf: every hole name (all 279), by exact folded name
    index = {}
    for name, lf in leaves.items():
        for label in [name] + lf["aka"]:
            index.setdefault(fold(label), name)
    hole_rows = {h["name"]: h["rows"] for h in export["holes"]}
    claimed = set()
    for hname, rows in hole_rows.items():
        target = None
        if hname in hmap:
            kind, t = hmap[hname]
            target = None if kind == "out" else t
        else:
            target = index.get(fold(hname))
        if target:
            leaves[target]["corpus_rows"] += rows
            leaves[target]["corpus_names"].append(hname)
            claimed.add(hname)

    def status(lf):
        if lf["catalog"]:
            return "cat"
        return "corpus" if lf["corpus_rows"] else "new"

    counts = {"cat": 0, "corpus": 0, "new": 0}
    for lf in leaves.values():
        counts[status(lf)] += 1
    outcome_counts = {}
    for _d, (o, _t, _n) in cmap.items():
        outcome_counts[o] = outcome_counts.get(o, 0) + 1
    stats = {"leaves": len(leaves), "groups": len(tree["groups"]), **counts,
             "outcomes": outcome_counts, "holes_total": len(hole_rows),
             "holes_claimed": len(claimed), "holes_repeated": len(repeated)}

    # --- HTML ---------------------------------------------------------------
    pill = {"cat": '<span class="pill cat">IN CATALOG</span>',
            "corpus": '<span class="pill corpus">RECIPES, NO DISH</span>',
            "new": '<span class="pill new">NEW</span>'}
    m = tree["meta"]
    c = export["meta"]["counts"]
    out = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{e(m['title'])}</title>
<style>
@page {{ size: Letter; margin: 0.6in 0.55in; }}
* {{ box-sizing: border-box; }}
body {{ font-family: "Segoe UI", Arial, sans-serif; font-size: 9.6pt; line-height: 1.38; color: #1c1c1c; margin: 0; }}
h1 {{ font-size: 21pt; margin: 0 0 4pt; }}
h2 {{ font-size: 14pt; margin: 22pt 0 4pt; padding-bottom: 3pt; border-bottom: 1.4pt solid #1c1c1c; break-after: avoid; }}
h3 {{ font-size: 11.5pt; margin: 16pt 0 1pt; break-after: avoid; }}
p {{ margin: 0 0 7pt; }}
.sub {{ color: #555; margin-bottom: 12pt; }}
.binds {{ color: #555; font-style: italic; margin: 0 0 4pt; break-after: avoid; }}
table {{ border-collapse: collapse; width: 100%; margin: 2pt 0 4pt; }}
th {{ text-align: left; font-size: 8pt; text-transform: uppercase; letter-spacing: .04em; color: #555; border-bottom: 1pt solid #888; padding: 3pt 5pt; }}
td {{ vertical-align: top; padding: 3.5pt 5pt; border-bottom: .5pt solid #d8d8d8; }}
tr {{ break-inside: avoid; }}
.aka {{ color: #555; font-size: 8.4pt; }}
.pill {{ display: inline-block; font-size: 6.8pt; font-weight: 700; letter-spacing: .04em; padding: 1pt 4.5pt; border-radius: 7pt; white-space: nowrap; }}
.pill.cat {{ background: #1f5f3a; color: #fff; }}
.pill.corpus {{ background: #f1c95b; color: #3a2c00; }}
.pill.new {{ background: #fff; color: #1d4f91; border: 1pt solid #1d4f91; }}
.legend td {{ border: none; padding: 3pt 5pt; }}
.box {{ border: 1pt solid #999; border-radius: 4pt; padding: 8pt 10pt; margin: 8pt 0 10pt; break-inside: avoid; }}
.nums td {{ border: none; padding: 1pt 12pt 1pt 0; }}
.nums .n {{ font-size: 17pt; font-weight: 700; }}
.note {{ color: #555; font-size: 8.6pt; }}
.newpage {{ break-before: page; }}
</style></head><body>
<h1>{e(m['title'])}</h1>
<p class="sub">Drafted {e(m['drafted'])} for the curator. A reading draft, not an approved taxonomy.</p>

<h2 style="margin-top:6pt">What this document is</h2>
<p>This is the list of soups and stews a knowledgeable cook would expect to find, across cuisines, written from culinary knowledge. It was <b>not</b> built from our recipe data. After the list was written, our data was laid against it, so that every dish on the list shows whether we already have it.</p>
<p>It answers the complaint that the earlier file only contained the dishes we already had. That earlier file was an export of our own data and nothing else.</p>

<div class="box">
<p><b>How to read a row.</b> Each row is one <b>dish</b>: the set of recipes a cook would accept as the same thing. A change of method or richness (roasted, creamy, slow cooker) is a variant of that dish, not a new one.</p>
<table class="legend">
<tr><td style="width:1.35in">{pill['cat']}</td><td>We already have this dish in the catalog. The last column gives the catalog's name for it and how many of our recipes are filed there.</td></tr>
<tr><td>{pill['corpus']}</td><td>We have no such dish, but recipes we hold call themselves this. Today they are filed under some other dish. The last column says how many.</td></tr>
<tr><td>{pill['new']}</td><td>Comes from culinary knowledge only. Nothing in our data mentions it. These are the gaps in what we have collected.</td></tr>
</table>
<p style="margin:5pt 0 0"><b>Columns.</b> <i>Dish</i> is the proposed name, with other names it goes by in small type. <i>Origin</i> is the cuisine. <i>What it is</i> is one line that separates it from its neighbours. <i>In our data</i> is what we hold today.</p>
</div>

<table class="nums"><tr>
<td><div class="n">{stats['leaves']}</div>dishes on the list,<br>in {stats['groups']} groups</td>
<td><div class="n">{counts['cat']}</div>already in<br>our catalog</td>
<td><div class="n">{counts['corpus']}</div>named by our recipes,<br>no dish yet</td>
<td><div class="n">{counts['new']}</div>new: from<br>knowledge only</td>
</tr></table>
<p class="note">Our side, from the export of 2026-11-19: {c['dishes']} catalog dishes in this chapter, {c['recipes']} recipes filed under or leaning on them, and {c['holes']} names that recipes use for themselves which are not catalog dishes.</p>

<h2 class="newpage">Part 1. The list, group by group</h2>
"""]
    for g in tree["groups"]:
        out.append(f"<h3>{e(g['title'])}</h3><p class='binds'>{e(g['binds'])}</p>")
        out.append("<table><tr><th style='width:23%'>Dish</th><th style='width:15%'>Origin</th>"
                   "<th style='width:33%'>What it is</th><th style='width:29%'>In our data</th></tr>")
        for name, aka, origin, what in g["leaves"]:
            lf = leaves[name]
            st = status(lf)
            if st == "cat":
                bits = []
                for dish, outcome in lf["catalog"]:
                    k = dishes[dish]["counts"]
                    n = k["curated"] + k["confident"]
                    label = "as itself" if dish == name else f"as &ldquo;{e(dish)}&rdquo;"
                    bits.append(f"{label}, {n} recipes")
                data = "; ".join(bits)
                if lf["corpus_rows"]:
                    data += f". Also {lf['corpus_rows']} misfiled under other dishes"
            elif st == "corpus":
                r = lf["corpus_rows"]
                data = f"{r} recipe{'s' if r != 1 else ''} call{'' if r != 1 else 's'} {'themselves' if r != 1 else 'itself'} this"
            else:
                data = "nothing"
            if landed is not None:
                k = landed.get(name, 0)
                data += f"<br><b>{k} land here</b> when our recipes are filed in this list" if k else "<br>none land here"
            akas = f"<br><span class='aka'>{e(', '.join(aka))}</span>" if aka else ""
            out.append(f"<tr><td><b>{e(name)}</b>{akas}</td><td>{e(origin)}</td><td>{e(what)}</td>"
                       f"<td>{pill[st]}<br><span class='aka'>{data}</span></td></tr>")
        out.append("</table>")

    oc = outcome_counts
    out.append(f"""<h2 class="newpage">Part 2. Our {c['dishes']} catalog dishes: where each one lands</h2>
<p>Every dish we have today, and what the list above does with it. <i>Recipes</i> is the number filed there with confidence; <i>weak</i> is the number that lean on the dish without really matching it.</p>
<p class="note">Stays as it is: {oc.get('same', 0)}. Better name: {oc.get('renamed', 0)}. Merged: {oc.get('merged', 0)}. Split: {oc.get('split', 0)}. Becomes another name: {oc.get('alias', 0)}. Leaves the chapter: {oc.get('out', 0)}.</p>
<table><tr><th style='width:24%'>Catalog dish today</th><th style='width:9%'>Recipes<br>/ weak</th><th style='width:18%'>What happens</th><th style='width:20%'>Lands on</th><th style='width:29%'>Why</th></tr>""")
    for dish in sorted(dishes, key=fold):
        outcome, target, note = cmap[dish]
        k = dishes[dish]["counts"]
        strong = "" if outcome == "same" else " style='font-weight:700'"
        out.append(f"<tr><td>{e(dish)}</td><td>{k['curated'] + k['confident']} / {k['weak']}</td>"
                   f"<td{strong}>{e(OUTCOME_WORDS[outcome])}</td><td>{e(target)}</td><td class='aka'>{e(note)}</td></tr>")
    out.append("</table>")

    out.append(f"""<h2>Part 3. Names our recipes use that are not catalog dishes</h2>
<p>The export found {stats['holes_total']} such names. The {stats['holes_repeated']} below are the ones used by two or more recipes; each was placed by hand. Of all {stats['holes_total']}, {stats['holes_claimed']} match a dish on the list by name. The rest are single recipes whose names were not matched; most are one-off variants, and they were not individually reviewed.</p>
<table><tr><th style='width:30%'>Name recipes use</th><th style='width:9%'>Recipes</th><th style='width:26%'>Filed today under</th><th style='width:35%'>On the list</th></tr>""")
    for h in sorted(repeated, key=lambda h: (-h["rows"], fold(h["name"]))):
        kind, target = hmap[h["name"]]
        out.append(f"<tr><td>{e(h['name'])}</td><td>{h['rows']}</td><td class='aka'>{e(', '.join(h['leans_on']))}</td>"
                   f"<td>{e(HOLE_WORDS[kind])} <b>{e(target)}</b></td></tr>")
    out.append("</table>")

    out.append("""<h2>Part 4. Deliberately left out of this chapter</h2>
<table><tr><th style='width:24%'>Kind of dish</th><th style='width:38%'>Examples</th><th style='width:38%'>Where it belongs</th></tr>""")
    for kind, ex, why in tree["excluded"]:
        out.append(f"<tr><td><b>{e(kind)}</b></td><td>{e(ex)}</td><td>{e(why)}</td></tr>")
    out.append("</table>")

    if evalrows is not None:
        in_cat = {name for name, lf in leaves.items() if lf["catalog"]}
        out.append(eval_section(evalrows, leaves, in_cat, 5))
    out.append(f"""<h2>Part {6 if evalrows is not None else 5}. Calls that are yours to make</h2>
<table><tr><th style='width:30%'>Question</th><th>What the draft does, and the alternative</th></tr>
<tr><td><b>Do noodle soups stay here?</b></td><td>The draft keeps ramen, pho and the rest as one group in this chapter. The alternative is a separate Noodle Soups tree; 23 dishes would move.</td></tr>
<tr><td><b>Is Turkey Chili its own dish?</b></td><td>Kept, because cooks search for it by name. Strictly it is Chili con Carne on a different meat, which the rule calls a variant.</td></tr>
<tr><td><b>Is Shrimp and Corn Chowder its own dish?</b></td><td>Kept. The alternative is a variant of Corn Chowder.</td></tr>
<tr><td><b>Shrimp Gumbo or Seafood Gumbo?</b></td><td>The draft folds Shrimp Gumbo into Seafood Gumbo and adds Chicken and Sausage Gumbo beside it. The alias check on 11-19 called them siblings instead.</td></tr>
<tr><td><b>Pumpkin and Butternut Squash</b></td><td>Two dishes in the draft. Many recipes are interchangeable; one dish with two names is defensible.</td></tr>
<tr><td><b>Thai Red Curry leaves</b></td><td>Moved to a Curries tree with Massaman. Its 19 recipes and 22 weak leaners need that tree to exist before they have a home.</td></tr>
<tr><td><b>Étouffée</b></td><td>Kept under Gumbo and Louisiana although it is served over rice.</td></tr>
</table>

<h2>What this draft is not</h2>
<p>{e(m['by'])} It is one pass by one author. The definitions are short and from memory, and a few regional attributions are simplified. I had already seen our catalog's dish names before writing the list, so it was written from knowledge by discipline, not in isolation. The list has not been through DPI's generate, critique and revise loop, and no recipe has been re-filed in the database. Nothing in the database was changed.</p>
<p class="note">Sources: {e(m['evidence'])} (our data) and docs/research/soups-stews-knowledge-tree-2026-11-20.json (the list). Built by scripts/build_dish_tree_pdf.py.</p>
</body></html>""")
    return "\n".join(out), stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tree", required=True)
    ap.add_argument("--export", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--eval", help="results JSON from classify_chapter_against_tree.py; adds the corpus section")
    a = ap.parse_args()
    tree = json.loads(Path(a.tree).read_text(encoding="utf-8"))
    export = json.loads(Path(a.export).read_text(encoding="utf-8"))
    evalrows = json.loads(Path(a.eval).read_text(encoding="utf-8"))["rows"] if a.eval else None
    page, stats = build(tree, export, evalrows)
    pdf = Path(a.out).resolve()
    htm = pdf.with_suffix(".html")
    htm.write_text(page, encoding="utf-8")
    browser = next((b for b in BROWSERS if Path(b).exists()), None)
    if not browser:
        print(f"no Edge/Chrome found; HTML written to {htm}", file=sys.stderr)
        return 1
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", htm.as_uri()], check=True, timeout=120,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(json.dumps(stats))
    print(f"wrote {pdf} ({pdf.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
