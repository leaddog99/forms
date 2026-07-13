"""scripts/backfill_equipment.py — re-derive `equipment` (with sizes) for existing recipes.

The base extraction now derives equipment automatically (enrich/api.py folds it into the
markdown-LLM prompt + fast-lane fallback), but recipes ingested BEFORE that change carry
un-sized or old-"derive" equipment. This backfill re-runs the SAME shared derivation
(enrich/equipment.derive_equipment) over existing rows and persists the result.

  Recipe table(s):  master_recipes (user_id=0)  and  recipes (personal, user_id>0).

Usage (from repo root):
  python -m scripts.backfill_equipment --dry-run                 # counts + cost estimate, no LLM
  python -m scripts.backfill_equipment --table master --mode no-size --limit 20   # small live batch
  python -m scripts.backfill_equipment --table both  --mode all   # the full re-derive

Modes:
  all       every recipe (default) — the "complete re-extraction"
  no-size   only recipes whose equipment has NO sized item yet (cheapest useful pass)
  missing   only recipes with NO equipment at all

Safe:
  - Idempotent + resumable: processes by id ASC; --start-after <id> resumes; --limit caps.
  - NEVER wipes: only writes when derive returns a non-empty list.
  - Per-recipe llm.context so spend journals to bcc_token_journal (operation=enrich_equipment).
  - Writes to recipes.db while the service runs are fine (WAL); equipment touches no vectors.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

# repo root import
sys.path.insert(0, ".")
import llm
from enrich.equipment import derive_equipment

DB = "recipes.db"
_TABLES = {"master": ("master_recipes", 0), "personal": ("recipes", None)}

# Rough Sonnet 4.6 cost per derive call (≈2k in + ≈0.4k out) for the estimate only.
_EST_COST_PER_CALL = 0.012


def _wants(recipe: dict, mode: str) -> bool:
    eq = recipe.get("equipment") or []
    if mode == "missing":
        return not eq
    if mode == "no-size":
        return not any(isinstance(e, dict) and e.get("size") for e in eq)
    return True  # all


def _rows(conn, table, start_after):
    # id is the autoincrement PK on both tables; recipe_id/user_id are the JSON identity.
    q = f"SELECT id, recipe_id, user_id, data FROM {table} WHERE id > ? ORDER BY id ASC"
    return conn.execute(q, (start_after,)).fetchall()


def _size_face(size):
    """Coerce a size to a display string. `_cook` sizes are measurement DICTS
    ({imperial,metric,convertible}); equipment.size must be a STRING (prefer imperial)."""
    if isinstance(size, dict):
        return (str(size.get("imperial") or size.get("metric") or "")).strip() or None
    if isinstance(size, str):
        return size.strip() or None
    return None


def _mirror_from_cook(cook_equipment):
    """Mirror `_cook.equipment` (id/name/size) into top-level HowToTool shape — same as
    save_recipe_api._recipe_equipment_from_cook. Dedup by name, order preserved; size
    coerced to the imperial-face string (never a raw measurement dict)."""
    out, seen = [], set()
    for e in (cook_equipment or []):
        name = ((e.get("name") if isinstance(e, dict) else None) or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        item = {"@type": "HowToTool", "name": name}
        size = _size_face(e.get("size") if isinstance(e, dict) else None)
        if size:
            item["size"] = size
        out.append(item)
    return out


def _write_equipment(conn, table, rid_pk, equipment):
    """Re-read + write ONLY the equipment field so a concurrent service edit isn't clobbered."""
    cur = conn.execute(f"SELECT data FROM {table} WHERE id = ?", (rid_pk,)).fetchone()
    d = json.loads(cur[0])
    d["equipment"] = equipment
    conn.execute(f"UPDATE {table} SET data = ? WHERE id = ?",
                 (json.dumps(d, ensure_ascii=False), rid_pk))
    conn.commit()


def run(table_key: str, mode: str, limit: int, start_after: int, dry_run: bool) -> dict:
    table, _fixed_uid = _TABLES[table_key]
    conn = sqlite3.connect(DB)
    conn.row_factory = None
    considered = matched = done = skipped_empty = errors = 0
    last_id = start_after
    for rid_pk, recipe_id, user_id, dj in _rows(conn, table, start_after):
        last_id = rid_pk
        try:
            recipe = json.loads(dj)
        except Exception:
            continue
        considered += 1
        # NEVER re-derive a cook-reworked recipe from instructions — its equipment
        # (with real sizes) comes from `_cook`; a conservative instruction-derive would
        # DROP those sizes. Mirror from _cook instead (free, lossless, keeps sizes).
        cook_eq = ((recipe.get("_cook") or {}).get("equipment")) or []
        if cook_eq:
            if dry_run:
                continue   # not counted as a derive-call cost (no LLM)
            new_eq = _mirror_from_cook(cook_eq)
            if new_eq and recipe.get("equipment") != new_eq:
                _write_equipment(conn, table, rid_pk, new_eq)
                done += 1
                print(f"  [cook] id={rid_pk} {recipe.get('name','?')[:40]:40} "
                      f"{len(new_eq)} tools from _cook")
            continue
        if not _wants(recipe, mode):
            continue
        matched += 1
        if dry_run:
            if limit and matched >= limit:
                break
            continue
        try:
            with llm.context(recipe_id=recipe_id, user_id=int(user_id or 0)):
                equipment = derive_equipment(recipe)
        except Exception as e:
            errors += 1
            print(f"  [ERR] id={rid_pk} {recipe.get('name','?')[:40]}: {type(e).__name__}: {e}")
            continue
        if not equipment:
            skipped_empty += 1
        else:
            _write_equipment(conn, table, rid_pk, equipment)
            done += 1
            n_sized = sum(1 for e in equipment if isinstance(e, dict) and e.get("size"))
            print(f"  [ok]  id={rid_pk} {recipe.get('name','?')[:40]:40} "
                  f"{len(equipment)} tools ({n_sized} sized)")
        if limit and done >= limit:
            break
    conn.close()
    return {"table": table, "considered": considered, "matched": matched,
            "written": done, "empty": skipped_empty, "errors": errors,
            "last_id": last_id}


def main():
    ap = argparse.ArgumentParser(description="Re-derive recipe equipment (with sizes).")
    ap.add_argument("--table", choices=["master", "personal", "both"], default="both")
    ap.add_argument("--mode", choices=["all", "no-size", "missing"], default="all")
    ap.add_argument("--limit", type=int, default=0, help="cap recipes WRITTEN (0 = no cap)")
    ap.add_argument("--start-after", type=int, default=0, help="resume after this pk id")
    ap.add_argument("--dry-run", action="store_true", help="count + estimate, no LLM/writes")
    args = ap.parse_args()

    keys = ["master", "personal"] if args.table == "both" else [args.table]
    t0 = time.perf_counter()
    grand_matched = grand_written = 0
    for k in keys:
        print(f"\n=== {k} (mode={args.mode}, dry_run={args.dry_run}) ===")
        r = run(k, args.mode, args.limit, args.start_after, args.dry_run)
        grand_matched += r["matched"]
        grand_written += r["written"]
        print(f"  considered={r['considered']} matched={r['matched']} "
              f"written={r['written']} empty={r['empty']} errors={r['errors']} "
              f"last_id={r['last_id']}")
    dt = time.perf_counter() - t0
    if args.dry_run:
        print(f"\nDRY RUN: {grand_matched} recipes would be re-derived "
              f"~ ${grand_matched * _EST_COST_PER_CALL:,.0f} (rough Sonnet est). "
              f"Run without --dry-run to execute.")
    else:
        print(f"\nDONE: wrote {grand_written} recipes in {dt:.0f}s.")


if __name__ == "__main__":
    main()
