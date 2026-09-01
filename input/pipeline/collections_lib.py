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


def remove_member(conn, collection_type, collection_key, url_normalized) -> bool:
    """Drop ONE member row (the junction only — never the recipe row it may
    point at). A curator spot-fix for one bad ledger entry; unlike an
    exclusion this is NOT a ban — the next harvest re-ranks the pool and may
    seat the URL again (per-domain candidate filters are the durable ban
    surface, when built)."""
    ensure_collection_members_table(conn)
    cur = conn.execute(
        "DELETE FROM collection_members WHERE collection_type = ? "
        "AND collection_key = ? AND url_normalized = ?",
        (collection_type, collection_key, url_normalized))
    conn.commit()
    return cur.rowcount > 0


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
    Returns the segment (e.g. 'recipes') or '' if undetectable.

    The key check asks the ACTIVE provider (serp_search.has_key), not SERPAPI_KEY —
    that guard predated the Scale SERP switch and would have silently disabled
    detection for everyone the day the (now unused) SerpApi key left .env. Falling
    back to a bare 'recipes' assumption is exactly what this function exists to
    avoid: for Milk Street the DETECTED path found 45 recipe pages vs 0 for the
    assumption, so the fallback WARNS instead of degrading silently."""
    from input.pipeline.serp_search import has_key, active_provider
    if not has_key():
        print(f"  [detect_recipe_path] no SERP key for active provider "
              f"'{active_provider()}' — assuming '/recipes' for {domain} "
              f"(detection skipped; harvest may come back empty)")
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
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def semrush_archive_dir():
    """<project>/input/semrush — the PERMANENT, git-tracked home for SEMrush exports.

    An export is not a throwaway import artifact: it carries per-URL traffic,
    keyword and position data we currently read one column of, and SEMrush will
    not sell the same historical snapshot back to us. So every export we have
    ever taken is kept, and kept somewhere it gets BACKED UP.

    A SUBFOLDER on purpose. `.gitignore` carries `input/*.xlsx`, which matches
    only the root level — exports sitting loose in input/ were silently
    untracked (35 of 55 on 2026-08-07, while the other 20 predated the rule and
    stayed tracked, which is why the gap was invisible). Nesting them one level
    down makes them tracked with no .gitignore change and no exception to
    forget."""
    return os.path.join(_input_dir(), "semrush")


def semrush_inbox_dir():
    """The admin-configured folder SEMrush exports are saved to (read DIRECTLY — no
    copy-into-input step). `system_config.semrush_inbox_dir` if set, else the OS
    Downloads folder. This is the DEFAULT import location for the backlinks harvest."""
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
    # The permanent archive comes BEFORE loose input/ — once an export is
    # filed it is the copy that survives, and the loose root is legacy.
    cand.append(semrush_archive_dir())
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


# ── SEMrush export archiving ────────────────────────────────────────────────
# An export is not a throwaway import artifact. It carries per-URL traffic,
# keywords, positions and history that we read one column of today, and SEMrush
# will not sell the same historical snapshot back to us later. So every export
# the harvest consumes is filed into input/semrush/, which IS backed up.
#
# The two shapes backlinks_file_path tolerates. Chrome's " (1)" duplicate
# suffix is stripped before matching — left in place it defeats both patterns
# and the file is silently skipped instead of archived.
_RE_EXPORT_ORG = re.compile(
    r"^(?P<stem>.+?)-organic\.PagesV3-(?P<db>[a-z]{2})-(?P<snap>\d{8})-(?P<ts>.+?)\.xlsx$", re.I)
_RE_EXPORT_BL = re.compile(r"^(?P<stem>.+?)[-_]backlinks[_-]pages\.xlsx$", re.I)
_RE_DUP_SUFFIX = re.compile(r"\s*\(\d+\)(?=\.xlsx$)", re.I)


def semrush_export_key(filename):
    """(stem, database) for a SEMrush export filename, or None if it isn't one.

    The key is deliberately NOT the domain. A domain legitimately has more than
    one CURRENT export — 101cookbooks has a -gr run and a -us run;
    thepioneerwoman has a backlinks-pages export and an organic one. Keying on
    the domain alone treats those as versions of each other and retires data
    that is not a duplicate.
    """
    fn = _RE_DUP_SUFFIX.sub("", os.path.basename(filename or ""))
    m = _RE_EXPORT_ORG.match(fn)
    if m:
        return m.group("stem").lower(), m.group("db").lower()
    m = _RE_EXPORT_BL.match(fn)
    if m:
        return m.group("stem").lower(), "backlinks"
    return None


def archive_export(path):
    """File a consumed SEMrush export into input/semrush/. Returns the archived
    path, or None if it wasn't an export / couldn't be filed.

    Older versions of the SAME (stem, database) are MOVED to
    input/semrush/_superseded/, never deleted — newest is not always richest.
    Measured 2026-08-07: three real cases had the newer export materially
    smaller than the one it replaced (marthastewart 74KB -> 26KB the same day),
    which is what a re-run with a narrower filter looks like. Losing the wider
    one to an automatic rule is not recoverable; SEMrush will not re-issue it.

    Copies, never moves, from outside the project — the admin's Downloads folder
    is theirs. Fully non-fatal: a harvest must never fail because bookkeeping
    did.
    """
    import shutil
    try:
        if not path or not os.path.isfile(path):
            return None
        key = semrush_export_key(path)
        if not key:
            return None
        archive = semrush_archive_dir()
        fn = os.path.basename(path)
        dest = os.path.join(archive, fn)
        if os.path.realpath(os.path.dirname(path)) == os.path.realpath(archive):
            return path                      # already filed
        os.makedirs(archive, exist_ok=True)
        if not os.path.exists(dest):
            shutil.copy2(path, dest)
            print(f"  [archive] filed {fn} -> input/semrush/")
        # Retire any older file already in the archive under the same key.
        superseded = os.path.join(archive, "_superseded")
        for other in os.listdir(archive):
            if other == fn or not other.lower().endswith(".xlsx"):
                continue
            op = os.path.join(archive, other)
            if not os.path.isfile(op) or semrush_export_key(other) != key:
                continue
            if os.path.getmtime(op) >= os.path.getmtime(dest):
                continue                     # the incoming one is not newer
            os.makedirs(superseded, exist_ok=True)
            tgt = os.path.join(superseded, other)
            if os.path.exists(tgt):
                os.remove(op)
            else:
                shutil.move(op, tgt)
            print(f"  [archive] superseded {other} -> _superseded/")
        return dest
    except Exception as e:
        print(f"  [archive] skipped ({type(e).__name__}: {e})")
        return None


def backlinks_file_path(domain, extra_dir=None):
    """Path to a publisher's SEMrush page export, searched DIRECTLY across
    `backlinks_search_dirs` (configured inbox / Downloads / input / per-domain override),
    or None. Reads in place — nothing is copied into input/.

    Resolves to the organic TOP-PAGES shape ONLY:
      - organic TOP-PAGES: {domain}_recipe-organic.PagesV3-us-…xlsx (traffic-ranked, and for
        a /recipe subfolder a clean current list of just recipe URLs)   ← the live shape
      - BACKLINKS pages ({domain}-backlinks-pages.xlsx) is LEGACY and skipped — see the
        exclusion note below. Reachable only via an explicit per-domain file override.
    We match `{domain}*[Pp]ages*.xlsx` — the `*` after the domain absorbs the subpath +
    export type; the trailing `*` tolerates the browser's de-dup suffix (`…(1).xlsx`) —
    then drop the legacy shape from the result. Across ALL search dirs the MOST RECENTLY
    MODIFIED surviving match wins.

    `extra_dir` (domains.backlinks_dir) may be EITHER a folder OR an EXACT FILE PATH — if it
    points at an existing file (or a folder + filename), that file is used DIRECTLY, no
    auto-detect. This is the explicit per-domain "use THIS file" override."""
    import glob
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
    # LEGACY SHAPE EXCLUDED (2026-08-07). Selection is now Top-Pages ranked by
    # TRAFFIC (project_two_stage_selection: harvest selects on last month's
    # traffic, OU ranks within that pool). The old backlinks-pages export ranks
    # by REFERRING DOMAINS, a different and now-superseded basis — and every
    # such file on disk was downloaded 2026-06-01..21.
    #
    # They were still being used: thekitchn and allrecipes both harvested on
    # 2026-07-22 off June backlinks files, because those were the only exports
    # they had. Silently selecting on a stale, retired signal is worse than not
    # finding a file, so we return None and let the caller raise its
    # export-not-found message (which names the legacy file it skipped).
    #
    # NOT deleted, and NOT unreachable: an explicit per-domain file override
    # (domains.backlinks_dir pointing AT the .xlsx) still uses it directly,
    # above — the capability stays, only the automatic pickup goes.
    # Uses the shared key, not a bare regex: it strips Chrome's " (1)" suffix
    # first, so `thepioneerwoman.com-backlinks_pages (1).xlsx` is recognised as
    # legacy rather than slipping through unmatched.
    def _is_legacy(p):
        k = semrush_export_key(os.path.basename(p))
        return bool(k) and k[1] == "backlinks"
    live = [h for h in hits if not _is_legacy(h)]
    if hits and not live:
        skipped = ", ".join(sorted(os.path.basename(h) for h in hits))
        print(f"  [harvest] ignoring legacy backlinks-shape export(s) for {domain}: {skipped}")
        print(f"  [harvest] a Top-Pages (organic.PagesV3) export is needed instead")
    return max(live, key=os.path.getmtime) if live else None


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
    base = os.path.basename(path)
    low = base.lower()
    cuts = [low.find(m) for m in ("-backlinks", "-organic") if low.find(m) > 0]
    return base[:min(cuts)] if cuts else ""


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


def _recipe_full_path_key(url):
    """Dedup key that KEEPS numeric segments — the fallback for publishers whose
    recipe URLs are numeric-id-only (`/recipe/100634884/`). For those, the alias
    key above is not merely coarse, it is catastrophic: with no slug to survive
    the digit-strip, EVERY recipe on the site collapses to one key.

    Measured 2026-08-14 on m.xiachufang.com: a 10,000-row SEMrush export produced
    exactly 2 candidates, because 9,999 of them keyed to ('xiachufang.com',
    'recipe'). Selected by `_read_backlinks_file`'s collapse guard, never by
    guessing per publisher."""
    segs = [s for s in urlparse(url).path.strip("/").split("/") if s]
    while segs and segs[-1].split(".")[0].lower() in ("detail", "index", "amp", ""):
        segs.pop()
    return (root_domain(url), "/".join(s.lower() for s in segs))


def _read_backlinks_file(domain, want, extra_dir=None):
    """Discovery from a local SEMrush page export, ranked by REFERRING DOMAINS desc
    (distinct linking sites — a robust authority marker, harder to game than raw
    backlink count). Returns [(url, title), …] for the top `want` response-200 content
    URLs in domains order. The SAME downstream pipeline (archive/collection pre-filter,
    recipe check, Moz scoring, rank, keep) then runs exactly as for Google results."""
    import openpyxl
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

    def _dedupe(keyfn):
        out, seen, meta = [], set(), {}
        for url, title, _r, traffic, tpct, seq in rows:
            key = keyfn(url)
            if key not in seen:
                seen.add(key)
                out.append((url, title))       # keep the highest-ranked variant
                meta[url] = {"traffic": traffic, "traffic_pct": tpct, "file_seq": seq}
            if len(out) >= want:
                break
        return out, meta

    out, meta = _dedupe(_recipe_path_key)      # collapse id / slug / detail.aspx aliases
    # ALIAS-COLLAPSE GUARD. The alias key drops numeric path segments so
    # /recipe/21014/slug/ and /recipe/slug/ are one recipe. On a publisher whose
    # URLs carry NO slug (/recipe/100634884/), nothing survives the digit-strip and
    # the whole site keys to ('host','recipe') — 10,000 rows in, 1 candidate out.
    # Detect it by RESULT rather than by guessing which publishers are numeric-id:
    # if deduping threw away most of what we asked for while the file plainly had
    # the rows, redo it on the full path. Loud, because silently harvesting 1 of 25
    # reads as "the publisher only has one recipe".
    _asked = min(want, len(rows))
    if len(out) < max(2, _asked // 2) < len(rows):
        alt, alt_meta = _dedupe(_recipe_full_path_key)
        if len(alt) > len(out):
            print(f"  [harvest] alias-dedupe collapsed {len(rows)} rows to {len(out)}; "
                  f"this publisher's URLs are numeric-id-only — re-deduped on the full "
                  f"path: {len(alt)} URLs")
            out, meta = alt, alt_meta
    print(f"  [harvest] SEMrush file {os.path.basename(path)}: {len(out)} URLs (by {rank_label})")
    # File the export into the tracked archive. Here, at the point it is
    # CONSUMED, is the one place every backlinks_file harvest passes through —
    # and after a successful parse, so a corrupt download is never archived as
    # though it were good. Non-fatal by construction; the harvest owns the
    # recipes, this is bookkeeping.
    archive_export(path)
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
    # Candidate ledger (docs/ai-editor-mediation.md): every URL this harvest throws
    # away, captured AT the drop. The publisher path already persists everything that
    # reaches Moz — collection_members keeps the losers too, flagged selected=0 — so
    # what was missing here is precisely the PRE-SCORING drops below, which until now
    # existed only in this function's stdout.
    dropped_candidates: list[dict] = []

    def _drop(url: str, title: str, reason: str, stage: str) -> None:
        dropped_candidates.append({"url": url, "title": title or "",
                                   "_dropped_reason": reason, "_stage": stage})

    # THE per-domain candidate filter (docs/candidate-filters.md, 2026-09-01):
    # one rule surface, SEMrush-filter-shaped, evaluated pre-fetch for free.
    # When present it SUPERSEDES the legacy recipe_path scope (the migration
    # ported those values into it as curator-authored keep conditions).
    # Born on the barilla run: a 403-walled domain paid an unblocker credit
    # per CATEGORY page just to drop it post-fetch, while /recipe/all/ told
    # the whole story in the URL.
    from input.pipeline import candidate_filter as cfilt
    _cf_rule = cfilt.parse_rule((_drow or {}).get("candidate_filter"))
    if _cf_rule:
        n_before = len(found)
        kept_f = []
        for _idx, (l, t) in enumerate(found, 1):
            _meta = file_meta.get(l) or {}
            ok, why = cfilt.evaluate(_cf_rule, {
                "url": l, "title": t,
                "traffic": _meta.get("traffic"),
                "traffic_pct": _meta.get("traffic_pct"),
                "rank": _meta.get("file_seq") or _idx})
            if ok:
                kept_f.append((l, t))
            else:
                _drop(l, t, why, "prefilter")
        found = kept_f
        if len(found) < n_before:
            print(f"  [harvest] candidate filter dropped {n_before - len(found)} "
                  f"URL(s) pre-fetch, {len(found)} kept")
        if n_before > 0 and not found:
            raise ValueError(
                "candidate_filter dropped ALL discovered URL(s) — the rule "
                "matches nothing on this publisher. Fix or clear it on the "
                "domain record.")
    elif recipe_path:
        n_before = len(found)
        kept_scoped = []
        for l, t in found:
            if _under_path(l, recipe_path):
                kept_scoped.append((l, t))
            else:
                _drop(l, t, f"off-path:/{recipe_path}", "prefilter")
        found = kept_scoped
        if len(found) < n_before:
            print(f"  [harvest] path-scoped to /{recipe_path}/ — dropped "
                  f"{n_before - len(found)} off-path URL(s), {len(found)} under path")
        # A scope that drops EVERYTHING is never the intended configuration —
        # it is a wrong value in the field (budgetbytes 2026-08-22: keep=50
        # landed in recipe_path, three runs "succeeded" at 0 URLs before
        # anyone read a log). Same rule as the dish pre-filter: a run must
        # not filter away the very thing it came for, silently.
        if n_before > 0 and not found:
            raise ValueError(
                f"recipe_path='/{recipe_path}/' dropped ALL {n_before} "
                f"discovered URL(s) — the path matches nothing on this "
                f"publisher. Fix or clear recipe_path on the domain record.")

    # Cheap pre-filter: drop archive/taxonomy/feed URLs (never a recipe) + collection/
    # listicle TITLES ("30 Greek Recipes", "10 Dinners") BEFORE the fetching recipe
    # check — saves credits + time (a bare `site:` query is mostly these). Title-based,
    # so it runs even when check_recipe is OFF (trusted/paywalled publishers): those
    # skip the fetch-verify but must still shed index/roundup pages. English-title
    # only here (no fetch ⇒ no lang/translation); non-English collection titles are
    # caught downstream by _is_recipe_filter when check_recipe is ON.
    from intake.build_query_batch import _looks_like_recipe_collection
    n_raw = len(found)
    kept_pre = []
    for l, t in found:
        if _looks_like_archive(l):
            _drop(l, t, "archive-url", "prefilter")
        elif _looks_like_recipe_collection(t):
            _drop(l, t, "collection-title", "prefilter")
        else:
            kept_pre.append((l, t))
    found = kept_pre
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
    # R3: a publisher MEASURED as unobtainable stops being fetched. It keeps its
    # membership, its DA/PA and its place in the corpus — we simply stop paying to
    # rediscover that its recipes are not in the HTML. Degrades to the score-only
    # path (discover + Moz-rank, zero fetches), which is what a curator was already
    # choosing manually as a euphemism for "don't burn money".
    if check_recipe and found:
        try:
            from input.pipeline import domains_lib as _dl0
            with _dl0._connect(_dl0._DEFAULT_DB) as _c0:
                _obt = _dl0.content_obtainable(_c0, domain)
                _human_only = _dl0.human_capture_only(_c0, domain)
        except Exception:
            _obt = "unknown"
            _human_only = False
        # R4 — the CURATED twin of content_obtainable='never'. Same outcome, but
        # reached by a decision instead of by waiting for the streak to prove it
        # again. 177milkstreet is the worked example: the body is never in the
        # response at any price, so every run that "verifies" it pays a render to
        # rediscover a paywall we have already measured (1 of 9, 11%).
        if _human_only:
            print(f"  [harvest] {domain} is HUMAN-CAPTURE-ONLY (curator-set) — "
                  f"discovering and scoring, but paying for no page fetches.")
            print(f"  [harvest] Ingest these from the cohort with the manual queue "
                  f"(open each page, click the bookmarklet), where YOUR browser is "
                  f"signed in and ours is not.")
            check_recipe = False
            score_only = True
        elif _obt == "never":
            print(f"  [harvest] {domain} is content_obtainable=NEVER — skipping all "
                  f"page fetches (measured: repeated runs extracted nothing).")
            print(f"  [harvest] Its recipes are captured by BOOKMARKLET, where your "
                  f"browser is authenticated and ours is not. Scoring only.")
            check_recipe = False
            score_only = True

    if check_recipe and found:
        from intake.build_query_batch import _is_recipe_filter
        # R2: a publisher already LEARNED to need a real browser should be fetched
        # rendered from the first request. Read here rather than threaded through
        # the job handler because the harvest already knows the domain, and this
        # keeps the caller's signature unchanged. mark_render_required wrote this
        # flag; until now nothing read it where it saves a credit.
        _render_upfront = False
        try:
            from input.pipeline import domains_lib as _dl
            with _dl._connect(_dl._DEFAULT_DB) as _c:
                _row = _dl.get_domain(_c, domain)
            _render_upfront = bool((_row or {}).get("render_required"))
        except Exception as _e:
            print(f"  [harvest] render_required lookup skipped: {type(_e).__name__}: {_e}")
        if _render_upfront:
            print(f"  [harvest] {domain} is render_required — fetching rendered up front "
                  f"(skips the known-doomed static fetch on every page)")
        kept, _dropped = _is_recipe_filter(   # _dropped: full entry dicts, ledgered below
            [{"url": l, "title": t} for l, t in found],
            capture_source="domain_harvest",
            capture_provenance={"domain": domain, "discover_source": source},
            unblocker=unblocker,   # flagged anti-bot publisher → live fetch via the paid unblocker
            render=_render_upfront,
            exclude_words=exclude_words,
            keyword_prescreen=keyword_prescreen, prescreen_domain=domain,
            domain_lang=domain_lang,
            should_cancel=should_cancel)
        recipe_pass = [(e["url"], e.get("title") or "") for e in kept]
        # These carry their own `_dropped_reason` (no-recipe-structure, fetch-failed,
        # collection-title, recipe-score<N …) — the same vocabulary the dish batch
        # emits, because it is the same filter. Let candidate_ledger.classify() read
        # it rather than restating the mapping here.
        dropped_candidates.extend(_dropped or [])
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
    from input.pipeline import url_scoring as _us
    _us.reset_moz_row_stats()
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
            _drop(url, title, "moz-unavailable", "moz")
            print(f"  [{i:>2}/{n_rp}] MOZ-FAIL  {url}")
    # STAMP THE PUBLISHER'S DA FROM THIS RUN. Moz returns domain_authority on
    # every URL it scores, so a harvest of N pages measures the same publisher's
    # DA N times and — until now — threw all N away. `domains.domain_authority`
    # was therefore whatever someone last typed by hand, going quietly stale
    # between harvests, and `da_last_scored` was never written at all.
    #
    # Median, not max: canonical-variant probing can return a neighbouring host's
    # figure, and one outlier should not move the publisher's recorded authority.
    # Written through update_domain so it takes the single validated write path
    # (which also stamps da_last_scored on an actual change).
    try:
        from input.pipeline.db import connect as _dbconn
        from input.pipeline import domains_lib as _dl_da
        _das = sorted(float(r["da"]) for r in scored if r.get("da") is not None)
        if _das:
            _median = _das[len(_das) // 2] if len(_das) % 2 else (
                (_das[len(_das) // 2 - 1] + _das[len(_das) // 2]) / 2.0)
            with _dbconn() as _dc:
                _prev = (_dl_da.get_domain(_dc, domain) or {}).get("domain_authority")
                if _dl_da.update_domain(_dc, domain, {"domain_authority": _median}) is not None:
                    if _prev is None or abs(float(_prev) - _median) > 1e-9:
                        print(f"  [harvest] DA refreshed for {domain}: "
                              f"{_prev if _prev is not None else '—'} -> {_median:g} "
                              f"(median of {len(_das)} Moz rows this run)")
    except Exception as e:
        # A stale DA is not worth failing a harvest over.
        print(f"  [harvest] DA stamp skipped ({type(e).__name__}: {e})")

    _ms = _us.moz_row_stats()
    print(f"  [harvest] Moz rows: {_ms['rows']} billed for {_ms['calls']} URL(s) "
          f"(canonical-variant learning saved ~{_ms['saved_vs_4x']} rows vs the old "
          f"4-variant probe; {_ms['uncrawled']} URL(s) had no Moz data)")

    # A harvest that BILLS and stores NOTHING is a failure, not a result.
    #
    # 2026-08-02: a scoring gate regression rejected every real Moz answer whose
    # http_code was not one of four allow-listed values. A pinchofyum refresh
    # billed 496 rows across 124 URLs and scored zero — and finished "successfully",
    # because every URL failing individually reads exactly like a publisher whose
    # pages Moz has never crawled. The curator found it by noticing the output, not
    # from any alarm: "got moz fail on all.. didn't get trapped anywhere".
    #
    # `uncrawled` is what separates the two. Moz genuinely having no data for a
    # whole publisher is a real (if unusual) answer and stays a soft result; every
    # URL failing for some OTHER reason — creds, quota, network, a bad gate — is a
    # bug, and spending money to learn nothing must be loud.
    if n_rp and not scored:
        _detail = (f"{n_rp} candidate(s), {_ms['rows']} Moz row(s) billed, 0 scored "
                   f"({_ms['uncrawled']} reported no Moz data)")
        if _ms["uncrawled"] >= n_rp:
            print(f"  [harvest] WARNING — Moz has no data for ANY candidate: {_detail}")
        else:
            raise RuntimeError(
                f"Moz scoring produced nothing while billing rows: {_detail}. "
                f"Not an uncrawled publisher — check credentials, quota and the "
                f"score_url_via_moz gate before re-running.")

    # DISCOVERED SOMETHING, KEPT NOTHING is the other silent failure, and it is the
    # one that actually bit: 2026-08-03, three publishers each discovered 40 URLs,
    # passed 0, and recorded `success`. The Moz guard above never fires there
    # because n_rp is 0 — nothing reached scoring — so a run that fetched 25 pages
    # THROUGH THE PAID UNBLOCKER and kept none of them read as a clean result.
    #
    # The cause was discovery, not filtering: no serp_query was set, so SERP
    # returned the sites' own /set/, /category/ and /tag/ archives. The filter was
    # right to drop every one. Loud, because the fix is a curator input (a
    # serp_query, or a SEMrush export) and nothing downstream can infer it.
    if n_raw and not n_rp:
        raise RuntimeError(
            f"Harvest discovered {n_raw} URL(s) and none were recipes. "
            f"Discovery is returning non-recipe pages — check serp_query, "
            f"recipe_path and harvest_source for this publisher rather than "
            f"re-running as-is.")
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
            "scored": len(scored), "recipe_path": used_path, "query": query,
            # Everything discarded before scoring, captured at the drop. The caller
            # persists it via candidate_ledger — see _handle_publisher_refresh_job.
            "dropped_candidates": dropped_candidates}
