# Is-recipe classifier — analysis, results, and integration plan

Written 2026-07-07. Supersedes the flat phrase-count heuristic for the harvest is-recipe gate.
Companion to [corpus-ml-strategy.md](corpus-ml-strategy.md) (this is its first shipped project).

## Problem
The harvest filter decides keep/drop for every candidate URL. Two failure modes were live:
- **False drops** (losing real recipes): the structural gate (needs an ingredients-section
  marker AND a method-section marker) drops paywalled stubs / JS shells / oddly-marked pages.
- **False keeps** (summaries leak in): how-to articles and roundups score high on the flat
  phrase count because they use the same cooking vocabulary.

## What the data said (training.db, 10,857 labeled samples; see analysis below)
- **Section markers are the discriminator, by 32×.** Recipes average 3.5 recipe-card markers
  ("prep time / cook time / servings / yield / directions:"); summaries average 0.1.
- **Verbs barely discriminate (1.7×)** — how-tos are full of "mix the / bake for". **Ingredients
  are weakest (1.4×).** So re-weighting toward verbs/measures does NOT filter summaries.
- **Repetition (multi-recipe tell) is a null signal here** — among structured pages, recipes and
  summaries repeat card timers identically (~0.7% with ≥3); real recipes even carry more
  (related-recipe widgets). A repetition cap changed leakage by 0.0%.
- **Most "false drops" are fetch stubs** — median 226 chars; 57% are sub-800-char shells. Those
  are a FETCH problem (render/unblocker), not a scoring problem.

## Models tested (held-out, then rigorous group-CV)
| Model | random-split AUC | **group-CV AUC (unseen domains)** |
|---|---|---|
| flat phrase count ≥ 7 | — | (leak 73%) |
| structural gate (live) | — | ~ (recall 95 / leak 35) |
| phrase-feature LR (7 hand feats) | 0.93 | — |
| embeddings LR (1536-dim) | **0.988** ← leakage | 0.90 ± 0.05 |
| structural feats only (6-dim) | — | 0.79 ± 0.11 |
| **hybrid: embeddings + structural, C=0.05** | — | **0.936 ± 0.026** ✅ |

**Key lesson:** the 0.988 was domain **memorization** (random split let the same publisher sit in
train and test). Group-CV by domain + dedupe (removed 29% duplicate captures) is the honest
protocol. The realistic number for NEW publishers is **AUC ~0.94 (hybrid)** — a real but
moderate lift, best and most stable when embeddings are combined with the section features.

## Chosen model
**Hybrid logistic regression**: `[1536-dim text-embedding-3-small] ++ [sec, measure, verb,
composite, log_len, struct]`, StandardScaler + LR(class_weight=balanced, C=0.05).
Artifact: `models/is_recipe/hybrid_lr.joblib`. Threshold is a config dial: **0.5** balanced
(recall↑), **0.7** to crush leak.

## Cost
- Embedding per candidate: ~$0.00003 (text-embedding-3-small); LR head is free. ~$0.30 / 10k
  candidates — and we already fetch the page. ~60× cheaper than an LLM classify.
- A **local** embedder (sentence-transformers) makes inference $0 and fully offline (portable-
  package ideal) — a follow-up option.

## Persistence
- `is_recipe_samples.embedding BLOB` + `embedding_model` columns added; 6,932 English labeled
  rows backfilled. Retraining is now free (no re-embed). Full-corpus backfill = a rate-limited
  follow-up job (OpenAI TPM cap 1M/min — throttle ~4s/batch).

## Integration plan (STAGED — not yet wired live)
1. **Inference helper** `is_recipe_ml(text) -> (prob, verdict)`: embed → structural feats →
   load artifact → probability. Cache the embedding on the row.
2. **Wire into `_is_recipe_filter`** behind an OFF-by-default config flag (`is_recipe_ml_enabled`),
   exactly like `keyword_prescreen`. When on: KEEP if `json-ld` OR `prob >= threshold`; the
   section-weighted heuristic stays as the OFFLINE FALLBACK.
3. **Cascade for the gray zone** (optional): if `prob` in an uncertain band, adjudicate with a
   Haiku keep/drop call (~$0.002) — LLMs generalize to unseen domains inherently.
4. **False-drop fetch fix** (separate track): render/unblocker-refetch stub domains + per-domain
   trust override — no classifier recovers 226 chars of nothing.
5. **Retraining loop**: the gauntlet + human_label column keep labeling for free; retrain
   periodically, group-CV before promoting an artifact.

## Open decision
Whether the **~0.94 hybrid** clears the bar to wire into the live gate (behind the flag) vs.
investing first in (a) more domains for generalization, or (b) an LLM cascade for new publishers.
