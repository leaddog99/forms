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


# Strict JSON-Schema response_format. This FORCES the model to produce
# every required field as a separate string, instead of jamming everything
# into one field (which gpt-4o-mini does at the slightest excuse with
# response_format=json_object). All fields must be required and
# additionalProperties must be false for strict mode to validate.
RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "RecipeEnrichment",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["provenance", "classification", "editorial"],
            "properties": {
                "provenance": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "ethnicity", "originRegion", "firstDocumented",
                        "traditionalContext", "notableVariations",
                        "relatedDishes", "sources",
                    ],
                    "properties": {
                        "ethnicity": {"type": "string"},
                        "originRegion": {"type": "string"},
                        "firstDocumented": {"type": ["string", "null"]},
                        "traditionalContext": {"type": "string"},
                        "notableVariations": {"type": "array", "items": {"type": "string"}},
                        "relatedDishes": {"type": "array", "items": {"type": "string"}},
                        "sources": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "classification": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["confidence", "reasoning", "hierarchyPath", "story"],
                    "properties": {
                        "confidence": {"type": "integer"},
                        "reasoning": {"type": "string"},
                        "hierarchyPath": {"type": "string"},
                        "story": {"type": "string"},
                    },
                },
                "editorial": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["opinion", "scoreCommentary", "sourcingNotes"],
                    "properties": {
                        "opinion": {"type": "string"},
                        "scoreCommentary": {"type": "string"},
                        "sourcingNotes": {"type": "string"},
                    },
                },
            },
        },
    },
}


SYSTEM_PROMPT = """
You are a culinary historian AND an opinionated food editor. Given a
dish's name, ingredients, cuisine hints, and (when available) the page's
authority scores, you produce THREE outputs the user values equally:

  (A) Structured provenance / classification metadata.
  (B) A substantive multi-paragraph STORY about the dish.
  (C) An EDITORIAL block: your opinion of this specific recipe, prose
      commentary on its SEO/authority scores, and sourcing notes for the
      handful of ingredients where quality dominates outcome.

== CRITICAL: THE STORY FIELD ==
The `classification.story` field is the main human-facing deliverable.
It is NOT a one-paragraph blurb. It MUST be:

  • 150 to 300 words total — count them.
  • Split into 3 to 5 short paragraphs.
  • Paragraphs separated by a literal "\\n\\n" (two newline characters
    inside the JSON string value).
  • Cover, in roughly this order:
      1. Origin & history — when/where the dish emerged, its lineage.
      2. Geography & culture — region, local ingredients/foodways that
         shaped it, why it belongs to that place.
      3. Traditional usage — meal type, season, occasion, who cooks it.
      4. Modern usage & spread — how it's prepared inside and outside
         its home region today; diaspora variations if any.
      5. Notable variations or widely-recognized renditions.

Hedge with "likely", "tradition holds", "commonly attributed to" when
inferring rather than quoting a fact. DO NOT invent specific chefs,
restaurants, or dates — omit rather than guess. For genuinely obscure
dishes, write a shorter honest paragraph saying so rather than padding.

A 3-sentence single-paragraph story is WRONG and will be rejected.

== STRUCTURED METADATA ==
Make a best-effort inference from ANY signal: dish name, cooking
technique (e.g. "au gratin" → French, "tagine" → North African,
"carbonara" → Roman), key ingredients, naming convention. Leaving a
field empty signals "no signal at all" — reserve that for genuinely
unidentifiable dishes.

Return STRICT JSON matching this shape exactly:

{
  "provenance": {
    "ethnicity":          "<cultural/ethnic origin, e.g. 'Italian-American', 'Cajun', 'Sichuan'. Infer from technique/ingredients when no explicit label.>",
    "originRegion":       "<geographic origin, e.g. 'Naples, Italy', 'Louisiana, USA'. Empty only when no regional signal.>",
    "firstDocumented":    "<approximate date or era, e.g. '19th century', '1930s'. null if truly unknown.>",
    "traditionalContext": "<one-paragraph note on when/how the dish is traditionally eaten. Brief inference beats empty.>",
    "notableVariations":  ["<well-known regional or family variations>"],
    "relatedDishes":      ["<closely related dishes by name>"],
    "sources":            []
  },
  "classification": {
    "confidence":   <integer 0-100. 70+ for unambiguous technique markers, 50-70 for technique + corroborating ingredients, 30-50 for a single weak cue, <30 only for genuinely unidentifiable.>,
    "reasoning":    "<one or two sentences explaining your provenance call.>",
    "hierarchyPath":"<slash-separated taxonomy like 'side/gratin/vegetable', 'main/braise/stew'.>",
    "story":        "<150-300 words across 3-5 paragraphs separated by \\n\\n. See the STORY rules above.>"
  },
  "editorial": {
    "opinion":         "<2-3 short paragraphs (separate with \\n\\n), roughly 100-200 words. Editorial take on THIS specific recipe — technique choices, ingredient ratios, what makes it work or wobble, who would love it. Concrete and opinionated. Not 'this is a classic dish' filler — comment on what the cook in front of this recipe is actually being asked to do.>",
    "scoreCommentary": "<1-2 short paragraphs interpreting the PA/DA/OU scores in plain language. PA is the page's Moz authority (0-100); DA is the domain's; OU = -3.0273 * DA^0.6034 + PA, which is positive when the page out-performs its domain baseline and negative when it under-performs. Translate the numbers into a reader-facing observation: 'this is a small/niche food blog (DA=X) but the page is punching above its weight (OU=+Y)', 'a well-established outlet (DA=X) and the page reflects that pedigree', 'mainstream domain but this particular page hasn't gained traction'. If scores are missing/zero (the recipe wasn't scored), say so and keep it short — don't fabricate authority claims.>",
    "sourcingNotes":   "<Markdown bullet list. Pick 2-5 ingredients where quality dominates outcome (raw oils, fresh herbs, aged cheeses, anchovies, vanilla, etc.). For each, one bullet of the form '- **Ingredient name**: why quality matters here, what to look for, descriptive sourcing guidance (origin, style, hallmarks of the good stuff). DO NOT invent brand names, shop names, or URLs — those will be layered in later from a curated affiliate database. Keep the descriptive sourcing language so it stays useful even without links.>"
  }
}

== EXAMPLE STORY (for length and tone calibration) ==
For a dish like "Asparagus au Gratin", a CORRECT story field value
looks like this (between the markers):

>>>BEGIN EXAMPLE STORY>>>
Gratins are a defining technique of French home cooking, traceable to
the 18th-century kitchens of the Dauphiné region where cooks layered
sliced vegetables with cream and breadcrumbs to make humble produce
keep longer and taste richer. The word itself comes from the French
verb gratter — to scrape — referring to the browned crust that forms
on top.

Asparagus arrived in this tradition somewhat later, prized as a
seasonal luxury in the Loire Valley and around Paris, where white
asparagus in particular became a springtime ritual. The combination of
the vegetable's sweetness with a creamy béchamel and a crisp gruyère
topping is now classical, but it began as a way to stretch a fleeting
crop into something celebratory.

Traditionally the dish is served as a starter or vegetable course at
Sunday lunch in spring, often alongside roast lamb or veal. Outside
France it has become a standard of bistro menus and home dinner
parties, sometimes lightened with crème fraîche, sometimes enriched
with ham or shaved truffle for a holiday version.

Modern variants run the gamut from purist (asparagus, butter,
breadcrumbs) to elaborate (multiple cheeses, leeks, pancetta), and the
dish translates easily to other tender vegetables — fennel, leeks,
endive — using the same gratin grammar.
<<<END EXAMPLE STORY<<<

That story is roughly 230 words across four paragraphs. Match that
density and structure for the dish you are given.

== OUTPUT RULES ==
Output ONLY the JSON object. No preamble, no commentary, no fences,
no markdown — pure JSON only.
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

    # Moz / page-authority scores for the editorial.scoreCommentary field.
    # We only include scores that have a real (non-zero) value so the model
    # doesn't fabricate commentary on a missing measurement.
    scoring = recipe.get("_scoring") or recipe.get("scoring") or {}
    pa = scoring.get("pageAuthority")
    da = scoring.get("domainAuthority")
    ou = scoring.get("ouScore")
    root_domain = scoring.get("rootDomain") or ""
    score_lines = []
    if pa is not None and float(pa) > 0:
        score_lines.append(f"  PA (page authority, 0-100): {float(pa):.1f}")
    if da is not None and float(da) > 0:
        score_lines.append(f"  DA (domain authority, 0-100): {float(da):.1f}")
    if ou is not None:
        # OU can be negative (page under-performs domain) or positive (over-
        # performs). 0 is meaningful too, so include it whenever it's set.
        score_lines.append(f"  OU (page-vs-domain over/under-performance, +/-): {float(ou):.1f}")
    if root_domain:
        score_lines.append(f"  Root domain: {root_domain}")
    if score_lines:
        lines.append("Page authority scores (for editorial.scoreCommentary):")
        lines.extend(score_lines)
    else:
        lines.append(
            "Page authority scores: NOT AVAILABLE — keep editorial.scoreCommentary brief and "
            "do not fabricate authority claims."
        )

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
            # Story (~400) + provenance (~150) + classification (~50) +
            # editorial.opinion (~250) + editorial.scoreCommentary (~150) +
            # editorial.sourcingNotes (~300) ≈ 1300. Strict JSON schema
            # adds structural overhead AND requires all sub-fields populated;
            # 4000 gives comfortable headroom so we never truncate mid-string
            # (truncation makes invalid JSON → parse failure → empty defaults).
            max_tokens=4000,
            temperature=0.4,
            response_format=RESPONSE_SCHEMA,
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
        # Useful diagnostics on parse fail. Print TAIL not just head — the
        # most common failure is truncation, which manifests at the end.
        print(f"     ENRICH: failed to parse JSON ({e}); leaving defaults")
        print(f"     DEBUG raw len={len(content)} finish_reason={response.choices[0].finish_reason}")
        print(f"     DEBUG head: {content[:300]}")
        print(f"     DEBUG tail: ...{content[-300:]}")
        return recipe

    if isinstance(parsed.get("provenance"), dict):
        recipe["provenance"] = parsed["provenance"]
    if isinstance(parsed.get("classification"), dict):
        recipe["classification"] = parsed["classification"]
    if isinstance(parsed.get("editorial"), dict):
        recipe["editorial"] = parsed["editorial"]
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