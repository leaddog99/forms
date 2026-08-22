# The Soundness Defect Pass — prompt spec

**Status:** DRAFT, 2026-08-22. Nothing built. The prompt below is the deliverable of the
2026-08-22 scoring-enhancement session; the measurement that justifies it is in that
day's session log (the structural thinness pass over all 5,657 winners).

**Naming:** it is a *defect pass*, its output is a *defect report*. Not "the gauntlet"
(taken — corpus-ML accept/reject), not "grading" (taken — OU exceptionalism), not
"rubric" (taken — the per-dish canon design in `docs/dish-rubrics.md`).

---

## 1. What it is, and the boundary it must never cross

One LLM call per selected winner that answers a single question:

> **Is this recipe, as stored, materially complete and internally consistent — and if
> not, what exactly is wrong, quoted from the text?**

It exists because the selection pipeline never reads the instructions. OU, PA, DA,
traffic, the blend — all judge the page's *standing*; none can see that step 4 uses a
marinade no step made. The 2026-08-22 measurement: a naive structural "thin" flag
catches 20.1% of winners, but judgment samples showed those are overwhelmingly innocent
(terse ≠ thin; "combine all the marinade ingredients" ≠ unused ingredients). Code
cannot separate thin-bad from terse-good. That separation is judgment — this pass.

**The boundary (from the AI-editor cancellation, 2026-08-12, and dish-rubrics §7):**

* It emits **no score, no letter, no rank — no scalar of any kind**. A thing you
  cannot `ORDER BY` cannot quietly become a selector.
* Every claim is a **defect with a verbatim quote** from the recipe. Auditable in ten
  seconds, true or false. Rankings from taste are unfalsifiable; defects are not.
* It may **disqualify for cause**, never demote or reorder. A disqualified winner takes
  the same path as a failed save: the reserve backfills, the blend's ordering is never
  touched. It is `MIN_OU_SCORE`'s sibling — a floor on the procedure instead of the
  authority.
* **Taste is out of scope by construction.** "Too simplistic" (the Mississippi Pot
  Roast: packet mixes, structurally complete, demand-selected) is an *editorial*
  observation for the commentary layer, never a defect. A defect must name what a
  competent cook cannot resolve, not what a good cook would disdain.

## 2. The prompt

House pattern: `extract/identity_card.py` — dedicated module (`extract/defect_pass.py`),
ordered tool_use schema, `_SYSTEM_PROMPT` hashed to `DEFECT_PROMPT_VERSION`. **Model is
Sonnet, not Haiku** — decided 2026-08-22 on the six-trap calibration set: Haiku kept
inventing defects through two rounds of targeted prompt fixes (demanded a grinding step
against an ingredient line reading "seeds removed and ground"; a cooling step against
"Spread over cooled cake") and inflated severity to critical. Sonnet: zero false
positives on the traps AND full recall on two synthetically broken recipes (deleted
meatball-forming step → critical+disqualify; injected time contradiction → major). Property order in the schema is load-bearing (facts before verdict),
same as the identity card: the model must enumerate defects BEFORE it answers
`disqualify`, so the verdict is forced to follow from the evidence.

### System prompt

```
You are auditing a recipe for MATERIAL DEFECTS — problems that would leave a
competent home cook unable to follow it as written. You are a proofreader of
procedure, not a food critic.

You are NOT judging:
- how good the finished dish would taste
- whether the recipe is sophisticated, traditional, or "worthy"
- brevity. A complete recipe can be two sentences. Cocktails, salads and
  simple sauces are often honestly written in 2-3 short steps. Terse is not
  a defect. ONLY missing/contradictory information is.
- style, storytelling, or anything about the source website

RESOLVE REFERENCES BEFORE FLAGGING. Recipes routinely say "combine all the
marinade ingredients" or "add the remaining dressing ingredients" without
naming each one. If a reasonable cook can resolve the reference from the
ingredient list and context, it is NOT a defect. Step names/section labels
(e.g. a step named "Frosting") are part of the context. Flag a group
reference ONLY when it genuinely cannot be resolved — e.g. two identical
"mix all ingredients" steps over one undivided list, with no way to tell
which ingredients belong to which.

DEFECT CATEGORIES (use exactly these):
- missing_step        an action the recipe depends on but never states
                      (dough is shaped "after resting" but no rest exists;
                      a component is used but never made)
- unused_ingredient   a listed ingredient no step uses or plausibly covers
                      via a resolvable group reference
- unmade_ingredient   a step uses something neither listed nor produced by
                      a prior step
- broken_order        a step depends on the result of a LATER step
- contradiction       two statements that cannot both be followed
                      (temperatures, times, quantities, pan sizes)
- unresolvable_reference  a group/section reference that cannot be resolved
                      (see above — flag ONLY after trying to resolve it)
- truncation          the text visibly breaks off: a step ends mid-sentence,
                      or the recipe plainly stops before the dish is made

SEVERITY:
- critical   the dish cannot be completed as written
- major      a competent cook must guess something consequential
- minor      a competent cook will notice, be briefly confused, and recover

EVIDENCE IS MANDATORY AND VERBATIM. Every defect must carry a quote copied
EXACTLY from the recipe text (the step or ingredient line it concerns).
If you cannot quote it, you cannot flag it.

DO NOT INVENT DEFECTS. Most published recipes that reached this audit are
sound. An empty defect list is the expected, normal outcome. Do not
manufacture minor defects to appear thorough. Do not pad.

disqualify = true ONLY when at least one critical defect, or defects that
together mean the recipe as stored cannot produce the dish. Missing garnish
detail is not disqualifying. When unsure, disqualify = false and let the
defects speak.

Output ONLY through the submit_defect_report tool.
```

### Tool schema (property order is the reasoning order)

```json
{
  "name": "submit_defect_report",
  "input_schema": {
    "type": "object",
    "properties": {
      "resolved_references": {
        "type": "array", "items": {"type": "string"},
        "description": "Group/collective references you found AND resolved (e.g. 'remaining marinade ingredients -> spices in lines 2-11'). Listing these first forces the resolution attempt."
      },
      "defects": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "category":  {"enum": ["missing_step","unused_ingredient","unmade_ingredient","broken_order","contradiction","unresolvable_reference","truncation"]},
            "severity":  {"enum": ["critical","major","minor"]},
            "evidence":  {"type": "string", "description": "VERBATIM quote from the recipe"},
            "explanation": {"type": "string", "description": "One sentence: why a competent cook is stuck"}
          },
          "required": ["category","severity","evidence","explanation"]
        }
      },
      "disqualify": {"type": "boolean"},
      "confidence": {"enum": ["high","medium","low"],
        "description": "LOW when quantities/yield are missing or the text looks truncated — i.e. when absence of evidence is not evidence of soundness"}
    },
    "required": ["resolved_references","defects","disqualify","confidence"]
  }
}
```

### User message

Name, yield, the ingredient list verbatim (with step `name` labels where present —
the section-name fix of 2026-08-22 makes these carry "Frosting Instructions"-style
context), the numbered instruction steps verbatim. NO source URL, NO publisher, NO
scores — the model must not know whose recipe it is (blind, per the ChatGPT thread's
one unambiguously right rule).

## 3. Code checks the model — the free honesty layer

`evidence` being verbatim makes hallucinated defects *mechanically detectable*: a
validator confirms each `evidence` string is a substring of the recipe text
(whitespace-folded). A defect whose quote does not appear is dropped and logged —
`defect_hallucinated` — before anything is stored. The model judges; code verifies
the citations. (Same boundary as dish-rubrics §5, pointed the other way.)

Cheap structural pre-checks stay in code, not in the prompt: `n_ingredients <= 2`
(the marker that caught #7545 — 1 hit corpus-wide, perfect precision) becomes a
save-time warning independent of this pass.

## 4. Storage and wiring

* Stamped as `_soundness` on the master row: `{prompt_version, model, checked_at,
  resolved_references, defects[], disqualify, confidence}`. Keyed journal copy on
  `url_normalized` (survives delete-and-replace, same rule as the activity log and
  recipe_rubric_score).
* **Shadow first, AI-editor style**: run over existing winners with `applied=0`.
  Nothing renders, nothing gates.
* Gate wiring (LATER, after calibration passes): in the batch save loop, a
  `disqualify=true` result routes the URL to the same reserve-backfill path as a
  failed save. `overturnable=1`, candidate-ledger style — a curator reversal is data.

## 5. Review that scales: calibrate per prompt version, verify per row in code

Per-row human review does not scale and is not the design. The load divides:

* **Per row, forever, in code (free):** the verbatim-evidence check. Every defect's
  quote must appear in the recipe text or the defect is dropped as hallucinated —
  and `disqualify` is only HONORED when a `critical` defect *survives* that check.
  A disqualification therefore always ships with machine-verified receipts; nothing
  gates on an uncited claim, ever.
* **Per prompt version, once, by a human (~30 rows):** a sampled precision audit —
  do the quoted defects actually describe what the quote shows? Target >90% on
  `major`+`critical`. Pass it and that `prompt_version` is calibrated; no further
  routine review. Change the prompt (or model) and the audit re-runs — the
  `prompt_version` hash makes "which calibration covers this row" a join, not a
  memory. (Known traps for the audit set: the shawarma "combine remaining marinade
  ingredients" idiom, terse tabbouleh, the pre-fix coconut cake as true positive.)
* **Ongoing, passive:** disqualifications are `overturnable` data. A curator can
  overturn any one they happen to see; overturn *rate* per prompt_version is the
  drift alarm that triggers an off-cycle re-audit — review by exception, not by
  queue.
* **Repeatability, scripted:** same recipe 3-5 runs at temp 0.2; the defect SET
  (category + evidence) should be stable. If a recipe alternates between 0 and 2
  major defects across runs, the pass is noise and must not gate anything. This is
  a script, not a review.

No correlation-with-traffic test — deliberately. Soundness is orthogonal to demand by
design; the validation is citation-precision, not prediction.

## 6. Cost

Sonnet, one call per winner, ~$0.01/call — ~$0.30 per 20-40-winner batch,
~$60 for the full 5.7k-winner corpus (run that once, deliberately). Piggybacking the fields onto the main extraction
call (ChatGPT's suggestion) was considered and declined for v1: the extraction call
must stay blind to nothing (it needs the page), while this call must stay blind to
provenance — separate calls keep the blinding honest, and the identity card is
precedent that a second save-time Haiku call is acceptable.

## 7. What was deliberately left out (do not re-add without re-arguing)

* **The 0-100 dimension scores** (formula_logic, technique, outcome_potential…) from
  the ChatGPT proposal. The verifiable ones collapse into the defect categories above;
  `outcome_potential` and `dish_fit` are taste-laundering (§13's "an AI cannot know
  what tastes better") and dish_fit belongs to the rubric design where canon and
  citations make it honest.
* **The BEST weighted formula and all weights.** Invented constants, validated by
  nothing. Refused.
* **Percentile-within-finalists.** Manufactures spread among 30 pre-selected rows
  whose true quality range may be narrower than the scorer's noise.
* **The pairwise/Bradley-Terry ranking of the top 8-10.** The one genuinely good
  scoring idea in the thread — parked, not killed: if built, its output is prose
  commentary on the algorithmic top-10, never a sort key.
