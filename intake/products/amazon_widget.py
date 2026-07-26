"""Amazon rating histogram from the customer-review popover widget — free, no API key.

**STAGE TWO of product SELECTION.** A collection (a saved Amazon search URL) yields a list
of ASINs with only an average and a rating count — enough for a rough prefilter, not enough
to score. This fetches each survivor's REAL 5/4/3/2/1 breakdown per-ASIN, cheaply, WITHOUT
opening the product page, so a shortlist can be rescored properly before we commit to
anything expensive.

It is NOT the histogram source for the enhancement stage. Once a product has been selected,
`realrank_research.fetch_owner_data` pulls ratings AND the listing facts the widget doesn't
carry (photo, price, top review bodies) in one structured call. This module runs BEFORE any
product is accessed; that one runs after.

Verified against the paid Rainforest API on two products (2026-07-26): identical averages
and identical histograms — B0029JQEIC 4.8★/11,623 [89,7,3,0,1] and B00006JSUB
4.6★/144,696 [82,10,4,1,3], where the 20-rating delta was simply a day of new ratings.

PARSING: the widget emits ONE canonical string per bar —

    aria-label="5 stars represent 82% of rating"

so that is the only thing we parse. An earlier version walked every element on the page
with loose regexes and merged whatever it found; it silently paired percentages with the
wrong star numbers and returned histograms totalling 410% and 445%. Broad fallback
extraction on a page like this doesn't add robustness, it adds confident wrong answers —
when the canonical pattern isn't there, the honest result is a FAILURE, not a guess.

Amazon rounds each bar to a whole percent, so counts here are DERIVED (pct x total) and
flagged as such. They're needed in count form because pooling several retailers means
summing counts (see realrank_index.pool_histograms) — percentages can't be summed.
"""
from __future__ import annotations

import re

import requests

# Undocumented endpoints — isolated so they can be swapped without touching the parser.
WIDGET_URLS = (
    "https://www.amazon.com/gp/customer-reviews/widgets/average-customer-review/popover/"
    "?contextId=dpx&asin={asin}",
    "https://www.amazon.com/review/widgets/average-customer-review/popover/"
    "?contextId=dpx&asin={asin}",
)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# The canonical bar label. Everything else in the widget is decoration.
_PCT_RE = re.compile(r"([1-5])\s*stars?\s+represent\s+(\d+(?:\.\d+)?)\s*%", re.I)
_AVG_RE = re.compile(r"([0-5](?:\.\d+)?)\s+out\s+of\s+5", re.I)
_TOTAL_RE = re.compile(r"([\d,]+)\s+(?:global|customer)\s+ratings?", re.I)
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

_BOT_MARKERS = ("enter the characters you see below", "not a robot",
                "validatecaptcha", "captchacharacters")


def _headers(asin: str) -> dict:
    return {"User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"https://www.amazon.com/dp/{asin}"}


def parse_widget(html: str) -> dict:
    """Pull avg / total / histogram out of the widget HTML. `error` is set and `ok` False
    when the canonical five bars aren't all present — never a partial histogram."""
    bars = {s: float(p) for s, p in _PCT_RE.findall(html or "")}
    missing = [s for s in "54321" if s not in bars]
    if missing:
        return {"ok": False, "error": f"histogram incomplete — missing {missing} star bar(s)"}
    pct = [bars[s] for s in "54321"]
    total_pct = sum(pct)
    # Independent rounding means 99 or 101 is normal; anything wider means we parsed junk.
    if not 95 <= total_pct <= 105:
        return {"ok": False, "error": f"histogram percentages total {total_pct}% — parse is wrong"}
    avg = _AVG_RE.search(html or "")
    tot = _TOTAL_RE.search(html or "")
    return {
        "ok": True, "error": "",
        "avg_rating": float(avg.group(1)) if avg else None,
        "ratings_total": int(tot.group(1).replace(",", "")) if tot else None,
        "histogram_pct": pct,
    }


def rating_histogram(asin: str, *, timeout: int = 20, allow_unblocker: bool = True) -> dict:
    """The 5..1 star breakdown for one ASIN.

    Returns {ok, asin, avg_rating, ratings_total, histogram (COUNTS 5..1, derived),
    histogram_pct, counts_derived, via, error}. `via` is 'direct' or 'unblocker'.

    A direct fetch is free; on a block (403 / 429 / CAPTCHA) we retry through BCC's
    unblocker rather than giving up, because at collection volume Amazon will throttle.
    """
    asin = (asin or "").strip().upper()
    if not _ASIN_RE.match(asin):
        return {"ok": False, "asin": asin, "error": "invalid ASIN (need 10 alphanumerics)"}

    blocked = False
    last_err = ""
    for url_t in WIDGET_URLS:
        url = url_t.format(asin=asin)
        try:
            r = requests.get(url, headers=_headers(asin), timeout=timeout, allow_redirects=True)
        except requests.RequestException as e:
            last_err = f"request failed: {e}"
            continue
        if r.status_code in (403, 429):
            blocked = True
            last_err = f"HTTP {r.status_code} (throttled/refused)"
            continue
        if not r.ok:
            last_err = f"HTTP {r.status_code}"
            continue
        html = r.text or ""
        if any(m in html.lower() for m in _BOT_MARKERS):
            blocked = True
            last_err = "bot-check page"
            continue
        parsed = parse_widget(html)
        if parsed["ok"]:
            return _finish(asin, parsed, "direct")
        last_err = parsed["error"]

    if blocked and allow_unblocker:
        try:
            from to_markdown.html_to_markdown import fetch_via_unblocker
            res = fetch_via_unblocker(WIDGET_URLS[0].format(asin=asin), render=False,
                                      timeout=60)
            if res is not None:
                parsed = parse_widget(res[0].text or "")
                if parsed["ok"]:
                    return _finish(asin, parsed, "unblocker")
                last_err = parsed["error"]
            else:
                last_err = f"{last_err}; unblocker also failed"
        except Exception as e:
            last_err = f"{last_err}; unblocker error: {e}"

    return {"ok": False, "asin": asin, "error": last_err or "widget unavailable"}


def _finish(asin: str, parsed: dict, via: str) -> dict:
    pct = parsed["histogram_pct"]
    total = parsed.get("ratings_total")
    # Counts are what pool_histograms sums; percentages can't be pooled across retailers.
    counts = [int(round(p / 100.0 * total)) for p in pct] if total else []
    return {
        "ok": True, "asin": asin, "error": "", "via": via,
        "avg_rating": parsed.get("avg_rating"),
        "ratings_total": total,
        "histogram": counts,          # 5..1 COUNTS (derived)
        "histogram_pct": pct,         # 5..1 percentages, as Amazon reports them
        "counts_derived": True,
        "url": f"https://www.amazon.com/dp/{asin}",
    }
