"""Canonical markdown -> RecipeModel.

One LLM call per recipe. JSON-LD-aware: when the markdown contains a
``STRUCTURED RECIPE DATA (JSON-LD)`` block, the prompt treats that JSON as
authoritative for the schema.org fields and uses the surrounding markdown
only for narrative content the JSON-LD doesn't cover.

This single call also fills the custom `provenance` and `classification`
blocks so we don't need a separate enrichment pass. Provenance inference is
inherently fuzzy on novel/regional dishes — `classification.confidence`
gates how the UI should treat the result.
"""
import json
import os
import re
import time
from typing import Any, Optional

import openai

from sanitize_recipe_data import sanitize_recipe_data
from recipe_model import RecipeModel
from input.pipeline.validators import is_recipe, stamp_validation_on_recipe
from input.pipeline.url_utils import normalize_url, root_domain
from input.pipeline.token_journal import build_usage_entry


openai.api_key = os.getenv("OPENAI_API_KEY")


_DATA_URL_IMG_RE = re.compile(r"!\[[^\]]*\]\(data:[^)]*\)")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def clean_markdown(md: str) -> str:
    """Drop base64 image refs and collapse excess blank lines."""
    md = _DATA_URL_IMG_RE.sub("", md)
    md = _BLANK_LINES_RE.sub("\n\n", md)
    return md.strip()


SYSTEM_PROMPT = f"""
You are a culinary data extractor. Given markdown describing a recipe — possibly with embedded JSON-LD structured data, possibly raw OCR'd from a photo, possibly a hand-typed note — produce a JSON object conforming exactly to the schema below.

AUTHORITY RULES:
1. If the markdown contains a section titled "STRUCTURED RECIPE DATA (JSON-LD)" with a fenced ```json``` block, treat that JSON-LD as the AUTHORITATIVE source for: name, description, ingredients (recipeIngredient), instructions (recipeInstructions), prepTime, cookTime, totalTime, recipeYield, recipeCategory, recipeCuisine, keywords, nutrition, aggregateRating, author, datePublished, dateModified, video, image. Copy values through with minimal reshaping to match the schema.
2. Use the surrounding markdown ONLY to fill fields the JSON-LD does not cover (notes, equipment, servingSuggestions) or to enrich provenance and classification.
3. If no JSON-LD section is present, derive ALL fields from the markdown body. Preserve quantities and unit text exactly as written — do not convert units.
4. Ignore page chrome, navigation links, advertisements, comment threads, and "related recipes" lists.

ENRICHMENT FIELDS — fill these whether JSON-LD is present or not. Make a best-effort inference using ANY signal: dish name, cooking technique (e.g. "au gratin" → French, "tagine" → North African, "carbonara" → Roman), key ingredients, naming convention. Leaving a field empty signals "no signal at all" — reserve that for genuinely unidentifiable dishes (e.g. "Mom's Mystery Casserole" with no other clue).

`provenance` (cultural/historical context):
- `ethnicity`: cultural/ethnic origin of the dish ("Italian-American", "Cajun", "Sichuan", "French"). Infer from technique or ingredients when no explicit label exists.
- `originRegion`: geographic region of origin ("Naples, Italy", "Louisiana, USA", "France"). Empty only when no regional signal exists.
- `firstDocumented`: approximate date or era if known ("19th century", "1950s"). Null is fine when truly unknown.
- `traditionalContext`: short paragraph on when/how the dish is traditionally eaten. Brief inference beats empty.
- `notableVariations`: list of well-known regional or family variations.
- `relatedDishes`: list of closely related dishes by name.
- `sources`: leave empty list; this is for citations added later.

`classification` (your confidence and reasoning):
- `confidence`: integer 0–100, reflecting how sure you are of the CUISINE-LEVEL provenance you're stating (broad regional/cultural origin), not of a specific city or chef.
    - 70+: well-documented dishes or clear technique/naming markers ("au gratin" → French, "tagine" → North African, "carbonara" → Roman, "tikka masala" → Indian-British, "miso" → Japanese).
    - 50–70: plausible inference from technique + corroborating ingredients, but technique alone is weak (e.g. "stew" or "casserole" — too generic).
    - 30–50: a single weak cue in the name (e.g. one ambiguous loanword); use sparingly.
    - <30: genuinely unidentifiable. Leave fields empty.
  Be willing to use 70+ when the technique is unambiguous. Don't hedge a clear cuisine signal down to 40 just because the family-recipe wrapping is unfamiliar.
- `reasoning`: one or two sentences explaining your provenance call. State explicitly when you're inferring vs. quoting from the source. Always populate when other fields have content.
- `hierarchyPath`: a slash-separated taxonomy path like "dessert/cookie/drop-cookie", "side/gratin/vegetable", "main/braise/stew". Provide whenever structural cues exist.
- `story`: one paragraph (2–4 sentences) telling the dish's story — its origin, what makes it distinctive, who eats it. Honest tone; don't fabricate. Provide for any recognizable cuisine.

EXAMPLE — "Asparagus au Gratin" appearing as just a recipe-card title with no explicit cuisine label should yield:
- ethnicity: "French"
- originRegion: "France"
- traditionalContext: brief note about gratin as a French baked-with-crust technique
- confidence: 70 (the technique marker is unambiguous at the cuisine level)
- reasoning: "'Au gratin' is a well-documented French cooking technique, so cuisine-level provenance is high-confidence; the specific regional origin within France is uncertain."
- hierarchyPath: "side/gratin/vegetable"
- story: short paragraph about gratin tradition
NOT all zeros / all empty.

OUTPUT RULES:
- Output a single valid JSON object matching the schema. No preamble, no fences, no commentary.
- Do NOT skip required fields. Use empty strings, empty lists, or null where appropriate.
- Don't fabricate specifics (e.g. invent a precise city or chef name). But DO infer at low confidence when there's any cuisine/technique signal — confidence 30–50 with populated fields beats confidence 0 with empties.

<SCHEMA>
{json.dumps(RecipeModel.model_json_schema(), indent=2)}
</SCHEMA>
""".strip()


def markdown_to_recipe(
    markdown_text: str,
    *,
    source_name: str = "",
    source_url: str = "",
    title: str = "",
    model: str = "gpt-4o-mini",
    timings: Optional[dict] = None,
    prompts: Optional[dict] = None,
    usage_log: Optional[list] = None,
) -> Optional[dict]:
    """Extract a full RecipeModel from canonical markdown in one LLM call.

    Caching lives at the endpoint layer (save_recipe_api.py) so that both
    this big-prompt path and the JSON-LD fast lane (jsonld_to_recipe +
    enrich_recipe) share one cache keyed by URL. Endpoints look up before
    calling this and write after.

    Args:
        markdown_text:  Canonical markdown (output of a to_markdown adapter).
        source_name:    Filename or source identifier for traceability.
        source_url:     Original URL the markdown was derived from, if any.
        title:          Page/source title for `_scoring.rawTitle` fallback.
        model:          OpenAI model id.
        timings:        Optional dict, populated in place with prep_ms,
                        extract_llm_ms, validate_ms.
        prompts:        Optional dict, populated in place with model,
                        system_prompt, user_prompt. Lets the UI surface
                        exactly what was sent to the LLM.
        usage_log:      Optional list — appended with one dict
                        (operation/model/input_tokens/output_tokens/meta)
                        so the caller can journal token usage.

    Returns:
        Validated RecipeModel as a dict (by_alias), or None if parsing failed.
    """
    t0 = time.perf_counter()
    cleaned_md = clean_markdown(markdown_text)

    validation = is_recipe(cleaned_md)
    print(f"     VALIDATE: {validation['reason']} -> "
          f"{'accepted' if validation['accepted'] else 'rejected (proceeding anyway)'}")

    user_prompt = (
        "Extract a complete structured recipe from this markdown. "
        "Return strict JSON.\n\n"
        f"<MARKDOWN>\n{cleaned_md}\n</MARKDOWN>"
    )

    if prompts is not None:
        prompts["model"] = model
        prompts["system_prompt"] = SYSTEM_PROMPT
        prompts["user_prompt"] = user_prompt

    t_prep = time.perf_counter()
    if timings is not None:
        timings["prep_ms"] = int((t_prep - t0) * 1000)

    response = openai.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4096,
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    if usage_log is not None:
        usage_log.append(build_usage_entry("markdown_to_recipe", model, response))

    content = response.choices[0].message.content
    try:
        json_data = json.loads(content)
    except Exception as e:
        print("     ERROR: Failed to parse GPT JSON:", e)
        print("     DEBUG: Raw output:\n", content)
        return None

    t_llm = time.perf_counter()
    if timings is not None:
        timings["extract_llm_ms"] = int((t_llm - t_prep) * 1000)

    _attach_source_metadata(json_data, source_url=source_url, title=title)

    try:
        sanitized = sanitize_recipe_data(json_data)
        if source_name:
            sanitized["inputImage"] = source_name
        stamp_validation_on_recipe(sanitized, validation)
        recipe = RecipeModel.model_validate(sanitized).model_dump(by_alias=True)
        if timings is not None:
            timings["validate_ms"] = int((time.perf_counter() - t_llm) * 1000)
        return recipe
    except Exception as e:
        print("     ERROR: Failed to validate against RecipeModel:", e)
        print("     DEBUG: Sanitized payload:\n", json.dumps(json_data, indent=2)[:2000])
        return None


def _attach_source_metadata(json_data: dict, *, source_url: str, title: str) -> None:
    """Stamp _source + _scoring with normalized URL / origin / rawTitle.

    Same logic as the legacy `extract_content_markdown.py`, kept here so the
    new canonical extract owns the contract end-to-end.
    """
    normalized = normalize_url(source_url) if source_url else ""
    if not (normalized or title):
        return

    existing_source = json_data.get("_source") or {}
    if normalized and not existing_source.get("originalUrl"):
        existing_source["originalUrl"] = normalized
    origin = existing_source.get("origin") or root_domain(normalized) or title
    if origin:
        existing_source["origin"] = origin
    existing_source.setdefault("type", "web")
    json_data["_source"] = existing_source

    if normalized:
        scoring = json_data.get("_scoring") or {}
        scoring.setdefault("rootDomain", root_domain(normalized))
        if title:
            scoring.setdefault("rawTitle", title)
        json_data["_scoring"] = scoring


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m extract.markdown_to_recipe <markdown_file> [source_url]")
        sys.exit(1)

    with open(sys.argv[1], "r", encoding="utf-8") as f:
        md = f.read()
    src_url = sys.argv[2] if len(sys.argv) > 2 else ""

    result = markdown_to_recipe(md, source_name=os.path.basename(sys.argv[1]),
                                source_url=src_url)
    if result is None:
        print("FAILED")
        sys.exit(1)
    print(json.dumps(result, indent=2))