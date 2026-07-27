"""Validate and ENRICH a curation result — the part only BCC can do.

EXPERIMENTAL. Read-only against production data; imported by nothing in the server.

The curator's original validator checks SHAPE: three per category, places 1-2-3, weights
summing to 1.0, ASIN ten characters, canonical /dp/ link, price numeric. All of that is kept
(`validate_shape`) because refusing to build beats emitting a plausible-but-broken artifact.

But shape is not truth. `B073Q9K2H3` passes every one of those rules — it is the Amazon
Basics pot that this system confidently scored as a Le Creuset on 2026-07-27. So we add four
checks that need real data:

  ASIN IDENTITY   the listing's brand and TYPE actually match the named product
  BLANK FILLING   an ASIN left blank out of caution is recovered from the variant family
                  or from a review that already linked it
  OWNER EVIDENCE  the real 5/4/3/2/1 histogram and a RealRank score, so a "best" claim has
                  arithmetic under it and not only judgment
  PROVENANCE      source_links must be present — required by the prompt, ignored by the
                  original validator, empty in every example row it shipped with

Each enrichment is BEST-EFFORT and additive: a network failure annotates the row, it never
invalidates a run that is otherwise sound.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from typing import Any

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for p in (_ROOT, os.path.join(_ROOT, "docs", "RealRank")):
    if p not in sys.path:
        sys.path.insert(0, p)

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
DB = os.path.join(_ROOT, "recipes.db")

# Product-type vocabulary is SHARED with realrank (intake/products/product_types) so the two
# cannot drift — and so both know that a cocotte is a dutch oven.
from intake.products.product_types import same_type  # noqa: E402


def rows_of(data: dict) -> list:
    """Every ranked row, overall + per category, with a label for error messages."""
    out = []
    for i, r in enumerate(data.get("overall_top_three") or [], 1):
        out.append((f"overall #{r.get('place', i)}", r))
    for i, r in enumerate(data.get("category_rankings") or [], 1):
        out.append((f"{r.get('category', '?')} #{r.get('place', i)}", r))
    return out


# --------------------------------------------------------------------------- #
#  Shape — the curator's original contract
# --------------------------------------------------------------------------- #

def validate_shape(data: dict) -> list:
    """Structural errors. Empty list = the artifact may be built."""
    errs = []
    overall = data.get("overall_top_three")
    if not isinstance(overall, list) or len(overall) != 3:
        errs.append("overall_top_three must contain exactly three rows")
    elif sorted(int(r.get("place", 0)) for r in overall) != [1, 2, 3]:
        errs.append("overall places must be 1, 2 and 3")

    cats = data.get("category_rankings")
    if not isinstance(cats, list) or not cats:
        errs.append("category_rankings must be a non-empty list")
    else:
        grouped: dict[str, list] = {}
        for r in cats:
            grouped.setdefault(str(r.get("category", "")).strip(), []).append(r)
        for cat, rows in grouped.items():
            if not cat:
                errs.append("a category row is missing its category name")
            elif sorted(int(r.get("place", 0)) for r in rows) != [1, 2, 3]:
                errs.append(f"category {cat!r} must have exactly places 1, 2 and 3")

    for label, r in rows_of(data):
        price = r.get("typical_price", r.get("price"))
        if not isinstance(price, (int, float)):
            errs.append(f"{label}: typical_price must be numeric")
        link = str(r.get("amazon_link") or "").strip()
        asin = str(r.get("amazon_asin") or "").strip().upper()
        if bool(link) != bool(asin):
            errs.append(f"{label}: amazon_link and amazon_asin must both be set or both blank")
        if asin:
            if not ASIN_RE.fullmatch(asin):
                errs.append(f"{label}: invalid ASIN {asin!r}")
            else:
                want = f"https://www.amazon.com/dp/{asin}"
                if link.split("?")[0].rstrip("/") != want:
                    errs.append(f"{label}: amazon_link must be canonical ({want})")
                r["amazon_asin"], r["amazon_link"] = asin, want
        # Provenance: required by the prompt, unchecked by the original validator.
        if not [u for u in (r.get("source_links") or []) if str(u).strip()]:
            errs.append(f"{label}: source_links is empty — a claim we cannot trace")

    crit = data.get("ranking_criteria")
    if not isinstance(crit, list) or not crit:
        errs.append("ranking_criteria must be a non-empty list")
    else:
        total = sum(float(c.get("weight", 0)) for c in crit)
        if abs(total - 1.0) > 0.001:
            errs.append(f"ranking weights must total 1.0; found {total:.3f}")
    return errs


# --------------------------------------------------------------------------- #
#  Enrichment — what needs BCC's stack
# --------------------------------------------------------------------------- #

def _asin_from_corpus(conn, title: str, manufacturer: str) -> tuple:
    """Has a reviewer we already hold linked this product? -> (asin, [reviewers]).

    The strongest blank-filler: an ASIN a named publisher put in its own buy link, already in
    our review store. Matched on brand plus the distinctive words of the title.
    """
    toks = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", title) if len(w) > 3][:4]
    if not toks:
        return "", []
    sql = ("SELECT p.asin, r.reviewer, p.name FROM review_products p "
           "JOIN reviews r ON r.review_id = p.review_id "
           "WHERE COALESCE(p.asin,'') <> '' AND lower(p.name) LIKE ?")
    like = f"%{(manufacturer or toks[0]).lower()}%"
    hits: dict[str, list] = {}
    for asin, reviewer, name in conn.execute(sql, (like,)):
        low = name.lower()
        if sum(1 for t in toks if t in low) >= max(2, len(toks) - 2):
            hits.setdefault(asin, [])
            if reviewer not in hits[asin]:
                hits[asin].append(reviewer)
    if not hits:
        return "", []
    best = max(hits.items(), key=lambda kv: len(kv[1]))
    return best[0], best[1]


def enrich(data: dict, *, use_network: bool = True) -> dict:
    """Attach identity + owner evidence to every ranked row. Returns a report."""
    report = {"verified": [], "rejected": [], "filled": [], "scored": [], "notes": []}
    conn = sqlite3.connect(DB)
    try:
        from intake.products import amazon_rainforest as az, amazon_widget as aw
        from realrank_index import realrank_index, polarization
    except Exception as e:                                     # pragma: no cover
        report["notes"].append(f"enrichment unavailable: {e}")
        return report

    for label, r in rows_of(data):
        title = f"{r.get('manufacturer','')} {r.get('product_title','')}".strip()
        asin = str(r.get("amazon_asin") or "").strip().upper()

        # 1. Fill a blank ASIN from our own review corpus first (free, and the strongest
        #    evidence: a publisher's own buy link for this product).
        if not asin:
            found, who = _asin_from_corpus(conn, r.get("product_title", ""),
                                           r.get("manufacturer", ""))
            if found:
                asin = found
                r["amazon_asin"] = asin
                r["amazon_link"] = f"https://www.amazon.com/dp/{asin}"
                r["asin_source"] = f"our review corpus ({', '.join(who)})"
                report["filled"].append(f"{label}: {asin} via {', '.join(who)}")

        if not asin or not use_network:
            continue

        # 2. Identity: is this listing actually the product named? Brand + type noun.
        try:
            listing = az.product_ratings(asin)
        except Exception as e:
            report["notes"].append(f"{label}: listing lookup failed ({e})")
            continue
        ltitle = listing.get("title") or ""
        # Compare on the manufacturer's LEADING TOKEN, not the whole string. Publishers write
        # parent companies and sub-brands in ("Staub (Zwilling)", "Lodge Cast Iron"), and
        # asking whether "staub (zwilling)" appears inside the listing's "STAUB" is backwards
        # — it flagged four correct rows.
        brand_raw = (r.get("manufacturer") or "").split("(")[0].strip().lower()
        brand = next((w for w in brand_raw.split() if len(w) >= 3), brand_raw)
        hay = f"{(listing.get('brand') or '').lower()} {ltitle.lower()}"
        brand_ok = (not brand) or brand in hay
        # Shared vocabulary: knows a cocotte IS a dutch oven while a bread oven is not, and
        # fails OPEN on anything ambiguous rather than dropping a legitimate product.
        type_ok, type_why = same_type(f"{title} {r.get('capacity','')}", ltitle)
        if not (brand_ok and type_ok):
            why = type_why if not type_ok else "brand does not match"
            r["identity_warning"] = (
                f"ASIN {asin} looks like a different product ({why}): {ltitle[:60]}")
            report["rejected"].append(f"{label}: {r['identity_warning']}")
            continue
        r["verified_title"] = ltitle
        report["verified"].append(f"{label}: {asin} — {ltitle[:56]}")

        # 2b. WHERE TO BUY — every retailer the reviews link, not just Amazon. A pick with
        # only an Amazon link earns nothing from a reader who buys at Williams-Sonoma, and
        # premium brands sell heavily direct. Tracking is stripped (a harvested link carries
        # the REVIEWER's affiliate tag — republished, it would pay them, not us) and
        # affiliate_url is left blank because our codes are applied at click time.
        try:
            from intake.products import buy_links as BL
            fam = {asin}
            try:
                pass  # offers match the EXACT asin; the family is for identity, not links
            except Exception:
                pass
            offers = BL.offers_from_reviews(conn, asins=fam,
                                            name_like=r.get("product_title", ""))
            # The model's own buy_link counts too, once cleaned — merged BY RETAILER so it
            # doesn't produce a second "Williams Sonoma" row differing only by a trailing
            # slash. The model's link wins for that retailer: it points at the exact size and
            # colour it ranked, where a review may link a sibling.
            own = BL.clean_url(r.get("buy_link") or "")
            if own:
                name = BL.retailer_name(own) or "source"
                existing = next((o for o in offers if o["retailer"] == name), None)
                if existing:
                    existing["url"] = own
                else:
                    offers.insert(0, {"retailer": name, "url": own,
                                      "affiliate_url": "", "seen_in": []})
            if offers:
                r["offers"] = offers
                report["offers"] = report.get("offers", [])
                report["offers"].append(
                    f"{label}: {len(offers)} retailer(s) — "
                    + ", ".join(o["retailer"] for o in offers[:5]))
        except Exception as e:
            report["notes"].append(f"{label}: buy-link gathering failed ({e})")

        # 3. Owner evidence: the real histogram, free, plus the index.
        try:
            h = aw.rating_histogram(asin)
        except Exception as e:
            report["notes"].append(f"{label}: histogram failed ({e})")
            continue
        if h.get("ok") and h.get("histogram"):
            r["owner_rating"] = h.get("avg_rating")
            r["owner_count"] = h.get("ratings_total")
            r["owner_histogram"] = h.get("histogram_pct")
            r["realrank_score"] = round(realrank_index(h["histogram"], h["ratings_total"]), 1)
            r["rating_shape"] = (polarization(h["histogram"]) or {}).get("label") or ""
            report["scored"].append(
                f"{label}: {r['owner_rating']}* x {r['owner_count']} -> {r['realrank_score']}"
                + (f" ({r['rating_shape']})" if r["rating_shape"] else ""))
        else:
            report["notes"].append(f"{label}: no histogram ({h.get('error','')})")
    conn.close()
    return report
