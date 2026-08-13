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

## 6b. `no-recipe-structure` is three different failures wearing one label — MEASURED 2026-08-13

`no-recipe-structure` is the largest is_recipe drop reason in the ledger (100). Sampling real
rejects and re-fetching them shows it is **not one thing**, and only one of the three is a
correct verdict:

| # | actual cause | example | verdict | remedy |
|---|---|---|---|---|
| A | **Genuinely not a recipe** | `dianekochilas.com/what-is-mahlepi/`, `eatnpark.com/MenuItem/…` | correct — an article and a menu item | none |
| B | **Structured data we don't look for** | `177milkstreet.com/recipes/…` | **our bug** | parse it |
| C | **A blocked fetch, misfiled** | `tiffycooks.com/…miso-ramen/` | **our bug, and the diagnosis is inverted** | classify as fetch-failed |

### B — the parser looks in one place

`extract_recipe_jsonld` calls `extruct.extract(..., syntaxes=["json-ld"])` and
`_response_to_filter_signals` only ever consults that. Two gaps:

* **JSON-LD outside a `<script>` tag.** Milk Street publishes the whole Recipe object in
  `<meta name="application/ld+json" content="…">`, HTML-escaped. Every parser that looks for
  `<script type="application/ld+json">` — including ours — finds nothing. The object is
  complete and valid; it is simply in an attribute.
* **The other syntaxes are never requested.** `extruct` also supports `microdata`,
  `microformat` (hRecipe) and `rdfa`, all of which carry `recipeIngredient`. We ask for
  none of them, so a site marking up recipes in microdata reads as structure-less.

Worth stating precisely: in the 4-URL sample, microdata/microformat/rdfa found nothing that
JSON-LD didn't. **The measured gap is the meta-tag case; the other syntaxes are a
hypothesis, not a finding.** Test before building.

### C — a 202 with a captcha is scored as a recipe page

`tiffycooks.com/super-easy-creamy-spicy-chicken-miso-ramen/` — unambiguously a recipe — returns:

    status 202 · 213 bytes · "captcha" present · visible text length 0

`fetch_with_full_fallback` accepts any 2xx (`if 200 <= resp.status_code < 300`), so the stub
is returned as a success, phrase-scored at 0, and filed **`no-recipe-structure`**.

That label is not merely imprecise, it is the *opposite* of the truth. It asserts a fact about
the page's content when we never saw the page. Consequences:

* the fetch-fail **salvage path never fires**, because the row isn't `fetch-failed`
* the curator and the AI editor read "not a recipe" for a page that is one
* the domain never learns it is blocked, so `mark_render_required` / unblocker escalation
  aren't triggered either

**The detector for this already exists.** `_looks_blocked()` (html_to_markdown.py:501) tests
exactly these signals — challenge markers, or a small body with no JSON-LD and no `<article>`.
It is consulted **only** when deciding whether to escalate to the paid unblocker tier, and
never on the path that writes the reject reason.

### Why this matters beyond the label

Every count built on `no-recipe-structure` is a blend of three populations. "100 pages had no
recipe structure" is really "some number were articles, some we failed to parse, and some we
never fetched." Any tuning of the phrase scorer against that bucket is tuning against noise.

### Recommendations (R7–R9)

* **R7 — classify blocked responses as `fetch-failed`, not `no-struct`.** ✅ **SHIPPED
  2026-08-13** — see §6c below. The plan ("call `_looks_blocked()` and return None") was
  half right: reusing the detector was correct, returning `None` was not.
* **R8 — parse JSON-LD in `<meta name="application/ld+json">`.** ✅ **SHIPPED 2026-08-13**
  — see §6d. "A contained addition to `extract_recipe_jsonld`" was the wrong shape: done
  that way it would have made the corpus WORSE, because what Milk Street puts in that tag
  is a paywall teaser. It became two functions instead.
* **R9 — measure, then maybe add, the other extruct syntaxes.** Run microdata/microformat/rdfa
  across a real sample of `no-struct` rejects first. Add only what the sample proves.

None of these changes what "is a recipe" means. They change whether we answer that question
about the page we actually received.

---

## 6c. R7 as built — a failure must say why (SHIPPED 2026-08-13)

The brief was *"a fetch failed needs an explanation in the log"* and *"restructure the
component for clarity"* — not a one-line early return. Four things came out of it, and two
were bugs the restructure exposed rather than defects it set out to fix.

### The shape change

`_fetch_for_filter` returned `Optional[tuple]`. Every distinct failure — timeout, 404,
captcha, parse error — collapsed into the same `None`, so the caller could only write a
bare `"fetch-failed"`. It now returns a **`FilterFetch` NamedTuple** whose `ok=False`
branch *always* carries `failure`, a human phrase. The reason reaches three places at once:
the run log (`why: …` under the FETCH-FAIL line), the entry's `_dropped_reason`, and the
ledger via `classify()` (longest-prefix on `fetch-failed` still matches, so the suffix is
free). Nothing anonymous survives.

### Two verdict paths, not one

`_fetch_for_filter` was the obvious site. The render escalation had the same defect one
level down: a rendered response that was itself a challenge stub scored 0 and the caller
filed it `no-recipe-structure`. Both now refuse before scoring.

### The threshold split — the part that was nearly wrong

`_looks_blocked`'s own docstring said it was safe *"ONLY to decide whether to escalate …
so a slightly eager match merely spends one credit."* Reusing it unchanged on the verdict
path violated that contract: there an eager match **discards a real recipe**. Hence two
thresholds — `_THIN_SPEND_CHARS = 15000` (eager, unchanged, credit at risk) and
`_THIN_VERDICT_CHARS = 2000` (strict, recipe at risk), selected by `strict=`.

### The marker tiers — the part that *was* wrong

Measured against 40 previously-kept recipes, the first strict implementation refused 4.
Two were **767 KB real pages from jamieoliver.com**. Cause: `challenge-platform` matches
Cloudflare's *passive* JSD probe (`/cdn-cgi/challenge-platform/scripts/jsd/main.js`),
which Cloudflare injects into pages it serves **normally**. The marker list had been
treating vendor plumbing as proof of a block — tolerable when it cost a credit, ruinous
as a verdict.

Markers are now two tiers:

| Tier | Examples | Meaning |
|---|---|---|
| **HARD** | `px-captcha`, `pardon our interruption`, `just a moment...`, `verify you are human` | Text that appears only *on* a challenge page. Sufficient alone. |
| **AMBIENT** | `challenge-platform`, `datadome`, `perimeterx`, `incapsula` | Vendor plumbing that rides along on served pages. Corroborating only — it names *who* blocked us once the thin body has established *that* we were blocked. |

`_BLOCK_MARKERS` remains as the union so existing importers are untouched.

### Measured result

| Set | n | Refused |
|---|---|---|
| Previously-kept recipes (random `master_recipes`) | 40 | 1 — and genuinely blocked today (1,115 b Cloudflare) |
| Known blocks (tiffycooks 213 b, bostonchefs 1,142 b, kalofagas 836 b) | 3 | 3 |

`tiffycooks.com/super-easy-creamy-spicy-chicken-miso-ramen/` — the page that started
this — now reads:

    blocked — thin body, no JSON-LD and no <article> (213 bytes < 2000; too small to be the page)

instead of claiming its recipe structure was missing.

### Two bugs the restructure surfaced

* `_fetch_text` did `result[0]` — under the NamedTuple that is now `ok`, i.e. it would
  have returned `True` as the page text. Fixed to `result.text`.
* The Phase-A salvage filter tested `_dropped_reason == "fetch-failed"` **exactly**, so
  the newly-labelled blocks would have been excluded from the very recovery path R7
  exists to feed. Changed to `startswith`.

### Log lines are ASCII

`└─` raises `UnicodeEncodeError` on this host's cp1252 stdout and would kill a harvest
mid-run; the sub-line is `why:`. (Em dashes are fine — cp1252 has one.)

---

## 6d. R8 as built — two questions, not one (SHIPPED 2026-08-13)

177milkstreet.com publishes every recipe's schema.org Recipe in a
`<meta name="application/ld+json" content="…">` tag (HTML-escaped) rather than a
`<script type="application/ld+json">` block. extruct correctly reads only the script form,
so a **180 KB page carrying a complete Recipe declaration scored ZERO structure**. Job 820:
8 of 10 candidates dropped as `no-recipe-structure`.

R7 is what made this diagnosable. That run logged **zero FETCH-FAIL** — the pages were
genuinely fetched, so the residual really was a structure problem and not a block. Before
R7 the two were indistinguishable.

### Why "a contained addition to `extract_recipe_jsonld`" was the wrong shape

What is in that meta tag is a **paywall teaser**, and it is built to look like a recipe:

    recipeIngredient: [
      "All-purpose flour, for dusting",
      "Flaky Pie Pastry, in a 4½-inch disk and refrigerated",
      "2 1/4 to 2½ pounds (4 to 6 medium) Honeycrisp apples",
      "... and more. Sign up for full access to all ingredients and instructions."
    ]
    recipeInstructions: [ one HowToStep ]

Meanwhile the page BODY, fetched through the unblocker, is complete — the Jordanian
flatbread extracted from markdown as 3 ingredients and 4 steps, which is the whole recipe
(shrak really is flour, salt, water). So handing the teaser to the content path would have
**replaced good content with an advert in the ingredient list**.

### The split

| function | question | gated teaser |
|---|---|---|
| `page_declares_recipe()` | IS this a recipe? | **counts** — the candidate filter asks this |
| `extract_recipe_jsonld()` | GIVE me the recipe | **refuses** — whatever it returns is ingested |

The gate is read, not guessed: `jsonld_declares_gated()` checks schema.org's own
`isAccessibleForFree` on the node and on any `hasPart` WebPageElement. Milk Street sets it
honestly — `false`, with `cssSelector: .paywalled-content`.

### Verified on job 822 (live)

`KEEP json-ld` on the recipe pages that were `DROP no-struct` in job 820, while
`177milkstreet.com/recipes` — the INDEX page — still correctly drops at `phrase=0`.
Control: recipetineats (normal `<script>` JSON-LD) unaffected.

| | job 820 | job 822 |
|---|---|---|
| candidates kept | 2 | **9** |
| saved to master | 1 | 1 |
| skipped at 0 ingredients | 1 | 8 |

### CORRECTION — Milk Street is NOT "crackable"

An earlier draft of this section claimed the unblocker returns full page content and only
the JSON-LD is truncated. **That was generalised from one page and job 822 disproves it.**
The run measured its own answer:

    obtainability: 177milkstreet.com -> unblocker_render
                   (1 of 9 extracted via unblocker_render (11% yield))

The Jordanian flatbread is the 11%, not the rule; the other 8 returned 0 ingredients
because the body really is gated. The one that came through is a short recipe whose method
Milk Street shows in full — not evidence that the paywall is porous.

**So R8's value is not access, it is CLASSIFICATION.** Those 8 pages are now filed as
*recipes we could not obtain* rather than *pages with no recipe structure*. That is the
difference between a publisher that looks worthless and one that is a known, measured,
human-capture target — and it is exactly the R4 "gated — human capture only" mode, which
now has a real example and a real number attached to it.

The partial-capture question therefore stays open and does NOT get answered by "we have
full access": we still must not store a 3-of-10 teaser, and for these pages the route that
works is the curator's own signed-in browser (📋 Queue / ⚡ Run userscript), not the paid
unblocker.

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
