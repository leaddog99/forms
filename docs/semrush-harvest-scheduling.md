# SEMrush domain harvest — the human-workflow loop

**Status: V1 SHIPPED (backend) 2026-06-19.** Builds on the SEMrush backlinks-file source ([[project_backlinks_source]], SHIPPED) and the domain master ([[project_domain_master]]). The SEMrush web subscription is flat/unlimited for a human but its API is line-metered and ~3.5× more (§6) — so we keep the human pressing **Export** and automate everything *around* the click. This note is the canonical spec for that **semi-automated loop**: the system says which domains are due and hands over the links; the human does the (free, ToS-safe) SEMrush clicks; the system ingests, scores, and re-schedules.

Reuses the existing `jobs` runner ([[project_jobs_as_executables]]), the `domains` table, the backlinks_file pipeline, and `system_config` ([[project_system_config]]). This is the *backlinks-file* discovery source, NOT the `site:` SERP source ([[project_serp_provider]]).

---

## 1. The loop (one harvest = one domain's SEMrush export)

1. **Every day the system generates a worklist** of domains that are **NEW** (never harvested) or **DUE/overdue** — derived from each domain's `last_harvested_at + harvest_ttl_days`.
2. Each worklist item carries a **one-click deep-link** straight to that domain's SEMrush Indexed-Pages report (subfolder filter pre-applied).
3. **Curator clicks → SEMrush opens → presses Run → Saves** the export to the **watched inbox** (their Downloads dir, or a configured folder).
4. **A scan routes each arrived file to its domain** by the `{domain}` filename prefix, moves it into `input/`, and spawns the existing `backlinks_file` harvest (is-it-a-recipe → Moz score → rank → keep top-N).
5. **On success the domain is stamped** (`last_harvested_at = now`) → the derived `next_harvest_at` rolls forward → the row **drops off the worklist**.

Net: the system is the dispatcher + bookkeeper; the human is just the "press Run + Save" hands. A semi-automated way to ride the flat SEMrush web sub instead of its exorbitant per-line API.

---

## 2. Data model — columns on `domains`, NOT a new table

Curator's call (2026-06-19): *"it doesn't need another table — just a different view of the data including the scheduling stuff in the domain record."* This is already the established pattern — every per-domain harvest setting (`harvest_source`, `serp_query`, `recipe_path`, `search_pages`, `harvestable`, `fetch_strategy`, `semrush_rank`) is a column on `domains`. So scheduling is **three more columns + a derived field** (`domains_lib._SCHEDULE_COLUMNS`):

| column | kind | meaning |
|---|---|---|
| `last_harvested_at` | stored, MANAGED | ISO ts stamped on a successful `backlinks_file` ingest. NULL = never → "new". |
| `harvest_ttl_days` | stored, EDITABLE | refresh cadence (default **90** = quarterly), curator-overridable like `search_pages`. |
| `semrush_report_url` | stored, EDITABLE | the one-click deep-link that opens SEMrush at *this* domain's report (`/recipes` filter pre-applied). `''` = not captured (worklist still lists the domain; link just absent). |
| `next_harvest_at` | **DERIVED on read** | `date(last_harvested_at) + harvest_ttl_days`. Never stored — like `bcc_rank` / the dish `next_run_at`, so a TTL edit or new domain can't leave it stale. |

**Why no `domain × report-type` table** (an earlier idea, dropped): the curator wants one record per domain. If Traffic/Trends reports later need their own URL+cadence, add a few more columns (or a small JSON) on the domain row before reaching for a junction table.

### A domain is "SEMrush-managed" when
`harvest_source == 'backlinks_file'` **OR** it has a non-empty `semrush_report_url`. Only those appear on the worklist; ad-hoc SERP refreshes are a different mechanism and never schedule.

### Derived `harvest_status`
`new` (never harvested) · `due` (`next_harvest_at <= today`) · `ok` (not yet due) · `None` (not SEMrush-managed). A missing/garbage `last_harvested_at` reads as `new`, never raises.

---

## 3. The worklist = a view, not an entity

`domains_lib.harvest_worklist(conn)` → allowed, SEMrush-managed domains where `harvest_due`, ordered **new-first then most-overdue-first**. Surfaced at:

- **`GET /domains/harvest-worklist`** → `{count, today, items[]}`; each item = domain · display_name · status · last/next dates · ttl · `semrush_report_url` · `expected_file`.

The UI (a "Due today" panel on `domains.html`, NOT YET BUILT) is just a render of this — each row: `[Open in SEMrush]` (the stored URL) · expected filename · last/next · status pill.

---

## 4. The watched inbox + file routing

**Decision: a polled scan, not a FileSystemWatcher daemon.** A scan is stateless, has no debounce/partial-file race (only fully-written `*.xlsx` match the glob), and works whether triggered by a button or a schedule. The dedicated harvest machine makes a periodic poll feel automatic without an always-on watcher.

- **`POST /semrush-inbox/scan`** (manual "Scan inbox now") — scans the inbox dir (`system_config.semrush_inbox_dir`, default `~/Downloads`) ∪ `input/` for `*-backlinks*pages.xlsx`, routes each by the `{domain}` filename **prefix** to a known domain (longest-host match, so `allrecipes.com_recipe…` → `allrecipes.com`), moves it into `input/`, and spawns the `backlinks_file` harvest (deduped on the in-flight `publisher:<host>` entity). Returns a per-file `{file, matched, job_id, skipped}` report.
- **Routing key** = the existing filename convention (`{domain}*-backlinks*pages.xlsx`) — no separate `file_pattern` column needed; the domain prefix *is* the key (`collections_lib.export_prefix` / `scan_export_inbox` / `intake_export_file`).
- **Stamping** lives in the `publisher_refresh` job handler: a successful `source='backlinks_file'` ingest calls `domains_lib.mark_harvested(host)` — so BOTH the manual refresh button and the inbox scan reschedule identically.

### Optional automatic poll (NEXT, not in V1)
A `semrush_inbox_scan` scheduled job calling the same scan every N minutes = the fully hands-off "file arrives → processes" experience. Left **off by default** for V1 because each scan spawns real harvest jobs (network/credits); the manual button is the safe default. Flip it on per the dedicated-machine plan.

---

## 5. Traffic stays OUT of the quality gate (design decision, 2026-06-18)

SEMrush also exposes **traffic** and **trending** ("what's hot"). When those get ingested:

- **Quality (OU/power authority) selects WHO qualifies as best; traffic NEVER gates that.** Letting popularity into `rank_score` re-ranks toward the generic high-traffic aggregator pile — exactly what the editorial-authority moat differentiates against.
- Traffic earns its keep elsewhere: (1) a candidate **prune** (cheap prior for "real flagship vs buried page", quality gate still runs after); (2) a **queue-prioritizer** (rising domains float up "Due today"); (3) a tie-break **badge** within the already-qualified top-N; (4) — the real prize — a standalone **"What's Hot" trending collection** ([[project_collections]]) that slow-moving authority ranking structurally can't produce.
- Discipline in one line: **quality selects; popularity orders-within-qualified or powers a separate trending surface — never the gate.**

The 2026-06-19 **SEMrush traffic-Rank + `bcc_rank`** work ([[project_domain_master]]) is a *display/authority signal* and is correctly **not** wired into `rank_score` — consistent with this. Model future per-page signals as a **bag keyed by URL** (`referring_domains` / `traffic` / `traffic_trend`), joined on URL, each with its own timestamp + TTL (backlinks ~90d, traffic ~30d, trends ~7–14d) — which is *why* TTL lives per-row, not global.

---

## 6. Manual web export vs the SEMrush API (settled: stay manual)

The SEMrush **web sub doesn't meter per query** (flat all-you-can-eat). The **API meters per line AND requires a higher plan floor** — automating converts a flat cost into a metered one plus the floor. Researched 2026-06-18:

| | Manual web export (current) | SEMrush API |
|---|---|---|
| Plan | **Guru ~$250/mo** | **Business ~$500/mo** (API is Business-tier only) |
| Units | n/a | **not included** — buy blocks at **~$50/million** |
| `backlinks_pages` cost | **$0 marginal** | **~40 units/line → ~$2 per 1,000 URLs** |
| Rows/export | **30,000** (Guru) | per-request limit, default 100 |

Even *one* domain/mo via API ≈ `$500 + ~$6` vs flat **$250 unlimited**; ~200 domains/quarter ≈ `$900/mo` vs **$250**. Plus scripting the web client violates ToS (the API is the only ToS-clean automation, and it's the expensive one). **Verdict: keep the human pressing Export**; revisit only if click-labor ever exceeds ~$650/mo. Sources: [API/units](https://www.semrush.com/kb/5-api) · [backlinks units](https://developer.semrush.com/api/v3/analytics/backlinks/) · [pricing](https://thatmarketingbuddy.com/blog/semrush-api-pricing) · [row limits](https://www.semrush.com/kb/501-backlinks-report-manual).

---

## 7. Build status

**V1 SHIPPED (backend, 2026-06-19):**
- `domains_lib`: `_SCHEDULE_COLUMNS` (migrated), `harvest_ttl_days`/`semrush_report_url` in `EDITABLE_FIELDS`, `_derive_schedule` (applied in `list_domains`/`get_domain`), `mark_harvested`, `harvest_worklist`.
- `collections_lib`: `export_prefix`, `scan_export_inbox`, `intake_export_file`.
- `save_recipe_api`: `GET /domains/harvest-worklist`, `POST /semrush-inbox/scan`, shared `_spawn_publisher_refresh`, `last_harvested_at` stamped in the `publisher_refresh` handler on a `backlinks_file` success.
- Verified: migration applies on the real DB; worklist surfaces the 3 backlinks domains as "new"; filename→domain parsing + schedule derivation unit-tested; routes registered with the static path before `/domains/{domain}`.

**NEXT (not built):**
1. **"Due today" panel on `domains.html`** — render `GET /domains/harvest-worklist` (rows with `[Open in SEMrush]` + `[Scan inbox]`), wire the `semrush_report_url` field + `harvest_ttl_days` into the domain form. This is the visible half.
2. **Capture the real SEMrush Indexed-Pages URL template** → seed `system_config.semrush_indexed_pages_url_template` so a per-domain `semrush_report_url` can be generated from `{domain}`+`{subfolder}` instead of hand-pasted ([[feedback_no_data_in_code]]).
3. **Optional scheduled `semrush_inbox_scan` job** (§4) for hands-off polling.
4. **Traffic/Trends ingestion** (§5) — the "What's Hot" surface + queue prioritization.

**NOT building:** the SEMrush API path, a FileSystemWatcher daemon, any headless-browser automation of the export click.
