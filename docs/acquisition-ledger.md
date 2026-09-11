# Acquisition ledger → per-domain fetch policy

*Design, 2026-09-09. Decided in discussion with the curator: the system should
learn, from its own attempts, which acquisition techniques to try for a
publisher and in what order — for minimal cost at maximal success — and
stamp that on the domain, instead of every run walking a generic ladder we
wrote by hand. Build order: doc → ledger (no behavior change) → carve the
ladder into techniques behind one contract → policy job + domain stamp →
exploration + platform inheritance + a bounded model call for novel failures.*

## 1. The problem

A recipe page is obtained by a fixed escalation written in
`to_markdown/html_to_markdown.py` (`fetch_with_full_fallback`) and wrapped
again by the harvest filter (`intake/build_query_batch.py::_fetch_for_filter`,
`_render_rescue`): direct → unblocker static → unblocker render → Wayback,
with per-domain flags bolted on one lesson at a time (`render_required`,
`content_obtainable`, `fetch_strategy`, `url_prefilter`, `human_capture_only`).

Three costs of the fixed ladder:

* **Re-runs re-guess.** When a publisher's or dish's review TTL expires, the
  next run pays the same rungs to rediscover what the last run learned.
* **Money spent on certainties.** mollybaz.com (job 1884): 2 paid fetches per
  candidate × 119 candidates to learn, 119 times, that the body is behind a
  membership wall. gressinghamduck: 74 render credits on a site whose static
  HTML carried the recipe.
* **The ladder is a braid.** Block detection, escalation, Wayback jitter and
  circuit breaker, the 202 stub, the 402 that bills as success, the render
  rescue — one function, months of scars, no per-rung test.

This is a **routing problem with a memory**, not an agent problem. The
techniques are instruments; a learned policy chooses among them; the run
stays a program (see the 09-08 discussion: agents operate the system, they
are not the system).

## 2. Techniques (the instruments)

One contract for every way we can obtain a page:

```
attempt(url, ctx) -> Attempt
Attempt = {
  technique:  str,          # see table
  ok:         bool,         # a usable body came back (NOT "the page said yes")
  body:       str | None,   # HTML (or markdown for walker/bookmarklet captures)
  reason:     str,          # failure class when not ok (section 4)
  cost_units: float,        # vendor credits / renders / 0 for free rungs
  cost_usd:   float | None, # when the vendor price is known
  ms:         int,
  meta:       dict,         # status, bytes, final_url, provider, variant…
}
```

| technique          | what it is                                   | cost     | exists today as                       |
|--------------------|----------------------------------------------|----------|---------------------------------------|
| `direct`           | plain GET with UA fallback                    | free     | `fetch_with_ua_fallback`              |
| `unblocker`        | Oxylabs/Bright Data, static                   | 1 credit | `fetch_via_unblocker(render=False)`   |
| `unblocker_render` | same, browser-rendered                        | 1 render | `fetch_via_unblocker(render=True)`    |
| `wayback`          | archived copy                                 | free     | `fetch_via_wayback`                   |
| `walker`           | signed-in Playwright capture (file contract)  | ~free    | `intake/capture/walk.py`              |
| `human`            | bookmarklet / 📋 Queue                        | curator  | `/stage-markdown`                     |
| `cache`            | page_fetch_cache hit                          | free     | `page_cache.py`                       |

Success is strict: **the body passed the recipe-structure gate** (JSON-LD or
phrase score) — the same verdict the harvest already computes — and, where
the run goes on to extract, **the extract saved**. A fetch that returns a
teaser is a failure with reason `wall:membership`, not a success.

## 3. The ledger (phase 1 — write-only, no behavior change)

Table `acquisition_attempts` in recipes.db, one row per technique invocation:

```
id, ts, job_id, run_kind ('dish'|'publisher'|'capture'|'process_selected'),
domain (canonical host), root_domain, url_normalized,
technique, rung (1-based position in the sequence that was tried),
ok, reason, status_code, bytes, ms, cost_units, cost_usd,
gate ('jsonld'|'phrase'|'trust'|'none'), gate_score,
saved (0/1, filled in when the extract stage writes a recipe), notes
```

Hooks: every call site of the four fetch functions and the walker writes a
row; the harvest's KEEP/DROP decision back-fills `gate`/`gate_score` on the
row it just consumed; the save stage flips `saved`. Nothing reads the table
in phase 1. `python -m jobs run acquisition_report` prints, per domain and
per technique: attempts, success rate, mean cost, mean ms, cost-to-first-
success — the BEFORE measurement the carve is judged against.

Retention: rows are small; keep 180 days, then roll up to per-domain
per-technique counters.

## 4. Failure classes (`reason`)

Deterministic first, model second, never free text:

```
block:soft        challenge stub returned 2xx (202/197-byte, sgcaptcha meta-refresh)
block:ratelimit   429 / too many requests — the origin says SLOW DOWN; the direct rung pauses + retries before any paid rung
block:hard        403/503 from a WAF
block:captcha     interstitial that did not clear (Cloudflare "Just a moment", sgcaptcha)
dead:parked       registrar/ad lander answers every path (domain expired) — TERMINAL for paid rungs; pre-flight aborts the harvest
shell:js          thin body, no JSON-LD, no <article> — needs a render
wall:membership   full page, no recipe structure, sign-in/subscribe markers (mollybaz)
wall:paywall      full page, recipe truncated + subscribe prompt (Milk Street)
gone:404          terminal
gone:redirect     redirected off the recipe (home, category)
thin:no-struct    full page, no structure, no wall markers (essay/technique page)
net:timeout / net:error
vendor:402        pay-per-crawl billed as success (Wayback carries it)
```

`wall:*` is the class the pipeline could not name on 2026-09-08 — it filed
mollybaz as `thin:no-struct` and only a human reading a cached body saw the
club. Phase 5 adds the bounded model call that names a novel failure from the
page text when the deterministic classifier returns `thin:no-struct` twice
for a domain; its answer becomes a `reason`, never an action.

## 5. Policy (phase 4)

Nightly job `acquisition_policy`:

* For each domain with ≥ N attempts (N=8 to start) across ≥ 2 techniques,
  order techniques by **expected cost to first success** =
  Σ over the sequence of (cost × P(reach this rung)) / P(success by the end),
  using the last 90 days, success as defined in §2.
* Stamp `domains.acquire_policy` (JSON: ordered techniques + per-rung
  P(success) + n) with `acquire_policy_method` (id, e.g. `cts_v1`) and
  `acquire_policy_inputs` (window, n, per-technique rates) and `_at` —
  stored, never computed on read ([[feedback_persist_derived_values]]).
* A domain below N inherits its **platform** policy (§6), else the generic
  prior = today's ladder.
* The ladder code becomes: `for technique in policy_for(domain): attempt()`.
  The existing flags (`render_required`, `human_capture_only`, `score_only`)
  are read as hard constraints on the sequence, not replaced — a curator's
  decision outranks the learner.

**Exploration is never zero.** With probability ε (0.05 to start, per
domain, decaying with n) a run tries the cheapest technique the policy
skips, so a site that drops its wall is noticed and a stale policy heals.
Same shape as the render/EV serving design (deterministic → learned, with a
floor).

## 6. Platform inheritance

The first response identifies the platform: SiteGround (`sgcaptcha`),
Cloudflare (`cf-*` headers, "Just a moment"), WordPress (+ membership
plugins: MemberPress, Paid Memberships Pro markers), Squarespace, Substack.
`acquisition_attempts.notes` carries the detected platform; the policy job
also builds a per-platform policy, and a brand-new domain inherits it on day
one. mollybaz-class walls are then recognized on page one of a new domain.

## 7. The carve (phase 3) — rules

* One technique at a time, behind the contract, ledger re-run after each.
* **No silent removal**: every branch in the ladder that encodes a lesson
  (the 202 stub, the 402, Wayback jitter/circuit-breaker, the render-rescue
  cost guard, the direct-first probe) moves with its comment into the
  technique that owns it, or into the router, and is named in the commit.
* Lint first ([[feedback_lint_before_refactor]]); the acquisition report is
  the regression check ([[project_regression_check_cycle]]).
* The walker and the human rung are techniques from day one — the file
  contract already returns what `Attempt` needs.

## 8. What this is not

Not an LLM choosing rungs at run time (variable cost, unreplayable path).
Not a replacement for curator flags. Not a change to selection or ranking —
acquisition decides *how* a page is obtained, never *whether* it is wanted
([[project_two_stage_selection]]).

## 9. Phase plan

1. This doc. ✔
2. Ledger + hooks + `acquisition_report` job. **No behavior change.** (next)
3. Carve: `direct`, `unblocker`, `unblocker_render`, `wayback`, `walker`,
   `human` behind `attempt()`; ladder = list; report unchanged before/after.
4. `acquisition_policy` job + domain stamp + `policy_for()` in the router.
5. Exploration ε; platform detection + inheritance; bounded model call for
   `thin:no-struct` repeats → `wall:*` naming.
