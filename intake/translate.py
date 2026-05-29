"""Recipe-aware translation of non-English source pages to English.

Two-flavor extraction (per [[multilingual-extraction]]): when a fetched
page is not in English, translate the cleaned markdown to English via
Haiku BEFORE handing it to the existing recipe filter and is_recipe LLM
check. The English version is the canonical recipe body; the original
language is stamped on `_source` for provenance and a future
"view original" affordance.

Why a separate module: this is used in TWO places — the batch pipeline
(`intake/build_query_batch.py`, between `_fetch_for_filter` and
`_is_recipe_filter`) and the single-URL extract path (the live form's
"Extract" button on a non-English source). Sharing one implementation
avoids drift.

Pipeline shape:

    fetched_text + html
        |
        v
    detect_language(html, headers, text)  ->  ISO 639-1 code
        |
       en?  -- yes ---> caller continues unchanged
        |
       no
        v
    translate_markdown(markdown, src_lang)  ->  (english_md, original_title)
        |
        v
    is_translation_plausible(original, translated) -> bool
        |
        v
    caller continues with translated markdown
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import anthropic


_anthropic_client = anthropic.Anthropic()

# Haiku is the right tier here: recipe-aware translation needs context
# (loanwords, quantity formats, technique verbs) which generic NMT
# misses, but doesn't need Sonnet's reasoning depth. Cost ~$0.0005/page.
_TRANSLATION_MODEL = "claude-haiku-4-5-20251001"
_TRANSLATION_MAX_TOKENS = 8000

# fasttext-langdetect's model file (lid.176.ftz) is ~917KB, downloaded
# once on first call and cached under ~/.cache/fasttext_langdetect.
# We import lazily so the module loads fine on systems without the
# package installed (until something actually calls detect_language).

_LANG_NAMES = {
    "el": "Greek",
    "it": "Italian",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "pt": "Portuguese",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "ru": "Russian",
    "tr": "Turkish",
    "ar": "Arabic",
    "he": "Hebrew",
    "nl": "Dutch",
    "pl": "Polish",
    "sv": "Swedish",
    "th": "Thai",
    "vi": "Vietnamese",
    "hi": "Hindi",
}


@dataclass
class TranslationResult:
    """Output of translate_markdown — the translated body plus enough
    metadata to (a) stamp `_source` for provenance and (b) let callers
    sanity-check the translation before passing it downstream."""
    translated_markdown: str
    original_title: Optional[str]   # for "view original" affordance
    source_language: str            # ISO 639-1
    source_language_name: str       # human-readable ("Greek")


# === Language detection ===========================================

_HTML_LANG_RE = re.compile(r'<html[^>]*\blang\s*=\s*["\']([a-zA-Z]{2,3})', re.IGNORECASE)


def detect_language(
    html: str,
    headers: Optional[dict] = None,
    visible_text: Optional[str] = None,
) -> str:
    """Return an ISO 639-1 (2-letter) language code for the page.

    Three-tier detection in cost-ascending order:

      1. <html lang="..."> — author-declared, free, accurate when present.
         Strip region suffix ("en-US" -> "en", "zh-Hans" -> "zh").
      2. Content-Language response header — second-best, also free.
      3. fasttext-langdetect on the visible text — covers pages that
         don't declare lang. Trained on Wikipedia + Common Crawl, 176
         languages, ~99% accurate on prose of more than ~50 chars.

    Returns 'en' as a conservative default if detection fails entirely
    (better to skip translation than mistranslate an English page).
    """
    # 1. <html lang="...">
    m = _HTML_LANG_RE.search(html or "")
    if m:
        code = m.group(1).lower()[:2]
        return code

    # 2. Content-Language header (some CDNs set this; many don't)
    if headers:
        # requests' CaseInsensitiveDict supports .get() like a normal
        # dict, but we also accept raw dicts from other clients.
        cl = headers.get("content-language") or headers.get("Content-Language")
        if cl:
            code = cl.split(",")[0].strip().lower()[:2]
            if code:
                return code

    # 3. fasttext-langdetect on visible text. Lazy import so module
    # load doesn't require the package.
    if visible_text and len(visible_text.strip()) >= 50:
        try:
            from ftlangdetect import detect as _ft_detect
            # ftlangdetect doesn't like newlines in input
            sample = visible_text.replace("\n", " ").strip()[:2000]
            result = _ft_detect(text=sample, low_memory=True)
            lang = (result.get("lang") or "").lower()
            if lang:
                return lang
        except ImportError:
            print("  [translate] fasttext-langdetect not installed; skipping detection")
        except Exception as e:
            print(f"  [translate] langdetect failed: {type(e).__name__}: {e}")

    # Conservative default: skip translation rather than mistranslate.
    return "en"


# === Translation ==================================================

_TRANSLATION_SYSTEM = """You are translating a web recipe page from {src_lang_name} to English.
This is for a culinary search index, so accuracy of cooking-specific detail matters more
than literary polish.

RULES:
1. Preserve culinary loanwords in their original spelling: feta, halloumi, saganaki,
   miso, dashi, baguette, brioche, mole, masala, kefir, tzatziki, ouzo, retsina,
   tahini, harissa, panko, etc. Do NOT translate dish names that are already familiar
   English-language culinary vocabulary.

2. On first mention of a dish name that is NOT familiar in English, render it as
   "Original (English gloss)" — e.g., "Spanakorizo (Greek spinach rice)". After the
   first mention, use the original alone.

3. Preserve all quantities in their original numeric+unit format. "200γρ" -> "200g",
   "1 κ.σ." -> "1 tbsp", "200 ml" -> "200 ml". Do NOT convert metric to imperial or
   vice versa. Do NOT spell out numerals ("2" stays "2", not "two").

4. Translate cooking verbs into standard culinary English. Greek examples:
   σοταρω -> sauté, τσιγαρίζω -> sweat / pan-fry, βράζω -> boil,
   σιγοβράζω -> simmer, ψήνω -> bake/roast (context-dependent),
   μαρινάρω -> marinate. Use the equivalent rule for whatever source language
   you're translating.

5. Translate ingredient names plainly when there is an unambiguous English
   equivalent (κρεμμύδι -> onion, σκόρδο -> garlic). When ambiguous, prefer the
   term a cookbook editor would use: αυγολέμονο -> avgolemono (loanword) NOT
   "egg-lemon sauce".

6. When a fenced ```json``` block appears (typically schema.org Recipe JSON-LD),
   preserve the JSON structure exactly — keys, brackets, commas, types — but
   translate the string VALUES. So `"name": "Σπανακόρυζο"` becomes
   `"name": "Spanakorizo"`, and `"recipeIngredient": ["σπανάκι 500γρ"]` becomes
   `"recipeIngredient": ["spinach 500g"]`. Numeric values, ISO duration strings
   ("PT15M"), URLs, and @type / @context values stay untouched.

7. Preserve markdown structure exactly: headings, lists, links, image URLs.
   Do not invent or omit sections.

8. Preserve author names, brand names, and URLs in original form.

Output ONLY the translated markdown. No preamble, no explanation, no code fences."""


_TRANSLATION_USER = "Translate the following {src_lang_name} recipe page to English:\n\n{markdown}"


# Match the first markdown ATX heading (# Title) OR the first setext-style
# heading underline (Title\n===). The dish title from the original page is
# preserved on `_source.originalTitle` for the "view original" link.
_H1_ATX_RE = re.compile(r'^\s*#\s+(.+?)\s*$', re.MULTILINE)
_H1_SETEXT_RE = re.compile(r'^\s*(.+?)\s*\n=+\s*$', re.MULTILINE)


def _extract_first_h1(markdown: str) -> Optional[str]:
    """Pull the first h1 from markdown as the original-language title.
    We want this BEFORE translation, so the byline-in-original-language
    is preserved for the "view original" affordance even when the
    translator rewrites the heading."""
    if not markdown:
        return None
    m = _H1_ATX_RE.search(markdown)
    if m:
        return m.group(1).strip() or None
    m = _H1_SETEXT_RE.search(markdown)
    if m:
        return m.group(1).strip() or None
    return None


def translate_markdown(markdown: str, src_lang: str) -> TranslationResult:
    """Translate cleaned markdown to English via Haiku with a
    recipe-aware system prompt.

    Caller passes the language code from `detect_language`. Empty markdown
    returns an empty TranslationResult — caller should skip those before
    calling this function rather than paying for a no-op LLM call.

    Raises anthropic.* errors on API failure; caller decides whether to
    retry, fall back to the original markdown, or drop the URL.
    """
    src_name = _LANG_NAMES.get(src_lang, src_lang.upper())
    original_title = _extract_first_h1(markdown)

    msg = _anthropic_client.messages.create(
        model=_TRANSLATION_MODEL,
        max_tokens=_TRANSLATION_MAX_TOKENS,
        system=_TRANSLATION_SYSTEM.format(src_lang_name=src_name),
        messages=[{
            "role": "user",
            "content": _TRANSLATION_USER.format(
                src_lang_name=src_name, markdown=markdown,
            ),
        }],
    )

    # Anthropic responses are a list of content blocks; for our single
    # text request there's exactly one TextBlock.
    translated = "".join(
        block.text for block in msg.content if getattr(block, "type", None) == "text"
    ).strip()

    return TranslationResult(
        translated_markdown=translated,
        original_title=original_title,
        source_language=src_lang,
        source_language_name=src_name,
    )


# === Sanity check =================================================

# Quantities we expect to survive translation intact. The translation
# prompt explicitly preserves numeric+unit formats, so a translated page
# missing all numeric content is a sign Haiku silently truncated or got
# confused.
_QUANTITY_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:g|kg|mg|ml|l|tbsp|tsp|cup|cups|oz|lb|°[cf]?)\b", re.IGNORECASE)
# Fallback: any standalone integer (cooking times, ingredient counts).
_INTEGER_RE = re.compile(r"\b\d{1,3}\b")


def is_translation_plausible(original: str, translated: str, *, min_length_ratio: float = 0.4) -> tuple[bool, str]:
    """Return (ok, reason). Used as a safety net BEFORE handing the
    translated markdown to the is_recipe LLM check — prefer dropping a
    URL with "translation suspect" over poisoning OU/extraction with
    garbage.

    Failure modes worth catching cheaply:

      - Truncation: Haiku occasionally stops mid-response, producing
        an output that's a tiny fraction of the original length.
      - Hallucinated empty: model returned an explanation instead of
        the translation, output is short prose with no recipe shape.
      - Lost numbers: model "explained" instead of preserving
        "200 g flour" -> result has no quantities at all.

    These checks are intentionally loose. A well-formed Greek page that
    DOES translate cleanly will satisfy all three; we only catch the
    obvious-failure cases.
    """
    if not translated or not translated.strip():
        return False, "empty translation"

    # 1. Length ratio. Translated English of a recipe is usually within
    #    ±30% of the source; below 40% suggests truncation.
    orig_len = len(original or "")
    if orig_len > 200:  # skip the check on tiny inputs (titles, snippets)
        ratio = len(translated) / orig_len
        if ratio < min_length_ratio:
            return False, f"length ratio {ratio:.2f} < {min_length_ratio}"

    # 2. Numeric content. Real recipes contain quantities; a translation
    #    with zero quantity-like numeric content lost the recipe shape.
    if not _QUANTITY_RE.search(translated) and len(_INTEGER_RE.findall(translated)) < 3:
        return False, "no quantities or numeric content"

    # 3. Some structural shape. A real recipe page produces at least
    #    one bullet list or heading after translation.
    if "\n- " not in translated and "\n* " not in translated and "\n#" not in translated:
        # Heuristic — accept the translation anyway if it's long enough
        # that the absence of list markers is just an artifact of how
        # the page was structured (some recipe sites do paragraphs).
        if len(translated) < 800:
            return False, "no list/heading structure"

    return True, "ok"


# === Public helpers ===============================================


def is_non_english(lang_code: str) -> bool:
    """True if the code is something we should translate. Empty / 'en'
    are no-ops."""
    if not lang_code:
        return False
    return lang_code.lower()[:2] != "en"


def language_name(lang_code: str) -> str:
    """English name for a language code. Falls back to the uppercased
    code when unknown."""
    if not lang_code:
        return ""
    return _LANG_NAMES.get(lang_code.lower()[:2], lang_code.upper())


# === Extraction-stage translation wrapper ==========================
#
# The extraction pipeline produces canonical markdown shaped like:
#
#     # <title>
#
#     URL: <final_url>
#
#     ## STRUCTURED RECIPE DATA (JSON-LD)
#
#     ```json
#     {...}
#     ```
#
#     ## PAGE CONTENT
#
#     <body markdown>
#
# For non-English pages we strip the JSON-LD section before translation
# so the downstream LLM (markdown_to_recipe) doesn't fall back to its
# "trust JSON-LD as authoritative" rule and end up pulling Greek strings
# into the recipe fields anyway. With the section gone, the LLM derives
# every field from the translated English prose.
#
# JSON-LD content is still available via the html_to_markdown result
# dict (`jsonld` list) — we just keep it out of the LLM's input on
# non-English pages.

_JSONLD_SECTION_RE = re.compile(
    r"##\s+STRUCTURED RECIPE DATA.*?(?=^##\s+|\Z)",
    re.DOTALL | re.MULTILINE,
)


def strip_jsonld_section(markdown: str) -> str:
    """Remove the `## STRUCTURED RECIPE DATA (JSON-LD)` section and its
    fenced ```json``` block. Returns markdown with title + URL preamble
    + `## PAGE CONTENT` body intact.

    Idempotent — calling on already-stripped markdown returns it as is.
    """
    return _JSONLD_SECTION_RE.sub("", markdown or "").strip()


@dataclass
class ExtractionTranslationResult:
    """Output of `translate_extraction_markdown` — carries everything an
    extraction pipeline needs to (a) hand the LLM English markdown,
    (b) stamp _source provenance fields, and (c) signal whether the
    JSON-LD fast lane should be skipped."""
    translated_markdown: str        # JSON-LD stripped + body translated
    original_title: Optional[str]   # for _source.originalTitle
    source_language: str            # ISO 639-1 -> _source.originalLanguage
    source_language_name: str       # for "Translated from Greek" pill
    skip_jsonld_fast_lane: bool = True  # always true for non-English
    plausibility_ok: bool = True
    plausibility_reason: str = "ok"


def translate_extraction_markdown(
    markdown_with_jsonld: str, src_lang: str,
) -> ExtractionTranslationResult:
    """Prep markdown for the extraction LLM when the source page is
    non-English. Translates the ENTIRE markdown including JSON-LD
    string values (per the prompt's JSON-aware rule #6) so the
    extraction LLM sees English regardless of whether it pulls from
    structured data or prose.

    Why not strip JSON-LD: on JSON-LD-heavy publishers like Akis
    Petretzikis, ALL recipe content lives in the structured data block
    and the body prose is just narrative. Stripping JSON-LD before
    translation left the plausibility check with no quantities to find
    (rejected with "no quantities or numeric content"). Translating
    JSON-LD in place — values translated, structure preserved —
    makes the plausibility signal honest and avoids losing the
    page's recipe content entirely.

    The downstream JSON-LD fast lane (`jsonld_to_recipe`) is still
    skipped via `skip_jsonld_fast_lane=True` because the JSON-LD blob
    on `md_result["jsonld"]` is the ORIGINAL parsed object (Greek
    values), not the translated one — only the markdown copy got
    translated. `markdown_to_recipe` reads from the markdown string
    and gets English; `jsonld_to_recipe` would read from the parsed
    dict and get Greek, so we skip it.

    Caller checks `plausibility_ok` before using; on failure or
    on translation API exception, caller falls back to the original
    markdown and does not stamp translation provenance.
    """
    tr = translate_markdown(markdown_with_jsonld, src_lang)
    ok, why = is_translation_plausible(
        markdown_with_jsonld, tr.translated_markdown,
    )
    return ExtractionTranslationResult(
        translated_markdown=tr.translated_markdown,
        original_title=tr.original_title,
        source_language=tr.source_language,
        source_language_name=tr.source_language_name,
        skip_jsonld_fast_lane=True,
        plausibility_ok=ok,
        plausibility_reason=why,
    )
