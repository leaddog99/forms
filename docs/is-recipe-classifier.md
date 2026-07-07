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

## LLM cascade — SHADOW MODE (SHIPPED 2026-07-07)
Why the cascade over the embeddings model: the embeddings-LR **memorizes domains** (0.94 only
because it saw the publisher; on truly-new publishers it degrades). An LLM has no such
dependence. AND a quick Haiku test surfaced the key implementation lesson — **send the recipe
region, not the page prefix** (a food blog leads with narrative; Haiku dropped a real 13.5k-char
recipe when shown only the intro). But a trustworthy accuracy number is **label-limited** (json-ld
/ URL proxies are weakest exactly in the gray zone), so we ship SHADOW mode first: it labels +
measures without deciding.

- `intake/isrecipe_cascade.py::shadow_classify(entries)` — over the GRAY ZONE (content-bearing,
  non-JSON-LD candidates), batched Haiku keep/drop on **recipe-anchored snippets**
  (`_smart_snippet`), best-effort, capped at 300/run. Stamps `_shadow_verdict`/`_shadow_reason`;
  does **not** change keep/drop.
- Hooked in `_is_recipe_filter` behind `system_config.is_recipe_cascade_shadow` (default OFF),
  just before `capture_samples`, which persists `shadow_verdict`/`shadow_reason` onto every
  `is_recipe_samples` row.
- **Review loop:** ⋮ admin → Labeling → filter **"LLM disagrees"** — the rows where Haiku
  contradicts the heuristic (a heuristic-drop the LLM would keep = a recovered recipe;
  heuristic-keep the LLM would drop = a caught leak). Correcting these mints the gold gray-zone
  labels we've been missing.
- **To run:** turn on the flag in System config → run any harvest → review disagreements. Needs a
  server restart for the new endpoint param + config seed.

## Remaining plan
1. After a batch or two of shadow data + human labels: measure the cascade honestly; if it clears
   the bar, flip it from shadow → deciding (KEEP if json-ld OR heuristic-keep OR LLM-keep).
2. **False-drop fetch fix** (separate track): render/unblocker-refetch stub domains + per-domain
   trust override — no classifier recovers 226 chars of nothing.
3. Re-evaluate the **embeddings-hybrid** once we have clean labels (it may still win on KNOWN
   publishers; a hybrid-model + LLM-cascade split by domain-seen-before is possible).
4. **Retraining loop**: embeddings persisted on the row → retrain free; group-CV before promoting.
