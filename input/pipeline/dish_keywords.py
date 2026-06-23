"""Demand-side dish-keyword capture (capture-now, normalize-later).

A SEMrush organic Top-Pages export carries, per recipe URL, the single highest-traffic
search query ("Top Keyword") + its traffic, intent, and which AI answer-engines surface
it. That's a DEMAND-VALIDATED, self-classifying dish name. We stash it cheaply while the
harvest is already reading the file; a deliberate later pass normalizes these raw keywords
to canonical dishes ([[project_dish_catalog_table]] / [[project_dish_library]]) and uses
the per-recipe keyword as a dish-match hint ([[project_identity_card]]). Same discipline as
the is-recipe corpus + the url word-lists: capture raw now, promote later
([[project_corpus_ml]]). Best-effort — a failure here never breaks a harvest.
"""
import sqlite3
from input.pipeline.db import connect as _connect  # WAL busy_timeout — input/pipeline/db.py
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db_path(db_path: Optional[str]) -> str:
    if db_path:
        return db_path
    import save_recipe_api as _api   # lazy — avoid import cycle at module load
    return _api.DB_PATH


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dish_keywords (
            url_normalized TEXT PRIMARY KEY,  -- join key to recipes.url_normalized + dedup
            keyword        TEXT,              -- SEMrush Top Keyword (raw demand-side dish name)
            url            TEXT,
            domain         TEXT,
            traffic        REAL,              -- monthly organic traffic to the page (demand weight)
            traffic_pct    REAL,              -- Traffic (%) — share of the publisher's traffic
            intent         TEXT,              -- Primary Intent (Informational/Commercial/…)
            answer_engines TEXT,              -- which AI engines surface it (search-gpt, gemini…)
            captured_at    TEXT
        )
        """
    )
    try:   # migrate pre-traffic_pct tables (ALTER is a no-op if present)
        conn.execute("ALTER TABLE dish_keywords ADD COLUMN traffic_pct REAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dishkw_kw ON dish_keywords(keyword)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dishkw_traffic ON dish_keywords(traffic)")


def capture(rows, domain, db_path: Optional[str] = None) -> int:
    """Upsert keyword rows keyed on NORMALIZED url (latest snapshot wins, so a re-harvest
    refreshes traffic rather than duplicating). `rows` = [{url, keyword, traffic, intent,
    answer_engines}]. Best-effort: never raises. Returns rows written."""
    try:
        try:
            from input.pipeline.url_utils import normalize_url
        except Exception:
            normalize_url = lambda u: u   # noqa: E731 — degrade gracefully
        now = _now()
        recs = []
        for r in rows:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            try:
                un = normalize_url(url)
            except Exception:
                un = url
            recs.append((un, (r.get("keyword") or "").strip(), url, domain,
                         r.get("traffic"), r.get("traffic_pct"), (r.get("intent") or "").strip(),
                         (r.get("answer_engines") or "").strip(), now))
        if not recs:
            return 0
        with _connect(_resolve_db_path(db_path), timeout=5.0) as conn:
            ensure_table(conn)
            conn.executemany(
                "INSERT OR REPLACE INTO dish_keywords"
                "(url_normalized,keyword,url,domain,traffic,traffic_pct,intent,answer_engines,captured_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)", recs)
            conn.commit()
        return len(recs)
    except Exception as e:   # capture must never break a harvest
        try:
            print(f"  [dish-keywords] capture skipped ({type(e).__name__}: {e})")
        except Exception:
            pass
        return 0


def stats(db_path: Optional[str] = None) -> dict:
    try:
        with _connect(_resolve_db_path(db_path), timeout=5.0) as conn:
            ensure_table(conn)
            total = conn.execute("SELECT COUNT(*) FROM dish_keywords").fetchone()[0]
            top = conn.execute(
                "SELECT keyword, traffic, domain FROM dish_keywords "
                "ORDER BY traffic DESC LIMIT 15").fetchall()
        return {"total": total, "top_by_traffic": top}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
