"""Dish-signal generator — lightweight matching-key LLM call.

The cohort matcher (input.pipeline.embeddings) embeds recipe + dish
text and scores them by cosine similarity. The quality of the match
depends entirely on the input text being focused and disambiguating.

Free-form recipe names like "Mom's Sunday Casserole" or "Cheesy Pasta
Bake" carry no dish identity. The full `enrich_recipe` step produces
chapter / cuisine / ethnicity which help, but it's heavy (~$0.05 +
~10s, three parallel Claude calls producing prose). This module is the
opposite: ONE Haiku call, ~50 output tokens, ~$0.00004/call, returning
exactly one sentence focused on dish identity.

Two entry points:

  generate_dish_signal_for_recipe(recipe_dict) -> str
    Stamps as `classification.dishSignal` on save. Embedding text
    composer in input.pipeline.embeddings uses it when present.

  generate_dish_signal_for_dish(name, queries) -> str
    Pre-fills the dish.description field from a "Suggest" button.
    Curator reviews + edits before save. Same prompt shape; the
    difference is recipe details vs. just-a-name.
"""
from __future__ import annotations

import hashlib
from typing import Optional

import anthropic


_anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env


# The prompt deliberately doesn't ask for cuisine, technique, or
# ingredients individually — just the cohort identity. Embedding
# matching is downstream of this; we want the model to compress the
# recipe down to "what dish would I look for in a cookbook to find
# variants of this?" The 200-char cap forces compression — verbose
# answers degrade embedding focus.
_SYSTEM_PROMPT = (
    "You produce one-sentence dish identification phrases for "
    "cohort matching. Output ONLY the sentence — no preamble, no "
    "explanation, no quotes.\n\n"
    "The sentence should capture the dish's identity so a reader can "
    "tell at a glance which traditional dish family this belongs to. "
    "Mention the canonical dish name (or closest equivalent) when "
    "there is one, then 3-5 dish-defining attributes (cuisine origin, "
    "core technique, key structural ingredients).\n\n"
    "Examples of well-formed signals:\n"
    "  - Pastitsio: Greek baked pasta with layers of macaroni, "
    "cinnamon-spiced ground meat sauce, and bechamel topping; "
    "sometimes called Greek lasagna.\n"
    "  - Spaghetti Bolognese: Italian pasta dish with slow-simmered "
    "ground meat and tomato sauce served over long noodles.\n"
    "  - Asparagus Au Gratin: French baked vegetable side dish with "
    "asparagus under a cheese and breadcrumb crust.\n\n"
    "Constraints:\n"
    "  - Single sentence.\n"
    "  - 150-220 characters.\n"
    "  - No fluff words ('delicious', 'amazing', 'perfect').\n"
    "  - No brand names, no chef attributions, no website mentions.\n"
    "  - If the dish is genuinely unidentifiable (truly novel or "
    "ambiguous), describe its structure rather than guess at a name."
)

DISH_SIGNAL_PROMPT_VERSION = hashlib.sha256(
    _SYSTEM_PROMPT.encode("utf-8")
).hexdigest()[:12]

_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 200          # ~150 input tokens of prose at 4 chars/token
_TEMPERATURE = 0.2         # low but not zero — slight room for natural phrasing


def _generate(user_prompt: str, *, usage_log: Optional[list] = None,
              operation: str = "dish_signal") -> str:
    """Shared one-call helper. Returns empty string on any failure —
    callers fall back to whatever they had (curator-supplied text, or
    the name+queries embedding for dishes; name+ingredients for
    recipes)."""
    try:
        response = _anthropic_client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        print(f"[WARN] dish_signal LLM call failed: {e}")
        return ""

    if usage_log is not None:
        try:
            from input.pipeline.token_journal import build_usage_entry
            usage_log.append(build_usage_entry(operation, _MODEL, response))
        except Exception:
            pass

    text_parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    text = " ".join(text_parts).strip()
    # The prompt forbids quotes, but defensively strip them anyway.
    return text.strip('"').strip("'").strip()


def generate_dish_signal_for_recipe(recipe: dict, *,
                                    usage_log: Optional[list] = None) -> str:
    """Generate the matching key for a recipe. Returns the one-sentence
    signal, or empty string on failure. Caller stamps the result on
    `classification.dishSignal`.

    Input shape: a sanitized recipe dict. Pulls name + first N
    ingredients + recipeCuisine + classification.chapter when present.
    Doesn't require enrichment — works on bare extracts too.
    """
    name = (recipe.get("name") or "").strip()
    if not name:
        return ""

    cls = recipe.get("classification") or {}
    prov = recipe.get("provenance") or {}
    cuisine = (recipe.get("recipeCuisine") or "").strip()
    chapter = (cls.get("chapter") or "").strip()
    ethnicity = (prov.get("ethnicity") or "").strip()
    description = (recipe.get("description") or "").strip()

    ings_raw = recipe.get("recipeIngredient") or []
    ings: list[str] = []
    for ing in ings_raw[:10]:
        if isinstance(ing, str):
            ings.append(ing.strip())
        elif isinstance(ing, dict):
            ings.append(str(ing.get("text") or ing.get("name") or "").strip())
    ings = [i for i in ings if i]

    lines = [f"Recipe name: {name}"]
    if cuisine:
        lines.append(f"Cuisine: {cuisine}")
    elif ethnicity:
        lines.append(f"Cuisine: {ethnicity}")
    if chapter:
        lines.append(f"Chapter: {chapter}")
    if description:
        # Trim — site-supplied descriptions can be very long.
        lines.append(f"Description: {description[:400]}")
    if ings:
        lines.append("Key ingredients: " + ", ".join(ings))

    return _generate("\n".join(lines), usage_log=usage_log,
                     operation="dish_signal_recipe")


def generate_dish_signal_for_dish(name: str, queries: Optional[list] = None,
                                  *,
                                  usage_log: Optional[list] = None) -> str:
    """Generate a starter description for a dish entry. Used by the
    curator's "Suggest" button — returns one sentence the curator
    accepts or edits. The shape mirrors the recipe version so dish
    descriptions and recipe signals land in the same embedding space.
    """
    name = (name or "").strip()
    if not name:
        return ""

    lines = [f"Dish name: {name}"]
    if queries:
        clean_q = [str(q).strip() for q in queries if str(q).strip()]
        # Drop a query that's just the name (no extra signal).
        clean_q = [q for q in clean_q if q.lower() != name.lower()]
        if clean_q:
            lines.append("Alternate names: " + ", ".join(clean_q))

    return _generate("\n".join(lines), usage_log=usage_log,
                     operation="dish_signal_dish")
