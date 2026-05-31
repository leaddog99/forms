"""Chapter-level OU fit — backbone grade cohort for recipes whose
per-dish cohort is too small (n<25) for a trustworthy fit.

Design context: per-dish exceptionalism fits work great when a dish
refresh produces 25+ qualifying URLs. Niche dishes (Agnolotti, Tourtière,
specific regional variants) end up with cohorts of 5-15 URLs after the
front-end pipeline cuts. The dish-level regression refuses to fit those
(below_min_n) and the recipes land ungraded — em-dash in the UI.

Chapter-level fit fills this gap. Each chapter aggregates the (DA, PA, OU)
of every saved master_recipe in that chapter, fits the same regression
shape used per-dish, and stores the result on a `chapters` table row.
When per-dish grading fails, the grading code falls through to the
chapter cohort. The grade is less editorially precise ("graded against
all Pasta & Noodles recipes" vs "graded against Agnolotti recipes") but
present rather than absent, and the basis block carries the cohort
identity so the UI can label which cohort produced the grade.

The fit math is identical to `intake.build_query_batch._compute_custom_ou`
(linear / quadratic / power, σ_effective with floor). It's duplicated
here to keep this module import-light — the batch path drags in
SerpAPI / Moz / numpy together, while this path only ever needs to
query the DB and do polyfit on a few hundred points.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import numpy as np


# Mirror constants from intake.build_query_batch. Keep in sync — if the
# batch path's grade scale changes, this fallback path must change too
# or grades won't compare apples-to-apples.
_MIN_FIT_N = 25
EXC_SIGMA_FLOOR = 0.5
EXC_BASE = 75.0
EXC_SIGMA_MULT = 10.0


def ensure_chapters_table(conn: sqlite3.Connection) -> None:
    """Create the chapters table if absent. Idempotent. One row per
    chapter; we don't pre-seed — rows get inserted lazily by
    compute_and_store_chapter_fit when first computed.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chapters (
            name              TEXT PRIMARY KEY,
            last_ou_fit       TEXT,
            fit_recipe_count  INTEGER,
            fit_updated_at    TEXT,
            notes             TEXT
        )
        """
    )
    # Top-10 recipes for the chapter (highest OU across its dishes), a JSON
    # snapshot recomputed at fit time. Added via ALTER for pre-existing DBs.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chapters)").fetchall()}
    if "top_recipes" not in cols:
        conn.execute("ALTER TABLE chapters ADD COLUMN top_recipes TEXT")
    # dish_run_data_points captures the FULL (DA, PA) cohort each dish
    # refresh feeds into _compute_custom_ou — including URLs that later
    # got dropped at the OU floor or failed extraction. That's the
    # statistically correct cohort for chapter-level aggregation: σ and
    # the regression coefficients should reflect the URL universe the
    # dish-level fit actually saw, not the heavily curated "winners"
    # subset that ended up in master_recipes. PK on (dish_name, url) —
    # one row per (dish, URL); replaced wholesale on each refresh of
    # that dish.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dish_run_data_points (
            dish_name   TEXT NOT NULL,
            url         TEXT NOT NULL,
            da          REAL,
            pa          REAL,
            created_at  TEXT NOT NULL,
            PRIMARY KEY (dish_name, url)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drdp_dish ON dish_run_data_points(dish_name)"
    )
    conn.commit()


def replace_data_points_for_dish(
    conn: sqlite3.Connection,
    dish_name: str,
    points: list[tuple[str, float | None, float | None]],
) -> int:
    """Wipe + rewrite the (URL, DA, PA) points for one dish. Called
    after each successful dish refresh's _compute_custom_ou step.

    `points` is a list of (url, da, pa) tuples — exactly the entries
    that fed the regression. None values for DA or PA are accepted
    and stored (filtered out at chapter-fit time)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute("DELETE FROM dish_run_data_points WHERE dish_name = ?", (dish_name,))
    conn.executemany(
        "INSERT INTO dish_run_data_points (dish_name, url, da, pa, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(dish_name, u, da, pa, now_iso) for u, da, pa in points],
    )
    conn.commit()
    return len(points)


def backfill_data_points_from_corpus(conn: sqlite3.Connection) -> dict:
    """One-shot seed of dish_run_data_points from the data we ALREADY
    have: master_recipes (saved winners) + dish_rejects (URLs that
    made it past Moz but failed extract / save / save-gate). The
    OU-floor drops aren't recoverable retroactively — they were
    discarded after the fit ran in the original refresh — so this
    seed is intentionally incomplete. Live refreshes going forward
    capture the full cohort via replace_data_points_for_dish.
    """
    ensure_chapters_table(conn)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Master recipes — keyed on _master.dish (only top-kind rows have
    # this; harvest / legacy do too but represent the same dish).
    n_master = conn.execute(
        """
        INSERT OR REPLACE INTO dish_run_data_points
            (dish_name, url, da, pa, created_at)
        SELECT
            json_extract(data, '$._master.dish'),
            COALESCE(json_extract(data, '$._source.originalUrl'), url_normalized),
            json_extract(data, '$._scoring.domainAuthority'),
            json_extract(data, '$._scoring.pageAuthority'),
            ?
        FROM master_recipes
        WHERE json_extract(data, '$._master.dish') IS NOT NULL
          -- Algorithmic source only: the OU fit must reflect the organic
          -- SERP authority landscape, NOT editorially curated picks
          -- (editors_choice / legacy). Those are exceptions by design and
          -- would skew the regression baseline.
          AND json_extract(data, '$._master.kind') IN ('top', 'harvest')
          AND json_extract(data, '$._scoring.domainAuthority') IS NOT NULL
          AND json_extract(data, '$._scoring.pageAuthority') IS NOT NULL
        """,
        (now_iso,),
    ).rowcount

    # Dish rejects — captures URLs that survived front-end + Moz but
    # got dropped during extract or save-gate.
    n_rejects = conn.execute(
        """
        INSERT OR REPLACE INTO dish_run_data_points
            (dish_name, url, da, pa, created_at)
        SELECT dish_name, url, da, pa, ?
        FROM dish_rejects
        WHERE da IS NOT NULL AND pa IS NOT NULL
        """,
        (now_iso,),
    ).rowcount

    conn.commit()
    return {"from_master_recipes": n_master, "from_dish_rejects": n_rejects}


def _r_squared(y_actual: np.ndarray, y_predicted: np.ndarray) -> float:
    ss_res = float(np.sum((y_actual - y_predicted) ** 2))
    ss_tot = float(np.sum((y_actual - y_actual.mean()) ** 2))
    if ss_tot <= 0:
        return 0.0
    return 1.0 - (ss_res / ss_tot)


def _fit_da_pa(da_arr: np.ndarray, pa_arr: np.ndarray) -> dict:
    """Run the linear/quadratic/power triple fit and pick best R².
    Returns the same fit-shape dict that
    `intake.build_query_batch._compute_custom_ou` stores on
    `dishes.last_ou_fit` — drop-in compatible with
    `input.pipeline.grading.compute_exceptionalism`.
    """
    n = len(da_arr)

    # Linear
    coeffs_lin = np.polyfit(da_arr, pa_arr, 1)
    pred_lin = np.polyval(coeffs_lin, da_arr)
    r2_lin = _r_squared(pa_arr, pred_lin)

    # Quadratic
    coeffs_quad = np.polyfit(da_arr, pa_arr, 2)
    pred_quad = np.polyval(coeffs_quad, da_arr)
    r2_quad = _r_squared(pa_arr, pred_quad)

    # Power (log-linear)
    pos_mask = (da_arr > 0) & (pa_arr > 0)
    if pos_mask.sum() >= _MIN_FIT_N:
        log_da = np.log(da_arr[pos_mask])
        log_pa = np.log(pa_arr[pos_mask])
        slope, intercept = np.polyfit(log_da, log_pa, 1)
        pwr_a = float(np.exp(intercept))
        pwr_b = float(slope)
        pred_pwr = np.where(
            da_arr > 0,
            pwr_a * (np.maximum(da_arr, 1e-9) ** pwr_b),
            0.0,
        )
        r2_pwr = _r_squared(pa_arr, pred_pwr)
        power_available = True
    else:
        pwr_a, pwr_b, r2_pwr, pred_pwr, power_available = 0.0, 0.0, float("-inf"), None, False

    candidates = [
        ("linear", r2_lin, coeffs_lin, pred_lin),
        ("quadratic", r2_quad, coeffs_quad, pred_quad),
    ]
    if power_available:
        candidates.append(("power", r2_pwr, np.array([pwr_a, pwr_b]), pred_pwr))
    chosen_name, chosen_r2, chosen_coeffs, chosen_pred = max(candidates, key=lambda c: c[1])

    residuals = pa_arr - chosen_pred
    sigma_observed = float(np.std(residuals, ddof=0))
    sigma_effective = max(sigma_observed, EXC_SIGMA_FLOOR)

    return {
        "used": True,
        "n": n,
        "model": chosen_name,
        "r2_linear": float(r2_lin),
        "r2_quadratic": float(r2_quad),
        "r2_power": float(r2_pwr) if power_available else None,
        "r2_chosen": float(chosen_r2),
        "coefficients": [float(x) for x in chosen_coeffs],
        "sigma_observed": round(sigma_observed, 4),
        "sigma_effective": round(sigma_effective, 4),
        "exc_base": EXC_BASE,
        "exc_sigma_mult": EXC_SIGMA_MULT,
        "exc_sigma_floor": EXC_SIGMA_FLOOR,
    }


def compute_chapter_top_recipes(
    conn: sqlite3.Connection, chapter: str, limit: int = 10,
) -> list[dict]:
    """The chapter's `limit` highest-OU master_recipes across all its dishes
    (joined via _master.dish → dishes.chapter). A compact snapshot stored on
    the chapter row at fit time — independent of the regression (works even
    when the chapter is below the fit minimum)."""
    rows = conn.execute(
        """
        SELECT mr.recipe_id, mr.data
        FROM master_recipes mr
        JOIN dishes d ON d.name = json_extract(mr.data, '$._master.dish')
        WHERE d.chapter = ?
          AND json_extract(mr.data, '$._scoring.ouScore') IS NOT NULL
        ORDER BY CAST(json_extract(mr.data, '$._scoring.ouScore') AS REAL) DESC, mr.id
        LIMIT ?
        """,
        (chapter, limit),
    ).fetchall()
    out: list[dict] = []
    for recipe_uuid, dj in rows:
        try:
            d = json.loads(dj)
        except Exception:
            continue
        s = d.get("_scoring") or {}
        m = d.get("_master") or {}
        exc = m.get("exceptionalism") or {}
        src = d.get("_source") or {}
        img = d.get("image")
        out.append({
            "recipe_id": recipe_uuid,
            "name": d.get("name") or "(no title)",
            "dish": m.get("dish") or "",
            "ou": s.get("ouScore"),
            "da": s.get("domainAuthority"),
            "pa": s.get("pageAuthority"),
            "grade": exc.get("grade"),
            "site_name": src.get("siteName") or "",
            "source_url": src.get("originalUrl") or "",
            "preview_image": src.get("previewImage") or "",
            "fallback_image": (img[0] if isinstance(img, list) and img else None),
        })
    return out


def get_chapter_top_recipes(conn: sqlite3.Connection, name: str) -> list[dict]:
    """Parsed top-recipes snapshot stored on the chapter row (or [])."""
    row = conn.execute(
        "SELECT top_recipes FROM chapters WHERE name = ?", (name,),
    ).fetchone()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except Exception:
        return []


def compute_and_store_chapter_fit(conn: sqlite3.Connection, chapter: str) -> dict:
    """Pull every saved master_recipe in `chapter`, fit the chapter-wide
    OU regression, store on the chapters row. Returns the fit dict (used
    field tells caller whether the fit succeeded).

    When n<_MIN_FIT_N, the fit is skipped and a {used: False, reason:
    'below_min_n'} stub is stored so the grading fallback can read it
    cheaply without re-running the SQL count.
    """
    # Pull (DA, PA) from dish_run_data_points joined to dishes — this
    # is the full URL cohort each dish refresh actually fit against,
    # including URLs later dropped at the OU floor or in extraction.
    # That's what the user flagged: chapter fits were biased by only
    # seeing the saved-winners subset. Now they see the same cohort
    # the per-dish fit did, summed across every dish in the chapter.
    rows = conn.execute(
        """
        SELECT data.da, data.pa
        FROM dish_run_data_points data
        JOIN dishes d ON d.name = data.dish_name
        WHERE d.chapter = ?
          AND data.da IS NOT NULL
          AND data.pa IS NOT NULL
        """,
        (chapter,),
    ).fetchall()

    da_vals: list[float] = []
    pa_vals: list[float] = []
    for da, pa in rows:
        if isinstance(da, (int, float)) and isinstance(pa, (int, float)):
            da_vals.append(float(da))
            pa_vals.append(float(pa))

    n = len(da_vals)
    now_iso = datetime.now(timezone.utc).isoformat()

    if n < _MIN_FIT_N:
        fit = {"used": False, "n": n, "reason": "below_min_n"}
    else:
        fit = _fit_da_pa(np.array(da_vals), np.array(pa_vals))

    # Snapshot the chapter's top-10 recipes by OU at the same time, so the
    # chapter record always carries a current "best of" set.
    ensure_chapters_table(conn)
    top = compute_chapter_top_recipes(conn, chapter, limit=10)

    conn.execute(
        """
        INSERT INTO chapters (name, last_ou_fit, fit_recipe_count, fit_updated_at, top_recipes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            last_ou_fit      = excluded.last_ou_fit,
            fit_recipe_count = excluded.fit_recipe_count,
            fit_updated_at   = excluded.fit_updated_at,
            top_recipes      = excluded.top_recipes
        """,
        (chapter, json.dumps(fit), n, now_iso, json.dumps(top)),
    )
    conn.commit()
    return fit


def get_chapter_fit(conn: sqlite3.Connection, chapter: str) -> Optional[dict]:
    """Return the stored fit dict for a chapter (or None if no row yet).
    Caller checks `fit['used']` before using; below_min_n fits won't
    grade anything."""
    row = conn.execute(
        "SELECT last_ou_fit FROM chapters WHERE name = ?", (chapter,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def list_chapters_with_status(
    conn: sqlite3.Connection,
    canonical_names: list[str],
) -> list[dict]:
    """For the chapters admin page sidebar. Returns one entry per
    canonical chapter (whether or not the chapters row exists yet) with
    the fit status + live recipe count joined in.

    Each entry:
        {
          name, last_ou_fit, fit_recipe_count, fit_updated_at,
          current_recipe_count, fit_status: 'graded'|'below_min_n'|'never'
        }
    """
    ensure_chapters_table(conn)
    # The chapters TABLE is the source of truth for which chapters exist.
    # Seed canonical (classifier-taxonomy) names lacking a row so the DB
    # stays authoritative — chapter data lives in the DB, not only in code.
    conn.executemany(
        "INSERT OR IGNORE INTO chapters (name) VALUES (?)",
        [(n,) for n in canonical_names if n != "Uncertain"],
    )
    conn.commit()
    # One pass: pull every row from the chapters table + every chapter's
    # current recipe count from master_recipes.
    fit_rows = {
        r[0]: r for r in conn.execute(
            "SELECT name, last_ou_fit, fit_recipe_count, fit_updated_at, notes "
            "FROM chapters"
        ).fetchall()
    }
    count_rows = {
        r[0]: r[1] for r in conn.execute(
            "SELECT json_extract(data, '$.classification.chapter') AS chapter, "
            "COUNT(*) FROM master_recipes "
            "WHERE chapter IS NOT NULL GROUP BY chapter"
        ).fetchall()
    }
    # Iterate the TABLE (now seeded with canonical names + any curator-created
    # chapters), not the code constant — the DB is the source of truth.
    out: list[dict] = []
    for name in fit_rows.keys():
        if name == "Uncertain":
            continue
        fit_row = fit_rows.get(name)
        if fit_row:
            _, raw_fit, n, updated, notes = fit_row
            try:
                fit = json.loads(raw_fit) if raw_fit else None
            except Exception:
                fit = None
        else:
            fit = None
            n = None
            updated = None
            notes = None
        if fit is None:
            status = "never"
        elif fit.get("used"):
            status = "graded"
        else:
            status = "below_min_n"
        out.append({
            "name": name,
            "last_ou_fit": fit,
            "fit_recipe_count": n,
            "fit_updated_at": updated,
            "current_recipe_count": int(count_rows.get(name, 0)),
            "fit_status": status,
            "notes": notes,
        })
    # Sort: graded first (most useful at top), then below_min_n (close
    # to graded), then never (no data yet). Within each bucket, by name.
    status_order = {"graded": 0, "below_min_n": 1, "never": 2}
    out.sort(key=lambda c: (status_order[c["fit_status"]], c["name"]))
    return out


def get_chapter_detail(conn: sqlite3.Connection, name: str) -> Optional[dict]:
    """Full detail blob for one chapter, used by the detail panel."""
    fit_row = conn.execute(
        "SELECT name, last_ou_fit, fit_recipe_count, fit_updated_at, notes "
        "FROM chapters WHERE name = ?",
        (name,),
    ).fetchone()
    current_n = conn.execute(
        "SELECT COUNT(*) FROM master_recipes "
        "WHERE json_extract(data, '$.classification.chapter') = ?",
        (name,),
    ).fetchone()[0]
    if fit_row:
        _, raw_fit, fit_n, updated, notes = fit_row
        try:
            fit = json.loads(raw_fit) if raw_fit else None
        except Exception:
            fit = None
    else:
        fit = None
        fit_n = None
        updated = None
        notes = None
    if fit is None:
        status = "never"
    elif fit.get("used"):
        status = "graded"
    else:
        status = "below_min_n"
    return {
        "name": name,
        "last_ou_fit": fit,
        "fit_recipe_count": fit_n,
        "fit_updated_at": updated,
        "current_recipe_count": int(current_n),
        "fit_status": status,
        "notes": notes,
    }


def update_chapter_notes(
    conn: sqlite3.Connection, name: str, notes: Optional[str],
) -> None:
    """Set or clear the curator's notes on a chapter row. Creates the
    row with a no-fit stub if it doesn't exist (so notes survive even
    on chapters that haven't been fit yet)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO chapters (name, notes, fit_updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET notes = excluded.notes",
        (name, notes, now_iso),
    )
    conn.commit()


def chapter_exists(conn: sqlite3.Connection, name: str) -> bool:
    """True if a row for this chapter exists in the chapters table (the
    source of truth for which chapters exist)."""
    return bool(conn.execute(
        "SELECT 1 FROM chapters WHERE name = ?", ((name or "").strip(),),
    ).fetchone())


def create_chapter(
    conn: sqlite3.Connection, name: str, notes: Optional[str] = None,
) -> None:
    """Insert a new curator-created chapter row. Raises ValueError on a
    blank name or a duplicate. A fresh chapter starts unfit (no recipes
    classified into it yet) until populated + refreshed."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Chapter name is required")
    ensure_chapters_table(conn)
    if chapter_exists(conn, name):
        raise ValueError(f"Chapter '{name}' already exists")
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO chapters (name, notes, fit_updated_at) VALUES (?, ?, ?)",
        (name, (notes or None), now_iso),
    )
    conn.commit()


def backfill_all_chapters(conn: sqlite3.Connection, chapter_names: list[str]) -> dict:
    """One-pass recompute of every chapter's fit. Returns a summary
    dict {chapter: {n, used, reason?}} — caller can log it or stash on
    the per-job result blob.

    Called: (1) at boot when the chapters table is empty (one-time
    seed); (2) on demand from an admin endpoint when a chapter looks
    stale; (3) by a nightly cron once the chapters table earns enough
    recipes for a meaningful refresh."""
    ensure_chapters_table(conn)
    out: dict[str, dict] = {}
    for ch in chapter_names:
        fit = compute_and_store_chapter_fit(conn, ch)
        out[ch] = {
            "n": fit.get("n"),
            "used": fit.get("used"),
            "reason": fit.get("reason"),
            "model": fit.get("model"),
        }
    return out
