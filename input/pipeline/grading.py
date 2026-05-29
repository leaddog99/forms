"""Exceptionalism grading — applies a stored OU-fit to a single URL.

The batch path (`intake.build_query_batch._compute_custom_ou`) fits a
PA-vs-DA regression across all URLs in one dish refresh, computes σ
across the residuals, and stamps an Exceptionalism letter grade on
every entry. That code lives there because it fits AND scores in one
pass with numpy arrays.

This module covers the *other* shape: one URL at a time, against a fit
that was computed during some earlier batch and now lives in
`dishes.last_ou_fit`. Use cases:

  - Harvest-from-reject saves where the dish is known explicitly but
    the row didn't get stamped during the batch (it was in the rejects).
  - Personal saves matched to a dish via embedding similarity (see
    input.pipeline.embeddings).
  - Backfill of legacy master rows that pre-date Exceptionalism.

The grade buckets and base constants are imported from
build_query_batch so the batch path and this path agree by construction.
Changing the buckets in one place changes it everywhere.
"""
from __future__ import annotations

from typing import Optional

# Grade buckets duplicated here to keep this module import-cheap (no
# numpy, no SerpAPI imports just to grade one URL). Must stay in lockstep
# with intake.build_query_batch._EXC_GRADE_BUCKETS — see test_grading
# parity check if/when tests land.
_EXC_GRADE_BUCKETS = [
    (97.5, "A+"),
    (92.5, "A"),
    (87.5, "A-"),
    (82.5, "B+"),
    (77.5, "B"),
    (72.5, "B-"),
    (67.5, "C+"),
    (62.5, "C"),
    (57.5, "C-"),
    (52.5, "D+"),
    (47.5, "D"),
    (42.5, "D-"),
]


def score_to_grade(score: float) -> str:
    """Letter grade for a T-score. Below the lowest bucket → 'F'."""
    for floor, letter in _EXC_GRADE_BUCKETS:
        if score >= floor:
            return letter
    return "F"


def predicted_pa(da: float, ou_fit: dict) -> Optional[float]:
    """Apply the stored fit's model + coefficients to a single DA value.
    Returns the predicted PA, or None when the fit isn't usable for this
    DA (power model on DA<=0).

    The three model shapes mirror the candidates in
    `_compute_custom_ou` (linear / quadratic / power). Coefficients are
    persisted as a list of floats in the order numpy.polyfit returns
    (highest-degree first) for linear/quadratic; for power, [a, b]
    where predicted_PA = a * DA^b.
    """
    model = (ou_fit or {}).get("model")
    coefs = (ou_fit or {}).get("coefficients") or []
    if not model or not coefs:
        return None
    try:
        da_f = float(da)
    except (TypeError, ValueError):
        return None

    if model == "linear":
        if len(coefs) < 2:
            return None
        m, b = float(coefs[0]), float(coefs[1])
        return m * da_f + b
    if model == "quadratic":
        if len(coefs) < 3:
            return None
        a, b, c = float(coefs[0]), float(coefs[1]), float(coefs[2])
        return a * da_f * da_f + b * da_f + c
    if model == "power":
        if len(coefs) < 2:
            return None
        a, b = float(coefs[0]), float(coefs[1])
        if da_f <= 0:
            return 0.0   # power model is undefined at 0; mirror fit-time behavior
        return a * (da_f ** b)
    return None


def compute_exceptionalism(da: float, pa: float, ou_fit: dict,
                            *,
                            matched_dish: Optional[str] = None,
                            match_confidence: Optional[float] = None,
                            match_method: Optional[str] = None) -> Optional[dict]:
    """Score one (DA, PA) pair against a stored OU-fit and return the
    `_master.exceptionalism`-shaped dict. None when the fit can't grade
    this point (missing σ, model, or non-finite DA/PA).

    `matched_dish` / `match_confidence` / `match_method` flow into the
    basis block when the cohort wasn't picked explicitly — embedding
    matches stamp the dish name + cosine similarity + 'embedding-match'
    so a future audit can spot mis-graded rows (wrong cohort match)
    without re-running the embedding pipeline.
    """
    sigma_eff = (ou_fit or {}).get("sigma_effective")
    if sigma_eff is None or float(sigma_eff) <= 0:
        return None
    try:
        da_f, pa_f = float(da), float(pa)
    except (TypeError, ValueError):
        return None

    predicted = predicted_pa(da_f, ou_fit)
    if predicted is None:
        return None
    residual = pa_f - predicted

    # T-score: 75 base, 10 points per σ above predicted. Mirrors the
    # batch path exactly (intake.build_query_batch lines 531-541).
    score = (residual / float(sigma_eff)) * 10.0 + 75.0
    grade = score_to_grade(score)

    basis = {
        "model": (ou_fit or {}).get("model"),
        "sigma_effective": round(float(sigma_eff), 4),
    }
    n = (ou_fit or {}).get("n")
    if n is not None:
        basis["n"] = n
    sigma_obs = (ou_fit or {}).get("sigma_observed")
    if sigma_obs is not None:
        basis["sigma_observed"] = sigma_obs
    if matched_dish:
        basis["matched_dish"] = matched_dish
    if match_confidence is not None:
        basis["match_confidence"] = round(float(match_confidence), 4)
    if match_method:
        basis["match_method"] = match_method

    return {
        "score": round(score, 2),
        "grade": grade,
        "basis": basis,
    }
