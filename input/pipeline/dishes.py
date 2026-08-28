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
import re
import sqlite3
from datetime import datetime, timedelta, timezone
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
    # Migration (2026-08-28): measured cohort evidence for the dish->product
    # pipeline (docs/dish-product-matching.md). JSON stamped by the
    # dish_signals job: term df + lift vs the corpus + example lines, per
    # signal family (ingredients / equipment / provenance).
    if "cohort_signals" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN cohort_signals TEXT")
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
    # Dish competitiveness within its chapter (0-100): how this dish's field
    # clout ranks among its chapter siblings — popular/contested vs niche. A
    # CROSS-dish rollup, so it would go stale if frozen per-refresh; recomputed
    # by the nightly chapter_rollups job, off the hot path.
    if "competitiveness_pct" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN competitiveness_pct REAL")
    # Absolute field clout = avg DA+PA of the dish's kind=top winners. The
    # MEANINGFUL, non-degenerate signal (the percentile collapses to 100/0 in a
    # 2-dish chapter); stored for every dish, even singletons.
    if "field_clout" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN field_clout REAL")
    # Cosmetic display name, decoupled from the immutable `name` join key. `name`
    # stays the key every linkage uses (_master.dish, dish_rejects.dish_name,
    # dish_run_data_points.dish_name) — display_name is purely what's SHOWN, free
    # to edit (e.g. capitalize "dal makhani" -> "Dal Makhani") with zero ripple.
    # Falls back to `name` when blank.
    if "display_name" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN display_name TEXT")
    # ALIASES (JSON array of strings) — other names this exact dish answers to:
    # spelling variants (Lasagne), native/transliterated forms (Kolokithopita),
    # and over-specified identity-card names (Lasagna Bolognese). Matched
    # accent-folded, EXACT-equality only, by dish_match's name-evidence
    # override — never substring/fuzzy (docs/dish-alias-normalization.md is the
    # design; this column is its minimal start, 2026-08-23).
    if "aliases" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN aliases TEXT")  # JSON
    # SOURCE LANGUAGE (ISO 639-1) — the language this dish HARVESTS IN, which is
    # a property of the dish, not of any one query string. NULL/'' = English (the
    # instance's base language), which is the overwhelming majority; set it only
    # for a dish deliberately sourced from foreign-language publishers.
    #
    # Load-bearing today for exactly one thing: it relaxes the min-OU floor, because
    # the OU baseline is calibrated on a US/English corpus and scores foreign
    # publishers negative almost by construction (see _min_ou_filter). Until now
    # that was GUESSED from the query text — `site:.gr` at first, then non-Latin
    # script — and guessing failed the Dan Dan Noodles run, where the query `担担面`
    # tripped neither and both of the run's min-OU drops were its Chinese pages.
    #
    # Deliberately NOT derived from `queries`: a dish can source foreign pages with
    # a Latin-script query ("tonkotsu ramen", "gratin dauphinois"), which no amount
    # of cleverness about the query string can detect. That is the whole reason
    # this is a stored field and not a smarter regex.
    #
    # §7 of docs/dish-variants-membership.md reserves this same column as the hard
    # pre-filter for variant matching (a Greek spanakopita and a US one are
    # identity-degenerate, so the vector cannot separate them). That is NOT built
    # here — this adds the column, the editor control and the harvest behaviour.
    if "source_language" not in cols:
        conn.execute("ALTER TABLE dishes ADD COLUMN source_language TEXT")
    # last_run_rejects column was briefly added 2026-05-27 then moved
    # to dish_rejects table — column stays nullable + unused for
    # forward-compat with rows created during the brief window.
    ensure_dish_rejects_table(conn)
    ensure_editors_choice_table(conn)
    # The candidate ledger is created with the schema, not lazily on first write:
    # a read surface (the dish/domain forms) must be able to query an empty table
    # rather than 500 because no run has happened yet on this install. It is shared
    # with the publisher harvest, hence the import rather than a local definition.
    try:
        from input.pipeline.candidate_ledger import ensure_candidate_ledger_table
        ensure_candidate_ledger_table(conn)
    except Exception as e:   # never block dish-table setup on it
        print(f"[SCHEMA] candidate ledger table setup skipped: {e}")
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


# --------------------------------------------------------------------------- #
# Editor's Choice — curator pins (junction-style membership)
# --------------------------------------------------------------------------- #
def ensure_editors_choice_table(conn: sqlite3.Connection) -> None:
    """Curator pins: a (dish, url) membership an admin adds by hand. Each refresh
    INCLUDES these URLs in the dish's candidate pool, so they're scored into the
    ledger like any SerpAPI result and surface in the top-N IF they rank. This is
    a junction-style membership — the pin is a (collection, url) row, NOT a stamp
    on the recipe — which is exactly the shape the future many-to-many 'collections'
    model generalizes. url_normalized is the dedup/JOIN key shared with the ledger
    and master_recipes.url_normalized."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dish_editors_choice (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            dish_name       TEXT NOT NULL COLLATE NOCASE,
            url             TEXT NOT NULL,
            url_normalized  TEXT NOT NULL,
            note            TEXT,
            added_at        TEXT NOT NULL,
            UNIQUE(dish_name, url_normalized)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dish_editors_choice_dish "
        "ON dish_editors_choice(dish_name)"
    )
    conn.commit()


_EC_COLS = ("id", "dish_name", "url", "url_normalized", "note", "added_at")
_EC_SELECT = "SELECT " + ", ".join(_EC_COLS) + " FROM dish_editors_choice"


def _ec_row(row) -> Optional[dict]:
    return dict(zip(_EC_COLS, row)) if row else None


def add_editors_choice(conn: sqlite3.Connection, dish_name: str, url: str,
                       note: Optional[str] = None) -> dict:
    """Pin a URL to a dish. Idempotent on (dish, normalized url)."""
    from input.pipeline.url_utils import normalize_url
    url = (url or "").strip()
    if not url:
        raise ValueError("url is required")
    norm = normalize_url(url)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO dish_editors_choice (dish_name, url, url_normalized, note, added_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(dish_name, url_normalized) DO UPDATE SET
               url = excluded.url, note = excluded.note""",
        (dish_name, url, norm, note, now),
    )
    conn.commit()
    return get_editors_choice(conn, dish_name, norm)


def get_editors_choice(conn: sqlite3.Connection, dish_name: str,
                       url_normalized: str) -> Optional[dict]:
    return _ec_row(conn.execute(
        _EC_SELECT + " WHERE dish_name = ? AND url_normalized = ?",
        (dish_name, url_normalized)).fetchone())


def list_editors_choice(conn: sqlite3.Connection, dish_name: str) -> list[dict]:
    """All pins for a dish, newest first."""
    return [_ec_row(r) for r in conn.execute(
        _EC_SELECT + " WHERE dish_name = ? ORDER BY added_at DESC", (dish_name,))]


def remove_editors_choice(conn: sqlite3.Connection, dish_name: str,
                          url_normalized: str) -> int:
    """Unpin. Returns rows deleted (0 or 1)."""
    n = conn.execute(
        "DELETE FROM dish_editors_choice WHERE dish_name = ? AND url_normalized = ?",
        (dish_name, url_normalized)).rowcount
    conn.commit()
    return n


def editors_choice_urls(conn: sqlite3.Connection, dish_name: str) -> list[str]:
    """The pinned URLs (original form) for a dish — added to the batch candidate
    pool on refresh so they're scored alongside the SerpAPI results."""
    return [r[0] for r in conn.execute(
        "SELECT url FROM dish_editors_choice WHERE dish_name = ?", (dish_name,))]


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


# ── query ROWS ──────────────────────────────────────────────────────────────
# A dish's queries were a flat list of strings with ONE select size
# (top_n_serpapi) applied to every one, and the SERP locale hardcoded to
# gl=us/hl=en in _serpapi_lookup. Two things that cost us:
#   - some search terms deserve a much larger select than others
#   - a dish could not combine, say, a Greek-locale query with a US one
#
# So a query becomes a ROW carrying its own select size and locale:
#
#     {"q": "γιουβαρλάκια αυγολέμονο", "n": 120, "gl": "gr", "hl": "el"}
#
# `n` = None means "use the dish's top_n_serpapi", which keeps that column
# meaningful as the default rather than orphaning it. gl/hl = None follows the
# SAME contract: "use the dish's source_language", resolved at refresh time by
# resolve_query_locales — so defaults are stored as None, never baked in.
#
# gl and hl are SEPARATE fields, not one "country". They are separate Google
# parameters and they genuinely diverge: gl=gr/hl=el gets Greek-language
# results, gl=gr/hl=en gets English-language pages that rank in Greece. The
# domains table already models country and language separately for the same
# reason.
#
# They are also NOT expressible as `site:.gr` in the query text. A TLD filter
# restricts which DOMAINS may appear; gl selects which Google index answers.
# Measured 2026-08-08: 10 of the 14 Greek publishers in the corpus are not on
# a .gr TLD — including akispetretzikis.com (DA 59, 21 master recipes) — so
# `site:.gr` silently excludes most of the Greek corpus while gl=gr does not.
# Anyone wanting the TLD restriction as well just keeps writing it in `q`,
# which still passes to Google verbatim.
#
# Storage is lazily migrated: a bare string coerces to a row on read, so no
# backfill and no schema change. Mixed old/new rows in the table are fine.
DEFAULT_GL = "us"
DEFAULT_HL = "en"
_CODE_RE = re.compile(r"^[a-z]{2}$")

# gl for languages whose obvious country code differs (hl=el -> gl=gr, etc.).
# Everything else maps language -> same-code country, which is right for the
# fr/it/es/de/pt class this exists for.
_GL_FOR_LANG = {"el": "gr", "en": "us", "ja": "jp", "zh": "cn", "ko": "kr",
                "da": "dk", "sv": "se", "cs": "cz", "uk": "ua"}


def resolve_query_locales(rows: list[dict], source_language) -> list[dict]:
    """Fill each row's None gl/hl with CONCRETE values at RUN time.

    Successor to apply_locale_defaults (the moules fix, 2026-08-25), which
    stamped source_language onto rows AT SAVE — that baked the language in
    and, because a stamped `us/en` was indistinguishable from an explicit
    one, an explicit English line on a Greek dish was inexpressible. Now
    None means "follow the dish" (exactly like `n` = None follows
    top_n_serpapi) and is STORED as None; this resolver runs at refresh:

      hl = row's own hl, else the dish's source_language, else en
      gl = row's own gl, else the country implied by the resolved hl

    So changing a dish's Sources-in retargets every default row on its
    next run — nothing to re-stamp — while an explicit locale, including
    an explicit us/en, is never overridden. Returns the same row dicts
    (mutated) for chaining."""
    lang = (str(source_language or "").strip().lower())
    if not _CODE_RE.match(lang):
        lang = DEFAULT_HL
    for r in rows:
        hl = r.get("hl") or lang
        r["hl"] = hl
        r["gl"] = r.get("gl") or _GL_FOR_LANG.get(hl, hl)
    return rows


def normalize_query_rows(raw) -> list[dict]:
    """Coerce whatever is stored/posted into canonical query rows.

    Accepts a list mixing bare strings (the legacy shape) and dicts, or a
    single string. Unknown keys are dropped; `query`/`text` alias `q`,
    `top_n` aliases `n`, `country`/`language` alias `gl`/`hl` so a caller
    using the UI's vocabulary still lands correctly.

    Pure and total: never raises, never consults config. Validation of
    bounds belongs to the validators, which own the error messages.
    """
    if isinstance(raw, (str, dict)):
        raw = [raw]
    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, str):
            q, n, gl, hl = item, None, None, None
        elif isinstance(item, dict):
            q = item.get("q", item.get("query", item.get("text", "")))
            n = item.get("n", item.get("top_n"))
            gl = item.get("gl", item.get("country"))
            hl = item.get("hl", item.get("language"))
        else:
            continue
        q = str(q or "").strip()
        if not q:
            continue
        try:
            n = int(n) if n not in (None, "") else None
        except (TypeError, ValueError):
            n = None
        # None/blank = "follow the dish" (source_language → locale), resolved
        # at run time by resolve_query_locales — the same contract as n=None.
        gl = (str(gl).strip().lower() or None) if gl not in (None, "") else None
        hl = (str(hl).strip().lower() or None) if hl not in (None, "") else None
        out.append({"q": q, "n": n, "gl": gl, "hl": hl})
    return out


def query_texts(rows) -> list[str]:
    """Just the query strings, in order — the LEGACY projection.

    Every existing consumer (compose_dish_text, dish_signal, identity_card,
    build_query_batch) does `str(q)` over this list. Handing them a dict
    would stringify it into the embedding text and silently stale every dish
    vector, so `row_to_dict` keeps serving strings under `queries` and
    publishes the rows separately. Consumers move over one at a time.
    """
    return [r["q"] for r in normalize_query_rows(rows)]


def validate_query_rows(raw, *, max_n: Optional[int] = None) -> list[dict]:
    """Normalize + validate. Raises ValueError; endpoints turn that into 400."""
    if not isinstance(raw, (list, str, dict)) or (isinstance(raw, list) and not raw):
        raise ValueError("queries must be a non-empty array")
    rows = normalize_query_rows(raw)
    if not rows:
        raise ValueError("queries must contain at least one non-empty query")
    for r in rows:
        if r["n"] is not None:
            if r["n"] <= 0:
                raise ValueError(f"select size for {r['q']!r} must be positive")
            if max_n is not None and r["n"] > max_n:
                raise ValueError(
                    f"select size {r['n']} for {r['q']!r} exceeds the max of "
                    f"{max_n} (raise it in System → Limits)")
        for field in ("gl", "hl"):
            if r[field] is not None and not _CODE_RE.match(r[field]):
                raise ValueError(
                    f"{field} for {r['q']!r} must be a two-letter code "
                    f"(got {r[field]!r}) — store the CODE, not a display name")
    return rows


def row_to_dict(row: tuple) -> dict:
    """Convert a SELECT * row into the dict shape every endpoint returns.

    `queries` is stored as a JSON string in SQLite; we materialize it to
    a list here so the API surfaces a real array. Adds a derived
    `is_due` field based on refresh_ttl_days + last_refreshed, and a
    derived `last_run_log_url` for the form's "View latest log" link.

    Emits BOTH shapes during the rollout: `queries` stays a list of plain
    strings for the four existing consumers, and `query_rows` carries the
    canonical rows (select size + locale). See normalize_query_rows.
    """
    (name, queries_json, top_n_serpapi, top_n_final, ttl_days,
     last_refreshed, last_run_status, last_run_count, notes,
     created_at, updated_at, last_run_log_filename, auto_enrich,
     last_ou_fit, last_run_bottom_ou, description, chapter,
     embedding_text, embedding_model, embedding_updated_at,
     identity_card_json, competitiveness_pct, field_clout, display_name,
     source_language) = row
    try:
        queries_raw = json.loads(queries_json) if queries_json else []
    except Exception:
        queries_raw = []
    query_rows = normalize_query_rows(queries_raw)
    queries = [r["q"] for r in query_rows]
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
        "queries": queries,          # legacy projection: plain strings
        "query_rows": query_rows,    # canonical: {q, n, gl, hl}
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
        "next_run_at": next_run_at(ttl_days, last_refreshed),
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
        "competitiveness_pct": competitiveness_pct,
        "field_clout": field_clout,
        # Cosmetic label; `name` is the key. Resolved here so every reader gets a
        # ready-to-show value without repeating the fallback.
        "display_name": (display_name or "").strip() or name,
        "display_name_set": bool((display_name or "").strip()),
        # '' rather than None: the editor's <select> matches on string equality,
        # and "not stated" is a real, selectable option rather than a missing one.
        "source_language": (source_language or "").strip().lower(),
        # rejects fetched on-demand via /dishes/<name>/rejects
    }


_SELECT_ALL_COLS = (
    "name, queries, top_n_serpapi, top_n_final, refresh_ttl_days, "
    "last_refreshed, last_run_status, last_run_count, notes, "
    "created_at, updated_at, last_run_log_filename, auto_enrich, "
    "last_ou_fit, last_run_bottom_ou, description, chapter, "
    "embedding_text, embedding_model, embedding_updated_at, identity_card, "
    "competitiveness_pct, field_clout, display_name, source_language"
)


def recompute_competitiveness(conn: sqlite3.Connection) -> dict:
    """Nightly chapter rollup: for every chapter, rank its dishes by field clout
    (avg DA+PA of their kind=top winners) and store each dish's in-chapter
    percentile (0-100) on dishes.competitiveness_pct. high = a popular/contested
    dish, low = niche. This is a CROSS-dish aggregate — it drifts whenever any
    sibling refreshes — so it lives OFF the hot refresh path, recomputed whole
    here. Singleton chapters get NULL (a percentile of one is meaningless).
    Returns {chapters, dishes_updated}."""
    rows = conn.execute(
        "SELECT json_extract(data,'$.classification.chapter') AS chapter, "
        "json_extract(data,'$._master.dish') AS dish, "
        # NO COALESCE TO 0. An unmeasured winner used to contribute 0 to its
        # dish's field clout, dragging the average down and making the dish read
        # as quieter/nicher than it is — which then propagates to
        # competitiveness_pct and the authority commentary. `da + pa` is NULL
        # when either side is, and SQLite's AVG ignores NULLs, so those rows are
        # excluded from the mean instead of counted as zero. A dish whose
        # winners are ALL unmeasured yields NULL and is skipped below, which is
        # the honest outcome: no clout figure, rather than a clout of 0.
        # (curator 2026-08-06: empty stats stay out of aggregates.)
        "AVG(json_extract(data,'$._scoring.domainAuthority') "
        "  + json_extract(data,'$._scoring.pageAuthority')) AS avg_power "
        "FROM master_recipes "
        "WHERE json_extract(data,'$._master.kind') = 'top' "
        "  AND json_extract(data,'$._master.dish') IS NOT NULL "
        "  AND json_extract(data,'$.classification.chapter') IS NOT NULL "
        "GROUP BY chapter, dish"
    ).fetchall()
    by_chapter: dict[str, list] = {}
    for chapter, dish, ap in rows:
        if dish is None or ap is None:
            continue
        by_chapter.setdefault(chapter, []).append((dish, float(ap)))

    # A within-chapter percentile is only meaningful with enough dishes — in a
    # 2-dish chapter `below/(n-1)*100` collapses to a meaningless 100/0. So always
    # store the ABSOLUTE field clout (avg winner DA+PA), but only assign the
    # popular/niche percentile at or above this floor; below it a dish carries its
    # clout but no rank (the UI shows clout + "too few dishes to rank").
    MIN_RANK = 4
    chapters_done = dishes_updated = 0
    for chapter, members in by_chapter.items():
        powers = [ap for _d, ap in members]
        rankable = len(members) >= MIN_RANK
        for dish, ap in members:
            pct = None
            if rankable:
                below = sum(1 for p in powers if p < ap)
                pct = round(below / (len(powers) - 1) * 100, 1)
            conn.execute(
                "UPDATE dishes SET field_clout = ?, competitiveness_pct = ? WHERE name = ?",
                (round(ap, 1), pct, dish))
            dishes_updated += 1
        if rankable:
            chapters_done += 1
    conn.commit()
    return {"chapters": chapters_done, "dishes_updated": dishes_updated}


def list_dishes(conn: sqlite3.Connection) -> list[dict]:
    """Return all dishes, newest first by created_at."""
    rows = conn.execute(
        f"SELECT {_SELECT_ALL_COLS} FROM dishes ORDER BY created_at DESC"
    ).fetchall()
    return [row_to_dict(r) for r in rows]


def representative_images(conn: sqlite3.Connection) -> dict:
    """{dish_name: image_url} — each dish's card image DERIVED from its recipes (the
    recipe table), not a stored column. For each dish, picks the best-RANKED master
    recipe (`_master.rank`) that actually HAS an image, preferring the cooped og-thumb
    (`_source.previewImage`) over the hotlinked schema.org `image[0]`.

    Phase 0 of docs/recipe-table-backed-lists.md — reads straight from `master_recipes`
    via `_master.dish`, NOT the `dish_run_data_points` ledger (that join over every
    run's rows × JSON-extracting big blobs cost ~18s; this single scan is ~70ms and
    covers legacy dishes too). `_master.rank`/`kind='top'` is the denormalized label the
    top-recipes tiles deliberately distrust for RANKING, but for a thumbnail it's fine —
    any decent top recipe's hero. No denormalized `dishes.image` data is introduced."""
    sql = """
        SELECT json_extract(data, '$._master.dish') AS dish,
               COALESCE(
                   NULLIF(json_extract(data, '$._source.previewImage'), ''),
                   json_extract(data, '$.image[0]')
               ) AS img,
               COALESCE(CAST(json_extract(data, '$._master.rank') AS INTEGER), 9999) AS rank
        FROM master_recipes
        WHERE json_extract(data, '$._master.dish') IS NOT NULL
    """
    try:
        best: dict = {}   # dish -> (rank, img)
        for dish, img, rank in conn.execute(sql).fetchall():
            if not dish or not img:
                continue
            if dish not in best or rank < best[dish][0]:
                best[dish] = (rank, img)
        return {dish: rank_img[1] for dish, rank_img in best.items()}
    except Exception as e:
        print(f"[dishes] representative_images failed: {e}")
        return {}


def get_dish(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    """Look up by name (case-insensitive — table uses COLLATE NOCASE)."""
    row = conn.execute(
        f"SELECT {_SELECT_ALL_COLS} FROM dishes WHERE name = ?",
        (name,),
    ).fetchone()
    return row_to_dict(row) if row else None


def dish_limits():
    """(max_serpapi_per_query, max_final) hard caps from the DB system config
    (default 200 each). Guards against an accidental fat-finger — e.g. tabbing
    past the 25 default and typing 50 yields 2550, which would ask SerpAPI for
    2550 rows PER QUERY and blow up cost + rate limits. Editable in System →
    Limits."""
    max_serp = max_final = 200
    try:
        from input.pipeline import system_config as cfg
        max_serp = int(cfg.get_setting("dish_max_serpapi", 200))
        max_final = int(cfg.get_setting("dish_max_final", 200))
    except Exception:
        pass
    return max_serp, max_final


def validate_create_payload(payload: dict) -> tuple[str, list[str], int, int, Optional[int], Optional[str], bool, Optional[str]]:
    """Validate a POST /dishes body. Returns (name, queries, top_n_serpapi,
    top_n_final, refresh_ttl_days, notes, auto_enrich, description).
    Raises ValueError on any problem; the endpoint converts that to a 400."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required and must be non-empty")
    _max_serp, _max_final = dish_limits()
    # Accepts BOTH the legacy array-of-strings and the canonical rows, so an
    # un-migrated client keeps posting strings and gets default locale + the
    # dish's top_n_serpapi. Returns rows; create_dish stores them.
    queries = validate_query_rows(payload.get("query_rows", payload.get("queries")),
                                  max_n=_max_serp)
    top_n_serpapi = int(payload.get("top_n_serpapi", 25))
    if top_n_serpapi <= 0:
        raise ValueError("top_n_serpapi must be positive")
    if top_n_serpapi > _max_serp:
        raise ValueError(f"top_n_serpapi {top_n_serpapi} exceeds the max of {_max_serp} "
                         f"(raise it in System → Limits)")
    top_n_final = int(payload.get("top_n_final", 10))
    if top_n_final <= 0:
        raise ValueError("top_n_final must be positive")
    if top_n_final > _max_final:
        raise ValueError(f"top_n_final {top_n_final} exceeds the max of {_max_final} "
                         f"(raise it in System → Limits)")

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
    collision (caller maps to 409). Returns the created dict.

    `queries` accepts either shape — the validator hands us canonical rows,
    but scripts and tests call this directly with plain strings. Normalizing
    here means the column only ever holds rows.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = normalize_query_rows(queries)
    conn.execute(
        "INSERT INTO dishes (name, queries, top_n_serpapi, top_n_final, "
        "refresh_ttl_days, notes, created_at, updated_at, auto_enrich, description) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, json.dumps(rows, ensure_ascii=False), top_n_serpapi, top_n_final,
         refresh_ttl_days, notes, now, now, 1 if auto_enrich else 0, description),
    )
    conn.commit()
    created = get_dish(conn, name)  # round-trip so we return the canonical shape
    _refresh_dish_embedding(conn, created)
    return created


def _refresh_dish_embedding(conn: sqlite3.Connection, dish_row: Optional[dict]) -> None:
    """Bring the dish's vector back in step with the row that was just written.

    This lives HERE, at the write, and not in the HTTP endpoints where it used
    to sit. `compose_dish_text` is built from name + description + queries (or
    the identity_card), so any write can invalidate the vector — and an
    invariant that each caller has to remember is one a caller eventually
    forgets. It already had: only the two endpoints re-embedded, so a script,
    a job or the jobs CLI editing a dish left `dishes.embedding` and
    `dishes_vec` describing the OLD queries, silently, with no error to notice.

    Cheap to call unconditionally: `ensure_dish_embedding` is content-addressed
    (it compares the stored `embedding_text` against the freshly composed text)
    so an update that did not change the embed text costs one string compare and
    no API call. Best-effort by design — a dish write must not fail because the
    embedding provider is down; the staleness check will catch it on the next
    write, and scripts/check_embeddings.py is the backstop.
    """
    if not dish_row:
        return
    try:
        from input.pipeline.embeddings import ensure_dish_embedding
        ensure_dish_embedding(conn, dish_row)
    except Exception as e:
        print(f"[EMBED] dish {dish_row.get('name')!r} re-embed failed: "
              f"{type(e).__name__}: {e}")


# The fields PATCH is allowed to update. `name` is intentionally absent —
# it's the join key into master_recipes._master.dish, so renaming would
# orphan recipes. To "rename", caller deletes + recreates (which deletes
# the master rows too — intentional cascade).
_PATCHABLE = {
    # `queries` = the legacy array of strings; `query_rows` = the canonical
    # {q, n, gl, hl}. Both accepted, both write the same column.
    "queries", "query_rows", "top_n_serpapi", "top_n_final",
    "refresh_ttl_days", "notes", "auto_enrich",
    "description", "display_name", "source_language",
}

# ISO 639-1 codes the translator can actually handle — _LANG_NAMES in
# intake/translate.py is the source of truth; '' clears the field back to
# "not stated". Validated rather than free-text because a typo'd 'ch' would
# silently read as foreign and lift the OU floor on an English dish.
_SOURCE_LANGS = {
    "en", "zh", "ja", "ko", "el", "it", "fr", "es", "de", "pt",
    "ru", "tr", "ar", "he", "nl", "pl", "sv", "th", "vi", "hi",
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

    _max_serp, _max_final = dish_limits()

    # `query_rows` wins when present (the canonical shape); `queries` remains
    # accepted so the current editor, which posts plain strings, keeps working.
    # NOTE for the UI phase: a client that posts bare strings RESETS n/gl/hl to
    # their defaults, because a string carries no locale to preserve. Harmless
    # today — nothing can author a non-default row yet — but the editor must
    # send query_rows in the same release that lets a curator set them.
    if "query_rows" in patch or "queries" in patch:
        raw = patch.get("query_rows", patch.get("queries"))
        rows = validate_query_rows(raw, max_n=_max_serp)
        # Stored AS POSTED — None gl/hl means "follow the dish" and is kept
        # None (resolve_query_locales fills it at refresh time). No save-time
        # stamping any more: a later Sources-in change retargets default rows
        # automatically on their next run, and an explicit locale — including
        # an explicit us/en on a foreign dish — survives every edit.
        sets.append("queries = ?")
        params.append(json.dumps(rows, ensure_ascii=False))
    if "top_n_serpapi" in patch:
        v = int(patch["top_n_serpapi"])
        if v <= 0:
            raise ValueError("top_n_serpapi must be positive")
        if v > _max_serp:
            raise ValueError(f"top_n_serpapi {v} exceeds the max of {_max_serp} "
                             f"(raise it in System → Limits)")
        sets.append("top_n_serpapi = ?")
        params.append(v)

    if "top_n_final" in patch:
        v = int(patch["top_n_final"])
        if v <= 0:
            raise ValueError("top_n_final must be positive")
        if v > _max_final:
            raise ValueError(f"top_n_final {v} exceeds the max of {_max_final} "
                             f"(raise it in System → Limits)")
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

    if "display_name" in patch:
        dn = patch["display_name"]
        if dn is None:
            sets.append("display_name = NULL")
        else:
            if not isinstance(dn, str):
                raise ValueError("display_name must be a string or null")
            stripped = dn.strip()
            # Blank or identical to the key -> store NULL so it falls back to name.
            if stripped and stripped != name:
                sets.append("display_name = ?")
                params.append(stripped)
            else:
                sets.append("display_name = NULL")

    if "source_language" in patch:
        sl = patch["source_language"]
        if sl is None or (isinstance(sl, str) and not sl.strip()):
            # Explicitly cleared -> back to inferring from the query text.
            sets.append("source_language = NULL")
        else:
            if not isinstance(sl, str):
                raise ValueError("source_language must be a string or null")
            code = sl.strip().lower()[:2]
            if code not in _SOURCE_LANGS:
                raise ValueError(
                    f"source_language {sl!r} is not a supported ISO 639-1 code "
                    f"({', '.join(sorted(_SOURCE_LANGS))})")
            sets.append("source_language = ?")
            params.append(code)

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
    updated = get_dish(conn, name)
    # Deliberately AFTER the write and NOT on the no-op early return above: a
    # patch that changed nothing cannot have staled the vector.
    _refresh_dish_embedding(conn, updated)
    return updated


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


def next_run_at(refresh_ttl_days: Optional[int],
                last_refreshed: Optional[str]) -> Optional[str]:
    """The ISO-8601 UTC timestamp this dish is next DUE for an auto-refresh,
    or None when it has no automatic schedule. Derived (not stored) so it can
    never drift from the cadence — the same single-source-of-truth treatment
    as `is_due`, which it mirrors:

      - refresh_ttl_days is None  → manual-only; no scheduled run → None.
      - last_refreshed is None    → never run; due now → returns 'now'.
      - else                      → last_refreshed + refresh_ttl_days.

    The scheduler (`find_due_dishes`) decides due-ness off `is_due`; this is the
    human-facing "next run: …" date surfaced at the dish level. A malformed
    last_refreshed reads as due-now (mirrors is_due's safer-to-refresh fallback).
    """
    if refresh_ttl_days is None:
        return None
    now = datetime.now(timezone.utc)
    if not last_refreshed:
        return now.isoformat()
    try:
        last = datetime.fromisoformat(last_refreshed.replace("Z", "+00:00"))
    except Exception:
        return now.isoformat()
    return (last + timedelta(days=refresh_ttl_days)).isoformat()


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


def retire_master_membership(conn: sqlite3.Connection, *, marker: str, value: str,
                             other_marker: str, remove_fields: list,
                             also_match: tuple = None,
                             url_normalized: str = None) -> tuple:
    """TYPED-BLOCK lifecycle on the single master (the shared inline reference count).
    A recipe row carries up to two membership blocks — a dish block (`_master.dish`)
    and a domain block (`_master.publisher`). To retire one owner's claim, CLEAR that
    owner's block and drop the row ONLY when no other block remains. One copy of this
    logic, called by BOTH the dish refresh and the publisher refresh.

    For every master row whose `_master.<marker>` == value (optionally also matching
    `also_match`=(field, val), e.g. ('kind','top') to spare pinned rows): remove
    `remove_fields` from `_master`; if `_master.<other_marker>` is still set, KEEP the
    row (now owned by the other type); else DELETE it. The trg_master_vec_cleanup
    AFTER DELETE trigger drops each deleted row's vector, so we load sqlite-vec first.
    `marker`/`other_marker`/`also_match[0]` are code-literals; values are bound.
    Returns (cleared, deleted)."""
    import json as _json
    _enable_vec_best_effort(conn)
    where = f"json_extract(data, '$._master.{marker}') = ?"
    params = [value]
    if also_match:
        where += f" AND json_extract(data, '$._master.{also_match[0]}') = ?"
        params.append(also_match[1])
    # Optional SINGLE-ROW scope. Retiring a whole membership is the batch case
    # (a dish refresh clearing all its kind='top' rows); revoking one Editor's
    # Choice award must touch exactly the one URL, so it passes this. Omitted =
    # unchanged behaviour for every existing caller.
    if url_normalized:
        where += " AND url_normalized = ?"
        params.append(url_normalized)
    rows = conn.execute(f"SELECT id, data FROM master_recipes WHERE {where}", params).fetchall()
    cleared = deleted = 0
    for rid, data in rows:
        try:
            d = _json.loads(data)
        except Exception:
            continue
        m = d.get("_master") or {}
        if m.get(other_marker):          # other block present → keep, clear ours
            for f in remove_fields:
                m.pop(f, None)
            d["_master"] = m
            conn.execute("UPDATE master_recipes SET data = ? WHERE id = ?",
                         (_json.dumps(d, indent=2), rid))
            cleared += 1
        else:                            # last block → drop the row (vec trigger cleans)
            conn.execute("DELETE FROM master_recipes WHERE id = ?", (rid,))
            deleted += 1
    conn.commit()
    return cleared, deleted


def delete_master_rows_for_dish(conn: sqlite3.Connection, dish_name: str,
                                kind: str = "top") -> int:
    """Retire a dish's master rows (clear the dish block; drop the row only if no
    domain block remains). Spares editors_choice/legacy via the kind filter. Thin
    wrapper over the shared retire_master_membership; returns rows actually deleted."""
    _cleared, deleted = retire_master_membership(
        conn, marker="dish", value=dish_name, other_marker="publisher",
        remove_fields=["dish", "exceptionalism"], also_match=("kind", kind))
    return deleted


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
