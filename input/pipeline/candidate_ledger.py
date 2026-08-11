"""candidate_ledger — every URL a run considered, and what the run decided.

Phase 0 of docs/ai-editor-mediation.md. The AI editor is supposed to mediate
over the kept set AND the rejections; today it could not, because the rejections
are thrown away. Measured on two ramen runs: `dish_rejects` captured **1 of 39**
drops on job 794 and **1 of 61** on job 795. Everything else — 35 OU-drops, 18
non-recipe drops, 8 Moz failures — existed only in a log file, which is not a
queryable record and is not what a mediation pass can read.

Why a new table rather than widening `dish_rejects`
---------------------------------------------------
Different grain, different job. `dish_rejects` is a CURATOR WORKLIST: dish-scoped,
current-state-only ("no history kept"), with a status lifecycle the curator drives
(new / recovered / skipped / unreachable). This is a RUN AUDIT: run-scoped,
immutable, holding winners and losers alike. Widening one into the other would
have made a table that is current-state for one reader and historical for another
— and would have put the curator's marks on rows nobody is asking them to triage.
They join cleanly on url_normalized when a reader wants both.

Capture at RUN TIME, never after
--------------------------------
At the moment of a drop the pipeline is holding the URL, title, SERP rank, DA, PA,
OU, the stage and the reason. All of it is free right then. Afterwards it costs
money: checked 2026-08-10, the OU-dropped ramen URLs are in NEITHER `metabase_url`
(Moz) nor `llm_extract_cache` — those hold only the rows that were kept. A reject
is cheap exactly once.

Judgments may be overturned; facts may not
------------------------------------------
`overturnable` is the mediation rule encoded as data at write time. The AI editor
may reconsider an INFERENCE (a proxy threshold, a detection failure, a missing
measurement). It may never overturn an OBSERVATION (the curator's blocklist, or
"this page is a listicle"). Most LLM-adjudication failures come from letting the
model argue with ground truth; the OU floor is precisely the thing worth arguing
with, since it is a proxy and we have watched it fail.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

# Stage a candidate reached before it stopped. Ordered by how far it got.
# `prefilter` is the publisher harvest's pre-fetch pass (path scope, archive/taxonomy
# URLs, roundup titles) — it has no dish-batch equivalent, which is why it is named
# rather than folded into `disallowed`.
STAGES = ("disallowed", "prefilter", "is_recipe", "moz", "min_ou", "rank_cut", "kept")

# reason prefix -> (stage, overturnable). Longest prefix wins.
# Overturnable = an inference we drew. Not overturnable = something we observed,
# or a rule the curator set by hand.
_REASON_MAP: tuple[tuple[str, str, bool], ...] = (
    # --- facts / curator policy: NOT overturnable -------------------------
    ("disallowed-domain", "disallowed", False),   # curator blocklist
    ("disallowed-path",   "disallowed", False),   # curator blocklist
    ("domain-exclude",    "is_recipe",  False),   # curator per-domain exclude
    ("collection-title",  "is_recipe",  False),   # it IS a roundup/listicle
    ("off-path",          "prefilter",  False),   # curator's recipe_path keep-scope
    ("archive-url",       "prefilter",  False),   # /tag/, /category/, feeds — never a recipe
    # --- inferences: overturnable ----------------------------------------
    ("no-recipe-structure", "is_recipe", True),   # detection failure
    ("recipe-score<",       "is_recipe", True),   # heuristic threshold
    ("translation-suspect", "is_recipe", True),   # heuristic
    ("url-prefilter",       "is_recipe", True),   # word-list heuristic
    ("kw-prescreen",        "is_recipe", True),   # heuristic
    ("fetch-failed",        "is_recipe", True),   # infrastructure, not a verdict
    ("moz-unavailable",     "moz",       True),   # missing measurement
    ("ou<",                 "min_ou",    True),   # THE proxy — see the ramen runs
)


def classify(reason: Optional[str]) -> tuple[str, bool]:
    """(stage, overturnable) for a raw `_dropped_reason`.

    Unknown reasons are conservatively NOT overturnable: a reason this module
    has not been taught about is one nobody has decided the semantics of, and
    silently granting the AI power over it is the wrong default.
    """
    r = (reason or "").strip()
    best: Optional[tuple[str, str, bool]] = None
    for prefix, stage, over in _REASON_MAP:
        if r.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, stage, over)
    if best:
        return best[1], best[2]
    return "is_recipe", False


def ensure_candidate_ledger_table(conn: sqlite3.Connection) -> None:
    """Create the ledger. `collection_type` is 'dish' or 'publisher' so a domain
    harvest lands in the same table as a dish refresh — the curator asked for the
    audit on every run, not only dish ones."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_candidates (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id           INTEGER,
            collection_type  TEXT NOT NULL,              -- 'dish' | 'publisher'
            collection_key   TEXT NOT NULL COLLATE NOCASE,
            run_started_at   TEXT NOT NULL,
            url              TEXT NOT NULL,
            url_normalized   TEXT NOT NULL,
            title            TEXT,
            serp_rank        INTEGER,                    -- rank in the SERP union
            queries          TEXT,                       -- JSON: which queries found it
            stage            TEXT NOT NULL,              -- how far it got (STAGES)
            outcome          TEXT NOT NULL,              -- 'kept' | 'dropped'
            reason           TEXT,                       -- verbatim _dropped_reason
            overturnable     INTEGER NOT NULL DEFAULT 0, -- may the AI editor argue?
            da               REAL,
            pa               REAL,
            ou               REAL,
            exc_score        REAL,
            exc_grade        TEXT,
            final_rank       INTEGER,                    -- rank among the kept
            pinned           INTEGER NOT NULL DEFAULT 0, -- was an Editor's Choice pin
            created_at       TEXT NOT NULL,
            UNIQUE(job_id, url_normalized)
        )
    """)
    for ddl in (
        "CREATE INDEX IF NOT EXISTS idx_run_candidates_coll "
        "ON run_candidates(collection_type, collection_key, run_started_at)",
        "CREATE INDEX IF NOT EXISTS idx_run_candidates_job ON run_candidates(job_id)",
        "CREATE INDEX IF NOT EXISTS idx_run_candidates_url ON run_candidates(url_normalized)",
        # The mediation read: "what may the editor reconsider for this run".
        "CREATE INDEX IF NOT EXISTS idx_run_candidates_mediate "
        "ON run_candidates(job_id, outcome, overturnable)",
    ):
        conn.execute(ddl)
    conn.commit()


def _norm(url: str) -> str:
    try:
        from input.pipeline.url_utils import normalize_url
        return normalize_url(url) or url
    except Exception:
        return url


def _num(v: Any) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_rows(batch: dict, *, collection_type: str, collection_key: str,
               job_id: Optional[int], run_started_at: str) -> list[dict]:
    """Turn one `build_batch` result into ledger rows — winners and losers alike.

    Pure: no DB, no network. `build_batch` already returns every dropped entry as
    a full dict (`dropped_disallowed`, `dropped_not_recipe`, `dropped_moz`,
    `dropped_low_ou`) and only its COUNTS reach the caller; this is what those
    counts were computed from.

    `rank_cut` is the class worth naming: entries that cleared every gate and
    merely fell below `top_n_final`. They are the cheapest promotions available
    to a mediation pass — already fetched, already scored, already judged a
    recipe — and today they vanish entirely.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    seen: set[str] = set()

    def add(e: dict, *, stage: str, outcome: str, reason: Optional[str],
            overturnable: bool, final_rank: Optional[int] = None) -> None:
        url = (e.get("url") or "").strip()
        if not url:
            return
        key = _norm(url)
        if key in seen:      # a URL can appear in two stage lists (e.g. fetch-fail
            return           # re-scored); the FIRST/most-advanced classification wins
        seen.add(key)
        # An entry may name the stage it died at (the publisher harvest does — the
        # same `collection-title` reason fires pre-fetch there and mid-filter in the
        # dish batch). Reason still decides overturnability; only the location moves.
        stage = e.get("_stage") or stage
        rows.append({
            "job_id": job_id,
            "collection_type": collection_type,
            "collection_key": collection_key,
            "run_started_at": run_started_at,
            "url": url,
            "url_normalized": key,
            "title": e.get("title") or None,
            "serp_rank": e.get("google_rank"),
            "queries": json.dumps(e.get("_queries") or [], ensure_ascii=False),
            "stage": stage,
            "outcome": outcome,
            "reason": reason,
            "overturnable": 1 if overturnable else 0,
            "da": _num(e.get("da")),
            "pa": _num(e.get("pa")),
            "ou": _num(e.get("ou")),
            "exc_score": _num(e.get("exc_score")),
            "exc_grade": e.get("exc_grade"),
            "final_rank": final_rank,
            "pinned": 1 if e.get("_pinned") else 0,
            "created_at": now,
        })

    # Most-advanced first, so a URL that appears twice keeps its best outcome.
    for i, e in enumerate(batch.get("entries") or [], start=1):
        add(e, stage="kept", outcome="kept", reason=None, overturnable=False,
            final_rank=i)
    for e in batch.get("reserve") or []:
        add(e, stage="rank_cut", outcome="dropped",
            reason="below top_n_final", overturnable=True)
    for key in ("dropped_low_ou", "dropped_moz", "dropped_not_recipe",
                "dropped_disallowed"):
        for e in batch.get(key) or []:
            reason = e.get("_dropped_reason")
            stage, over = classify(reason)
            add(e, stage=stage, outcome="dropped", reason=reason, overturnable=over)
    return rows


def build_publisher_rows(harvest: dict, *, collection_key: str,
                         job_id: Optional[int], run_started_at: str) -> list[dict]:
    """Ledger rows for one `harvest_publisher_top` result.

    The publisher path needs less than the dish path, and it is worth saying why:
    everything that REACHES Moz is already persisted — `collection_members` keeps
    the losers too, flagged `selected=0` (15,102 rows today). So a publisher's
    `rank_cut` class was never lost. What was lost is everything discarded BEFORE
    scoring — the path scope, the archive/roundup pre-filter, the is_recipe drops
    and the Moz failures — which is exactly `harvest['dropped_candidates']`.

    The scored members are still written here so one run reads as one ledger, and
    so `mediatable_for_run` can hand the editor the kept set and the rejects
    together without joining across two tables with different lifecycles.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    seen: set[str] = set()

    def add(e: dict, *, stage: str, outcome: str, reason: Optional[str],
            overturnable: bool, final_rank: Optional[int] = None) -> None:
        url = (e.get("url") or "").strip()
        if not url:
            return
        key = _norm(url)
        if key in seen:
            return
        seen.add(key)
        rows.append({
            "job_id": job_id, "collection_type": "publisher",
            "collection_key": collection_key, "run_started_at": run_started_at,
            "url": url, "url_normalized": key, "title": e.get("title") or None,
            "serp_rank": e.get("file_seq"), "queries": json.dumps([], ensure_ascii=False),
            "stage": stage, "outcome": outcome, "reason": reason,
            "overturnable": 1 if overturnable else 0,
            "da": _num(e.get("da")), "pa": _num(e.get("pa")), "ou": _num(e.get("ou")),
            "exc_score": _num(e.get("rank_score")), "exc_grade": None,
            "final_rank": final_rank, "pinned": 0, "created_at": now,
        })

    for m in harvest.get("members") or []:
        if m.get("selected"):
            add(m, stage="kept", outcome="kept", reason=None, overturnable=False,
                final_rank=m.get("rank"))
        else:
            # Scored but below `keep` — the publisher analogue of rank_cut, and the
            # cheapest thing an editor can promote: already fetched, already scored.
            add(m, stage="rank_cut", outcome="dropped", reason="below keep",
                overturnable=True, final_rank=m.get("rank"))
    for e in harvest.get("dropped_candidates") or []:
        reason = e.get("_dropped_reason")
        stage, over = classify(reason)
        add(e, stage=e.get("_stage") or stage, outcome="dropped",
            reason=reason, overturnable=over)
    return rows


def record_run(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Persist ledger rows. Idempotent per (job_id, url_normalized) so a retried
    save cannot double-write a run."""
    rows = list(rows)
    if not rows:
        return 0
    ensure_candidate_ledger_table(conn)
    cols = ("job_id", "collection_type", "collection_key", "run_started_at", "url",
            "url_normalized", "title", "serp_rank", "queries", "stage", "outcome",
            "reason", "overturnable", "da", "pa", "ou", "exc_score", "exc_grade",
            "final_rank", "pinned", "created_at")
    conn.executemany(
        f"INSERT INTO run_candidates ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)}) "
        f"ON CONFLICT(job_id, url_normalized) DO NOTHING",
        [tuple(r.get(c) for c in cols) for r in rows],
    )
    conn.commit()
    return len(rows)


_SELECT = "SELECT * FROM run_candidates"


def list_for_run(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    """Every candidate of one run, winners first then nearest-miss outward."""
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        _SELECT + " WHERE job_id = ? ORDER BY outcome DESC, final_rank, ou DESC",
        (job_id,))]


def mediatable_for_run(conn: sqlite3.Connection, job_id: int) -> dict:
    """The mediation packet: what was kept, and what the editor is ALLOWED to
    argue for. Facts are excluded here rather than in the prompt — a boundary
    that lives in a prompt is a boundary that erodes."""
    conn.row_factory = sqlite3.Row
    kept = [dict(r) for r in conn.execute(
        _SELECT + " WHERE job_id = ? AND outcome = 'kept' ORDER BY final_rank",
        (job_id,))]
    reconsider = [dict(r) for r in conn.execute(
        _SELECT + " WHERE job_id = ? AND outcome = 'dropped' AND overturnable = 1 "
        "ORDER BY ou DESC", (job_id,))]
    excluded = conn.execute(
        "SELECT COUNT(*) FROM run_candidates WHERE job_id = ? AND outcome = 'dropped' "
        "AND overturnable = 0", (job_id,)).fetchone()[0]
    return {"kept": kept, "reconsider": reconsider,
            "excluded_as_fact": excluded}


def run_summary(conn: sqlite3.Connection, job_id: int) -> dict:
    """Counts by stage — the funnel, reconstructable from the ledger alone."""
    conn.row_factory = sqlite3.Row
    return {r["stage"]: r["n"] for r in conn.execute(
        "SELECT stage, COUNT(*) AS n FROM run_candidates WHERE job_id = ? "
        "GROUP BY stage", (job_id,))}
