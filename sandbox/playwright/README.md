# Playwright sandbox

Scratch space for getting familiar with Playwright before committing to
it as the production fetcher fallback. **Nothing here is imported by the
main app.** Treat each script as a one-off probe — copy, hack, rerun.

## Why this exists

The batch recipe-refresh pipeline (`extract_recipe_from_url`) uses a
plain `requests.get()` to fetch source pages. Two failure modes that
plain HTTP can't fix:

1. **Anti-bot 403** — sites like cleanfoodiecravings.com return 403 to
   bare `requests` UA strings but render fine for a real browser with
   cookies + JS.
2. **JS-rendered widgets** — some modern food blogs ship empty HTML
   shells and hydrate the recipe via JS. Our `bs4` parser sees no
   recipe content because there isn't any yet.

Both go away if we render the page in a headless Chromium. We already
solve this client-side via the bookmarklet (runs in the user's real
browser). Playwright is the same idea, server-side.

## Goals for this sandbox

- Get Playwright installed and Chromium downloaded.
- Render a known JS-heavy recipe page and dump the resolved DOM.
- Run the bookmarklet's `pickBestRoot()` server-side via
  `page.evaluate(picker_js)` and confirm we get the same root as the
  bookmarklet does.
- Measure: per-page latency, memory footprint, concurrency ceiling.
- Decide: does this graduate into a `to_markdown/playwright_fetch.py`
  fallback that `extract_recipe_from_url` calls when plain fetch fails?

## What's here

- `01_install.md` — install steps (one-time).
- `02_smoke.py` — open a page, dump title + HTML length.
- (later) `03_picker_via_evaluate.py` — inject pickBestRoot JS, score.
- (later) `04_anti_bot_test.py` — try the sites we currently 403 on.

## What's NOT here (yet)

- Production integration. No wiring into `extract_recipe_from_url`.
- Context pooling, retry policy, anti-detection tweaks. All later.
- Docker / Fly.io packaging. Defer until the hosting move.
