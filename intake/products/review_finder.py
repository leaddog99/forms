"""Find candidate review pages for a product — SERP first, curator second, fetch LAST.

The automation of the curator's *fetching* step, NOT of their judgment
([[project_llm_review_synthesis]]). The flow this module serves:

    product name
      -> serp_search()            find candidate review pages   (this module, no fetch)
      -> CURATOR APPROVES a subset                              (the UI; hard requirement)
      -> html_to_markdown(unblocker=True)  fetch OUR OWN COPY   (this module)
      -> review_sources.ingest_review()    decode + store       (existing rails)
      -> LLM synthesizes facts FROM OUR STORED COPIES           (later step, elsewhere)

Why the curator gate matters, concretely: a general web-browsing agent asked for the same
roundup got blocked by Amazon, Wirecutter and Serious Eats, and silently substituted a
Best Buy page for Amazon and a *Bloglovin mirror* for Serious Eats. Attributing a finding
to Serious Eats when we only ever read a third-party scrape is exactly the claim the
two-table review store exists to prevent. So:

  - **Discovery never fetches.** `find_candidates()` spends one SERP call and touches no
    target site; nothing is written to the DB.
  - **A named source that can't be fetched is ABSENT, never substituted.** `ingest_url()`
    fails that URL and says why; it does not go looking for a mirror.
  - Every candidate is labelled with what we know about it (do we have a decoder? is the
    publisher in our domains master? have we already ingested it?) so the curator picks
    with the evidence in front of them.

Fetching is deliberately ONE URL PER CALL: an unblocker fetch of a paywalled review runs
tens of seconds, so a 6-source roundup would blow any sane request timeout as a batch. The
UI walks the approved list sequentially and shows each row's outcome as it lands.
"""
from __future__ import annotations

import sqlite3
from urllib.parse import urlparse

# Search terms appended to the product name. Plain and verbatim — we want the pages a
# person would find, not a clever query.
_QUERY_SUFFIX = "review"

# SERP results that are never a review page worth fetching (retail listings, video, social).
# Bootstrap/seed only — the curator's approval is the real filter ([[feedback_no_data_in_code]]).
_SKIP_HOSTS = {
    "youtube.com", "m.youtube.com", "youtu.be", "reddit.com", "www.reddit.com",
    "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com",
    "twitter.com", "x.com", "pinterest.com", "www.pinterest.com", "tiktok.com",
}


def _host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def _known_hosts(conn: sqlite3.Connection) -> set:
    """Publishers already in the domains master — a candidate on one of these is a source we
    have a considered opinion about, rather than an unknown site the SERP happened to surface."""
    try:
        rows = conn.execute("SELECT domain, root_domain FROM domains").fetchall()
    except sqlite3.OperationalError:
        return set()
    out = set()
    for dom, root in rows:
        for v in (dom, root):
            if v:
                out.add(str(v).strip().lower())
    return out


def _ingested_urls(conn: sqlite3.Connection) -> dict:
    """{normalized url: review_id} for reviews we already hold, so the curator isn't offered
    a page we've already got."""
    try:
        rows = conn.execute("SELECT review_id, url FROM reviews WHERE url <> ''").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {_norm(u): rid for rid, u in rows if u}


def _norm(url: str) -> str:
    if not url:
        return ""
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()


def find_candidates(conn: sqlite3.Connection, product: str, *, want: int = 12,
                    extra_terms: str = "") -> dict:
    """SERP for review pages covering `product`. Costs ONE SERP call; fetches nothing and
    writes nothing. Returns {query, candidates:[…]} where each candidate carries what we know:

        url, title, domain, rank
        decoder        the review_sources key that recognizes this host, or None
        decoder_label  its human label
        known_source   the publisher is in our domains master
        review_id      we have ALREADY ingested this exact page (else None)
    """
    from input.pipeline.serp_search import serp_search
    from intake.products import review_sources

    product = (product or "").strip()
    if not product:
        raise ValueError("product name required")
    query = " ".join(x for x in (product, extra_terms.strip(), _QUERY_SUFFIX) if x)

    results = serp_search(query, pages=2, want=max(want * 2, 20))
    known = _known_hosts(conn)
    already = _ingested_urls(conn)
    labels = {s["key"]: s["label"] for s in review_sources.supported()}

    out, seen = [], set()
    for r in results:
        url = (r.get("link") or "").strip()
        host = _host(url)
        if not url or not host or host in _SKIP_HOSTS:
            continue
        nu = _norm(url)
        if nu in seen:
            continue
        seen.add(nu)
        decoder = review_sources.detect_source(url) or None
        out.append({
            "url": url,
            "title": (r.get("title") or "").strip(),
            "domain": host,
            "rank": r.get("rank"),
            "decoder": decoder,
            "decoder_label": labels.get(decoder or "", ""),
            "known_source": host in known,
            "review_id": already.get(nu),
        })
        if len(out) >= want:
            break
    return {"query": query, "candidates": out}


def ingest_url(conn: sqlite3.Connection, url: str) -> dict:
    """Fetch ONE approved review page through the unblocker and run it down the existing
    ingest rails (decode -> review header -> review_products -> resolve links).

    Returns {ok, url, review_id, reviewer, product_count, error}. A page we cannot fetch or
    decode is reported as a failure for THAT url — we never substitute another source for it.
    """
    from to_markdown.html_to_markdown import html_to_markdown
    from intake.products import review_sources

    url = (url or "").strip()
    if not url:
        return {"ok": False, "url": url, "error": "url required"}

    try:
        doc = html_to_markdown(url, unblocker=True, render=True)
    except Exception as e:
        return {"ok": False, "url": url, "error": f"fetch failed: {e}"}

    md = (doc or {}).get("markdown") or ""
    if not md.strip():
        return {"ok": False, "url": url, "error": "fetch returned no readable content "
                                                  "(blocked or JS-only page)"}
    try:
        review = review_sources.ingest_review(conn, md, url=doc.get("source_url") or url)
    except (ValueError, NotImplementedError) as e:
        return {"ok": False, "url": url, "error": str(e)}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"ingest failed: {e}"}

    return {
        "ok": True,
        "url": url,
        "review_id": review.get("review_id"),
        "reviewer": review.get("reviewer", ""),
        "title": review.get("title", ""),
        "product_count": len(review.get("products") or []),
    }
