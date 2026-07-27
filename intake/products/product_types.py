"""What KIND of thing is this? — one shared vocabulary for product-identity checks.

Used to answer a narrow question: when we resolve a product name to an Amazon listing, is the
listing the same TYPE of object? That check caught a real error — a Le Creuset Signature Round
BREAD oven standing in for a Le Creuset dutch oven, same brand, same colour, nothing else to
tell them apart (2026-07-27).

But a naive noun match then produced a false REJECT on the very next run: Staub's listing is
titled "Cast Iron Dutch Oven 5.5-qt Round Cocotte" while the review called it a "Round
Cocotte" — one product, two names, flagged as a mismatch.

So the model here is SYNONYM GROUPS plus a fail-open rule:

  * words in the same group are the same kind of thing (cocotte IS a dutch oven);
  * a conflict is only declared when BOTH texts resolve to a canonical type AND those types
    are different. Anything ambiguous or unrecognised passes.

Fail-open is deliberate. A false reject silently drops a legitimate product from a ranking and
looks like an absence; a false accept still has to get past the brand check and the capacity,
and shows up as a visibly wrong title. The asymmetry favours letting doubt through.

AMBIGUOUS_TERMS are the words that name a shape rather than a product — "casserole" is a
dutch oven at ATK ("Cuisinart Chef's Enameled Cast Iron Casserole") and a 13x9 baking dish at
Williams-Sonoma. They are deliberately NOT canonicalized: they must never be the thing that
decides a conflict.
"""
from __future__ import annotations

# Each group = one kind of object under all the names publishers give it.
TYPE_GROUPS: list[set] = [
    {"dutch oven", "french oven", "cocotte", "coquette"},
    {"bread oven", "bread cloche", "cloche"},
    {"braiser", "brazier"},
    {"stock pot", "stockpot", "multipot"},
    {"saucepan", "saucier"},
    {"skillet", "fry pan", "frying pan", "sauté pan", "saute pan"},
    {"grill pan", "griddle"},
    {"wok"},
    {"roasting pan", "roaster"},
    {"loaf pan", "loaf tin", "bread pan"},
    {"cake pan", "cake tin"},
    {"sheet pan", "baking sheet", "cookie sheet"},
    {"muffin pan", "muffin tin", "cupcake pan"},
    {"pie dish", "pie plate", "pie pan"},
    {"tart pan", "tart tin"},
    {"ramekin"},
    {"mixing bowl"},
    {"cutting board", "chopping board"},
    {"stand mixer"},
    {"hand mixer"},
    {"food processor"},
    {"blender"},
    {"kettle", "tea kettle"},
    {"knife", "santoku", "cleaver"},
    {"shears", "kitchen scissors"},
]

# Shape words that name several different products depending on the publisher. Never used to
# declare a conflict.
AMBIGUOUS_TERMS = {"casserole", "pot", "pan", "dish", "bowl", "cooker", "baker"}

_LOOKUP: dict[str, int] = {}
for _i, _g in enumerate(TYPE_GROUPS):
    for _term in _g:
        _LOOKUP[_term] = _i


def types_in(text: str) -> set:
    """Canonical type ids named in `text`. Longest match wins, so "bread oven" is not read
    as "oven" and "loaf pan" is not read as "pan"."""
    low = (text or "").lower()
    found = set()
    for term in sorted(_LOOKUP, key=len, reverse=True):
        if term in low:
            found.add(_LOOKUP[term])
    return found


def names_for(type_id: int) -> str:
    return "/".join(sorted(TYPE_GROUPS[type_id]))


def same_type(a: str, b: str) -> tuple:
    """Are these two descriptions the same kind of product? -> (ok, reason).

    Fail-open: True whenever either side is unrecognised or they overlap. False ONLY when
    both resolve and share nothing.
    """
    ta, tb = types_in(a), types_in(b)
    if not ta or not tb:
        return True, "type not determinable — allowed"
    if ta & tb:
        return True, "types agree"
    return False, (f"{'/'.join(names_for(t) for t in sorted(tb))} vs "
                   f"{'/'.join(names_for(t) for t in sorted(ta))}")
