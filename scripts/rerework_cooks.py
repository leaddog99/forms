"""rerework_cooks.py — batch re-rework every persisted `_cook` to the current
prompt version so they all gain v2 sub-steps.

One in-process program ([[batch_is_single_program]]): iterates master + user
recipes that have a `_cook` whose `rework_prompt_version` is not current, runs
the SHARED engine (cook_rework.rework_recipe) on each, and persists ONLY when the
§5 gauntlet passes — mirroring _handle_cook_rework_job exactly. It touches ONLY
the `_cook` block (never `_master`/grade/embedding), so it can't clobber rankings.

IDEMPOTENT + RESUMABLE: a recipe already at the current version is skipped, so a
re-run after an interruption only does the remainder. A recipe whose rework fails
the gauntlet (or truncates) keeps its existing `_cook` and is reported, not lost.

Usage:
  python -m scripts.rerework_cooks --dry-run     # list what would run
  python -m scripts.rerework_cooks               # run it
  python -m scripts.rerework_cooks --master-only
  python -m scripts.rerework_cooks --user-only
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Project root on the path (this file lives in scripts/) so `save_recipe_api`,
# `cook_rework`, etc. import whether invoked as a path or a module.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")

from save_recipe_api import DB_PATH
from cook_rework import rework_recipe, REWORK_PROMPT_VERSION
import llm  # central LLM gateway — journal batch rework usage too

TABLES = {"master_recipes": "master_recipes", "recipes": "user recipes"}


def _targets(conn, which: str):
    """(table, recipe_id, user_id, name) for every _cook NOT at the current version."""
    out = []
    tables = []
    if which in ("all", "master"):
        tables.append("master_recipes")
    if which in ("all", "user"):
        tables.append("recipes")
    for tbl in tables:
        rows = conn.execute(
            f"SELECT recipe_id, user_id, json_extract(data,'$.name'), "
            f"json_extract(data,'$._cook.rework_prompt_version') "
            f"FROM {tbl} WHERE json_extract(data,'$._cook') IS NOT NULL"
        ).fetchall()
        for rid, uid, name, ver in rows:
            if ver != REWORK_PROMPT_VERSION:
                out.append((tbl, rid, uid, name or rid))
    return out


def _rerework_one(tbl: str, rid: str, uid: int) -> dict:
    """Load fresh, rework, persist ONLY if the gauntlet passes. Returns a result dict."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            f"SELECT data FROM {tbl} WHERE recipe_id=? AND user_id=?", (rid, uid)
        ).fetchone()
    if not row:
        return {"ok": False, "reason": "row vanished"}
    recipe = json.loads(row[0])
    recipe.pop("_cook", None)  # rework fresh

    with llm.context(recipe_id=rid, user_id=uid):
        cook, report = rework_recipe(recipe, log=lambda m: print("     " + m, flush=True))
    if not report.passed:
        return {"ok": False, "reason": "gauntlet failed", "failures": report.failures[:4]}

    cook.rework_prompt_version = REWORK_PROMPT_VERSION
    cook.reworked_at = datetime.now(timezone.utc).isoformat()
    n_sub = sum(len(s.substeps) for s in cook.steps)

    with sqlite3.connect(DB_PATH) as conn:
        cur = json.loads(conn.execute(
            f"SELECT data FROM {tbl} WHERE recipe_id=? AND user_id=?", (rid, uid)).fetchone()[0])
        cur["_cook"] = cook.model_dump()
        conn.execute(
            f"UPDATE {tbl} SET data=?, updated_at=? WHERE recipe_id=? AND user_id=?",
            (json.dumps(cur, ensure_ascii=False), cook.reworked_at, rid, uid))
        conn.commit()
    return {"ok": True, "steps": len(cook.steps), "substeps": n_sub}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--master-only", action="store_true")
    ap.add_argument("--user-only", action="store_true")
    args = ap.parse_args()
    which = "master" if args.master_only else "user" if args.user_only else "all"

    with sqlite3.connect(DB_PATH) as conn:
        targets = _targets(conn, which)

    print(f"target version: {REWORK_PROMPT_VERSION}")
    print(f"{len(targets)} recipe(s) to re-rework\n", flush=True)
    if args.dry_run:
        for tbl, rid, uid, name in targets:
            print(f"  [{tbl} uid={uid}] {name}")
        return 0

    passed = failed = 0
    fail_list = []
    for i, (tbl, rid, uid, name) in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {name!r}  ({tbl} uid={uid})", flush=True)
        try:
            res = _rerework_one(tbl, rid, uid)
        except Exception as e:  # noqa: BLE001 — one bad recipe must not sink the batch
            res = {"ok": False, "reason": f"exception: {e}"}
        if res.get("ok"):
            passed += 1
            print(f"     OK — {res['steps']} steps / {res['substeps']} substeps persisted\n", flush=True)
        else:
            failed += 1
            fail_list.append((name, res.get("reason"), res.get("failures")))
            print(f"     SKIPPED — {res.get('reason')}"
                  + (f" :: {res.get('failures')}" if res.get("failures") else "") + "\n", flush=True)

    print("=" * 60)
    print(f"DONE: {passed} re-reworked, {failed} skipped (kept old _cook)")
    for name, reason, failures in fail_list:
        print(f"  - {name}: {reason}" + (f" :: {failures}" if failures else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
