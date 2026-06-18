"""Token-cost estimation for a cook-rework run.

Accumulates per-call usage (each `_emit_cook` opus pass + the augment sonnet pass) and
prices it at the LATEST per-model rates. Anthropic bills three input buckets separately:
plain input (1×), cache-read (0.1×), cache-write (1.25×); output at the model's output
rate. Used to log a cost summary after each rework + project to assumed volumes.
"""
from __future__ import annotations

# $ per 1,000,000 tokens — (input, output). Keep current with the model lineup.
PRICES = {
    "claude-opus-4-8":   (5.00, 25.00),
    "claude-opus-4-7":   (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00, 5.00),
    "claude-fable-5":    (10.00, 50.00),
}
_CACHE_READ_MULT = 0.10    # cached input billed at 1/10 the input rate
_CACHE_WRITE_MULT = 1.25   # writing the cache costs 1.25× the input rate


def _rate(model: str):
    for k, v in PRICES.items():
        if model.startswith(k):
            return v
    return (5.00, 25.00)   # unknown → assume opus-tier (don't under-state)


def record(usages: list, model: str, usage_obj) -> None:
    """Append one normalized usage row from an Anthropic `resp.usage`."""
    usages.append({
        "model": model,
        "in":  getattr(usage_obj, "input_tokens", 0) or 0,
        "out": getattr(usage_obj, "output_tokens", 0) or 0,
        "cache_read":  getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
    })


def _cost_of(rec: dict, warm: bool = False) -> float:
    """Cost of one call. `warm` reprices cache-WRITE tokens at the cache-READ rate — the
    steady state in a batch, where the cached prefix is written once and read thereafter."""
    pin, pout = _rate(rec["model"])
    cw_mult = _CACHE_READ_MULT if warm else _CACHE_WRITE_MULT
    return (rec["in"] * pin
            + rec["out"] * pout
            + rec["cache_read"] * pin * _CACHE_READ_MULT
            + rec["cache_write"] * pin * cw_mult) / 1_000_000.0


def estimate(usages: list, volumes=(1_000, 10_000, 100_000)) -> dict:
    """Return {total, warm_total, calls[], volumes{}} for a rework's accumulated usage.
    `total` is THIS run (cold cache); `warm_total` is the per-recipe cost in a batch
    (cache prefix already written); volume projection uses warm_total."""
    calls = [{**r, "cost": round(_cost_of(r), 6)} for r in usages]
    total = round(sum(c["cost"] for c in calls), 6)
    warm = round(sum(_cost_of(r, warm=True) for r in usages), 6)
    return {"total": total,
            "warm_total": warm,
            "calls": calls,
            "volumes": {v: round(warm * v, 2) for v in volumes}}


def format_summary(est: dict) -> str:
    """A human one-block summary for the rework log / stream."""
    lines = ["[cook-rework] estimated cost (latest token prices):"]
    for c in est["calls"]:
        extra = ""
        if c["cache_write"] or c["cache_read"]:
            extra = f" (+{c['cache_write']:,} cache-w / {c['cache_read']:,} cache-r)"
        lines.append(f"    {c['model']}: {c['in']:,} in / {c['out']:,} out{extra}  = ${c['cost']:.4f}")
    warm = est.get("warm_total", est["total"])
    lines.append(f"  TOTAL this recipe (cold cache): ${est['total']:.4f}"
                 + (f"  ·  warm-cache batch: ~${warm:.4f}/recipe" if abs(warm - est['total']) > 1e-4 else ""))
    proj = " · ".join(f"{v:,}→${amt:,.2f}" for v, amt in est["volumes"].items())
    lines.append(f"  projected at volume (warm cache): {proj}")
    return "\n".join(lines)
