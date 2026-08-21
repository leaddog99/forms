"""Re-derive identity-card primaryIngredients and re-embed.

WHY (2026-08-06): `_derive_primary_ingredients` used to return primaries in
recipe-listing order, so an incidental ingredient could lead the embed text —
Moules Frites embedded as `primary: yukon gold potatoes, mussels, ...` because
the aioli and fries came first in the ingredient list, and the recommender
answered it with french fries and mashed potato. The derivation now ranks by
INGREDIENT_ROLES position (main_protein before primary_vegetable before
starch). `primaryIngredients` is STAMPED on each card, so existing rows keep
the old order until this script rewrites them.

It re-derives from the card's stored `ingredientRoles` — a pure function, no
LLM call, and `ingredientRoles` is never modified, so the whole pass is
reversible by reverting the code and re-running.

Also fills rows that have no embedding at all, and stamps embedding
provenance (model + text hash + timestamp) so a stale vector becomes
DETECTABLE — `dishes` already had these columns; the recipe/product tables
did not, which is why this class of bug went unnoticed. See
scripts/check_embeddings.py for the recurring check.

Cost: text-embedding-3-small, ~62 tokens/row -> ~$0.006 for the full corpus.
Wall time is API-bound, roughly 10-25 min for ~4.8k rows.

Usage:
  python -m scripts.reembed_identity --dry-run              # report only, no API calls, no writes
  python -m scripts.reembed_identity --dry-run --verbose    # + show sample text diffs
  python -m scripts.reembed_identity --limit 20             # small live batch first
  python -m scripts.reembed_identity                        # master + personal
  python -m scripts.reembed_identity --products             # also backfill unembedded products
  python -m scripts.reembed_identity --force                # re-embed even if the text is unchanged

Resumable: a row whose stored embedding_text_hash already matches the newly
composed text is skipped, so re-running after an interruption costs nothing.
"""
from __future__ import annotations

import argparse
import hashlib
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

from extract.identity_card import _derive_primary_ingredients  # noqa: E402
from input.pipeline.embeddings import (  # noqa: E402
    EMBED_MODEL, compose_recipe_text, embed_text, text_hash, vec_to_bytes,
)
from input.pipeline import vector_store  # noqa: E402

DB_PATH = str(PROJECT_ROOT / "recipes.db")

# Additive, idempotent. Mirrors the columns `dishes` already carries.
PROVENANCE_COLUMNS = (
    ("embedding_model", "TEXT"),
    ("embedding_text_hash", "TEXT"),
    ("embedding_updated_at", "TEXT"),
)


def _text_hash(text: str) -> str:
    # Canonical definition lives in input.pipeline.embeddings — see text_hash.
    return text_hash(text)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def ensure_provenance_columns(conn: sqlite3.Connection, table: str) -> None:
    """Add embedding provenance columns if absent. Additive only — never
    touches existing data, and safe against the generated columns on the
    recipe tables."""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in PROVENANCE_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            print(f"  [migrate] {table}.{name} added")
    conn.commit()


def _process_recipe_table(conn: sqlite3.Connection, table: str, *,
                          dry_run: bool, limit: int, force: bool,
                          verbose: bool) -> Counter:
    is_master = (table == "master_recipes")
    counts: Counter = Counter()
    # The provenance column may not exist yet on a --dry-run (the migration
    # only runs on a live pass), so select a NULL placeholder instead.
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    hash_col = "embedding_text_hash" if "embedding_text_hash" in have else "NULL"
    rows = conn.execute(
        f"SELECT id, data, embedding, {hash_col} FROM {table} ORDER BY id"
    ).fetchall()
    print(f"--- {table}: {len(rows)} rows ---")
    t0 = time.perf_counter()
    done = 0

    for rid, dj, emb, old_hash in rows:
        try:
            d = json.loads(dj)
        except Exception:
            counts["bad-json"] += 1
            continue

        card = d.get("_identity") or {}
        roles = card.get("ingredientRoles")
        changed_order = False

        if isinstance(card, dict) and roles:
            new_primary = _derive_primary_ingredients(roles)
            if new_primary != (card.get("primaryIngredients") or []):
                changed_order = True
        else:
            counts["no-card"] += 1
            new_primary = None

        # Compose the text we WOULD store, with the re-derived order applied.
        candidate = d
        if changed_order:
            candidate = json.loads(dj)
            candidate["_identity"]["primaryIngredients"] = new_primary
        new_text = compose_recipe_text(candidate)
        if not new_text.strip():
            counts["empty-text"] += 1
            continue
        new_hash = _text_hash(new_text)

        needs_embed = force or emb is None or old_hash != new_hash
        if not needs_embed and not changed_order:
            counts["already-current"] += 1
            continue

        if verbose and changed_order:
            print(f"  [{table}.{rid}] {str(d.get('name'))[:44]}")
            print(f"      old primary: {card.get('primaryIngredients')}")
            print(f"      new primary: {new_primary}")

        counts["reordered" if changed_order else "reembed-only"] += 1
        if emb is None:
            counts["was-unembedded"] += 1

        if dry_run:
            done += 1
            if limit and done >= limit:
                print(f"  reached limit ({limit})")
                break
            continue

        try:
            vec = embed_text(new_text)
        except Exception as e:
            print(f"  [error] {table}.{rid} embed failed: {e}")
            counts["embed-error"] += 1
            continue

        if changed_order:
            candidate_json = json.dumps(candidate, indent=2)
            conn.execute(f"UPDATE {table} SET data = ? WHERE id = ?", (candidate_json, rid))
        conn.execute(
            f"UPDATE {table} SET embedding = ?, embedding_model = ?, "
            f"embedding_text_hash = ?, embedding_updated_at = ? WHERE id = ?",
            (vec_to_bytes(vec), EMBED_MODEL, new_hash, _now(), rid),
        )
        if is_master:
            # Keep the KNN index in lockstep with the source-of-truth BLOB.
            ch = ((candidate.get("classification") or {}).get("chapter") or None)
            dish = (candidate.get("_master") or {}).get("dish") or None
            vector_store.upsert_recipe_vector(conn, rid, vec, chapter=ch, dish=dish)
        conn.commit()

        counts["written"] += 1
        done += 1
        if done % 25 == 0:
            el = time.perf_counter() - t0
            print(f"  ... {done} processed ({done/el:.1f}/s)")
        if limit and done >= limit:
            print(f"  reached limit ({limit})")
            break

    print(f"  {table} done: {dict(counts)} in {time.perf_counter()-t0:.1f}s")
    return counts


def _process_products(conn: sqlite3.Connection, *, dry_run: bool,
                      limit: int, force: bool) -> Counter:
    from intake.products.catalog_store import compose_product_text
    counts: Counter = Counter()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    sel = "SELECT product_id, embedding FROM products ORDER BY rowid"
    rows = conn.execute(sel).fetchall()
    print(f"--- products: {len(rows)} rows ---")
    done = 0
    for pid, emb in rows:
        if emb is not None and not force:
            counts["already-embedded"] += 1
            continue
        row = conn.execute("SELECT * FROM products WHERE product_id = ?", (pid,)).fetchone()
        rec = dict(zip([d[0] for d in conn.execute(
            "SELECT * FROM products LIMIT 1").description], row))
        text = compose_product_text(rec)
        if not (text or "").strip():
            counts["empty-text"] += 1
            continue
        counts["to-embed"] += 1
        if dry_run:
            done += 1
            if limit and done >= limit:
                break
            continue
        try:
            vec = embed_text(text)
        except Exception as e:
            print(f"  [error] product {pid}: {e}")
            counts["embed-error"] += 1
            continue
        conn.execute("UPDATE products SET embedding = ? WHERE product_id = ?",
                     (vec_to_bytes(vec), pid))
        try:
            vector_store.upsert_product_vector(
                conn, pid, vec,
                product_class=rec.get("product_class"), category=rec.get("category"))
        except Exception as e:
            print(f"  [warn] product vec upsert {pid}: {e}")
        conn.commit()
        counts["written"] += 1
        done += 1
        if limit and done >= limit:
            break
    print(f"  products done: {dict(counts)}")
    return counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="report only — no API calls, no writes")
    p.add_argument("--verbose", action="store_true", help="show sample reorderings")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--force", action="store_true",
                   help="re-embed even when the composed text is unchanged")
    p.add_argument("--master-only", action="store_true")
    p.add_argument("--personal-only", action="store_true")
    p.add_argument("--products", action="store_true",
                   help="also backfill products that have no embedding")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        vector_store.enable_vec(conn)
    except Exception as e:
        print(f"[warn] sqlite-vec unavailable ({e}) — index will NOT be updated")

    grand: Counter = Counter()
    tables = []
    if not args.personal_only:
        tables.append("master_recipes")
    if not args.master_only:
        tables.append("recipes")

    for t in tables:
        if not args.dry_run:
            ensure_provenance_columns(conn, t)
        else:
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}
            missing = [n for n, _ in PROVENANCE_COLUMNS if n not in have]
            if missing:
                print(f"  [migrate-pending] {t}: would add {missing}")
        grand.update(_process_recipe_table(
            conn, t, dry_run=args.dry_run, limit=args.limit,
            force=args.force, verbose=args.verbose))

    if args.products:
        grand.update(_process_products(
            conn, dry_run=args.dry_run, limit=args.limit, force=args.force))

    print()
    print(f"=== TOTAL: {dict(grand)} ===")
    if args.dry_run:
        est = grand.get("reordered", 0) + grand.get("reembed-only", 0) + grand.get("to-embed", 0)
        print(f"(dry-run — no writes. {est} rows would be embedded, "
              f"~${est*62*0.02/1e6:.4f})")


if __name__ == "__main__":
    main()
