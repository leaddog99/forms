"""
models.py — the single source of truth for a step-anchored recipe.

This is a Schema.org/Recipe-compatible core (name, recipeYield, recipeIngredient,
recipeInstructions) extended with the fields the step-anchored cook view needs:
per-step equipment (sized from quantities), per-step ingredient bindings with both
unit systems, and order-of-need equipment aggregation.

Everything downstream (rendering, product lookup, evals) reads from RecipeDoc.
Nothing else should hold recipe state.
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Amounts
# --------------------------------------------------------------------------- #
class Amount(BaseModel):
    """A quantity carried in both unit systems.

    The 'imperial'/'metric' strings are display-ready and use sensible cooking
    roundings (e.g. '450 g', not '453.6 g'). The measure does NOT have to be a
    number: a vessel-as-measure ('a bowl of', 'a handful of') is a valid amount,
    in which case both fields hold the same text and `convertible` is False.
    """
    imperial: str
    metric: str
    convertible: bool = True

    @classmethod
    def same(cls, text: str) -> "Amount":
        return cls(imperial=text, metric=text, convertible=False)


# --------------------------------------------------------------------------- #
# Master ingredient list
# --------------------------------------------------------------------------- #
class Ingredient(BaseModel):
    id: str = Field(..., description="Stable id, e.g. 'ing_clams'. Referenced by steps.")
    amount: Amount
    name: str = Field(..., description="Display name, e.g. 'fresh vongole (small clams)'.")
    note: Optional[str] = None


# --------------------------------------------------------------------------- #
# Equipment
# --------------------------------------------------------------------------- #
class Equipment(BaseModel):
    id: str = Field(..., description="Stable id, e.g. 'eq_saute_pan'.")
    name: str = Field(..., description="e.g. 'Heavy-bottomed sauté pan with lid'.")
    size_matters: bool = Field(
        ...,
        description="True if size affects the outcome (bowls, pots, pans, colanders, "
                    "serving vessels). False for spoons, tongs, timers.",
    )
    size: Optional[Amount] = Field(
        None,
        description="Required when size_matters is True; inferred from quantities. "
                    "None when size_matters is False.",
    )
    category: str = Field(
        ...,
        description="Catalog key for product lookup: bowl|colander|pot|pan|spoon|"
                    "knife|board|tongs|timer|serving|other.",
    )
    why: Optional[str] = Field(
        None, description="One line: why this size, tied to the recipe quantities."
    )


# --------------------------------------------------------------------------- #
# Step bindings
# --------------------------------------------------------------------------- #
class StepIngredient(BaseModel):
    """An ingredient as it is used *in a particular step*."""
    ingredient_id: str
    amount: Amount
    label: str = Field(..., description="How it reads inline, e.g. 'clams', 'cornmeal'.")
    reused_from_step: Optional[int] = Field(
        None,
        description="1-based step number where this item was introduced, if it is "
                    "being reused rather than added fresh. None when first used here.",
    )


class StepEquipment(BaseModel):
    equipment_id: str
    reused_from_step: Optional[int] = None


class Step(BaseModel):
    number: int
    name: str = Field(..., description="Short imperative title, e.g. 'Boil the spaghetti'.")
    instruction: str = Field(
        ...,
        description=(
            "Rewritten instruction containing inline tokens that the renderer expands:\n"
            "  {ingN}  -> the Nth entry of this step's `ingredients` list, rendered as\n"
            "            <bold-ingredient> with its amount in the active unit system.\n"
            "  {amt:IMPERIAL|METRIC} -> a standalone measure that is not an ingredient\n"
            "            (e.g. a time or temperature), e.g. {amt:about 3 minutes}.\n"
            "Use {amt:X} with a single value when it does not convert."
        ),
    )
    ingredients: list[StepIngredient] = Field(default_factory=list)
    equipment: list[StepEquipment] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# The document
# --------------------------------------------------------------------------- #
class RecipeDoc(BaseModel):
    # Schema.org/Recipe core
    name: str
    subtitle: Optional[str] = None
    author: Optional[str] = None
    source_url: Optional[str] = None
    recipe_yield: int = Field(..., description="Number of servings.")
    total_time_minutes: Optional[int] = None

    ingredients: list[Ingredient]
    steps: list[Step]

    # Extended: equipment aggregated in order of first need (deduped).
    equipment: list[Equipment] = Field(default_factory=list)

    def equipment_by_id(self, eid: str) -> Optional[Equipment]:
        return next((e for e in self.equipment if e.id == eid), None)
