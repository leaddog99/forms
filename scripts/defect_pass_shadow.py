"""Soundness defect pass — SHADOW runner over master winners.

Runs extract.defect_pass over kind='top' master rows and stores the
validated reports in `soundness_reports`. Nothing renders, nothing gates:
`applied` is 0 on every row this script writes, and there is no code path
here that touches ranking, membership or the batch save loop.

Storage is keyed on (url_normalized, prompt_version) — url because
publisher/dish refreshes are delete-and-replace (the same rule as the
activity log), prompt_version because a calibration audit covers exactly
one prompt. Severity counts are real columns so calibration queries are
SQL, not JSON spelunking (feedback: persist derived values).

Usage (from the project root):
    python scripts/defect_pass_shadow.py --limit 30            # random sample
    python scripts/defect_pass_shadow.py --dish "Tabouleh"
    python scripts/defect_pass_shadow.py --ids 5252,8462,8634
    python scripts/defect_pass_shadow.py --repeat 5 --ids 8582  # repeatability

Already-reported rows (same url + prompt_version) are skipped unless
--repeat N is given, which re-runs N times and reports set-stability
instead of writing (the repeatability test from the spec §5).

NOTE the import-resets-jobs landmine: this script must never import
save_recipe_api. It touches recipes.db directly with its own connection.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(PROJ / ".env")                                       # before the client import

from extract.defect_pass import (                                # noqa: E402
    DEFECT_PROMPT_VERSION, run_defect_pass,
)

DB_PATH = PROJ / "recipes.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS soundness_reports (
    url_normalized   TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    model            TEXT NOT NULL,
    checked_at       TEXT NOT NULL,
    report_json      TEXT NOT NULL,   -- full validated report (defects, refs)
    n_critical       INTEGER NOT NULL DEFAULT 0,
    n_major          INTEGER NOT NULL DEFAULT 0,
    n_minor          INTEGER NOT NULL DEFAULT 0,
    disqualify       INTEGER NOT NULL DEFAULT 0,
    confidence       TEXT,
    evidence_dropped INTEGER NOT NULL DEFAULT 0,
    applied          INTEGER NOT NULL DEFAULT 0,  -- shadow: always 0 here
    overturned       INTEGER NOT NULL DEFAULT 0,  -- curator reversal = data
    PRIMARY KEY (url_normalized, prompt_version)
);
"""


def _severity_counts(report: dict) -> tuple[int, int, int]:
    c = m = n = 0
    for d in report.get("defects") or []:
        s = d.get("severity")
        if s == "critical":
            c += 1
        elif s == "major":
            m += 1
        elif s == "minor":
            n += 1
    return c, m, n


def _defect_set(report: dict) -> frozenset:
    """Category+evidence identity for the repeatability comparison."""
    return frozenset((d.get("category"), (d.get("evidence") or "").strip())
                     for d in report.get("defects") or [])


def _select_rows(conn, args) -> list[tuple[int, str, dict]]:
    where, params = ["json_extract(data,'$._master.kind')='top'"], []
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if args.dish:
        where.append("(json_extract(data,'$._master.dish')=? "
                     "OR json_extract(data,'$._match.dish')=?)")
        params.extend([args.dish, args.dish])
    rows = conn.execute(
        f"SELECT id, url_normalized, data FROM master_recipes "
        f"WHERE {' AND '.join(where)}", params).fetchall()
    out = [(rid, url, json.loads(blob)) for rid, url, blob in rows]
    if args.limit and len(out) > args.limit:
        random.seed(args.seed)
        out = random.sample(out, args.limit)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=0,
                    help="random-sample this many rows (0 = all selected)")
    ap.add_argument("--dish", type=str, default="")
    ap.add_argument("--ids", type=str, default="",
                    help="comma-separated master_recipes.id list")
    ap.add_argument("--seed", type=int, default=14)
    ap.add_argument("--repeat", type=int, default=0,
                    help="repeatability mode: run each row N times, "
                         "report defect-set stability, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-run rows that already have a report for the "
                         "current prompt_version (overwrites)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute(_SCHEMA)
    conn.commit()

    rows = _select_rows(conn, args)
    print(f"[SHADOW] prompt_version={DEFECT_PROMPT_VERSION} "
          f"selected {len(rows)} winner(s)")

    # ---------------- repeatability mode: measure, never write ------------- #
    if args.repeat > 0:
        for rid, url, recipe in rows:
            sets = []
            for i in range(args.repeat):
                rep = run_defect_pass(recipe)
                if rep is None:
                    print(f"  #{rid} run {i + 1}: CALL FAILED")
                    continue
                sets.append(_defect_set(rep))
                c, m, n = _severity_counts(rep)
                print(f"  #{rid} run {i + 1}: crit={c} major={m} minor={n} "
                      f"dropped={rep['evidence_dropped']} "
                      f"dq={rep['disqualify']}")
            stable = len(set(sets)) <= 1
            print(f"  #{rid} defect-set stable across {len(sets)} runs: "
                  f"{'YES' if stable else 'NO — ' + str(len(set(sets))) + ' distinct sets'}")
        return 0

    # ---------------- shadow mode: run + store ----------------------------- #
    done = skipped = failed = flagged = dq = 0
    for rid, url, recipe in rows:
        if not args.force:
            hit = conn.execute(
                "SELECT 1 FROM soundness_reports WHERE url_normalized=? "
                "AND prompt_version=?", (url, DEFECT_PROMPT_VERSION)).fetchone()
            if hit:
                skipped += 1
                continue
        rep = run_defect_pass(recipe)
        if rep is None:
            failed += 1
            print(f"  #{rid} FAILED {url}")
            continue
        c, m, n = _severity_counts(rep)
        conn.execute(
            "INSERT OR REPLACE INTO soundness_reports "
            "(url_normalized, prompt_version, model, checked_at, report_json,"
            " n_critical, n_major, n_minor, disqualify, confidence,"
            " evidence_dropped, applied, overturned) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0)",
            (url, rep["prompt_version"], rep["model"], rep["checked_at"],
             json.dumps({"resolved_references": rep["resolved_references"],
                         "defects": rep["defects"]}, ensure_ascii=False),
             c, m, n, int(rep["disqualify"]), rep["confidence"],
             rep["evidence_dropped"]))
        conn.commit()
        done += 1
        if rep["defects"]:
            flagged += 1
        if rep["disqualify"]:
            dq += 1
            print(f"  #{rid} DISQUALIFY {url}")
        tag = (f"crit={c} major={m} minor={n}" if rep["defects"] else "clean")
        print(f"  #{rid} {tag}"
              + (f" dropped={rep['evidence_dropped']}"
                 if rep["evidence_dropped"] else "")
              + f" conf={rep['confidence']} | {(recipe.get('name') or '')[:50]}")

    print(f"\n[SHADOW] done={done} skipped={skipped} failed={failed} "
          f"with-defects={flagged} disqualify={dq}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
