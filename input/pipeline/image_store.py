"""Where cooped previews, AI-generated tiles and page screenshots live.

The backends - a local folder served by `/generated`, or the S3 bucket -
are imagekit's (`imagekit.storage`, adopted 2026-10-01). Two apps had each
written that layer and each copy had a bug the other lacked: this one read an
env name its own docs did not use and fell silently to local for months; f2n
had the Windows trust-store workaround this host needs. The kit owns the
config resolution, the client, the retry/timeout posture, the no-ACL rule and
the loud failure when S3 is asked for without a bucket. See its docstring.

What stays HERE: one process-wide store, built once, and the mapping of this
app's older .env names onto the kit's. (A per-put manifest sidecar lived here
from 2026-05-28 to 2026-10-01; nothing read it and it was removed with its
data - see the state log for that date.)

Configuration is `.env`, under the kit's names with the app prefix:

    BCC_IMAGE_STORE=local|s3     (the older BCC_IMAGE_STORE_BACKEND still works)
    BCC_S3_BUCKET, BCC_S3_REGION, BCC_S3_PREFIX, BCC_S3_PUBLIC_BASE_URL
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY

Key shape (single source of truth across backends):
  og-thumbs/<hash>.webp          - cooped thumbnails (older ones are .jpg)
  recipe-thumbs/<recipe_id>.webp - per-recipe previews
  generated/<name>.webp|png      - AI-generated dish images
  screenshots/...                - page screenshots
"""
from __future__ import annotations

import os
import threading
from typing import Optional

from imagekit import storage as _storage
from imagekit.storage import LocalStore, S3Store, Store, StoreError  # noqa: F401  (re-exported)

_PREFIX = "BCC"

# The names this app used before the kit's. Mapped once, at first use, so one
# .env keeps working across the switch; the kit never sees the old names.
_LEGACY_ENV = {
    "BCC_IMAGE_STORE_BACKEND": "BCC_IMAGE_STORE",
    "BCC_S3_KEY_PREFIX": "BCC_S3_PREFIX",
    "BCC_IMAGE_STORE_LOCAL_ROOT": "BCC_LOCAL_ROOT",
    "BCC_IMAGE_STORE_LOCAL_PUBLIC_PREFIX": "BCC_LOCAL_PUBLIC_PREFIX",
}


def _map_legacy_env() -> None:
    for old, new in _LEGACY_ENV.items():
        if os.environ.get(old) and not os.environ.get(new):
            os.environ[new] = os.environ[old]


# The interface is the kit's: put(key, data, *, content_type) -> url,
# get, exists, delete, url_for.
ImageStore = Store

_store: Optional[Store] = None
_store_lock = threading.Lock()


def get_image_store() -> ImageStore:
    """The configured store, built once per process. Asking for S3 without a
    bucket RAISES (the kit's rule): the old quiet fall-back to local is how new
    pictures lived on one disk for months with nothing in any log."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _map_legacy_env()
                _store = _storage.from_env(_PREFIX)
                print(f"[image_store] using {_store!r}")
    return _store


def is_ours(url: str) -> bool:
    """True when `url` points into our own store - local mount or the bucket -
    so a re-extract keeps it instead of coopting our own copy into a second
    object under a new hash. Any backend the app has ever used counts: a row
    written in the local days still says /generated/... after S3 is on."""
    u = (url or "").strip()
    if not u:
        return False
    if u.startswith("/generated/") or u.startswith("/og-thumbs/") or u.startswith("/screenshot/"):
        return True
    try:
        return u.startswith(get_image_store().base_url())
    except Exception:  # noqa: BLE001  - unconfigured store: only the local shapes count
        return False


def reset_image_store_for_test() -> None:
    """Test hook - drop the cached store so a re-init picks up new env."""
    global _store
    _store = None
