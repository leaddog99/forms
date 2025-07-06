from recipe_model import RecipeModel
import uuid
from datetime import datetime
from typing import Any, Dict

def is_nullish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in ("", "null", "none"):
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False

def sanitize_nullish_fields(obj: dict) -> dict:
    return {k: (None if is_nullish(v) else v) for k, v in obj.items()}

def set_if_nullish(obj, key, default):
    if key not in obj or is_nullish(obj[key]):
        obj[key] = default

def is_valid_video_object(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and obj.get("@type") == "VideoObject"
        and isinstance(obj.get("contentUrl", None), str)
    )

def is_valid_aggregate_rating(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and obj.get("@type") == "AggregateRating"
        and isinstance(obj.get("ratingValue", 0), (int, float))
        and isinstance(obj.get("reviewCount", 0), int)
    )

def sanitize_recipe_data(data: dict) -> dict:
    """
    Fill in missing required fields with default empty values
    to avoid Pydantic validation errors. Assumes top-level keys follow RecipeModel.
    """
    sanitized = data.copy()

    # Required top-level fields
    set_if_nullish(sanitized, "@context", "https://schema.org")
    set_if_nullish(sanitized, "@type", "Recipe")
    set_if_nullish(sanitized, "id", str(uuid.uuid4()))
    set_if_nullish(sanitized, "name", "")
    set_if_nullish(sanitized, "description", "")
    set_if_nullish(sanitized, "image", [])

    if isinstance(sanitized.get("image"), str):
        sanitized["image"] = [sanitized["image"]]

    set_if_nullish(sanitized, "author", {"@type": "Person", "name": "", "image": None})
    set_if_nullish(sanitized, "datePublished", datetime.utcnow().isoformat())
    set_if_nullish(sanitized, "dateModified", datetime.utcnow().isoformat())
    set_if_nullish(sanitized, "recipeYield", "")
    set_if_nullish(sanitized, "prepTime", "")
    set_if_nullish(sanitized, "cookTime", "")
    set_if_nullish(sanitized, "totalTime", "")
    set_if_nullish(sanitized, "recipeCategory", "")
    set_if_nullish(sanitized, "recipeCuisine", "")
    set_if_nullish(sanitized, "keywords", [])

    if not is_valid_aggregate_rating(sanitized.get("aggregateRating")):
        sanitized["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": 0, "reviewCount": 0}

    # Nutrition deep patch
    if not isinstance(sanitized.get("nutrition"), dict) or is_nullish(sanitized.get("nutrition")):
        sanitized["nutrition"] = {
            "calories": "",
            "fatContent": "",
            "carbohydrateContent": "",
            "proteinContent": ""
        }
    else:
        set_if_nullish(sanitized["nutrition"], "calories", "")
        set_if_nullish(sanitized["nutrition"], "fatContent", "")
        set_if_nullish(sanitized["nutrition"], "carbohydrateContent", "")
        set_if_nullish(sanitized["nutrition"], "proteinContent", "")

    set_if_nullish(sanitized, "recipeIngredient", [])

    # Convert flat 'instructions' if 'recipeInstructions' not present
    if "recipeInstructions" not in sanitized and "instructions" in sanitized:
        raw_instructions = sanitized.pop("instructions", [])
        steps = []
        if isinstance(raw_instructions, list):
            for i, step in enumerate(raw_instructions):
                if isinstance(step, str):
                    steps.append({"@type": "HowToStep", "position": i + 1, "text": step})
                elif isinstance(step, dict):
                    step.setdefault("@type", "HowToStep")
                    step.setdefault("position", i + 1)
                    steps.append(step)
        sanitized["recipeInstructions"] = steps
    else:
        set_if_nullish(sanitized, "recipeInstructions", [])

    set_if_nullish(sanitized, "notes", "")
    set_if_nullish(sanitized, "tags", [])

    # Strip invalid video object
    if not is_valid_video_object(sanitized.get("video")):
        sanitized["video"] = {
            "@type": "VideoObject",
            "name": "",
            "contentUrl": "",
            "thumbnailUrl": "",
            "uploadDate": datetime.utcnow().isoformat(),
            "description": ""
        }

    set_if_nullish(sanitized, "servingSuggestions", "")
    set_if_nullish(sanitized, "cookingMethod", "")
    set_if_nullish(sanitized, "equipment", [])
    set_if_nullish(sanitized, "suitableForDiet", [])

    # History deep patch
    if not isinstance(sanitized.get("provenance"), dict) or is_nullish(sanitized.get("provenance")):
        sanitized["provenance"] = {
            "ethnicity": "",
            "originRegion": "",
            "firstDocumented": None,
            "traditionalContext": "",
            "notableVariations": [],
            "relatedDishes": [],
            "sources": []
        }
    else:
        set_if_nullish(sanitized["provenance"], "ethnicity", "")
        set_if_nullish(sanitized["provenance"], "originRegion", "")
        set_if_nullish(sanitized["provenance"], "firstDocumented", None)
        set_if_nullish(sanitized["provenance"], "traditionalContext", "")
        set_if_nullish(sanitized["provenance"], "notableVariations", [])
        set_if_nullish(sanitized["provenance"], "relatedDishes", [])
        set_if_nullish(sanitized["provenance"], "sources", [])

    set_if_nullish(sanitized, "imageSource", "")
    set_if_nullish(sanitized, "_imported_from", "")
    set_if_nullish(sanitized, "_editor_version", "")
    set_if_nullish(sanitized, "_access", {"visibility": "public", "sharedWith": []})
    set_if_nullish(sanitized, "_source", {"type": "image", "origin": "", "originalUrl": ""})

    # Final validation
    validated = RecipeModel(**sanitized)
    return validated.model_dump(by_alias=True, exclude_none=True)
