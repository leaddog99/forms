# TODO (revisit): add a sourceImage field to RecipeModel to retain the original
# image used during AI extraction. Useful during edit for cross-referencing
# handwritten notes the extractor may have missed. See matching TODOs in
# save_recipe_api.py (storage on /extract) and recipe_form_styled.html (UI).
# Decide: single field vs. list (re-extractions / multi-page sources), URL vs.
# stored path, and back-fill strategy for existing records.

from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Union, Literal
from datetime import datetime


# ============================================================================
# STATIC vs USER field classification.
#
# Every recipe blob contains two kinds of fields:
#   - STATIC ("platonic") — the recipe itself: name, ingredients, steps,
#     LLM enrichment (provenance/classification/editorial), URL-keyed
#     scoring. Same across all users for a given URL.
#   - USER — bound to a specific row/owner: the row UUID, owner user_id,
#     accept/reject status, visibility, claim provenance, ephemeral
#     debug fields, and (future) per-user comments.
#
# `static_subset()` returns a copy of a recipe blob containing ONLY the
# static fields. Use it whenever data flows between owners:
#   - claim (master → user, user → user)
#   - cache write (an extract result feeds the URL-keyed cache; we must
#     not leak the writing user's accept/reject decision)
#   - cache read into a new form (cache → user; user fills in personal
#     fields fresh)
#   - master backfill / promotion
# Centralized here so the two cache/claim sites can't drift.
# ============================================================================

# Fields belonging to the platonic recipe — same for everyone at a URL.
# Includes the LLM enrichment blocks (provenance/classification/editorial)
# and the URL-keyed Moz scoring (_scoring). All of these are safe to
# carry across owner boundaries.
STATIC_TOP_LEVEL_FIELDS = frozenset({
    # schema.org wire fields
    "@context", "@type",
    # core recipe content
    "name", "headline", "description", "image", "author", "publisher",
    "datePublished", "dateModified",
    "recipeYield", "prepTime", "cookTime", "totalTime",
    "recipeCategory", "recipeCuisine", "keywords", "tags",
    "aggregateRating", "nutrition",
    "recipeIngredient", "recipeInstructions",
    "notes", "video", "servingSuggestions", "cookingMethod",
    "equipment", "suitableForDiet",
    "imageSource", "inputImage",
    # LLM enrichment — same dish, same provenance/classification regardless
    # of who owns this row.
    "provenance", "classification", "editorial",
    # URL-keyed Moz scoring (PA/DA/OU/rootDomain/recipeScore/etc.)
    "_scoring",
    # Batch lineage — which curation batch this recipe came from.
    # URL-static (a URL was extracted from one batch; doesn't change per
    # owner). Keep so master/cache lineage survives a claim.
    "_batch",
    # Structured dish identity card (extract.identity_card output) —
    # ingredientRoles + cuisine + ethnicity + technique + likelyDish +
    # primaryIngredients. URL-static: the dish IS the dish regardless
    # of who owns the row, so the card travels through claim / cache
    # / promote alongside provenance and classification.
    "_identity",
})

# Fields tied to a specific row / owner — must be re-minted, dropped, or
# explicitly re-stamped after the copy.
USER_TOP_LEVEL_FIELDS = frozenset({
    "id",                # row UUID — minted fresh per row
    "recipe_id",         # duplicate of id that some flows write into the
                         #   data blob; must be re-minted
    "user_id",           # row owner — set to target user
    "_imported_from",    # ephemeral debug ("imported via /extract-from-url
                         #   on 2026-03-12")
    "_editor_version",   # ephemeral debug
    "_access",           # per-user visibility / sharing
    "current_status",    # this user's accept/reject decision (validator
                         #   re-stamps it on every extract anyway)
    "_master",           # dish-library provenance (kind/dish/refreshed_at/
                         #   rank/queries). Tied to the row's place in the
                         #   dish library — a claimed copy should NOT inherit
                         #   "kind=top" since it's no longer in the top-N;
                         #   promote-to-master re-stamps fresh on the new row.
    # Future: "userComments" — when added, list here so cache/claim
    # never carry one user's comments to another.
})

# _source has a MIX of static and user-specific sub-keys. originalUrl /
# origin / type identify the dish; affiliateUrl / claimedFrom / claimedAt
# / claimedFromRecipeId are owner-specific provenance.
_SOURCE_STATIC_SUBKEYS = frozenset({
    "type",          # 'web' | 'local' | 'cookbook'
    "origin",        # root domain or origin label
    "originalUrl",   # canonical URL — the cache key itself
    "previewImage",  # og:image URL declared by the source for sharing
                     # — the explicitly consent-given preview thumbnail.
                     # Used by the TBOTB display as a clickable link tile.
    "previewDescription",  # og:description — one-sentence teaser the
                     # source publishes for social-card display. Useful
                     # under tiles + as a fallback when editorial.opinion
                     # hasn't run.
    "previewImageAlt",  # og:image:alt — descriptive alt text for the
                     # cooped image tile. Used for accessibility.
    "siteName",      # og:site_name — human-readable site name
                     # ("Bon Appétit") vs raw hostname. Display in
                     # tile attribution + sidebar.
    "author",        # article:author — site-published byline. Falls
                     # back when JSON-LD recipe.author isn't set.
    "publishedTime", # article:published_time (ISO 8601)
    "modifiedTime",  # article:modified_time (ISO 8601)
    "pageScreenshot", # captured screenshot of the source page (above-
                     # fold view), processed to the standard 1500x1000
                     # landscape via process_thumbnail. Stamped on
                     # extract via input.pipeline.screenshot_pipeline.
                     # Survives claim/cache like the rest of the
                     # consent-given preview metadata.
})


def static_subset(recipe_data: dict) -> dict:
    """Return a copy of `recipe_data` containing only the platonic
    recipe fields. Strips per-row state (id/user_id/recipe_id), per-user
    state (_access/current_status), claim provenance (_source.claimedFrom/
    claimedAt/claimedFromRecipeId), personal affiliate links
    (_source.affiliateUrl), and ephemeral debug fields (_imported_from,
    _editor_version).

    Designed for the boundaries where a recipe crosses owners: claim,
    cache-write, and future user-to-user share. Callers that need to
    add fresh per-row state (new UUID, target user_id, fresh
    claimedFrom stamp) do so on top of this output.
    """
    out: dict = {}
    for k, v in (recipe_data or {}).items():
        if k in STATIC_TOP_LEVEL_FIELDS:
            out[k] = v
    src = (recipe_data or {}).get("_source") or {}
    if isinstance(src, dict):
        filtered = {k: v for k, v in src.items() if k in _SOURCE_STATIC_SUBKEYS}
        if filtered:
            out["_source"] = filtered
    return out

class AggregateRating(BaseModel):
    type: str = Field(default="AggregateRating", alias="@type")
    ratingValue: float
    reviewCount: int

class NutritionInfo(BaseModel):
    calories: Optional[str] = ""
    fatContent: Optional[str] = ""
    carbohydrateContent: Optional[str] = ""
    proteinContent: Optional[str] = ""

class HowToStep(BaseModel):
    type: str = Field(default="HowToStep", alias="@type")
    position: Optional[int] = None
    name: Optional[str] = None
    text: str
    image: Optional[str] = None
    imageCredit: Optional[str] = None

class Tool(BaseModel):
    type: str = Field(default="HowToTool", alias="@type")
    name: str

class VideoObject(BaseModel):
    type: str = Field(default="VideoObject", alias="@type")
    name: str
    contentUrl: str
    thumbnailUrl: Optional[str] = ""
    uploadDate: Optional[str] = ""
    description: Optional[str] = ""

class Author(BaseModel):
    type: str = Field(default="Person", alias="@type")
    name: Optional[str] = ""
    image: Optional[str] = None

class Provenance(BaseModel):
    ethnicity: Optional[str] = ""
    originRegion: Optional[str] = ""
    firstDocumented: Optional[str] = None
    traditionalContext: Optional[str] = ""
    notableVariations: Optional[List[str]] = []
    relatedDishes: Optional[List[str]] = []
    sources: Optional[List[dict]] = []

class AccessControl(BaseModel):
    visibility: Optional[str] = ""
    sharedWith: Optional[List[str]] = []

class SourceInfo(BaseModel):
    type: Optional[str] = ""
    origin: Optional[str] = ""
    originalUrl: Optional[str] = ""
    affiliateUrl: Optional[str] = ""
    # og:image / twitter:image URL the source explicitly declares for
    # link-preview sharing. Extracted at fetch time in
    # to_markdown.html_to_markdown.extract_og_meta and stamped on
    # /extract-from-url + the in-process callable. Used as the
    # TBOTB display tile because it's the consent-given preview
    # image (vs. hotlinking the recipe's hero photo from JSON-LD).
    previewImage: Optional[str] = ""
    # og:description / twitter:description — the teaser sentence the
    # source publishes for social-card display. Goes under the tile
    # title as a one-liner; also a useful fallback when our
    # LLM-generated editorial.opinion hasn't been run on the row.
    previewDescription: Optional[str] = ""
    # og:image:alt — descriptive alt text. Render as <img alt="…">
    # on the cooped tile for accessibility.
    previewImageAlt: Optional[str] = ""
    # og:site_name — human-readable site name ("Bon Appétit") vs
    # raw hostname. Use in tile attribution + sidebar where it
    # exists; fall back to the parsed root domain otherwise.
    siteName: Optional[str] = ""
    # article:author — source-published byline. JSON-LD recipe.author
    # is preferred when present; this is the fallback.
    author: Optional[str] = ""
    # article:published_time / article:modified_time — ISO 8601.
    # Useful for "recipe last updated" UI + freshness scoring.
    publishedTime: Optional[str] = ""
    modifiedTime: Optional[str] = ""
    # Captured screenshot of the source page (above-fold view, ~800px
    # tall, then processed to the corpus-standard 1500×1000 landscape).
    # Stored locally / S3 via image_store; URL points at our static
    # mount. Gives the form + cookbook display a "this is what the
    # source actually looked like" affordance. Filename pattern
    # `recipe-screens/<recipe_id>-<sha8>.jpg` lets us trace files
    # back to recipes independently of the DB.
    pageScreenshot: Optional[str] = ""


# Dish-library provenance block — stamped on master_recipes rows so the
# delete-and-replace refresh logic can find them, and so the live form
# can later suggest "better recipes for this dish" by joining on dish.
# kind drives refresh behavior:
#   "top"            — batch-sourced, replaced on each dish refresh
#   "editors_choice" — curator's manual Promote-to-Master, permanent
#   "legacy"         — pre-existing rows from before this scheme
# See memory/project_dish_library.md.
class MasterMetadata(BaseModel):
    model_config = {"populate_by_name": True, "extra": "allow"}
    kind: Optional[str] = None                    # "top" | "editors_choice" | "legacy"
    dish: Optional[str] = None                    # join key into dishes table
    refreshed_at: Optional[str] = None            # ISO-8601 UTC; top-kind only
    rank: Optional[int] = None                    # within dish, top-kind only
    queries: Optional[List[str]] = None           # queries that surfaced this URL
    batch_source: Optional[str] = None            # provenance debug

# Pipeline-side metadata. Defaults are empty so interactive saves don't have to
# populate them; batch stages fill them in over time.
class ScoringMetadata(BaseModel):
    pageAuthority: float = 0.0
    domainAuthority: float = 0.0
    ouScore: float = 0.0
    rootDomain: str = ""
    rawTitle: str = ""
    iconUrl: str = ""
    recipeScore: int = 0
    recipeScoreThreshold: int = 0

class ClassificationMetadata(BaseModel):
    confidence: int = 0
    reasoning: str = ""
    hierarchyPath: str = ""
    story: str = ""
    # Cookbook chapter (flat enum, one of CHAPTERS from
    # extract/chapter_classifier.py). Populated at extract time by the
    # keyword-shortcut + LLM-fallback classifier. Editable on the form;
    # user override persists.
    chapter: str = ""


# Editorial commentary on a specific recipe (not the dish — see story for
# dish history). Generated by the same enrich LLM call. Three free-form
# markdown strings so the LLM can format with paragraphs/bullets as it
# sees fit; the form renders them in auto-grow textareas.
class EditorialMetadata(BaseModel):
    # Editorial opinion on THIS recipe: technique, ingredient ratios, what
    # makes it work or wobble, who'd enjoy it.
    opinion: str = ""
    # Prose interpretation of the PA/DA/OU scores: how the page authority
    # relates to the domain authority, whether the recipe punches above or
    # below its domain's weight.
    scoreCommentary: str = ""
    # Markdown bullets like "- **Olive oil**: raw in this recipe, so
    # quality dominates. Look for cold-pressed Ligurian or Tuscan...".
    # Stored as a single string; the LLM is free to bullet or paragraph.
    # No affiliate links yet — placeholders for product names only.
    sourcingNotes: str = ""

class StatusField(BaseModel):
    value: Literal["accepted", "rejected"]
    reason: Optional[str] = None
    timestamp: Optional[str] = None

class RecipeModel(BaseModel):
    model_config = {"populate_by_name": True, "extra": "allow"}

    context: Optional[str] = Field(default="https://schema.org", alias="@context")
    type: Optional[str] = Field(default="Recipe", alias="@type")
    id: Optional[str] = ""
    name: Optional[str] = ""
    description: Optional[str] = ""
    image: Optional[List[str]] = []
    author: Optional[Author] = None
    datePublished: Optional[str] = ""
    dateModified: Optional[str] = ""
    recipeYield: Optional[str] = ""
    prepTime: Optional[str] = ""
    cookTime: Optional[str] = ""
    totalTime: Optional[str] = ""
    recipeCategory: Optional[str] = ""
    recipeCuisine: Optional[str] = ""
    keywords: Optional[List[str]] = []
    aggregateRating: Optional[AggregateRating] = None
    nutrition: Optional[NutritionInfo] = None
    recipeIngredient: Optional[List[str]] = []
    recipeInstructions: Optional[List[HowToStep]] = []
    notes: Optional[str] = ""
    tags: Optional[List[str]] = []
    video: Optional[Union[VideoObject, str]] = None
    servingSuggestions: Optional[str] = ""
    cookingMethod: Optional[str] = ""
    equipment: Optional[List[Tool]] = []
    suitableForDiet: Optional[List[str]] = []
    provenance: Optional[Provenance] = None
    imageSource: Optional[str] = ""
    inputImage: Optional[str] = None
    imported_from: Optional[str] = Field(default="", alias="_imported_from")
    editor_version: Optional[str] = Field(default="", alias="_editor_version")
    access: Optional[AccessControl] = Field(default=None, alias="_access")
    source: Optional[SourceInfo] = Field(default=None, alias="_source")
    scoring: Optional[ScoringMetadata] = Field(default=None, alias="_scoring")
    # Dish-library block — present on master_recipes rows from a batch
    # refresh (kind="top") or a curator promote (kind="editors_choice").
    # Absent on personal recipes. Declared explicitly so it survives
    # model_dump(by_alias=True) the same way _source/_scoring do.
    master: Optional[MasterMetadata] = Field(default=None, alias="_master")
    classification: Optional[ClassificationMetadata] = None
    editorial: Optional[EditorialMetadata] = None
    current_status: Optional[StatusField] = None
    # Owner discriminator. 0 = sys-admin / batch-curated master collection
    # (lives in master_recipes table); any other value = personal collection
    # (recipes table). Pydantic must know about this field explicitly so it
    # survives `model_dump(by_alias=True, exclude_none=True)` in
    # sanitize_recipe_data — `extra="allow"` accepts unknown fields on
    # construction but drops them on dump.
    user_id: Optional[int] = None

    @field_validator('servingSuggestions', mode='before')
    @classmethod
    def convert_serving_suggestions(cls, v):
        if isinstance(v, list):
            return ", ".join(str(item) for item in v if item)
        return v

    # Dead code removed 2026-05-27: an older RecipeModel-based image
    # prompt builder (file_exists / needs_image_generation /
    # prefers_dish_image / generate_prompt / _extract_visual_details_
    # from_instructions, plus their is_nullish helper). The current
    # image-generation pipeline lives in image_gen_openai.py
    # (_build_dish_prompt) and never touched any of these. Confirmed
    # zero callers across active source before removal.
