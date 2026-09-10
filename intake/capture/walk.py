"""Signed-in capture walker — the bookmarklet, automated.

    python -m intake.capture.walk --host mollybaz.com --login
        Opens a HEADED Chromium on this machine's screen with the host's
        persistent profile. Sign in by hand; press Enter here when done.
        The session lives in data/browser_profiles/<host>/ — no password is
        ever typed by code.

    python -m intake.capture.walk --host mollybaz.com --urls-file five.txt
    python -m intake.capture.walk --host mollybaz.com --from-ledger --limit 5
        Visits each URL in the signed-in profile, captures the rendered DOM,
        reduces it with the canonical HTML→markdown reducer, saves the hero
        image with the session's cookies, and writes one capture per page
        into input/captures/<host>/ (docs/capture-folder-contract.md).
        STOPS on the first signed-out page. Polite: 3–7 s between pages, one
        page at a time.

Spike scope (2026-09-09): console only; ingest is the next step. Everything
lives in intake/capture/ so backing out is deleting the folder.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from intake.capture.contract import (Capture, build_body, canon_host, host_dir,  # noqa: E402
                                     log_line, normalize_url, profile_dir, slug_for,
                                     write_capture)

# Per-host signed-out signature: ALL of `absent` must be missing from the page
# text and ANY of `present` must appear. Deliberately conservative — a signed-in
# page that merely mentions "Sign In" in the header must not trip it, so
# `absent` (recipe structure) is required too.
SIGNED_OUT = {
    "mollybaz.com": {"present": ("try the club", "gift the club", "this is part of the club"),
                     "absent": ("ingredients",)},
    "177milkstreet.com": {"present": ("start your free trial", "log in to see", "subscribe to see"),
                          "absent": ("ingredients",)},
}
RECIPE_MARKERS = re.compile(r"(^|\n)#{1,4}\s*(ingredients|instructions|directions|method)\b", re.I)
PAUSE_S = (3.0, 7.0)
PAGE_TIMEOUT_MS = 45_000
# Anti-bot interstitials. Cloudflare's "Just a moment…" and SiteGround's
# sgcaptcha meta-refresh both clear on their own in a real, headed browser
# within seconds; a headless one sits on them forever (measured on
# simplyrecipes.com, 2026-09-09). Wait, re-read, then call it.
CHALLENGE = re.compile(r"just a moment|attention required|sgcaptcha|verify you are human|"
                       r"checking your browser|cf-challenge|challenge-platform", re.I)
CHALLENGE_WAIT_S = 30
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def is_challenge(title: str, html: str) -> bool:
    return bool(CHALLENGE.search(title or "")) or bool(CHALLENGE.search((html or "")[:20_000]))


def _text_of(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).lower()


def signed_out(host: str, html: str, markdown: str) -> bool:
    sig = SIGNED_OUT.get(canon_host(host))
    if not sig:
        return False
    text = _text_of(html)
    hit = any(p in text for p in sig["present"])
    lacks = all(a not in markdown.lower() for a in sig["absent"])
    return hit and lacks


def has_recipe(markdown: str, jsonld: list) -> bool:
    if any("recipe" in json.dumps(b).lower()[:4000] and '"@type"' in json.dumps(b) for b in jsonld):
        for b in jsonld:
            s = json.dumps(b)
            if re.search(r'"@type"\s*:\s*"Recipe"', s) or re.search(r'"@type"\s*:\s*\[[^\]]*"Recipe"', s):
                return True
    return bool(RECIPE_MARKERS.search(markdown or ""))


def _jsonld_blocks(page) -> list:
    raw = page.evaluate(
        "() => Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]')).map(s => s.textContent)")
    out = []
    for s in raw or []:
        try:
            out.append(json.loads(s))
        except Exception:
            continue
    return out


def _hero_url(page, jsonld: list) -> str:
    for b in jsonld:
        nodes = b.get("@graph", [b]) if isinstance(b, dict) else []
        for n in nodes:
            if isinstance(n, dict) and "Recipe" in str(n.get("@type", "")):
                img = n.get("image")
                if isinstance(img, list) and img:
                    img = img[0]
                if isinstance(img, dict):
                    img = img.get("url")
                if isinstance(img, str) and img.startswith("http"):
                    return img
    og = page.evaluate("() => (document.querySelector('meta[property=\"og:image\"]') || {}).content || ''")
    return og if isinstance(og, str) and og.startswith("http") else ""


def urls_from_ledger(host: str, limit: int) -> list:
    """The publisher's cohort as the harvest ranked it (run_candidates), latest
    run first; kept before dropped; ledger order within."""
    import sqlite3
    conn = sqlite3.connect(os.path.join(_ROOT, "recipes.db"))
    rows = conn.execute(
        "SELECT url FROM run_candidates WHERE collection_type='publisher' AND collection_key=? "
        "ORDER BY run_started_at DESC, CASE outcome WHEN 'kept' THEN 0 ELSE 1 END, "
        "COALESCE(final_rank, 9999), serp_rank LIMIT ?", (canon_host(host), limit)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def do_login(host: str) -> None:
    from playwright.sync_api import sync_playwright
    pdir = profile_dir(host)
    os.makedirs(pdir, exist_ok=True)
    print(f"[capture] opening headed Chromium with profile {pdir}")
    print(f"[capture] sign in to https://{host}/ in that window, then press Enter here.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(pdir, headless=False, args=LAUNCH_ARGS,
                                                   viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"https://{host}/", timeout=PAGE_TIMEOUT_MS)
        try:
            input()
        except EOFError:
            print("[capture] no console; waiting 120 s for you to sign in…")
            time.sleep(120)
        ctx.close()
    log_line(host, "login: profile saved by curator")
    print("[capture] profile saved. Run the walk.")


def walk(host: str, urls: list, *, identity: int, headed: bool, limit: int | None) -> dict:
    from to_markdown.html_to_markdown import markdown_from_html
    from playwright.sync_api import sync_playwright

    host = canon_host(host)
    pdir = profile_dir(host)
    if not os.path.isdir(pdir):
        raise SystemExit(f"no profile for {host} — run: python -m intake.capture.walk --host {host} --login")
    urls = [u for u in urls if u][: (limit or len(urls))]
    seen, todo = set(), []
    for u in urls:
        n = normalize_url(u)
        if n in seen:
            continue
        seen.add(n)
        if os.path.exists(os.path.join(host_dir(host), slug_for(u) + ".md")):
            print(f"[capture] skip (already captured): {u}")
            continue
        todo.append(u)
    tally = {"captured": 0, "no-recipe": 0, "challenge": 0, "error": 0, "signed-out": 0,
             "skipped": len(urls) - len(todo)}
    try:
        from input.pipeline import acquisition as ACQ
        ACQ.set_context(run_kind="capture", domain=host)
    except Exception:
        pass
    print(f"[capture] {host}: {len(todo)} to visit ({tally['skipped']} already captured)")
    log_line(host, f"walk start: {len(todo)} urls identity={identity}")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            pdir, headless=not headed, args=LAUNCH_ARGS, viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for i, url in enumerate(todo, 1):
            print(f"  [{i:>2}/{len(todo)}] visiting  {url}", flush=True)
            _t0 = time.time()
            captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            cap = Capture(source_url=url, host=host, captured_at=captured_at, identity=identity)
            html = None
            hero_bytes = None
            try:
                page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                cap.title = page.title() or ""
                html = page.content()
                deadline = time.time() + CHALLENGE_WAIT_S
                while is_challenge(cap.title, html) and time.time() < deadline:
                    time.sleep(2)
                    cap.title = page.title() or ""
                    html = page.content()
                jsonld = _jsonld_blocks(page)
                md = markdown_from_html(html, url)
                cap.body = build_body(title=cap.title, source_url=url, captured_at=captured_at,
                                      jsonld_blocks=jsonld, markdown=md)
                if is_challenge(cap.title, html):
                    cap.status, cap.note = "challenge", f"anti-bot interstitial did not clear in {CHALLENGE_WAIT_S}s"
                elif signed_out(host, html, md):
                    cap.status, cap.note = "signed-out", "publisher's signed-out signature on page"
                elif not has_recipe(md, jsonld):
                    cap.status, cap.note = "no-recipe", "no ingredients/instructions heading and no Recipe JSON-LD"
                else:
                    cap.hero_source = _hero_url(page, jsonld)
                    if cap.hero_source:
                        try:
                            r = ctx.request.get(cap.hero_source, timeout=20_000)
                            if r.ok and (r.headers.get("content-type", "").startswith("image/")):
                                hero_bytes = r.body()
                        except Exception as e:
                            cap.note = f"hero fetch failed: {type(e).__name__}"
            except Exception as e:
                cap.status, cap.note = "error", f"{type(e).__name__}: {str(e)[:200]}"
            ext = ".png" if cap.hero_source.lower().endswith(".png") else ".jpg"
            path = write_capture(cap, html=html, hero_bytes=hero_bytes, hero_ext=ext)
            tally[cap.status] += 1
            # Acquisition ledger: the walker is a technique like any other.
            try:
                from input.pipeline import acquisition as ACQ
                _ok = cap.status in ("captured", "no-recipe")
                _reason = {"signed-out": "wall:membership (signed-out signature)",
                           "challenge": cap.note, "error": cap.note}.get(cap.status, "")
                _rid = ACQ.record(url, "walker", ok=_ok, rung=1, reason=_reason,
                                  nbytes=len(html) if html else None,
                                  ms=int((time.time() - _t0) * 1000), notes=cap.status)
                if _ok:
                    ACQ.gate(url, gate="jsonld" if "STRUCTURED RECIPE DATA" in (cap.body or "") else "phrase",
                             score=None, usable=(cap.status == "captured"))
            except Exception as _e:
                print(f"[ACQ] walker ledger skipped: {type(_e).__name__}: {_e}")
            log_line(host, f"{cap.status:10s} {url} -> {os.path.basename(path)} {cap.note}")
            print(f"           {cap.status:10s} {os.path.basename(path)}"
                  + (f"  hero={'yes' if hero_bytes else 'no'}" if cap.status == "captured" else f"  {cap.note}"))
            if cap.status == "signed-out":
                print(f"\n  !! {host} shows its signed-out page. Stopping — nothing else visited. "
                      f"Re-login: python -m intake.capture.walk --host {host} --login\n")
                break
            if i < len(todo):
                time.sleep(random.uniform(*PAUSE_S))
        ctx.close()
    log_line(host, f"walk end: {tally}")
    print(f"[capture] done: {tally}")
    return tally


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True)
    ap.add_argument("--login", action="store_true", help="open a headed browser to sign in; saves the profile")
    ap.add_argument("--urls-file", help="one URL per line")
    ap.add_argument("--from-ledger", action="store_true", help="use the publisher's run_candidates cohort")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--identity", type=int, default=0, help="user id to save under (0 = master)")
    ap.add_argument("--headless", action="store_true",
                    help="hide the browser (default is HEADED: anti-bot interstitials clear in a real window)")
    a = ap.parse_args(argv)
    if a.login:
        do_login(a.host)
        return 0
    urls: list = []
    if a.urls_file:
        urls += [ln.strip() for ln in open(a.urls_file, encoding="utf-8") if ln.strip() and not ln.startswith("#")]
    if a.from_ledger:
        urls += urls_from_ledger(a.host, a.limit or 50)
    if not urls:
        ap.error("give --urls-file or --from-ledger (or --login)")
    walk(a.host, urls, identity=a.identity, headed=not a.headless, limit=a.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
