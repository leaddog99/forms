"""collections_lib.py — generic typed collections + membership (the M2M junction).

A recipe (by url_normalized) can belong to MANY collections — a dish, a
publisher's "best of", a chef, a curated list. Each is a (collection_type,
collection_key) pair; `collection_members` is the junction + per-collection
ranking ledger. One recipe → many membership rows (the "join" is free).

First consumer: PUBLISHER collections (the domains page, dishes-page-style). A
publisher refresh discovers the publisher's recipe URLs (SERP `site:domain/recipes`
+ filter=0), Moz-scores them, ranks by PA (most-notable), and keeps the top-N
(`keep`, default 10, per-publisher overridable — the analog of a dish's
top_n_final). Content extraction is SEPARATE (authenticated fetch) — this is the
discovery + ranking ledger. See docs/collections.md / project_collections.

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
    # image_url — the recipe's og:image, captured at harvest for the selected top-N so
    # DISCOVERED (not-yet-ingested) members still show a thumbnail. Added idempotently.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(collection_members)")}
    if "image_url" not in cols:
        conn.execute("ALTER TABLE collection_members ADD COLUMN image_url TEXT")
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
             adjusted_pa, rank_score, rank, selected, note, image_url, model_version, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [(collection_type, collection_key, normalize_url(m["url"]) or m["url"], m.get("title"),
          m.get("da"), m.get("pa"), m.get("adjusted_pa"), m.get("rank_score"),
          m.get("rank"), 1 if m.get("selected") else 0, m.get("note"),
          m.get("image_url"), model_version, now) for m in members],
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
               cm.rank_score, cm.rank, cm.selected,
               m.recipe_id,
               json_extract(m.data, '$.name'),
               json_extract(m.data, '$._master.exceptionalism.grade'),
               COALESCE(json_extract(m.data, '$._source.previewImage'),
                        json_extract(m.data, '$.image[0]'),
                        cm.image_url)
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
            "selected", "recipe_id", "name", "grade", "preview_image"]
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
from input.pipeline.url_word_lists import url_lacks_recipe_signal  # noqa: E402,F401


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
    and the harvest reads it in place — no need to move it into input/ first."""
    import os
    cand = []
    if extra:
        cand.extend(extra if isinstance(extra, (list, tuple)) else [extra])
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

    Tolerant of how SEMrush names the file:
      - whole site:  {domain}-backlinks-pages.xlsx  (or the _pages underscore variant)
      - a subdirectory export prepends the subpath:
                     {domain}_recipe-backlinks_pages.xlsx  (allrecipes.com/recipe)
    So we match `{domain}*-backlinks*pages*.xlsx` — the `*` after the domain absorbs an
    optional `_subpath`; the trailing `*` tolerates the browser's de-dup suffix
    (`…pages (1).xlsx`). Across ALL search dirs, the MOST RECENTLY MODIFIED match wins."""
    import os, glob
    hits = []
    for d in backlinks_search_dirs(extra_dir):
        hits += glob.glob(os.path.join(d, f"{domain}*-backlinks*pages*.xlsx"))
    return max(hits, key=os.path.getmtime) if hits else None


def export_prefix(path):
    """The `{domain}[_subpath]` prefix of a SEMrush page-export filename — the part
    before `-backlinks`. e.g. `allrecipes.com_recipe-backlinks_pages.xlsx` →
    `allrecipes.com_recipe`; `addapinch.com-backlinks-pages.xlsx` → `addapinch.com`.
    Returns '' if the name isn't an export."""
    import os
    base = os.path.basename(path)
    low = base.lower()
    i = low.find("-backlinks")
    return base[:i] if i > 0 else ""


def scan_export_inbox(dirs):
    """Find SEMrush page-export files (`*-backlinks*pages.xlsx`) sitting in any of
    `dirs` (the watched inbox — typically ~/Downloads). Returns
    [{"path", "prefix"}] newest-first. Pure discovery — the caller matches each
    prefix to a known domain and routes it (intake_export_file)."""
    import os, glob
    seen, out = set(), []
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for p in glob.glob(os.path.join(d, "*-backlinks*pages*.xlsx")):
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
        raise FileNotFoundError(
            f"No SEMrush export found for {domain}. Expected a file named "
            f"'{domain}-backlinks-pages.xlsx' (or '{domain}_<subpath>-backlinks_pages.xlsx') "
            f"in one of: {', '.join(searched) or '(no existing folder configured)'}. "
            f"Save the export there (no need to move it into input/) and run again.")
    ws = openpyxl.load_workbook(path, read_only=True).active
    it = ws.iter_rows(values_only=True)
    header = [str(h or "") for h in next(it)]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None
    ui = col("Source url", "Page URL", "Target url", "URL")
    ti = col("Source title", "Title", "Page title")
    ci = col("Response code", "Status code")
    di = col("Domains", "Referring Domains", "Referring domains")
    if ui is None or di is None:
        raise ValueError(f"Unexpected SEMrush columns (need a URL + Domains): {header}")
    rows = []
    for r in it:
        url = str(r[ui] or "").strip()
        if not url:
            continue
        # Keep 2xx AND 3xx: a 301 here is just the http://… form of a real recipe
        # (pre-HTTPS backlinks, common in a subdirectory export) — the recipe check and
        # Moz follow the redirect to the canonical page. Only drop dead 4xx/5xx. The
        # downstream archive filter drops the bare homepage; the recipe check drops
        # non-recipes.
        code = str(r[ci]).strip() if ci is not None else ""
        if code[:1] in ("4", "5"):
            continue
        rows.append((url, str(r[ti] if ti is not None else "") or "", int(r[di] or 0)))
    rows.sort(key=lambda x: -x[2])   # referring-domains desc
    out, seen = [], set()
    for url, title, _dom in rows:
        key = _recipe_path_key(url)            # collapse id / slug / detail.aspx aliases
        if key not in seen:
            seen.add(key); out.append((url, title))   # keep the highest-domains variant
        if len(out) >= want:
            break
    print(f"  [harvest] backlinks file {os.path.basename(path)}: {len(out)} URLs (by referring domains)")
    return out


def harvest_publisher_top(domain, keep=10, discover_n=80, recipe_path=None,
                          query=None, check_recipe=True, source="serp", records=None,
                          unblocker=False, should_cancel=None, backlinks_dir=None,
                          url_prefilter=False) -> dict:
    """Discover a publisher's recipe URLs, (optionally) VERIFY each is a real recipe,
    Moz-score the survivors, rank by PA, mark the top `keep` selected.

    `source` — 'serp' (Google, default) or 'backlinks_file' (local SEMrush export,
    input/{domain}-backlinks-pages.xlsx, ranked by referring domains). `records` is the
    file-source analog of SERP `discover_n` — how many top rows to pull from the file.
    BOTH sources feed the IDENTICAL downstream pipeline (archive/collection filter →
    recipe check → Moz → rank → keep), so a file URL is treated exactly like a Google one.

    `query` — a VERBATIM Google query (e.g. 'site:bostonglobe.com recipe'), run as-is
    via SerpAPI; OVERRIDES path detection. The curator owns the Google syntax (same
    as dish SERP queries); the code just executes it. Needed for publishers whose
    recipes aren't under a clean path segment (Boston Globe lives under /YYYY/.../slug,
    so `site:domain/recipes` finds nothing — the term form does).

    `check_recipe` — fetch each candidate and keep only real recipes via the CANONICAL
    dish-batch filter (`_is_recipe_filter`: schema.org/Recipe JSON-LD, else phrase
    score). Drops the /recipes/ index, 'best-...-2025' listicles, /recipe-database/,
    and section pages that merely contain the word 'recipe'.

    Returns {members, discovered, recipe_pass, scored, recipe_path, query}. Content
    extraction / ingestion into master is a SEPARATE step (not done here)."""
    if source == "backlinks_file":
        found = _read_backlinks_file(domain, want=int(records or discover_n or 100),
                                     extra_dir=backlinks_dir)
        used_path = None
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
        used_path = None
    else:
        if not recipe_path:
            recipe_path = detect_recipe_path(domain)
        found = discover_publisher_recipe_urls(domain, want=discover_n, recipe_path=recipe_path)
        used_path = recipe_path

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

    # TEE-UP LEARN (before filtering): tokenize THIS batch's URLs, drop tokens already in
    # either list, classify the unknown remainder in ONE call, update both lists — so a new
    # publisher's own dish words are known for THIS run. Learning from the INCOMING
    # (unconfirmed) batch is SAFE because the classifier prompt is strict: venue/dining/
    # business words (bistro/cantina/buffet/dining) go to 'stop', only true foods/dishes to
    # 'food'. (A first cut with a loose prompt poisoned 'food' with venue words — fixed.)
    # See [[project_url_word_filter]].
    if url_prefilter and found:
        try:
            from input.pipeline.url_word_lists import learn_from_urls
            learn_from_urls([l for l, _ in found])
        except Exception as ex:
            print(f"  [harvest] url-word tee-up learn skipped: {type(ex).__name__}: {ex}")

    # Recipe check — reuse the dish batch's filter so "is this a recipe" is decided
    # ONE way everywhere ([[single-path]]): JSON-LD Recipe → keep; else phrase score.
    # The OPTIONAL URL-text pre-filter (domains.url_prefilter) is applied INSIDE
    # _is_recipe_filter — the fetch choke point — so the same skip serves every caller
    # (it drops /restaurant//chef//jobs/ URLs before the paid fetch). When check_recipe
    # is OFF (trusted/paywalled — no fetch), apply it inline so the option still bites.
    recipe_pass = found
    if check_recipe and found:
        from intake.build_query_batch import _is_recipe_filter
        kept, _dropped = _is_recipe_filter(
            [{"url": l, "title": t} for l, t in found],
            capture_source="domain_harvest",
            capture_provenance={"domain": domain, "discover_source": source},
            unblocker=unblocker,   # flagged anti-bot publisher → live fetch via the paid unblocker
            url_prefilter=url_prefilter,
            should_cancel=should_cancel)
        recipe_pass = [(e["url"], e.get("title") or "") for e in kept]
    elif url_prefilter and found:
        n_pre = len(found)
        recipe_pass = [(l, t) for l, t in found if not url_lacks_recipe_signal(l)]
        if len(recipe_pass) < n_pre:
            print(f"  [harvest] url-prefilter dropped {n_pre - len(recipe_pass)} "
                  f"non-recipe-looking URLs (no fetch-verify on this publisher)")

    scored = []
    for url, title in recipe_pass:
        if should_cancel and should_cancel():
            from input.pipeline.jobs import JobCancelled
            raise JobCancelled("cancelled during Moz scoring")
        s = score_url_via_moz(url)
        if s and s.get("page_authority"):
            scored.append({"url": url, "title": title,
                           "da": float(s["domain_authority"]),
                           "pa": float(s["page_authority"])})
    scored.sort(key=lambda m: -m["pa"])
    keep = max(1, int(keep or 10))
    for i, m in enumerate(scored, 1):
        m["rank"] = i
        m["rank_score"] = m["pa"]          # within-publisher rank IS page authority
        m["selected"] = 1 if i <= keep else 0
    # Thumbnail the SELECTED top-N only (bounded) — captures og:image so discovered
    # members show a picture; ingested members override with the master's real image.
    n_img = n_serp = 0
    for m in scored:
        if not m["selected"]:
            continue
        img, title = _fetch_og_meta(m["url"])
        if title and not (m.get("title") or "").strip():
            m["title"] = title    # source had no title (e.g. subdir export) → use page/slug
        # Anti-bot/blocked publishers (thekitchn) return no og:image on a direct
        # fetch → fall back to a FETCH-FREE SERP image lookup (Google has the image).
        # Bounded to the selected top-N + only on an og:image miss, so ~1 credit per
        # blocked pick and zero when the direct grab works.
        if not img:
            img = _serp_image_for(domain, m.get("title") or "")
            if img:
                n_serp += 1
        m["image_url"] = img
        if img:
            n_img += 1
    extra = f" ({n_serp} via SERP image fallback)" if n_serp else ""
    print(f"  [harvest] captured {n_img} thumbnails for {keep} selected{extra}")
    return {"members": scored, "discovered": n_raw, "recipe_pass": len(recipe_pass),
            "scored": len(scored), "recipe_path": used_path, "query": query}
