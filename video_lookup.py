"""Resolve a video URL the curator typed into the recipe form to a schema.org
VideoObject dict (the shape recipe_model.VideoObject stores).

One function, one chokepoint: YouTube and Vimeo go through their free oEmbed
endpoints (no key, no quota of note) for the title / author / thumbnail, and
the embed URL is DERIVED from the video id so an oEmbed hiccup never costs
the player. Anything else http(s) is kept as a bare contentUrl so a direct
.mp4 or a publisher's player page still round-trips; nothing is fetched for
those. Never fabricates: uploadDate stays "" when the provider does not say.

The thumbnail is hot-linked from the provider's own image host (i.ytimg.com /
vumbnail-style CDN), the same creator-sanctioned pattern the cook KB media
uses — never re-hosted (image_policy).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs, quote

import requests

_YT_HOSTS = {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "youtu.be"}
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_VIMEO_ID_RE = re.compile(r"/(?:video/)?(\d{6,12})(?:$|[/?#])")
_TIMEOUT = 6


def _host(u: str) -> str:
    h = (urlparse(u).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def youtube_id(url: str) -> str | None:
    """watch?v= · youtu.be/ · shorts/ · embed/ · live/ · v/ — else None."""
    try:
        p = urlparse(url)
    except Exception:
        return None
    if _host(url) not in _YT_HOSTS:
        return None
    cand = None
    if p.hostname and p.hostname.lower().endswith("youtu.be"):
        cand = p.path.strip("/").split("/")[0]
    else:
        q = parse_qs(p.query).get("v")
        if q:
            cand = q[0]
        else:
            m = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", p.path)
            if m:
                cand = m.group(1)
    return cand if cand and _YT_ID_RE.match(cand) else None


def vimeo_id(url: str) -> str | None:
    if _host(url) not in {"vimeo.com", "player.vimeo.com"}:
        return None
    m = _VIMEO_ID_RE.search(urlparse(url).path)
    return m.group(1) if m else None


def _oembed(endpoint: str, url: str) -> dict:
    try:
        r = requests.get(f"{endpoint}?url={quote(url, safe='')}&format=json", timeout=_TIMEOUT,
                         headers={"User-Agent": "Mozilla/5.0 (BCC recipe form)"})
        if r.ok:
            d = r.json()
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _obj(**kw) -> dict:
    base = {"@type": "VideoObject", "name": "", "contentUrl": "", "embedUrl": "",
            "thumbnailUrl": "", "uploadDate": "", "description": ""}
    base.update({k: (v or "") for k, v in kw.items()})
    return base


def resolve_video(url: str) -> dict:
    """Return a VideoObject dict for `url`, plus `provider` ('youtube' | 'vimeo' |
    'link') so the caller can tell an embeddable result from a plain link.
    Raises ValueError for a non-http(s) URL."""
    u = (url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise ValueError("a video link must start with http:// or https://")

    yid = youtube_id(u)
    if yid:
        watch = f"https://www.youtube.com/watch?v={yid}"
        oe = _oembed("https://www.youtube.com/oembed", watch)
        out = _obj(
            name=oe.get("title"),
            contentUrl=watch,
            embedUrl=f"https://www.youtube-nocookie.com/embed/{yid}",
            thumbnailUrl=oe.get("thumbnail_url") or f"https://i.ytimg.com/vi/{yid}/hqdefault.jpg",
            description=(f"by {oe['author_name']}" if oe.get("author_name") else ""),
        )
        out["provider"] = "youtube"
        return out

    vid = vimeo_id(u)
    if vid:
        page = f"https://vimeo.com/{vid}"
        oe = _oembed("https://vimeo.com/api/oembed.json", page)
        out = _obj(
            name=oe.get("title"),
            contentUrl=page,
            embedUrl=f"https://player.vimeo.com/video/{vid}",
            thumbnailUrl=oe.get("thumbnail_url"),
            description=(f"by {oe['author_name']}" if oe.get("author_name") else ""),
            uploadDate=oe.get("upload_date"),
        )
        out["provider"] = "vimeo"
        return out

    out = _obj(contentUrl=u)
    out["provider"] = "link"
    return out
