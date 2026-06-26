# Traffic as a ranking signal — current state + what the SEMrush API would add

## What shipped (2026-06-26) — traffic from the FILE export

Per-page **traffic** + **traffic %** + **file sequence** are now captured from the SEMrush
Top-Pages export and flow all the way through:

- **Capture** — `collections_lib._read_backlinks_file` reads the `Traffic` and `Traffic (%)`
  columns + the row's position in the export, and returns a `{url → {traffic, traffic_pct,
  file_seq}}` map.
- **Store** — `collection_members` gains `traffic`, `traffic_pct`, `file_seq` columns; the
  harvest stamps them on each scored member; the extracted master recipe gets them on
  `_scoring.traffic` / `_scoring.trafficPct`.
- **Rank** — selection now orders by `rank_score` DESC, then **`traffic` DESC** as the
  tiebreaker. This is the fix for foreign sites whose pages all share a near-identical PA
  (PA saturates) → near-identical authority score → traffic is what actually distinguishes
  their hero recipes.
- **Show** — the recipe editor's scoring strip has a "Traffic / mo" chip (+ "% of site").

This works for the **publisher harvest** only, because that's the one path with a SEMrush
file. The **dish batch** (multi-domain Google search) and **live single-URL extract** have
no traffic — there's no per-publisher file for arbitrary URLs.

## What it would take to add traffic to the OTHER extracts (SEMrush API)

To get traffic for an arbitrary URL/domain (dish batch, live extract) we'd call the SEMrush
**Analytics API** instead of reading a file.

### The pieces
1. **API client** — `input/pipeline/semrush_api.py`. Auth = a SEMrush **API key** (distinct
   from the manual export workflow), stored in `system_config`. Billed in **API units** per
   request (real per-call money), so every call is gated/opt-in like the unblocker.
2. **The right report** — for per-URL organic traffic the closest analog to the Top-Pages
   export is the **`url_organic`** report (the keywords a URL ranks for, each with its
   traffic); summing gives the URL's estimated organic traffic. Domain-level overview is
   `domain_ranks` / `domain_organic`. (SEMrush "Traffic Analytics / Trends" is a *different*,
   visit-level product — costlier, domain-grained — not the organic page traffic we want.)
3. **One chokepoint** — a `traffic_provider` abstraction mirroring `serp_search()`: the file
   export is one provider, the API is another; selected by `system_config`. Consumers don't
   change — they already read `_scoring.traffic` and the tiebreaker already sorts on it. Only
   the *source* of the number is new.
4. **Wiring (opt-in, behind a config flag):**
   - **Dish batch** — after Moz scoring, optionally `url_traffic(url)` per candidate → stamp
     `traffic` → the existing tiebreaker applies. Cost = N API calls per dish refresh.
   - **Live extract** — optionally fetch `url_traffic` at extract → stamp `_scoring.traffic`
     for the editor.

### Caveats to design around
- **`traffic_pct` (share of site)** isn't a free per-URL field via `url_organic` — the file
  gives it because it's relative to the whole export. Via API you'd derive it from the
  domain's total organic traffic (an extra call) or just omit pct for API-sourced rows.
- **Coverage** — small/foreign publishers can have thin or no SEMrush data via the API, same
  limitation as the file.
- **Units budget + rate limits** — bound the calls (top-N only, cache results, config cap),
  exactly like the SERP-credit discipline.

### Effort
Medium. The **storage / display / tiebreaker plumbing is already done** (this session), so
the remaining work is: the `semrush_api.py` client (key + units-aware), the `traffic_provider`
selector, the pct-derivation decision, and the opt-in wiring into the dish batch + live
extract. No schema or ranking changes needed — those landed with the file path.
