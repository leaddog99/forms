# Split — Autonomous Decision Log

Running log of judgment calls made while executing the split (SplitSpec.md +
docs/split-phase1-map.md) autonomously. Each entry: the call, and *why*. The
user is offline; these are for review on return.

Format: `DL-n (date) — decision — rationale`.

---

### DL-1 (2026-06-05) — Work on a feature branch `split/enrichment-api`, not master.
A multi-hour strangler refactor can destabilize; the user explicitly cares about
"a functional master." Branch keeps master shippable; merge when proven. Master's
last good state is the caching work + docs (already pushed).

### DL-2 (2026-06-05) — Strangler by delegation, not code-move (yet).
`recipe_enrichment.enrich()` *imports and calls* the existing monolith functions
(`extract/*`, `sanitize`, serializer) rather than moving their code into the
package. Rationale: one implementation, zero behavior change, lowest risk. The
physical code-move into the package is a later step, once the seam is proven and
each path is rerouted. (SplitSpec Phase 2 build-up philosophy.)

### DL-3 (2026-06-05) — Verification bar: NOT "byte-identical" for fresh extracts.
`markdown_to_recipe` is a non-deterministic LLM call, so two fresh extracts can
never be byte-identical — that bar was wrong. Replaced with: (a) **equivalence by
construction** — enrich() calls the *same* functions with the *same* args, so the
rerouted path is the same computation; (b) a **live smoke test** against the
running server; (c) **structural validity** of the returned recipe; (d) the
deterministic **cache-hit** path stays genuinely identical. Code review covers
the rest.

### DL-4 (2026-06-05) — First carve = extraction only; identity/translate/enrich/embed deferred.
`enrich()` v1 owns ONLY the extract step (JSON-LD fast lane → markdown LLM
fallback, same selection the monolith used inline) + the profile seal. Identity
card, extraction-stage translation, the enrich blocks, sanitize, and embedding
generation are carved in LATER, one at a time, each verified. Rationale: narrowest
safe slice proves the seam with near-zero blast radius; widen incrementally.

### DL-5 (2026-06-05) — BYOK deferred for customer-zero (in-process).
In-process, the existing Anthropic client reads `ANTHROPIC_API_KEY` from env, so
`llm_key=None` and nothing changes. BYOK-over-the-wire (caller passes an encrypted
key) belongs to the HTTP surface (`service.py`), built when the network boundary
goes up — not needed while the monolith calls enrich() in-process.

### DL-6 (2026-06-05) — Reroute behind an OFF-by-default flag.
The monolith chooses inline-vs-API via a single env flag
(`BCC_ENRICHMENT_API=1`), default OFF. With it off, behavior is byte-for-byte the
current code (the new path is dead). Flip it on to exercise the seam; flip off to
revert instantly. No deletion of inline code until the flag has run on in
practice (then the bypassed code is the empirical cut — SplitSpec Phase 3 gate).

### DL-7 (2026-06-05) — First-cut `corpus_public_view` (the `public` seal) — whitelist.
Implemented the seal serializer now (it's base architecture, "what doesn't come
back"), as a strict WHITELIST per SplitSpec §131. Emits: rich-result envelope
(name/image/times/yield/rating/nutrition/author/publisher) + our work-product
(editorial/classification/provenance/_scoring/_identity/_master) + keys
(originalUrl/domain/dates) + a MANDATORY source link (refuses to emit without
one). SEALS by omission: recipeIngredient, recipeInstructions, raw description &
notes prose, video, equipment, the JSON-LD blob, AND our cooped `previewImage`
(conservative — the cooped copy is the *local* image-rights posture per §149;
public uses thumbnail-and-link via the original `image` URL). Marked to revisit:
whether to emit a short attributed publisher-description snippet, and the
thumbnail policy. The `public` profile is not yet exercised (extract path uses
`full`); building it now so the chokepoint exists.

### DL-8 (2026-06-05) — Verified at unit level; did NOT restart the live server onto the branch.
The reroute is verified by (a) `enrich()` producing a valid recipe through the
real `jsonld_to_recipe`/`markdown_to_recipe` (jsonld path is deterministic — ran
it), (b) `_extract_via_enrichment_api` producing a valid recipe + correct
`path_used` + merged trace, (c) `save_recipe_api` imports with the flag defaulting
False. I did NOT restart the running dev server onto this branch with
`BCC_ENRICHMENT_API=1`: the server currently serves master (the caching work) and
the user may rely on it; restarting onto a branch would leave it in a surprising
state, and the `.bat` restart is finicky through PowerShell. A live flag-on smoke
test is the recommended next step on the user's return (or on merge) — risk is
nil because the flag is OFF by default.

### DL-9 (2026-06-05) — Stop carving after the EXTRACT step this session; build the HTTP surface instead.
Further carves (identity, translate, sanitize, enrich-blocks) touch
cache-entangled, tail-ordered code that really wants a live flag-on verification I
chose not to run (DL-8). Rather than stack partially-verified refactors of the
running path while unsupervised, I'm leaving a clean, proven, flag-gated extract
carve + a documented carve plan, and spending remaining time on ZERO-RISK new
code: the HTTP surface (`service.py`) and package tests. Both advance the base
architecture (the real network boundary + durable verification) without touching
the monolith's hot path.

### DL-12 (2026-06-06) — Non-English: translate the JSON-LD, don't discard it (use the fast lane).
User insight: "if there's a JSON-LD component we can use its info to feed our
algos." The old non-English path translated the markdown and DROPPED the JSON-LD
(`md_result["jsonld"]=[]`), forcing the ~17s markdown-LLM. Now `enrich()` does a
translate step BEFORE extract: if the page is non-English and ships JSON-LD, it
translates the JSON-LD's STRING VALUES to English (one Haiku call via
intake.translate's recipe-aware prompt, structure preserved) and runs the FAST
LANE on it (jsonld-direct, ~1s). Falls back to translating the markdown for the
LLM path if JSON-LD translation fails/mangles. Original preserved on
`_source.originalLanguage`/`originalTitle` (the two-flavor goal). The monolith's
URL-path translation is gated on `not _USE_ENRICHMENT_API` (flag off = legacy
translate-then-drop; flag on = enrich does JSON-LD-first). New API helpers:
`_translate_jsonld_for_fastlane`, `_translate_markdown_for_llm`,
`_extract_json_block`. Verified live: Greek Spanakopita JSON-LD → English via
jsonld-direct, provenance stamped. (api-only + a one-line monolith gate.)

### DL-13 (2026-06-06) — Translation target is the INSTANCE/USER language, not "English".
User: "what if the product is Greek, the user is Greek, the recipe is Greek?" — then
translating to English is wrong. Reframed: translate IFF `page_language != target_language`,
where target = per-user-preference -> instance-default -> "en". Added
`target_language` to EnrichmentRequest + service EnrichBody + the harness; the
monolith reroute passes `INSTANCE_TARGET_LANGUAGE` (env `BCC_TARGET_LANGUAGE`,
default "en"; per-user override is a TODO). Default "en" makes behavior IDENTICAL
to before (source!=en == old is_non_english), so nothing breaks — the new path
only triggers for a non-en target. CAVEAT: the recipe-aware translator targets
English only today, so a non-English TARGET (e.g. en->el) is not yet translated —
we skip + log rather than mistranslate; the common localized case (Greek instance
+ Greek recipe) needs no translation anyway (source==target). Verified live:
el+target el -> Greek kept (no translation, jsonld-direct); el+target en ->
English. Portable-package aligned ([[project_portable_package]]); generalizing the
translator to arbitrary targets is the follow-up.

### Noted follow-ups (small, deferred)
- **Translate to non-English targets:** generalize intake.translate (its prompt
  targets English) so a Greek/Italian/etc. instance can translate FOREIGN recipes
  INTO its language (en->el). Today only ->en is supported; non-en targets skip.
- **Per-user target language:** wire a user language preference (form/Ghost) so
  one instance serves mixed locales; today the monolith uses the instance default.
- **Staged/bookmarklet path not carved yet (2026-06-06):** the misko.gr case
  (and any bot-blocked site) flows through the STAGED path (`/extract-from-markdown`),
  which is NOT yet rerouted through `enrich()` — so the JSON-LD-translation win
  above only applies to the URL-paste path until the staged path is carved
  (carve-plan #6). Do that so the bookmarklet benefits.
- **Playwright browser-simulation (user wants to revisit):** drive a real
  (headless) browser server-side so the corpus crawl can fetch bot-protected /
  Cloudflare-challenged pages (bostonchefs, misko.gr) that block our direct
  fetch. Distinct from the bookmarklet (user session). Relates to SplitSpec
  §177-181 (the corpus is the bot; respect robots/UA/rate-limit). Restart this
  convo later.
- **Extract error message wording (2026-06-06):** when the SOURCE site bot-blocks
  our server fetch (e.g. bostonchefs/Cloudflare), the endpoint returns 502 and
  Cloudflare-in-front-of-tbotb.com repaints it as a branded 502 page; the form's
  sniffer then says "Cloudflare/bot challenge on the source page" — right
  conclusion, confusing finger-point (it names the tunnel's Cloudflare). Reword to
  plainly: "the source site blocked our fetch — use the bookmarklet." Low priority.

### Carve plan (for review — order of future reroutes)
1. ✅ extract (jsonld/markdown selection) — DONE, flag-gated.
2. identity card — move `generate_identity_card_for_recipe` into `enrich()`
   (`do_identity`); make the tail's `_attach_identity_card` the idempotent
   fallback. Watch the cache interaction (identity is cached pre-write).
3. extraction-stage translation — move into `enrich()` (`page_language`).
4. sanitize + the enrich blocks (`enrich={...}` by registry name).
5. embedding generation (`do_embed`).
6. the other extract endpoints: image / pdf / staged-markdown.
7. the batch (TBOTB) path → its own fetch + the SAME `enrich()`.
Each: reroute behind the flag, verify, then delete the bypassed inline code
(the empirical cut, SplitSpec Phase 3 gate).

### DL-10 (2026-06-06) — JSON-LD fast-lane fix + pydantic-error logging, API-ONLY (user directive).
Live test showed a fresh Sally's extract took 24s on the `markdown-llm` path even
though the page shipped JSON-LD (`has_jsonld=True`). Root cause (diagnosed from
the page): `RecipeModel` rejects `video.thumbnailUrl` when it's a LIST of URLs
(it wants a string) — one peripheral field sank the whole fast lane, forcing the
~17s LLM fallback. The user directed: **fix it ONLY in the API code**, not the
legacy `jsonld_to_recipe`/monolith — so the API path becomes strictly faster on
these pages (another reason to migrate; the inline path keeps the bug until cut).

Fix (in `recipe_enrichment/api.py`):
- `_coerce_jsonld_for_fastlane()` — normalizes `video` before the fast lane
  (list→single dict; `thumbnailUrl` list→string; unusable shape→dropped).
  Confirmed: Sally's now extracts via `jsonld-direct` (~1s vs ~25s).
- drop-`video` retry as a safety net before paying for the LLM.

Logging (user: "any pydantic error should be logged.. we're still finding them"):
`jsonld_to_recipe` builds `RecipeModel.model_validate` and SWALLOWS the
`ValidationError` (prints a buried multi-line blob, returns None), so the API
never saw which field failed. Added `_log_fastlane_validation_errors()` — on every
fast-lane miss it replays flatten+sanitize+validate and logs ONE greppable line
with the exact fields: `PYDANTIC FAST-LANE MISS -> ~17s LLM. url=... fields=[video.VideoObject.thumbnailUrl=string_type; ...]`.
Note the ValidationError can originate in `sanitize_recipe_data` (it validates)
OR `model_validate` — caught either way. Best-effort: never breaks the extract.
Verified + 4 new regression tests (12/12 pass).

### DL-11 (2026-06-06) — Enrich carve: /enrich-recipe through the API, INDIVIDUALLY selectable blocks.
User requirement: "each item in enrich (critique, etc) should be selectable
individually.. there will be more." The legacy `enrich_recipe()` runs ALL blocks
all-or-nothing, so the API needed its own per-block runner.

- `run_enrichment_blocks(recipe, block_names=None, *, llm_key=None)` — runs only
  the SELECTED blocks (None=all), registry-driven off `ENRICHMENT_BLOCKS`, one
  parallel LLM call per block, per-block failure isolated. Delegates to the
  existing `_run_block`/`_build_user_prompt` (strangler: one implementation).
- `available_enrichment_blocks()` — enumerates the registry so the form/UI can
  render a checkbox per block; "there will be more" needs no code change here.
- `enrich()` now runs `req.enrich` (the selected set) after extract.
- `/enrich-recipe` reroutes through `run_enrichment_blocks(recipe, payload["blocks"])`
  behind `BCC_ENRICHMENT_API` (default OFF -> legacy all-blocks). `blocks` omitted
  -> all (back-comat). path = "enrich-api" when on.
- BYOK accepted but not yet injected (DL-5); the form UI for per-block checkboxes
  is a separate Local task (the API + endpoint support it now).
- Verified: selection logic (empty/unknown -> no LLM), single-block isolation;
  2 new tests (14/14). Live LLM path verified via the running server.
