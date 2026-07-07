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

_SYS = (
    "You are a strict is-recipe gate. For each page snippet (anchored on its recipe region), "
    "decide KEEP or DROP. KEEP only a SINGLE complete cooking recipe — one dish with an "
    "ingredient list AND step-by-step instructions. DROP anything else: a recipe roundup / "
    "listicle (many recipes), a how-to / technique article, an ingredient or equipment "
    "explainer, a product / restaurant / news / story page. Judge by the CONTENT, not by how "
    "much cooking vocabulary appears. Give a terse (<=8 word) reason."
)

_TOOL = {
    "name": "verdicts",
    "description": "One keep/drop verdict per page id.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "keep": {"type": "boolean"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "keep"],
                },
            }
        },
        "required": ["items"],
    },
}


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
            if 0 <= idx < len(chunk):
                chunk[idx]["_shadow_verdict"] = "keep" if it.get("keep") else "drop"
                chunk[idx]["_shadow_reason"] = str(it.get("reason") or "")[:120]
                done += 1
    # How often does the LLM DISAGREE with what the heuristic decided? (the signal to review)
    dis = sum(1 for e in gray
              if e.get("_shadow_verdict")
              and (e.get("_shadow_verdict") == "keep") != (e.get("_dropped_reason") is None))
    log(f"  [cascade-shadow] classified {done}/{len(gray)} gray-zone candidates "
        f"({dis} disagree with the heuristic — review in Labeling)")
    return done
