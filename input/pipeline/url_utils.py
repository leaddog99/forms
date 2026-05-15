# URL normalization. One canonical form across the system so the metabase_url
# table key is stable and joins are trivial.

from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# Query params we always drop. Blocklist (not allowlist) so unfamiliar
# site-specific params like ?recipeId=42 survive.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer", "source",
    "igshid", "_ga", "yclid", "msclkid",
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


def root_domain(url: str) -> str:
    """Return the registrable domain (last two host parts). Empty if no host."""
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
    return ".".join(parts[-2:]) if len(parts) >= 2 else netloc
