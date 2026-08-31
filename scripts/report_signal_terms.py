"""Corpus-wide signal-terms report -> docs/reports/signal-terms.html.

Every distinct term in dishes.cohort_signals (deduped exact strings, alpha
by default), with kind (ingredient/equipment/both), dish count, max lift and
the dish that produced it. Filter box + click-to-sort headers.

Run after a dish_signals sweep:  python scripts/report_signal_terms.py
"""
from __future__ import annotations

import html
import json
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "reports" / "signal-terms.html"


def main():
    conn = sqlite3.connect(ROOT / "recipes.db")
    terms = defaultdict(lambda: {"ing": 0, "eq": 0, "max_lift": 0.0, "max_dish": ""})
    dishes = 0
    for name, sj in conn.execute(
            "SELECT name, cohort_signals FROM dishes WHERE cohort_signals IS NOT NULL"):
        try:
            sig = json.loads(sj)
        except Exception:
            continue
        if not sig.get("cohort_n"):
            continue
        dishes += 1
        for kind, key in (("ing", "ingredients"), ("eq", "equipment")):
            for r in sig.get(key) or []:
                t = (r.get("term") or "").strip().lower()
                if not t:
                    continue
                d = terms[t]
                d[kind] += 1
                lift = float(r.get("lift") or 0)
                if lift > d["max_lift"]:
                    d["max_lift"], d["max_dish"] = lift, name
    conn.close()

    rows = []
    for t in sorted(terms):
        d = terms[t]
        kind = "both" if d["ing"] and d["eq"] else ("ingredient" if d["ing"] else "equipment")
        rows.append(
            f"<tr><td>{html.escape(t)}</td><td class='k {kind[:2]}'>{kind}</td>"
            f"<td class='n' data-v='{d['ing'] + d['eq']}'>{d['ing'] + d['eq']}</td>"
            f"<td class='n' data-v='{d['max_lift']:.1f}'>{d['max_lift']:.1f}×</td>"
            f"<td class='muted'>{html.escape(d['max_dish'])}</td></tr>")

    n = len(rows)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Signal terms — full corpus</title>
<style>
  :root {{ --ink:#2a2622; --muted:#8a8178; --line:#e7e1d8; --bg:#faf7f2; --card:#fff; --accent:#0f6e5c; }}
  body {{ margin:0; font:14px/1.5 -apple-system,'Segoe UI',sans-serif; color:var(--ink); background:var(--bg); }}
  .wrap {{ max-width:900px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-size:1.35rem; margin:0 0 4px; }}
  .sub {{ color:var(--muted); font-size:.85rem; margin-bottom:18px; }}
  input {{ width:100%; padding:9px 12px; font:inherit; border:1px solid var(--line); border-radius:8px; margin-bottom:14px; background:var(--card); }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  th {{ text-align:left; font-size:.72rem; text-transform:uppercase; letter-spacing:.05em; color:var(--muted); padding:8px 10px; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--card); cursor:pointer; user-select:none; white-space:nowrap; }}
  th:hover {{ color:var(--ink); }}
  th .dir {{ font-size:.65rem; margin-left:3px; }}
  td {{ padding:5px 10px; border-bottom:1px solid var(--line); }}
  tr:last-child td {{ border-bottom:0; }}
  td.n {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
  td.k {{ font-size:.75rem; white-space:nowrap; }}
  td.k.in {{ color:var(--accent); }} td.k.eq {{ color:#8a5a2a; }} td.k.bo {{ color:#5a5a8a; }}
  td.muted {{ color:var(--muted); font-size:.8rem; }}
  .count {{ color:var(--muted); font-size:.8rem; margin-bottom:8px; }}
</style></head><body><div class="wrap">
<h1>Signal terms — full corpus</h1>
<div class="sub">Every distinct term in dishes.cohort_signals (method df-lift-v3) · {dishes} dishes ·
{n:,} unique terms · generated {date.today().isoformat()}. “Dishes” = how many dishes rank the term;
“max lift” = its highest lift anywhere, with the dish that produced it. Click a header to sort.</div>
<input id="q" type="search" placeholder="Filter terms… (plain substring)">
<div class="count" id="cnt">{n:,} terms</div>
<table><thead><tr>
  <th data-c="0">Term<span class="dir"></span></th>
  <th data-c="1">Kind<span class="dir"></span></th>
  <th data-c="2" data-num="1">Dishes<span class="dir"></span></th>
  <th data-c="3" data-num="1">Max lift<span class="dir"></span></th>
  <th data-c="4">At<span class="dir"></span></th>
</tr></thead>
<tbody id="tb">
{chr(10).join(rows)}
</tbody></table></div>
<script>
  const q = document.getElementById('q'), tb = document.getElementById('tb'), cnt = document.getElementById('cnt');
  const trs = [...tb.rows];
  q.addEventListener('input', () => {{
    const v = q.value.trim().toLowerCase();
    let shown = 0;
    for (const tr of trs) {{
      const hit = !v || tr.cells[0].textContent.includes(v);
      tr.style.display = hit ? '' : 'none';
      if (hit) shown++;
    }}
    cnt.textContent = shown.toLocaleString() + ' terms' + (v ? ' (filtered)' : '');
  }});
  // Click-to-sort: toggles asc/desc per column; numeric columns sort on
  // data-v; filter visibility is a per-row style, so it survives re-append.
  let sortCol = 0, sortDir = 1;
  document.querySelectorAll('th').forEach(th => th.addEventListener('click', () => {{
    const c = +th.dataset.c, num = !!th.dataset.num;
    sortDir = (sortCol === c) ? -sortDir : (num ? -1 : 1);   // numbers open descending
    sortCol = c;
    trs.sort((a, b) => {{
      const av = num ? +a.cells[c].dataset.v : a.cells[c].textContent.toLowerCase();
      const bv = num ? +b.cells[c].dataset.v : b.cells[c].textContent.toLowerCase();
      return (av < bv ? -1 : av > bv ? 1 : 0) * sortDir;
    }});
    tb.append(...trs);
    document.querySelectorAll('th .dir').forEach(s => s.textContent = '');
    th.querySelector('.dir').textContent = sortDir === 1 ? '▲' : '▼';
  }}));
</script></body></html>"""
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT}: {dishes} dishes, {n} unique terms, {len(page)//1024} KB")


if __name__ == "__main__":
    main()
