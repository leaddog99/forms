"""cuisine_canon.py — canonicalize LLM-emitted cuisine/ethnicity strings AT THE
RECIPE, at stamp time (curator, 2026-09-04: "we should be doing that in the
recipe!! and saving it").

The extraction models emit free text: "Greek-Mexican fusion", "Creole and Cajun
(African, French, Spanish...)", "Mediterranean with Southeast Asian influences",
"Chinese American". Left raw, every variant becomes its own facet value, its own
would-be cookbook class, and its own join miss. The fold rule (curator's):
take the FIRST cuisine of a concatenation, drop descriptor words, keep the
diaspora hyphen (Italian-American), normalize spelling variants.

ONE function, used by: the identity-card scrub (cuisine/ethnicity), the
provenance enrichment (ethnicity), and the stored-row backfill. Never
destructive: input that can't be canonicalized comes back unchanged — a weird
value in the data is better than a silently emptied one (absent-not-zero)."""
from __future__ import annotations

import re

# Spelling/format variants -> canonical.
FIXUPS = {
    "texmex": "Tex-Mex", "tex mex": "Tex-Mex",
    "italian american": "Italian-American", "chinese american": "Chinese-American",
    "mexican american": "Mexican-American", "korean american": "Korean-American",
    "asian american": "Asian-American", "japanese american": "Japanese-American",
    "greek american": "Greek-American", "cuban american": "Cuban-American",
    "argentine": "Argentinian", "creole": "Creole", "cajun creole": "Cajun",
    "nordic": "Scandinavian", "nordic scandinavian": "Scandinavian",
}
# Words that mark a descriptor, not a cuisine — stripped; if nothing survives,
# the original is returned untouched.
DESCRIPTOR = {"fusion", "inspired", "influence", "influences", "contemporary",
              "modern", "style", "likely", "particularly", "adaptation",
              "coastal", "cuisine", "food", "cooking", "traditional"}
# Multi-word names that are real cuisines, kept whole.
TWO_WORD_OK = {"latin american", "puerto rican", "south african", "sri lankan",
               "west african", "north african", "east african", "eastern european",
               "central european", "western european", "new zealand",
               "central american", "south american", "southeast asian",
               "south asian", "east asian", "middle eastern", "pennsylvania dutch",
               "costa rican", "latin", "african american", "soul food",
               "tex-mex", "emilia-romagna", "han chinese"}
# A hyphen folds to its first part ONLY when both halves are recognizable
# cuisines ("Greek-Mexican" = fusion). A hyphenated name whose halves aren't
# cuisines is ONE identity — "Emilia-Romagna", "Tex-Mex" — and stays whole
# (the first backfill mangled 48 Tex-Mex rows to "Tex").
KNOWN_CUISINES = {"american", "italian", "greek", "french", "british", "mexican",
    "chinese", "japanese", "thai", "indian", "jewish", "german", "spanish",
    "asian", "filipino", "vietnamese", "hungarian", "irish", "jamaican",
    "korean", "egyptian", "turkish", "scandinavian", "english", "cajun",
    "austrian", "caribbean", "lebanese", "australian", "european", "bulgarian",
    "cambodian", "haitian", "cuban", "swedish", "dutch", "ethiopian", "russian",
    "portuguese", "peruvian", "brazilian", "scottish", "moroccan", "belgian",
    "polish", "creole", "ukrainian", "canadian", "indonesian", "argentinian",
    "mediterranean", "persian", "israeli", "welsh", "colombian", "nigerian",
    "czech", "basque", "armenian", "norwegian", "malaysian", "pakistani",
    "taiwanese", "singaporean", "iranian", "danish", "georgian", "afghan",
    "swiss", "finnish"}


def canonicalize(raw: str) -> str:
    """Fold a free-text cuisine/ethnicity to its canonical first cuisine.
    Returns the input unchanged when no confident fold exists."""
    s = str(raw or "").strip()
    if not s:
        return s
    # 1. Concatenations: take the FIRST part (curator's rule) — commas,
    #    slashes, " and ", " with ", parentheticals.
    first = re.split(r"[,/]| and | with |\(", s, maxsplit=1)[0].strip(" -–")
    if not first:
        return s
    low = first.lower()
    if low in FIXUPS:
        return FIXUPS[low]
    if low in TWO_WORD_OK:      # whole-name matches BEFORE descriptor stripping
        return _title(low)      # ("Soul Food" must not lose its 'food')
    words = [w for w in re.split(r"\s+", low) if w]
    # 2. Drop descriptor words ("Modern Australian" -> "Australian").
    kept = [w for w in words if w not in DESCRIPTOR]
    if not kept:
        return s
    low2 = " ".join(kept)
    if low2 in FIXUPS:
        return FIXUPS[low2]
    if low2 in TWO_WORD_OK or len(kept) == 1:
        # 3. Hyphen FUSIONS fold to their first cuisine ("Greek-Mexican" ->
        #    Greek) — only when BOTH halves are known cuisines; the diaspora
        #    pattern X-American stays whole; a hyphenated single identity
        #    (Emilia-Romagna, Tex-Mex) stays whole.
        w = kept[0] if len(kept) == 1 else low2
        if "-" in w and not w.endswith("-american"):
            parts = w.split("-")
            if len(parts) == 2 and all(p in KNOWN_CUISINES for p in parts):
                w = parts[0]
        return _title(w)
    # Multiword, unrecognized ("Han Chinese", "Mid-Atlantic American"): keep the
    # original — better odd than wrong.
    return s


def _title(s: str) -> str:
    return " ".join(
        "-".join(part.capitalize() for part in w.split("-")) for w in s.split())
