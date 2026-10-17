"""youtube_source.py — a YouTube video as a recipe source: the LADDER.

A recipe video carries its recipe in one of a few places, and we read them in
a fixed order, stopping at the first that holds one (curator, 2026-10-16):

    1. the DESCRIPTION  — the text under the video; many creators paste the
                          whole recipe there (Fallow's beef pie: 5,000 chars of
                          ingredients and method)
    2. a LINK           — the description points at a written recipe on a blog;
                          that page is our ordinary URL path and beats anything
                          spoken, so the importer offers it instead
    3. the FIRST COMMENT — the creator's own (often pinned) comment, where
                          some channels put the recipe; only the browser sees
                          comments (YouTube loads them on scroll), so they
                          arrive with a bookmarklet capture or not at all
    4. the TRANSCRIPT   — the auto-generated captions: a cook talking, with
                          "a knob of butter" where a recipe would say 30 g.
                          Usable, flagged as spoken, never silently precise
    5. a polite REJECT  — none of the above holds a recipe we can read

Whichever rung supplies the recipe, the transcript (when the video has one)
rides along in the staged markdown as corroboration: the extractor sees what
was actually said and checks sequence, times and temperatures against it.
That is the audit, done at the one point where it can still change the
result. The thumbnail is lifted from the video id.

NO YouTube Data API (decision 2026-10-16, "option 2"). The API cannot download
another channel's captions (captions.download needs edit permission on the
video), and its Developer Policies forbid an API client from deriving scores
from view/like counts, from storing statistics past 30 days, and from fetching
YouTube data by any other means. Everything here comes from the public watch
page and the caption endpoint the page itself uses, under YouTube's ordinary
terms — the same footing as every other page we fetch. Transcripts are read
with youtube-transcript-api, which works from a residential/business address
(ours) and is blocked from cloud ranges; that is a property of where BCC runs,
not of the code.

Doors that reach this module (all end in the ONE staged-markdown path):
  - POST /stage-capture   the bookmarklet's capture (description + comments)
  - POST /stage-markdown  the bookmarklet payload on a page whose policy let it run
  - extract_recipe_from_url  a pasted URL; the server fetches the watch page
See docs/youtube-ladder.md.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import requests

from video_lookup import youtube_id

__all__ = [
    "LadderResult", "VideoInfo", "ladder", "fetch_watch", "fetch_transcript",
    "recipe_signal", "recipe_links", "build_markdown", "canonical_url",
    "thumbnail_url", "is_youtube_url",
]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_WATCH_TIMEOUT_S = 20
# A transcript longer than this is cut (a 60-minute video runs ~12k words);
# the recipe is never in the last third of a cooking video, the credits are.
TRANSCRIPT_MAX_WORDS = 12000
# Below this a "transcript" is a jingle, not a cook talking.
TRANSCRIPT_MIN_WORDS = 150


def is_youtube_url(url: str) -> bool:
    return youtube_id(url or "") is not None


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def thumbnail_url(video_id: str) -> str:
    """Derived from the id, never fetched here: img.youtube.com serves the
    creator-sanctioned stills (same rule as the recipe form's video field)."""
    return f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"


@dataclass
class VideoInfo:
    video_id: str
    title: str = ""
    channel: str = ""
    channel_id: str = ""
    description: str = ""
    length_s: int = 0
    caption_tracks: list[tuple[str, str]] = field(default_factory=list)  # (lang, kind)
    playable: bool = True
    unplayable_reason: str = ""

    @property
    def url(self) -> str:
        return canonical_url(self.video_id)

    @property
    def thumbnail(self) -> str:
        return thumbnail_url(self.video_id)


def _player_response(html_text: str) -> Optional[dict]:
    """ytInitialPlayerResponse as a dict. raw_decode from the first brace,
    so a '};' inside a string cannot end the match early."""
    m = re.search(r"ytInitialPlayerResponse\s*=\s*\{", html_text)
    if not m:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html_text, m.end() - 1)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def fetch_watch(video_id: str, *, timeout: float = _WATCH_TIMEOUT_S,
                log: Callable[[str], None] = print) -> Optional[VideoInfo]:
    """The watch page's own player data: title, channel, full description,
    length, which caption tracks exist. None when the page could not be read
    (network, consent wall, a playability block) — the caller falls back to
    whatever the browser captured."""
    try:
        r = requests.get(canonical_url(video_id), params={"hl": "en"},
                         headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
                         cookies={"CONSENT": "YES+1", "SOCS": "CAI"}, timeout=timeout)
    except requests.RequestException as e:
        log(f"[YOUTUBE] watch page fetch failed for {video_id}: {type(e).__name__}: {e}")
        return None
    if r.status_code != 200:
        log(f"[YOUTUBE] watch page HTTP {r.status_code} for {video_id}")
        return None
    pr = _player_response(r.text)
    if not pr:
        log(f"[YOUTUBE] no player response in watch page for {video_id}")
        return None
    vd = pr.get("videoDetails") or {}
    status = pr.get("playabilityStatus") or {}
    tracks = (((pr.get("captions") or {}).get("playerCaptionsTracklistRenderer") or {})
              .get("captionTracks")) or []
    info = VideoInfo(
        video_id=video_id,
        title=(vd.get("title") or "").strip(),
        channel=(vd.get("author") or "").strip(),
        channel_id=(vd.get("channelId") or "").strip(),
        description=(vd.get("shortDescription") or "").strip(),
        length_s=int(vd.get("lengthSeconds") or 0),
        caption_tracks=[((t.get("languageCode") or ""), (t.get("kind") or "manual")) for t in tracks],
        playable=(status.get("status") or "OK") == "OK",
        unplayable_reason=(status.get("reason") or ""),
    )
    log(f"[YOUTUBE] {video_id}: {info.title!r} by {info.channel!r}, "
        f"{len(info.description)} desc chars, {info.length_s}s, "
        f"captions={info.caption_tracks or 'none'}"
        + ("" if info.playable else f", NOT playable: {info.unplayable_reason}"))
    return info


# ----------------------------------------------------------------- the signal
# Does a block of text hold a recipe? Conservative on purpose: a description
# that merely says "full recipe on my blog" must NOT pass, or the ladder stops
# on the wrong rung and the written recipe behind the link is never offered.
_QTY_RE = re.compile(
    r"(?:^|[\s(])(?:\d+(?:[.,/]\d+)?(?:\s*[-–]\s*\d+(?:[.,/]\d+)?)?|\d*\s?[½¼¾⅓⅔⅛⅜⅝⅞]|"
    r"(?:a|one|two|three|four|half a|a couple of|a few|a pinch of|a handful of|a splash of|a knob of)\s)"
    r"\s*(?:g|kg|mg|ml|l|cl|dl|oz|fl\.? ?oz|lb|lbs|pounds?|ounces?|grams?|kilos?|cups?|c\.|tbsp|tsp|"
    r"tablespoons?|teaspoons?|pints?|quarts?|gallons?|litres?|liters?|cloves?|sprigs?|sticks?|"
    r"cans?|tins?|jars?|packets?|slices?|bunch(?:es)?|heads?|stalks?|large|medium|small|whole|"
    r"pinch|handful|knob|dash|splash|egg|eggs|onions?|carrots?|potatoes|lemons?|limes?)\b",
    re.I)
_ING_HEAD_RE = re.compile(r"^\W*(?:ingredients?|you(?:'|’)ll need|what you(?:'|’)ll need|shopping list)\b", re.I)
_METHOD_HEAD_RE = re.compile(r"^\W*(?:method|instructions?|directions|steps|how to make|preparation|procedure)\b", re.I)
_NUMBERED_RE = re.compile(r"^\W*(?:\d{1,2}[.)]|step\s*\d+)\s*\S", re.I)
_LINK_ONLY_RE = re.compile(r"(?:full|written|printable|complete)\s+recipe\b.{0,40}(?:link|below|blog|website|site|here|description)", re.I)


@dataclass
class RecipeSignal:
    ingredient_lines: int = 0
    step_lines: int = 0
    has_ingredient_heading: bool = False
    has_method_heading: bool = False
    points_elsewhere: bool = False

    @property
    def has_recipe(self) -> bool:
        if self.has_ingredient_heading and self.has_method_heading and self.ingredient_lines >= 3:
            return True
        if self.ingredient_lines >= 4 and (self.step_lines >= 2 or self.has_method_heading):
            return True
        return False


def recipe_signal(text: str) -> RecipeSignal:
    sig = RecipeSignal()
    lines = [ln.strip() for ln in (text or "").splitlines()]
    after_method = False
    for ln in lines:
        if not ln:
            continue
        if _ING_HEAD_RE.match(ln) and len(ln) < 60:
            sig.has_ingredient_heading = True
            after_method = False
            continue
        if _METHOD_HEAD_RE.match(ln) and len(ln) < 60:
            sig.has_method_heading = True
            after_method = True
            continue
        if len(ln) <= 140 and _QTY_RE.search(ln) and not after_method:
            sig.ingredient_lines += 1
            continue
        if _NUMBERED_RE.match(ln) and len(ln.split()) >= 4:
            sig.step_lines += 1
            continue
        if after_method and len(ln.split()) >= 6:
            sig.step_lines += 1
    sig.points_elsewhere = bool(_LINK_ONLY_RE.search(text or ""))
    return sig


# ------------------------------------------------------------------- the links
# Hosts that are never a written recipe: the creator's other channels, the
# shop, the sponsor. A description link that survives this is offered as the
# recipe's real address (rung 2).
_NOT_A_RECIPE_HOST = (
    "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "facebook.com", "fb.com",
    "twitter.com", "x.com", "threads.net", "pinterest.com", "snapchat.com", "discord.gg",
    "discord.com", "patreon.com", "ko-fi.com", "buymeacoffee.com", "paypal.com", "paypal.me",
    "amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.ca", "amzn.to", "amzn.eu",
    "linktr.ee", "beacons.ai", "spotify.com", "apple.com", "music.apple.com", "podcasts.apple.com",
    "teespring.com", "shopify.com", "merch", "google.com", "goo.gl", "epidemicsound.com",
    "artlist.io", "squarespace.com", "nordvpn", "expressvpn", "hellofresh", "skillshare",
)
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def _host(u: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/?#]+)", u, re.I)
    return m.group(1).lower() if m else ""


def recipe_links(text: str) -> list[str]:
    """Outbound links in a description that could be a written recipe, the
    likeliest first (a URL that says 'recipe' outranks the channel homepage)."""
    seen, out = set(), []
    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(".,;:!?)")
        h = _host(u)
        if not h or any(h == b or h.endswith("." + b) or b in h for b in _NOT_A_RECIPE_HOST):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    out.sort(key=lambda u: (0 if "recipe" in u.lower() else 1, 0 if len(u.split("/")) > 4 else 1))
    return out


# -------------------------------------------------------------- the transcript
@dataclass
class Transcript:
    text: str
    language: str
    is_generated: bool
    words: int
    truncated: bool = False


def fetch_transcript(video_id: str, *, languages: tuple[str, ...] = ("en",),
                     max_words: int = TRANSCRIPT_MAX_WORDS,
                     log: Callable[[str], None] = print) -> Optional[Transcript]:
    """The caption track as one block of prose (no timestamps — the extractor
    wants what was said, not when). English first, then whatever the video
    has. None when the video has no captions or YouTube refused (cloud IPs
    get empty bodies; ours does not)."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        log("[YOUTUBE] youtube-transcript-api not installed; transcript rung off")
        return None
    try:
        api = YouTubeTranscriptApi()
        tl = api.list(video_id)
        try:
            tr = tl.find_transcript(list(languages))
        except Exception:
            tr = next(iter(tl), None)
        if tr is None:
            log(f"[YOUTUBE] {video_id}: no caption tracks")
            return None
        fetched = tr.fetch()
    except Exception as e:  # noqa: BLE001 — the library raises a family of its own
        log(f"[YOUTUBE] {video_id}: transcript unavailable ({type(e).__name__}: {str(e)[:120]})")
        return None
    pieces = [re.sub(r"\s+", " ", s.text).strip() for s in fetched.snippets]
    words = " ".join(p for p in pieces if p).split()
    truncated = len(words) > max_words
    text = " ".join(words[:max_words])
    # Auto-captions carry no sentence breaks; a paragraph every ~120 words
    # keeps the block readable for the model and for a curator.
    text = re.sub(r"((?:\S+\s+){119}\S+)\s+", r"\1\n\n", text)
    out = Transcript(text=text, language=getattr(fetched, "language_code", "") or tr.language_code,
                     is_generated=bool(getattr(fetched, "is_generated", tr.is_generated)),
                     words=min(len(words), max_words), truncated=truncated)
    log(f"[YOUTUBE] {video_id}: transcript {out.words} words, {out.language}, "
        f"{'auto-generated' if out.is_generated else 'manual'}{', truncated' if truncated else ''}")
    return out


# --------------------------------------------------------------------- the result
@dataclass
class LadderResult:
    video_id: str
    url: str
    title: str
    channel: str
    thumbnail: str
    rung: Optional[str]                 # description | link | comment | transcript | None
    markdown: Optional[str]             # staged text; None for link / reject
    links: list[str]
    reject: Optional[str]               # user-facing sentence when nothing is extractable
    description_chars: int
    comments_used: int
    transcript_words: int
    caption_language: str
    length_s: int

    def capture_extra(self) -> dict:
        """The fields stamped on the staged grab's `capture` block and, from
        there, on the recipe's `_source.capture`."""
        return {"rung": self.rung, "channel": self.channel or None, "length_s": self.length_s or None,
                "transcript_words": self.transcript_words or None,
                "caption_language": self.caption_language or None,
                "links": self.links or None, "reject": self.reject}


def _strip_title(t: str) -> str:
    """document.title on a watch page: '(12) Title - YouTube'."""
    t = re.sub(r"^\(\d+\+?\)\s*", "", t or "").strip()
    return re.sub(r"\s*-\s*YouTube$", "", t).strip()


def build_markdown(info: Optional[VideoInfo], *, video_id: str, source_url: str, title: str,
                   description: str, comments: list[str], transcript: Optional[Transcript],
                   rung: str, captured_at: Optional[str] = None) -> str:
    """The staged envelope every other grab writes (title, Source, Captured,
    rule, body) — `markdown_passthrough` reads it back identically — with the
    video's parts under their own headings so the model (and a curator) can
    tell written from spoken."""
    captured_at = captured_at or datetime.now(timezone.utc).isoformat()
    head = (info.title if info and info.title else _strip_title(title)) or "YouTube recipe"
    parts = [f"# {head}\n", f"*Source: {source_url}*  ", f"*Captured: {captured_at}*",
             "*Captured from: YouTube video*"]
    if info and info.channel:
        parts.append(f"*Channel: {info.channel}*")
    if info and info.length_s:
        parts.append(f"*Length: {info.length_s // 60} min {info.length_s % 60} s*")
    parts.append(f"*Recipe found in: {rung}*")
    parts.append("\n---\n")
    if (description or "").strip():
        parts.append("## Video description\n\n" + description.strip())
    for i, c in enumerate(comments or [], 1):
        if (c or "").strip():
            parts.append(f"\n## Comment {i}\n\n{c.strip()}")
    if transcript:
        role = ("the recipe itself — the cook speaking" if rung == "transcript"
                else "corroboration of the written recipe above")
        parts.append(f"\n## Transcript ({'auto-generated' if transcript.is_generated else 'creator'} captions, "
                     f"{transcript.words} words{', cut at the limit' if transcript.truncated else ''}; {role})\n\n"
                     + transcript.text)
    md = "\n".join(parts)
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


def _reject_text(*, had_comments: bool, had_transcript: bool, links: list[str]) -> str:
    looked = "this video's description"
    if had_comments:
        looked += ", its first comments"
    looked += " and its transcript" if had_transcript else ", and it has no transcript we can read"
    s = f"We read {looked}, and did not find a recipe."
    if links:
        s += f" The description links to {_host(links[0])}; the written recipe may be there."
    else:
        s += " If the creator links to a written recipe elsewhere, import from that page."
    return s


def ladder(url: str, *, description: Optional[str] = None, comments: Optional[list[str]] = None,
           want_transcript: bool = True, log: Callable[[str], None] = print,
           _fetch_watch=None, _fetch_transcript=None) -> LadderResult:
    """Climb the ladder for one video. `description` / `comments` are what the
    browser captured (may be None: a pasted URL has no browser); the server's
    own read of the watch page is preferred for the description because it is
    the clean text, not the DOM's. Never raises for an unreadable video — the
    result says so in `reject`. Raises ValueError for a non-YouTube URL."""
    vid = youtube_id(url or "")
    if not vid:
        raise ValueError(f"not a YouTube video URL: {url!r}")
    fw = _fetch_watch or fetch_watch
    ft = _fetch_transcript or fetch_transcript
    info = fw(vid, log=log)
    desc = (info.description if info and info.description else (description or "")).strip()
    comments = [c.strip() for c in (comments or []) if (c or "").strip()][:3]
    sig = recipe_signal(desc)
    links = recipe_links(desc)
    comment_hits = [c for c in comments if recipe_signal(c).has_recipe]
    has_captions = bool(info.caption_tracks) if info else True   # unknown → try
    transcript = ft(vid, log=log) if (want_transcript and has_captions) else None
    if transcript and transcript.words < TRANSCRIPT_MIN_WORDS:
        log(f"[YOUTUBE] {vid}: transcript too short to be a cook talking ({transcript.words} words)")
        transcript = None

    title = (info.title if info else "") or ""
    channel = (info.channel if info else "") or ""
    length_s = info.length_s if info else 0
    base = dict(video_id=vid, url=canonical_url(vid), title=title, channel=channel,
                thumbnail=thumbnail_url(vid), links=links, description_chars=len(desc),
                comments_used=0, transcript_words=transcript.words if transcript else 0,
                caption_language=transcript.language if transcript else "", length_s=length_s)

    if sig.has_recipe:
        rung = "description"
        md = build_markdown(info, video_id=vid, source_url=url, title=title, description=desc,
                            comments=[], transcript=transcript, rung=rung)
        log(f"[YOUTUBE] {vid}: rung 1 — recipe in the description "
            f"({sig.ingredient_lines} ingredient lines, {sig.step_lines} step lines)")
        return LadderResult(rung=rung, markdown=md, reject=None, **base)
    if links:
        log(f"[YOUTUBE] {vid}: rung 2 — description points at {links[0]}")
        return LadderResult(rung="link", markdown=None, reject=None, **base)
    if comment_hits:
        rung = "comment"
        md = build_markdown(info, video_id=vid, source_url=url, title=title, description=desc,
                            comments=comment_hits, transcript=transcript, rung=rung)
        base["comments_used"] = len(comment_hits)
        log(f"[YOUTUBE] {vid}: rung 3 — recipe in a comment")
        return LadderResult(rung=rung, markdown=md, reject=None, **base)
    if transcript:
        rung = "transcript"
        md = build_markdown(info, video_id=vid, source_url=url, title=title, description=desc,
                            comments=[], transcript=transcript, rung=rung)
        log(f"[YOUTUBE] {vid}: rung 4 — transcript only ({transcript.words} words)")
        return LadderResult(rung=rung, markdown=md, reject=None, **base)
    reject = _reject_text(had_comments=bool(comments), had_transcript=False, links=links)
    log(f"[YOUTUBE] {vid}: rung 5 — nothing to read; rejecting politely")
    return LadderResult(rung=None, markdown=None, reject=reject, **base)


def markdown_result(url: str, *, log: Callable[[str], None] = print) -> dict:
    """`html_to_markdown`'s return shape for the server-side URL path, so
    `extract_recipe_from_url` treats a watch URL like any fetched page.
    Raises RuntimeError with the user-facing sentence when the ladder ends in
    a reject or a link (the written recipe lives at the link; the caller
    surfaces it rather than extracting a description that has none)."""
    res = ladder(url, log=log)
    if res.rung == "link":
        raise RuntimeError(f"This video's description does not hold the recipe; it links to "
                           f"{res.links[0]} — import from that page.")
    if not res.markdown:
        raise RuntimeError(res.reject or "No recipe found in this video.")
    # og_meta is what the extractor's hero coopt and site-name stamp read for an
    # ordinary page; the video supplies the same three things.
    return {"markdown": res.markdown, "source_url": url, "title": res.title,
            "has_jsonld": False, "jsonld": [], "youtube": res.capture_extra(),
            "hero_image_url": res.thumbnail,
            "og_meta": {"image": res.thumbnail, "siteName": "YouTube", "author": res.channel}}
