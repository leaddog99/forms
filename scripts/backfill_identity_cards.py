"""One-shot backfill: generate identity cards for existing dishes +
recipes, then re-embed everything so the cohort matcher uses the new
card-derived embed text.

Order matters:
  1. Dishes — card → re-embed → update dishes.embedding + dishes_vec
  2. Master recipes — card → re-embed → update recipes_master_vec
  3. Personal recipes — card → (no vec0 today, but the card itself
     lets the matcher work)

Cost: ~$0.0001 per row × ~340 rows ≈ $0.04 total. Wall: ~10-15 min.
Idempotent: skips rows that already carry a card.

Usage:
  python -m scripts.backfill_identity_cards --dry-run
  python -m scripts.backfill_identity_cards --limit 5
  python -m scripts.backfill_identity_cards            # full run
  python -m scripts.backfill_identity_cards --force    # re-card every row
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

import numpy as np  # noqa: E402

from extract.identity_card import (  # noqa: E402
    generate_identity_card_for_recipe,
    generate_identity_card_for_dish,
)
from input.pipeline.embeddings import (  # noqa: E402
    compose_recipe_text, compose_dish_text, embed_text,
)
from input.pipeline import vector_store  # noqa: E402
from input.pipeline import dishes as dishes_lib  # noqa: E402

DB_PATH = str(PROJECT_ROOT / "recipes.db")


def _has_card(d: dict) -> bool:
    card = d.get("_identity") if isinstance(d, dict) else None
    return isinstance(card, dict) and bool((card.get("likelyDish") or "").strip())


def _process_dishes(conn: sqlite3.Connection, *,
                    dry_run: bool, limit: int, force: bool) -> Counter:
    rows = dishes_lib.list_dishes(conn)
    print(f"--- dishes: {len(rows)} rows ---")
    counts: Counter = Counter()
    updated = 0
    t0 = time.perf_counter()

    for d in rows:
        name = d["name"]
        existing_card = d.get("identity_card")
        if not force and isinstance(existing_card, dict) and (existing_card.get("likelyDish") or "").strip():
            counts["already"] += 1
            continue
        try:
            card = generate_identity_card_for_dish(d)
        except Exception as e:
            print(f"  [error] dish {name!r}: {e}")
            counts["error"] += 1
            continue
        if not card:
            counts["no-card"] += 1
            continue
        counts["carded"] += 1
        d["identity_card"] = card
        # Re-embed from the card
        text = compose_dish_text(d)
        vec = embed_text(text)
        if not dry_run:
            conn.execute(
                "UPDATE dishes SET identity_card = ?, embedding = ?, "
                "embedding_text = ?, embedding_updated_at = "
                "datetime('now'), updated_at = datetime('now') "
                "WHERE name = ?",
                (json.dumps(card), vec.astype(np.float32).tobytes(),
                 text, name),
            )
            vector_store.upsert_dish_vector(conn, name, vec)
            conn.commit()
        updated += 1
        print(f"  [ok] {name!r:30s} likelyDish={card.get('likelyDish')!r}")
        if limit and updated >= limit:
            print(f"  reached limit ({limit})")
            break

    print(f"  dishes done: {dict(counts)} in {time.perf_counter()-t0:.1f}s")
    return counts


def _process_recipes(conn: sqlite3.Connection, *, table: str,
                     dry_run: bool, limit: int, force: bool) -> Counter:
    rows = conn.execute(
        f"SELECT id, recipe_id, data FROM {table} ORDER BY id"
    ).fetchall()
    print(f"--- {table}: {len(rows)} rows ---")
    counts: Counter = Counter()
    updated = 0
    t0 = time.perf_counter()
    is_master = (table == "master_recipes")

    for rid, _ruuid, dj in rows:
        try:
            d = json.loads(dj)
        except Exception:
            counts["error"] += 1
            continue
        if not force and _has_card(d):
            counts["already"] += 1
            continue

        try:
            card = generate_identity_card_for_recipe(d)
        except Exception as e:
            print(f"  [error] id={rid}: {e}")
            counts["error"] += 1
            continue
        if not card:
            counts["no-card"] += 1
            continue

        d["_identity"] = card
        # Keep classification.dishSignal in sync for backward compat
        cls = d.get("classification") or {}
        cls["dishSignal"] = (card.get("likelyDish") or "").strip()
        d["classification"] = cls

        counts["carded"] += 1
        # Re-embed from card (for master rows only — recipes_master_vec
        # is populated; personal recipes don't have a vec table today)
        if is_master:
            text = compose_recipe_text(d)
            vec = embed_text(text)
            if not dry_run:
                conn.execute(
                    "UPDATE master_recipes SET data = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE id = ?",
                    (json.dumps(d, indent=2), rid),
                )
                ch = (d.get("classification") or {}).get("chapter") or None
                dish_for_vec = (d.get("_master") or {}).get("dish") or None
                vector_store.upsert_recipe_vector(
                    conn, rid, vec, chapter=ch, dish=dish_for_vec,
                )
                conn.commit()
        else:
            if not dry_run:
                conn.execute(
                    "UPDATE recipes SET data = ?, "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE id = ?",
                    (json.dumps(d, indent=2), rid),
                )
                conn.commit()

        updated += 1
        if updated % 20 == 0:
            print(f"  ... {updated} carded so far ({rid=})")
        if limit and updated >= limit:
            print(f"  reached limit ({limit})")
            break

    print(f"  {table} done: {dict(counts)} in {time.perf_counter()-t0:.1f}s")
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="cap per table")
    p.add_argument("--force", action="store_true",
                   help="re-card rows that already have a card")
    p.add_argument("--dishes-only", action="store_true")
    p.add_argument("--master-only", action="store_true")
    p.add_argument("--personal-only", action="store_true")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH)
    vector_store.enable_vec(conn)
    vector_store.ensure_vec_tables(conn)

    grand = Counter()

    if args.dishes_only:
        grand.update(_process_dishes(conn, dry_run=args.dry_run, limit=args.limit, force=args.force))
    elif args.master_only:
        grand.update(_process_recipes(conn, table="master_recipes",
                                       dry_run=args.dry_run, limit=args.limit,
                                       force=args.force))
    elif args.personal_only:
        grand.update(_process_recipes(conn, table="recipes",
                                       dry_run=args.dry_run, limit=args.limit,
                                       force=args.force))
    else:
        grand.update(_process_dishes(conn, dry_run=args.dry_run, limit=args.limit, force=args.force))
        grand.update(_process_recipes(conn, table="master_recipes",
                                       dry_run=args.dry_run, limit=args.limit,
                                       force=args.force))
        grand.update(_process_recipes(conn, table="recipes",
                                       dry_run=args.dry_run, limit=args.limit,
                                       force=args.force))

    print()
    print(f"=== TOTAL: {dict(grand)} ===")
    if args.dry_run:
        print("(dry-run — no writes)")


if __name__ == "__main__":
    main()
