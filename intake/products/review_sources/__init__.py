"""Per-source review decoders — registry, dispatch, and the ingest bridge.

The bookmarklet captures a review page (markdown + url); `detect_source()` routes it to the right
per-source decoder (atk / wirecutter / williams_sonoma / wsj / …), each of which converts the page
into ONE canonical shape (see base.py). `ingest_review()` then writes that canonical result into
the two-table store — a review header + its review_products, which link DYNAMICALLY to catalog
products ([[project_reviews_architecture]]). This is the review analog of recipe standardization:
many source styles, one canonical form ([[feedback_single_path]]).
"""
from __future__ import annotations

import sqlite3

from . import atk, seriouseats, wirecutter, williams_sonoma, wsj, base  # noqa: F401

# Registry (order = detection priority). Add a module here to support a new source.
# A source MISSING from this list still works — it just falls through to the generic prompt,
# which reads the page's own wording for tiers. That is how Serious Eats produced nine
# unique one-off "tiers" from its award headlines (2026-07-27). Absence is silent.
SOURCES = [atk, seriouseats, wirecutter, williams_sonoma, wsj]


def detect_source(url: str, head: str = "") -> str | None:
    """Return the KEY of the decoder that recognizes this page, or None."""
    for mod in SOURCES:
        try:
            if mod.matches(url, head):
                return mod.KEY
        except Exception:
            continue
    return None


def _module(key: str):
    return next((m for m in SOURCES if m.KEY == key), None)


def supported() -> list:
    """[{key, label, implemented}] — for a UI that shows which sources we can decode."""
    return [{"key": m.KEY, "label": getattr(m, "LABEL", m.KEY),
             "implemented": getattr(m, "IMPLEMENTED", False)} for m in SOURCES]


def parse_review(md: str, *, url: str = "", captured_at: str = "") -> dict | None:
    """Route to the right decoder and return the canonical extraction. None if no source matches;
    raises NotImplementedError if the source is recognized but its decoder isn't built yet."""
    key = detect_source(url, md[:400])
    mod = _module(key) if key else None
    if mod is None:
        return None
    return mod.parse(md, url=url, captured_at=captured_at)


def extract_review(md: str, *, url: str = "", captured_at: str = "") -> dict:
    """LLM extraction with a PER-SOURCE PROMPT (memory/project_reviews_architecture). Detect the source
    from the URL, load its `EXTRACT_HINTS` prompt fragment (atk / wirecutter / williams_sonoma / wsj —
    generic for anything else), and run the shared LLM extractor (extract/markdown_to_review) tuned to
    THAT site's structure. Not deterministic: the per-source regex parsers proved too brittle for real
    pages, so the source modules now contribute a PROMPT, not a parser. Raises ValueError if nothing
    extractable."""
    from extract.markdown_to_review import markdown_to_review
    key = detect_source(url, md[:400])
    mod = _module(key) if key else None
    hints = getattr(mod, "EXTRACT_HINTS", "") if mod else ""
    ext = markdown_to_review(md, source_url=url, source_hints=hints)
    if not ext or not ext.get("products"):
        raise ValueError("Could not extract any product recommendations from this page.")
    return ext


def ingest_review(conn: sqlite3.Connection, md: str, *, url: str = "",
                  captured_at: str = "") -> dict:
    """Full bookmarklet -> catalog path: decode the page (deterministic-first + LLM fallback),
    create/lookup the review header, SELECT its WS taxonomy from the list (the same matcher the
    commerce join uses), upsert its reviewed items (idempotent — re-ingest updates, never
    duplicates), then resolve the dynamic links. Returns the stored review."""
    from intake.products import review_store, catalog_store

    ext = extract_review(md, url=url, captured_at=captured_at)

    src = ext.get("review_source") or {}
    pc = ext.get("product_class") or {}
    review = review_store.create_review(conn, {
        "reviewer": src.get("reviewer", ""),
        "product_class": pc.get("name", ""),
        "category": pc.get("category", ""),
        "title": src.get("title", ""),
        "url": src.get("url", "") or url,
        "last_updated": src.get("last_updated", ""),
        "captured_at": src.get("captured_at", "") or captured_at,
        "rating_scale": src.get("rating_scale", ""),
        "buying_guide": pc.get("buying_guide", ""),   # the review's editorial header (rich source copy)
        "criteria": pc.get("criteria") or [],
    })
    rid = review["review_id"]

    # On (re-)ingest, fill EMPTY header fields from the extraction — a review created empty by an
    # earlier import (or a first pass that found nothing) gets its title/date/scale/buying-guide
    # backfilled — without clobbering anything the curator already set.
    header_fill = {k: v for k, v in (
        ("title", src.get("title")), ("last_updated", src.get("last_updated")),
        ("rating_scale", src.get("rating_scale")), ("buying_guide", pc.get("buying_guide")),
    ) if (v or "").strip() and not (review.get(k) or "").strip()}
    if header_fill:
        review = review_store.update_review(conn, rid, header_fill)

    # Taxonomy = a SELECTION from the WS list via search (not an LLM-invented label): classify the
    # header-derived class into the actual ws_categories taxonomy (reuses the equipment matcher — the
    # sibling of the type-ahead + commerce join). Only when the curator hasn't already pinned one.
    if not review.get("ws_category_id") and pc.get("name"):
        try:
            from intake.products.equipment_match import classify_term
            hit = classify_term(pc["name"], conn)
            if hit.get("ws_category_id"):
                review = review_store.update_review(
                    conn, rid, {"ws_category_id": hit["ws_category_id"]})
        except Exception as e:  # taxonomy match is best-effort — never block the import
            print(f"[review-ingest] ws taxonomy match skipped: {e}")

    for p in ext.get("products", []):
        verd = p.get("verdict") or {}
        offers = p.get("retailer_offers") or []
        amz = next((o for o in offers if (o.get("asin") or "")), offers[0] if offers else {})
        item = {
            "name": p.get("name", ""),
            "brand": p.get("brand") or catalog_store._guess_brand(p.get("name", "")),
            "tier": verd.get("tier", ""),
            "summary": verd.get("summary", ""),
            "price_at_test": verd.get("price_at_test"),
            "specs": p.get("specs") or {},
            "retailer_offers": offers,
            "asin": (amz.get("asin") or "").strip(),
            "url": (amz.get("source_url") or "").strip(),
        }
        # The extractor often leaves `asin` empty even when the buy link contains one — ATK
        # percent-encodes the Amazon URL inside a redirector, so it isn't visible as a field.
        # resolve_asin digs it out of the links, which is where the reviewer actually stated
        # which product they tested.
        item["asin"] = review_store.resolve_asin(item)
        review_store.upsert_review_product(conn, rid, item)
    conn.commit()
    review_store.resolve_links(conn, rid, force=True)
    return review_store.get_review(conn, rid)