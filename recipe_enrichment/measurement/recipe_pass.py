"""Recipe-level measurement pass — the hybrid orchestration.

For every `recipeIngredient` string:
  1. parse it deterministically (parse.py)            -> qty, unit, name
  2. resolve the name to a KA ingredient (resolve.py) -> density / g-per-item
  3. compute grams + mL deterministically (convert.py)

Lines the free path can't parse OR can't resolve are collected and sent to ONE
batched LLM call (the fallback), which returns qty/unit + a density or
grams-per-item per line; those are then run through the SAME deterministic
engine (LLMs do arithmetic badly, ingredient identification well — so the model
only identifies, never multiplies).

Writes `recipe["_measurements"]`: a list aligned 1:1 with `recipeIngredient`.
`recipeIngredient` itself is left untouched (display strings stay the source's;
this adds the structured metric/weight layer beside them). Deterministic and
free unless the fallback fires; best-effort throughout (a failure leaves an
entry non-convertible rather than aborting).
"""

from __future__ import annotations

import json
import time
from typing import Optional

from .convert import convert, resolve_unit, Domain
from .parse import parse_ingredient, clean_name
from .resolve import resolve_canonical


def _fmt(x: float) -> str:
    """Compact number for display: '120' not '120.0', '1.5' kept."""
    return str(int(x)) if float(x).is_integer() else str(x)


def _metric_amount(grams, ml, dom) -> Optional[str]:
    """The metric AMOUNT to show in the toggle. Food-aware weight is the whole
    point, so prefer grams; fall back to mL for pure-volume with no density;
    None for counts (a count like '3 eggs' is unchanged in metric)."""
    if dom is Domain.COUNT:
        return None
    if grams is not None:
        return f"{_fmt(grams)} g"
    if ml is not None:
        return f"{_fmt(ml)} ml"
    return None


def _metric_line(grams, ml, dom, name_part: str) -> Optional[str]:
    """Full metric display line ('120 g all-purpose flour'), or None when there's
    no metric form to show (count / unconvertible) — the form then shows raw."""
    amt = _metric_amount(grams, ml, dom)
    if not amt:
        return None
    name_part = (name_part or "").strip()
    return f"{amt} {name_part}".strip()


def _round_cook(x: Optional[float]) -> Optional[float]:
    """Cooking-sensible rounding: whole numbers above 10, one decimal below."""
    if x is None:
        return None
    if x >= 100:
        return round(x)
    if x >= 10:
        return round(x, 1)
    return round(x, 2)


def _compute(qty, unit, *, density=None, per_item=None) -> tuple[Optional[float], Optional[float]]:
    """grams + mL from a quantity/unit, using whatever bridge constant we have.
    Each direction is attempted independently so a volume with no density still
    yields mL (same-domain), and a count with g/item still yields grams."""
    grams = ml = None
    try:
        grams = convert(qty, unit, "g", density_g_per_ml=density,
                        grams_per_item=per_item).amount
    except (ValueError, KeyError):
        pass
    try:
        ml = convert(qty, unit, "ml", density_g_per_ml=density,
                     grams_per_item=per_item).amount
    except (ValueError, KeyError):
        pass
    return _round_cook(grams), _round_cook(ml)


def _entry(raw, qty, unit, name, *, canonical=None, grams=None, ml=None,
           convertible=False, resolved_by=None, source="", note="",
           metric=None) -> dict:
    e = {
        "raw": raw,           # imperial display = the source's original string
        "metric": metric,     # metric display string, or None -> show raw
        "qty": qty,
        "unit": unit,
        "ingredient": name,
        "canonical": canonical,
        "grams": grams,
        "ml": ml,
        "convertible": convertible,
        "resolved_by": resolved_by,
    }
    if source:
        e["source"] = source
    if note:
        e["note"] = note
    return e


def convert_recipe_measurements(
    recipe: dict,
    *,
    use_llm_fallback: bool = True,
    prior: Optional[list] = None,
    model: str = "claude-haiku-4-5",
    llm_key: Optional[str] = None,
) -> dict:
    """Populate recipe['_measurements'] (1:1 with recipeIngredient), recomputed
    from the CURRENT ingredient list so it's always in sync. Returns meta
    {timings, usage, counts}. Mutates `recipe` in place.

    `prior`: a previous _measurements list. Convertible entries are carried
    forward by exact `raw` string for any line the deterministic pass can't
    resolve — so a deterministic-only recompute (e.g. on every save) never loses
    or re-pays for an exotic ingredient an earlier LLM pass already resolved.

    `use_llm_fallback`: when True, lines still unresolved after carry-forward go
    to ONE batched LLM call. Save recomputes with this OFF (free + instant);
    extract runs it ON (the deliberate moment to resolve exotic ingredients)."""
    t0 = time.perf_counter()
    ingredients = recipe.get("recipeIngredient") or []
    entries: list[dict] = [None] * len(ingredients)  # type: ignore[list-item]
    unresolved: list[int] = []   # lines without a confident deterministic result
    n_det = 0

    for i, raw in enumerate(ingredients):
        p = parse_ingredient(raw)
        if not p.parseable or p.qty is None or not p.unit:
            # No usable qty/unit (e.g. "Salt, to taste"). Track for carry-forward
            # / LLM only when there's a name worth resolving.
            entries[i] = _entry(raw, p.qty, p.unit, p.name or clean_name(raw),
                                convertible=False, note=p.note)
            if p.name or (raw or "").strip():
                unresolved.append(i)
            continue

        ing = resolve_canonical(p.name, p.unit)
        density = ing.g_per_ml if ing else None
        per_item = ing.grams_per_item if ing else None
        grams, ml = _compute(p.qty, p.unit, density=density, per_item=per_item)

        dom, _f = resolve_unit(p.unit)
        needs_bridge = dom in (Domain.VOLUME, Domain.COUNT)
        resolved = ing is not None
        if resolved or grams is not None or (dom is Domain.MASS):
            metric = _metric_line(grams, ml, dom, p.name_raw or p.name)
            entries[i] = _entry(
                raw, p.qty, p.unit, p.name,
                canonical=ing.name if ing else None,
                grams=grams, ml=ml, metric=metric,
                convertible=grams is not None or ml is not None,
                resolved_by="deterministic" if resolved else None,
                source=(ing.source if ing else ""), note=p.note,
            )
            if resolved:
                n_det += 1
            elif needs_bridge and grams is None:
                # Have mL but not the food-aware weight — a prior/LLM density helps.
                unresolved.append(i)
        else:
            entries[i] = _entry(raw, p.qty, p.unit, p.name,
                                convertible=False, note=p.note)
            unresolved.append(i)

    # Carry forward prior resolutions (by exact raw) for the unresolved lines —
    # runs regardless of use_llm_fallback, so a save preserves earlier LLM work.
    n_carried = 0
    if prior:
        prior_by_raw = {m.get("raw"): m for m in prior
                        if isinstance(m, dict) and m.get("raw") and m.get("convertible")}
        if prior_by_raw:
            still: list[int] = []
            for i in unresolved:
                pm = prior_by_raw.get(entries[i].get("raw"))
                if pm is not None:
                    entries[i] = dict(pm)   # reuse the earlier (e.g. LLM) result
                    n_carried += 1
                else:
                    still.append(i)
            unresolved = still

    usage: list = []
    n_llm = 0
    if unresolved and use_llm_fallback:
        try:
            n_llm = _llm_resolve_misses(
                ingredients, entries, unresolved, model=model, usage=usage)
        except Exception as e:
            print(f"[measure] LLM fallback failed ({e}); leaving "
                  f"{len(unresolved)} line(s) unconverted")

    recipe["_measurements"] = entries
    elapsed = int((time.perf_counter() - t0) * 1000)
    return {
        "timings": {"measure_ms": elapsed},
        "usage": usage,
        "counts": {
            "total": len(ingredients),
            "deterministic": n_det,
            "carried": n_carried,
            "llm": n_llm,
            "unconverted": sum(1 for e in entries if not e.get("convertible")),
        },
    }


# --------------------------------------------------------------------------- #
# LLM fallback — identify only; the engine still does the math.
# --------------------------------------------------------------------------- #

_FALLBACK_TOOL = {
    "name": "submit_measurements",
    "description": "Return parsed quantity, unit, and a density or per-item "
                   "weight for each ingredient line so a deterministic engine "
                   "can compute grams and millilitres.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["index", "convertible"],
                    "properties": {
                        "index": {"type": "integer",
                                  "description": "the line's index as given"},
                        "convertible": {"type": "boolean",
                                        "description": "false for 'to taste', "
                                        "garnishes, or anything without a real amount"},
                        "qty": {"type": ["number", "null"]},
                        "unit": {"type": ["string", "null"],
                                 "description": "g, ml, cup, tbsp, tsp, oz, lb, "
                                 "each, clove, etc."},
                        "ingredient": {"type": ["string", "null"],
                                       "description": "the core ingredient name"},
                        "density_g_per_ml": {"type": ["number", "null"],
                                             "description": "for volume<->weight; "
                                             "e.g. water 1.0, flour ~0.53, honey ~1.42"},
                        "grams_per_item": {"type": ["number", "null"],
                                           "description": "for count<->weight; "
                                           "e.g. large egg 50"},
                    },
                },
            },
        },
    },
}

_FALLBACK_SYSTEM = (
    "You are a culinary measurement normalizer. For each recipe ingredient line "
    "you are given, extract the numeric quantity and unit, identify the core "
    "ingredient, and supply the physical constant needed to bridge volume and "
    "weight: density_g_per_ml for volume measures, or grams_per_item for counted "
    "items. Use well-established culinary reference values. Do NOT compute grams "
    "or millilitres yourself — a deterministic engine does the arithmetic. If a "
    "line has no real measurable amount (\"to taste\", \"for garnish\"), set "
    "convertible=false and leave the numbers null. Return one entry per line, "
    "echoing its given index."
)


def _llm_resolve_misses(ingredients, entries, misses, *, model, usage) -> int:
    """One batched call covering every missed line. Fills `entries[i]` in place
    via the deterministic engine using the LLM-supplied constants. Returns the
    count successfully converted."""
    import anthropic
    from input.pipeline.token_journal import build_usage_entry

    client = anthropic.Anthropic()  # ambient env key (BYOK injection: DL-5)
    lines = "\n".join(f"{i}: {ingredients[i]}" for i in miss_chunk(misses))
    prompt = ("Normalize these ingredient lines (index: text):\n\n" + lines)

    resp = client.messages.create(
        model=model,
        max_tokens=2048,
        temperature=0,
        system=_FALLBACK_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        tools=[_FALLBACK_TOOL],
        tool_choice={"type": "tool", "name": "submit_measurements"},
    )
    usage.append(build_usage_entry("measure_fallback", model, resp))

    parsed = next(
        (b.input for b in resp.content
         if getattr(b, "type", None) == "tool_use"
         and b.name == "submit_measurements"),
        None,
    )
    if not isinstance(parsed, dict):
        return 0

    converted = 0
    miss_set = set(misses)
    for item in parsed.get("items", []):
        i = item.get("index")
        if not isinstance(i, int) or i not in miss_set:
            continue
        raw = ingredients[i]
        name = (item.get("ingredient") or entries[i].get("ingredient")
                or clean_name(raw))
        if not item.get("convertible"):
            entries[i] = _entry(raw, item.get("qty"), item.get("unit"), name,
                                convertible=False, resolved_by="llm")
            converted += 1
            continue
        qty = item.get("qty")
        unit = item.get("unit")
        if qty is None or not unit:
            continue
        grams, ml = _compute(qty, unit,
                             density=item.get("density_g_per_ml"),
                             per_item=item.get("grams_per_item"))
        if grams is None and ml is None:
            continue
        try:
            dom, _f = resolve_unit(unit)
        except ValueError:
            dom = None
        metric = _metric_line(grams, ml, dom, name)
        entries[i] = _entry(
            raw, qty, unit, name, canonical=item.get("ingredient"),
            grams=grams, ml=ml, metric=metric,
            convertible=True, resolved_by="llm", source="LLM (reference density)",
        )
        converted += 1
    return converted


def miss_chunk(misses, limit: int = 60):
    """Cap the fallback batch so a pathological ingredient list can't blow the
    token budget; over the cap, the tail is left unconverted (logged)."""
    if len(misses) > limit:
        print(f"[measure] {len(misses)} misses > {limit} cap; "
              f"converting first {limit}, leaving {len(misses) - limit}")
        return misses[:limit]
    return misses
