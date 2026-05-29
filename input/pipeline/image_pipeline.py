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


def process_thumbnail(raw: bytes) -> Optional[bytes]:
    """Process raw image bytes into a consistently-sized cookbook-grade
    JPEG. Output is ALWAYS either LANDSCAPE_TARGET or PORTRAIT_TARGET —
    every cooped image across the corpus uses one of two sizes.

    Pipeline:
      1. Open + EXIF-transpose (rotated phone photos straighten)
      2. Convert to RGB (drop alpha / paletted modes; JPEG requirement)
      3. Pick target bucket: landscape if source aspect >= threshold,
         else portrait
      4. ImageOps.fit center-crops + scales to target size (upscales
         if source is smaller — LANCZOS keeps it reasonable up to ~2x)
      5. Save as progressive JPEG at q=85, EXIF stripped

    None when Pillow can't open the input (HTML/SVG/junk response).
    """
    try:
        img = Image.open(io.BytesIO(raw))
        # ImageOps.exif_transpose handles rotated phone photos
        img = ImageOps.exif_transpose(img)
        # Convert to RGB to drop alpha / paletted modes; JPEG requires it
        if img.mode not in ("RGB", "L"):
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = bg
            else:
                img = img.convert("RGB")

        # Pick target bucket by source aspect ratio. Square-ish
        # inputs lean landscape — cookbook hero conventions.
        aspect = img.width / img.height if img.height else 1.0
        target = (LANDSCAPE_TARGET if aspect >= LANDSCAPE_ASPECT_THRESHOLD
                  else PORTRAIT_TARGET)

        # Center-crop + scale to fill target dimensions. ImageOps.fit
        # chooses the largest centered crop that matches the target
        # aspect, then resizes to exact target size. Upscales when
        # source is smaller; LANCZOS produces soft-but-acceptable
        # results up to ~2x.
        img = ImageOps.fit(img, target, method=Image.LANCZOS,
                           centering=(0.5, 0.5))

        out = io.BytesIO()
        img.save(out, format="JPEG",
                 quality=THUMB_JPEG_QUALITY,
                 optimize=True, progressive=True)
        return out.getvalue()
    except Exception as e:
        print(f"[image_pipeline] Pillow process failed: {e}")
        return None


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
