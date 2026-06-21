"""One-off backfill: strip source HTML from already-saved recipe prose.

The model now strips HTML at extract/save time (recipe_model._strip_html via
_as_str), but rows saved before that keep raw <p>/<i>/&amp; markup that renders
literally in the form's intro/steps. This cleans the DISPLAY prose only —
description, headline, name, instruction text/name (incl. nested sections), and
ingredients — and deliberately leaves `_extract_trace` untouched (it preserves
the raw HTML input as provenance on purpose).

  python -m scripts.strip_html_prose            # dry run (report only)
  python -m scripts.strip_html_prose --apply     # write changes
"""
import json
import sqlite3
import sys

import recipe_model as rm

DB = "recipes.db"
TABLES = ("recipes", "master_recipes")
PROSE_KEYS = ("description", "headline", "name")


def _clean_str(d, key):
    v = d.get(key)
    if isinstance(v, str):
        nv = rm._strip_html(v)
        if nv != v:
            d[key] = nv
            return True
    return False


def _clean_step(step):
    changed = False
    if isinstance(step, dict):
        for k in ("text", "name"):
            changed |= _clean_str(step, k)
        for sub in (step.get("itemListElement") or []):  # HowToSection
            changed |= _clean_step(sub)
    return changed


def clean_data(d):
    changed = False
    for k in PROSE_KEYS:
        changed |= _clean_str(d, k)
    ri = d.get("recipeInstructions")
    if isinstance(ri, list):
        for step in ri:
            changed |= _clean_step(step)
    ing = d.get("recipeIngredient")
    if isinstance(ing, list):
        for i, x in enumerate(ing):
            if isinstance(x, str):
                nv = rm._strip_html(x)
                if nv != x:
                    ing[i] = nv
                    changed = True
    return changed


def main(apply):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    total = touched = 0
    samples = []
    for table in TABLES:
        for r in con.execute(f"SELECT id, recipe_id, data FROM {table}").fetchall():
            total += 1
            try:
                data = json.loads(r["data"])
            except Exception:
                continue
            before_desc = data.get("description", "")
            if clean_data(data):
                touched += 1
                if len(samples) < 15:
                    samples.append((table, r["recipe_id"], before_desc[:70], data.get("description", "")[:70]))
                if apply:
                    con.execute(
                        f"UPDATE {table} SET data = ? WHERE id = ?",
                        (json.dumps(data, ensure_ascii=False), r["id"]),
                    )
    if apply:
        con.commit()
    con.close()
    print(f"{'APPLIED' if apply else 'DRY RUN'} — scanned {total}, "
          f"{'cleaned' if apply else 'would clean'} {touched}")
    for table, rid, b, a in samples:
        print(f"  [{table}] {rid}")
        print(f"      before: {b!r}")
        print(f"      after : {a!r}")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
