# Acquisition logic — a study before the next patch

**Status: ANALYSIS. No code changed.** Written 2026-08-13 at the curator's request, after a
177milkstreet run spent 54 unblocker calls to save 1 recipe and the instinct was "we are
patching this and will pay the price later." That instinct is correct. What follows is the
input patterns, the code walked against them, and where the model — not the code — is wrong.

---

## 1. The measurement that frames everything

Recent publisher runs, from their own logs:

| publisher | extract attempts | saved | SKIP-THIN | unblocker calls | calls per save |
|---|---|---|---|---|---|
| cooking.nytimes.com | 130 | 100 | 15 | 110 | **1.1** |
| cooking.nytimes.com | 5 | 5 | 0 | 5 | 1.0 |
| dianekochilas.com | 36 | 30 | 3 | 35 | 1.2 |
| dianekochilas.com | 36 | 30 | 3 | **2** | 0.07 |
| 177milkstreet.com | 17 | 1 | 8 | 54 | **54** |
| 177milkstreet.com | 0 | 0 | 0 | 20 | **∞ (20 calls, 0 saves)** |

**cooking.nytimes.com and 177milkstreet.com carry the identical flags** — `paywall=1`,
`fetch_strategy=unblocker`, `render_required=1` — and differ by a factor of ~50 in cost per
result. Whatever the flags are describing, it is not the thing that determines whether a run
works.

---

## 2. The input patterns that actually exist

Derived from the ledger's reject taxonomy and the per-publisher outcomes, not from
imagination:

| # | pattern | signature | example | current handling |
|---|---|---|---|---|
| 1 | **Open + JSON-LD** | direct fetch, `has_jsonld=True` | most of the corpus (291 domains are `plain/0/0/0`) | correct, cheap |
| 2 | **Open, no JSON-LD** | direct fetch, phrase score passes | curiousprovence, walterpurkisandsons | correct |
| 3 | **JS-rendered** | thin stub direct, full text rendered | delish (learned via probe) | correct *after* learning; pays a doomed fetch first, forever |
| 4 | **Anti-bot** | direct fails, unblocker succeeds | fearlesseating, lecreuset | correct |
| 5 | **Gated but obtainable** | unblocker returns the *whole recipe* | **cooking.nytimes.com** (110 calls → 100 saves) | works, but the flag says "paywall" which implies it shouldn't |
| 6 | **Gated and unobtainable** | unblocker returns a stub; `markdown_len≈2353`, 0 ingredients, at every render level | **177milkstreet.com**, likely cookscountry (0 rows ever) | **catastrophic — retries forever, learns nothing** |
| 7 | **Licensing gate (402)** | target bills the fetch as success | seriouseats / People Inc. | recorded in memory, not modelled in code |
| 8 | **Non-English** | JSON-LD present, English phrases absent | Greek corpus | correct (JSON-LD trusted) |
| 9 | **Not a recipe page** | article/explainer under a recipe path | dianekochilas gyro history (4,617 traffic!) | dropped, correctly — but it is her best page |

**Patterns 5 and 6 are the whole problem.** They are indistinguishable in the control surface
and opposite in reality.

---

## 3. Walking the code against the patterns

A publisher URL crosses **four** decision points, each fetching independently:

    ① _fetch_for_filter        unblocker=?, render NEVER passed
    ② _maybe_render_escalate   render=True, only on a would-be drop
    ③ extract_recipe_from_url  honours the domain fetch policy
    ④ render-retry on THIN     render=True again

### ①→② The learned flag is written and never read

`_fetch_for_filter(url, unblocker=...)` calls `fetch_with_full_fallback(...)` and **never
passes `render`**, and never consults `render_required`. The escalation comment at
build_query_batch.py:553 says escalation is for "a domain ALREADY known to need a real
browser" — but the *first* fetch doesn't know that. So on every `render_required=1` domain,
every page pays a plain fetch that is known in advance to return a stub, then pays again to
render it.

`mark_render_required` learns the fact. Nothing acts on it at the point that would save the
credit.

### ①→③ The filter's fetch is thrown away

`_fetch_for_filter` keeps `(text, jsonld, lang)` and discards the response. `page_cache`
appears **nowhere** in `collections_lib.py` or the filter path. `_extract_publisher_url_to_master`
then opens `with page_cache.enabled():` and fetches the same URL again, seconds later.

**Two paid fetches for one page, on every publisher, always.** Not a paywall issue — a
plumbing one.

### ③→④ The retry repeats an experiment already run

Extract finds 0 ingredients and retries with render. But for a pattern-6 domain, ② already
proved rendering doesn't help — the log literally says *"still scores 2 rendered"*. That
verdict is not carried forward, so ④ re-runs it.

### Net, per URL on 177milkstreet

    ① plain unblocker   → stub   (avoidable: render_required is known)
    ② render            → stub   (the real answer)
    ③ extract           → stub   (avoidable: ② already fetched it)
    ④ render-retry      → stub   (avoidable: ② already proved this)

Four fetches; one would do; **zero can ever succeed.**

---

## 4. Where the model is wrong (not the code)

### 4.1 `paywall` conflates a business fact with a technical one

`paywall=1` currently means, depending on who reads it: *"gated"* (the domain form's own
words: "the FACT that it's gated"), *"trust it, skip verification"* (the harvest), and
*"expect suppressed PA"* (the calibration).

NYT is gated **and** fully obtainable. Milk Street is gated **and** unobtainable. The flag
cannot tell them apart, so it cannot drive a correct decision for either.

The missing axis is **obtainability**, and it is a *measured fact*, not a curator opinion:
*did we ever get a complete recipe from this domain, and by which method?*

### 4.2 The UI modes are a fetch-policy shortcut wearing a strategy label

    open:    plain     / render 0 / verify 1 / score_only 0
    blocked: unblocker / render 1 / verify 1 / score_only 0
    curate:  unblocker / render 1 / verify 0 / score_only 1

Three cards, and **none of them expresses pattern 6.** A hard-gated publisher is not "open",
is not "blocked" (blocked implies the unblocker *works*), and "curate" is a *workflow* choice
that happens to avoid the problem by ingesting nothing. The curator reaches for Curate because
it is the only card that doesn't burn money — not because scoring-without-ingesting is what
they wanted.

This is the sync failure the curator suspected: the cards are quick-set macros over four
fields, and the field set has no dimension for "content is unobtainable by any automated
method."

### 4.3 Nothing learns from repeated total failure

`mark_render_required` learns *"needs a browser"* on a success. There is no counterpart for
*"20 URLs, 4 fetches each, 0 ingredients every time"*. So run 2 on Milk Street repeated run 1
exactly — 20 more unblocker calls, 0 saves — and run 3 would too.

**A domain that has never yielded a recipe should stop being fetched automatically.**

### 4.4 The ledger records candidate gates, not acquisition

Milk Street's ledger reads `{'is_recipe': 11, 'kept': 9}` — 9 kept. **One** reached master.
The 8 that died at extract (`SKIP-THIN`, 0 ingredients) are recorded nowhere: they are "kept"
in the ledger and absent from the corpus, with the two fetches each cost invisibly. Any
analysis of harvest health built on the ledger will overstate success on exactly the
publishers that fail worst.

---

## 5. Recommendations

Ordered by "fixes a class of problem" over "fixes a symptom". **1 and 2 are plumbing and pay
back on every publisher; 3–5 are the model change the curator is asking about.**

### R1 — One fetch per URL per run (plumbing, no behaviour change)

Put the filter's response into `page_cache` so ③ reuses it, and carry ②'s rendered response
forward. Expected: 4 fetches → 1 on gated domains, 2 → 1 everywhere else. **This is the single
highest-value change and it alters no decision logic.**

### R2 — Read the flag we already write

`_fetch_for_filter` should honour `render_required` and go straight to a rendered fetch on
domains already known to need one. Removes the doomed plain fetch permanently.

### R3 — Replace `paywall` (one flag) with two measured facts

Keep `paywall` as the business fact. Add, as **measured, not curated**:

    content_obtainable   unknown | direct | render | unblocker | unblocker_render | NEVER
    obtainable_checked_at / _n

Set it from actual outcomes — a save proves obtainability by the method that worked; N
consecutive complete failures prove `NEVER`. Then:

* `NEVER` → the harvest refuses to fetch and says so, offering the bookmarklet/userscript path
* everything else → start at the cheapest method known to work, not at the bottom of the ladder

This directly answers *"do we need special-case instructions for ATK or Milk Street"*:
**no — we need one measured field that makes them ordinary.** A per-domain special case is a
patch that will drift; a measured capability is a fact that maintains itself. ATK (23 rows,
partial) and Milk Street (6 rows, futile) then differ by data, not by hand-written exception.

### R4 — Add a fourth mode that tells the truth

    open      · blocked · curate ·  **gated — human capture only**

The fourth sets the harvest to score-only *and* labels why, so the curator isn't picking
"Curate" as a euphemism. Its help text should name the honest path: your browser is
authenticated, ours is not.

### R5 — Record the acquisition outcome in the ledger

`SKIP-THIN` / `SAVE-FAIL` at extract must become ledger rows with a stage of `acquire`, so
"kept 9, saved 1" is visible in the data and not only in a log a human happened to read.

### R6 — Only then, the cheap guards

Skip ④ when ② already proved rendering doesn't help. Worth doing, but it is a symptom fix and
it should land *after* R1–R3 or it will be re-argued when the structure changes.

---

## 6. What NOT to do

* **Do not add per-domain special-case branches** (`if host == '177milkstreet.com'`). That is
  the price the curator is worried about paying, and it is paid at the third such branch.
* **Do not delete the render probe.** It is what learns pattern 3, and it is bounded. The bug
  is that its *result* isn't read, not that it runs.
* **Do not treat `check_recipe=False` as the fix for gated sites.** It removes the verification
  fetches but leaves extract fetching a stub and saving nothing — 20 calls, 0 saves, which is
  exactly the second Milk Street run in the table above.
* **Do not assume the ledger's `kept` means acquired** until R5 lands.

---

## 7. Open questions — ANSWERED 2026-08-13

1. **Is a `NEVER` domain worth keeping in the corpus at all?** → **YES.** It keeps its DA/PA
   and stays scoreable, rankable and linkable; only ingestion is impossible. So `NEVER` must
   suppress *fetching*, never membership — the domain stays a first-class publisher whose
   recipes we point at rather than hold.
2. **Should the bookmarklet be promoted from fallback to the PRIMARY method for gated
   publishers?** → **YES.** For pattern 6 it is the only thing that works and it is free, so
   the UI should present it as the route for such a domain rather than as a consolation after
   an automated run has already failed and been paid for.
3. **A second collection type for explainers** (dianekochilas' best page by traffic, 4,617, is
   an article) → **TBD.**

### R1/R2 status — SHIPPED 2026-08-13

Verified on 177milkstreet (`render_required=1`), 3 URLs: **3 unblocker calls, no escalation**,
against ~12 before. The rendered pages land in `page_cache` under `variant='render'` — the
variant extract asks for on such a domain — so the second fetch is now a hit.

R3–R6 remain open, and answers 1 and 2 above shape R3/R4: `content_obtainable = NEVER` gates
the fetch only, and the fourth mode should offer the bookmarklet as the method, not as an
apology.
