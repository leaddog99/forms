"""Byproduct training-data capture for the is-recipe classifier.

Every is-recipe decision — dish batch AND domain harvest, both routed through
the canonical ``intake.build_query_batch._is_recipe_filter`` — is logged here
as a labeled sample (input content, heuristic signals, score, decision, reason,
provenance) into a SEPARATE, git-ignored SQLite DB. We accumulate a training
set as a free side effect of running the pipeline. See docs/corpus-ml-strategy.md.

Design rules (the no-regret discipline):
  - APPEND-ONLY and OFF THE HOT PATH: capture is best-effort and never raises.
    A failure here must never break a harvest/batch run.
  - SEPARATE DB (``training.db``, git-ignored): never bloats recipes.db.
  - Stores the CONTENT the filter actually scored (trimmed), so future feature
    sets / embeddings are derivable later. Labels ALONE just re-learn the
    heuristic — keeping the source text is what lets a classifier beat it.
  - Reserves ``human_label`` / ``human_labeled_at`` for curator corrections:
    the gold signal that teaches where the heuristic is WRONG (the leaks).

This module is deliberately self-contained — it imports nothing from the
pipeline, so it can't introduce a circular import and is safe to call from the
filter chokepoint.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Separate, git-ignored store at the repo root (parent of intake/).
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "training.db"
TRAINING_DB_PATH = os.getenv("BCC_TRAINING_DB", str(_DEFAULT_DB))

# Cap stored content so a pathological page can't bloat a row, but generous
# enough that the ingredient/direction vocabulary the phrase scorer keys on is
# preserved (disk is cheap; lost signal isn't).
_CONTENT_CAP = 50_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS is_recipe_samples (
    sample_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at      TEXT NOT NULL,   -- ISO-8601 UTC
    url              TEXT,
    title            TEXT,
    content          TEXT,            -- trimmed visible text the filter scored
    content_chars    INTEGER,
    lang_code        TEXT,
    has_jsonld       INTEGER,         -- 0/1 schema.org/Recipe present
    translated       INTEGER,         -- 0/1 filter-stage translation used
    recipe_score     REAL,            -- phrase score (or JSON-LD pass score)
    threshold        REAL,            -- IS_RECIPE_THRESHOLD at capture time
    decision         TEXT,            -- 'kept' | 'dropped'  (the heuristic LABEL)
    reason           TEXT,            -- json-ld | phrase-score | collection-title | fetch-failed | ...
    source           TEXT,            -- 'dish_batch' | 'domain_harvest' | ...
    provenance       TEXT,            -- JSON: {dish/domain, ...}
    explore          INTEGER DEFAULT 0,-- 1 = ε-exploration: a would-be url-prefilter SKIP
                                       -- that we verified anyway, so this row is an
                                       -- UNBIASED label in the region the filter skips.
    human_label      TEXT,            -- NULLABLE gold correction: 'recipe' | 'not_recipe'
    human_labeled_at TEXT,
    human_note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_isr_decision ON is_recipe_samples(decision);
CREATE INDEX IF NOT EXISTS idx_isr_human    ON is_recipe_samples(human_label);
CREATE INDEX IF NOT EXISTS idx_isr_source   ON is_recipe_samples(source);
"""

_COLUMNS = (
    "captured_at,url,title,content,content_chars,lang_code,has_jsonld,"
    "translated,recipe_score,threshold,decision,reason,source,provenance,explore,"
    "human_label,human_labeled_at,human_note"
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(TRAINING_DB_PATH, timeout=5.0)
    conn.executescript(_SCHEMA)  # idempotent self-install
    try:                          # migrate pre-explore DBs (ALTER is a no-op if present)
        conn.execute("ALTER TABLE is_recipe_samples ADD COLUMN explore INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    return conn


def ensure_table() -> None:
    """Create the table/indexes if needed (idempotent, best-effort)."""
    try:
        _connect().close()
    except Exception:
        pass


def _reason_for(entry: dict, decision: str) -> str:
    if entry.get("jsonld_recipe"):
        return "json-ld"
    if decision == "dropped":
        return entry.get("_dropped_reason") or "dropped"
    return "phrase-score"


def capture_samples(kept, dropped, *, source="unknown", provenance=None,
                    threshold=None) -> int:
    """Append one labeled sample per entry: ``kept`` -> 'kept', ``dropped`` ->
    'dropped'. Reads filter fields off each entry dict, including the transient
    ``_cap_text`` the filter stashed (the content it scored).

    Best-effort: never raises. Returns the number of rows written (0 on any
    failure, so the caller can log/ignore without a guard of its own).
    """
    try:
        prov_json = json.dumps(provenance, ensure_ascii=False) if provenance else None
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for decision, entries in (("kept", kept or []), ("dropped", dropped or [])):
            for e in entries:
                if not isinstance(e, dict):
                    continue
                content = (e.get("_cap_text") or "")[:_CONTENT_CAP]
                rows.append((
                    now,
                    e.get("url"),
                    e.get("title"),
                    content,
                    len(content),
                    e.get("_lang"),
                    1 if e.get("jsonld_recipe") else 0,
                    1 if e.get("_translated_for_filter") else 0,
                    e.get("recipe_score"),
                    threshold,
                    decision,
                    _reason_for(e, decision),
                    source,
                    prov_json,
                    1 if e.get("_explore") else 0,
                    None, None, None,   # human_label, human_labeled_at, human_note
                ))
        if not rows:
            return 0
        placeholders = ",".join("?" * 18)
        conn = _connect()
        try:
            conn.executemany(
                f"INSERT INTO is_recipe_samples ({_COLUMNS}) VALUES ({placeholders})",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return len(rows)
    except Exception as ex:  # logging must never break the pipeline
        try:
            print(f"  [training-capture] skipped ({type(ex).__name__}: {ex})")
        except Exception:
            pass
        return 0


def stats() -> dict:
    """Quick counts for verification: total + by decision/source/human_labeled."""
    try:
        conn = _connect()
        try:
            cur = conn.cursor()
            total = cur.execute("SELECT COUNT(*) FROM is_recipe_samples").fetchone()[0]
            by_decision = dict(cur.execute(
                "SELECT decision, COUNT(*) FROM is_recipe_samples GROUP BY decision"
            ).fetchall())
            by_source = dict(cur.execute(
                "SELECT source, COUNT(*) FROM is_recipe_samples GROUP BY source"
            ).fetchall())
            human = cur.execute(
                "SELECT COUNT(*) FROM is_recipe_samples WHERE human_label IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        return {"db": TRAINING_DB_PATH, "total": total,
                "by_decision": by_decision, "by_source": by_source,
                "human_labeled": human}
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}", "db": TRAINING_DB_PATH}


# --------------------------------------------------------------------------
# Human-correction UI support (the gold-label workflow). Curators review the
# captured samples and correct the heuristic where it's wrong → `human_label`.
# See forms/training.html + the /training/is-recipe/* endpoints.
# --------------------------------------------------------------------------

_LIST_COLUMNS = (
    "sample_id, captured_at, url, title, content_chars, lang_code, has_jsonld, "
    "translated, recipe_score, threshold, decision, reason, source, provenance, explore, "
    "human_label, human_labeled_at, human_note"
)

# Structural markers that LOCATE the recipe body inside a full-page text dump so
# the snippet can skip the site chrome (nav / cookie banner / subscribe / byline)
# that otherwise fills the first few hundred chars of every page. Deliberately
# includes terms PRUNED from RECIPE_PHRASES (e.g. "ingredients") — here they're
# snippet ANCHORS for legibility, not scoring signals. Captured content is
# lowercased, so these are lowercase.
_SNIPPET_ANCHORS = (
    "ingredients", "instructions", "directions", "method:", "method ",
    "prep time", "cook time", "total time", "yield", "servings", "serves",
    "preheat",
)


def _smart_snippet(content: str, max_chars: int = 400) -> str:
    """A RECIPE-RELEVANT window of the captured (lowercased) page text. Anchors
    on the earliest structural recipe marker so the curator sees the ingredient/
    step region instead of the page header; falls back to the head when no marker
    is present (which is itself a weak "probably not a recipe" tell)."""
    if not content:
        return ""
    pos = -1
    for a in _SNIPPET_ANCHORS:
        i = content.find(a)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        return content[:max_chars]
    start = max(0, pos - 40)
    if start > 0:  # snap to a word boundary so we don't cut mid-word
        sp = content.find(" ", start)
        start = sp + 1 if (sp != -1 and sp < pos) else start
    snip = content[start:start + max_chars]
    return ("…" + snip) if start > 0 else snip


def _matched_phrases(content: str, limit: int = 30) -> list:
    """The RECIPE_PHRASES actually present in the captured text — the exact
    evidence the heuristic score is built from. A page dense with these
    ("prep time", "1/2 cup", "preheat the", "bake for") is obviously a recipe;
    an empty list is the strongest "not a recipe" tell. Ordered by RECIPE_PHRASES
    so the high-signal structural markers come first. Best-effort: never raises."""
    if not content:
        return []
    try:
        from input.pipeline.config import RECIPE_PHRASES
    except Exception:
        return []
    seen, out = set(), []
    for p in RECIPE_PHRASES:
        if p in content:
            t = p.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
                if len(out) >= limit:
                    break
    return out


def list_samples(*, limit=50, offset=0, search=None, decision=None,
                 source=None, label=None, has_content=False, snippet_chars=400,
                 sort="recent") -> dict:
    """Page of samples for the correction UI + a short content snippet. Filters:
    ``search`` (url/title/content substring), ``decision`` ('kept'|'dropped'),
    ``source``, ``label`` ('unlabeled'|'labeled'|'recipe'|'not_recipe'), and
    ``has_content`` (True = only rows the filter actually scored text for — hides
    fetch-failed and the pre-fetch collection-title drops, i.e. the rows not
    worth correcting).

    ``sort``: 'recent' (default, newest first) or 'borderline' — orders by
    distance from the decision threshold (``ABS(recipe_score - threshold)``
    ascending) so the most UNCERTAIN rows (score ≈ threshold) come first and the
    JSON-LD certainties (score 100) sink. This is uncertainty sampling: the rows
    where the heuristic is most likely WRONG are the highest-value to label.
    Returns ``{total, rows}`` (``total`` = rows matching the filter).
    """
    try:
        where, params = [], []
        if has_content:
            where.append("content_chars > 0")
        if search:
            where.append("(url LIKE ? OR title LIKE ? OR content LIKE ?)")
            like = f"%{search}%"
            params += [like, like, like]
        if decision in ("kept", "dropped"):
            where.append("decision = ?")
            params.append(decision)
        if source:
            where.append("source = ?")
            params.append(source)
        if label == "unlabeled":
            where.append("human_label IS NULL")
        elif label == "labeled":
            where.append("human_label IS NOT NULL")
        elif label in ("recipe", "not_recipe"):
            where.append("human_label = ?")
            params.append(label)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        # 'borderline' = uncertainty sampling: nearest the threshold first. Rows
        # with no score/threshold (pre-fetch drops, legacy) go last; recency
        # breaks ties.
        if sort == "borderline":
            order_by = ("ORDER BY (recipe_score IS NULL OR threshold IS NULL) ASC, "
                        "ABS(recipe_score - threshold) ASC, sample_id DESC")
        else:
            order_by = "ORDER BY sample_id DESC"
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM is_recipe_samples {clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS}, content AS _content "
                f"FROM is_recipe_samples {clause} "
                f"{order_by} LIMIT ? OFFSET ?",
                [*params, int(limit), int(offset)],
            ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            d = dict(r)
            content = d.pop("_content", "") or ""
            # Surface the recipe EVIDENCE, not the page chrome: a recipe-anchored
            # snippet + the matched recipe phrases the score is built from.
            d["snippet"] = _smart_snippet(content, int(snippet_chars))
            d["matched_phrases"] = _matched_phrases(content)
            out.append(d)
        return {"total": total, "rows": out}
    except Exception as ex:
        return {"total": 0, "rows": [], "error": f"{type(ex).__name__}: {ex}"}


def set_human_label(sample_id, label, note=None) -> dict:
    """Set or clear the curator's correction on one sample. ``label`` ∈
    {'recipe', 'not_recipe'} to set, or None/'' to clear. Returns the updated
    {sample_id, human_label, human_labeled_at, human_note} (or {'error': ...})."""
    try:
        label = (label or "").strip().lower() or None
        if label not in (None, "recipe", "not_recipe"):
            return {"error": f"invalid label {label!r} (recipe|not_recipe|null)"}
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            if label is None:
                conn.execute(
                    "UPDATE is_recipe_samples SET human_label=NULL, "
                    "human_labeled_at=NULL, human_note=NULL WHERE sample_id=?",
                    (int(sample_id),),
                )
            else:
                conn.execute(
                    "UPDATE is_recipe_samples SET human_label=?, "
                    "human_labeled_at=?, human_note=? WHERE sample_id=?",
                    (label, datetime.now(timezone.utc).isoformat(), note,
                     int(sample_id)),
                )
            conn.commit()
            row = conn.execute(
                "SELECT sample_id, human_label, human_labeled_at, human_note "
                "FROM is_recipe_samples WHERE sample_id=?", (int(sample_id),)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return {"error": f"sample {sample_id} not found"}
        return dict(row)
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}


if __name__ == "__main__":  # `python -m intake.training_capture` -> print stats
    import pprint
    pprint.pprint(stats())
