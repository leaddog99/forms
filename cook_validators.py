"""cook_validators.py — the gauntlet (recipe-optimization-handoff.md §5).

Mechanical, deterministic gates over a CookMetadata. A rework that fails any gate
does NOT ship — these are code, not LLM self-review, because we missed unit-mixing
twice by eye. Each validator returns a list of human-readable failures (empty =
pass); run_all aggregates into a CookValidatorReport stored on the block.

Operates on the STRUCTURED `_cook` object (better than the prototype's text scans):
the model carries the hooks the gates key on — `convertible`, `definiteness`,
`first_step`, `reused_from_step`, bundle membership.
"""
from __future__ import annotations

import re
from typing import List, Tuple, Iterable

from cook_model import CookMetadata, CookAmount


# --------------------------------------------------------------------------- #
# Shared scanners
# --------------------------------------------------------------------------- #
# Units are detected ONLY when attached to a number ("\d ... unit") so an "l" in
# a word or a "g" in "garlic" never false-positives. Longest tokens first.
_METRIC_RE = re.compile(
    r"\d\s*(?:kg|mg|ml|cl|litre|liter|km|cm|mm|g|l)\b", re.IGNORECASE)
_METRIC_TEMP_RE = re.compile(r"\d\s*°?\s*C\b")
_IMPERIAL_RE = re.compile(
    r"\d\s*(?:lbs|lb|oz|cups|cup|quart|qt|pint|pt|fl\.?\s?oz|inches|inch|in)\b",
    re.IGNORECASE)
_IMPERIAL_TEMP_RE = re.compile(r"\d\s*°?\s*F\b")

# A real measure carries a NUMBER — a digit or a cooking-fraction glyph. IMPRECISE
# measures (splash, drizzle, glug, knob, "a handful", pinch, dash, "to taste") are
# valid and unitless: they are measurements people understand, not defects. The model
# marks them convertible=False (CookAmount docstring: "vessel-as-measure ('a
# handful')") and the numeric gate EXEMPTS them. A survey of 830 recipes confirmed
# these words are overwhelmingly legitimate — verbs ("Drizzle with oil", "Ladle into
# bowls"), the form "saffron threads", or finishing/to-taste prose — so we do NOT
# word-scan step prose at all. Precision is enforced structurally: a CONVERTIBLE
# amount (one the model claims has a real unit) must show a number.
_NUMERIC_RE = re.compile(r"[0-9¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]")

# Lookback phrases — a step must be self-contained.
_LOOKBACK_RE = re.compile(
    r"\b(?:see (?:the )?list|see above|as above|listed above|from the list|"
    r"picker above|above\b.*\blist|refer to)\b", re.IGNORECASE)

_BACKREF_DEFINITENESS = {"the", "your", "reserved", "from-step", "bundle"}


def _has_metric(s: str) -> bool:
    return bool(_METRIC_RE.search(s) or _METRIC_TEMP_RE.search(s))


def _has_imperial(s: str) -> bool:
    return bool(_IMPERIAL_RE.search(s) or _IMPERIAL_TEMP_RE.search(s))


def _iter_amounts(cook: CookMetadata) -> Iterable[Tuple[str, CookAmount]]:
    """Every CookAmount in the block, tagged with where it lives."""
    for ing in cook.ingredients:
        for fv in ing.form_variants:
            yield (f"ingredient {ing.id} form '{fv.form}'", fv.amount)
    for b in cook.bundles:
        for m in b.members:
            yield (f"bundle {b.id} member {m.ingredient_id}", m.amount)
    for s in cook.steps:
        for si in s.ingredients:
            yield (f"step {s.number} ingredient {si.ingredient_id}", si.amount)
    for r in cook.reserved:
        if r.amount:
            yield (f"reserved {r.id}", r.amount)


# --------------------------------------------------------------------------- #
# The gates
# --------------------------------------------------------------------------- #
def v_unit_consistency(cook: CookMetadata) -> List[str]:
    """No amount face mixes systems: a convertible amount's imperial face carries
    no metric token and vice-versa. convertible=False (counts/pinch/back-refs) is
    exempt — those show identically in both systems."""
    out = []
    for where, a in _iter_amounts(cook):
        if not a.convertible:
            continue
        if _has_metric(a.imperial):
            out.append(f"{where}: metric unit in the IMPERIAL face ({a.imperial!r})")
        if _has_imperial(a.metric):
            out.append(f"{where}: imperial unit in the METRIC face ({a.metric!r})")
    return out


def v_definite_article(cook: CookMetadata) -> List[str]:
    """Every ingredient used in a method step is a back-reference (the measuring
    already happened in the mise) — `definiteness` ∈ {the,your,reserved,from-step,
    bundle}. An indefinite/blank marker means an ingredient escaped the mise."""
    out = []
    for s in cook.steps:
        for si in s.ingredients:
            d = (si.definiteness or "").strip().lower()
            if d not in _BACKREF_DEFINITENESS:
                out.append(f"step {s.number} '{si.label}': non-back-reference "
                           f"definiteness {si.definiteness!r} (introduces, not refers back)")
        # Belt-and-suspenders: an INGREDIENT amount INTRODUCED indefinitely in
        # prose ("add a ¼ cup of cream"). Precision matters — the rule is about
        # ingredient amounts, NOT every "a ¼ X" phrase. We require a volume/weight
        # unit + "of <ingredient>" (the introduction signature), and exclude
        # per-portion / technique measures that legitimately use "a":
        #   "a ¼ cup at a time", "a ¼ cup per egg", "a ¼-inch border" -> NOT flagged.
        if re.search(
            r"\b(?:a|an)\s+(?:¼|½|¾|⅓|⅔|\d+(?:[./]\d+)?)\s*"
            r"(?:cups?|tbsps?|tablespoons?|tsps?|teaspoons?|oz|ounces?|lbs?|pounds?|"
            r"g|grams?|kg|ml|l)\s+of\b"
            r"(?!.{0,40}\b(?:per|at a time|each|apiece|or so)\b)",
            s.instruction, re.IGNORECASE,
        ):
            out.append(f"step {s.number}: indefinite ingredient measure in prose "
                       f"('a ¼ cup of …') — should read 'the …' (back-referenced)")
    return out


def v_every_measure_numeric(cook: CookMetadata) -> List[str]:
    """A CONVERTIBLE amount — one the model claims carries a real unit+number — must
    show a number. Imprecise measures (splash/drizzle/pinch/"a handful"/to-taste) are
    convertible=False and EXEMPT: they're valid unitless measurements, not defects.
    Step prose is NOT word-scanned (a finishing 'drizzle of oil' is correct English)."""
    out = []
    for where, a in _iter_amounts(cook):
        if not a.convertible:
            continue
        if not _NUMERIC_RE.search(a.imperial or "") and not _NUMERIC_RE.search(a.metric or ""):
            out.append(f"{where}: convertible amount carries no number "
                       f"({a.imperial!r}/{a.metric!r}) — give a number or mark it "
                       f"unitless (convertible=False)")
    return out


def v_no_lookback(cook: CookMetadata) -> List[str]:
    """No step points the cook away to a list/picker/'as above'."""
    out = []
    for s in cook.steps:
        m = _LOOKBACK_RE.search(s.instruction)
        if m:
            out.append(f"step {s.number}: lookback phrase '{m.group(0)}'")
    return out


def v_mise_complete(cook: CookMetadata) -> List[str]:
    """Every ingredient is pre-measured in the mise (appears in some bundle,
    incl. the 'Measured & ready' catch-all), and no step introduces an ingredient
    that isn't declared."""
    out = []
    ing_ids = {i.id for i in cook.ingredients}
    bundle_ids = {b.id for b in cook.bundles}
    in_bundle = {m.ingredient_id for b in cook.bundles for m in b.members}
    for iid in ing_ids - in_bundle:
        out.append(f"ingredient {iid}: not in any bundle — escapes the mise "
                   f"(would be measured mid-cook)")
    # A step may reference an INGREDIENT id, a BUNDLE id, or a declared PUT-ASIDE
    # (ReservedItem) id. Bundles deploy a pre-combined mise entry as one unit;
    # put-asides are cooking intermediates set aside mid-cook and used later —
    # reduced pan juices, reserved pasta water, browned meat — which by definition
    # have NO starting-ingredient id (ReservedItem.id is documented as "reused by a
    # consuming StepIngredient"). Their created→consumed lifecycle + ordering are
    # enforced by the appearance-order / consumed-step validators, not here. A step
    # ingredient matching none of the three is a genuine fresh introduction (mise leak).
    reserved_ids = {r.id for r in cook.reserved}
    valid_refs = ing_ids | bundle_ids | reserved_ids
    for s in cook.steps:
        for si in s.ingredients:
            if si.ingredient_id not in valid_refs:
                out.append(f"step {s.number}: '{si.ingredient_id}' is not a declared "
                           f"ingredient, bundle, or put-aside — introduced fresh in a method step")
    return out


def v_appearance_order(cook: CookMetadata) -> List[str]:
    """The four lists are sorted by first appearance, and nothing is referenced
    before it appears (reuse points BACK)."""
    out = []

    def _check_sorted(items, key, label):
        prev = None
        for it in items:
            k = getattr(it, key, None)
            if k is None:
                continue
            if prev is not None and k < prev:
                out.append(f"{label} not in appearance order: {key}={k} after {prev}")
            prev = k

    _check_sorted(cook.ingredients, "first_step", "ingredients")
    _check_sorted(cook.bundles, "first_step", "bundles")
    _check_sorted(cook.equipment, "first_step", "equipment")
    _check_sorted(cook.reserved, "created_step", "reserved")

    for s in cook.steps:
        for si in s.ingredients:
            if si.reused_from_step is not None and si.reused_from_step >= s.number:
                out.append(f"step {s.number} '{si.label}': reused_from_step "
                           f"{si.reused_from_step} does not point BACK")
        for se in s.equipment:
            if se.reused_from_step is not None and se.reused_from_step >= s.number:
                out.append(f"step {s.number} equipment {se.equipment_id}: "
                           f"reused_from_step {se.reused_from_step} does not point back")
    for r in cook.reserved:
        if r.consumed_step is not None and r.consumed_step < r.created_step:
            out.append(f"reserved {r.id}: consumed (step {r.consumed_step}) before "
                       f"created (step {r.created_step})")
    return out


_LAYERED_STAPLE = re.compile(r"\b(salt|pepper|oil|water|butter)\b", re.IGNORECASE)


def v_reuse_referenced(cook: CookMetadata) -> List[str]:
    """A tool/ingredient used again in a later step references its origin step.
    EXCEPT staples/seasonings added in LAYERS — salt, pepper, oil, water, or any
    `to_taste` item — where each addition is a fresh small amount with no single
    origin portion to point back to (you season in stages, not 'reuse the salt').
    Distinctive single-portion reuse (a held pan, reserved water) is a put-aside
    and carries reused_from_step legitimately; tools always point back."""
    out = []
    layered = set()
    for i in cook.ingredients:
        if i.to_taste or _LAYERED_STAPLE.search(i.id) or _LAYERED_STAPLE.search(i.name or ""):
            layered.add(i.id)
    first_ing: dict = {}
    first_eq: dict = {}
    for s in sorted(cook.steps, key=lambda x: x.number):
        for si in s.ingredients:
            if si.ingredient_id in layered:
                continue
            if si.ingredient_id in first_ing:
                if si.reused_from_step is None:
                    out.append(f"step {s.number} '{si.label}': re-used (first at step "
                               f"{first_ing[si.ingredient_id]}) but no reused_from_step")
            else:
                first_ing[si.ingredient_id] = s.number
        for se in s.equipment:
            if se.equipment_id in first_eq:
                if se.reused_from_step is None:
                    out.append(f"step {s.number} equipment {se.equipment_id}: re-used "
                               f"(first at step {first_eq[se.equipment_id]}) but no "
                               f"reused_from_step")
            else:
                first_eq[se.equipment_id] = s.number
    return out


def v_single_deployment(cook: CookMetadata) -> List[str]:
    """An ingredient enters the method EXACTLY ONCE — directly in a step OR via a
    deployed bundle, never both (and never via two different bundles). The John's
    Pesto rework mentioned basil with its full 70 g at step 1 directly AND again at
    step 5 inside the basil+salt bundle, garlic at steps 2 and 3 — reading as
    duplicated ingredients to the cook. `v_reuse_referenced` can't see this: the
    direct id and the bundle id differ, so its per-id re-use map never fires.
    Exempt: layered staples / to_taste (seasoning in stages is not a re-entry) and
    direct refs that explicitly point BACK (reused_from_step, or definiteness
    'reserved'/'from-step') — those are acknowledged re-uses, not fresh mentions."""
    out = []
    layered = set()
    for i in cook.ingredients:
        if i.to_taste or _LAYERED_STAPLE.search(i.id) or _LAYERED_STAPLE.search(i.name or ""):
            layered.add(i.id)
    members_by_bundle = {b.id: {m.ingredient_id for m in b.members} for b in cook.bundles}
    # ingredient -> [(step, via)] fresh entries only
    entries: dict = {}
    for s in sorted(cook.steps, key=lambda x: x.number):
        for si in s.ingredients:
            if si.reused_from_step is not None or (si.definiteness or "") in ("reserved", "from-step"):
                continue  # acknowledged re-use, not a fresh entry
            if si.ingredient_id in members_by_bundle:      # a bundle deployed as a unit
                for mid in members_by_bundle[si.ingredient_id]:
                    if mid not in layered:
                        entries.setdefault(mid, []).append((s.number, f"bundle {si.ingredient_id}"))
            elif si.ingredient_id not in layered:
                entries.setdefault(si.ingredient_id, []).append((s.number, "directly"))
    for iid, hits in entries.items():
        if len(hits) > 1:
            where = " and ".join(f"step {n} ({via})" for n, via in hits)
            out.append(f"ingredient {iid}: enters the method more than once — {where} — "
                       f"deploy it once (prep lives in the mise OR a step, never both; "
                       f"a real later re-use points back via reused_from_step)")
    return out


def v_has_steps(cook: CookMetadata) -> List[str]:
    """A rework with ZERO steps is not a cook — it's a truncated or failed emit. All
    the other gates iterate OVER steps, so they pass vacuously on an empty method and
    a stepless `_cook` would persist (the José Andrés Gambas bug: max_tokens truncation
    dropped the late-emitted `steps` array → 0 steps → gauntlet passed). This gate is
    the floor: no steps ⇒ fail."""
    if not cook.steps:
        return ["no steps — empty method (truncated/failed emit); a cook must have steps"]
    return []


def v_substeps(cook: CookMetadata) -> List[str]:
    """The voice-pacing sub-step split (v2) is well-formed — fires ONLY for steps
    that HAVE substeps, so steps/reworks without them are untouched (back-compat):
      • every sub-step carries a non-empty `voice` line (the spoken action);
      • every `ingredients` index points at a real parent step ingredient (1-based);
      • COVERAGE — every parent step ingredient is deployed in some sub-step, so a
        split never silently drops an ingredient the cook needs to hear.
    Quality (split-on-independence, ≤3 elements) is the model's judgment; this gate
    only guards mechanical integrity, matching the rest of the gauntlet."""
    out = []
    for s in cook.steps:
        if not s.substeps:
            continue
        n_ing = len(s.ingredients)
        covered = set()
        for j, ss in enumerate(s.substeps, 1):
            if not (ss.voice or "").strip():
                out.append(f"step {s.number} sub-step {j}: empty voice line")
            for idx in ss.ingredients:
                if idx < 1 or idx > n_ing:
                    out.append(f"step {s.number} sub-step {j}: ingredient index {idx} "
                               f"out of range (step has {n_ing} ingredient(s))")
                else:
                    covered.add(idx)
        missing = [k for k in range(1, n_ing + 1) if k not in covered]
        if missing:
            out.append(f"step {s.number}: ingredient(s) {missing} not deployed in any "
                       f"sub-step — the split would leave them unspoken")
    return out


def v_bundling(cook: CookMetadata) -> List[str]:
    """Every bundle states WHY it's combined (combine_note) or why an item is kept
    separate (excluded_reason) — co-added groups are bundled or carry a reason."""
    out = []
    for b in cook.bundles:
        if not (b.combine_note or "").strip() and not (b.excluded_reason or "").strip():
            out.append(f"bundle {b.id} '{b.label}': no combine_note or excluded_reason "
                       f"(must state why it is/isn't combined)")
    return out


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
VALIDATORS = [
    ("has-steps", v_has_steps),
    ("unit-consistency", v_unit_consistency),
    ("definite-article", v_definite_article),
    ("every-measure-numeric", v_every_measure_numeric),
    ("no-lookback", v_no_lookback),
    ("mise-complete", v_mise_complete),
    ("appearance-order", v_appearance_order),
    ("reuse-referenced", v_reuse_referenced),
    ("single-deployment", v_single_deployment),
    ("bundling", v_bundling),
    ("substeps", v_substeps),
]


def run_all(cook: CookMetadata):
    """Run the full gauntlet; return a CookValidatorReport (passed iff zero
    failures). Failures are prefixed with their gate name."""
    from cook_model import CookValidatorReport
    failures: List[str] = []
    ran: List[str] = []
    for name, fn in VALIDATORS:
        ran.append(name)
        for f in fn(cook):
            failures.append(f"[{name}] {f}")
    return CookValidatorReport(passed=not failures, failures=failures, ran=ran)
