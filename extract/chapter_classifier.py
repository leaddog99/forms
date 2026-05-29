"""Chapter classifier — cookbook-style flat classification.

Tier-1 keyword shortcut layer in front of a Claude enum LLM call.
Most recipes (specific dish nouns like "Risotto", "Pho", "Carbonara")
hit the shortcut layer and never make an API call. Ambiguous titles
fall through to claude-haiku-4-5 with tool_use + input_schema enum
that guarantees the output is one of the 24 canonical chapters —
byte for byte.

CHAPTERS is the schema, kept in code so the enum constraint and the
form UI can't drift from each other. Shortcuts live in
`chapter_shortcuts.json` because they're tunable DATA (add a dish,
edit the list, no code change).

Trap phrases ("ice cream sandwich", "taco salad") use a __DEFER__
sentinel in the shortcuts file. Their presence forces the LLM path
regardless of any other shortcut match in the same title — the
ambiguity is itself the signal.

Per-call cost when the shortcut layer misses: ~150 input + ~10 output
tokens at claude-haiku-4-5. Shortcut hits cost zero. Most titles
hit a shortcut.
"""
import json
import re
import unicodedata
from pathlib import Path
from typing import Optional

import anthropic


_anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env


# 24 canonical chapters. Order roughly follows menu progression
# (openers → mains → sides → desserts → drinks → utilities).
# "Uncertain" is the explicit escape hatch — better to flag than guess.
CHAPTERS = [
    "Appetizers & Starters",
    "Soups & Stews",
    "Salads",
    "Eggs & Breakfast",
    "Sandwiches",
    "Pasta & Noodles",
    "Rice & Grains",
    "Beans, Legumes & Tofu",
    "Vegetables",
    "Fish & Shellfish",
    "Poultry",
    "Meat",
    "Sauces, Dressings & Condiments",
    "Breads",
    "Cakes",
    "Cookies & Bars",
    "Pies and Pastries - Sweet",
    "Pies and Pastries - Savory",
    "Custards, Puddings & Mousses",
    "Frozen Desserts",
    "Fruit Desserts",
    "Candies & Confections",
    "Beverages & Cocktails",
    "Preserving & Pickling",
    "Uncertain",
]

_DEFER = "__DEFER__"
_SHORTCUTS_PATH = Path(__file__).parent / "chapter_shortcuts.json"


def _normalize(s: str) -> str:
    """Normalize for word-bounded phrase matching: NFD-decompose, strip
    combining marks (so "crème" → "creme"), lowercase, collapse all
    non-alphanumerics to single spaces, pad with surrounding spaces so
    phrase containment matches at word boundaries.

    Without the padding, the substring "salad" would match inside
    "salads" and inside "salade niçoise" — the conversation's analysis
    that broad chapter words are unsafe to shortcut hinged on this.
    Padding makes " salad " require an actual word break on both sides.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return f" {s} " if s else ""


def _load_shortcuts():
    """Load and validate the shortcuts file. Every value must either be
    one of CHAPTERS or the __DEFER__ sentinel. Fails loudly on a typo —
    silent misrouting is the failure mode we're avoiding.

    Returns (shortcuts_dict, traps_set) where:
      shortcuts_dict: { padded_normalized_phrase: chapter }
      traps_set: set of padded_normalized_phrases that force the LLM
    """
    if not _SHORTCUTS_PATH.exists():
        print(f"[WARN] chapter_shortcuts.json not found at {_SHORTCUTS_PATH}")
        return {}, set()
    with _SHORTCUTS_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    valid = set(CHAPTERS)
    shortcuts = {}
    traps = set()
    for key, value in raw.items():
        if key.startswith("_"):
            continue  # skip metadata keys like _comment
        normalized = _normalize(key)
        if not normalized.strip():
            # Key contains only non-Latin characters that the normalizer
            # strips (Greek, Cyrillic, CJK, Arabic, etc.). Empty
            # normalized form would substring-match EVERY title and
            # poison the lookup. Skip with a warning so the user knows
            # the key is dead code rather than silently working.
            print(f"[WARN] chapter_shortcuts.json: key {key!r} normalizes to empty (non-Latin chars); skipping. Use a transliterated key instead.")
            continue
        if value == _DEFER:
            traps.add(normalized)
        elif value in valid:
            shortcuts[normalized] = value
        else:
            raise ValueError(
                f"chapter_shortcuts.json: key {key!r} points to chapter "
                f"{value!r} which is not in CHAPTERS. Add it to CHAPTERS "
                f"or fix the mapping. Valid chapters: {sorted(valid)}"
            )
    return shortcuts, traps


_SHORTCUTS, _TRAPS = _load_shortcuts()


def _shortcut_lookup(title: str) -> Optional[str]:
    """Return a chapter if exactly one Tier-1 shortcut applies and no
    trap phrase is present. Returns None otherwise (caller falls to LLM).

    The two ways this returns None:
      1. A trap phrase ("ice cream sandwich", "taco salad") is present.
         Forces the LLM regardless of what else matches.
      2. Either zero shortcuts match, OR multiple shortcuts match that
         disagree on chapter. Multi-distinct-match is itself a signal
         of ambiguity — exactly the case the LLM is for.
    """
    norm = _normalize(title)
    if not norm:
        return None

    # Trap check first. "ice cream sandwich" is a trap even though
    # "ice cream" alone is a Tier-1 shortcut.
    for trap in _TRAPS:
        if trap in norm:
            return None

    matches = set()
    for phrase, chapter in _SHORTCUTS.items():
        if phrase in norm:
            matches.add(chapter)
    if len(matches) == 1:
        return next(iter(matches))
    return None


# System prompt for the LLM fallback. Encodes the governing principle
# plus the directional tie-break rules cookbook editors actually use —
# the same rules that resolve the systematic collisions (salads vs
# protein, soups vs braise, eggs-as-breakfast vs eggs-in-dessert, etc.).
# These came out of the design conversation and replace what a labeled
# eval set would otherwise teach the model the hard way.
_SYSTEM_PROMPT = (
    "You are a cookbook editor classifying a recipe into one of "
    f"{len(CHAPTERS)} canonical chapters. Pick EXACTLY one.\n\n"
    "GOVERNING PRINCIPLE: classify by the dish's identity and where a "
    "cook would look for it in a cookbook, NOT by its most prominent "
    "ingredient. Most misclassifications happen because the model "
    "reaches for the loudest ingredient instead of the dish's actual "
    "role on the table.\n\n"
    "TIE-BREAK RULES (apply in order; first that fits wins):\n\n"
    "1. A dish built on greens or composed cold/room-temperature "
    "elements is \"Salads\" even when it contains chicken, beef, or "
    "seafood. The protein chapters (Poultry / Meat / Fish & Shellfish) "
    "are only for dishes that are fundamentally a cooked piece of "
    "protein as the centerpiece.\n\n"
    "2. If it's served in its own liquid and eaten with a spoon, it's "
    "\"Soups & Stews\" regardless of dominant ingredient. A braise "
    "served ON something (over polenta, rice, mashed potatoes) goes "
    "to the protein chapter; a braise served as a bowl of broth-and-"
    "contents is Soups & Stews.\n\n"
    "3. A brothy bean dish eaten with a spoon (lentil soup, "
    "minestrone) is \"Soups & Stews\". A bean dish that's the "
    "substantial plated thing (refried beans, a chickpea braise "
    "served as a main) is \"Beans, Legumes & Tofu\".\n\n"
    "4. Cooked vegetable dishes served warm are \"Vegetables\". Raw "
    "or composed cold vegetable dishes with a dressing are \"Salads\".\n\n"
    "5. Noodles in a bowl of broth (pho, ramen, chicken noodle) are "
    "\"Soups & Stews\". Noodles as the dish with sauce clinging to "
    "them are \"Pasta & Noodles\".\n\n"
    "6. A grain or pasta dish where the starch is the foundation and "
    "protein is a component (risotto with shrimp, fried rice with "
    "pork, spaghetti Bolognese) goes to \"Pasta & Noodles\" or \"Rice "
    "& Grains\". The protein chapter is ONLY when the protein is the "
    "plated centerpiece.\n\n"
    "7. An egg dish in the breakfast/brunch canon (omelet, frittata, "
    "shakshuka, quiche, eggs benedict) is \"Eggs & Breakfast\" even "
    "when technically a savory tart or vegetable braise. BUT a "
    "dessert containing eggs (custard, sponge cake) is the relevant "
    "dessert chapter — course/sweetness overrides ingredient.\n\n"
    "8. The bread recipe itself (focaccia, pizza dough, baguette) is "
    "\"Breads\". A bread-based handheld assembly with fillings — "
    "sandwich, grilled cheese, panini, sub, hoagie, gyro wrap, "
    "shawarma wrap, sloppy joe, cheesesteak — is \"Sandwiches\". "
    "Finished pizza, calzone, stromboli, empanada, pasty, meat pie, "
    "chicken pot pie, and other enclosed/topped savory doughs are "
    "\"Pies and Tarts - Savory\" (pizza is structurally a savory pie "
    "with a flat crust + topping).\n\n"
    "9. \"Appetizers & Starters\" is reserved for things that exist "
    "ONLY as starters and don't fit a dish-type chapter (cheese boards, "
    "dips, mixed canapés, bar snacks). A small plate of meatballs is "
    "\"Meat\"; a cup of soup is \"Soups & Stews\".\n\n"
    "10. Within sweet desserts: pies and tarts (apple pie, pecan pie, "
    "tarte tatin, lemon tart), choux pastries (éclair, profiteroles, "
    "cream puffs, Paris-Brest), laminated puff-pastry desserts "
    "(mille-feuille, palmiers), and phyllo desserts (baklava, "
    "galaktoboureko, strudel) all go to \"Pies and Pastries - Sweet\" "
    "— crust/dough form wins over filling. A baked custard with NO "
    "crust (crème brûlée, flan, pot de crème) is \"Custards, Puddings "
    "& Mousses\"; a fruit dish that's mostly fruit with topping "
    "(crisp, cobbler) is \"Fruit Desserts\"; anything churned or "
    "frozen is \"Frozen Desserts\"; sugar-based confections (fudge, "
    "caramels, brittle) are \"Candies & Confections\".\n\n"
    "10b. Savory pies and pastries (chicken pot pie, beef pot pie, "
    "meat pie, steak and kidney pie, Cornish pasty, tourtière, "
    "empanada, calzone, stromboli, pizza, vol-au-vent, savory hand "
    "pie) all go to \"Pies and Pastries - Savory\". Pizza is "
    "structurally a savory open-faced pie. Quiche is genuinely "
    "contested between Eggs & Breakfast and Pies and Pastries - "
    "Savory — when the title says quiche, lean Eggs & Breakfast "
    "unless the recipe emphasizes the crust as the dish's "
    "identity.\n\n"
    "11. Only the STANDALONE recipe for a sauce / dressing / "
    "condiment goes in \"Sauces, Dressings & Condiments\". A dish "
    "that prominently features a sauce still classifies by the dish. "
    "\"Celery Salad with Green Apple Vinaigrette\" is \"Salads\". "
    "\"Chimichurri Steak\" is \"Meat\". \"Asparagus with Hollandaise\" "
    "is \"Vegetables\".\n\n"
    "12. ONE-POT RICE DISHES with mixed protein (jambalaya, paella, "
    "biryani, gumbo-over-rice) classify as \"Rice & Grains\" regardless "
    "of which protein dominates the ingredient list. The dish identity "
    "is the rice preparation, not the protein. \"One Pan Jambalaya\" "
    "and \"Jambalaya\" both go to \"Rice & Grains\".\n\n"
    "13. QUICK BREADS AND MUFFINS go to \"Breads\", not \"Cakes\", "
    "regardless of sugar content. Banana bread, blueberry muffins, "
    "biscuits (US sense), corn muffins — all \"Breads\". "
    "\"Cakes\" is reserved for proper layer / sponge / pound / Bundt "
    "cake structures.\n\n"
    "13b. SCONES are an exception to rule 13. Sweet scones (blueberry, "
    "cream, currant, lemon, chocolate chip, glazed, etc.) go to "
    "\"Pies and Pastries - Sweet\". Savory scones (cheese, herb, "
    "chive, cheddar, bacon, etc.) go to \"Pies and Pastries - "
    "Savory\". A bare \"Scones\" title without a flavor qualifier "
    "leans \"Pies and Pastries - Sweet\" (the dominant tradition).\n\n"
    "If genuinely ambiguous after these rules, return \"Uncertain\". "
    "Don't hedge with a guess."
)

# Hash the prompt for cache-keying if we ever cache chapter results.
# Not used today — chapter calls are cheap enough that caching adds
# more complexity than it saves — but the hash gives us a stable
# version handle for the eval harness comparisons.
import hashlib
CHAPTER_PROMPT_VERSION = hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


# Anthropic tool with an enum input_schema. tool_choice forces the model
# to call submit_chapter, and the SDK validates the input against the
# enum — same byte-for-byte guarantee OpenAI's json_schema enum gave us.
CHAPTER_TOOL = {
    "name": "submit_chapter",
    "description": "Submit the cookbook chapter classification for the recipe.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["chapter"],
        "properties": {
            "chapter": {
                "type": "string",
                "enum": CHAPTERS,
            },
        },
    },
}


def classify_chapter(
    name: str,
    ingredients: Optional[list] = None,
    *,
    model: str = "claude-haiku-4-5",
    usage_log: Optional[list] = None,
) -> str:
    """Classify a recipe into one of CHAPTERS.

    Shortcut layer first (zero-cost; most dishes hit it). LLM fallback
    with tool_use + enum input_schema guarantees the output is
    byte-for-byte one of CHAPTERS — no parsing, no normalization, no
    drift.

    `usage_log`, if provided, gets one entry appended:
      - For shortcut hits: zero-token entry tagged "chapter_shortcut"
        (lets per-recipe usage queries show that we saved a call)
      - For LLM hits: standard build_usage_entry output, operation
        "chapter_classify"

    Never raises. Returns "Uncertain" on any error so the caller can
    journal it and move on.
    """
    if not name or not name.strip():
        return "Uncertain"

    # Tier-1 shortcut layer (title-only — ingredients don't influence
    # the shortcut decision; they're an LLM-fallback signal only).
    short = _shortcut_lookup(name)
    if short is not None:
        if usage_log is not None:
            usage_log.append({
                "operation": "chapter_shortcut",
                "model": "(keyword)",
                "input_tokens": 0,
                "output_tokens": 0,
                "meta": {
                    "matched_chapter": short,
                    "title": name,
                },
            })
        return short

    ingredients_sample = (ingredients or [])[:12]
    user_lines = [f"Recipe: {name.strip()}"]
    if ingredients_sample:
        user_lines.append("Key ingredients:")
        for ing in ingredients_sample:
            user_lines.append(f"- {ing}")
    user_prompt = "\n".join(user_lines)

    try:
        response = _anthropic_client.messages.create(
            model=model,
            max_tokens=200,
            temperature=0,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[CHAPTER_TOOL],
            tool_choice={"type": "tool", "name": "submit_chapter"},
        )
    except Exception as e:
        print(f"[WARN] chapter_classifier LLM call failed: {e}")
        return "Uncertain"

    if usage_log is not None:
        try:
            from input.pipeline.token_journal import build_usage_entry
            usage_log.append(build_usage_entry("chapter_classify", model, response))
        except Exception:
            pass  # journal failures never propagate

    tool_input = next(
        (b.input for b in response.content if b.type == "tool_use" and b.name == "submit_chapter"),
        None,
    )
    if not isinstance(tool_input, dict):
        return "Uncertain"
    chapter = tool_input.get("chapter", "Uncertain")
    if chapter not in CHAPTERS:
        # Belt-and-suspenders — the enum input_schema should prevent this.
        return "Uncertain"
    return chapter


if __name__ == "__main__":
    # Smoke tests against the boundary cases from the design conversation.
    cases = [
        ("Risotto with Shrimp", None, "Rice & Grains"),           # specific dish noun wins regardless of protein
        ("Pho Ga", None, "Soups & Stews"),                        # broth-and-spoon
        ("Carbonara", None, "Pasta & Noodles"),                   # pasta name wins
        ("Crème Brûlée", None, "Custards, Puddings & Mousses"),   # accent stripping
        ("Crab Cakes", None, "Fish & Shellfish"),                 # resolves "cake" substring trap correctly
        ("Vinaigrette", None, "Sauces, Dressings & Condiments"),  # sauce standalone
    ]
    defer_cases = [
        "Salade Niçoise",             # broad "salad" word shouldn't shortcut
        "Ice Cream Sandwich",         # trap — defers
        "Taco Salad",                 # trap — defers
        "Pesto Chicken",              # sauce-in-dish-title not in shortcuts
        "Swordfish and Chips",        # substring hazard — must not match "fish and chips"
    ]
    print("Tier-1 hits expected:")
    for title, ings, expected in cases:
        got = _shortcut_lookup(title)
        status = "OK " if got == expected else "FAIL"
        print(f"  {status} {title!r:40} -> {got!r:35} (want {expected!r})")
    print()
    print("Defer-to-LLM expected (no shortcut match):")
    for title in defer_cases:
        got = _shortcut_lookup(title)
        status = "OK " if got is None else "FAIL"
        print(f"  {status} {title!r:40} -> {got!r}")
