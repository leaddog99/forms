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
_ARCHIVE_RE = re.compile(
    r"/(tag|tags|category|categories|author|page|archives?|topics?|search|feed|"
    r"comments?|wp-json|wp-admin|wp-content|wp-includes|sitemap|amp)(/|$)"
    r"|/page/\d+", re.IGNORECASE)


def _looks_like_archive(url: str) -> bool:
    """True for taxonomy/pagination/feed/admin URLs + the bare site root — never a
    recipe page. Used to pre-filter discovery before the (fetching) recipe check."""
    path = urlparse(url).path.strip("/")
    if not path:  # bare host root / homepage
        return True
    return bool(_ARCHIVE_RE.search(urlparse(url).path))


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
_FETCH_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def _fetch_og_image(url, timeout=8):
    """Best-effort og:image (or twitter:image) for a recipe URL — a thumbnail for the
    DISCOVERED (not-yet-ingested) members. Reads only the document <head> region, never
    raises (anti-bot/timeouts are normal here); returns an absolute URL or None."""
    try:
        resp = requests.get(url, headers=_FETCH_UA, timeout=timeout,
                            stream=True, allow_redirects=True)
        if resp.status_code != 200:
            return None
        head = ""
        for chunk in resp.iter_content(chunk_size=16384, decode_unicode=True):
            head += chunk if isinstance(chunk, str) else chunk.decode("utf-8", "ignore")
            if "</head>" in head.lower() or len(head) > 200_000:
                break
        resp.close()
    except Exception:
        return None
    m = _OG_IMAGE_RE.search(head) or _OG_IMAGE_RE2.search(head)
    if not m:
        return None
    img = m.group(1).strip()
    if img.startswith("//"):
        img = "https:" + img
    elif img.startswith("/"):
        p = urlparse(url)
        img = f"{p.scheme}://{p.netloc}{img}"
    return img if img.startswith("http") else None


def harvest_publisher_top(domain, keep=10, discover_n=80, recipe_path=None,
                          query=None, check_recipe=True) -> dict:
    """Discover a publisher's recipe URLs, (optionally) VERIFY each is a real recipe,
    Moz-score the survivors, rank by PA, mark the top `keep` selected.

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
    if query:
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

    # Cheap pre-filter: drop archive/taxonomy/feed URLs (never a recipe) BEFORE the
    # fetching recipe check — saves credits + time (a bare `site:` query is mostly
    # these). Conservative patterns only.
    n_raw = len(found)
    found = [(l, t) for l, t in found if not _looks_like_archive(l)]
    if len(found) < n_raw:
        print(f"  [harvest] pre-filtered {n_raw - len(found)} archive/taxonomy URLs "
              f"({len(found)} candidates remain)")

    # Recipe check — reuse the dish batch's filter so "is this a recipe" is decided
    # ONE way everywhere ([[single-path]]): JSON-LD Recipe → keep; else phrase score.
    recipe_pass = found
    if check_recipe and found:
        from intake.build_query_batch import _is_recipe_filter
        kept, _dropped = _is_recipe_filter([{"url": l, "title": t} for l, t in found])
        recipe_pass = [(e["url"], e.get("title") or "") for e in kept]

    scored = []
    for url, title in recipe_pass:
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
    n_img = 0
    for m in scored:
        if m["selected"]:
            m["image_url"] = _fetch_og_image(m["url"])
            if m["image_url"]:
                n_img += 1
    print(f"  [harvest] captured {n_img} thumbnails for {keep} selected")
    return {"members": scored, "discovered": n_raw, "recipe_pass": len(recipe_pass),
            "scored": len(scored), "recipe_path": used_path, "query": query}
