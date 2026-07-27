"""Where to buy — clean destinations now, our affiliate codes at click time.

THIS IS THE REVENUE PATH, and it has a trap in it.

Reviews are full of buy links, and 69 of the 139 we currently hold carry SOMEONE ELSE'S
affiliate tracking:

    https://amazon.com/dp/B0029JQEIC?tag=atkequipland-20        <- ATK's associate tag
    https://nytimes.com/wirecutter/out/link/7492/22112/4/112744  <- Wirecutter's redirector

Republishing those does not merely fail to earn — it pays a competitor on every sale we
generate. So a URL harvested from a review is treated as IDENTITY ONLY (which product, at
which retailer), never as a destination, and `clean_url` strips the tracking before we store
it (the rule already stated in product_model.RetailerOffer, now enforced).

We deliberately do NOT mint an affiliate URL here. The curator's decision: tags are assigned
at EXECUTION TIME — at the click — so attribution can be tracked per placement and per
session, and so a change of network doesn't require rewriting stored rows. `affiliate_url`
therefore stays empty and a renderer shows the clean destination; the click layer decorates
it later. Storing a baked-in tag would be the harder thing to undo.

Multiple retailers matter: a pick with only an Amazon link earns nothing from a reader who
buys at Williams-Sonoma, and some products (Le Creuset, Staub) sell far better direct.
"""
from __future__ import annotations

import json
import re
import sqlite3
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode, unquote

# Host -> display name. Reuses the map the product extractor already maintains rather than
# starting a second one; falls back to the bare host for anything unlisted.
try:
    from extract.markdown_to_product import _RETAILERS as _HOST_NAMES
except Exception:                                              # pragma: no cover
    _HOST_NAMES = {"amazon.com": "Amazon"}

# Tracking parameters, by prefix or exact name. Anything matching is removed.
_DROP_EXACT = {
    "tag", "linkcode", "linkid", "ascsubtag", "creative", "creativeasin", "camp",
    "ref", "ref_", "psc", "th", "smid", "qid", "sr", "keywords", "dib", "dib_tag",
    "content-id", "pd_rd_i", "pd_rd_r", "pd_rd_w", "pd_rd_wg", "pf_rd_i", "pf_rd_m",
    "pf_rd_p", "pf_rd_r", "pf_rd_s", "pf_rd_t", "irclickid", "irgwc", "clickid",
    "cjevent", "sourceid", "afsrc", "sid", "cm_mmc", "cm_sp", "gclid", "msclkid",
    "fbclid", "mc_cid", "mc_eid", "srsltid", "rtime", "redeem",
}
# Prefix families rather than a list of names — "cm_" alone covers cm_ven/cm_mmc/cm_sp/cm_re
# (Coremetrics, all over Williams-Sonoma), and one of them slipped through a name-only list.
_DROP_PREFIX = ("utm_", "aff", "partner", "impact", "pcrid", "trk", "sc_", "cm_", "cjc",
                "epik", "gad_", "gbraid", "wbraid", "ir_", "sscid")

# Affiliate redirectors: the real destination is inside, or one HTTP hop away.
_REDIRECTORS = ("trx-hub.com", "sovrn.co", "goto.walmart.com", "go.skimresources.com",
                "shop-links.co", "pntra.com", "pntrs.com", "dpbolvw.net", "anrdoezrs.net",
                "jdoqocy.com", "tkqlhce.com", "linksynergy.com", "/out/link/")

_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.I)


def is_redirector(url: str) -> bool:
    low = (url or "").lower()
    return any(m in low for m in _REDIRECTORS)


def unwrap(url: str) -> str:
    """Pull a real destination out of an affiliate wrapper WITHOUT a network call.

    Many wrappers carry the target percent-encoded in a query param (ATK's trx-hub uses
    `?q=<encoded amazon url>`). Opaque ones (Wirecutter's /out/link/) cannot be unwrapped
    offline — the caller falls back to the ASIN, or resolves the hop separately.
    """
    if not url:
        return ""
    try:
        qs = dict(parse_qsl(urlparse(url).query))
    except Exception:
        return url
    for key in ("q", "u", "url", "murl", "RD_PARM1", "destination"):
        v = qs.get(key) or ""
        if v.startswith("http"):
            return unquote(v)
    dec = unquote(url)
    m = re.search(r"https?://(?:www\.)?amazon\.[a-z.]+/[^\s\"'&]+", dec)
    return m.group(0) if m else url


def clean_url(url: str) -> str:
    """A canonical, publishable destination — every tracking parameter stripped.

    Returns "" when the link CANNOT be made safe. That is the important case: an opaque
    affiliate redirector (Wirecutter's /out/link/7492/22112/...) cannot be unwrapped without
    a network hop, and publishing it as-is would send the reader through Wirecutter's
    affiliate account and pay them for our sale. Refusing to emit a link we cannot clean is
    the only safe default — an offer we drop costs us one placement; an offer we mis-emit
    costs us the revenue AND funds a competitor.

    Amazon collapses to /dp/<ASIN>: the `ref=sr_1_7`, `dib=`, `qid=` cruft on a harvested
    link encodes the SEARCH POSITION it was found at, which is meaningless to a reader and
    stale by the next run.
    """
    if not url:
        return ""
    if is_redirector(url):
        url = unwrap(url)
        if is_redirector(url):          # still wrapped -> not publishable
            return ""
    try:
        u = urlparse(url)
    except Exception:
        return ""
    if not u.scheme.startswith("http"):
        return ""
    host = (u.hostname or "").lower()

    if "amazon." in host:
        m = _ASIN_RE.search(u.path) or _ASIN_RE.search(unquote(url))
        if m:
            return f"https://www.{host.lstrip('www.')}/dp/{m.group(1).upper()}"

    keep = [(k, v) for k, v in parse_qsl(u.query)
            if k.lower() not in _DROP_EXACT
            and not any(k.lower().startswith(p) for p in _DROP_PREFIX)]
    return urlunparse((u.scheme, u.netloc, u.path, "", urlencode(keep), ""))


def retailer_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().replace("www.", "") if url else ""
    if not host:
        return ""
    for h, name in _HOST_NAMES.items():
        if host == h or host.endswith("." + h):
            return name
    return host


def offers_from_reviews(conn: sqlite3.Connection, *, asins: set | None = None,
                        name_like: str = "") -> list:
    """Every distinct place to buy this product, gathered from the reviews we hold.

    Matched by ASIN FAMILY when we have one (a reviewer may have linked a different colour),
    else by name. Returns [{retailer, url, seen_in: [reviewers]}] with the reviewer's tracking
    stripped and `affiliate_url` deliberately absent — it is minted at click time.
    """
    asins = {a.upper() for a in (asins or set()) if a}
    rows = conn.execute(
        "SELECT p.asin, p.name, p.data, r.reviewer FROM review_products p "
        "JOIN reviews r ON r.review_id = p.review_id").fetchall()

    def _matches(asin, pname) -> bool:
        """Prefer the ASIN. A variant FAMILY is right for identity ("is this the same
        product?") but far too wide for offers: one Le Creuset family spans 61 ASINs across
        many reviewed sizes, and matching on it gathered 48 links for a single pick. Names
        are only consulted when we have no ASIN at all."""
        if asins:
            return (asin or "").upper() in asins
        if not name_like:
            return False
        toks = [t for t in re.findall(r"[A-Za-z0-9]+", name_like.lower()) if len(t) > 3][:4]
        low = (pname or "").lower()
        return bool(toks) and sum(1 for t in toks if t in low) >= max(2, len(toks) - 2)

    # ONE offer per retailer. A reader wants "buy at Amazon / Williams Sonoma / direct", not
    # nine Amazon links to sibling colours.
    by_retailer: dict[str, dict] = {}
    for asin, pname, dj, reviewer in rows:
        if not _matches(asin, pname):
            continue
        try:
            d = json.loads(dj or "{}")
        except Exception:
            continue
        for o in (d.get("retailer_offers") or []):
            if not isinstance(o, dict):
                continue
            url = clean_url(o.get("source_url") or o.get("affiliate_url") or "")
            if not url:                      # unresolvable redirector — never publish it
                continue
            name = o.get("retailer") or retailer_name(url) or "source"
            e = by_retailer.get(name)
            if e is None:
                e = by_retailer[name] = {"retailer": name, "url": url,
                                         "affiliate_url": "",   # minted at click time
                                         "seen_in": []}
            elif len(url) < len(e["url"]):   # prefer the tidier canonical link
                e["url"] = url
            if reviewer and reviewer not in e["seen_in"]:
                e["seen_in"].append(reviewer)

    # Amazon first (it is where most readers land), then alphabetical for stability.
    return sorted(by_retailer.values(),
                  key=lambda e: (e["retailer"] != "Amazon", e["retailer"]))
