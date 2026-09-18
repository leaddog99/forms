"""One-time relabel pass: put each SHARED recipe under the dish it is nearest to.

A recipe page can be a current winner in two or more dishes. Until 2026-09-18 a dish
refresh stamped its own name on every winner it saved, so a shared recipe's label sat
with whichever dish refreshed LAST. The refresh now asks dish_match.label_holder first;
this script applies the same judgment to the labels that are already wrong.

Scope: library rows (user_id=0, kind=top) that the LATEST run of 2+ dishes selected, and
that are labelled to one of those dishes. OLDEST first (created_at), `--limit` of them.
Unmeasurable pairs never move here (no refresh is happening, so there is no challenger).

    python scripts/relabel_shared_recipes.py --limit 50            # dry run: prints every row
    python scripts/relabel_shared_recipes.py --limit 50 --apply    # writes; saves an undo file
    python scripts/relabel_shared_recipes.py --undo logs/relabel_<stamp>.json

What a move writes: _master.dish -> the nearer dish; _master.rank removed (it was the rank
inside the OTHER dish's run — absent, not wrong); _master.relabelled = the audit trail
(from, to, method, both distances, prior rank, when); the vector table's dish/chapter aux
columns. updated_at is NOT bumped: the recipe did not change, only where it is filed.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from input.pipeline import dish_match as dm, vector_store as vs  # noqa: E402
from input.pipeline.embeddings import bytes_to_vec  # noqa: E402

DB = "recipes.db"
SHARED_SQL = """
WITH latest AS (SELECT dish_name, MAX(model_version) mv FROM dish_run_data_points GROUP BY 1)
SELECT dp.url, dp.dish_name
FROM dish_run_data_points dp
JOIN latest l ON l.dish_name = dp.dish_name AND l.mv = dp.model_version
WHERE dp.selected = 1
"""


def plan(conn, limit: int) -> list[dict]:
    live = {r[0] for r in conn.execute("SELECT name FROM dishes")}
    by = collections.defaultdict(set)
    for url, dish in conn.execute(SHARED_SQL):
        if dish in live:                       # a renamed/deleted dish is not a contender
            by[url].add(dish)
    names = dm.name_index(conn)
    out = []
    rows = conn.execute(
        "SELECT id, url_normalized, data, embedding, created_at FROM master_recipes "
        "WHERE user_id = 0 ORDER BY created_at ASC, id ASC").fetchall()
    for rid, url, data, emb, created in rows:
        dishes = by.get(url)
        if not dishes or len(dishes) < 2:
            continue
        d = json.loads(data)
        m = d.get("_master") or {}
        cur = m.get("dish")
        if m.get("kind") != "top" or cur not in dishes:
            continue
        vec = bytes_to_vec(emb) if emb else None
        likely = (d.get("_identity") or {}).get("likelyDish") or ""
        holder, trail = cur, []
        for ch in sorted(dishes - {cur}):
            w = dm.label_holder(conn, vec, holder, ch, likely_dish=likely, names=names)
            trail.append({"vs": ch, **w})
            if w["method"] != "unmeasured":
                holder = w["holder"]
        out.append({"id": rid, "url": url, "created_at": created, "name": d.get("name") or "",
                    "likely": likely, "contenders": sorted(dishes), "from": cur, "to": holder,
                    "prior_rank": m.get("rank"), "trail": trail,
                    "measured": any(t["method"] != "unmeasured" for t in trail)})
        if len(out) >= limit:
            break
    return out


def show(rows: list[dict]) -> None:
    moves = [r for r in rows if r["to"] != r["from"]]
    print(f"{len(rows)} oldest shared recipes examined | {len(moves)} would MOVE | "
          f"{len(rows) - len(moves)} stay | "
          f"{sum(1 for r in rows if not r['measured'])} unmeasurable (left alone)\n")
    for r in rows:
        t = r["trail"][-1]
        tag = "MOVE" if r["to"] != r["from"] else "stay"
        dist = " / ".join(f"{x['vs']}={x['challenger']}" for x in r["trail"])
        print(f"  {tag}  id={r['id']:<6} {r['created_at'][:10]}  {r['name'][:40]:42s} "
              f"{r['from']!r}" + (f" -> {r['to']!r}" if tag == "MOVE" else "")
              + f"   [{t['method']}: {r['from']}={r['trail'][0]['current']} | {dist}]")


def apply(conn, rows: list[dict]) -> str:
    vs.enable_vec(conn)
    now = datetime.now(timezone.utc).isoformat()
    undo, n = [], 0
    for r in rows:
        if r["to"] == r["from"]:
            continue
        data, emb = conn.execute("SELECT data, embedding FROM master_recipes WHERE id = ?",
                                 (r["id"],)).fetchone()
        d = json.loads(data)
        m = d.get("_master") or {}
        if m.get("dish") != r["from"]:          # changed under us (a refresh ran) — skip
            print(f"  skipped id={r['id']}: label is now {m.get('dish')!r}, not {r['from']!r}")
            continue
        undo.append({"id": r["id"], "master": dict(m)})
        last = r["trail"][-1]
        m["dish"] = r["to"]
        m.pop("rank", None)
        m["relabelled"] = {"from": r["from"], "to": r["to"], "at": now,
                           "method": last["method"], "prior_rank": r["prior_rank"],
                           "distances": {r["from"]: r["trail"][0]["current"],
                                         **{t["vs"]: t["challenger"] for t in r["trail"]}},
                           "by": "scripts/relabel_shared_recipes.py"}
        d["_master"] = m
        conn.execute("UPDATE master_recipes SET data = ? WHERE id = ?",
                     (json.dumps(d, ensure_ascii=False), r["id"]))
        if emb:
            ch = conn.execute("SELECT chapter FROM dishes WHERE name = ?", (r["to"],)).fetchone()
            vs.upsert_recipe_vector(conn, r["id"], bytes_to_vec(emb),
                                    chapter=(ch[0] if ch else None), dish=r["to"])
        n += 1
    conn.commit()
    path = f"logs/relabel_{now[:19].replace(':', '-')}.json"
    json.dump({"at": now, "rows": undo}, open(path, "w", encoding="utf-8"), indent=1)
    print(f"\nAPPLIED {n} move(s). Undo file: {path}")
    return path


def enforce_margin(conn, do_apply: bool) -> None:
    """Revert earlier DISTANCE moves that the margin rule would not have made.

    The first 50 were relabelled before move_margin() existed. A row whose audit
    block shows a distance move with a margin under the threshold goes back to
    the dish it came from (its prior rank restored)."""
    vs.enable_vec(conn)
    margin = dm.move_margin(conn)
    n = 0
    for rid, data, emb in conn.execute(
            "SELECT id, data, embedding FROM master_recipes "
            "WHERE json_extract(data, '$._master.relabelled.method') = 'distance'").fetchall():
        d = json.loads(data)
        m = d["_master"]
        rl = m["relabelled"]
        dist = rl.get("distances") or {}
        if rl["from"] not in dist or rl["to"] not in dist:
            continue
        got = round(dist[rl["from"]] - dist[rl["to"]], 4)
        if got >= margin or m.get("dish") != rl["to"]:
            continue
        print(f"  REVERT id={rid:<6} {(d.get('name') or '')[:42]:44s} {rl['to']!r} -> back to "
              f"{rl['from']!r}  (margin {got} < {margin})")
        n += 1
        if not do_apply:
            continue
        m["dish"] = rl["from"]
        if rl.get("prior_rank") is not None:
            m["rank"] = rl["prior_rank"]
        m.pop("relabelled", None)
        m["relabel_reverted"] = {"was_moved_to": rl["to"], "margin": got, "threshold": margin,
                                 "at": datetime.now(timezone.utc).isoformat()}
        conn.execute("UPDATE master_recipes SET data = ? WHERE id = ?",
                     (json.dumps(d, ensure_ascii=False), rid))
        if emb:
            ch = conn.execute("SELECT chapter FROM dishes WHERE name = ?", (rl["from"],)).fetchone()
            vs.upsert_recipe_vector(conn, rid, bytes_to_vec(emb),
                                    chapter=(ch[0] if ch else None), dish=rl["from"])
    conn.commit()
    print(f"{n} sub-margin move(s) " + ("reverted." if do_apply else "would be reverted (dry run)."))


def undo(conn, path: str) -> None:
    vs.enable_vec(conn)
    rows = json.load(open(path, encoding="utf-8"))["rows"]
    for u in rows:
        data, emb = conn.execute("SELECT data, embedding FROM master_recipes WHERE id = ?",
                                 (u["id"],)).fetchone()
        d = json.loads(data)
        d["_master"] = u["master"]
        conn.execute("UPDATE master_recipes SET data = ? WHERE id = ?",
                     (json.dumps(d, ensure_ascii=False), u["id"]))
        if emb:
            dish = u["master"].get("dish")
            ch = conn.execute("SELECT chapter FROM dishes WHERE name = ?", (dish,)).fetchone()
            vs.upsert_recipe_vector(conn, u["id"], bytes_to_vec(emb),
                                    chapter=(ch[0] if ch else None), dish=dish)
    conn.commit()
    print(f"restored {len(rows)} label(s) from {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo")
    ap.add_argument("--plan-out", help="write the plan as JSON (for the accuracy check)")
    ap.add_argument("--enforce-margin", action="store_true",
                    help="revert earlier distance moves that fall under the margin")
    a = ap.parse_args()
    c = sqlite3.connect(DB)
    if a.undo:
        undo(c, a.undo)
        sys.exit(0)
    if a.enforce_margin:
        enforce_margin(c, a.apply)
        sys.exit(0)
    p = plan(c, a.limit)
    show(p)
    if a.plan_out:
        json.dump(p, open(a.plan_out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    if a.apply:
        apply(c, p)
    else:
        print("\ndry run — nothing written. Re-run with --apply.")
