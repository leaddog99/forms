# Capture folder contract

*Signed-in page captures as files. Written 2026-09-09 for the capture walker
(membership sites: mollybaz.com's CLUB, 177milkstreet.com). Restore point:
git tag `pre-capture-walker`.*

## Why files

A members-only publisher's recipe body sits behind a login the server does not
have. The unblocker fetches the real page and it holds a teaser (job 1884). The
only reader that sees the recipe is a browser that is signed in — today, the
curator's, via the bookmarklet, one click per page.

The walker automates that click: a persistent Chromium profile the curator
signed into once, visiting a URL list, capturing each page. It writes what it
captured to a folder rather than posting it, so capture and processing are
decoupled: the browser session is not needed to process, the server is not
needed to capture, a crashed run leaves inspectable files, and the batch
ingests the folder at its own pace. One capture, three consumers (stage →
extract → save) — the SAME ones the bookmarklet feeds.

## Layout

```
input/captures/<host>/<slug>.md        # front matter + markdown body (below)
input/captures/<host>/<slug>.html      # the rendered DOM as captured (raw, verbatim)
input/captures/<host>/<slug>.jpg       # hero image saved WITH the session's cookies
input/captures/<host>/_walk.log        # one line per visit (append-only)
data/browser_profiles/<host>/          # persistent Chromium profile (never in git)
```

`<host>` is the canonical host (no `www.`). `<slug>` is the URL path with `/`
→ `__`, trimmed, lowercased, at most 120 chars; a trailing 8-hex hash of the
normalized URL disambiguates. Both folders are git-ignored.

## The .md file

```
---
contract: bcc-capture/1
source_url: https://mollybaz.com/salty-honey-smacks/
url_normalized: https://mollybaz.com/salty-honey-smacks
host: mollybaz.com
title: Salty Honey Smacks - Molly Baz
captured_at: 2026-09-09T02:14:11Z
method: playwright-walk            # playwright-walk | userscript-walk | bookmarklet
identity: 0                        # save under this user id (0 = master/curator)
hero_image: salty-honey-smacks__a1b2c3d4.jpg   # sibling file, or blank
hero_source: https://mollybaz.com/wp-content/uploads/…jpg
status: captured                   # captured | signed-out | no-recipe | error
note:                              # free text when status != captured
---
# Salty Honey Smacks - Molly Baz

*Source: https://mollybaz.com/salty-honey-smacks/*
*Captured: 2026-09-09T02:14:11Z*

## STRUCTURED RECIPE DATA (JSON-LD)

```json
[ … every <script type="application/ld+json"> on the page, verbatim … ]
```

---

<markdown of the page's main content>
```

The body after the front matter is BYTE-COMPATIBLE with what the bookmarklet
posts to `/stage-markdown` (`forms/bookmarklet.js` builds the same header,
JSON-LD block and `---` rule). The markdown is produced server-side by
`to_markdown.markdown_from_html` — the canonical reducer — from the captured
DOM, so a walker capture and a server fetch reduce identically.

## Status values

* `captured` — page held recipe structure (an ingredients/instructions
  heading or a Recipe JSON-LD). Ingest it.
* `signed-out` — the page shows the publisher's signed-out signature (per
  host: mollybaz "Try the club" / "Sign In"). The walker STOPS the run on the
  first one; nothing after it is visited. Re-login and re-run.
* `no-recipe` — signed in, page fetched, but no recipe structure (an essay,
  a technique page). Kept for the record; ingest skips it.
* `challenge` — an anti-bot interstitial (Cloudflare "Just a moment…",
  SiteGround sgcaptcha) that did not clear within 30 s. Headless Chromium sits
  on these forever; a headed window clears them — the walker runs headed by
  default. Re-walk later; ingest skips it.
* `error` — navigation/capture failure; `note` carries the exception.

## Ingest (next step, not built yet)

`python -m intake.capture.ingest --host mollybaz.com [--dry-run]` reads every
`captured` .md, turns it into exactly the record `/stage-markdown` would have
produced (markdown, source_url, title, local hero image, hints), and runs the
existing extract → save step under `identity`. A file is renamed
`<slug>.md.done` on success and `<slug>.md.failed` (+ a `.err` sibling) on
failure. `--dry-run` reports what it would extract and saves nothing.

## Politeness and permission

The walker pauses a random 3–7 s between pages and never parallelizes within a
host. Interactive capture is only for publishers that agreed to it; that
agreement is to be recorded on the domain record (flag + who/when) before the
domain-form button exists. Until then the walker is console-only.

## Backing out

Everything lives under `intake/capture/`, this doc, two git-ignore lines, and
the two ignored folders. `git checkout pre-capture-walker` restores the tree;
deleting `intake/capture/` and the folders removes it from a later tree with
no other file touched.
