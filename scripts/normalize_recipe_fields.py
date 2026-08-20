"""Normalise stored recipe fields that the facet/sort work found to be dirty.

Three defects, all discovered 2026-08-20 while moving sorting into SQL and
building the cuisine/ethnicity facet columns. Each is fixed at its source as
well; this script cleans the rows written before those fixes existed.

  1. TIMESTAMPS WITHOUT A TIMEZONE.  2,471 master `updated_at` values (and more
     `created_at`) are bare ISO with no offset, e.g. "2026-07-10T18:18:06.307130".
     SQLite string-compares them fine, but JavaScript parses that shape as LOCAL
     time, so on EDT a row reads four hours later than it is — which is exactly
     the one residual inversion left in the updated_desc sort, and the same trap
     library-shell.js fmtDate already documents.

     They are UTC. Proven, not assumed: master id 3512 has created_at naive
     18:23:14 and updated_at aware 18:25:40+00:00 on the same day. If the naive
     value were local (UTC-4) the row would have been created four hours AFTER
     it was updated. The naive run 18:17:04 → 18:18:06 → 18:23:14 → 18:25:40Z is
     continuous and monotonic, which only holds on one clock.

     Current writers already emit timezone-aware values
     (datetime.now(timezone.utc).isoformat()), so this is historical only — there
     is no code fix to pair with it.

  2. NAMES WITH STRAY WHITESPACE.  34 recipes carry a leading or trailing space
     ("Best Lasagna Recipe "), and one leads with a space, which sorted it FIRST
     in the whole A-Z list. The `recipe_name` facet column TRIMs, so ordering is
     already correct without this; the stored value is fixed so the displayed
     title and the sort key stop disagreeing.

  3. PLACEHOLDER CUISINE / ETHNICITY.  One master row carries the literal
     "<UNKNOWN>" in both. The identity-card schema asks for an empty string when
     genuinely unknown, so it is a contract violation, and a facet dropdown built
     by SELECT DISTINCT would offer it as a selectable cuisine. New cards are
     scrubbed by extract/identity_card._scrub_placeholders; this clears the row
     that predates it.

Dry-run by default. Pass --apply to write.

    python scripts/normalize_recipe_fields.py            # report only
    python scripts/normalize_recipe_fields.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

DB_PATH = os.environ.get("BCC_DB_PATH", "recipes.db")
TABLES = ("master_recipes", "recipes")

# Mirrors extract/identity_card._PLACEHOLDERS. Duplicated deliberately rather
# than imported: importing that module pulls in the LLM client, and a data
# migration should not need an API key to run.
PLACEHOLDERS = {
    "<unknown>", "unknown", "n/a", "na", "none", "null", "undefined",
    "other", "various", "unspecified", "tbd", "-", "--", "?", "??",
}
SCRUBBED_FIELDS = ("cuisine", "ethnicity", "technique")


def _aware(ts: str | None) -> bool:
    """Already carries an offset (or the Z shorthand)?"""
    return bool(ts) and (ts.endswith("+00:00") or ts.endswith("Z") or
                         # +HH:MM / -HH:MM at the very end
                         (len(ts) > 6 and ts[-6] in "+-" and ts[-3] == ":"))


def scan(conn: sqlite3.Connection) -> dict:
    """Collect every change this script would make, without making any."""
    plan = {"timestamps": [], "names": [], "identity": []}

    for table in TABLES:
        rows = conn.execute(
            f"SELECT id, data, created_at, updated_at FROM {table}"
        ).fetchall()
        for rid, raw, created, updated in rows:
            # --- 1. timestamps ---
            fixes = {}
            for col, val in (("created_at", created), ("updated_at", updated)):
                if val and not _aware(val):
                    fixes[col] = val + "+00:00"
            if fixes:
                plan["timestamps"].append((table, rid, fixes))

            # --- 2 & 3 need the JSON ---
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            name = data.get("name")
            if isinstance(name, str) and name != name.strip() and name.strip():
                plan["names"].append((table, rid, name, name.strip()))

            ident = data.get("_identity")
            if isinstance(ident, dict):
                hits = {
                    f: ident[f] for f in SCRUBBED_FIELDS
                    if isinstance(ident.get(f), str)
                    and ident[f].strip().lower() in PLACEHOLDERS
                }
                if hits:
                    plan["identity"].append((table, rid, hits))
    return plan


def report(plan: dict, *, sample: int = 5) -> None:
    ts, names, ident = plan["timestamps"], plan["names"], plan["identity"]

    print(f"\n1. TIMESTAMPS missing a timezone: {len(ts)} row(s)")
    by_table: dict[str, int] = {}
    cols: dict[str, int] = {}
    for table, _rid, fixes in ts:
        by_table[table] = by_table.get(table, 0) + 1
        for c in fixes:
            cols[c] = cols.get(c, 0) + 1
    for t, n in sorted(by_table.items()):
        print(f"     {t}: {n}")
    for c, n in sorted(cols.items()):
        print(f"     {c}: {n} value(s) get '+00:00'")
    for table, rid, fixes in ts[:sample]:
        for c, new in fixes.items():
            print(f"     e.g. {table}#{rid} {c}: {new[:-6]!r} -> {new!r}")

    print(f"\n2. NAMES with stray whitespace: {len(names)} row(s)")
    for table, rid, old, new in names[:sample]:
        print(f"     {table}#{rid}: {old!r} -> {new!r}")

    print(f"\n3. PLACEHOLDER identity values: {len(ident)} row(s)")
    for table, rid, hits in ident[:sample]:
        print(f"     {table}#{rid}: {hits} -> ''")

    total = len(ts) + len(names) + len(ident)
    print(f"\n   {total} row-change(s) in total.")


def apply(conn: sqlite3.Connection, plan: dict) -> None:
    for table, rid, fixes in plan["timestamps"]:
        sets = ", ".join(f"{c} = ?" for c in fixes)
        conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?",
                     [*fixes.values(), rid])

    # The JSON edits re-serialise `data`, so read-modify-write one row at a
    # time. json_set() in SQL would be fewer statements but would silently
    # reorder keys and re-encode unicode escapes across the whole document.
    touched: dict[tuple[str, int], dict] = {}
    for table, rid, _old, new in plan["names"]:
        touched.setdefault((table, rid), {})["name"] = new
    for table, rid, hits in plan["identity"]:
        touched.setdefault((table, rid), {})["_identity"] = hits

    for (table, rid), edits in touched.items():
        raw = conn.execute(f"SELECT data FROM {table} WHERE id = ?", (rid,)).fetchone()[0]
        data = json.loads(raw)
        if "name" in edits:
            data["name"] = edits["name"]
        if "_identity" in edits:
            for f in edits["_identity"]:
                data["_identity"][f] = ""
        conn.execute(f"UPDATE {table} SET data = ? WHERE id = ?",
                     (json.dumps(data, ensure_ascii=False), rid))
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry-run report)")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    try:
        plan = scan(conn)
        report(plan)
        if not args.apply:
            print("\n   DRY RUN — nothing written. Re-run with --apply.")
            return 0
        apply(conn, plan)
        print("\n   APPLIED.")

        left = scan(conn)
        remaining = sum(len(v) for v in left.values())
        print(f"   re-scan finds {remaining} remaining change(s) "
              f"({'clean' if remaining == 0 else 'NOT CLEAN — investigate'}).")
        return 0 if remaining == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
