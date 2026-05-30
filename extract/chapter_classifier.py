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


# 24 canonical chapters + an "Uncertain" escape hatch. Order roughly
# follows menu progression (openers → mains → sides → desserts → drinks
# → utilities). Mostly identity chapters ("classify by what the dish is"),
# with two deliberate FORMAT chapters that win over ingredient identity:
# "Sandwiches, Pizza & Savory Pastry" (handheld dough that encloses a
# filling) and "Casseroles & Baked Dishes" (composite mains baked and
# served from a dish — classify by the object on the plate, not the
# protein). "Uncertain" is better than a guess.
CHAPTERS = [
    "Appetizers & Starters",
    "Soups & Stews",
    "Salads",
    "Eggs & Breakfast",
    "Sandwiches, Pizza & Savory Pastry",
    "Pasta & Noodles",
    "Rice & Grains",
    "Beans, Legumes & Tofu",
    "Vegetables",
    "Fish & Shellfish",
    "Poultry",
    "Meat",
    "Casseroles & Baked Dishes",
    "Sauces, Dressings & Condiments",
    "Breads",
    "Cakes",
    "Cookies & Bars",
    "Pies & Pastries",
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

# Chapters whose shortcut phrases are "weak": a phrase like "tomato sauce"
# is a real classification ONLY when the title essentially IS that thing
# ("Tomato Sauce"), NOT when it's a modifier inside a larger dish title
# ("Shrimp Enchiladas in Tomato Sauce" — that's a casserole that happens to
# contain a sauce). For these chapters a shortcut fires only on a
# whole-title match; embedded matches defer to the LLM, which has the
# dish-vs-component tie-break rules (rule 11). Without this, a sauce noun
# buried in a dish title hijacks the classification before the LLM is even
# consulted — the exact bug that sent "Shrimp Enchiladas in Tomato Sauce"
# to Sauces.
_STANDALONE_ONLY_CHAPTERS = {"Sauces, Dressings & Condiments"}


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
        if phrase not in norm:
            continue
        if chapter in _STANDALONE_ONLY_CHAPTERS and norm.strip() != phrase.strip():
            # Weak phrase (e.g. a sauce noun) embedded in a larger dish
            # title — don't let it classify; let the LLM judge the dish.
            continue
        matches.add(chapter)
    if len(matches) == 1:
        return next(iter(matches))
    return None


# =====================================================================
# CLASSIFICATION RULES — the decision tree, in plain English.
#
# This is the human-readable spec. _SYSTEM_PROMPT below is the machine
# encoding of exactly these rules; keep the two in sync. The philosophy,
# in one sentence:
#
#   Classify by the structural object the cook recognizes ON THE PLATE,
#   with structure outranking ingredients, cooking method, vessel, and
#   name.
#
# The decision tree (apply in order; first rule that decides, wins):
#
#   R1. IDENTITY OVER INGREDIENTS — classify by what the dish IS, not
#       what it contains. Lasagne is lasagne, not "pasta". Pizza is
#       pizza, not bread.
#   R2. PHYSICAL FORM OVER TECHNIQUE — "baked/fried/roasted/braised" is
#       secondary. An empanada is an enclosed pastry however it's cooked.
#   R3. THE DEFINING STRUCTURE WINS — remove one thing; what destroys the
#       dish's identity? Remove a pot pie's CRUST → stew (crust defines
#       it → Savory Pastry). Remove lasagne's LAYERING → pasta with sauce
#       (assembly defines it → Casseroles). Remove chicken parm's CHICKEN
#       → sauce with cheese (cutlet defines it → Poultry). Resolves most
#       controversies.
#   R4. CRUST BEATS FILLING — if a pastry/dough crust is the defining
#       structure (pot pie, tourtière, meat pie, empanada, pasty,
#       calzone, Jamaican patty, pizza) it's Sandwiches, Pizza & Savory
#       Pastry, whatever the filling.
#   R5. DISCRETE CENTERPIECE BEATS ASSEMBLY — an individual item served
#       as itself (chicken/veal parmesan, meatloaf, crab cakes, stuffed
#       peppers) classifies by its centerpiece; a portion CUT from a
#       larger baked assembly (lasagne, moussaka, pastitsio, baked ziti)
#       is Casseroles & Baked Dishes.
#   R6. NAMES DON'T OVERRIDE STRUCTURE — shepherd's/cottage "pie" has no
#       pastry crust → baked assembly → Casseroles. The word "pie"
#       doesn't control; structure does.
#   R7. SANITY CHECK — would a cook expect these to live together? If a
#       placement breaks an obvious cluster, reconsider.
#
# Worked edge cases (the famously contested ones):
#   Lasagne / Baked Ziti / Moussaka / Pastitsio  -> Casseroles & Baked Dishes (R5)
#   Chicken Pot Pie / Steak & Kidney Pie / Tourtière / Empanada / Calzone
#                                                 -> Sandwiches, Pizza & Savory Pastry (R4)
#   Chicken Parmesan -> Poultry,  Veal Parmesan -> Meat,
#   Eggplant Parmesan -> Vegetables               (R3/R5)
#   Shepherd's Pie / Cottage Pie -> Casseroles & Baked Dishes (R6: no crust)
#
# Below the tree, SPECIFIC CASE RULES (numbered 1..13b in the prompt)
# cover collisions the tree doesn't settle directly: salads vs protein,
# soups/stews vs braise, brothy beans, noodles-in-broth, one-pot rice,
# eggs-as-breakfast, standalone sauces, appetizers, quick breads, scones,
# and the sweet-dessert subdivisions. These came out of the design
# conversation and replace what a labeled eval set would otherwise teach
# the model the hard way.
# =====================================================================
#
# System prompt for the LLM fallback — the machine encoding of the rules
# documented above.
_SYSTEM_PROMPT = (
    "You are a cookbook editor classifying a recipe into one of "
    f"{len(CHAPTERS)} canonical chapters. Pick EXACTLY one.\n\n"
    "CORE PRINCIPLE: classify by the structural object the cook "
    "recognizes ON THE PLATE — structure outranks ingredients, cooking "
    "method, vessel, and name. Ask \"what would a knowledgeable cook "
    "call this dish?\" Most misclassifications come from reaching for "
    "the loudest ingredient or the cooking technique instead of the "
    "dish's actual identity.\n\n"
    "DECISION TREE — apply in order; the first rule that decides, wins:\n\n"
    "R1. IDENTITY OVER INGREDIENTS. Classify by what the dish IS, not "
    "what it contains. Lasagne is lasagne (not \"pasta\"); chicken "
    "parmesan is a chicken dish (not a casserole); pizza is pizza (not "
    "bread).\n\n"
    "R2. PHYSICAL FORM OVER TECHNIQUE. \"Baked / fried / roasted / "
    "braised\" is usually secondary. An empanada and a calzone are "
    "enclosed pastries; lasagne is a layered baked block; chicken "
    "parmesan is a breaded cutlet — regardless of how they were "
    "cooked.\n\n"
    "R3. THE DEFINING STRUCTURE WINS. Ask: if I removed one thing, what "
    "would destroy the dish's identity? Remove a chicken pot pie's "
    "CRUST and you have stew → the crust defines it → Savory Pastry. "
    "Remove lasagne's LAYERING and you have pasta with sauce → the "
    "assembly defines it → Casseroles. Remove chicken parmesan's "
    "CHICKEN and you have sauce with cheese → the cutlet defines it → "
    "Poultry. This rule resolves most controversies.\n\n"
    "R4. CRUST BEATS FILLING. If a pastry/dough crust is the defining "
    "structure — pot pie, tourtière, meat pie, empanada, Cornish "
    "pasty, calzone, Jamaican patty, pizza — it is \"Sandwiches, Pizza "
    "& Savory Pastry\", whatever the filling.\n\n"
    "R5. DISCRETE CENTERPIECE BEATS ASSEMBLY. An individual item served "
    "as itself (chicken/veal parmesan, meatloaf, crab cakes, stuffed "
    "peppers) classifies by its centerpiece protein/vegetable. A "
    "portion CUT from a larger baked assembly (lasagne, moussaka, "
    "pastitsio, baked ziti) is \"Casseroles & Baked Dishes\".\n\n"
    "R6. NAMES DON'T OVERRIDE STRUCTURE. Some names lie: shepherd's pie "
    "and cottage pie have NO pastry crust → they are baked assemblies → "
    "\"Casseroles & Baked Dishes\". The word \"pie\" does not control "
    "the classification; structure does.\n\n"
    "R7. SANITY CHECK — would a cook expect these to live together? "
    "Savory crust pies cluster (pot pie, steak & kidney pie, tourtière, "
    "empanada, pasty); baked assemblies cluster (lasagne, moussaka, "
    "pastitsio, baked ziti); chicken dishes cluster (parmesan, marsala, "
    "piccata, cacciatore). If a placement breaks an obvious cluster, "
    "reconsider.\n\n"
    "SPECIFIC CASE RULES — for collisions the tree above doesn't settle "
    "directly (first that fits wins):\n\n"
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
    "8. BREADS vs SANDWICHES/PIZZA/PASTRY. The bread recipe itself "
    "(focaccia, pizza dough, baguette, dinner rolls, savory scones, "
    "cornbread, biscuits) is \"Breads\". A finished handheld assembly — "
    "sandwich, grilled cheese, panini, sub, hoagie, gyro/shawarma wrap, "
    "sloppy joe, cheesesteak, quesadilla, burger — is \"Sandwiches, "
    "Pizza & Savory Pastry\", which per R4 also holds pizza and all "
    "crust-defined savory pies/pastries.\n\n"
    "8b. \"Casseroles & Baked Dishes\" (per R5/R6) holds crustless "
    "baked assemblies: enchiladas, lasagna, baked ziti, manicotti, "
    "stuffed shells, cannelloni, baked mac & cheese, pastitsio, "
    "moussaka, chicken/tuna-noodle casserole, tetrazzini, chicken "
    "divan, king ranch chicken, shepherd's/cottage pie, tamale pie. "
    "EXCEPTIONS that keep their identity chapter even when baked in a "
    "dish: a breaded-cutlet centerpiece (chicken/veal/eggplant "
    "parmesan) stays protein/vegetable; vegetable sides (green bean "
    "casserole, gratins) stay \"Vegetables\"; breakfast bakes (strata, "
    "egg/breakfast casserole) stay \"Eggs & Breakfast\"; a brothy thing "
    "eaten with a spoon is still \"Soups & Stews\".\n\n"
    "9. \"Appetizers & Starters\" is reserved for things that exist "
    "ONLY as starters and don't fit a dish-type chapter (cheese boards, "
    "dips, mixed canapés, bar snacks). A small plate of meatballs is "
    "\"Meat\"; a cup of soup is \"Soups & Stews\".\n\n"
    "10. Within sweet desserts: pies and tarts (apple pie, pecan pie, "
    "tarte tatin, lemon tart), choux pastries (éclair, profiteroles, "
    "cream puffs, Paris-Brest), laminated puff-pastry desserts "
    "(mille-feuille, palmiers), and phyllo desserts (baklava, "
    "galaktoboureko, strudel) all go to \"Pies & Pastries\" "
    "— crust/dough form wins over filling. (\"Pies & Pastries\" is "
    "SWEET ONLY. Judge by SWEETNESS, not by the word \"pie\" or the "
    "presence of a crust: a SAVORY pie — spinach pie / spanakopita, "
    "cheese pie / tiropita, börek, savory galette — must NEVER land "
    "here; route it by rules 8/8b instead.) A baked custard with NO "
    "crust (crème brûlée, flan, pot de crème) is \"Custards, Puddings "
    "& Mousses\"; a fruit dish that's mostly fruit with topping "
    "(crisp, cobbler) is \"Fruit Desserts\"; anything churned or "
    "frozen is \"Frozen Desserts\"; sugar-based confections (fudge, "
    "caramels, brittle) are \"Candies & Confections\".\n\n"
    "10b. QUICHE leans \"Eggs & Breakfast\" (breakfast canon) unless "
    "the recipe emphasizes the crust as the dish's identity, in which "
    "case it is \"Sandwiches, Pizza & Savory Pastry\".\n\n"
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
    "13b. SCONES: sweet scones (blueberry, cream, currant, lemon, "
    "chocolate chip, glazed, etc.) go to \"Pies & Pastries\". Savory "
    "scones (cheese, herb, chive, cheddar, bacon, etc.) are a savory "
    "quick bread and go to \"Breads\". A bare \"Scones\" title without "
    "a flavor qualifier leans \"Pies & Pastries\" (the dominant "
    "tradition).\n\n"
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
