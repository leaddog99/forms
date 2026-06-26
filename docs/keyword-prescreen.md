# Non-English recipe filtering — free per-language phrase scoring + structural is-recipe gate

> **FINAL DESIGN (2026-06-26): fully LLM-FREE for languages with a phrase pack.**
> The recipe filter, for a non-English page with no JSON-LD, no longer translates the page.
> It scores the RAW text against the language's phrase pack (`intake/recipe_phrases/<lang>.json`,
> built once) and decides keep/drop with a **structural gate**: a real recipe has BOTH an
> **ingredients section** AND a **method section** (`validators.has_recipe_structure`,
> accent-insensitive so `ΥΛΙΚΑ`/`υλικά` both match), in the instance BASE language OR the page
> language. Vocabulary-rich guides/tips/roundups have at most one section → dropped (they used
> to false-keep on a raw phrase COUNT, e.g. a Greek pork-tenderloin guide scored 17). The raw
> count (`recipe_score`) is still stamped for ranking/training, but it is NOT the keep decision.
>
> The **LLM keyword pre-screen below is now OPTIONAL and OFF by default** (`keyword_prescreen_default`).
> It only ever saved the *fetch* on junk; the fetch is cheap now that translation is gone, and the
> free structural gate makes the same recipe-vs-guide call after fetching. Turn it back on only for
> big/blocked harvests where skipping fetches is worth one batched Haiku call.
>
> Scoring language = curator-set `domains.language` (normalized, `gr`→`el`), defaulting to the
> instance base language; the base list is always scored and the page-language list is added when
> different (scored once when the same). Per-language packs are made by
> `scripts/translate_recipe_phrases.py <lang>` (one offline call → phrases + section headers).

---

## (Original) URL keyword pre-screen — cut the per-URL translation cost on non-English mixed sites

## Problem (observed on `meatandgrillstories.com`, a Greek grill site)

A publisher harvest with `check_recipe=True` on a non-English site pays, **per candidate
URL**, a full page fetch **plus a whole-page Haiku translation** (`translate_markdown`,
entire markdown in → entire English markdown out) just to phrase-score "is this a recipe".
Measured ≈ **40 s/URL** in the recipe-FILTER stage (job #374), before the heavy extract of
the winners even begins. The pages have **no `schema.org/Recipe` JSON-LD**, so none take the
cheap `KEEP json-ld` fast path — every one hits the expensive translate branch.

The site is **mixed**: tips/guides ("όλα τα μυστικά για…" = *all the secrets for…*,
"συμβουλές για…" = *advice for…*, "τα πάντα για…" = *everything about…*) interleaved with
real dish recipes ("η συνταγή για…" = *the recipe for…*). So we can't just disable
`check_recipe` — the filter is correctly dropping the non-recipes. We're paying the
translation tax to discover what a human can tell from the **URL slug** alone.

## Why the existing prefilters don't solve it

- **`url_prefilter` (food-presence skip, `url_word_lists.url_lacks_recipe_signal`)** drops a
  URL only when its path has **no food token**. On this site the dish noun (`kontosouvli`,
  `kalamari`, `mpiftekia`) appears in **both** the recipe and the tips pages — so the
  discriminator is the *modifier* (`μυστικά`/secrets vs `συνταγή`/recipe), which a
  food-presence gate can't see. Turning it on would either keep everything (dish noun
  present) or, on transliterated slugs the vocab hasn't learned, drop everything. It's the
  wrong tool for recipe-vs-tips.
- **Title translate + collection guard** — the original idea was to translate the SEMrush
  *title* and prefilter on it. **Dead for this export format:** the SEMrush *PagesV3*
  organic export has **no Title column at all** (0% present). The only pre-fetch text is the
  URL **slug** (100%) and the **Top Keyword** column (≈53% present, real Greek query, e.g.
  `συκωτι συνταγεσ` / `μπιφτεκια επαγγελματικη συνταγη`).

## Design — a confidence-gated LLM pre-screen on slug + Top Keyword

A new, cheap, **pre-fetch** tier between the free local food-word prefilter and the
expensive fetch+full-page translate:

1. **Batch** all candidate URLs into **one** Haiku call (`intake/url_prescreen.py`). Per
   URL we send the **slug** (always) and the **Top Keyword** (when `dish_keywords` has it).
   The model reads transliterated Greek / any language natively.
2. The model returns a verdict per URL: `recipe` | `not` | `unsure`. It is told to be
   **conservative** — only `not` when clearly a non-recipe (buying guide, technique/tips
   article, "everything about X", product/category/tag/about page); anything that could
   plausibly be a recipe (incl. "10 ways to cook X" listicles, "how to make X") → `unsure`.
3. In `_is_recipe_filter`, **only a `not` verdict drops the URL pre-fetch** (reason
   `kw-prescreen`). `recipe`/`unsure` fall through to the unchanged fetch + translate +
   phrase-score. So the pre-screen can only **save** translations on confident non-recipes;
   it can never *admit* a page the body check would reject, and (being negative-only +
   conservative) it minimizes false drops of real recipes.
4. **ε-exploration** (reuse the `url_prefilter_explore_rate` knob): a small random fraction
   of would-be `not` drops are fetched+verified anyway, to keep minting unbiased is-recipe
   labels in the region the pre-screen skips (same rationale as the food-word prefilter; see
   `docs/corpus-ml-strategy.md`).

### Why negative-only / conservative

On a mixed site a slug can't *prove* a recipe ("πώς να μαγειρέψετε…" / "how to cook…" is
either), but it can *disprove* one ("όλα τα μυστικά…" is a guide). Dropping only on
confidence preserves recall: a real recipe mislabeled `unsure` just costs one translation
(status quo), whereas a false `not` would lose it. Validated on the meatandgrill 10: the 4
clear "secrets/advice/everything-about" pages drop pre-fetch; the genuine recipe and the
ambiguous listicle stay → **0 false drops, 4/7 translations saved**.

## Config / wiring

- `system_config.keyword_prescreen_default` (bool, default **off**) — global enable, flippable
  in the admin system-config editor so it can be tried on a Greek run without a deploy.
- `harvest_publisher_top(..., keyword_prescreen=None)` → resolves to the config default when
  `None`; threads into `_is_recipe_filter(..., keyword_prescreen=…)`.
- Keyword map: `dish_keywords.keywords_for_domain(domain)` (raw `url` → `keyword`), already
  populated by the SEMrush loader's capture step.
- **Cost:** one Haiku tool call per harvest (a few hundred short items), ≈ a fraction of a
  cent — vs the ~40 s + full-page translate it saves on each confident drop.

## Phase 2 (later)

- **Per-domain UI toggle** on `domains.html` + a harvest MODE preset (the score/curate modes
  already preset flags — add prescreen to the "blocked/mixed" presets), and thread it through
  the refresh-top endpoint + job params like `url_prefilter`.
- **Self-learning convergence** ([[project_url_word_filter]]): feed the pre-screen's
  confident verdicts back as labels for the local url-word vocab (and the is-recipe corpus),
  so over time the *free* food-word lists learn this publisher's vocabulary and the LLM
  pre-screen is needed less. Same capture-now/promote-later discipline as `dish_keywords`.
- **English sites:** the pre-screen is language-agnostic and would also cheaply shed
  obvious non-recipes before the fetch on English mixed publishers — but the win is largest
  where the post-fetch path is a full-page translation. Default scope = non-English / mixed.
