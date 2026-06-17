"""cook_polish.py — deterministic text hygiene for a reworked `_cook` block.

A final, $0, judgment-free pass that fixes the MECHANICAL prose defects an LLM
leaves behind — doubled words ("the the"), doubled/space-before punctuation,
collapsed whitespace, sentence-start capitalization — across every prose field
the cook-view shows or speaks. It is NOT a grammar rewriter: it never changes
wording, amounts, or meaning, and it never touches the `{ingN}`/`{amt:..}`/
`{bundle:..}` tokens (they're masked out before any rule runs and restored
after), so it cannot break rendering or the gauntlet. The richer grammar/flow
rewrite, if ever wanted, is a separate constrained LLM pass — this is the safe
floor that runs on every rework.

A common duplication source is token expansion: prose "Add the {ing1}" + a use
label "the garlic" renders "Add the the garlic". So we also strip a leading
article off USE labels (they should be bare nouns; the prose/ definiteness
supplies the article) — `strip_leading_article`.
"""
from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"\{[^{}]*\}")
_ARTICLE_RE = re.compile(r"^\s*(?:the|a|an|your)\s+", re.IGNORECASE)
_DUP_WORD_RE = re.compile(r"\b(\w+)(\s+\1\b)+", re.IGNORECASE)


def strip_leading_article(label: str) -> str:
    """Drop a leading the/a/an/your from a use label (a bare noun is wanted —
    the surrounding prose supplies the article). Leaves a None/'' untouched."""
    if not label:
        return label
    return _ARTICLE_RE.sub("", label, count=1)


def tidy(text: str) -> str:
    """Mechanical clean-up of one prose string. Token-safe + meaning-preserving."""
    if not text:
        return text
    # Mask tokens so no punctuation/space/dup rule can reach inside them.
    toks: list = []

    def _mask(m):
        toks.append(m.group(0))
        return f"\x00{len(toks) - 1}\x00"

    s = _TOKEN_RE.sub(_mask, str(text))

    s = _DUP_WORD_RE.sub(r"\1", s)            # "the the" -> "the"
    s = re.sub(r"[ \t]+", " ", s)             # collapse runs of spaces/tabs
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)    # no space before , . ; : ! ?
    s = re.sub(r"([,.;:!?])\1+", r"\1", s)    # ".." -> ".", "!!" -> "!"
    # one space after sentence/clause punctuation when a letter follows — but NOT
    # between digits ("1.5", "1,000" stay intact).
    s = re.sub(r"(?<![0-9])([,.;:!?])(?=[A-Za-z])", r"\1 ", s)
    s = s.strip()
    # Capitalize the first alphabetic character of the field.
    s = re.sub(r"^([^A-Za-z]*)([a-z])", lambda m: m.group(1) + m.group(2).upper(), s)

    # Restore tokens.
    s = re.sub(r"\x00(\d+)\x00", lambda m: toks[int(m.group(1))], s)
    return s


def polish_cook(cook) -> None:
    """In-place: run `tidy` over every prose field of a CookMetadata, and strip
    leading articles off the inline USE labels (the chief "the the" source).
    Called by the rework engine after assembly, before the gauntlet, so the
    persisted + validated block is the clean one."""
    def t(v):
        return tidy(v) if isinstance(v, str) else v

    cook.headnote = t(cook.headnote)
    cook.finish = t(cook.finish)
    cook.cooks_note = t(cook.cooks_note)
    cook.technique_changes = [t(x) for x in (cook.technique_changes or [])]

    for ing in cook.ingredients:
        ing.name = t(ing.name)
        ing.note = t(ing.note)
        for fv in ing.form_variants:
            fv.name = t(fv.name)

    for b in cook.bundles:
        b.label = t(b.label)
        b.combine_note = t(b.combine_note)
        b.excluded_reason = t(b.excluded_reason)
        for m in b.members:
            m.label = t(strip_leading_article(m.label))

    for eq in cook.equipment:
        eq.name = t(eq.name)
        eq.why = t(eq.why)

    for r in cook.reserved:
        r.label = t(r.label)
        r.note = t(r.note)

    for s in cook.steps:
        s.name = t(s.name)
        s.instruction = t(s.instruction)
        for si in s.ingredients:
            si.label = t(strip_leading_article(si.label))
        for ss in s.substeps:
            ss.voice = t(ss.voice)
            ss.screen = t(ss.screen)
        for a in s.attachments:
            a.text = t(a.text)

    for a in cook.tips:
        a.text = t(a.text)
