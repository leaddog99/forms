"""Image cooperation pipeline — fetch a remote image, normalize it,
store it locally (or S3), return the public URL.

Used by:
  - Extract path: when a recipe is extracted with an og:image, fetch +
    coopt it so the form displays our hosted thumbnail (not a hotlink
    that costs the source site bandwidth).
  - Backfill: walk existing master_recipes rows, coopt their previews.
  - Future bookmarklet path: capture client-side screenshots, upload
    raw bytes, route through `process_thumbnail` for consistent sizing.

Why coopt (vs. hotlink the og:image directly):

  1. Bandwidth — every TBOTB page view that displays an image hits
     the source's CDN. At any real traffic this becomes a problem for
     them AND for us (slow, unreliable, theirs to rate-limit at will).
  2. Permanence — source URLs change. We cache once at extract time
     and the recipe display stays stable for the row's lifetime.
  3. Performance — we control the size + format + cache headers.
  4. Legal positioning — we host a thumbnail we generated from their
     publicly-declared og:image; that's a derived work used as a link
     preview, vs. embedding their raw img URL.

Pillow processing:
  - Auto-orient via EXIF (some og:images come rotated)
  - EXIF stripped on output (privacy + smaller files)
  - Downscale to max 600px wide, preserving aspect ratio
  - JPEG quality 85, progressive
  - Convert any input (PNG / WebP / HEIC / etc.) to JPEG

Failures are silent: a failed coopt leaves `previewImage` empty and the
form falls back to whatever JSON-LD image URL exists. We never block
the extract on image processing.
"""
from __future__ import annotations

import hashlib
import io
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import requests

from input.pipeline.image_store import get_image_store


# Cookbook-grade target sizes — every cooped image lands as either
# landscape (3:2) or portrait (2:3), center-cropped to fill. Two
# sizes, used consistently across the corpus, give the dish + recipe
# pages a deliberate visual rhythm rather than a thrift-store
# collage of random aspect ratios.
#
# 3:2 is the cookbook standard (NYT Cooking, ATK, Bon Appétit, every
# Phaidon cookbook). 1500×1000 lands ~150-250KB at JPEG q=85 — large
# enough to look crisp at hero size (600px display × 2x retina = 1200px
# needed), small enough to ship over slow links.
#
# Center-crop preserves the photographic subject (food is almost always
# composed center-frame). Up-scaling is allowed via LANCZOS for sources
# < target size — produces soft results past 2x but acceptable for
# demo quality.
LANDSCAPE_TARGET = (1500, 1000)   # 3:2 landscape
PORTRAIT_TARGET = (1000, 1500)    # 2:3 portrait
# Aspect ratio threshold for picking landscape vs portrait. Square-ish
# (0.9-1.1) inputs get bucketed as landscape — slight horizontal lean
# matches cookbook conventions (square thumbnails read as "social
# media post," landscape reads as "editorial").
LANDSCAPE_ASPECT_THRESHOLD = 0.95   # source.width / source.height
THUMB_JPEG_QUALITY = 85
# PHASE 2 (2026-09-30, curator: "leave size as is and let webp compress"):
# the stored copy is WebP, at the quality f2n uses on BAILEY (its
# DISPLAY_QUALITY = 82, imagekit's default). Measured on 30 real thumbnails
# at the same 1500x1000 bucket: JPEG q85 avg 242 KB -> WebP q82 avg 158 KB,
# 65%. Every browser decodes WebP; the cookbook exporter does not read these.
# An existing .jpg is REUSED, never re-fetched: the key is a hash of the
# source URL, so the same picture would otherwise be stored twice.
THUMB_FORMAT = "WEBP"
THUMB_EXT = ".webp"
THUMB_CONTENT_TYPE = "image/webp"
THUMB_WEBP_QUALITY = 82
# Legacy alias — kept so any caller still reading THUMB_MAX_WIDTH gets
# the landscape width (effectively unchanged behavior for unaware
# callers).
THUMB_MAX_WIDTH = LANDSCAPE_TARGET[0]

# Sanity limit on download size — refuse images claiming to be huge
# before we read them all into memory. og:image is typically <500KB;
# anything over 10MB is a red flag (could be a misconfigured server
# sending a full uncompressed bitmap, or a malicious response).
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT_S = 15


def _fetch_image_via_unblocker(url: str) -> Optional[bytes]:
    """Download the image THROUGH the configured web unblocker (Oxylabs/etc.) — for anti-bot
    CDNs that 403 a direct image download even though the PAGE came through the unblocker
    (e.g. fnl-guide.com). render=False (an image needs no JS render). Best-effort: None when
    there are no unblocker creds, the bytes aren't an image, or anything fails."""
    try:
        from to_markdown.html_to_markdown import fetch_via_unblocker, unblocker_available
    except Exception:
        return None
    if not unblocker_available():
        return None
    try:
        res = fetch_via_unblocker(url, render=False)
        if not res:
            return None
        resp, _meta = res
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and not ctype.startswith("image/"):
            return None
        data = resp.content
        if not data or len(data) > MAX_DOWNLOAD_BYTES:
            return None
        print(f"[image_pipeline] fetched image via unblocker: {url!r}")
        return data
    except Exception as e:
        print(f"[image_pipeline] unblocker image fetch failed for {url!r}: {e}")
        return None


# Query strings that are a CDN RESIZE DIRECTIVE rather than an identifier. An
# og:image often points at a thumbnail-sized derivative, so rehosting the URL as
# given banks a postage stamp forever. Qiniu (`?imageView2/1/w/300/h/200/...`,
# used by chuimg/xiachufang) is the case that surfaced this; the width/height
# forms cover Cloudinary, imgix, WordPress and friends.
_RESIZE_QUERY_HINTS = ("imageview2", "imagemogr", "/w/", "w=", "width=",
                       "h=", "height=", "resize", "fit=", "size=")


def _full_size_variant(url: str) -> Optional[str]:
    """The same image without its resize query, when the query looks like a
    resize directive. None when there is nothing worth trying."""
    try:
        parts = urlsplit(url)
    except Exception:
        return None
    if not parts.query:
        return None
    q = parts.query.lower()
    if not any(h in q for h in _RESIZE_QUERY_HINTS):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _fetch_image_bytes(url: str) -> Optional[bytes]:
    """Direct browser-UA GET; on failure (an anti-bot CDN blocking the direct download)
    retry the download THROUGH the web unblocker so anti-bot publishers' images still rehost
    locally instead of leaving a blank/hotlinked remote URL.

    When the URL carries a resize directive, try the un-parameterized original
    FIRST and keep it only if it is genuinely bigger. Self-validating rather than
    pattern-trusting: a CDN that needs its query to serve anything at all just
    fails the probe and we fall through to the URL as given. Measured 2026-08-14
    on xiachufang — `?imageView2/1/w/300/h/200/q/75` yields 300x200 / 16 KB, the
    bare path 1080x1440 / 240 KB.
    """
    full = _full_size_variant(url)
    if full and full != url:
        cand = _fetch_image_direct(full)
        if cand and len(cand) > 0:
            small = _fetch_image_direct(url)
            if small is None or len(cand) > len(small):
                print(f"[image_pipeline] using full-size original "
                      f"({len(cand)}B vs {len(small) if small else 0}B): {full[:90]}")
                return cand
            return small
    direct = _fetch_image_direct(url)
    return direct if direct is not None else _fetch_image_via_unblocker(url)


def _fetch_image_direct(url: str) -> Optional[bytes]:
    """GET the image with a browser-shaped User-Agent (most CDNs allow
    image requests from a browser UA but block our bot string). Returns
    bytes on 2xx OR None on any failure / size violation."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "image/*,*/*;q=0.8",
    }
    try:
        # Stream + cap size as we read to avoid loading huge bytes
        # into memory for hostile servers.
        with requests.get(url, timeout=FETCH_TIMEOUT_S,
                          headers=headers, stream=True) as r:
            if not (200 <= r.status_code < 300):
                return None
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and not ctype.startswith("image/"):
                # Some servers return HTML errors with 200 — don't
                # pass garbage to Pillow.
                return None
            cl = r.headers.get("Content-Length")
            if cl and int(cl) > MAX_DOWNLOAD_BYTES:
                return None
            buf = io.BytesIO()
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                buf.write(chunk)
                if buf.tell() > MAX_DOWNLOAD_BYTES:
                    return None
            return buf.getvalue()
    except Exception as e:
        print(f"[image_pipeline] fetch failed for {url!r}: {e}")
        return None


def _parse_dims(s, default):
    """'1500x1000' -> (1500, 1000); falls back to default on anything odd."""
    try:
        w, h = str(s).lower().split("x")
        return (int(w), int(h))
    except Exception:
        return default


def _img_config():
    """Standardization knobs (quality + target buckets) from the DB system
    config (cached), falling back to the module defaults when config/DB isn't
    available (early boot, tests). Keeps the bake parameters out of code so a
    portable instance can tune them in the System admin (memory/project_system_config)."""
    q, land, port = THUMB_WEBP_QUALITY, LANDSCAPE_TARGET, PORTRAIT_TARGET
    try:
        from input.pipeline import system_config as cfg
        q = int(cfg.get_setting("image_webp_quality", q))
        land = _parse_dims(cfg.get_setting("image_landscape_target", None), land)
        port = _parse_dims(cfg.get_setting("image_portrait_target", None), port)
    except Exception:
        pass
    return q, land, port


# ---------------------------------------------------------------------------
# PIXELS: imagekit (sibling repo, `pip install -e ../imagekit`), adopted
# 2026-09-30 per imagekit/docs/adopting-in-recipes.md. It replaced four private
# helpers here (_open_oriented / _to_rgb / _fit_and_encode / _contain_and_encode)
# that were one of EIGHT copies of decode-validate-resize-encode across the two
# apps, with quality values that disagreed. imagekit owns: EXIF orientation
# (always, before anything), alpha flattening onto a background when the target
# is opaque, metadata stripping, a never-upscale `contain`, a cover crop, a
# pixel-bomb cap and a format allow-list. Storage, URLs and the landscape /
# portrait BUCKET choice stay here - those are product decisions.
#
# PHASE 1 (this): same OUTPUT as before - progressive JPEG at the configured
# quality, 1500x1000 / 1000x1500 cover for the corpus, contain-to-max_px for a
# hero - only the engine changed. PHASE 2 (a separate decision): WebP + a
# display size; that touches every stored file's extension and the manifest.
# ---------------------------------------------------------------------------
def _bucket_for(width: int, height: int, land, port):
    """Landscape or portrait target box for a source of this shape. The
    product rule, unchanged: aspect >= LANDSCAPE_ASPECT_THRESHOLD -> landscape."""
    aspect = width / height if height else 1.0
    return land if aspect >= LANDSCAPE_ASPECT_THRESHOLD else port


def _hero_max_px():
    """Longest-edge cap for a user's hero image (config-driven, default 1600)."""
    try:
        from input.pipeline import system_config as cfg
        return int(cfg.get_setting("image_hero_max_px", 1600))
    except Exception:
        return 1600


def _probe(raw: bytes):
    """imagekit.inspect with THIS app's limits (the 10 MB download guard kept;
    the pixel cap and format allow-list are new, deliberately). Raises
    imagekit.ImageRejected (a ValueError) with a message written for a person."""
    from imagekit import inspect as ik_inspect
    return ik_inspect(raw, max_bytes=MAX_DOWNLOAD_BYTES)


def process_thumbnail(raw: bytes, *, quality=None, landscape=None, portrait=None) -> Optional[bytes]:
    """Process raw image bytes into a consistently-sized cookbook-grade WebP
    (one of two buckets), EXIF stripped. Config-driven (quality/targets) unless
    explicitly overridden. None when the input is not a usable image."""
    try:
        from imagekit import derive
        q, land, port = _img_config()
        _probe(raw)                       # the guards: not-an-image, pixel bomb, format
        # The bucket is chosen from the ORIENTED shape - what a person sees -
        # not the stored header's: a phone photo saved sideways with an EXIF
        # rotation is a portrait, and the old code (exif_transpose first) got
        # that right. A cheap contain pass at the source's own size reports
        # the oriented dimensions without resampling anything.
        shape = derive(raw, width=10 ** 6, fit="contain", fmt="JPEG", quality=30,
                       keep_alpha=False)
        tw, th = _bucket_for(shape.source_width, shape.source_height,
                             landscape if landscape is not None else land,
                             portrait if portrait is not None else port)
        # cover = centre-crop-and-fill to exactly the box, upscaling if the
        # source is smaller - what ImageOps.fit did. keep_alpha=False
        # composites onto white, what _to_rgb did.
        d = derive(raw, width=tw, height=th, fit="cover", fmt=THUMB_FORMAT,
                   quality=quality if quality is not None else q, keep_alpha=False)
        return d.data
    except Exception as e:
        print(f"[image_pipeline] imagekit process failed: {e}")
        return None


def standardize_and_meta(raw: bytes, *, source_url: Optional[str] = None,
                         localized: bool = True) -> tuple[Optional[bytes], dict]:
    """Phase-1 capture step: standardize raw image bytes (config-driven) AND
    return an `imageMeta` block describing the result. The meta earns its keep -
    it drives quality warnings (too-small hero), variant-readiness, dedup, and
    the capture log. `bytes` is None if the input is not a usable image (caller
    may fall back to storing raw)."""
    meta: dict = {"source_url": source_url, "localized": bool(localized),
                  "bytes_in": len(raw) if raw else 0}
    try:
        from imagekit import derive
        probe = _probe(raw)
        meta["orig_format"] = (probe.format or "").lower() or None
        q, _land, _port = _img_config()
        # Hero image: CONTAIN (preserve whole image + orientation, no crop, no
        # upscale) - not the corpus crop bucket. A small paste stays its own size.
        # The longest-edge cap: imagekit's `width` is a ceiling on width only, so
        # a tall portrait gets its height capped by also passing height.
        cap = _hero_max_px()
        d = derive(raw, width=cap, height=cap, fit="contain", fmt=THUMB_FORMAT,
                   quality=q, keep_alpha=False)
        # The ORIENTED source size (what the user sees), not the stored header's.
        meta["orig_width"], meta["orig_height"] = d.source_width, d.source_height
        ow, oh = d.width, d.height
        meta.update(width=ow, height=oh, format=THUMB_FORMAT.lower(), bytes=len(d.data),
                    orientation=("portrait" if oh > ow else "square" if oh == ow else "landscape"),
                    upscaled=False, standardized=True)
        return d.data, meta
    except Exception as e:
        print(f"[image_pipeline] standardize failed: {e}")
        meta["standardized"] = False
        return None, meta


def _content_hash(data: bytes) -> str:
    """8-char prefix of the sha256. Short enough for tidy URLs,
    long enough to avoid collisions at the scale we're at (8 hex chars
    = ~4B namespace)."""
    return hashlib.sha256(data).hexdigest()[:16]


def coopt_image(url: str, *,
                 key_prefix: str = "og-thumbs",
                 reuse_by_url_hash: bool = True,
                 manifest_meta: Optional[dict] = None) -> Optional[str]:
    """Full pipeline: fetch → process → store → return public URL.

    Keying strategy:
      - `reuse_by_url_hash=True` (default): key = "{prefix}/{sha8 of url}.jpg"
        — two recipes that reference the same og:image share one
        thumbnail. Cheap dedup.
      - `reuse_by_url_hash=False`: key includes a content hash of the
        processed bytes instead, so visually-identical thumbnails from
        different URLs dedup too. More expensive (must process first).

    Idempotent: if the store reports the key already exists, we skip
    the fetch+process and just return the URL.
    """
    if not url or not url.strip():
        return None
    store = get_image_store()

    # Default manifest meta lets backfills + saves attribute files to
    # recipes even when nobody passed explicit meta. Always include
    # the source URL so a future audit can reverse-engineer "where
    # did this image come from."
    full_meta = {"source_url": url}
    if manifest_meta:
        full_meta.update(manifest_meta)

    if reuse_by_url_hash:
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        # Either extension counts as "already have it": the 11k JPEGs stored
        # before the WebP switch stay valid and are never re-fetched.
        for ext in (THUMB_EXT, ".jpg"):
            if store.exists(f"{key_prefix}/{url_hash}{ext}"):
                return store.url_for(f"{key_prefix}/{url_hash}{ext}")
        key = f"{key_prefix}/{url_hash}{THUMB_EXT}"
        raw = _fetch_image_bytes(url)
        if not raw:
            return None
        processed = process_thumbnail(raw)
        if not processed:
            return None
        return store.put(key, processed, content_type=THUMB_CONTENT_TYPE,
                          meta=full_meta)

    # Content-hash variant: we have to process before keying
    raw = _fetch_image_bytes(url)
    if not raw:
        return None
    processed = process_thumbnail(raw)
    if not processed:
        return None
    c_hash = _content_hash(processed)
    key = f"{key_prefix}/{c_hash}{THUMB_EXT}"
    if store.exists(key):
        return store.url_for(key)
    return store.put(key, processed, content_type=THUMB_CONTENT_TYPE,
                      meta=full_meta)
