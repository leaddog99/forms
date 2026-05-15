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
from input.pipeline.extract_cache import (
    hash_text,
    prompt_version_for,
    get_cached_extract,
    set_cached_extract,
)


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

ENRICHMENT FIELDS — fill these whether JSON-LD is present or not:

`provenance` (cultural/historical context):
- `ethnicity`: cultural/ethnic origin of the dish ("Italian-American", "Cajun", "Sichuan"). Empty string if uncertain.
- `originRegion`: geographic region of origin if known ("Naples, Italy", "Louisiana, USA"). Empty if uncertain.
- `firstDocumented`: approximate date or era if known ("19th century", "1950s"). Null if unknown.
- `traditionalContext`: short paragraph on when/how the dish is traditionally eaten. Empty if uncertain.
- `notableVariations`: list of well-known regional or family variations.
- `relatedDishes`: list of closely related dishes by name.
- `sources`: leave empty list; this is for citations added later.

`classification` (your confidence and reasoning):
- `confidence`: integer 0–100. Your confidence that the provenance fields above are accurate. Use < 40 for novel/unknown dishes, 40–70 for plausible inference, 70+ only for well-documented classics.
- `reasoning`: one or two sentences explaining your provenance call. State explicitly when you're inferring vs. quoting from the source.
- `hierarchyPath`: a slash-separated taxonomy path like "dessert/cookie/drop-cookie" or "main/braise/stew". Empty if unclear.
- `story`: one paragraph (2–4 sentences) telling the dish's story — its origin, what makes it distinctive, who eats it. Honest tone; don't fabricate. Empty string if you have nothing real to say.

OUTPUT RULES:
- Output a single valid JSON object matching the schema. No preamble, no fences, no commentary.
- Do NOT skip required fields. Use empty strings, empty lists, or null where appropriate.
- Honesty over completeness on provenance/classification: low confidence + empty fields is better than a confident fabrication.

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
    cache_db_path: Optional[str] = None,
) -> Optional[dict]:
    """Extract a full RecipeModel from canonical markdown in one LLM call.

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
    # Cache key components. URL is the partition; markdown hash captures
    # actual content; prompt_version auto-bumps when SYSTEM_PROMPT changes.
    md_hash = hash_text(cleaned_md)
    pv = prompt_version_for(SYSTEM_PROMPT)
    url_norm = normalize_url(source_url) if source_url else ""

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

    # Cache lookup. Same (url, markdown, model, prompt) => same LLM output.
    # On hit, skip the LLM call and journal as cache_hit_markdown_to_recipe
    # with zero tokens so usage queries can surface "tokens saved" later.
    json_data = None
    cache_status = "skip"  # 'hit' / 'miss' / 'skip' (no URL or no db path)
    if cache_db_path and url_norm:
        cached = get_cached_extract(
            cache_db_path,
            url_normalized=url_norm,
            markdown_hash=md_hash,
            model=model,
            prompt_version=pv,
        )
        if cached:
            json_data = cached["llm_output"]
            cache_status = "hit"
            print(f"     CACHE HIT: cached_at={cached['cached_at']}")
            if usage_log is not None:
                usage_log.append({
                    "operation": "cache_hit_markdown_to_recipe",
                    "model": model,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "meta": {
                        "cache_key_url": url_norm,
                        "cache_markdown_hash": md_hash[:16],
                        "cached_at": cached["cached_at"],
                    },
                })
        else:
            cache_status = "miss"

    if json_data is None:
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

        # Store the raw LLM JSON output (pre-sanitize, pre-source-stamping)
        # so subsequent reads can apply fresh source_url/title without
        # invalidating the cache on those purely-metadata changes.
        if cache_db_path and url_norm:
            set_cached_extract(
                cache_db_path,
                url_normalized=url_norm,
                markdown_hash=md_hash,
                model=model,
                prompt_version=pv,
                llm_output=json_data,
            )

    t_llm = time.perf_counter()
    if timings is not None:
        timings["extract_llm_ms"] = 0 if cache_status == "hit" else int((t_llm - t_prep) * 1000)
        timings["cache"] = cache_status

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