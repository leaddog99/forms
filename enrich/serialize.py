"""The profile serializer — the one canonical "what comes back" chokepoint.

Three profiles (see docs/split-phase1-map.md §0 and SplitSpec "three-tier field
partition"):

  full    everything (internal processing; the body included)
  static  body kept, per-user/claim/owner fields dropped — BCC's saved copy
          (delegates to recipe_model.static_subset, the existing boundary)
  public  body SEALED; only work-product + rich-result envelope + keys + a
          MANDATORY source link — TBOTB's public / subscriber emit boundary

WHITELIST, never blacklist (SplitSpec §131): the day someone adds a new field
carrying source text, it must stay out by default. So `public` lists exactly what
may cross; everything else is sealed by omission.
"""
from __future__ import annotations

from typing import Optional


class SealError(Exception):
    """Raised when a record cannot be emitted publicly — currently only when it
    has no resolvable source URL (the mandatory-link gate)."""


# --- public emit allow-list (TBOTB destination site + subscriber read API) ---
# Top-level fields that may cross the public boundary.
_PUBLIC_TOP_LEVEL = frozenset({
    # rich-result envelope — the fields publishers ship in schema.org Recipe
    # JSON-LD *so* search engines syndicate them and link back (SplitSpec §139).
    "name", "headline", "image", "author", "publisher",
    "datePublished", "dateModified",
    "prepTime", "cookTime", "totalTime", "recipeYield",
    "recipeCategory", "recipeCuisine", "keywords", "tags",
    "aggregateRating", "nutrition",
    # our work product — the only expressive thing the corpus serves.
    "provenance", "classification", "editorial",
    "_scoring", "_identity", "_master",
})
# DELIBERATELY SEALED (never listed, so omitted by default):
#   recipeIngredient, recipeInstructions  -> the substitute-recipe body
#   description, notes                     -> publisher prose (our blurb is in editorial)
#   video, equipment, servingSuggestions, cookingMethod, suitableForDiet
#   imageSource, inputImage                -> source/raw image refs
#   any raw JSON-LD blob
# If a future field carries source text, it stays sealed until explicitly added.

# _source sub-keys that may cross (keys / provenance / attribution only).
_PUBLIC_SOURCE_SUBKEYS = frozenset({
    "originalUrl",     # the mandatory link / canonical pointer
    "origin",          # root domain / origin label
    "type",            # 'web' | ...
    "siteName",        # human attribution ("Bon Appétit")
    "author",          # site byline
    "publishedTime",
    "modifiedTime",
    # NOTE: previewImage/previewImageAlt (our cooped copy) are SEALED here —
    # the cooped copy is the LOCAL image-rights posture (§149); public display
    # uses thumbnail-and-link via the top-level `image` URL. Revisit (DL-7).
    # previewDescription is publisher prose -> SEALED.
})


def corpus_public_view(recipe: dict) -> dict:
    """Return the public-safe view of a corpus record: work-product + envelope +
    keys + a mandatory outbound source link. Raises SealError if there is no
    resolvable source URL (an index points; it never becomes the destination)."""
    rec = recipe or {}
    src = rec.get("_source") or {}
    link = (src.get("originalUrl") or "").strip()
    if not link:
        raise SealError("public emit requires a resolvable source URL (none found)")

    out = {k: v for k, v in rec.items() if k in _PUBLIC_TOP_LEVEL}
    filtered_src = {k: v for k, v in src.items() if k in _PUBLIC_SOURCE_SUBKEYS}
    if filtered_src:
        out["_source"] = filtered_src
    out["sourceLink"] = link  # explicit mandatory outbound link
    return out


def apply_profile(recipe: dict, profile: str) -> dict:
    """Shape a recipe for output per profile. `full` is identity; `static` and
    `public` are the two seal boundaries."""
    if profile == "full":
        return recipe
    if profile == "static":
        from recipe_model import static_subset
        return static_subset(recipe)
    if profile == "public":
        return corpus_public_view(recipe)
    raise ValueError(f"unknown profile: {profile!r} (expected full|static|public)")
