"""cook_tips_kb.py — STUB tips/checks knowledge base for the cook rework.

The real moat lives here eventually: a DB-resident, retrievable knowledge base of
cooking success-tips and failure-mode CHECKS, sourced from our research on why
recipes succeed and fail. For now this is a hand-authored seed (~15-20 entries)
injected into the rework prompt so the model can attach a relevant `tip` / `check`
to the steps it writes. Keyed by a coarse technique/situation tag the model picks
from. See recipe_anchor/phase3-pipeline-design.md ("stubbed tips KB").

Each entry:
  tip   — a short success cue (do this and it comes out better)
  check — a doneness / failure-avoidance signal (how to KNOW it's right / not ruined)
"""
from __future__ import annotations

# Coarse, technique/situation-keyed. The model is shown these and told to attach the
# relevant ones to the matching step (or none). Keep them short and concrete.
TIPS_KB: dict[str, dict] = {
    "saute_garlic": {
        "tip": "Cook garlic to blond, not brown.",
        "check": "Fragrant and pale gold — the moment it browns it turns bitter; pull it early.",
    },
    "saute_aromatics": {
        "tip": "Sweat onions on medium until translucent before adding garlic, which burns faster.",
        "check": "Soft and glassy, no color, ~5-8 min.",
    },
    "bloom_spices": {
        "tip": "Bloom dry spices / tomato paste in the fat for ~30-60 s before liquids — it deepens flavor.",
        "check": "Fragrant and a shade darker; don't let them scorch.",
    },
    "brown_meat": {
        "tip": "Dry the meat and don't crowd the pan — crowding steams instead of sears.",
        "check": "A deep brown crust (fond) on the bottom; work in batches if needed.",
    },
    "deglaze": {
        "tip": "Deglaze with the wine/stock while the fond is hot, scraping it up — that's free flavor.",
        "check": "Bottom of the pan comes clean; liquid picks up the brown.",
    },
    "salt_pasta_water": {
        "tip": "Salt the pasta water until it tastes like the sea — it's the pasta's only seasoning.",
        "check": "Distinctly salty to taste before the pasta goes in.",
    },
    "reserve_pasta_water": {
        "tip": "Reserve a cup of starchy pasta water before draining — it emulsifies and loosens the sauce.",
        "check": "Cloudy and starchy; set it aside, don't dump it.",
    },
    "pasta_al_dente": {
        "tip": "Pull pasta 1-2 min shy of the box time; it finishes in the sauce.",
        "check": "Tender with a firm core when bitten; it'll soften as it tosses in sauce.",
    },
    "rest_meat": {
        "tip": "Rest roasts/steaks 5-10 min (large roasts longer) before slicing.",
        "check": "Cutting early lets the juices run out onto the board instead of staying in the meat.",
    },
    "emulsify_sauce": {
        "tip": "Finish the sauce off heat or low, adding fat/starchy water gradually while tossing.",
        "check": "Glossy and clinging to the pasta, not pooled or broken/oily.",
    },
    "temper_eggs": {
        "tip": "Temper eggs by whisking in a little hot liquid first, then return — never add eggs to a boil.",
        "check": "Thick enough to coat the back of a spoon; if it scrambles, the heat was too high.",
    },
    "dont_overmix_batter": {
        "tip": "Mix just until combined; lumps are fine.",
        "check": "Overmixing develops gluten and bakes up tough/dense.",
    },
    "preheat_oven": {
        "tip": "Start the oven first — a fully preheated oven is assumed by every bake time.",
        "check": "Wait for the preheat signal; an under-temp oven throws off the whole bake.",
    },
    "season_in_layers": {
        "tip": "Season in layers as you go, and account for salty components added later (cheese, cured meat, soy, capers).",
        "check": "Taste at the end before the final salt — you can always add, not subtract.",
    },
    "drain_ricotta": {
        "tip": "Press/drain wet cheeses (ricotta) before filling — excess whey makes a watery bake.",
        "check": "Thick and scoopable, not soupy; the strainer has given up liquid.",
    },
    "bake_rest_before_cut": {
        "tip": "Let layered bakes (lasagna, gratin) rest 10-15 min before cutting so they set.",
        "check": "Holds a clean edge when sliced instead of sliding apart.",
    },
    "discard_unopened_shellfish": {
        "tip": "Steam clams/mussels just until they open; pull each as it does so it doesn't overcook.",
        "check": "SAFETY: discard any that stay shut after cooking.",
    },
    "toast_nuts_spices": {
        "tip": "Toast nuts/whole spices dry until fragrant to wake them up.",
        "check": "Fragrant and lightly colored; they go from toasted to burnt fast — watch them.",
    },
}


def format_kb_for_prompt() -> str:
    """Render the KB as compact reference text for the rework system prompt."""
    lines = []
    for key, e in TIPS_KB.items():
        bits = []
        if e.get("tip"):
            bits.append(f"tip: {e['tip']}")
        if e.get("check"):
            bits.append(f"check: {e['check']}")
        lines.append(f"- {key} — " + " | ".join(bits))
    return "\n".join(lines)
