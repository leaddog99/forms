"""Weakest-link report — where the dish catalog has holes.

A recipe that resolves its dish only through a LONG nearest-distance is a
recipe the catalog doesn't really cover: it hangs off whichever dish happens
to be least far away ("a loose nearest is still evidence" — but a 0.9 nearest
is barely evidence at all). One such recipe is noise; a CLUSTER of them that
all name the same `likelyDish` on their identity cards is a hole — the dish
they wish existed.

Two sections, both from the same scan:
  - holes:   unconfident rows grouped by folded likelyDish, count >= min_group,
             sorted by count — the actionable "create this dish" list. Groups
             whose likelyDish already IS a catalog dish (name/alias, folded)
             are excluded: those recipes aren't in a hole, they're waiting for
             the name-exact override / next rematch to claim them.
  - weakest: the top-N individual rows by nearest distance — the tail of the
             corpus, for eyeballing what the groups miss.

Read-only; produced by the `dish_gap_report` job (jobs-as-executables), whose
log carries the human-readable table and whose result carries the JSON.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict


def build_report(conn: sqlite3.Connection, *, limit: int = 100,
                 min_group: int = 3, max_dist: float = 0.6) -> dict:
    """Scan master for un-anchored rows and rank the weakness. Rows carrying a
    curated/run `_master.dish` are anchored and skipped — their connection is
    a decision, not a distance."""
    from input.pipeline import dish_match
    names_idx = dish_match.name_index(conn)   # folded name/alias -> canonical

    weak_rows: list[dict] = []
    groups: dict = defaultdict(list)
    scanned = unmatched = 0
    for rid, recipe_id, url, data in conn.execute(
            "SELECT id, recipe_id, url_normalized, data FROM master_recipes"):
        d = json.loads(data)
        if (d.get("_master") or {}).get("dish"):
            continue
        scanned += 1
        m = d.get("_match") or {}
        cand = (m.get("candidates") or [{}])[0]
        dist = cand.get("distance")
        if dist is None:
            unmatched += 1
            continue
        if dist <= max_dist:
            continue                          # confidently covered
        likely = ((d.get("_identity") or {}).get("likelyDish") or "").strip()
        row = {"id": rid, "recipe_id": recipe_id, "url": url,
               "name": d.get("name") or "(no title)",
               "nearest": cand.get("dish"), "distance": dist,
               "likely_dish": likely}
        weak_rows.append(row)
        folded = dish_match._fold_name(likely)
        # A likelyDish that already IS a catalog dish isn't a hole — the
        # name-exact override claims those at the next (re)match.
        if folded and folded not in names_idx:
            groups[folded].append(row)

    weak_rows.sort(key=lambda r: -r["distance"])
    holes = []
    for folded, rows in groups.items():
        if len(rows) < min_group:
            continue
        # Display name = the most common raw spelling in the group.
        spellings = defaultdict(int)
        for r in rows:
            spellings[r["likely_dish"]] += 1
        display = max(spellings, key=spellings.get)
        holes.append({
            "likely_dish": display,
            "count": len(rows),
            "avg_distance": round(sum(r["distance"] for r in rows) / len(rows), 3),
            "nearest_dishes": sorted({r["nearest"] for r in rows if r["nearest"]})[:4],
            "examples": [r["name"] for r in rows[:4]],
        })
    holes.sort(key=lambda h: -h["count"])

    return {
        "scanned_unanchored": scanned,
        "unmatched": unmatched,
        "weak_total": len(weak_rows),
        "max_dist": max_dist,
        "min_group": min_group,
        "holes": holes,
        "weakest": weak_rows[:limit],
    }


def print_report(rep: dict, log=print) -> None:
    """Human-readable rendering into the job log."""
    log(f"[GAPS] {rep['weak_total']} weak links among {rep['scanned_unanchored']} "
        f"un-anchored rows (bar {rep['max_dist']}); {rep['unmatched']} never matched")
    log("")
    log(f"[GAPS] ── HOLES — likelyDish clusters with no catalog dish "
        f"(>= {rep['min_group']} recipes) ──")
    if not rep["holes"]:
        log("[GAPS]   (none — every weak cluster already names a catalog dish)")
    for h in rep["holes"]:
        log(f"[GAPS]   {h['count']:3d}x  {h['likely_dish']}  "
            f"(avg d={h['avg_distance']}, now leaning on: {', '.join(h['nearest_dishes'])})")
        for ex in h["examples"]:
            log(f"[GAPS]         e.g. {ex}")
    log("")
    log(f"[GAPS] ── WEAKEST {len(rep['weakest'])} individual rows ──")
    for r in rep["weakest"]:
        log(f"[GAPS]   d={r['distance']:.3f}  #{r['id']:<6} {r['name'][:58]!r}  "
            f"likely={r['likely_dish'] or '—'}  nearest={r['nearest'] or '—'}")
