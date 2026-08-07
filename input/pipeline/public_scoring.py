"""public_scoring — the ONE place a private score becomes a public one.

Everything the ranking engine actually runs on (OU, power, DA, PA, the blend,
the percentile) is ours and stays ours. This module is the single chokepoint
that turns those into the two things a user is ever shown: a star fill and,
occasionally, a badge. Nothing else crosses.

Why a chokepoint and not a formatter at each call site
------------------------------------------------------
Coarsening only protects you if it happens BEFORE the wire. A page that
receives ``{"ou": 12.4, "power": 131}`` and draws stars in JavaScript has
published the algorithm to anyone who opens devtools — the pixels are not the
leak, the payload is. So a public endpoint calls ``public_score()`` and serves
its output verbatim; it never serves the inputs. Same rule for the accessible
name: "3½ out of 5", never the underlying number.

The star scale starts at 3
--------------------------
Every recipe in the index already survived selection — the index IS the filter
(see memory/project_two_stage_selection: harvest selects, OU ranks WITHIN the
selected pool). Forcing a percentile band onto that pool would hand 20% of a
curated set a single star, which insults a recipe we chose ourselves and which
no one would ever save. So the five levels map onto 3 .. 5 in half steps, and
the fill percentages land on the star geometry in components.css:

    3.0 -> 60    3.5 -> 70    4.0 -> 80    4.5 -> 90    5.0 -> 100

Stars are frozen when a card is stored
--------------------------------------
A stored card keeps the rating the user read. Measured 2026-08-07 on 4,525
master rows by replaying corpus growth: in the steady state 2-4% of rows shift
one band as the corpus grows (never two), and freezing the THRESHOLDS does not
help — ``blend`` is itself built from in-cohort percentiles, so the cohort
dependency sits a level below any cut point. The fix is not more math, it is
provenance: store ``public_score()``'s output together with its inputs and a
``scored_at``, and let a refresh job re-score and surface "our rating changed"
as an event rather than silently rewriting a number someone already saw.
(Same shape as the screenshot refresh cycle, and the same lesson as the
orphaned grades: a derived artifact must carry its inputs or it outlives them.)
"""

from __future__ import annotations

from typing import Optional

try:  # keep this module importable standalone (scripts, tests, the split API)
    from .config import POWER_BLEND_WEIGHT
except Exception:  # pragma: no cover - config is optional here
    POWER_BLEND_WEIGHT = 30.0

# Cut points between the five display levels, applied to the BLEND VALUE
# (0..1), not to a rank position within the corpus.
#
# That distinction is the whole design. Cutting on rank would force exactly 20%
# of the index into each band, so every time the corpus grows some recipe is
# demoted purely by arithmetic — a stored card silently loses a star although
# nothing about that recipe changed. Cutting on the value makes a star an
# absolute claim ("this is a 5-star recipe"), which is what a user reads it as
# and what a saved card needs it to be.
#
# The blend is an average of two percentiles, so it is naturally centre-heavy
# rather than flat. Measured over 4,525 master rows, 2026-08-07:
#
#     3.0  536 (12%)   3.5 1222 (27%)   4.0 1075 (24%)   4.5 1131 (25%)   5.0 561 (12%)
#
# Scarce at both ends, which is what a rating should look like — a top band
# holding a fifth of everything means nothing. Move these to make 5 stars rarer
# or more generous; that is a curator call, not a measurement. Change it here,
# never at a call site.
STAR_CUTS = (0.20, 0.40, 0.60, 0.80)

# Fill percentage per level. These are not arbitrary: the glyph in
# components.css is tiled at 20% with the star centred inside its tile, so
# these values land exactly on star centres (half) and inter-star gaps (whole).
STAR_FILLS = (60, 70, 80, 90, 100)

# A badge fires only where the two axes genuinely disagree. Measured on the
# same 4,525 rows: OU and power correlate at only +0.235, so the second axis
# carries real information -- but it is spent on a LABEL, never on a second
# star scale. Two continuous scales side by side is an invitation to work out
# the relationship between them. Populations at these thresholds: hidden gem
# 3.6%, trusted 6.8% — ~90% of recipes get no badge, which is the point. A
# badge on everything is decoration.
BADGE_CUTS = {"strong": 0.80, "weak": 0.40}


def star_level(blend: float) -> float:
    """Blend value (0..1) -> a display rating in {3, 3.5, 4, 4.5, 5}."""
    p = min(max(float(blend), 0.0), 1.0)
    idx = 0
    for cut in STAR_CUTS:
        if p >= cut:
            idx += 1
    return 3.0 + 0.5 * idx


def star_fill(blend: float) -> int:
    """Blend value (0..1) -> the CSS fill percentage, one of STAR_FILLS.

    This is the only number that goes over the wire. It is quantised here, on
    the server, precisely so that reading it off the DOM recovers a band and
    not a score.
    """
    return STAR_FILLS[int(round((star_level(blend) - 3.0) * 2))]


def star_label(blend: float) -> str:
    """Accessible name — coarse for the same reason the fill is."""
    lvl = star_level(blend)
    txt = str(int(lvl)) if lvl == int(lvl) else f"{int(lvl)}½"
    return f"{txt} out of 5"


def badge_for(ou_pct: Optional[float], power_pct: Optional[float]) -> Optional[str]:
    """The second axis, as a label. None for ~90% of recipes.

    ``hidden_gem``  strong OU, ordinary reach — a smaller kitchen that beat the
                    room. This is the one the whole ranking exists to find.
    ``trusted``     strong reach, ordinary OU — the established name doing the
                    dish the standard way. Useful, just not a discovery.
    """
    if ou_pct is None or power_pct is None:
        return None
    strong, weak = BADGE_CUTS["strong"], BADGE_CUTS["weak"]
    if ou_pct >= strong and power_pct <= weak:
        return "hidden_gem"
    if power_pct >= strong and ou_pct <= weak:
        return "trusted"
    return None


def blend_value(ou_pct: float, power_pct: float,
                weight: float = POWER_BLEND_WEIGHT) -> float:
    """The ranking blend, 0..1, from the two INPUT percentiles.

    Mirrors score_data_points_for_dish's ``rank_score`` — power's share is
    ``weight`` out of 100, OU takes the remainder. Kept here so a caller that
    already holds the two percentiles does not have to reimplement it (and
    get the weight wrong).
    """
    return ((100.0 - weight) * ou_pct + weight * power_pct) / 100.0


def public_score(ou_pct: Optional[float], power_pct: Optional[float],
                 weight: float = POWER_BLEND_WEIGHT) -> dict:
    """The complete public payload for one recipe. Serve this verbatim.

    Returns ``{}`` when the recipe is unscored — ABSENT, not a zero or a
    default 3 stars (memory/feedback_absent_not_zero). An unrated recipe must
    render as no stars at all, because a manufactured floor is exactly how the
    scoring zeros got believed for a month.
    """
    if ou_pct is None or power_pct is None:
        return {}
    b = blend_value(ou_pct, power_pct, weight)
    return {
        "stars": star_level(b),
        "starFill": star_fill(b),
        "starLabel": star_label(b),
        "badge": badge_for(ou_pct, power_pct),
    }


BADGE_TEXT = {"hidden_gem": "Hidden gem", "trusted": "Widely trusted"}
