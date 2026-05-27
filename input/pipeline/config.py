# Batch pipeline configuration.
#
# Most tunables here load from `bcc_config.json` at the project root (a
# git-tracked file separate from `.env` for secrets). Edit that JSON +
# restart uvicorn to retune the batch funnel without touching code.
# Defaults are baked into each `.get()` below so a missing file or a
# missing key is non-fatal — the app starts with sane behavior.
#
# Cross-cutting constants that affect the LIVE form (save gate
# thresholds, BCC permalink domain, placeholder user id) intentionally
# stay in code — they're not batch-pipeline-only tunables. The user's
# call 2026-05-24.
#
# RECIPE_PHRASES stays in code too — it's 154 entries of semi-data and
# is reviewed/diffed in code rather than tuned in a config file.

import json as _json
from pathlib import Path as _Path

_CONFIG_PATH = _Path(__file__).resolve().parent.parent.parent / "bcc_config.json"


def _load_bcc_config() -> dict:
    """Return parsed bcc_config.json, or {} on missing-file (defaults
    apply). Malformed JSON raises — fail loudly so the developer fixes
    it instead of silently running with built-in defaults that mask the
    intended config."""
    if not _CONFIG_PATH.exists():
        print(f"[CONFIG] {_CONFIG_PATH} not found — using built-in defaults")
        return {}
    try:
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            data = _json.load(f)
        # Strip _comment_* keys so they don't trip an "unknown key" warning
        # if we ever add validation; they exist purely for the JSON file's
        # human readers.
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except _json.JSONDecodeError as e:
        print(f"[CONFIG] FATAL: {_CONFIG_PATH} is malformed JSON: {e}")
        raise


_cfg = _load_bcc_config()


RECIPE_PHRASES = [
    # ------ Structural recipe-section markers (high signal) ------
    # These appear in the structured "info box" of recipe pages (Schema.org-
    # rendered headers, recipe-card plugins, etc). Rare in narrative articles.
    "prep time", "cook time", "total time", "servings:", "servings ",
    "yield:", "yield ", "course:", "cuisine:", "calories:",
    "directions:", "instructions:", "method:",

    # ------ Quantified measurements (high signal) ------
    # Bare " cup" / " cups" / single-word spice names were pruned 2026-05-23
    # because they match narrative prose ("a cup of coffee", "Greek food uses
    # oregano"). Quantified versions stay — those are recipe-specific.
    "teaspoon", "teaspoons", "tsp", "tablespoon", "tablespoons", "tbsp",
    "1/4 cup", "1/3 cup", "1/2 cup", "2/3 cup", "3/4 cup",
    " ounce", " ounces", " oz", "-oz ", " lb", " pounds ", " kilos ",
    " gram", " grams", " g ", " ml", " litre", " litres",
    "pinch of", "sea salt", "kosher salt", "ground black pepper",
    "freshly ground", "ground cumin", "ground cinnamon", "cinnamon stick",
    "ground ginger", "cloves of", "to taste",

    # ------ Recipe-specific composite phrases ------
    "unsalted butter", "1 medium", "1 large", "medium onion", "large onion",
    "small onion", "red onion", "beef stock", "chicken stock", "chicken broth",
    "fish stock", "seafood stock", "hard-boil", "boiling salted water",
    "bring to a boil", "diced ", "sprig ", "finely chopped ",
    "in a food processor", "chopped ", "minced ", "grated ", "peeled ",
    "gutted ", "crushed ", "finely crushed ", "steam ", "boil ",

    # ------ Cooking-imperative verbs (high signal when chained with "the") ------
    "add the", "add remaining", "boil the", "chop the", "cook the",
    "crack the", "cut into", "dice the", "discard the", "finely chop ",
    "coarsely chop", "grate the", "peel the", "not peeled", "prepare the",
    "juice the", "juice of", "pulse the", "mix the", "remove the",
    "rinse the", "rinse off", "salt the", "low heat", "medium heat",
    "high heat", "spread the", "stir the", "stir in", "stir into",
    "fill the", "fill a", "set aside", "sprinkle the", "pour the",
    "scrub the", "seal the", "drizzle the", "spread the", "slide the",
    "fold in", "fold into", "bake for", "hand mixer", "stand mixer",
    "cool completely", "beat together", "beat until", "before serving",
    "serve immediately", "top with", "pour batter", "pour into", "pour over",
    "spread batter", "mash the", "mash a", "season to taste", "bring to a ",
    "serve as a ", "mince the", "whiz the", "bake the", "cool the",
    "preheat the", "knead the", "knead", "prick the", "spoon the",
    "chill for", "stuff the", "line the", "skim the", "strain the",
    "strain sauce", "for serving", "tear up", "sauté ",
]
# Pruned 2026-05-23: "ingredients", " cup", " cups", "clove", "allspice",
# "cayenne", "paprika", "bay leaves", "bay leaf", "oregano", "turmeric",
# "fenugreek", "parsley", "capers", "coriander", "cardamon", "ghee",
# "simmer", "parchment" — these were noise; appeared in narrative
# articles (AmericasTestKitchen /articles/ roundup hit 5 of them) without
# any recipe structure present.

# Built-in defaults if bcc_config.json is missing the key.
_DEFAULT_DISALLOWED_DOMAINS = [
    "youtube.com", "facebook.com", "reddit.com", "twitter.com",
    "pinterest.com", "tiktok.com", "linkedin.com",
    # Wikipedia articles ABOUT dishes use enough recipe vocabulary to
    # pass is_recipe (e.g. /wiki/Savory_spinach_pie scored 8), but
    # they're encyclopedia entries, not recipes. Filter at the domain
    # layer so they're dropped before is_recipe / Moz spend.
    "wikipedia.org",
]
_DEFAULT_DISALLOWED_URL_PATH_FRAGMENTS = [
    # URL-path substrings that signal a roundup/article/news page
    # rather than a recipe. Catches the americastestkitchen.com/
    # articles/24-the-best-beef-stew pattern that survives is_recipe
    # (because narrative articles use the same vocabulary).
    "/articles/", "/article/", "/news/", "/blog-post",
    "/best-of", "/roundup", "/listicle", "/buying-guide",
]

DISALLOWED_DOMAINS = set(_cfg.get("disallowed_domains", _DEFAULT_DISALLOWED_DOMAINS))
DISALLOWED_URL_PATH_FRAGMENTS = set(
    _cfg.get("disallowed_url_path_fragments", _DEFAULT_DISALLOWED_URL_PATH_FRAGMENTS)
)

# Recipe-phrase match threshold for is_recipe(). Used by the batch
# pipeline as a hard filter; used by the live extract path as an
# informational stamp on _scoring.recipeScore (not a gate — see
# memory/project_live_is_recipe_warn.md).
IS_RECIPE_THRESHOLD = int(_cfg.get("is_recipe_threshold", 7))

# Page-quality floor for the batch front-end. Negative OU means Moz
# judges the page as under-performing its domain baseline — typically
# a roundup or article rather than a hero recipe page. The ATK
# articles/24-the-best-beef-stew page had OU = -6.64 and slipped
# through every other filter; this catches that pattern cleanly.
MIN_OU_SCORE = float(_cfg.get("min_ou_score", 0.0))

# Domain-authority floor for the batch front-end. The enrichment
# editor commentary on low-DA pages frequently flags them as low
# quality — this filter drops them before the LLM extract spend even
# tries (and before they pollute master_recipes stats). Default 30
# is a "decent food blog or above" threshold. The user's 2026-05-24
# call.
MIN_DA_SCORE = float(_cfg.get("min_da_score", 30.0))

# Per-query SerpAPI funnel size (count of organic results to fetch per
# query in the multi-query case). Top-final is the post-rank cull size.
# serpapi_max_pages caps the page count so total quota stays bounded
# even if a future caller asks for a huge target_n.
DEFAULT_TOP_SERPAPI_PER_QUERY = int(_cfg.get("default_top_serpapi_per_query", 25))
DEFAULT_TOP_FINAL = int(_cfg.get("default_top_final", 10))
SERPAPI_MAX_PAGES = int(_cfg.get("serpapi_max_pages", 10))

# Save-gate thresholds. Apply to BOTH the live form's POST /recipes
# and the batch's save loop — keeps the recipes/master_recipes tables
# statistically clean. The form's 422 with `thin_recipe: true` offers a
# "Save anyway" override; batch saves skip with a SAVE-SKIP log line.
SAVE_GATE_MIN_INGREDIENTS = int(_cfg.get("save_gate_min_ingredients", 3))
SAVE_GATE_MIN_INSTRUCTIONS = int(_cfg.get("save_gate_min_instructions", 3))

# Canonical public domain for BCC permalinks. Self-URL minting uses
# this when a recipe has no external source; the /r/<id> route resolves
# either this or the legacy recipes.tbotb.com host to the form.
BCC_PUBLIC_DOMAIN = str(_cfg.get("bcc_public_domain", "bestcooksclub.com"))

# DALL-E 3 dish-image generation.
#   IMAGE_GEN_QUALITY: "standard" ($0.04/image, default for live form) or
#     "hd" ($0.08, for cookbook-print-ready output). Cookbook export jobs
#     should override per-call instead of flipping the global default.
#   IMAGE_GEN_SIZE: "1024x1024" is the cheapest DALL-E 3 size and matches
#     the form's square thumbnail. "1792x1024" / "1024x1792" cost the
#     same as hd-square.
IMAGE_GEN_QUALITY = str(_cfg.get("image_gen_quality", "standard"))
IMAGE_GEN_SIZE = str(_cfg.get("image_gen_size", "1024x1024"))