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
        with sqlite3.connect(path) as conn:
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


def url_lacks_recipe_signal(url: str, db_path: Optional[str] = None) -> bool:
    """True (→ drop, skip the fetch) when the URL path mentions no food/recipe word —
    probably a /restaurant//chef//jobs/ page, not a recipe. Any food-list token keeps
    it; the fetch-verify still runs on survivors. The 'stop' list isn't consulted here
    (a non-food token is simply no signal) — it only spares the SWEEP from re-asking."""
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
    "You classify word tokens pulled from recipe-site URL paths. A token is 'food' if it "
    "names a food, ingredient, a specific dish in ANY language or transliteration (e.g. "
    "spanakorizo, coquilles, kleftiko, kartoffelpuffer), a drink, or a cooking method/"
    "technique (baked, braised, cured). Otherwise it is 'not_food': person names, places, "
    "dates, numbers, site words like html/www/blog, bare cuisine/nationality adjectives "
    "like 'italian'/'cajun'/'haitian', and generic filler like 'easy'/'best'/'delicious'. "
    "Put EVERY input token in exactly one of the two lists."
)


def classify_unknowns(tokens, batch: int = 400) -> dict:
    """ONE Haiku call (batched if huge) splitting `tokens` into {'food': [...],
    'stop': [...]}. Routes through the LLM gateway (auto-journaled). Tokens the model
    fails to place are left out (re-tried next sweep). Returns the two lists."""
    import llm
    toks = sorted({str(t).strip().lower() for t in tokens
                   if len(str(t).strip()) >= _MIN_TOKEN_LEN})
    food, stop = [], []
    for i in range(0, len(toks), batch):
        chunk = toks[i:i + batch]
        try:
            resp = llm.create(
                operation="url_word_classify", model=_MODEL,
                max_tokens=8000, temperature=0, system=_CLASSIFY_SYS,
                messages=[{"role": "user",
                           "content": "Classify these tokens:\n" + ", ".join(chunk)}],
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


def sweep_master_urls(db_path: Optional[str] = None, log=print) -> dict:
    """Collect every URL-path token in the master corpus that's in NEITHER list, send the
    unknown batch through ONE classify call, and INSERT the results (incremental). Returns
    {scanned, unknown, added_food, added_stop}. The only model-touching entry point."""
    path = _resolve_db_path(db_path)
    self_hosts = ("bestcooksclub.com", "tbotb.com", "recipes.tbotb.com",
                  "amazon.com", "share.google")
    with sqlite3.connect(path) as conn:
        seed_if_empty(conn)
        known = set()
        for (w,) in conn.execute("SELECT word FROM url_word_class"):
            known.add(w)
        scanned, unknown = 0, set()
        for (u,) in conn.execute(
                "SELECT url_normalized FROM master_recipes WHERE url_normalized LIKE 'http%'"):
            host = (urlparse(u).hostname or "").lower()
            if any(s in host for s in self_hosts):
                continue
            scanned += 1
            for t in path_tokens(u):
                if t not in known:
                    unknown.add(t)
        log(f"[url-words] scanned {scanned} master urls; {len(unknown)} unknown tokens")
        added_food = added_stop = 0
        if unknown:
            split = classify_unknowns(unknown)
            added_food = add_words(conn, split["food"], "food", source="ai")
            added_stop = add_words(conn, split["stop"], "stop", source="ai")
            conn.commit()
            log(f"[url-words] classified -> +{added_food} food, +{added_stop} stop "
                f"(food e.g. {', '.join(split['food'][:12])})")
    invalidate(path)
    return {"scanned": scanned, "unknown": len(unknown),
            "added_food": added_food, "added_stop": added_stop}
