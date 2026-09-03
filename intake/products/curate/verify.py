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
from input.pipeline.db import connect as db_connect
import sys
from typing import Any

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "..", ".."))
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
    ec = data.get("editors_choice")
    if isinstance(ec, dict):
        # The curator-pinned pick gets the SAME identity/owner/offer machinery as every
        # ranked row — analysis parity is the point; only its provenance label differs.
        out.append(("editors-choice", ec))
    return out


def flag_offclass_titles(data: dict, product_class: str, terms: list) -> int:
    """Deterministic on-class title gate (curator suggestion, 2026-09-03): every
    ranked pick's title should name the class — or one of the collection's
    fallback search_terms — with singular/plural fuzz. A miss stamps a visible
    `identity_warning` (FLAG, never delete: 'Stainless Steel Canner with Rack'
    IS a water-bath canner whose title omits the phrase, while the Presto
    'Multi-Cooker/Canner' contains 'canner' and is NOT one — titles alone can't
    convict or acquit, so the flag points the curator's eye and the prompt's
    CLASS BOUNDARY carries the judgment). Editors-choice rows are exempt — the
    curator pinned those personally. Returns the number flagged."""
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()

    def _variants(phrase: str) -> set:
        words = phrase.split()
        if not words:
            return set()
        outs = {phrase}
        last = words[-1]
        if last.endswith("s"):
            outs.add(" ".join(words[:-1] + [last[:-1]]))
        else:
            outs.add(" ".join(words[:-1] + [last + "s"]))
        return outs

    pats: set = set()
    for a in [product_class] + [t for t in (terms or []) if (t or "").strip()]:
        pats |= _variants(_norm(a))
    flagged = 0
    for label, r in rows_of(data):
        if label == "editors-choice":
            continue
        title = " " + _norm(r.get("product_title")) + " "
        if any(p and f" {p} " in title for p in pats):
            continue
        note = (f"title does not name the class {product_class!r}"
                + (" or any fallback term" if terms else "")
                + " — verify this is on-class, not a neighbor or accessory")
        prev = (r.get("identity_warning") or "").strip()
        r["identity_warning"] = (prev + "; " if prev else "") + note
        flagged += 1
    if flagged:
        print(f"[CURATE] on-class title gate: {flagged} pick(s) flagged")
    return flagged


# --------------------------------------------------------------------------- #
#  Shape — the curator's original contract
# --------------------------------------------------------------------------- #

def _check_requested(data: dict, grouped: dict) -> list:
    """Did we get back the categories that were ASKED FOR?

    Categories are a staff input (prompt.normalize_categories), and `research` stamps the
    requested list onto the result. Checking the delivered set against it is what makes that
    input binding: without it, a category the model renamed, quietly dropped for want of a
    third product, or invented for itself all render identically in the finished brief.

    An EMPTY requested list is a request, not a missing one — it means "rank the whole class"
    — so it is enforced too, and any category that turns up is one nobody asked for. Only an
    ABSENT field skips the check: a JSON produced by hand or in ChatGPT is still a valid input
    to `brief`, it simply carries no record of the request to check against.
    """
    requested = data.get("categories_requested")
    if requested is None:
        return []
    want = [str(c).strip() for c in requested if str(c).strip()]
    by_key = {k.strip().lower(): k for k in grouped if str(k).strip()}
    missing = [c for c in want if c.lower() not in by_key]
    extra = [by_key[k] for k in by_key if k not in {c.lower() for c in want}]

    errs = []
    if missing:
        errs.append("requested categories missing from the result: "
                    + ", ".join(repr(c) for c in missing))
    if extra:
        errs.append("categories nobody asked for: " + ", ".join(repr(c) for c in extra))
    # Same category, different spelling — take the staff wording, which is what gets printed.
    for c in want:
        delivered = by_key.get(c.lower())
        if delivered and delivered != c:
            for r in grouped[delivered]:
                r["category"] = c
    return errs


def check_independent_sources(data: dict) -> list:
    """The Bean Pot lesson (2026-09-03): a pick ranked #3 on the strength of the
    MANUFACTURER'S OWN product page — rule 12 required source_links, nothing
    required them independent. Deterministic backstop under prompt rule 12b:
    a ranked row whose every source_link is self-referential — same host as its
    own buy_link/amazon_link, any amazon domain, or a host whose name contains
    the manufacturer's name (squashed) — has NO independent evidence and errors.
    Heuristic on purpose: an unrecognized independent host passes (the prompt
    carries the judgment); the common self-citation shapes cannot.
    Editors-choice rows are exempt — curator provenance, honesty rules apply."""
    def _host(u: str) -> str:
        m = re.search(r"https?://([^/]+)", str(u or "").lower())
        return (m.group(1) if m else "").removeprefix("www.")

    def _squash(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())

    errs = []
    for label, r in rows_of(data):
        if label == "editors-choice":
            continue
        links = [u for u in (r.get("source_links") or []) if str(u or "").strip()]
        if not links:
            continue  # rule-12 emptiness is validate_shape/enrich territory
        own_hosts = {h for h in (_host(r.get("buy_link")), _host(r.get("amazon_link"))) if h}
        maker = _squash(r.get("manufacturer"))
        independent = []
        for u in links:
            h = _host(u)
            if not h or h in own_hosts or "amazon." in h:
                continue
            if maker and len(maker) >= 5 and maker[:12] in _squash(h):
                continue  # the maker's own site under another spelling
            independent.append(u)
        if not independent:
            errs.append(f"{label} ({r.get('product_title')!r}): every source_link is the "
                        f"product's own maker/retailer page — a rank requires at least one "
                        f"independent review source, or the slot goes to omitted_slots")
    return errs


def _declared_omissions(data: dict) -> tuple:
    """(set of (section_lower, place), errors) from `omitted_slots` — THE HONEST GAP
    (2026-09-03, the Water-Bath-Canner padding): a ranking place may be unfilled ONLY
    when the model declares it with a reason. An undeclared shortfall still fails (a
    truncated reply must not masquerade as an honest gap), a declared-but-filled place
    is a contradiction, and place 1 can never be omitted — a section with no winner at
    all is not a section."""
    errs, declared = [], set()
    slots = data.get("omitted_slots") or []
    if not isinstance(slots, list):
        return set(), ["omitted_slots must be a list"]
    for s in slots:
        if not isinstance(s, dict):
            errs.append("omitted_slots entries must be objects")
            continue
        section = str(s.get("section") or "").strip().lower()
        try:
            place = int(s.get("place", 0))
        except (TypeError, ValueError):
            place = 0
        if place not in (2, 3):
            errs.append(f"omitted slot {s.get('section')!r}#{s.get('place')}: only places "
                        f"2 and 3 may be omitted — place 1 is the section's reason to exist")
            continue
        if not str(s.get("reason") or "").strip():
            errs.append(f"omitted slot {s.get('section')!r}#{place}: a reason is required")
            continue
        declared.add((section, place))
    return declared, errs


def _check_places(rows: list, declared: set, section: str, label: str) -> list:
    """Places 1..3 each filled by a row XOR declared omitted."""
    errs = []
    have = sorted(int(r.get("place", 0)) for r in rows)
    if len(set(have)) != len(have):
        return [f"{label}: duplicate places {have}"]
    key = section.strip().lower()
    for place in (1, 2, 3):
        filled = place in have
        omitted = (key, place) in declared
        if filled and omitted:
            errs.append(f"{label} place {place}: both ranked and declared omitted")
        elif not filled and not omitted:
            errs.append(f"{label} place {place}: missing — rank a product or declare "
                        f"the slot in omitted_slots with a reason")
    extra = [p for p in have if p not in (1, 2, 3)]
    if extra:
        errs.append(f"{label}: invalid places {extra}")
    return errs


def validate_shape(data: dict) -> list:
    """Structural errors. Empty list = the artifact may be built."""
    declared, errs = _declared_omissions(data)
    errs += check_independent_sources(data)
    overall = data.get("overall_top_three")
    if not isinstance(overall, list) or not overall:
        errs.append("overall_top_three must contain at least one ranked row")
    else:
        errs += _check_places(overall, declared, "", "overall")

    # An EMPTY category_rankings is valid: no categories were asked for, so the brief is the
    # overall three over the whole class. Whether that emptiness was REQUESTED is the separate
    # question _check_requested answers.
    cats = data.get("category_rankings")
    if not isinstance(cats, list):
        errs.append("category_rankings must be a list")
    elif not cats:
        errs += _check_requested(data, {})
    else:
        grouped: dict[str, list] = {}
        for r in cats:
            grouped.setdefault(str(r.get("category", "")).strip(), []).append(r)
        for cat, rows in grouped.items():
            if not cat:
                errs.append("a category row is missing its category name")
            else:
                errs += _check_places(rows, declared, cat, f"category {cat!r}")
        errs += _check_requested(data, grouped)

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

    # The pinned pick is a REQUEST like categories are: asked-for but absent is an error,
    # not a silent omission.
    if data.get("editors_choice_requested") and not isinstance(data.get("editors_choice"), dict):
        errs.append(f"editors_choice was requested "
                    f"({data['editors_choice_requested']!r}) but is missing from the result")

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


_FORM_WORDS = ("whole", "ground", "organic", "smoked", "sticks", "powder",
               "seeds", "dried", "fresh", "instant")


def _asin_from_search(r: dict) -> tuple:
    """Blank-ASIN recovery via ONE EasyParser Amazon search (1 credit).

    Built for the Nutmeg run (2026-08-31): every pick blank because the only
    prior recovery was our review corpus, which has no spice coverage. Query
    = brand + model number (the review-stated disambiguator, when present) +
    title + capacity. The FORM GUARD is the point: 'whole nutmeg' must never
    resolve to the ground jar — any form word in the pick title must appear
    in the listing title too. Step-2 identity verification still re-checks
    whatever this returns. -> (asin, note) or ("", "")."""
    from urllib.parse import quote_plus
    try:
        from intake.products import easyparser as ep
    except Exception:
        return "", ""
    q = " ".join(x for x in (r.get("manufacturer", ""), r.get("model_number", ""),
                             r.get("product_title", ""), r.get("capacity", "")) if x).strip()
    if not q:
        return "", ""
    try:
        res = ep.search_url(f"https://www.amazon.com/s?k={quote_plus(q)}", pages=1)
    except Exception:
        return "", ""
    if not res.get("ok"):
        return "", ""
    brand_raw = (r.get("manufacturer") or "").split("(")[0].strip().lower()
    brand = next((w for w in brand_raw.split() if len(w) >= 3), brand_raw)
    want = (r.get("product_title") or "").lower()
    want_forms = {f for f in _FORM_WORDS if f in want}
    for it in (res.get("items") or [])[:10]:
        t = (it.get("title") or "").lower()
        hay = f"{(it.get('brand') or '').lower()} {t}"
        if brand and brand not in hay:
            continue
        if any(f not in t for f in want_forms):
            continue
        asin = (it.get("asin") or "").strip().upper()
        if asin:
            return asin, (it.get("title") or "")[:60]
    return "", ""


def enrich(data: dict, *, use_network: bool = True) -> dict:
    """Attach identity + owner evidence to every ranked row. Returns a report."""
    report = {"verified": [], "rejected": [], "filled": [], "scored": [], "notes": []}
    conn = db_connect(DB)
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

        # 1b. Still blank and allowed online: ONE Amazon search (1 credit),
        #     form-guarded (whole vs ground). Step 2 re-verifies the result.
        if not asin and use_network:
            found, ltitle_note = _asin_from_search(r)
            if found:
                asin = found
                r["amazon_asin"] = asin
                r["amazon_link"] = f"https://www.amazon.com/dp/{asin}"
                r["asin_source"] = "amazon search"
                report["filled"].append(f"{label}: {asin} via amazon search — {ltitle_note}")

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
        # The listing lookup already carries the photo and often the
        # manufacturer's model number — keep both on the pick (the photo is
        # the row's thumbnail; the model number disambiguates brand siblings).
        # Research's own model_number wins; the listing only fills a blank.
        if listing.get("image"):
            r["image"] = listing["image"]
        if listing.get("model_number") and not (r.get("model_number") or "").strip():
            r["model_number"] = listing["model_number"]
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
