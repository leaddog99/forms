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
import os
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


# The five capture knobs above are the built-in DEFAULTS; each is overridable
# per-instance via a documented system_config key (screenshot_*), so a host can
# tune timing for a slow network or a different page shape WITHOUT a code change
# (portable-package principle: config defines the instance, not the code).
def _screenshot_cfg() -> dict:
    try:
        from input.pipeline import system_config as _cfg
        g = _cfg.get_setting
        return {
            "viewport_w":     int(g("screenshot_viewport_w", VIEWPORT_W)),
            "viewport_h":     int(g("screenshot_viewport_h", VIEWPORT_H)),
            "capture_height": int(g("screenshot_capture_height", CAPTURE_HEIGHT)),
            "settle_ms":      int(g("screenshot_settle_ms", SETTLE_MS)),
            "nav_timeout_ms": int(g("screenshot_nav_timeout_ms", NAV_TIMEOUT_MS)),
        }
    except Exception:
        return {"viewport_w": VIEWPORT_W, "viewport_h": VIEWPORT_H,
                "capture_height": CAPTURE_HEIGHT, "settle_ms": SETTLE_MS,
                "nav_timeout_ms": NAV_TIMEOUT_MS}


def _resolve_playwright_browsers_path() -> Optional[str]:
    """Where Playwright's headless-Chromium browsers live — resolved at RUNTIME so
    capture works regardless of HOW the app was launched and on ANY host, with no
    machine-specific service-env surgery (portable-package). A Windows service runs
    as LocalSystem, whose %LOCALAPPDATA% has no browsers, so we can't rely on the
    default lookup. Precedence:
      1. PLAYWRIGHT_BROWSERS_PATH already in the environment — honored as-is.
      2. system_config `playwright_browsers_path` — the documented per-instance knob.
      3. Auto-detect: scan the standard install dirs (incl. EVERY Windows user
         profile, since the service account can't see the launching user's) for a
         chromium-* build.
    Returns a dir containing chromium-* builds, or None (let Playwright default)."""
    import glob
    env = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if env and os.path.isdir(env):
        return env
    try:
        from input.pipeline import system_config as _cfg
        cfgp = os.path.expanduser((_cfg.get_setting("playwright_browsers_path", "") or "").strip())
        if cfgp and os.path.isdir(cfgp):
            return cfgp
    except Exception:
        pass
    candidates = [
        os.path.expanduser(os.path.join("~", "AppData", "Local", "ms-playwright")),   # Windows (per-user)
        os.path.expanduser(os.path.join("~", ".cache", "ms-playwright")),             # Linux
        os.path.expanduser(os.path.join("~", "Library", "Caches", "ms-playwright")),  # macOS
    ]
    if os.name == "nt":
        candidates += glob.glob(r"C:\Users\*\AppData\Local\ms-playwright")
    for c in candidates:
        try:
            if c and os.path.isdir(c) and glob.glob(os.path.join(c, "chromium-*")):
                return c
        except Exception:
            continue
    return None


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


def _capture_raw_bytes(url: str) -> Optional[bytes]:
    """Drive headless Chromium (in a subprocess) and return the raw
    above-fold screenshot bytes for `url`. None on any failure.

    Viewport / capture-height / settle / timeout come from _screenshot_cfg()
    (system_config-backed, defaults = the module constants). The resolved
    Playwright browsers dir is passed to the child via env so capture works
    under a service account on any host.

    Run in a subprocess because Playwright's sync API can't be called
    from a thread inside uvicorn's asyncio context on Windows —
    `sync_playwright()` raises NotImplementedError there (the parent's
    ProactorEventLoop can't spawn subprocess children from worker
    threads). A fresh Python process has its own event-loop policy and
    works cleanly. Trade-off: ~200ms subprocess startup per capture; at
    2-3s/page total it's noise.

    Shared by capture_screenshot (image_store URL) and
    capture_and_store_blob (media.db BLOB).
    """
    if not url or not url.strip():
        return None
    import subprocess
    import sys as _sys
    from pathlib import Path as _Path

    worker_path = (_Path(__file__).resolve().parent.parent.parent
                   / "scripts" / "_capture_screenshot_worker.py")
    if not worker_path.exists():
        print(f"[screenshot] worker not found: {worker_path}")
        return None

    cfg = _screenshot_cfg()
    # Child inherits our env; point it at the resolved browsers dir so a
    # LocalSystem service (whose own profile has no browsers) can still launch.
    child_env = dict(os.environ)
    bpath = _resolve_playwright_browsers_path()
    if bpath:
        child_env["PLAYWRIGHT_BROWSERS_PATH"] = bpath

    try:
        result = subprocess.run(
            [
                _sys.executable, str(worker_path),
                url,
                str(cfg["viewport_w"]), str(cfg["viewport_h"]),
                str(cfg["capture_height"]),
                str(cfg["settle_ms"]),
                str(cfg["nav_timeout_ms"]),
            ],
            capture_output=True,
            timeout=(cfg["nav_timeout_ms"] // 1000) + 15,  # buffer for browser+settle
            env=child_env,
        )
        if result.returncode != 0:
            print(f"[screenshot] worker exit {result.returncode} for "
                  f"{url!r}: {result.stderr.decode('utf-8', errors='replace')[:200]}")
            return None
        return result.stdout or None
    except subprocess.TimeoutExpired:
        print(f"[screenshot] worker timeout for {url!r}")
        return None
    except Exception as e:
        print(f"[screenshot] worker spawn failed: {e}")
        return None


def capture_screenshot(url: str, recipe_id: str) -> Optional[str]:
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

    raw_bytes = _capture_raw_bytes(url)
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


# === Durable screenshot BLOB store (media.db) ==============================
# Screenshots live in a SEPARATE git-ignored media.db (NOT recipes.db) so the
# binary never bloats the git-tracked recipe DB. Keyed by url_normalized so a
# re-extract of the same URL overwrites exactly one row (dedup). The recipe
# carries only the short /screenshot/<id> URL; the table-reading endpoint
# serves the BLOB on demand. Regenerable on re-extract, so "not in git" is
# safe — even total loss of media.db is recovered by the next extraction.

def screenshot_id_for(url_normalized: str) -> str:
    """Deterministic 16-char id from the normalized URL — both the public
    /screenshot/<id> path component and the BLOB table primary key. Stable
    across re-extracts so the same URL overwrites one row."""
    return hashlib.sha256((url_normalized or "").encode("utf-8")).hexdigest()[:16]


def ensure_page_screenshots_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS page_screenshots (
               screenshot_id  TEXT PRIMARY KEY,
               url_normalized TEXT NOT NULL,
               jpeg           BLOB NOT NULL,
               created_at     TEXT NOT NULL
           )"""
    )
    conn.commit()


def crop_above_fold(raw_bytes: bytes) -> Optional[bytes]:
    """Crop a full-length capture down to the same above-the-fold window a
    headless capture produces.

    A browser-rendered capture (html2canvas, from the user's own signed-in page)
    is the WHOLE recipe element and can be several thousand pixels tall, where a
    server capture is one viewport. Both end up in the same tile, so they have to
    be framed the same way — otherwise a paywalled site's screenshot is a long
    ribbon next to everyone else's clip.

    The window is taken from VIEWPORT_W/CAPTURE_HEIGHT rather than a literal, so
    this stays tied to the server geometry: change the viewport and both paths
    move together. Shorter-than-the-window images are returned untouched — we
    crop, never pad or upscale.
    """
    try:
        import io as _io
        from PIL import Image
        im = Image.open(_io.BytesIO(raw_bytes)).convert("RGB")
        want_h = max(1, round(im.width * (CAPTURE_HEIGHT / float(VIEWPORT_W))))
        if im.height <= want_h:
            return raw_bytes
        im = im.crop((0, 0, im.width, want_h))
        out = _io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"[screenshot] above-fold crop failed ({e}); keeping the full capture")
        return raw_bytes


def _to_blob_jpeg(raw_bytes: bytes, *, max_w: int = 800, quality: int = 65) -> Optional[bytes]:
    """Downscale + re-encode the raw capture to a compact JPEG. The page
    screenshot is a 'real source on a real site' signal, not a hero image,
    so 800px wide @ q65 (~30-60KB) is plenty and keeps media.db lean."""
    try:
        import io as _io
        from PIL import Image
        im = Image.open(_io.BytesIO(raw_bytes)).convert("RGB")
        if im.width > max_w:
            h = max(1, round(im.height * max_w / im.width))
            im = im.resize((max_w, h), Image.LANCZOS)
        out = _io.BytesIO()
        im.save(out, format="JPEG", quality=quality, optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"[screenshot] blob encode failed: {e}")
        return None


def store_screenshot_blob(db_path: str, url_normalized: str, jpeg_bytes: bytes) -> Optional[str]:
    """Upsert the JPEG BLOB for this URL into media.db. Returns the public
    /screenshot/<id> path, or None on failure / empty input."""
    if not url_normalized or not jpeg_bytes:
        return None
    sid = screenshot_id_for(url_normalized)
    try:
        import sqlite3 as _sqlite3
        with _sqlite3.connect(db_path, timeout=30) as conn:
            ensure_page_screenshots_table(conn)
            conn.execute(
                """INSERT INTO page_screenshots
                       (screenshot_id, url_normalized, jpeg, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(screenshot_id) DO UPDATE SET
                       jpeg       = excluded.jpeg,
                       created_at = excluded.created_at""",
                (sid, url_normalized, jpeg_bytes,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        return f"/screenshot/{sid}"
    except Exception as e:
        print(f"[screenshot] blob store failed: {e}")
        return None


def read_screenshot_blob(db_path: str, screenshot_id: str) -> Optional[bytes]:
    """Fetch a stored JPEG BLOB by id. None on miss / error."""
    if not screenshot_id:
        return None
    try:
        import sqlite3 as _sqlite3
        with _sqlite3.connect(db_path, timeout=30) as conn:
            ensure_page_screenshots_table(conn)
            row = conn.execute(
                "SELECT jpeg FROM page_screenshots WHERE screenshot_id = ?",
                (screenshot_id,),
            ).fetchone()
            return row[0] if row else None
    except Exception as e:
        print(f"[screenshot] blob read failed: {e}")
        return None


def capture_and_store_blob(url: str, url_normalized: str, db_path: str) -> Optional[str]:
    """Capture the source page, shrink to a compact JPEG, store the BLOB in
    media.db keyed by url_normalized, and return the public /screenshot/<id>
    URL. None on any failure (caller leaves pageScreenshot unset)."""
    if not url or not url_normalized:
        return None
    raw = _capture_raw_bytes(url)
    if not raw:
        return None
    blob = _to_blob_jpeg(raw)
    if not blob:
        return None
    return store_screenshot_blob(db_path, url_normalized, blob)
