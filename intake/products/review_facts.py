"""Reverse index: a product's retail URL / ASIN -> the review FACTS we already hold for it.

The bridge between the two product paths. The curator extracts a product from a retail page
(Amazon/SLT/WS); we normalize its identity (ASIN, else normalized URL) and look it up against
the review catalog (the ATK-style `products` rows, whose `retailer_offers` carry the same
ASINs/URLs). If we have a verdict for it, the product form surfaces:
  - the VERIFIED fact — reviewer name + tier + verbatim verdict (real by construction), and
  - an OUR-VOICE paraphrase (brand-safe: attribute the finding to the source, our wording).

So the review roundup we already ingested becomes ENRICHMENT for the product-first workflow,
without re-ingesting locked-down review pages.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import unquote

from input.pipeline.url_utils import normalize_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

_ASIN_RE = re.compile(r"/(?:dp|gp/product|product|-)/([A-Z0-9]{10})(?:[/?]|$)", re.I)
_BARE_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def asin_from(url: str) -> str:
    """Extract an Amazon ASIN from a product URL (or accept a bare ASIN).

    URL-DECODES first: review sites almost always route buy links through an affiliate
    redirector with the real Amazon URL percent-encoded in a query param — ATK uses
    `clicks.trx-hub.com/xid/...?q=https%3A%2F%2Fwww.amazon.com%2Fdp%2FB01NARK346%3Ftag%3D...`.
    Without decoding, 46 of the 50 buy links on a single ATK page yielded nothing.
    """
    if not url:
        return ""
    if _BARE_ASIN_RE.match(url.strip()):
        return url.strip().upper()
    m = _ASIN_RE.search(url)
    if m:
        return m.group(1).upper()
    try:
        decoded = unquote(unquote(url))          # doubly-encoded redirects do occur
    except Exception:
        return ""
    m = _ASIN_RE.search(decoded)
    return m.group(1).upper() if m else ""


def asins_in(text: str) -> list:
    """Every Amazon ASIN linked from a page, in order of first appearance, deduped.

    Works on the raw markdown of a review: the reviewer's own buy links are the most direct
    statement of WHICH product they tested — far better identity than searching Amazon by
    product name, which returns competitors (see amazon_rainforest.find_asin, where the
    most-reviewed hit for a Le Creuset query was an Amazon Basics pot).
    """
    if not text:
        return []
    hay = text
    try:
        hay = text + "\n" + unquote(text)        # catch encoded links without losing plain ones
    except Exception:
        pass
    seen, out = set(), []
    for m in _ASIN_RE.finditer(hay):
        a = m.group(1).upper()
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def asins_near(text: str, keywords, window: int = 600) -> list:
    """ASINs linked WITHIN `window` chars of a keyword mention, most-frequent first.

    A roundup page links 20+ products; only some are the one we're researching. Proximity to
    the product's distinctive tokens is what separates "the pot this review is about" from
    "the other twenty pots on the page".
    """
    if not text or not keywords:
        return []
    try:
        hay = text + "\n" + unquote(text)
    except Exception:
        hay = text
    low = hay.lower()
    spots = []
    for k in keywords:
        k = (k or "").lower().strip()
        if len(k) < 3:
            continue
        start = 0
        while True:
            i = low.find(k, start)
            if i < 0:
                break
            spots.append(i)
            start = i + len(k)
    if not spots:
        return []
    counts: dict[str, int] = {}
    for m in _ASIN_RE.finditer(hay):
        pos = m.start()
        if any(abs(pos - s) <= window for s in spots):
            a = m.group(1).upper()
            counts[a] = counts.get(a, 0) + 1
    return [a for a, _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def _norm(url: str) -> str:
    """Normalized URL with query stripped (drops affiliate tags like ?tag=… so a curator's
    plain URL matches the review's tagged one)."""
    if not url:
        return ""
    try:
        n = normalize_url(url) or url
    except Exception:
        n = url
    return n.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()


def _fact_from_product(d: dict) -> dict:
    """Distill one review-catalog product row into the facts a form would surface."""
    v = (d.get("verdicts") or [{}])[0]
    return {
        "product_name": d.get("name", ""),
        "product_class": d.get("product_class", ""),
        "category": d.get("category", ""),
        "brand": d.get("brand", ""),
        "reviewer": v.get("reviewer", ""),
        "tier": v.get("tier", ""),
        "verdict": v.get("summary", ""),           # verbatim (verified by construction)
        "capacity": (d.get("specs") or {}).get("capacity", ""),
    }


def consensus_asin(docs, keywords) -> dict:
    """Which ASIN do the REVIEWERS agree this product is?

    `docs` = [{label, markdown}, ...] — the source pages we fetched. Each reviewer links to
    the product they actually tested, so the ASIN appearing near the product's name across
    the MOST INDEPENDENT SOURCES is the identity, established by people who had the thing in
    their hands. That beats searching Amazon by name, which ranks by popularity and happily
    returns a competitor.

    Returns {asin, sources: [labels], candidates: {asin: [labels]}} — empty asin when the
    sources don't agree or none link one, in which case the caller should fall back to a
    brand-matched search rather than guess.
    """
    by_asin: dict[str, list] = {}
    for d in docs or []:
        md = d.get("markdown") or ""
        if not md:
            continue
        near = asins_near(md, keywords)
        # Fall back to every ASIN on the page only for SHORT pages (a single-product review);
        # on a 20-product roundup that would make every rival look like agreement.
        found = near or (asins_in(md) if len(md) < 8000 else [])
        for a in found[:3]:                      # a page's top few, not its whole catalogue
            by_asin.setdefault(a, [])
            if d.get("label") and d["label"] not in by_asin[a]:
                by_asin[a].append(d["label"])
    if not by_asin:
        return {"asin": "", "sources": [], "candidates": {}}
    best, labels = max(by_asin.items(), key=lambda kv: (len(kv[1]), kv[0]))
    # One lone source is a mention, not a consensus — but it is still better than nothing,
    # so return it and let the caller weigh it against the brand-matched search.
    return {"asin": best, "sources": labels, "candidates": by_asin}


def build_reverse_index(conn: sqlite3.Connection) -> dict:
    """{'by_asin': {asin: fact}, 'by_url': {normurl: fact}} over the review catalog. A product
    is indexed under EVERY retail identity its offers carry, so any of them can find it."""
    by_asin, by_url = {}, {}
    try:
        rows = conn.execute("SELECT data FROM products").fetchall()
    except sqlite3.OperationalError:
        return {"by_asin": {}, "by_url": {}}
    for (dj,) in rows:
        try:
            d = json.loads(dj)
        except Exception:
            continue
        fact = _fact_from_product(d)
        for o in (d.get("retailer_offers") or []):
            if not isinstance(o, dict):
                continue
            asin = (o.get("asin") or "").strip().upper() or asin_from(o.get("source_url") or "")
            if asin:
                by_asin.setdefault(asin, fact)
            nu = _norm(o.get("source_url") or "")
            if nu:
                by_url.setdefault(nu, fact)
    return {"by_asin": by_asin, "by_url": by_url}


# ---- Materialized reverse table (scales to a mass ATK-rec scan) ---------------------
# A persistent index keyed by retail identity ("asin:XXX" / "url:normurl") so a product
# lookup is a single indexed read, not a full rebuild — refreshed whenever the review
# catalog grows (each ATK-review ingest). This is the "reverse table" our product records
# join to by URL.
_FACT_COLS = ("product_name", "product_class", "category", "brand", "reviewer", "tier",
              "verdict", "capacity")


def ensure_links_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS product_review_links ("
        "key TEXT PRIMARY KEY, " + ", ".join(f"{c} TEXT" for c in _FACT_COLS) +
        ", updated_at TEXT)")
    conn.commit()


def refresh_review_links(conn: sqlite3.Connection) -> int:
    """(Re)materialize the reverse table from the whole review catalog. Idempotent — run it
    after a mass ATK-rec scan (or any review ingest). Returns the number of keys indexed."""
    ensure_links_table(conn)
    idx = build_reverse_index(conn)
    now = _now()
    n = 0
    cols = ", ".join(_FACT_COLS)
    ph = ", ".join("?" for _ in _FACT_COLS)
    upd = ", ".join(f"{c}=excluded.{c}" for c in _FACT_COLS)
    for kt, kmap in (("asin", idx["by_asin"]), ("url", idx["by_url"])):
        for k, f in kmap.items():
            conn.execute(
                f"INSERT INTO product_review_links(key, {cols}, updated_at) "
                f"VALUES(?, {ph}, ?) ON CONFLICT(key) DO UPDATE SET {upd}, updated_at=excluded.updated_at",
                (f"{kt}:{k}", *[f.get(c, "") for c in _FACT_COLS], now))
            n += 1
    conn.commit()
    return n


def lookup(conn: sqlite3.Connection, *, asin: str = "", url: str = "") -> list[dict]:
    """Review facts for a product identity (ASIN first, then normalized URL) from the
    materialized reverse table. Returns a list (usually 0 or 1) of fact dicts."""
    ensure_links_table(conn)
    keys = []
    a = asin_from(asin) or asin_from(url)
    if a:
        keys.append(f"asin:{a}")
    nu = _norm(url)
    if nu:
        keys.append(f"url:{nu}")
    out, seen = [], set()
    for key in keys:
        r = conn.execute(
            f"SELECT {', '.join(_FACT_COLS)} FROM product_review_links WHERE key = ?",
            (key,)).fetchone()
        if r:
            f = dict(zip(_FACT_COLS, r))
            if f["product_name"] not in seen:
                out.append(f); seen.add(f["product_name"])
    return out


def our_voice(fact: dict) -> str:
    """A short BCC-voice paraphrase of the source's finding — brand-safe: it ATTRIBUTES the
    finding to the source (which we verified by extracting their page) but is OUR wording, not
    a verbatim copy. Best-effort; falls back to a plain attributed sentence on any LLM error."""
    reviewer = fact.get("reviewer") or "the reviewer"
    tier = fact.get("tier") or ""
    verdict = fact.get("verdict") or ""
    if not verdict:
        return f"{reviewer} rated this {tier}." if tier else ""
    try:
        import llm
        sys = ("Rewrite a product-test finding into ONE tight sentence in a confident house "
               "editorial voice. ATTRIBUTE it to the named source. Keep only claims present in "
               "the source text — invent nothing. Do NOT quote verbatim; paraphrase.")
        user = f"Source: {reviewer}\nTier: {tier}\nFinding: {verdict}"
        resp = llm.create(operation="product_fact_voice", model="claude-haiku-4-5",
                          max_tokens=160, temperature=0.3, system=sys,
                          messages=[{"role": "user", "content": user}])
        txt = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "").strip()
        return txt or (f"{reviewer} named this {tier}." if tier else "")
    except Exception:
        return f"{reviewer} named this {tier}." if tier else ""
