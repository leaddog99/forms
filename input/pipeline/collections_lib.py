"""collections_lib.py — generic typed collections + membership (the M2M junction).

A recipe (by url_normalized) can belong to MANY collections — a dish, a
publisher's "best of", a chef, a curated list. Each is a (collection_type,
collection_key) pair; `collection_members` is the junction + per-collection
ranking ledger. One recipe → many membership rows (the "join" is free).

First consumer: PUBLISHER collections (the domains page, dishes-page-style). A
publisher refresh discovers the publisher's recipe URLs (SERP `site:domain/recipes`
+ filter=0), Moz-scores them, ranks by PA (most-notable), and keeps the top-N
(`keep`, default 10, per-publisher overridable — the analog of a dish's
top_n_final). This module builds the discovery + ranking LEDGER; the publisher
refresh JOB then auto-extracts the selected winners into master_recipes (the
"ledger -> master" step in save_recipe_api._handle_publisher_refresh_job, added
2026-06-22 / f306d80, mirroring the dish batch). See docs/collections.md /
project_collections.

Dishes keep their own ledger (dish_run_data_points) for now; a recipe's full
membership view unions both.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from input.pipeline.url_scoring import score_url_via_moz  # loads .env on import
from input.pipeline.url_utils import normalize_url, root_domain

_SERPAPI_KEY = os.getenv("SERPAPI_KEY")


def ensure_collection_members_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_members (
            collection_type TEXT NOT NULL,     -- 'publisher' (later 'dish','chef',…)
            collection_key  TEXT NOT NULL,     -- the domain / dish name / …
            url_normalized  TEXT NOT NULL,     -- the recipe (→ master_recipes/recipes)
            title           TEXT,
            da              REAL,
            pa              REAL,
            adjusted_pa     REAL,              -- paid-remapped PA (when it competes cross-collection)
            rank_score      REAL,
            rank            INTEGER,           -- 1-based within the collection
            selected        INTEGER NOT NULL DEFAULT 0,  -- 1 = a kept top-N member
            note            TEXT,
            model_version   TEXT,              -- the refresh run id
            created_at      TEXT NOT NULL,
            PRIMARY KEY (collection_type, collection_key, url_normalized)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_coll_members_url ON collection_members(url_normalized)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_coll_members_coll ON collection_members(collection_type, collection_key, rank)")
    # Idempotent column adds:
    #  image_url   — the recipe's og:image (selected top-N) so discovered members show a thumb.
    #  traffic     — SEMrush per-page monthly organic traffic (the meaningful tiebreaker when
    #                PA saturates to near-identical values across a publisher's pages).
    #  traffic_pct — SEMrush "Traffic (%)" — the page's share of the publisher's traffic.
    #  file_seq    — 1-based position in the SEMrush export as delivered (provenance/future use).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(collection_members)")}
    for _c, _t in (("image_url", "TEXT"), ("traffic", "REAL"),
                   ("traffic_pct", "REAL"), ("file_seq", "INTEGER")):
        if _c not in cols:
            conn.execute(f"ALTER TABLE collection_members ADD COLUMN {_c} {_t}")
    conn.commit()


def replace_members(conn, collection_type, collection_key, members, model_version=None) -> int:
    """Delete-and-replace the members of ONE collection — on the JUNCTION, never a
    master content row (the collections-correct delete-replace). `members` = dicts
    {url, title, da, pa, adjusted_pa, rank_score, rank, selected, note}."""
    ensure_collection_members_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "DELETE FROM collection_members WHERE collection_type = ? AND collection_key = ?",
        (collection_type, collection_key),
    )
    conn.executemany(
        """
        INSERT INTO collection_members
            (collection_type, collection_key, url_normalized, title, da, pa,
             adjusted_pa, rank_score, rank, selected, note, image_url,
             traffic, traffic_pct, file_seq, model_version, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [(collection_type, collection_key, normalize_url(m["url"]) or m["url"], m.get("title"),
          m.get("da"), m.get("pa"), m.get("adjusted_pa"), m.get("rank_score"),
          m.get("rank"), 1 if m.get("selected") else 0, m.get("note"),
          m.get("image_url"), m.get("traffic"), m.get("traffic_pct"), m.get("file_seq"),
          model_version, now) for m in members],
    )
    conn.commit()
    return len(members)


def clear_members(conn, collection_type, collection_key) -> int:
    """Wipe ONE collection's members (the junction only) — for a botched harvest the
    curator wants to throw away. Returns the row count deleted."""
    ensure_collection_members_table(conn)
    cur = conn.execute(
        "DELETE FROM collection_members WHERE collection_type = ? AND collection_key = ?",
        (collection_type, collection_key),
    )
    conn.commit()
    return cur.rowcount


def get_collection_top(conn, collection_type, collection_key, limit=50,
                       selected_only=False) -> list:
    """Top members of a collection, LEFT-JOINed to master_recipes on url_normalized
    (the url work): an already-ingested recipe shows its REAL name/grade/thumbnail
    and `ingested=True`; a discovered-but-not-yet-fetched one shows the SERP title +
    `ingested=False`. The join is the canonical-URL link between ledger and content.

    `selected_only` — return only the kept top-N (the curated winners), the way the
    dishes top-recipes list shows winners only; the rest are ranked-but-not-kept."""
    ensure_collection_members_table(conn)
    sel_clause = " AND cm.selected = 1" if selected_only else ""
    rows = conn.execute(
        f"""
        SELECT cm.url_normalized, cm.title, cm.da, cm.pa, cm.adjusted_pa,
               cm.rank_score, cm.rank, cm.selected, cm.traffic, cm.traffic_pct,
               m.recipe_id,
               json_extract(m.data, '$.name'),
               json_extract(m.data, '$._master.exceptionalism.grade'),
               -- NULLIF: a failed co-opt can leave previewImage='' (29 corpus rows);
               -- COALESCE treats '' as a real value and returns a blank <img src> →
               -- a "missing image". NULLIF('','')→NULL so it falls through to the
               -- recipe's own image / the harvested og:image, which DO load.
               COALESCE(NULLIF(json_extract(m.data, '$._source.previewImage'), ''),
                        NULLIF(json_extract(m.data, '$.image[0]'), ''),
                        NULLIF(cm.image_url, ''))
        FROM collection_members cm
        LEFT JOIN master_recipes m
          ON m.url_normalized = cm.url_normalized AND m.user_id = 0
        WHERE cm.collection_type = ? AND cm.collection_key = ?{sel_clause}
        ORDER BY cm.rank IS NULL, cm.rank, cm.pa DESC
        LIMIT ?
        """,
        (collection_type, collection_key, limit),
    ).fetchall()
    cols = ["url", "ledger_title", "da", "pa", "adjusted_pa", "rank_score", "rank",
            "selected", "traffic", "traffic_pct", "recipe_id", "name", "grade", "preview_image"]
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["ingested"] = d["recipe_id"] is not None
        d["title"] = d.get("name") or d.get("ledger_title") or d["url"]
        out.append(d)
    return out


def get_memberships_for_url(conn, url) -> list:
    """All collections a recipe belongs to (for the recipe form's 'Member of' chips)."""
    ensure_collection_members_table(conn)
    nu = normalize_url(url) or url
    rows = conn.execute(
        "SELECT collection_type, collection_key, rank, selected FROM collection_members "
        "WHERE url_normalized = ? ORDER BY collection_type, rank",
        (nu,),
    ).fetchall()
    return [{"type": r[0], "key": r[1], "rank": r[2], "selected": bool(r[3])} for r in rows]


# --------------------------------------------------------------------------- #
# Publisher harvest — discover + Moz-score + rank by PA
# --------------------------------------------------------------------------- #
def _host(u):
    return (urlparse(u).hostname or "").replace("www.", "").lower() if u else ""


def _first_seg(u):
    segs = [s for s in urlparse(u).path.lower().strip("/").split("/") if s]
    return segs[0] if segs else ""


def _under_path(u, seg):
    """True if url's path is `/<seg>/<slug>…` (a leaf under the recipe path)."""
    segs = [s for s in urlparse(u).path.lower().strip("/").split("/") if s]
    return len(segs) >= 2 and segs[0] == (seg or "").lower()


# WordPress/CMS archive + taxonomy + feed URLs that are NEVER an individual recipe
# (a bare `site:` query surfaces these heavily — aglaiakremezi.com returned mostly
# /tag/, /category/, /page/N/, /links/). Dropping them BEFORE the recipe-check fetch
# saves credits + time. Conservative: only patterns a recipe post never has.
# Segment-anchored — each alternative must be a WHOLE path segment ((/|$)), so a
# recipe slug that merely CONTAINS one of these words (e.g. /shopska-salad,
# /about-greek-food) is NOT caught. Only e.g. /shop/, /about, /category/… match.
_ARCHIVE_RE = re.compile(
    r"/(tag|tags|category|categories|author|page|archives?|topics?|search|feed|"
    r"comments?|wp-json|wp-admin|wp-content|wp-includes|sitemap|amp|"
    # utility / commerce / boilerplate pages — never a recipe
    r"about|about-us|contact|contact-us|privacy|privacy-policy|terms|disclaimer|"
    r"disclosure|faq|shop|store|cart|checkout|account|my-account|login|register|"
    r"subscribe|newsletter|press|media-kit|advertise|sitemap\.xml)(/|$)"
    r"|/page/\d+", re.IGNORECASE)

# Date-only archive: /2023, /2023/04, /2023/04/05 with NO slug after — a WP date
# archive, not a post. A dated POST like /2023/04/tzatziki keeps (has the slug).
_DATE_ARCHIVE_RE = re.compile(r"^\d{4}(/\d{1,2}){0,2}$")


def _looks_like_archive(url: str) -> bool:
    """True for taxonomy/pagination/feed/admin/utility URLs, date-only archives, and
    the bare site root — never a recipe page. Pre-filter before the recipe check."""
    path = urlparse(url).path.strip("/")
    if not path:  # bare host root / homepage
        return True
    if _DATE_ARCHIVE_RE.match(path):  # /2023, /2023/04 — date archive, no slug
        return True
    return bool(_ARCHIVE_RE.search(urlparse(url).path))


# The URL-text pre-filter (food/recipe-word skip) + its self-learning two-list
# vocabulary now live in the shared `url_word_lists` module so BOTH this harvest and
# build_query_batch._is_recipe_filter use ONE implementation ([[single canonical path]]).
# Re-exported here for back-compat with existing callers/tests.


def _serp_links(query, want=50) -> list:
    """Raw organic links for a verbatim Google query (filter=0, paginated). Unfiltered
    — callers filter. Returns [(link, title), …]. Delegates to the provider-agnostic
    serp_search chokepoint (default SerpApi = unchanged behavior; Scale SERP when the
    `serp_provider` config flips). No gl/hl here — preserves prior behavior."""
    from input.pipeline.serp_search import serp_search
    pages = (want + 20) // 10 + 1
    return [(r["link"], r["title"]) for r in serp_search(query, pages=pages, want=want)]


def detect_recipe_path(domain) -> str:
    """Find the publisher's recipe URL path segment — DON'T assume `/recipes`. Probe
    common candidates (`recipes`, `recipe`, `recipe-finder`…); whichever has the most
    `site:domain/<cand>` leaf hits wins. If none, broad-scan `site:domain` and infer
    the dominant first segment among leaf (slug) URLs, preferring a recipe-ish one.
    Returns the segment (e.g. 'recipes') or '' if undetectable."""
    if not _SERPAPI_KEY:
        return "recipes"
    best, best_n = "", 0
    for cand in ("recipes", "recipe", "recipe-finder", "cooking"):
        hits = [l for l, _ in _serp_links(f"site:{domain}/{cand}", want=12)
                if _host(l) == domain and _under_path(l, cand)]
        if len(hits) > best_n:
            best, best_n = cand, len(hits)
    if best_n >= 3:
        return best
    # Fallback: infer from a broad scan of the domain's leaf URLs.
    from collections import Counter
    segs = Counter()
    for l, _ in _serp_links(f"site:{domain}", want=60):
        if _host(l) == domain:
            s = _first_seg(l)
            p = [x for x in urlparse(l).path.strip("/").split("/") if x]
            if s and len(p) >= 2:
                segs[s] += 1
    for seg, _ in segs.most_common():
        if "recipe" in seg:
            return seg
    return segs.most_common(1)[0][0] if segs else ""


def discover_publisher_recipe_urls(domain, want=80, recipe_path="recipes") -> list:
    """Recipe URLs on a publisher via SerpAPI `site:domain/<recipe_path>` + filter=0
    (precise — path-prefix is a valid Google operator; for Milk Street it found 45
    real recipe pages vs 0 for the term form, measured 2026-06-16). `recipe_path` is
    DETECTED per publisher (detect_recipe_path), never assumed. Canonical host only."""
    if not recipe_path:
        return []
    out, seen = [], set()
    for link, title in _serp_links(f"site:{domain}/{recipe_path}", want=want + 20):
        if _host(link) == domain and link not in seen and _under_path(link, recipe_path):
            seen.add(link)
            out.append((link, title))
        if len(out) >= want:
            break
    return out


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url|:url)?|twitter:image)["\']'
    r'[^>]*\bcontent=["\']([^"\']+)["\']', re.I)
_OG_IMAGE_RE2 = re.compile(
    r'<meta[^>]+\bcontent=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']'
    r'(?:og:image(?::secure_url|:url)?|twitter:image)["\']', re.I)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\']'
    r'[^>]*\bcontent=["\']([^"\']+)["\']', re.I)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_FETCH_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _title_from_url(url):
    """Cheap human title from a recipe URL slug, when the source has no title (e.g. a
    SEMrush subdirectory export's empty title column). /recipe/21014/good-old-fashioned-
    pancakes/ -> 'Good Old Fashioned Pancakes'. Best-effort fallback only."""
    segs = [s for s in urlparse(url).path.strip("/").split("/") if s]
    segs = [s for s in segs if not s.isdigit() and s.lower() not in ("recipe", "recipes")]
    last = (segs[-1] if segs else "").split(".")[0]   # drop detail.aspx / .html
    return last.replace("-", " ").replace("_", " ").strip().title()


def _fetch_og_meta(url, timeout=8):
    """Best-effort (og:image, title) for a recipe URL — thumbnail + display title for a
    DISCOVERED (not-yet-ingested) member. Reads only the <head>, follows redirects (the
    http→https 301s in a subdir export), never raises. title falls back to <title> then
    the URL slug. Returns (image_url|None, title|'')."""
    # Delegate to the CANONICAL extractor (extract_og_meta) — same path as
    # extract/coopt-backfill, no bespoke regex. But fetch DIRECT only
    # (try_wayback=False): a thumbnail is cosmetic and NOT worth hammering Wayback
    # — on anti-bot sites the per-URL Wayback hits get us rate-limited (the
    # thekitchn cascade). Blocked site → no thumbnail, no Wayback load.
    img, title = None, ""
    try:
        from to_markdown.html_to_markdown import fetch_with_full_fallback, extract_og_meta
        from bs4 import BeautifulSoup
        resp, _meta = fetch_with_full_fallback(url, timeout=timeout, try_wayback=False)
        if resp is not None and getattr(resp, "status_code", 0) == 200:
            meta = extract_og_meta(BeautifulSoup(resp.text, "html.parser"), getattr(resp, "url", url))
            img = (meta.get("image") or "").strip() or None
            title = (meta.get("title") or "").strip()
    except Exception:
        img, title = None, ""
    if not title:
        title = _title_from_url(url)
    return img, title


def _clean_title_for_query(title: str) -> str:
    """Strip the ' | Publisher' suffix + parentheticals so a Google-Images query is
    just the dish name (e.g. 'Homemade Ricotta Cheese Recipe (Only 2 Ingredients!) |
    The Kitchn' → 'Homemade Ricotta Cheese Recipe')."""
    import re
    t = (title or "").split("|")[0]
    t = re.sub(r"\([^)]*\)", "", t)
    return " ".join(t.split())


def _serp_image_for(domain, title):
    """Fetch-free thumbnail fallback for sites we can't fetch (anti-bot/blocked):
    Google already has the image, so look it up via the SERP vendor. A
    `site:{domain} {title}` query reliably returns the publisher's OWN hero at rank
    1 (verified on thekitchn). ~1 credit; only called when the direct og:image is
    empty. Returns an image URL or None."""
    from input.pipeline import serp_search
    if not serp_search.has_key():
        return None
    q = _clean_title_for_query(title)
    if not q:
        return None
    try:
        hits = serp_search.serp_image_search(f"site:{domain} {q}", want=1)
    except Exception:
        return None
    return hits[0]["image"] if hits else None


def _input_dir():
    """<project>/input — a fallback search location (where the tracked sample exports
    live + where the old Scan-inbox MOVED files). No longer the only/required place."""
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def semrush_inbox_dir():
    """The admin-configured folder SEMrush exports are saved to (read DIRECTLY — no
    copy-into-input step). `system_config.semrush_inbox_dir` if set, else the OS
    Downloads folder. This is the DEFAULT import location for the backlinks harvest."""
    import os
    try:
        from input.pipeline import system_config as _cfg
        d = (_cfg.get_setting("semrush_inbox_dir", "") or "").strip()
    except Exception:
        d = ""
    return d or os.path.join(os.path.expanduser("~"), "Downloads")


def backlinks_search_dirs(extra=None):
    """Ordered, deduped list of EXISTING folders to look for a domain's SEMrush export
    in — searched directly, newest match wins. Priority:
      1. `extra` — a per-domain override folder (domains.backlinks_dir), if set
      2. the configured inbox folder (semrush_inbox_dir → Downloads default)
      3. <project>/input — fallback for the tracked sample exports
    So an admin can just leave the export in Downloads (or point a domain at any folder)
    and the harvest reads it in place — no need to move it into input/ first.

    A per-domain override may be a FILE path (the explicit "use THIS file" case). Its
    exact file is returned up-front by backlinks_file_path; HERE we contribute the file's
    PARENT FOLDER to the search set so that if the exact file is STALE (re-downloaded with
    a new timestamp, or moved), a fresh matching export in that same folder is still found
    (newest match wins) instead of the override silently failing."""
    import os
    cand = []
    if extra:
        for e in (extra if isinstance(extra, (list, tuple)) else [extra]):
            e = (e or "").strip().strip('"').strip("'")
            if not e:
                continue
            ep = os.path.expanduser(e)
            # File path (existing OR stale-but-.xlsx) → search its folder, not the file.
            cand.append(os.path.dirname(ep) if (ep.lower().endswith(".xlsx") or os.path.isfile(ep)) else ep)
    cand.append(semrush_inbox_dir())
    cand.append(_input_dir())
    seen, out = set(), []
    for d in cand:
        d = (d or "").strip()
        if not d:
            continue
        full = os.path.realpath(os.path.expanduser(d))
        if full in seen or not os.path.isdir(full):
            continue
        seen.add(full)
        out.append(full)
    return out


def backlinks_file_path(domain, extra_dir=None):
    """Path to a publisher's SEMrush page export, searched DIRECTLY across
    `backlinks_search_dirs` (configured inbox / Downloads / input / per-domain override),
    or None. Reads in place — nothing is copied into input/.

    Tolerant of BOTH SEMrush page-export shapes (the reader auto-detects which):
      - BACKLINKS pages:  {domain}-backlinks-pages.xlsx / {domain}_recipe-backlinks_pages.xlsx
      - organic TOP-PAGES: {domain}_recipe-organic.PagesV3-us-…xlsx (traffic-ranked, and for
        a /recipe subfolder a clean current list of just recipe URLs)
    So we match `{domain}*[Pp]ages*.xlsx` — the `*` after the domain absorbs the subpath +
    export type; the trailing `*` tolerates the browser's de-dup suffix (`…(1).xlsx`).
    Across ALL search dirs, the MOST RECENTLY MODIFIED match wins (so a fresh Top-Pages
    export supersedes an old backlinks one).

    `extra_dir` (domains.backlinks_dir) may be EITHER a folder OR an EXACT FILE PATH — if it
    points at an existing file (or a folder + filename), that file is used DIRECTLY, no
    auto-detect. This is the explicit per-domain "use THIS file" override."""
    import os, glob
    # Explicit file override: a full path to an .xlsx (handles quotes + ~).
    if extra_dir:
        ex = os.path.expanduser(str(extra_dir).strip().strip('"').strip("'"))
        if os.path.isfile(ex):
            return ex
        # also allow a folder set elsewhere + a bare filename pasted here
        if ex.lower().endswith(".xlsx"):
            for d in backlinks_search_dirs(None):
                cand = os.path.join(d, os.path.basename(ex))
                if os.path.isfile(cand):
                    return cand
    # Filename match patterns are CONFIG, not code (no-data-in-code) — so a SEMrush rename
    # is a system_config edit, not a code change. `{domain}` is substituted; the reader
    # then auto-detects the format from the COLUMNS, so patterns can be broad.
    hits = []
    for d in backlinks_search_dirs(extra_dir):
        for pat in semrush_export_patterns():
            hits += glob.glob(os.path.join(d, pat.replace("{domain}", domain)))
    hits = list({os.path.realpath(h): h for h in hits}.values())   # dedup across patterns
    return max(hits, key=os.path.getmtime) if hits else None


def semrush_export_patterns() -> list:
    """Glob patterns (with `{domain}`) used to FIND a publisher's SEMrush export — editable
    via system_config `semrush_export_patterns` so a SEMrush filename change is a config
    edit, not code. Default catches both shapes (backlinks_pages + organic.PagesV3, both
    contain 'pages'/'Pages')."""
    # Three patterns so we match BOTH the plain export ({domain} at the start) AND SEMrush's
    # URL-FORM export, which prefixes the name with the full URL (e.g.
    # `https___www.177milkstreet.com_-organic.PagesV3-…xlsx`) → the domain sits mid-filename,
    # preceded by `.` (www.) or `_`. The separator before {domain} (start / `.` / `_`) anchors
    # it so a SUBSTRING domain can't false-match (e.g. `mango.com` ✗ `oliveandmango.com`).
    default = ["{domain}*[Pp]ages*.xlsx",
               "*.{domain}*[Pp]ages*.xlsx",
               "*_{domain}*[Pp]ages*.xlsx"]
    try:
        from input.pipeline import system_config as _cfg
        val = _cfg.get_setting("semrush_export_patterns", default)
        if isinstance(val, str):
            val = [p.strip() for p in val.splitlines() if p.strip()]
        return [p for p in (val or default) if "{domain}" in p] or default
    except Exception:
        return default


def expected_export_name(domain: str) -> str:
    """Short example filename of the SEMrush export we look for. We harvest the
    Organic TOP-PAGES export now (traffic-ranked) — NOT the old backlinks-pages
    report — so the hint reflects that. Display-only."""
    return f"{domain}-organic.PagesV3-<region>-<date>.xlsx"


def export_not_found_message(domain: str, searched, override: str = None) -> str:
    """One canonical 'no SEMrush export found' message, so all callers say the same
    (Top-Pages, not backlinks) and an explicit per-domain override is honored in the
    error too. `searched` = folders checked; `override` = domains.backlinks_dir if set."""
    where = ", ".join(searched) or "(no existing folder configured)"
    examples = (f"Expected a SEMrush Organic Top-Pages export — e.g. "
                f"'{expected_export_name(domain)}', a subfolder export "
                f"'{domain}_recipe-organic.PagesV3-…xlsx', or the URL-form "
                f"'https___{domain}…-organic.PagesV3-…xlsx'.")
    if override:
        return (f"No SEMrush export found for {domain}: the per-domain override you set "
                f"({override}) didn't resolve to an export file on THIS (server) machine. "
                f"An entered path/filename takes precedence over the default — fix it on "
                f"the domain form (make sure the file is saved on the server, not another "
                f"machine), or clear it to use the newest match in: {where}. {examples}")
    return (f"No SEMrush export found for {domain}. {examples} "
            f"Looked in: {where}. Save the export there — no need to move it into input/.")


def export_prefix(path):
    """The `{domain}[_subpath]` prefix of a SEMrush page-export filename — the part before
    the export-type marker (`-backlinks` OR `-organic`). e.g.
    `allrecipes.com_recipe-backlinks_pages.xlsx` → `allrecipes.com_recipe`;
    `bostonchefs.com_recipe-organic.PagesV3-us-….xlsx` → `bostonchefs.com_recipe`.
    Returns '' if the name isn't an export."""
    import os
    base = os.path.basename(path)
    low = base.lower()
    cuts = [low.find(m) for m in ("-backlinks", "-organic") if low.find(m) > 0]
    return base[:min(cuts)] if cuts else ""


def scan_export_inbox(dirs):
    """Find SEMrush page-export files (backlinks-pages OR organic Top-Pages) sitting in any
    of `dirs` (the watched inbox — typically ~/Downloads). Returns [{"path", "prefix"}]
    newest-first. Pure discovery — the caller matches each prefix to a known domain and
    routes it (intake_export_file)."""
    import os, glob
    seen, out = set(), []
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for pat in ("*-backlinks*pages*.xlsx", "*-organic.[Pp]ages*.xlsx"):
            for p in glob.glob(os.path.join(d, pat)):
                rp = os.path.realpath(p)
                if rp in seen:
                    continue
                seen.add(rp)
                out.append({"path": p, "prefix": export_prefix(p)})
    out.sort(key=lambda r: -os.path.getmtime(r["path"]))
    return out


def intake_export_file(path):
    """Move a SEMrush export from the watched inbox into <project>/input/ so the
    backlinks pipeline (backlinks_file_path) finds it. Returns the new path. A
    same-named file already in input/ is overwritten (the freshest export wins)."""
    import os, shutil
    dest_dir = _input_dir()
    dest = os.path.join(dest_dir, os.path.basename(path))
    if os.path.realpath(path) == os.path.realpath(dest):
        return dest                      # already in input/ — nothing to move
    shutil.move(path, dest)              # replaces an existing same-named file
    return dest


def _recipe_path_key(url):
    """A dedup key that collapses a publisher's URL ALIASES for the same recipe — the
    numeric-id form (/recipe/21014/slug/), the bare slug (/recipe/slug/), and the legacy
    /detail.aspx variant all map to one key. Drops numeric path segments + trailing
    detail/index boilerplate; keeps the rest of the path so genuinely different recipes
    that merely share a last segment (/breakfast/pancakes vs /brunch/pancakes) stay
    distinct. Used by the backlinks-file reader, whose exports are full of these aliases."""
    segs = [s for s in urlparse(url).path.strip("/").split("/") if s and not s.isdigit()]
    while segs and segs[-1].split(".")[0].lower() in ("detail", "index", "amp", ""):
        segs.pop()
    return (root_domain(url), "/".join(s.lower() for s in segs))


def _read_backlinks_file(domain, want, extra_dir=None):
    """Discovery from a local SEMrush page export, ranked by REFERRING DOMAINS desc
    (distinct linking sites — a robust authority marker, harder to game than raw
    backlink count). Returns [(url, title), …] for the top `want` response-200 content
    URLs in domains order. The SAME downstream pipeline (archive/collection pre-filter,
    recipe check, Moz scoring, rank, keep) then runs exactly as for Google results."""
    import os, openpyxl
    path = backlinks_file_path(domain, extra_dir=extra_dir)
    if not path:
        searched = backlinks_search_dirs(extra_dir)
        raise FileNotFoundError(export_not_found_message(domain, searched, override=extra_dir))
    ws = openpyxl.load_workbook(path, read_only=True).active
    it = ws.iter_rows(values_only=True)
    header = [str(h or "") for h in next(it)]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None
    ui = col("Source url", "Page URL", "Target url", "URL")
    if ui is None:
        raise ValueError(f"Unexpected SEMrush columns (no URL column): {header}")
    ti = col("Source title", "Title", "Page title")
    ci = col("Response code", "Status code")
    # SEMrush ships two page-export shapes; auto-detect by the ranking column present:
    #   - BACKLINKS pages export → rank by referring DOMAINS (link authority).
    #   - organic TOP-PAGES export (…-organic.PagesV3…) → rank by TRAFFIC. For a subfolder
    #     filter (…/recipe) this is a CLEAN, CURRENT list of just recipe URLs (no
    #     /restaurant/ noise) and the traffic order is its own relevance signal.
    di = col("Domains", "Referring Domains", "Referring domains")
    tri = col("Traffic")
    if di is not None:
        rank_idx, rank_label = di, "referring domains"
    elif tri is not None:
        rank_idx, rank_label = tri, "traffic"
    else:
        raise ValueError(f"Unexpected SEMrush columns (need Domains or Traffic): {header}")
    # DEMAND-SIDE capture (Top-Pages export only): the per-URL Top Keyword is a traffic-
    # validated, self-classifying dish name — stash it while we're here (capture-now). See
    # input.pipeline.dish_keywords.
    ki = col("Top Keyword", "Keyword")
    ii = col("Primary Intent", "Intent")
    aei = col("Answer Engines")
    tpi = col("Traffic (%)", "Traffic %")
    rows, kw_rows = [], []
    for seq, r in enumerate(it, 1):   # seq = 1-based position in the export AS DELIVERED
        url = str(r[ui] or "").strip()
        if not url:
            continue
        # Keep 2xx AND 3xx: a 301 here is just the http://… form of a real recipe
        # (pre-HTTPS backlinks, common in a subdirectory export) — the recipe check and
        # Moz follow the redirect to the canonical page. Only drop dead 4xx/5xx.
        code = str(r[ci]).strip() if ci is not None else ""
        if code[:1] in ("4", "5"):
            continue
        try:
            rank = float(r[rank_idx] or 0)
        except (TypeError, ValueError):
            rank = 0.0
        # Per-page TRAFFIC numbers — present on a Top-Pages export, absent (None) on a pure
        # backlinks export. Read independently of `rank` (which may be referring-domains).
        # Stored on the member as the meaningful PA tiebreaker + kept for future weighting.
        try:
            traffic = float(r[tri]) if tri is not None and r[tri] not in (None, "") else None
        except (TypeError, ValueError):
            traffic = None
        try:
            tpct = float(r[tpi]) if tpi is not None and r[tpi] not in (None, "") else None
        except (TypeError, ValueError):
            tpct = None
        rows.append((url, str(r[ti] if ti is not None else "") or "", rank, traffic, tpct, seq))
        if ki is not None:
            kw = str(r[ki] or "").strip()
            if kw:
                kw_rows.append({"url": url, "keyword": kw,
                                "traffic": traffic, "traffic_pct": tpct,
                                "intent": str(r[ii] or "").strip() if ii is not None else "",
                                "answer_engines": str(r[aei] or "").strip() if aei is not None else ""})
    if kw_rows:
        try:
            from input.pipeline import dish_keywords
            n = dish_keywords.capture(kw_rows, domain)
            print(f"  [dish-keywords] captured {n} demand keywords from {os.path.basename(path)}")
        except Exception as e:
            print(f"  [dish-keywords] capture skipped ({type(e).__name__}: {e})")
    rows.sort(key=lambda x: -x[2])   # rank desc (domains or traffic)
    out, seen, meta = [], set(), {}
    for url, title, _r, traffic, tpct, seq in rows:
        key = _recipe_path_key(url)            # collapse id / slug / detail.aspx aliases
        if key not in seen:
            seen.add(key)
            out.append((url, title))           # keep the highest-ranked variant
            meta[url] = {"traffic": traffic, "traffic_pct": tpct, "file_seq": seq}
        if len(out) >= want:
            break
    print(f"  [harvest] SEMrush file {os.path.basename(path)}: {len(out)} URLs (by {rank_label})")
    return out, meta


def harvest_publisher_top(domain, keep=10, discover_n=80, recipe_path=None,
                          query=None, check_recipe=True, source="serp", records=None,
                          unblocker=False, should_cancel=None, backlinks_dir=None,
                          exclude_words=None, score_only=False,
                          keyword_prescreen=None) -> dict:
    """Discover a publisher's recipe URLs, (optionally) VERIFY each is a real recipe,
    Moz-score the survivors, rank by PA, mark the top `keep` selected.

    `source` — 'serp' (Google, default) or 'backlinks_file' (local SEMrush export,
    input/{domain}-backlinks-pages.xlsx, ranked by referring domains). `records` is the
    file-source analog of SERP `discover_n` — how many top rows to pull from the file.
    BOTH sources feed the IDENTICAL downstream pipeline (archive/collection filter →
    recipe check → Moz → rank → keep), so a file URL is treated exactly like a Google one.

    `query` — a VERBATIM Google query (e.g. 'site:bostonglobe.com recipe'), run as-is
    via SerpAPI; overrides path-based DISCOVERY (how candidates are FOUND). The curator
    owns the Google syntax (same as dish SERP queries); the code just executes it. Needed
    for publishers whose recipes aren't under a clean path segment (Boston Globe lives
    under /YYYY/.../slug, so `site:domain/recipes` finds nothing — the term form does).

    `recipe_path` — the publisher's recipe URL path segment ('recipes'), a source-agnostic
    KEEP SCOPE applied AFTER discovery to EVERY source (file / verbatim-query / path). It
    is orthogonal to `query`: `query` decides what's discovered, `recipe_path` decides
    what's kept. Blank = no scoping. Auto-detected (detect_recipe_path) only for the pure
    path-discovery source when left blank.

    `check_recipe` — fetch each candidate and keep only real recipes via the CANONICAL
    dish-batch filter (`_is_recipe_filter`: schema.org/Recipe JSON-LD, else phrase
    score). Drops the /recipes/ index, 'best-...-2025' listicles, /recipe-database/,
    and section pages that merely contain the word 'recipe'.

    Returns {members, discovered, recipe_pass, scored, recipe_path, query}. This
    function builds the discovery/ranking LEDGER only; the actual content extraction
    into master_recipes is done by the CALLER — see the "ledger -> master" block in
    save_recipe_api._handle_publisher_refresh_job, which extracts the selected
    winners (with backfill) right after persisting these members. (So a publisher
    refresh DOES populate master_recipes; this library fn just doesn't.)"""
    # SCORE-ONLY mode (anti-bot / expensive publishers, docs/score-only-curation.md):
    # rank by Moz (URL-only, zero renders) WITHOUT the per-candidate fetch-verify, and
    # mark NO winners — the human selects which to process from the scored list. So force
    # the verify OFF here regardless of what the caller passed.
    if score_only:
        check_recipe = False
    # Keyword pre-screen: explicit arg wins; else fall back to the global system_config
    # default (off). One cheap Haiku pass drops confident non-recipes before the fetch +
    # whole-page translate on mixed/non-English publishers. See docs/keyword-prescreen.md.
    if keyword_prescreen is None:
        try:
            from input.pipeline.system_config import get_setting as _gs
            keyword_prescreen = bool(_gs("keyword_prescreen_default", False))
        except Exception:
            keyword_prescreen = False
    # Curator-set domain language (authoritative for the recipe-filter's scoring language;
    # '' = domain context but unspecified → the filter defaults to the instance base language).
    # A publisher harvest ALWAYS has a domain, so we pass a string (never None — None means
    # "no domain context", which routes the filter to per-page detection for the dish batch).
    domain_lang = ""
    try:
        from input.pipeline import domains_lib
        from input.pipeline.db import connect as _connect
        with _connect() as _c:
            _drow = domains_lib.get_domain(_c, domain)
        domain_lang = (_drow or {}).get("language") or ""
    except Exception:
        domain_lang = ""
    # file_meta: {url -> {traffic, traffic_pct, file_seq}} from a SEMrush export; {} for the
    # SERP/path sources (no per-page traffic available without the SEMrush API — see scoping
    # in docs/recipe-candidate-pipeline.md). Stamped onto members below; traffic tiebreaks.
    # Normalize the (optional) recipe-path SCOPE to a single leading segment
    # ('/recipes/' | 'recipes' → 'recipes'; single-prefix for now). This is a
    # source-agnostic KEEP filter (the publisher fact "recipes live under /<path>"),
    # NOT a discovery mechanism — DISCOVERY is chosen by `source`/`query` below, SCOPE
    # is applied uniformly after. Kept separate so `serp_query` is discovery-only.
    recipe_path = (recipe_path or "").strip().strip("/").split("/")[0] or None

    file_meta = {}
    if source == "backlinks_file":
        found, file_meta = _read_backlinks_file(domain, want=int(records or discover_n or 100),
                                                extra_dir=backlinks_dir)
    elif query:
        target = root_domain("https://" + domain)
        found, seen = [], set()
        for link, title in _serp_links(query, want=discover_n + 20):
            if link in seen:
                continue
            if root_domain(link) == target:   # honor the query's site: scope; safety net vs strays
                seen.add(link)
                found.append((link, title))
            if len(found) >= discover_n:
                break
    else:
        if not recipe_path:
            recipe_path = detect_recipe_path(domain)   # convenience auto-detect for the path source
        found = discover_publisher_recipe_urls(domain, want=discover_n, recipe_path=recipe_path)
    used_path = recipe_path

    # SOURCE-AGNOSTIC PATH SCOPE: if the publisher's recipes live under a known URL path,
    # keep only candidates under it — a hard pre-fetch filter applied to EVERY discovery
    # source: file export rows (previously UNSCOPED — the missing counterpart), verbatim-
    # query hits (previously host-checked only), and the path-discovery list (idempotent
    # there — discover_publisher_recipe_urls already filtered). Optional: blank recipe_path
    # = no scoping, for publishers with no clean prefix (e.g. Boston Globe /YYYY/.../slug —
    # use a verbatim query instead). Drops off-path clutter (/gallery, /video, section
    # indexes) before any fetch spend.
    if recipe_path:
        n_before = len(found)
        found = [(l, t) for l, t in found if _under_path(l, recipe_path)]
        if len(found) < n_before:
            print(f"  [harvest] path-scoped to /{recipe_path}/ — dropped "
                  f"{n_before - len(found)} off-path URL(s), {len(found)} under path")

    # Cheap pre-filter: drop archive/taxonomy/feed URLs (never a recipe) + collection/
    # listicle TITLES ("30 Greek Recipes", "10 Dinners") BEFORE the fetching recipe
    # check — saves credits + time (a bare `site:` query is mostly these). Title-based,
    # so it runs even when check_recipe is OFF (trusted/paywalled publishers): those
    # skip the fetch-verify but must still shed index/roundup pages. English-title
    # only here (no fetch ⇒ no lang/translation); non-English collection titles are
    # caught downstream by _is_recipe_filter when check_recipe is ON.
    from intake.build_query_batch import _looks_like_recipe_collection
    n_raw = len(found)
    found = [(l, t) for l, t in found
             if not _looks_like_archive(l) and not _looks_like_recipe_collection(t)]
    if len(found) < n_raw:
        print(f"  [harvest] pre-filtered {n_raw - len(found)} archive/taxonomy/collection URLs "
              f"({len(found)} candidates remain)")

    # Recipe check — reuse the dish batch's filter so "is this a recipe" is decided
    # ONE way everywhere ([[single-path]]): JSON-LD Recipe → keep; else the free per-language
    # phrase scoring + structural is-recipe gate (ingredients + method). The per-domain
    # food-word URL pre-filter was removed (2026-06-26) — the structural gate makes the keep
    # call reliably after a now-cheap fetch, so the food-word skip was redundant + useless on
    # foreign slugs. exclude_words (the curator's own taxonomy) is still honored. When
    # check_recipe is OFF (trusted/paywalled — no fetch), exclude_words is applied inline.
    recipe_pass = found
    if check_recipe and found:
        from intake.build_query_batch import _is_recipe_filter
        kept, _dropped = _is_recipe_filter(
            [{"url": l, "title": t} for l, t in found],
            capture_source="domain_harvest",
            capture_provenance={"domain": domain, "discover_source": source},
            unblocker=unblocker,   # flagged anti-bot publisher → live fetch via the paid unblocker
            exclude_words=exclude_words,
            keyword_prescreen=keyword_prescreen, prescreen_domain=domain,
            domain_lang=domain_lang,
            should_cancel=should_cancel)
        recipe_pass = [(e["url"], e.get("title") or "") for e in kept]
        # Auto-learn the JS-rendered hint: if any kept recipe was only recoverable
        # via a full-browser render escalation, flag the domain so the form shows it
        # (and future runs can fetch render-first). Idempotent + best-effort.
        if any(e.get("_render_escalated") for e in kept):
            try:
                from input.pipeline import domains_lib
                domains_lib.mark_render_required(domain)
                print(f"  [harvest] learned render_required for {domain} "
                      f"(a recipe was only recoverable with full-browser render)")
            except Exception:
                pass
    elif exclude_words and found:
        from input.pipeline.url_word_lists import url_excluded_by_domain
        n_pre = len(found)
        recipe_pass = [(l, t) for l, t in found
                       if not url_excluded_by_domain(l, exclude_words)]
        if len(recipe_pass) < n_pre:
            print(f"  [harvest] exclude-words dropped {n_pre - len(recipe_pass)} "
                  f"URLs (no fetch-verify on this publisher)")

    scored = []
    n_rp = len(recipe_pass)
    print(f"  [harvest] Moz scoring {n_rp} recipe candidate(s)…")
    for i, (url, title) in enumerate(recipe_pass, 1):
        if should_cancel and should_cancel():
            from input.pipeline.jobs import JobCancelled
            raise JobCancelled("cancelled during Moz scoring")
        s = score_url_via_moz(url)
        if s and s.get("page_authority"):
            pa, da = s.get("page_authority"), s.get("domain_authority")
            _fm = file_meta.get(url) or {}
            scored.append({"url": url, "title": title,
                           "da": float(da), "pa": float(pa),
                           "traffic": _fm.get("traffic"),
                           "traffic_pct": _fm.get("traffic_pct"),
                           "file_seq": _fm.get("file_seq")})
            # Per-URL Moz line, mirroring the dish batch's _moz_score log. No OU —
            # publishers have no per-publisher fit; within-publisher rank IS pa.
            _fp = lambda v: ("?" if v is None else f"{v:>3}")
            print(f"  [{i:>2}/{n_rp}] MOZ-OK   pa={_fp(pa)} da={_fp(da)}  {url}")
        else:
            print(f"  [{i:>2}/{n_rp}] MOZ-FAIL  {url}")
    # System-wide authority score: replace raw-PA ranking with the corpus-grain
    # OU/power blend (one global PA~DA fit, paywall-remapped PA), so rank_score is
    # comparable ACROSS publishers, not just within one. Within a publisher DA is
    # ~constant → the score is monotonic in PA → the kept top-N is unchanged; only
    # the number becomes a "best recipes anywhere" value. Falls back to raw PA when
    # no global fit has been computed yet. See input/pipeline/domain_scoring.py.
    from input.pipeline import domain_scoring
    domain_scoring.score_members(scored)   # stamps adjusted_pa / rank_score / ou / power
    # Order by the authority score DESC, then TRAFFIC DESC as the tiebreaker. Foreign sites
    # often have near-identical PA across all pages (PA saturates) → near-identical rank_score
    # → traffic is what actually distinguishes their hero recipes. None traffic sorts last.
    scored.sort(key=lambda m: ((m.get("rank_score") is None), -(m.get("rank_score") or 0.0),
                               -(m.get("traffic") or 0.0)))
    keep = max(1, int(keep or 10))
    for i, m in enumerate(scored, 1):
        m["rank"] = i
        # score_only: mark NOTHING selected — the human picks winners from the scored list.
        m["selected"] = 0 if score_only else (1 if i <= keep else 0)
    # Ranked authority display WITH OU: the per-URL MOZ-OK line above shows pa/da but not
    # OU, because OU needs the whole-corpus fit that score_members() stamps only after every
    # candidate is scored. Surface pa/da/OU here in rank order; '*' marks the selected top-N.
    _ns = len(scored)
    for m in scored:
        _ou = m.get("ou")
        _ous = "    ?" if _ou is None else f"{_ou:+5.1f}"
        _sel = "*" if m.get("selected") else " "
        print(f"  {_sel}[{m['rank']:>2}/{_ns}] pa={m['pa']:>4.0f} da={m['da']:>4.0f} ou={_ous}  {m['url']}")
    # Translate the SELECTED members' titles to the instance base language for a READABLE
    # ledger: the harvested og:title is in the publisher's language (e.g. Greek 'Μπουγάτσα με
    # κρέμα'), and while the ingested master copy is translated, the discovery list shows this
    # ledger title. Bounded to the top-N; only when the domain's language differs from base.
    from input.pipeline.validators import instance_base_language, normalize_lang
    _base_l = instance_base_language()
    _dom_l = normalize_lang(domain_lang)
    _xlate_titles = bool(_dom_l and _dom_l != _base_l)

    # Thumbnail the SELECTED top-N only (bounded) — captures og:image so discovered
    # members show a picture; ingested members override with the master's real image.
    n_img = n_serp = n_xt = 0
    for m in scored:
        if not m["selected"]:
            continue
        img, title = _fetch_og_meta(m["url"])
        if title and not (m.get("title") or "").strip():
            m["title"] = title    # source had no title (e.g. subdir export) → use page/slug
        # Anti-bot/blocked publishers (thekitchn) return no og:image on a direct
        # fetch → fall back to a FETCH-FREE SERP image lookup (Google has the image).
        # Bounded to the selected top-N + only on an og:image miss, so ~1 credit per
        # blocked pick and zero when the direct grab works. (Uses the ORIGINAL-language
        # title — it matches the publisher's own page best — BEFORE we translate below.)
        if not img:
            img = _serp_image_for(domain, m.get("title") or "")
            if img:
                n_serp += 1
        m["image_url"] = img
        if img:
            n_img += 1
        # Translate the ledger title to base language (display readability). After the image
        # lookup so that used the native title. translate_title is a cheap one-shot.
        if _xlate_titles and (m.get("title") or "").strip():
            try:
                from intake.translate import translate_title
                m["title"] = translate_title(m["title"], _dom_l)
                n_xt += 1
            except Exception as ex:
                print(f"  [harvest] title translate skipped ({type(ex).__name__})")
    extra = f" ({n_serp} via SERP image fallback)" if n_serp else ""
    xt = f" · translated {n_xt} titles → {_base_l}" if n_xt else ""
    print(f"  [harvest] captured {n_img} thumbnails for {keep} selected{extra}{xt}")
    return {"members": scored, "discovered": n_raw, "recipe_pass": len(recipe_pass),
            "scored": len(scored), "recipe_path": used_path, "query": query}
