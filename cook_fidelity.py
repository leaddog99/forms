"""cook_fidelity.py — the source-fidelity gate for cook reworks.

The mechanical gauntlet (cook_validators) checks STRUCTURE — units, mise coverage,
ordering. It cannot see MEANING: the John's Pesto rework passed every gate while
silently turning "add the Pecorino in chunks and pound" into "finely grate the
Pecorino" — a technique substitution that changes the mouthfeel the recipe exists
for, and one the prompt required be declared in `technique_changes` (it wasn't).

This gate is the meaning check: ONE cheap LLM comparison of the source method
against the reworked plan, flagging any technique / tool / ingredient-form the
source explicitly states that the plan contradicts WITHOUT declaring the change in
`technique_changes`. Additions where the source is silent, copy edits, splitting/
merging steps, and mise re-ordering are NOT violations — only substitutions of a
stated way of doing something.

Called from cook_rework.rework_recipe AFTER the structural gauntlet passes; a
violation triggers the same repair-pass machinery, and an unrepaired violation
fails the rework (persisting a plausible-but-unfaithful cook is worse than no
cook — Ask Chef grounds on `_cook` and will confidently defend whatever it says).
"""
from __future__ import annotations

from typing import Callable, List, Optional

import cook_costs
import llm  # central LLM gateway — auto-journals usage to bcc_token_journal
from cook_model import CookMetadata

MODEL = "claude-sonnet-4-6"   # judgment-light diff of two short texts
_MAX_TOKENS = 1500
FIDELITY_PROMPT_VERSION = "cook-fidelity-v1-2026-09-03"

_REPORT_TOOL = {
    "name": "report_fidelity",
    "description": "Report every source-fidelity violation found (empty list = faithful).",
    "input_schema": {
        "type": "object",
        "properties": {
            "violations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_says": {"type": "string",
                                        "description": "The source's stated technique/tool/form, quoted or closely paraphrased."},
                        "plan_says": {"type": "string",
                                      "description": "What the reworked plan does instead (name the step/bundle)."},
                        "why_it_matters": {"type": "string",
                                           "description": "One line: what the substitution changes about the result."},
                    },
                    "required": ["source_says", "plan_says", "why_it_matters"],
                },
            },
        },
        "required": ["violations"],
    },
}

_SYSTEM = """\
You are a recipe fidelity auditor. You are given a SOURCE recipe (the author's own
ingredients and method) and a REWORKED cook plan derived from it. The plan is ALLOWED
to: restructure steps, pre-measure everything into mise bundles, add technique where
the source is silent, fix objectively wrong order or unsafe handling, and rewrite the
prose. Every deliberate change to the cooking is supposed to be declared in the plan's
`technique_changes` list.

Your ONE job: find places where the plan CONTRADICTS a technique, tool, motion, or
ingredient FORM that the source explicitly states, WITHOUT that exact change being
declared in `technique_changes`. Ingredient form at the moment of use counts fully:
whole vs chunks vs grated vs sliced vs pounded vs torn is technique, not copy.
Examples of violations: source pounds cheese in as chunks in a mortar, plan grates it
ahead; source whisks by hand, plan uses a food processor; source folds, plan stirs.
NOT violations: the plan adds a doneness cue the source lacks; the plan pre-measures
into bundles; the plan splits or merges steps; the plan tightens wording; a change
that IS declared in technique_changes (even loosely — judge intent, not word match).

Report via report_fidelity exactly once. Be precise and quote the source. An empty
violations list is the correct answer for a faithful plan — do not invent nitpicks.\
"""


def _plan_text(cook: CookMetadata) -> str:
    """Serialize the parts of the plan a fidelity judgment needs: what each step
    does (with its ingredient labels, where prep/form lives), what the mise
    pre-prepares (prep_verb/label carry the form), and the declared changes."""
    lines: List[str] = ["DECLARED technique_changes:"]
    for t in (cook.technique_changes or []):
        lines.append(f"  - {t}")
    if not cook.technique_changes:
        lines.append("  (none declared)")
    lines.append("\nMISE BUNDLES (prepped before cooking starts):")
    for b in cook.bundles:
        members = "; ".join(
            f"{m.label}" + (f" [prep: {m.prep_verb}]" if m.prep_verb else "")
            for m in b.members)
        lines.append(f"  - {b.label}: {members}")
    lines.append("\nINGREDIENT NOTES:")
    for i in cook.ingredients:
        if i.note:
            lines.append(f"  - {i.name}: {i.note}")
    lines.append("\nSTEPS:")
    for s in sorted(cook.steps, key=lambda x: x.number):
        ings = ", ".join(f"{{ing{k}}}={si.label}" for k, si in enumerate(s.ingredients, 1))
        lines.append(f"  {s.number}. {s.name}: {s.instruction}" + (f"  ({ings})" if ings else ""))
    return "\n".join(lines)


def check_fidelity(rework_input: dict, cook: CookMetadata, log: Callable,
                   usages: Optional[list] = None) -> List[str]:
    """Compare the source (the same dict handed to the rework model) against the
    assembled plan. Returns human-readable failure strings (empty = faithful).
    Best-effort on the CALL only in the sense that a hard API failure raises —
    the caller decides; we never swallow a violation."""
    src_lines = ["SOURCE INGREDIENTS:"]
    for r in (rework_input.get("ingredients") or []):
        src_lines.append(f"  - {r.get('source_line')}")
    src_lines.append("\nSOURCE METHOD:")
    for i, s in enumerate(rework_input.get("steps") or [], 1):
        src_lines.append(f"  {i}. {s}")
    user = (
        "SOURCE RECIPE:\n" + "\n".join(src_lines)
        + "\n\n---\n\nREWORKED PLAN:\n" + _plan_text(cook)
        + "\n\nAudit the plan against the source and call report_fidelity."
    )
    resp = llm.create(
        operation="cook_fidelity", model=MODEL, max_tokens=_MAX_TOKENS,
        system=_SYSTEM,
        tools=[_REPORT_TOOL],
        tool_choice={"type": "tool", "name": "report_fidelity"},
        messages=[{"role": "user", "content": user}],
    )
    if usages is not None:
        cook_costs.record(usages, MODEL, resp.usage)
    log(f"[cook-fidelity] {MODEL.split('-')[1]}: {resp.usage.input_tokens} in / "
        f"{resp.usage.output_tokens} out")
    violations = []
    for block in resp.content:
        if block.type == "tool_use":
            violations = (block.input or {}).get("violations") or []
            break
    return [
        f"source says {v.get('source_says')!r} but the plan does {v.get('plan_says')!r} "
        f"and technique_changes does not declare it ({v.get('why_it_matters')}) — "
        f"restore the source's technique/form, or (only if objectively better for the "
        f"result) keep it AND declare it in technique_changes"
        for v in violations
        if isinstance(v, dict)
    ]
