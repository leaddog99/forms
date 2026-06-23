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

## The model — one scored list, a human in the middle, two processing paths

**Shared front (identical for both paths).** SEMrush/SerpApi *supplies the URL list*;
**Moz scores them all** (URL-only → DA/PA → `rank_score`). Run the free pre-filters
(archive/collection/url-word) first; **no fetch, no render, no unblocker.** Store **all**
candidates in `collection_members`, ranked, **none auto-selected**. Render the list in a
form **sorted by `rank_score` desc, with a checkbox per row**. The human is now the man in
the middle of *selection*. The real structural change vs today's harvest: **Moz runs
BEFORE any fetch** (today it fetch-verifies first, then Moz the survivors).

Then the human checks the worthy rows (judging from the **slug** + score + the SEMrush
keyword/traffic) and picks a destination:

### Path #1 — "Process selected" (automated · paid · hands-off)  ← BUILD FIRST
The checked URLs go through the **existing batch ingest** — `extract_recipe_from_url`
(which already routes a `fetch_strategy='unblocker'` domain through the unblocker, render
per `render_required`) → save to `master_recipes` (`_master.kind='top'`). Everything
downstream is the *current* harvest code; we've only (a) gated it to the **checked** URLs
and (b) moved Moz earlier. **Cost: 1 unblocker render per *checked* URL** (vs one per
candidate today). Unchecked rows cost nothing but their Moz score.

### Path #2 — "Queue for manual capture" (manual · free · beats the bot)
The checked URLs form a **queue the human works in their own browser, one at a time**:
open the page (a real browser loads it fine — *not* blocked), the **bookmarklet** grabs the
live JSON-LD → editor → save to master. **Zero unblocker cost** — human labor replaces the
paid render. Honest limit: the capture is genuinely per-page manual (we can't auto-inject
the bookmarklet into a third-party page — cross-origin), but the app smooths it: present the
queue, "open next", and **auto-tick done** as each saves.

**Status self-updates (both paths).** The cohort/worklist view LEFT-JOINs `master_recipes`
on `url_normalized`, so a saved recipe's row flips to **"in corpus"** automatically.

**Pick per situation:** a big batch you trust → #1 (paid, hands-off); a handful of gems →
#2 (free, hands-on). Both start from the same scored, checkboxed list — the only divergence
is the button.

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

## To build

### Shared (both paths)
- **`score_only` harvest mode** — `harvest_publisher_top` + the `publisher_refresh` job:
  when set, force `check_recipe=False`, mark **no** members `selected`, and the job **skips
  the auto-extract** block. Net: pre-filter → Moz-score all → `rank_score` →
  `replace_members` (all candidates, `selected=0`). No fetch/render.
- **Worklist UI** — the scored-cohort panel in `domains.html` gets a **checkbox per row**
  (non-ingested rows) + a sticky action bar: **"Process selected (unblocker)"** [#1] and
  later **"Queue for manual capture"** [#2]. Sorted by `rank_score` desc; shows
  slug/score/DA-PA/keyword. "in corpus" rows are disabled (already done).

### Path #1 (BUILD NOW)
- **`POST /domains/{domain}/process-selected`** `{urls:[…]}` → enqueue a `process_selected`
  job + spawn out-of-process; in-flight-deduped; returns job id + stream url.
- **`process_selected` job handler** — loop the given URLs through the **shared per-URL
  extract→master helper** (`extract_recipe_from_url` force-refresh → save-gate →
  render-retry → `_save_recipe_core` with `_master.kind='top'`, `publisher=host`), **append**
  (don't retire the publisher's existing master block), and **mark `selected=1`** in the
  ledger for each saved URL so the worklist reflects it.
- **Factor the per-URL extract+save** out of `_handle_publisher_refresh_job`'s existing
  auto-extract block into that shared helper (single-path — the harvest winner-extract and
  process-selected must not drift).
- **Editor extract → unblocker is already wired** (`extract_recipe_from_url` ~6597 resolves
  `fetch_strategy`/`render_required`). A JS-blocked site needs `render_required=1`
  (auto-learned by a prior harvest, or one tick).

### Path #2 — manual capture queue (free)

**The automation ceiling (thought through 2026-06-23).** Reading a rendered, anti-bot page
needs code running in the page's *own origin*. A plain web app **cannot inject script into
a third-party tab it opens** (cross-origin) — so a bookmarklet's click is irreducible *in a
web app*. The ways to run code automatically on the page, best-first for a self-hoster:

| Approach | Clicks/page | Free | Install | Robust |
|---|---|---|---|---|
| **Userscript (Tampermonkey)** | **0** | ✅ | TM + 1 script (once) | ✅ real browser |
| **Browser extension** | **0** | ✅ | our extension (once) | ✅ |
| **Self-advancing bookmarklet** | 1 (auto-advances) | ✅ | none | ✅ |
| **App-driven queue + current bookmarklet** | 1 | ✅ | none | ✅ |
| **Local Playwright "free unblocker"** | 0 | ✅ | — | ❌ **trap** |

- **Recommended zero-click: a userscript** — it's the existing bookmarklet **plus**
  "fetch next from the queue → extract → POST to master → navigate to next". Tampermonkey
  auto-runs it on `publisher/*`, so after "Go" it's hands-off, free, and undetectable (it's
  the user's real Chrome). One-time TM install. This is the real "automate the bookmarklet".
- **Local Playwright is a trap:** it's *detectable* automation (`navigator.webdriver`/CDP) —
  the exact thing the paid Oxylabs unblocker (Path #1) exists to defeat. If it beat these
  blocks we wouldn't need Path #1. #2's whole value is the *genuine human browser*.

**Shipped tonight (ROUGH PROTOTYPE — UI redesign pending):** the no-install version — a
client-side **capture queue** (`captureQueue()` in `domains.html`): check rows → **📋 Queue
(manual)** → a floating panel **opens each page** ("Open next"), the human clicks their
bookmarklet, and it **auto-advances by polling `/top`** (a URL flips to `ingested` once the
bookmarklet saves it to master). No server/fetch; reuses the existing bookmarklet + the
URL-join for done-tracking. NEXT: promote to a **userscript** for true zero-click.

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
