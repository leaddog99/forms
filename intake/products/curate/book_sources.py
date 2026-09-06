"""Book evidence for the amazon_pool curation mode (docs/book-review-curation.md).

The sources stage that replaces fetch_docs for book classes: no authority
publishes equipment-style roundups of cookbooks, so the evidence is the POOL's
own Amazon product data — one Traject `book_product` call per candidate
(2 credits each), cached on disk by ASIN so re-runs and prompt iterations
don't re-bill.

Returns docs in the exact shape build_prompt/fetch_docs use
([{label, url, markdown, via, error}]) plus an evidence index by ASIN that the
verify stage reads instead of making a second network pass — the evidence was
fetched BY ASIN, so identity is given, not recovered.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Callable

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "..", ".."))
_REALRANK = os.path.join(_ROOT, "docs", "RealRank")
for _p in (_ROOT, _REALRANK):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BOOK_CACHE_DIR = os.path.join(_ROOT, "cache", "curate", "books")
POOL_TOP_N = 10


def _cache_path(asin: str) -> str:
    os.makedirs(BOOK_CACHE_DIR, exist_ok=True)
    return os.path.join(BOOK_CACHE_DIR, f"{asin.upper()}.json")


def _fetch_book(asin: str, *, refresh: bool = False) -> dict:
    """One book's evidence, disk-cached by ASIN. A fetch failure returns
    {"error": ...} and is cached NOT — a book missing today may resolve
    tomorrow, and caching a failure would hide it until a manual refresh."""
    path = _cache_path(asin)
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    from intake.products import amazon_rainforest as az
    try:
        ev = az.book_product(asin)
    except Exception as e:
        return {"asin": asin, "error": f"{type(e).__name__}: {e}"}
    if not (ev.get("title") or "").strip():
        return {"asin": asin, "error": "empty product response"}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ev, f, indent=1)
    return ev


def _realrank(ev: dict) -> None:
    """Stamp realrank_score/rating_shape onto the evidence in place — the
    arithmetic backbone, computed once at fetch so verify never recomputes."""
    hist, total = ev.get("histogram") or [], ev.get("ratings_total") or 0
    if not hist or not total:
        return
    try:
        from realrank_index import realrank_index, polarization
        ev["realrank_score"] = round(realrank_index(hist, total), 1)
        ev["rating_shape"] = (polarization(hist) or {}).get("label") or ""
    except Exception as e:                                     # pragma: no cover
        print(f"[CURATE]   realrank unavailable for {ev.get('asin')}: {e}")


def _doc_markdown(ev: dict) -> str:
    """One book's evidence as the document the research prompt reads. Provenance
    labels are baked into the section headings so the model can only quote a
    voice by naming it (owners vs Amazon's AI summary vs press)."""
    L = [f"# {ev.get('title', '')}"]
    if ev.get("sub_title"):
        L.append(f"Subtitle: {ev['sub_title']}")
    who = ", ".join(ev.get("authors") or [])
    facts = " · ".join(x for x in (
        f"by {who}" if who else "",
        ev.get("publisher") or "",
        ev.get("publication_date") or "",
        f"ISBN-10 {ev['isbn_10']}" if ev.get("isbn_10") else "",
        ev.get("format") or "") if x)
    if facts:
        L.append(facts)
    L.append(f"ASIN: {ev['asin']} — https://www.amazon.com/dp/{ev['asin']}")
    if ev.get("price"):
        L.append(f"Current Amazon price: {ev['price']}")

    L.append("\n## Owner arithmetic (the ranking backbone)")
    L.append(f"- {ev.get('rating')}★ across {ev.get('ratings_total'):,} ratings"
             if ev.get("ratings_total") else "- no ratings data")
    if ev.get("histogram"):
        L.append("- histogram (5→1 stars): " + ", ".join(map(str, ev["histogram"])))
    if ev.get("realrank_score") is not None:
        L.append(f"- RealRank {ev['realrank_score']}"
                 + (f" ({ev['rating_shape']})" if ev.get("rating_shape") else ""))
    for b in (ev.get("bestsellers_rank") or [])[:4]:
        L.append(f"- Amazon bestseller rank: #{b['rank']:,} in {b['category']}")

    if ev.get("customers_say_summary"):
        L.append("\n## Amazon's AI summary of customer reviews (Amazon's voice — "
                 "attribute as such)")
        L.append(ev["customers_say_summary"])
    if ev.get("customers_say"):
        L.append("\n## Review attributes (owner sentiment, with mention counts)")
        L.append("; ".join(f"{a['name']}: {a['value']}" for a in ev["customers_say"]))

    if ev.get("editorial_reviews"):
        L.append("\n## Editorial reviews (publisher-SELECTED press — color, never "
                 "the reason a rank exists)")
        for e in ev["editorial_reviews"][:4]:
            L.append(f"- {e.get('title') or 'press'}: {e['body'][:400]}")

    if ev.get("book_description"):
        # Descriptions front-load verifiable public claims (The Wok, 2026-09-05:
        # "#1 NYT Bestseller · Winner of the 2023 James Beard Award · NPR Books
        # We Love" — all in the first 400 chars). The heading tells the model
        # which parts are minable facts vs marketing; the prompt's evidence
        # rules carry the full carve-out.
        L.append("\n## Publisher's description (marketing voice — but named awards, "
                 "bestseller-list placements, and named best-of lists inside it are "
                 "verifiable public claims, quotable with the institution named)")
        L.append(ev["book_description"][:1200])

    for i, r in enumerate(ev.get("top_reviews") or [], 1):
        if i == 1:
            L.append("\n## Top customer reviews (individual owners)")
        L.append(f"### Review {i} — {r.get('rating')}★ — {r.get('title', '')}")
        L.append(r.get("body", ""))
    return "\n".join(L)


def fetch_book_docs(pool_collection: str, product_class: str, *,
                    top_n: int = POOL_TOP_N, refresh: bool = False,
                    terms: list | None = None, editors_choice: str = "",
                    should_cancel: Callable[[], bool] | None = None) -> tuple:
    """The pool's top candidates -> (docs, evidence).

    Pool = the search collection's non-excluded candidates, Wilson order —
    harvest SELECTS, this run RANKS within it (two-stage gospel). Captured
    reviews overlay after, exactly as for equipment: a bookmarklet-captured
    NYT cookbook roundup joins the supplied documents.
    """
    import sqlite3
    from intake.products import collections_store as cst
    from intake.products.curate import verify as V
    from intake.products.curate.pipeline import _overlay_captured_reviews

    conn = sqlite3.connect(V.DB)
    try:
        # RealRank order (curator, 2026-09-05: "realrank is free via the widget").
        # The refresh's widget screen already scored the Wilson top-10 — the same
        # set this pool takes — so RealRank is stored and free here; list_candidates'
        # "realrank" order falls back to wilson_score for unmeasured rows (SQLite
        # sorts NULL last on DESC), so absent stays absent, never zero.
        cands = [c for c in cst.list_candidates(conn, pool_collection, order="realrank")
                 if not c.get("excluded") and (c.get("asin") or "").strip()][:top_n]
    finally:
        conn.close()
    if not cands:
        raise ValueError(f"pool collection {pool_collection!r} has no usable candidates "
                         f"— run its Amazon search first")
    scored = sum(1 for c in cands if c.get("realrank_score") is not None)
    print(f"[CURATE] pool {pool_collection!r}: {len(cands)} candidate(s), RealRank order "
          f"({scored} widget-scored, rest by Wilson)")

    docs, evidence = [], {}
    for c in cands:
        if should_cancel and should_cancel():
            raise KeyboardInterrupt("cancelled while fetching book evidence")
        asin = c["asin"].strip().upper()
        ev = _fetch_book(asin, refresh=refresh)
        label = f"{asin} — {(ev.get('title') or c.get('title') or '?')[:60]}"
        if ev.get("error"):
            print(f"[CURATE]   {asin} FAILED — {ev['error']}")
            docs.append({"label": label, "url": f"https://www.amazon.com/dp/{asin}",
                         "markdown": "", "via": "traject-product", "error": ev["error"]})
            continue
        _realrank(ev)
        md = _doc_markdown(ev)
        print(f"[CURATE]   {asin} {len(md):>6} chars | {(ev.get('title') or '')[:60]}")
        docs.append({"label": label, "url": f"https://www.amazon.com/dp/{asin}",
                     "markdown": md, "via": "traject-product", "error": ""})
        evidence[asin] = ev
    # THE CURATOR'S PIN (the Kenji lesson, 2026-09-05): the quoted harvest for
    # "wok cookbooks" never returned The Wok — the James Beard winner, #1 in
    # Wok Cookery — because its listing lacks the exact phrase, and the closed
    # world rightly refused to rank a book nobody supplied. An editors_choice
    # carrying an ASIN widens the closed world to pool + pin: its evidence is
    # fetched and supplied like any pool book's, labeled with its provenance,
    # and the prompt's editors-choice rules govern how it may place.
    # Real ASIN shapes only — B0-prefixed retail ASINs or ISBN-10s (digits,
    # optional X check digit). A bare [A-Z0-9]{10} matched the word
    # "TECHNIQUES" in the pin text (job 1792) and fetched a ten-letter ASIN.
    import re as _re
    m = _re.search(r"\b(B0[A-Z0-9]{8}|\d{9}[\dX])\b", (editors_choice or "").upper())
    pin = m.group(1) if m else ""
    if pin and pin not in evidence:
        ev = _fetch_book(pin, refresh=refresh)
        if ev.get("error"):
            print(f"[CURATE]   pin {pin} FAILED — {ev['error']}")
            docs.append({"label": f"CURATOR PIN {pin}", "markdown": "",
                         "url": f"https://www.amazon.com/dp/{pin}",
                         "via": "traject-product", "error": ev["error"]})
        else:
            _realrank(ev)
            md = _doc_markdown(ev)
            print(f"[CURATE]   pin {pin} {len(md):>6} chars | {(ev.get('title') or '')[:60]}")
            docs.append({"label": f"CURATOR PIN {pin} — {(ev.get('title') or '')[:60]}",
                         "url": f"https://www.amazon.com/dp/{pin}",
                         "markdown": md, "via": "traject-product (curator pin)",
                         "error": ""})
            evidence[pin] = ev
    docs = _overlay_captured_reviews(docs, product_class, terms)
    return docs, evidence
