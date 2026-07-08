"""Lightweight, always-on EQUIPMENT extraction for ANY recipe.

The cook-rework engine only builds an equipment list inside the `_cook` block, and only
~1% of master / ~6% of personal recipes have been cook-reworked. Affiliate product
matching needs equipment on the WHOLE corpus, so this is a cheap Haiku pass over a
recipe's ingredients + instructions that emits a normalized equipment list for any recipe.

Precedence: if a recipe already has a `_cook` block, derive equipment from it (accurate,
per-step, model-authored); otherwise run the lightweight LLM pass.

EXPERIMENTAL (experiments/affiliate/). If it proves out, promote to an
`extract/enrich_recipe.py` EnrichmentBlock (name='equipment') that populates the existing
top-level schema.org `equipment` field at ingest, so every recipe carries it.
"""
from __future__ import annotations

# Canonical equipment categories — aligned with the seed catalog's join keys.
_CATEGORIES = [
    "dutch-oven", "pot", "saucepan", "stockpot", "pan", "skillet", "wok", "roasting-pan",
    "bowl", "knife", "cutting-board", "whisk", "strainer", "colander", "thermometer",
    "stand-mixer", "hand-mixer", "blender", "immersion-blender", "food-processor",
    "sheet-pan", "baking-dish", "cake-pan", "springform", "grater", "zester", "scale",
    "rolling-pin", "mandoline", "tongs", "spatula", "ladle", "peeler", "measuring",
    "grill", "steamer", "other",
]

_TOOL = {
    "name": "emit_equipment",
    "description": "The kitchen equipment a cook needs to make this recipe.",
    "input_schema": {
        "type": "object",
        "properties": {
            "equipment": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "canonical, lowercase common name — e.g. "
                                                "'dutch oven', \"chef's knife\", 'fine-mesh strainer', 'whisk'"},
                        "category": {"type": "string", "enum": _CATEGORIES},
                        "essential": {"type": "boolean",
                                      "description": "true only if the dish genuinely can't be made without it; "
                                                     "false for substitutable / nice-to-have"},
                        "step": {"type": "integer", "description": "1-based step where first needed, or 0 if general"},
                    },
                    "required": ["name", "category", "essential"],
                },
            }
        },
        "required": ["equipment"],
    },
}

_SYS = (
    "You are a kitchen-equipment extractor. Given a recipe's ingredients and step-by-step "
    "instructions, list ONLY the equipment a cook actually needs — inferred from the verbs "
    "and vessels named or implied ('braise'/'simmer covered' -> a heavy pot or Dutch oven; "
    "'whisk' -> a whisk; 'sear'/'fry' -> a skillet; 'strain'/'pass through a sieve' -> a "
    "strainer; 'stir-fry' -> a wok; 'roast' -> a sheet pan or roasting pan; 'cream butter and "
    "sugar' -> a mixer). Use canonical, lowercase common names. Do NOT invent equipment the "
    "steps don't imply. Mark `essential` true only when the dish can't reasonably be made "
    "without it. Skip trivial universals (a spoon, a plate) unless a step specifically needs one."
)


def extract_equipment_llm(recipe: dict) -> list[dict]:
    """Cheap Haiku pass over ingredients + instructions -> normalized equipment list."""
    import llm
    ings = recipe.get("recipeIngredient") or []
    steps = recipe.get("recipeInstructions") or []
    step_texts = []
    for i, s in enumerate(steps, 1):
        t = s.get("text") if isinstance(s, dict) else str(s)
        if t:
            step_texts.append(f"{i}. {t}")
    user = ("INGREDIENTS:\n" + "\n".join(f"- {x}" for x in ings) +
            "\n\nINSTRUCTIONS:\n" + "\n".join(step_texts))
    resp = llm.create(
        operation="equipment_extract", model="claude-haiku-4-5",
        max_tokens=1200, temperature=0, system=_SYS,
        messages=[{"role": "user", "content": user}],
        tools=[_TOOL], tool_choice={"type": "tool", "name": "emit_equipment"})
    ti = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    items = (ti or {}).get("equipment", []) if isinstance(ti, dict) else []
    for it in items:
        it["_source"] = "llm"
    return items


def equipment_from_cook(recipe: dict) -> list[dict] | None:
    """Derive equipment from an existing `_cook` block (accurate, model-authored)."""
    cook = recipe.get("_cook") or {}
    eq = cook.get("equipment")
    if not eq:
        return None
    out = []
    for e in eq:
        if not isinstance(e, dict):
            continue
        out.append({
            "name": (e.get("name") or "").strip().lower(),
            "category": e.get("category") or "other",
            "essential": True,  # the cook block only lists genuinely-used equipment
            "step": e.get("first_step") or 0,
            "_source": "cook",
        })
    return out or None


def extract_equipment(recipe: dict) -> list[dict]:
    """The always-on entry point: prefer accurate `_cook` equipment, else the LLM pass."""
    return equipment_from_cook(recipe) or extract_equipment_llm(recipe)
