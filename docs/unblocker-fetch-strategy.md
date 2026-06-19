# `unblocker` fetch strategy — paid web-unblocker as a per-domain last resort

**Status: SKETCH (2026-06-19).** The fetch tier + config + strategy vocab are in;
the one remaining wire is making the harvest *pass* `unblocker=True` for a flagged
domain (below). Builds on the Wayback jitter+circuit-breaker (same session) and the
SERP-image thumbnail fallback. See [[project_playwright_fetch]], [[project_domain_master]].

## Why
Hard anti-bot publishers (thekitchn = PerimeterX "press-and-hold") block on the
**first** request via browser-fingerprint + JS challenge. No UA rotation, delay, or
plain Playwright beats that (verified: a real browser gets the challenge too). The
only thing that reads the **live** page is a managed **web unblocker** — residential
IPs + real-browser render + CAPTCHA/challenge solving in one call. We already use the
*concept*: the SERP vendor is a managed unblocker pointed at Google. This tier points
one at the publisher directly, for the cases where a live full page is worth paying for.

It is a **deliberate, per-domain, paid last resort**, NOT a blanket fetcher:
- Free stack first — direct → (this) → Wayback. Wayback + SERP already cover
  discovery / recipe-verify / thumbnails for most blocked sites.
- This tier earns its cost only for **live full-content ingest** of a high-value
  blocked recipe (the queued "authenticated fetch of gated recipes" item) — a months-
  old Wayback snapshot is fine to *verify/score*, but for *extract-and-serve* you may
  want the live page.
- ~$1–5 / 1,000 requests, so an 87-URL harvest ≈ $0.10–0.40. Cost isn't the blocker;
  restraint is — defeating a challenge is more aggressive/ToS-grey than reading a
  public archive, so use it where chosen, not everywhere.

## Where it sits (code)
`to_markdown/html_to_markdown.py`:
- `fetch_via_unblocker(url)` — provider-agnostic GET; sets `response.url` to the
  TARGET (so JSON-LD / og:image parse against the right base). Returns `(resp, meta)`
  with `meta.source="unblocker"`, or `None` on no-key/failure.
- `unblocker_available()` — True only when a BYOK key is set (else the tier no-ops).
- `fetch_with_full_fallback(url, …, unblocker=False)` — new tier between direct and
  Wayback: `direct → [unblocker if opted-in] → Wayback`. 404/410 stay terminal.

## Config (BYOK + no-data-in-code)
Secrets are **env only, never the DB** (portable-package secret rule):
- **GET-style** (ScraperAPI/ScrapingBee/Zyte): `UNBLOCKER_API_KEY`.
- **PROXY-style** (Oxylabs/Bright Data): `UNBLOCKER_PROXY_USER` + `UNBLOCKER_PROXY_PASS`.

Provider + optional overrides via `system_config` (admin-editable; seed to surface):
- **`unblocker_provider`** (or `UNBLOCKER_PROVIDER` env). Default `scraperapi`. Set to
  `oxylabs` / `brightdata` for the proxy path.
- **`unblocker_endpoint`** — GET-style endpoint override (any vendor, no code change).
- **`unblocker_proxy_host` / `unblocker_proxy_port`** — proxy-style host/port override
  (defaults baked per provider).

## Provider matrix
| Provider | Shape | Wired | Notes |
|---|---|---|---|
| **Oxylabs Web Unblocker** | **proxy** (`unblock.oxylabs.io:60000`, `x-oxylabs-render`) | ✅ | top tier, cheapest; our default |
| **Bright Data Web Unlocker** | **proxy** (`brd.superproxy.io:33335`, renders by default) | ✅ | most bulletproof on hardest anti-bot |
| **ScraperAPI** | GET `?api_key=&url=&render=true` | ✅ | value tier, simplest |
| **ScrapingBee** | GET `?api_key=&url=&render_js=true` | ✅ | value tier, dev-friendly |
| **Zyte API** | `browserHtml` (really a POST/JSON API) | ⚠️ stub | best for bundled extraction; real wiring is a POST with JSON body |

Proxy-style (Oxylabs/Bright Data) route `requests.get(url, proxies={...},
verify=False)` through the vendor's unblocking zone — `_fetch_via_proxy_unblocker()`.
GET-style hit the vendor API — `_fetch_via_get_unblocker()`. `fetch_via_unblocker()`
dispatches by provider.

## To ACTIVATE (the remaining wire)
1. Set the BYOK creds + provider in the host env:
   - Oxylabs (default pick): `UNBLOCKER_PROVIDER=oxylabs`, `UNBLOCKER_PROXY_USER=…`,
     `UNBLOCKER_PROXY_PASS=…`.
   - Bright Data: `UNBLOCKER_PROVIDER=brightdata` + the same proxy user/pass (the
     `brd-customer-<id>-zone-<zone>` user + zone password).
   - (Seed `unblocker_provider` in `system_config` so it's admin-editable too.)
2. Flip the publisher: domain editor → **Fetch strategy = `unblocker`** (vocab added).
3. **Wire the harvest to pass it** (the one code change left): in the publisher
   harvest / `_is_recipe_filter`, resolve the domain's `fetch_strategy` and call
   `fetch_with_full_fallback(url, unblocker=(strategy == "unblocker"))`. Today the
   recipe-check calls it without the flag, so the tier never fires — by design until
   a key exists. Keep the resolution at the harvest level (it has the domain context)
   rather than coupling the low-level fetcher to `domains_lib`.

## Guards / TODO
- **Per-run cost cap** — a counter so a runaway flagged harvest can't burn budget
  (e.g. `unblocker_max_per_run`); log what was skipped past the cap (no silent caps).
- **Prefer live over archive** — the tier is already ordered before Wayback. For a
  flagged domain you may also want to SKIP the futile direct attempt (it always 403s);
  cheap as-is (one fast failure) so left in for the rare fetchable page.
- **Image policy** — a live-unblocked hero is the source's real image → still an
  attributed+linked thumbnail on the corpus side (DL-16, [[project_image_policy]]).
- **ToS** — defeating a challenge is grey; pair with the corpus's link-back + rich-
  result-envelope posture, and reserve for chosen high-value publishers.
