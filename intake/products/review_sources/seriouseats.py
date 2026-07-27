"""Serious Eats equipment reviews — per-source extraction hints.

Added 2026-07-27 after a real failure: with no module here, Serious Eats fell through to the
GENERIC prompt, which tells the extractor to "read the page's OWN wording to find each
product's tier label". SE heads every pick with an award phrase rather than a tier, so the
model faithfully produced nine unique one-off "tiers" — "The Best Dutch Oven (Ever!)", "The
Best Budget Dutch Oven Under $80", "The Best Self-Basting Dutch Oven" — which are unusable
for filtering or ranking. It also captured Caraway twice, once from the summary block at the
top and once from the tested list.

SE's shape, and what it means for us:
  * a summary/at-a-glance block near the top repeating the winners (a DEDUPE hazard);
  * per-pick sections headed by an award phrase, sometimes with "Also Good" runners-up;
  * a "What We Didn't Like"/"The Competition" tail listing everything that lost;
  * "We tested N, here are the M best" framing — so most tested products are NOT picks.

Like the other source modules this contributes a PROMPT, not a regex parser: the per-source
regex parsers proved too brittle on real pages (see review_sources/__init__).
"""
from __future__ import annotations

KEY = "seriouseats"
LABEL = "Serious Eats"
IMPLEMENTED = True

EXTRACT_HINTS = """SOURCE: Serious Eats equipment review. Reviewer = "Serious Eats".
- SE uses AWARD PHRASES as its section headings, not tiers. Map them onto the controlled
  vocabulary and keep the phrase at the front of `verdict.summary`:
    "The Best <thing> (Ever!)" / the single overall winner            -> tier "Winner"
    "The Best <qualified> ..." (Budget, Lightweight, Ceramic Coating,
      Self-Basting, Modern Design, Under $80, Round Staub, ...)       -> tier "Highly Recommended"
    "Also Good" / runner-up / honorable mention                        -> tier "Recommended"
    "The Competition" / "What We Didn't Like" / didn't-make-the-cut    -> tier "Not Recommended"
  NEVER emit an award phrase as the tier itself.
- DEDUPE the summary block. SE repeats its winners in an at-a-glance list near the top and
  again in the body; the top block often uses a SHORT name ("Caraway Dutch Oven") while the
  body uses the full one ("Caraway 6.5 Quart Round Enameled Cast Iron Dutch Oven"). These are
  ONE product — keep a single entry, preferring the fuller name.
- The headline usually states the scope ("I Tested 26 Dutch Ovens to Find the 9 Best"):
  most tested products are NOT picks, so expect a long Not Recommended tail. Capture them all.
- EDITORIAL: the how-to-choose prose ("Which Material Should I Choose?", "What to Look For",
  "How We Tested", FAQs) goes into product_class.buying_guide.
- SE quotes prices inline and links to retailers by name ("Buy at Amazon"); specs sit in a
  per-product "The Specs" block (capacity, weight, material) when present."""


def matches(url: str, head: str = "") -> bool:
    return "seriouseats.com" in (url or "").lower() or "seriouseats.com" in (head or "").lower()


def parse(md: str, *, url: str = "", captured_at: str = "") -> dict:
    """No deterministic parser — SE's layout varies too much between reviews. The LLM path
    (extract_review) uses EXTRACT_HINTS above."""
    raise NotImplementedError("Serious Eats uses the LLM extractor with per-source hints.")
