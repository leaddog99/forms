"""
pipeline.py — fetch -> clean -> extract (LLM) -> anchor (LLM) -> RecipeDoc.

Two forced-tool LLM passes assemble the single-source-of-truth RecipeDoc. HTML cleaning
is intentionally simple here (readability-style strip); swap in your bookmarklet-captured
cleaned HTML / screenshot path in production.
"""
from __future__ import annotations

import os
import re
import json
import httpx
from anthropic import Anthropic

from .models import (
    Amount, Ingredient, Equipment, Step, StepIngredient, StepEquipment, RecipeDoc,
)
from .prompts import (
    EXTRACT_SYSTEM, EXTRACT_TOOL, ANCHOR_SYSTEM, ANCHOR_TOOL,
)

# Set the exact model ids you are entitled to here (or via env).
MODEL_EXTRACT = os.getenv("RA_MODEL_EXTRACT", "claude-sonnet-4-5")
MODEL_ANCHOR = os.getenv("RA_MODEL_ANCHOR", "claude-sonnet-4-5")

_client = Anthropic()  # reads ANTHROPIC_API_KEY


# --------------------------------------------------------------------------- #
# Fetch + clean
# --------------------------------------------------------------------------- #
def fetch_clean(url: str, timeout: float = 20.0) -> str:
    """Fetch a page and reduce it to readable text. Replace with your capture path."""
    r = httpx.get(url, timeout=timeout, follow_redirects=True,
                  headers={"User-Agent": "recipe-anchor/1.0"})
    r.raise_for_status()
    html = r.text
    html = re.sub(r"(?is)<(script|style|nav|footer|header|svg)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()[:20000]


# --------------------------------------------------------------------------- #
# Forced-tool helper
# --------------------------------------------------------------------------- #
def _forced_tool(model: str, system: str, user: str, tool: dict) -> dict:
    msg = _client.messages.create(
        model=model,
        max_tokens=4000,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": user}],
    )
    for block in msg.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return block.input
    raise RuntimeError(f"Model did not call {tool['name']}")


# --------------------------------------------------------------------------- #
# Pass 1
# --------------------------------------------------------------------------- #
def extract_base(cleaned_text: str) -> dict:
    user = f"Parse this recipe page into structured data.\n\n<page>\n{cleaned_text}\n</page>"
    return _forced_tool(MODEL_EXTRACT, EXTRACT_SYSTEM, user, EXTRACT_TOOL)


# --------------------------------------------------------------------------- #
# Pass 2
# --------------------------------------------------------------------------- #
def anchor(base: dict) -> dict:
    user = (
        "Here is the parsed recipe. Produce the step-anchored plan.\n\n"
        f"<recipe>\n{json.dumps(base, ensure_ascii=False, indent=2)}\n</recipe>"
    )
    return _forced_tool(MODEL_ANCHOR, ANCHOR_SYSTEM, user, ANCHOR_TOOL)


# --------------------------------------------------------------------------- #
# Assemble RecipeDoc
# --------------------------------------------------------------------------- #
def _amount(d: dict | None) -> Amount | None:
    if not d:
        return None
    return Amount(imperial=d["imperial"], metric=d["metric"],
                  convertible=d.get("convertible", True))


def assemble(base: dict, plan: dict, source_url: str | None) -> RecipeDoc:
    ingredients = [
        Ingredient(id=i["id"], name=i["name"], note=i.get("note"),
                   amount=Amount.same(i["amount_text"]))
        for i in base["ingredients"]
    ]
    equipment = [
        Equipment(id=e["id"], name=e["name"], size_matters=e["size_matters"],
                  size=_amount(e.get("size")), category=e["category"], why=e.get("why"))
        for e in plan["equipment_order"]
    ]
    steps = []
    for s in plan["steps"]:
        steps.append(Step(
            number=s["number"],
            name=s["name"],
            instruction=s["instruction"],
            ingredients=[
                StepIngredient(
                    ingredient_id=si["ingredient_id"],
                    amount=_amount(si["amount"]),
                    label=si["label"],
                    reused_from_step=si.get("reused_from_step"),
                ) for si in s["ingredients"]
            ],
            equipment=[
                StepEquipment(equipment_id=se["equipment_id"],
                              reused_from_step=se.get("reused_from_step"))
                for se in s["equipment"]
            ],
        ))
    return RecipeDoc(
        name=base["name"],
        subtitle=plan.get("subtitle") or base.get("subtitle"),
        author=base.get("author"),
        source_url=source_url,
        recipe_yield=base.get("recipe_yield") or 4,
        total_time_minutes=base.get("total_time_minutes"),
        ingredients=ingredients,
        steps=steps,
        equipment=equipment,
    )


def build_from_url(url: str) -> RecipeDoc:
    base = extract_base(fetch_clean(url))
    plan = anchor(base)
    return assemble(base, plan, source_url=url)


def build_from_text(cleaned_text: str, source_url: str | None = None) -> RecipeDoc:
    base = extract_base(cleaned_text)
    plan = anchor(base)
    return assemble(base, plan, source_url=source_url)
