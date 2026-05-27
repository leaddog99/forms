from recipe_model import RecipeModel
import html
import uuid
from datetime import datetime
from typing import Any, Dict


def _decode_entities_deep(obj: Any) -> Any:
    """Recursively walk a recipe-shaped dict/list and decode HTML entities
    on every string value. `html.unescape` handles named (&amp;, &nbsp;,
    &mdash;), decimal (&#39;), and hex (&#x27;) entities — anything a
    browser would decode. Strings without an '&' short-circuit so the
    walk stays cheap. Dict KEYS are untouched (they're schema field names,
    not user content). Some JSON-LD sources (notably NYT, Kitchn) ship
    titles and bodies pre-encoded as `Banana &amp; Walnut Loaf`; this is
    the upstream-canonicalization fix that keeps `&amp;` from leaking
    into stored data, which would then surface as a literal in any
    consumer that renders via textContent / .text / json."""
    if isinstance(obj, str):
        return html.unescape(obj) if "&" in obj else obj
    if isinstance(obj, dict):
        return {k: _decode_entities_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_entities_deep(v) for v in obj]
    return obj


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

def _coerce_image_entry(entry: Any) -> str:
    """Schema.org image fields may be strings, ImageObject dicts, or nested lists."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in ("url", "contentUrl", "@id"):
            v = entry.get(key)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def _join_if_list(value: Any) -> Any:
    """Some JSON-LD emits multi-value strings as lists. Form fields expect strings."""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if v not in (None, "", []))
    return value


def sanitize_recipe_data(data: dict) -> dict:
    # Decode HTML entities upfront so every downstream defaulting +
    # validation step works on canonical strings. The data.copy() above
    # was shallow; _decode_entities_deep returns a new dict tree so we
    # don't mutate the caller's data.
    sanitized = _decode_entities_deep(data)

    set_if_nullish(sanitized, "@context", "https://schema.org")
    set_if_nullish(sanitized, "@type", "Recipe")
    # @type may arrive as a list when a page also tags the recipe as e.g.
    # ["Recipe", "NewsArticle"] (AllRecipes does this). Keep "Recipe" if
    # present, else fall back to whichever first string we find.
    raw_type = sanitized["@type"]
    if isinstance(raw_type, list):
        if "Recipe" in raw_type:
            sanitized["@type"] = "Recipe"
        else:
            first = next((t for t in raw_type if isinstance(t, str) and t), "Recipe")
            sanitized["@type"] = first
    set_if_nullish(sanitized, "id", str(uuid.uuid4()))
    set_if_nullish(sanitized, "name", "")
    set_if_nullish(sanitized, "description", "")
    set_if_nullish(sanitized, "image", [])

    # Image entries can arrive as ImageObject dicts (NYT, Kitchn JSON-LD).
    # Flatten to a list of URL strings.
    raw_image = sanitized.get("image")
    if isinstance(raw_image, str):
        sanitized["image"] = [raw_image] if raw_image else []
    elif isinstance(raw_image, dict):
        coerced = _coerce_image_entry(raw_image)
        sanitized["image"] = [coerced] if coerced else []
    elif isinstance(raw_image, list):
        sanitized["image"] = [u for u in (_coerce_image_entry(i) for i in raw_image) if u]

    set_if_nullish(sanitized, "author", {"@type": "Person", "name": "", "image": None})
    # JSON-LD recipes with multiple bylines ship author as a list. Our model
    # is a single Author; keep the first sensible one.
    if isinstance(sanitized.get("author"), list):
        first_author = next(
            (a for a in sanitized["author"] if isinstance(a, dict) and a.get("name")),
            None,
        )
        sanitized["author"] = first_author or {"@type": "Person", "name": "", "image": None}
    set_if_nullish(sanitized, "datePublished", datetime.utcnow().isoformat())
    set_if_nullish(sanitized, "dateModified", datetime.utcnow().isoformat())
    set_if_nullish(sanitized, "recipeYield", "")
    # recipeYield can be a list like ["48", "4 dozen cookies"] — pick the
    # most useful single value. Prefer the human-readable form (longer string)
    # when there's a number and a phrase; otherwise first non-empty.
    if isinstance(sanitized["recipeYield"], list):
        items = [str(y).strip() for y in sanitized["recipeYield"] if str(y).strip()]
        sanitized["recipeYield"] = max(items, key=len) if items else ""
    set_if_nullish(sanitized, "prepTime", "")
    set_if_nullish(sanitized, "cookTime", "")
    set_if_nullish(sanitized, "totalTime", "")
    set_if_nullish(sanitized, "recipeCategory", "")
    set_if_nullish(sanitized, "recipeCuisine", "")
    # Schema.org allows category/cuisine as string OR list. Our model is string;
    # comma-join lists so we don't lose anything but stay validation-clean.
    sanitized["recipeCategory"] = _join_if_list(sanitized["recipeCategory"])
    sanitized["recipeCuisine"] = _join_if_list(sanitized["recipeCuisine"])
    set_if_nullish(sanitized, "keywords", [])
    # Schema.org allows keywords as a comma-separated string OR a list. Our
    # model is List[str]; split string form on commas, drop empties.
    if isinstance(sanitized.get("keywords"), str):
        sanitized["keywords"] = [k.strip() for k in sanitized["keywords"].split(",") if k.strip()]

    # NYT and others ship `ratingCount` instead of `reviewCount`. Map it over
    # before the validity check so we keep the real number instead of zeroing.
    raw_rating = sanitized.get("aggregateRating")
    if isinstance(raw_rating, dict) and "reviewCount" not in raw_rating and "ratingCount" in raw_rating:
        raw_rating["reviewCount"] = raw_rating["ratingCount"]
    if not is_valid_aggregate_rating(sanitized.get("aggregateRating")):
        sanitized["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": 0, "reviewCount": 0}

    # Nutrition. NutritionInfo fields are Optional[str]; JSON-LD sometimes
    # ships numeric values (NYT sends calories as int). Coerce to string.
    if not isinstance(sanitized.get("nutrition"), dict) or is_nullish(sanitized.get("nutrition")):
        sanitized["nutrition"] = {
            "calories": "",
            "fatContent": "",
            "carbohydrateContent": "",
            "proteinContent": ""
        }
    else:
        for k in ("calories", "fatContent", "carbohydrateContent", "proteinContent"):
            set_if_nullish(sanitized["nutrition"], k, "")
            v = sanitized["nutrition"].get(k)
            if v is not None and not isinstance(v, str):
                sanitized["nutrition"][k] = str(v)

    set_if_nullish(sanitized, "recipeIngredient", [])

    # 🔧 FIX recipeInstructions
    if "recipeInstructions" in sanitized:
        steps = []
        for i, step in enumerate(sanitized["recipeInstructions"]):
            if isinstance(step, str):
                steps.append({
                    "@type": "HowToStep",
                    "position": i + 1,
                    "text": step
                })
            elif isinstance(step, dict):
                step["@type"] = "HowToStep"
                step.pop("type", None)
                step.setdefault("position", i + 1)
                # Per-step image: JSON-LD often ships a list of ImageObject
                # dicts (AllRecipes does); HowToStep.image is a single URL
                # string. Coerce by taking the first URL we can extract.
                if "image" in step:
                    img = step["image"]
                    if isinstance(img, list):
                        urls = [u for u in (_coerce_image_entry(i) for i in img) if u]
                        step["image"] = urls[0] if urls else None
                    elif isinstance(img, dict):
                        step["image"] = _coerce_image_entry(img) or None
                steps.append(step)
        sanitized["recipeInstructions"] = steps
    else:
        set_if_nullish(sanitized, "recipeInstructions", [])

    set_if_nullish(sanitized, "notes", "")
    set_if_nullish(sanitized, "tags", [])

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
    set_if_nullish(sanitized, "_source", {"type": "image", "origin": "", "originalUrl": "", "affiliateUrl": ""})

    # Pipeline-side metadata. Leave as None when not provided so we don't
    # carry empty defaults on every record; sanitizer ensures shape only.
    if isinstance(sanitized.get("_scoring"), dict):
        set_if_nullish(sanitized["_scoring"], "pageAuthority", 0.0)
        set_if_nullish(sanitized["_scoring"], "domainAuthority", 0.0)
        set_if_nullish(sanitized["_scoring"], "ouScore", 0.0)
        set_if_nullish(sanitized["_scoring"], "rootDomain", "")
        set_if_nullish(sanitized["_scoring"], "rawTitle", "")
        set_if_nullish(sanitized["_scoring"], "iconUrl", "")
        set_if_nullish(sanitized["_scoring"], "recipeScore", 0)
        set_if_nullish(sanitized["_scoring"], "recipeScoreThreshold", 0)

    if isinstance(sanitized.get("classification"), dict):
        set_if_nullish(sanitized["classification"], "confidence", 0)
        set_if_nullish(sanitized["classification"], "reasoning", "")
        set_if_nullish(sanitized["classification"], "hierarchyPath", "")
        set_if_nullish(sanitized["classification"], "story", "")

    if isinstance(sanitized.get("editorial"), dict):
        set_if_nullish(sanitized["editorial"], "opinion", "")
        set_if_nullish(sanitized["editorial"], "scoreCommentary", "")
        set_if_nullish(sanitized["editorial"], "sourcingNotes", "")

    # Final validation
    validated = RecipeModel(**sanitized)
    return validated.model_dump(by_alias=True, exclude_none=True)
