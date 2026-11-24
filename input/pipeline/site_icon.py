"""The icon a site declares for itself.

    find("smittenkitchen.com") -> "https://smittenkitchen.com/apple-touch-icon.png" or ""

Replaces the logo address the domain record used to carry: it pointed at a
third-party logo service that no longer exists (curator, 2026-11-24: "a logo
field ... that seems to go to a site that doesnt exist"). The icon comes from the
site's own home page: the largest <link rel="icon"> / apple-touch-icon it
declares, else /favicon.ico.

Free first: one direct home-page request and one or two small image requests.
When the site REFUSES the direct request (403 / 429: smittenkitchen.com answers
a server 429 and a browser 200) and `paid=True`, the home page is read once
through the paid unblocker (curator, 2026-11-24: "use unblocker to get an
favicon if rejected"). The icon itself is never fetched through the paid route:
a browser loads it without trouble, so on a refusing site the address the home
page DECLARES is trusted unchecked. Only when the page declares none is one more
paid fetch spent, on /favicon.ico. A blank is a correct answer.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept": "text/html,image/*,*/*;q=0.8"}
_REFUSED = (401, 403, 406, 429, 503)
_IMG_EXT = (".ico", ".png", ".svg", ".jpg", ".jpeg", ".webp", ".gif")


def _declared(html: str, base: str) -> list[tuple[int, str]]:
    """(size, address) for every icon link on the page, largest first."""
    from bs4 import BeautifulSoup
    out = []
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return out
    for link in soup.find_all("link"):
        rel = " ".join(link.get("rel") or []).lower() if isinstance(link.get("rel"), list) \
            else str(link.get("rel") or "").lower()
        href = (link.get("href") or "").strip()
        if not href or "icon" not in rel or "mask-icon" in rel or href.startswith("data:"):
            continue
        nums = [int(n) for n in re.findall(r"(\d+)x\d+", str(link.get("sizes") or ""))]
        # No declared size: an apple-touch-icon is 180 by convention, a plain icon small.
        size = max(nums) if nums else (180 if "apple-touch" in rel else 32)
        if href.lower().split("?")[0].endswith(".svg"):
            size = max(size, 256)                      # scales to anything
        out.append((size, urljoin(base, href)))
    out.sort(key=lambda t: -t[0])
    return out


def _image_check(url: str, timeout: float):
    """True = answers with an image; False = does not; None = the site refused us."""
    import requests
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout, stream=True)
        ctype = (r.headers.get("content-type") or "").lower()
        status = r.status_code
        r.close()
    except Exception:
        return False
    if status in _REFUSED:
        return None
    return (status == 200 and "text/html" not in ctype
            and ("image" in ctype or url.lower().split("?")[0].endswith(_IMG_EXT)))


def _home_direct(base: str, timeout: float):
    """-> (html or '', final address, refused?)"""
    import requests
    try:
        r = requests.get(base, headers=_HEADERS, timeout=timeout)
    except Exception:
        return "", base, False
    if r.status_code == 200 and r.text:
        return r.text[:400_000], (r.url or base), False
    return "", base, r.status_code in _REFUSED


def _paid_get(url: str, log):
    """One paid fetch, no browser render (icon links are in the served HTML)."""
    try:
        from to_markdown.fetch import fetch_via_unblocker, unblocker_available
        if not unblocker_available():
            log("  [icon] paid fetch not configured")
            return None
        got = fetch_via_unblocker(url, render=False)
        return got[0] if got else None
    except Exception as e:  # noqa: BLE001
        log(f"  [icon] paid fetch failed: {type(e).__name__}: {e}")
        return None


def find(host: str, *, timeout: float = 8.0, paid: bool = False, log=print) -> str:
    """The site's own icon address, or '' when it has none we can establish.
    `paid=True` allows the paid unblocker when the site refuses the direct request."""
    host = (host or "").strip().lower()
    if not host:
        return ""
    base = f"https://{host}/"
    html, base, refused = _home_direct(base, timeout)
    via_paid = False
    if not html and refused and paid:
        resp = _paid_get(base, log)
        if resp is not None and getattr(resp, "status_code", 0) == 200 and resp.text:
            html, via_paid = resp.text[:400_000], True
            log(f"  [icon] {host}: home page read through the paid fetch")
    candidates = [u for _, u in _declared(html, base)] if html else []
    fallback = urljoin(base, "/favicon.ico")
    seen = set()
    for url in candidates[:4] + [fallback]:
        if url in seen:
            continue
        seen.add(url)
        ok = _image_check(url, timeout)
        if ok:
            return url
        if ok is None and url != fallback and via_paid:
            return url          # the page declares it; the site refuses only our check
    if (via_paid or (refused and paid)) and not candidates:
        # Nothing declared (or no page at all): is there a /favicon.ico? One paid fetch.
        resp = _paid_get(fallback, log)
        ctype = (getattr(resp, "headers", {}) or {}).get("content-type", "").lower() if resp is not None else ""
        if resp is not None and resp.status_code == 200 and "text/html" not in ctype and resp.content:
            log(f"  [icon] {host}: /favicon.ico confirmed through the paid fetch")
            return fallback
    return ""
