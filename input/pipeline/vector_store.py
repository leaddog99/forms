"""sqlite-vec virtual-table backing for cohort matching + recommender.

Why sqlite-vec: the naive in-Python numpy cosine scan (input.pipeline.
embeddings.find_best_dish_match) works fine at our current scale
(11 dishes, ~300 recipes), but loads every embedding BLOB and scores
every one in Python. That's ~6 MB of bytes round-tripped per query at
the recipe scale we're heading to (10K+ rows). sqlite-vec moves the
KNN search into the database engine: queries like
`WHERE embedding MATCH ? ORDER BY distance LIMIT 5` use a brute-force
scan implemented in C with SIMD, and a `vec0` index when populated
beyond ~1k rows.

This module is the single point that:
  - loads the sqlite-vec extension on a connection
  - creates the vec0 virtual tables (idempotent migrations)
  - upserts vectors as the recipe / dish save paths run
  - provides find_similar_* helpers that JOIN vec0 distances against
    the regular tables (dishes.chapter, master_recipes.classification.chapter,
    last_ou_fit, etc.)

Vec0 quirks worth knowing:
  - Cannot use ON CONFLICT — upsert is DELETE + INSERT.
  - distance is L2 (Euclidean) by default. For L2-normalized inputs
    (which OpenAI text-embedding-3-small returns) L2 is monotonic
    with cosine — sort order matches. We use L2 throughout to avoid
    explicit `vec_distance_cosine(...)` calls in every query.
  - The extension must be loaded on EVERY connection that touches
    vec0 tables. sqlite3.Connection.enable_load_extension(True) is
    required before sqlite_vec.load().
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import numpy as np
import sqlite_vec


EMBED_DIM = 1536  # text-embedding-3-small


def enable_vec(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec onto a connection. Idempotent — safe to call
    multiple times (sqlite3 short-circuits double-loads of the same
    extension)."""
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def ensure_vec_tables(conn: sqlite3.Connection) -> None:
    """Create the vec0 virtual tables if absent. One per logical
    entity that needs KNN — currently dishes + master_recipes.
    Personal recipes (`recipes` table) get added later when the
    recommender expands to them.

    rowid mapping:
      dishes_vec.rowid     ↔ a hash of dishes.name (case-insensitive,
                              stable across renames-of-different-case)
      recipes_master_vec.rowid ↔ master_recipes.id (integer PK)

    For dishes we keep an explicit `name TEXT` aux column to make
    JOINs against dishes obvious — vec0 supports text columns
    alongside the embedding for filter + projection.
    """
    enable_vec(conn)
    # dishes_vec: keyed by dish name (lowercase-canonical for stable
    # lookups). embedding float[1536] is the OpenAI vector.
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS dishes_vec USING vec0(
            name TEXT PRIMARY KEY,
            embedding float[{EMBED_DIM}]
        )
    """)
    # recipes_master_vec: one row per master recipe with an embedding.
    # Keyed by recipes.id so JOINs are integer-fast. Chapter is
    # stored as an auxiliary column so chapter-filtered KNN runs
    # in one query instead of needing a separate JOIN. Personal
    # recipes will get their own vec0 table when the recommender
    # extends to them.
    conn.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS recipes_master_vec USING vec0(
            id INTEGER PRIMARY KEY,
            embedding float[{EMBED_DIM}],
            +chapter TEXT,
            +dish TEXT
        )
    """)
    conn.commit()
    ensure_vec_triggers(conn)


def ensure_vec_triggers(conn: sqlite3.Connection) -> None:
    """Create the AFTER DELETE triggers that keep the vec0 index tables
    in lockstep with their base tables — recipes_master_vec↔master_recipes
    (keyed by id) and dishes_vec↔dishes (keyed by name). Without them,
    deleting a base row orphans its vector: there's no FK between a base
    table and a vec0 virtual table (and vec0 can't be an FK target), so
    SQLite has nothing to cascade.

    IMPORTANT: the trigger bodies delete from vec0 tables, so ANY
    connection that deletes from master_recipes / dishes must have
    sqlite-vec loaded (enable_vec) or the DELETE fails. That's
    intentional — a loud failure beats silently accumulating orphans.
    All app delete paths load the extension; ensure_vec_tables runs at
    startup with it loaded.
    """
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_master_vec_cleanup
        AFTER DELETE ON master_recipes
        BEGIN
            DELETE FROM recipes_master_vec WHERE id = OLD.id;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_dish_vec_cleanup
        AFTER DELETE ON dishes
        BEGIN
            DELETE FROM dishes_vec WHERE name = OLD.name;
        END
        """
    )
    conn.commit()


def prune_orphaned_master_vectors(conn: sqlite3.Connection) -> int:
    """Delete any recipes_master_vec rows whose id no longer exists in
    master_recipes. The AFTER DELETE trigger prevents NEW orphans; this
    is the one-shot / safety-net sweep that cleans pre-trigger debris.
    Returns the number of vectors pruned."""
    enable_vec(conn)
    cur = conn.execute(
        "DELETE FROM recipes_master_vec "
        "WHERE id NOT IN (SELECT id FROM master_recipes)"
    )
    conn.commit()
    return cur.rowcount


def rebuild_master_vec_from_blobs(conn: sqlite3.Connection) -> int:
    """Rebuild recipes_master_vec entirely from the source-of-truth
    `master_recipes.embedding` BLOB column — the free, offline,
    API-less regeneration path (mirrors how dishes_vec rebuilds from
    dishes.embedding). Clears the vec table first, so this also drops
    any orphans as a side effect. Returns the number of vectors written.

    chapter/dish aux columns are derived from the row JSON so the
    chapter-filtered KNN + same-dish-exclusion still work after a
    rebuild. Rows with a NULL embedding BLOB are skipped (no vector to
    index yet)."""
    import json as _json
    enable_vec(conn)
    conn.execute("DELETE FROM recipes_master_vec")
    rows = conn.execute(
        "SELECT id, data, embedding FROM master_recipes "
        "WHERE embedding IS NOT NULL"
    ).fetchall()
    written = 0
    for rid, data_json, blob in rows:
        # Decode inline with numpy (vector_store already imports np);
        # embeddings.bytes_to_vec lives in a module that imports THIS one,
        # so importing it here would be circular.
        vec = np.frombuffer(blob, dtype="float32") if blob else None
        if vec is None or vec.size != EMBED_DIM:
            continue
        try:
            d = _json.loads(data_json) if data_json else {}
        except Exception:
            d = {}
        chapter = ((d.get("classification") or {}).get("chapter") or None)
        dish = ((d.get("_master") or {}).get("dish") or None)
        upsert_recipe_vector(conn, rid, vec, chapter=chapter, dish=dish)
        written += 1
    return written


# === Dish vec0 helpers ======================================================


def upsert_dish_vector(conn: sqlite3.Connection, name: str,
                       embedding: np.ndarray) -> None:
    """Replace the vec0 row for a dish. Uses DELETE + INSERT because
    vec0 doesn't support ON CONFLICT. Idempotent."""
    arr = np.asarray(embedding, dtype="float32")
    if arr.size != EMBED_DIM:
        raise ValueError(f"embedding wrong dim: {arr.size} != {EMBED_DIM}")
    conn.execute("DELETE FROM dishes_vec WHERE name = ?", (name,))
    conn.execute("INSERT INTO dishes_vec(name, embedding) VALUES (?, ?)",
                 (name, arr.tobytes()))
    conn.commit()


def delete_dish_vector(conn: sqlite3.Connection, name: str) -> None:
    """Drop a dish's vec0 row when the dish itself is deleted."""
    conn.execute("DELETE FROM dishes_vec WHERE name = ?", (name,))
    conn.commit()


def find_similar_dishes(conn: sqlite3.Connection, query_vec: np.ndarray,
                        *, k: int = 5,
                        chapter: Optional[str] = None) -> list[dict]:
    """KNN search over dishes_vec, optionally filtered to a chapter.
    Returns a list of {name, distance, ou_fit} dicts in
    distance-ascending order.

    Vec0 v0.1.9 disallows aux-column equality inside KNN, BUT
    `<pk> IN (<subselect>)` IS honored as a pre-filter — vec0 scans
    only the PKs returned by the subselect rather than the whole
    table. So chapter filtering uses a subselect on the regular
    dishes table (where chapter is a normal column), and the KNN
    scan only considers those PKs. No over-fetch, no post-filter
    waste. K stays the requested K.

    Distance is L2 on raw float32 bytes. OpenAI
    text-embedding-3-small returns L2-normalized vectors, so L2
    order matches cosine order — smaller distance = more similar.
    """
    arr = np.asarray(query_vec, dtype="float32").tobytes()

    if chapter:
        sql = """
            SELECT v.name, v.distance, d.last_ou_fit
            FROM dishes_vec v
            JOIN dishes d ON d.name = v.name
            WHERE v.embedding MATCH ? AND k = ?
              AND v.name IN (SELECT name FROM dishes WHERE chapter = ?)
            ORDER BY v.distance
        """
        params = (arr, k, chapter)
    else:
        sql = """
            SELECT v.name, v.distance, d.last_ou_fit
            FROM dishes_vec v
            JOIN dishes d ON d.name = v.name
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance
        """
        params = (arr, k)

    rows = conn.execute(sql, params).fetchall()

    import json as _json
    out: list[dict] = []
    for name, dist, ou_json in rows:
        try:
            fit = _json.loads(ou_json) if ou_json else None
        except Exception:
            fit = None
        out.append({
            "name": name,
            "distance": float(dist),
            "ou_fit": fit,
        })
    return out


# === Master-recipe vec0 helpers =============================================


def upsert_recipe_vector(conn: sqlite3.Connection, recipe_id: int,
                         embedding: np.ndarray, *,
                         chapter: Optional[str] = None,
                         dish: Optional[str] = None) -> None:
    """Replace the vec0 row for a master recipe. recipe_id is the
    integer PK from master_recipes.id. chapter + dish are aux columns
    used to pre-filter KNN queries (sidebar-style "more like this"
    recommender, scope = same chapter, exclude same dish).
    """
    arr = np.asarray(embedding, dtype="float32")
    if arr.size != EMBED_DIM:
        raise ValueError(f"embedding wrong dim: {arr.size} != {EMBED_DIM}")
    conn.execute("DELETE FROM recipes_master_vec WHERE id = ?", (recipe_id,))
    conn.execute(
        "INSERT INTO recipes_master_vec(id, embedding, chapter, dish) "
        "VALUES (?, ?, ?, ?)",
        (recipe_id, arr.tobytes(), chapter, dish),
    )
    conn.commit()


def delete_recipe_vector(conn: sqlite3.Connection, recipe_id: int) -> None:
    """Drop a master recipe's vec0 row when the row is deleted."""
    conn.execute("DELETE FROM recipes_master_vec WHERE id = ?", (recipe_id,))
    conn.commit()


def find_similar_master_recipes(conn: sqlite3.Connection,
                                 query_vec: np.ndarray, *,
                                 k: int = 5,
                                 chapter: Optional[str] = None,
                                 exclude_id: Optional[int] = None,
                                 exclude_dish: Optional[str] = None
                                 ) -> list[dict]:
    """KNN search over master recipe vectors. Used by the
    "We Think You'd Like" recommender — pass the current recipe's
    embedding, get the K most similar OTHER master recipes.

    Vec0 v0.1.9 disallows aux-column equality inside KNN, BUT
    `id IN (<subselect>)` IS honored as a pre-filter. We compose a
    single subselect on `recipes_master_vec` itself (its `+chapter`
    and `+dish` aux columns are queryable in a non-KNN context) so
    vec0 scans ONLY the matching PKs — no over-fetch, K stays the
    requested K. Significantly tighter than the previous over-fetch
    + Python post-filter approach.

    Returns [{id, distance, chapter, dish}] in distance-ascending
    order.
    """
    arr = np.asarray(query_vec, dtype="float32").tobytes()

    # Build the pre-filter subselect on the same table so we can
    # consult the aux columns (chapter, dish). Always exclude
    # exclude_id (typically the current recipe so it doesn't recommend
    # itself). When no filter applies, the subselect is just "all rows
    # not the excluded one" — still pushes the constraint into vec0's
    # pre-filter rather than fetching everything.
    sub_clauses: list[str] = []
    sub_params: list = []
    if chapter is not None:
        sub_clauses.append("chapter = ?")
        sub_params.append(chapter)
    if exclude_dish is not None:
        sub_clauses.append("(dish IS NULL OR dish != ?)")
        sub_params.append(exclude_dish)
    if exclude_id is not None:
        sub_clauses.append("id != ?")
        sub_params.append(exclude_id)

    if sub_clauses:
        subselect = "SELECT id FROM recipes_master_vec WHERE " + " AND ".join(sub_clauses)
        sql = (
            "SELECT id, distance, chapter, dish FROM recipes_master_vec "
            "WHERE embedding MATCH ? AND k = ? AND id IN (" + subselect + ") "
            "ORDER BY distance"
        )
        rows = conn.execute(sql, [arr, k, *sub_params]).fetchall()
    else:
        sql = (
            "SELECT id, distance, chapter, dish FROM recipes_master_vec "
            "WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance"
        )
        rows = conn.execute(sql, (arr, k)).fetchall()

    return [
        {"id": r[0], "distance": float(r[1]),
         "chapter": r[2], "dish": r[3]}
        for r in rows
    ]
