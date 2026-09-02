"""BestReviews review decoder — covers bestreviews.com AND its newspaper white-labels
(reviews.chicagotribune.com and the other Nexstar/Tribune paper verticals, which serve the
identical BestReviews template under the paper's banner).

Added 2026-09-01 off the Tribune pressure-canners page. The shape, and what it means for us:
  * a TOP-PICKS strip up front: each pick headed by a tier banner ("BEST OF THE BEST",
    "BEST BANG FOR THE BUCK") or a specialty role ("Best Electric Pressure Canner"),
    brand on its own line above the product name, a role tagline ("Best for Experts"),
    a blurb, and Check Price / Shop Now buttons;
  * an EDITORS' PICKS body section repeating the winners at full depth — full product
    name, price ("$349.95 at Amazon"), a pipe-separated specs line (Dimensions | Weight |
    Liquid Capacity | Mason Jar Capacity | Material), and several verdict paragraphs;
  * the OUR TOP PICKS summary carousel repeats every pick VERBATIM up to three times
    at the page tail (a dedupe hazard);
  * a SHOP SIMILAR PRODUCTS cross-sell block of UNRELATED products (a contamination
    hazard);
  * a long buying guide ("Buying guide for ...", What to consider, How we tested/
    analyzed, FAQ) with named staff writers.

Like the other source modules this contributes a PROMPT, not a regex parser: the
per-source regex parsers proved too brittle on real pages (see review_sources/__init__).
"""
from __future__ import annotations

KEY = "bestreviews"
LABEL = "BestReviews (+ Tribune papers)"
IMPLEMENTED = True

EXTRACT_HINTS = """SOURCE: BestReviews roundup (also served white-labeled on newspaper domains like
reviews.chicagotribune.com — the reviewer is still "BestReviews").
- TIERS: each pick carries a label — use it as the tier, title-cased:
    "BEST OF THE BEST" / "EDITORS' FAVORITE"      -> tier "Best of the Best"
    "BEST BANG FOR THE BUCK" / "GREAT VALUE"      -> tier "Best Bang for the Buck"
    a specialty label ("Best Electric Pressure Canner", "Best for Induction Stoves",
    a bare "TOP PICK")                            -> use the specialty label as the tier
      ("Top Pick" only when no more specific label exists for that product).
- DEDUPE HARD. The same picks appear up to THREE times: the strip at the top, the
  "EDITORS' PICKS" body write-ups, and an "OUR TOP PICKS" carousel repeated at the tail.
  The strip uses a SHORT name ("Canner Pressure Cooker") under a separate brand line;
  the body uses the full one ("All American 15.5-Quart Pressure Cooker/Canner"). ONE
  entry per product, preferring the fuller body name, with the body's verdict prose.
- EXCLUDE the "SHOP SIMILAR PRODUCTS" block entirely — it cross-sells UNRELATED
  products (vegetable wash, dishwasher baskets) that are NOT part of this review.
  Likewise skip "Related Reviews" / "Related Articles" / "You Might Also Like".
- SPECS: the body write-ups carry a pipe-separated specs line (Dimensions | Weight |
  Liquid Capacity | Mason Jar Capacity | Material) — parse it into specs fields.
- PRICES read like "$349.95 at Amazon" — capture price and retailer. Buy buttons are
  "Check Price" / "Shop Now" redirectors; capture a real ASIN only when one is visible.
- EDITORIAL HEADER: the "Buying guide for ..." essay, "What to consider", types/
  materials prose, "How we tested"/"How we analyzed", and the FAQ go into
  product_class.buying_guide. rating_scale: none — BestReviews publishes no scores."""


def matches(url: str, head: str = "") -> bool:
    u = (url or "").lower()
    h = (head or "").lower()
    return ("bestreviews.com" in u or "bestreviews.com" in h
            or "reviews.chicagotribune.com" in u
            or "bestreviews is reader-supported" in h)


def parse(md: str, *, url: str = "", captured_at: str = "") -> dict:
    """No deterministic parser — the LLM path (extract_review) uses EXTRACT_HINTS above."""
    raise NotImplementedError("BestReviews uses the LLM extractor with per-source hints.")
