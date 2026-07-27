"""Canonical markdown -> ProductReviewExtraction (a product-ROUNDUP review page).

The review-side analog of extract/markdown_to_product.py. Given the markdown of ONE
product-review page (America's Test Kitchen, Wirecutter, Williams Sonoma, WSJ Buy Side, ...),
one LLM call produces the canonical decoder dict that intake/products/review_sources.ingest_review
consumes: a single `product_class` (the whole roundup is about ONE class, taken from the page
header), the `review_source` provenance, and one entry per tested product with its tier + verdict +
retailer offer(s).

Why an LLM (not the deterministic per-source regex parsers): review sites vary wildly and even one
site (ATK) renders differently across pages — e.g. specs/prices hidden behind collapsed toggles — so
a regex that keys off labeled spec blocks silently drops products. This is a low-volume, curator-
driven task, so an LLM call per import is fine and far more robust. Used as the fallback when a
deterministic decoder yields nothing (see review_sources/__init__.ingest_review).

BRAND-SAFETY (memory/project_affiliate_catalog): a `verdict`/`tier` must reflect what the page
actually says — the extractor NEVER invents specs, prices, ASINs, or verdicts. Empty where the page
is silent. BCC's own editorial (bcc_pick/bcc_blurb) is added later in the form, never here.

Output shape is the DECODER dict (singular `verdict` per product), NOT the pydantic product_model
`Product` (which carries plural homogenized `verdicts`).
"""
import json
import os
import re
import time
from typing import List, Optional

from pydantic import BaseModel, Field

from product_model import ProductSpecs, ReviewSource
from input.pipeline.url_utils import normalize_url, root_domain
import llm  # central LLM gateway — auto-journals usage to bcc_token_journal


_DATA_URL_IMG_RE = re.compile(r"!\[[^\]]*\]\(data:[^)]*\)")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# host -> friendly retailer name (fallback when the LLM leaves an offer's retailer blank).
_RETAILERS = {
    "amazon.com": "Amazon", "williams-sonoma.com": "Williams Sonoma",
    "surlatable.com": "Sur La Table", "crateandbarrel.com": "Crate & Barrel",
    "target.com": "Target", "walmart.com": "Walmart", "wayfair.com": "Wayfair",
    "oxo.com": "OXO", "cuisinart.com": "Cuisinart",
}


# ---- The canonical decoder-dict shape (what ingest_review consumes) --------------------
class _Offer(BaseModel):
    retailer: str = ""
    asin: str = ""                       # Amazon ASIN, only from a real /dp/ link
    source_url: str = ""


class _Verdict(BaseModel):
    tier: str = ""                       # Winner | Co-Winner | Highly Recommended | Recommended | Recommended with Reservations | Not Recommended | Discontinued
    summary: str = ""                    # the reviewer's verdict prose (verbatim-ish)
    price_at_test: Optional[float] = None


class _ProductClass(BaseModel):
    name: str = ""                       # SIZE-scoped class for the WHOLE roundup, from the header
    category: str = ""                   # coarse aisle: Bakeware | Cookware | Knives | ...
    criteria: List[str] = Field(default_factory=list)
    buying_guide: str = ""               # the review's EDITORIAL HEADER (lede + what-to-look-for /
                                         # avoid / considerations / FAQ takeaways) — our raw material
                                         # for writing our own summary; the per-product verdicts are sparse


class _RevProduct(BaseModel):
    name: str = ""
    brand: str = ""
    specs: ProductSpecs = Field(default_factory=ProductSpecs)
    verdict: _Verdict = Field(default_factory=_Verdict)
    retailer_offers: List[_Offer] = Field(default_factory=list)


class ReviewExtraction(BaseModel):
    product_class: _ProductClass = Field(default_factory=_ProductClass)
    review_source: ReviewSource = Field(default_factory=ReviewSource)
    products: List[_RevProduct] = Field(default_factory=list)


def clean_markdown(md: str) -> str:
    md = _DATA_URL_IMG_RE.sub("", md)
    md = _BLANK_LINES_RE.sub("\n\n", md)
    return md.strip()


_GENERIC_HINTS = (
    "SOURCE: a general product-review / roundup page. Read the page's OWN wording to find each product's "
    "recommendation/tier label, the tested-products list, and the buying-guide / how-to-choose prose."
)


def _build_system_prompt(source_hints: str = "") -> str:
    """Assemble the extractor system prompt: shared base rules + SOURCE-SPECIFIC guidance (a per-source
    prompt fragment supplied by review_sources/<source>.py) + the JSON schema. Per-source prompts sharpen
    extraction on each site's structure (ATK's 'Everything We Tested'/tier sections vs. Wirecutter's
    'Our pick'/'How we picked', etc.) without duplicating the shared mechanism."""
    hints = (source_hints or _GENERIC_HINTS).strip()
    return f"""
You are a product-review extractor. Given the markdown of ONE product-ROUNDUP review page (America's
Test Kitchen, Wirecutter, Williams Sonoma, WSJ Buy Side, or similar), produce a JSON object conforming
exactly to the schema below.

SOURCE-SPECIFIC GUIDANCE (use this to locate the products, tiers, retailers, and editorial on THIS site):
{hints}

THE WHOLE REVIEW IS ABOUT ONE PRODUCT CLASS — TAKE IT FROM THE HEADER:
- The page title / H1 names the class the entire roundup tests (e.g. "The Best 13 by 9-Inch Baking
  Pans/Dishes", "The Best Loaf Pans"). Derive `product_class.name` as a concise, SIZE-scoped label from
  that header (e.g. "13x9 Baking Pans", "Loaf Pans (1 lb)"), and `product_class.category` as the coarse
  aisle ("Bakeware", "Cookware", "Knives", "Electrics"). Every tested product inherits this one class —
  do NOT try to give each product its own class.

CAPTURE THE EDITORIAL HEADER (`product_class.buying_guide`) — THIS IS IMPORTANT:
- Everything BEFORE the "Everything We Tested" product list is the reviewer's buying guide, and it is the
  RICHEST content on the page (the per-product verdicts are terse). Capture it thoroughly into
  `product_class.buying_guide` as clean, faithful prose: the opening overview/lede, "What You Need to
  Know", "What to Look For", "What to Avoid", "Other Considerations", and the key takeaways from any FAQs.
- Preserve the substance and the specifics (e.g. "golden pans bake more evenly", "a slick nonstick
  coating is essential for inversion", "avoid deeply sloped walls"). This is the source material we will
  base OUR OWN editorial summary on, so keep it complete and information-dense — several paragraphs is
  fine. Do NOT invent claims; capture only what the page states. Do NOT add BCC opinion here.
  `product_class.criteria` = the short list of testing/what-to-look-for headings (e.g. ["Golden color",
  "Nonstick coating", "Straight walls", "Crisp corners", "Handles"]).

OUTPUT ORDER — emit `products` BEFORE `product_class` in the JSON object. The products are the
part we cannot reconstruct later; the buying guide is prose that can be re-captured. On a big
roundup (50+ products) writing the guide first has cost us the product list entirely.

PRODUCTS — extract EVERY product tested:
- The "Everything We Tested" section lists each product under a tier heading. Capture ALL of them (a
  roundup usually has 10-20). If a "Top Pick"/"Winner" block repeats the top product at the very top of
  the page, DEDUPE it — include that product once.
- `verdict.tier` MUST be one of this CONTROLLED VOCABULARY, exactly — never the site's own wording:
      Winner | Co-Winner | Highly Recommended | Recommended | Recommended with Reservations |
      Not Recommended | Discontinued
  Prefer the most specific: a "Winner" badge under "Highly Recommended" => tier "Winner".
  Many sites head their picks with an AWARD PHRASE instead of a tier — "The Best Dutch Oven (Ever!)",
  "The Best Budget Dutch Oven Under $80", "Also Good", "Our pick", "Upgrade pick". MAP those onto the
  vocabulary (a single overall best => "Winner"; a category/budget/upgrade best => "Highly Recommended";
  a runner-up or "Also Good" => "Recommended"), and PRESERVE the site's exact award phrase by starting
  `verdict.summary` with it, e.g. "Best Budget Dutch Oven Under $80 — ...". The tier is what we filter
  and rank on, so a free-text tier makes every winner its own unique category and is useless.
- `verdict.summary`: the product's verdict paragraph, close to verbatim (light cleanup only), prefixed
  with the award phrase when the page gave one.
- `verdict.price_at_test`: the tested price ONLY if the page shows one; else null.
- `name`: the FULL product name exactly as written on the page, INCLUDING the brand (e.g. "Williams
  Sonoma Goldtouch Nonstick Cake Pan", "USA Pan Rectangular Cake Pan"). Only strip a leading tier/badge
  word (Winner / Co-Winner). Do NOT drop the brand from the name — the name is used to match products.
  `brand`: ALSO set the manufacturer separately (e.g. "USA Pan", "OXO Good Grips", "Le Creuset").
- `retailer_offers`: from the buy links ("Buy at Williams Sonoma", "Buy from 3 Sellers", "Buy at Amazon").
  Set `retailer` and `source_url` from a real link; set `asin` ONLY from a real Amazon /dp/XXXXXXXXXX
  link. If the page only says "Buy from N Sellers" with no direct link, you may add one offer with just
  the retailer note or leave it empty — never invent a URL or ASIN.
- `specs` (model_number/mpn/gtin/material/dimensions_in/capacity/weight/dishwasher_safe): fill ONLY from
  values explicitly on the page; many roundup pages hide specs behind toggles — leave them "" if absent.

REVIEW SOURCE: fill `review_source` — `reviewer` = the publication ("America's Test Kitchen", "Wirecutter",
etc.), `authors` = the byline author(s), `title` = the page H1, `last_updated` = any "Last Updated ..."
date, `rating_scale` = the scale if stated (e.g. "3-star (Good/Fair/Poor)"). `url` may be left "" (added
by the caller).

BRAND-SAFETY — DO NOT FABRICATE: never invent specs, prices, ASINs, model numbers, brands, or verdicts.
A verdict/tier must reflect what the page actually states. Use "" / [] / null where unknown. Do NOT write
any BCC opinion — only extract the source's content.

OUTPUT RULES:
- Output a single valid JSON object matching the schema. No preamble, no fences, no commentary.
- Use empty strings / empty lists / null where a value is unknown.

<SCHEMA>
{json.dumps(ReviewExtraction.model_json_schema(), indent=2)}
</SCHEMA>
""".strip()


def markdown_to_review(
    markdown_text: str,
    *,
    source_url: str = "",
    title: str = "",
    source_hints: str = "",
    model: str = "claude-sonnet-4-6",
    # 8192 was not enough for a big roundup. ATK's large-Dutch-ovens review tests ~50
    # products; the model spent its budget on the editorial header and was cut off
    # mid-string, so the JSON never parsed and the caller reported "no product
    # recommendations" — a truncation reported as an empty page.
    max_tokens: int = 20000,
    timings: Optional[dict] = None,
    prompts: Optional[dict] = None,
) -> Optional[dict]:
    """Extract a ProductReviewExtraction (decoder dict) from a review page's markdown. Returns the
    dict (product_class / review_source / products[...]) or None on parse/validate failure.

    `source_hints` = the per-source prompt fragment (from review_sources/<source>.EXTRACT_HINTS) that
    tunes the extractor to THIS site's structure; falls back to a generic hint. Defaults to a sonnet-tier
    model for reliable multi-product extraction (low-volume, accuracy-first); pass
    model="claude-haiku-4-5" for the cheaper option."""
    t0 = time.perf_counter()
    cleaned_md = clean_markdown(markdown_text)
    system_prompt = _build_system_prompt(source_hints)

    user_prompt = (
        "Extract the roundup review below into strict JSON per the schema. Take the product class from "
        "the page header; capture every tested product with its tier and verdict, and the editorial "
        "header into buying_guide.\n\n"
        f"<MARKDOWN>\n{cleaned_md}\n</MARKDOWN>"
    )
    if prompts is not None:
        prompts.update({"model": model, "system_prompt": system_prompt, "user_prompt": user_prompt})
    t_prep = time.perf_counter()
    if timings is not None:
        timings["prep_ms"] = int((t_prep - t0) * 1000)

    with llm.stream(
        operation="review_extract", model=model,
        max_tokens=max_tokens, temperature=0.2, system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    content = next((b.text for b in response.content if b.type == "text"), "")
    # Say WHY when the model ran out of room. Truncated JSON fails to parse a few lines
    # below, and without this the caller only learns "no products found" — which sends you
    # looking at the page instead of at the token budget.
    if getattr(response, "stop_reason", None) == "max_tokens":
        print(f"     ERROR: review extraction hit max_tokens ({max_tokens}) — the JSON is "
              f"truncated. Raise max_tokens or trim the page; this is NOT an empty page.")
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    try:
        json_data = json.loads(stripped)
    except Exception as e:
        print("     ERROR: Failed to parse review JSON:", e)
        print("     DEBUG: Raw output:\n", content[:1500])
        return None
    if not isinstance(json_data, dict):
        print("     ERROR: extraction returned no usable review object")
        return None

    t_llm = time.perf_counter()
    if timings is not None:
        timings["extract_llm_ms"] = int((t_llm - t_prep) * 1000)

    try:
        ext = ReviewExtraction.model_validate(json_data).model_dump()
    except Exception as e:
        print("     ERROR: Failed to validate against ReviewExtraction:", e)
        print("     DEBUG: payload:\n", json.dumps(json_data, indent=2)[:1500])
        return None

    _stamp_source(ext, source_url=source_url, title=title)
    if timings is not None:
        timings["validate_ms"] = int((time.perf_counter() - t_llm) * 1000)
    return ext


def _stamp_source(ext: dict, *, source_url: str, title: str) -> None:
    """Fill the review_source url/title from the caller when the LLM left them blank, and normalize
    each offer's source_url + retailer from its host (mirrors markdown_to_product._attach_source)."""
    src = ext.setdefault("review_source", {})
    if source_url and not src.get("url"):
        src["url"] = source_url
    if title and not src.get("title"):
        src["title"] = title
    for p in ext.get("products") or []:
        for o in p.get("retailer_offers") or []:
            if not isinstance(o, dict):
                continue
            u = o.get("source_url") or ""
            if u:
                o["source_url"] = normalize_url(u) or u
                if not o.get("retailer"):
                    host = (root_domain(o["source_url"]) or "").lower()
                    if host in _RETAILERS:
                        o["retailer"] = _RETAILERS[host]


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m extract.markdown_to_review <markdown_file> [source_url]")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        md = f.read()
    src = sys.argv[2] if len(sys.argv) > 2 else ""
    r = markdown_to_review(md, source_url=src)
    if not r:
        print("FAILED")
    else:
        print(f"class: {r['product_class']}")
        print(f"reviewer: {r['review_source'].get('reviewer')} | products: {len(r['products'])}")
        for p in r["products"]:
            print(f"  [{p['verdict']['tier']}] {p['name']}  (brand={p['brand']!r})")
