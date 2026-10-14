"""cook_audit.py — the auditor: reads the plan AS THE COOK WILL SEE IT and fixes it.

Why this exists (2026-10-13, the vodka-sauce cook): the step cards read
"Add the 2½ cups onion + 3 cloves garlic Onion & garlic", "Purée with the 2 Tbsp
Fresh basil, for the purée until smooth", "a generous handful, grated Grated
cheese". The rework passed every gate: the gauntlet checks structure, the fidelity
gate checks meaning, polish cleans each FIELD — and none of them ever RENDERED the
plan. The duplicate words live at the seam between `amount` and `label`, which
only exists once `{ingN}` expands. 51 of 58 stored plans had the defect.

The auditor is the editor's read. Two halves:

  1. MECHANICAL ($0, cook_render + regexes): render every line the card shows and
     the voice says, then flag what still reads wrong — a doubled word, an amount
     that names the ingredient its label names again, a prep word said twice, a
     label carrying a clause ("basil, for the purée"), a sentence-case label glued
     mid-sentence. Each finding quotes the RENDERED line.
  2. JUDGMENT (Opus): the rendered plan + the mechanical findings + a map of the
     editable fields → `report_audit`: a grade, findings (blocker | polish), and
     TARGETED EDITS (path → new value). Code applies the edits, re-polishes,
     re-renders, re-checks; one more round if blockers remain. No whole-plan
     re-emit: a re-emit costs ~16K output tokens and can introduce fresh defects;
     an edit touches one field.

What it may change: prose fields only (instruction, name, voice/screen, labels,
amount faces, headnote/finish/cook's note, tips/attachment text, technique_changes;
it may drop a KB attachment that contradicts the plan). It may NOT add/remove
ingredients, steps or sub-steps, re-number anything, or change a quantity's value
— the gauntlet re-runs after the edits so a structural break fails loudly.

Residual BLOCKERS after the last round fail the rework (same stance as the
fidelity gate: a sloppy plan Chef then defends out loud is worse than no plan).
Polish-grade residue is recorded in `cook.audit` and logged, never fatal.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

import cook_costs
import llm  # central LLM gateway — auto-journals usage to bcc_token_journal
from cook_model import CookMetadata
from cook_polish import polish_cook
from cook_render import amt_face, ing_phrase, render_plan_text, step_lines

MODEL = "claude-opus-4-8"       # the quality bar is the point; judgment, not realization
_MAX_TOKENS = 6000
AUDIT_VERSION = "cook-audit-v1-2026-10-13"
MAX_ROUNDS = 2

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_DUP = re.compile(r"\b(\w+)\s+\1\b", re.I)
_PREP = re.compile(r"\b(grated|minced|chopped|cooked|sliced|diced|crushed|shredded|torn|cubed|"
                   r"peeled|melted|softened|beaten|toasted|drained|rinsed|halved|quartered|zested|"
                   r"juiced|crumbled|ground|mashed|whisked|cooled|warmed|thawed)\b", re.I)
_UNITS = {
    "cup", "cups", "tbsp", "tsp", "tablespoon", "tablespoons", "teaspoon", "teaspoons", "oz",
    "ounce", "ounces", "lb", "lbs", "pound", "pounds", "g", "kg", "ml", "l", "clove", "cloves",
    "can", "cans", "pinch", "handful", "to", "taste", "a", "an", "of", "plus", "and", "or",
    "about", "generous", "small", "large", "medium", "each", "whole", "piece", "pieces", "bunch",
    "sprig", "sprigs", "slice", "slices", "inch", "in", "the", "for", "more", "extra", "as",
    "needed", "divided", "two", "three", "one", "half", "quarter", "heaping", "level", "scant",
    "roughly", "finely", "thinly", "coarsely", "packed", "stick", "sticks", "sheet", "sheets",
    "cooked", "grated", "minced", "chopped", "sliced", "diced", "approx", "approximately",
}
# Proper nouns that legitimately keep a capital mid-sentence (a label starting with one
# of these is NOT a sentence-case slip).
_PROPER = {"parmigiano", "parmesan", "pecorino", "san", "dijon", "worcestershire", "tabasco",
           "kosher", "spanish", "italian", "french", "greek", "thai", "chinese", "japanese",
           "mexican", "indian", "maldon", "diamond", "morton", "old", "cajun", "creole",
           "gruyère", "gruyere", "cheddar", "swiss", "brussels", "yukon", "roma", "romano",
           "marsala", "madeira", "calabrian", "aleppo", "sichuan", "szechuan", "meyer", "key",
           "granny", "honeycrisp", "fuji", "gala", "tuscan", "neapolitan", "sicilian", "genovese",
           "basmati", "arborio", "carnaroli", "jasmine", "fresno", "anaheim", "serrano", "hatch",
           "bourbon", "cognac", "champagne", "prosecco", "sherry", "port", "chianti", "pinot"}


def _words(s: str) -> List[str]:
    return [w.lower() for w in _WORD.findall(s or "")]


# --------------------------------------------------------------------------- #
# 1. MECHANICAL findings over the RENDERED plan
# --------------------------------------------------------------------------- #
def mechanical_findings(cook: CookMetadata, units: str = "imp") -> List[Dict[str, str]]:
    """Every finding quotes what the cook will read. `severity` is 'blocker' for
    what reads as broken English or hides/duplicates an amount, 'polish' for a
    sentence-case slip or a clause the label shouldn't carry."""
    out: List[Dict[str, str]] = []
    bundles = {b.id: b for b in cook.bundles}

    def add(sev, where, rendered, problem, hint):
        out.append({"severity": sev, "where": where, "rendered": rendered[:200],
                    "problem": problem, "hint": hint})

    for s in sorted(cook.steps, key=lambda x: x.number):
        for k, si in enumerate(s.ingredients, 1):
            a = amt_face(si.amount, units)
            lab = si.label or ""
            path = f"steps[{s.number}].ingredients[{k}]"
            aw = set(_words(a)) - _UNITS
            lw = set(_words(lab))
            # A bundle deployed as one unit is SUPPOSED to list its members in the
            # amount ("2½ cups onion + 3 cloves garlic"); the card then shows the
            # amount alone (label words all inside it). Only a PARTIAL overlap is a seam.
            if aw & lw and not (si.ingredient_id in bundles and lw <= set(_words(a))):
                add("blocker", path, ing_phrase(a, lab),
                    f"the amount {a!r} names the ingredient that the label {lab!r} names again",
                    "amount = the measure only (e.g. '2 Tbsp'); label = the bare noun phrase. "
                    "For a bundle deployed as one unit, amount lists each member with its "
                    "measure and label = the bundle's label (the card then shows the amount).")
            pa, pl = {x.lower() for x in _PREP.findall(a)}, {x.lower() for x in _PREP.findall(lab)}
            if pa & pl:
                add("blocker", path, ing_phrase(a, lab),
                    f"prep word {sorted(pa & pl)} appears in both the amount {a!r} and the label {lab!r}",
                    "keep the prep word in ONE place — in the label ('minced garlic') with the "
                    "amount a pure measure ('1 tsp').")
            if re.search(r",\s*(for|to|in|if|as)\b|\(", lab):
                add("polish", path, ing_phrase(a, lab),
                    f"label {lab!r} carries a clause/parenthetical — it renders mid-sentence",
                    "label = a bare noun phrase; put the purpose/condition in the instruction "
                    "or screen fragment instead.")
            # (Sentence-case labels mid-line — "¼ cup Olive oil" — were reported as polish
            # at first; Opus left all 13 alone on the vodka sauce and the list only padded
            # the report. The v2.4 prompt asks for lowercase labels on NEW reworks; the
            # auditor no longer nags about it.)
        for j, ss in enumerate(s.substeps, 1):
            if ss.ingredients and not (ss.screen or "").strip():
                add("blocker", f"steps[{s.number}].substeps[{j}].screen",
                    (ss.voice or ""),
                    f"sub-step deploys ingredient(s) {list(ss.ingredients)} but has no screen fragment "
                    f"(the card falls back to the terse voice line plus bolted-on amounts)",
                    "write the screen fragment with the step's {ingN} tokens, e.g. 'Stir in the {ing1}; "
                    "cook {amt:1 min} until fragrant'.")
        for ln in step_lines(s, units, bundles):
            for text, face in ((ln["screen"], "screen"), (ln["voice"], "voice")):
                m = _DUP.search(text)
                if m and not re.fullmatch(r"\d+(\s+\d+)+", m.group(0)):
                    add("blocker", ln["where"] + f" ({face})", text,
                        f"doubled word {m.group(0)!r} as rendered", "reword so no word repeats.")
            if re.search(r"\{ing\d+\}|\{amt:|\{bundle:", ln["screen"]):
                add("blocker", ln["where"], ln["screen"],
                    "an unexpanded token is visible — it points at nothing",
                    "fix the token index or remove it.")
    for b in cook.bundles:
        for j, m in enumerate(b.members, 1):
            a = amt_face(m.amount, units)
            aw = set(_words(a)) - _UNITS
            if aw & set(_words(m.label or "")):
                add("polish", f"bundles[{b.id}].members[{j}]", ing_phrase(a, m.label or ""),
                    f"bundle member amount {a!r} repeats the label {m.label!r}",
                    "amount = measure only.")
    return out


# --------------------------------------------------------------------------- #
# 2. The editable field map (what the auditor may change, by exact path)
# --------------------------------------------------------------------------- #
def editable_map(cook: CookMetadata) -> Dict[str, Any]:
    """Every field the auditor may edit, keyed by the exact path it must echo
    back. Step/sub-step/ingredient indices are the 1-based NUMBERS the cook sees
    (step.number, sub-step position, {ingN} index) — never list positions."""
    m: Dict[str, Any] = {}
    for key in ("headnote", "finish", "cooks_note"):
        m[key] = getattr(cook, key)
    for i, t in enumerate(cook.technique_changes, 1):
        m[f"technique_changes[{i}]"] = t
    for i, a in enumerate(cook.tips, 1):
        m[f"tips[{i}].text"] = a.text
    for b in cook.bundles:
        m[f"bundles[{b.id}].label"] = b.label
        for j, mem in enumerate(b.members, 1):
            m[f"bundles[{b.id}].members[{j}].label"] = mem.label
            m[f"bundles[{b.id}].members[{j}].amount.imperial"] = mem.amount.imperial
            m[f"bundles[{b.id}].members[{j}].amount.metric"] = mem.amount.metric
    for s in cook.steps:
        p = f"steps[{s.number}]"
        m[f"{p}.name"] = s.name
        m[f"{p}.instruction"] = s.instruction
        for k, si in enumerate(s.ingredients, 1):
            m[f"{p}.ingredients[{k}].label"] = si.label
            m[f"{p}.ingredients[{k}].amount.imperial"] = si.amount.imperial
            m[f"{p}.ingredients[{k}].amount.metric"] = si.amount.metric
        for j, ss in enumerate(s.substeps, 1):
            m[f"{p}.substeps[{j}].voice"] = ss.voice
            m[f"{p}.substeps[{j}].screen"] = ss.screen
        for j, a in enumerate(s.attachments, 1):
            m[f"{p}.attachments[{j}].text"] = a.text
    return m


_PATH = re.compile(
    r"^(?:(headnote|finish|cooks_note)"
    r"|technique_changes\[(\d+)\]"
    r"|tips\[(\d+)\]\.text"
    r"|bundles\[([A-Za-z0-9_\-]+)\]\.(label|members\[(\d+)\]\.(label|amount\.imperial|amount\.metric))"
    r"|steps\[(\d+)\]\.(name|instruction"
    r"|ingredients\[(\d+)\]\.(label|amount\.imperial|amount\.metric)"
    r"|substeps\[(\d+)\]\.(voice|screen)"
    r"|attachments\[(\d+)\]\.text))$")


def apply_edit(cook: CookMetadata, path: str, value: Any) -> Optional[str]:
    """Apply ONE edit. Returns None on success or a reason string on refusal.
    Values must be strings (or null for a sub-step screen). Token references in
    an instruction/screen must stay inside the step's ingredient range."""
    m = _PATH.match(path or "")
    if not m:
        return f"path {path!r} is not editable"
    if value is not None and not isinstance(value, str):
        return f"{path}: value must be a string"
    g = m.groups()
    try:
        if g[0]:
            setattr(cook, g[0], value)
            return None
        if g[1]:
            cook.technique_changes[int(g[1]) - 1] = value or ""
            return None
        if g[2]:
            cook.tips[int(g[2]) - 1].text = value or ""
            return None
        if g[3]:
            b = next((x for x in cook.bundles if x.id == g[3]), None)
            if b is None:
                return f"{path}: no bundle {g[3]!r}"
            if g[4] == "label":
                b.label = value or b.label
                return None
            mem = b.members[int(g[5]) - 1]
            if g[6] == "label":
                mem.label = value or mem.label
            elif g[6] == "amount.imperial":
                mem.amount.imperial = value or mem.amount.imperial
            else:
                mem.amount.metric = value or mem.amount.metric
            return None
        s = next((x for x in cook.steps if x.number == int(g[7])), None)
        if s is None:
            return f"{path}: no step {g[7]}"
        field = g[8]
        if field in ("name", "instruction"):
            if field == "instruction":
                bad = [int(n) for n in re.findall(r"\{ing(\d+)\}", value or "")
                       if int(n) < 1 or int(n) > len(s.ingredients)]
                if bad:
                    return f"{path}: token index {bad} out of range (step has {len(s.ingredients)})"
            setattr(s, field, value or getattr(s, field))
            return None
        if field.startswith("ingredients["):
            si = s.ingredients[int(g[9]) - 1]
            if g[10] == "label":
                si.label = value or si.label
            elif g[10] == "amount.imperial":
                si.amount.imperial = value or si.amount.imperial
            else:
                si.amount.metric = value or si.amount.metric
            return None
        if field.startswith("substeps["):
            ss = s.substeps[int(g[11]) - 1]
            if g[12] == "voice":
                if not (value or "").strip():
                    return f"{path}: voice may not be emptied"
                ss.voice = value
            else:
                bad = [int(n) for n in re.findall(r"\{ing(\d+)\}", value or "")
                       if int(n) < 1 or int(n) > len(s.ingredients)]
                if bad:
                    return f"{path}: token index {bad} out of range"
                if ss.ingredients and not (value or "").strip():
                    return f"{path}: a sub-step that deploys ingredients keeps a screen fragment"
                ss.screen = value
            return None
        if field.startswith("attachments["):
            s.attachments[int(g[13]) - 1].text = value or ""
            return None
    except (IndexError, ValueError) as e:
        return f"{path}: {e}"
    return f"{path}: unhandled"


def remove_attachment(cook: CookMetadata, step: Optional[int], kb_id: str) -> bool:
    """Drop one KB attachment (step-level when `step` is given, else recipe tip)."""
    if step is None:
        n = len(cook.tips)
        cook.tips = [a for a in cook.tips if a.kb_id != kb_id]
        return len(cook.tips) != n
    s = next((x for x in cook.steps if x.number == step), None)
    if s is None:
        return False
    n = len(s.attachments)
    s.attachments = [a for a in s.attachments if a.kb_id != kb_id]
    return len(s.attachments) != n


# --------------------------------------------------------------------------- #
# 3. The judgment pass
# --------------------------------------------------------------------------- #
_REPORT_TOOL = {
    "name": "report_audit",
    "description": "The audit verdict: a grade, every finding, and the targeted edits that fix them.",
    "input_schema": {
        "type": "object",
        "properties": {
            "grade": {"type": "integer", "minimum": 1, "maximum": 5,
                      "description": "1 = unusable at the stove, 5 = publishable as-is (BEFORE your edits)."},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "where": {"type": "string", "description": "Which line/field (quote the path from the editable map when one applies)."},
                        "severity": {"type": "string", "enum": ["blocker", "polish"]},
                        "problem": {"type": "string", "description": "One line: what reads wrong, quoting the rendered words."},
                    },
                    "required": ["where", "severity", "problem"],
                },
            },
            "edits": {
                "type": "array",
                "description": "Targeted field edits. `path` must be copied EXACTLY from the editable map.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "value": {"type": ["string", "null"]},
                        "why": {"type": "string"},
                    },
                    "required": ["path", "value"],
                },
            },
            "remove_attachments": {
                "type": "array",
                "description": "KB attachments to drop because they contradict the plan or repeat it.",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": ["integer", "null"], "description": "Step number, or null for a recipe-level tip."},
                        "kb_id": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["kb_id"],
                },
            },
        },
        "required": ["grade", "findings", "edits"],
    },
}

_SYSTEM = """\
You are the EDITOR of a hands-free cooking plan. You are shown the plan exactly as the
cook will meet it at the stove — every step card line as rendered on screen, every line
the voice will speak, the mise, the finish, the tips. Your job is to make it as good as it
can be: clean, unambiguous English with every amount where the cook needs it, nothing
said twice, nothing that contradicts something else in the plan.

HOW THE SCREEN IS BUILT (so you fix causes, not symptoms): an ingredient token {ingN}
renders as AMOUNT + " " + LABEL from the step's ingredient N. "Add the {ing1}" with amount
"2 Tbsp" and label "minced garlic" renders "Add the 2 Tbsp minced garlic". Therefore:
  • amount = the MEASURE only ("2 Tbsp", "¾ lb", "two 28-oz cans") — never the ingredient's
    name, never a prep word the label already carries;
  • label = a bare, lowercase noun phrase ("minced garlic", "Parmigiano") — no leading
    article, no clause (", for the purée"), no parenthetical; proper nouns keep their caps;
  • a bundle deployed as ONE unit: amount lists each member with its measure
    ("2½ cups onion + 3 cloves garlic"), label = the bundle's label; the card shows the
    amount alone, so the label must not be needed to make sense of the line;
  • a seasoning "to taste": amount "to taste", label "kosher salt" → renders "kosher salt
    (to taste)"; an informal measure ("a pinch") renders "a pinch of black pepper".
  • a sub-step line is its `screen` fragment (tokens expanded) or, with no screen, its
    `voice` line plus the amounts it omitted appended after a dash. The voice line is
    SPOKEN: terse, no amounts needed, but it must be clear and natural when heard.

WHAT TO FIX (severity blocker — the plan is not acceptable with these):
  • doubled or redundant words as rendered; an amount that renders the ingredient's name
    twice; a prep word said twice; broken English at the amount/label seam;
  • an amount missing where the cook acts (a sub-step that deploys an ingredient but shows
    no quantity);
  • a CONTRADICTION anywhere in the plan — finish vs steps, a tip or attachment vs what the
    steps or the declared changes do (e.g. a tip says reserve pasta water, the finish says
    don't); drop the attachment or reword the field that is wrong, matching the declared
    changes (they are the plan's decisions);
  • a voice line that is unclear or misleading when heard alone.
Severity polish: sentence-case labels mid-sentence, a label carrying a clause, clumsy
phrasing, a headnote/finish that rambles. Fix these too when the fix is a plain edit.

WHAT YOU MAY NOT DO: add, remove or re-order ingredients, steps or sub-steps; change any
quantity's VALUE (move words between amount and label freely, never change the numbers);
change the cooking (technique, order, times, temperatures) — declared changes are
decisions already made; invent tips. Keep the {ingN}/{amt:..}/{bundle:..} tokens in an
instruction or screen fragment — edit the words around them, or the amount/label they
expand to. Never put the ingredient's name into an instruction when a token carries it.

OUTPUT: call report_audit exactly once. Grade the plan AS GIVEN (before your edits).
List every finding, then the edits that fix every blocker and as many polish items as
plain edits can. `path` must be copied EXACTLY from the EDITABLE FIELDS map; the value is
the complete new text for that field. An edit you cannot express as one field's new text
is not yours to make — report it as a finding instead. If the plan is clean, say so: an
empty edits list with grade 5 is a correct answer; do not invent nitpicks.\
"""


def _llm_round(cook: CookMetadata, mech: List[Dict[str, str]], log: Callable,
               usages: Optional[list], round_no: int, residual: Optional[list] = None) -> dict:
    rendered = render_plan_text(cook)
    emap = editable_map(cook)
    user_parts = [
        "THE PLAN AS THE COOK SEES IT:\n" + rendered,
        "\nMECHANICAL FINDINGS (already detected by code — every blocker must be fixed by your edits):\n"
        + ("\n".join(f"  - [{f['severity']}] {f['where']}: {f['problem']}\n      renders: {f['rendered']!r}\n      hint: {f['hint']}"
                     for f in mech) if mech else "  (none)"),
    ]
    if residual:
        user_parts.append("\nSTILL PRESENT AFTER YOUR PREVIOUS EDITS (round %d) — fix these specifically:\n" % (round_no - 1)
                          + "\n".join(f"  - {r}" for r in residual))
    user_parts.append("\nEDITABLE FIELDS (path → current value; copy paths exactly):\n"
                      + json.dumps(emap, ensure_ascii=False, indent=1))
    user_parts.append("\nAudit the plan and call report_audit.")
    resp = llm.create(
        operation="cook_audit", model=MODEL, max_tokens=_MAX_TOKENS, system=_SYSTEM,
        tools=[_REPORT_TOOL], tool_choice={"type": "tool", "name": "report_audit"},
        messages=[{"role": "user", "content": "\n".join(user_parts)}],
    )
    if usages is not None:
        cook_costs.record(usages, MODEL, resp.usage)
    log(f"[cook-audit] round {round_no} opus: {resp.usage.input_tokens} in / {resp.usage.output_tokens} out"
        + (" [TRUNCATED]" if resp.stop_reason == "max_tokens" else ""))
    for block in resp.content:
        if block.type == "tool_use":
            return block.input or {}
    return {}


def _fmt(f: Dict[str, str]) -> str:
    return f"{f['where']}: {f['problem']} (renders {f['rendered']!r})"


def audit_cook(cook: CookMetadata, log: Callable = print, usages: Optional[list] = None,
               rounds: int = MAX_ROUNDS, llm_pass: bool = True) -> dict:
    """Audit + repair IN PLACE. Returns the report persisted as `cook.audit`:
      {version, grade, rounds:[{findings, edits_applied, edits_refused, removed}],
       mechanical_before, residual: [...blockers still present...],
       residual_polish: [...], passed}
    `passed` is False only when a BLOCKER survives every round."""
    report: dict = {"version": AUDIT_VERSION, "grade": None, "rounds": [],
                    "mechanical_before": [], "residual": [], "residual_polish": [], "passed": True}
    mech = mechanical_findings(cook)
    report["mechanical_before"] = [_fmt(f) for f in mech]
    log(f"[cook-audit] mechanical: {len(mech)} finding(s) "
        f"({sum(1 for f in mech if f['severity'] == 'blocker')} blocker)")
    if not llm_pass:
        report["residual"] = [_fmt(f) for f in mech if f["severity"] == "blocker"]
        report["residual_polish"] = [_fmt(f) for f in mech if f["severity"] == "polish"]
        report["passed"] = not report["residual"]
        return report

    residual: Optional[list] = None
    for r in range(1, rounds + 1):
        verdict = _llm_round(cook, mech, log, usages, r, residual)
        rr = {"grade": verdict.get("grade"), "findings": verdict.get("findings") or [],
              "edits_applied": [], "edits_refused": [], "removed": []}
        if report["grade"] is None:
            report["grade"] = verdict.get("grade")
        for e in verdict.get("edits") or []:
            if not isinstance(e, dict):
                continue
            err = apply_edit(cook, e.get("path"), e.get("value"))
            (rr["edits_refused"] if err else rr["edits_applied"]).append(
                err or {"path": e.get("path"), "value": e.get("value"), "why": e.get("why")})
        for ra in verdict.get("remove_attachments") or []:
            if isinstance(ra, dict) and ra.get("kb_id"):
                if remove_attachment(cook, ra.get("step"), ra["kb_id"]):
                    rr["removed"].append({"step": ra.get("step"), "kb_id": ra["kb_id"], "why": ra.get("why")})
        polish_cook(cook)      # the edits get the same hygiene as the original emit
        report["rounds"].append(rr)
        n_b = sum(1 for f in rr["findings"] if isinstance(f, dict) and f.get("severity") == "blocker")
        log(f"[cook-audit] round {r}: grade {rr['grade']}/5 · {len(rr['findings'])} finding(s) "
            f"({n_b} blocker) · {len(rr['edits_applied'])} edit(s) applied"
            + (f", {len(rr['edits_refused'])} refused" if rr["edits_refused"] else "")
            + (f", {len(rr['removed'])} attachment(s) dropped" if rr["removed"] else ""))
        for err in rr["edits_refused"]:
            log(f"[cook-audit]   refused: {err}")
        mech = mechanical_findings(cook)
        blockers = [f for f in mech if f["severity"] == "blocker"]
        # Blockers the MODEL reported that aren't mechanical can't be re-verified by
        # code; a second round only makes sense for what code can still see.
        if not blockers:
            break
        residual = [_fmt(f) for f in blockers]
        log(f"[cook-audit] {len(blockers)} mechanical blocker(s) remain after round {r}")
    mech = mechanical_findings(cook)
    report["residual"] = [_fmt(f) for f in mech if f["severity"] == "blocker"]
    report["residual_polish"] = [_fmt(f) for f in mech if f["severity"] == "polish"]
    report["passed"] = not report["residual"]
    log(f"[cook-audit] done: passed={report['passed']} · grade before edits {report['grade']}/5 · "
        f"{sum(len(x['edits_applied']) for x in report['rounds'])} edit(s) total · "
        f"{len(report['residual'])} blocker(s) / {len(report['residual_polish'])} polish left")
    return report
