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


# --------------------------------------------------------------------------- #
# Equipment — order of need, sized only where it matters
# --------------------------------------------------------------------------- #
class CookEquipment(BaseModel):
    id: str
    name: str
    size_matters: bool = Field(..., description="True for bowls/pots/pans/colanders/vessels; "
                                                "False for spoons/tongs/timers.")
    size: Optional[CookAmount] = Field(None, description="Required when size_matters; "
                                                         "inferred from quantities.")
    category: str = Field(..., description="Product-catalog key: bowl|colander|pot|pan|spoon|"
                                           "knife|board|tongs|timer|serving|other.")
    why: Optional[str] = Field(None, description="One line: why this size, tied to quantities.")
    reused_from_step: Optional[int] = None


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
    # Research annotations (KB stubbed for now) — interspersed tips + checks.
    tip: Optional[str] = Field(None, description="A success tip gleaned from research "
                                                 "(seasoning staging, technique). KB-sourced later.")
    check: Optional[str] = Field(None, description="A doneness/failure-avoidance CHECK "
                                                   "('curds set when it coats the spoon').")


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
    (not a replacement for) the faithful schema.org capture."""
    schema_version: int = 1
    headnote: Optional[str] = Field(None, description="Short framing — what this dish is, "
                                                      "what the rework optimized.")
    recipe_yield: Optional[int] = None
    total_time_minutes: Optional[int] = None

    ingredients: List[CookIngredient] = Field(default_factory=list)
    bundles: List[Bundle] = Field(default_factory=list)
    equipment: List[CookEquipment] = Field(default_factory=list)
    steps: List[CookStep] = Field(default_factory=list)

    finish: Optional[str] = Field(None, description="The real finish: plate, vessel, temp, "
                                                    "'serve at once', what NOT to add.")
    cooks_note: Optional[str] = Field(None, description="One short earned note.")
    # Phase-1 technique audit: name every change to the COOKING and why (source
    # fidelity is not required — we fix the dish, not just the copy).
    technique_changes: List[str] = Field(default_factory=list)

    validators: Optional[CookValidatorReport] = None

    def ingredient_by_id(self, iid: str) -> Optional[CookIngredient]:
        return next((i for i in self.ingredients if i.id == iid), None)
