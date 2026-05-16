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
classification.

Make a best-effort inference from ANY signal: dish name, cooking
technique (e.g. "au gratin" → French, "tagine" → North African,
"carbonara" → Roman), key ingredients, naming convention. Leaving a
field empty signals "no signal at all" — reserve that for genuinely
unidentifiable dishes.

Return STRICT JSON matching this shape exactly:

{
  "provenance": {
    "ethnicity":          "<cultural/ethnic origin, e.g. 'Italian-American', 'Cajun', 'Sichuan', 'French'. Infer from technique/ingredients when no explicit label.>",
    "originRegion":       "<geographic origin, e.g. 'Naples, Italy', 'Louisiana, USA', 'France'. Empty only when no regional signal.>",
    "firstDocumented":    "<approximate date or era, e.g. '19th century', '1930s'. null if truly unknown.>",
    "traditionalContext": "<one-paragraph note on when/how the dish is traditionally eaten. Brief inference beats empty.>",
    "notableVariations":  ["<well-known regional or family variations>"],
    "relatedDishes":      ["<closely related dishes by name>"],
    "sources":            []
  },
  "classification": {
    "confidence":   <integer 0-100. 30-50 for inferences from dish name/technique alone, 50-70 when corroborated by ingredients, 70+ for well-documented classics. <30 only for genuinely unidentifiable.>,
    "reasoning":    "<one or two sentences explaining your provenance call. State explicitly when you're inferring vs. quoting. Always populate when other fields have content.>",
    "hierarchyPath":"<slash-separated taxonomy path like 'dessert/cookie/drop-cookie', 'side/gratin/vegetable', 'main/braise/stew'. Provide whenever structural cues exist.>",
    "story":        "<one paragraph (2-4 sentences) telling the dish's story — origin, what makes it distinctive, who eats it. Honest tone; don't fabricate. Provide for any recognizable cuisine.>"
  }
}

Example: "Asparagus au Gratin" with no explicit cuisine label should
yield ethnicity="French", originRegion="France", hierarchyPath=
"side/gratin/vegetable", confidence=40, with a brief story about French
gratin tradition. NOT all-zeros.

Don't fabricate specifics (precise city, named chef) when only the
cuisine is clear. But DO infer at low confidence when there's any
signal — confidence 30-50 with populated fields beats confidence 0
with empties.
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
    usage_log: Optional[list] = None,
) -> dict:
    """Mutate `recipe` in place with inferred provenance + classification.

    Returns the same recipe dict. If the LLM call fails, leaves the existing
    provenance/classification blocks untouched (sanitize will have given them
    sensible defaults already). If `usage_log` is provided, one entry is
    appended on success so the caller can journal token usage.
    """
    from input.pipeline.token_journal import build_usage_entry

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
    if usage_log is not None:
        usage_log.append(build_usage_entry("enrich_recipe", model, response))

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