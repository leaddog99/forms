"""Where cooped previews, AI-generated tiles and page screenshots live.

The backends - a local folder served by `/generated`, or the S3 bucket -
are imagekit's (`imagekit.storage`, adopted 2026-10-01). Two apps had each
written that layer and each copy had a bug the other lacked: this one read an
env name its own docs did not use and fell silently to local for months; f2n
had the Windows trust-store workaround this host needs. The kit owns the
config resolution, the client, the retry/timeout posture, the no-ACL rule and
the loud failure when S3 is asked for without a bucket. See its docstring.

What stays HERE: the `put(key, data, content_type, meta=)` signature the six
callers use, and one process-wide store, built once.

The manifest is gone (2026-10-01, curator: "drop the manifest"). From
2026-05-28 every put appended a line to `_manifest.jsonl` so the file ->
recipe mapping could be recovered without the database. Nothing ever read it;
the recipe row holds the URL, the og-thumb key is the hash of a URL in that
row, and the database is backed up nightly three ways. On S3 it had become a
get-append-put of a growing file on every upload. The local file
(`generated/_manifest.jsonl`, 13,587 lines) is left on disk, not deleted.

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
from typing import Optional, Protocol

from imagekit import storage as _storage
from imagekit.storage import LocalStore, S3Store, StoreError  # noqa: F401  (re-exported)

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


class ImageStore(Protocol):
    """A backend that takes bytes + a key, returns a public URL."""

    def put(self, key: str, data: bytes, content_type: str = "image/webp",
            meta: Optional[dict] = None) -> str: ...
    def url_for(self, key: str) -> str: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class AppStore:
    """imagekit's store under this app's signature. `meta` is accepted and
    ignored: the callers still pass source_url / recipe_id from the manifest
    days, and those values already live in the recipe row."""

    def __init__(self, inner):
        self.inner = inner

    def put(self, key: str, data: bytes, content_type: str = "image/webp",
            meta: Optional[dict] = None) -> str:
        return self.inner.put(key, data, content_type=content_type)

    def url_for(self, key: str) -> str:
        return self.inner.url_for(key)

    def exists(self, key: str) -> bool:
        return self.inner.exists(key)

    def delete(self, key: str) -> None:
        self.inner.delete(key)

    def __repr__(self) -> str:
        return f"AppStore({self.inner!r})"


_store: Optional[AppStore] = None
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
                inner = _storage.from_env(_PREFIX)
                print(f"[image_store] using {inner!r}")
                _store = AppStore(inner)
    return _store


def reset_image_store_for_test() -> None:
    """Test hook - drop the cached store so a re-init picks up new env."""
    global _store
    _store = None
