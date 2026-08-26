# URL normalization. One canonical form across the system so the metabase_url
# table key is stable and joins are trivial.

import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# A Wayback URL is a TRANSPORT, never an identity:
#   https://web.archive.org/web/20251011133422id_/https://site/path  ->  https://site/path
# The modifier after the timestamp (id_, im_, js_, cs_, if_ …) asks Wayback for
# the raw asset; it is part of how we fetched, not of what we fetched.
#
# THE CANONICAL COPY. Three near-identical regexes existed independently — in
# to_markdown/html_to_markdown.py, an inline `_canon` closure in save_recipe_api.py,
# and scripts/unwrap_wayback_urls.py — each written when a different consumer hit
# the same problem, and none of them covering the scoring path where it costs money.
_WAYBACK_RE = re.compile(
    r"^https?://web\.archive\.org/web/[^/]*?/(?P<orig>https?://.+)$",
    re.IGNORECASE,
)


def unwrap_wayback(url: str) -> str:
    """The publisher URL inside a Wayback snapshot URL; the input unchanged if
    it is not one. Idempotent, so it is safe to call at every layer."""
    m = _WAYBACK_RE.match((url or "").strip())
    return m.group("orig") if m else (url or "")


def is_wayback(url: str) -> bool:
    """True when `url` is a Wayback snapshot wrapper."""
    return bool(_WAYBACK_RE.match((url or "").strip()))

# Query params we always drop. Blocklist (not allowlist) so unfamiliar
# site-specific params like ?recipeId=42 survive.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer", "source",
    "igshid", "_ga", "yclid", "msclkid",
    # Google click/serving IDs — appended per-impression to organic results, so two
    # SERP hits of the SAME page differ only here and dodge dedup (seen: Google now
    # decorates organic results with srsltid, which fetched one page 4x in a batch).
    "srsltid", "gclsrc", "dclid", "gbraid", "wbraid", "gad_source", "_gl",
    # Dotdash Meredith newsletter/aggregator decoration (EatingWell, Food & Wine,
    # Serious Eats, MyRecipes — one publisher group, one param set). Worse than
    # the Google case above: `bvee` is a MILLISECOND TIMESTAMP, so every click
    # mints a unique URL and the same recipe would save as a NEW recipe each
    # time. Measured 2026-08-13 on a phone capture of eatingwell.com/
    # melting-zucchini-11798596, which arrived carrying all six and was stored
    # under a 300-character identity with its own screenshot row.
    "hid", "did", "lr_input", "lctg", "bvee", "kw",
}


def normalize_url(url: str) -> str:
    """Return a canonical form of `url`. Empty input returns empty string."""
    if not url:
        return ""
    raw = url.strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw

    scheme = (parsed.scheme or "https").lower()

    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if ":" in netloc:
        host, port = netloc.rsplit(":", 1)
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            netloc = host

    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    if parsed.query:
        kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                if k.lower() not in _TRACKING_PARAMS]
        query = urlencode(kept)
    else:
        query = ""

    # Drop fragment.
    return urlunparse((scheme, netloc, path, "", query, ""))


# Two-part public suffixes (co.uk-style). Without these, the naive last-two-labels
# parse returned the bare SUFFIX for e.g. deliciousmagazine.co.uk -> "co.uk", and
# deep-enrich then asked Moz for the DA of co.uk itself (=52), which the form saved
# as the publisher's DA (bug found 2026-08-26). Not a full PSL - a curated set
# covering the ccTLD patterns recipe publishers actually use; extend as new ones
# appear. Infrastructure constant, not domain data.
_TWO_PART_SUFFIXES = frozenset(
    f"{sld}.{tld}"
    for tld in ("uk", "au", "nz", "jp", "br", "in", "mx", "za", "il", "kr",
                "sg", "hk", "tw", "my", "ph", "th", "id", "tr", "ar", "co")
    for sld in ("co", "com", "org", "net", "ac", "gov", "edu")
) | frozenset({"ne.jp", "or.jp"})


def root_domain(url: str) -> str:
    """Return the registrable domain (public-suffix aware for common two-part
    ccTLD suffixes: bbc.co.uk -> bbc.co.uk, not co.uk). Empty if no host."""
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if ":" in netloc:
        netloc = netloc.split(":", 1)[0]
    parts = netloc.split(".")
    if len(parts) < 2:
        return netloc
    if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
