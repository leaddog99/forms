"""
RealRank index — turn a star-rating distribution into a single 0-100 score.

Method (NPS-from-stars, confidence-adjusted for sample size):
  * promoters = share of 5-star   (optionally + weight * 4-star)
  * detractors = share of 3+2+1-star,   4-star passive by default
  * NPS = promoters - detractors                       (range -1 .. +1)
  * subtract a confidence penalty using the review count, so a 5.0 from
    9 reviews can't outrank a 4.8 from 4,000 (Wilson/NPS lower bound)
  * rescale the -1..+1 lower bound to a friendly 0-100 index

Dependencies: standard library only.
"""

import math


def realrank_index(distribution, n_reviews, z=1.96, four_star_weight=0.0):
    """Return a 0-100 RealRank score for a product.

    Parameters
    ----------
    distribution : the 5..1 star split. Accepts either
        - a dict {5: .., 4: .., 3: .., 2: .., 1: ..}, or
        - a sequence [five, four, three, two, one].
        Values may be raw counts, percentages (0-100), or fractions (0-1);
        they are normalized internally, so only their *ratios* matter.
    n_reviews : int
        Total number of reviews. Drives the small-sample confidence penalty.
    z : float
        Confidence z-score. 1.96 = 95% (default). Larger z = harsher penalty
        on thinly-reviewed products.
    four_star_weight : float
        0.0 -> 4-star counts as passive (classic NPS, default).
        0.8 -> 4-star counts as a partial promoter.

    Returns
    -------
    float : score in [0, 100]. Returns 0.0 if there are no reviews.
    """
    # --- unpack the distribution into five..one ---
    if isinstance(distribution, dict):
        five, four, three, two, one = (
            float(distribution.get(5, 0)),
            float(distribution.get(4, 0)),
            float(distribution.get(3, 0)),
            float(distribution.get(2, 0)),
            float(distribution.get(1, 0)),
        )
    else:
        five, four, three, two, one = (float(x) for x in distribution)

    total = five + four + three + two + one
    if total <= 0 or n_reviews <= 0:
        return 0.0

    # --- normalize to fractions ---
    p5, p4, p3, p2, p1 = (five / total, four / total,
                          three / total, two / total, one / total)

    # --- NPS-from-stars ---
    promoters = p5 + four_star_weight * p4
    detractors = p3 + p2 + p1
    nps = promoters - detractors                      # -1 .. +1

    # --- confidence penalty (multinomial SE of a difference of shares) ---
    variance = (promoters + detractors - nps * nps) / n_reviews
    se = math.sqrt(variance) if variance > 0 else 0.0
    nps_lower = nps - z * se                          # -1 .. +1

    # --- rescale to a 0-100 index ---
    index = 100.0 * (nps_lower + 1.0) / 2.0
    return max(0.0, min(100.0, index))


def _as_five(distribution):
    """Normalize either accepted shape into a [five, four, three, two, one] list."""
    if isinstance(distribution, dict):
        return [float(distribution.get(k, 0) or 0) for k in (5, 4, 3, 2, 1)]
    return [float(x or 0) for x in distribution]


def pool_histograms(sources):
    """Combine star histograms from SEVERAL retailers into one distribution.

    `sources` = [{"source": "amazon", "histogram": [5,4,3,2,1 counts], "total": n}, ...].
    Returns {"histogram": [...], "total": n, "sources": [{source, total, share}, ...]}.

    Why pool the DISTRIBUTIONS rather than average the scores: the retailers use the same
    5-point scale on the same product, so their ratings are two samples of one population.
    Averaging two scores would weigh 3,642 Best Buy reviews equally against 145,000 Amazon
    ones; summing the counts weighs them by evidence, and the confidence penalty in
    realrank_index then reflects the true combined n.

    Only pool listings that are genuinely the SAME product — a different size or generation
    is a different thing, and merging them launders that away. Per-source totals are
    returned so the split stays visible instead of disappearing into one number.
    """
    hist = [0.0] * 5
    used = []
    for s in sources or []:
        h = s.get("histogram")
        if not h or len(h) != 5:
            continue
        counts = _as_five(h)
        if sum(counts) <= 0:
            continue
        for i, c in enumerate(counts):
            hist[i] += c
        used.append({"source": s.get("source", "?"),
                     "total": s.get("total") or int(sum(counts))})
    if not used:
        return {"histogram": [], "total": 0, "sources": []}
    total = sum(u["total"] for u in used)
    for u in used:
        u["share"] = round(100.0 * u["total"] / total, 1) if total else 0.0
    return {"histogram": [int(round(x)) for x in hist], "total": total, "sources": used}


def polarization(distribution):
    """Is the rating curve J-SHAPED (a barbell) rather than a clean taper?

    An average hides the shape: 4.6 stars can be a gentle slope or "most people love it,
    a hard core hate it, nobody is mildly disappointed". The tell is **1-star outnumbering
    2-star** — real on the Lodge skillet (3% vs 1%), and it usually means a learning curve
    or quality-control variance rather than a mediocre product.

    Returns {j_shaped, one_star_pct, hard_core_pct, detractor_pct, label}. `label` is
    'polarizing' | 'clean' | 'weak' | None — the renderer decides the wording.
    """
    five, four, three, two, one = _as_five(distribution)
    total = five + four + three + two + one
    if total <= 0:
        return {"j_shaped": False, "one_star_pct": None, "hard_core_pct": None,
                "detractor_pct": None, "label": None}
    p1, p2, p3, p5 = one / total, two / total, three / total, five / total
    detr = p1 + p2 + p3
    j = p1 > p2                       # the barbell tell
    if j and p1 >= 0.02 and p5 >= 0.5:
        label = "polarizing"          # loved by most, with a real hard core against
    elif detr <= 0.05:
        label = "clean"               # almost no detractors, no barbell
    elif detr >= 0.25:
        label = "weak"                # a quarter of buyers unhappy — not a shape story
    else:
        label = None
    return {"j_shaped": j,
            "one_star_pct": round(100 * p1, 1),
            "hard_core_pct": round(100 * p1, 1),
            "detractor_pct": round(100 * detr, 1),
            "label": label}


if __name__ == "__main__":
    print(round(realrank_index({5: 230, 4: 18, 3: 3, 2: 2, 1: 5}, 258), 1))   # KitchenAid -> 89.9
    print(round(realrank_index([89, 7, 1, 1, 2], 258), 1))                    # same, as %  -> 89.9
    print(round(realrank_index({5: 11, 4: 1, 3: 0, 2: 0, 1: 0}, 12), 1))      # sparse     -> 88.0
    print(round(realrank_index([2200, 1000, 400, 200, 200], 4000), 1))        # 4.2*, 4k   -> 66.2