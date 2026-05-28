"""Playwright smoke test — open a page, dump basics.

First probe. Goal: confirm Playwright is wired up, see how long it
takes to launch + navigate + read DOM. Run only after `01_install.md`
steps.

Usage:
    python sandbox/playwright/02_smoke.py [URL]

Default URL is cleanfoodiecravings.com — the page our batch fetcher
403's on. If Playwright gets through where requests.get didn't, that's
already a meaningful signal.
"""
import sys
import time

DEFAULT_URL = "https://cleanfoodiecravings.com/the-best-foolproof-scrambled-eggs/"


def main() -> None:
    from playwright.sync_api import sync_playwright

    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print(f"target: {url}")

    t0 = time.perf_counter()
    with sync_playwright() as p:
        t_pw = time.perf_counter()
        browser = p.chromium.launch(headless=True)
        t_browser = time.perf_counter()
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        t_page = time.perf_counter()

        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else "<no response>"
        except Exception as exc:
            print(f"goto error: {exc!r}")
            status = "exception"

        t_goto = time.perf_counter()

        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        t_idle = time.perf_counter()

        title = page.title()
        html = page.content()
        t_dump = time.perf_counter()

        browser.close()

    print(f"\nstatus      : {status}")
    print(f"title       : {title!r}")
    print(f"html length : {len(html):,} chars")
    print(f"\n--- timings (ms) ---")
    print(f"sync_playwright   : {int((t_pw - t0) * 1000)}")
    print(f"launch chromium   : {int((t_browser - t_pw) * 1000)}")
    print(f"new context+page  : {int((t_page - t_browser) * 1000)}")
    print(f"goto              : {int((t_goto - t_page) * 1000)}")
    print(f"networkidle wait  : {int((t_idle - t_goto) * 1000)}")
    print(f"title+content     : {int((t_dump - t_idle) * 1000)}")
    print(f"total             : {int((t_dump - t0) * 1000)}")


if __name__ == "__main__":
    main()
