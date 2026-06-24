"""Backfill: normalize the CHAPTER of canonical-dish recipes (#2).

A native-anchored dish (Kolokithopita, Spanakopita, …) should sit in ONE chapter, but its
members scatter (e.g. Kolokithopita across Pies & Pastries / Sandwiches / Vegetables /
Casseroles). This resolves each master recipe to its canonical dish off the unambiguous
native/transliterated anchor (intake/dish_alias.resolve) and pins
classification.chapter to the dish's authoritative chapter — plus the dishes row itself.

Does NOT touch the author's title (provenance) or re-assign _master.dish (the matcher owns
that). Chapter only. Master corpus only. See docs/dish-alias-normalization.md.

  python -m scripts.normalize_dish_aliases            # dry-run
  python -m scripts.normalize_dish_aliases --apply
"""
import argparse
import json
import sqlite3
import sys

sys.path.insert(0, ".")
from intake import dish_alias

DB = "recipes.db"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()
    conn = sqlite3.connect(DB, timeout=30)

    per_dish: dict[str, list] = {}
    changed = 0
    for rid, dj in conn.execute("SELECT recipe_id, data FROM master_recipes WHERE user_id=0"):
        try:
            d = json.loads(dj)
        except Exception:
            continue
        name = d.get("name") or ""
        ot = (d.get("_source") or {}).get("originalTitle") or ""
        cur_dish = (d.get("_master") or {}).get("dish") or ""
        entry = dish_alias.resolve(name, ot, cur_dish)
        if not entry:
            continue
        cur_ch = (d.get("classification") or {}).get("chapter")
        want = entry["chapter"]
        if cur_ch == want:
            continue
        per_dish.setdefault(entry["canonical"], []).append((name, cur_ch, want))
        if args.apply:
            d.setdefault("classification", {})["chapter"] = want
            conn.execute("UPDATE master_recipes SET data = ? WHERE recipe_id = ?",
                         (json.dumps(d), rid))
            changed += 1

    # Pin the canonical dishes' own chapter so dish + members agree.
    dish_ch = 0
    for entry in dish_alias._dishes():
        if args.apply:
            cur = conn.execute(
                "UPDATE dishes SET chapter = ? WHERE name = ? AND (chapter IS NULL OR chapter <> ?)",
                (entry["chapter"], entry["canonical"], entry["chapter"]))
            dish_ch += cur.rowcount or 0

    if args.apply:
        conn.commit()

    total = sum(len(v) for v in per_dish.values())
    print(f"recipes needing a chapter fix: {total}")
    for dish, items in sorted(per_dish.items()):
        print(f"  {dish} ({len(items)}) -> {items[0][2]!r}")
        for name, cur, want in items[:8]:
            print(f"     {name[:52]:52} {cur!r} -> {want!r}")
    print(f"\n{'APPLIED' if args.apply else 'DRY-RUN'}: "
          f"chapter changes={changed if args.apply else total}, dish-row chapter updates={dish_ch}")
    conn.close()


if __name__ == "__main__":
    main()
