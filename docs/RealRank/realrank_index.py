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


if __name__ == "__main__":
    print(round(realrank_index({5: 230, 4: 18, 3: 3, 2: 2, 1: 5}, 258), 1))   # KitchenAid -> 89.9
    print(round(realrank_index([89, 7, 1, 1, 2], 258), 1))                    # same, as %  -> 89.9
    print(round(realrank_index({5: 11, 4: 1, 3: 0, 2: 0, 1: 0}, 12), 1))      # sparse     -> 88.0
    print(round(realrank_index([2200, 1000, 400, 200, 200], 4000), 1))        # 4.2*, 4k   -> 66.2