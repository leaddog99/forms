"""Amazon owner reviews as a STANDARD evidence source for a curated run.

Until 2026-09-14 only the book pipeline read Amazon's AI review summary
("Customers say") — the product path made the same Rainforest product call per
pick but never asked for the summarization attributes, and the model ranked from
the nine editorial sources alone. Curator: "it should be a standard review site."

This module turns the class's Amazon search POOL (product_collections, the same
cohort the collection_refresh job screens on owner ratings) into ONE supplied
document, "Amazon (owner reviews)", laid out per ASIN exactly like the book
evidence: owner arithmetic, Amazon's AI summary, the review attributes with
mention counts, and a few top reviews. It rides through `fetch_docs` → the prompt
like a captured review does. The prompt labels it OWNER VOICE — it can carry
"what owners say", never the independent-evidence requirement (verify's
check_independent_sources still treats every amazon host as non-independent).

Per-ASIN Rainforest replies are cached at cache/curate/asins/<ASIN>.json
(2 credits each with the summary; `refresh` re-fetches).
"""
from __future__ import annotations

import json
import os
import sqlite3

from intake.products.curate.pipeline import CACHE_DIR

LABEL = "Amazon (owner reviews)"
TOP_N = 10


def _cache_path(asin: str) -> str:
    d = os.path.join(CACHE_DIR, "asins")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{asin}.json")


def _fetch_listing(asin: str, *, refresh: bool = False) -> dict:
    """One listing with the AI summary, cached. A failure is returned, not cached."""
    from intake.products import amazon_rainforest as az
    path = _cache_path(asin)
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    try:
        ev = az.product_ratings(asin, summarization=True)
    except Exception as e:
        return {"asin": asin, "error": f"{type(e).__name__}: {e}"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ev, f)
    return ev


def listing_markdown(ev: dict, cand: dict | None = None) -> str:
    """The per-ASIN section, same shape as book_sources' evidence text."""
    import sys
    _rr = os.path.join(os.path.dirname(CACHE_DIR), "..", "docs", "RealRank")
    _rr = os.path.abspath(_rr)
    if _rr not in sys.path:
        sys.path.insert(0, _rr)
    from realrank_index import realrank_index, polarization
    L = [f"### {ev.get('title') or (cand or {}).get('title') or ev.get('asin')}"]
    if ev.get("brand"):
        L.append(f"Brand: {ev['brand']}")
    L.append(f"ASIN: {ev['asin']} — https://www.amazon.com/dp/{ev['asin']}")
    if ev.get("price"):
        L.append(f"Current Amazon price: {ev['price']}")
    L.append("\n#### Owner arithmetic")
    if ev.get("ratings_total"):
        L.append(f"- {ev.get('rating')}★ across {int(ev['ratings_total']):,} ratings")
    else:
        L.append("- no ratings data")
    hist = ev.get("histogram") or []
    if hist and ev.get("ratings_total"):
        L.append("- histogram (5→1 stars): " + ", ".join(map(str, hist)))
        try:
            rr = round(realrank_index(hist, ev["ratings_total"]), 1)
            shape = (polarization(hist) or {}).get("label") or ""
            L.append(f"- RealRank {rr}" + (f" ({shape})" if shape else ""))
        except Exception:
            pass
    if ev.get("customers_say_summary"):
        L.append("\n#### Amazon's AI summary of customer reviews (Amazon's voice — attribute as such)")
        L.append(ev["customers_say_summary"])
    if ev.get("customers_say"):
        L.append("\n#### Review attributes (owner sentiment, with mention counts)")
        L.append("; ".join(f"{a['name']}: {a['value']}" for a in ev["customers_say"]))
    for i, r in enumerate((ev.get("top_reviews") or [])[:3], 1):
        if i == 1:
            L.append("\n#### Top customer reviews (individual owners)")
        L.append(f"- {r.get('rating')}★ {r.get('title', '')}: {(r.get('body') or '')[:400]}")
    return "\n".join(L)


def owner_doc(product_class: str, pool_name: str, *, db_path: str,
              refresh: bool = False, top_n: int = TOP_N) -> dict:
    """The supplied document. Never raises: a pool with no candidates comes back as a
    FAILED doc with the reason, which the prompt lists under COULD NOT RETRIEVE."""
    from intake.products import collections_store as cst
    conn = sqlite3.connect(db_path)
    try:
        cands = [c for c in cst.list_candidates(conn, pool_name, order="realrank")
                 if not c.get("excluded") and (c.get("asin") or "").strip()][:top_n]
    finally:
        conn.close()
    if not cands:
        return {"label": LABEL, "url": "", "via": "rainforest", "markdown": "",
                "error": f"pool collection {pool_name!r} has no usable candidates"}
    scored = sum(1 for c in cands if c.get("realrank_score") is not None)
    print(f"[CURATE] {LABEL}: pool {pool_name!r} → {len(cands)} listing(s) "
          f"(RealRank order, {scored} widget-scored)")
    sections = [f"# Amazon owner reviews for the {product_class} class\n"
                f"Source: the class's Amazon search pool ({pool_name!r}), top {len(cands)} by "
                f"RealRank/Wilson. Owner voice — arithmetic and Amazon's own review summary — "
                f"NOT an editorial review."]
    got = 0
    for c in cands:
        asin = c["asin"].strip().upper()
        ev = _fetch_listing(asin, refresh=refresh)
        if ev.get("error"):
            print(f"[CURATE]   {asin} FAILED — {ev['error']}")
            continue
        got += 1
        print(f"[CURATE]   {asin} {('✓ summary' if ev.get('customers_say_summary') else '– no summary')}"
              f" | {ev.get('rating')}★ × {ev.get('ratings_total')} | {(ev.get('title') or '')[:60]}")
        sections.append(listing_markdown(ev, c))
    if not got:
        return {"label": LABEL, "url": "", "via": "rainforest", "markdown": "",
                "error": "every listing fetch failed"}
    md = "\n\n".join(sections)
    return {"label": LABEL, "url": f"pool:{pool_name}", "via": "rainforest",
            "markdown": md, "class_hits": got}
