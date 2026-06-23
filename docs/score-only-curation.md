# Score-only manual curation (for anti-bot / expensive publishers)

> Status: **SPEC** (2026-06-23). Not built. Design agreed in conversation; this is the
> shape + the build checklist.

## The problem

A publisher that anti-bot-blocks server fetches (e.g. kalofagas.ca — serves an 840-char
`NOINDEX` stub to every non-browser request) can only be fetched through the **paid
unblocker with render=True** (~20–30s + bandwidth per page). The normal harvest
**render-verifies every candidate** to decide "is it a recipe" — so picking 10 winners
out of a 180-URL SEMrush export costs **~180 paid renders**. That's the wrong trade.

## The insight — Moz is free of fetching; the human's browser isn't blocked

- **Moz DA/PA is URL-only** — no page fetch, no render. So we can score (and fully rank
  via the system-wide `rank_score`, which only needs DA+PA) **every** SEMrush URL for
  **zero renders**.
- **Scoring already ranks worthiness** — the top-N by score *are* the top-N worthy; the
  human judges from the **URL slug** (`/classic-greek-pork-chops/` is self-evidently
  worthy) + the score, no fetch needed.
- **Rendering is only needed to ingest a recipe's content** — so spend it **only on what
  the human picks**, one render per chosen recipe. Human labor (review + click) replaces
  automated render-verify; the only standing cost is the cheap Moz ranking.

## The model

1. **Score-only harvest.** Take the SEMrush URLs → run the free pre-filters
   (archive/collection/url-word) → **Moz-score all** → compute `rank_score` → store **all**
   in `collection_members` (ranked; top-N marked `selected`). **No fetch, no render, no
   unblocker.** This is the existing harvest with the **render-verify step skipped**
   (`check_recipe=False`) and **no auto-ingest**.
2. **The cohort view is the worklist.** The existing "☰ Scored cohort" panel already
   renders the ledger ranked with DA/PA + a source link. Each row gets one action:
   **Open in editor** (deep-links the URL into the recipe form's extract-from-URL).
3. **Click = curate + pay.** Opening a row in the editor fires extract-from-URL, which
   **already routes a `fetch_strategy='unblocker'` domain through the unblocker** (render
   per `render_required`) → the real recipe populates the editor → the human reviews/edits
   → **Save → master**. "As if it came from the browser page" — identical editor flow to a
   paste/bookmarklet, just originated from the worklist.
4. **Status self-updates.** The cohort view LEFT-JOINs `master_recipes` on `url_normalized`,
   so a saved recipe's row flips to **"in corpus"** automatically — the worklist shows
   what's done with no extra plumbing.

**Cost:** Moz ranking (cheap, metered, no renders) + **1 unblocker render per recipe the
human actually clicks**. Unclicked rows cost nothing.

## What already exists (reuse, don't build)

- `collection_members` ledger stores `(url, da, pa, rank_score, selected)` — the worklist.
- The **scored-cohort view** (`/domains/{d}/top?all=1` + the toggle) renders it ranked.
- **Editor extract → unblocker is DONE:** `extract_recipe_from_url` (save_recipe_api ~6597)
  resolves the domain's `fetch_strategy`/`render_required` and passes `unblocker`/`render`
  to the fetch. Verified live: kalofagas (`unblocker` + `render_required=1`, the latter
  **auto-learned** by its first harvest) extracts the real recipe.
- **dish_keywords** already captures the SEMrush Top Keyword + traffic per URL — extra
  worthiness signal to show on each worklist row.
- The **"in corpus" URL-join** gives free done/not-done status.

## To build (small)

- **(a) `score_only` harvest mode** — `harvest_publisher_top` / the `publisher_refresh`
  job: when set, skip the recipe-verify fetch loop and the auto-extract; just pre-filter →
  Moz-score all → `rank_score` → `replace_members` (all, top-N `selected`). Essentially
  `check_recipe=False` + `ingest=False`.
- **(b) Worklist surfacing** — make the scored-cohort view first-class for these domains
  (default to it; sortable by score) and add the per-row **Open in editor** deep-link
  (recipe form with `?url=…` auto-running extract).
- **(c) Editor extract → unblocker** — *already wired.* Only caveat: a JS-blocked site
  needs `render_required=1` (auto-learned from a prior harvest, or one manual tick) or the
  click extracts the stub. Optional hardening: give the editor extract the same
  thin-shell→render=True escalation the harvest filter has, so a never-harvested score-only
  domain self-heals on the first click.

## Open decisions

- **Trigger for score-only mode**: a per-domain toggle, vs auto-on when
  `fetch_strategy='unblocker'`? (Leaning: auto-on for `unblocker`, with an override — that's
  exactly where render-to-verify is the expensive mistake.)
- **Worklist home**: extend the cohort panel in `domains.html`, or a dedicated worklist
  page? (Leaning: extend the cohort panel — it already exists.)
- **Deep-link shape**: `recipe_form_styled.html?extractUrl=<url>` that auto-fires extract,
  vs a staged "Extract" the human presses. (Leaning: stage it, so the human is in control
  before spending the render.)

## Out of scope (later)

- Bulk "open next N" / queue mode.
- A pure **free-bookmarklet** path (human opens the page themselves + bookmarklets) — viable
  since the browser isn't blocked, but the editor deep-link is smoother and the unblocker
  cost is bounded to clicked items anyway.
- The **anti-bot hint** (when a normal harvest hits ~100% zero-score drops, nudge: "looks
  anti-bot — switch this domain to score-only / unblocker"). Small, complementary; folds in
  here as the on-ramp to this mode.
