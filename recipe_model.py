# TODO (revisit): add a sourceImage field to RecipeModel to retain the original
# image used during AI extraction. Useful during edit for cross-referencing
# handwritten notes the extractor may have missed. See matching TODOs in
# save_recipe_api.py (storage on /extract) and recipe_form_styled.html (UI).
# Decide: single field vs. list (re-extractions / multi-page sources), URL vs.
# stored path, and back-fill strategy for existing records.

import re
import html as _html
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional, Union, Literal
from datetime import datetime

# Step-anchored cook rework block (recipe._cook). cook_model imports only
# pydantic, so no import cycle.
from cook_model import CookMetadata


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
    "imageSource", "inputImage", "sourceImage",
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
    # Ingredient-context-aware measurement conversions, one entry per
    # recipeIngredient (enrich.measurement). Derived purely from
    # the recipe content (King Arthur densities), so URL-static: same for
    # every owner. Powers the metric/imperial toggle in the editor.
    "_measurements",
    # Hero-image metadata captured at coopt/upload (image_pipeline
    # standardize_and_meta): width/height/format/bytes/orientation/
    # orig dims/localized/source_url. URL-static (a property of the image
    # we own), so it travels through claim/cache/promote.
    "_imageMeta",
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
    "originalLanguage", # ISO 639-1 of the source page when non-English
                     # (e.g. 'el' for Greek). Empty/absent for English
                     # pages. Set by the extraction-stage translation
                     # in intake/translate.py.
    "translated",    # bool — True when the recipe body fields are the
                     # English translation of a non-English source.
                     # Lets the UI render a "Translated from X" pill +
                     # offer a "view original" affordance.
    "translatedAt",  # ISO 8601 timestamp of the translation call. Lets
                     # us re-run translation later (improved prompt,
                     # better model) and only on rows that have it set.
    "originalTitle", # The source-language title preserved from the
                     # page's <h1>/<title> before translation. Used by
                     # the UI's "view original" link affordance.
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

# --- schema.org shape coercion -------------------------------------------------
# Real-world JSON-LD is loose: a field that schema.org types as Text often
# arrives as a number, a list, or an object ({"@value":…}/{"url":…}). Pydantic's
# `str` rejects those, and because a recipe validates as a WHOLE, one off-type
# field silently drops the entire recipe from a batch — e.g. recipeYield=8 (int)
# and video.thumbnailUrl=[…] (list), both seen 2026-06-09. These helpers
# normalize at the MODEL boundary so every ingest path (extract / save /
# bookmarklet / API) is covered once. (feedback_single_path + recipe_model_first.)

# Source recipe prose (JSON-LD description, HowToStep.text…) frequently
# carries raw HTML — <p>, <i>, <a>, &amp;, &#39; — which then renders as
# literal markup in the form's plain-text intro/steps. Strip it at the
# model boundary so the stored value is clean for EVERY field that coerces
# through _as_str (description, instruction text, name, headline…) and for
# every recipe (master AND personal user recipes share this model).
_HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")
# Only act on strings that ACTUALLY look like markup, so we never touch a
# legitimate lone "<" ("cook < 5 min") or "&" ("salt & pepper") in plain prose.
_LOOKS_HTML_RE = re.compile(r"<[a-zA-Z/][^>]*>|&[a-zA-Z][a-zA-Z0-9]+;|&#\d+;")

def _strip_html(s):
    if not isinstance(s, str) or not _LOOKS_HTML_RE.search(s):
        return s
    txt = _html.unescape(s)            # &amp;->&, &#39;->', &lt;->< (then re-stripped)
    txt = _HTML_TAG_RE.sub(" ", txt)   # drop tags (space so "a<br>b" -> "a b")
    txt = re.sub(r"\s+", " ", txt)
    txt = re.sub(r"\s+([,.;:!?])", r"\1", txt)  # no space before punctuation
    return txt.strip()

def _normalize_md_bullets(v):
    """Put each `- **Name**…` markdown bullet on its OWN line. The enrich LLM
    sometimes returns sourcingNotes as one run-together blob (no newlines between
    bullets) → the form renders a wall of text. Split before each bold-bullet
    marker so it's a real list. Idempotent; only acts when bold bullets are
    present, so plain prose / paragraph notes are left untouched."""
    if not isinstance(v, str) or "**" not in v:
        return v
    # Collapse whatever whitespace precedes a "- **"/"* **" marker into one newline.
    return re.sub(r"\s*[-*]\s+\*\*", "\n- **", v).strip()

def _as_str(v):
    """A schema.org Text-ish value → string (source HTML stripped from prose).
    number/bool→str, list→joined, dict→best key."""
    if v is None:
        return v
    if isinstance(v, str):
        return _strip_html(v)
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    if isinstance(v, dict):
        for k in ("@value", "value", "name", "text", "url", "@id"):
            if v.get(k):
                return _strip_html(str(v[k]))
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(s for s in (_as_str(x) for x in v) if s)
    return str(v)

def _as_one(v):
    """Like _as_str, but a list means 'first usable one' — for URLs / single values."""
    if isinstance(v, (list, tuple)):
        for x in v:
            s = _as_str(x)
            if s:
                return s
        return ""
    return _as_str(v)

def _as_yield(v):
    """recipeYield: a list means 'most descriptive' (['8','8 servings'] → '8 servings')."""
    if isinstance(v, (list, tuple)):
        items = [s for s in (_as_str(x) for x in v) if s]
        return max(items, key=len) if items else ""
    return _as_str(v)

def _as_str_list(v, *, split=False):
    """→ list[str]: bare string → [string] (or comma-split if split=True);
    dict → [best-key]; list → each item coerced, empties dropped."""
    if v is None:
        return v
    if isinstance(v, str):
        if split:
            return [p.strip() for p in v.split(",") if p.strip()]
        return [v] if v.strip() else []
    if isinstance(v, dict):
        s = _as_str(v)
        return [s] if s else []
    if isinstance(v, (list, tuple)):
        return [s for s in (_as_str(x) for x in v) if s]
    s = _as_str(v)
    return [s] if s else []

def _as_num(v, kind):
    """Rating/count → float|int, tolerating '', None, '4.8 stars', '1,234'."""
    if v in (None, ""):
        return None
    try:
        token = str(v).replace(",", "").split()[0]
        return float(token) if kind is float else int(float(token))
    except (ValueError, IndexError):
        return None


class AggregateRating(BaseModel):
    type: str = Field(default="AggregateRating", alias="@type")
    ratingValue: Optional[float] = None
    reviewCount: Optional[int] = None

    @field_validator('ratingValue', mode='before')
    @classmethod
    def _v_rating(cls, v): return _as_num(v, float)

    @field_validator('reviewCount', mode='before')
    @classmethod
    def _v_count(cls, v): return _as_num(v, int)

class NutritionInfo(BaseModel):
    calories: Optional[str] = ""
    fatContent: Optional[str] = ""
    carbohydrateContent: Optional[str] = ""
    proteinContent: Optional[str] = ""

    @field_validator('calories', 'fatContent', 'carbohydrateContent', 'proteinContent', mode='before')
    @classmethod
    def _v_text(cls, v): return _as_str(v)

class HowToStep(BaseModel):
    type: str = Field(default="HowToStep", alias="@type")
    position: Optional[int] = None
    name: Optional[str] = None
    text: Optional[str] = ""
    image: Optional[str] = None
    imageCredit: Optional[str] = None

    @field_validator('name', 'text', mode='before')
    @classmethod
    def _v_text(cls, v): return _as_str(v)

    @field_validator('image', 'imageCredit', mode='before')
    @classmethod
    def _v_url(cls, v): return _as_one(v)

class Tool(BaseModel):
    type: str = Field(default="HowToTool", alias="@type")
    name: Optional[str] = ""
    # Size is the product-commerce class grain (equipment -> product_class,
    # e.g. "Saucepans (2 qt)"). Populated at extract time (verb/pan-derived),
    # by the cook-rework mirror, or by hand in the editor. Present ONLY when the
    # recipe states/implies one — never invented. Without this field the model
    # would silently DROP size on every RecipeModel round-trip (extraction, save).
    size: Optional[str] = None

    @field_validator('name', mode='before')
    @classmethod
    def _v_text(cls, v): return _as_str(v)

class VideoObject(BaseModel):
    type: str = Field(default="VideoObject", alias="@type")
    name: Optional[str] = ""
    contentUrl: Optional[str] = ""
    thumbnailUrl: Optional[str] = ""
    uploadDate: Optional[str] = ""
    description: Optional[str] = ""

    @field_validator('name', 'uploadDate', 'description', mode='before')
    @classmethod
    def _v_text(cls, v): return _as_str(v)

    @field_validator('contentUrl', 'thumbnailUrl', mode='before')
    @classmethod
    def _v_url(cls, v): return _as_one(v)

class Author(BaseModel):
    type: str = Field(default="Person", alias="@type")
    name: Optional[str] = ""
    image: Optional[str] = None

    @field_validator('name', mode='before')
    @classmethod
    def _v_name(cls, v): return _as_str(v)

    @field_validator('image', mode='before')
    @classmethod
    def _v_img(cls, v): return _as_one(v)

class Provenance(BaseModel):
    ethnicity: Optional[str] = ""
    originRegion: Optional[str] = ""
    firstDocumented: Optional[str] = None
    traditionalContext: Optional[str] = ""
    notableVariations: Optional[List[str]] = []
    relatedDishes: Optional[List[str]] = []
    sources: Optional[List[dict]] = []

    # Tolerant coercion (same philosophy as the rest of the model: an off-type
    # value must never drop the whole recipe). The enrich/provenance LLM block
    # sometimes emits a bare STRING where a list is declared — wrap it as a
    # single-element list rather than 500 the save. notableVariations is often a
    # prose sentence and relatedDishes a parenthetical comma list, so do NOT
    # split (a naive comma split would shred "Vichyssoise (French leek…)").
    @field_validator('ethnicity', 'originRegion', 'firstDocumented', 'traditionalContext', mode='before')
    @classmethod
    def _v_text(cls, v): return _as_str(v)

    @field_validator('notableVariations', 'relatedDishes', mode='before')
    @classmethod
    def _v_list(cls, v): return _as_str_list(v)

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
    # === Translation provenance =====================================
    # When the source page is non-English, the recipe body fields are
    # the canonical English translation. These four keys carry the
    # provenance signal so the UI can render a "Translated from X" pill
    # and a "view original" link.
    originalLanguage: Optional[str] = ""    # ISO 639-1 ("el", "it", ...)
    translated: Optional[bool] = False
    translatedAt: Optional[str] = ""        # ISO 8601 UTC
    originalTitle: Optional[str] = ""       # source-language title


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
    """ABSENT MEANS UNMEASURED. Every numeric field here defaults to None, not
    0.0, because 0 is not a legal measured value for any of them — Moz PA/DA are
    >= 1 for any page it has crawled, and a cohort percentile of 0 alongside
    fieldN 0 means there was no cohort, not that the recipe came last.

    These WERE `= 0.0`, and that is the root of a bug fixed three times at the
    sink and never at the source (2026-08-06). Every extract passes through this
    model, so the defaults MANUFACTURED a full block of zeros on every recipe,
    which then read as measured everywhere downstream. The repairs — the
    `pre_scored` writer (07-30), `_sanitize_scoring` at the save boundary
    (08-03), and the restart that finally made the latter run (08-06) — were all
    deleting values this class had just created.

    Three genuinely different states were collapsed into that one 0: NOT
    APPLICABLE (a handwritten recipe minted a /r/<uuid> self-URL; no third party
    can link to it, so PA is meaningless), NOT MEASURED YET (a real page Moz
    hasn't crawled), and NO COHORT (saved outside a dish batch, so the field
    statistics have nothing to rank within). None expresses all three honestly;
    0.0 expresses none of them.

    Audited 2026-08-06 before flipping: every reader already tolerates absence —
    `setScoreChip` renders "—" for null, `blend._power` returns None unless both
    operands are numbers, `enrich_recipe` guards with `is not None`/truthiness,
    chapters.py filters `IS NOT NULL`, dishes.py wraps in COALESCE. Nothing
    consumed the zeros; they only ever misinformed.

    `traffic`/`trafficPct` below were already Optional — this brings the rest of
    the block in line with them. Strings keep "" (an empty name is not a false
    measurement) and recipeScore keeps 0 (our own validator computes it on
    essentially every row).
    """
    pageAuthority: Optional[float] = None
    domainAuthority: Optional[float] = None
    ouScore: Optional[float] = None
    # SUPERSEDED 2026-08-12 by adjustedDomainAuthority/adjustedOuScore below.
    # The old correction rewrote PA to a "free-equivalent"; it is no longer
    # computed or stamped anywhere. Kept declared ONLY so rows written while it
    # was live remain legal on the wire — do not populate it.
    adjustedPageAuthority: Optional[float] = None
    # Paywall DA-adjustment (pa_gap_v1). DA is measured across the whole domain
    # while a gated publisher's recipes sit behind the wall, so OU judges the
    # page against an ungated domain's bar. These carry the DA that publisher's
    # WALLED SECTION behaves like, and the OU that follows from it. PA is left
    # exactly as measured — it is the one thing here that was observed.
    #
    # ABSENT unless the publisher carries a calibrated adjustment, so a reader
    # can tell "no adjustment applies" from "adjusted to the same number"
    # (memory/feedback_absent_not_zero).
    #
    # DERIVED ON READ, never persisted: `get_recipe` stamps them from the LIVE
    # calibration. A stored copy would go stale the moment the calibration moves
    # — the exact failure that left four calibrations untouched for seven weeks.
    adjustedDomainAuthority: Optional[float] = None
    adjustedOuScore: Optional[float] = None
    # Raw clout (DA+PA) and the two in-cohort PERCENTILE ranks (0-100) the
    # OU/power blend ranks on — the axes of the exceptionality×clout 2x2.
    # Stamped from the batch entry (rank_by_blend) so the editorial authority
    # commentary can read where the page sits, not just its raw OU.
    power: Optional[float] = None
    ouPercentile: Optional[float] = None
    powerPercentile: Optional[float] = None
    # Field context (dish-level): the cohort's absolute clout (DA+PA, 0-200) +
    # spread, and any geo/site restriction in the query (e.g. "gr" = Greek-only
    # search). Lets the commentary say "established-publisher field" vs
    # "specialist-site field" and "among Greek sites".
    fieldAvgPower: Optional[float] = None
    fieldMaxPower: Optional[float] = None
    fieldMinPower: Optional[float] = None
    fieldN: Optional[int] = None
    fieldScope: str = ""
    # How this dish's field clout ranks among its CHAPTER siblings (0-100):
    # high = a popular/contested dish (covered by strong publishers), low = a
    # niche dish. Lets the commentary say "a hotly-covered dish" vs "a quiet
    # corner". Stamped at dish refresh from the chapter's other dishes.
    dishCompetitivenessPct: Optional[float] = None
    # PA PROVENANCE — Moz's http_code at the moment pageAuthority was measured.
    # NOT declared here when the flag shipped (2026-08-04), so pydantic silently
    # DROPPED it on every extract that round-trips this model: the value was
    # written by url_scoring, survived the batch and save paths, and vanished
    # here. The one field whose 0 IS a real measurement — "Moz answered and has
    # no data", i.e. any PA on the row is the domain-derived placeholder.
    # None = never verified. See input/pipeline/url_scoring.moz_http_status.
    mozHttpCode: Optional[int] = None
    rootDomain: str = ""
    rawTitle: str = ""
    iconUrl: str = ""
    recipeScore: int = 0
    recipeScoreThreshold: int = 0
    # SEMrush per-page demand (publisher harvest, from the Top-Pages export): monthly
    # organic traffic + the page's share of the publisher's traffic. The meaningful
    # tiebreaker when PA saturates across a publisher's pages; shown in the editor's
    # scoring strip. None/0 when unknown (SERP-discovered or no SEMrush data).
    traffic: Optional[float] = None
    trafficPct: Optional[float] = None
    # WHY this row has no authority numbers, in plain language, written by
    # _sanitize_scoring at the save boundary. Undeclared until 2026-08-13 and so
    # dropped by this model on every round-trip — the same silent loss documented
    # above for mozHttpCode, and with the same consequence: a curator looking at
    # a strip of em-dashes had no way to tell "Moz hasn't crawled this yet" from
    # "something broke". Never a substitute for a value: it explains an absence,
    # it does not fill one.
    scoringNote: Optional[str] = None

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

    # Normalize a run-together bullet blob into one-bullet-per-line (the enrich
    # LLM is inconsistent about newlines). Only touches bold-bullet text.
    @field_validator('sourcingNotes', mode='before')
    @classmethod
    def _v_bullets(cls, v): return _normalize_md_bullets(v)

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
    # The ORIGINAL captured image(s) the recipe was extracted from (served URL),
    # SEPARATE from `image` (the hero). The hero is editable; this is immutable
    # provenance — retained so we can validate our extraction against the source
    # (e.g. handwritten margin notes) even after the hero is swapped. Set once at
    # extract time; mirrors `image` as a list. (Completes the top-of-file TODO.)
    sourceImage: Optional[List[str]] = []
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
    # Ingredient-context-aware measurement conversions — one entry per
    # recipeIngredient (see enrich.measurement). Declared
    # explicitly (like _source/_scoring) so it survives model_dump(by_alias);
    # left as List[dict] to stay flexible as the entry shape evolves.
    measurements: Optional[List[dict]] = Field(default=None, alias="_measurements")
    # Step-anchored cook rework (recipe_anchor capability) — the optimized,
    # mise-complete cook experience: ingredient uses, bundles, scheduled +
    # anchored steps with tips/checks. PARALLEL to the faithful schema.org
    # capture (recipeIngredient/recipeInstructions stay intact). The LLM emits
    # this DATA; the cook view is a deterministic render of it. See cook_model.py.
    # Declared explicitly so it survives model_dump(by_alias=True).
    cook: Optional[CookMetadata] = Field(default=None, alias="_cook")
    # Hero-image metadata from coopt/upload (dims/format/bytes/orientation/
    # localized/source). Declared explicitly so it survives model_dump
    # (extra='allow' accepts but DROPS unknown fields on dump).
    image_meta: Optional[dict] = Field(default=None, alias="_imageMeta")
    current_status: Optional[StatusField] = None
    # Owner discriminator. 0 = sys-admin / batch-curated master collection
    # (lives in master_recipes table); any other value = personal collection
    # (recipes table). Pydantic must know about this field explicitly so it
    # survives `model_dump(by_alias=True, exclude_none=True)` in
    # sanitize_recipe_data — `extra="allow"` accepts unknown fields on
    # construction but drops them on dump.
    user_id: Optional[int] = None

    # Text fields a source may deliver as a number / list / object — coerce so
    # an off-type value never drops the whole recipe (see _as_str above).
    @field_validator('name', 'description', 'datePublished', 'dateModified',
                     'recipeCategory', 'recipeCuisine', 'cookingMethod',
                     'servingSuggestions', 'imageSource', 'notes',
                     'prepTime', 'cookTime', 'totalTime', mode='before')
    @classmethod
    def _coerce_text(cls, v):
        return _as_str(v)

    @field_validator('recipeYield', mode='before')
    @classmethod
    def _coerce_yield(cls, v):
        return _as_yield(v)

    # List-of-Text fields a source may deliver as a bare string / object / list
    # of objects (image as a URL string or ImageObject; a single diet; etc.).
    @field_validator('image', 'sourceImage', 'recipeIngredient', 'suitableForDiet', 'tags', mode='before')
    @classmethod
    def _coerce_str_lists(cls, v):
        return _as_str_list(v)

    # keywords: schema.org allows a comma-separated STRING or a list — split it.
    @field_validator('keywords', mode='before')
    @classmethod
    def _coerce_keywords(cls, v):
        return _as_str_list(v, split=True)

    # Dead code removed 2026-05-27: an older RecipeModel-based image
    # prompt builder (file_exists / needs_image_generation /
    # prefers_dish_image / generate_prompt / _extract_visual_details_
    # from_instructions, plus their is_nullish helper). The current
    # image-generation pipeline lives in image_gen_openai.py
    # (_build_dish_prompt) and never touched any of these. Confirmed
    # zero callers across active source before removal.
