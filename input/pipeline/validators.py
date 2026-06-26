# Recipe-content validators. Pure functions over text; no I/O.

import json
import os
import unicodedata
from datetime import datetime, timezone

from input.pipeline.config import RECIPE_PHRASES, IS_RECIPE_THRESHOLD


def score_recipe_text(text: str) -> int:
    """Count occurrences of recipe phrases (case-insensitive)."""
    if not text:
        return 0
    lowered = text.lower()
    return sum(1 for phrase in RECIPE_PHRASES if phrase in lowered)


# --- Per-language phrase scoring (score RAW non-English text, no per-page translate) ---
# intake/recipe_phrases/<lang>.json = {"lang","threshold","phrases":[...]} — built ONCE by
# scripts/translate_recipe_phrases.py (the English RECIPE_PHRASES rendered as the natural
# recipe phrasing of that language, incl. abbreviations/conjugations). The filter scores a
# Greek page's raw text against the Greek list instead of translating the whole page first.
_PHRASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "intake", "recipe_phrases")
_LANG_PHRASES: dict = {}   # lang -> {"phrases": [...], "threshold": int} | None (cached)


def _load_lang_phrases(lang: str):
    lang = (lang or "").lower()[:2]
    if lang in _LANG_PHRASES:
        return _LANG_PHRASES[lang]
    data = None
    path = os.path.join(_PHRASE_DIR, f"{lang}.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            phrases = [str(p).lower() for p in raw.get("phrases", []) if str(p).strip()]
            if phrases:
                data = {"phrases": phrases,
                        "threshold": int(raw.get("threshold", IS_RECIPE_THRESHOLD)),
                        "sections": raw.get("sections") or {}}
        except Exception:
            data = None
    _LANG_PHRASES[lang] = data
    return data


def recipe_phrase_lang_available(lang: str) -> bool:
    """True when we have a translated phrase list for `lang` (→ score raw text, no translate)."""
    return _load_lang_phrases(lang) is not None


def lang_recipe_threshold(lang: str) -> int:
    """The keep-vs-drop phrase threshold for `lang` (its file's value, else the global)."""
    data = _load_lang_phrases(lang)
    return data["threshold"] if data else IS_RECIPE_THRESHOLD


def score_recipe_text_lang(text: str, lang: str):
    """Count this language's recipe phrases in RAW `text` (case-insensitive). Returns the
    int score, or None when no phrase list exists for `lang` (caller falls back to translate)."""
    data = _load_lang_phrases(lang)
    if data is None:
        return None
    if not text:
        return 0
    lowered = text.lower()
    return sum(1 for phrase in data["phrases"] if phrase in lowered)


def instance_base_language() -> str:
    """This instance's configured language (env BCC_TARGET_LANGUAGE, default 'en'). The
    English RECIPE_PHRASES is the base list for an English instance; a French instance's base
    is fr.json, etc. — so scoring is never hardcoded to English."""
    import os
    return (os.getenv("BCC_TARGET_LANGUAGE", "en").strip().lower()[:2] or "en")


# domains.language is curator/enrich-set and a bit messy ('gr' is the COUNTRY code, not the
# ISO 639-1 'el' for Greek; some rows hold a full name). Normalize to the 2-letter code our
# phrase-pack files are keyed on.
_LANG_ALIAS = {
    "gr": "el", "greek": "el", "ell": "el", "grc": "el", "ελληνικά": "el", "ελληνικα": "el",
    "en": "en", "eng": "en", "english": "en",
    "fr": "fr", "french": "fr", "français": "fr", "francais": "fr",
    "es": "es", "spanish": "es", "español": "es", "espanol": "es",
    "it": "it", "italian": "it", "italiano": "it",
    "de": "de", "german": "de", "deutsch": "de",
    "pt": "pt", "portuguese": "pt", "tr": "tr", "turkish": "tr", "nl": "nl", "dutch": "nl",
}


def normalize_lang(raw) -> str:
    """A messy language value (ISO code, country code, or name) → ISO 639-1 2-letter code.
    '' when empty. 'gr'→'el', 'Greek'→'el', etc."""
    if not raw:
        return ""
    k = str(raw).strip().lower()
    if k in _LANG_ALIAS:
        return _LANG_ALIAS[k]
    return _LANG_ALIAS.get(k[:2], k[:2])


def _phrases_and_threshold(lang: str):
    """(phrases, threshold) for `lang`: the English master RECIPE_PHRASES for 'en', else the
    translated <lang>.json. (None, None) when we have no list for it."""
    lang = (lang or "en").lower()[:2]
    if lang == "en":
        return RECIPE_PHRASES, IS_RECIPE_THRESHOLD
    data = _load_lang_phrases(lang)
    if data:
        return data["phrases"], data["threshold"]
    return None, None


def score_recipe_for_lang(text: str, lang: str):
    """Count `lang`'s recipe phrases in RAW `text`. 'en' uses the master RECIPE_PHRASES; any
    other language uses its <lang>.json. None when we have no list for `lang`."""
    phrases, _ = _phrases_and_threshold(lang)
    if phrases is None:
        return None
    if not text:
        return 0
    lowered = text.lower()
    return sum(1 for phrase in phrases if phrase in lowered)


# Section markers for the BASE/English instance (the non-English packs carry their own
# "sections" block in <lang>.json). A real recipe has BOTH an ingredients section AND a
# method section — a vocabulary-rich GUIDE/tips article has at most one. This structural
# AND is the free is-recipe gate (no LLM, no per-page translation).
_BASE_SECTIONS_EN = {
    "ingredients": ["ingredients"],
    "method": ["instructions", "directions", "method", "preparation", "steps"],
}


def _strip_accents(s: str) -> str:
    """Lowercase + drop combining accents. Greek section HEADERS are often UPPERCASE
    (ΥΛΙΚΑ/ΕΚΤΕΛΕΣΗ) and uppercase Greek has NO accents, so 'υλικά' wouldn't match a
    lowercased 'υλικα' without this — match accent-insensitively."""
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def recipe_sections(lang: str) -> dict:
    """{'ingredients':[...], 'method':[...]} section markers for `lang`. English from the
    base constant; other languages from their pack's 'sections' block. {} if unknown."""
    lang = (lang or "en").lower()[:2]
    if lang == "en":
        return _BASE_SECTIONS_EN
    data = _load_lang_phrases(lang)
    return (data.get("sections") if data else {}) or {}


def has_recipe_structure(text: str, base_lang: str, page_lang: str = None) -> bool:
    """True when `text` has BOTH an ingredients-section marker AND a method-section marker,
    in the base language OR the page language, matched accent-insensitively. This is the
    free structural is-recipe gate: real recipes have both sections; guides/tips/roundups
    rarely do. Bilingual so a site mixing base + its own language is covered."""
    if not text:
        return False
    t = _strip_accents(text)
    ing, meth, seen = [], [], set()
    for L in (base_lang, page_lang):
        L = (L or "").lower()[:2]
        if not L or L in seen:
            continue
        seen.add(L)
        s = recipe_sections(L)
        ing += s.get("ingredients", [])
        meth += s.get("method", [])
    has_ing = any(_strip_accents(m) in t for m in ing)
    has_meth = any(_strip_accents(m) in t for m in meth)
    return has_ing and has_meth


def score_recipe_bilingual(text: str, page_lang: str):
    """(score, threshold) for a page whose language differs from the instance base. A site
    freely MIXES its base language and the page's own language in recipe text/headers, so sum
    BOTH phrase lists (disjoint vocabularies). Threshold = the page language's (its list is the
    real discriminator) when it has one, else the base's."""
    base = instance_base_language()
    score = score_recipe_for_lang(text, base) or 0
    _, thr = _phrases_and_threshold(base)
    page = (page_lang or "").lower()[:2]
    if page and page != base and recipe_phrase_lang_available(page):
        score += score_recipe_for_lang(text, page) or 0
        _, thr = _phrases_and_threshold(page)
    return score, (thr if thr is not None else IS_RECIPE_THRESHOLD)


def is_recipe(text: str, threshold: int = IS_RECIPE_THRESHOLD) -> dict:
    """
    Return a structured validation result for a chunk of recipe-candidate text.

    Mirrors worker_is_recipe in the batch pipeline so behavior stays consistent.
    """
    score = score_recipe_text(text)
    accepted = score >= threshold
    return {
        "accepted": accepted,
        "score": score,
        "threshold": threshold,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": f"Recipe-phrase score {score} (threshold {threshold})",
    }


def stamp_validation_on_recipe(recipe: dict, validation: dict) -> None:
    """Write validation result onto a sanitized recipe dict in-place."""
    recipe["current_status"] = {
        "value": "accepted" if validation["accepted"] else "rejected",
        "reason": validation["reason"],
        "timestamp": validation["timestamp"],
    }
    scoring = recipe.get("_scoring") or {}
    scoring["recipeScore"] = validation["score"]
    scoring["recipeScoreThreshold"] = validation["threshold"]
    recipe["_scoring"] = scoring