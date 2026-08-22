"""Soundness defect pass — material-defect audit of a stored recipe.

One Haiku call per recipe answering a single question: is this recipe, AS
STORED, materially complete and internally consistent — and if not, what
exactly is wrong, quoted verbatim from the text?

Design: docs/soundness-defect-pass.md. The boundary that must hold (from
the AI-editor cancellation 2026-08-12): NO scalar of any kind is emitted —
defects with citations only, plus a disqualify-for-cause boolean. Taste,
sophistication and brevity are out of scope by construction; terse is not
thin (measured 2026-08-22: 20.1% of winners flag "thin" structurally, and
judgment samples showed those overwhelmingly innocent).

The honesty layer is mechanical, not human: every defect must carry a
VERBATIM quote from the recipe text, and `validate_report` drops any
defect whose quote does not appear (whitespace-folded) — a hallucinated
citation cannot survive into storage, and `disqualify` is only honored
when a critical defect survives validation. Review scales by calibrating
per DEFECT_PROMPT_VERSION (sampled audit), never per row.

Follows the extract/identity_card.py house pattern: ordered tool_use
schema (facts before verdict — the model must list resolved references,
then defects, and only then may answer disqualify), hashed prompt
version, temperature 0.2.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

import anthropic

# Module-level client (identity_card pattern): reads ANTHROPIC_API_KEY.
_anthropic_client = anthropic.Anthropic()


# --------------------------------------------------------------------------- #
# Tool schema. Property ORDER matters: Anthropic's tool_use emits keys in
# schema order, so resolved_references is composed BEFORE defects (forcing
# the resolution attempt) and defects BEFORE disqualify (forcing the
# verdict to follow from the evidence).
# --------------------------------------------------------------------------- #
DEFECT_CATEGORIES = [
    "missing_step",
    "unused_ingredient",
    "unmade_ingredient",
    "broken_order",
    "contradiction",
    "unresolvable_reference",
    "truncation",
]

DEFECT_TOOL = {
    "name": "submit_defect_report",
    "description": (
        "Submit the defect audit. resolved_references FIRST (group "
        "references you resolved), then defects (each with a VERBATIM "
        "quote), then the disqualify verdict."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "resolved_references": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Group/collective references found AND resolved, e.g. "
                    "'remaining marinade ingredients -> the spices in the "
                    "ingredient list'. Empty when the recipe has none."
                ),
            },
            "defects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string", "enum": DEFECT_CATEGORIES},
                        "severity": {"type": "string",
                                     "enum": ["critical", "major", "minor"]},
                        "evidence": {
                            "type": "string",
                            "description": "VERBATIM quote from the recipe "
                                           "(a step or ingredient line)",
                        },
                        "explanation": {
                            "type": "string",
                            "description": "One sentence: why a competent "
                                           "cook is stuck",
                        },
                    },
                    "required": ["category", "severity", "evidence",
                                 "explanation"],
                },
            },
            "disqualify": {"type": "boolean"},
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": (
                    "LOW when quantities/yield are missing or the text looks "
                    "truncated — absence of evidence is not evidence of "
                    "soundness"
                ),
            },
        },
        "required": ["resolved_references", "defects", "disqualify",
                     "confidence"],
    },
}


_SYSTEM_PROMPT = (
    "You are auditing a recipe for MATERIAL DEFECTS — problems that would "
    "leave a competent home cook unable to follow it as written. You are a "
    "proofreader of procedure, not a food critic.\n\n"
    "You are NOT judging:\n"
    "- how good the finished dish would taste\n"
    "- whether the recipe is sophisticated, traditional, or \"worthy\"\n"
    "- brevity. A complete recipe can be two sentences. Cocktails, salads "
    "and simple sauces are often honestly written in 2-3 short steps. "
    "Terse is not a defect. ONLY missing or contradictory information "
    "is.\n"
    "- style, storytelling, or anything about the source website\n\n"
    "RESOLVE REFERENCES BEFORE FLAGGING. Recipes routinely say \"combine "
    "all the marinade ingredients\" or \"add the remaining dressing "
    "ingredients\" without naming each one. If a reasonable cook can "
    "resolve the reference from the ingredient list and context, it is "
    "NOT a defect. Step labels (e.g. a step labelled \"Frosting\") are "
    "part of the context. Flag a group reference ONLY when it genuinely "
    "cannot be resolved — e.g. two identical \"mix all ingredients\" "
    "steps over one undivided list, with no way to tell which "
    "ingredients belong to which.\n\n"
    "DEFECT CATEGORIES (use exactly these):\n"
    "- missing_step: an action the recipe depends on but never states "
    "(dough is shaped \"after resting\" but no rest exists; a component "
    "is used but never made)\n"
    "- unused_ingredient: a listed ingredient no step uses or plausibly "
    "covers via a resolvable group reference\n"
    "- unmade_ingredient: a step uses something neither listed nor "
    "produced by a prior step\n"
    "- broken_order: a step depends on the result of a LATER step\n"
    "- contradiction: two statements that cannot both be followed "
    "(temperatures, times, quantities, pan sizes)\n"
    "- unresolvable_reference: a group reference that cannot be resolved "
    "(see above — flag ONLY after trying to resolve it)\n"
    "- truncation: the text visibly breaks off — a step ends "
    "mid-sentence, or the recipe plainly stops before the dish is made\n\n"
    "SEVERITY:\n"
    "- critical: the dish cannot be completed as written\n"
    "- major: a competent cook must guess something consequential\n"
    "- minor: a competent cook will notice, be briefly confused, and "
    "recover\n\n"
    "EVIDENCE IS MANDATORY AND VERBATIM. Every defect must carry a quote "
    "copied EXACTLY from the recipe text (the step or ingredient line it "
    "concerns). If you cannot quote it, you cannot flag it.\n\n"
    "DO NOT INVENT DEFECTS. Most published recipes that reached this "
    "audit are sound. An empty defect list is the expected, normal "
    "outcome. Do not manufacture minor defects to appear thorough. Do "
    "not pad.\n\n"
    "disqualify = true ONLY when at least one critical defect exists, or "
    "defects together mean the recipe as stored cannot produce the dish. "
    "Missing garnish detail is not disqualifying. When unsure, "
    "disqualify = false and let the defects speak.\n\n"
    "Output ONLY through the submit_defect_report tool — no narration."
)

DEFECT_PROMPT_VERSION = hashlib.sha256(
    _SYSTEM_PROMPT.encode("utf-8")
).hexdigest()[:12]

_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 2000
_TEMPERATURE = 0.2


# --------------------------------------------------------------------------- #
# Prompt composition — BLIND: no URL, no publisher, no scores. The model
# must not know whose recipe it is.
# --------------------------------------------------------------------------- #
def _iter_step_texts(recipe: dict):
    """Yield (label, text) per instruction step. label is the step's own
    name when it carries one (section labels landed by jsonld_to_recipe's
    flattener), else ''."""
    for s in recipe.get("recipeInstructions") or []:
        if isinstance(s, dict):
            text = (s.get("text") or "").strip()
            label = (s.get("name") or "").strip()
        else:
            text, label = str(s).strip(), ""
        if text:
            yield label, text


def build_defect_user_prompt(recipe: dict) -> str:
    name = (recipe.get("name") or "").strip()
    ry = recipe.get("recipeYield")
    if isinstance(ry, list):
        ry = "; ".join(str(y) for y in ry if y)
    lines = [f"RECIPE: {name}" if name else "RECIPE (untitled)"]
    if ry:
        lines.append(f"Yield: {ry}")
    lines.append("")
    lines.append("INGREDIENTS:")
    for ing in recipe.get("recipeIngredient") or []:
        ing = str(ing).strip()
        if ing:
            lines.append(f"- {ing}")
    lines.append("")
    lines.append("INSTRUCTIONS:")
    for i, (label, text) in enumerate(_iter_step_texts(recipe), 1):
        prefix = f"{i}. [{label}] " if label else f"{i}. "
        lines.append(prefix + text)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The mechanical honesty layer: code verifies every citation.
# --------------------------------------------------------------------------- #
def _fold(s: str) -> str:
    """Whitespace-fold + lowercase for substring matching. Curly quotes
    and dashes normalised so a model's typographic slip doesn't void a
    real citation."""
    s = (s or "").lower()
    s = (s.replace("’", "'").replace("‘", "'")
          .replace("“", '"').replace("”", '"')
          .replace("–", "-").replace("—", "-"))
    return re.sub(r"\s+", " ", s).strip()


def recipe_haystack(recipe: dict) -> str:
    """The text a defect is allowed to cite: name, yield, ingredient
    lines, step labels and step texts."""
    parts = [recipe.get("name") or ""]
    ry = recipe.get("recipeYield")
    parts.extend(str(y) for y in (ry if isinstance(ry, list) else [ry]) if y)
    parts.extend(str(i) for i in recipe.get("recipeIngredient") or [])
    for label, text in _iter_step_texts(recipe):
        parts.append(label)
        parts.append(text)
    return _fold(" \n ".join(parts))


def validate_report(report: dict, recipe: dict) -> dict:
    """Drop defects whose evidence is not a verbatim substring of the
    recipe text; honor disqualify only if a CRITICAL defect survives.
    Returns a new report dict with `evidence_dropped` (count) added.
    Never raises."""
    haystack = recipe_haystack(recipe)
    kept, dropped = [], 0
    for d in report.get("defects") or []:
        if not isinstance(d, dict):
            dropped += 1
            continue
        ev = _fold(d.get("evidence") or "")
        if ev and ev in haystack:
            kept.append(d)
        else:
            dropped += 1
            print(f"[DEFECT-PASS] defect_hallucinated: "
                  f"{d.get('category')} evidence not found: "
                  f"{(d.get('evidence') or '')[:80]!r}")
    disqualify = bool(report.get("disqualify")) and any(
        d.get("severity") == "critical" for d in kept)
    if report.get("disqualify") and not disqualify:
        print("[DEFECT-PASS] disqualify NOT honored — no critical defect "
              "survived evidence validation")
    return {
        "resolved_references": report.get("resolved_references") or [],
        "defects": kept,
        "disqualify": disqualify,
        "confidence": report.get("confidence") or "low",
        "evidence_dropped": dropped,
    }


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #
def _call_defect_tool(user_prompt: str, *,
                      usage_log: Optional[list] = None) -> Optional[dict]:
    """Shared LLM call shape (identity_card pattern). Returns the raw tool
    input dict, or None on failure. Never raises."""
    try:
        response = _anthropic_client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            temperature=_TEMPERATURE,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            tools=[DEFECT_TOOL],
            tool_choice={"type": "tool", "name": "submit_defect_report"},
        )
    except Exception as e:
        print(f"[DEFECT-PASS] LLM call failed: {type(e).__name__}: {e}")
        return None

    # Same journaling contract as identity_card: not routed through the
    # llm.py gateway; the caller owns attribution via usage_log.
    if usage_log is not None:
        try:
            from input.pipeline.token_journal import build_usage_entry
            usage_log.append(
                build_usage_entry("defect_pass", _MODEL, response))
        except Exception:
            pass

    tool_input = next(
        (b.input for b in response.content
         if getattr(b, "type", "") == "tool_use"
         and getattr(b, "name", "") == "submit_defect_report"),
        None,
    )
    if not isinstance(tool_input, dict):
        return None
    return tool_input


def run_defect_pass(recipe: dict, *,
                    usage_log: Optional[list] = None) -> Optional[dict]:
    """Audit one recipe. Returns the validated `_soundness`-shaped dict
    (prompt/model stamped, citations verified), or None when the call
    failed or the recipe has no instruction text to audit."""
    from datetime import datetime, timezone
    if not any(True for _ in _iter_step_texts(recipe)):
        return None
    raw = _call_defect_tool(build_defect_user_prompt(recipe),
                            usage_log=usage_log)
    if raw is None:
        return None
    report = validate_report(raw, recipe)
    report.update({
        "prompt_version": DEFECT_PROMPT_VERSION,
        "model": _MODEL,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })
    return report
