"""Listing identity — does an Amazon listing name THIS product?

One scorer for every path an ASIN can enter a pick (the research model's own
memory, a corpus buy link, a Google result), and one candidate finder that
asks Google instead of Amazon.

Why (2026-09-07 audit, 223 live picks): every path checked brand plus a coarse
type noun and nothing else, so a same-brand SIBLING sailed through — Pastene
San Marzano DOP resolved to Pastene "Kitchen Ready" ground tomatoes, ChefAlarm
to the ThermoWorks DOT, a 2-piece carving set to a 6-piece BBQ set, San-J
Tamari to Tamari Splash. Six wrong buy links carried NO warning because
"tomatoes vs tomatoes" passes a type check. Amazon's own search was the fill
of last resort and ranks a brand's bestseller, not the named product; Google
scoped to amazon.com returned the right listing for all six (rank 1 for four).

The score is RECALL of the pick title's distinctive words in the listing title
— brand and filler stripped, units normalised — with the manufacturer's model
number decisive when it appears (KHM7210, TX-1100, DPC-9SS name ONE product
even across sister brands), the shared type vocabulary kept as a hard gate,
and accessory words (replacement, refill, bundle…) as a penalty. A missing
brand is a penalty, not a veto: Pulltex sells "Pulltap's".

Verdicts: verified (>= VERIFIED) · weak (>= WEAK, listed with a warning) ·
reject. A blank ASIN is a correct answer; a guess is not.
"""
from __future__ import annotations

import re

from intake.products.product_types import same_type

VERIFIED = 0.6
WEAK = 0.34

# Filler that never distinguishes one product from another.
_STOP = {
    "the", "and", "with", "for", "from", "of", "in", "by", "to", "a", "an",
    "new", "one", "size", "color", "colour", "set", "kit", "fl",
}
# Irregular plurals + publisher/listing synonyms that name the same thing.
_CANON = {
    "knives": "knife", "mill": "grinder", "mills": "grinder",
    "corkscrew": "opener", "corkscrews": "opener",
}
# Unit synonyms -> one token, so "5-Quart" == "5 qt" and "28-Ounce" == "28 oz".
_UNITS = {
    "quart": "qt", "quarts": "qt", "qt": "qt",
    "inch": "in", "inches": "in", "in": "in",
    "ounce": "oz", "ounces": "oz", "oz": "oz",
    "pound": "lb", "pounds": "lb", "lb": "lb", "lbs": "lb",
    "liter": "l", "litre": "l", "liters": "l", "litres": "l",
    "cup": "cup", "cups": "cup",
    "piece": "pc", "pieces": "pc", "pc": "pc", "pcs": "pc",
    "count": "ct", "ct": "ct",
    "gallon": "gal", "gallons": "gal", "gal": "gal",
}
# Words that mark a listing as a DIFFERENT object in the product's orbit.
# Deliberately short: "lid", "cover" and "holder" appear in honest titles
# ("with Lid", "Knife Block Holder") and penalised two correct picks.
_ACCESSORY = {
    "replacement", "refill", "refills", "bundle", "accessory", "accessories",
    "compatible", "duo",
}
# Sibling markers: the same product in another guise. Only ever a mild penalty.
_VARIANT = {
    "organic", "bulk", "lite", "light", "reduced", "unsalted", "mini", "travel",
    "sodium", "decaf", "refurbished", "renewed",
}
# Mutually exclusive forms: a conflict only when BOTH sides name one and they differ.
_FORM_FAMILIES = [
    {"whole", "ground", "crushed", "diced", "puree", "pureed", "paste", "chunky"},
    {"cordless", "corded"},
    {"digital", "analog"},
    {"electric", "manual"},
]
# Model-shaped: letters THEN digits (TP16, KHM7210, BHM800, TX-1100, F-737,
# DPC-9SS). Never digit-first — "2-Piece", "5-Quart", "12-Cup" are sizes, and
# "2PIECE" matched a Furi carving set to a Messermeister pick in testing.
_MODEL_TOKEN = re.compile(r"^[A-Z]{1,6}-?\d+[A-Z0-9-]*$")
_ASIN_IN_URL = re.compile(r"/(?:dp|gp/product|clp|gp/aw/d)/([A-Z0-9]{10})(?:[/?#]|$)")


def _norm_tokens(text: str) -> list:
    out = []
    for w in re.findall(r"[A-Za-z0-9]+", (text or "").replace("'s", "").replace("’s", "").lower()):
        w = _CANON.get(w, _UNITS.get(w, w))
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w in _STOP or (len(w) < 2 and not w.isdigit()):
            continue
        out.append(w)
    return out


def _tok_hit(t: str, have: set) -> bool:
    """Exact, or a stem the other side extends by a suffix: mix/mixer,
    grind/grinder, slice/slicer — never grip/grips-of-something-else."""
    if t in have:
        return True
    if len(t) >= 3:
        return any(h.startswith(t) and len(h) - len(t) <= 3 for h in have)
    return False


def _brand_token(manufacturer: str) -> str:
    raw = (manufacturer or "").split("(")[0].strip().lower()
    return next((w for w in raw.split() if len(w) >= 3), raw)


def clean_model_number(raw: str) -> str:
    """The stated model_number, or '' when it is not one. Listings hand back
    UPC/EAN codes (0075810032251) and internal strings (Wake.42.34.14.34.74836)
    in that field; a re-verify that trusted them matched the wrong listing to
    itself, and a Google query carrying one matched only that page."""
    s = (raw or "").strip()
    if not s or "." in s or " " in s.strip():
        return ""
    flat = re.sub(r"[^A-Z0-9]", "", s.upper())
    if len(flat) < 4 or (flat.isdigit() and len(flat) >= 11):
        return ""
    return s


def _model_keys(pick: dict) -> set:
    """Model-number strings that name one product: the stated model_number plus
    any model-shaped token in the title (TP16, BHM800, KHM7210, TX-1100)."""
    keys = set()
    mn = re.sub(r"[^A-Z0-9]", "", clean_model_number(pick.get("model_number") or "").upper())
    if mn:
        keys.add(mn)
    for tok in re.findall(r"[A-Za-z0-9-]+", pick.get("product_title") or ""):
        up = tok.upper()
        if _MODEL_TOKEN.match(up):
            keys.add(re.sub(r"[^A-Z0-9]", "", up))
    return keys


def identity_score(pick: dict, listing_title: str, listing_brand: str = "",
                   listing_model: str = "") -> dict:
    """-> {score, verdict, method, why}. `pick` carries manufacturer,
    product_title, capacity, model_number as the research model wrote them."""
    ltitle = listing_title or ""
    hay = f"{listing_brand or ''} {ltitle} {listing_model or ''}".lower()
    hay_flat = re.sub(r"[^A-Z0-9]", "", hay.upper())
    pick_title = f"{pick.get('manufacturer', '')} {pick.get('product_title', '')}".strip()

    # Hard gate: the shared type vocabulary (a stand mixer is not a hand mixer).
    type_ok, type_why = same_type(f"{pick_title} {pick.get('capacity', '')}", ltitle)
    if not type_ok:
        return {"score": 0.0, "verdict": "reject", "method": "type",
                "why": type_why}

    brand = _brand_token(pick.get("manufacturer", ""))
    # A pick with no brand cannot be verified against anything — the model
    # named a generic ("Greek Oregano") and any jar would pass. Unbranded is
    # scored like a brand miss, not like a match.
    brand_ok = bool(brand) and brand in hay
    brand_why = ("pick names no brand" if not brand
                 else f"brand '{brand}' not in listing" if not brand_ok else "")

    # Decisive: the manufacturer's model number in the listing.
    for key in _model_keys(pick):
        if key in hay_flat:
            if not brand_ok and len(key) < 5:
                continue        # a short code without the brand is not proof
            score = 1.0 if brand_ok else 0.9
            return {"score": score, "verdict": "verified", "method": "model",
                    "brand_ok": brand_ok, "why": f"model number {key} in listing"}

    brand_toks = set(_norm_tokens(pick.get("manufacturer", "")))
    want = [t for t in _norm_tokens(pick.get("product_title", "")) if t not in brand_toks]
    have = set(_norm_tokens(ltitle))
    if not want:
        score = 1.0 if brand_ok else 0.5
        return {"score": score, "verdict": "verified" if score >= VERIFIED else "weak",
                "method": "brand-only", "brand_ok": brand_ok,
                "why": "title carries no distinctive words" + (f"; {brand_why}" if brand_why else "")}
    hits = [t for t in want if _tok_hit(t, have)]
    score = len(hits) / len(want)
    why = f"{len(hits)}/{len(want)} distinctive words in listing"
    missing = [t for t in want if t not in hits]
    if missing:
        why += f" (missing: {', '.join(missing[:4])})"

    # Form conflict: whole vs ground, cordless vs corded…
    for fam in _FORM_FAMILIES:
        a = fam & set(want)
        b = fam & have
        if a and b and not (a & b):
            score *= 0.5
            why += f"; form conflict {'/'.join(a)} vs {'/'.join(b)}"
            break
    extra = _ACCESSORY & have
    if extra and not (extra & set(want)):
        score *= 0.5
        why += f"; listing looks like an accessory ({', '.join(sorted(extra))})"
    # Recall ignores EXTRA words, so the organic / low-sodium / bulk sibling
    # scores as high as the plain product it names. A variant marker on the
    # listing only is a mild penalty — enough to order siblings, not to reject.
    var = (_VARIANT & have) - set(want)      # markers the PICK did not ask for
    if var:
        score *= 0.8
        why += f"; listing is a variant ({', '.join(sorted(var))})"
    if not brand_ok:
        score *= 0.6
        why += f"; {brand_why}"

    score = round(score, 2)
    verdict = "verified" if score >= VERIFIED else ("weak" if score >= WEAK else "reject")
    return {"score": score, "verdict": verdict, "method": "title", "brand_ok": brand_ok,
            "why": why}


def asin_from_url(url: str) -> str:
    m = _ASIN_IN_URL.search(url or "")
    return m.group(1) if m else ""


def google_candidates(pick: dict, *, retailer: str = "amazon.com", want: int = 10) -> list:
    """Google, scoped to the retailer -> [{asin, title, rank}] in result order.

    The ASIN comes free from the result URL (/dp/, /gp/product/, /clp/); the
    result title is what the scorer reads. One SERP credit. Empty on any
    failure — the caller leaves the ASIN blank, it never guesses."""
    try:
        from input.pipeline.serp_search import serp_search
    except Exception:
        return []
    title = pick.get("product_title", "") or ""
    brand = pick.get("manufacturer", "") or ""
    if brand and title.lower().startswith(brand.lower()):
        brand = ""                                   # "Pastene Pastene …" once is enough
    q = " ".join(x for x in (brand, clean_model_number(pick.get("model_number", "")),
                             title) if x).strip()
    if not q:
        return []
    rows = []
    for attempt in (1, 2):
        try:
            rows = serp_search(f"{q} site:{retailer}", pages=1, want=want)
        except Exception as e:
            print(f"[identity] google lookup failed for {q!r}: {e}")
            return []
        if rows:
            break
        # Scale SERP's "unable to fulfil your request at this time, please
        # retry" is uncharged and transient; one retry recovered every case.
        import time
        time.sleep(2)
    out = _asin_rows(rows, retailer)
    if not out and clean_model_number(pick.get("model_number", "")):
        # A model number that came off a WRONG listing (verify fills a blank
        # from the listing) pins Google to that page or to nothing. Once more
        # without it.
        try:
            q2 = " ".join(x for x in (brand, title) if x).strip()
            out = _asin_rows(serp_search(f"{q2} site:{retailer}", pages=1, want=want), retailer)
        except Exception:
            pass
    elif len(out) < 3:
        # Page one was mostly search/category pages (long descriptive titles
        # do that). One more page, one more credit, before giving up.
        try:
            rows = serp_search(f"{q} site:{retailer}", pages=2, want=want * 2)
            out = _asin_rows(rows, retailer)
        except Exception:
            pass
    return out


def _asin_rows(rows: list, retailer: str) -> list:
    from urllib.parse import urlparse
    out, seen = [], set()
    for r in rows:
        link = r.get("link") or ""
        if retailer not in (urlparse(link).netloc or ""):
            continue
        # A localized page (/-/zh_TW/…) carries a translated title, but its
        # URL slug still spells the English product name — ChefAlarm's real
        # listing surfaced ONLY that way. Keep the row; the scorer reads the slug too.
        localized = bool(re.search(r"/-/[a-z]{2}(?:_[A-Z]{2})?/", link))
        m = re.search(r"amazon\.com/(?:-/[a-z]{2}(?:_[A-Z]{2})?/)?([^/?#]+)/(?:dp|gp/product)/", link)
        slug = m.group(1).replace("-", " ") if m and not _ASIN_IN_URL.match("/" + m.group(1)) else ""
        asin = asin_from_url(link)
        if not asin or asin in seen:
            continue
        seen.add(asin)
        out.append({"asin": asin, "title": "" if localized else (r.get("title") or ""),
                    "slug": slug, "rank": r.get("rank")})
    return out


def best_candidate(pick: dict, candidates: list) -> tuple:
    """Score every candidate — its result title AND its URL slug, best of the
    two — -> (candidate, score_info) for the best one that is not a reject,
    else (None, None). Ties go to the earlier rank."""
    best, best_info = None, None
    for c in candidates:
        info = None
        for text in (c.get("title", ""), c.get("slug", "")):
            if not text:
                continue
            i = identity_score(pick, text, c.get("brand", ""))
            if info is None or i["score"] > info["score"]:
                info = i
        if info is None or info["verdict"] == "reject":
            continue
        if best is None or info["score"] > best_info["score"]:
            best, best_info = c, info
    return best, best_info
