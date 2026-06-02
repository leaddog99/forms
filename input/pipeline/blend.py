"""Canonical OU/power percentile-blend ranking.

One home for the (configurable) blend so the batch *selector*
(build_query_batch._rank_blended) and the *display* rankings (chapter
Top-10, dish top-recipes) order recipes identically — see
memory/feedback_single_path.md. Two recipes ranked in two places must
never disagree because the math drifted between copies.

OU rewards exceptionalism (a page punching above its domain weight);
power (DA+PA) rewards raw authority. Each is mapped to an in-cohort
percentile rank (0..1, outlier-robust) and blended:

    blend = (1 - w) * ou_pct + w * power_pct,   w = POWER_BLEND_WEIGHT / 100
"""
from __future__ import annotations

from typing import Optional

from input.pipeline.config import POWER_BLEND_WEIGHT


def percentile_ranks(values: list[Optional[float]]) -> list[float]:
    """Map each value to its percentile rank in [0,1] within the list
    (0 = lowest, 1 = highest), averaging ties. None ranks below every
    real value and gets 0.0 — a missing signal can't lift a page, only
    fail to. Robust to outliers by construction: one extreme value can't
    compress the rest, the reason we rank rather than min-max scale
    (user call 2026-06-01)."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0 if values[0] is not None else 0.0]
    neg = float("-inf")
    keyed = [v if v is not None else neg for v in values]
    order = sorted(range(n), key=lambda i: keyed[i])
    pct = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and keyed[order[j + 1]] == keyed[order[i]]:
            j += 1
        p = ((i + j) / 2.0) / (n - 1)        # avg 0-indexed rank of tie group -> [0,1]
        for k in range(i, j + 1):
            idx = order[k]
            pct[idx] = 0.0 if values[idx] is None else p
        i = j + 1
    return pct


def _power(row: dict, da_key: str, pa_key: str) -> Optional[float]:
    da, pa = row.get(da_key), row.get(pa_key)
    if isinstance(da, (int, float)) and isinstance(pa, (int, float)):
        return da + pa
    return None


def rank_by_blend(
    rows: list[dict],
    *,
    ou_key: str = "ou",
    da_key: str = "da",
    pa_key: str = "pa",
    weight: Optional[float] = None,
) -> list[dict]:
    """Return `rows` sorted descending by the OU/power percentile blend,
    stamping each with `power` (DA+PA), `ou_pct`, `power_pct`, and
    `blend_score`. Pure ordering — the caller slices top-N and assigns
    1-indexed ranks. `weight` overrides POWER_BLEND_WEIGHT (out of 100)."""
    if not rows:
        return []
    w_pow = (POWER_BLEND_WEIGHT if weight is None else weight) / 100.0
    w_ou = 1.0 - w_pow
    powers = [_power(r, da_key, pa_key) for r in rows]
    ou_pct = percentile_ranks([r.get(ou_key) for r in rows])
    pw_pct = percentile_ranks(powers)
    for r, o, p, pw in zip(rows, ou_pct, pw_pct, powers):
        r["power"] = pw
        r["ou_pct"] = round(o, 4)
        r["power_pct"] = round(p, 4)
        r["blend_score"] = round(w_ou * o + w_pow * p, 6)
    return sorted(rows, key=lambda r: r["blend_score"], reverse=True)
