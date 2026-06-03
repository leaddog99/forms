# The Extract Cache — How It Works

This document explains, in plain English, the caching system behind recipe
extraction: what it caches, what it deliberately does *not*, how it decides a
"hit," where the bytes live, and why a cache hit is fast.

Audience: anyone touching `extract_recipe_from_url`, the extract endpoints, the
cache layer, or the screenshot store.

Source files:
- `input/pipeline/extract_cache.py` — the cache table + freshness logic
- `save_recipe_api.py` — the orchestrator (`extract_recipe_from_url`), the
  cache lookup/write wrappers, the enrichment tail, the `/screenshot/{id}`
  endpoint
- `input/pipeline/screenshot_pipeline.py` — the page-screenshot BLOB store
- `recipe_model.py` — `static_subset` (the cache/claim boundary)
- `input/pipeline/url_utils.py` — `normalize_url`

---

## 1. The one-sentence version

When you extract a recipe from a URL, the expensive part — running an LLM over
the page to produce a structured recipe — is saved in a database keyed by the
**normalized URL**, so the next person who extracts the *same* URL gets the
result back in well under a second instead of ~25 seconds, without paying for
the LLM call again.

---

## 2. What is actually cached (and what is not)

The cache stores **one thing**: the structured recipe produced by the extraction
pipeline (the "LLM extract"). That is the costly, stable-per-URL artifact.

It does **not** cache the network fetch or HTML parse. Those are cheap (~60ms
fetch + ~800ms parse) and, on the normal path, they have to run anyway to
discover the page's canonical URL before we even know which cache row to look
at. (The one exception is the *speculative fast-path* in §8, which skips them.)

Two physical stores are involved:

| Store | File | Git-tracked? | Holds |
|-------|------|--------------|-------|
| Extract cache | `recipes.db` → table `llm_extract_cache` | No (as of 2026-06-01) | The structured recipe JSON per URL |
| Screenshot store | `media.db` → table `page_screenshots` | No | The source-page screenshot as a JPEG BLOB per URL |

Neither DB is in git. `recipes.sql` (a text dump of `recipes.db`) is the
git-side backup; `media.db` is regenerable (a re-extract recreates any
screenshot), so it is simply never backed up to git.

---

## 3. The cache table

`llm_extract_cache` (created/maintained by `ensure_llm_extract_cache_table`):

| Column | Meaning |
|--------|---------|
| `url_normalized` | **PRIMARY KEY** — the canonical URL (see §4). One row per URL, period. |
| `model` | Which model produced this row (e.g. `claude-haiku-4-5`). |
| `prompt_version` | A 12-char hash of the extraction prompts (see §6). |
| `recipe_json` | The cached recipe — but only its *static subset* (see §7). |
| `semantic_fingerprint` | A hash of the recipe's load-bearing content, for drift detection (see §9). |
| `created_at` | When this row was written. **This is the TTL clock.** |
| `last_used_at`, `hit_count` | Present but intentionally **not** updated on a hit (a write on every read would defeat the fast read path). |

### Why the key is the URL alone

Earlier the key was `(url, model, prompt_version)`. That fragmented the cache: a
prompt edit created a *parallel* row instead of replacing the old one, so each
URL accumulated orphaned versions and the cache went cold after every prompt
change.

Now the key is `url_normalized` **alone**, and `model` / `prompt_version` are
ordinary columns used only for the freshness decision. A prompt or model change
still forces a fresh extraction (the comparison fails — see §5), but the
re-extract **overwrites** the one row. Result: exactly one live row per URL,
improvements propagate on the next touch, and the table doubles as a clean
"latest extraction per URL" library.

---

## 4. URL normalization (`normalize_url`)

The cache key is a *canonical* form of the URL so that trivially different URLs
share one row. `normalize_url` does:

- Lowercase the scheme and host; default scheme `https`.
- Strip a leading `www.`.
- Strip the default port (`:80` for http, `:443` for https).
- Strip a trailing slash (except the root `/`).
- Drop a blocklist of tracking params (`utm_*`, `fbclid`, `gclid`, `ref`,
  `igshid`, …). Unknown params (e.g. `?recipeId=42`) are **kept** — they may be
  load-bearing.
- Drop the `#fragment`.

So `https://www.Example.com/recipe/?utm_source=x#top` and
`https://example.com/recipe` collapse to the same key.

---

## 5. The freshness decision: hit / stale / miss / skip

When we look up a URL (`get_cached_extract` → `_extract_cache_lookup`), one of
four things happens:

- **skip** — no URL to key on, or it's one of our own BCC permalinks (those
  aren't externally extractable). Cache is bypassed.
- **miss** — no row exists for this URL. Extract from scratch, then write.
- **stale** — a row exists but can't be reused. Two ways to be stale:
  1. **Different pipeline** — the row's `model` or `prompt_version` doesn't
     match the current ones.
  2. **Expired** — `created_at` is older than the TTL (**30 days**, see
     `EXTRACT_CACHE_TTL_DAYS`).
  On stale, we re-extract and **overwrite** the row (resetting the clock).
- **hit** — the row exists, the pipeline matches, and it's within TTL. Serve it.

In code, the rule is simply:

```
HIT  iff  row.model == current_model
     AND  row.prompt_version == current_prompt_version
     AND  created_at is within 30 days
```

The 30-day TTL is deliberately matched to the Moz score refresh cadence
(`MOZ_REFRESH_TTL_DAYS`) so "stale" means the same thing across the system.

---

## 6. What flips `model` / `prompt_version`

- `EXTRACT_MODEL` is a constant (`"claude-haiku-4-5"`).
- `EXTRACT_PROMPT_VERSION` is `sha256(...)[:12]` of the **combined** extraction
  prompts: the markdown→recipe prompt + the enrich prompt + the
  image→markdown prompt + the pdf→markdown prompt.

So **editing any of those prompts changes the version**, which makes every
cached row stale on its next touch — they re-extract and overwrite with the
improved output. You never bump a version constant by hand; the hash does it.
The current value is printed at startup: `[CACHE] EXTRACT_PROMPT_VERSION = …`.

---

## 7. What gets stored: the "static subset"

We do **not** cache the whole recipe object. The write path runs it through
`static_subset` (`recipe_model.py`) first, which keeps only the **platonic,
URL-static** fields — the ones that describe *the dish*, not *this row* or
*this user*.

Kept (top-level): the schema.org recipe content (`name`, `recipeIngredient`,
`recipeInstructions`, times, yield, image, …), plus the LLM enrichment
(`provenance`, `classification`, `editorial`), `_scoring` (Moz PA/DA/OU),
`_batch` (curation lineage), and `_identity` (the dish identity card).

Kept (inside `_source`, whitelisted by `_SOURCE_STATIC_SUBKEYS`):
`type`, `origin`, `originalUrl`, the og: preview fields (`previewImage`,
`previewDescription`, `previewImageAlt`, `siteName`, `author`,
`publishedTime`, `modifiedTime`), translation provenance
(`originalLanguage`, `translated`, `translatedAt`, `originalTitle`), and
**`pageScreenshot`** (the `/screenshot/<id>` URL).

Dropped: per-row state (`id`, `user_id`, `recipe_id`), per-user state
(`_access`, `current_status`), claim provenance, personal affiliate links,
and ephemeral debug fields. Those are re-minted or re-stamped per request.

Why this matters: because `_identity` and `_source.pageScreenshot` are in the
whitelist, the identity card and the page screenshot **travel inside the cached
recipe**. That's the whole reason a cache hit can serve them for free (see §10).

---

## 8. The request lifecycle (full path)

For `extract_recipe_from_url(url)` on a normal request:

1. **Speculative fast-path probe** (§8a) — may short-circuit here.
2. **HEAD probe** for Content-Type (route PDFs vs HTML).
3. **Fetch + convert** the page to markdown, harvesting JSON-LD + og: meta.
   This yields the page's *resolved* `source_url`.
4. **Translate** to English if the page is non-English (and drop the
   original-language JSON-LD so extraction works from the translated prose).
5. **Normalize** the resolved URL → `url_norm`. **Look up the cache.**
6. **Branch:**
   - **Hit** → use the cached recipe. Note whether it's *complete* (§10).
   - **Miss/stale** → extract: JSON-LD-direct if the page shipped clean
     schema.org JSON-LD, otherwise the markdown→recipe LLM call. Stamp
     translation provenance.
7. **Enrichment tail** (runs for both hit and miss, but every step is
   idempotent/guarded so a complete hit re-does nothing):
   - `_attach_chapter` — cookbook chapter (keyword shortcut; no-op if set).
   - og: preview text + **coopt the og:image** (skipped if `previewImage` set).
   - **Moz scoring** (skipped for batch `pre_scored`).
   - **Identity card** (~2s Haiku; no-op if `_identity` already present).
   - **Page screenshot** (~3-5s; skipped if `pageScreenshot` already present) —
     stored as a BLOB in `media.db` (§11).
8. **Cache write** — `_extract_cache_write` runs **after** the enrichment tail,
   so the screenshot/identity/preview are part of what gets written. It writes
   on a fresh extract, or to *self-heal* an incomplete hit row (§10).
9. **Per-row finishing** (not cached): apply batch `pre_scored` / overrides,
   mint a fresh `id`, journal token usage, stamp source-drift.
10. **Stamp `total_ms`** (true wall-clock) and return.

> **Ordering is the crux.** Previously the cache write happened at step 6 and the
> enrichment tail at step 7+ — so the screenshot and identity card were produced
> *after* the row was saved and were never cached. Every "cache hit" then re-ran
> the ~2s identity call and the ~3-5s screenshot. Moving the write to step 8 is
> what makes a hit actually cheap.

### 8a. The speculative fast-path

The fetch+parse in steps 2-3 exist only to discover the resolved URL before the
cache lookup. But for a plainly-pasted URL, the resolved URL is almost always
identical to `normalize_url(input)`. So before doing any network work, we probe
the cache on the **normalized input URL**:

- If it's a **complete fresh hit** (`_cache_row_complete`, §10), we return it
  immediately — no HEAD, no fetch, no parse, no screenshot, no identity call.
  This is the genuine sub-second hit, flagged `fast_path: true` in the timings.
- If it misses, is stale, or is incomplete, we fall through to the full path,
  which keys on the *resolved* URL and still hits if only a redirect or a
  tracking param differed.

The fast-path is **disabled** for batch ingestion (`pre_scored` /
`batch_overrides`) and for `force_refresh`, which always take the full path so
their authoritative fields and fresh extracts apply.

The fast-path still re-stamps the cheap, per-row things: a fresh `id`, the
chapter (no-op if set), and Moz scoring (a cheap read from the metabase cache).
It only *skips* the expensive, already-cached work.

---

## 9. Drift detection (semantic fingerprint)

Each row stores a `semantic_fingerprint`: `sha256` of the recipe's load-bearing
content — `name` + `recipeIngredient[]` + instruction texts, lowercased.
Description/image/dates are excluded because they flip on the source page
without the recipe actually changing.

On a **TTL-expired, same-pipeline** re-extract, we compare the new fingerprint to
the old one. If they differ, the source page meaningfully changed: callers stamp
`recipes.source_changed_at` on saved copies, and the form shows a "source
updated — review and re-save" banner (cleared on save).

A model/prompt change is **not** treated as drift (the content didn't change, the
pipeline did), so the fingerprint comparison is suppressed in that case to avoid
false "source changed" flags.

---

## 10. Complete vs incomplete rows, and self-healing

A cached recipe is **complete** when it carries both the expensive URL-static
extras — `_source.pageScreenshot` and a populated `_identity` card
(`_cache_row_complete`).

- Rows written **after** the 2026-06-01 ordering fix are complete (the
  enrichment tail runs before the write).
- Rows written **before** it lack the screenshot/identity.

Incomplete rows self-heal: the fast-path won't serve them, so they fall to the
full path, which re-runs the (now-cached-before-write) enrichment and
**re-writes** the row. After that one pass the row is complete and all future
hits take the fast path. No migration needed — the cache fills itself in.

---

## 11. The screenshot BLOB store (`media.db`)

The source-page screenshot is a "this came from a real site with editorial
standards" signal. It's captured with headless Chromium (Playwright, run in a
subprocess for Windows event-loop reasons), above-the-fold, after a 1.5s settle.

Storage decisions (2026-06-01):

- It lives in a **separate `media.db`**, not `recipes.db`, so binary never
  bloats the git-tracked recipe DB.
- Table `page_screenshots(screenshot_id PK, url_normalized, jpeg BLOB,
  created_at)`.
- `screenshot_id = sha256(url_normalized)[:16]` — deterministic, so a re-extract
  of the same URL **overwrites one row** (dedup; one screenshot per URL, not per
  recipe).
- The image is shrunk to a compact JPEG (~800px wide, quality 65, ~30-60KB) —
  it doesn't need to be high quality.
- The recipe stores only the short URL `_source.pageScreenshot =
  "/screenshot/<id>"`. The endpoint **`GET /screenshot/{id}`** reads the BLOB and
  returns `image/jpeg`. A 404 means the BLOB is gone (e.g. `media.db` wiped) — a
  re-extract regenerates it.

Old screenshots produced before this change used the `image_store` backend and
have `/generated/...` URLs; those still resolve via the static mount, so the two
formats coexist.

---

## 12. Cache-hit accounting (token journal)

A cache hit appends a zero-token `cache_hit_markdown_to_recipe` entry to the
usage log (journaled per user). This lets cost reports total the tokens
*saved* by the cache alongside actual spend. (The fast-path is careful to
journal the hit only when it actually serves the row — it probes into a throwaway
log and merges it in only on a real hit, so a fall-through doesn't double-count.)

---

## 13. What is NOT cached / is re-done every time

Even on a hit, these run per request (cheap, or intentionally fresh):

- A fresh recipe `id` (UUID) is minted.
- Moz scoring is re-attached (cheap read from the `metabase_url` cache).
- The cookbook chapter is re-checked (no-op if already set).
- Token usage is journaled.
- Batch `pre_scored` scores and `batch_overrides` are applied **after** the
  write, so batch-specific values never pollute the shared cache row.

---

## 14. Cacheability guard

`_is_cacheable` refuses to write rows that look like a bad extraction (paywall,
404, wrong-recipe sidebar carousel): it requires a non-empty `name`, **≥2
ingredients**, and **≥2 instructions**. (The `/recipes` *save* gate uses stricter
≥3/≥3 thresholds, because junk in the recipes tables corrupts aggregate stats.)
BCC self-URLs are never cached either (they'd resolve to our own form HTML).

---

## 15. Forcing a refresh

- **Edit a prompt** → `EXTRACT_PROMPT_VERSION` flips → every row re-extracts on
  next touch.
- **`force_refresh=True`** (the proactive daily-refresh job) → discards an
  unexpired hit and re-extracts in place, keeping drift detection.
- **Wait 30 days** → TTL expiry → re-extract on next touch.
- **`scripts/refresh_expiring_cache.py`** proactively refreshes rows nearing
  expiry.

---

## 16. The other extract paths

The same cache (keyed by `url_normalized`) backs all the extract entry points:

- **URL** (`extract_recipe_from_url` / `POST /extract-from-url`) — the main
  interactive path; has the fast-path, the screenshot, and the
  enrichment-before-write ordering.
- **Image** and **PDF** uploads — cache only when the bookmarklet supplied a
  `source_url`; a hit skips both the vision/OCR step *and* the markdown-extract
  LLM call. (These paths have no page screenshot, and their identity card is
  still stamped after the write — a known parity follow-up.)
- **Staged markdown** (bookmarklet) — same cache, keyed by the staged
  `source_url`.

---

## 17. Timing panel reference

Fields the form shows under an extract, and what they mean:

| Field | Meaning |
|-------|---------|
| `path` | `cache-hit`, `jsonld-direct`, or `markdown-llm`. |
| `fast_path` | `true` when the speculative input-URL probe served it (no fetch). |
| `cache` | `hit` / `stale` / `miss` / `skip` / `written`. |
| `cache_key_url` | The `url_normalized` the row is keyed on. |
| `fetch_ms`, parse fields | Network fetch + HTML→markdown/JSON-LD time. |
| `moz_ms`, `identity_ms`, `screenshot_ms`, `image_coopt_ms` | Per-step times for the enrichment tail. |
| `total_ms` | **True wall-clock**, stamped just before return. |
| `source_drift` | Set when a TTL re-extract's fingerprint differs from the cached one. |

> **The "894ms but it took 6 seconds" bug, explained:** before 2026-06-01,
> `total_ms` was stamped right after the cache lookup, so it only measured
> fetch + parse (~894ms) and never the enrichment tail that ran afterward — and
> that tail (identity ~2s + screenshot ~3-5s) ran on *every* hit because it
> executed after the cache write and wasn't cached. The fix: run the tail before
> the write (so it's cached and skipped on hits), add the speculative fast-path
> (so a hit skips fetch+parse too), and stamp `total_ms` at the end (so the
> number is honest).

---

## 18. Operational notes

- **Files:** `recipes.db` and `media.db` live at the repo root, both git-ignored.
- **Backup:** `recipes.sql` (text dump) is the git-side backup of recipe data;
  `bcc_backup.bat` refreshes it and copies both DBs to the ADAM disk. `media.db`
  is regenerable and not backed up.
- **Restart:** the server has no `--reload`; changes to this code require
  `bcc_restart.bat` (watch for the Windows zombie-socket issue).
- **Rebuilds:** `ensure_llm_extract_cache_table` drops and rebuilds the cache
  table if it finds a legacy schema (old composite key or `markdown_hash` key) —
  the cache is fully recomputable, so this is safe.
