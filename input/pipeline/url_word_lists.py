"""Self-learning two-list vocabulary for the URL-text recipe pre-filter.

A word token pulled from a recipe-site URL path is either a FOOD signal (a food,
ingredient, dish in ANY language, drink, or cooking method) or it is not (a person
name, place, date, site word like html/www, a bare cuisine adjective, filler). Those
two lists live in the `url_word_class` table. An occasional SWEEP of the master
corpus' URLs collects any token in NEITHER list and sends the whole unknown batch
through ONE Haiku call that splits it; the results are INSERTed (incremental — never a
wholesale rewrite, [[not a replace]]).

Retrieval (`url_lacks_recipe_signal`, used by `_is_recipe_filter`'s opt-in pre-fetch
skip) loads both lists ONCE into cached frozensets and does pure O(1) set lookups — the
model is NEVER called on the harvest hot path. The Python constants below are the
BOOTSTRAP SEED only ([[no data in code]] — the table is canonical once seeded).
"""
import os
import re
import sqlite3
from input.pipeline.db import connect as _connect  # WAL busy_timeout — input/pipeline/db.py
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse, unquote

# ── SEED (bootstrap only; the table is canonical once populated) ─────────────
# Recipe SIGNAL words: 'recipe' itself, cooking METHODS, and meal/course words — a URL
# carrying one is a recipe even without a specific food noun. All seed as kind='food'.
_SEED_RECIPE_WORDS = {
    "recipe", "recipes", "homemade",
    "baked", "grilled", "roasted", "fried", "braised", "stewed", "smoked",
    "poached", "seared", "sauteed", "broiled", "steamed", "simmered", "roast",
    "marinated", "glazed", "stuffed", "candied", "pickled", "caramelized",
    "grill", "bake", "braise", "saute", "fry",
    "breakfast", "brunch", "lunch", "dinner", "supper", "dessert", "desserts",
    "appetizer", "snack", "snacks", "entree",
}
# Non-signal words that recur inside dish names / URLs (cuisines, adjectives,
# connectives, calendar) — seed as kind='stop' so the sweep won't re-classify them.
_SEED_STOP_WORDS = {
    "and", "the", "with", "for", "from", "style", "easy", "best",
    "classic", "simple", "quick", "perfect", "ultimate", "creamy", "crispy",
    "fresh", "spicy", "sweet", "savory",
    "italian", "french", "greek", "mexican", "american", "asian", "chinese",
    "indian", "thai", "spanish", "german", "japanese", "korean", "southern",
    "mediterranean", "moroccan", "turkish", "vietnamese", "english", "irish",
    "old", "fashioned", "made", "your", "own", "one", "pot", "pan", "sheet",
    "day", "days", "new", "year", "years", "time", "week", "night", "game",
    "html", "www", "com", "https", "http", "blog", "blogs", "web", "amp", "asp",
}
# Common single foods/ingredients the dish catalog may not surface as a token.
_SEED_BASE_FOOD = {
    "chicken", "beef", "pork", "lamb", "turkey", "duck", "bacon", "ham", "sausage",
    "fish", "salmon", "tuna", "shrimp", "crab", "lobster", "scallop", "clam", "oyster",
    "egg", "eggs", "cheese", "butter", "cream", "milk", "yogurt", "buttermilk",
    "chocolate", "vanilla", "caramel", "cinnamon", "honey", "maple",
    "bread", "brioche", "toast", "bagel", "biscuit", "muffin", "scone", "roll",
    "cake", "pie", "tart", "cookie", "brownie", "pudding", "custard", "cobbler",
    "pasta", "spaghetti", "noodle", "risotto", "rice", "quinoa", "couscous", "gnocchi",
    "pizza", "burger", "sandwich", "taco", "burrito", "wrap", "soup", "stew", "chili",
    "salad", "slaw", "dip", "sauce", "salsa", "gravy", "dressing", "marinade",
    "potato", "tomato", "onion", "garlic", "mushroom", "pepper", "spinach", "kale",
    "broccoli", "carrot", "zucchini", "squash", "bean", "lentil", "chickpea", "corn",
    "apple", "banana", "lemon", "lime", "orange", "berry", "strawberry", "blueberry",
    "peach", "pumpkin", "coconut", "almond", "peanut", "walnut", "pecan",
    "steak", "ribs", "meatball", "casserole", "curry", "stir",
    "pancake", "waffle", "omelet", "frittata", "quiche", "pasty", "pastry", "dough",
    "tea", "coffee", "smoothie", "cocktail", "lemonade", "punch", "latte",
    "orzo", "wing", "wings", "scallops", "oat", "oats", "oatcake", "oatcakes",
    "pecorino", "parmesan", "parmigiano", "mozzarella", "feta", "ricotta", "gouda",
    "rosemary", "thyme", "basil", "oregano", "sage", "cilantro", "parsley", "dill", "mint",
    "cracker", "crackers", "ketchup", "mustard", "mayo", "pickle", "pickles", "jam", "jelly",
    "pesto", "hummus", "falafel", "gyro", "kebab", "ramen", "dumpling", "dumplings", "tofu",
}

_MODEL = "claude-haiku-4-5"
_MIN_TOKEN_LEN = 3

# process cache: db_path -> {"food": frozenset, "stop": frozenset}
_CACHE: dict = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db_path(db_path: Optional[str]) -> str:
    if db_path:
        return db_path
    import save_recipe_api as _api   # lazy — avoid import cycle at module load
    return _api.DB_PATH


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS url_word_class (
            word        TEXT PRIMARY KEY,
            kind        TEXT NOT NULL,                 -- 'food' | 'stop'
            source      TEXT NOT NULL DEFAULT 'ai',    -- 'seed' | 'ai' | 'manual'
            created_at  TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _catalog_food_tokens() -> set:
    """Dish-catalog names (chapter_shortcuts.json, ~1600) tokenized → food seed. DATA,
    not code. Empty set if the file is unreadable (the base seed still applies)."""
    import json
    out: set = set()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # <project>/input
    for cand in (os.path.join(here, "..", "extract", "chapter_shortcuts.json"),
                 os.path.join(here, "extract", "chapter_shortcuts.json")):
        if os.path.exists(cand):
            try:
                with open(cand, encoding="utf-8") as f:
                    cat = json.load(f)
                for name in cat:
                    if name.startswith("_"):
                        continue
                    for tok in re.split(r"[^a-z]+", name.lower()):
                        if len(tok) >= _MIN_TOKEN_LEN and tok not in _SEED_STOP_WORDS:
                            out.add(tok)
                break
            except Exception as e:
                print(f"  [url-words] dish catalog unreadable ({e})")
    return out


def add_words(conn: sqlite3.Connection, words, kind: str, source: str = "ai") -> int:
    """INSERT OR IGNORE words as `kind` ('food'|'stop'). Incremental — existing rows are
    left untouched (the first classification of a word wins). Returns rows added."""
    now = _now()
    clean = {str(w).strip().lower() for w in words}
    rows = [(w, kind, source, now) for w in clean if len(w) >= _MIN_TOKEN_LEN]
    if not rows:
        return 0
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO url_word_class(word, kind, source, created_at) VALUES (?,?,?,?)",
        rows)
    return conn.total_changes - before


def seed_if_empty(conn: sqlite3.Connection) -> None:
    """Populate the table from the code seed + dish catalog on first use only."""
    ensure_table(conn)
    if conn.execute("SELECT 1 FROM url_word_class LIMIT 1").fetchone():
        return
    food = set(_SEED_RECIPE_WORDS) | set(_SEED_BASE_FOOD) | _catalog_food_tokens()
    add_words(conn, food, "food", source="seed")
    add_words(conn, _SEED_STOP_WORDS, "stop", source="seed")
    conn.commit()


def get_word_sets(db_path: Optional[str] = None) -> dict:
    """(cached) {'food': frozenset, 'stop': frozenset} read from the table. Seeds on
    first call. Loaded ONCE per process — the hot path is pure set membership."""
    path = _resolve_db_path(db_path)
    if path in _CACHE:
        return _CACHE[path]
    food, stop = set(), set()
    try:
        with _connect(path) as conn:
            seed_if_empty(conn)
            for w, k in conn.execute("SELECT word, kind FROM url_word_class"):
                (food if k == "food" else stop).add(w)
    except Exception as e:
        print(f"  [url-words] load failed ({e}); falling back to code seed")
        food = set(_SEED_RECIPE_WORDS) | set(_SEED_BASE_FOOD) | _catalog_food_tokens()
        stop = set(_SEED_STOP_WORDS)
    sets = {"food": frozenset(food), "stop": frozenset(stop)}
    _CACHE[path] = sets
    return sets


def invalidate(db_path: Optional[str] = None) -> None:
    if db_path is None:
        _CACHE.clear()
    else:
        _CACHE.pop(_resolve_db_path(db_path), None)


# ── retrieval (hot path — pure set lookups, NO model call) ───────────────────
def path_tokens(url: str) -> set:
    """Lowercase alpha word tokens (≥3 chars) from a URL path. URL-DECODED first so
    %-escaped slugs tokenize (Latin only; non-Latin scripts yield no ascii tokens)."""
    return {t for t in re.split(r"[^a-z]+", unquote(urlparse(url).path).lower())
            if len(t) >= _MIN_TOKEN_LEN}


def parse_exclude_words(raw) -> set:
    """Admin's per-domain exclude list (domains.exclude_words) → a set of lowercased
    section words. Accepts a comma/space/newline-separated string or a list."""
    if not raw:
        return set()
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    return {w.strip().lower() for w in raw if w and str(w).strip()}


def url_excluded_by_domain(url: str, exclude_words) -> bool:
    """True (→ skip) when a PATH SECTION equals one of the domain's exclude words —
    the site's own taxonomy says this isn't a recipe (bostonchefs /restaurant/, /chef/,
    /news/). EXCLUSIONARY by design and PER-DOMAIN (admin opt-in), unlike the global
    food list. Splits the path on '/' and '_' only (NOT '-'), so a real recipe slug like
    /recipe/restaurant-style-chicken/ is NOT excluded by the word 'restaurant'."""
    words = exclude_words if isinstance(exclude_words, set) else parse_exclude_words(exclude_words)
    if not words:
        return False
    parts = [p for p in re.split(r"[/_]", unquote(urlparse(url).path).lower()) if p]
    return any(p in words for p in parts)


def url_lacks_recipe_signal(url: str, db_path: Optional[str] = None) -> bool:
    """True (→ drop, skip the fetch) when the URL path has NO food-list token. Food
    presence is the ONLY gate — a non-food token (restaurant/chef/news) is just no
    signal, NEVER exclusionary, so a real recipe can't be dropped over an incidental
    word. The 'stop' list isn't consulted here (it only spares the SWEEP from re-asking).
    The fetch-verify still runs on survivors, so a restaurant named after a real dish
    (/restaurant/sarma/) is kept-then-dropped by verify — that's the accepted ceiling;
    the cure for junk keeps is fixing mis-classified food tokens, not exclusion rules."""
    return not (path_tokens(url) & get_word_sets(db_path)["food"])


# ── sweep (occasional, out-of-process — the only place the model is called) ──
_CLASSIFY_TOOL = {
    "name": "split_tokens",
    "description": "Split URL-path word tokens into food vs non-food.",
    "input_schema": {
        "type": "object",
        "properties": {
            "food": {"type": "array", "items": {"type": "string"},
                     "description": "tokens that name a food, ingredient, specific dish "
                                    "(ANY language/transliteration, e.g. spanakorizo, "
                                    "coquilles, kleftiko), a drink, or a cooking method"},
            "not_food": {"type": "array", "items": {"type": "string"},
                         "description": "everything else: person names, places, dates, "
                                        "numbers, site words (html/www/com/blog), bare "
                                        "cuisine adjectives (italian/cajun), generic "
                                        "words (easy/best/delicious)"},
        },
        "required": ["food", "not_food"],
    },
}
_CLASSIFY_SYS = (
    "You classify word tokens pulled from recipe-site URL paths into 'food' vs 'not_food'. "
    "A token is 'food' ONLY if it names something you would COOK or EAT: a food, an "
    "ingredient, a specific dish in ANY language or transliteration (e.g. spanakorizo, "
    "coquilles, kleftiko, kartoffelpuffer), a drink, or a cooking method/technique (baked, "
    "braised, cured). "
    "It is 'not_food' if it is anything else, and IN PARTICULAR — be strict about these — a "
    "RESTAURANT or VENUE type (bistro, cantina, trattoria, brasserie, osteria, eatery, "
    "buffet, cafe, diner, tavern, pub, grill-as-a-place), a DINING / HOSPITALITY / BUSINESS "
    "word (dining, restaurant, menu, reservation, catering, hours, location, event, party, "
    "special, specials, takeout, delivery, brunch-service), a MEAL word used as a time not "
    "a dish (cena=dinner, lunch, supper), a GENERIC CATEGORY word (food, meal, cuisine, "
    "dish, ingredient, drink), a bare COLOR (red, white, green, black), a SEASON (summer, "
    "winter, spring, fall), a TEMPERATURE (hot, cold, warm), a person name, a place, a "
    "date, a number, a site word (html/www/blog), a bare cuisine/nationality adjective "
    "(italian/cajun/haitian), or generic filler (easy/best/delicious). Only a SPECIFIC "
    "edible item, ingredient, or named dish is 'food' — a category, color, season, venue, "
    "or dining concept is NEVER food, even when it sounds culinary. Put EVERY input token "
    "in exactly one of the two lists."
)


def classify_unknowns(tokens, batch: int = 400,
                      food_examples=None, stop_examples=None) -> dict:
    """ONE Haiku call (batched if huge) splitting `tokens` into {'food': [...],
    'stop': [...]}. Routes through the LLM gateway (auto-journaled). Tokens the model
    fails to place are left out (re-tried next sweep). `food_examples`/`stop_examples`
    are already-classified words shown to the model as few-shot guidance so it stays
    consistent with our conventions (esp. curator corrections). Returns the two lists."""
    import llm
    toks = sorted({str(t).strip().lower() for t in tokens
                   if len(str(t).strip()) >= _MIN_TOKEN_LEN})
    primer = ""
    if food_examples or stop_examples:
        primer = ("Stay consistent with these already-classified examples —\n"
                  f"FOOD: {', '.join(food_examples or [])}\n"
                  f"NOT_FOOD: {', '.join(stop_examples or [])}\n\n")
    food, stop = [], []
    for i in range(0, len(toks), batch):
        chunk = toks[i:i + batch]
        try:
            resp = llm.create(
                operation="url_word_classify", model=_MODEL,
                max_tokens=8000, temperature=0, system=_CLASSIFY_SYS,
                messages=[{"role": "user",
                           "content": primer + "Classify these tokens:\n" + ", ".join(chunk)}],
                tools=[_CLASSIFY_TOOL],
                tool_choice={"type": "tool", "name": "split_tokens"})
        except Exception as e:
            print(f"  [url-words] classify call failed: {type(e).__name__}: {e}")
            continue
        ti = next((b.input for b in resp.content
                   if getattr(b, "type", "") == "tool_use"), None)
        if isinstance(ti, dict):
            food += [w for w in ti.get("food", []) if isinstance(w, str)]
            stop += [w for w in ti.get("not_food", []) if isinstance(w, str)]
    # food wins ties (a word the model put in both lists is treated as food).
    foods = {w.strip().lower() for w in food}
    stops = {w.strip().lower() for w in stop} - foods
    return {"food": sorted(foods), "stop": sorted(stops)}


def _example_pool(conn: sqlite3.Connection, kind: str, n: int) -> list:
    """Up to `n` few-shot examples of `kind` ('food'|'stop') for the classifier prompt:
    the curator CORRECTIONS (source='manual') first — they're authoritative and the most
    instructive (the words we had to fix) — then fill with a spread of the rest."""
    manual = [w for (w,) in conn.execute(
        "SELECT word FROM url_word_class WHERE kind=? AND source='manual'", (kind,))]
    out = manual[:n]
    if len(out) < n:
        # deterministic spread across the rest (every k-th by rowid) — variety without RNG.
        rest = [w for (w,) in conn.execute(
            "SELECT word FROM url_word_class WHERE kind=? AND source!='manual' ORDER BY word",
            (kind,)) if w not in set(out)]
        step = max(1, len(rest) // max(1, (n - len(out))))
        out += rest[::step][:n - len(out)]
    return out


def _learn_from(conn: sqlite3.Connection, urls, log) -> dict:
    """Shared core: tokenize `urls`, keep tokens in NEITHER list (the 'stop' list is the
    negative cache that keeps this set to genuinely-NEW words — without it we'd re-bill
    the AI for the same junk every run), classify the unknown batch in ONE call, and
    INSERT both kinds (incremental — never a rewrite). Returns counts."""
    seed_if_empty(conn)
    known = {w for (w,) in conn.execute("SELECT word FROM url_word_class")}
    unknown = set()
    for u in urls:
        for t in path_tokens(u):
            if t not in known:
                unknown.add(t)
    added_food = added_stop = 0
    if unknown:
        # Few-shot guidance for the classifier: the curator CORRECTIONS (source='manual',
        # authoritative — e.g. food/tiki/summer demoted to stop) ALWAYS, plus a bounded
        # sample of each list so the model classifies consistently with our conventions.
        food_ex = _example_pool(conn, "food", 40)
        stop_ex = _example_pool(conn, "stop", 50)
        split = classify_unknowns(unknown, food_examples=food_ex, stop_examples=stop_ex)
        added_food = add_words(conn, split["food"], "food", source="ai")
        added_stop = add_words(conn, split["stop"], "stop", source="ai")
        conn.commit()
        log(f"[url-words] classified {len(unknown)} unknown -> +{added_food} food, "
            f"+{added_stop} stop (food e.g. {', '.join(split['food'][:12])})")
    return {"unknown": len(unknown), "added_food": added_food, "added_stop": added_stop}


def learn_from_urls(urls, db_path: Optional[str] = None, log=print) -> dict:
    """Per-harvest TEE-UP learn: classify THIS batch's unknown URL tokens (one AI call)
    and add them BEFORE the filter judges them — so a new publisher's own dish words are
    known for this very run, not just whatever the last master sweep learned. Cache is
    invalidated so the immediately-following filter reads the updated food list."""
    path = _resolve_db_path(db_path)
    with _connect(path) as conn:
        res = _learn_from(conn, list(urls), log)
    invalidate(path)
    return res


def sweep_master_urls(db_path: Optional[str] = None, log=print) -> dict:
    """Periodic backfill from CONFIRMED recipes: learn every still-unknown URL token in the
    master corpus (one classify call). Complements the per-harvest learn (higher-trust
    source). Returns {scanned, unknown, added_food, added_stop}."""
    path = _resolve_db_path(db_path)
    self_hosts = ("bestcooksclub.com", "tbotb.com", "recipes.tbotb.com",
                  "amazon.com", "share.google")
    with _connect(path) as conn:
        urls = [u for (u,) in conn.execute(
                    "SELECT url_normalized FROM master_recipes WHERE url_normalized LIKE 'http%'")
                if not any(s in (urlparse(u).hostname or "").lower() for s in self_hosts)]
        log(f"[url-words] sweeping {len(urls)} master urls")
        res = _learn_from(conn, urls, log)
    invalidate(path)
    return {"scanned": len(urls), **res}
