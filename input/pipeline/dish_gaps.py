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

NIGHTLY NOTICE (curator 2026-11-19: "the nightly run should notify us if a
recipe does not meet a minimal threshold for matching, and suggest the dishes
that might fit that aren't in the dish list"). The same scan, given `since`,
also lists the rows whose verdict was written in that window and fell short of
the bar - the recipes that arrived or were re-scored and found no home - and
`digest()` turns that plus the holes into one email. The scheduled job sends it
only when the window has such rows; a quiet night sends nothing.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict


def build_report(conn: sqlite3.Connection, *, limit: int = 100,
                 min_group: int = 3, max_dist: float = 0.6,
                 since: str = "", tables=("master_recipes", "recipes")) -> dict:
    """Scan the un-anchored rows of `tables` and rank the weakness. Rows carrying
    a curated/run `_master.dish` are anchored and skipped — their connection is
    a decision, not a distance. `since` (ISO instant) adds `new_weak`: the weak
    rows whose `_match.matched_at` falls in the window — the save path stamps it
    on capture and the rematch only on a changed verdict, so this is exactly
    "arrived or re-scored since the last report, and found no home"."""
    from input.pipeline import dish_match
    names_idx = dish_match.name_index(conn)   # folded name/alias -> canonical

    weak_rows: list[dict] = []
    new_weak: list[dict] = []
    groups: dict = defaultdict(list)
    scanned = unmatched = 0
    for table in tables:
        for rid, recipe_id, url, data in conn.execute(
                f"SELECT id, recipe_id, url_normalized, data FROM {table}"):
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
            folded = dish_match._fold_name(likely)
            row = {"table": table, "id": rid, "recipe_id": recipe_id, "url": url,
                   "name": d.get("name") or "(no title)",
                   "nearest": cand.get("dish"), "distance": dist,
                   "likely_dish": likely,
                   # the hole this row would fill, if its likelyDish is not a catalog dish
                   "hole": folded if (folded and folded not in names_idx) else ""}
            weak_rows.append(row)
            if since and (m.get("matched_at") or "") >= since:
                new_weak.append(row)
            # A likelyDish that already IS a catalog dish isn't a hole — the
            # name-exact override claims those at the next (re)match.
            if row["hole"]:
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
            "folded": folded,
            "count": len(rows),
            "new_count": sum(1 for r in rows if r in new_weak),
            "avg_distance": round(sum(r["distance"] for r in rows) / len(rows), 3),
            "nearest_dishes": sorted({r["nearest"] for r in rows if r["nearest"]})[:4],
            "examples": [r["name"] for r in rows[:4]],
        })
    holes.sort(key=lambda h: -h["count"])
    new_weak.sort(key=lambda r: -r["distance"])

    return {
        "scanned_unanchored": scanned,
        "unmatched": unmatched,
        "weak_total": len(weak_rows),
        "max_dist": max_dist,
        "min_group": min_group,
        "since": since,
        "new_weak": new_weak,
        "holes": holes,
        "weakest": weak_rows[:limit],
    }


def digest(rep: dict, *, max_rows: int = 40) -> tuple:
    """(subject, body) for the curator: the recipes that fell short of the bar in
    the window, each with the guess it is leaning on and the dish it names, then
    the suggested dishes - likelyDish clusters with no catalog dish - the newly
    weak rows would fill, then the largest standing holes. The full lists are
    in the job log; the mail is what a person reads over coffee."""
    new = rep.get("new_weak") or []
    holes = rep.get("holes") or []
    grown = [h for h in holes if h.get("new_count")]
    subject = (f"Dish match: {len(new)} recipe(s) below the bar"
               + (f", {len(grown)} suggested dish(es)" if grown else ""))
    lines = [f"Since {rep.get('since') or 'the last report'}: {len(new)} recipe(s) were matched "
             f"and no catalog dish came within {rep['max_dist']} (cosine-distance bar). Each is",
             "filed under its NEAREST guess until a dish exists for it. 'names' is the dish the",
             "recipe's own identity card calls it; '+' marks a name that is not in the catalog.", ""]
    for r in new[:max_rows]:
        tag = "+" if r.get("hole") else " "
        lines.append(f"- {r['table']} {r['id']} {r['name']!r}: nearest {r['nearest']!r} "
                     f"(d={r['distance']:.2f}), names {tag}{r['likely_dish'] or '-'}")
    if len(new) > max_rows:
        lines.append(f"  ... {len(new) - max_rows} more in the job log")
    if grown:
        lines += ["", "SUGGESTED DISHES - not in the catalog, named by recipes above (and how many",
                  "weak recipes in all would move there once created):"]
        for h in sorted(grown, key=lambda h: (-h["new_count"], -h["count"])):
            lines.append(f"- {h['likely_dish']}: {h['new_count']} new, {h['count']} in all "
                         f"(avg d={h['avg_distance']}; leaning on {', '.join(h['nearest_dishes'])})")
    if holes:
        lines += ["", f"LARGEST STANDING HOLES ({len(holes)} clusters of >= {rep['min_group']}; "
                      f"{rep['weak_total']} weak recipes in all):"]
        for h in holes[:10]:
            lines.append(f"- {h['likely_dish']}: {h['count']} recipes, leaning on "
                         f"{', '.join(h['nearest_dishes'])}")
        lines += ["", "Create a dish from the a/c/d Dishes page (the gap report has a + per hole);",
                  "the rematch that follows re-aims these rows automatically."]
    return subject, "\n".join(lines)


def digest_html(rep: dict, *, max_rows: int = 40) -> str:
    """The same notice as `digest`, as tables (input.pipeline.alerts helpers)."""
    from input.pipeline.alerts import Raw, html_page, html_table, pill
    from html import escape
    new = rep.get("new_weak") or []
    holes = rep.get("holes") or []
    grown = sorted((h for h in holes if h.get("new_count")),
                   key=lambda h: (-h["new_count"], -h["count"]))

    def recipe_cell(r):
        url = (r.get("url") or "").strip()
        name = escape(r["name"])
        if url:
            if not url.startswith("http"):
                url = "https://" + url
            name = f'<a href="{escape(url, quote=True)}" style="color:#1d4ed8">{name}</a>'
        return Raw(f'{name}<br><span style="color:#6b7280;font-size:12px">'
                   f'{"cookbook" if r["table"] == "recipes" else "library"} #{r["id"]}</span>')

    def names_cell(r):
        if not r.get("likely_dish"):
            return Raw('<span style="color:#9ca3af">not stated</span>')
        return Raw(escape(r["likely_dish"]) + (" " + pill("not in catalogue") if r.get("hole") else ""))

    sections = []
    rows = [[recipe_cell(r), names_cell(r), r["nearest"] or "", f"{r['distance']:.2f}"]
            for r in new[:max_rows]]
    note = (f"A distance above {rep['max_dist']} means no dish in the catalogue is a real fit. "
            "The recipe is filed under its nearest guess until a dish exists for it.")
    if len(new) > max_rows:
        note += f" Showing the {max_rows} furthest off; the other {len(new) - max_rows} are in the job log."
    sections.append((f"Recipes with no good dish ({len(new)})", note,
                     html_table(["Recipe", "The recipe calls itself", "Filed under (nearest guess)",
                                 "Distance"], rows, right=(3,))))
    if grown:
        sections.append((
            f"Suggested new dishes ({len(grown)})",
            "Dishes the recipes above name that the catalogue does not have. "
            "Creating one moves every waiting recipe to it at the next rematch.",
            html_table(["Dish to create", "New this time", "Waiting in all", "Now filed under"],
                       [[h["likely_dish"], h["new_count"], h["count"], ", ".join(h["nearest_dishes"])]
                        for h in grown], right=(1, 2))))
    if holes:
        sections.append((
            f"Largest standing holes (top {min(10, len(holes))} of {len(holes)})",
            f"Groups of {rep['min_group']} or more recipes naming the same missing dish, "
            f"whenever they arrived. {rep['weak_total']} recipes in all have no good dish.",
            html_table(["Missing dish", "Recipes", "Now filed under"],
                       [[h["likely_dish"], h["count"], ", ".join(h["nearest_dishes"])]
                        for h in holes[:10]], right=(1,))))
    since = (rep.get("since") or "")[:16].replace("T", " ")
    return html_page(
        f"Dish match: {len(new)} recipe(s) with no good dish",
        f"Recipes matched since {since or 'the last report'} (UTC) that no catalogue dish fits. "
        "Sent once a day by the dish gap report, and only when there are such recipes.",
        sections,
        footer="Create a dish from the Dishes page (the gap report there has a + beside each hole).")


def print_report(rep: dict, log=print) -> None:
    """Human-readable rendering into the job log."""
    log(f"[GAPS] {rep['weak_total']} weak links among {rep['scanned_unanchored']} "
        f"un-anchored rows (bar {rep['max_dist']}); {rep['unmatched']} never matched")
    if rep.get("since"):
        new = rep.get("new_weak") or []
        log(f"[GAPS] ── NEW since {rep['since']}: {len(new)} row(s) matched and left below the bar ──")
        for r in new:
            log(f"[GAPS]   {r['table']} #{r['id']:<6} d={r['distance']:.3f} {r['name'][:56]!r}  "
                f"names={r['likely_dish'] or '—'}{' (+hole)' if r.get('hole') else ''}  "
                f"nearest={r['nearest'] or '—'}")
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
