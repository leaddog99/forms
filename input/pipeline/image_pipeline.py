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

import requests
from PIL import Image, ImageOps

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


def _fetch_image_bytes(url: str) -> Optional[bytes]:
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
    q, land, port = THUMB_JPEG_QUALITY, LANDSCAPE_TARGET, PORTRAIT_TARGET
    try:
        from input.pipeline import system_config as cfg
        q = int(cfg.get_setting("image_jpeg_quality", q))
        land = _parse_dims(cfg.get_setting("image_landscape_target", None), land)
        port = _parse_dims(cfg.get_setting("image_portrait_target", None), port)
    except Exception:
        pass
    return q, land, port


def _open_oriented(raw: bytes) -> "Image.Image":
    """Open + apply EXIF orientation (straighten rotated phone photos)."""
    return ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))


def _to_rgb(img: "Image.Image") -> "Image.Image":
    """Flatten alpha / paletted modes onto white → RGB (JPEG requirement)."""
    if img.mode in ("RGB", "L"):
        return img
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        return bg
    return img.convert("RGB")


def _fit_and_encode(img, quality, land, port):
    """Center-crop+scale to the landscape/portrait bucket, encode progressive
    JPEG (EXIF stripped). Returns (bytes, out_w, out_h)."""
    aspect = img.width / img.height if img.height else 1.0
    target = land if aspect >= LANDSCAPE_ASPECT_THRESHOLD else port
    img = ImageOps.fit(img, target, method=Image.LANCZOS, centering=(0.5, 0.5))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue(), img.width, img.height


def process_thumbnail(raw: bytes, *, quality=None, landscape=None, portrait=None) -> Optional[bytes]:
    """Process raw image bytes into a consistently-sized cookbook-grade JPEG
    (one of two buckets), EXIF stripped. Config-driven (quality/targets) unless
    explicitly overridden. None when Pillow can't open the input."""
    try:
        q, land, port = _img_config()
        data, _w, _h = _fit_and_encode(
            _to_rgb(_open_oriented(raw)),
            quality if quality is not None else q,
            landscape if landscape is not None else land,
            portrait if portrait is not None else port,
        )
        return data
    except Exception as e:
        print(f"[image_pipeline] Pillow process failed: {e}")
        return None


def standardize_and_meta(raw: bytes, *, source_url: Optional[str] = None,
                         localized: bool = True) -> tuple[Optional[bytes], dict]:
    """Phase-1 capture step: standardize raw image bytes (config-driven) AND
    return an `imageMeta` block describing the result. The meta earns its keep —
    it drives quality warnings (too-small hero), variant-readiness, dedup, and
    the capture log. `bytes` is None if Pillow can't open the input (caller may
    fall back to storing raw)."""
    meta: dict = {"source_url": source_url, "localized": bool(localized),
                  "bytes_in": len(raw) if raw else 0}
    try:
        opened = Image.open(io.BytesIO(raw))
        meta["orig_format"] = ((opened.format or "").lower() or None)  # .format is lost after transpose
        src = ImageOps.exif_transpose(opened)
        meta["orig_width"], meta["orig_height"] = src.width, src.height
        q, land, port = _img_config()
        data, ow, oh = _fit_and_encode(_to_rgb(src), q, land, port)
        meta.update(width=ow, height=oh, format="jpeg", bytes=len(data),
                    orientation=("portrait" if oh > ow else "square" if oh == ow else "landscape"),
                    standardized=True)
        return data, meta
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
        key = f"{key_prefix}/{url_hash}.jpg"
        if store.exists(key):
            return store.url_for(key)
        raw = _fetch_image_bytes(url)
        if not raw:
            return None
        processed = process_thumbnail(raw)
        if not processed:
            return None
        return store.put(key, processed, content_type="image/jpeg",
                          meta=full_meta)

    # Content-hash variant: we have to process before keying
    raw = _fetch_image_bytes(url)
    if not raw:
        return None
    processed = process_thumbnail(raw)
    if not processed:
        return None
    c_hash = _content_hash(processed)
    key = f"{key_prefix}/{c_hash}.jpg"
    if store.exists(key):
        return store.url_for(key)
    return store.put(key, processed, content_type="image/jpeg",
                      meta=full_meta)
