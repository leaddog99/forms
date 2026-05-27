"""gpt-image-1 dish-image generation.

The lone OpenAI-API-using module in the codebase post-Anthropic-migration —
Anthropic doesn't offer image generation, and we need a way to produce
dish photography for recipes that don't have a source image (typed-in
recipes, claimed-and-edited, cookbook export). Restored from git history
(commit 143e016^, deleted 2026-05-15 during the canonical-extract
cleanup, re-added 2026-05-26) and extended to read quality/size from
bcc_config.json so cookbook-print jobs can request high quality without
flipping the default.

OpenAI deprecated dall-e-3 and replaced it with gpt-image-1; the
quality vocabulary changed too (low/medium/high/auto, not standard/hd),
and the API now returns base64-encoded bytes inline (no separate URL
download step). The bcc_config.json keys still use the
"standard"/"hd" names for caller stability — we map them to the
gpt-image-1 quality values below.

Cost reference (gpt-image-1, OpenAI 2026 pricing per 1024x1024):
  low     ≈ $0.011
  medium  ≈ $0.042   ("standard" default — matches the prior dall-e-3 standard)
  high    ≈ $0.167   ("hd" for cookbook-print-ready output)

Per-call overrides take precedence over the bcc_config.json defaults
so callers (e.g. a future cookbook export job) can pin "hd" without
flipping the global.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

# Add project root to sys.path so `input.pipeline.config` resolves when
# this module is imported from anywhere in the repo. Mirrors the same
# preamble used by intake/build_query_batch.py.
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import base64

import openai

from input.pipeline.config import IMAGE_GEN_QUALITY, IMAGE_GEN_SIZE


client = openai.OpenAI()

# Map our config's quality vocabulary (kept for caller stability across
# OpenAI's model renamings) to gpt-image-1's quality vocabulary.
_QUALITY_MAP = {
    "standard": "medium",
    "hd":       "high",
    "low":      "low",
    "medium":   "medium",
    "high":     "high",
    "auto":     "auto",
}

# gpt-image-1 accepts a different size set than dall-e-3 did. Map the
# legacy values forward and leave already-valid ones alone.
_SIZE_MAP = {
    "1792x1024": "1536x1024",  # landscape: dall-e-3 -> gpt-image-1
    "1024x1792": "1024x1536",  # portrait:  dall-e-3 -> gpt-image-1
    "1024x1024": "1024x1024",
    "1536x1024": "1536x1024",
    "1024x1536": "1024x1536",
}


def _generate_image(
    prompt: str,
    *,
    quality: Optional[str] = None,
    size: Optional[str] = None,
) -> bytes:
    """Call gpt-image-1, return raw image bytes.

    quality/size default to the values in bcc_config.json; callers can
    pin per-call (e.g. cookbook export forces quality='hd'). Returns
    bytes directly — gpt-image-1 ships base64-encoded image data in
    the response (no separate URL fetch step like dall-e-3 needed)."""
    cfg_q = quality or IMAGE_GEN_QUALITY
    cfg_s = size or IMAGE_GEN_SIZE
    api_q = _QUALITY_MAP.get(cfg_q, "medium")
    api_s = _SIZE_MAP.get(cfg_s, "1024x1024")
    response = client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size=api_s,
        quality=api_q,
        n=1,
    )
    b64 = response.data[0].b64_json
    if not b64:
        raise RuntimeError("gpt-image-1 returned no image data")
    return base64.b64decode(b64)


def _get(obj, key, default=""):
    """Read `key` from either a dict or an object — lets callers pass
    a sanitized recipe dict OR a RecipeModel instance interchangeably."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# Per-chapter scene/environment cues — what gets in the frame around
# the dish to give it context, story, and warmth. Lifted from how the
# big food publications stage similar categories (NYT Cooking, Bon
# Appétit, Saveur). Keys are lowercased to match against
# classification.chapter case-insensitively.
_SCENE_BY_CHAPTER = {
    "soups & stews": (
        "served in a rustic earthenware or enameled cast iron bowl on a warm "
        "wooden farmhouse table, with a slice of crusty bread on the side, "
        "a small linen napkin in the corner of the frame, soft steam rising"
    ),
    "meat": (
        "on a butcher-block surface or rustic ceramic platter in a warm "
        "country kitchen, with sprigs of fresh herbs and a small ramekin of "
        "flaked salt nearby, a few seasonal roasted vegetables in soft focus "
        "in the background"
    ),
    "poultry": (
        "on a warm wooden surface in a farmhouse kitchen, with fresh thyme "
        "and rosemary nearby, a small carafe of pan juices, soft window light"
    ),
    "fish & shellfish": (
        "coastal kitchen aesthetic, light marble or pale wood surface, a "
        "lemon wedge and fresh dill or parsley nearby, a glass of white wine "
        "out of focus in the background"
    ),
    "vegetables": (
        "on a sunlit farmhouse table, wooden surface, scattered fresh herbs, "
        "a small dish of olive oil, additional produce in soft focus nearby"
    ),
    "salads": (
        "in a wide wooden or ceramic bowl, with scattered ingredients on the "
        "wood surface around it, a linen napkin, wooden serving utensils"
    ),
    "breads": (
        "on a rustic wooden cutting board lightly dusted with flour, in a "
        "warm country kitchen, a kitchen towel draped nearby, perhaps a "
        "small dish of butter or honey beside it"
    ),
    "cakes": (
        "on a vintage cake stand by a sunlit window, a slice cut out showing "
        "the interior crumb, a dessert fork on a small plate nearby, soft "
        "afternoon light"
    ),
    "cookies & bars": (
        "on a parchment-lined wooden tray, with a glass of milk or a cup of "
        "tea nearby, a few extra cookies arranged casually in the background"
    ),
    "pies & tarts": (
        "on a wooden surface or vintage pie stand by a sunlit window, a "
        "slice cut showing the interior, a fork on a small plate, a dollop "
        "of whipped cream or scoop of ice cream nearby if appropriate"
    ),
    "pasta & noodles": (
        "on a wide rim plate, fork twirled into the pasta, fresh basil or "
        "parsley garnish, a wooden grater of Parmesan and a glass of red "
        "wine out of focus in the background"
    ),
    "rice & grains": (
        "in a warm earthenware bowl on a wooden surface, garnishes of fresh "
        "herbs or aromatics scattered around the bowl"
    ),
    "beans, legumes & tofu": (
        "in a rustic ceramic bowl on a warm wooden surface, with fresh herb "
        "garnish and small dishes of accompaniments nearby"
    ),
    "eggs & breakfast": (
        "in soft morning light on a simple ceramic plate, with a cup of "
        "coffee or glass of orange juice out of focus in the background, "
        "linen napkin"
    ),
    "sandwiches, pizza & savory pastry": (
        "on a wooden cutting board or simple ceramic plate, casual "
        "presentation, a small dish of pickles, olives, or chips nearby"
    ),
    "appetizers & starters": (
        "on a small elegant plate, a wine glass or cocktail out of focus "
        "in the background, a linen napkin, intimate composition"
    ),
    "sauces, dressings & condiments": (
        "in a small ceramic sauce dish or jar on a wooden surface, with the "
        "intended accompaniment (a piece of bread, pasta, or roasted dish) "
        "visible in soft focus nearby"
    ),
    "beverages & cocktails": (
        "in appropriate glassware with proper garnish, on a bar or rustic "
        "wooden surface, soft mood lighting, additional ingredients or "
        "barware in the soft-focus background"
    ),
    "frozen desserts": (
        "in a chilled coupe or rustic ceramic bowl, slight condensation on "
        "the vessel, fresh fruit or garnish nearby, cool soft light"
    ),
    "fruit desserts": (
        "in a rustic ceramic vessel on a sunlit surface, with fresh fruit "
        "and a sprig of mint nearby"
    ),
    "custards, puddings & mousses": (
        "in a small ramekin or elegant glass vessel, with a delicate "
        "garnish, soft warm light"
    ),
    "candies & confections": (
        "on a small ceramic plate or in a vintage tin, soft warm light, "
        "elegant restrained presentation"
    ),
    "preserving & pickling": (
        "in glass jars on a wooden farmhouse surface, with fresh ingredients "
        "and herbs arranged beside them"
    ),
}
_DEFAULT_SCENE = (
    "on a warm wooden surface in a country kitchen, with simple props "
    "(linen napkin, a few complementary ingredients) arranged casually nearby"
)


def _scene_for(chapter: str) -> str:
    if not chapter:
        return _DEFAULT_SCENE
    return _SCENE_BY_CHAPTER.get(chapter.strip().lower(), _DEFAULT_SCENE)


def _clean_ingredient(ing: str) -> str:
    """Strip leading quantity + first comma-clause so the ingredient
    reads as visual content ("ground turkey" not "1 lb ground turkey,
    room temperature")."""
    s = str(ing).split(",", 1)[0].strip()
    # Drop leading "1 ", "2 tablespoons", "½ cup" style quantity prefixes
    import re as _re
    s = _re.sub(r"^[\d¼-¾⅐-⅞↉\.\-\/\s]+", "", s)
    for token in ("tablespoons", "tablespoon", "tbsp", "teaspoons",
                  "teaspoon", "tsp", "cups", "cup", "ounces", "ounce",
                  "oz", "pounds", "pound", "lbs", "lb", "grams", "g",
                  "kilograms", "kg", "milliliters", "ml", "liters", "l"):
        if s.lower().startswith(token + " "):
            s = s[len(token) + 1:].strip()
            break
    # Drop trailing parenthetical (e.g. "(approximately ½ cup)")
    s = _re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    return s


def _build_dish_prompt(recipe, extra_prompt: str = "") -> str:
    """Build a flowing editorial-photography brief from whatever recipe
    fields we have. Restructured 2026-05-26 away from the "Description:
    / Cuisine: / Category:" labeled-dump style — gpt-image-1 reads that
    as a database row, not a photograph brief, and produces flat, fake-
    looking results. The new structure: one paragraph framing the
    subject + scene, one paragraph specifying the photographic
    technique, one closing anti-CGI clause. Strong instructions live
    at the END because gpt-image-1 weights trailing tokens heavily.

    extra_prompt: optional user-supplied override. Appended at the very
    end with strong "IMPORTANT" framing so it dominates any conflicts
    with the auto-built sections (e.g. "carrots should be shredded not
    chunked" wins over the LLM's default rendering)."""
    name = _get(recipe, "name") or "the dish"
    description = (_get(recipe, "description") or "").strip()
    cuisine = (_get(recipe, "recipeCuisine") or "").strip()
    classification = _get(recipe, "classification") or {}
    chapter = (_get(classification, "chapter") or "").strip()
    reasoning = (_get(classification, "reasoning") or "").strip()
    ingredients = _get(recipe, "recipeIngredient") or []

    # Subject + identity phrase — natural prose, no labels.
    subject_bits = [name]
    if cuisine:
        subject_bits.append(f"a {cuisine.lower()} dish")
    if chapter:
        subject_bits.append(f"from the {chapter.lower()} category")
    subject = ", ".join(subject_bits)

    # Visible ingredients summary — flows into the description.
    top = [_clean_ingredient(i) for i in ingredients[:5]]
    top = [t for t in top if t]
    ingredients_clause = (
        f" Visible elements include {', '.join(top[:-1])}, and {top[-1]}."
        if len(top) > 1 else (f" Featuring {top[0]}." if top else "")
    )

    # Cultural / technique context — keep as one sentence, dropped if
    # absent. The full reasoning blob is verbose; take the first
    # sentence to anchor without flooding.
    technique_clause = ""
    if reasoning:
        first_sentence = reasoning.split(". ")[0].strip().rstrip(".")
        if first_sentence:
            technique_clause = f" {first_sentence}."

    scene = _scene_for(chapter)

    # Paragraph 1 — what + where (subject, story, scene).
    para1 = (
        f"An editorial food photograph in the style of premium cookbook "
        f"publications (Bon Appétit, NYT Cooking, Saveur). The subject is "
        f"{subject}.{(' ' + description) if description else ''}"
        f"{ingredients_clause}{technique_clause} The dish is presented "
        f"{scene}."
    )

    # Paragraph 2 — how it's shot (technique, anti-CGI cues at the end).
    para2 = (
        "Shot on a 50mm lens with shallow depth of field, soft natural "
        "window light from one side casting gentle directional shadows, "
        "a warm but realistic color palette, slight film grain, restrained "
        "and considered composition. The image should read as REAL food "
        "photography — a tangible plate of food on a real surface in a real "
        "kitchen — not a 3D render, not a digital illustration, not an "
        "overly polished stock photo. No text, no watermarks, no logos, "
        "no signage. Imperfections are welcome: a crumb on the surface, a "
        "smudge on the rim, herbs slightly askew."
    )

    prompt = para1 + " " + para2
    if extra_prompt:
        # gpt-image-1 weights trailing instructions heavily — putting
        # the user override at the very end with an IMPORTANT prefix
        # gives it the strongest possible weight, overriding any
        # conflicting details from the auto-built sections.
        prompt += " IMPORTANT — user-specified corrections that MUST take precedence: " + extra_prompt
    return prompt


def generate_dish_image(recipe, *, quality: Optional[str] = None,
                        size: Optional[str] = None,
                        extra_prompt: str = "") -> bytes:
    """Hero shot of a finished dish — what goes on the form's main
    image well and on a cookbook page. `recipe` may be a RecipeModel
    instance or the sanitized recipe dict; _get() handles both shapes.
    extra_prompt is appended to the auto-built brief with strong
    weighting (see _build_dish_prompt)."""
    prompt = _build_dish_prompt(recipe, extra_prompt=extra_prompt)
    return _generate_image(prompt, quality=quality, size=size)


def generate_ingredient_image(recipe_model, *, quality: Optional[str] = None,
                              size: Optional[str] = None) -> bytes:
    """Flat-lay of the dish's ingredients — auxiliary image for the
    cookbook layout or a secondary slot on the form."""
    ingredients = recipe_model.recipeIngredient or []
    prompt = (
        "Flat lay photo of ingredients including " +
        ", ".join(ingredients[:6]) +
        ". Natural lighting, white background, no packaging, no text."
    )
    return _generate_image(prompt, quality=quality, size=size)
