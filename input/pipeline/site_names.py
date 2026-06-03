"""Friendly publisher name resolution for recipe sources.

A recipe stores the human-readable site name on ``_source.siteName`` (see
``recipe_model.py``). The canonical source of that value is the page's own
``og:site_name`` meta tag, captured at extract time. But many save paths
carry no og-meta (bookmarklet / markdown / cache-served), and some pages
simply don't declare one — so the field was empty on the vast majority of
historical rows and fell back to the bare hostname in every list.

This module is the single resolver used everywhere a friendly name is
produced (save chokepoint, master-list endpoints, chapter snapshots,
backfill). Priority:

    captured og:site_name  >  domain master map  >  ""   (caller shows host)

The name map is owned by the ``domains`` table (the editable domain master —
see ``domains_lib``); ``domain_display_names.json`` is only its bootstrap
seed. BCC self-URLs resolve to "" so our own pages never masquerade as a
publisher.
"""

from urllib.parse import urlparse

# Our own surfaces — never show a publisher name for these. The recipe
# sidebar already suppresses the source link for BCC self-URLs; this keeps
# the stored field empty for them too.
_SELF_HOSTS = {
    "bestcooksclub.com",
    "recipes.tbotb.com",
    "yboc.ai",
    "localhost",
}


def _canon_host(host: str) -> str:
    """Lowercase, strip a leading dot and a single ``www.`` prefix."""
    h = (host or "").strip().lower().lstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return h


def _load_map() -> dict:
    # Cached {host: display_name} from the domains table (seeded from JSON
    # on first use). Lazy import keeps this module's import graph light for
    # callers like chapters.py.
    from input.pipeline.domains_lib import get_display_map
    return get_display_map()


def host_from_url(url: str) -> str:
    """Canonical hostname for a URL (no scheme, no ``www.``)."""
    if not url:
        return ""
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    try:
        return _canon_host(urlparse(u).hostname or "")
    except Exception:
        return ""


def friendly_site_name(captured: str = "", url: str = "") -> str:
    """Resolve the human-readable publisher name for a recipe source.

    ``captured`` is whatever already rode in on ``_source.siteName`` (the
    page's og:site_name when extraction parsed it). It wins when present.
    Otherwise the curated domain map is consulted. Returns "" for BCC
    self-URLs and for unknown domains with no captured name — callers fall
    back to the bare host.
    """
    cap = (captured or "").strip()
    host = host_from_url(url)
    if host in _SELF_HOSTS:
        return ""
    if cap:
        return cap
    return _load_map().get(host, "")
