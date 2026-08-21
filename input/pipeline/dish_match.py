"""Which canonical dish is this recipe? — one implementation, three callers.

The rule is **infer a dish when the row does not already know one**. It is NOT
"user rows get a match and master rows don't", which is what the save path used
to do: master rows were assumed to carry `_master.dish` because a dish refresh
curates them FOR a dish, and that is true of harvested rows and false of every
interactive capture. Those rows stored a vector and discarded the one thing the
vector was for.

Three callers share this: the save path (`save_recipe_api._stamp_dish_match`),
the backfill script, and the nightly `dish_rematch` job. They were going to
drift — the save path and the first backfill already disagreed about whether to
stamp the vec index — so the logic lives here once.

WHY A SWEEP EXISTS AT ALL. New recipes are matched AT SAVE, so a sweep is not
how new recipes get a dish. What the sweep is for is the other direction: the
DISH CATALOG changes (~45-60 new dishes a month, plus description/query edits
that move a dish's own vector), and a row already carries the best match from a
catalog that no longer exists. Creating "Pumpkin Pie" does not move the pumpkin
pies out of Cream Pie; only a re-score does.

WRITE ONLY ON CHANGE. The sweep re-scores every unclaimed row but writes only
the ones whose verdict actually moved. This matters because it runs on a
schedule: stamping a fresh `matched_at` on every row every night would dirty
thousands of JSON blobs for nothing, inflate the WAL, and make every night's
`recipes.sql` backup diff the size of the table.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from input.pipeline import vector_store
from input.pipeline.embeddings import bytes_to_vec

DEFAULT_MAX_DIST = 0.6
_SETTING = "dish_match_max_distance"


def max_distance(conn_db_path: Optional[str] = None) -> float:
    """The confidence threshold, from system_config.

    Lowered 0.8 -> 0.6 on 2026-08-21 against measured data: agreement between
    the match and the recipe's own identity-card `likelyDish` fell off a cliff
    above ~0.6 (0.5-0.6 band 29% disagreement, 0.6-0.7 65%, 0.75-0.8 85%), and
    the 0.75-0.8 band was returning things like Pumpkin Spice Latte -> Pumpkin
    Pie. Dropping to 0.6 took the overall disagreement rate from 45% to 14%.
    """
    try:
        from input.pipeline import system_config as _cfg
        # db_path passed through EXPLICITLY where the caller knows it:
        # get_setting() with no db_path lazily imports save_recipe_api to read
        # DB_PATH, and that import runs the app's startup, which resets
        # in-flight jobs.
        if conn_db_path:
            return float(_cfg.get_setting(_SETTING, DEFAULT_MAX_DIST,
                                          db_path=conn_db_path))
        return float(_cfg.get_setting(_SETTING, DEFAULT_MAX_DIST))
    except Exception:
        return DEFAULT_MAX_DIST


def build_match(conn: sqlite3.Connection, rec_vec, *, max_dist: float) -> Optional[dict]:
    """The `_match` block for a recipe vector, or None if the dish index is
    empty. Reuses the vector the caller already has — no embed, no API call."""
    cands = vector_store.find_similar_dishes(conn, rec_vec, k=3)
    if not cands:
        return None
    best = cands[0]
    confident = best["distance"] <= max_dist
    return {
        "dish": best["name"] if confident else None,
        "distance": round(best["distance"], 4),
        "confident": confident,
        "candidates": [
            {"dish": m["name"], "distance": round(m["distance"], 4)}
            for m in cands
        ],
        "matched_at": datetime.now(timezone.utc).isoformat(),
    }


def same_verdict(old: Optional[dict], new: Optional[dict]) -> bool:
    """Do two `_match` blocks say the same thing? `matched_at` is excluded on
    purpose — it changes every run and is not part of the verdict, so including
    it would make every row look changed and defeat write-on-change."""
    if not old or not new:
        return False
    return (old.get("dish") == new.get("dish")
            and bool(old.get("confident")) == bool(new.get("confident"))
            and old.get("distance") == new.get("distance"))


def rematch_unclaimed(conn: sqlite3.Connection, *, db_path: Optional[str] = None,
                      limit: int = 0, dry_run: bool = False,
                      only_unmatched: bool = False,
                      log=print) -> dict:
    """Re-score every master row that carries no curated `_master.dish`.

    `only_unmatched=True` restricts to rows that have never been matched (the
    first-pass backfill). The default re-scores rows that already have a match,
    which is the point on a schedule — the catalog moved under them.

    Returns a summary dict; writes only rows whose verdict changed.
    """
    max_dist = max_distance(db_path)
    vector_store.enable_vec(conn)

    sql = ("SELECT id, data, embedding FROM master_recipes "
           " WHERE embedding IS NOT NULL "
           "   AND json_extract(data, '$._master.dish') IS NULL")
    if only_unmatched:
        sql += "   AND json_extract(data, '$._match') IS NULL"
    sql += " ORDER BY id"

    rows = conn.execute(sql).fetchall()
    if limit:
        rows = rows[:limit]

    scanned = confident = changed = failed = 0
    moves: list[tuple] = []

    for n, (rid, dj, blob) in enumerate(rows, 1):
        try:
            d = json.loads(dj)
            prev = d.get("_match") or None
            new = build_match(conn, bytes_to_vec(blob), max_dist=max_dist)
            scanned += 1
            if new is None:
                continue
            if new["confident"]:
                confident += 1
            if same_verdict(prev, new):
                continue                      # <- the whole point: no write

            changed += 1
            moves.append((rid, (prev or {}).get("dish"), new["dish"]))
            if dry_run:
                continue

            d["_match"] = new
            conn.execute("UPDATE master_recipes SET data = ? WHERE id = ?",
                         (json.dumps(d), rid))
            # Unconditional, dish=None when not confident: a row DEMOTED from a
            # confident match would otherwise keep the stale dish in the index
            # while `data` said otherwise, and the KNN filter reads the index.
            ch = ((d.get("classification") or {}).get("chapter") or None)
            vector_store.upsert_recipe_vector(
                conn, rid, bytes_to_vec(blob), chapter=ch, dish=new["dish"])
            if changed % 200 == 0:
                conn.commit()
        except Exception as e:
            failed += 1
            log(f"[REMATCH] row {rid} FAILED: {type(e).__name__}: {e}")

    if not dry_run:
        conn.commit()

    return {
        "scanned": scanned,
        "confident": confident,
        "changed": changed,
        "unchanged": scanned - changed,
        "failed": failed,
        "threshold": max_dist,
        "moves": moves,
    }
