# The AI Editor — mediation over the statistical pass

> ## ⚠️ SUPERSEDED 2026-08-12 — the founding premise was wrong
>
> **The selection role is cancelled. What survives is commentary.** Read this
> box before anything below it; §1–§4 argue from an example that measurement has
> since falsified.
>
> This doc was written because the statistical pass ranked an instant-noodle toss
> #1 and dropped Adam Liaw's ramen school, and that looked self-evidently broken.
> We finally pulled the SEMrush demand data for those exact URLs:
>
>     thesaltymarshmallow  Sesame Garlic Ramen Noodles   ranked #1 "wrongly"   9,667
>     adamliaw             Ramen School 001               dropped "wrongly"       105
>
> **92× in favour of the recipe the doc holds up as the mistake.** Adam Liaw's
> whole ramen-school series reads 357 / 210 / 105 / 19 / 4 / 1. The statistical
> pass was not broken on the founding example. It was right, and the human and AI
> reading of it — mine included — was wrong.
>
> The shadow mediation on job 795 says the same thing at scale. Traffic for each
> URL against the verdict I gave it:
>
>     thecozycook      demote → floor      96,775
>     pinchofyum       demote → floor      77,242
>     justonecookbook  demoted #2 → #9     40,238
>     seriouseats      hold #2             16,278
>     fifteenspatulas  demote → floor       2,448
>     joshuaweissman   NOMINATE (rescue)    1,744
>     honestcooking    hold #1  ← my top    1,075
>     bbcgoodfood      demote → floor         866
>     epicurious       NOMINATE (rescue)      120
>
> My number-one pick draws 1,075. The two I sent to the floor draw 96,775 and
> 77,242. The page I argued hardest to rescue — writing that its negative OU was
> "an authority artifact, not a quality signal" — is last in the set at 120, which
> is precisely what its OU predicted. **The ranking I produced is close to
> inverted against demand.**
>
> ### Why, and what it means for the design
>
> The rubric in §4 scores `dish_fidelity`, `method_completeness`, and
> `craft_specificity` — is this really the dish, made properly. That is a coherent
> question. It is not the question the business is asking. We are after traffic
> too: the statistical pass already surfaces what people actually cook, and it
> does that job well. Re-ranking it on authenticity replaces a measurement with an
> AI's taste — and the curator's objection stands on its own even without the
> numbers: **the criteria are subjective, and they are subjective coming from an
> AI.**
>
> I was not uniformly wrong, and the exception is instructive. bbcgoodfood
> ("Cheat's", 866) and fifteenspatulas (2,448) are low-fidelity *and* low-demand,
> so demoting those held up — the arithmetic did over-credit bbcgoodfood's DA-88
> domain. The failure was not "demoting shortcuts". It was that I could not tell
> which shortcuts people actually want, and ranked with full confidence anyway.
>
> ### The revision
>
> **Keep:** the per-recipe LLM read. The `evidence` and `axes` blocks are the
> valuable output and they are *descriptive*, not evaluative — "3 lb split pig
> trotters, 4-hour rolling boil to emulsify the fatback", "eggs simmered exactly
> 6 min and marinated 4–12 hrs", "broth is 700ml store stock + Worcestershire +
> five-spice". That is real editorial copy for a top-10 display, and it is
> checkable against the page.
>
> **Cut:** `verdict`, `ordinal_rank`, and `band` as anything that moves selection.
> The algorithms pick the mix; the AI describes it. The mediation log stays as a
> record, not as an input.
>
> **Residue worth one more look, but weak:** a *factual* identity mismatch is
> different from a quality judgment — cjeatsrecipes' "Spicy Garlic Ramen" has no
> broth at all, so calling it a soup is checkably wrong, not a matter of taste.
> But even that is contested by the data: thecozycook is self-labelled
> American/Asian with Frank's hot sauce and honey-roasted peanuts, and it is the
> single biggest traffic winner in the cohort. So if identity-mismatch survives at
> all it flags for a human, and never demotes on its own.
>
> One caveat on the evidence, stated so it is not over-read: traffic partly
> reflects head-term targeting. "Homemade ramen" is a high-volume generic query;
> "rich and creamy tonkotsu broth from scratch" is niche. The demand winners are
> partly winning by answering the query most people type — which is arguably
> exactly the signal a home-cook product wants, but it is not a pure quality
> measure either.
>
> **Everything below is retained as the record of the original argument.**

**Status: DESIGN — SELECTION ROLE CANCELLED, see the box above.** Nothing here is
built. Written 2026-08-10, out of the ramen pass (job 794) where the statistical
ranking put an instant-noodle toss at #1 and excluded Adam Liaw's ramen school
entirely.

The curator's framing, which is the design:

> we do a statistical first pass, then hand it and the rejections to the ai for
> potential mediation both for and against. we keep the mediation log for
> verification if needed... the mechanism for reconsideration is similar to the
> editors pick at least for adding.. in this case the editor is the ai with the
> power of thumbs up or thumbs down

Cost is explicitly not a constraint: the audit is low volume and runs on every
dish and domain pass.

---

## 1. Why — the ramen evidence

Job 794 ranked by OU exceptionalism. The result:

    #1   thesaltymarshmallow  Sesame Garlic Ramen Noodles     6 ingredients, 6 steps
    #3   seriouseats          Rich and Creamy Tonkotsu Broth  10 / 7
    --   adamliaw             Ramen School 001: Clear Broth   DROPPED, ou -1.04
    --   seriouseats          Miso Butter Ramen               DROPPED, ou -0.43
    --   101cookbooks         Great Vegan Ramen               DROPPED, ou -4.97
    --   tasteofhome          Homemade Ramen NOODLES          DROPPED, ou -3.87

The #1 is an instant-noodle sauce toss. It won because DA 51 / PA 50 punches
above its weight, which is what OU measures. **OU measures link-earning
exceptionalism; it does not measure whether this is the best version of the
dish.** Usually those correlate well enough. On ramen — the dish with the
highest comparison-intent ratio we have measured, 272% — they came apart
visibly, on the one dish where "which is best" is the entire user question.

This is not an argument against OU. Per `docs/recipe-scoring-design.md` and the
settled findings, OU does real work in a dish cohort and PA is a strong
within-site ranker. It is an argument that **the statistical pass is an
admission test, not a verdict.**

---

## 2. Four things verified in the code before designing anything

These constrain the build far more than the concept does.

### 2.1 The reject pool is not persisted — this blocks everything

`dish_rejects` holds **1** row for Ramen. The run dropped 39 candidates:

    2   filter_disallowed        blocklist
    14  is_recipe                collections/listicles, no-struct
    2   MOZ-FAIL                 no score returned
    22  min-OU floor             the class we most want reconsidered
    1   skip-thin                <- the only one that reached the table

The other 38 exist **only in the job's log file**. You cannot mediate over a
pool you did not keep. **Phase 0 is the candidate ledger; the AI is Phase 2.**

### 2.2 Editor's Choice is candidacy, NOT override

This is the most important finding, because it is the mechanism the curator
named. `dish_editors_choice` is a clean (dish, url) junction with a `note`
field — the right shape. But `build_query_batch.py:1353` states the semantics
plainly: pins "surface in the top-N **IFF they rank** — junction-style
membership, not a forced override."

And `_pinned` is **written twice and read nowhere**. A pin gets no special
treatment at any gate: is_recipe, Moz, the OU floor and the top-N truncation
all treat it as an ordinary SERP result.

So mapping "thumbs up" onto today's pin **would not work**. The AI would
promote Adam Liaw, the pin would put him back in the pool, and the same OU
floor would drop him again. A thumbs-up has to be an **override carrying a
recorded reason**, not a re-entry ticket.

(The same hole affects the curator: a deliberate human pin can be silently
dropped by the floor. There is 1 pin in the DB and it is not in
`master_recipes` — n=1, so that is weak as evidence, but the code path is
unambiguous on its own.)

### 2.3 A reject's evidence is not recoverable later

Checked both caches for the OU-dropped URLs. `metabase_url` (Moz) and
`llm_extract_cache` (content) hold the **kept** ramen rows — 27 and 26 — and
**none** of adamliaw, seriouseats/miso-butter, 101cookbooks, recipetineats.
The rejects were dropped before extraction and their Moz scores were not
retained.

**Principle: capture at run time.** At the moment of the drop the pipeline is
holding the URL, title, SERP rank, PA, DA, OU, the drop stage and the drop
reason. All of it is free right then and costs money to reacquire. The ledger
must be written by the run, not reconstructed after it.

### 2.4 `public_scoring.py` already settles most of the star question

It shipped 2026-08-09 as the single chokepoint, and its reasoning changes two of
my instincts:

* **Stars run 3.0 → 5.0 in half steps.** Never below 3, because "the index IS
  the filter" — a recipe we selected ourselves should not be shown a single
  star.
* **Cuts land on the blend VALUE, not on rank.** Deliberate: rank-cutting forces
  20% into each band, so corpus growth silently demotes a stored card. A star
  is meant to read as an absolute claim.
* **Stars freeze when a card is stored**, and a derived artifact must carry its
  inputs or it outlives them.

Two consequences for this design, both good:

1. The AI judges **within** the dish cohort (where LLM comparison is reliable
   and the `identity_card` gives grounding) but must emit an **absolute band
   against a written rubric**. Cohort is the context for judging; the rubric is
   what makes the output absolute. That preserves the existing design instead of
   fighting it.
2. **Thumbs-down means removal from the set, not one star.** That is what keeps
   the 3-star floor honest — the floor only means something if things the editor
   rejects never get a card at all.

---

## 3. The mechanism

Generalize `dish_editors_choice` from a curator-only pin table into a
**verdict** table with an actor. Same junction shape, same normalized-URL join
key, `note` becomes the rationale.

    verdict     actor      effect
    ---------------------------------------------------------------------
    promote     ai|human   enters the top set, OVERRIDING the gate that
                           dropped it (which gate is recorded)
    demote      ai|human   removed from the top set, reason recorded
    hold        ai         agrees with the statistical pass (logged, no-op)
    flag        ai         cannot decide; surfaces to the curator, no effect

A human verdict always outranks an AI verdict on the same URL, and an AI pass
never overwrites one. That is the whole conflict rule.

### The boundary that keeps this safe: overturn judgments, not facts

The AI may reconsider a drop only when the drop was an inference. It may never
overturn an observation.

    OVERTURNABLE (judgment)          NOT OVERTURNABLE (fact)
    min-OU floor      a proxy        filter_disallowed  curator's blocklist
    no-struct         a detection    DROP collection    it IS a listicle
                      failure        skip-thin          it has 1 instruction
    MOZ-FAIL          missing data   fetch failure      no page was retrieved

Most LLM-adjudication failures come from letting the model argue with ground
truth. The OU floor is exactly the right thing to let it argue with: it is a
proxy, and the ramen pass shows it failing.

---

## 4. Scoring — the part the curator flagged as the problem

> the problem is the scoring... how do we score/rank.. maybe by stars (which is
> what we should be doing for the user) with the ai determining the appropriate
> award.

Stars are the right unit: they already exist, they are already the only thing a
user sees, and `public_score()` is already the one place private becomes public.

**Do not ask the model for a number.** LLMs are unreliable at absolute numeric
scoring — compression toward the middle, position bias, run-to-run drift — and
reliable at ordinal comparison and rubric-anchored banding. So:

1. **Rank the cohort ordinally first.** Best ramen to worst, as a list.
2. **Then assign bands, constrained to be monotonic with that order.** A band
   may not contradict the ranking the model just produced.
3. **Every band above the floor must cite evidence from the record** — which
   defining technique is present, which failure mode is addressed — not
   adjectives. This is the `cook_kb` precedent: the model selects and cites, it
   does not invent.

### The rubric — what actually makes a recipe the best version of a dish

Five axes, all checkable against data we already hold:

| axis | grounded in |
|---|---|
| **Dish fidelity** — is this even the dish? | the dish's `identity_card` (already canonical for cohort match) |
| **Method completeness** — does it do the defining work or shortcut it? | `recipeInstructions`, `_cook` when present |
| **Craft specificity** — temps, times, doneness cues, real measurements | `_measurements`, the cook-rework gauntlet's existing checks |
| **Source trust** | the statistical half — PA/DA/OU, adjusted PA |
| **Failure-mode coverage** — does it address the dish's known way of going wrong? | the keyword work: high comparison ratio *tracks* a known failure mode |

That last axis is the one that ties the whole thesis together. Yesterday's
finding was that comparison-intent ratio tracks a **known failure mode** — dry
pork chop, gluey mash, flat ramen broth. The very property that makes a dish
worth ranking is the property the ranking should reward. A ramen recipe that
never addresses a thin, under-gelatinous broth is not the best ramen recipe,
whatever its PA.

Axis 1 alone would have caught the ramen #1: sesame-garlic instant noodles fails
dish fidelity against the Ramen identity card.

### How the award is computed

Keep the axes separate and make the combination explicit:

    statistical pass  -> ADMISSION  (who is considered; the trust floor)
    AI editor         -> AWARD      (the band, within-dish, rubric-anchored)
    disagreement      -> FLAG       (surfaced to the curator, not silently split)

`public_score()` stays the single chokepoint; the editorial band becomes an
input to it rather than a second scale beside it. Two continuous scales side by
side is what that module's own docstring warns against.

---

## 5. The mediation log

One row per (run, url, verdict). It must carry:

* the statistical state at decision time — stage, drop reason, PA/DA/OU, rank
* the verdict, the actor, the rubric axis scores, the **evidence cited**
* model id + prompt version + cost
* whether a human later overrode it, and what they said

Three reasons it earns its keep beyond audit:

1. **Verification** — the curator's stated requirement.
2. **Calibration** — disagreement rate between AI and curator is the metric that
   says whether to widen or narrow the AI's authority.
3. **Training data.** This is a labeled preference dataset: *this beat that, for
   this reason, on this dish*. That is the self-labeling corpus of
   `project_corpus_ml`, and the gauntlet already proved the pattern.

---

## 6. Phasing

**Phase 0 — the candidate ledger.** Persist every drop with stage + reason +
scores, at run time. Nothing else is possible without it, and it is useful
alone: "what did we throw away" is currently a question only a log file can
answer.

**Phase 1 — shadow.** The AI runs, writes verdicts to the log, changes nothing.
One or two dishes, compared against the curator's own read. This is a
calibration step, not a permanent limitation — the point is to see whether the
verdicts are sane before they are load-bearing, and it costs one run.

**Phase 2 — authority.** Promote/demote take effect, every change recorded.
Requires the override plumbing from §2.2 — which also fixes the curator's own
pins being droppable.

**Phase 3 — stars.** The editorial band feeds `public_score()`.

---

## 7. Open questions

* **Does a promote bypass extraction cost?** A promoted reject was never
  extracted, so promoting it means fetching and extracting it then. Fine at
  ~20 candidates per dish; needs a cap.
* **Ramen is arguably two dishes** — from-scratch vs quick bowl. Mediation can
  rank within one set; it cannot decide the set should be split. That is
  `project_dish_variants_membership`, and it may be the deeper fix.
* **Domain runs need a different rubric.** These axes are dish-shaped. A
  publisher harvest is asking "is this publisher's page worth holding", which is
  a different question.
* **Ordinal ranking over how many candidates?** 48 survived to the OU stage on
  ramen. Full ordinal ranking of 48 in one call is within reach; 200 is not.
