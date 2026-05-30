"""Re-derive cookbook chapters across the WHOLE corpus after a taxonomy
change, and reconcile the `chapters` registry table.

Unlike backfill_chapters.py (which only fills MISSING chapters on the
`recipes` table), this RE-CLASSIFIES every row so renamed/removed/moved
chapters get corrected. It covers all three places a chapter lives:

  - recipes        (user recipes)   -> data.classification.chapter (JSON)
  - master_recipes (master cookbook)-> data.classification.chapter (JSON)
  - dishes         (dish library)   -> chapter column
  - chapters       (registry/lookup)-> name column (reconciled to CHAPTERS)

Default is DRY-RUN: it prints every old->new change and writes nothing.
Pass --apply to actually write.

  python scripts/migrate_chapters.py            # dry-run (default)
  python scripts/migrate_chapters.py --apply     # write changes

--preserve-manual keeps any chapter that differs from what the OLD
classifier would have produced (i.e. likely a human override) — only
meaningful once we agree on the policy; off by default.
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _load_dotenv():
    """Load KEY=VALUE pairs from .env into os.environ BEFORE importing the
    classifier, which constructs its Anthropic client (reading the key) at
    import time. Only sets keys not already in the environment."""
    env = PROJECT_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


import os  # noqa: E402  (needed by _load_dotenv above)
_load_dotenv()

from extract.chapter_classifier import classify_chapter, CHAPTERS  # noqa: E402

DB_PATH = str(PROJECT_ROOT / "recipes.db")
REGISTRY = [c for c in CHAPTERS if c != "Uncertain"]  # 24 real chapters

# Old registry names that map cleanly to a new name (metadata carried over).
REGISTRY_RENAMES = {
    "Sandwiches": "Sandwiches, Pizza & Savory Pastry",
    "Pies and Pastries - Sweet": "Pies & Pastries",
}
# Old registry names with no 1:1 successor (their recipes re-derive into
# multiple chapters); the row is dropped.
REGISTRY_DROPS = {"Pies and Pastries - Savory"}


def _name_and_ings(data_json):
    d = json.loads(data_json)
    name = (d.get("name") or "").strip()
    ings = d.get("recipeIngredient") or []
    return d, name, ings


def reclassify_json_table(conn, table, usage):
    """Return list of change dicts for a JSON-blob table. No writes."""
    rows = conn.execute(f"SELECT id, data FROM {table} ORDER BY id").fetchall()
    changes, unchanged, no_name = [], 0, 0
    for rid, data_json in rows:
        try:
            d, name, ings = _name_and_ings(data_json)
        except Exception as e:
            print(f"  [{table} id={rid}] JSON parse failed: {e} — skipping")
            continue
        if not name:
            no_name += 1
            continue
        old = (d.get("classification") or {}).get("chapter")
        new = classify_chapter(name, ings, usage_log=usage)
        if old != new:
            changes.append({"table": table, "id": rid, "name": name,
                            "old": old, "new": new})
        else:
            unchanged += 1
    return changes, unchanged, no_name


def reclassify_dishes(conn, usage):
    rows = conn.execute("SELECT name, chapter FROM dishes ORDER BY name").fetchall()
    changes, unchanged = [], 0
    for name, old in rows:
        new = classify_chapter((name or "").strip(), usage_log=usage)
        if old != new:
            changes.append({"table": "dishes", "id": name, "name": name,
                            "old": old, "new": new})
        else:
            unchanged += 1
    return changes, unchanged, 0


# Chapters that this taxonomy change actually restructured. By default we
# only write a row whose OLD or NEW chapter is one of these — so the
# migration corrects the taxonomy change without re-litigating unrelated
# borderline dishes the LLM might now judge differently (boeuf bourguignon
# Meat<->Soups, a cookie->candy, etc.). --full lifts the filter.
CHANGED_CHAPTERS = {
    "Casseroles & Baked Dishes",            # new
    "Sandwiches, Pizza & Savory Pastry",    # renamed (from Sandwiches)
    "Pies & Pastries",                      # renamed (from Pies...Sweet)
    "Sandwiches",                           # old name
    "Pies and Pastries - Sweet",            # old name
    "Pies and Pastries - Savory",           # old name (removed)
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--full", action="store_true",
                    help="include changes between chapters this migration didn't "
                         "restructure (default: scope to restructured chapters only)")
    args = ap.parse_args()
    dry = not args.apply
    scoped = not args.full

    def in_scope(c):
        return c["old"] in CHANGED_CHAPTERS or c["new"] in CHANGED_CHAPTERS

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("!! ANTHROPIC_API_KEY not set — shortcut MISSES will return "
              "'Uncertain' instead of an LLM answer. Results for ambiguous "
              "titles will be wrong. Set the key for an accurate run.\n")

    conn = sqlite3.connect(DB_PATH)
    usage = []
    all_changes = []
    print(f"{'DRY-RUN' if dry else 'APPLY'} — re-deriving chapters\n" + "=" * 60)

    out_of_scope = []
    for table in ("recipes", "master_recipes"):
        ch, unchanged, no_name = reclassify_json_table(conn, table, usage)
        keep = [c for c in ch if in_scope(c)] if scoped else ch
        out_of_scope += [c for c in ch if not in_scope(c)] if scoped else []
        print(f"\n## {table}: {len(keep)} changes"
              + (f" (+{len(ch) - len(keep)} out-of-scope, suppressed)" if scoped and len(ch) != len(keep) else "")
              + f", {unchanged} unchanged, {no_name} skipped (no name)")
        for c in keep:
            print(f"   id={c['id']:5} {c['name'][:46]:46} {c['old']!r} -> {c['new']!r}")
        all_changes += keep

    ch, unchanged, _ = reclassify_dishes(conn, usage)
    keep = [c for c in ch if in_scope(c)] if scoped else ch
    out_of_scope += [c for c in ch if not in_scope(c)] if scoped else []
    print(f"\n## dishes: {len(keep)} changes, {unchanged} unchanged")
    for c in keep:
        print(f"   {c['name'][:46]:46} {c['old']!r} -> {c['new']!r}")
    all_changes += keep

    if scoped and out_of_scope:
        print(f"\n## {len(out_of_scope)} out-of-scope changes SUPPRESSED "
              "(chapters this migration didn't restructure; use --full to include):")
        for c in out_of_scope:
            print(f"   [{c['table']}] {c['name'][:42]:42} {c['old']!r} -> {c['new']!r}")

    # --- registry reconcile (preview) ---
    existing = [r[0] for r in conn.execute("SELECT name FROM chapters")]
    reg_renames = [(o, n) for o, n in REGISTRY_RENAMES.items() if o in existing]
    reg_drops = [o for o in REGISTRY_DROPS if o in existing]
    have_after = (set(existing) - set(REGISTRY_DROPS)
                  | {n for _, n in reg_renames}) - {o for o, _ in reg_renames}
    reg_adds = [c for c in REGISTRY if c not in have_after]
    print("\n## chapters registry")
    for o, n in reg_renames:
        print(f"   RENAME {o!r} -> {n!r}")
    for o in reg_drops:
        print(f"   DROP   {o!r}  (recipes re-derive into pastry/casserole)")
    for a in reg_adds:
        print(f"   ADD    {a!r}")

    # --- transition summary ---
    print("\n" + "=" * 60)
    trans = Counter((c["old"], c["new"]) for c in all_changes)
    print(f"TOTAL row changes: {len(all_changes)}")
    print("By transition (old -> new):")
    for (o, n), cnt in trans.most_common():
        print(f"   {cnt:4}  {o!r} -> {n!r}")
    llm_calls = sum(1 for u in usage if u.get("operation") == "chapter_classify")
    shortcuts = sum(1 for u in usage if u.get("operation") == "chapter_shortcut")
    print(f"\nClassifier work: {shortcuts} shortcut hits (free), "
          f"{llm_calls} LLM calls")

    if dry:
        print("\n(DRY-RUN — nothing written. Re-run with --apply to commit.)")
        return

    # --- writes ---
    print(f"\nWriting {len(all_changes)} row changes + registry...")
    for c in all_changes:
        if c["table"] == "dishes":
            conn.execute("UPDATE dishes SET chapter=? WHERE name=?", (c["new"], c["id"]))
        else:
            row = conn.execute(f"SELECT data FROM {c['table']} WHERE id=?", (c["id"],)).fetchone()
            d = json.loads(row[0])
            cls = d.get("classification") or {}
            cls["chapter"] = c["new"]
            d["classification"] = cls
            conn.execute(f"UPDATE {c['table']} SET data=? WHERE id=?",
                         (json.dumps(d, indent=2), c["id"]))
    for o, n in reg_renames:
        conn.execute("UPDATE chapters SET name=? WHERE name=?", (n, o))
    for o in reg_drops:
        conn.execute("DELETE FROM chapters WHERE name=?", (o,))
    for a in reg_adds:
        conn.execute("INSERT INTO chapters (name) VALUES (?)", (a,))
    conn.commit()
    print("Done.")


if __name__ == "__main__":
    main()
