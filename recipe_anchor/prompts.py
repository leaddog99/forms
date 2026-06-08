"""
prompts.py — system prompts and forced-tool schemas for the two LLM passes.

Pass 1  EXTRACT  : cleaned recipe HTML/text  ->  base recipe (ingredients + raw steps)
Pass 2  ANCHOR   : base recipe              ->  step-anchored doc (equipment + bindings)

Both passes use a single forced tool call so the model must return structured JSON
that maps onto the Pydantic models. No prose, no markdown.

Product recommendations are deliberately NOT produced here. Per the buying-intelligence
architecture, the LLM is a context gate that only emits an equipment `category`; code
resolves categories to products and affiliate links. E-commerce data never enters the
model context.
"""

# --------------------------------------------------------------------------- #
# Pass 1: extraction
# --------------------------------------------------------------------------- #
EXTRACT_SYSTEM = """\
You are a careful recipe parser. You convert one cleaned recipe page into structured \
data. You never invent ingredients, quantities, or steps that are not present in the \
source. If the source omits something (e.g. a yield), leave it null rather than guessing.

Rules:
- Preserve the source's quantities exactly. Do not scale or round at this stage.
- Split combined steps only where the source clearly describes separate actions.
- Keep instruction wording close to the source; light copy-editing for clarity is fine, \
but do not add technique the source does not state.
- Give every ingredient a stable snake_case id derived from its name (e.g. 'ing_olive_oil').
Return your result by calling the `emit_recipe` tool exactly once. Output nothing else.\
"""

EXTRACT_TOOL = {
    "name": "emit_recipe",
    "description": "Return the parsed base recipe.",
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "subtitle": {"type": ["string", "null"]},
            "author": {"type": ["string", "null"]},
            "recipe_yield": {"type": ["integer", "null"],
                             "description": "Servings, if stated."},
            "total_time_minutes": {"type": ["integer", "null"]},
            "ingredients": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "amount_text": {"type": "string",
                                        "description": "Exactly as written, e.g. '1/4 cup'."},
                        "name": {"type": "string"},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["id", "amount_text", "name"],
                },
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer"},
                        "instruction": {"type": "string"},
                    },
                    "required": ["number", "instruction"],
                },
            },
        },
        "required": ["name", "ingredients", "steps"],
    },
}


# --------------------------------------------------------------------------- #
# Pass 2: step anchoring
# --------------------------------------------------------------------------- #
ANCHOR_SYSTEM = """\
You are a kitchen workflow designer. Given a parsed recipe, you produce a STEP-ANCHORED \
version whose goal is that a cook never has to scroll back to a master list: each step \
states the equipment to gather and the amounts to use, in place.

Produce your answer by calling `emit_step_plan` exactly once. Follow every rule below.

EQUIPMENT
- For each step, list the equipment that step requires.
- Specify a SIZE for any equipment where size affects the outcome — mixing bowls, pots, \
pans, colanders, serving vessels — and infer that size from the recipe's quantities \
(e.g. 2 lb of clams need a 5 qt+ bowl; 1 lb of pasta needs a 6-8 qt pot). Set \
size_matters=true and fill `size`.
- For tools where size is irrelevant — spoons, tongs, timers, knives, boards — set \
size_matters=false and leave `size` null.
- Give every distinct piece a stable id (e.g. 'eq_saute_pan'). When a later step reuses \
a piece introduced earlier, reference the SAME id and set reused_from_step to the step \
where it first appeared. Do not re-introduce it.
- Also return `equipment_order`: the deduped list of all equipment in the order each \
piece is FIRST needed across the steps.

INGREDIENTS (inline)
- Do not output a separate per-step ingredient list. Instead, rewrite each step's \
instruction so the amounts live in the sentence, and mark them with tokens:
    {ingN}            references this step's Nth `ingredients` entry (0-based)
    {amt:VALUE}       a standalone measure that is not an ingredient (a time, a temp)
- Every ingredient the step acts on should appear as a token, including water and \
seasonings. An ingredient first used in this step gets its amount; an ingredient reused \
from earlier is referenced with reused_from_step set and may carry no fresh amount.
- The measure is not always a number. A vessel-as-measure is valid: 'a bowl of' is the \
amount for the water, 'a handful of' for parsley. Put that text in the amount.

UNITS
- Give every amount in BOTH imperial and metric, display-ready, using standard cooking \
roundings (1 lb -> '450 g', 1/4 cup -> '60 ml', 12 in -> '30 cm'). If a measure does \
not convert (counts like '3 cloves', vessel measures, 'to taste'), set convertible=false \
and put the same text in both fields.

CATEGORY (for product lookup, not shopping)
- Tag each equipment with a `category` from: bowl, colander, pot, pan, spoon, knife, \
board, tongs, timer, serving, other. Emit only the category. Never emit product names, \
brands, prices, or URLs.

STYLE
- Keep step `name` short and imperative ('Boil the spaghetti').
- Keep instructions faithful to the source technique.\
"""

# Reusable sub-schemas
_AMOUNT = {
    "type": "object",
    "properties": {
        "imperial": {"type": "string"},
        "metric": {"type": "string"},
        "convertible": {"type": "boolean"},
    },
    "required": ["imperial", "metric", "convertible"],
}

ANCHOR_TOOL = {
    "name": "emit_step_plan",
    "description": "Return the step-anchored recipe plan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subtitle": {"type": ["string", "null"]},
            "equipment_order": {
                "type": "array",
                "description": "All equipment, deduped, in order of first need.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "size_matters": {"type": "boolean"},
                        "size": {"anyOf": [_AMOUNT, {"type": "null"}]},
                        "category": {
                            "type": "string",
                            "enum": ["bowl", "colander", "pot", "pan", "spoon",
                                     "knife", "board", "tongs", "timer", "serving", "other"],
                        },
                        "why": {"type": ["string", "null"]},
                    },
                    "required": ["id", "name", "size_matters", "category"],
                },
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer"},
                        "name": {"type": "string"},
                        "instruction": {
                            "type": "string",
                            "description": "Rewritten, with {ingN} and {amt:..} tokens.",
                        },
                        "ingredients": {
                            "type": "array",
                            "description": "Ordered to match {ing0},{ing1},... in instruction.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ingredient_id": {"type": "string"},
                                    "amount": _AMOUNT,
                                    "label": {"type": "string"},
                                    "reused_from_step": {"type": ["integer", "null"]},
                                },
                                "required": ["ingredient_id", "amount", "label"],
                            },
                        },
                        "equipment": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "equipment_id": {"type": "string"},
                                    "reused_from_step": {"type": ["integer", "null"]},
                                },
                                "required": ["equipment_id"],
                            },
                        },
                    },
                    "required": ["number", "name", "instruction", "ingredients", "equipment"],
                },
            },
        },
        "required": ["equipment_order", "steps"],
    },
}
