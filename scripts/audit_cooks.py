"""audit_cooks.py — run the AUDITOR (cook_audit) over already-persisted `_cook` plans.

The rework pipeline now audits every NEW plan (cook_rework → cook_audit). The 58
plans persisted before 2026-10-13 never had that read — 51 of them render a seam
defect ("¾ lb cooked Cooked penne", "2½ cups onion + 3 cloves garlic Onion &
garlic"). This repairs them IN PLACE with the auditor's targeted edits — no
re-rework (a re-rework re-emits the whole plan at ~10x the cost and can change
the cooking; the audit changes prose only). Persists ONLY when the audit passes
AND the structural gauntlet still passes after the edits; a plan that fails keeps
its existing `_cook` untouched and is reported.

One in-process program ([[batch_is_single_program]]); idempotent — a plan whose
`_cook.audit.version` is current is skipped unless --force.

Usage:
  python -m scripts.audit_cooks --dry-run              # mechanical findings only, $0, nothing written
  python -m scripts.audit_cooks --recipe <id>          # one plan (master or user table)
  python -m scripts.audit_cooks --render --recipe <id> # print the plan as the cook sees it
  python -m scripts.audit_cooks                        # every stale plan
  python -m scripts.audit_cooks --force --recipe <id>  # re-audit even if current
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")

from save_recipe_api import DB_PATH  # noqa: E402
from cook_model import CookMetadata  # noqa: E402
from cook_validators import run_all  # noqa: E402
from cook_render import render_plan_text  # noqa: E402
from cook_audit import AUDIT_VERSION, audit_cook, mechanical_findings  # noqa: E402
import cook_costs  # noqa: E402
import llm  # noqa: E402

TABLES = ("master_recipes", "recipes")


def _targets(conn, recipe_id, force):
    out = []
    for tbl in TABLES:
        q = (f"SELECT recipe_id, user_id, json_extract(data,'$.name'), "
             f"json_extract(data,'$._cook.audit.version') FROM {tbl} "
             f"WHERE json_extract(data,'$._cook.steps[0]') IS NOT NULL")
        args = ()
        if recipe_id:
            q += " AND recipe_id = ?"
            args = (recipe_id,)
        for rid, uid, name, ver in conn.execute(q, args).fetchall():
            if force or ver != AUDIT_VERSION:
                out.append((tbl, rid, uid, name or rid))
    return out


def _load(conn, tbl, rid, uid):
    row = conn.execute(f"SELECT data FROM {tbl} WHERE recipe_id=? AND user_id=?", (rid, uid)).fetchone()
    return json.loads(row[0]) if row else None


def _audit_one(tbl, rid, uid, dry):
    with sqlite3.connect(DB_PATH) as conn:
        recipe = _load(conn, tbl, rid, uid)
    if not recipe:
        return {"ok": False, "reason": "row vanished"}
    cook = CookMetadata(**recipe["_cook"])
    if dry:
        mech = mechanical_findings(cook)
        for f in mech:
            print(f"     [{f['severity']}] {f['where']}: {f['problem']}\n         renders: {f['rendered']}")
        return {"ok": True, "dry": True,
                "blockers": sum(1 for f in mech if f["severity"] == "blocker"),
                "polish": sum(1 for f in mech if f["severity"] == "polish")}

    usages: list = []
    pre = set(run_all(cook).failures)      # gate failures the plan ALREADY had (older gates)
    with llm.context(recipe_id=rid, user_id=uid):
        aud = audit_cook(cook, log=lambda m: print("     " + m, flush=True), usages=usages)
    est = cook_costs.estimate(usages)
    cost = est.get("total") if isinstance(est, dict) else None
    post = run_all(cook)
    new_fail = [f for f in post.failures if f not in pre]
    if new_fail:   # only what the EDITS broke fails the audit; a pre-existing gap is reported, not fatal
        return {"ok": False, "reason": "gauntlet failed after edits", "failures": new_fail[:4], "cost": cost}
    if post.failures:
        print(f"     note: {len(post.failures)} pre-existing gate failure(s) remain (older rework): {post.failures[:2]}")
    for p in aud.get("residual_polish", []):
        print(f"     polish left: {p}")
    if not aud.get("passed"):
        return {"ok": False, "reason": "blockers remain", "failures": aud.get("residual", [])[:4], "cost": cost}
    cook.audit = aud
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = _load(conn, tbl, rid, uid)
        cur["_cook"] = cook.model_dump()
        cur["_cook"]["audited_at"] = now
        conn.execute(f"UPDATE {tbl} SET data=?, updated_at=? WHERE recipe_id=? AND user_id=?",
                     (json.dumps(cur, ensure_ascii=False), now, rid, uid))
        conn.commit()
    n_edits = sum(len(r["edits_applied"]) for r in aud["rounds"])
    return {"ok": True, "grade": aud.get("grade"), "edits": n_edits,
            "rounds": len(aud["rounds"]), "polish_left": len(aud.get("residual_polish", [])), "cost": cost}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="mechanical findings only; writes nothing")
    ap.add_argument("--recipe", help="one recipe_id")
    ap.add_argument("--render", action="store_true", help="print the plan as the cook sees it (with --recipe)")
    ap.add_argument("--force", action="store_true", help="re-audit plans already at the current audit version")
    args = ap.parse_args()

    with sqlite3.connect(DB_PATH) as conn:
        targets = _targets(conn, args.recipe, args.force or args.render or args.dry_run)

    if args.render:
        for tbl, rid, uid, name in targets:
            with sqlite3.connect(DB_PATH) as conn:
                recipe = _load(conn, tbl, rid, uid)
            print(f"==== {name} [{tbl} uid={uid}]\n")
            print(render_plan_text(CookMetadata(**recipe["_cook"])))
            print()
        return 0

    print(f"audit version: {AUDIT_VERSION}")
    print(f"{len(targets)} plan(s) to audit{' (dry run)' if args.dry_run else ''}\n", flush=True)
    done = failed = 0
    total_cost = 0.0
    for i, (tbl, rid, uid, name) in enumerate(targets, 1):
        print(f"[{i}/{len(targets)}] {name}  [{tbl} uid={uid} {rid[:8]}]", flush=True)
        try:
            r = _audit_one(tbl, rid, uid, args.dry_run)
        except Exception as e:  # noqa: BLE001 — one failure must not stop the batch
            r = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
        if r.get("cost"):
            total_cost += float(r["cost"])
        if r.get("ok"):
            done += 1
            print(f"     -> {json.dumps(r, ensure_ascii=False)}")
        else:
            failed += 1
            print(f"     -> FAILED: {r.get('reason')} {r.get('failures') or ''}")
    print(f"\ndone: {done} ok, {failed} failed" + (f", ${total_cost:.2f}" if total_cost else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
