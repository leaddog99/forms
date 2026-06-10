"""cook_rework.py — turn a captured recipe into a validated `_cook` block.

The cook-rework ENGINE (recipe_anchor/phase3-pipeline-design.md). SHARED componentry:
both surfaces use it (TBOTB reworks curated masters; BCC reworks a user's own recipe).
Surface-agnostic — the trigger/permission differs per surface, this engine does not.

Division of labor (the cost architecture made real):
  • CODE does the mechanical/deterministic work — pull amounts + conversions from
    `_measurements`, compute the appearance-order `first_step` indices, run the §5
    gauntlet (cook_validators). $0, reliable.
  • The LLM does ONLY judgment — technique audit, scheduling, bundling/mise, anchoring
    prose, copy. It is GROUNDED by `_measurements` (told to use the provided metric
    conversions, never invent them) so amounts stay unit-consistent.

v1 = a single Opus pass that emits the whole `_cook`, then validate + one repair loop.
The confirmed opus→sonnet tiering (judgment pass → realization pass) is the next
optimization; this proves the loop end-to-end first.
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable, Optional, Tuple

import anthropic

from cook_model import CookMetadata
from cook_validators import run_all
from cook_augment import augment_cook

_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

OPUS = "claude-opus-4-8"
_MAX_TOKENS = 8192

# Bump when the prompt/schema changes so stale reworks are detectable + re-runnable.
# v2: rework no longer invents tips/checks — the augment pass (cook_augment) attaches
# them from the PUBLISHED KB with provenance (kb_id).
REWORK_PROMPT_VERSION = "cook-rework-v2-2026-06-10"


# --------------------------------------------------------------------------- #
# PRE (code): build the rework input from the recipe + _measurements
# --------------------------------------------------------------------------- #
def build_rework_input(recipe: dict) -> dict:
    """Assemble exactly what the model needs to JUDGE: the source ingredients each
    paired with its measured conversion (so the model copies, never computes), the
    raw steps, yield, and a little dish context. `_measurements` is one entry per
    recipeIngredient (raw / metric / grams / ml / convertible)."""
    ings = recipe.get("recipeIngredient") or []
    meas = recipe.get("_measurements") or []
    rows = []
    for i, raw in enumerate(ings):
        m = meas[i] if i < len(meas) else {}
        rows.append({
            "source_line": raw if isinstance(raw, str) else str(raw),
            # The measured conversion reference — the model must USE these, not invent.
            "metric": (m or {}).get("metric"),     # e.g. "454 g phyllo…"
            "grams": (m or {}).get("grams"),
            "ml": (m or {}).get("ml"),
            "convertible": (m or {}).get("convertible", True),
        })
    steps = recipe.get("recipeInstructions") or []
    step_texts = []
    for s in steps:
        if isinstance(s, dict):
            step_texts.append(s.get("text") or s.get("name") or "")
        else:
            step_texts.append(str(s))
    master = recipe.get("_master") or {}
    return {
        "name": recipe.get("name") or "",
        "yield": recipe.get("recipeYield"),
        "dish": master.get("dish"),
        "ingredients": rows,
        "steps": step_texts,
    }


# --------------------------------------------------------------------------- #
# The forced-tool schema (mirrors the emittable half of CookMetadata)
# --------------------------------------------------------------------------- #
_AMOUNT = {
    "type": "object",
    "properties": {
        "imperial": {"type": "string"},
        "metric": {"type": "string"},
        "convertible": {"type": "boolean"},
    },
    "required": ["imperial", "metric", "convertible"],
}

_EMIT_COOK_TOOL = {
    "name": "emit_cook",
    "description": "Return the fully reworked, step-anchored recipe.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headnote": {"type": ["string", "null"]},
            "recipe_yield": {"type": ["integer", "null"]},
            "total_time_minutes": {"type": ["integer", "null"]},
            "technique_changes": {
                "type": "array", "items": {"type": "string"},
                "description": "Each change you made to the COOKING, named + why (better result).",
            },
            "ingredients": {
                "type": "array",
                "description": "Purchasable ingredients. Stable snake_case ids (ing_...).",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "shopping_hint": {"type": ["string", "null"],
                                          "description": "Raw count for BUYING (e.g. '3 cloves')."},
                        "aisle": {"type": ["string", "null"]},
                        "to_taste": {"type": "boolean"},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["id", "name", "to_taste"],
                },
            },
            "bundles": {
                "type": "array",
                "description": "Pre-combined mise; include a 'Measured & ready' catch-all so "
                               "EVERY ingredient is pre-measured. State why combined/separate.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "combine_note": {"type": ["string", "null"]},
                        "excluded_reason": {"type": ["string", "null"]},
                        "make_ahead": {"type": "boolean"},
                        "members": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ingredient_id": {"type": "string"},
                                    "amount": _AMOUNT,
                                    "label": {"type": "string"},
                                    "prep_verb": {"type": ["string", "null"]},
                                },
                                "required": ["ingredient_id", "amount", "label"],
                            },
                        },
                    },
                    "required": ["id", "label", "members"],
                },
            },
            "reserved": {
                "type": "array",
                "description": "Put-asides: set aside mid-cook, used later (reserved pasta water…).",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "amount": {"anyOf": [_AMOUNT, {"type": "null"}]},
                        "from_ingredient_id": {"type": ["string", "null"]},
                        "created_step": {"type": "integer"},
                        "consumed_step": {"type": ["integer", "null"]},
                        "note": {"type": ["string", "null"]},
                    },
                    "required": ["id", "label", "created_step"],
                },
            },
            "equipment": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "size_matters": {"type": "boolean"},
                        "size": {"anyOf": [_AMOUNT, {"type": "null"}]},
                        "category": {"type": "string",
                                     "enum": ["bowl", "colander", "pot", "pan", "spoon", "knife",
                                              "board", "tongs", "timer", "serving", "other"]},
                        "why": {"type": ["string", "null"]},
                        "reused_from_step": {"type": ["integer", "null"]},
                    },
                    "required": ["id", "name", "size_matters", "category"],
                },
            },
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "number": {"type": "integer"},
                        "name": {"type": "string"},
                        "instruction": {"type": "string",
                                        "description": "Anchored: {ingN} per this step's ingredients, "
                                                       "{amt:VALUE} for times/temps, {bundle:ID} for a bundle."},
                        "ingredients": {
                            "type": "array",
                            "description": "Ordered to match {ing1},{ing2}… in the instruction.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "ingredient_id": {"type": "string"},
                                    "amount": _AMOUNT,
                                    "label": {"type": "string"},
                                    "definiteness": {"type": "string",
                                                     "enum": ["the", "your", "reserved", "from-step", "bundle"]},
                                    "reused_from_step": {"type": ["integer", "null"]},
                                },
                                "required": ["ingredient_id", "amount", "label", "definiteness"],
                            },
                        },
                        "equipment": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "equipment_id": {"type": "string"},
                                    "reused_from_step": {"type": ["integer", "null"]},
                                },
                                "required": ["equipment_id"],
                            },
                        },
                        "duration_minutes": {"type": ["integer", "null"]},
                        "attention": {"type": "string", "enum": ["active", "passive"]},
                        "depends_on": {"type": "array", "items": {"type": "integer"}},
                        "resource": {"type": ["string", "null"]},
                    },
                    "required": ["number", "name", "instruction", "ingredients", "equipment", "attention"],
                },
            },
            "finish": {"type": ["string", "null"]},
            "cooks_note": {"type": ["string", "null"]},
        },
        "required": ["ingredients", "bundles", "equipment", "steps", "technique_changes"],
    },
}


# --------------------------------------------------------------------------- #
# System prompt (the SKILL phases, condensed) + KB
# --------------------------------------------------------------------------- #
def _system_prompt() -> str:
    return f"""\
You are a master recipe developer + kitchen workflow designer. You turn a captured recipe \
into a STEP-ANCHORED, mise-complete cook plan and return it by calling `emit_cook` exactly \
once. Output nothing else.

THREE BINDING RULES (resolve any ambiguity):
1. No lookback — each step states its own gear + amounts + action; never "see the list above".
2. No measuring mid-cook — EVERY ingredient (including the first into the pan) is pre-measured \
in a bundle. The method only ACTS and refers BACK: "Add the {{ing1}}" with definiteness "the"/\
"reserved"/"from-step"/"bundle" — never introduces a fresh amount.
3. Decisions already made — chosen unit, form, and yield are written into the prose.

WHAT TO DO:
- Cook it in your head (fix the COOKING, not just the copy): add missing technique (reserve \
liquid, finish in sauce, rest, bloom, deglaze, temper), doneness cues, seasoning staging \
(account for salty components added later), safety discards, wrong-order cooking. Put each \
change in `technique_changes` with WHY. Result fidelity matters; source fidelity does not.
- Schedule: give steps duration_minutes, attention (active/passive), depends_on, resource.
- Bundle the mise: pre-combine ingredients added at the same moment into labeled bundles \
(combine_note = why); keep separate when combining early harms (excluded_reason). Put \
everything else under a "Measured & ready" bundle so 100% of measuring is in the mise.
- Put-asides: anything set aside and used later goes in `reserved` (created_step/consumed_step).
- Precision: amount is the PREPARED state ("2 Tbsp minced"); raw count -> shopping_hint. No \
bare informal measures (handful/ladle/drizzle) — a number is required.
- Equipment: size the things where size affects outcome (bowls/pots/pans), inferred from \
quantities; leave size off spoons/tongs/timers; reuse points back via reused_from_step.
- Anchor each step: short imperative name; instruction with {{ingN}} tokens (ingredients in \
order), {{amt:...}} for times/temps, {{bundle:ID}} for a bundle. One action per step. A step's \
ingredient reference may be an ingredient id OR a bundle id (to deploy a pre-combined bundle as \
one unit, with a label + combined amount) — both are in the mise.
- Copy: plain, specific, no clichés. Headnote, a real finish (plate/vessel/temp/"serve at \
once"/what NOT to add), one earned cook's note.

UNITS — DO NOT COMPUTE CONVERSIONS. Each source ingredient is given to you WITH its measured \
metric conversion. Use the provided metric value for the metric face; put the imperial in the \
imperial face. If a measure doesn't convert (counts, "to taste", a pinch), set convertible=false \
and put the same text in both faces. Never mix systems within one face.

Do NOT add cooking tips or doneness checks — a later pass attaches those from our curated \
knowledge base. Focus on the COOKING (technique_changes) and the anchored prose.

You do NOT assign first-appearance order or product names — code handles ordering, and product \
recommendations are resolved from the equipment `category` downstream. Emit only the category.
"""


# --------------------------------------------------------------------------- #
# LLM call
# --------------------------------------------------------------------------- #
def _emit_cook(messages: list, log: Callable) -> Tuple[dict, object]:
    resp = _client.messages.create(
        model=OPUS, max_tokens=_MAX_TOKENS, system=_system_prompt(),
        tools=[_EMIT_COOK_TOOL],
        tool_choice={"type": "tool", "name": "emit_cook"},
        messages=messages,
    )
    for block in resp.content:
        if block.type == "tool_use":
            u = resp.usage
            log(f"[cook-rework] opus: {u.input_tokens} in / {u.output_tokens} out")
            return block.input, resp.usage
    raise RuntimeError("emit_cook tool was not called (tool_choice should have forced it)")


# --------------------------------------------------------------------------- #
# ASSEMBLE (code): dict -> CookMetadata + compute appearance-order first_step
# --------------------------------------------------------------------------- #
def _assemble(emitted: dict) -> CookMetadata:
    cook = CookMetadata(**emitted)  # pydantic validates the shape
    _stamp_first_step(cook)
    return cook


def _assemble_with_repair(emitted: dict, log: Callable) -> CookMetadata:
    """Assemble, and if the emitted object fails SCHEMA validation (the LLM dropped
    or mis-typed a field), do one repair pass feeding the exact pydantic errors
    back. This is distinct from the gauntlet repair (which runs AFTER a successful
    assembly) — a structural error would otherwise crash before the gauntlet."""
    from pydantic import ValidationError
    try:
        return _assemble(emitted)
    except ValidationError as e:
        log(f"[cook-rework] schema validation failed ({e.error_count()} issues) — repair pass…")
        repair_user = (
            "Your emit_cook output failed SCHEMA validation. Fix EXACTLY these "
            "missing/invalid fields and re-emit the FULL corrected object via emit_cook:\n"
            + str(e) + "\n\nYour output was:\n" + json.dumps(emitted, ensure_ascii=False))
        emitted2, _ = _emit_cook([{"role": "user", "content": repair_user}], log)
        return _assemble(emitted2)  # if it still fails, let it raise (caller logs it)


def _stamp_first_step(cook: CookMetadata) -> None:
    """Code owns the appearance-order invariant: derive first_step from the steps
    (the linear spine), then sort the four lists. This makes appearance-order pass
    by construction rather than trusting the model to order things."""
    # ingredient -> first step that references it directly
    ing_first: dict = {}
    eq_first: dict = {}
    for s in sorted(cook.steps, key=lambda x: x.number):
        for si in s.ingredients:
            ing_first.setdefault(si.ingredient_id, s.number)
        for se in s.equipment:
            eq_first.setdefault(se.equipment_id, s.number)
    for ing in cook.ingredients:
        ing.first_step = ing_first.get(ing.id)
    for eq in cook.equipment:
        eq.first_step = eq_first.get(eq.id)
    # A bundle appears at the earlier of: a step that deploys it as a unit (its
    # bundle id used in step.ingredients) OR its first member's first use. Members
    # deployed only via the bundle inherit the bundle's step (so they don't sort
    # last for never being named directly).
    for b in cook.bundles:
        direct = ing_first.get(b.id)
        member_steps = [ing_first[m.ingredient_id] for m in b.members if m.ingredient_id in ing_first]
        cands = [x for x in ([direct] + member_steps) if x is not None]
        b.first_step = min(cands) if cands else None
        if b.first_step is not None:
            for m in b.members:
                mi = cook.ingredient_by_id(m.ingredient_id)
                if mi is not None and mi.first_step is None:
                    mi.first_step = b.first_step

    _BIG = 10_000  # un-referenced items sort last, deterministically
    cook.ingredients.sort(key=lambda x: (x.first_step if x.first_step is not None else _BIG, x.id))
    cook.bundles.sort(key=lambda x: (x.first_step if x.first_step is not None else _BIG, x.id))
    cook.equipment.sort(key=lambda x: (x.first_step if x.first_step is not None else _BIG, x.id))
    cook.reserved.sort(key=lambda x: x.created_step)


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #
def rework_recipe(recipe: dict, log: Callable = print) -> Tuple[CookMetadata, object]:
    """Rework a captured recipe into a validated CookMetadata. Returns (cook, report).
    `cook.validators` carries the gate result; the CALLER decides whether to persist
    (only when report.passed)."""
    name = recipe.get("name") or "(unnamed)"
    log(f"[cook-rework] '{name}': building input from recipe + _measurements")
    inp = build_rework_input(recipe)
    user = ("Rework this recipe. Source ingredients (with measured metric conversions to USE) "
            "and steps:\n\n" + json.dumps(inp, ensure_ascii=False, indent=2))

    emitted, _ = _emit_cook([{"role": "user", "content": user}], log)
    cook = _assemble_with_repair(emitted, log)
    report = run_all(cook)
    log(f"[cook-rework] gauntlet: passed={report.passed}"
        + (f" ({len(report.failures)} failures)" if not report.passed else ""))

    if not report.passed:
        log("[cook-rework] repair pass (opus) on the gate failures…")
        repair_user = (
            "Your reworked recipe FAILED these mechanical gates. Fix ONLY these issues and "
            "re-emit the full corrected recipe via emit_cook. Failures:\n- "
            + "\n- ".join(report.failures)
            + "\n\nThe recipe you produced:\n" + json.dumps(emitted, ensure_ascii=False))
        emitted2, _ = _emit_cook([{"role": "user", "content": repair_user}], log)
        cook = _assemble(emitted2)
        report = run_all(cook)
        log(f"[cook-rework] after repair: passed={report.passed}"
            + (f" ({len(report.failures)} failures)" if not report.passed else ""))

    # Augment ONLY a structurally-sound cook (no point annotating a failed rework).
    # Additive + best-effort: a failure here never sinks the rework. Provenance —
    # every attached tip/check traces to a published KB entry by kb_id (validated).
    if report.passed:
        try:
            augment_cook(cook, log, recipe_name=name)
        except Exception as e:  # noqa: BLE001 — augment is non-fatal
            log(f"[cook-rework] augment pass failed (non-fatal): {e}")

    cook.validators = report
    cook.schema_version = 1
    return cook, report
