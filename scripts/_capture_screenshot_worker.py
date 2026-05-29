"""Subprocess worker: capture one screenshot via Playwright + write to
stdout as raw bytes. Spawned from screenshot_pipeline.capture_screenshot
to dodge the Windows asyncio ProactorEventLoop incompatibility that
breaks `sync_playwright()` when called inside uvicorn's worker threads.

Usage (not for direct user invocation):
  python -m scripts._capture_screenshot_worker <url> <viewport_w> <viewport_h> <capture_h> <settle_ms> <nav_timeout_ms>

Writes raw JPEG bytes (quality=90) to stdout on success.
Writes nothing + non-zero exit on any failure (caller treats as None).
"""
from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) != 7:
        print(f"usage error: {sys.argv}", file=sys.stderr)
        return 2
    _, url, vw, vh, ch, settle, nav_to = sys.argv
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
                page.goto(url, wait_until="domcontentloaded",
                          timeout=nav_timeout_ms)
                page.wait_for_timeout(settle_ms)
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
