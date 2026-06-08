# Recipe Rework Playbook

A step-by-step path to follow when critiquing and regenerating a recipe into the
step-anchored, precise, decision-resolved form we're after. Each phase has a **Goal**,
the **Work**, and a **Gate** — a concrete check that must pass before moving on. The
recurring failure mode is skipping a gate, so the gates are not optional.

## North star

Three promises define "done." Every phase serves them:

1. **No lookback.** While cooking a step, the cook never has to look anywhere else —
   not at an ingredient list, not at a settings panel, not at an earlier step. Everything
   the step needs is in the step.
2. **Every measure is real.** No quantity is a guess, a raw-count proxy, or a hand-wave.
   A number exists for every amount; informal phrases ride alongside a number, never
   instead of it.
3. **No measuring mid-cook.** *Every* ingredient is portioned in the mise en place —
   including the first things into the pan. The method only *acts*; it never introduces a
   quantity. Grammatically: a method step always refers back ("Add **the** 1½ lb ground
   beef"), never declares ("measure **a** ¼ cup"). The definite article is the proof the
   measuring already happened upstream; an indefinite measure in a step is a defect.
4. **The page reflects decisions already made.** If the cook has chosen units, an
   ingredient form, a serving size — the prose is already rewritten to match. Choices flow
   forward; they are never deferred back to the reader.

**Page section order** (mirrors the workflow shop → gather → bundle → make):
**Ingredients → Equipment → Bundles → Instructions.** Section headings are
"Ingredients", "Equipment", "Bundles", "Instructions" (not "…list", not "Bundling", not
"Method"); the Bundles section sits immediately before the instructions.

**Ingredients are one list; Bundles / Shopping / In-order are *views* of it** (group-by +
sort-by), not separate data. Bundles is a view, never a method step — a "do the bundles"
step reintroduces the lookback we're killing; doing the bundles first is implied. Checkbox
state belongs to the ingredient and is shared across views. Checkboxes sit on the left;
autoscroll is on the Instructions only (the views don't scroll).

**A form-variant carries name, amount, prep_verb, *and* aisle/category.** A variant can move
an ingredient between shopping groups (dry basil → Pantry, fresh basil → Produce), so aisle
lives on the variant, not as a fixed field on the ingredient.

**Two kinds of prep.** Regrouping/combining ingredients lives in the Bundles *view*
(implied "do first") — not a step. Standing tasks with a lead time (heat the oven, press
the ricotta, put the water on) *are* instruction steps, ordered by lead time (longest-lead
first — the oven is usually step 1). Portioning that depends on already-cooked components is
a real step, placed where it actually happens in the flow.

**Encoding.** Store data as plain text (a bare "&"); entity-encode only at the HTML render
boundary. Never put an HTML entity into a value rendered via `textContent` — it prints
literally. (Lint: flag any HTML entity inside a `textContent` assignment.)

---

## Phase 0 — Capture & parse

**Goal:** a faithful structured copy of the source, with nothing invented and nothing lost.

**Work:**
- Pull the source to clean text. Extract title, yield, times, ingredients (quantity
  *verbatim*, unscaled), and raw steps.
- Give every ingredient a stable id.
- Record what the source *omits* (yield, technique, rest times) rather than papering over it.

**Gate:** every source quantity is preserved exactly; no ingredient or step has been added
or dropped; missing fields are flagged null, not fabricated.

---

## Phase 1 — Cook it in your head (technique audit)

**Goal:** fix the cooking, not just the copy. Fidelity to the source is *not* required;
fidelity to a good result is.

**Work:** mentally cook the dish and ask where a competent chef would diverge from the
written method. Run the standard omission checklist:
- Is a key technique missing? (pasta finished in the sauce, pasta water reserved, meat
  rested, aromatics bloomed, pan deglazed, dairy tempered.)
- Are doneness cues given, not just times? ("until barely blond," not only "3 min.")
- Is seasoning staged correctly? (season early vs. adjust at the end; account for salty/
  briny components added later.)
- Are safety/quality discards stated? (clams that won't open, bad shellfish, etc.)
- Does anything cook too long/short because steps were written in the wrong order?

**Gate:** you can name every change you're making to the source method and why it produces
a better result. If a change is purely stylistic, it belongs in Phase 7, not here.

---

## Phase 2 — Schedule, not sequence (map the work)

**Goal:** treat the recipe as a project, not a list. Surface concurrency honestly.

**Work:** decompose into tasks, each carrying four attributes:
- **duration** (range is fine)
- **attention:** active (pins the cook) or passive (hands-off — soak, boil, rest, preheat)
- **dependencies:** what must finish first (clams open *before* pasta joins them)
- **resource:** what it occupies (a burner, the oven, the cook's hands)

Then:
- Find the passive windows and pack active prep into them.
- Order tasks so nothing idle-waits; if two run in parallel, they must be *marked* parallel.
- Decide the render: a numbered list with explicit **"Meanwhile —"** flags for a simple
  dish; a parallel-track timeline when concurrency is the whole story or multiple dishes
  share resources.

**Gate (concurrency honesty):** the chosen layout cannot *imply* an order it doesn't mean.
If step 4 runs alongside step 3, the words say so. A linear list that secretly requires
parallelism fails this gate.

---

## Phase 2.5 — Bundling (mise en place)

**Goal:** front-load everything that can be combined before the heat goes on, so the
cooking steps run without prep interruptions and without stress.

**Work:**
- **Scan for co-added ingredients** — anything that enters the same process at the same
  moment is a bundling opportunity. Pre-combine them into one labeled bundle: warm spices
  + bay → **Spices 1**; crushed tomatoes + sugar → **Tomato**; egg whites + feta →
  **Pasta dressing**; nutmeg + salt + pepper → **Béchamel seasoning**.
- **Pre-measure the singles** that go in alone but can be portioned ahead (tomato paste,
  wine, butter, flour, warmed milk) under a **Measured & ready** line. **This includes the
  first things into the pan** — the beef, the oil. The mise holds 100% of the measuring;
  do not exclude an ingredient just because it's used first.
- **Exception — keep separate when combining early does harm.** Do *not* bundle across
  different add-times (tomato paste blooms before the tomatoes; flour goes in after the
  butter), or where pre-mixing curdles/reacts, makes something weep, kills aeration, or
  the amount is judged to taste. State, in one line, why things are and aren't bundled.
- List bundles in a **Bundling / mise en place** block before the method. In the steps,
  keep the components in the prose (no lookback) and tag the bundle in a parenthetical
  ("…crushed tomatoes with sugar (**Tomato**)"). Move the prep *out* of the cooking steps.
- **Definite-article invariant.** Because every amount is set in the mise, the method
  *refers back*, never *introduces*. Every ingredient mention in a step uses "the / your /
  reserved / from step N" ("Add **the** 1½ lb ground beef"). An indefinite or fresh measure
  in a step ("add **a** ¼ cup", "measure **2 Tbsp**") is a defect — it means an ingredient
  escaped the mise. The method becomes pure action.
- Bundles inherit form and unit awareness (Spices 1 follows the whole/ground picker; the
  bay leaf rides along and is always fished out at the end).

**Gate:** every ingredient (incl. the first used) is in the mise; no cooking step performs
prep that could have been done cold; every co-added group is bundled or has a stated reason
it isn't; every ingredient mention in a step is a back-reference, never an introduction;
switching a form/unit updates the bundle too.

---

## Phase 3 — Pin every measure (precision pass)

**Goal:** kill proxies and hand-waves. This is the phase I have historically rushed.

**Work, rule by rule:**
- **Measure the prepared state, not the raw item.** "2 Tbsp slivered garlic," not
  "3 cloves." Raw count survives only as a parenthetical shopping hint
  ("≈ 3 large cloves").
- **No informal measure stands alone.** "a handful," "a ladle," "a thread," "salt it hard,"
  "a drizzle" are banned as the quantity. Replace with a number; keep the phrase only as
  flavor *next to* the number.
- **Salt by weight + concentration.** Give grams and a salinity target (e.g. ~1% / 10 g per
  L of water). For volume, name the brand, because kosher brands differ ~2×.
- **Ranges only when justified.** A range is for genuine variation (clam size, heat
  tolerance, "3–5 min until they open"), not for laziness.
- **Pinches and counts stay honest.** "¼ tsp" or "a pinch (≈0.5 g)"; a thing that doesn't
  convert is marked, not force-converted.

**Gate (every-measure-numeric):** scan every amount. Each is a number or an explicitly
non-convertible count/pinch. Zero bare informal measures remain.

---

## Phase 4 — Variants & dosing

**Goal:** handle ingredients that come in incompatible forms, and amounts judged by taste.

**Work:**
- **Form-variants.** When an ingredient has forms that don't convert (chili: whole / flakes
  / ground / fresh / jarred), store each form's *own* amount **and its own prep verb**
  (crumble / measure out / mince / spoon out). They are independent, not computed from
  each other.
- **Resolve forward.** The cook's chosen form must be written into *every* step that
  touches it — the prep step, the cooking step, the adjust step — verb and amount included.
  Never write "prepare the X (see picker)." That is a lookback and fails the north star.
- **Dose-to-taste.** For anything judged by palate (heat, final salt, acid), split into a
  conservative **base amount** placed early and an **adjust beat** placed *after* the cook
  can first taste the dish. Flag the ingredient `to_taste`.

**Gate:** pick each form in turn and read the whole recipe. Every mention updates (verb +
amount + name). No step says "see above." Every to-taste item has both a base and a
later adjust beat.

---

## Phase 5 — Equipment

**Goal:** the gear is gathered and sized before the cook starts, and never over-specified.

**Work:**
- Derive the equipment each step needs.
- **Size anything where size affects the outcome** (bowls, pots, pans, colanders, serving
  vessels), inferred from the recipe's quantities. Leave size off tools where it's
  irrelevant (spoons, tongs, timers).
- Build the **order-of-need master list:** every piece, deduped, in the order it's first
  used.
- **Reuse by reference.** A tool used again points back to where it was introduced
  ("the same pan, from step 4"); it is not re-introduced.

**Gate (size-on-anything-that-matters):** every size-relevant tool carries a quantity-
derived size; nothing irrelevant is sized; reuses reference their origin step.

---

## Phase 6 — Anchor into steps

**Goal:** assemble the step-anchored method that delivers the no-lookback promise.

**Work:**
- Each step leads with its **equipment**, then the **instruction with amounts inline** —
  ingredients bold, the amount included, woven into the sentence (not a separate list).
- An ingredient introduced here shows its amount; an ingredient reused from earlier is a
  bold noun with no repeated number.
- One action per step — unless two actions are genuinely one motion (add + cover + steam),
  in which case merge them.
- Write **transitions:** each step hands to the next ("while they soak…," "the clams cook
  in the time the pasta has left").

**Gate (no-lookback):** read each step in isolation. It states its gear, its amounts, and
its action without requiring any other part of the page. Any "see the list / see above"
is a failure.

---

## Phase 7 — Copy pass

**Goal:** the prose a cook trusts, not prose that's pleased with itself.

**Work:**
- Cut clichés ("taste like seawater," "X waits for no one," "marry the…," "no hiding in…").
- One image per idea. Watch for motif fatigue — a vivid word ("glossy," "briny," "the sea")
  used twice goes dull; keep the best instance, cut the echoes.
- Prefer the plain, specific, slightly bossy line over the decorative one ("barely blond,
  never browned" beats "kissed by gentle heat").
- Bold discipline: **bold = an ingredient in play**; the amount is part of the bold; verbs
  and prose stay unbolded. Step titles are short and imperative.

**Gate:** no stock phrase survives; no signature image appears more than once; the bolding
is consistent.

---

## Phase 8 — Delivery

**Goal:** the dish lands, and the writing frames it.

**Work:**
- A **headnote** that sets expectation and the one-line philosophy of the dish.
- A real **finish:** how to plate, the serving vessel (warmed?), the timing ("serve at
  once"), what *not* to add (no cheese on vongole).
- A short **cook's note** for the thing a good cook says at the table.

**Gate:** the last step ends at the table, not at the stove; serving temperature and vessel
are specified; the close earns its place (no filler).

---

## Phase 9 — Validation gauntlet

**Goal:** mechanical gates that ship-block a bad recipe. Run *all* of them, every time.
Prefer deterministic checks over self-review — I have missed these by eye.

- [ ] **Unit consistency.** No single rendered line carries both an imperial and a metric
      token. The other system lives only behind the toggle. Count/pinch (`convertible:false`)
      exempt. (Lint: strip toggle data + scripts, regex each line for imperial∧metric.)
- [ ] **Every measure numeric.** No bare "handful/ladle/thread/drizzle/salt-hard."
- [ ] **Mise-complete.** Every ingredient appears in the mise/bundling, including the first
      used; nothing is first introduced in a method step.
- [ ] **Definite-article.** Every ingredient quantity in a method step is a back-reference
      (the / your / reserved / from step N); no indefinite or freshly-introduced measures.
- [ ] **No lookback.** No "see the list," "form picker above," "as above" in any step.
- [ ] **Size on anything that matters.** Per Phase 5 gate.
- [ ] **Form-variants resolve forward.** Switching any picker rewrites every mention.
- [ ] **To-taste staged.** Base amount + later adjust beat for every palate-judged item.
- [ ] **Concurrency honest.** Parallel work is marked; the layout implies no false order.
- [ ] **Reuse referenced.** Repeated tools/ingredients point to their origin, not re-listed.
- [ ] **Prep bundled.** Do-ahead prep is in a labeled mise en place block, tagged in the steps; no cooking step holds prep that could have been done cold.

**Gate:** every box checked. A single failure blocks the regenerate.

---

## Output contract (what the data model must carry)

The phases above are only enforceable if `RecipeDoc` stores the right things. Minimum:

- **Amounts as two faces** (imperial + metric) with a `convertible` flag; the two faces may
  differ in detail (salt brand note), not just number.
- **Prepared-quantity** as the source of truth per ingredient; raw count demoted to an
  optional `shopping_hint`.
- **Form-variants:** a list of `{form, amount, prep_verb, name}` per variable ingredient,
  amounts independent.
- **`to_taste`** flag plus a second step-binding for the adjust beat.
- **Tasks with `duration`, `attention` (active/passive), `depends_on`, `resource`** — so the
  list, the timeline, and a "start-by" time computed backward from serve are three renders
  of one schedule.
- **Equipment** with `size_matters`, quantity-derived `size`, `category` (for product
  lookup), and `reused_from_step`.

If a phase can't be satisfied because the model can't hold the data, the model is the bug.
