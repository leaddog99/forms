"""backfill_scoring_power.py — repair `_scoring` rows that recorded 0 for
"we could not compute this".

THE BUG (found 2026-07-30). `_scoring.power` is 0.0 on 1,778 master rows — 51%
of the corpus — whose `domainAuthority` and `pageAuthority` are both real and
non-zero. `power` and the percentiles were written from the BATCH ENTRY, whose
Moz call had returned 0/0 for small sites, while DA/PA on the same row were
written from a later successful scoring. The zero propagated into
`powerPercentile` and the whole field block: EVERY affected row has `fieldN: 0`,
and no row with a real cohort is affected. So anything ranking on the stored
percentiles pinned half the corpus to the floor of the power dimension — at
`power_blend_weight` 30, up to 0.30 of blend score wrongly deducted.

WHAT THIS DOES
  power        -> recomputed as DA + PA (the same rule blend._power() applies,
                  and correct whenever DA and PA are).
  percentiles  -> REMOVED, not zeroed. A percentile is meaningless without the
  + field block  cohort it was ranked within, and these rows have none. Absent
                  is the truthful value; the SQL columns then read NULL, which
                  is a value you can filter on. Writing 0 is what caused this.

Ranking does not depend on the removal: rank with PERCENT_RANK() over the
`ou_score`/`power` generated columns, which is cohort-explicit at query time.

The write side is fixed in intake/process_batch.pre_scored_from_entry, so new
batches cannot reintroduce this. Run with --dry-run first; it prints exactly
what would change and writes nothing.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "recipes.db"

# Cohort-derived keys that are meaningless without a cohort. Removed, not zeroed.
_COHORT_KEYS = ("ouPercentile", "powerPercentile", "fieldAvgPower", "fieldMaxPower",
                "fieldMinPower", "fieldN", "dishCompetitivenessPct")


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def scan(conn: sqlite3.Connection, table: str) -> list[tuple[int, dict, dict]]:
    """Rows needing repair: (id, old_scoring, new_scoring)."""
    out = []
    for rid, data in conn.execute(f"SELECT id, data FROM {table}"):
        try:
            blob = json.loads(data)
        except Exception:
            continue
        s = blob.get("_scoring")
        if not isinstance(s, dict):
            continue
        da, pa, power = _num(s.get("domainAuthority")), _num(s.get("pageAuthority")), _num(s.get("power"))
        # The signature: power says zero while DA+PA says otherwise.
        if power != 0 or da is None or pa is None or (da + pa) <= 0:
            continue
        new = dict(s)
        new["power"] = float(da) + float(pa)
        for k in _COHORT_KEYS:
            # Only drop the ones that are the zero placeholder — never a real
            # value that happens to sit on a repaired row.
            if _num(new.get(k)) == 0:
                new.pop(k, None)
        out.append((rid, s, new))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--tables", default="master_recipes,recipes")
    a = ap.parse_args()

    conn = sqlite3.connect(a.db)
    total = 0
    for table in [t.strip() for t in a.tables.split(",") if t.strip()]:
        rows = scan(conn, table)
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"\n=== {table}: {len(rows)} of {n} rows need repair ===")
        for rid, old, new in rows[:3]:
            print(f"  id={rid}  power {old.get('power')} -> {new['power']}"
                  f"  (da={old.get('domainAuthority')} pa={old.get('pageAuthority')})"
                  f"  dropped: {sorted(set(old) - set(new))}")
        if len(rows) > 3:
            print(f"  … and {len(rows) - 3} more")
        if a.dry_run or not rows:
            total += len(rows)
            continue
        for rid, _old, new in rows:
            (data,) = conn.execute(f"SELECT data FROM {table} WHERE id = ?", (rid,)).fetchone()
            blob = json.loads(data)
            blob["_scoring"] = new
            conn.execute(f"UPDATE {table} SET data = ? WHERE id = ?",
                         (json.dumps(blob, ensure_ascii=False), rid))
        conn.commit()
        print(f"  WROTE {len(rows)} rows")
        total += len(rows)

    print(f"\n{'Would repair' if a.dry_run else 'Repaired'} {total} rows.")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
