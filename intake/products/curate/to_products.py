"""Curated picks -> catalog product records.

The end of the run, and the reason it exists: a brief nobody can act on is an essay. What
comes out the far side is a row in `products` carrying the placement, the reasoning, the
owner evidence and the buy links.

Three rules the mapping is built around:

  IDENTITY IS THE ASIN. `catalog_store._mfg_key` matches on brand + GTIN/MPN/model number,
  and a review roundup states none of those reliably — so every re-run would create a
  duplicate. An ASIN taken from the reviewer's own buy link is the identity these picks
  actually carry, and it is exact.

  ONE PRODUCT, MANY PLACEMENTS. The same pan can be overall #2 and "Best value" #1. That is
  one product record holding two placements, not two records.

  A SUSPECT LISTING PUBLISHES NO BUY LINK. When enrichment flagged the ASIN as a different
  product, the record is still created — a reviewer really did rank that product — but the
  Amazon offer is dropped rather than published. Dropping an offer costs one placement;
  emitting the wrong one sends a reader (and our commission) to the wrong thing.
"""
from __future__ import annotations

import sqlite3

from intake.products import catalog_store


def _identity(p: dict) -> tuple:
    """Group placements that describe the SAME product. ASIN when we have one; otherwise the
    brand+title, which is all a pick without a verified listing gives us."""
    asin = (p.get("asin") or "").strip().upper()
    if asin:
        return ("asin", asin)
    return ("name", (p.get("manufacturer") or "").strip().lower(),
            (p.get("product_title") or "").strip().lower())


def _pick_label(p: dict) -> str:
    sec = p.get("section") or "overall"
    return f"{sec} #{p.get('place')}"


# `bcc_pick` is deliberately NOT written from a run. Filling it looked free — a create could
# stamp "Best Overall" and existing surfaces would light up — but `save_product`'s merge path
# never rewrites it, so a product demoted to #3 by a later run kept the badge from the first
# one (measured: the USA Pan 1140LF, still reading "Best Overall" at overall #3). A field we
# can set but cannot refresh is a staleness trap. The placement is authoritative and lives in
# `curation`, where it is replaced wholesale every run; bcc_pick stays a human field.


def _offers_for(picks: list, *, drop_amazon: bool) -> list:
    """RetailerOffer rows from the enriched buy links.

    `source_url` holds the CLEAN destination — every tracking parameter already stripped by
    buy_links.clean_url, because a harvested link carries the REVIEWER's affiliate tag and
    republishing it would pay them on our sale. `affiliate_url` stays blank on purpose: our
    codes are minted at click time, so nothing is stored pre-tagged.
    """
    seen, out = set(), []
    for p in picks:
        asin = (p.get("asin") or "").strip().upper()
        for o in (p.get("offers") or []):
            retailer = (o.get("retailer") or "").strip()
            url = (o.get("url") or "").strip()
            if not url:
                continue
            is_amazon = "amazon." in url.lower() or retailer.lower() == "amazon"
            if is_amazon and drop_amazon:
                continue
            key = (retailer.lower(), url.split("?")[0].rstrip("/").lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"retailer": retailer or "source", "source_url": url,
                        "asin": asin if is_amazon else "",
                        "affiliate_url": "",
                        "price": p.get("current_price") or p.get("typical_price")})
    if not drop_amazon:
        asin = next((("" + (p.get("asin") or "")).strip().upper() for p in picks
                     if (p.get("asin") or "").strip()), "")
        if asin and not any((o.get("asin") or "") == asin for o in out):
            out.append({"retailer": "Amazon", "asin": asin,
                        "source_url": f"https://www.amazon.com/dp/{asin}",
                        "affiliate_url": "", "price": None})
    return out


def _realrank_from(picks: list, collection: str) -> dict | None:
    """The owner half, when enrichment got a real histogram. Arithmetic over the feed — never
    the model's opinion — which is why it needs no approval."""
    src = next((p for p in picks if p.get("owner_count") and p.get("realrank_score") is not None),
               None)
    if not src:
        return None
    return {
        "score": src.get("realrank_score"),
        "score_basis": f"Amazon owner histogram, {int(src['owner_count']):,} ratings "
                       f"(curated run: {collection})",
        "owner": {"avg_rating": src.get("owner_rating"),
                  "review_count": src.get("owner_count"),
                  "histogram": src.get("owner_histogram") or [],
                  "polarization": ({"label": src.get("rating_shape")}
                                   if src.get("rating_shape") else {}),
                  "sources": [{"source": "amazon", "listing_id": src.get("asin") or "",
                               "avg_rating": src.get("owner_rating"),
                               "count": src.get("owner_count"),
                               "histogram": src.get("owner_histogram") or []}]},
    }


def _curation_from(picks: list, *, collection: str, product_class: str,
                   job_id: int | None) -> dict:
    return {
        "collection": collection,
        "product_class": product_class,
        "job_id": job_id,
        "placements": [{
            "collection": collection,
            "section": p.get("section") or "",
            "place": p.get("place"),
            "best_for": p.get("best_for") or "",
            "why_it_ranks_here": p.get("why_it_ranks_here") or "",
            "edge_over_next": p.get("edge_over_next") or "",
            "important_tradeoff": p.get("important_tradeoff") or "",
            "typical_price": p.get("typical_price"),
            "current_price": p.get("current_price"),
            "price_type": p.get("price_type") or "",
            "source_links": p.get("source_links") or [],
        } for p in picks],
    }


def _existing_row(conn: sqlite3.Connection, rows: list, asin: str, suspect: bool,
                  brand: str, title: str) -> str | None:
    """Which catalog row this pick already IS, strongest evidence first.

    Getting this wrong in either direction is expensive, so the order is deliberate:

      1. the product_id the LAST RUN linked to this pick — a fact we recorded, not a guess;
      2. the ASIN, exact, when the listing wasn't flagged as a different product;
      3. brand + name exactly — the only identity a direct-sell product carries.

    A miss creates a duplicate on every re-run (measured: the Williams Sonoma pan, which has
    no ASIN and no manufacturer id, forked a second row on the second run). Falling through
    to `save_product`'s own manufacturer-key match is the last step, not the first.
    """
    for r in rows:
        pid = (r.get("product_id") or "").strip()
        if pid:
            return pid
    if asin and not suspect:
        hit = catalog_store.find_by_asin(conn, asin)
        if hit:
            return hit
    return catalog_store.find_by_name(conn, brand, title)


def materialize(conn: sqlite3.Connection, *, collection: str, product_class: str,
                picks: list, job_id: int | None = None,
                on_result=None) -> dict:
    """Turn a run's placements into catalog rows. Returns a summary.

    `on_result(slot, product_id, action)` is called per placement so the caller can write the
    link back onto the pick row without this module knowing about that table.
    """
    groups: dict[tuple, list] = {}
    for p in picks:
        groups.setdefault(_identity(p), []).append(p)

    created = merged = skipped = 0
    for ident, rows in groups.items():
        title = (rows[0].get("product_title") or "").strip()
        brand = (rows[0].get("manufacturer") or "").strip()
        if not title:
            skipped += len(rows)
            continue
        asin = (rows[0].get("asin") or "").strip().upper()
        suspect = bool(rows[0].get("identity_warning"))
        offers = _offers_for(rows, drop_amazon=suspect)

        product = {
            "product_class": product_class,
            "brand": brand,
            "name": title,
            "specs": {"capacity": rows[0].get("capacity") or ""},
            "retailer_offers": offers,
            "sources": sorted({s for p in rows for s in _sources_of(p)}),
        }

        res = catalog_store.save_product(conn, product,
                                         merge_into=_existing_row(conn, rows, asin, suspect,
                                                                  brand, title))
        pid, action = res["product_id"], res["action"]
        created += action == "created"
        merged += action == "merged"

        rr = _realrank_from(rows, collection)
        if rr:
            catalog_store.set_realrank(conn, pid, rr)
        catalog_store.set_curation(conn, pid, _curation_from(
            rows, collection=collection, product_class=product_class, job_id=job_id))

        for p in rows:
            if on_result:
                on_result(p.get("slot") or p.get("_slot") or "", pid, action)
            note = " [ASIN withheld — listing looked like another product]" if suspect else ""
            print(f"[CURATE]   {action:<7} {_pick_label(p):<22} {brand} {title}"[:150] + note)

    return {"products_created": created, "products_merged": merged,
            "placements": len(picks), "skipped": skipped,
            "distinct_products": len(groups)}


def _sources_of(p: dict) -> list:
    """The REVIEWERS covering this pick, by name.

    `Product.sources` means "reviewer names covering this product", so only the named
    authorities count. A pick's `source_links` are whatever was read to verify the row, which
    includes retailer product pages — the first cut derived hostnames from them and wrote
    `walmart.com` and `nordstrom.com` into the reviewer list, turning a buy link into an
    apparent review. An unrecognized host now yields nothing rather than a guess.

    Matching reuses the fetcher's own `_host_ok`, which already knows that Wirecutter is a
    PATH under nytimes.com — two copies of that rule would eventually disagree.
    """
    try:
        import realrank_research as rr
    except Exception:
        return []
    out = []
    for u in (p.get("source_links") or []):
        for label, site in rr.SOURCE_SITES:
            try:
                if rr._host_ok(str(u), site):
                    out.append(label)
                    break
            except Exception:
                continue
    return out
