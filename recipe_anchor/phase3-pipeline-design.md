# Phase 3 design — the cook-rework pipeline (recipe → `_cook`)

**Goal:** take a CURATED master recipe and emit a validated `_cook` block (cook_model.py),
gated by the §5 validators (cook_validators.py). Master recipes first; tips KB stubbed.

## SHARED componentry (both surfaces) — not a TBOTB feature
The whole cook-rework stack — `_cook` model, validators, the rework pipeline/job, the
renderer (Phase 4), and the voice loop (deferred) — is **shared engine componentry consumed
by BOTH surfaces**, never bolted onto one:
- **TBOTB / corpus (discover & judge):** reworks its curated master recipes — the showcase
  cook experience for the recipes it surfaces.
- **BCC / local (possess & use):** reworks a user's OWN saved recipe — the cook experience
  for the recipe they're about to make.
Same engine, same `_cook` block on the same `RecipeModel`, same gauntlet, same render, same
voice. "Curated/master first" is the first CONSUMER, not the only one. This is the
portable-package thesis (one engine, many instances/surfaces) + the single-canonical-path
rule (convert any recipe to the model once; every interface is a uniform render of it). It
lives in the canonical core (`forms/`), surface-agnostic — the trigger/permission differs per
surface, the engine does not. Keep the pipeline free of any surface-specific assumption.

## The reshaping insight: we don't start from scratch
The prototype did `extract → anchor` (2 LLM passes) because it began from raw HTML. **The app
has already captured the recipe** — so Phase 0 (capture) and most of Phase 3 (precision) are
already done:
- `recipeIngredient` / `recipeInstructions` — the parsed recipe.
- **`_measurements`** — per-ingredient prepared quantity + imperial/metric conversions +
  ingredient weights (from enrich.measurement). **This IS the two-faced `CookAmount` source.**

So **code builds the amounts; the LLM only judges.** This is the cost architecture made real:
the unit conversion (error-prone, and the thing the unit-consistency gate guards) comes from
`_measurements`, not the model — so amounts are unit-consistent *by construction*, and the
frontier tokens go only to judgment.

## Pipeline shape: 2 LLM passes + code, with a repair loop

```
PRE (code)   load recipe + _measurements -> rework input
                (ingredients w/ prepared qty + both unit faces, raw steps, yield, _master/_scoring)
PASS 1 (OPUS)  COOK + MISE JUDGMENT  — the deepest judgment, forced-tool emit:
                · technique audit (missing technique/doneness/seasoning-staging/safety/order)
                  -> technique_changes[] (each change named + justified)
                · schedule: tasks w/ duration/attention/depends_on/resource
                · bundling: which co-add (combine_note) vs separate (excluded_reason);
                  "Measured & ready" catch-all; put-asides -> reserved[]
                · per-ingredient: to_taste, form_variants, shopping_hint, first_step
                · tips/checks sourced from the STUBBED KB injected into the system prompt
PASS 2 (SONNET) ANCHOR + EQUIPMENT + COPY — realization, forced-tool emit:
                · anchored step instructions w/ inline {ingN}/{amt}/{bundle} tokens +
                  definiteness markers; one action per step; transitions
                · equipment: sized-where-it-matters (from quantities), order-of-need, reuse refs
                · copy pass + delivery: headnote, finish, cook's note
ASSEMBLE (code) stitch ingredients(+_measurements faces) + bundles + reserved + steps +
                equipment -> CookMetadata; compute/verify first_step; sort the four lists
VALIDATE (code) run_all() -> CookValidatorReport
REPAIR (OPUS, only if failures)  feed the failures + offending slice back -> targeted fix
                -> re-validate (max 1-2 loops)
PERSIST (code)  if passed (or repaired): write _cook on the recipe + the report; log tokens.
                if still failing: do NOT persist; log failures + flag for review.
```

Pass 1 is judgment-dense but its OUTPUT is a compact structured plan (not prose), so Opus
holds it. If it strains in practice, split into 1a (technique + schedule) and 1b (bundling +
variants) — same tiers, more focused retries. Start at 2; split only if needed.

## Where it runs
A new **`cook_rework` job** (jobs infra), out-of-process like `dish_refresh`:
`register_handler("cook_rework", _handle_cook_rework_job)`.
- Trigger: `POST /recipes/{id}/cook-rework` (master-only, perm-gated) → enqueue + spawn the
  runner (`python -m jobs exec`), live-streamable log.
- Why a job: multi-pass + frontier + ~30–60 s + real tokens → out-of-process (no event-loop
  block), a cost/timing record, a live log, retry, and it scales to "rework a chapter's top
  recipes" later.
- The recipe form gets a "Rework for cooking" action (master recipes) + shows `_cook` status
  (reworked? validators passed? when?).

## Model tiers (reuse the app's anthropic + forced-tool pattern)
- Pass 1 (judgment): **claude-opus-4-8**, `tool_choice` forced, `effort:"high"`.
- Pass 2 (realization): **claude-sonnet-4-6**, forced tool.
- Repair: **opus** (only when the gauntlet fails).
- Mechanical (conversion, assembly, validation): **code, $0**.
Rough cost: ~2 frontier-ish calls + 1 sonnet per recipe; logged per pass. Curated-only keeps
volume low.

## The stubbed tips KB (this build)
A hand-authored `cook_tips_kb.py` — ~15–20 entries keyed by technique/ingredient, each a
short success TIP and/or doneness CHECK:
`{"saute_garlic": {"tip": "...", "check": "blond, not brown — browned garlic turns bitter"},
  "rest_meat": {...}, "bloom_spices": {...}, "salt_pasta_water": {...}, ...}`.
Injected into Pass 1's system prompt as reference knowledge; the model attaches relevant
entries to `step.tip` / `step.check`. Proper KB (DB-resident, retrieval, sourced from the
success/failure research) is a later design — this is the seed (real moat lives there).

## Versioning / idempotence
`_cook.schema_version` + a `rework_prompt_version` hash (like `EXTRACT_PROMPT_VERSION`) stamped
on the block, so a stale rework (prompt changed, or the recipe edited since) is detectable and
re-runnable. Don't re-run unless the recipe changed or the prompt version bumped. Edits later
are diffs on `_cook`, never full regen.

## Open decisions (confirm before building)
1. **2 passes** (opus judgment → sonnet realization) + code + 1 repair loop — vs 3-pass
   (split copy/delivery out) or 4-pass (max-tiered)? *Rec: start at 2.*
2. **Drive amounts from `_measurements`** (code builds CookAmount faces; LLM never converts)?
   *Rec: yes — cheaper, accurate, unit-consistent by construction.* Fallback: LLM converts
   only ingredients `_measurements` didn't cover.
3. **On validator failure: 1 auto-repair (opus diff) then flag** — vs flag immediately? *Rec:
   1 repair then flag.*
4. **Tips KB:** hand-authored `cook_tips_kb.py` (~15–20 entries) injected into Pass 1. *Confirm.*
5. **Runs as a `cook_rework` job** triggered from the recipe form (master only). *Confirm.*
6. **First test recipes:** two structurally different masters (one stovetop/timing — e.g.
   vongole; one bake/rest — e.g. lasagna/pastitsio) to see which gates fire + where rules are
   overfit (handoff §7.6).
