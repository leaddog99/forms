"""Repair grades whose underlying score no longer exists.

WHY (2026-08-06): the manufactured-zero strip (e942c78) removed fabricated
PA/DA/OU from `_scoring` but left the DERIVED `_master.exceptionalism` block
behind, so 17 master rows display a grade computed from data that is gone.
Curator: "if there isn't a score nor should there be a grade."

Two distinct populations, which is why this re-scores rather than deletes:

  - LOW orphans (exc ~45-52) were graded off a manufactured pa=0 — the grade
    itself is garbage and a fresh score fixes it.
  - HIGH orphans (exc 90-100) were graded off REAL scores that later went
    missing; deleting would throw away a legitimate grade.

So: re-score the URL through Moz, recompute the grade against the dish's
last_ou_fit, and only DROP the grade when Moz genuinely has no data — which is
the honest outcome for a page nothing can measure, not a low grade.

Reuses the canonical calls end to end (feedback_single_path): score_url_via_moz,
update_recipe_scoring, compute_exceptionalism, find_best_dish_match. Nothing
about the scoring or grading math lives here.

Cost: ~1-5 Moz rows per URL (a URL Moz has never crawled self-heals by
re-probing all four variants), so ~17 urls is pennies. Slow — roughly 2 round
trips each.

Usage:
  python -m scripts.repair_orphan_grades              # dry run, report only
  python -m scripts.repair_orphan_grades --commit     # write
  python -m scripts.repair_orphan_grades --commit --limit 3
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from input.pipeline.grading import compute_exceptionalism  # noqa: E402
from input.pipeline.embeddings import find_best_dish_match  # noqa: E402
from input.pipeline import dishes as dishes_lib  # noqa: E402
from input.pipeline import vector_store  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from backfill_url_scoring import refresh_url, update_recipe_scoring  # noqa: E402

DB_PATH = str(PROJECT_ROOT / "recipes.db")
BACKUP = PROJECT_ROOT / "docs" / "reports" / "orphan-grades-backup.json"

# A BCC self-permalink (_bcc_link_permalink shape). Matched on PATH rather than
# host because the public host has changed over time (bestcooksclub.com,
# recipes.tbotb.com, bestcooks.club) while the /r/<uuid> shape has not.
_SELF_URL_RE = re.compile(
    r"/r/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _drop_grade(conn: sqlite3.Connection, table: str, rid: int, d: dict,
                is_master: bool, *, commit: bool) -> None:
    """Remove the orphaned grade. Absent, not a low score — a page nothing can
    measure has no grade, it does not have a bad one (feedback_absent_not_zero)."""
    if is_master:
        m = d.get("_master") or {}
        m.pop("exceptionalism", None)
        d["_master"] = m
    else:
        d.pop("_grade", None)
    if commit:
        conn.execute(f"UPDATE {table} SET data = ? WHERE id = ?",
                     (json.dumps(d, indent=2), rid))
        conn.commit()


def find_orphans(conn: sqlite3.Connection) -> list[tuple[str, int, dict]]:
    """Rows carrying a grade with no live PA/DA behind it."""
    out = []
    for table in ("master_recipes", "recipes"):
        for rid, dj in conn.execute(f"SELECT id, data FROM {table}"):
            try:
                d = json.loads(dj)
            except Exception:
                continue
            if table == "master_recipes":
                grade = ((d.get("_master") or {}).get("exceptionalism") or {}).get("grade")
            else:
                grade = (d.get("_grade") or {}).get("grade")
            if grade is None:
                continue
            s = d.get("_scoring") or {}
            if s.get("pageAuthority") is None or s.get("domainAuthority") is None:
                out.append((table, rid, d))
    return out


def regrade(conn: sqlite3.Connection, d: dict, is_master: bool):
    """Recompute the grade from the CURRENT _scoring. Returns the grade dict or
    None. Mirrors backfill_grading: explicit dish first, embedding match after."""
    s = d.get("_scoring") or {}
    da, pa = s.get("domainAuthority"), s.get("pageAuthority")
    if da is None or pa is None:
        return None
    if is_master:
        explicit = ((d.get("_master") or {}).get("dish") or "").strip()
        if explicit:
            row = dishes_lib.get_dish(conn, explicit)
            if row and row.get("last_ou_fit"):
                g = compute_exceptionalism(da, pa, row["last_ou_fit"],
                                           matched_dish=explicit,
                                           match_method="explicit")
                if g:
                    return g
    match = find_best_dish_match(conn, d)
    if match and match.get("ou_fit"):
        return compute_exceptionalism(
            da, pa, match["ou_fit"], matched_dish=match["dish_name"],
            match_confidence=match["confidence"],
            match_method=("embedding-match-narrow" if match.get("chapter_filtered")
                          else "embedding-match-wide"))
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--commit", action="store_true", help="write (default: dry run)")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        vector_store.enable_vec(conn)
    except Exception as e:
        print(f"[warn] sqlite-vec: {e}")

    orphans = find_orphans(conn)
    print(f"orphaned grades (grade present, PA/DA absent): {len(orphans)}\n")
    if not orphans:
        return 0

    if args.commit:
        BACKUP.parent.mkdir(parents=True, exist_ok=True)
        BACKUP.write_text(json.dumps(
            [{"table": t, "id": i, "data": d} for t, i, d in orphans], indent=1),
            encoding="utf-8")
        print(f"backup -> {BACKUP}\n")

    counts: Counter = Counter()
    done = 0
    for table, rid, d in orphans:
        is_master = (table == "master_recipes")
        name = str(d.get("name"))[:40]
        old = (((d.get("_master") or {}).get("exceptionalism") or {}) if is_master
               else (d.get("_grade") or {})).get("grade")
        url = ((d.get("_source") or {}).get("originalUrl") or "").strip()
        if not url:
            counts["no-url -> grade dropped"] += 1
            print(f"  [drop] {table}.{rid} grade {old} -> None (no source url) — {name}")
            _drop_grade(conn, table, rid, d, is_master, commit=args.commit)
            done += 1
            if args.limit and done >= args.limit:
                break
            continue

        # NOT APPLICABLE, not unmeasured. A handwritten recipe mints its own
        # /r/<uuid> permalink; nothing external can ever link to it, so Moz will
        # never hold data and a low grade is meaningless — it grades the
        # curator's own recipe as failing for want of inbound links to a URL
        # that exists only for them. Drop the grade WITHOUT spending a Moz call
        # (Moz bills per target url, and this can only ever come back empty).
        if _SELF_URL_RE.search(url):
            counts["self-url (not applicable) -> grade dropped"] += 1
            print(f"  [drop] {table}.{rid} grade {old} -> None (own /r/ permalink) — {name}")
            _drop_grade(conn, table, rid, d, is_master, commit=args.commit)
            done += 1
            if args.limit and done >= args.limit:
                break
            continue

        _, scores = refresh_url(conn, url, dry_run=not args.commit)
        if not scores or scores.get("page_authority") is None:
            # Moz has no data. The honest outcome is NO grade, not a low one.
            counts["no-moz-data -> grade dropped"] += 1
            print(f"  [drop] {table}.{rid} grade {old} -> None (Moz has no data) — {name}")
            _drop_grade(conn, table, rid, d, is_master, commit=args.commit)
            done += 1
            if args.limit and done >= args.limit:
                break
            continue

        update_recipe_scoring(d, scores)
        g = regrade(conn, d, is_master)
        if g is None:
            counts["scored but no cohort -> grade dropped"] += 1
            print(f"  [drop] {table}.{rid} grade {old} -> None "
                  f"(pa={scores['page_authority']} but no dish cohort) — {name}")
            _drop_grade(conn, table, rid, d, is_master, commit=args.commit)
        else:
            counts["regraded"] += 1
            print(f"  [ok]   {table}.{rid} grade {old} -> {g['grade']} "
                  f"(pa={scores['page_authority']} da={scores['domain_authority']}) — {name}")
            if args.commit:
                if is_master:
                    m = d.get("_master") or {}
                    m["exceptionalism"] = g
                    d["_master"] = m
                else:
                    d["_grade"] = g
        if args.commit:
            conn.execute(f"UPDATE {table} SET data = ? WHERE id = ?",
                         (json.dumps(d, indent=2), rid))
            conn.commit()
        done += 1
        if args.limit and done >= args.limit:
            print(f"  reached limit ({args.limit})")
            break

    print(f"\n=== {dict(counts)} ===")
    if not args.commit:
        print("(dry run — no writes; pass --commit)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
