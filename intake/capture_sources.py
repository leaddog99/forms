"""Capture sources — the TYPED front door for recipes that live on locked-down,
loosely-written pages: Facebook posts and reels, Instagram captions, Reddit
threads, YouTube descriptions, Medium and Substack posts.

Why this exists (2026-10-06). A growing share of the recipes people want to keep
are not on recipe blogs with schema.org markup; they are a caption under a video.
Two things are different about those pages, and this module names both:

1. TRANSPORT. The page's Content-Security-Policy refuses our injected
   bookmarklet payload AND the fetch that stages it (Facebook allows only its own
   hosts for both), and a server fetch gets a logged-out preview. The only copy of
   the text is in the user's signed-in browser. So the bookmark's own code — which
   a page CSP cannot block — captures the text and hands it to us through the URL
   fragment of a page on OUR host (`forms/importing.html#capture=…`). The
   fragment never reaches any server log. `POST /stage-capture` is where that
   capture becomes the SAME staged markdown every other grab becomes, so from
   there on it is the one canonical path: `/staged-markdown/{token}` →
   `/extract-from-markdown` → `markdown_to_recipe`. No parallel extractor.

2. STRUCTURE. A caption has no headings, no JSON-LD, emoji bullets, quantities
   written inline, and on Facebook the method is routinely "in the first
   comment" — which, as often as not, is a LINK to a blog (verified on a real reel
   2026-10-06: the author's comment was `Recipe >>>> https://…`). So a capture
   carries the author's own comments, we pull the links out of them, and the
   importer can offer the linked page — our ordinary URL path — instead. And a
   good share of these WILL fail or come through thin; the registry says what to
   expect, in words the in-progress page shows the user and in words the model
   reads as publisher notes.

Where the knowledge lives:
- DOM selectors (what to read on the page) live in the LOADER one-liner in
  `forms/install.html` / `forms/bookmarklet.js`, because the loader is the only
  code that runs on a CSP-locked page and it cannot fetch anything from us. Kept
  generic (selection → longest `dir=auto` span → article/main → body) with the
  few verified per-site marks noted below.
- Everything the SERVER needs to know about a kind lives here: the host match,
  the structure, the model guidance, the user-facing expectation. This is a small
  structural taxonomy (six platforms), not curated publisher data — publisher
  prose stays in `domains.extract_notes`, which the prompt already reads; the
  kind note is appended beside it, never instead of it.

Verified vs. assumed: only `facebook` has been checked against a live page.
The others are registered so the typing, provenance and messaging are in place;
their notes say what we EXPECT and will be corrected as each is tried.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

__all__ = [
    "CaptureSource", "classify_capture_url", "capture_source_for", "all_sources",
    "build_capture_markdown", "links_in_text", "capture_prompt_note",
]


@dataclass(frozen=True)
class CaptureSource:
    kind: str                       # stable id, stamped on _source.capture.kind
    label: str                      # user-facing name of the platform
    hosts: tuple[str, ...]          # registrable-domain suffixes that match
    structure: str                  # post | reel | caption | thread | video | article
    # What the model should know about text of this shape. Appended to the
    # extraction system prompt as a clearly subordinate note (see
    # markdown_to_recipe); it never overrides the general rules.
    model_note: str
    # What the person sees on the in-progress page, before the result lands.
    user_note: str
    # What to say when the result is thin. Specific to the platform, with the
    # one thing they can do about it.
    thin_result_note: str
    verified: bool = False          # checked against a live page?
    # URL path patterns that refine the structure (e.g. a Facebook /reel/ URL).
    structure_by_path: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def structure_for(self, url: str) -> str:
        path = (urlparse(url).path or "").lower()
        for pat, s in self.structure_by_path:
            if re.search(pat, path):
                return s
        return self.structure


_SOCIAL_COMMON = (
    "The text is a social-media caption, not an article: no headings, emoji or "
    "asterisks as bullets, quantities written inline, hashtags at the end. Read "
    "ingredients and steps out of plain prose where you must. Never invent a step "
    "the text does not state; if the method is absent, return the ingredients and "
    "leave instructions empty rather than guessing. Ignore hashtags, follow/like "
    "prompts, 'see more'/'see less', and view counts."
)

_SOURCES: tuple[CaptureSource, ...] = (
    CaptureSource(
        kind="facebook", label="Facebook",
        hosts=("facebook.com", "fb.com", "fb.watch"),
        structure="post",
        structure_by_path=((r"/reel/|/reels/|/videos?/|/watch", "reel"),),
        verified=True,
        model_note=_SOCIAL_COMMON + (
            " On Facebook the method is often posted as a COMMENT by the page itself; "
            "any 'Author comment' sections in the markdown are that, and belong to the "
            "recipe. If the caption says the recipe is 'in the first comment' and the "
            "comment is only a link, there is no method here — leave instructions empty."
        ),
        user_note=(
            "Facebook posts are loosely written and the method is often in a comment "
            "or behind a link, so check the ingredients and steps once it opens."
        ),
        thin_result_note=(
            "Facebook posts rarely carry a full recipe in the post itself. If the method "
            "is in a comment, select that text on the post and press the bookmark again."
        ),
    ),
    CaptureSource(
        kind="instagram", label="Instagram",
        hosts=("instagram.com",),
        structure="caption",
        structure_by_path=((r"/reels?/", "reel"),),
        model_note=_SOCIAL_COMMON + (
            " Instagram captions are short; the recipe may be only a title and a few "
            "ingredients, or may say 'recipe on my blog' or 'link in bio'. Extract only "
            "what is written."
        ),
        user_note=(
            "Instagram captions are short and the recipe itself is often on the "
            "creator's blog, so this may come through as a title and a few ingredients."
        ),
        thin_result_note=(
            "Instagram captions seldom hold a whole recipe. If the caption points to a "
            "blog, import from that page instead; otherwise select the recipe text on "
            "the post and press the bookmark again."
        ),
    ),
    CaptureSource(
        kind="reddit", label="Reddit",
        hosts=("reddit.com", "redd.it"),
        structure="thread",
        model_note=(
            "The text is a Reddit thread: a title, a post body that may be only a photo, "
            "and comments. The recipe is usually in the ORIGINAL POSTER's first comment, "
            "often written as a paragraph. Use only the poster's own text; other "
            "commenters' variations are not the recipe. " + _SOCIAL_COMMON
        ),
        user_note=(
            "On Reddit the recipe usually lives in the poster's own comment, so we read "
            "the thread rather than the post alone."
        ),
        thin_result_note=(
            "The recipe may be in a comment we could not reach. Open the poster's "
            "comment, select its text, and press the bookmark again."
        ),
    ),
    CaptureSource(
        kind="youtube", label="YouTube",
        hosts=("youtube.com", "youtu.be"),
        structure="video",
        model_note=(
            "The text is a YouTube video description: the recipe, when present, is a "
            "block of ingredients and brief steps between the intro and the links, "
            "sometimes with timestamps. Strip timestamps, sponsor text, channel links "
            "and 'subscribe' prompts. " + _SOCIAL_COMMON
        ),
        user_note=(
            "We read the video's description, not the video. Recipes there are usually "
            "a short ingredient list with brief steps."
        ),
        thin_result_note=(
            "The description did not hold a usable recipe. If the creator links to a "
            "blog, import from that page; the video itself is not read."
        ),
    ),
    CaptureSource(
        kind="medium", label="Medium",
        hosts=("medium.com",),
        structure="article",
        model_note=(
            "The text is a Medium article: ordinary prose with headings; recipes here "
            "are often narrative, with quantities in sentences. Extract as from any "
            "article; do not invent what the prose leaves out."
        ),
        user_note="Medium articles are prose, so quantities and steps may be written into sentences.",
        thin_result_note=(
            "This article did not yield a full recipe. Select the recipe section and "
            "press the bookmark again, or check that the article has one."
        ),
    ),
    CaptureSource(
        kind="substack", label="Substack",
        hosts=("substack.com",),
        structure="article",
        model_note=(
            "The text is a Substack newsletter post: an essay that usually ends in a "
            "recipe with a conventional ingredient list and numbered steps. Prefer the "
            "recipe block; the essay is context, not instructions."
        ),
        user_note="Substack posts are essays that usually end in the recipe; we read the whole post.",
        thin_result_note=(
            "The post did not yield a full recipe. Some newsletters keep the recipe for "
            "paid subscribers; if you can see it, select it and press the bookmark again."
        ),
    ),
)

_BY_KIND = {s.kind: s for s in _SOURCES}


def all_sources() -> tuple[CaptureSource, ...]:
    return _SOURCES


def capture_source_for(kind: str) -> Optional[CaptureSource]:
    return _BY_KIND.get((kind or "").strip().lower())


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().strip(".")
    except Exception:
        return ""


def classify_capture_url(url: str) -> Optional[CaptureSource]:
    """The registered source whose host matches `url`, or None for an ordinary
    page. Suffix match on the host so `m.facebook.com` and `www.reddit.com`
    classify; custom-domain Substacks do NOT (they are ordinary publishers with
    their own `domains` row, which is the right record for them)."""
    host = _host_of(url)
    if not host:
        return None
    for s in _SOURCES:
        for h in s.hosts:
            if host == h or host.endswith("." + h):
                return s
    return None


_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def links_in_text(*texts: str, exclude_source: str = "") -> list[str]:
    """Outbound links found in capture text, de-duplicated, the capture's own
    host left out. A Facebook author comment that is `Recipe >>>> https://…` is
    the recipe's real address; the importer offers it."""
    src_host = _host_of(exclude_source)
    seen: set[str] = set()
    out: list[str] = []
    for t in texts:
        for m in _URL_RE.finditer(t or ""):
            u = m.group(0).rstrip(".,;:!?")
            h = _host_of(u)
            if not h:
                continue
            if src_host and (h == src_host or h.endswith("." + src_host)):
                continue
            # Facebook wraps outbound links as l.facebook.com/l.php?u=… ; the
            # capture reads innerText so we usually see the bare URL, but skip
            # the wrapper if it ever appears.
            if h.startswith("l.") and h.endswith((".facebook.com", ".instagram.com")):
                continue
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
    return out


_WS_RE = re.compile(r"[ \t]+\n")
_BLANKS_RE = re.compile(r"\n{3,}")


def build_capture_markdown(*, url: str, title: str, text: str,
                           comments: list[str] | None, captured_at: str,
                           source: Optional[CaptureSource]) -> str:
    """The staged markdown for a capture: the SAME envelope the bookmarklet
    payload writes (title, Source, Captured, rule, body) so
    `markdown_passthrough` reads the url/title back identically and nothing
    downstream knows this came in by a different door. No JSON-LD block —
    these pages have none. The author's comments, when any, follow the caption
    under their own heading so the model (and a reader) can tell them apart."""
    label = source.label if source else ""
    head_title = (title or "").strip()
    # Facebook's document.title is "(20+) Facebook" — the notification count and
    # the site, never the post. The caption's first line is the honest title.
    if source and (not head_title or re.fullmatch(r"(\(\d+\+?\)\s*)?" + re.escape(label), head_title, re.I)):
        first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
        head_title = first[:140] or f"{label} recipe"
    parts = [f"# {head_title}\n",
             f"*Source: {url}*  ",
             f"*Captured: {captured_at}*",
             ]
    if source:
        parts.append(f"*Captured from: {label} {source.structure_for(url)}*")
    parts.append("\n---\n")
    parts.append((text or "").strip())
    for i, c in enumerate(comments or [], 1):
        c = (c or "").strip()
        if not c:
            continue
        parts.append(f"\n## Author comment {i}\n\n{c}")
    md = "\n".join(parts)
    md = _WS_RE.sub("\n", md)
    md = _BLANKS_RE.sub("\n\n", md)
    return md.strip() + "\n"


def capture_prompt_note(url: str) -> str:
    """The kind's model note for `url`, or '' for an ordinary page. Read by
    markdown_to_recipe beside the publisher's own `extract_notes`, so a Facebook
    URL that arrives by ANY door (bookmarklet, fallback capture, a pasted URL
    the server fetched) gets the same guidance."""
    s = classify_capture_url(url)
    if not s:
        return ""
    return f"Source shape: {s.label} {s.structure_for(url)}. {s.model_note}"
