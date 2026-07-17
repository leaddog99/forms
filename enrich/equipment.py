"""enrich/equipment.py — derive a recipe's equipment from its instructions.

The "WITHOUT a cook-view" path (see project_enrich_equipment): for recipes never
through cook-rework (no `_cook`), read the ingredients + instructions and extract
the tools the cook needs — ordered by first appearance, deduped, with a size where
the recipe states or clearly implies one. Output is the recipe's top-level schema
`equipment` (HowToTool + size) — the SAME field cook-rework mirrors — so the editor
shows it and product-commerce can key off equipment -> product_class (size = the
class grain).

Direct grounded LLM call (Sonnet, forced tool for a clean list), routed through
`llm` so usage journals to bcc_token_journal (operation="enrich_equipment").
Standalone for now; folds into the enrich block registry later.
"""
from __future__ import annotations

import json
import re

import llm  # central gateway — auto-journals usage

MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1200
DERIVE_PROMPT_VERSION = "equip-derive-v1-2026-07-10"

_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _as_item_list(items):
    """Coerce the model's `equipment` tool value into a LIST of items.

    The model intermittently serializes the array as a JSON STRING instead of a
    real list. Iterating that string downstream explodes it character-by-character
    into one bogus single-letter HowToTool each (deduped) — the "char-explosion"
    equipment corruption seen in the DB. Parse a JSON string back to a list
    (tolerating trailing commas, which the model sometimes emits and which make
    strict json.loads fail — a silent drop to empty), and hard-reject any non-list
    so we never char-iterate a scalar."""
    if isinstance(items, list):
        return items
    if isinstance(items, str):
        for candidate in (items, _TRAILING_COMMA.sub(r"\1", items)):
            try:
                parsed = json.loads(candidate)
            except Exception:
                continue
            if isinstance(parsed, list):
                return parsed
    return []

_TOOL = {
    "name": "equipment_list",
    "description": "Return the equipment/tools the cook needs, in order of first use.",
    "input_schema": {
        "type": "object",
        "properties": {
            "equipment": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "the tool, plainly named and singular "
                                                "(e.g. 'large skillet', '9x13 baking dish', 'box grater')"},
                        "size": {"type": "string",
                                 "description": "ONLY when the recipe states or clearly implies one: "
                                                "a 12-inch skillet -> '12 in', a 9x13 dish -> '9x13 in', "
                                                "a 3-quart saucepan -> '3 qt'. Omit when unstated — never invent."},
                    },
                    "required": ["name"],
                },
            }
        },
        "required": ["equipment"],
    },
}

_SYSTEM = (
    "You are a precise kitchen assistant. Given a recipe, list EVERY piece of equipment "
    "or tool the cook needs, inferred from the ingredients and the instructions. Include "
    "tools implied by a prep or cook verb (grate -> grater, whisk -> whisk, zest -> zester, "
    "sift -> sieve, fry -> skillet, bake -> the stated pan). ORDER them by first appearance "
    "in the instructions; dedupe so each tool appears once. Give a size ONLY when the recipe "
    "states or clearly implies one — never invent a size. Do NOT list ingredients or pantry "
    "staples. Call the equipment_list tool exactly once."
)


def _recipe_text(recipe: dict) -> str:
    instrs = recipe.get("recipeInstructions") or []
    steps = [(s.get("text") if isinstance(s, dict) else str(s)) for s in instrs]
    return json.dumps({
        "name": recipe.get("name"),
        "ingredients": recipe.get("recipeIngredient") or [],
        "instructions": steps,
        "notes": recipe.get("notes"),
    }, ensure_ascii=False, indent=1)


def derive_equipment(recipe: dict, *, operation: str = "enrich_equipment") -> list:
    """Return a HowToTool list [{'@type','name','size'?}] ordered by first use, deduped.
    `[]` when nothing is derivable. Raises on a hard API failure (caller maps to 503).
    Usage journals under `operation` (wrap the call in `llm.context(recipe_id,user_id)`)."""
    user = "Here is the recipe:\n\n" + _recipe_text(recipe) + "\n\nList the equipment."
    resp = llm.create(
        operation=operation, model=MODEL, max_tokens=_MAX_TOKENS,
        system=_SYSTEM, tools=[_TOOL],
        tool_choice={"type": "tool", "name": "equipment_list"},
        messages=[{"role": "user", "content": user}],
    )
    items = []
    for b in resp.content:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "equipment_list":
            items = (b.input or {}).get("equipment") or []
            break

    items = _as_item_list(items)

    out, seen = [], set()
    for it in items:
        # The model usually returns {name, size?} objects, but occasionally a bare
        # string — accept both.
        if isinstance(it, str):
            name, size = it.strip(), None
        elif isinstance(it, dict):
            name = str(it.get("name") or "").strip()
            size = it.get("size")
        else:
            continue
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        tool = {"@type": "HowToTool", "name": name}
        size = size.strip() if isinstance(size, str) else size
        if size:
            tool["size"] = size
        out.append(tool)
    return out
