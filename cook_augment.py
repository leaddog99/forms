"""cook_augment.py — the AUGMENT pass (3c-b): attach curated KB tips/checks to a
reworked cook, by SELECTION + PROVENANCE.

Runs AFTER the rework, on the optimized `_cook`. Projects the PUBLISHED knowledge
base (cook_kb), asks a model to SELECT the entries that genuinely apply to each
step and reword them for THIS recipe — it never invents — then MECHANICALLY
VALIDATES every attachment: its `kb_id` must be one we injected (the moat — this
is how invented advice is caught in code, not trusted to the model) and its
`step_index` must be in range. Survivors attach as Attachment{kb_id, kind, text};
everything else is dropped + logged. `kind` is taken from the KB entry by id, not
from the model, so an entry can't be relabeled.

The curated-asset / runtime-selection split made real: durable owned knowledge
lives in the DB; the model only picks + contextualizes; provenance is enforced in
code. See recipe_anchor/phase3-pipeline-design.md + project_cook_kb.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

import llm  # central LLM gateway — auto-journals usage to bcc_token_journal

from cook_model import CookMetadata, Attachment

SONNET = "claude-sonnet-4-6"     # select + contextualize — judgment-light, cheaper tier
_MAX_TOKENS = 4096
AUGMENT_PROMPT_VERSION = "cook-augment-v1-2026-06-10"

_MAX_PER_STEP = 2
_MAX_RECIPE = 3


# --------------------------------------------------------------------------- #
# Forced tool — the model returns kb_id + reworded text; we own kind + validation
# --------------------------------------------------------------------------- #
_ATTACH_TOOL = {
    "name": "attach_guidance",
    "description": "Attach curated tips/checks to the recipe, selected ONLY from the provided KB.",
    "input_schema": {
        "type": "object",
        "properties": {
            "recipe_tips": {
                "type": "array",
                "description": "scope=recipe/either entries that apply to the dish overall.",
                "items": {
                    "type": "object",
                    "properties": {
                        "kb_id": {"type": "string", "description": "exact id of the KB entry used."},
                        "text": {"type": "string", "description": "the entry's knowledge reworded for THIS recipe."},
                    },
                    "required": ["kb_id", "text"],
                },
            },
            "step_augments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step_index": {"type": "integer", "description": "the step number this applies to."},
                        "attachments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kb_id": {"type": "string"},
                                    "text": {"type": "string"},
                                },
                                "required": ["kb_id", "text"],
                            },
                        },
                    },
                    "required": ["step_index", "attachments"],
                },
            },
        },
        "required": ["recipe_tips", "step_augments"],
    },
}


def _system_prompt(kb: list) -> str:
    return f"""You attach cooking guidance to a reworked recipe by SELECTING from a fixed \
knowledge base. You NEVER invent advice.

RULES:
- Use ONLY entries from the KB below, each referenced by its exact `kb_id`. Never attach a \
tip/check that is not in the KB.
- Attach an entry ONLY where it genuinely applies — match its `applies_when` (technique_tags, \
trigger_signals, ingredient_classes, equipment, scope) against what the step actually does. \
When in doubt, leave it out: a sparse, correct set beats a dense, loose one.
- Reword each entry's `knowledge` for THIS recipe, following its `render_hint`: name the actual \
ingredient / equipment / stage. One or two tight sentences. Never contradict the step.
- If an entry lists variants, pick the ONE that matches this recipe and ignore the rest.
- A step-scoped entry goes in `step_augments` (by step number); a recipe-scoped entry that \
applies to the whole dish goes in `recipe_tips`. At most {_MAX_PER_STEP} per step and \
{_MAX_RECIPE} recipe-level.
- NO REDUNDANCY: attach each piece of guidance to the SINGLE most relevant place. Do not repeat \
a kb_id, do not say the same thing twice in different words across steps, and do not echo in \
recipe_tips anything you already attached to a step. Every tip/check must add something new.

KNOWLEDGE BASE (the only entries you may use):
{json.dumps(kb, ensure_ascii=False, indent=2)}

Call `attach_guidance` exactly once. Output nothing else."""


def _recipe_view(cook: CookMetadata, recipe_name: Optional[str]) -> dict:
    """The compact view of the OPTIMIZED cook the model selects against — steps with
    their anchored prose, ingredients, and equipment, plus the full ingredient list."""
    steps = []
    for s in sorted(cook.steps, key=lambda x: x.number):
        steps.append({
            "step_index": s.number,
            "name": s.name,
            "instruction": s.instruction,
            "ingredients": [si.label for si in s.ingredients],
            "equipment": [se.equipment_id for se in s.equipment],
        })
    return {
        "name": recipe_name or cook.headnote or "",
        "ingredients": [i.name for i in cook.ingredients],
        "equipment": [e.name for e in cook.equipment],
        "steps": steps,
    }


def _call(kb: list, view: dict, log: Callable, usages: Optional[list] = None) -> dict:
    user = "Attach guidance to this reworked recipe:\n\n" + json.dumps(view, ensure_ascii=False, indent=2)
    # The system prompt (rules + the projected KB) carries NO per-recipe data — it's
    # identical across reworks until the KB is edited/published — so we mark it as a
    # prompt-cache prefix (tools + system are cached together). Consecutive reworks
    # then re-read those ~15K input tokens at a fraction of the price instead of
    # re-paying full freight. It's content-addressed: a KB edit changes the text and
    # auto-misses the next call (which re-writes the cache), so no invalidation code
    # is needed. Default ephemeral TTL is 5 min — the right window for a batch of runs.
    resp = llm.create(
        operation="cook_augment", model=SONNET, max_tokens=_MAX_TOKENS,
        system=[{
            "type": "text",
            "text": _system_prompt(kb),
            "cache_control": {"type": "ephemeral"},
        }],
        tools=[_ATTACH_TOOL], tool_choice={"type": "tool", "name": "attach_guidance"},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            u = resp.usage
            cw = getattr(u, "cache_creation_input_tokens", 0) or 0
            cr = getattr(u, "cache_read_input_tokens", 0) or 0
            if usages is not None:
                import cook_costs
                cook_costs.record(usages, SONNET, u)
            log(f"[augment] sonnet: {u.input_tokens} in / {u.output_tokens} out "
                f"(cache: {cr} read / {cw} write)")
            return block.input
    raise RuntimeError("attach_guidance tool was not called (tool_choice should have forced it)")


# --------------------------------------------------------------------------- #
# Grounding validators (2026-07-28)
# --------------------------------------------------------------------------- #
#
# A live cook hit this on turkey meatloaf: `bloom_spices_in_fat` was attached to
# step 1 (sauté the vegetables) and reworded as
#
#     "...let them cook for the full minute until fragrant BEFORE ADDING THE
#      CHICKEN STOCK — this blooms their fat-soluble flavor compounds..."
#
# The KB entry says "before adding LIQUID" and never mentions stock. Step 1 has no
# liquid at all; the chicken stock is in the step-3 binder. Grounding the generic
# phrase, the model reached for the only liquid in the recipe and moved it three
# steps earlier — turning a correct entry into a wrong instruction. Provenance was
# intact the whole time (a real kb_id, a real step), which is exactly why kb_id +
# step-range validation passed it.
#
# Two checks, both fail-open: they drop an attachment only on positive evidence of
# a mismatch, never on absence of evidence.


def _bundle_for(cook: CookMetadata, ingredient_id: str):
    """The bundle a step's `definiteness='bundle'` reference points at.

    A step names ONE ingredient id to stand for a whole bundle, and an id can appear
    in several bundles — kosher salt is in both 'Sauté seasoning' and 'Meatloaf
    seasoning & binder'. The representative is the bundle's FIRST member, so match on
    that before falling back to mere membership. (`first_step` cannot disambiguate:
    every mise bundle in the meatloaf carries first_step=1, including the one used at
    step 3.)
    """
    bundles = getattr(cook, "bundles", None) or []
    for b in bundles:
        mem = getattr(b, "members", None) or []
        if mem and getattr(mem[0], "ingredient_id", None) == ingredient_id:
            return b
    for b in bundles:
        for m in (getattr(b, "members", None) or []):
            if getattr(m, "ingredient_id", None) == ingredient_id:
                return b
    return None


def _deployed_by_step(cook: CookMetadata) -> dict:
    """{step_number: set(ingredient_id)} — everything actually in play at that step,
    with bundle references expanded to their members."""
    out = {}
    for s in cook.steps:
        ids = set()
        for si in (s.ingredients or []):
            ids.add(si.ingredient_id)
            if (si.definiteness or "") == "bundle":
                b = _bundle_for(cook, si.ingredient_id)
                if b:
                    ids.update(m.ingredient_id for m in (b.members or []))
        out[s.number] = ids
    return out


def _first_use(cook: CookMetadata) -> dict:
    """{ingredient_id: earliest step number it is deployed in}."""
    first = {}
    for num in sorted(_deployed_by_step(cook)):
        for iid in _deployed_by_step(cook)[num]:
            first.setdefault(iid, num)
    return first


_PLURAL = re.compile(r"(?:es|s)$")


def _mentions(text: str, name: str) -> bool:
    """Does `text` name this ingredient? Whole-word, case- and plural-insensitive.

    Deliberately literal. A looser match (stemming, synonyms) would start dropping
    correct attachments, and a false drop silently removes real guidance — the more
    expensive error, because nobody sees what is missing.
    """
    n = (name or "").strip().lower()
    if len(n) < 4:
        return False
    n = _PLURAL.sub("", n)
    return re.search(r"\b" + re.escape(n) + r"(?:es|s)?\b", (text or "").lower()) is not None


def forward_reference(text: str, step_number: int, cook: CookMetadata) -> str:
    """The ingredient this text tells the cook to use BEFORE the recipe introduces it,
    or "" if it is clean.

    Only FORWARD references are faults. An attachment may freely mention something
    already in the pan ("the onions you softened earlier"), and step 3's own tip
    legitimately names the chicken stock because that is where the stock goes. What a
    cook cannot act on is being told to add something three steps early.
    """
    first = _first_use(cook)
    for ing in (cook.ingredients or []):
        used_at = first.get(ing.id)
        if used_at is None or used_at <= step_number:
            continue                      # not used at all, or already in play
        if _mentions(text, ing.name):
            return f"{ing.name} (first used in step {used_at})"
    return ""


# aisle (recipe data, model-authored) -> KB ingredient_classes. Coarse on purpose: it
# exists to catch a GROSS mismatch (a poultry entry on a dessert step), not to
# adjudicate borderline cases. Unmapped aisles yield nothing and the check fails open.
_AISLE_CLASSES = {
    "produce": {"vegetables", "fruit"},
    "meat": {"meat", "poultry", "pork"},
    "poultry": {"poultry", "meat"},
    "seafood": {"seafood", "fish"},
    "fish": {"fish", "seafood"},
    "dairy": {"dairy", "eggs"},
    "eggs": {"eggs", "dairy"},
    "spices": {"spices"},
    "baking": {"dough", "batter", "grains"},
    "bakery": {"dough", "grains"},
    "pasta": {"pasta", "noodles", "grains"},
    "grains": {"grains"},
    "legumes": {"legumes"},
    "canned": {"legumes", "vegetables"},
}
# Name overrides where the aisle is too coarse: turkey sits in the meat aisle.
_NAME_CLASSES = (
    (re.compile(r"\b(chicken|turkey|duck|hen)\b", re.I), {"poultry"}),
    (re.compile(r"\b(pork|bacon|ham|pancetta|prosciutto)\b", re.I), {"pork"}),
    (re.compile(r"\b(beef|lamb|veal|steak)\b", re.I), {"meat"}),
    (re.compile(r"\b(shrimp|prawn|scallop|clam|mussel|crab|lobster|squid)\b", re.I), {"seafood"}),
    (re.compile(r"\b(salmon|tuna|cod|halibut|anchov|sardine)\b", re.I), {"fish"}),
    (re.compile(r"\begg\b", re.I), {"eggs"}),
)


def _classes_of(ing) -> set:
    out = set(_AISLE_CLASSES.get((getattr(ing, "aisle", "") or "").strip().lower(), set()))
    for rx, cls in _NAME_CLASSES:
        if rx.search(ing.name or ""):
            out |= cls
    return out


def scope_mismatch(entry: dict, step_number: int, cook: CookMetadata) -> str:
    """Why this KB entry cannot apply to this step, or "" if it might.

    Checks the entry's DECLARED scope (`applies_when.ingredient_classes`) against what
    the step actually deploys. Fails open in every uncertain direction: no declared
    classes, or a step whose ingredients we cannot classify at all, is never a
    mismatch. A validator that cannot tell must not veto.

    Worth being precise about what this does NOT catch — including the case that
    prompted it. `bloom_spices_in_fat` declares ingredient_classes ['spices'], and
    step 1 deploys kosher salt (aisle 'spices'), so it passes here. What made it wrong
    was the entry's unstated PRECONDITION — "before adding liquid", and step 1 has no
    liquid. Preconditions are not in the schema, so `forward_reference` is what
    actually caught it. This check is the coarser net beside it.
    """
    want = set((entry.get("applies_when") or {}).get("ingredient_classes") or [])
    if not want:
        return ""
    deployed = _deployed_by_step(cook).get(step_number) or set()
    by_id = {i.id: i for i in (cook.ingredients or [])}
    have, classified = set(), 0
    for iid in deployed:
        ing = by_id.get(iid)
        if not ing:
            continue
        cls = _classes_of(ing)
        if cls:
            classified += 1
            have |= cls
    if not classified:
        return ""                          # nothing classifiable — cannot judge
    if have & want:
        return ""
    return (f"step deploys {sorted(have) or 'nothing classifiable'}; "
            f"entry needs one of {sorted(want)}")


# --------------------------------------------------------------------------- #
# The pass
# --------------------------------------------------------------------------- #
def augment_cook(cook: CookMetadata, log: Callable = print,
                 recipe_name: Optional[str] = None, usages: Optional[list] = None) -> CookMetadata:
    """Attach published-KB tips/checks to `cook` IN PLACE (and return it). Best-effort
    + additive — the caller treats a raise as non-fatal. Every attachment is validated:
    kb_id ∈ injected published set (provenance/anti-invention) AND step_index in range."""
    # Project the published KB (lazy imports avoid an import cycle with save_recipe_api).
    import sqlite3
    from save_recipe_api import DB_PATH
    from input.pipeline import cook_kb
    with sqlite3.connect(DB_PATH) as conn:
        cook_kb.ensure_cook_kb_table(conn)
        kb = cook_kb.project_published(conn)
        media_by_id = cook_kb.published_media_by_id(conn)  # code-pulled, model never sees it
    if not kb:
        log("[augment] no PUBLISHED KB entries — nothing to attach")
        return cook
    kind_by_id = {e["id"]: e["kind"] for e in kb}   # the moat set + kind source of truth

    view = _recipe_view(cook, recipe_name)
    out = _call(kb, view, log, usages)

    step_by_num = {s.number: s for s in cook.steps}
    attached = dropped = 0

    def _make(kid: str, text: str):
        # kind AND media are taken from the KB entry by id — the model can't relabel
        # a tip as a check, nor invent a media URL (it only picked the kb_id).
        return Attachment(kb_id=kid, kind=kind_by_id[kid], text=(text or "").strip(),
                          media=media_by_id.get(kid, []))

    # recipe-level (scope=recipe/either)
    seen_recipe = set()
    for a in (out.get("recipe_tips") or []):
        kid = a.get("kb_id")
        if kid not in kind_by_id:
            dropped += 1; log(f"[augment] DROP invented kb_id {kid!r} (recipe-level)"); continue
        if kid in seen_recipe or len(cook.tips) >= _MAX_RECIPE:
            continue
        cook.tips.append(_make(kid, a.get("text"))); seen_recipe.add(kid); attached += 1

    # step-level
    entry_by_id = {e["id"]: e for e in kb}
    for sa in (out.get("step_augments") or []):
        idx = sa.get("step_index")
        step = step_by_num.get(idx)
        if step is None:
            dropped += 1; log(f"[augment] DROP step_augment: step_index {idx} out of range"); continue
        seen = {att.kb_id for att in step.attachments}
        for a in (sa.get("attachments") or []):
            kid = a.get("kb_id")
            if kid not in kind_by_id:
                dropped += 1; log(f"[augment] DROP invented kb_id {kid!r} (step {idx})"); continue
            if kid in seen or len(step.attachments) >= _MAX_PER_STEP:
                continue
            text = (a.get("text") or "").strip()
            # GROUNDING. Provenance (a real kb_id, a real step) says the advice is ours;
            # these say it is true OF THIS STEP. The meatloaf failure had perfect
            # provenance and told the cook to add chicken stock two steps early.
            fwd = forward_reference(text, idx, cook)
            if fwd:
                dropped += 1
                log(f"[augment] DROP {kid} (step {idx}): forward reference to {fwd}")
                continue
            bad = scope_mismatch(entry_by_id.get(kid) or {}, idx, cook)
            if bad:
                dropped += 1
                log(f"[augment] DROP {kid} (step {idx}): out of scope — {bad}")
                continue
            step.attachments.append(_make(kid, text)); seen.add(kid); attached += 1

    log(f"[augment] attached {attached} item(s); dropped {dropped} "
        f"(invented kb_id, out-of-range step, forward ingredient reference, or out of scope)")
    return cook


# --------------------------------------------------------------------------- #
# Redundancy check (deterministic) — run before persist
# --------------------------------------------------------------------------- #
_WORD_RE = re.compile(r"[a-z0-9]+")


def _words(s: str) -> set:
    return set(_WORD_RE.findall((s or "").lower()))


def _near_dup(a: str, b: str, thresh: float = 0.75) -> bool:
    """True if a and b say nearly the same thing. Word-set Jaccard with a high
    threshold so distinct advice is preserved; exact-match for very short texts."""
    wa, wb = _words(a), _words(b)
    if len(wa) < 4 or len(wb) < 4:
        return (a or "").strip().lower() == (b or "").strip().lower()
    union = len(wa | wb)
    return union > 0 and len(wa & wb) / union >= thresh


def dedupe_annotations(cook: CookMetadata, log: Callable = print) -> CookMetadata:
    """The redundancy check before publish. Removes repeated guidance IN PLACE:
    (1) a recipe-level tip whose entry is already attached to a step (the step is
    more specific); (2) near-duplicate TEXT across all tips + step attachments
    (e.g. the same entry attached to two steps with near-identical wording);
    (3) near-duplicate technique_changes. Conservative — high word-overlap
    threshold, so the same technique applied at genuinely distinct moments with
    distinct wording survives."""
    removed = 0
    # (1) recipe tip duplicated by a step attachment of the same entry
    step_kbids = {a.kb_id for s in cook.steps for a in s.attachments}
    keep = [t for t in cook.tips if t.kb_id not in step_kbids]
    removed += len(cook.tips) - len(keep)
    cook.tips = keep

    # (2) near-duplicate annotation text — recipe tips first, then steps in order
    seen: list = []

    def _fresh(text: str) -> bool:
        if any(_near_dup(text, s) for s in seen):
            return False
        seen.append(text)
        return True

    new_tips = []
    for t in cook.tips:
        if _fresh(t.text):
            new_tips.append(t)
        else:
            removed += 1
    cook.tips = new_tips
    for s in cook.steps:
        kept = []
        for a in s.attachments:
            if _fresh(a.text):
                kept.append(a)
            else:
                removed += 1
        s.attachments = kept

    # (3) near-duplicate technique_changes (the rework occasionally restates one)
    tc: list = []
    for ch in cook.technique_changes:
        if any(_near_dup(ch, k) for k in tc):
            removed += 1
        else:
            tc.append(ch)
    cook.technique_changes = tc

    if removed:
        log(f"[augment] redundancy check: removed {removed} duplicate annotation(s)")
    return cook
