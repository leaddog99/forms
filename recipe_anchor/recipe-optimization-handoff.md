# Handoff — Recipe Optimization ("Step-Anchored + Bundling") capability

**For:** Claude Code, working inside the existing (well-developed) application.
**From:** a prototyping session that worked outside the app. Everything here is a
*specification of intent*, not code to paste. The app already has scaffolding — models,
renderer, product source, pipeline. **Adapt every idea below to what already exists; do
not recreate things the app has.** Where this brief and the app disagree on structure,
the app wins; carry over the *rules*, not my file layout.

---

## 0. What this capability is

Turn any recipe into a **step-anchored** cook view where the cook never looks away from the
current step and never measures mid-cook. Two renders of one data model: a step list and a
parallel-track timeline. The intelligence is in a reusable rework procedure + a set of hard
validators.

Prototype artifacts from the session (reference only, readable, not authoritative over the
app): a step-anchored HTML page, a parallel-track timeline, a small FastAPI package
(`recipe_anchor/`) with models/prompts/pipeline/renderer, the `recipe-rework-playbook.md`,
and a callable `SKILL.md`. Mine them for wording and structure; re-implement against the
app's real abstractions.

---

## 1. North star (the binding invariants)

1. **No lookback.** While on a step, the cook needs nothing else on screen — gear, amounts,
   and action are all in the step.
2. **No measuring mid-cook.** *Every* ingredient is portioned in the mise en place. The
   method only *acts*; it never introduces a quantity.
3. **The page reflects decisions already made.** Chosen units, ingredient forms, and yield
   are already written into the prose — never deferred to a control or a list.

These three resolve any ambiguity. #2 is the one we under-applied; see §3.

---

## 2. The rework procedure (phases, each with a hard gate)

Port these as the pipeline's processing stages. Judgment phases (1, 2, 2.5, 7, 8) are LLM
work; the rest should be deterministic where possible.

- **0 Capture** — parse source faithfully; preserve quantities verbatim; flag omissions
  null, invent nothing.
- **1 Technique audit** — fix the *cooking*, not just the copy (source fidelity not
  required). Check for missing technique, doneness cues, seasoning staging, safety
  discards, wrong-order cooking. Name every change and why.
- **2 Schedule, not sequence** — tasks carry `duration`, `attention` (active/passive),
  `depends_on`, `resource`. Pack active prep into passive windows; mark parallel work;
  choose render (list vs timeline). *Gate: layout never implies a false order.*
- **2.5 Bundling (mise en place)** — see §3.
- **3 Precision** — prepared-quantity is the source of truth; raw count demoted to a
  shopping hint; no bare informal measures; salt by weight + salinity; ranges only for real
  variation; pinches/counts marked non-convertible.
- **4 Variants & dosing** — form-variants store `{form, amount, prep_verb, name}`
  independently and resolve forward into *every* step; to-taste = conservative base + a
  later adjust beat placed after first taste.
- **5 Equipment** — size only where size affects outcome, inferred from quantities; dedupe
  to an order-of-need list; reuse references its origin step.
- **6 Anchor** — equipment first, then instruction with amounts inline (see §3 for the
  grammar); one action per step unless genuinely one motion; transitions hand step to step.
- **7 Copy** — cut clichés; one image per idea; plain/specific over decorative; bold = an
  ingredient in play; amount included.
- **8 Delivery** — headnote; real finish (plate, vessel, temp, "serve at once", what not to
  add); a short earned cook's note.
- **9 Gauntlet** — see §5. Run all, every time, deterministically.

Full rationale: `recipe-rework-playbook.md`. Condensed callable form: the `SKILL.md`.

---

## 3. Bundling + the definite-article rule (the part to get right)

**Bundling.** Scan for ingredients added to the same process at the same moment and
pre-combine them into one labeled bundle, assembled before cooking:
- e.g. **Spices 1** = warm spices + bay leaf; **Tomato** = crushed tomatoes + sugar;
  **Pasta dressing** = egg whites + feta; **Aromatics** = onion + garlic.
- **Everything else is pre-measured too**, under a **Measured & ready** group — *including
  the first things into the pan* (the beef, the ¼ cup oil). The mise holds 100% of the
  measuring. (This is the opportunity we missed: do not exclude an ingredient from the mise
  just because it's used first.)
- **Exception — keep separate when combining early harms:** different add-times (tomato
  paste blooms before the tomatoes; flour after the butter), curdle/weep/react, lost
  aeration, or to-taste amounts. Always state, in one line, why things are/aren't bundled.

**The definite-article invariant (new, important).** Because every amount is established in
the mise, the **method must always refer back, never introduce**:
- Use **"the"**: "Add **the** 1½ lb ground beef," "warm **the** ¼ cup olive oil." The
  definite article is the proof the measuring already happened upstream.
- An indefinite quantity in a method step ("add **a** ¼ cup…", "measure **2 Tbsp**…") is a
  **defect** — it means an ingredient escaped the mise. This is mechanically checkable: flag
  any method-step quantity not preceded by a back-reference ("the", "your", "reserved",
  "from step N").
- Same principle already applies to reuse ("the same pan, from step 4") and to bundle tags;
  this just extends it to *all* ingredients.

Net effect: the method becomes pure action. Steps read "Add the beef… stir in the
Aromatics… pour in the wine," with amounts present (inline, bold) but grammatically marked
as already-portioned.

---

## 4. Data-model requirements (map onto the app's existing models)

The procedure is only enforceable if the model carries:
- **Amount** = two faces (imperial + metric), `convertible` flag; faces may differ in
  detail (salt brand note), not only number. tsp/Tbsp treated as system-neutral.
- **Ingredient**: `prepared_quantity` as source of truth; `shopping_hint` for raw count;
  `to_taste` flag.
- **FormVariant**: list of `{form, amount, prep_verb, name}`, amounts independent; the chosen
  form resolves into every reference.
- **Bundle**: `{label, members[], combine_note, make_ahead}`; members may be ingredients or
  other measured singles; bundle is form/unit-aware; a `combine_ok` reason or an
  `excluded_reason`.
- **Task**: `duration`, `attention` (active/passive), `depends_on`, `resource` — so list,
  timeline, and a backward-scheduled "start by <serve time>" are three renders of one
  schedule.
- **Equipment**: `size_matters`, quantity-derived `size`, `category` (for product lookup),
  `reused_from_step`.
- **Step reference**: ingredient mentions are *references* to mise entries (carry a
  definiteness marker), not fresh declarations.

If a phase can't be satisfied because the model can't hold the data, **the model is the
bug** — extend the model, don't weaken the phase.

---

## 5. Validators (build as code; cheaper and more reliable than LLM self-review)

A recipe that fails any check does not ship. We missed unit-mixing twice by eye — these
must be mechanical.
- [ ] **Unit consistency** — no rendered line carries both an imperial (lb, oz, cup, qt, in,
      °F) and a metric (g, kg, ml, L, cm, °C) token; tsp/Tbsp neutral; `convertible:false`
      exempt.
- [ ] **Definite-article / no-introduce** — every ingredient quantity in a method step is a
      back-reference (the/your/reserved/from step N); no indefinite or fresh measures.
- [ ] **Every measure numeric** — no bare informal measures (handful/ladle/thread/drizzle).
- [ ] **No lookback** — no "see list / picker above / as above" in any step.
- [ ] **Mise-complete** — every ingredient appears in the mise; nothing is first introduced
      in a method step.
- [ ] **Size where it matters** — size-relevant equipment carries a quantity-derived size.
- [ ] **Form resolves forward** — switching a form rewrites every mention + prep verb.
- [ ] **To-taste staged** — base amount + a later adjust beat.
- [ ] **Concurrency honest** — parallel work marked; no false implied order.
- [ ] **Reuse referenced** — repeated tools/ingredients point to origin.
- [ ] **Bundling** — co-added groups are bundled or carry a stated reason they aren't.

---

## 6. Cost architecture (why this is worth doing in-app, not by hand)

The expensive failure in the prototype was **regenerating the whole page on every edit**
(~12–15 K output tokens/turn; rebuilt 3× for one recipe). The fix is structural:
- **LLM emits only the data** (a small recipe object, ~2–3 K tokens) + judgment prose. The
  CSS, JS, template, and product catalog are **fixed app assets**, never model output.
- **Deterministic renderer** builds both the page and the timeline from the data.
- **Validators are code**, ~free, and gate the build.
- **Right-size models per phase**: cheap/fast model for extraction + lints; frontier model
  only for technique audit, copy, and bundling judgment.
- **Edit as diffs**, never regenerate, once data and presentation are separate.
Principle: the model should produce *only the parts that need judgment*; everything
mechanical (layout, catalog, conversion, validation) is code.

---

## 7. Suggested first moves for Claude Code (adapt to the repo)

1. Reconcile the data-model requirements (§4) against the app's existing recipe models;
   extend, don't duplicate.
2. Implement the §5 validators as functions/tests over the app's recipe object; wire into
   the build so a failing recipe can't ship. Start with unit-consistency and
   definite-article — they're the highest-value and fully mechanical.
3. Fold the §2 phases into the existing extraction/anchoring prompt(s); keep the judgment
   phases LLM, push the rest to code.
4. Render the step view and the timeline from one object; reuse the app's existing
   template/asset system rather than the prototype's inline CSS/JS.
5. Keep product lookup the way the app already does it — category in, products out — so
   e-commerce data never enters an LLM prompt.
6. Run two structurally different recipes (one stovetop/timing-driven like vongole, one
   bake/rest-driven like pastitsio) through the pipeline and check which gates fire, to find
   where the rules are overfit to the prototypes.
