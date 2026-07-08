# How the pipeline decides — every gate, plus the embedding & regression math

Plain-language map of what happens to a candidate recipe URL from discovery to ingestion,
and — in particular — how the **embedding** and the **regression** actually compute and what
each one decides. Written 2026-07-07.

Companion docs: [recipe-candidate-pipeline.md](recipe-candidate-pipeline.md) (the gate-by-gate
detail with every `if`), [harvest-and-cache-explained.md](harvest-and-cache-explained.md) (the
SEMrush→cache journey), [is-recipe-classifier.md](is-recipe-classifier.md) (the classifier
analysis + the LLM cascade).

> **The honest one-liner:** keep/drop is decided by **rules + the LLM cascade** (NOT a
> regression); the **embedding** decides *which dish* a kept recipe is; the **regression**
> decides *ranking*, not survival. The embedding-based logistic-regression *classifier* was a
> measured prototype we chose **not** to ship.

---

## Every gate a candidate passes through

### Stage 0 — Discovery (find candidates)
Google `site:` search, a local SEMrush export, or (for a dish) several web searches unioned.
Produces a list of candidate URLs. Dedupes tracking-param variants (incl. Google's `srsltid`,
which otherwise makes one page look like several).

### Stage 1 — Cheap pre-filters (no fetch, ~free)
Dropped before spending anything if: it's an archive/tag/category/feed URL, the **title** looks
like a roundup ("15 Best…"), it's on the **disallowed-domains** blocklist, or it falls outside
the publisher's **recipe-path** scope (e.g. only keep `domain.com/recipes/…`).

### Stage 2 — The is-recipe gate (`_is_recipe_filter`) — the core keep/drop
> **Note:** this is a *provisional* sort into `kept`/`dropped`. In `decide` mode the LLM cascade
> (Stage 2.5) can still MOVE a gray-zone page between the two buckets afterward — a heuristic drop
> is not final until the cascade has had its say.

Per URL, in order:
1. **Fetch** (normal UA chain → Wayback fallback → paid unblocker; a JS-only shell is re-fetched
   with a real browser render).
2. **JSON-LD check** — if the page declares `schema.org/Recipe`, **KEEP** (it self-certifies; no
   further analysis, language-agnostic).
3. **Structural gate** (pages without JSON-LD) — does the text have **both** an *ingredients-
   section* marker and a *method-section* marker? Yes → **KEEP**; no → **DROP** (`no-structure`).
   The recipe-phrase *count* is computed but, for English, only recorded — not the decider.
   (In practice this is an *"is it cleanly extractable"* gate, not just *"is a recipe present"*.)
4. **Translate branch** (non-English pages with no phrase pack) — translate a capped snippet, then
   keep if the phrase score clears a threshold.

### Stage 2.5 — LLM cascade (the adjudicator for the ambiguous middle)
**The LLM is NOT always called.** It runs only on the **gray zone**: candidates that are
**(a) content-bearing AND (b) have no JSON-LD** — the ambiguous middle where the heuristic is least
sure. That set spans both heuristic-*kept* and heuristic-*dropped* pages (so it can rescue *and*
catch). It is deliberately **skipped** for: JSON-LD keeps (already certain), pre-fetch drops
(archive/roundup-title/excluded-section — no content to judge), and fetch-failed/stub drops (<200
chars — nothing there; those need the render/unblocker *fetch* fix, not the cascade).

> **"Structured" ≠ a free pass.** Two different things get called "structured": (1) a page with
> machine-readable **JSON-LD** `schema.org/Recipe`, and (2) a page that merely passed the **structural
> gate** (has ingredient/method *text* markers but no JSON-LD). ONLY #1 skips the LLM. A #2
> structural-gate KEEP is still content-bearing + non-JSON-LD → it's in the gray zone → the LLM
> audits it and can CATCH it as a messy roundup. (Proof: batches show `heuristic=kept, LLM=poor_quality`
> rows — those are structural-gate keeps the LLM overruled.)

For each gray-zone page, a cheap Haiku pass reads a **recipe-anchored** snippet (the recipe region,
NOT the blog intro) and returns one of three verdicts:
- **`recipe`** — a single dish, cleanly extractable (distinct ingredient list + ordered steps).
- **`not_recipe`** — a roundup, how-to article, explainer, product/restaurant/news page.
- **`poor_quality`** — a recipe IS there, but too messy to extract cleanly (ingredients bleed into
  prose, image-heavy). Rejected as a poor *source*. (Tie-break: when torn, choose `poor_quality`.)

Modes (`system_config.is_recipe_cascade_mode`):
- **off** — never runs.
- **shadow** — classifies + records its verdict next to the heuristic's in `training.db`, but does
  NOT change keep/drop. (Measure + mint gold labels; review in ⋮ admin → Labeling → "LLM disagrees".)
- **decide** — also applies the validated **asymmetric override** (AFTER training capture, so the
  recorded label stays the heuristic's):
  - **RESCUE** a heuristic-DROP the LLM calls `recipe` → keep (~77% precision on curator labels).
  - **CATCH** a heuristic-KEEP the LLM calls `poor_quality`/`not_recipe` → drop (~88% precision).
  - Never overrides a JSON-LD keep (those never reach the LLM).

Measured on 28 curator-labeled gray-zone pages: cascade decision accuracy **75%** vs the heuristic's
**29%** (≈2.6×). Cost ~$0.002/gray-zone page (Haiku).

### Stage 3 — Moz score + rank + keep
Survivors get Moz metrics; the **ranking regression** (below) scores + orders them; the top N are
marked kept. (Score-only mode stops here and lets a curator pick.)

### Stage 4 — Extract winners → `master_recipes`
Each winner is **extracted** (an LLM reads the page into structured recipe fields), then the
**save-gate** rejects anything with fewer than the configured minimum ingredients/instructions
(the final "is this a usable recipe" check, with a "save anyway" override). Then the
**dish-match embedding** (below) tags which canonical dish it is.

---

## The embedding and the regression — how they actually compute

Two different pieces of math that decide two different things.

### 1. The embedding — "what dish is this?"  (LIVE)
- Build a short, fact-dense text for the recipe (canonical dish name + key ingredients).
- `text-embedding-3-small` returns a **list of 1,536 numbers** — a coordinate in a 1,536-dimensional
  space where *similar meaning lands nearby*. Think: a GPS pin for the recipe's meaning.
- Every canonical dish has its own pin (stored in the sqlite-vec index).
- Measure **distance** (cosine / L2) from the recipe's pin to each dish's pin; the nearest dish
  within a cutoff (~0.85 L2 ≈ 0.55 cosine) is the match. That's how a recipe is labeled a Greek Salad.
- This is the **only embedding running live** — for *matching*, not keep/drop.

### 2. The ranking regression — "how good is this page?"  (LIVE)
- NOT a keep/drop gate — it **ranks** survivors so we keep the best N.
- Each page has two Moz numbers: **PA** (page authority) and **DA** (domain authority).
- Fit a **regression line**: *given a domain's DA, what PA do its pages usually get?* A page's **OU**
  ("over/under") = how far its actual PA sits above/below that predicted line. Positive OU = it
  punches above its domain's weight (a standout page on a modest site).
- Blend OU with **power** (absolute PA/DA percentile) → a single `rank_score`. Highest wins the
  top-N slots. (Paywalled publishers get their link-starved PA remapped upward first, so gated sites
  aren't unfairly buried.)

### 3. The is-recipe classifier (embedding + logistic regression) — **PROTOTYPE, not live**
Built and measured, deliberately **not** wired into the live gate.
- Idea: embed the page (1,536 numbers) + a few structural features → a **logistic regression** — a
  model that learns a **weight per input**, computes weight×value summed, squashes the total through
  an S-curve into a **0–1 probability** the page is a clean recipe; a threshold turns that into
  keep/drop.
- Honest measurement (group-CV *by domain*, so it can't cheat by memorizing a publisher it also
  trained on): **~0.94 AUC** — a real but moderate win. Its weakness: it partly memorizes *which
  publishers* it saw, so on a **brand-new** site it's weaker.
- Because the **LLM cascade generalizes to unseen sites** (it reasons; it didn't train on our
  domains) and won on the disagreements, **we shipped the cascade instead.** The regression model is
  saved (`models/is_recipe/hybrid_lr.joblib`) as a fallback/future option, not in the runtime path.

---

## Quick reference — who decides what
| Decision | Mechanism | Live? |
|---|---|---|
| Keep / drop (self-certified) | JSON-LD `schema.org/Recipe` present | ✅ |
| Keep / drop (heuristic) | structural gate (ingredients + method markers) | ✅ |
| Keep / drop (gray zone) | LLM cascade (Haiku three-way, shadow/decide) | ✅ |
| Final usable-recipe check | save-gate (min ingredients/instructions) | ✅ |
| **Which dish** it is | **embedding** → nearest-dish cosine/L2 | ✅ |
| **Ranking** among survivors | **regression** (OU/power authority blend) | ✅ |
| Keep / drop (ML classifier) | embedding + **logistic regression** | ⚠️ prototype only |
