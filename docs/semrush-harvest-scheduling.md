# SEMrush domain harvest — stay manual, kill the friction, schedule the queue

**Status: DESIGN (2026-06-18).** Builds on the SEMrush backlinks-file source ([[project_backlinks_source]], SHIPPED) and the domain master ([[project_domain_master]]). Decides the **manual web export vs API** question (verdict: stay manual), then designs the two things that actually hurt — getting the export file into the project, and knowing **which domains to refresh today**. Reuses the existing `jobs` runner ([[project_jobs_as_executables]]), the `domains` table, and `system_config` ([[project_system_config]]). No SerpApi/Scale-SERP overlap — this is the *backlinks-file* discovery source, not the `site:` SERP source ([[project_serp_provider]]).

---

## 1. The decision: manual web export, NOT the SEMrush API

The SEMrush **web subscription does not meter per query** — it's flat all-you-can-eat for a human. The **API meters per line AND requires a higher plan floor.** Automating via the API converts a flat cost into a metered one and adds the floor on top — a strict loss at our scale.

**The numbers (researched 2026-06-18):**

| | Manual web export (current) | SEMrush API |
|---|---|---|
| Plan required | **Guru ~$250/mo** (current) | **Business ~$500/mo** (API is Business-tier only) |
| API units | n/a | **Not included** — start at zero, buy blocks (2M/5M/10M/20M) at **~$50/million** |
| Indexed Pages (`backlinks_pages`) cost | **$0 marginal** | **~40 units/line → ~$0.002/URL → ~$2 per 1,000 URLs** |
| Rows per export | **30,000** (Guru; Pro 10k, Business 50k) | per-request `limit`, default 100 |
| Domains | **unlimited** | metered per pull |

**Worked cost:** even *one* domain/month via the API = `$500 floor + ~$6 (3k URLs) ≈ $506` vs the flat **$250 for unlimited** manually. At ~200 domains refreshed quarterly (~67 pulls/mo): `$500 + ~$400 units ≈ $900/mo` vs the same flat **$250**. The API is ~3.5× more for zero data-quality gain.

**Two more reasons manual wins:** (1) scripting the *web client* with a headless browser violates SEMrush ToS and risks the account — the API is the only ToS-clean automation, and it's the expensive one; (2) the manual export already feeds the **existing** is-it-a-recipe → score/rank pipeline unchanged.

**Verdict:** keep the human pressing **Export**. The human-in-the-loop click is the only free, ToS-safe step. Everything *around* it gets automated. Revisit the API only if click-labor ever becomes worth >$650/mo.

Sources: [SEMrush API/units (kb/5-api)](https://www.semrush.com/kb/5-api) · [Backlinks units/line (developer)](https://developer.semrush.com/api/v3/analytics/backlinks/) · [API pricing breakdown](https://thatmarketingbuddy.com/blog/semrush-api-pricing) · [Export row limits by plan](https://www.semrush.com/kb/501-backlinks-report-manual).

---

## 2. Friction #1 — getting the export file into `input/`

Today: SEMrush writes the CSV to the browser's **Downloads** dir (not configurable from SEMrush), then it's **hand-moved** to `input/`, then the **Process** button finds and ingests it. Kill the hand-move. Two clean fixes — do both; B is the safety net.

### Fix A — point the browser's download dir at the project
The OS "Downloads" default is irrelevant; **every browser sets its own default download folder.** Point the SEMrush browser at `…/forms/input` and turn **off** "ask where to save each file." Export now lands directly in `input/`.
- On the dedicated harvest machine, give SEMrush its **own browser profile** with that download dir so it never pollutes normal Downloads.

### Fix B — Process button reads Downloads directly (the robust path)
Don't depend on browser config. The **Process** action scans BOTH `input/` and `~/Downloads` (`%USERPROFILE%\Downloads`) for the SEMrush filename pattern, takes the **newest match**, moves it into `input/`, ingests, then **deletes it from Downloads**.
- Stateless — no daemon, no `watchdog`, no `.crdownload` partial-file race, because the human only clicks Process *after* the download finishes.
- Self-cleans Downloads as a side effect.

### Rejected — FileSystemWatcher / auto-copy daemon
Possible (`watchdog`, or PowerShell `Register-ObjectEvent` on a `.NET FileSystemWatcher`) but over-engineered: a watcher must debounce until the file stops growing, and it adds a always-on process for a quarterly task. Fix B gets the same result with none of it.

**Recommendation:** Fix B is primary (works regardless of browser, auto-cleans). Fix A on top so files usually land in `input/` already.

**Filename matching:** record the actual SEMrush export naming convention (typically `{domain}_{report}_{YYYYMMDD}.csv` or the subfolder-export form `{domain}_recipe-backlinks_pages.xlsx` already tolerated by the ingest). Match by `{domain}` + report token, newest mtime wins. The ingest already tolerates the subdir-export quirks ([[project_backlinks_source]]).

---

## 3. Friction #2 — which domains to refresh today (the scheduler)

A near-direct clone of the per-dish scheduler (`dishes.next_run_at` DERIVED, [[project_jobs_as_executables]]).

### Data (on the `domains` table)
- `last_harvested_at` (stored, stamped on a successful Process).
- `harvest_ttl_days` (per-domain; default from `system_config.semrush_harvest_ttl_days`, **~90** = quarterly).
- `next_harvest_at` = **DERIVED** `last_harvested_at + harvest_ttl_days` (not stored — mirror `next_run_at`).

### The "Due today" queue (a panel on `domains.html`, or its own admin page)
List domains where `next_harvest_at <= today`, each row carrying:
1. **One-click deep-link** straight to that domain's SEMrush **Indexed Pages** report, target + `/recipes` subfolder filter pre-applied → you land on the exact page and just press **Export**.
2. The expected **filename / subfolder** reminder for that domain.
3. A **Process** button = §2 Fix B (scan → ingest → score/rank via the existing pipeline).
4. **Mark done** → stamps `last_harvested_at`, recomputes the next due date, drops the row off the queue.

This is the whole loop: the system says *which* domains and hands you the exact links; you do the (free, ToS-safe) SEMrush clicks; the system does ingestion + bookkeeping. Fits the "dedicated machine for this process" plan.

### The SEMrush deep-link — capture, don't guess
The Backlink Analytics Indexed Pages report URL takes the target + filters as query params, but the exact format drifts. **At build time:** navigate to one real Indexed Pages report filtered to `/recipes`, copy the address-bar URL, and templatize it (substitute the domain + subfolder). Store the template in `system_config` (`semrush_indexed_pages_url_template`) so it's a config edit, not a code change ([[feedback_no_data_in_code]] / [[project_portable_package]]) — and so a re-skin of SEMrush's URLs is a one-field fix.

---

## 4. Relationship to system-wide domain scoring
Orthogonal but adjacent. This note covers **discovery + ingestion cadence** (get the right URLs into the corpus). [[project_domain_scoring]] (`docs/domain-scoring.md`, design) covers how those ingested URLs get **ranked** (single global OU/power fit replacing raw-PA). The backlinks file is ranked by referring **Domains** at ingest (the real discriminator on big saturated-PA sites — [[project_backlinks_source]]); the system-wide score is the downstream selection step. No coupling — build independently.

---

## 5. Build order (when picked up)
1. `domains.last_harvested_at` + `harvest_ttl_days` columns; `next_harvest_at` derived in `row_to_dict` (clone the dish pattern). `system_config` defaults (`semrush_harvest_ttl_days`, `semrush_indexed_pages_url_template`).
2. Process button reads `~/Downloads` ∪ `input/`, newest-match by `{domain}`+report token, move → ingest → delete-from-Downloads, stamp `last_harvested_at` (§2 Fix B).
3. "Due today" queue panel on `domains.html`: due list + deep-link + Process + Mark-done (§3).
4. Capture the real SEMrush Indexed-Pages URL template into `system_config` (§3).
5. (Optional later) Fix A doc: set the harvest browser/profile download dir to `input/`.

**Not** building: the SEMrush API path, a FileSystemWatcher daemon, any headless-browser automation of the export click.
