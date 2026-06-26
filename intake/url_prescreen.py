"""LLM pre-screen: decide is-this-a-recipe from the URL slug (+ SEMrush Top Keyword)
in ONE batched Haiku call, BEFORE the expensive fetch + whole-page translation.

Why this exists: on a non-English mixed publisher (e.g. a Greek grill site) the
recipe FILTER pays a full page fetch + a full-page `translate_markdown` PER candidate
just to phrase-score "is this a recipe" — ~40 s/URL — because the pages carry no
schema.org/Recipe JSON-LD. Yet a human can tell a tips/guide page ("όλα τα μυστικά
για…" = all the secrets for…) from a real recipe ("η συνταγή για…" = the recipe for…)
from the slug alone. This module turns that human read into one cheap batched call.

Design (see docs/keyword-prescreen.md):
  - ONE Haiku tool call for the whole candidate list; the model reads transliterated
    Greek / any language natively.
  - Verdict per URL: 'recipe' | 'not' | 'unsure'. CONSERVATIVE — only 'not' when clearly
    a non-recipe (buying guide, technique/tips, "everything about X", product/category/
    about). Anything plausibly a recipe (incl. "10 ways to cook X", "how to make X") → 'unsure'.
  - The caller drops ONLY on 'not' (negative-only), so the pre-screen can save translations
    but never admit a page the body check would reject, and false drops are minimized.
  - Best-effort: any failure returns {} → every URL is treated as 'unsure' → no drops
    (the harvest behaves exactly as before).
"""
import re
from urllib.parse import urlparse, unquote

_MODEL = "claude-haiku-4-5"
_BATCH = 80
_VALID = {"recipe", "not", "unsure"}

_SYS = (
    "You screen URLs from ONE recipe publisher's website to decide which pages are actual "
    "step-by-step DISH RECIPES versus other content. You are given each page's URL slug "
    "and (when available) the top search keyword that brings traffic to it. Slugs and "
    "keywords may be transliterated Greek or any other language — read them natively.\n\n"
    "For EACH item return a verdict:\n"
    "  'recipe' — clearly a specific dish recipe page.\n"
    "  'not'    — clearly NOT a recipe: a buying/shopping guide, a technique or tips article "
    "('secrets/μυστικά', 'advice/συμβουλές', 'everything about/τα πάντα για'), a product page, "
    "a category/tag/archive page, or an about/contact/news page.\n"
    "  'unsure' — anything that could plausibly be a recipe.\n\n"
    "Be CONSERVATIVE: only answer 'not' when you are confident. When in doubt, 'unsure'. "
    "A listicle like '10 ways to cook X' or a 'how to make/πώς να φτιάξετε X' page MIGHT be a "
    "recipe → use 'unsure', never 'not'. Greek cues: 'συνταγή/syntagi' = recipe (→ recipe); "
    "'μυστικά/mystika' = secrets, 'συμβουλές/simvoules' = advice, 'τα πάντα/ta-panta' = "
    "everything-about (→ likely 'not'). Return a verdict for EVERY item id you are given."
)

_TOOL = {
    "name": "verdicts",
    "description": "Recipe-vs-not verdict for each URL item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["recipe", "not", "unsure"]},
                    },
                    "required": ["id", "verdict"],
                },
            }
        },
        "required": ["items"],
    },
}


def slug_text(url: str) -> str:
    """Human-readable text from a URL path: last non-empty segment, hyphens/underscores →
    spaces. URL-decoded so %-escaped (incl. real Greek-script) slugs come through."""
    path = unquote(urlparse(url).path or "")
    segs = [s for s in path.split("/") if s]
    seg = segs[-1] if segs else ""
    return re.sub(r"[-_]+", " ", seg).strip()


def prescreen(urls, *, keyword_map=None, log=print) -> dict:
    """Classify `urls` (an iterable of URL strings) in batched Haiku calls.
    `keyword_map` optionally maps url -> SEMrush Top Keyword (extra signal).
    Returns {url: 'recipe'|'not'|'unsure'}. Missing/failed urls are omitted
    (caller treats absence as 'unsure'). Never raises."""
    keyword_map = keyword_map or {}
    urls = [u for u in dict.fromkeys(urls) if u]   # de-dupe, preserve order
    if not urls:
        return {}
    try:
        import llm
    except Exception as e:  # noqa: BLE001
        log(f"  [kw-prescreen] llm gateway unavailable ({type(e).__name__}); skipping")
        return {}

    out: dict = {}
    for start in range(0, len(urls), _BATCH):
        chunk = urls[start:start + _BATCH]
        lines = []
        for i, u in enumerate(chunk):
            kw = (keyword_map.get(u) or "").strip()
            txt = f"[{i}] slug: {slug_text(u) or '(none)'}"
            if kw:
                txt += f"  | keyword: {kw}"
            lines.append(txt)
        try:
            resp = llm.create(
                operation="url_prescreen", model=_MODEL,
                max_tokens=4000, temperature=0, system=_SYS,
                messages=[{"role": "user",
                           "content": "Classify these pages:\n" + "\n".join(lines)}],
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "verdicts"})
        except Exception as e:  # noqa: BLE001
            log(f"  [kw-prescreen] call failed ({type(e).__name__}: {e}); "
                f"{len(chunk)} urls left unsure")
            continue
        ti = next((b.input for b in resp.content
                   if getattr(b, "type", "") == "tool_use"), None)
        if not isinstance(ti, dict):
            continue
        for it in ti.get("items", []):
            try:
                idx = int(it.get("id"))
                v = str(it.get("verdict", "")).strip().lower()
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(chunk) and v in _VALID:
                out[chunk[idx]] = v
    return out
