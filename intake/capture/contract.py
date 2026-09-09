"""The capture file: front matter + a bookmarklet-compatible markdown body.

docs/capture-folder-contract.md is the contract; this module is its only
reader and writer. The body is built to be byte-compatible with what
forms/bookmarklet.js posts to /stage-markdown (title header, Source/Captured
lines, JSON-LD block, `---` rule, page markdown), so the ingest step can hand
a file to the same stage → extract → save path a bookmarklet capture takes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

CONTRACT = "bcc-capture/1"
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CAPTURES_DIR = os.path.join(_ROOT, "input", "captures")
PROFILES_DIR = os.path.join(_ROOT, "data", "browser_profiles")

STATUSES = ("captured", "signed-out", "no-recipe", "challenge", "error")


def canon_host(url_or_host: str) -> str:
    h = url_or_host.strip().lower()
    if "://" in h:
        h = urlsplit(h).netloc
    return h[4:] if h.startswith("www.") else h


def normalize_url(url: str) -> str:
    """Scheme+host+path, no query/fragment, no trailing slash — the ledger's
    url_normalized shape, so dedupe agrees with run_candidates."""
    p = urlsplit(url.strip())
    path = (p.path or "/").rstrip("/") or "/"
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return urlunsplit((p.scheme or "https", host, path, "", "")).rstrip("/")


def slug_for(url: str) -> str:
    norm = normalize_url(url)
    path = urlsplit(norm).path.strip("/") or "index"
    s = re.sub(r"[^a-z0-9]+", "-", path.lower().replace("/", "__")).strip("-")[:120]
    return f"{s}__{hashlib.sha1(norm.encode()).hexdigest()[:8]}"


def host_dir(host: str) -> str:
    return os.path.join(CAPTURES_DIR, canon_host(host))


def profile_dir(host: str) -> str:
    return os.path.join(PROFILES_DIR, canon_host(host))


@dataclass
class Capture:
    source_url: str
    host: str
    title: str = ""
    captured_at: str = ""
    method: str = "playwright-walk"
    identity: int = 0
    hero_image: str = ""
    hero_source: str = ""
    status: str = "captured"
    note: str = ""
    url_normalized: str = ""
    contract: str = CONTRACT
    body: str = field(default="", repr=False)

    def __post_init__(self):
        self.host = canon_host(self.host or self.source_url)
        self.url_normalized = self.url_normalized or normalize_url(self.source_url)
        self.captured_at = self.captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, not {self.status!r}")

    @property
    def slug(self) -> str:
        return slug_for(self.source_url)

    def front_matter(self) -> str:
        d = asdict(self)
        d.pop("body", None)
        keys = ("contract", "source_url", "url_normalized", "host", "title", "captured_at",
                "method", "identity", "hero_image", "hero_source", "status", "note")
        lines = ["---"]
        for k in keys:
            v = d.get(k, "")
            v = "" if v is None else str(v)
            if any(c in v for c in (":", "#", "\n")) and not v.startswith('"'):
                v = json.dumps(v)               # quote anything YAML would misread
            lines.append(f"{k}: {v}")
        lines.append("---")
        return "\n".join(lines) + "\n"

    def render(self) -> str:
        return self.front_matter() + (self.body or "")


def build_body(*, title: str, source_url: str, captured_at: str,
               jsonld_blocks: list, markdown: str) -> str:
    """Exactly the bookmarklet's stage payload body (forms/bookmarklet.js)."""
    body = f"# {title}\n\n*Source: {source_url}*  \n*Captured: {captured_at}*\n\n"
    if jsonld_blocks:
        body += ("## STRUCTURED RECIPE DATA (JSON-LD)\n\n```json\n"
                 + json.dumps(jsonld_blocks, indent=2, ensure_ascii=False) + "\n```\n\n")
    body += "---\n\n" + re.sub(r"\n{3,}", "\n\n", markdown or "").strip()
    return body + "\n"


def write_capture(cap: Capture, *, html: str | None = None,
                  hero_bytes: bytes | None = None, hero_ext: str = ".jpg") -> str:
    """Write <slug>.md (+ .html, + hero) into the host folder. Returns the .md path."""
    d = host_dir(cap.host)
    os.makedirs(d, exist_ok=True)
    base = os.path.join(d, cap.slug)
    if hero_bytes:
        cap.hero_image = cap.slug + hero_ext
        with open(base + hero_ext, "wb") as f:
            f.write(hero_bytes)
    if html is not None:
        with open(base + ".html", "w", encoding="utf-8", newline="\n") as f:
            f.write(html)
    with open(base + ".md", "w", encoding="utf-8", newline="\n") as f:
        f.write(cap.render())
    return base + ".md"


_FM = re.compile(r"\A---\n(.*?)\n---\n", re.S)


def read_capture(path: str) -> Capture:
    text = open(path, encoding="utf-8").read()
    m = _FM.match(text)
    if not m:
        raise ValueError(f"{path}: no front matter")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v.startswith('"'):
            v = json.loads(v)
        meta[k.strip()] = v
    if meta.get("contract") != CONTRACT:
        raise ValueError(f"{path}: contract {meta.get('contract')!r}, expected {CONTRACT!r}")
    return Capture(source_url=meta["source_url"], host=meta.get("host", ""),
                   title=meta.get("title", ""), captured_at=meta.get("captured_at", ""),
                   method=meta.get("method", ""), identity=int(meta.get("identity") or 0),
                   hero_image=meta.get("hero_image", ""), hero_source=meta.get("hero_source", ""),
                   status=meta.get("status", "captured"), note=meta.get("note", ""),
                   url_normalized=meta.get("url_normalized", ""),
                   body=text[m.end():])


def log_line(host: str, line: str) -> None:
    d = host_dir(host)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(d, "_walk.log"), "a", encoding="utf-8") as f:
        f.write(f"{stamp} {line}\n")
