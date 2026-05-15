"""Schema.org Recipe JSON-LD -> RecipeModel. No LLM.

Fast lane for URLs whose pages already publish complete structured data
(AllRecipes, Kitchn, NYT public, Food Network, etc. — most recipe sites
that care about SEO ship this).

The JSON-LD shape is *very close* to RecipeModel by design. We do minimal
reshaping here and lean on sanitize_recipe_data for the quirks it already
handles (keywords string/list, recipeCategory list/string, image dict/string,
HowToStep coercion, etc.).

If the JSON-LD is missing the meaty fields (name, ingredients, or
instructions), return None — caller falls back to the markdown_to_recipe
big-prompt path.
"""
import json
import time
from typing import Any, Optional

from sanitize_recipe_data import sanitize_recipe_data
from recipe_model import RecipeModel
from input.pipeline.validators import is_recipe, stamp_validation_on_recipe
from input.pipeline.url_utils import normalize_url, root_domain


def _has_required_fields(jsonld: dict) -> tuple[bool, str]:
    """Return (ok, reason). Required: name + non-empty ingredients +
    non-empty instructions. Anything thinner and we fall back to the LLM."""
    if not isinstance(jsonld, dict):
        return False, "JSON-LD is not a dict"
    name = jsonld.get("name")
    if not isinstance(name, str) or not name.strip():
        return False, "missing name"
    ingredients = jsonld.get("recipeIngredient") or []
    if not isinstance(ingredients, list) or not [i for i in ingredients if str(i).strip()]:
        return False, "missing or empty recipeIngredient"
    instructions = jsonld.get("recipeInstructions")
    if not instructions:
        return False, "missing recipeInstructions"
    # Strings, list of strings, list of HowToStep dicts, and list of
    # HowToSection dicts (which contain itemListElement) all count as present.
    if isinstance(instructions, list) and not [i for i in instructions if i]:
        return False, "empty recipeInstructions"
    return True, "ok"


def _flatten_instructions(raw: Any) -> list:
    """Schema.org allows several shapes; flatten to a list of HowToStep dicts
    or strings. sanitize_recipe_data does the final coercion."""
    if raw is None:
        return []
    if isinstance(raw, str):
        # Whole instructions as one string with newlines.
        return [s.strip() for s in raw.split("\n") if s.strip()]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                item_type = item.get("@type")
                if item_type == "HowToSection" or (isinstance(item_type, list) and "HowToSection" in item_type):
                    # Section wraps a list of steps.
                    for nested in item.get("itemListElement") or []:
                        if isinstance(nested, dict):
                            out.append(nested)
                        elif isinstance(nested, str):
                            out.append(nested)
                else:
                    out.append(item)
        return out
    return [raw]


def jsonld_to_recipe(
    jsonld: dict,
    *,
    source_url: str = "",
    title: str = "",
    timings: Optional[dict] = None,
) -> Optional[dict]:
    """Build a validated RecipeModel directly from a schema.org Recipe dict.

    Returns the recipe dict on success, or None when JSON-LD lacks the
    required core fields (caller should fall back to the LLM path).
    """
    t0 = time.perf_counter()

    ok, reason = _has_required_fields(jsonld)
    if not ok:
        print(f"     JSONLD-DIRECT: not eligible ({reason}); fall back to LLM")
        if timings is not None:
            timings["jsonld_check_ms"] = int((time.perf_counter() - t0) * 1000)
        return None

    # Start from a shallow copy of the JSON-LD so we can stamp fields without
    # mutating the caller's dict.
    payload: dict[str, Any] = dict(jsonld)

    # Normalize the few things that need it before sanitize sees them.
    payload["recipeInstructions"] = _flatten_instructions(payload.get("recipeInstructions"))

    # Source metadata. URL is normalized so the recipe stored is canonical
    # against the metabase_url table.
    normalized = normalize_url(source_url) if source_url else ""
    existing_source = payload.get("_source") or {}
    if normalized and not existing_source.get("originalUrl"):
        existing_source["originalUrl"] = normalized
    origin = existing_source.get("origin") or root_domain(normalized) or title
    if origin:
        existing_source["origin"] = origin
    existing_source.setdefault("type", "web")
    payload["_source"] = existing_source

    if normalized:
        scoring = payload.get("_scoring") or {}
        scoring.setdefault("rootDomain", root_domain(normalized))
        if title:
            scoring.setdefault("rawTitle", title)
        payload["_scoring"] = scoring

    # Run the same recipe-text score we use elsewhere so the UI shows a
    # comparable value. The text we score on is the ingredients + instructions
    # joined, since we don't have the full body markdown here.
    score_text = "\n".join([str(x) for x in payload.get("recipeIngredient") or []])
    instr_for_score = []
    for step in payload["recipeInstructions"]:
        if isinstance(step, dict):
            instr_for_score.append(step.get("text", ""))
        else:
            instr_for_score.append(str(step))
    score_text += "\n" + "\n".join(instr_for_score)
    validation = is_recipe(score_text)
    print(f"     JSONLD-DIRECT VALIDATE: {validation['reason']} -> "
          f"{'accepted' if validation['accepted'] else 'rejected (proceeding anyway)'}")

    try:
        sanitized = sanitize_recipe_data(payload)
        stamp_validation_on_recipe(sanitized, validation)
        recipe = RecipeModel.model_validate(sanitized).model_dump(by_alias=True)
    except Exception as e:
        print("     ERROR: jsonld_to_recipe validation failed:", e)
        print("     DEBUG: payload:\n", json.dumps(payload, indent=2, default=str)[:2000])
        if timings is not None:
            timings["jsonld_direct_ms"] = int((time.perf_counter() - t0) * 1000)
        return None

    if timings is not None:
        timings["jsonld_direct_ms"] = int((time.perf_counter() - t0) * 1000)
    return recipe


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m extract.jsonld_to_recipe <jsonld_file.json> [source_url]")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
    src_url = sys.argv[2] if len(sys.argv) > 2 else ""
    result = jsonld_to_recipe(data, source_url=src_url)
    if result is None:
        print("INELIGIBLE (or failed validation)")
        sys.exit(1)
    print(json.dumps(result, indent=2))