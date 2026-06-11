"""cook_model.py — the `_cook` block: a recipe reworked into a step-anchored,
mise-complete cook experience.

This is the DATA the rework pipeline emits and the cook-view renderer consumes —
the structural half of the recipe-anchor capability (recipe_anchor/START_HERE.md +
recipe-optimization-handoff.md §4). The LLM produces ONLY this data; the CSS / JS /
template / product catalog are fixed app assets, never model output. The renderer
turns one CookMetadata into THREE ingredient views (Shopping / In-order / Bundles)
and two step renders (list / timeline) — so views are computed, never stored.

It hangs off RecipeModel as `_cook`, PARALLEL to the faithful schema.org capture
(recipeIngredient / recipeInstructions stay intact for provenance). The technique
audit deliberately diverges from the source — `_cook` is our optimized overlay,
not the original.

The binding invariants (handoff §1), all mechanically checkable downstream:
  1. No lookback — a step carries its own gear + amounts + action.
  2. No measuring mid-cook — every amount is portioned in the mise; the method only
     ACTS. A method-step quantity must be a back-reference ("the 1½ lb beef"), never
     an introduction ("a ¼ cup…").
  3. The page reflects decisions already made — chosen unit system, ingredient form,
     and yield are written into the prose, not deferred to a control.
"""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Amounts — two faces, one truth
# --------------------------------------------------------------------------- #
class CookAmount(BaseModel):
    """A quantity in both unit systems, display-ready with cooking roundings
    ('450 g', not '453.6 g'). `convertible=False` for unit-neutral measures
    (tsp/Tbsp/pinch, COUNTS) and vessel-as-measure ('a handful') — those show
    identically in both systems and are EXEMPT from the unit-consistency gate.
    The two faces may differ in more than number (e.g. a salt-brand note)."""
    imperial: str
    metric: str
    convertible: bool = True

    @classmethod
    def same(cls, text: str) -> "CookAmount":
        """A unit-neutral amount (count, pinch, vessel-as-measure, a back-reference
        like 'the reserved water') — same text in both systems, non-convertible."""
        return cls(imperial=text, metric=text, convertible=False)


# --------------------------------------------------------------------------- #
# Ingredients (the PURCHASABLE thing) vs USES (its deployments)
# --------------------------------------------------------------------------- #
class FormVariant(BaseModel):
    """A buyable form of an ingredient (dry basil vs fresh basil). Each form
    carries its OWN amount, prep verb, name AND aisle (dry=Pantry, fresh=Produce)
    — and the chosen form must resolve forward into EVERY step that uses it."""
    form: str = Field(..., description="e.g. 'dried', 'fresh', 'bagged shredded'.")
    name: str
    amount: CookAmount
    prep_verb: Optional[str] = Field(None, description="e.g. 'crumbled', 'minced'.")
    aisle: Optional[str] = Field(None, description="Shopping aisle for THIS form.")


class CookIngredient(BaseModel):
    """A purchasable ingredient. Its DEPLOYED amounts live on uses (step
    ingredients / bundle members) — this is the shopping-grain entity. The
    Shopping view groups uses under it and shows each use's 'amount — what it's
    for'; it does NOT auto-sum (the cook decides — e.g. fresh vs bagged mozz)."""
    id: str = Field(..., description="Stable id referenced by steps/bundles, e.g. 'ing_clams'.")
    name: str
    # prepared_quantity is the SOURCE OF TRUTH (2 Tbsp / 10 g minced); raw count
    # (3 cloves) is a buying hint only — handoff §3 / START_HERE.
    shopping_hint: Optional[str] = Field(
        None, description="Raw count for BUYING (e.g. '3 cloves', '1 medium onion'). "
                          "The real measure is the prepared amount on the uses.")
    aisle: Optional[str] = None
    to_taste: bool = Field(False, description="True => conservative base now + a later "
                                              "adjust beat after first taste.")
    form_variants: List[FormVariant] = Field(default_factory=list)
    note: Optional[str] = None
    # First-appearance index: the step number where this ingredient first enters
    # the cook sequence (directly, or via the bundle it's in). The ingredients
    # list is kept SORTED by this so the mise lines up in the exact order it'll be
    # reached for — a living progress indicator. Enforced by the appearance-order
    # validator. (Order of APPEARANCE/use, not alphabetical or shopping order.)
    first_step: Optional[int] = None


# --------------------------------------------------------------------------- #
# Bundles — pre-combined mise (a VIEW that is also planned data)
# --------------------------------------------------------------------------- #
class BundleMember(BaseModel):
    """One ingredient's USE inside a bundle (its deployed amount here)."""
    ingredient_id: str
    amount: CookAmount
    label: str = Field(..., description="How it reads in the bundle, e.g. 'crushed tomatoes'.")
    prep_verb: Optional[str] = None


class Bundle(BaseModel):
    """Co-added ingredients pre-combined into one labeled bundle, assembled before
    cooking (handoff §3). The catch-all 'Measured & ready' group — everything not
    otherwise bundled, INCLUDING the first things into the pan — is also a Bundle,
    so the mise holds 100% of the measuring. Always state, in one line, WHY things
    are/aren't combined."""
    id: str
    label: str = Field(..., description="e.g. 'Spices 1', 'Aromatics', 'Measured & ready'.")
    members: List[BundleMember] = Field(default_factory=list)
    combine_note: Optional[str] = Field(
        None, description="One line: why these combine OK (co-added, same moment).")
    excluded_reason: Optional[str] = Field(
        None, description="If an item is kept SEPARATE: why (blooms first, curdles, "
                          "lost aeration, to-taste). Mutually informative with combine_note.")
    make_ahead: bool = False
    # The step where this bundle is DEPLOYED (the moment it goes into the pan).
    # Bundles list is kept SORTED by this — bundles line up in deploy order so the
    # cook reaches for them in sequence. Appearance-order validator enforces it.
    first_step: Optional[int] = None


# --------------------------------------------------------------------------- #
# Equipment — order of need, sized only where it matters
# --------------------------------------------------------------------------- #
class CookEquipment(BaseModel):
    id: str
    name: str
    # Defaulted (not required) so an occasional LLM omission on a nested array
    # item can't hard-fail the whole rework — the gauntlet + a repair pass catch
    # real problems; a missing category just falls back to "other" (product
    # lookup degrades gracefully) and missing size_matters to False.
    size_matters: bool = Field(False, description="True for bowls/pots/pans/colanders/vessels; "
                                                  "False for spoons/tongs/timers.")
    size: Optional[CookAmount] = Field(None, description="Required when size_matters; "
                                                         "inferred from quantities.")
    category: str = Field("other", description="Product-catalog key: bowl|colander|pot|pan|spoon|"
                                               "knife|board|tongs|timer|serving|other.")
    why: Optional[str] = Field(None, description="One line: why this size, tied to quantities.")
    reused_from_step: Optional[int] = None
    # First-need index: the step that first calls for this tool. Equipment list is
    # kept SORTED by this (order of need, deduped) — gear lines up in the order
    # it's grabbed. Appearance-order validator enforces it.
    first_step: Optional[int] = None


# --------------------------------------------------------------------------- #
# Steps — anchored: gear first, then action with already-portioned amounts inline
# --------------------------------------------------------------------------- #
class StepIngredient(BaseModel):
    """An ingredient as USED in a step — a reference to a mise entry, not a fresh
    declaration. `definiteness` is the proof the measuring happened upstream: a
    method-step amount must read 'the'/'your'/'reserved'/'from step N'."""
    ingredient_id: str
    amount: CookAmount
    label: str = Field(..., description="How it reads inline, e.g. 'clams', 'cornmeal'.")
    definiteness: str = Field("the", description="Back-reference marker: the|your|reserved|"
                                                 "from-step|bundle. Indefinite => mise leak (defect).")
    reused_from_step: Optional[int] = Field(
        None, description="1-based step where this was introduced, if reused not added fresh.")


class StepEquipment(BaseModel):
    equipment_id: str
    reused_from_step: Optional[int] = None


class MediaRef(BaseModel):
    """A curated media reference (video / image) carried by a KB entry. CODE-SOURCED,
    never model-emitted: the augment model only selects a kb_id; code stamps the
    entry's vetted media onto the attachment, so a URL can never be hallucinated.
    Rights stance (image_policy / DL-16): reference + attribute + EMBED, never rehost."""
    kind: str = Field(..., description="video | image")
    provider: Optional[str] = Field(None, description="youtube | vimeo | ... (None = generic).")
    url: str
    title: Optional[str] = None
    source: Optional[str] = Field(None, description="Attribution, e.g. 'Culinary Institute of America'.")
    start_seconds: Optional[int] = Field(None, description="Optional deep-link into a video.")
    note: Optional[str] = None


class Attachment(BaseModel):
    """A KB-sourced tip or check attached by the AUGMENT pass (3c-b). `kb_id` is
    the PROVENANCE — it must trace to a PUBLISHED cook_tips_kb entry, validated
    mechanically so the model can't invent advice (the moat). `text` is the
    entry's claim/action reworded for THIS recipe; `kind` is fixed by the entry.
    `media` is the entry's curated video/image refs, stamped by code (not the
    model) at augment time — so a quiet ▶ link can render with the guidance."""
    kb_id: str
    kind: str = Field(..., description="tip | check — inherited from the KB entry, not chosen.")
    text: str = Field(..., description="The entry's guidance contextualized to this recipe.")
    media: List[MediaRef] = Field(default_factory=list)


class CookStep(BaseModel):
    number: int
    name: str = Field(..., description="Short imperative title, e.g. 'Boil the spaghetti'.")
    instruction: str = Field(
        ...,
        description="Anchored instruction with inline tokens the renderer expands:\n"
                    "  {ingN}            -> Nth entry of this step's `ingredients`, bold + amount\n"
                    "  {amt:IMP|MET}     -> a standalone non-ingredient measure (time/temp)\n"
                    "  {amt:X}           -> single value when it doesn't convert\n"
                    "  {bundle:ID}       -> a bundle reference ('the Aromatics')\n"
                    "Pure ACTION: every ingredient amount is a back-reference, never introduced.")
    ingredients: List[StepIngredient] = Field(default_factory=list)
    equipment: List[StepEquipment] = Field(default_factory=list)
    # Schedule (handoff §2): three renders of one schedule (list / timeline /
    # backward 'start by <serve time>').
    duration_minutes: Optional[int] = None
    attention: str = Field("active", description="active (hands-on) | passive (unattended).")
    depends_on: List[int] = Field(default_factory=list,
                                  description="Step numbers that must finish first.")
    resource: Optional[str] = Field(None, description="Contended resource (oven, stovetop, "
                                                      "cook's hands) for honest concurrency.")
    # KB-sourced tips/checks attached by the AUGMENT pass (3c-b), each carrying a
    # kb_id (provenance, validated). ≤2 per step. The bare tip/check below are
    # DEPRECATED — the rework no longer emits them; kept so a _cook produced
    # before 3c-b still renders.
    attachments: List[Attachment] = Field(default_factory=list)
    tip: Optional[str] = Field(None, description="DEPRECATED — superseded by attachments.")
    check: Optional[str] = Field(None, description="DEPRECATED — superseded by attachments.")


# --------------------------------------------------------------------------- #
# Put-asides (reserved items) — set aside during cooking, used later
# --------------------------------------------------------------------------- #
class ReservedItem(BaseModel):
    """A PUT-ASIDE: something set aside mid-cook and used later — reserved pasta
    water, browned meat resting off-heat, a handful of cheese held for the top.
    Not purchasable (a cooking intermediate, often derived from an ingredient or a
    step's output), so it lives here, not in `ingredients`. Ordered by
    `created_step` and shown HELD from created_step → consumed_step, so put-asides
    line up as living progress indicators too — the cook can see what's waiting on
    the counter to go back in, and in what order it returns."""
    id: str = Field(..., description="Stable id; a consuming StepIngredient reuses it.")
    label: str = Field(..., description="e.g. 'reserved pasta water', 'browned beef'.")
    amount: Optional[CookAmount] = None
    from_ingredient_id: Optional[str] = Field(
        None, description="Source ingredient, if this is a held portion of one.")
    created_step: int = Field(..., description="Step that sets it aside (where it appears).")
    consumed_step: Optional[int] = Field(
        None, description="Step that uses it back. None if it's served as-is / a garnish.")
    note: Optional[str] = None


# --------------------------------------------------------------------------- #
# The block
# --------------------------------------------------------------------------- #
class CookValidatorReport(BaseModel):
    """Result of the §5 gauntlet — recorded so a save can refuse an un-passed
    rework and the UI can show which gates fired."""
    passed: bool = False
    failures: List[str] = Field(default_factory=list)
    ran: List[str] = Field(default_factory=list)


class CookMetadata(BaseModel):
    """The reworked, step-anchored form of a recipe — `recipe._cook`. Parallel to
    (not a replacement for) the faithful schema.org capture.

    APPEARANCE-ORDER INVARIANT: `ingredients`, `bundles`, `equipment`, and
    `reserved` are ALL stored in order of first appearance in the cook sequence
    (each carries a step index — `first_step` / `created_step`). Nothing is in
    alphabetical or shopping order here; the lists mirror the order the cook will
    reach for things, so the mise, gear, bundles, and put-asides can be laid out
    left-to-right and consumed in sequence — living progress indicators. The
    appearance-order validator (phase 2) enforces the sort + that nothing is
    referenced before it appears (reuse points BACK via reused_from_step)."""
    schema_version: int = 1
    headnote: Optional[str] = Field(None, description="Short framing — what this dish is, "
                                                      "what the rework optimized.")
    recipe_yield: Optional[int] = None
    total_time_minutes: Optional[int] = None

    # All four kept SORTED by first-appearance step (see invariant above).
    ingredients: List[CookIngredient] = Field(default_factory=list)
    bundles: List[Bundle] = Field(default_factory=list)
    equipment: List[CookEquipment] = Field(default_factory=list)
    reserved: List[ReservedItem] = Field(default_factory=list)
    steps: List[CookStep] = Field(default_factory=list)

    finish: Optional[str] = Field(None, description="The real finish: plate, vessel, temp, "
                                                    "'serve at once', what NOT to add.")
    cooks_note: Optional[str] = Field(None, description="One short earned note.")
    # Recipe-level (scope=recipe) KB tips attached by the augment pass — guidance
    # that isn't tied to one step (season in layers, hold a finisher). ≤3.
    tips: List[Attachment] = Field(default_factory=list)
    # Phase-1 technique audit: name every change to the COOKING and why (source
    # fidelity is not required — we fix the dish, not just the copy).
    technique_changes: List[str] = Field(default_factory=list)

    validators: Optional[CookValidatorReport] = None
    # Stamped by the rework job on persist — lets a stale rework (prompt changed,
    # or the recipe edited since) be detected + re-run.
    rework_prompt_version: Optional[str] = None
    reworked_at: Optional[str] = None

    def ingredient_by_id(self, iid: str) -> Optional[CookIngredient]:
        return next((i for i in self.ingredients if i.id == iid), None)
