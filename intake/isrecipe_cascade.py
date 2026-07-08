"""isrecipe_cascade.py — the LLM keep/drop adjudicator for the is-recipe gate.

SHADOW MODE (only mode for now): runs a cheap Haiku keep/drop classify over the
GRAY ZONE of a harvest — the candidates the phrase/structural heuristic is least
sure about (content-bearing pages WITHOUT a schema.org/Recipe declaration) — and
records its verdict alongside the heuristic's decision in `training.db`. It does
NOT change what the harvest keeps/drops; it only LABELS, so a batch or two mints
the gold gray-zone data we lack and lets us measure the cascade before wiring it
to actually decide. See docs/is-recipe-classifier.md.

Key implementation lesson (2026-07-07): send the LLM the RECIPE REGION, not the
page prefix — a food blog leads with a long narrative intro, and Haiku will drop
a real recipe if it only sees the story. We anchor via training_capture._smart_snippet.

Best-effort: any failure leaves the entries unstamped (caller ignores). Never raises.
"""
from __future__ import annotations

_MODEL = "claude-haiku-4-5"
_BATCH = 8               # anchored snippets per Haiku call
_MIN_CHARS = 200         # skip stubs / empty — nothing to classify
_MAX_PER_RUN = 300       # cost backstop per harvest

_VALID = {"recipe", "not_recipe", "poor_quality"}

_SYS = (
    "You are a recipe-SOURCE quality gate. For each page snippet (anchored on its recipe "
    "region), return ONE of three verdicts — judge EXTRACTABILITY, not just presence:\n"
    "- 'recipe' — a SINGLE dish presented as a CLEAN, extractable recipe: a distinct "
    "ingredient list AND ordered step-by-step instructions we could reliably pull.\n"
    "- 'not_recipe' — genuinely not a recipe: a roundup/listicle (many recipes), a "
    "how-to/technique article, an ingredient or equipment explainer, a review, or a "
    "product/restaurant/news/story page.\n"
    "- 'poor_quality' — a recipe IS technically present, but the layout is too messy to "
    "extract cleanly: ingredients bleed into editorial prose, image-heavy with little text, "
    "no clear ingredient/step separation. A human could dig the recipe out, but it's a poor "
    "source. Reject it here.\n"
    "TIE-BREAK: when genuinely torn between 'recipe' and 'poor_quality', choose 'poor_quality' "
    "(protect ingest quality — only clean, confidently-extractable pages earn 'recipe').\n"
    "Judge the CONTENT, not how much cooking vocabulary appears. Give a terse (<=8 word) reason."
)

_TOOL = {
    "name": "verdicts",
    "description": "One verdict per page id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "verdict": {"type": "string", "enum": ["recipe", "not_recipe", "poor_quality"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "verdict"],
                },
            }
        },
        "required": ["items"],
    },
}


def cascade_mode() -> str:
    """Effective cascade mode from system_config `is_recipe_cascade_mode`
    ('off' | 'shadow' | 'decide'). Anything unrecognized -> 'off'. (Replaces the
    legacy `is_recipe_cascade_shadow` bool, migrated to 'shadow' on this instance.)"""
    try:
        from input.pipeline.system_config import get_setting as _gs
        m = (_gs("is_recipe_cascade_mode", "off") or "off").strip().lower()
        return m if m in ("off", "shadow", "decide") else "off"
    except Exception:
        return "off"


def apply_decide(kept, dropped, *, log=print) -> tuple:
    """DECIDE mode — apply the asymmetric cascade override, mutating `kept`/`dropped`
    IN PLACE (call AFTER shadow_classify has stamped verdicts AND after training capture,
    so the recorded label stays the HEURISTIC decision). Policy (validated ~77% rescue /
    ~88% catch precision on curator labels):
      - RESCUE: a heuristic DROP the LLM calls 'recipe'            -> move to kept.
      - CATCH:  a heuristic KEEP the LLM calls not_recipe/poor_quality -> move to dropped
                (never overrides a JSON-LD keep — those never reach the LLM anyway).
    Entries with no LLM verdict (outside the gray zone) are untouched. Returns (rescued, caught)."""
    new_kept, new_dropped, resc, caught = [], [], 0, 0
    for e in kept:
        sv = e.get("_shadow_verdict")
        if sv in ("not_recipe", "poor_quality") and not e.get("jsonld_recipe"):
            e["_dropped_reason"] = f"cascade-{sv}"
            new_dropped.append(e)
            caught += 1
        else:
            new_kept.append(e)
    for e in dropped:
        if e.get("_shadow_verdict") == "recipe":
            e.pop("_dropped_reason", None)
            new_kept.append(e)
            resc += 1
        else:
            new_dropped.append(e)
    kept[:] = new_kept
    dropped[:] = new_dropped
    log(f"  [cascade-decide] rescued {resc} (heuristic-drop -> keep), "
        f"caught {caught} (heuristic-keep -> drop)")
    return resc, caught


def shadow_classify(entries, *, log=print) -> int:
    """Stamp `_shadow_verdict` ('keep'|'drop') + `_shadow_reason` onto each GRAY-ZONE
    entry in `entries` (a mixed list of kept+dropped filter entries). Returns the count
    classified. Never raises. Reads the transient `_cap_text` the filter stashed."""
    try:
        from intake.training_capture import _smart_snippet
    except Exception:
        return 0
    gray = [e for e in (entries or [])
            if isinstance(e, dict) and not e.get("jsonld_recipe")
            and len((e.get("_cap_text") or "").strip()) >= _MIN_CHARS]
    if not gray:
        return 0
    if len(gray) > _MAX_PER_RUN:
        log(f"  [cascade-shadow] {len(gray)} gray-zone candidates — capping at {_MAX_PER_RUN}")
        gray = gray[:_MAX_PER_RUN]
    try:
        import llm
    except Exception as e:  # noqa: BLE001
        log(f"  [cascade-shadow] llm gateway unavailable ({type(e).__name__}); skipping")
        return 0

    done = 0
    for start in range(0, len(gray), _BATCH):
        chunk = gray[start:start + _BATCH]
        lines = [f"[{i}] {_smart_snippet((e.get('_cap_text') or ''), 1600)}"
                 for i, e in enumerate(chunk)]
        try:
            resp = llm.create(
                operation="isrecipe_cascade_shadow", model=_MODEL,
                max_tokens=2500, temperature=0, system=_SYS,
                messages=[{"role": "user", "content": "Pages:\n\n" + "\n\n".join(lines)}],
                tools=[_TOOL], tool_choice={"type": "tool", "name": "verdicts"})
        except Exception as e:  # noqa: BLE001
            log(f"  [cascade-shadow] call failed ({type(e).__name__}: {e}); {len(chunk)} left unclassified")
            continue
        ti = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        if not isinstance(ti, dict):
            continue
        for it in ti.get("items", []):
            try:
                idx = int(it.get("id"))
            except (TypeError, ValueError):
                continue
            v = str(it.get("verdict") or "").strip().lower()
            if 0 <= idx < len(chunk) and v in _VALID:
                chunk[idx]["_shadow_verdict"] = v   # three-way: recipe|not_recipe|poor_quality
                chunk[idx]["_shadow_reason"] = str(it.get("reason") or "")[:120]
                done += 1
    # How often does the LLM DISAGREE with what the heuristic decided? (the signal to review)
    # keep-equivalent = verdict 'recipe'; both not_recipe and poor_quality mean drop.
    dis = sum(1 for e in gray
              if e.get("_shadow_verdict")
              and (e.get("_shadow_verdict") == "recipe") != (e.get("_dropped_reason") is None))
    log(f"  [cascade-shadow] classified {done}/{len(gray)} gray-zone candidates "
        f"({dis} disagree with the heuristic — review in Labeling)")
    return done
