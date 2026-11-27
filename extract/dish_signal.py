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


import llm  # central LLM gateway — auto-journals usage to bcc_token_journal
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
        response = llm.create(
            operation=operation, model=_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        print(f"[WARN] dish_signal LLM call failed: {e}")
        return ""
    # usage auto-journaled by the gateway (operation=operation).

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


# === Evidence-based description ==============================================
#
# `generate_dish_signal_for_dish` above is ONE small-model guess from the dish
# NAME. For a well-known dish that is enough. For an obscure one it can be
# plainly wrong ("Sauce Jean Vignard", a white-wine and cream sauce, described
# as a brown sauce, 2026-11-27), and the description is what the dish's
# matching profile is built from.
#
# The two functions below write and check a description from EVIDENCE instead:
# web search results for the dish, the recipes filed under it, the curator's
# notes. input/pipeline/dish_describe.py gathers that evidence and decides when
# each runs (dish creation, the editor's Retry button, after a refresh); this
# module only owns the prompts and the model calls.

DESCRIBE_MODEL = "claude-opus-4-8"
DESCRIBE_METHOD_ID = "dish_description_evidence_v1"
CHECK_METHOD_ID = "dish_description_check_v1"

MAX_SEARCH_RESULTS = 10
MAX_RECIPES = 8

_EVIDENCE_RULES = (
    "\n\nYou are working from evidence, not from memory. You may be given: the "
    "dish's name and other names, the curator's notes, a previous description, "
    "web search results for the dish (titles and snippets), and the recipes "
    "currently filed under this dish.\n"
    "  - Describe the CANONICAL dish: its cuisine, its defining ingredients "
    "and its technique.\n"
    "  - The curator's notes outrank everything else.\n"
    "  - Search results say what the dish is generally understood to be. Use "
    "the ones about this dish; ignore results about something else that "
    "shares the name (a person, a restaurant, a product).\n"
    "  - Filed recipes show the dish in practice. Use what they have in "
    "common; ignore one that is plainly a different dish filed by mistake, "
    "and do not promote one recipe's shortcut into the definition.\n"
    "  - Where a previous description contradicts the evidence, the evidence "
    "wins. Do not repeat its wording unless it was right.\n"
    "  - With no usable evidence, use what is reliably known about the dish; "
    "if it is not a dish you know, describe only what the name itself states "
    "and do not invent a cuisine, a colour or a technique.\n"
    "The sentence starts with the dish name and a colon."
)

_CHECK_RULES = (
    "\n\nHERE you are a CHECKER, and you answer with the tool instead of a bare "
    "sentence. Decide whether the stored description materially misdescribes "
    "the dish the filed recipes are. Be slow to disagree: only a wrong cuisine, "
    "a wrong defining ingredient or a wrong technique counts; wording, emphasis "
    "and optional ingredients do not."
)

_CHECK_TOOL = {
    "name": "description_verdict",
    "description": "Report whether the stored description fits the dish's filed recipes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "agrees": {"type": "boolean", "description":
                       "true when the stored description names the same dish the recipes are."},
            "why": {"type": "string", "description":
                    "When agrees is false: one short plain sentence naming what is wrong "
                    "(for example: it says brown sauce; every recipe is a white-wine cream sauce)."},
            "description": {"type": "string", "description":
                            "When agrees is false: the corrected one-sentence description."},
        },
        "required": ["agrees"],
    },
}


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _evidence_message(name: str, *, alternates=None, notes: str = "", previous: str = "",
                      stored: str = "", search_results=None, recipes=None) -> tuple:
    """The user message both calls share: (text, recipes_used, results_used)."""
    lines = [f"Dish name: {name}"]
    if stored.strip():
        lines.append("Stored description to judge: " + stored.strip()[:400])
    alts = list(dict.fromkeys(a for a in (_one_line(x) for x in (alternates or []))
                              if a and a.lower() != name.lower()))
    if alts:
        lines.append("Other names and search phrases: " + ", ".join(alts))
    if notes.strip():
        lines.append("Curator's notes: " + notes.strip()[:1200])
    if previous.strip():
        lines.append("Previous description: " + previous.strip()[:400])
    n_results = 0
    for r in (search_results or [])[:MAX_SEARCH_RESULTS]:
        title, snippet = _one_line(r.get("title")), _one_line(r.get("snippet"))
        if not (title or snippet):
            continue
        n_results += 1
        if n_results == 1:
            lines.append("\nWeb search results for this dish:")
        lines.append(f"  {n_results}. {title}" + (f" ({r['host']})" if r.get("host") else "")
                     + (f" - {snippet}" if snippet else ""))
    n_recipes = 0
    for r in recipes or []:
        ingredients = [i for i in (_one_line(x) for x in (r.get("ingredients") or [])) if i][:14]
        if not ingredients:
            continue
        n_recipes += 1
        lines.append(f"\nFiled recipe {n_recipes}: {_one_line(r.get('name'))[:90]}"
                     + (f" [{r['cuisine']}]" if r.get("cuisine") else ""))
        lines.append("  ingredients: " + "; ".join(ingredients))
        if _one_line(r.get("method")):
            lines.append("  method: " + _one_line(r["method"])[:420])
        if n_recipes >= MAX_RECIPES:
            break
    if not n_recipes and not n_results:
        lines.append("\n(No search results and no recipes are available for this dish.)")
    elif not n_recipes:
        lines.append("\n(No recipes are filed under this dish yet.)")
    return "\n".join(lines), n_recipes, n_results


def compose_dish_description(name: str, *, alternates: Optional[list] = None,
                             recipes: Optional[list] = None, notes: str = "",
                             previous: str = "", search_results: Optional[list] = None,
                             operation: str = "dish_description", create=None) -> dict:
    """Write the dish's one-sentence description from the evidence given.
    `recipes` = [{name, cuisine, ingredients:[...], method}]; `search_results`
    = [{title, snippet, host}]. Returns {description, method, model,
    recipes_used, results_used}; description is '' when the call fails."""
    name = (name or "").strip()
    out = {"description": "", "method": DESCRIBE_METHOD_ID, "model": DESCRIBE_MODEL,
           "recipes_used": 0, "results_used": 0}
    if not name:
        return out
    message, out["recipes_used"], out["results_used"] = _evidence_message(
        name, alternates=alternates, notes=notes or "", previous=previous or "",
        search_results=search_results, recipes=recipes)
    try:
        response = (create or llm.create)(
            operation=operation, model=DESCRIBE_MODEL, max_tokens=300,
            system=_SYSTEM_PROMPT + _EVIDENCE_RULES,
            messages=[{"role": "user", "content": message}])
        text = " ".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    except Exception as e:
        print(f"[WARN] dish description call failed: {type(e).__name__}: {e}")
        return out
    out["description"] = _one_line(text).strip('"').strip("'").strip()
    return out


def judge_dish_description(name: str, *, description: str, recipes: list,
                           alternates: Optional[list] = None, notes: str = "",
                           create=None) -> Optional[dict]:
    """Does the stored description fit the recipes now filed under the dish?
    Returns {agrees, why, description, recipes_used, method, model}, or None
    when the call fails (no verdict is not a verdict)."""
    message, n_recipes, _ = _evidence_message(
        (name or "").strip(), alternates=alternates, notes=notes or "",
        stored=description or "", recipes=recipes)
    try:
        response = (create or llm.create)(
            operation="dish_description_check", model=DESCRIBE_MODEL, max_tokens=400,
            system=_SYSTEM_PROMPT + _EVIDENCE_RULES + _CHECK_RULES,
            tools=[_CHECK_TOOL], tool_choice={"type": "tool", "name": _CHECK_TOOL["name"]},
            messages=[{"role": "user", "content": message}])
        verdict = next((b.input for b in response.content
                        if getattr(b, "type", "") == "tool_use"), None)
    except Exception as e:
        print(f"[WARN] dish description check failed: {type(e).__name__}: {e}")
        return None
    if not isinstance(verdict, dict) or "agrees" not in verdict:
        return None
    return {"agrees": bool(verdict["agrees"]), "why": _one_line(verdict.get("why")),
            "description": _one_line(verdict.get("description")),
            "recipes_used": n_recipes, "method": CHECK_METHOD_ID, "model": DESCRIBE_MODEL}
