"""domain_scoring.py — the SYSTEM-WIDE recipe authority score.

A publisher harvest used to rank its recipes by raw PA (`rank_score = pa` in
collections_lib.harvest_publisher_top). Raw PA ignores DA context — a PA-40 page
on a DA-90 domain is UNDER-performing; PA-40 on a DA-50 domain OVER-performs — so
raw-PA ranking quietly favors big domains and gives no number you can compare
ACROSS publishers.

This module replaces that with a single SYSTEM-WIDE authority score, the corpus-
grain generalization of the per-dish OU/power blend (see project_domain_scoring,
project_ou_power_blend):

  - Fit ONE quadratic PA~DA curve over the WHOLE scored population (every dish
    cohort point + every publisher member), not per-dish/per-cohort.
  - OU    = adjusted_PA − predicted_PA(DA)   (over-performance vs the global curve)
  - power = DA + adjusted_PA                  (raw authority)
  - rank_score = (1-w)·pct(OU) + w·pct(power), where each percentile is taken
    against the WHOLE-SYSTEM distribution (not within a single publisher), and
    w = POWER_BLEND_WEIGHT/100.

`adjusted_PA` is the paywall PA-remap (project_paid_pa_calibration): gated
publishers' link-starved pages get lifted to their free-equivalent BEFORE scoring
so OU stops penalizing them for the paywall instead of for quality.

WHY the score is cross-publisher comparable but the within-publisher winner picks
DON'T churn: inside one publisher DA is ~constant, so OU and power are both
monotonic in PA, so the blended percentile is monotonic in PA too — the top-N a
publisher keeps is unchanged; only the rank_score VALUE becomes a comparable
"best recipes anywhere" number.

Single-path reuse:
  - the global fit reuses chapters._fit_da_pa (the same quadratic shape the dish +
    chapter fits use — pinned to quadratic, no model-flip jitter);
  - the paywall remap reuses domains_lib.get_paywall_calibrations.

The fit (coefficients + 101-point OU/power quantile SKETCHES) is persisted in a
single-row `domain_score_fit` table — a computed artifact, the corpus analog of
dishes.last_ou_fit (NOT a system_config setting). Both the LIVE harvest and the
scheduled batch percentile through the SAME stored sketches, so a freshly
harvested publisher is immediately comparable on the same scale the batch uses.
Recompute in batch on a schedule (the corpus drifts slowly — weekly is fine).
"""
from __future__ import annotations

import json
import sqlite3
from input.pipeline.db import connect as _connect  # WAL busy_timeout — input/pipeline/db.py
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from input.pipeline.config import POWER_BLEND_WEIGHT
from input.pipeline.url_utils import normalize_url

_DEFAULT_DB = "recipes.db"
_MIN_FIT_N = 25          # below this the global regression overfits noise (matches dishes)
_N_QUANTILES = 101       # 0,1,…,100th percentile breakpoints in the stored sketch


# --------------------------------------------------------------------------- #
# Store — single-row computed artifact (NOT system_config; this is derived state)
# --------------------------------------------------------------------------- #
def ensure_fit_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domain_score_fit (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            fit         TEXT NOT NULL,   -- JSON: coefficients + quantile sketches + meta
            computed_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def get_global_fit(conn: Optional[sqlite3.Connection] = None,
                   db_path: str = _DEFAULT_DB) -> Optional[dict]:
    """The stored global fit dict, or None if never computed. Pass a `conn`,
    or omit it to open `db_path` (lets the connection-free harvest read it)."""
    own = conn is None
    if own:
        conn = _connect(db_path)
    try:
        ensure_fit_table(conn)
        row = conn.execute("SELECT fit FROM domain_score_fit WHERE id = 1").fetchone()
        if not row or not row[0]:
            return None
        return json.loads(row[0])
    except Exception:
        return None
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def _store_fit(conn: sqlite3.Connection, fit: dict) -> None:
    ensure_fit_table(conn)
    conn.execute(
        "INSERT INTO domain_score_fit (id, fit, computed_at) VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET fit = excluded.fit, computed_at = excluded.computed_at",
        (json.dumps(fit), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Paywall PA-remap (Python mirror of score_data_points_for_dish's SQL eff_pa)
# --------------------------------------------------------------------------- #
def _host_of(url: str) -> str:
    """The url's host without www (the key get_paywall_calibrations uses)."""
    from urllib.parse import urlparse
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        h = ""
    return h[4:] if h.startswith("www.") else h


def _cal_index(calibrations: list) -> dict:
    """{host: cal} for O(1) lookup; only cals with a positive pa_std are usable."""
    out = {}
    for c in calibrations or []:
        if float(c.get("pa_std") or 0) > 0:
            out[c["domain"]] = c
    return out


def _adjusted_pa(url: str, pa: Optional[float], cal_by_host: dict) -> Optional[float]:
    """Paywall remap: adjusted_PA = max(pa, free_mean + (pa − paid_mean)·(free_std/paid_std)).
    ONE-DIRECTIONAL (a paywall can only suppress links → only ever LIFT); a publisher
    already above the free line keeps its real PA. No calibration → pa unchanged."""
    if pa is None:
        return None
    c = cal_by_host.get(_host_of(url))
    if not c:
        return float(pa)
    ps = float(c["pa_std"])
    if ps <= 0:
        return float(pa)
    remap = float(c["free_mean"]) + (float(pa) - float(c["pa_mean"])) * (float(c["free_std"]) / ps)
    return max(float(pa), remap)


# --------------------------------------------------------------------------- #
# Percentile via the stored quantile sketch — ONE path for live + batch
# --------------------------------------------------------------------------- #
def _build_quantiles(values: list[float]) -> list[float]:
    """101 evenly-spaced quantile breakpoints (p0..p100) of `values`. A compact,
    persistable summary of the whole-system distribution — percentile lookups
    interpolate against it, so live and batch scoring agree to within sketch
    resolution without storing every raw point."""
    arr = np.asarray([v for v in values if v is not None], dtype=float)
    if arr.size == 0:
        return []
    probs = np.linspace(0.0, 1.0, _N_QUANTILES)
    return [float(x) for x in np.quantile(arr, probs)]


def _percentile(v: Optional[float], quantiles: list[float]) -> float:
    """Map `v` to its percentile (0..100) against the stored quantile sketch.
    None or an empty sketch → 0.0 (a missing signal can't lift a page)."""
    if v is None or not quantiles:
        return 0.0
    n = len(quantiles)
    probs = np.linspace(0.0, 100.0, n)
    # np.interp clamps below q[0]→0 and above q[-1]→100; quantiles are non-decreasing.
    return float(np.interp(float(v), quantiles, probs))


# --------------------------------------------------------------------------- #
# The fit population — union of every scored (url, da, pa) in the corpus
# --------------------------------------------------------------------------- #
def _population(conn: sqlite3.Connection) -> list[tuple[str, float, float]]:
    """Every scored (url, da, pa) point: the dish ledger's cohorts UNION the
    publisher members, deduped by normalized url (a recipe that's both a dish
    winner and a publisher member counts once). This is BOTH the regression
    population and the percentile reference — so a publisher recipe is ranked
    against every recipe in the system ("best anywhere")."""
    rows = conn.execute(
        """
        SELECT url AS u, da, pa FROM dish_run_data_points
        WHERE da IS NOT NULL AND pa IS NOT NULL
        UNION ALL
        SELECT url_normalized AS u, da, pa FROM collection_members
        WHERE da IS NOT NULL AND pa IS NOT NULL
        """
    ).fetchall()
    seen: dict[str, tuple[str, float, float]] = {}
    for u, da, pa in rows:
        if not isinstance(da, (int, float)) or not isinstance(pa, (int, float)):
            continue
        key = normalize_url(u) or u
        seen.setdefault(key, (key, float(da), float(pa)))   # first wins; dedup by url
    return list(seen.values())


def compute_global_fit(conn: sqlite3.Connection,
                       weight: float = POWER_BLEND_WEIGHT) -> dict:
    """Fit the single system-wide quadratic, build the OU/power quantile sketches
    over the whole (paywall-adjusted) population, persist, and return a summary.

    The quadratic is fit on RAW PA~DA (same as the dish fit); the remap is applied
    only when computing OU/power — so the curve reflects the organic link landscape
    and the remap just relocates gated pages onto it. Returns the stored fit dict
    (`used` tells the caller whether enough data existed)."""
    from input.pipeline import chapters, domains_lib

    pop = _population(conn)
    n = len(pop)
    now_iso = datetime.now(timezone.utc).isoformat()

    if n < _MIN_FIT_N:
        fit = {"used": False, "n": n, "reason": "below_min_n",
               "weight": float(weight), "computed_at": now_iso}
        _store_fit(conn, fit)
        return fit

    da_arr = np.asarray([p[1] for p in pop], dtype=float)
    pa_arr = np.asarray([p[2] for p in pop], dtype=float)

    # Reuse the canonical quadratic fit (chapters._fit_da_pa) — same formula shape
    # as every other OU fit in the system (pinned quadratic, no model-flip jitter).
    base = chapters._fit_da_pa(da_arr, pa_arr)
    a0, a1, a2 = (float(x) for x in base["coefficients"])

    # OU/power over the ADJUSTED-PA population → the reference distribution the
    # percentile sketches summarize. Build cal index once.
    cal_by_host = _cal_index(domains_lib.get_paywall_calibrations(conn))
    ou_vals, pw_vals = [], []
    for url, da, pa in pop:
        apa = _adjusted_pa(url, pa, cal_by_host)
        pred = a0 * da * da + a1 * da + a2
        ou_vals.append(apa - pred)
        pw_vals.append(da + apa)

    fit = {
        "used": True,
        "n": n,
        "model": base.get("model", "quadratic"),
        "coefficients": [a0, a1, a2],
        "r2_chosen": base.get("r2_chosen"),
        "sigma_observed": base.get("sigma_observed"),
        "sigma_effective": base.get("sigma_effective"),
        "weight": float(weight),
        "ou_quantiles": _build_quantiles(ou_vals),
        "power_quantiles": _build_quantiles(pw_vals),
        "n_paywall_calibrated": len(cal_by_host),
        "computed_at": now_iso,
    }
    _store_fit(conn, fit)
    return fit


# --------------------------------------------------------------------------- #
# Scoring — stamp rank_score / adjusted_pa onto member dicts
# --------------------------------------------------------------------------- #
def score_members(members: list[dict], *,
                  conn: Optional[sqlite3.Connection] = None,
                  fit: Optional[dict] = None,
                  db_path: str = _DEFAULT_DB) -> list[dict]:
    """Stamp each member dict (needs `url`, `da`, `pa`) with `adjusted_pa`,
    `ou`, `power`, `ou_pct`, `power_pct`, and `rank_score` (the system-wide
    authority score), using the stored global fit. The list is mutated in place
    and returned (unsorted — the caller slices top-N).

    Fallback (so the harvest never hard-depends on a prior batch): if there's no
    usable global fit yet, `rank_score` falls back to raw PA — byte-identical to
    the old `rank_score = pa` behavior. Pass `conn` to reuse one, or omit and a
    short-lived `db_path` connection reads the fit + calibrations."""
    if not members:
        return members

    own = conn is None
    if own:
        conn = _connect(db_path)
    try:
        if fit is None:
            fit = get_global_fit(conn)
        from input.pipeline import domains_lib
        cal_by_host = _cal_index(domains_lib.get_paywall_calibrations(conn))
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass

    usable = bool(fit and fit.get("used"))
    if usable:
        a0, a1, a2 = (float(x) for x in fit["coefficients"])
        ouq = fit.get("ou_quantiles") or []
        pwq = fit.get("power_quantiles") or []
        w = float(fit.get("weight", POWER_BLEND_WEIGHT)) / 100.0

    for m in members:
        pa = m.get("pa")
        da = m.get("da")
        apa = _adjusted_pa(m.get("url") or "", pa, cal_by_host)
        m["adjusted_pa"] = round(apa, 4) if apa is not None else None
        if not usable:
            # BOOTSTRAP — no global fit computed yet → raw-PA fallback for EVERY member
            # across every publisher. The scale is uniform (all raw PA), so within- and
            # cross-publisher ordering stay consistent; this is the documented old
            # behavior until the first batch run computes a fit.
            m["ou"] = None
            m["power"] = (float(da) + apa) if (da is not None and apa is not None) else None
            m["ou_pct"] = None
            m["power_pct"] = None
            m["rank_score"] = float(pa) if pa is not None else None
            continue
        if apa is None or da is None:
            # Fit EXISTS but this member has no authority signal (no PA, or no DA so OU is
            # uncomputable) → it can't be placed on the system scale. NULL it (sorts last,
            # excluded from the leaderboard). Crucially NOT raw PA: a 0–100 PA value mixed
            # into 0–1 system scores would outrank every properly-scored recipe on the
            # cross-publisher leaderboard. A missing signal can't lift a page, only fail to.
            m["ou"] = None
            m["power"] = None
            m["ou_pct"] = None
            m["power_pct"] = None
            m["rank_score"] = None
            continue
        ou = apa - (a0 * da * da + a1 * da + a2)
        power = float(da) + apa
        ou_pct = _percentile(ou, ouq)
        pw_pct = _percentile(power, pwq)
        m["ou"] = round(ou, 4)
        m["power"] = round(power, 4)
        m["ou_pct"] = round(ou_pct, 4)
        m["power_pct"] = round(pw_pct, 4)
        m["rank_score"] = round(((1.0 - w) * ou_pct + w * pw_pct) / 100.0, 6)
    return members


def rescore_all_members(conn: sqlite3.Connection,
                        fit: Optional[dict] = None) -> dict:
    """Batch: recompute adjusted_pa + rank_score for EVERY publisher member against
    the (freshly computed or passed) global fit, and re-rank within each publisher
    by the new score. `selected` is left untouched — within a publisher the score is
    monotonic in PA, so the winner set never changes; only the comparable rank_score
    value (and rank order, refreshed for consistency) does. Returns a summary."""
    if fit is None:
        fit = get_global_fit(conn)
    rows = conn.execute(
        "SELECT collection_type, collection_key, url_normalized, da, pa "
        "FROM collection_members"
    ).fetchall()
    # Group by collection so we can re-rank within each.
    by_coll: dict[tuple, list[dict]] = {}
    for ct, ck, url, da, pa in rows:
        by_coll.setdefault((ct, ck), []).append(
            {"url": url, "da": da, "pa": pa})

    updates = []
    n_collections = 0
    for (ct, ck), members in by_coll.items():
        n_collections += 1
        score_members(members, conn=conn, fit=fit)
        # Re-rank within the collection by the new score (NULLs last). Stable on ties.
        members.sort(key=lambda m: (m.get("rank_score") is None,
                                    -(m.get("rank_score") or 0.0)))
        for i, m in enumerate(members, 1):
            updates.append((m.get("adjusted_pa"), m.get("rank_score"), i, ct, ck, m["url"]))

    conn.executemany(
        "UPDATE collection_members SET adjusted_pa = ?, rank_score = ?, rank = ? "
        "WHERE collection_type = ? AND collection_key = ? AND url_normalized = ?",
        updates,
    )
    conn.commit()
    return {"fit_used": bool(fit and fit.get("used")),
            "fit_n": (fit or {}).get("n"),
            "collections": n_collections,
            "members_rescored": len(updates)}


def recompute_and_rescore(conn: sqlite3.Connection,
                          weight: float = POWER_BLEND_WEIGHT) -> dict:
    """The scheduled batch entry point: refit the global curve over the whole
    corpus, then rescore every publisher member against it. Returns a merged
    summary for the job log."""
    fit = compute_global_fit(conn, weight=weight)
    summary = rescore_all_members(conn, fit=fit)
    summary["fit"] = {k: fit.get(k) for k in
                      ("used", "n", "model", "coefficients", "r2_chosen",
                       "n_paywall_calibrated", "weight")}
    return summary
