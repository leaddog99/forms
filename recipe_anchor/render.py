"""
render.py — RecipeDoc -> HTML (matching the prototype page).

The instruction strings carry {ingN} and {amt:..} tokens; we expand those into the
inline <span class="ing">/<span class="amt u"> markup the stylesheet expects, with
data-imp/data-met so the client-side units toggle works. Everything else is Jinja.
"""
from __future__ import annotations

import re
from html import escape
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import RecipeDoc, Step
from .products import picks_for

_TPL_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TPL_DIR),
    autoescape=select_autoescape(["html", "j2"]),
)

_ING_RE = re.compile(r"\{ing(\d+)\}")
_AMT_RE = re.compile(r"\{amt:([^}]*)\}")


def _amount_span(imperial: str, metric: str, convertible: bool) -> str:
    if convertible and imperial != metric:
        return (f'<span class="amt u" data-imp="{escape(imperial)}" '
                f'data-met="{escape(metric)}">{escape(imperial)}</span>')
    return f'<span class="amt">{escape(imperial)}</span>'


def expand_instruction(step: Step) -> str:
    """Replace {ingN} and {amt:..} tokens with inline markup. Marked safe in template."""
    def ing_sub(m: re.Match) -> str:
        idx = int(m.group(1))
        if idx >= len(step.ingredients):
            return ""
        si = step.ingredients[idx]
        label = escape(si.label)
        if si.reused_from_step:                      # reused -> bold noun, no fresh amount
            return f'<span class="ing">{label}</span>'
        amt = _amount_span(si.amount.imperial, si.amount.metric, si.amount.convertible)
        return f'<span class="ing">{amt} {label}</span>'

    def amt_sub(m: re.Match) -> str:
        raw = m.group(1)
        if "|" in raw:
            imp, met = (p.strip() for p in raw.split("|", 1))
            inner = _amount_span(imp, met, True)
        else:
            inner = f'<span class="amt">{escape(raw)}</span>'
        return f'<span class="ing">{inner}</span>'

    text = escape(step.instruction)
    # tokens were escaped ({ -> { stays, but braces are not escaped by html.escape),
    # so run the regexes on the escaped text directly.
    text = _ING_RE.sub(ing_sub, text)
    text = _AMT_RE.sub(amt_sub, text)
    return text


def render_html(doc: RecipeDoc) -> str:
    # Build per-step view models
    steps_vm = []
    for i, s in enumerate(doc.steps):
        equip_vm = []
        for se in s.equipment:
            eq = doc.equipment_by_id(se.equipment_id)
            if eq:
                equip_vm.append({"eq": eq, "reused_from": se.reused_from_step})
        steps_vm.append({
            "step": s,
            "instruction_html": expand_instruction(s),
            "equipment": equip_vm,
            "is_last": i == len(doc.steps) - 1,
        })

    equipment_vm = [{"eq": e, "products": picks_for(e.category)} for e in doc.equipment]

    tpl = _env.get_template("recipe.html.j2")
    return tpl.render(doc=doc, steps=steps_vm, equipment=equipment_vm)
