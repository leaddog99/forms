"""LLM-assisted grams-per-cup estimate for a food item Enrich has no measured or
reference value for (e.g. "meat sauce for lasagna").

Curator-facing and ONE-TIME: the suggestion is reviewed in the measures editor
and saved into the ingredient_measures row. After that the deterministic engine
resolves it for free forever — the recipe master never re-derives it, and no
per-recipe LLM call is ever paid for that ingredient again.

Identify/estimate only: the model returns a per-cup weight + its reasoning; Enrich
derives density (g_per_ml = grams_per_cup / cup_mL). The row is flagged
`llm_derived` (asterisked in recipe display) until a human measures it and
upgrades it to `measured`. The name, aliases, and description are the context
that makes the estimate good — especially the physical form (chunky vs smooth,
packed vs loose, cooked vs raw).
"""
from __future__ import annotations

from typing import Optional

from .convert import CUP_ML


class EstimateError(Exception):
    """The model returned no usable estimate."""


_TOOL = {
    "name": "estimate_cup_weight",
    "description": "Return the estimated weight in grams of one US cup of a food "
                   "item, with the reasoning behind it.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["grams_per_cup", "confidence"],
        "properties": {
            "grams_per_cup": {
                "type": "number",
                "description": "weight in grams of 1 US cup (236.6 mL) of the item, "
                               "as typically measured in a home recipe",
            },
            "basis": {
                "type": "string",
                "description": "one line: what reference/comparison the estimate rests on",
            },
            "assumptions": {
                "type": "string",
                "description": "form/packing assumed, e.g. 'chunky sauce, lightly packed'",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
        },
    },
}

_SYSTEM = (
    "You are a culinary measurement expert. Estimate the weight in grams of ONE "
    "US cup (236.6 mL) of the given food item, as it would typically be measured "
    "in a home recipe. Use the name, any aliases, and the description to judge the "
    "item's physical form and how it packs — that is what drives the answer.\n\n"
    "Calibration anchors (grams per US cup):\n"
    "  water 236 | milk 244 | thin broth/stock 240 | most tomato or meat sauces 245-260 |\n"
    "  honey/syrup 320-340 | oil 218 | flour (spooned) 120 | granulated sugar 200 |\n"
    "  brown sugar (packed) 220 | cocoa 85 | ground meat (raw) 225-240 | cooked rice 175-200 |\n"
    "  chopped vegetables 120-160 | shredded cheese 100 | leafy greens (packed) 30-60 |\n"
    "  whole nuts 140 | dry breadcrumbs 110\n\n"
    "Guidance:\n"
    "  - Liquids and smooth sauces are water-adjacent (~236-260) unless oily (lower) "
    "or sugary/syrupy (higher).\n"
    "  - Chunky or packed items weigh more per cup than loose/fluffy ones; cooked "
    "weighs more than the same volume raw.\n"
    "  - If the description specifies form (chopped, packed, drained, cooked, pureed), "
    "reflect it in the number.\n"
    "Return grams_per_cup, a one-line basis, your assumptions, and a confidence. "
    "Do not show arithmetic."
)


def estimate_cup_weight(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    description: str = "",
    model: str = "claude-haiku-4-5",
    llm_key: Optional[str] = None,
) -> dict:
    """Ask the model for grams-per-cup of `name`, helped by aliases + description.
    Returns {grams_per_cup, g_per_ml, basis, assumptions, confidence, usage}.
    Raises EstimateError if the model returns nothing usable."""
    import anthropic
    from ..journal import build_usage_entry

    client = anthropic.Anthropic(api_key=llm_key) if llm_key else anthropic.Anthropic()
    parts = [f"Food item: {name.strip()}"]
    if aliases:
        parts.append("Also known as: " + ", ".join(a for a in aliases if a))
    if description.strip():
        parts.append("Description: " + description.strip())
    prompt = "\n".join(parts) + "\n\nEstimate the weight of 1 US cup."

    resp = client.messages.create(
        model=model,
        max_tokens=512,
        temperature=0,
        system=_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": "estimate_cup_weight"},
    )
    usage = [build_usage_entry("measure_estimate", model, resp)]

    data = next(
        (b.input for b in resp.content
         if getattr(b, "type", None) == "tool_use" and b.name == "estimate_cup_weight"),
        None,
    )
    if not isinstance(data, dict) or data.get("grams_per_cup") in (None, ""):
        raise EstimateError("the model returned no usable estimate")
    gpc = float(data["grams_per_cup"])
    if gpc <= 0:
        raise EstimateError(f"implausible estimate ({gpc} g/cup)")
    return {
        "grams_per_cup": round(gpc, 1),
        "g_per_ml": round(gpc / CUP_ML, 5),
        "basis": (data.get("basis") or "").strip(),
        "assumptions": (data.get("assumptions") or "").strip(),
        "confidence": (data.get("confidence") or "").strip(),
        "usage": usage,
    }
