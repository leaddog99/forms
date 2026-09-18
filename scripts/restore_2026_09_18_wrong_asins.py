"""Undo two wrong ASIN fills made on 2026-09-18 (agent error) — restore from the 03:00 backup.

What went wrong: asked to fill two blank ASINs, the agent ran resolve -> enrich_one ->
apply_pick_asin -> rematerialize on
  * Ground Cinnamon #2 (Kirkland Signature): Google's best hit was a McCORMICK listing
    (B07F1T7JW1). Full title recall x the 0.6 brand-miss penalty = exactly 0.6 = VERIFIED.
  * Utility Knife #2 (Togiharu PRO Petty): Google's result title matched 1.0, but the live
    listing for B00FRC9ZKC is a MISONO petty knife (0.45, weak, brand absent) — kept anyway.
rematerialize then pushed those onto the product records, and for Utility Knife it also
replaced the Amazon offers on the three product rows that collection's picks point at —
which are HARDWARE utility knives (Milwaukee / DeWalt / LENOX), a conflation that predates
today and still needs its own fix.

Restores, whole-row, from the backup: 2 pick rows + 4 product rows. Nothing else.
Dry run by default:   python scripts/restore_2026_09_18_wrong_asins.py
Apply:                python scripts/restore_2026_09_18_wrong_asins.py --apply
"""
import json
import sqlite3
import sys

BACKUP = "file:/Z:/Backups/recipes-db/recipes_2026-09-18_030049.db?mode=ro"
PICKS = [("Ground Cinnamon", "overall.2"), ("Utility Knife", "overall.2")]
PRODUCTS = ["c9707550-074e-458f-949c-c0ac2b5d89bf",   # Kirkland cinnamon
            "fe030e25-e979-4170-a5de-397aaf9efa64",   # Milwaukee Fastback
            "ba684cf4-8a6e-4495-869c-810df33f8218",   # DeWalt DWHT10992
            "49e28383-6024-4fc7-bf28-9d4c9f34e8c2"]   # LENOX Quick-Change


def main(apply: bool) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    bk = sqlite3.connect(BACKUP, uri=True)
    bk.row_factory = sqlite3.Row
    lv = sqlite3.connect("recipes.db")
    lv.row_factory = sqlite3.Row

    def restore(table, where, args):
        rows = bk.execute(f"SELECT * FROM {table} WHERE {where}", args).fetchall()
        assert len(rows) == 1, (table, args, len(rows))
        b = dict(rows[0])
        live_cols = {r[1] for r in lv.execute(f"PRAGMA table_info({table})")}
        cols = [k for k in b if k != "id" and k in live_cols]
        now = dict(lv.execute(f"SELECT * FROM {table} WHERE {where}", args).fetchone())
        diff = [k for k in cols if now.get(k) != b[k] and k != "embedding"]
        print(f"  {table} {args}: {len(diff)} column(s) differ -> {diff}")
        if apply:
            lv.execute(f"UPDATE {table} SET " + ", ".join(f'"{k}"=?' for k in cols)
                       + f" WHERE {where}", [b[k] for k in cols] + list(args))

    for coll, slot in PICKS:
        restore("curated_collection_picks", "collection=? AND slot=?", (coll, slot))
    for pid in PRODUCTS:
        restore("products", "product_id=?", (pid,))
    if apply:
        lv.commit()
        print("APPLIED.")
        for pid in PRODUCTS:
            r = lv.execute("SELECT brand, name, realrank_score, data FROM products "
                           "WHERE product_id=?", (pid,)).fetchone()
            offers = [(o.get("retailer"), o.get("asin"))
                      for o in json.loads(r["data"]).get("retailer_offers") or []]
            print(f"  {r['brand']} | {r['name'][:40]} | rr {r['realrank_score']} | {offers}")
    else:
        print("dry run — nothing written. Re-run with --apply.")


if __name__ == "__main__":
    main("--apply" in sys.argv)
