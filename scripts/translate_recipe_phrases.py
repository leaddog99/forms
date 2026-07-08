r"""Translate the English RECIPE_PHRASES list into another language ONCE, offline.

The is-recipe filter scores a page by counting recipe-detection phrases in its text.
For a non-English page we used to translate the WHOLE PAGE (per URL, ~40s) just to run
that English phrase count. Instead, translate the PHRASE LIST one time per language and
score the RAW page text directly — no per-page translation ever again.

This is NOT a literal translation: we ask for how Greek (etc.) recipes ACTUALLY write
each thing — natural section headers (υλικά/εκτέλεση), common measurement abbreviations
(κ.σ/κ.γ/γρ./φλ.), and the conjugated cooking verbs recipes use (προσθέτουμε) — so the
phrases match real pages, not a dictionary gloss.

Output: intake/recipe_phrases/<lang>.json  =  {"lang","threshold","phrases":[...]}.
Run:  python -m scripts.translate_recipe_phrases el   [--threshold 7]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from input.pipeline.config import RECIPE_PHRASES, IS_RECIPE_THRESHOLD  # noqa: E402


def _live_recipe_phrases():
    """The LIVE English phrase list (system_config, seed-fallback) so a regenerated pack
    reflects any curator edit made in the admin — not the stale code seed."""
    try:
        from input.pipeline.validators import recipe_phrases
        return recipe_phrases()
    except Exception:
        return RECIPE_PHRASES

_OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "intake", "recipe_phrases")

_LANG_NAMES = {"el": "Greek", "es": "Spanish", "it": "Italian", "fr": "French",
               "de": "German", "pt": "Portuguese", "tr": "Turkish", "nl": "Dutch"}

_TOOL = {
    "name": "phrase_translations",
    "description": "Natural-language recipe phrases for matching real recipe pages.",
    "input_schema": {
        "type": "object",
        "properties": {
            "phrases": {
                "type": "array",
                "items": {"type": "string"},
                "description": "lowercase phrases AS THEY APPEAR IN REAL RECIPES of the "
                               "target language — section headers, measurement words AND "
                               "their common abbreviations, conjugated cooking verbs. "
                               "Include multiple natural variants; omit anything with no "
                               "real recipe form.",
            },
            "ingredients_headers": {
                "type": "array", "items": {"type": "string"},
                "description": "the word(s) that head an INGREDIENTS list in real recipes of "
                               "this language (e.g. Greek 'υλικά', 'συστατικά'). lowercase.",
            },
            "method_headers": {
                "type": "array", "items": {"type": "string"},
                "description": "the word(s) that head the METHOD / preparation steps in real "
                               "recipes of this language (e.g. Greek 'εκτέλεση', 'οδηγίες', "
                               "'παρασκευή'). lowercase. NOT prep-TIME labels.",
            },
        },
        "required": ["phrases", "ingredients_headers", "method_headers"],
    },
}


def _sys_prompt(lang_name: str) -> str:
    return (
        f"You build a phrase list used to DETECT recipe content on {lang_name} web pages by "
        f"plain substring matching of the page's RAW text. You are given the English detection "
        f"phrases. For each, output how {lang_name} recipes ACTUALLY write it — not a literal "
        f"gloss. CRUCIAL:\n"
        f"- Section headers (ingredients→the real {lang_name} header, method/instructions→the "
        f"real header, prep time/cook time/servings/yield).\n"
        f"- Measurements: give BOTH the full word AND the common abbreviation real recipes use "
        f"(e.g. for Greek: γραμμάρια AND γρ.; κουταλιά της σούπας AND κ.σ; κουταλάκι AND κ.γ; "
        f"φλιτζάνι AND φλ.; χιλιοστόλιτρα AND ml).\n"
        f"- Cooking-imperative verbs: give the form recipes actually use (Greek recipes use the "
        f"1st-person plural 'we add/stir/bake' = προσθέτουμε/ανακατεύουμε/ψήνουμε, and the "
        f"imperative). Include several common verbs even if the English entry was one verb.\n"
        f"Output lowercase, deduplicated, multiple variants allowed. Skip an English phrase if "
        f"it has no meaningful {lang_name} recipe form. Match quality > literalness."
    )


def _dedup_lower(items) -> list:
    out, seen = [], set()
    for p in items or []:
        p = str(p).strip().lower()
        if len(p) >= 2 and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def translate_phrases(lang: str):
    """Returns (phrases, sections) — the detection phrase list plus the structural
    section-header markers {'ingredients':[...], 'method':[...]}."""
    import llm
    lang_name = _LANG_NAMES.get(lang, lang.upper())
    resp = llm.create(
        operation="translate_recipe_phrases", model="claude-haiku-4-5",
        max_tokens=8000, temperature=0, system=_sys_prompt(lang_name),
        messages=[{"role": "user",
                   "content": "English detection phrases:\n" + ", ".join(_live_recipe_phrases())}],
        tools=[_TOOL], tool_choice={"type": "tool", "name": "phrase_translations"})
    ti = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
    ti = ti if isinstance(ti, dict) else {}
    phrases = _dedup_lower(ti.get("phrases", []))
    sections = {"ingredients": _dedup_lower(ti.get("ingredients_headers", [])),
                "method": _dedup_lower(ti.get("method_headers", []))}
    return phrases, sections


def main() -> int:
    ap = argparse.ArgumentParser(description="Translate RECIPE_PHRASES to a language (once).")
    ap.add_argument("lang", help="ISO 639-1 code, e.g. el")
    ap.add_argument("--threshold", type=int, default=IS_RECIPE_THRESHOLD,
                    help=f"keep-vs-drop phrase count for this language (default {IS_RECIPE_THRESHOLD})")
    args = ap.parse_args()
    phrases, sections = translate_phrases(args.lang)
    if not phrases:
        print("ERROR: no phrases returned", file=sys.stderr)
        return 1
    os.makedirs(_OUT_DIR, exist_ok=True)
    path = os.path.join(_OUT_DIR, f"{args.lang}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"lang": args.lang, "threshold": args.threshold,
                   "sections": sections, "phrases": phrases},
                  f, ensure_ascii=False, indent=2)
    print(f"wrote {len(phrases)} phrases -> {path} (threshold {args.threshold})")
    print(f"sections: ingredients={sections['ingredients']}  method={sections['method']}")
    if not sections["ingredients"] or not sections["method"]:
        print("WARNING: missing ingredients/method section headers — the structural is-recipe "
              "gate needs BOTH; add them by hand if the model omitted them.")
    print("sample:", ", ".join(phrases[:25]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
