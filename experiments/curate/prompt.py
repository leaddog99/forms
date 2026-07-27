"""The curation research prompt — the curator's ChatGPT prompt, grounded.

EXPERIMENTAL. Nothing here is imported by the server or any job.

The base is the curator's own `best_dutch_ovens_full_research_prompt.txt`, which is already a
specification rather than a wish: its rules 4-9 encode the exact anti-substitution discipline
BCC arrived at independently ("never infer or manufacture an ASIN"; "verify the listing
matches the stated manufacturer, product family, capacity and form"; "reject a bundle, used
item, accessory, replacement lid, miniature pot or wrong capacity"). Those are kept verbatim
in spirit.

Four changes, each earning its place:

1. GROUNDING. The base prompt asks for "reputable independent product-testing sources" — but
   a browsing model cannot read America's Test Kitchen, Serious Eats or Wirecutter, and when
   blocked it does not stop, it quietly substitutes whatever is reachable (measured
   2026-07-25: all three missing, Forbes/Foodal/Woman & Home in their place, nothing saying
   so). We fetch those pages ourselves and hand them over, so the judgment rests on the
   sources that were asked for.

2. TYPICAL PRICE, not today's. Price carries real ranking weight, and a temporary discount
   should not move a permanent recommendation — in the curator's own run the #1 overall was
   a sale-priced model no expert source covered. Rank on `typical_price`; record
   `current_price` separately as perishable.

3. SOURCE LINKS ENFORCED. The base prompt requires them and its validator ignores them; every
   example row shipped with `source_links: []`. It is the only field that makes a claim
   auditable later, so it is now mandatory and checked.

4. TEXT, NOT A SPREADSHEET. Same JSON contract, rendered as a written brief.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
#  Categories — a staff input, never a default
# --------------------------------------------------------------------------- #

NO_CATEGORIES = """\
no categories supplied — this run needs them and must not invent them.

A category is not a label on the output, it is a THREE-PRODUCT SECTION that a
product can win by being the only candidate in it. Deciding that "Best budget"
or "Best for bread" is a slot worth filling decides what gets recommended at
all, so it is a staff call, per class, every time.

This runner used to carry four defaults. Two of them were Dutch-oven concepts,
they applied silently to any class that did not pass its own, and on the loaf-pan
run the categories were invented rather than asked for. Nothing in the output
said so either way. Hence: required, and loud when missing.

    python -m experiments.curate.run research "Loaf pans" --categories "Classic 1lb; Best value; Nonstick; Pullman"
    python -m experiments.curate.run research "Loaf pans" --category "Classic 1lb" --category "Best value"
"""


def normalize_categories(categories) -> list:
    """The single gate every category list passes through. Raises on anything unusable.

    Accepts a list of names or one delimited string and converts either to the canonical
    form, so the CLI, a future collection record and a direct call cannot diverge. Names are
    split on `;` `,` and newlines — a category name may therefore not contain those.
    """
    if categories is None:
        raise ValueError(NO_CATEGORIES)
    if isinstance(categories, str):
        categories = [categories]
    try:
        parts = [p for c in categories for p in re.split(r"[;,\n]", str(c))]
    except TypeError:
        raise ValueError(f"categories must be a list of names, got "
                         f"{type(categories).__name__}\n\n{NO_CATEGORIES}") from None

    names, seen, dupes = [], {}, []
    for p in (p.strip() for p in parts):
        if not p:
            continue
        if p.lower() in seen:
            dupes.append(p)
        else:
            seen[p.lower()] = p
            names.append(p)
    if not names:
        raise ValueError(NO_CATEGORIES)
    if dupes:
        raise ValueError(
            "duplicate categories: " + ", ".join(repr(d) for d in dupes)
            + " — each category is its own three-product section, so a repeat is a typo "
              "rather than a request.")
    return names


BASE_RULES = """\
You are producing a current, evidence-based {product_class} comparison for a consumer
product database and a written buyer's brief.

OBJECTIVE
Research and rank:
1. The top three {product_class} overall.
2. The top three products in each requested category.
3. A normal retailer purchase link.
4. A separate Amazon link and Amazon ASIN when an exact matching Amazon listing can be verified.

CATEGORIES
{categories}

RESEARCH RULES
1. Use current manufacturer pages, major-retailer listings, Amazon product pages, and
   reputable independent product-testing sources.
2. Verify product title, manufacturer, capacity, typical price, and purchase link.
3. Rank on TYPICAL price — the normal selling price, not a temporary discount. Record today's
   price separately in `current_price` and set `price_type` to "regular" or "sale". A sale
   must never move a product's rank; it is perishable and we re-check it on a schedule.
4. Do not use a search-result URL as the purchase URL. Use the actual product page.
5. Do not include duplicate products that differ only by color.
6. Amazon assigns different ASINs by color and size, but they belong to ONE listing family and
   share the same customer ratings. Record the ASIN for the exact listing you verified; do not
   leave the field blank merely because other colors exist.
7. Never infer or manufacture an ASIN. Leave both amazon_link and amazon_asin blank if an
   exact match cannot be verified. A blank is a correct answer; a guess is not.
8. Verify that the Amazon listing matches the stated manufacturer, product family, capacity,
   and form. A same-brand product of a DIFFERENT TYPE is the most common error — a bread oven
   is not a dutch oven, a braiser is not a stockpot.
9. If an Amazon result is a marketplace bundle, used item, accessory, replacement lid,
   miniature pot, or wrong capacity, reject it.
10. Keep product comments factual, concise, and useful rather than promotional.
11. Record the date checked.
12. EVERY row must carry at least one entry in `source_links` — the URLs you actually used to
    verify that row. A claim we cannot trace is a claim we cannot publish.
13. Prefer the SUPPLIED SOURCE DOCUMENTS below over anything you find by searching. They were
    retrieved directly from the publishers named in them. If a supplied document contradicts
    a search result, the supplied document wins, and say so in `why_it_ranks_here`.
14. EXPLAIN THE ORDER, not just the picks. `why_it_ranks_here` says why a product is good;
    `edge_over_next` must say why it beat the one below it — name that product and name the
    criterion from RANKING WEIGHTS that separated them. For the LAST place in a list, name the
    strongest product that did NOT make the list and say what kept it out. If two picks are
    genuinely close, say that rather than inventing a difference: "essentially tied with X;
    placed ahead on price alone" is a better answer than a manufactured one.

RANKING WEIGHTS
{weights}

OUTPUT REQUIREMENTS
Return valid JSON only. No Markdown, no commentary outside the JSON, no code fences.

{schema}

VALIDATION CHECKLIST
Before returning the JSON, confirm internally that:
- overall_top_three contains exactly three entries with places 1, 2 and 3.
- Every category contains exactly three entries with places 1, 2 and 3.
- No Amazon ASIN appears without an Amazon link, and none is invented.
- Every Amazon link is https://www.amazon.com/dp/ASIN and every ASIN is exactly 10 characters.
- Every typical_price is numeric, not text.
- Every row's source_links is a non-empty list of URLs you actually used.
- Every row's edge_over_next names a specific competing product, not a generic quality.
- The category rankings are logically independent of the overall ranking.
"""

DEFAULT_WEIGHTS = [
    ("Cooking performance", 0.25,
     "Heat retention, evenness, browning, braising, moisture retention, bread performance."),
    ("Durability and construction", 0.20,
     "Enamel quality, lid fit, chip resistance, handle construction, expected service life."),
    ("Ease of use", 0.15,
     "Empty and loaded weight, handle size, interior color, cleaning, monitoring fond."),
    ("Versatility", 0.15,
     "Soups, sauces, stews, braises, bread, roasts, everyday stovetop use."),
    ("Value at typical price", 0.15,
     "Typical (not sale) price relative to performance, capacity and expected lifespan."),
    ("Capacity and shape", 0.05,
     "Useful volume, burner footprint, round versus oval geometry."),
    ("Brand support and warranty", 0.05,
     "Manufacturer history, warranty confidence, replacement support."),
]

SCHEMA = """\
Use this exact structure:

{
  "date_checked": "YYYY-MM-DD",
  "currency": "USD",
  "product_class": "",
  "overall_top_three": [
    {
      "place": 1,
      "product_title": "",
      "manufacturer": "",
      "capacity": "",
      "typical_price": 0.00,
      "current_price": 0.00,
      "price_type": "regular",
      "best_for": "",
      "why_it_ranks_here": "",
      "edge_over_next": "",
      "important_tradeoff": "",
      "buy_link": "",
      "amazon_link": "",
      "amazon_asin": "",
      "source_links": ["https://..."]
    }
  ],
  "category_rankings": [
    {
      "category": "",
      "place": 1,
      "product_title": "",
      "manufacturer": "",
      "capacity": "",
      "typical_price": 0.00,
      "current_price": 0.00,
      "price_type": "regular",
      "why_it_ranks_here": "",
      "edge_over_next": "",
      "important_tradeoff": "",
      "buy_link": "",
      "amazon_link": "",
      "amazon_asin": "",
      "source_links": ["https://..."]
    }
  ],
  "ranking_criteria": [
    {"criterion": "", "weight": 0.25, "what_was_considered": "", "effect_on_ranking": ""}
  ],
  "methodology_note": ""
}
"""


def build_prompt(product_class: str, categories: list, docs: list | None = None,
                 weights: list | None = None) -> str:
    """Assemble the research prompt, with our fetched source documents inlined.

    `docs` = [{label, url, markdown, via}] from BCC's fetch stack. Supplying them is the
    difference between "reputable independent testing sources" as an instruction and as a
    fact — the model cannot reach ATK, Serious Eats or Wirecutter on its own.

    `categories` is REQUIRED and validated here as well as at the caller: this is the last
    point at which a bad list is still cheap. Weights stay defaultable — the curator judged
    the seven universal, and unlike categories they rank what was already chosen rather than
    deciding what gets considered.
    """
    categories = normalize_categories(categories)
    weights = weights or DEFAULT_WEIGHTS
    parts = [BASE_RULES.format(
        product_class=product_class,
        categories="\n".join(f"- {c}" for c in categories),
        weights="\n".join(f"- {n}: {int(w*100)}%   ({what})" for n, w, what in weights),
        schema=SCHEMA,
    )]

    supplied = [d for d in (docs or []) if d.get("markdown")]
    if supplied:
        parts.append(
            "\n\nSUPPLIED SOURCE DOCUMENTS — retrieved by us directly from these publishers.\n"
            "Treat them as the authoritative text for those sources; do not search for them\n"
            "again. Long roundups are trimmed to the passages about this product class.\n")
        for d in supplied:
            parts.append(f"\n===== {d['label']} =====\nURL: {d.get('url','')}\n"
                         f"Retrieved via: {d.get('via','')}\n-----\n{d['markdown']}\n"
                         f"===== end {d['label']} =====\n")
    missing = [d for d in (docs or []) if not d.get("markdown")]
    if missing:
        parts.append(
            "\nCOULD NOT RETRIEVE — every fetch tier failed for these. You may search for "
            "them, but do NOT replace them with a different publisher and do not cite a "
            "mirror or reprint as though it were them:\n"
            + "\n".join(f"- {d['label']} ({d.get('error','')})" for d in missing))

    parts.append("\n\nNow output the single JSON record per the schema.")
    return "\n".join(parts)
