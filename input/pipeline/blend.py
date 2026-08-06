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
    """Map each value to its percentile rank in [0,1] AMONG THE MEASURED VALUES
    (0 = lowest, 1 = highest), averaging ties. None gets 0.0 — a missing signal
    can't lift a page, only fail to. Robust to outliers by construction: one
    extreme value can't compress the rest, the reason we rank rather than
    min-max scale (user call 2026-06-01).

    THE UNMEASURED ARE EXCLUDED FROM THE DENOMINATOR (2026-08-06, curator: "the
    'empty' stats should not be included in any aggregate analysis"). They used
    to be keyed to -inf and ranked alongside real values, which left them at the
    bottom — correct — but kept them in `n`, so they occupied rank slots and
    compressed everyone else into the top of the range. With 5 of 10 values
    missing, the measured pages spanned 0.56-1.0 instead of 0-1, and a
    "70th percentile" page was 70th among candidates INCLUDING ones we could not
    measure, which is not a statistic about anything. Ordering was unaffected;
    the blend score, which consumes the percentile as a magnitude, was not.

    Now that `_scoring` fields default to None rather than 0.0 (see
    ScoringMetadata), absence is common enough that this matters routinely
    rather than at the margins.

    Note a measured WORST value also lands on 0.0, so it is not distinguishable
    from unmeasured in the output — true before this change too, and acceptable
    because both mean "gets no lift from this dimension"."""
    n = len(values)
    if n == 0:
        return []
    pct = [0.0] * n
    real = [i for i, v in enumerate(values) if v is not None]
    m = len(real)
    if m == 0:
        return pct                       # nothing measured — nobody gets lift
    if m == 1:
        pct[real[0]] = 1.0               # the only measured value tops its cohort
        return pct
    order = sorted(real, key=lambda i: values[i])
    i = 0
    while i < m:
        j = i
        while j + 1 < m and values[order[j + 1]] == values[order[i]]:
            j += 1
        p = ((i + j) / 2.0) / (m - 1)    # avg 0-indexed rank of tie group -> [0,1]
        for k in range(i, j + 1):
            pct[order[k]] = p
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
