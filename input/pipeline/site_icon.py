"""The icon a site declares for itself.

    find("smittenkitchen.com") -> "https://smittenkitchen.com/apple-touch-icon.png" or ""

Replaces the logo address the domain record used to carry: it pointed at a
third-party logo service that no longer exists (curator, 2026-11-24: "a logo
field ... that seems to go to a site that doesnt exist"). The icon comes from the
site's own home page: the largest <link rel="icon"> / apple-touch-icon it
declares, else /favicon.ico. Every candidate is requested once, and only an
address that answers with an image is returned: a blank is a correct answer.

Free: one direct home-page request and one or two small image requests. No paid
fetch is ever used for an icon.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept": "text/html,image/*,*/*;q=0.8"}


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


def _is_image(url: str, timeout: float) -> bool:
    import requests
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout, stream=True)
        ok = (r.status_code == 200
              and ("image" in (r.headers.get("content-type") or "").lower()
                   or url.lower().split("?")[0].endswith((".ico", ".png", ".svg", ".jpg", ".webp")))
              and "text/html" not in (r.headers.get("content-type") or "").lower())
        r.close()
        return ok
    except Exception:
        return False


def find(host: str, *, timeout: float = 8.0) -> str:
    """The site's own icon address, or '' when it declares none that loads."""
    import requests
    host = (host or "").strip().lower()
    if not host:
        return ""
    base = f"https://{host}/"
    candidates: list[str] = []
    try:
        r = requests.get(base, headers=_HEADERS, timeout=timeout)
        if r.status_code == 200 and r.text:
            base = r.url or base                       # follows www / redirects
            candidates = [u for _, u in _declared(r.text[:400_000], base)]
    except Exception:
        pass
    candidates.append(urljoin(base, "/favicon.ico"))
    seen = set()
    for url in candidates[:4] + candidates[-1:]:
        if url in seen:
            continue
        seen.add(url)
        if _is_image(url, timeout):
            return url
    return ""
