"""Domain master — the canonical per-publisher record.

A *domain* (full host, e.g. ``cooking.nytimes.com``) is the entity that owns
everything we know about a source site independent of any single recipe:

  - editorial    display_name, story, logo
  - provenance   country + primary language (a default for the multilingual
                 pipeline's _source.originalLanguage), optional cuisine_focus
                 HINT (NOT authoritative recipe ethnicity — that stays dish-
                 derived; a Greek site can still post a taco)
  - extraction   fetch_strategy / extract_notes / custom_extractor — folds in
                 the long-planned "domain quirks registry"
  - gatekeeping  harvestable (skip a known publisher's refresh); the hard host
                 blocklist moved OUT to system_config `disallowed_domains` (a
                 junk host no longer needs a domains-master row to be blocked)
  - authority    domain_authority (DA is a domain property; metabase_url keeps
                 only the per-URL PA) + da_last_scored
  - ops          notes, failure_count, timestamps

This is the source of truth (per ``feedback_no_data_in_code``). The shipped
``domain_display_names.json`` is demoted to a one-time BOOTSTRAP SEED — same
relationship ``chapter_classifier.CHAPTERS`` has to the ``chapters`` table.

Keyed on the full host (per the grain decision) so subdomains can carry
distinct display names / extraction tips ("NYT Cooking" vs "The New York
Times"); ``root_domain`` is carried alongside so DA can be shared across a
publisher's subdomains.
"""

from __future__ import annotations

import json
import os
import sqlite3
from input.pipeline.db import connect as _connect  # WAL busy_timeout — input/pipeline/db.py
from datetime import datetime, timedelta, timezone
from typing import Optional

from input.pipeline.url_utils import root_domain

_SEED_PATH = os.path.join(os.path.dirname(__file__), "domain_display_names.json")
_DEFAULT_DB = "recipes.db"

# Editable columns surfaced by the a/c/d editor, in display order. domain
# (PK) + the timestamps are managed, not in this list.
EDITABLE_FIELDS = (
    "display_name",
    "story",
    "logo_url",
    "country",
    "language",
    "cuisine_focus",     # the publisher's cuisine (searchable; was a "hint", now a real field)
    "ethnicity",         # the publisher's cultural origin (optional; not searched yet)
    "fetch_strategy",
    "render_required",   # JS-rendered site → fetch with a real browser (unblocker render=True)
    "score_only",        # harvest MODE: 1 = Curate (score URL-only, pick manually); persists the picker choice
    "extract_notes",
    "custom_extractor",
    "domain_authority",
    "da_last_scored",
    "notes",
    "failure_count",
    "keep_top_n",     # publisher top-N to keep on refresh
    "harvest_records",# records to pull from the SEMrush export on a backlinks_file harvest
    "recipe_path",    # publisher recipe URL path segment (detected, overridable)
    "serp_query",     # VERBATIM Google query for the harvest (overrides recipe_path)
    "search_pages",   # how many SERP pages (~10 results each) to fetch on refresh
    "harvest_source", # discovery source: 'serp' (Google site:) or 'backlinks_file' (SEMrush export)
    "harvestable",    # 0 = no mechanical recipe access; skip publisher refresh
    "paywall",        # gated premium publisher (drives PA-remap)
    "harvest_ttl_days",    # refresh cadence (days) → drives the due-today worklist
    "semrush_report_url",  # editable one-click deep-link into SEMrush for this domain
    "backlinks_dir",       # OPTIONAL per-domain override folder for the SEMrush export
    "exclude_words",       # OPTIONAL per-domain EXCLUSIONARY sections (restaurant/chef/news)
)


def _canon_host(host: str) -> str:
    """Canonical host key: lowercase, no scheme, no path/query, no port, no
    leading dot, no ``www.``. Tolerates a pasted URL or trailing slash so the
    create form is forgiving about what the curator types."""
    h = (host or "").strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]  # drop path/query/frag
    h = h.split(":", 1)[0]                                    # drop :port
    h = h.lstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Denormalized recipe counts — STORED on the row (refreshed on access /
# domain-form open) so the list can show them at a glance. The recipe LISTS
# themselves are never stored — they're computed live by recipes_for_domain.
_COUNT_COLUMNS = {
    "master_recipe_count": "INTEGER NOT NULL DEFAULT 0",
    "user_recipe_count": "INTEGER NOT NULL DEFAULT 0",
    "counts_updated_at": "TEXT",
}

# Paywall PA-calibration (shift-and-scale remap of a gated publisher's PA to its
# free-equivalent). Recomputed by scripts/calibrate_paid_pa.py; consumed by the
# scorer (score_data_points_for_dish) for paywall=1 domains.
_PAYWALL_COLUMNS = {
    "paywall": "INTEGER NOT NULL DEFAULT 0",        # 1 = gated premium publisher
    "pa_cal_mean": "REAL",                          # this publisher's recipe-PA mean
    "pa_cal_std": "REAL",                           # ...std (spread)
    "pa_cal_n": "INTEGER",                          # sample size behind the calibration
    "pa_cal_free_mean": "REAL",                     # matched-DA free PA mean (target)
    "pa_cal_free_std": "REAL",                      # ...std
    "pa_cal_at": "TEXT",                            # when calibrated (ISO)
    # How many top recipes to KEEP (mark selected) on a publisher refresh — the
    # domains-page analog of a dish's top_n_final. Default 10, curator-overridable.
    "keep_top_n": "INTEGER NOT NULL DEFAULT 10",
    # How many records to pull from the SEMrush export file on a backlinks_file
    # harvest (the form's "Records to pull from file"). A harvest knob like
    # keep_top_n/search_pages; PERSISTED so it sticks across edits/reloads (was
    # read only at refresh time and reset to 100 each load). Default 100.
    "harvest_records": "INTEGER NOT NULL DEFAULT 100",
    # The publisher's recipe URL path segment (e.g. 'recipes', 'recipe', 'cooking').
    # NOT assumed — detected per publisher (collections_lib.detect_recipe_path) and
    # stored here, curator-overridable. '' = not yet detected.
    "recipe_path": "TEXT NOT NULL DEFAULT ''",
    # VERBATIM Google query for the publisher harvest (e.g. 'site:bostonglobe.com
    # recipe'). When set, it's run as-is via SerpAPI and OVERRIDES recipe_path — the
    # curator owns the Google syntax. '' = fall back to recipe_path detection.
    "serp_query": "TEXT NOT NULL DEFAULT ''",
    # 1 = this publisher has a mechanical way to enumerate recipes (refresh runs).
    # 0 = no mechanical access (no clean index / JS-only / hard paywall) → refresh
    # skips it so it doesn't pollute the list. Curator-set.
    "harvestable": "INTEGER NOT NULL DEFAULT 1",
    # SERP search depth on refresh: how many ~10-result pages to fetch (= discover_n
    # /10). More pages = more candidates + (Scale SERP) more credits + slower when
    # the recipe-check fetches each. Default 4 (~40 candidates).
    "search_pages": "INTEGER NOT NULL DEFAULT 4",
    # Discovery source for the publisher harvest: 'serp' (Google site: search) or
    # 'backlinks_file' (local SEMrush export, ranked by referring domains — the better
    # method for big sites with NO clean recipe subdir, where site: scatters and most
    # candidates are rejects). Curator-set; PERSISTED so the choice sticks across edits
    # / reloads (was previously read only at refresh time and lost). Default 'serp'.
    "harvest_source": "TEXT NOT NULL DEFAULT 'serp'",
    # SEMrush Rank — global ordinal by organic search TRAFFIC (rank 1 = most
    # traffic), looked up from the semrush_ranks reference table (see
    # input/pipeline/semrush_ranks.py). An independent traffic-authority signal
    # alongside DA/PA. NULL when the domain isn't in the (top-N, per-region) file.
    # Stamped at create + by refresh_all_semrush_ranks; NOT hand-edited (managed,
    # like the recipe counts) → kept out of EDITABLE_FIELDS. The corpus-relative
    # "rank the rank" is DERIVED, never stored (adding one domain reshuffles all).
    "semrush_rank": "INTEGER",
    "semrush_rank_at": "TEXT",   # when the rank was last stamped (ISO)
}

# Harvest scheduling — the SEMrush human-workflow loop (docs/semrush-harvest-
# scheduling.md). A "harvest" of a domain is the semi-automated SEMrush export:
# the system tells the curator WHICH domains are due, hands them a one-click deep-
# link into SEMrush, they press Run + Save the export, the watched inbox routes the
# file back here by its `{domain}` filename prefix, the existing backlinks_file
# pipeline ingests it, and on success we stamp `last_harvested_at` → the derived
# `next_harvest_at` rolls forward → the row drops off the "Due today" worklist.
# All on the domain row — NO separate task table (per the curator: "just a
# different view of the data incl. the scheduling stuff in the domain record").
_SCHEDULE_COLUMNS = {
    # When this domain's recipes were last successfully (re)harvested (ISO). NULL =
    # never harvested → "new" on the worklist. MANAGED (stamped by mark_harvested on
    # a successful backlinks_file ingest), not hand-edited.
    "last_harvested_at": "TEXT",
    # Per-domain refresh cadence in days (default ~quarterly). next_harvest_at =
    # last_harvested_at + this. Curator-overridable, like search_pages.
    "harvest_ttl_days": "INTEGER NOT NULL DEFAULT 90",
    # The editable one-click deep-link that opens SEMrush at THIS domain's Indexed-
    # Pages report (subfolder filter pre-applied) — the hotlink the curator clicks
    # from the worklist, then just presses Run + Export. '' = not captured yet (the
    # worklist still lists the domain; the link is just absent). Curator-owned, like
    # serp_query, so a SEMrush URL re-skin is a per-row field edit, not a code change.
    "semrush_report_url": "TEXT NOT NULL DEFAULT ''",
    # OPTIONAL per-domain override folder for THIS domain's SEMrush export. Blank =
    # use the configured inbox folder (system_config.semrush_inbox_dir → Downloads).
    # The harvest reads the file DIRECTLY from here — no copy into input/.
    "backlinks_dir": "TEXT NOT NULL DEFAULT ''",
    # OPTIONAL per-domain EXCLUSIONARY section words (comma/space-separated, e.g.
    # "restaurant chef news holiday event jobs"). A URL whose path has a SECTION matching
    # one is skipped outright — this site's taxonomy says it's not a recipe — overriding
    # any incidental food word (/restaurant/coppa/). PER-DOMAIN, never global. See
    # url_word_lists.url_excluded_by_domain.
    "exclude_words": "TEXT NOT NULL DEFAULT ''",
}

_RENDER_COLUMNS = {
    # JS-rendered publisher hint. 1 = the article body is injected client-side, so a
    # static (render=False) fetch sees only the nav shell and the is-recipe verify
    # under-scores real recipes (Boston Globe). When set, the harvest fetches this
    # domain with a real browser (unblocker render=True) up front instead of paying a
    # wasted plain pass first. AUTO-LEARNED: the harvest sets it the first time a
    # render-escalation rescues a recipe here; also curator-editable. Needs an
    # unblocker-capable fetch_strategy + BYOK creds to take effect.
    "render_required": "INTEGER NOT NULL DEFAULT 0",
    # When render_required was last auto-learned by a render-escalation (ISO). MANAGED.
    "render_learned_at": "TEXT",
    # Harvest MODE persistence: 1 = the curator's default for this domain is the
    # "Curate · score & pick" mode (score every candidate URL-only, select nothing,
    # skip auto-extract). It's what distinguishes Curate from Blocked (both use
    # fetch_strategy=unblocker), so without persisting it the Curate choice was lost
    # on save. Editable via the harvest-mode picker; the form's Save now stores it.
    "score_only": "INTEGER NOT NULL DEFAULT 0",
}

# Editorial provenance the curator can set (optional). cuisine_focus already lives in
# the base CREATE; ethnicity (cultural origin) is added here so PRE-EXISTING DBs migrate.
_EDITORIAL_COLUMNS = {
    "ethnicity": "TEXT",
}

# Poor-publisher signal (2026-07-08). The is-recipe LLM cascade tags harvest pages
# recipe|not_recipe|poor_quality; a domain whose pages are repeatedly poor_quality
# (a messy source we can't extract cleanly) is a POOR PUBLISHER. We roll the cascade
# verdicts up per domain (from training.db) and, past a sample+fraction threshold,
# flag it here so the harvest STOPS paying the per-page LLM cascade for its pages and
# the curator can review it. MANAGED (recomputed by refresh_poor_publisher_flags), not
# hand-edited → kept out of EDITABLE_FIELDS.
_QUALITY_COLUMNS = {
    "poor_quality_flag": "INTEGER NOT NULL DEFAULT 0",   # 1 = flagged poor publisher
    "poor_quality_rate": "REAL",                         # fraction of poor_quality verdicts
    "poor_quality_samples": "INTEGER",                   # cascade-classified pages behind it
    "poor_quality_flagged_at": "TEXT",                   # when last (re)computed (ISO)
}


def ensure_domains_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS domains (
            domain              TEXT PRIMARY KEY,
            root_domain         TEXT NOT NULL DEFAULT '',
            display_name        TEXT NOT NULL DEFAULT '',
            story               TEXT,
            logo_url            TEXT,
            country             TEXT,
            language            TEXT,
            cuisine_focus       TEXT,
            ethnicity           TEXT,
            fetch_strategy      TEXT NOT NULL DEFAULT 'plain',
            extract_notes       TEXT,
            custom_extractor    TEXT,
            allowed             INTEGER NOT NULL DEFAULT 1,
            domain_authority    REAL,
            da_last_scored      TEXT,
            notes               TEXT,
            failure_count       INTEGER NOT NULL DEFAULT 0,
            master_recipe_count INTEGER NOT NULL DEFAULT 0,
            user_recipe_count   INTEGER NOT NULL DEFAULT 0,
            counts_updated_at   TEXT,
            created_at          TEXT NOT NULL DEFAULT '',
            updated_at          TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # Migrate pre-existing tables that lack the count columns.
    have = {r[1] for r in conn.execute("PRAGMA table_info(domains)")}
    for col, decl in _COUNT_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    # Paywall PA-calibration (2026-06-16): premium/gated publishers earn far less
    # PAGE authority than free sites at the same DA, so OU penalizes them unfairly.
    # We store a per-publisher shift-and-scale calibration so the scorer can remap
    # their PA to a free-equivalent. `paywall=1` flags a gated premium publisher;
    # pa_cal_* are this publisher's own PA mean/std/n; pa_cal_free_* are the matched-
    # DA free reference at calibration time. See scripts/calibrate_paid_pa.py.
    for col, decl in _PAYWALL_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    # Harvest-scheduling columns (the SEMrush human-workflow loop).
    for col, decl in _SCHEDULE_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    for col, decl in _RENDER_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    for col, decl in _EDITORIAL_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    for col, decl in _QUALITY_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE domains ADD COLUMN {col} {decl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_domains_root ON domains(root_domain)"
    )
    conn.commit()


def set_paywall_calibration(conn, domain, *, da=None, pa_mean, pa_std, pa_n,
                            free_mean, free_std, paywall=True):
    """Store/refresh a premium publisher's PA-calibration on its domains row
    (creating a minimal row if absent). Recomputed by the harvester; read by the
    scorer to remap gated PA to free-equivalent."""
    ensure_domains_table(conn)
    host = _canon_host(domain)
    now = _now()
    if not domain_exists(conn, host):
        conn.execute(
            "INSERT INTO domains (domain, root_domain, created_at, updated_at) VALUES (?,?,?,?)",
            (host, root_domain(host) or host, now, now),
        )
    sets = ["paywall = ?", "pa_cal_mean = ?", "pa_cal_std = ?", "pa_cal_n = ?",
            "pa_cal_free_mean = ?", "pa_cal_free_std = ?", "pa_cal_at = ?", "updated_at = ?"]
    params = [1 if paywall else 0, pa_mean, pa_std, pa_n, free_mean, free_std, now, now]
    if da is not None:
        sets.append("domain_authority = ?"); params.append(da)
    params.append(host)
    conn.execute(f"UPDATE domains SET {', '.join(sets)} WHERE domain = ?", params)
    conn.commit()


def get_paywall_calibrations(conn=None, db_path: str = _DEFAULT_DB) -> list:
    """Flagged premium publishers with a usable calibration — for the scorer AND
    the batch winner-selector. Returns dicts {domain, da, pa_mean, pa_std,
    free_mean, free_std}; only rows with a positive pa_cal_std (a real spread to
    scale against) and a solid sample (n>=15). Pass a `conn`, or omit it to open
    `db_path` (lets build_query_batch read it connection-free)."""
    own = conn is None
    if own:
        conn = _connect(db_path)
    try:
        if own:
            ensure_domains_table(conn)
        rows = conn.execute(
            "SELECT domain, domain_authority, pa_cal_mean, pa_cal_std, "
            "pa_cal_free_mean, pa_cal_free_std FROM domains "
            "WHERE paywall = 1 AND pa_cal_mean IS NOT NULL AND pa_cal_std > 0 "
            "AND pa_cal_free_mean IS NOT NULL AND pa_cal_free_std IS NOT NULL "
            # Ignore thin/unreliable calibrations (e.g. a publisher with too few
            # discovered recipe pages) so the remap only fires on solid samples.
            "AND COALESCE(pa_cal_n, 0) >= 15"
        ).fetchall()
        return [{"domain": r[0], "da": r[1], "pa_mean": r[2], "pa_std": r[3],
                 "free_mean": r[4], "free_std": r[5]} for r in rows]
    except Exception:
        return []
    finally:
        if own:
            try:
                conn.close()
            except Exception:
                pass


def seed_domains(conn: sqlite3.Connection) -> int:
    """One-time bootstrap from the shipped JSON name map. INSERT OR IGNORE so
    curator edits to the table are never clobbered on a later boot (same as
    the chapters seed). Returns the number of rows inserted."""
    ensure_domains_table(conn)
    try:
        with open(_SEED_PATH, encoding="utf-8") as f:
            seed = json.load(f)
    except Exception:
        return 0
    now = _now()
    inserted = 0
    for raw_host, name in seed.items():
        host = _canon_host(raw_host)
        if not host:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO domains
                (domain, root_domain, display_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (host, root_domain(host) or host, name, now, now),
        )
        inserted += cur.rowcount or 0
    conn.commit()
    invalidate_cache()
    return inserted


# --- Harvest scheduling (the SEMrush human-workflow worklist) -----------------
# A domain is part of the SEMrush manual flow when its discovery source is the
# backlinks file OR it carries a captured SEMrush report URL. For those, we derive
# the schedule on read (never stored — adding a domain or editing a TTL can't leave
# it stale, exactly like bcc_rank and the dish next_run_at).

def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def report_url_template(conn: Optional[sqlite3.Connection] = None) -> str:
    """The SEMrush Indexed-Pages deep-link template (`{domain}` placeholder), from
    system_config so a SEMrush URL re-skin is a config edit ([[feedback_no_data_in_code]])."""
    from input.pipeline import system_config
    return system_config.get_setting(
        "semrush_indexed_pages_url_template",
        "https://www.semrush.com/analytics/backlinks/pages/"
        "?q={domain}&searchType=domain&sort_field=domainsnum")


def _derive_schedule(d: dict, report_template: Optional[str] = None) -> None:
    """Stamp DERIVED harvest-schedule fields onto a domain dict in place:
      - semrush_report_url : if the row has none stored, default it from the template
                             with `{domain}` substituted (so the worklist link + form
                             field just work; a stored value overrides).
      - next_harvest_at : last_harvested_at's date + harvest_ttl_days (None if the
                          domain isn't SEMrush-managed; '' last → never)
      - harvest_status  : 'new' (never harvested) | 'due' (next <= today) | 'ok'
                          | None (not part of the SEMrush flow)
      - harvest_due     : bool — convenience for the worklist filter
    next_harvest is date-grain (the cadence is days); a missing/garbage timestamp
    reads as 'new' rather than raising."""
    # Worklist membership = the SEMrush backlinks-file flow ONLY. Deliberately NOT
    # keyed on semrush_report_url — that link is now auto-defaulted for every domain
    # (below), so keying on it would put the whole corpus on the worklist.
    managed = d.get("harvest_source") == "backlinks_file"
    # Default the report deep-link from the template when the row has none stored.
    if not (d.get("semrush_report_url") or "").strip() and d.get("domain"):
        tmpl = report_template if report_template is not None else report_url_template()
        if tmpl:
            d["semrush_report_url"] = tmpl.replace("{domain}", d["domain"])
    d["next_harvest_at"] = None
    d["harvest_status"] = None
    d["harvest_due"] = False
    if not managed:
        return
    last = (d.get("last_harvested_at") or "").strip()
    if not last:
        d["harvest_status"] = "new"
        d["harvest_due"] = True
        return
    try:
        ttl = int(d.get("harvest_ttl_days") or 90)
        nxt = (datetime.fromisoformat(last).date()
               + timedelta(days=ttl)).isoformat()
    except Exception:
        d["harvest_status"] = "new"
        d["harvest_due"] = True
        return
    d["next_harvest_at"] = nxt
    if nxt <= _today():
        d["harvest_status"] = "due"
        d["harvest_due"] = True
    else:
        d["harvest_status"] = "ok"


def mark_harvested(conn: sqlite3.Connection, domain: str,
                   when: Optional[str] = None) -> None:
    """Stamp a successful harvest → resets the derived next_harvest_at and drops the
    domain off the worklist. Called by the publisher_refresh job on a successful
    backlinks_file ingest (covers BOTH the manual refresh button and the watched-
    inbox path). Best-effort; never raises into the job."""
    host = _canon_host(domain)
    try:
        ensure_domains_table(conn)
        conn.execute("UPDATE domains SET last_harvested_at = ? WHERE domain = ?",
                     (when or _now(), host))
        conn.commit()
        invalidate_cache()
    except Exception:
        pass


def mark_render_required(domain: str, conn: Optional[sqlite3.Connection] = None,
                         db_path: str = _DEFAULT_DB) -> None:
    """Auto-learn the JS-rendered hint: the first time a render-escalation rescues a
    recipe on this domain, flag it so future harvests fetch it with a real browser up
    front (skipping the wasted plain pass). Idempotent — only writes if not already
    set. Best-effort; never raises into a job. Pass a `conn`, or omit to open
    `db_path` (lets the out-of-process harvest call it connection-free)."""
    own = conn is None
    if own:
        conn = _connect(db_path)
    try:
        ensure_domains_table(conn)
        host = _canon_host(domain)
        row = conn.execute("SELECT render_required FROM domains WHERE domain = ?",
                           (host,)).fetchone()
        if row is None or row[0]:           # absent row, or already flagged → nothing to do
            return
        conn.execute("UPDATE domains SET render_required = 1, render_learned_at = ?, "
                     "updated_at = ? WHERE domain = ?", (_now(), _now(), host))
        conn.commit()
        invalidate_cache()
    except Exception:
        pass
    finally:
        if own:
            conn.close()


def harvest_worklist(conn: sqlite3.Connection) -> list[dict]:
    """The "Due today" worklist: every SEMrush-managed, allowed domain that is NEW
    (never harvested) or DUE/overdue, each carrying its deep-link + schedule fields.
    Ordered new-first, then by oldest next_harvest_at (most overdue first). A plain
    view over the domains rows — no separate table."""
    out = [d for d in list_domains(conn)
           if d.get("allowed") and d.get("harvest_due")]
    out.sort(key=lambda d: (d.get("harvest_status") != "new",
                            d.get("next_harvest_at") or ""))
    return out


def list_domains(conn: sqlite3.Connection) -> list[dict]:
    ensure_domains_table(conn)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM domains ORDER BY domain COLLATE NOCASE"
    ).fetchall()]
    # Derive bcc_rank = the corpus-relative ordinal ("rank the rank"): order the
    # ALLOWED domains that HAVE a semrush_rank by it asc, assign 1..N. Blocked
    # domains (allowed=0) and null-rank domains get None. Computed here (not
    # stored) so adding/blocking a domain can't leave it stale.
    ranked = sorted(
        (d for d in rows if d.get("semrush_rank") is not None and d.get("allowed")),
        key=lambda d: d["semrush_rank"],
    )
    for i, d in enumerate(ranked, 1):
        d["bcc_rank"] = i
    tmpl = report_url_template()   # read once, not per-row
    for d in rows:
        d.setdefault("bcc_rank", None)
        _derive_schedule(d, tmpl)
    return rows


def get_domain(conn: sqlite3.Connection, domain: str) -> Optional[dict]:
    ensure_domains_table(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM domains WHERE domain = ?", (_canon_host(domain),)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["bcc_rank"] = _semrush_rank_local(conn, d)
    _derive_schedule(d)
    return d


def domain_exists(conn: sqlite3.Connection, domain: str) -> bool:
    ensure_domains_table(conn)
    return bool(conn.execute(
        "SELECT 1 FROM domains WHERE domain = ?", (_canon_host(domain),)
    ).fetchone())


def create_domain(conn: sqlite3.Connection, domain: str, fields: dict) -> dict:
    """Insert a curator-created domain row. Raises ValueError on a blank or
    duplicate host. Unknown keys in `fields` are ignored."""
    host = _canon_host(domain)
    if not host:
        raise ValueError("Domain (host) is required")
    ensure_domains_table(conn)
    if domain_exists(conn, host):
        raise ValueError(f"Domain '{host}' already exists")
    now = _now()
    payload = {k: fields.get(k) for k in EDITABLE_FIELDS if k in fields}
    payload.setdefault("display_name", "")
    cols = ["domain", "root_domain", "created_at", "updated_at", *payload.keys()]
    vals = [host, root_domain(host) or host, now, now, *payload.values()]
    conn.execute(
        f"INSERT INTO domains ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        vals,
    )
    conn.commit()
    stamp_semrush_rank(conn, host)   # look up + persist the new domain's SEMrush rank
    invalidate_cache()
    return get_domain(conn, host)


def update_domain(conn: sqlite3.Connection, domain: str, fields: dict) -> dict:
    """Patch an existing row. Only keys in EDITABLE_FIELDS are written."""
    host = _canon_host(domain)
    if not domain_exists(conn, host):
        raise ValueError(f"Domain '{host}' not found")
    sets = {k: fields[k] for k in EDITABLE_FIELDS if k in fields}
    # The form pre-fills semrush_report_url with the auto-derived default; if the
    # curator didn't customize it, store '' so the field keeps meaning "custom
    # override only" and a template change still propagates. A real custom URL persists.
    if "semrush_report_url" in sets:
        derived = (report_url_template() or "").replace("{domain}", host)
        if (sets["semrush_report_url"] or "").strip() == derived:
            sets["semrush_report_url"] = ""
    if not sets:
        return get_domain(conn, host)
    sets["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in sets)
    conn.execute(
        f"UPDATE domains SET {assignments} WHERE domain = ?",
        (*sets.values(), host),
    )
    conn.commit()
    invalidate_cache()
    return get_domain(conn, host)


def delete_domain(conn: sqlite3.Connection, domain: str) -> bool:
    host = _canon_host(domain)
    ensure_domains_table(conn)
    cur = conn.execute("DELETE FROM domains WHERE domain = ?", (host,))
    conn.commit()
    invalidate_cache()
    return bool(cur.rowcount)


# --- SEMrush traffic rank -----------------------------------------------
# A domain's global SEMrush Rank (organic-traffic ordinal) is looked up from the
# semrush_ranks reference table and STORED on the row (a snapshot, stable until a
# refresh). The corpus-relative local rank ("rank the rank") is DERIVED on read.

def stamp_semrush_rank(conn: sqlite3.Connection, domain: str,
                       region: str = "us") -> Optional[int]:
    """Look up `domain`'s SEMrush rank in the reference table and persist it on
    its domains row. Returns the rank (or None when the domain isn't in the file).
    Best-effort — never raises if the ranks table is empty/absent."""
    from input.pipeline import semrush_ranks
    host = _canon_host(domain)
    try:
        row = semrush_ranks.get_rank(conn, host, region=region)
    except Exception:
        return None
    rank = row.get("rank") if row else None
    try:
        ensure_domains_table(conn)
        conn.execute(
            "UPDATE domains SET semrush_rank = ?, semrush_rank_at = ? WHERE domain = ?",
            (rank, _now(), host),
        )
        conn.commit()
    except Exception:
        pass
    return rank


def refresh_all_semrush_ranks(conn: sqlite3.Connection, region: str = "us") -> dict:
    """Re-stamp every domain's semrush_rank from the current semrush_ranks table —
    run after importing a fresh ranks file. Matched by the domain's exact host OR
    its two-part root (so subdomains inherit their root's rank). Returns
    {matched, total}. The two-part-root match is fiddly in pure SQL, so we build
    the rank map once and match in Python via lookup_keys — ~270 rows, trivial."""
    from input.pipeline import semrush_ranks
    ensure_domains_table(conn)
    semrush_ranks.ensure_semrush_ranks_table(conn)
    now = _now()
    rank_by_domain = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT domain, rank FROM semrush_ranks WHERE region = ?", (region,)
        ).fetchall()
    }
    matched = total = 0
    for (host,) in conn.execute("SELECT domain FROM domains").fetchall():
        total += 1
        rank = None
        for key in semrush_ranks.lookup_keys(host):
            if key in rank_by_domain:
                rank = rank_by_domain[key]
                break
        conn.execute(
            "UPDATE domains SET semrush_rank = ?, semrush_rank_at = ? WHERE domain = ?",
            (rank, now, host),
        )
        if rank is not None:
            matched += 1
    conn.commit()
    invalidate_cache()
    return {"matched": matched, "total": total, "region": region}


def _semrush_rank_local(conn: sqlite3.Connection, row: dict) -> Optional[int]:
    """BCC rank for a domain row: how it ranks among ONLY our ALLOWED domains
    that carry a SEMrush rank (1 = most-traffic publisher). DERIVED, not stored.
    Blocked domains (allowed=0) keep their raw semrush_rank but get no BCC rank —
    they aren't sourcing targets, so they'd only skew the ladder."""
    rank = row.get("semrush_rank")
    if rank is None or not row.get("allowed"):
        return None
    n = conn.execute(
        "SELECT COUNT(*) FROM domains "
        "WHERE allowed = 1 AND semrush_rank IS NOT NULL AND semrush_rank < ?",
        (rank,),
    ).fetchone()[0]
    return int(n) + 1


def recipes_for_domain(conn: sqlite3.Connection, domain: str) -> dict:
    """Live lookup of the recipes sourced from a domain, split master vs user.

    Returns ``{"master": {"count", "items"}, "user": {"count", "items"}}``
    where each item is ``{recipe_id, name}`` (lightweight — enough to fill a
    browse dropdown). Matches on the recipe's canonical HOST (derived from
    url_normalized, falling back to _source.originalUrl), consistent with the
    full-host grain of the domain master.

    Side effect: STORES the two counts (and counts_updated_at) on the domain
    row so the list view can show them without recomputing. The LISTS are not
    stored — they're cheap to recompute and would drift.
    """
    from input.pipeline.site_names import host_from_url

    host = _canon_host(domain)
    result = {
        "master": {"count": 0, "items": []},
        "user": {"count": 0, "items": []},
    }
    for tbl, bucket in (("master_recipes", "master"), ("recipes", "user")):
        try:
            # Cheap pass: pull ONLY recipe_id + url_normalized (no ~19KB data blob).
            # Match the host off url_normalized; rows with an empty url_normalized
            # fall back to the source URL's host (older rows predating the url
            # backfill — 38 such rows still carry _source.originalUrl). The data
            # blob is then parsed ONLY for candidates (matches + the few empties),
            # not the whole table — the audit's 960-blob scan → ~matches+empties.
            id_rows = conn.execute(
                f"SELECT recipe_id, url_normalized FROM {tbl}"
            ).fetchall()
        except Exception:
            continue
        match_ids: list = []      # url_normalized host == this domain
        fallback_ids: list = []   # empty url_normalized → check _source.originalUrl
        for recipe_id, url_norm in id_rows:
            h = host_from_url(url_norm or "")
            if h == host:
                match_ids.append(recipe_id)
            elif not h:
                fallback_ids.append(recipe_id)
        items = []
        need = match_ids + fallback_ids
        if need:
            mset = set(match_ids)
            placeholders = ",".join("?" * len(need))
            for recipe_id, dj in conn.execute(
                f"SELECT recipe_id, data FROM {tbl} WHERE recipe_id IN ({placeholders})",
                need,
            ):
                try:
                    d = json.loads(dj)
                except Exception:
                    d = {}
                if recipe_id in mset:
                    items.append({"recipe_id": recipe_id, "name": d.get("name") or "(no title)"})
                else:  # empty url_normalized — match on the source URL's host
                    fh = host_from_url((d.get("_source") or {}).get("originalUrl") or "")
                    if fh == host:
                        items.append({"recipe_id": recipe_id, "name": d.get("name") or "(no title)"})
        items.sort(key=lambda x: x["name"].lower())
        result[bucket]["items"] = items
        result[bucket]["count"] = len(items)

    # Persist the counts (refresh-on-access). Best-effort; never raises.
    try:
        ensure_domains_table(conn)
        if domain_exists(conn, host):
            conn.execute(
                "UPDATE domains SET master_recipe_count = ?, user_recipe_count = ?, "
                "counts_updated_at = ? WHERE domain = ?",
                (result["master"]["count"], result["user"]["count"], _now(), host),
            )
            conn.commit()
    except Exception:
        pass

    result["domain"] = host
    return result


# --- display-name cache for the hot resolver path -----------------------
# friendly_site_name() runs on every save and every master-list row, so it
# can't open a connection per call. Cache the {host: display_name} map for
# allowed-or-not rows (display is independent of the allow flag) and let the
# CRUD writers above invalidate it.

_DISPLAY_CACHE: Optional[dict] = None
_BLOCKED_CACHE: Optional[set] = None
_RENDER_CACHE: Optional[set] = None
_POOR_CACHE: Optional[set] = None


def invalidate_cache() -> None:
    global _DISPLAY_CACHE, _BLOCKED_CACHE, _RENDER_CACHE, _POOR_CACHE
    _DISPLAY_CACHE = None
    _BLOCKED_CACHE = None
    _RENDER_CACHE = None
    _POOR_CACHE = None


def parse_serp_exclusions(db_path: str = _DEFAULT_DB) -> tuple[set, list]:
    """Parse the editable `serp_exclusions` system_config list into
    (domains, terms). A line containing a dot is a DOMAIN (drives the SERP
    `-site:` exclusion AND the downstream domain filter); any other line is a
    bare TERM (`-term`, SERP-query only). Lenient: strips a stray leading '-'
    or 'site:', drops blank/`#` lines, accepts comma or newline separators.
    Returns (set(), []) on any error."""
    try:
        from input.pipeline import system_config as cfg
        raw = cfg.get_setting("serp_exclusions", "", db_path=db_path) or ""
    except Exception:
        return set(), []
    domains: set = set()
    terms: list = []
    for line in str(raw).replace(",", "\n").splitlines():
        s = line.strip().lstrip("-").strip()
        if s.lower().startswith("site:"):
            s = s[5:].strip()
        if not s or s.startswith("#"):
            continue
        if "." in s:
            d = s.lower()
            if d.startswith("www."):
                d = d[4:]
            domains.add(d)
        else:
            terms.append(s)
    return domains, terms


def get_render_eligible_hosts(db_path: str = _DEFAULT_DB) -> set:
    """Set of hosts+roots whose pages may use the full-browser render escalation:
    domains flagged ``render_required = 1`` (learned/curated JS-rendered sites) OR
    carrying an unblocker fetch_strategy. Used by the DISH batch to mark per-result
    entries `_allow_render`, so a multi-domain batch escalates only these (a publisher
    refresh of a single flagged domain escalates the whole run without this). Cached;
    CRUD writers + mark_render_required invalidate it."""
    global _RENDER_CACHE
    if _RENDER_CACHE is None:
        hosts: set = set()
        try:
            with _connect(db_path) as conn:
                ensure_domains_table(conn)
                for dom, root in conn.execute(
                    "SELECT domain, root_domain FROM domains "
                    "WHERE render_required = 1 OR fetch_strategy = 'unblocker'"
                ):
                    if dom:
                        hosts.add(dom.lower())
                    if root:
                        hosts.add(root.lower())
        except Exception:
            pass
        _RENDER_CACHE = hosts
    return _RENDER_CACHE


def get_poor_publisher_hosts(db_path: str = _DEFAULT_DB) -> set:
    """Cached set of hosts+roots flagged POOR publishers (poor_quality_flag = 1). Used
    by the is-recipe LLM cascade to SKIP the per-page classify for a domain we've already
    judged a messy source — stop re-paying to relearn it. CRUD writers +
    refresh_poor_publisher_flags invalidate it."""
    global _POOR_CACHE
    if _POOR_CACHE is None:
        hosts: set = set()
        try:
            with _connect(db_path) as conn:
                ensure_domains_table(conn)
                for dom, root in conn.execute(
                    "SELECT domain, root_domain FROM domains WHERE poor_quality_flag = 1"
                ):
                    if dom:
                        hosts.add(dom.lower())
                    if root:
                        hosts.add(root.lower())
        except Exception:
            pass
        _POOR_CACHE = hosts
    return _POOR_CACHE


def refresh_poor_publisher_flags(conn: Optional[sqlite3.Connection] = None,
                                 db_path: str = _DEFAULT_DB, *,
                                 min_samples: Optional[int] = None,
                                 threshold: Optional[float] = None) -> dict:
    """Roll the is-recipe LLM cascade's per-page verdicts (from the git-ignored
    training.db) up per HOST and (re)set each domain's poor_quality_* columns. Keyed on
    the URL host across EVERY source (dish batches AND publisher harvests) — a messy
    source is a messy source wherever it shows up. A host with at least `min_samples`
    cascade-classified pages whose poor_quality FRACTION is >= `threshold` is flagged a
    poor publisher (poor_quality_flag = 1) — EXCEPT a curated `paywall = 1` domain, which is
    EXEMPT (its stubs read poor_quality because they're GATED, not messy) — auto-creating a
    minimal domains row if absent (mirrors set_paywall_calibration) so the flag persists + the
    curator can review it; a host that already HAS a row also gets its rate/samples refreshed
    (and cleared to 0 if it no longer crosses). Thresholds default from system_config
    (poor_publisher_min_samples / poor_publisher_threshold). Best-effort; returns a summary
    {flagged:[...], exempted_paywall:[...], scored:int, min_samples, threshold} ({} on error). Pass a
    `conn`, or omit to open `db_path` (lets the out-of-process harvest call it
    connection-free)."""
    if min_samples is None or threshold is None:
        try:
            from input.pipeline import system_config as cfg
            if min_samples is None:
                min_samples = int(cfg.get_setting("poor_publisher_min_samples", 8) or 8)
            if threshold is None:
                threshold = float(cfg.get_setting("poor_publisher_threshold", 0.5) or 0.5)
        except Exception:
            pass
    min_samples = int(min_samples if min_samples is not None else 5)
    threshold = float(threshold if threshold is not None else 0.5)

    def _host(u: str) -> str:
        try:
            h = u.split("//", 1)[1].split("/", 1)[0].lower() if "//" in u else ""
            return h[4:] if h.startswith("www.") else h
        except Exception:
            return ""

    # Aggregate cascade verdicts per URL HOST from training.db (every source).
    try:
        from intake.training_capture import TRAINING_DB_PATH
        tconn = sqlite3.connect(TRAINING_DB_PATH, timeout=5.0)
        try:
            rows = tconn.execute(
                "SELECT url, shadow_verdict FROM is_recipe_samples "
                "WHERE shadow_verdict IS NOT NULL AND url IS NOT NULL"
            ).fetchall()
        finally:
            tconn.close()
    except Exception:
        return {}
    counts: dict = {}   # canon host -> [n, poor]
    for url, verdict in rows:
        dom = _canon_host(_host(url or ""))
        if not dom:
            continue
        c = counts.setdefault(dom, [0, 0])
        c[0] += 1
        if verdict == "poor_quality":
            c[1] += 1
    own = conn is None
    if own:
        conn = _connect(db_path)
    flagged, scored = [], 0
    try:
        ensure_domains_table(conn)
        now = _now()
        exempted = []
        for dom, (n, poor) in counts.items():
            rate = (poor / n) if n else 0.0
            flag = 1 if (n >= min_samples and rate >= threshold) else 0
            row = conn.execute("SELECT paywall FROM domains WHERE domain = ?", (dom,)).fetchone()
            exists = row is not None
            # PAYWALL EXEMPTION: a gated publisher's pages fetch as STUBS and get tagged
            # poor_quality — that's the PAYWALL, not a messy source. Never flag a paywall=1
            # domain (we'd wrongly suppress its cascade); still record the rate for the
            # curator's visibility. (An auto-created host is never paywalled, so this only
            # spares curated paywall publishers like Milk Street.)
            if flag and exists and row[0]:
                flag = 0
                exempted.append(dom)
            if not exists:
                if not flag:
                    continue   # don't mint rows for unknown hosts that aren't poor
                conn.execute(
                    "INSERT INTO domains (domain, root_domain, display_name, notes, "
                    "created_at, updated_at) VALUES (?, ?, '', ?, ?, ?)",
                    (dom, root_domain(dom) or dom,
                     "auto-added: poor-publisher signal (is-recipe cascade)", now, now),
                )
            conn.execute(
                "UPDATE domains SET poor_quality_flag = ?, poor_quality_rate = ?, "
                "poor_quality_samples = ?, poor_quality_flagged_at = ? WHERE domain = ?",
                (flag, round(rate, 4), n, now, dom),
            )
            scored += 1
            if flag:
                flagged.append(dom)
        conn.commit()
        invalidate_cache()
    except Exception:
        return {}
    finally:
        if own:
            conn.close()
    return {"flagged": flagged, "exempted_paywall": exempted, "scored": scored,
            "min_samples": min_samples, "threshold": threshold}


def get_blocked_root_domains(db_path: str = _DEFAULT_DB) -> set:
    """Set of blocked root domains: the editable system_config `disallowed_domains`
    list UNIONed with the `serp_exclusions` textarea's domain lines. Used by the batch
    SERP filter at root-domain grain, BEFORE any fetch. Both sources are DB-resident +
    curator-editable — no code change, and (unlike the retired ``domains.allowed = 0``
    flag this replaced) no per-publisher domains-master row is needed to block a junk
    host. Read fresh through system_config's own write-invalidated cache, so edits take
    effect without a restart. Degrades to whatever it can read on error."""
    blocked: set = set()
    try:
        from input.pipeline import system_config as cfg
        raw = cfg.get_setting("disallowed_domains", [], db_path=db_path) or []
        if isinstance(raw, str):                    # tolerate a text/newline value
            raw = raw.replace(",", "\n").splitlines()
        for item in raw:
            h = _canon_host(str(item))
            if not h:
                continue
            blocked.add(h)
            r = (root_domain(h) or "").lower()
            if r:
                blocked.add(r)
    except Exception:
        pass
    try:
        extra, _ = parse_serp_exclusions(db_path)
    except Exception:
        extra = set()
    return blocked | extra


def seed_disallowed_domains(conn: sqlite3.Connection, hosts) -> int:
    """Seed the batch's hardcoded disallowed list into the table as
    ``allowed = 0`` rows so the table becomes the source of truth. Existing
    rows are forced to allowed=0 (a curator can re-allow later). Returns the
    number of rows touched."""
    ensure_domains_table(conn)
    now = _now()
    n = 0
    for raw in hosts:
        host = _canon_host(raw)
        if not host:
            continue
        if domain_exists(conn, host):
            conn.execute(
                "UPDATE domains SET allowed = 0, updated_at = ? WHERE domain = ?",
                (now, host),
            )
        else:
            conn.execute(
                """
                INSERT INTO domains (domain, root_domain, display_name, allowed,
                                     notes, created_at, updated_at)
                VALUES (?, ?, '', 0, 'seeded from disallowed list', ?, ?)
                """,
                (host, root_domain(host) or host, now, now),
            )
        n += 1
    conn.commit()
    invalidate_cache()
    return n


def get_display_map(db_path: str = _DEFAULT_DB) -> dict:
    """Cached {host: display_name} for every domain row with a name. Seeds
    the table from JSON on first use if it's empty. Falls back to the raw
    JSON map if the DB is unavailable so the resolver never hard-fails."""
    global _DISPLAY_CACHE
    if _DISPLAY_CACHE is not None:
        return _DISPLAY_CACHE
    try:
        with _connect(db_path) as conn:
            ensure_domains_table(conn)
            row = conn.execute("SELECT COUNT(*) FROM domains").fetchone()
            if not row or row[0] == 0:
                seed_domains(conn)
            rows = conn.execute(
                "SELECT domain, display_name FROM domains "
                "WHERE display_name != ''"
            ).fetchall()
        _DISPLAY_CACHE = {d: n for d, n in rows}
    except Exception:
        try:
            with open(_SEED_PATH, encoding="utf-8") as f:
                _DISPLAY_CACHE = {_canon_host(k): v for k, v in json.load(f).items()}
        except Exception:
            _DISPLAY_CACHE = {}
    return _DISPLAY_CACHE
