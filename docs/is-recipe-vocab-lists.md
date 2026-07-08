# Is-recipe vocabulary lists (the editable structural word-lists)

The is-recipe gate decides "is this page a recipe?" using several small **word-lists**.
Per `feedback_no_data_in_code` / `project_system_config`, the live values are **DB-resident,
curator-editable `system_config` settings** — the code holds only a `*_SEED` fallback (used
before the table is seeded, or if config is unreachable) which is also the value shipped in
`SYSTEM_DEFAULTS`. Edit them in **⋮ admin → System config → Harvest**; changes take effect
without a code change or (for out-of-process harvests) a restart.

This doc is the single reference for **what each list does, which module reads it, how to write
it, and the multi-language story.**

---

## The lists

| system_config key | Consumed by | What it does |
|---|---|---|
| `structure_ingredient_markers` | `input/pipeline/validators.py` → `recipe_sections('en')` → `has_recipe_structure` | Header words that mark the **ingredients** section (English/base). |
| `structure_method_markers` | same | Header words that mark the **method/directions** section (English/base). |
| `structure_method_verbs` | `validators.py` → `_method_verb_count` → `has_recipe_structure` | Imperative cooking verbs — the **header-less method fallback** (see below). |
| `structure_method_verb_min` | `validators.py` → `has_recipe_structure` | How many DISTINCT method verbs count as a header-less method section. |
| `snippet_recipe_anchors` | `intake/training_capture.py` → `_smart_snippet` | Markers used to find **where the recipe region begins** when excerpting a page for the LLM cascade / Labeling UI. |
| `snippet_comment_markers` | `training_capture.py` → `_smart_snippet` | Comment/footer markers used as the recipe **back-anchor** (see below). |
| `snippet_lead_chars` | `training_capture.py` → `_smart_snippet` | Chars of context to include BEFORE the recipe anchor (a small lead-in, not the window size). |
| `disallowed_domains` | `input/pipeline/domains_lib.py` → `get_blocked_root_domains` | Root-domain hard blocklist, dropped before any fetch. |

Related (not structural vocab, but same "editable list" family): `serp_exclusions`
(SERP `-site:`/`-term`), `semrush_export_patterns` (inbox filename globs).

### The structure gate (`has_recipe_structure`)
A real recipe has **BOTH an ingredients section AND a method section**; a vocabulary-rich
guide/tips/roundup article usually has at most one. This structural AND is the *free* gate
(no LLM, no per-page translation). The method side is satisfiable two ways:
1. a **method HEADER** (`structure_method_markers`), or
2. **≥ `structure_method_verb_min` DISTINCT `structure_method_verbs`** — the *header-less
   fallback* for blog recipes that write the method as prose ("whisk the milk… bake…") with
   no "Directions/Method" heading. The ingredients section is **still required**, so precision
   holds; the LLM gray-zone cascade catches any residual leak.

### The recipe-region snippet (`_smart_snippet`)
When we excerpt a page for the LLM cascade (or the Labeling UI), we must send the **recipe
region, not the page header** — a food blog opens with a long narrative and Haiku will call a
real recipe `not_recipe` if it only sees the story. Resolution order:
1. Anchor on the earliest `snippet_recipe_anchors` match (the recipe region).
2. **Back-anchor** (the "scroll back from comments" idea): if no recipe anchor is found (a
   header-less recipe buried under a long intro), a blog page runs *[intro] → [recipe] →
   [comments]*, so take the window **ending at** the earliest `snippet_comment_markers` match
   — the recipe sits just above the comments.
3. Only if neither is found, return the head (itself a weak "probably not a recipe" tell).

**Window sizing (important):** the excerpt is NOT "the first N chars." On an anchor hit it
*jumps to the anchor* — anywhere in the page, even char 5,000 past a long intro — and takes
the **caller's `max_chars` forward** (the LLM cascade passes **1600**; the Labeling UI 400),
with only `snippet_lead_chars` (default 80) of lead-in before the anchor for context. The
comments back-anchor likewise takes a **full `max_chars` scrolling back** from the comments
boundary. So the long intro is skipped entirely; raise the caller's `max_chars` (in
`isrecipe_cascade`/Labeling) if a long recipe is being truncated — `snippet_lead_chars` is
only the pre-anchor context, not the window.

---

## Guidelines for writing the lists

- **Lowercase.** Captured/scored content is lowercased before matching, so write entries
  lowercase.
- **Accent-free for the structure gate.** `has_recipe_structure` accent-strips the text
  (`_strip_accents`) before matching, so write accent-free forms (`saute`, not `sauté`).
- **Match mode matters:**
  - *Markers/anchors* (`structure_*_markers`, `snippet_*`) are **substring** matches. Make
    them specific enough to avoid false hits — e.g. `method:` and `method ` (trailing space)
    rather than bare `method`, so "methodology" doesn't trip it.
  - *Method verbs* are **word-boundary** matched (`\bverb\b`), so "add" won't match
    "addition". Use the **base imperative** form (`whisk`, `fold`, `simmer`).
- **Keep verbs discriminating.** Avoid words that are common in non-recipe prose (that's why
  the gate requires several DISTINCT verbs, not one). Adding rare/edge verbs is cheap; adding
  a generic word like "make"/"use" would erode the signal.
- **Comment markers must be END-of-recipe signals** that reliably sit *after* the recipe and
  do **not** appear inside a recipe body (e.g. "leave a comment", "post navigation",
  "related recipes"). A marker that appears mid-recipe would truncate the snippet early.
- **Disallowed domains** are **root-domain grain** (`youtube.com`, not
  `www.youtube.com/...`).
- **Lean conservative.** The heuristic provides recall; the LLM cascade
  (`is_recipe_cascade_mode`) provides precision. Prefer a slightly-too-permissive list (the
  cascade catches leaks) over a too-strict one (which silently drops real recipes).
- **Validate against labeled data** before/after a change: `training.db`'s
  `is_recipe_samples` (with `human_label` corrections) is the ground truth — a good edit
  recovers `human_label='recipe'` drops without newly admitting `human_label='not_recipe'`.

---

## Multi-language considerations

The is-recipe pipeline is multilingual (`project_multilingual_extraction`), and these lists
split into **base (English)** and **per-language** homes:

- **The structure gate is BILINGUAL.** `has_recipe_structure(text, base_lang, page_lang)`
  unions section markers from BOTH the **base** language (English, from the
  `structure_*_markers` system_config lists) AND the **page** language, so a site that mixes
  English + its own language in headers/body is covered.
- **Per-language section markers live in the language packs**, not system_config:
  `intake/recipe_phrases/<lang>.json` carries a `"sections": {"ingredients": [...], "method":
  [...]}` block (built by `scripts/translate_recipe_phrases.py`). `recipe_sections(lang)`
  reads the `system_config` lists for `en` and the pack's `sections` block for any other
  language. **So: to tune English, edit system_config; to tune Greek, edit `el.json`.**

### Known English-only gaps (the honest limitations)
These three currently only work for English/base and would under-serve a non-English-base
instance until extended:

1. **Method-verb fallback** — gated to `"en" in seen` in `has_recipe_structure`. A non-English
   page with a header-less prose method won't get the verb rescue. *To extend:* add a
   `"method_verbs"` + `"method_verb_min"` to each language pack (or a per-language config) and
   have `_method_verb_count` pick the page-language list.
2. **Snippet recipe anchors** (`snippet_recipe_anchors`) are English terms; on a non-English
   page they won't match, so `_smart_snippet` falls through to the comment back-anchor or the
   head. *To extend:* source anchors from the page-language pack too.
3. **Comment back-anchor** (`snippet_comment_markers`) are English phrases ("leave a comment");
   a French/German blog's "laisser un commentaire" / "Kommentar hinterlassen" won't be caught.
   *To extend:* move comment markers into the language packs, or keep a per-language
   system_config list.

**Design principle for i18n here:** English/base structural vocab → `system_config` (the
system record); every other language's equivalent → its `recipe_phrases/<lang>.json` pack.
When a non-English-base deployment needs the verb-fallback / snippet / comment features, promote
those three lists into the per-language packs rather than duplicating them in code.
