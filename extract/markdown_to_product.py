"""Canonical markdown -> Product (single retailer product page).

The product-side analog of extract/markdown_to_recipe.py. Given the markdown of ONE
retailer product page (Amazon / Williams Sonoma / Sur La Table / manufacturer) — usually
carrying schema.org Product JSON-LD — one LLM call fills the existing product_model.Product:
name, brand, category, a SIZE-SPECIFIC product_class, specs, and ONE retailer offer.

This is the curator's workflow: pick the product -> land on its retail page -> product
bookmarklet -> extract into the model -> edit in the form -> save (classify + embed).

NOT reviews: `verdicts` stays empty (a product page is not a third-party review), and the
BCC editorial (`bcc_pick`/`bcc_blurb`/`critique`) stays empty — that's the curator's voice,
added in the form, never fabricated by the extractor. SIZE/CAPACITY is captured carefully
because it's how a product maps to the right ranked class (a small saucepan != a Dutch oven).
"""
import json
import os
import re
import time
from typing import Optional

from product_model import Product
from input.pipeline.url_utils import normalize_url, root_domain
import llm  # central LLM gateway — auto-journals usage to bcc_token_journal


_DATA_URL_IMG_RE = re.compile(r"!\[[^\]]*\]\(data:[^)]*\)")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# host -> friendly retailer name (drives RetailerOffer.retailer when the LLM is unsure).
_RETAILERS = {
    "amazon.com": "Amazon", "williams-sonoma.com": "Williams Sonoma",
    "surlatable.com": "Sur La Table", "surlatable.com": "Sur La Table",
    "crateandbarrel.com": "Crate & Barrel", "target.com": "Target",
    "wayfair.com": "Wayfair", "kingarthurbaking.com": "King Arthur",
}


def clean_markdown(md: str) -> str:
    md = _DATA_URL_IMG_RE.sub("", md)
    md = _BLANK_LINES_RE.sub("\n\n", md)
    return md.strip()


SYSTEM_PROMPT = f"""
You are a product-data extractor. Given markdown of ONE retailer PRODUCT PAGE (Amazon,
Williams Sonoma, Sur La Table, or a manufacturer) — possibly with embedded schema.org
Product JSON-LD — produce a JSON object conforming exactly to the schema below.

AUTHORITY RULES:
1. If the markdown contains a schema.org Product JSON-LD block, treat it as AUTHORITATIVE for:
   name, brand, image (-> image_url), price/availability, and identifiers (SKU/ASIN/GTIN).
2. Use the surrounding markdown (title, bullet "About this item", specs table, description)
   to fill `specs` and anything JSON-LD doesn't cover.
3. Ignore page chrome, navigation, ads, "customers also bought", Q&A, and the customer-review
   section — this is the product record, not a review.

IMAGE: set `image_url` to the product's primary hero image. Priority: the JSON-LD `image`,
else a "PRIMARY IMAGE" URL line in the markdown (the bookmarklet includes og:image there),
else the first clearly-product image. Output an ABSOLUTE https URL; never a data: URI.

DESCRIPTION: write `description` — a concise, FACTUAL 2-4 sentence product description drawn
ONLY from the page (material, construction, size, notable features, intended use). This is
reference copy that also drives search/matching, so pack in the concrete facts. STRIP marketing
hype and superlatives ("best-in-class", "you'll love") and do NOT add any opinion — that's the
curator's `bcc_blurb`, not this. If the page gives nothing factual, leave it "".

SIZE IS CRITICAL: capture capacity/volume and key dimensions into `specs` (capacity,
dimensions_in), and reflect the size in `product_class` — the specific, SIZE-SCOPED ranked
grouping this product belongs to (e.g. "Loaf Pans (1 lb)", "Saucepans (2 qt)", "Dutch Ovens
(6-7 qt)"), NOT a generic "Pans". `category` is the coarse aisle ("Bakeware", "Cookware",
"Knives"). A small saucepan and a Dutch oven are DIFFERENT classes. Capture sizes as written;
we canonicalize units afterward, so don't fret the exact syntax.

RETAILER OFFER: fill exactly one `retailer_offers` entry for THIS page — retailer name, the
ASIN if it's Amazon, `source_url` (the page URL), and `price` if shown. Do NOT invent an
affiliate_url (we mint our own later).

LEAVE EMPTY (do not fabricate): `verdicts` (no third-party review here), and `bcc_pick`/
`bcc_blurb`/`critique` (the curator's editorial voice, added later). `rank_score` = null.

OUTPUT RULES:
- Output a single valid JSON object matching the schema. No preamble, no fences, no commentary.
- Use empty strings / empty lists / null where a value is unknown.

<SCHEMA>
{json.dumps(Product.model_json_schema(), indent=2)}
</SCHEMA>
""".strip()


def markdown_to_product(
    markdown_text: str,
    *,
    source_name: str = "",
    source_url: str = "",
    title: str = "",
    model: str = "claude-haiku-4-5",
    timings: Optional[dict] = None,
    prompts: Optional[dict] = None,
) -> Optional[dict]:
    """Extract one Product from a retailer product page's markdown. Returns a validated
    Product as a dict, or None on parse/validate failure. Mirrors markdown_to_recipe."""
    t0 = time.perf_counter()
    cleaned_md = clean_markdown(markdown_text)

    user_prompt = (
        "Extract a single structured product from this retailer product page. "
        "Return strict JSON.\n\n"
        f"<MARKDOWN>\n{cleaned_md}\n</MARKDOWN>"
    )
    if prompts is not None:
        prompts.update({"model": model, "system_prompt": SYSTEM_PROMPT, "user_prompt": user_prompt})
    t_prep = time.perf_counter()
    if timings is not None:
        timings["prep_ms"] = int((t_prep - t0) * 1000)

    with llm.stream(
        operation="markdown_to_product", model=model,
        max_tokens=2048, temperature=0.2, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    content = next((b.text for b in response.content if b.type == "text"), "")
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)
    try:
        json_data = json.loads(stripped)
    except Exception as e:
        print("     ERROR: Failed to parse product JSON:", e)
        print("     DEBUG: Raw output:\n", content[:1500])
        return None
    if isinstance(json_data, list):
        json_data = next((x for x in json_data if isinstance(x, dict)), None)
    if not isinstance(json_data, dict):
        print("     ERROR: extraction returned no usable product object")
        return None

    t_llm = time.perf_counter()
    if timings is not None:
        timings["extract_llm_ms"] = int((t_llm - t_prep) * 1000)

    _attach_source(json_data, source_url=source_url, title=title)

    try:
        product = Product.model_validate(json_data).model_dump()
        from intake.products.measures import normalize_product
        normalize_product(product)   # canonicalize dimensions/capacity/class size
        if timings is not None:
            timings["validate_ms"] = int((time.perf_counter() - t_llm) * 1000)
        return product
    except Exception as e:
        print("     ERROR: Failed to validate against Product model:", e)
        print("     DEBUG: payload:\n", json.dumps(json_data, indent=2)[:1500])
        return None


def _attach_source(json_data: dict, *, source_url: str, title: str) -> None:
    """Stamp the retailer offer's source_url + retailer from the page URL, and absolutize a
    site-relative image_url. The product record has no `_source` block (retailer_offers ARE
    the provenance), so we normalize the offer instead."""
    normalized = normalize_url(source_url) if source_url else source_url
    host = (root_domain(normalized) or "").lower() if normalized else ""
    retailer = _RETAILERS.get(host, "")
    offers = json_data.get("retailer_offers")
    if not offers and normalized:
        offers = [{"retailer": retailer, "source_url": normalized}]
    for o in offers or []:
        if isinstance(o, dict):
            o.setdefault("source_url", normalized)
            if not o.get("retailer") and retailer:
                o["retailer"] = retailer
    if offers:
        json_data["retailer_offers"] = offers
    # Absolutize a site-relative image URL against the page origin.
    img = json_data.get("image_url")
    if isinstance(img, str) and img.startswith("/") and not img.startswith("//") and source_url:
        from urllib.parse import urljoin
        json_data["image_url"] = urljoin(source_url, img)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m extract.markdown_to_product <markdown_file> [source_url]")
        sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        md = f.read()
    src = sys.argv[2] if len(sys.argv) > 2 else ""
    r = markdown_to_product(md, source_name=os.path.basename(sys.argv[1]), source_url=src)
    print(json.dumps(r, indent=2) if r else "FAILED")
