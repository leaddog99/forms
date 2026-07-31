"""host_gate.py — what the PUBLIC hostname is allowed to serve.

Two hostnames reach the same app through the same tunnel:

    bestcooksclub.com    customers        (public, no Cloudflare Access)
    recipes.tbotb.com    staff/curators   (admin; Access belongs HERE, only)

That split is the BCC/TBOTB architecture expressed in DNS, and it only works if
the app can tell the hostnames apart. Until 2026-07-30 it could not — both served
the full API, which is why putting Cloudflare Access on recipes.tbotb.com
protected nothing at all: the identical admin surface answered on
bestcooksclub.com with no login.

DENY BY DEFAULT. On a public host only the paths listed below exist; everything
else 404s (not 403 — a public visitor should not learn that /domains is a real
endpoint). The point is that a route someone forgets to gate is not automatically
public. This is a perimeter, NOT a replacement for _require_perm: staff hosts
still enforce permissions, and defence in depth means both.

Adding a customer feature means adding its path here. That is deliberate — the
failure mode is a visibly broken feature, never a silently exposed admin route.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

# Hosts treated as the customer-facing surface. Port and case are stripped before
# matching. Override with BCC_PUBLIC_HOSTS (comma-separated) — a self-hoster
# points their own domain here without touching code.
#
# bestcooks.club is the SAME customer product as bestcooksclub.com, not a second
# thing — an alias of the front door. It was missing here while home.html's own
# (now deleted) hostname test did count it as customer, which is the drift that
# made a single list worth having: this tuple is the only place the question is
# answered, so adding a customer domain is one edit and the front-door router
# follows automatically.
#
# Anything NOT listed is treated as the admin surface, so a new customer domain
# that is forgotten here serves the back office. Add both apex and www.
_DEFAULT_PUBLIC_HOSTS = (
    "bestcooksclub.com", "www.bestcooksclub.com",
    "bestcooks.club", "www.bestcooks.club",
)


def public_hosts() -> frozenset[str]:
    raw = os.environ.get("BCC_PUBLIC_HOSTS")
    if raw:
        return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())
    return frozenset(_DEFAULT_PUBLIC_HOSTS)


def is_public_host(host: Optional[str]) -> bool:
    if not host:
        return False
    return host.split(":")[0].strip().lower() in public_hosts()


# --- The allowlist ----------------------------------------------------------
# Each entry is (regex, allowed methods). The regex must match the WHOLE path.
# Ordered loosely by traffic. Keep the comments — the reason a path is public is
# the thing a reviewer needs, and it is what makes removing one safe later.

_ALLOW: tuple[tuple[str, frozenset[str]], ...] = (
    # --- app shell + assets ------------------------------------------------
    # /forms/ is allowed PAGE BY PAGE, and the list mirrors the `group: 'user'`
    # entries in library-shell.js NAV_ITEMS — the customer hamburger. That is the
    # split: two menus, and the public host serves exactly what the customer menu
    # can reach. Everything in the `group: 'admin'` burger (domains, dishes,
    # products, reviews, users, system, jobs, chapters, taxonomy…) is absent and
    # 404s here.
    #
    # KEEP THIS IN SYNC WITH NAV_ITEMS. A new customer page needs an entry here;
    # a new admin page needs nothing, and is private the moment it is written.
    #
    # Note the recipe form IS customer-facing — same page for both audiences.
    # What differs is privilege, not the file: master rows are read-only without
    # the edit_master permission, which _require_perm already enforces. The menu
    # is the affordance; the permission is the control.
    (r"/forms/recipe_form_styled\.html", frozenset({"GET"})),
    (r"/forms/install\.html", frozenset({"GET"})),       # the grab bookmarklet
    (r"/forms/cook\.html", frozenset({"GET"})),
    (r"/forms/[^/]+\.(css|js)", frozenset({"GET"})),     # shared shell assets
    (r"/r/.+", frozenset({"GET"})),                      # short permalink -> the form
    (r"/", frozenset({"GET"})),
    (r"/generated/.*", frozenset({"GET"})),             # hero + og images
    (r"/branding", frozenset({"GET"})),
    (r"/robots\.txt", frozenset({"GET"})),
    (r"/healthz", frozenset({"GET"})),           # liveness; leaks nothing
    (r"/status-messages", frozenset({"GET"})),          # the funny wait messages
    (r"/messages", frozenset({"GET"})),

    # --- identity ----------------------------------------------------------
    # Customers must be able to sign in, so /auth/login belongs here — it was
    # missed on the first pass, which left the public host with no way to
    # authenticate at all. /auth/master stays absent: that is the CURATOR
    # password, and it has no business being reachable from the customer domain.
    (r"/auth/me", frozenset({"GET"})),
    (r"/auth/login", frozenset({"POST"})),
    # Self-signup is the ONLY account-creation path that belongs on the public
    # host. POST /users is the admin create — it takes a role — and is absent
    # here on purpose.
    (r"/auth/signup", frozenset({"POST"})),
    # Verification links are CLICKED FROM A MAIL CLIENT, so they must resolve on
    # the customer hostname — the one the address belongs to. A link built from
    # public_base_url would point at the admin host and 404 for the person it was
    # sent to. Any path that appears in customer email needs an entry here;
    # otherwise the mail is fine and the link is dead on arrival.
    (r"/auth/verify", frozenset({"GET"})),
    (r"/auth/send-verification", frozenset({"POST"})),

    # --- a customer's own recipes ------------------------------------------
    (r"/recipes", frozenset({"GET", "POST"})),
    (r"/recipes/[^/]+", frozenset({"GET", "POST", "PATCH", "DELETE"})),
    (r"/recipes/[^/]+/.*", frozenset({"GET", "POST", "PATCH", "DELETE"})),
    (r"/recipes/measure", frozenset({"POST"})),
    (r"/recipes/similar-master", frozenset({"POST"})),  # "clone one from curated"
    (r"/claim-recipe.*", frozenset({"POST"})),

    # --- the grab-a-recipe bookmarklet + the form's own extract paths -------
    # The customer bookmarklet only. /extract-product and /extract-review are
    # curator tools and are deliberately absent.
    (r"/extract-from-url", frozenset({"POST"})),
    (r"/extract-from-image", frozenset({"POST"})),
    (r"/extract-from-pdf", frozenset({"POST"})),
    (r"/extract-from-markdown", frozenset({"POST"})),
    (r"/enrich-recipe", frozenset({"POST"})),
    (r"/stage-markdown", frozenset({"POST"})),
    (r"/staged-markdown.*", frozenset({"GET"})),
    (r"/stage-image", frozenset({"POST"})),
    (r"/staged-image.*", frozenset({"GET"})),
    (r"/images/.*", frozenset({"POST"})),               # own-recipe uploads
    (r"/url-metadata.*", frozenset({"GET"})),
    (r"/screenshot.*", frozenset({"GET"})),

    # --- the cook view: the end-user surface --------------------------------
    (r"/cook/.*", frozenset({"GET", "POST"})),

    # --- curated content, READ ONLY ----------------------------------------
    # Customers browse dishes/collections; they never write them. Writes fall
    # through to the deny and 404 on this host.
    (r"/dishes", frozenset({"GET"})),
    (r"/dishes/[^/]+", frozenset({"GET"})),
    (r"/dishes/[^/]+/(top|recipes)", frozenset({"GET"})),
    (r"/collections.*", frozenset({"GET"})),
    (r"/master-recipes.*", frozenset({"GET"})),
    (r"/product-catalog", frozenset({"GET"})),          # the affiliate product block
    (r"/product-facts.*", frozenset({"GET"})),
)

_COMPILED = tuple((re.compile(rf"^{pat}$"), meths) for pat, meths in _ALLOW)


def is_allowed(path: str, method: str) -> bool:
    """True iff `path`+`method` is on the customer allowlist."""
    m = (method or "GET").upper()
    if m in ("HEAD", "OPTIONS"):          # let CORS preflight and HEAD through
        m = "GET"
    for rx, meths in _COMPILED:
        if rx.match(path) and m in meths:
            return True
    return False


def blocked_reason(host: str, path: str, method: str) -> Optional[str]:
    """None when the request may proceed; a short log string when it must 404.
    Kept separate from the middleware so it is unit-testable without ASGI."""
    if not is_public_host(host):
        return None
    if is_allowed(path, method):
        return None
    return f"public-host block {host} {method} {path}"
