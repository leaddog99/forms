"""Page screenshot capture — Playwright headless Chromium → above-fold
view → process_thumbnail → image_store.

Why this exists:

The cooped og:image (input.pipeline.image_pipeline.coopt_image) gives
us a single hero photo per recipe — clean, consistent, designed for
sharing. But it's a portrait of THE DISH, not a portrait of THE PAGE.
For demo / cookbook / "this is a real source on a real site" framing,
a literal screenshot of the source carries different signal:
masthead + headline + hero photo + first paragraph of editorial =
"yes, this came from somewhere with editorial standards."

Capture details:
  - Headless Chromium via Playwright (sandbox/playwright/ already
    installed; same engine the bookmarklet runs in client-side).
  - Viewport 1500×900 — matches the corpus landscape target so the
    crop has minimal cropping at hero size.
  - Capture height: 800px (above-fold view, masthead through start of
    body).
  - Wait for `domcontentloaded` + a 1.5s settle for JS-rendered
    content (recipe widgets, ingredient/method blocks loading async).
  - Pillow center-crop via the same process_thumbnail pipeline used
    for cooped previews → exact 1500×1000 output. Visually
    indistinguishable from og:image thumbnails in the gallery.

Key shape:
  recipe-screens/<recipe_id>-<sha8>.jpg

The <sha8> is the first 8 chars of sha256(recipe_id + capture_ts).
Lets the same recipe be re-captured later without overwriting the
prior version. Useful for "source page changed since last capture"
forensics. The recipe_id PREFIX is what makes files traceable back
to recipes without the DB — the user's explicit ask from 2026-05-28.

Failures are silent. A failed capture leaves _source.pageScreenshot
empty; the UI just doesn't show the screenshot well for that row.
"""
from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from typing import Optional


# Viewport matches the landscape target so the captured image needs
# minimal cropping. Height 900 gives breathing room for the page to
# render before we cap at 800 for the capture window.
VIEWPORT_W = 1500
VIEWPORT_H = 900

# Capture window — top of page after settle. 800 keeps it clearly
# "above the fold" without missing the recipe title + intro on most
# sites. Then process_thumbnail center-crops to 1500×1000 final.
CAPTURE_HEIGHT = 800

# Settle delay after domcontentloaded — gives recipe widgets time to
# render. 1.5s is a sweet spot: enough for most JS-rendered content,
# not enough to wait out a paywall modal that'd ruin the shot.
SETTLE_MS = 1500

# Hard timeout per capture so a hung page can't stall the backfill.
NAV_TIMEOUT_MS = 25_000


def _key_for(recipe_id: str) -> str:
    """recipe-screens/<recipe_id>-<sha8 of ts>.jpg

    The sha8 component is what makes re-captures non-overwriting; the
    recipe_id PREFIX is the user's explicit ask for "file → recipe"
    traceability without the DB.
    """
    ts = datetime.now(timezone.utc).isoformat()
    salt = (recipe_id or "") + "|" + ts
    sha8 = hashlib.sha256(salt.encode("utf-8")).hexdigest()[:8]
    return f"recipe-screens/{recipe_id}-{sha8}.jpg"


def capture_screenshot(url: str, recipe_id: str, *,
                        viewport_w: int = VIEWPORT_W,
                        viewport_h: int = VIEWPORT_H,
                        capture_h: int = CAPTURE_HEIGHT,
                        ) -> Optional[str]:
    """Capture above-fold view of a URL with headless Chromium, run
    through process_thumbnail, store via image_store, return public URL.

    Returns None on any failure (Playwright launch failure, navigation
    timeout, processing failure, store failure). Caller stamps the
    returned URL on `_source.pageScreenshot` only if non-None.

    This is a SYNCHRONOUS call — wraps Playwright's sync API. Wall
    time per capture is dominated by page-load + settle (1.5s settle
    + however long the page takes to load, typically 2-5s). At
    ~4s/page wall, a 354-row backfill takes ~25 minutes.
    """
    if not url or not url.strip():
        return None
    if not recipe_id:
        return None

    # Run Playwright in a subprocess. The sync API can't be called
    # from a thread that lives inside uvicorn's asyncio context on
    # Windows — `sync_playwright()` raises NotImplementedError
    # because the parent's ProactorEventLoop can't spawn subprocess
    # children from worker threads. A fresh Python process has its
    # own event-loop policy and works cleanly. Trade-off: ~200ms
    # subprocess startup overhead per capture; at 2-3s/page total
    # it's noise.
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    worker_path = (_Path(__file__).resolve().parent.parent.parent
                   / "scripts" / "_capture_screenshot_worker.py")
    if not worker_path.exists():
        print(f"[screenshot] worker not found: {worker_path}")
        return None

    raw_bytes: Optional[bytes] = None
    try:
        result = subprocess.run(
            [
                _sys.executable, str(worker_path),
                url,
                str(viewport_w), str(viewport_h),
                str(capture_h),
                str(SETTLE_MS),
                str(NAV_TIMEOUT_MS),
            ],
            capture_output=True,
            timeout=(NAV_TIMEOUT_MS // 1000) + 15,  # buffer for browser+settle
        )
        if result.returncode != 0:
            print(f"[screenshot] worker exit {result.returncode} for "
                  f"{url!r}: {result.stderr.decode('utf-8', errors='replace')[:200]}")
            return None
        raw_bytes = result.stdout
    except subprocess.TimeoutExpired:
        print(f"[screenshot] worker timeout for {url!r}")
        return None
    except Exception as e:
        print(f"[screenshot] worker spawn failed: {e}")
        return None

    if not raw_bytes:
        return None

    # Normalize through the same Pillow pipeline used for cooped
    # og:image. Landscape source (1500×800) → exact 1500×1000 after
    # process_thumbnail's center-crop (the slight aspect difference
    # adds a thin matching padding band).
    try:
        from input.pipeline.image_pipeline import process_thumbnail
        processed = process_thumbnail(raw_bytes)
    except Exception as e:
        print(f"[screenshot] post-process failed: {e}")
        return None
    if not processed:
        return None

    # Store via the active backend. Key includes recipe_id prefix so
    # files trace back without the DB.
    try:
        from input.pipeline.image_store import get_image_store
        store = get_image_store()
        key = _key_for(recipe_id)
        meta = {
            "recipe_id": recipe_id,
            "source_url": url,
            "kind": "page-screenshot",
        }
        return store.put(key, processed,
                          content_type="image/jpeg", meta=meta)
    except Exception as e:
        print(f"[screenshot] store put failed: {e}")
        return None
