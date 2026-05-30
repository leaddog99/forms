"""Dish library — the dishes table + helpers.

A dish is the unit of curated top-recipe collection. Each row in this
table maps a canonical dish name (e.g. "Spaghetti and Meat Sauce") to
a set of SerpAPI queries that populate it, plus tuning + refresh
metadata.

The dish name is the IMMUTABLE primary key — every recipe in
master_recipes that came from a batch refresh stamps `_master.dish`
with this name, and the delete-and-replace logic uses it as the join
key. Renaming would orphan all those rows; that's why we forbid it
(callers delete + recreate to "rename" — which also deletes the
master rows, intentionally).

Both the admin form-driven refresh button AND the cron-fired
`refresh_due_dishes.py` agent operate on this table:
  - Form: list + create + edit + delete + manual refresh
  - Agent: scans for due dishes (refresh_ttl_days elapsed since
           last_refreshed) and runs each through the same in-process
           build_batch + delete-and-replace as the form.

See memory/project_dish_library.md for the broader design.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


def _enable_vec_best_effort(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec on `conn` so the vec-cleanup AFTER DELETE triggers
    can run. Lazy import keeps dishes.py importable without the extension
    (tests, tooling); swallow failures — if vec is truly absent there's
    no index to keep in sync."""
    try:
        from input.pipeline import vector_store
        vector_store.enable_vec(conn)
    except Exception as e:
        print(f"[VEC] enable_vec (dishes delete) skipped: {e}")


def ensure_dishes_table(conn: sqlite3.Connection) -> None:
    """Create the dishes table and its indexes if absent. Idempotent —
    safe to call on every startup. Also runs lightweight ALTER TABLE
    migrations for columns added after the initial schema."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dishes (
            name              TEXT PRIMARY KEY COLLATE NOCASE,
            queries           TEXT NOT NULL,           -- JSON array of strings
            top_n_serpapi     INTEGER NOT NULL DEFAULT 25,
            top_n_final       INTEGER NOT NULL DEFAULT 10,
            refresh_ttl_days  INTEGER DEFAULT 30,      -- NULL = manual-only (agent skips)
            last_refreshed    TEXT,                    -- ISO-8601 UTC; null if never
            last_run_status   TEXT,                    -- 'success' | 'error:<reason>'
            last_run_count    INTEGER,                 -- rows landing in master after refresh
            notes             TEXT,                    -- curator's free-form note
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
        """
    )
    # Migration (2026-05-24): add last_run_log_filename. Idempotent —
    # check existing columns and ADD only when absent.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dishes)")}
    if "last_run_log_filename" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN last_run_log_filename TEXT")
    # Migration (2026-05-27): add auto_enrich opt-in flag. Default 0
    # (off) — dish refreshes save fast and cheap by default; user opts
    # in per-dish to run enrich_recipe on every saved master row.
    if "auto_enrich" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN auto_enrich INTEGER NOT NULL DEFAULT 0")
    # Migration (2026-05-27): per-run persistence for OU-fit and the
    # bar-to-beat. Rejects live in their own table (see
    # ensure_dish_rejects_table) — proper rows, indexable by URL,
    # joinable with master_recipes. `last_ou_fit` is the
    # {model, coefficients, n, r2} from _compute_custom_ou so a manual
    # rescore of any URL uses the same formula the batch did.
    # `last_run_bottom_ou` is the OU of the lowest-included URL in
    # the final top-N — the "bar to beat" the form flags each reject
    # against.
    if "last_ou_fit" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN last_ou_fit TEXT")  # JSON
    if "last_run_bottom_ou" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN last_run_bottom_ou REAL")
    # Migration (2026-05-28): cached embedding of `name + queries` used
    # as the cohort-match key for harvest / personal / legacy saves
    # that don't carry an explicit `_master.dish`. embedding_text is
    # the exact string that was embedded — diff against current
    # composition to detect staleness when queries change.
    # See input/pipeline/embeddings.py for details.
    if "embedding" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN embedding BLOB")
    if "embedding_text" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN embedding_text TEXT")
    if "embedding_model" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN embedding_model TEXT")
    if "embedding_updated_at" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN embedding_updated_at TEXT")
    # Curator-supplied prose to disambiguate the dish for the embedding
    # matcher. Name + queries alone are often thin ("Pastitsio" → only
    # name-token matches succeed); a one-line description like "Greek
    # baked pasta with cinnamon and tomatoes, layered with bechamel"
    # lets recipes titled "Greek Lasagna with Béchamel" still find the
    # right cohort. Optional — dishes without it fall back to
    # name+queries only.
    if "description" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN description TEXT")
    # Cookbook chapter (one of CHAPTERS in extract.chapter_classifier).
    # Populated by chapter_classifier when the description is
    # generated. Used as a SQL pre-filter in find_best_dish_match —
    # only score against dishes in the recipe's chapter — so the
    # cosine scan stays small as the dish library grows.
    if "chapter" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN chapter TEXT")
    # Identity card (extract.identity_card.generate_identity_card_for_dish
    # output) — structured cohort fingerprint mirroring the recipe-side
    # _identity field. Stored as JSON text. The matcher derives both
    # dish and recipe embed text from the SAME card shape, which
    # gives the cosine a clean apples-to-apples comparison.
    if "identity_card" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN identity_card TEXT")  # JSON
    # last_run_rejects column was briefly added 2026-05-27 then moved
    # to dish_rejects table — column stays nullable + unused for
    # forward-compat with rows created during the brief window.
    ensure_dish_rejects_table(conn)
    # Index on refresh_ttl_days so the agent's "find due" query is cheap.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dishes_ttl "
        "ON dishes(refresh_ttl_days) WHERE refresh_ttl_days IS NOT NULL"
    )
    conn.commit()


def ensure_dish_rejects_table(conn: sqlite3.Connection) -> None:
    """Create the dish_rejects table if absent. One row per URL that
    made it past the batch's front-end (filter_disallowed +
    is_recipe + Moz scoring) but then failed extract / save / thin-
    gate during the dish refresh.

    Lifecycle:
      - status='new' rows are wiped on each refresh and replaced with
        the current run's rejects.
      - User-marked rows ('recovered', 'skipped', 'unreachable')
        survive across refreshes — institutional memory.
      - If a URL appears in the new run AND has a prior user-marked
        row, the score columns get refreshed but status + notes are
        preserved.

    Indexed by dish_name for fast per-dish fetch + by URL for
    cross-dish JOINs."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dish_rejects (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dish_name       TEXT NOT NULL COLLATE NOCASE,
            url             TEXT NOT NULL,
            reason          TEXT NOT NULL,
            title           TEXT,
            da              REAL,
            pa              REAL,
            ou              REAL,        -- against the run's custom fit
            rank            INTEGER,     -- original SerpAPI rank
            run_started_at  TEXT,        -- ISO ts, ties to the refresh run
            created_at      TEXT NOT NULL
        )
    """)
    # Migration (2026-05-27): user-status tracking. Lets the curator
    # mark each reject as recovered / skipped / unreachable so the
    # next refresh doesn't surface it as a fresh discovery.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dish_rejects)")}
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE dish_rejects ADD COLUMN status TEXT NOT NULL DEFAULT 'new' "
            "CHECK (status IN ('new', 'recovered', 'skipped', 'unreachable'))"
        )
    if "notes" not in cols:
        conn.execute("ALTER TABLE dish_rejects ADD COLUMN notes TEXT")
    if "marked_at" not in cols:
        conn.execute("ALTER TABLE dish_rejects ADD COLUMN marked_at TEXT")
    # Migration (2026-05-27): Exceptionalism grade per reject row, so
    # the dish form can show "would have graded A-" alongside the
    # existing "would qualify" indicator.
    if "exc_score" not in cols:
        conn.execute("ALTER TABLE dish_rejects ADD COLUMN exc_score REAL")
    if "exc_grade" not in cols:
        conn.execute("ALTER TABLE dish_rejects ADD COLUMN exc_grade TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dish_rejects_dish "
        "ON dish_rejects(dish_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dish_rejects_url "
        "ON dish_rejects(url)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dish_rejects_status "
        "ON dish_rejects(status)"
    )
    conn.commit()


def replace_rejects_for_dish(conn: sqlite3.Connection, dish_name: str,
                              rejects: list[dict],
                              run_started_at: Optional[str] = None) -> int:
    """Merge the new run's rejects into dish_rejects, preserving user
    annotations. Returns count of rows inserted or updated.

    Algorithm:
      1. Delete all status='new' rows for this dish (untouched rejects
         from the previous run that the user never acted on).
      2. For each reject in the new batch:
         - If a row already exists for (dish, url) AND its status is
           non-'new': UPDATE the score / reason / rank / run_started_at
           columns to reflect the latest values. Preserve status,
           notes, marked_at — that's the user's institutional memory.
         - Else: INSERT with status='new'.

    Net effect: user-marked rows (recovered / skipped / unreachable)
    survive across refreshes and never re-surface as fresh discoveries,
    while their scores keep updating so the form's "would qualify"
    badge stays accurate."""
    # Step 1: drop unmarked rejects from the previous run.
    conn.execute(
        "DELETE FROM dish_rejects WHERE dish_name = ? AND status = 'new'",
        (dish_name,),
    )
    # Step 2: build a lookup of the surviving (marked) URLs.
    existing_rows = conn.execute(
        "SELECT url FROM dish_rejects WHERE dish_name = ?",
        (dish_name,),
    ).fetchall()
    existing_urls = {r[0] for r in existing_rows}

    now = datetime.now(timezone.utc).isoformat()
    rts = run_started_at or now
    count = 0
    for r in rejects:
        url = r.get("url")
        if url in existing_urls:
            # User-marked row — refresh score columns but keep status.
            conn.execute(
                "UPDATE dish_rejects SET reason = ?, title = ?, "
                "da = ?, pa = ?, ou = ?, rank = ?, run_started_at = ?, "
                "exc_score = ?, exc_grade = ? "
                "WHERE dish_name = ? AND url = ?",
                (
                    r.get("reason"),
                    r.get("title"),
                    r.get("da"),
                    r.get("pa"),
                    r.get("ou"),
                    r.get("rank"),
                    rts,
                    r.get("exc_score"),
                    r.get("exc_grade"),
                    dish_name,
                    url,
                ),
            )
        else:
            conn.execute(
                "INSERT INTO dish_rejects (dish_name, url, reason, title, "
                "da, pa, ou, rank, run_started_at, created_at, status, "
                "exc_score, exc_grade) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)",
                (
                    dish_name,
                    url,
                    r.get("reason"),
                    r.get("title"),
                    r.get("da"),
                    r.get("pa"),
                    r.get("ou"),
                    r.get("rank"),
                    rts,
                    now,
                    r.get("exc_score"),
                    r.get("exc_grade"),
                ),
            )
        count += 1
    conn.commit()
    return count


def update_reject_status(conn: sqlite3.Connection, reject_id: int,
                         status: str, notes: Optional[str] = None) -> Optional[dict]:
    """Update a single reject's user-status + notes. Returns the
    updated row dict, or None if reject_id doesn't exist. Raises
    ValueError on invalid status."""
    valid = {"new", "recovered", "skipped", "unreachable"}
    if status not in valid:
        raise ValueError(f"status must be one of {sorted(valid)}; got {status!r}")
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE dish_rejects SET status = ?, notes = ?, marked_at = ? "
        "WHERE id = ?",
        (status, (notes or None), now, reject_id),
    )
    if cur.rowcount == 0:
        return None
    conn.commit()
    row = conn.execute(
        "SELECT id, dish_name, url, reason, title, da, pa, ou, rank, "
        "run_started_at, created_at, status, notes, marked_at, "
        "exc_score, exc_grade "
        "FROM dish_rejects WHERE id = ?",
        (reject_id,),
    ).fetchone()
    return {
        "id": row[0], "dish_name": row[1], "url": row[2], "reason": row[3],
        "title": row[4], "da": row[5], "pa": row[6], "ou": row[7],
        "rank": row[8], "run_started_at": row[9], "created_at": row[10],
        "status": row[11], "notes": row[12], "marked_at": row[13],
        "exc_score": row[14], "exc_grade": row[15],
    }


def list_rejects_for_dish(conn: sqlite3.Connection, dish_name: str) -> list[dict]:
    """Return all rejects for a dish, ordered status-then-OU:
    'new' rows first (actionable), then marked rows (recovered /
    skipped / unreachable) so user-decided items don't crowd the
    fresh-actionable ones. Within a status group: OU descending."""
    rows = conn.execute(
        "SELECT id, url, reason, title, da, pa, ou, rank, "
        "run_started_at, created_at, status, notes, marked_at, "
        "exc_score, exc_grade "
        "FROM dish_rejects WHERE dish_name = ? "
        "ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, "
        "         ou DESC NULLS LAST, id",
        (dish_name,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "url": r[1],
            "reason": r[2],
            "title": r[3],
            "da": r[4],
            "pa": r[5],
            "ou": r[6],
            "rank": r[7],
            "run_started_at": r[8],
            "created_at": r[9],
            "status": r[10] or "new",
            "notes": r[11],
            "marked_at": r[12],
            "exc_score": r[13],
            "exc_grade": r[14],
        })
    return out


def row_to_dict(row: tuple) -> dict:
    """Convert a SELECT * row into the dict shape every endpoint returns.

    `queries` is stored as a JSON string in SQLite; we materialize it to
    a list here so the API surfaces a real array. Adds a derived
    `is_due` field based on refresh_ttl_days + last_refreshed, and a
    derived `last_run_log_url` for the form's "View latest log" link.
    """
    (name, queries_json, top_n_serpapi, top_n_final, ttl_days,
     last_refreshed, last_run_status, last_run_count, notes,
     created_at, updated_at, last_run_log_filename, auto_enrich,
     last_ou_fit, last_run_bottom_ou, description, chapter,
     embedding_text, embedding_model, embedding_updated_at,
     identity_card_json) = row
    try:
        queries = json.loads(queries_json) if queries_json else []
    except Exception:
        queries = []
    try:
        ou_fit = json.loads(last_ou_fit) if last_ou_fit else None
    except Exception:
        ou_fit = None
    try:
        identity_card = json.loads(identity_card_json) if identity_card_json else None
    except Exception:
        identity_card = None
    return {
        "name": name,
        "queries": queries,
        "top_n_serpapi": top_n_serpapi,
        "top_n_final": top_n_final,
        "refresh_ttl_days": ttl_days,
        "last_refreshed": last_refreshed,
        "last_run_status": last_run_status,
        "last_run_count": last_run_count,
        "notes": notes,
        "created_at": created_at,
        "updated_at": updated_at,
        "is_due": is_due(ttl_days, last_refreshed),
        "last_run_log_filename": last_run_log_filename,
        "last_run_log_url": f"/logs/{last_run_log_filename}" if last_run_log_filename else None,
        "auto_enrich": bool(auto_enrich),
        "last_ou_fit": ou_fit,
        "last_run_bottom_ou": last_run_bottom_ou,
        "description": description,
        "chapter": chapter,
        # Embedding cache metadata (not the BLOB itself — 6KB of binary
        # is useless to the client). The text + model + timestamp let
        # the dish form show "this is what we fed the embedder, on
        # this date, with this model" so the curator can verify
        # matching is using the description they wrote.
        "embedding_text": embedding_text,
        "embedding_model": embedding_model,
        "embedding_updated_at": embedding_updated_at,
        "identity_card": identity_card,
        # rejects fetched on-demand via /dishes/<name>/rejects
    }


_SELECT_ALL_COLS = (
    "name, queries, top_n_serpapi, top_n_final, refresh_ttl_days, "
    "last_refreshed, last_run_status, last_run_count, notes, "
    "created_at, updated_at, last_run_log_filename, auto_enrich, "
    "last_ou_fit, last_run_bottom_ou, description, chapter, "
    "embedding_text, embedding_model, embedding_updated_at, identity_card"
)


def list_dishes(conn: sqlite3.Connection) -> list[dict]:
    """Return all dishes, newest first by created_at."""
    rows = conn.execute(
        f"SELECT {_SELECT_ALL_COLS} FROM dishes ORDER BY created_at DESC"
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def get_dish(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    """Look up by name (case-insensitive — table uses COLLATE NOCASE)."""
    row = conn.execute(
        f"SELECT {_SELECT_ALL_COLS} FROM dishes WHERE name = ?",
        (name,),
    ).fetchone()
    return row_to_dict(row) if row else None


def validate_create_payload(payload: dict) -> tuple[str, list[str], int, int, Optional[int], Optional[str], bool, Optional[str]]:
    """Validate a POST /dishes body. Returns (name, queries, top_n_serpapi,
    top_n_final, refresh_ttl_days, notes, auto_enrich, description).
    Raises ValueError on any problem; the endpoint converts that to a 400."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required and must be non-empty")
    queries_raw = payload.get("queries")
    if not isinstance(queries_raw, list) or not queries_raw:
        raise ValueError("queries must be a non-empty array of strings")
    queries = [str(q).strip() for q in queries_raw if str(q).strip()]
    if not queries:
        raise ValueError("queries must contain at least one non-empty string")

    top_n_serpapi = int(payload.get("top_n_serpapi", 25))
    if top_n_serpapi <= 0:
        raise ValueError("top_n_serpapi must be positive")
    top_n_final = int(payload.get("top_n_final", 10))
    if top_n_final <= 0:
        raise ValueError("top_n_final must be positive")

    ttl_raw = payload.get("refresh_ttl_days", 30)
    if ttl_raw is None or ttl_raw == "":
        ttl: Optional[int] = None
    else:
        ttl = int(ttl_raw)
        if ttl <= 0:
            raise ValueError("refresh_ttl_days must be positive or null")

    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be a string or null")
    notes = (notes or None) if notes is None else notes.strip() or None

    # auto_enrich: optional bool, defaults to False (cheap fast saves
    # during refresh; user opts in per-dish to run enrich on each row).
    auto_enrich = bool(payload.get("auto_enrich", False))

    description = payload.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("description must be a string or null")
    description = (description.strip() or None) if isinstance(description, str) else None

    return name, queries, top_n_serpapi, top_n_final, ttl, notes, auto_enrich, description


def create_dish(conn: sqlite3.Connection, *,
                name: str,
                queries: list[str],
                top_n_serpapi: int = 25,
                top_n_final: int = 10,
                refresh_ttl_days: Optional[int] = 30,
                notes: Optional[str] = None,
                auto_enrich: bool = False,
                description: Optional[str] = None) -> dict:
    """Insert a new dish. Raises sqlite3.IntegrityError on name
    collision (caller maps to 409). Returns the created dict."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO dishes (name, queries, top_n_serpapi, top_n_final, "
        "refresh_ttl_days, notes, created_at, updated_at, auto_enrich, description) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, json.dumps(queries), top_n_serpapi, top_n_final,
         refresh_ttl_days, notes, now, now, 1 if auto_enrich else 0, description),
    )
    conn.commit()
    return get_dish(conn, name)  # round-trip so we return the canonical shape


# The fields PATCH is allowed to update. `name` is intentionally absent —
# it's the join key into master_recipes._master.dish, so renaming would
# orphan recipes. To "rename", caller deletes + recreates (which deletes
# the master rows too — intentional cascade).
_PATCHABLE = {
    "queries", "top_n_serpapi", "top_n_final",
    "refresh_ttl_days", "notes", "auto_enrich",
    "description",
}


def update_dish(conn: sqlite3.Connection, name: str, patch: dict) -> Optional[dict]:
    """Apply a partial update to a dish row. Returns the updated dict, or
    None if the row doesn't exist. Raises ValueError on field-validation
    failures."""
    existing = get_dish(conn, name)
    if existing is None:
        return None

    sets: list[str] = []
    params: list = []

    if "queries" in patch:
        q = patch["queries"]
        if not isinstance(q, list) or not q:
            raise ValueError("queries must be a non-empty array of strings")
        q_clean = [str(x).strip() for x in q if str(x).strip()]
        if not q_clean:
            raise ValueError("queries must contain at least one non-empty string")
        sets.append("queries = ?")
        params.append(json.dumps(q_clean))

    if "top_n_serpapi" in patch:
        v = int(patch["top_n_serpapi"])
        if v <= 0:
            raise ValueError("top_n_serpapi must be positive")
        sets.append("top_n_serpapi = ?")
        params.append(v)

    if "top_n_final" in patch:
        v = int(patch["top_n_final"])
        if v <= 0:
            raise ValueError("top_n_final must be positive")
        sets.append("top_n_final = ?")
        params.append(v)

    if "refresh_ttl_days" in patch:
        raw = patch["refresh_ttl_days"]
        if raw is None or raw == "":
            sets.append("refresh_ttl_days = NULL")
        else:
            v = int(raw)
            if v <= 0:
                raise ValueError("refresh_ttl_days must be positive or null")
            sets.append("refresh_ttl_days = ?")
            params.append(v)

    if "notes" in patch:
        n = patch["notes"]
        if n is None:
            sets.append("notes = NULL")
        else:
            if not isinstance(n, str):
                raise ValueError("notes must be a string or null")
            stripped = n.strip()
            if stripped:
                sets.append("notes = ?")
                params.append(stripped)
            else:
                sets.append("notes = NULL")

    if "auto_enrich" in patch:
        sets.append("auto_enrich = ?")
        params.append(1 if bool(patch["auto_enrich"]) else 0)

    if "description" in patch:
        d = patch["description"]
        if d is None:
            sets.append("description = NULL")
        else:
            if not isinstance(d, str):
                raise ValueError("description must be a string or null")
            stripped = d.strip()
            if stripped:
                sets.append("description = ?")
                params.append(stripped)
            else:
                sets.append("description = NULL")

    extras = set(patch.keys()) - _PATCHABLE
    if extras:
        raise ValueError(f"non-patchable fields in body: {sorted(extras)}")

    if not sets:
        # No updatable fields supplied — return the existing row unchanged.
        return existing

    sets.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(name)

    conn.execute(
        f"UPDATE dishes SET {', '.join(sets)} WHERE name = ?",
        params,
    )
    conn.commit()
    return get_dish(conn, name)


def delete_dish(conn: sqlite3.Connection, name: str) -> bool:
    """Delete a dish row + its dish_rejects rows. Returns True if the
    dish row was removed.

    NOTE: this does NOT yet cascade-delete the dish's top-kind rows in
    master_recipes — that's done by the in-process refresh logic when
    #3 lands. For now the dish row goes; any existing master rows
    stamped with `_master.dish == name` (if/when they exist) stay
    until the next batch refresh. Tracked in project_dish_library.md.
    """
    # Load sqlite-vec so the trg_dish_vec_cleanup AFTER DELETE trigger
    # (which deletes the dishes_vec row) can run; without the module
    # loaded the DELETE below would fail. Best-effort — if the extension
    # is genuinely absent there's no vec table to keep in sync anyway.
    _enable_vec_best_effort(conn)
    # Wipe the per-run reject rows first so they don't dangle pointing
    # at a deleted dish.
    conn.execute("DELETE FROM dish_rejects WHERE dish_name = ?", (name,))
    cur = conn.execute("DELETE FROM dishes WHERE name = ?", (name,))
    conn.commit()
    return cur.rowcount > 0


def is_due(refresh_ttl_days: Optional[int], last_refreshed: Optional[str]) -> bool:
    """True when an auto-refresh agent should pick this dish up.

    Rules:
      - refresh_ttl_days is None → manual-only; never due automatically.
      - last_refreshed is None → never run; always due.
      - now - last_refreshed >= ttl_days → due.
    """
    if refresh_ttl_days is None:
        return False
    if not last_refreshed:
        return True
    try:
        last = datetime.fromisoformat(last_refreshed.replace("Z", "+00:00"))
    except Exception:
        return True  # malformed timestamp → safer to refresh
    age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
    return age_days >= refresh_ttl_days


def find_due_dishes(conn: sqlite3.Connection) -> list[dict]:
    """Return all dishes whose auto-refresh is due. Used by the cron-fired
    refresh_due_dishes.py agent. Filters out refresh_ttl_days IS NULL
    (manual-only) at the SQL layer; final due check is in Python."""
    rows = conn.execute(
        f"SELECT {_SELECT_ALL_COLS} FROM dishes "
        f"WHERE refresh_ttl_days IS NOT NULL "
        f"ORDER BY last_refreshed IS NULL DESC, last_refreshed ASC"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = row_to_dict(r)
        if d["is_due"]:
            out.append(d)
    return out


def delete_master_rows_for_dish(conn: sqlite3.Connection, dish_name: str,
                                kind: str = "top") -> int:
    """Delete every row in master_recipes whose `_master.dish` matches
    `dish_name` AND `_master.kind` matches `kind`. Returns the
    deletion count. Used by the refresh-dish path to clear the prior
    top-N before re-populating; editors_choice and legacy rows are
    untouched because the kind filter excludes them.

    The trg_master_vec_cleanup AFTER DELETE trigger drops each row's
    recipes_master_vec vector automatically, so we load sqlite-vec first
    (the trigger deletes from a vec0 table and needs the module).
    """
    _enable_vec_best_effort(conn)
    cur = conn.execute(
        "DELETE FROM master_recipes "
        "WHERE json_extract(data, '$._master.dish') = ? "
        "AND json_extract(data, '$._master.kind') = ?",
        (dish_name, kind),
    )
    conn.commit()
    return cur.rowcount


def record_run_result(conn: sqlite3.Connection, name: str, *,
                      status: str, count: Optional[int] = None,
                      log_filename: Optional[str] = None,
                      ou_fit: Optional[dict] = None,
                      rejects: Optional[list] = None,
                      bottom_ou: Optional[float] = None) -> None:
    """Stamp a refresh run's outcome on the dish row. Called by both the
    /dishes/<name>/refresh endpoint and the agent. `status` is
    'success' or 'error:<short-reason>'. `log_filename` is the basename
    of the per-run log file under forms/logs/; the form turns it into
    a /logs/<filename> link.

    `ou_fit` is the {model, coefficients, n, r2} dict from
    `_compute_custom_ou` — persisted so manual single-URL rescoring
    (a rejected URL the user later bookmarklets) uses the same formula
    the batch did. `bottom_ou` is the OU of the lowest-included URL
    in the final top-N (so the form can flag "would have qualified"
    on each reject). `rejects`, when supplied, replaces the dish's
    rows in dish_rejects (per-run state, no history kept)."""
    import json as _json
    now = datetime.now(timezone.utc).isoformat()
    # Build the SET clause dynamically so we always include the new
    # per-run fields (even when None — clears them from the last run).
    fields = [
        ("last_refreshed", now),
        ("last_run_status", status),
        ("last_run_count", count),
        ("last_ou_fit", _json.dumps(ou_fit) if ou_fit is not None else None),
        ("last_run_bottom_ou", bottom_ou),
        ("updated_at", now),
    ]
    if log_filename is not None:
        fields.insert(-1, ("last_run_log_filename", log_filename))
    set_clause = ", ".join(f"{k} = ?" for k, _ in fields)
    params = [v for _, v in fields] + [name]
    conn.execute(f"UPDATE dishes SET {set_clause} WHERE name = ?", params)
    # Replace dish_rejects rows in the same connection so it's
    # transactional with the dishes-row update.
    if rejects is not None:
        replace_rejects_for_dish(conn, name, rejects, run_started_at=now)
    conn.commit()
