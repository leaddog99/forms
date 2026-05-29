"""Backend-agnostic image storage for cooped previews + AI-generated tiles + page screenshots.

Two backends:
  - LocalStore: writes to forms/generated/ on local disk, served by the
    existing /generated static mount. Dev-friendly and zero config.
  - S3Store: uploads to an S3 bucket via boto3 + returns the public URL.
    Production-friendly, scales with traffic, doesn't bottleneck on the
    home machine. Needs AWS credentials (AWS_ACCESS_KEY_ID +
    AWS_SECRET_ACCESS_KEY) and a bucket name (BCC_S3_BUCKET).

Backend selection is config-driven (`image_store_backend` in
bcc_config.json, default "local"). Code that calls get_image_store()
gets back the right backend with no awareness of which one is active —
the only contract is `.put(key, bytes, content_type)` returns a URL
the recipe can store and the form can display.

Key shape (single source of truth across backends):
  recipe-thumbs/<recipe_id>.jpg     — per-recipe preview thumbnails
  og-thumbs/<sha8>.jpg               — content-hashed for reuse across recipes
  generated/<name>.png               — AI-generated dish images

All thumbnails are JPEG q=85, EXIF stripped, capped at a max width so
the storage footprint stays small (~30KB each). Pillow handles the
processing; pillow_processor.py wraps the pipeline.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Protocol


# === Configuration ==========================================================
# Read from bcc_config.json with env-var override. The env-var path is
# the production-friendly one (set in the deploy environment, not in a
# checked-in config file).

def _config_value(key: str, default: str) -> str:
    """Try env var first, then bcc_config.json, then default. Env wins
    so production deploys don't have to edit config files."""
    env_name = "BCC_" + key.upper()
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val
    try:
        from input.pipeline.config import _load_bcc_config
        cfg = _load_bcc_config()
        if cfg.get(key) is not None:
            return str(cfg[key])
    except Exception:
        pass
    return default


# === Protocol ===============================================================


class ImageStore(Protocol):
    """A backend that takes bytes + a key, returns a public URL."""

    def put(self, key: str, data: bytes,
            content_type: str = "image/jpeg",
            meta: Optional[dict] = None) -> str: ...

    def url_for(self, key: str) -> str: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...


# Manifest file: append-only JSONL written next to the stored files
# (LocalStore) or as an S3 object (S3Store) so that if the recipes
# DB is ever blown up, the file → recipe mapping is recoverable from
# the storage backend alone. Each line is one put().
#
# Schema per line:
#   {"file": "og-thumbs/abc.jpg",
#    "url":  "/generated/og-thumbs/abc.jpg",
#    "ts":   "2026-05-28T16:42:00Z",
#    "meta": {... whatever the caller passed: source_url, recipe_id, …}}
#
# Append-only; nothing prunes it. At our scale (354 recipes × maybe
# 2-3 artifacts each = ~1000 lines, ~200KB) it never gets big.
_MANIFEST_NAME = "_manifest.jsonl"


def _manifest_line(key: str, public_url: str,
                    meta: Optional[dict]) -> str:
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    entry = {
        "file": key,
        "url":  public_url,
        "ts":   _dt.now(_tz.utc).isoformat(),
    }
    if meta:
        entry["meta"] = meta
    return _json.dumps(entry, ensure_ascii=False) + "\n"


# === LocalStore ============================================================


class LocalStore:
    """Writes to forms/generated/<key> and serves via the existing
    /generated static mount. Public URL is the relative path under
    the app origin — callers compose with the host as needed.

    bcc_config option: `image_store_local_root` (default
    "forms/generated"). bcc_config option: `image_store_public_prefix`
    (default "/generated") — must match the FastAPI static mount.
    """

    def __init__(self, root: str = "generated",
                 public_prefix: str = "/generated"):
        self.root = Path(root)
        self.public_prefix = public_prefix.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        # Defensive: refuse path-traversal keys.
        safe = key.replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            raise ValueError(f"unsafe key: {key!r}")
        full = self.root / safe
        full.parent.mkdir(parents=True, exist_ok=True)
        return full

    def put(self, key: str, data: bytes,
            content_type: str = "image/jpeg",
            meta: Optional[dict] = None) -> str:
        path = self._path_for(key)
        path.write_bytes(data)
        url = self.url_for(key)
        try:
            manifest_path = self.root / _MANIFEST_NAME
            with manifest_path.open("a", encoding="utf-8") as fh:
                fh.write(_manifest_line(key, url, meta))
        except Exception as e:
            print(f"[image_store/local] manifest append failed: {e}")
        return url

    def url_for(self, key: str) -> str:
        safe = key.lstrip("/")
        return f"{self.public_prefix}/{safe}"

    def exists(self, key: str) -> bool:
        try:
            return self._path_for(key).exists()
        except Exception:
            return False

    def delete(self, key: str) -> None:
        try:
            p = self._path_for(key)
            if p.exists():
                p.unlink()
        except Exception:
            pass


# === S3Store ===============================================================


class S3Store:
    """Uploads bytes to an S3 bucket. Public-read bucket (or a
    CloudFront distribution in front) so URLs are openable without
    signing. Falls back to presigned URLs when public-read is off
    (set `image_store_s3_public=false` in config).

    Required config:
      - BCC_S3_BUCKET (env) or `image_store_s3_bucket` (bcc_config.json)
    Optional:
      - BCC_S3_REGION / `image_store_s3_region` (default boto3 default)
      - BCC_S3_KEY_PREFIX / `image_store_s3_key_prefix` (default "" — all
        objects sit at the bucket root; set to e.g. "bcc/" to share a
        bucket with other apps)
      - BCC_S3_PUBLIC / `image_store_s3_public` (default true — public
        URLs; set false to get presigned)
      - BCC_S3_PUBLIC_BASE_URL — when set, used as the URL prefix
        (CDN/CloudFront domain). When unset, falls back to the
        s3.amazonaws.com path-style URL.

    Credentials come from the standard boto3 chain: env vars
    (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), shared credentials
    file (~/.aws/credentials), IAM role on EC2, etc.
    """

    def __init__(self, *,
                 bucket: str,
                 region: Optional[str] = None,
                 key_prefix: str = "",
                 public: bool = True,
                 public_base_url: Optional[str] = None):
        import boto3
        self.bucket = bucket
        self.key_prefix = key_prefix.lstrip("/").rstrip("/") + "/" if key_prefix else ""
        self.public = public
        self.public_base_url = (public_base_url or "").rstrip("/") or None
        self.region = region
        self._client = boto3.client("s3", region_name=region) if region else boto3.client("s3")

    def _full_key(self, key: str) -> str:
        return f"{self.key_prefix}{key.lstrip('/')}"

    def put(self, key: str, data: bytes,
            content_type: str = "image/jpeg",
            meta: Optional[dict] = None) -> str:
        full_key = self._full_key(key)
        extra_args = {
            "ContentType": content_type,
            "CacheControl": "public, max-age=31536000, immutable",
        }
        if self.public:
            extra_args["ACL"] = "public-read"
        self._client.put_object(
            Bucket=self.bucket,
            Key=full_key,
            Body=data,
            **extra_args,
        )
        url = self.url_for(key)
        # Manifest append via a read-modify-write GET/PUT roundtrip.
        # At our volume (a few thousand entries) this is fast enough
        # and S3-eventually-consistent reads are OK since the manifest
        # is recovery-oriented, not read-hot.
        try:
            import json as _json
            manifest_key = self._full_key(_MANIFEST_NAME)
            existing = b""
            try:
                obj = self._client.get_object(Bucket=self.bucket, Key=manifest_key)
                existing = obj["Body"].read()
            except Exception:
                pass  # first put — no manifest yet
            new_line = _manifest_line(key, url, meta).encode("utf-8")
            self._client.put_object(
                Bucket=self.bucket,
                Key=manifest_key,
                Body=existing + new_line,
                ContentType="application/jsonl",
                # Manifest is NOT cache-immutable — it grows on every put.
                CacheControl="no-cache",
                **({"ACL": "public-read"} if self.public else {}),
            )
        except Exception as e:
            print(f"[image_store/s3] manifest update failed: {e}")
        return url

    def url_for(self, key: str) -> str:
        full_key = self._full_key(key)
        if self.public:
            if self.public_base_url:
                return f"{self.public_base_url}/{full_key}"
            # path-style fallback (works with default public buckets)
            if self.region and self.region != "us-east-1":
                return f"https://s3.{self.region}.amazonaws.com/{self.bucket}/{full_key}"
            return f"https://{self.bucket}.s3.amazonaws.com/{full_key}"
        # presigned (1 day default)
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": full_key},
            ExpiresIn=86400,
        )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(
                Bucket=self.bucket, Key=self._full_key(key)
            )
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(
                Bucket=self.bucket, Key=self._full_key(key)
            )
        except Exception:
            pass


# === Factory ===============================================================


_store: Optional[ImageStore] = None


def get_image_store() -> ImageStore:
    """Return the configured image store, instantiating once.

    Selection precedence: env `BCC_IMAGE_STORE_BACKEND` → bcc_config
    `image_store_backend` → default 'local'. Setting it to 's3'
    requires `BCC_S3_BUCKET` (or `image_store_s3_bucket` in config) to
    be set; without it, falls back to LocalStore with a warning.
    """
    global _store
    if _store is not None:
        return _store

    backend = _config_value("image_store_backend", "local").strip().lower()

    if backend == "s3":
        bucket = _config_value("image_store_s3_bucket", "")
        if not bucket:
            print("[image_store] backend=s3 but BCC_S3_BUCKET unset — "
                  "falling back to LocalStore")
            backend = "local"

    if backend == "s3":
        region = _config_value("image_store_s3_region", "") or None
        key_prefix = _config_value("image_store_s3_key_prefix", "")
        public_str = _config_value("image_store_s3_public", "true").strip().lower()
        public = public_str not in ("false", "0", "no")
        public_base = _config_value("image_store_s3_public_base_url", "") or None
        try:
            _store = S3Store(
                bucket=bucket, region=region, key_prefix=key_prefix,
                public=public, public_base_url=public_base,
            )
            print(f"[image_store] using S3Store bucket={bucket!r} "
                  f"region={region!r} prefix={key_prefix!r}")
        except Exception as e:
            print(f"[image_store] S3Store init failed: {e} — using LocalStore")
            _store = LocalStore()
    else:
        root = _config_value("image_store_local_root", "generated")
        prefix = _config_value("image_store_local_public_prefix", "/generated")
        _store = LocalStore(root=root, public_prefix=prefix)
        print(f"[image_store] using LocalStore root={root!r} prefix={prefix!r}")

    return _store


def reset_image_store_for_test() -> None:
    """Test hook — drop the cached store so a re-init picks up new env."""
    global _store
    _store = None
