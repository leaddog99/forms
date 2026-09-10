# Acquisition techniques — what each one actually does

*Companion to docs/acquisition-ledger.md. Written 2026-09-10 for the admin
surfaces: this is the text the technique registry is seeded from (§8), so
what an admin reads on the System → Acquisition page and behind the ⓘ on
the domain form is this document, not a paraphrase of it.*

A "technique" is one way of obtaining a publisher's page. Every technique
answers the same call — `attempt(url, ctx)` — and returns the same record:
did a usable body come back, what it cost, how long it took, and if it
failed, which named failure class. "Usable" is strict: the body must pass
the recipe-structure gate (Recipe JSON-LD, or the ingredients/instructions
phrase check). A page that arrives but holds only a teaser is a failure.

Today's hand-written order is: cache → direct → unblocker → unblocker render
→ Wayback, with the walker and the human rung outside the ladder. The ledger
and policy job (acquisition-ledger.md) will learn a per-publisher order; the
techniques themselves do not change.

---

## 1. `cache` — the page we already fetched

**What it does.** Before any network call, the raw-page cache
(`page_cache.db`, table `page_fetch_cache`) is checked for this URL. The key
is the normalized URL plus a *variant* — `static` or `render` — because a
rendered page and a static one are different documents. A hit that is
younger than the TTL (`page_cache_ttl_days`, System → Cache, default 5) is
returned as if fetched, with the original status code, headers and body.

**When it is written.** Only after a successful, non-stub fetch by one of
the techniques below; a challenge page is never cached. The publisher
harvest turns the cache on so its is-recipe filter and its winner extract
share ONE fetch; other callers leave it off.

**Cost.** Free. **Typical latency.** Milliseconds.

**How it fails.** It cannot fail; it either has a fresh page or it does not.
A stale or missing entry simply falls through.

**Why it exists.** Re-harvests within the TTL, and the filter-then-extract
double read, used to pay the unblocker twice for the same page.

---

## 2. `direct` — a plain HTTP request with a user-agent chain

**What it does.** A normal HTTPS GET, following redirects, with a small
chain of user-agent strings tried in order until one gets a 2xx with no
network error. The chain is bot-first: our own honest bot string
(`recipe-forms/0.1`) is tried before a Chrome desktop string. Most recipe
blogs accept both; a few (The Kitchn is the canonical case) reject
Chrome-style strings and accept the bot; others do the reverse. One string
cannot satisfy both, so both are tried, the honest one first because it
costs nothing and is accepted more often.

**What it checks before believing the answer.** A 2xx is not proof of a
page. `blocked_reason()` inspects the response for two separate signals:
a *known challenge marker* (Cloudflare "Just a moment", SiteGround's
`sgcaptcha` meta-refresh, WAF interstitials) and a *thin body with no
structure* (under 15,000 characters with neither Recipe JSON-LD nor an
`<article>` element). The first means the origin actively blocked us; the
second means an interstitial or an unrendered JavaScript shell. Both are
reported with a specific phrase, because "fetch-failed" with no reason was
the defect this replaced (a 202 / 213-byte captcha stub was once filed as
"no recipe structure" — a statement about content we never received).

**Cost.** Free. **Typical latency.** 0.5–3 s.

**How it fails.** `block:soft` (a 2xx stub), `block:hard` (403/429/503),
`shell:js` (thin body, no structure), `gone:404`, `net:timeout`.

**Why it comes first.** It is free and it is right most of the time. The
render-eligible path still *probes* direct first (2026-08-28): a wrongly
set render flag once paid 74 render credits on a site whose static HTML
carried the complete recipe. The probe is accepted only when the body is
plainly the real page — not block-flagged and carrying structure.

---

## 3. `unblocker` — a paid anti-bot proxy, static

**What it does.** The same GET, routed through a commercial "web unblocker"
that supplies a residential IP, a managed browser session, and the
solving of standard anti-bot challenges on its side. Two integration styles
exist and the configured provider decides which: *proxy-style* (Oxylabs,
Bright Data — the request is sent through their proxy endpoint, and a
header asks for static or rendered HTML) or *GET-style* (ScraperAPI,
ScrapingBee, Zyte — the target URL is passed as a parameter to their API).
Credentials live in the environment only, never in the database.

**Static means:** the provider fetches the page and returns the HTML as
served, without running its JavaScript. Recipe pages almost always carry
their content and their JSON-LD in the static HTML, so this is the normal
paid rung: roughly 3× faster and 3× cheaper than a render.

**What it checks.** The same `blocked_reason()` gate. A provider can
succeed at its job and still hand back a page that holds no recipe — a
membership teaser is a 200 with a full HTML document. That is exactly the
mollybaz.com case: 55–108 KB WordPress pages, no ingredients, a "Sign In /
Try the club" block. The technique "succeeded"; the attempt did not.

**Cost.** One credit per fetch, billed per request by the provider.
Pay-per-crawl origins can bill a 402 as a success; Wayback carries those.
**Typical latency.** ~5 s.

**How it fails.** `block:captcha` (the provider could not clear the
interstitial), `wall:membership` / `wall:paywall` (full page, no recipe,
sign-in markers), `vendor:402`, `net:timeout`.

**When it is skipped.** A domain whose curator-set `fetch_strategy` is not
`unblocker` never spends here unless the direct rung was block-flagged AND
escalation is enabled for the run. A domain marked *human capture only*
never spends here at all.

---

## 4. `unblocker_render` — the same proxy, with a real browser

**What it does.** The provider loads the page in a headless browser, runs
its JavaScript, waits for the document to settle, and returns the rendered
DOM. This is the rung for sites whose recipe body is injected client-side
(a JavaScript shell that the static fetch returns nearly empty).

**When it is tried.** Only after a static fetch came back as a thin body
with no structure — the render-escalation guard — or up front when the
domain is already known to need it (`render_required`, learned the first
time a render rescued one of its pages). Rendering first without that
evidence is the mistake that cost 74 credits on gressinghamduck.co.uk.

**Cost.** One render credit, several times a static credit.
**Typical latency.** 17–30 s.

**How it fails.** As for `unblocker`, plus `shell:js` when even the rendered
document carries no structure (the page truly has no recipe, or requires a
login to inject one).

---

## 5. `wayback` — the Internet Archive's copy

**What it does.** Asks archive.org's availability API for the most recent
snapshot of the URL, then fetches that snapshot's *raw* form (`id_` flag)
so the body is the original page without the Wayback toolbar. Recipe
content is essentially static, so a snapshot from weeks or months ago is
fine for cohort matching, grading and most extraction; the snapshot
timestamp is stamped into the recipe's provenance so the UI can say
"snapshot from YYYY-MM-DD".

**Politeness.** A jittered delay before each call, and a global circuit
breaker: after four consecutive connection failures archive.org is left
alone for a window rather than hammered. An open circuit degrades safely —
fewer rescues that hour, nothing wrong.

**Cost.** Free. **Typical latency.** 2–10 s.

**How it fails.** `gone:no-snapshot` (publisher excludes via robots.txt, or
the URL is too new to have been crawled), `net:circuit-open`,
`net:timeout`. A snapshot of a challenge page is possible and is gated
like any other body.

**Why it is last in the ladder.** It is free but it is not the live page,
and it is absent for many publishers. It is the backstop when a site 403s
every user-agent and the unblocker is off or has failed.

---

## 6. `walker` — a signed-in browser we operate (spike, 2026-09-08)

**What it does.** A Chromium under Playwright, launched with a *persistent
profile* the curator has signed into by hand (once; no password is ever
typed by code), visits each URL, waits for the DOM, captures it, reduces it
with the same HTML→markdown reducer the server uses, saves the hero image
with the session's cookies, and writes one capture per page into
`input/captures/<host>/` (docs/capture-folder-contract.md). It runs
*headed*: a real window clears Cloudflare's "Just a moment" in seconds
where a headless browser sits on it forever (measured on
simplyrecipes.com).

**What it checks.** The publisher's signed-out signature (per host: "Try
the club" with no ingredients on the page) — the run STOPS on the first
signed-out page rather than capturing fifty teasers — and the same
recipe-structure gate as every other technique.

**Cost.** Free per page; the curator's time to sign in once per site, and
a politeness pause of 3–7 s between pages. **Typical latency.** 5–15 s per
page including the pause.

**How it fails.** `signed-out` (session expired — re-login needed),
`challenge` (an interstitial that did not clear in 30 s), `no-recipe`,
`error`.

**When it applies.** Members-only publishers that have agreed to an
interactive crawl (mollybaz.com's CLUB, Milk Street), and — the same loop
without the login — anti-bot sites the unblocker chokes on. Today it is a
console command; the ledger will make it a rung the policy can choose.

---

## 7. `human` — the bookmarklet, one page at a time

**What it does.** The curator opens the page in their own browser, signed
in, and clicks the bookmarklet. It captures the page's main content as
markdown plus the JSON-LD blocks, uploads the hero image through the
authenticated session, posts the bundle to `/stage-markdown`, and hands a
token to the recipe form, where the curator clicks Extract and Save. The
📋 Queue on the domain form opens each checked cohort page for exactly this
click.

**Cost.** Free in credits; a minute of curator time per page.

**How it fails.** It does not, mechanically; it does not scale. Thirteen
Milk Street recipes in three months is its ceiling, which is why the walker
exists.

**When it applies.** A domain marked *human capture only*; any page the
curator wants captured exactly as they see it.

---

## 8. Where admins see this

Three surfaces, all reading ONE registry so the text cannot drift:

1. **System → Acquisition** (a settings-style page like System → Limits):
   one card per technique — label, this description, cost note, latency,
   the failure classes it can produce, enabled/disabled — and, once the
   ledger exists, the live numbers beside each: attempts, success rate,
   mean cost, mean latency, cost to first success, over the last 90 days.
2. **The domain form**: the *Fetch strategy* field gets an ⓘ that opens the
   same descriptions (info-dot convention — never inline prose), and a
   per-publisher **Acquisition** panel shows what the ledger knows about
   THIS host: which techniques have been tried, their outcomes, and — after
   phase 4 — the stamped policy order with its method and inputs, and a
   one-click "reset to prior".
3. **Job logs**: every attempt prints one line naming the technique, the
   rung, the outcome class and the cost, so a curator reading a log sees
   `[ACQ] rung 2 unblocker  ok  1 credit  5.1s` rather than an anonymous
   fetch.

The registry is a table, `acquisition_techniques` (key, label,
description, cost_note, latency_note, enabled, sort), seeded from this
document the way `system_config` is seeded from its defaults: code is the
seed, the row is the truth, and an admin may edit the label or description
in place. The ledger's `technique` column is a foreign key to it.
