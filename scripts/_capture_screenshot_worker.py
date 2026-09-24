"""Subprocess worker: capture one screenshot via Playwright + write to
stdout as raw bytes. Spawned from screenshot_pipeline.capture_screenshot
to dodge the Windows asyncio ProactorEventLoop incompatibility that
breaks `sync_playwright()` when called inside uvicorn's worker threads.

Usage (not for direct user invocation):
  python -m scripts._capture_screenshot_worker <url> <viewport_w> <viewport_h> <capture_h> <settle_ms> <nav_timeout_ms>

With HTML on STDIN, the browser renders THAT document (base URL = <url>, so relative
images and stylesheets still resolve) instead of navigating to the site - the
unblocker rung: the page was fetched through the paid unblocker, which solved the
site's bot check; a headless Chromium at our own address never could.

Writes raw JPEG bytes (quality=90) to stdout on success.
Writes nothing + non-zero exit on any failure (caller treats as None).
Exit 7 = the page was an INTERSTITIAL (a block notice or a bot check), not the
recipe: nothing is written, so no screenshot is stored - see _is_interstitial.
"""
from __future__ import annotations

import sys


# Words a BLOCK PAGE or BOT CHECK says, and a real recipe page never does above
# the fold. Measured 2026-09-24: williams-sonoma.com served "Sorry, due to
# website restrictions we are unable to display the requested page" to all 72
# captures of a run (every blob 3,809 bytes, identical), smittenkitchen.com
# "Checking your browser", instantpot.com Cloudflare's "Your connection needs to
# be verified" - 305 such blobs across 10 hosts had been STORED as screenshots,
# because a block page has enough contrast to pass the blank detector (stddev
# 7-14 against a refuse line of 2). A stored block page is a permanent wrong
# answer on every surface; NO screenshot is honest and re-capturable, and the
# nightly refresh's failure latch then paces the retries.
_INTERSTITIAL = (
    "unable to display the requested page", "due to website restrictions",
    "checking your browser", "verify you are human", "verify you are a human",
    "connection needs to be verified", "just a moment", "attention required",
    "access denied", "access to this page has been denied", "request blocked",
    "please enable cookies", "enable javascript and cookies to continue",
    "are you a robot", "bot detection", "captcha", "press and hold",
    "pardon our interruption", "why do i have to complete a captcha",
    "this site can't be reached", "503 service", "403 forbidden",
    # A bot check that never clears for a headless browser: the whole page is
    # a spinner and two words (timoleondiamantis.gr, 20 of 20 captures, 2026-09-24).
    "please wait", "loading...", "one moment", "redirecting",
    # Cloudflare's managed challenge, once the spinner resolves.
    "performing security verification", "security service to protect against",
    "verifies you are not a bot", "verifies that you are not a bot",
    "checking if the site connection is secure", "needs to review the security",
)
# A page whose ENTIRE visible text is this short is not a recipe page, whatever
# it says: a spinner, a logo, a cookie wall with no content behind it.
_INTERSTITIAL_MIN_CHARS = 40
# A real page carries far more text than this above the fold even before its
# images load; an interstitial is a sentence or two.
_INTERSTITIAL_MAX_CHARS = 600


def _is_interstitial(page) -> str:
    """The matched phrase when the rendered page is a block/bot-check
    interstitial rather than content, else ''. Judged on the page's own visible
    words: short body text carrying one of the known phrases."""
    try:
        text = page.evaluate("() => (document.body && document.body.innerText) || ''")
    except Exception:
        return ""
    body = " ".join(str(text).split()).lower()
    if len(body) > _INTERSTITIAL_MAX_CHARS:
        return ""
    for phrase in _INTERSTITIAL:
        if phrase in body:
            return phrase
    if len(body) < _INTERSTITIAL_MIN_CHARS:
        return f"almost no text ({len(body)} chars)"
    return ""


def main() -> int:
    if len(sys.argv) != 7:
        print(f"usage error: {sys.argv}", file=sys.stderr)
        return 2
    _, url, vw, vh, ch, settle, nav_to = sys.argv
    html_in = None
    if not sys.stdin.isatty():
        try:
            data = sys.stdin.buffer.read()
            html_in = data.decode("utf-8", errors="replace") if data else None
        except Exception:
            html_in = None
    try:
        viewport_w = int(vw)
        viewport_h = int(vh)
        capture_h = int(ch)
        settle_ms = int(settle)
        nav_timeout_ms = int(nav_to)
    except Exception as e:
        print(f"arg parse error: {e}", file=sys.stderr)
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"playwright import failed: {e}", file=sys.stderr)
        return 3

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--disable-blink-features=AutomationControlled",
            ])
            context = browser.new_context(
                viewport={"width": viewport_w, "height": viewport_h},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                device_scale_factor=1.0,
            )
            page = context.new_page()
            try:
                if html_in:
                    # Route the DOCUMENT request to the supplied HTML; every
                    # sub-resource (images, CSS, fonts) still loads from the
                    # site, which serves assets to anyone.
                    page.route(url, lambda route: route.fulfill(
                        status=200, content_type="text/html; charset=utf-8", body=html_in))
                page.goto(url, wait_until="domcontentloaded",
                          timeout=nav_timeout_ms)
                page.wait_for_timeout(settle_ms)
                hit = _is_interstitial(page)
                if hit:
                    # A bot check sometimes clears itself given a few more
                    # seconds; a block page never does. One more wait, one
                    # more look, then refuse.
                    page.wait_for_timeout(min(4000, max(1500, settle_ms)))
                    hit = _is_interstitial(page)
                if hit:
                    print(f"interstitial: {hit!r}", file=sys.stderr)
                    return 7
                raw = page.screenshot(
                    type="jpeg",
                    quality=90,
                    clip={
                        "x": 0, "y": 0,
                        "width": viewport_w,
                        "height": capture_h,
                    },
                    full_page=False,
                )
            finally:
                browser.close()
    except Exception as e:
        print(f"capture failed: {e}", file=sys.stderr)
        return 4

    if not raw:
        return 5
    # Ensure stdout is binary so we can write JPEG bytes.
    try:
        sys.stdout.buffer.write(raw)
    except Exception as e:
        print(f"stdout write failed: {e}", file=sys.stderr)
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
