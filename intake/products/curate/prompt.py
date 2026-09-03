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
#  Categories — a staff input; absent means the WHOLE CLASS
# --------------------------------------------------------------------------- #
#
# A category is a SUBSET somebody chose to carve out of the class — "Best budget",
# "Best for bread" — and each one is a three-product section a product can win by
# being the only candidate in it. Choosing to carve one out therefore decides what
# gets recommended at all, which makes it a staff call.
#
# So the default is NOT a set of categories, it is NO categories: rank the whole
# class and return the overall three. This runner used to carry four defaults, two
# of them Dutch-oven concepts, applied silently to any class that passed none — and
# on the loaf-pan run the categories were invented rather than asked for. Nothing in
# the output said so either way. An empty list is now an honest, complete answer;
# an invented subset never was.


def normalize_categories(categories) -> list:
    """The single gate every category list passes through. `[]` is a valid answer.

    Accepts a list of names, one delimited string, or nothing, and converts all three to the
    canonical form, so the CLI, a future collection record and a direct call cannot diverge.
    Names are split on `;` `,` and newlines — a category name may therefore not contain
    those. Raises only on input that cannot be meant: a non-iterable, or a duplicate.
    """
    if categories is None:
        return []
    if isinstance(categories, str):
        categories = [categories]
    try:
        parts = [p for c in categories for p in re.split(r"[;,\n]", str(c))]
    except TypeError:
        raise ValueError(f"categories must be a list of names or a delimited string, got "
                         f"{type(categories).__name__}") from None

    names, seen, dupes = [], {}, []
    for p in (p.strip() for p in parts):
        if not p:
            continue
        if p.lower() in seen:
            dupes.append(p)
        else:
            seen[p.lower()] = p
            names.append(p)
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
{objective}

{categories}

RESEARCH RULES
1. Use current manufacturer pages, major-retailer listings, Amazon product pages, and
   reputable independent product-testing sources.
2. Verify product title, manufacturer, capacity, typical price, and purchase link.
2b. Record the MANUFACTURER'S MODEL/PART NUMBER in `model_number` (e.g. KHM7210,
   5.5SQCI). Within a brand's product line it is the only unambiguous identity —
   "KitchenAid 7-speed hand mixer" names several different mixers; the model number
   names one. Source it FROM THE REVIEWS FIRST: the SUPPLIED SOURCE DOCUMENTS state
   the exact model the reviewer tested, and THAT model is the recommendation — a
   model number taken from a retail listing instead can silently name a sibling the
   reviewer never tested. Fall back to the manufacturer page only when no review
   states it. Blank if unverifiable; never guessed.
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
14. ACCOUNT FOR EVERY SUPPLIED SOURCE DOCUMENT in `source_report` — one entry per
    supplied document, `source` copied exactly from its ===== label =====.
    `page_covers`: a few words saying what the page ACTUALLY reviews — its own
    subject, not the subject we hoped it covered. `relevance`: "on-class" (the page
    reviews this exact product class), "related" (an adjacent class, a single-product
    review, or an advice/safety article that still informed your judgment), or
    "off-topic" (a different class — e.g. a pressure-COOKER roundup supplied for a
    pressure-CANNER run). `used_in_ranking`: whether the document actually influenced
    a pick. An honestly-reported off-topic page is a GOOD answer — it tells the
    curator that this publisher has no review of this class; never stretch an
    off-topic page into citations to make a source look consulted.
15. EXPLAIN THE ORDER, not just the picks. `why_it_ranks_here` says why a product is good;
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
{checklist_categories}
- No Amazon ASIN appears without an Amazon link, and none is invented.
- Every Amazon link is https://www.amazon.com/dp/ASIN and every ASIN is exactly 10 characters.
- Every typical_price is numeric, not text.
- Every row's source_links is a non-empty list of URLs you actually used.
- Every row's edge_over_next names a specific competing product, not a generic quality.
- source_report has exactly one entry per supplied source document, honest about relevance.
"""

# The two shapes this prompt takes. Written out rather than assembled from fragments so each
# can be read as the model receives it.
CATEGORIES_ASKED = """\
CATEGORIES
Also rank the top three in each of these, independently of the overall ranking:
{categories}"""

CATEGORIES_NONE = """\
CATEGORIES
None. Rank the class AS A WHOLE and return `category_rankings` as an empty list.

Do not invent categories. A category is a subset somebody chose to carve out, and
nobody carved one out here — so the overall three must be the best of the ENTIRE
class rather than the best of some niche you selected for yourself. If the class
genuinely splits (a size, a material, a use that changes the answer), say so in
`methodology_note` so a curator can decide whether to ask for those categories;
do not answer the question by inventing them."""

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
      "model_number": "",
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
      "model_number": "",
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
  "source_report": [
    {"source": "", "page_covers": "", "relevance": "on-class", "used_in_ranking": true}
  ],
  "methodology_note": ""
}
"""


EDITORS_CHOICE_ASKED = """\

EDITOR'S CHOICE
The curator has pinned this product for analysis alongside the ranking:
  {pick}
Add a top-level "editors_choice" object to the JSON — the same row shape as an
overall_top_three entry, with "place": 1. Analyze it with the SAME rigor: real
product_title, manufacturer, model_number, typical_price, buy_link; the ASIN rules
above apply unchanged. It must NOT displace, reorder, or appear in overall_top_three
or any category — it is a separate, labeled slot with curator provenance.
Honesty rules for it: if none of the supplied source documents tested this product,
say so plainly at the START of why_it_ranks_here ("Curator's pick — not tested by
the supplied sources; ...") and ground the analysis in what CAN be verified
(manufacturer specs, retailer listings — those pages are then your source_links).
Never cite a supplied source for a claim it did not make about THIS product.
why_it_ranks_here explains what this pick offers relative to the ranked three;
important_tradeoff keeps its meaning; for edge_over_next, state honestly how it
compares to the overall #1 (it has no "next" — a candid comparison is the useful
answer)."""


# Always-on class boundary — the Water-Bath-Canner lesson (2026-09-03): with only
# the class NAME to go on, the model ranked an electric multi-cooker #2 and an
# accessory rack matched as #3. The boundary is stated for EVERY run; the curator's
# per-collection criteria (curated_collections.class_criteria), when present, are
# appended as the binding definition that outranks the model's own judgment.
CLASS_BOUNDARY = """

CLASS BOUNDARY — WHAT COUNTS AS A {product_class}
Before researching, fix what a {product_class} IS and IS NOT. Every ranked product must be
one a shopper who searched "{product_class}" would accept as EXACTLY that — never:
- a neighboring class or a variant with a different power source or working principle
  (an electric multi-cooker is not a stovetop water-bath canner; a pressure canner is not
  a water-bath canner);
- a larger or multi-purpose appliance that merely CAN do the job;
- an accessory, insert, rack, basket, lid, or replacement part FOR the class.
When a strong, well-reviewed candidate fails this boundary, LEAVE IT OUT of every ranking —
name it and the reason in `methodology_note` instead, so the curator sees the judgment call.
"""

CURATOR_CRITERIA = """
THE CURATOR'S BINDING DEFINITION for this collection — where it conflicts with your own
reading of the class, the curator wins. Reject anything it excludes, even a #1 product:
{criteria}
"""


def build_prompt(product_class: str, categories: list, docs: list | None = None,
                 weights: list | None = None, editors_choice: str = "",
                 class_criteria: str = "") -> str:
    """Assemble the research prompt, with our fetched source documents inlined.

    `docs` = [{label, url, markdown, via}] from BCC's fetch stack. Supplying them is the
    difference between "reputable independent testing sources" as an instruction and as a
    fact — the model cannot reach ATK, Serious Eats or Wirecutter on its own.

    `categories` is optional, and EMPTY IS A REAL ANSWER — the prompt then asks for the whole
    class and forbids inventing subsets, rather than falling back to somebody else's. The
    objective list and the checklist change with it, so the model is never asked to fill a
    section that was not requested.
    """
    categories = normalize_categories(categories)
    weights = weights or DEFAULT_WEIGHTS

    # Number-neutral phrasing: class names are canonically SINGULAR ("Muffin
    # Pan" — they are join keys naming a product type), so the sentence can't
    # lean on the name being plural.
    objective = [f"The top three products in the {product_class} class, overall."]
    if categories:
        objective.append("The top three products in each requested category.")
    objective += ["A normal retailer purchase link.",
                  "A separate Amazon link and Amazon ASIN when an exact matching Amazon "
                  "listing can be verified."]

    parts = [BASE_RULES.format(
        product_class=product_class,
        objective="\n".join(f"{i}. {t}" for i, t in enumerate(objective, 1)),
        categories=(CATEGORIES_ASKED.format(
            categories="\n".join(f"- {c}" for c in categories)) if categories
            else CATEGORIES_NONE),
        checklist_categories=(
            "- Every category contains exactly three entries with places 1, 2 and 3.\n"
            "- The category rankings are logically independent of the overall ranking."
            if categories else
            "- category_rankings is an empty list; no categories were invented."),
        weights="\n".join(f"- {n}: {int(w*100)}%   ({what})" for n, w, what in weights),
        schema=SCHEMA,
    )]
    parts.append(CLASS_BOUNDARY.format(product_class=product_class))
    if (class_criteria or "").strip():
        parts.append(CURATOR_CRITERIA.format(criteria=class_criteria.strip()))
    if (editors_choice or "").strip():
        parts.append(EDITORS_CHOICE_ASKED.format(pick=editors_choice.strip()))

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
