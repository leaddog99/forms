# Recipe-content validators. Pure functions over text; no I/O.

from datetime import datetime, timezone

from input.pipeline.config import RECIPE_PHRASES, IS_RECIPE_THRESHOLD


def score_recipe_text(text: str) -> int:
    """Count occurrences of recipe phrases (case-insensitive)."""
    if not text:
        return 0
    lowered = text.lower()
    return sum(1 for phrase in RECIPE_PHRASES if phrase in lowered)


def is_recipe(text: str, threshold: int = IS_RECIPE_THRESHOLD) -> dict:
    """
    Return a structured validation result for a chunk of recipe-candidate text.

    Mirrors worker_is_recipe in the batch pipeline so behavior stays consistent.
    """
    score = score_recipe_text(text)
    accepted = score >= threshold
    return {
        "accepted": accepted,
        "score": score,
        "threshold": threshold,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": f"Recipe-phrase score {score} (threshold {threshold})",
    }


def stamp_validation_on_recipe(recipe: dict, validation: dict) -> None:
    """Write validation result onto a sanitized recipe dict in-place."""
    recipe["current_status"] = {
        "value": "accepted" if validation["accepted"] else "rejected",
        "reason": validation["reason"],
        "timestamp": validation["timestamp"],
    }
    scoring = recipe.get("_scoring") or {}
    scoring["recipeScore"] = validation["score"]
    scoring["recipeScoreThreshold"] = validation["threshold"]
    recipe["_scoring"] = scoring