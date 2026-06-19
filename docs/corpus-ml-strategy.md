# Corpus as an ML asset — distillation, classifiers, and capture-now data hygiene

**Status: DESIGN / DIRECTION (2026-06-18).** The recipe system is accumulating an extensive, *structured, self-labeling* corpus. This note frames its ML value pragmatically — what's worth doing, what to ignore ("don't boil the ocean"), and the one thing to do **now** so the option stays open. Connects to [[project_recipe_anchor]] (cook-rework + gauntlet), [[project_llm_gateway]] (the capture substrate), [[project_identity_card]], [[project_domain_scoring]], [[project_portable_package]], [[project_user_as_publisher]] §6 (production economics).

---

## 1. What we actually own (it's not the recipes)

Raw recipes (ingredients + method) aren't ownable ([[project_user_as_publisher]] §1). Everything we layer on top **is** original, structured, and rare:
- **Paired cook-reworks**: `(recipe + grounded _measurements) → validated _cook`, each **gauntlet-PASSED** (auto-labeled correct) and many **hand-corrected by a cook** afterward (correction/preference signal).
- **Identity cards** ([[project_identity_card]]), **tips KB** (provenanced), **dish catalog + embeddings**, **scoring ledger** (OU/power fits), **domain authority**, **is-recipe / collection-filter decisions**.

This is **structured supervision**, not a text pile. The moat is the judgments. The **gauntlet makes the corpus self-labeling** — that's the rare, valuable part.

---

## 2. The tier ladder (and what to ignore)

- **Tier 0 — already ML, keep going:** embeddings + sqlite-vec (dish match, recommender); the **scoring fits are classical regression** on our data (OU/power quadratic, calibrated PA, planned system-wide domain scoring [[project_domain_scoring]]).
- **Tier 1 — distillation (highest leverage):** fine-tune a small/open model on our **Opus-generated, gauntlet-passed** outputs for the expensive repetitive jobs — **cook-rework first**, then identity-card extraction, measurement resolution. The gauntlet is the free auto-labeler; hand-edits are the preference signal. Closes three loops: cuts production cost ([[project_user_as_publisher]] §6), yields the *local model* the portable package wants ([[project_portable_package]]) — **ours, tuned**, not generic — and compounds as a moat.
- **Tier 2 — cheap narrow classifiers:** train small deterministic models on the labeled corpus to replace heuristics + LLM calls. **First target = the recipe classifier (§4).**
- **Tier 99 — DON'T:** train a foundation model from scratch. That's the ocean. Distillation + narrow classifiers get ~95% of the value for ~1% of the effort.

---

## 3. The capture-now move (the only urgent thing)

Don't train anything today. The cheap, irreversible-if-skipped move is to **capture training-ready data as a byproduct**:
- Log every LLM call as `(inputs → output, gauntlet verdict, model + prompt version)`. **`llm.py` gateway + `bcc_token_journal` are ~80% of the substrate** ([[project_llm_gateway]]) — extend them to persist the *content pairs*, not just token counts.
- Capture **human edits as deltas** against the model output (the salmon hand-fix case — "recipe editing IS a feature").
- Tag provenance so we can later filter to the **gold set** (Opus, gauntlet-passed, human-unmodified) vs the **preference set** (human-corrected).

Data hygiene now → train when volume + need line up, not before.

### SHIPPED 2026-06-18 — is-recipe byproduct capture

The first capture stream is live. `intake/training_capture.py` (self-contained, append-only, best-effort, separate git-ignored `training.db`) logs one labeled sample per is-recipe decision, hooked at the **single canonical chokepoint** `intake.build_query_batch._is_recipe_filter` — so it captures **both** the dish batch (`source=dish_batch`) and the domain harvest (`source=domain_harvest`, which reuses the same filter). Off the hot path; a logging failure can never break a run; the transient page text is popped before the entry flows downstream.

Minimal record (`is_recipe_samples` table):
```
sample_id, captured_at,
url, title,
content, content_chars,     -- the visible text the filter SCORED (trimmed ≤50k) — keeps features/embeddings derivable later
lang_code, has_jsonld, translated,
recipe_score, threshold,    -- the numeric signal + IS_RECIPE_THRESHOLD at capture time
decision,                   -- 'kept' | 'dropped'  (the heuristic LABEL)
reason,                     -- json-ld | phrase-score | collection-title | fetch-failed | recipe-score<.. | translation-suspect:..
source, provenance,         -- surface + {dish|domain}
human_label, human_labeled_at, human_note   -- NULLABLE gold correction (where the heuristic is WRONG — the highest-value column)
```
Two non-obvious must-haves are baked in: (1) **content is stored** (labels alone re-learn the heuristic; the source text is what lets a classifier beat it), and (2) the **`human_label` column exists** for curator corrections of the leaks (the gold signal). Inspect with `python -m intake.training_capture` (prints counts by decision/source). A curator-correction UI that writes `human_label` is the natural follow-up; same byproduct pattern extends to the cook-rework gauntlet verdicts (the distillation labels) alongside the `llm.py` content-pair logging.

---

## 4. First concrete project — the recipe (is-recipe) classifier

Perfect warmup: low-stakes, **critical asset** (is-recipe gates whole-harvest quality), and it builds the data→train→eval→shadow-deploy loop that distillation reuses. Replaces/augments the current phrase-score + JSON-LD heuristic (`_is_recipe_filter`).

**Reframe:** this is **text classification, NOT a generative LLM** — needs **no GPU / no 3090**. "Local model" here = a tiny pickled sklearn model in-process.

**Tiers (easiest first):**
- **Tier B — embedding + logistic-regression head (START HERE):** we already embed every recipe/page (sqlite-vec) → train a LogisticRegression/MLP on the existing vectors. Zero new infra, seconds on CPU, immediately comparable to the heuristic.
- **Tier A — classical features + LR/XGBoost:** has-JSON-LD-Recipe, ingredient-token density, "ingredients"/"directions" headers, listicle-title pattern, structural counts. Fully interpretable; seconds on CPU.
- **Tier C — fine-tune a small encoder** (BERT/ModernBERT ~100–400M): a GPU helps but a 3090 is overkill (minutes on a 3090 / free on Colab / slow on CPU). Only if A/B leave a gap.

**What's needed today:**
1. **Labeled data — already minted as a byproduct:** positives = pages that passed `_is_recipe_filter`; negatives = harvest rejects (collections, listicles, `/tag/`, archives, the "Cup to Gram Conversions" leak). **Catch:** labels come from a heuristic, so training only on them re-learns the heuristic — the lift comes from (a) features it doesn't use, and (b) a **small hand-label pass on borderline/leaked cases**.
2. **~30-line sklearn script:** split → fit → confusion matrix + P/R/F1 → pickle.
3. **Eval vs the heuristic** on held-out data — does it beat phrase-score? If not, cheap lesson.
4. **Shadow-deploy behind `_is_recipe_filter`** ([[feedback_single_path]]): run alongside, log disagreements, let it decide only once proven.

**The 3090's real job:** not the classifier — it's the **generative/distillation tier**: self-host a quantized open model (Qwen 30B-class fits ~24GB quantized) as the distillation/local-LLM backend (§2 Tier 1, [[project_portable_package]]) + local embedding generation. Justify the chip by the local-LLM ambition; the classifier is the free CPU warmup.

---

## 5. Sequence
1. **Now:** capture-ready data hygiene (§3) — rides along with the `llm.py` work in flight.
2. **Warmup (weekend-sized):** the is-recipe classifier, Tier B, shadow-mode (§4).
3. **Then:** Tier-2 collection/listicle classifier (same pipeline).
4. **When volume + a 3090 (or rented GPU) are in place:** Tier-1 cook-rework distillation → the tuned local backend.
5. **Never:** foundation model from scratch.
