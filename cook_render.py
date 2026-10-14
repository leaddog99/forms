"""cook_render.py — render a `_cook` plan into the exact text the cook SEES and HEARS.

The cook view (forms/cook.html) expands `{ingN}` into AMOUNT + " " + LABEL at render
time. Every quality check that reads the stored tokens instead of that expansion is
blind to the seam between the two — which is where the 2026-10-13 "duplicate words"
came from: amount "¾ lb cooked" + label "Cooked penne" → "¾ lb cooked Cooked penne";
amount "2½ cups onion + 3 cloves garlic" + label "Onion & garlic" → the ingredient
named twice; amount "to taste" + label "Kosher salt" → "Season with to taste Kosher
salt". 51 of the 58 stored plans had at least one such seam. The polish pass and
the gauntlet never saw them because neither renders.

This module is the ONE Python mirror of the page's expansion, used by:
  • cook_audit  — the auditor judges the RENDERED plan, not the tokens;
  • scripts     — corpus scans of what cooks actually read.

It also owns the SEAM SAFETY NET — `ing_phrase` — three deterministic rules that
make the obvious cases read as English regardless of what the model emitted. The
page carries the SAME rules in `ingPhrase()` (keep the two in lockstep; the audit
script `scripts/audit_cooks.py --render` prints what this side produces so a drift
is visible). The safety net is a floor, not the fix: the rework prompt (v2.4) now
states the render contract, and the auditor repairs what the net can't.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from cook_polish import strip_leading_article, tidy

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
# A qualifier "amount": there is no measure, the label IS the instruction.
_QUALIFIER = re.compile(r"^(to taste|as needed|as required|optional|if needed|for serving|for garnish)(?:[,\s]+(.+))?$", re.I)
# An informal measure that wants "of": "a pinch", "a handful", "a generous handful",
# "a splash", "some". No digit anywhere, starts with a/an/some.
_INFORMAL = re.compile(r"^(a|an|some)\b[^0-9]*$", re.I)


def _get(o: Any, k: str, default=None):
    """Attribute-or-key access so the renderer takes pydantic models AND plain dicts."""
    if o is None:
        return default
    if isinstance(o, dict):
        return o.get(k, default)
    return getattr(o, k, default)


def amt_face(a: Any, units: str = "imp") -> str:
    """Mirror of cook.html amtStr(): the chosen face, falling back to the other."""
    if not a:
        return ""
    if isinstance(a, str):
        return a
    imp, met = _get(a, "imperial") or "", _get(a, "metric") or ""
    return (met or imp) if units == "met" else (imp or met)


def _words(s: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(s or "")]


def ing_phrase(amount: str, label: str) -> str:
    """AMOUNT + LABEL as one readable phrase — the seam safety net (mirrored in
    cook.html ingPhrase()). Rules, in order:
      1. label is a bare noun: a leading the/a/an/your is dropped;
      2. a qualifier amount (to taste / as needed) goes AFTER the label in parens:
         "Kosher salt (to taste)" — never "to taste Kosher salt";
      3. an informal measure (a pinch / a handful / some) takes "of":
         "a pinch of black pepper";
      4. every word of the label already in the amount → the amount alone
         ("2½ cups onion + 3 cloves garlic" + "Onion & garlic");
      5. the amount's trailing words that the label repeats at its start are not
         said twice ("¾ lb cooked" + "Cooked penne" → "¾ lb cooked penne";
         "1 tsp minced" + "Minced garlic" → "1 tsp minced garlic").
    Never changes an amount's numbers; never invents words beyond "of"."""
    a = (amount or "").strip()
    lab = strip_leading_article((label or "").strip())
    if not a:
        return lab
    if not lab:
        return a
    qm = _QUALIFIER.match(a)
    if qm:
        # "to taste, grated" + "Parmigiano" -> "grated Parmigiano (to taste)"
        rest = (qm.group(2) or "").strip()
        return f"{(rest + ' ' + lab).strip()} ({qm.group(1).lower()})"
    aw, lw = _words(a), _words(lab)
    if lw and set(lw) <= set(aw):
        return a
    # overlap: longest k with aw[-k:] == lw[:k]
    k = 0
    for n in range(min(len(aw), len(lw)), 0, -1):
        if aw[-n:] == lw[:n]:
            k = n
            break
    if k:
        # the overlap belongs to the label (it carries the form): cut those words
        # off the AMOUNT's tail — "a generous handful, grated" + "grated cheese" →
        # "a generous handful" + "grated cheese" (and then "of" below).
        m = list(_WORD.finditer(a))
        a = a[:m[-k].start()].rstrip(" ,;:-–—")
        if not a:
            return lab
    if _INFORMAL.match(a) and not re.search(r"\bof$", a, re.I):
        return f"{a} of {lab}"
    return f"{a} {lab}"


def _step_ings(step: Any) -> list:
    return list(_get(step, "ingredients") or [])


def expand(step: Any, text: Optional[str], units: str = "imp", bundles: Optional[dict] = None) -> str:
    """Plain-text expansion of {ingN}/{amt:..}/{bundle:..} over `text` in the
    step's token space (cook.html expandInstr, minus the <b> markup)."""
    t = tidy(text or "")
    ings = _step_ings(step)

    def _ing(m):
        n = int(m.group(1))
        if n < 1 or n > len(ings):
            return m.group(0)          # dangling token — left visible on purpose
        si = ings[n - 1]
        return ing_phrase(amt_face(_get(si, "amount"), units), _get(si, "label") or "")

    t = re.sub(r"\{ing(\d+)\}", _ing, t)
    t = re.sub(r"\{amt:([^}]*)\}",
               lambda m: ((m.group(1).split("|") + [""])[1] if units == "met" and "|" in m.group(1)
                          else m.group(1).split("|")[0]).strip(), t)

    def _bundle(m):
        bid = m.group(1).strip()
        b = (bundles or {}).get(bid)
        return (_get(b, "label") or bid) if b else bid

    t = re.sub(r"\{bundle:([^}]*)\}", _bundle, t)
    return tidy(_article_clash(t))


_ARTICLE_CLASH = re.compile(r"\b(the|your)\s+(a|an|some)\s+", re.I)


def _article_clash(t: str) -> str:
    """"Stir in the {ing2}" + an informal amount ("a generous handful") renders
    "the a generous handful" — the prose's article meets the amount's. Keep the
    amount's (mirrored in cook.html expandInstr/spokenFragment)."""
    return _ARTICLE_CLASH.sub(r"\2 ", t)


def missing_ing_chips(step: Any, ss: Any, frag: str, units: str = "imp") -> str:
    """Mirror of cook.html missingIngChips(): the amounts a sub-step covers but its
    fragment never names, appended as " — amt label, …"."""
    shown = {int(n) for n in re.findall(r"\{ing(\d+)\}", frag or "")}
    ings = _step_ings(step)
    chips = []
    for n in (_get(ss, "ingredients") or []):
        if n in shown or n < 1 or n > len(ings):
            continue
        si = ings[n - 1]
        chips.append(ing_phrase(amt_face(_get(si, "amount"), units), _get(si, "label") or ""))
    return (" — " + ", ".join(chips)) if chips else ""


def step_lines(step: Any, units: str = "imp", bundles: Optional[dict] = None) -> List[Dict[str, str]]:
    """What the step card shows, line by line: with sub-steps the card shows ONLY
    the sub-step lines (screen fragment, else the voice line, plus chips); without,
    the whole instruction. Each line also carries what the voice says for it."""
    out = []
    subs = list(_get(step, "substeps") or [])
    if subs:
        for j, ss in enumerate(subs, 1):
            frag = (_get(ss, "screen") or "").strip()
            voice = (_get(ss, "voice") or "").strip()
            screen = (expand(step, frag, units, bundles) if frag else voice) \
                + missing_ing_chips(step, ss, frag if frag else "", units)
            out.append({"where": f"step {_get(step, 'number')} sub-step {j}",
                        "screen": screen, "voice": voice})
    else:
        out.append({"where": f"step {_get(step, 'number')}",
                    "screen": expand(step, _get(step, "instruction"), units, bundles),
                    "voice": expand(step, _get(step, "instruction"), units, bundles)})
    return out


def render_plan_text(cook: Any, units: str = "imp") -> str:
    """The whole plan as the cook meets it, top to bottom — the document the
    auditor reads. Headnote, mise, steps (screen + voice), attachments, finish,
    tips, cook's note, declared changes."""
    bundles = {_get(b, "id"): b for b in (_get(cook, "bundles") or [])}
    L: List[str] = []
    if _get(cook, "headnote"):
        L += ["HEADNOTE:", f"  {_get(cook, 'headnote')}", ""]
    L.append("MISE (bundles as listed on screen):")
    for b in _get(cook, "bundles") or []:
        # the mise row shows amount and label as separate columns (ingRowEl): no seam
        mem = "; ".join(f"{_get(m, 'label')} — {amt_face(_get(m, 'amount'), units)}"
                        + (f" [{_get(m, 'prep_verb')}]" if _get(m, "prep_verb") else "")
                        for m in (_get(b, "members") or []))
        L.append(f"  • {_get(b, 'label')}: {mem}")
    L.append("")
    L.append("STEPS (each line exactly as the card shows it; «voice» = what is spoken):")
    for s in sorted(_get(cook, "steps") or [], key=lambda x: _get(x, "number") or 0):
        L.append(f"  {_get(s, 'number')}. {_get(s, 'name')}")
        for ln in step_lines(s, units, bundles):
            if ln["screen"] == ln["voice"]:
                L.append(f"     {ln['screen']}")
            else:
                L.append(f"     {ln['screen']}")
                L.append(f"        «voice» {ln['voice']}")
        for a in _get(s, "attachments") or []:
            L.append(f"     [{_get(a, 'kind')} · {_get(a, 'kb_id')}] {_get(a, 'text')}")
    L.append("")
    if _get(cook, "finish"):
        L += ["FINISH:", f"  {_get(cook, 'finish')}", ""]
    if _get(cook, "tips"):
        L.append("TIPS (recipe-level):")
        for a in _get(cook, "tips"):
            L.append(f"  [{_get(a, 'kind')} · {_get(a, 'kb_id')}] {_get(a, 'text')}")
        L.append("")
    if _get(cook, "cooks_note"):
        L += ["COOK'S NOTE:", f"  {_get(cook, 'cooks_note')}", ""]
    if _get(cook, "technique_changes"):
        L.append("DECLARED CHANGES TO THE COOKING:")
        for t in _get(cook, "technique_changes"):
            L.append(f"  - {t}")
    return "\n".join(L)
