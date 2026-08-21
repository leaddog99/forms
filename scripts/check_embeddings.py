"""Embedding-system regression check. Run on demand or daily.

WHY (2026-08-06): a recipe-order bug in `_derive_primary_ingredients` put an
incidental ingredient at the head of the embed text for 41% of the master
corpus, and nothing surfaced it — the recommender just quietly answered a
mussel recipe with french fries. Every invariant below was checkable the whole
time. This script makes the check cheap enough to run every day.

Exits non-zero when any check FAILS, so it can be wired to a scheduled job.
Read-only: never writes to recipes.db.

Checks:
  1. dimension        every stored vector is EMBED_DIM floats
  2. norm             vectors are L2-normalized (the cosine<->L2 identity in
                      _l2_to_cosine_sim depends on it)
  3. zero vectors     an empty-text row embedded as all-zeros ranks
                      arbitrarily in KNN
  4. index agreement  vec0 rows match the source-of-truth BLOB
  5. orphans          vec0 <-> base table, both directions
  6. coverage         rows with no embedding at all
  7. staleness        stored embedding_text_hash still matches the text the
                      current composer produces  <-- catches the 2026-08-06 bug
  8. model drift      one embedding model across the corpus

Usage:
  python -m scripts.check_embeddings
  python -m scripts.check_embeddings --sample 2000   # deeper index sampling
  python -m scripts.check_embeddings --quiet         # only failures
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402
load_dotenv()

from input.pipeline import vector_store  # noqa: E402
from input.pipeline.embeddings import (  # noqa: E402
    EMBED_DIM, EMBED_MODEL, bytes_to_vec, compose_recipe_text, text_hash,
)

DB_PATH = str(PROJECT_ROOT / "recipes.db")

FAILURES: list[str] = []
WARNINGS: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  [FAIL] {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  [warn] {msg}")


def ok(msg: str, quiet: bool) -> None:
    if not quiet:
        print(f"  [ok]   {msg}")


def _text_hash(text: str) -> str:
    # Delegates to the canonical definition so this check can never disagree
    # with the writer it is checking (2026-08-21: it did, for every saved row).
    return text_hash(text)


def check_vectors(conn, table, quiet):
    try:
        rows = conn.execute(
            f"SELECT embedding FROM {table} WHERE embedding IS NOT NULL").fetchall()
    except Exception as e:
        warn(f"{table}: not readable ({e})")
        return
    if not rows:
        warn(f"{table}: no embeddings stored")
        return
    bad_dim = zero = off_norm = 0
    for (b,) in rows:
        v = bytes_to_vec(b)
        if v is None or v.size != EMBED_DIM:
            bad_dim += 1
            continue
        n = float(np.linalg.norm(v))
        if n < 1e-6:
            zero += 1
        elif abs(n - 1.0) > 0.01:
            off_norm += 1
    if bad_dim:
        fail(f"{table}: {bad_dim} vectors with wrong dimension (expected {EMBED_DIM})")
    if zero:
        fail(f"{table}: {zero} ZERO vectors — these rank arbitrarily in KNN")
    if off_norm:
        fail(f"{table}: {off_norm} vectors not L2-normalized "
             f"(breaks the cosine<->L2 conversion)")
    if not (bad_dim or zero or off_norm):
        ok(f"{table}: {len(rows)} vectors — dim, norm, non-zero all clean", quiet)


def check_coverage(conn, table, quiet):
    try:
        tot = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        missing = conn.execute(
            f"SELECT count(*) FROM {table} WHERE embedding IS NULL").fetchone()[0]
    except Exception as e:
        warn(f"{table}: coverage unreadable ({e})")
        return
    if missing:
        warn(f"{table}: {missing}/{tot} rows have NO embedding "
             f"(invisible to similarity search)")
    else:
        ok(f"{table}: all {tot} rows embedded", quiet)


def check_index(conn, base, idcol, vec_table, sample, quiet, veccol="id"):
    """vec0 index must agree with the source-of-truth BLOB, and neither side
    may carry rows the other doesn't. `veccol` is the vec0 primary key, which
    is NOT always `id` — dishes_vec keys on name, products_vec on product_id.
    """
    try:
        extra = conn.execute(
            f"SELECT count(*) FROM {vec_table} WHERE {veccol} NOT IN "
            f"(SELECT {idcol} FROM {base})").fetchone()[0]
        absent = conn.execute(
            f"SELECT count(*) FROM {base} WHERE embedding IS NOT NULL AND {idcol} "
            f"NOT IN (SELECT {veccol} FROM {vec_table})").fetchone()[0]
    except Exception as e:
        warn(f"{vec_table}: orphan check failed ({e})")
        return
    if extra:
        fail(f"{vec_table}: {extra} orphaned index rows with no {base} row "
             f"(delete trigger not firing?)")
    if absent:
        fail(f"{base}: {absent} embedded rows missing from {vec_table} "
             f"(will never be returned by KNN)")
    if not (extra or absent):
        ok(f"{vec_table}: no orphans in either direction", quiet)

    ids = [r[0] for r in conn.execute(f"SELECT {idcol} FROM {base} "
                                      f"WHERE embedding IS NOT NULL")]
    if not ids:
        return
    step = max(1, len(ids) // max(sample, 1))
    drift = checked = 0
    for i in ids[::step]:
        b = conn.execute(f"SELECT embedding FROM {base} WHERE {idcol} = ?", (i,)).fetchone()
        r = conn.execute(f"SELECT embedding FROM {vec_table} WHERE {veccol} = ?", (i,)).fetchone()
        if not b or not r:
            continue
        checked += 1
        if float(np.linalg.norm(bytes_to_vec(b[0]) - np.frombuffer(r[0], dtype="float32"))) > 1e-5:
            drift += 1
    if drift:
        fail(f"{vec_table}: {drift}/{checked} sampled vectors DISAGREE with the "
             f"{base}.embedding BLOB — index is stale")
    else:
        ok(f"{vec_table}: {checked} sampled vectors match the source BLOB", quiet)


def check_staleness(conn, table, quiet):
    """The check that would have caught the 2026-08-06 ordering bug: does the
    stored embedding still correspond to what the CURRENT composer produces?"""
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "embedding_text_hash" not in have:
        warn(f"{table}: no embedding_text_hash column — staleness is UNDETECTABLE. "
             f"Run: python -m scripts.reembed_identity")
        return
    rows = conn.execute(
        f"SELECT id, data, embedding_text_hash FROM {table} "
        f"WHERE embedding IS NOT NULL").fetchall()
    stale = unstamped = 0
    for rid, dj, h in rows:
        if not h:
            unstamped += 1
            continue
        try:
            d = json.loads(dj)
        except Exception:
            continue
        if _text_hash(compose_recipe_text(d)) != h:
            stale += 1
    if stale:
        fail(f"{table}: {stale} embeddings STALE — the composer now produces "
             f"different text than what was embedded. Run scripts.reembed_identity")
    if unstamped:
        warn(f"{table}: {unstamped} embeddings have no text hash (provenance not "
             f"backfilled yet)")
    if not (stale or unstamped):
        ok(f"{table}: all {len(rows)} embeddings match current composer output", quiet)


def check_model(conn, table, quiet):
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "embedding_model" not in have:
        return
    rows = conn.execute(
        f"SELECT COALESCE(embedding_model,'(unset)'), count(*) FROM {table} "
        f"WHERE embedding IS NOT NULL GROUP BY 1").fetchall()
    others = [r for r in rows if r[0] not in (EMBED_MODEL, "(unset)")]
    if others:
        fail(f"{table}: mixed embedding models {others} — vectors from different "
             f"models are NOT comparable")
    else:
        ok(f"{table}: single model ({EMBED_MODEL})", quiet)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sample", type=int, default=600,
                   help="how many rows to sample for index-vs-BLOB agreement")
    p.add_argument("--quiet", action="store_true", help="print only failures/warnings")
    args = p.parse_args()

    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        vector_store.enable_vec(conn)
    except Exception as e:
        fail(f"sqlite-vec failed to load: {e}")

    print("=== vector integrity ===")
    for t in ("master_recipes", "recipes", "dishes", "products"):
        check_vectors(conn, t, args.quiet)

    print("\n=== coverage ===")
    for t in ("master_recipes", "recipes", "dishes", "products"):
        check_coverage(conn, t, args.quiet)

    print("\n=== index agreement ===")
    check_index(conn, "master_recipes", "id", "recipes_master_vec", args.sample, args.quiet)
    check_index(conn, "dishes", "name", "dishes_vec", args.sample, args.quiet,
                veccol="name")
    check_index(conn, "products", "product_id", "products_vec", args.sample,
                args.quiet, veccol="product_id")

    print("\n=== staleness (composer drift) ===")
    for t in ("master_recipes", "recipes"):
        check_staleness(conn, t, args.quiet)

    print("\n=== model consistency ===")
    for t in ("master_recipes", "recipes", "dishes"):
        check_model(conn, t, args.quiet)

    print()
    print(f"=== {len(FAILURES)} failure(s), {len(WARNINGS)} warning(s) ===")
    for f in FAILURES:
        print(f"  FAIL: {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
