"""Small LLM call that fills only `provenance` and `classification` on a
recipe that already has its standard fields populated (typically from a
JSON-LD direct parse).

This is the *enrichment* half of the JSON-LD fast lane:

    JSON-LD direct parse  →  enrich_recipe  →  full RecipeModel
    (no LLM, ~1ms)           (small LLM,         (same shape as the
                              ~1–3 s)             big-prompt path)

The prompt is tiny on purpose — we send the dish name, a short ingredient
sample, description, and any cuisine/category hints, NOT the full schema or
the page markdown. That's the whole performance win: ~25 s of token churn
becomes ~2 s of focused inference.
"""
import json
import os
import time
from typing import Optional

import openai


openai.api_key = os.getenv("OPENAI_API_KEY")


SYSTEM_PROMPT = """
You are a culinary historian. Given a dish's name, ingredients, and any
cuisine/category hints, infer cultural provenance and a confidence-scored
classification. Return STRICT JSON matching this shape exactly:

{
  "provenance": {
    "ethnicity":          "<cultural/ethnic origin, e.g. 'Italian-American', 'Cajun', 'Sichuan'. Empty string if uncertain.>",
    "originRegion":       "<geographic origin, e.g. 'Naples, Italy', 'Louisiana, USA'. Empty if uncertain.>",
    "firstDocumented":    "<approximate date or era, e.g. '19th century', '1930s'. null if unknown.>",
    "traditionalContext": "<one-paragraph note on when/how the dish is traditionally eaten. Empty if uncertain.>",
    "notableVariations":  ["<well-known regional or family variations>"],
    "relatedDishes":      ["<closely related dishes by name>"],
    "sources":            []
  },
  "classification": {
    "confidence":   <integer 0-100. <40 for novel/unknown dishes, 40-70 for plausible inference, 70+ only for well-documented classics>,
    "reasoning":    "<one or two sentences explaining your provenance call. State explicitly when you're inferring vs. quoting from the input.>",
    "hierarchyPath":"<slash-separated taxonomy path like 'dessert/cookie/drop-cookie' or 'main/braise/stew'. Empty if unclear.>",
    "story":        "<one paragraph (2-4 sentences) telling the dish's story — origin, what makes it distinctive, who eats it. Honest tone; don't fabricate. Empty string if you have nothing real to say.>"
  }
}

Honesty over completeness — low confidence + empty fields beats a confident fabrication.
Output ONLY the JSON object. No preamble, no commentary, no fences.
""".strip()


def _build_user_prompt(recipe: dict) -> str:
    """Compact context for the model. Avoids dumping anything we don't need."""
    name = (recipe.get("name") or "").strip()
    description = (recipe.get("description") or "").strip()
    cuisine = (recipe.get("recipeCuisine") or "").strip()
    category = (recipe.get("recipeCategory") or "").strip()
    ingredients = recipe.get("recipeIngredient") or []
    # Cap at 12 to keep the prompt small; ingredients are all the signal the
    # model needs for cultural attribution.
    ing_sample = ingredients[:12]

    lines = [f"Dish name: {name}"]
    if description:
        lines.append(f"Description: {description}")
    if cuisine:
        lines.append(f"Cuisine label from source: {cuisine}")
    if category:
        lines.append(f"Category label from source: {category}")
    if ing_sample:
        lines.append("Ingredients:")
        for ing in ing_sample:
            lines.append(f"  - {ing}")
        if len(ingredients) > len(ing_sample):
            lines.append(f"  (+ {len(ingredients) - len(ing_sample)} more)")
    return "\n".join(lines)


def enrich_recipe(
    recipe: dict,
    *,
    model: str = "gpt-4o-mini",
    timings: Optional[dict] = None,
    prompts: Optional[dict] = None,
) -> dict:
    """Mutate `recipe` in place with inferred provenance + classification.

    Returns the same recipe dict. If the LLM call fails, leaves the existing
    provenance/classification blocks untouched (sanitize will have given them
    sensible defaults already).
    """
    t0 = time.perf_counter()
    user_prompt = _build_user_prompt(recipe)

    if prompts is not None:
        prompts["model"] = model
        prompts["system_prompt"] = SYSTEM_PROMPT
        prompts["user_prompt"] = user_prompt

    t_prep = time.perf_counter()
    if timings is not None:
        timings["enrich_prep_ms"] = int((t_prep - t0) * 1000)

    try:
        response = openai.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=800,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        print(f"     ENRICH: LLM call failed ({e}); leaving defaults")
        if timings is not None:
            timings["enrich_llm_ms"] = int((time.perf_counter() - t_prep) * 1000)
        return recipe

    if timings is not None:
        timings["enrich_llm_ms"] = int((time.perf_counter() - t_prep) * 1000)

    content = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(content)
    except Exception as e:
        print(f"     ENRICH: failed to parse JSON ({e}); leaving defaults")
        print(f"     DEBUG raw: {content[:500]}")
        return recipe

    if isinstance(parsed.get("provenance"), dict):
        recipe["provenance"] = parsed["provenance"]
    if isinstance(parsed.get("classification"), dict):
        recipe["classification"] = parsed["classification"]
    return recipe


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python -m extract.enrich_recipe <recipe.json>")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        recipe = json.load(f)
    timings: dict = {}
    enriched = enrich_recipe(recipe, timings=timings)
    print("timings:", timings)
    print(json.dumps({"provenance": enriched.get("provenance"),
                      "classification": enriched.get("classification")}, indent=2))