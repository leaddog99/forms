# Cook sub-steps v2.1 — separating MISE from METHOD (the organic re-plan)

**Status: DESIGN (2026-06-17).** Continuation of sub-steps v2 ([[project_recipe_anchor]] / [[project_cook_voice]], shipped 2026-06-16). v2 split each *step* into memory-sized voice chunks. v2.1 fixes the layer above: the boundary between the **mise** (the pre-step lay-out) and the **method** (the steps) currently overlaps, so the spoken mise and the step sub-steps say the same thing twice. Implement AFTER the v2.2 re-rework batch finishes (this is an engine + prompt-version change). Grounded in `docs/procedural-instruction-research.md`.

---

## 1. The problem (observed, concrete)

The first real cook surfaced it and it's why **auto-mise was disabled** (`TIP_OFFER`/auto-mise notes, 2026-06-12 / 06-15 logs): the spoken mise walk and an early method step duplicate each other.

Chicken Milanese (real reworked `_cook`), **step 1**:

```
STEP 1 "Mix seasoned salt":
  instruction: "In a small bowl, stir together the {bundle:bnd_seasoned_salt} until evenly blended."
  (deploys bundle bnd_seasoned_salt; no heat, no duration)
```

But `bnd_seasoned_salt` is **already a bundle** — the mise. So the per-cluster mise walk says *"Seasoned salt: onion powder, garlic powder, salt, pepper"*, and then **step 1 re-states the same combine** (*"stir together the seasoned salt"*). The cook hears the spice blend twice. Multiply across spice blends, dressings, slurries, breading mixes → the mise feels redundant, which is exactly the "it duplicated steps like 'combine the spices'" report.

**Root cause:** the rework conflates two different kinds of "combining," and emits one of them in *both* places:
- **cold pre-combining** done at the counter before any heat (stir a spice blend, whisk a dressing, make a cornstarch slurry) — this is **mise**, and a `Bundle` already captures it (`combine_note`, `make_ahead`);
- **combining-as-cooking** that must happen at a moment, often with heat/time (whisk eggs into hot custard, deglaze, emulsify a hot sauce) — this is **method**, a real step.

Today both land as method `steps`, and the cold one *also* becomes a bundle → overlap. The fix is to put each kind in exactly one place.

---

## 2. The principle: mise vs method

- **MISE** = everything done **cold, at the counter, before heat**: gather, measure, peel/chop/grate prep, and **pre-combine**. Output = labeled, measured, ready-to-deploy **bundles** (+ put-asides). The mise holds 100% of the measuring (already an invariant) **and 100% of the cold combining** (the v2.1 addition).
- **METHOD** = **transformations**: applying heat / time / technique that changes the food's state (sear, simmer, fry, bake, temper, emulsify-while-hot, reduce). Steps **consume** bundles by name and state the *action*, never re-combine or re-measure them.

**Boundary test** for each source instruction: *does it change the food's state via heat/time/technique, or is it just assembling cold components that could be done up front and held?*
- Cold assembly that can be held → **mise** (a bundle).
- State change, or assembly that must happen at a specific moment → **method** (a step).

**Gray-zone rule** (write into the prompt): if the combining can be done anytime up front and set aside, it's mise; if it must happen at a particular point in the sequence (final toss at serve, whisk into a hot base, fold to preserve aeration), it's a method step. Final cold assembly (toss salad with dressing) stays a method step because it's a serve-moment action consuming the dressing bundle — it adds the greens, it doesn't *make* the dressing.

---

## 3. What changes

### 3a. Data model — minimal (one optional field, no restructure)

Bundles already carry pre-combining (`combine_note`, `make_ahead`, `members[]` with amounts). Two small, optional additions so the mise *reads as an action* and the renderer/validator can phrase + check it:

- **`Bundle.mise_action: Optional[str]`** — the prep verb phrase for the spoken/printed mise line: `"stir together"`, `"whisk until emulsified"`, `"make a slurry with"`. The mise walk renders `"<label> — <mise_action> <members>"`; absent ⇒ `"<label>: <members>, measured and ready"` (a pure gather cluster). This is the line that *replaces* the deleted combine step.
- **`Bundle.kind: str = "gather"`** — `gather` (measured, not combined) | `combine` (pre-combined cold) | `catchall` (the "Measured & ready" group). Lets the renderer phrase the mise line and lets a validator target `combine` bundles. Derivable from `combine_note`/`mise_action` presence, so optional — but explicit is cleaner for the gate in 3c.

No change to `steps`, `reserved`, or the sub-step shape from v2. This stays a **prompt + discipline** change with two helper fields, not a schema overhaul ([[feedback_single_path]] — one model, richer, not a parallel structure).

### 3b. Rework prompt (`cook_rework._system_prompt`)

Add a **planning instruction up front**: classify each source instruction as mise or method before emitting.
- Cold pre-combining → a `Bundle` with `combine_note` + `mise_action` (+ `make_ahead` when it holds). It does **not** also appear as a step.
- The `steps` array contains **only transform actions**. A step deploys a pre-combined bundle via `{bundle:id}` and states the action on it (*"Bloom the {bundle:spices} in the oil, ~30 seconds"*), never re-combining it.
- **Explicit negative rule:** *Do NOT emit a method step whose only action is combining ingredients that are already in a bundle. If the only thing to do is "stir these together," that's the bundle (mise), not a step.*

### 3c. Validators (`cook_validators.py`) — one new, lenient gate

**`v_no_redundant_combine`** — fire only on the tight case so cold final-assembly (salad toss) is safe:
- a step whose `instruction` matches a combine verb (`combine|mix|stir together|whisk together|blend together`),
- **AND** deploys exactly one ingredient-ref and it's a **bundle** (not a bundle + other ingredients),
- **AND** has no `duration_minutes` and no heat/temperature cue,
⇒ FAIL: "step N is a pure mise-combine of an already-bundled group — move it into the bundle (`combine_note`/`mise_action`)."

Two refs (dressing **+** greens) or any duration/heat ⇒ not flagged (it's a real method step). Lenient by construction; the gauntlet's job is mechanical integrity, and this one is narrow.

### 3d. Renderer (`forms/cook.html`)

- **Mise walk reads the bundles as the prep:** each bundle line speaks `<label> — <mise_action> <members w/ amounts>` (combine bundles) or `<label>: <members>, measured and ready` (gather bundles); make-ahead flagged ("you can do this ahead"). This is where the combining is *heard*, once.
- **Re-enable auto-mise at fresh hands-free start.** The reason it was turned off — duplication with step 1 — is gone once mise-combines no longer appear as steps. Keep it gated to Talk-on + a non-trivial mise (≥1 combine bundle or ≥N items); the "mise" command stays for opt-in replay.
- Steps already reference bundles by name (`{bundle:id}`) and v2 sub-steps speak the action — no change needed there; they're now guaranteed disjoint from the mise.

---

## 4. Guarantees this locks in

- **Measure-once** (already an invariant via `v_mise_complete` + `v_definite_article`): every amount lives on a bundle member; steps back-reference by name. v2.1 makes the mise the **sole** place both measuring *and* cold combining happen.
- **No overlap:** mise walk = gather + measure + cold-combine; method = transforms only. Disjoint by construction + enforced by `v_no_redundant_combine`.
- **Cluster naming:** bundles get real labels so steps refer to them by name ("the spice blend", "the dressing") instead of re-listing members — the v2 "refer to clusters by name, no amount repetition" goal, now also true across the mise/method seam.

---

## 5. Migration & sequencing

- Prompt + schema helper change ⇒ bump `REWORK_PROMPT_VERSION` → **v2.3**.
- Re-rework everything via **`scripts/rerework_cooks.py`** (built 2026-06-17, idempotent/resumable) — it re-does every `_cook` not at the current version. So v2.1 ships *for all recipes* by re-running that batch.
- **Order:** do this only AFTER the in-flight v2.2 batch completes (editing the engine / bumping the version mid-batch breaks its idempotency check).
- Old `_cook` blocks without `mise_action`/`kind` still render (fields optional) — the mise walk falls back to the current "measured & ready" phrasing.

## 6. Open questions (decide at build time)

- **`kind` derived vs explicit?** Could infer `combine` from `combine_note`/`mise_action` presence and skip the field. Leaning explicit for the validator's clarity — cheap.
- **Make-ahead surfacing** — should make-ahead bundles get their own "Prep ahead" section on screen, separate from the at-cook mise? Probably yes, later; out of scope for v2.1.
- **The 3 known pre-existing gauntlet failures** (Frico definite-article, Courgette risotto reuse-referenced, Portokalopita mise-complete) are independent of v2.1 — address separately; the v2.2 batch will re-surface them as SKIPPED.
