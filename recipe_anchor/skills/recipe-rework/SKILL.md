---
name: recipe-rework
description: >
  Operating procedure for turning a recipe into the step-anchored, bundled, mise-complete
  format. Load and follow whenever a recipe (URL, pasted text, file, or an existing recipe
  record in the app) is to be critiqued, reworked, regenerated, step-anchored, or
  validated. Triggers: "rework/fix/improve this recipe", "regenerate", "critique",
  "make this step-anchored", "turn this into our format", "validate this recipe", or any
  recipe dropped in for processing. Run every phase and pass every gate before returning.
version: 1.1
---

# Recipe Rework — operating procedure

Full rationale lives in `recipe-rework-playbook.md`. This file is the callable, condensed
form. Run phases 0–9 in order. Each ends in a **gate** that must pass; the recurring
failure is skipping a gate, so do not.

> **In the Code application:** the LLM emits only the recipe *data object* (+ judgment
> prose) — never CSS/JS/template/product catalog, which are fixed app assets. The renderer
> builds the page and the timeline from the object; the §gauntlet runs as deterministic
> code/tests and gates the build. Right-size models per phase (cheap for extraction + lints,
> frontier for technique/copy/bundling judgment). Edit the object as diffs; never regenerate
> whole output. See `recipe-optimization-handoff.md` for the integration plan and data model.

## North star (resolves any ambiguity)
1. **No lookback** — the cook never leaves the current step for info.
2. **Every measure is real** — a number for every amount; informal phrases only beside a number.
3. **No measuring mid-cook** — every ingredient (incl. the first used: beef, oil) is portioned in the mise; the method only acts. It refers back ("Add the 1½ lb beef"), never introduces ("measure a ¼ cup"). The "the" is proof the measuring happened upstream.
4. **Decisions already made** — chosen units/forms/yield are written into the prose, never deferred to a control.

**Page section order:** Ingredients → Equipment → Bundles → Instructions (shop → gather → bundle → make). Headings "Ingredients"/"Equipment"/"Bundles"/"Instructions"; the Bundles section sits immediately before the instructions.

**Ingredients are ONE list; Bundles / Shopping / In-order are VIEWS of it** (group-by + sort-by), not separate sections — Bundles is a *view*, never a method step (a "do the bundles" step reintroduces lookback; doing bundles first is implied). Checkbox state keys to the ingredient and is shared across views. Checkboxes on the LEFT; autoscroll on Instructions only (views don't scroll).

**A form-variant carries name, amount, prep_verb, AND aisle** — a variant can move an ingredient between shopping groups (dry basil = Pantry, fresh = Produce); put aisle on the variant.

**Two kinds of prep.** Regrouping/combining ingredients lives in the Bundles *view* (implied "do first"), NOT a step. Standing tasks with a lead time (heat oven, press ricotta, boil water) ARE instruction steps, ordered by lead time (longest first — oven usually step 1). Portioning that depends on cooked components is a real step placed where it occurs.

**Encoding.** Store data as plain text (bare "&"); entity-encode only at the HTML render boundary. Never put HTML entities into a value rendered via textContent (it prints them literally). Lint: flag any HTML entity inside a textContent assignment.

## Procedure

**0 · Capture.** Parse to structured data; preserve source quantities verbatim; flag omissions null, never invent.
→ *Gate:* nothing added/dropped; missing fields null.

**1 · Cook it in your head.** Source fidelity is NOT required; result fidelity is. Audit for missing technique (reserve liquid, finish in sauce, rest, bloom, deglaze, temper), doneness cues, seasoning staging (account for salty components added later), safety discards, and wrong-order cooking.
→ *Gate:* every change to the method is named and justified by a better result.

**2 · Schedule, not sequence.** Decompose into tasks with `duration`, `attention` (active/passive), `depends_on`, `resource`. Pack active prep into passive windows; mark parallel work "Meanwhile —"; choose render (list for simple, parallel-track timeline when concurrency or multi-dish dominates).
→ *Gate (concurrency honesty):* the layout never implies an order it doesn't mean.

**2.5 · Bundling (mise en place).** Scan for ingredients added to the same process at the same moment and pre-combine into labeled bundles (Spices 1 = spices + bay; Tomato = tomatoes + sugar; Pasta dressing = whites + feta); pre-measure singles under "Measured & ready" — INCLUDING the first things into the pan (the beef, the oil): the mise holds 100% of the measuring. EXCEPTION — keep separate when combining early harms: different add-times (paste blooms before tomatoes; flour after butter), curdle/weep/react, lost aeration, or to-taste. List bundles before the method; in steps keep components in the prose and tag the bundle in parens; move prep OUT of cooking steps. Definite-article invariant: the method refers back, never introduces — every ingredient mention uses "the/your/reserved/from step N" ("Add the 1½ lb beef"); an indefinite/fresh measure in a step is a defect. Bundles are form- and unit-aware; state in one line why things are/aren't bundled.
→ *Gate:* every ingredient (incl. first used) is in the mise; no step holds prep doable cold; co-added groups bundled or reasoned; every step mention is a back-reference, never an introduction.

**3 · Pin every measure.** Measure the PREPARED state ("2 Tbsp slivered garlic"), demote raw count to a parenthetical shopping hint. Ban bare informal measures (handful/ladle/thread/drizzle/"salt it hard") — number required, phrase optional beside it. Salt by weight + salinity %, name brand for volume. Ranges only for real variation. Pinches/counts marked non-convertible, never force-converted.
→ *Gate (every-measure-numeric):* zero bare informal measures remain.

**4 · Variants & dosing.** For incompatible forms (e.g. chili: whole/flakes/ground/fresh/jarred) store each form's own amount AND its own prep verb (crumble/measure out/mince/spoon out), independent. The chosen form must be written into EVERY step that touches it (verb + amount + name) — never "prepare the X (see picker)." For palate-judged amounts (heat, final salt, acid): a conservative base early + an adjust beat placed AFTER the cook can first taste; flag `to_taste`.
→ *Gate:* switching any form rewrites every mention; each to-taste item has base + later adjust.

**5 · Equipment.** Derive per step. Size anything where size affects outcome (bowls, pots, pans, colanders, serving vessels) from the quantities; leave size off where irrelevant (spoons, tongs, timers). Build the deduped order-of-need master list; reuse points back to the origin step, never re-introduced.
→ *Gate (size-on-anything-that-matters):* size-relevant tools sized from quantities; reuses referenced.

**6 · Anchor into steps.** Each step: equipment first, then instruction with amounts INLINE (ingredients bold, amount included in the bold, verbs/prose unbolded). Reused ingredient = bold noun, no repeated number. One action per step unless two are one motion (add+cover+steam). Write transitions that hand each step to the next.
→ *Gate (no-lookback):* each step read alone states its gear, amounts, and action; no "see above/list/picker."

**7 · Copy pass.** Cut clichés ("taste like seawater", "X waits for no one", "marry the…", "no hiding in…"). One image per idea; no motif fatigue (a vivid word twice goes dull — keep the best, cut echoes). Prefer plain/specific/bossy over decorative. Bold = ingredient in play; titles short and imperative.
→ *Gate:* no stock phrase survives; no signature image repeats; bolding consistent.

**8 · Delivery.** A headnote (expectation + one-line philosophy). A real finish (plate, vessel, warmed?, "serve at once", what NOT to add). A short cook's note that earns its place.
→ *Gate:* last step ends at the table; serving temp/vessel specified; no filler close.

**9 · Validation gauntlet — run ALL, every time. Prefer deterministic checks over eyeballing.**
- [ ] Unit consistency — no line mixes imperial + metric; other system behind the toggle; count/pinch exempt.
- [ ] Every measure numeric — no bare informal measures.
- [ ] Mise-complete — every ingredient (incl. first used) is in the mise; nothing first introduced in a step.
- [ ] Definite-article — every step quantity is a back-reference (the/your/reserved/from step N); no fresh/indefinite measures.
- [ ] No lookback — no "see list / picker above / as above" in any step.
- [ ] Size on anything that matters.
- [ ] Form-variants resolve forward — switching a picker rewrites every mention.
- [ ] To-taste staged — base + later adjust beat.
- [ ] Concurrency honest — parallel work marked; no false implied order.
- [ ] Reuse referenced — repeated tools/ingredients point to origin.
- [ ] Prep bundled — do-ahead prep in a labeled mise en place block, tagged in steps; no cooking step holds prep doable cold.
→ *Gate:* every box checked. One failure blocks the regenerate.

## Output contract (the model must carry this or a gate is unenforceable)
- Amounts as two faces (imperial + metric) + `convertible`; faces may differ in detail (salt brand), not only number.
- Prepared-quantity as source of truth; raw count demoted to `shopping_hint`.
- Form-variants: list of `{form, amount, prep_verb, name}`, amounts independent.
- `to_taste` flag + a second step-binding for the adjust beat.
- Tasks with `duration`, `attention`, `depends_on`, `resource` → list / timeline / start-by are three renders of one schedule.
- Equipment with `size_matters`, quantity-derived `size`, `category`, `reused_from_step`.
- If a phase can't be satisfied because the model can't hold the data, **the model is the bug.**

## Render targets (current)
Step-anchored HTML page (units toggle, form-picker that resolves forward, foot-of-step
checkbox that scrolls onward, sized equipment list with picks). Parallel-track timeline
as an alternate execution view. Both are renders of one `RecipeDoc`.
