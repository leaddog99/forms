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
             adjusted_pa, rank_score, rank, selected, note, model_version, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [(collection_type, collection_key, normalize_url(m["url"]) or m["url"], m.get("title"),
          m.get("da"), m.get("pa"), m.get("adjusted_pa"), m.get("rank_score"),
          m.get("rank"), 1 if m.get("selected") else 0, m.get("note"),
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


def get_collection_top(conn, collection_type, collection_key, limit=50) -> list:
    """Top members of a collection, LEFT-JOINed to master_recipes on url_normalized
    (the url work): an already-ingested recipe shows its REAL name/grade/thumbnail
    and `ingested=True`; a discovered-but-not-yet-fetched one shows the SERP title +
    `ingested=False`. The join is the canonical-URL link between ledger and content."""
    ensure_collection_members_table(conn)
    rows = conn.execute(
        """
        SELECT cm.url_normalized, cm.title, cm.da, cm.pa, cm.adjusted_pa,
               cm.rank_score, cm.rank, cm.selected,
               m.recipe_id,
               json_extract(m.data, '$.name'),
               json_extract(m.data, '$._master.exceptionalism.grade'),
               COALESCE(json_extract(m.data, '$._source.previewImage'),
                        json_extract(m.data, '$.image[0]'))
        FROM collection_members cm
        LEFT JOIN master_recipes m
          ON m.url_normalized = cm.url_normalized AND m.user_id = 0
        WHERE cm.collection_type = ? AND cm.collection_key = ?
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


def _serp_links(query, want=50) -> list:
    """Raw organic links for a SerpAPI query (filter=0, paginated). Unfiltered —
    callers filter. Returns [(link, title), …]."""
    if not _SERPAPI_KEY:
        return []
    out, seen = [], set()
    for start in range(0, want + 20, 10):
        try:
            r = requests.get("https://serpapi.com/search.json", params={
                "engine": "google", "q": query, "num": 10,
                "start": start, "filter": 0, "api_key": _SERPAPI_KEY}, timeout=30)
            res = r.json().get("organic_results") or []
        except Exception:
            break
        for it in res:
            link = it.get("link") or ""
            if link and link not in seen:
                seen.add(link)
                out.append((link, it.get("title") or ""))
        if not res or len(out) >= want:
            break
    return out[:want]


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
    return {"members": scored, "discovered": len(found), "recipe_pass": len(recipe_pass),
            "scored": len(scored), "recipe_path": used_path, "query": query}
