"""Quick-and-dirty probe of the Semrush Analytics API for PER-URL data.

Answers the question that reframes docs/recipe-scoring-design.md: can we buy traffic,
search volume and month-by-month history for an ARBITRARY recipe url, rather than only
getting traffic as a by-product of a publisher harvest?

Three calls, deliberately tiny (~300 units of a ~50k balance):

    url_ranks     live        -> Ot (absolute organic traffic), Or (organic keywords)   10 units/line
    url_ranks     display_date-> the same url month by month                            50 units/line

    NB the API `type` is `url_ranks`. The docs page is TITLED "URL Overview (one
    database)" and `type=url_overview` returns "query type not found".
    url_organic               -> that url's keywords with Nq (SEARCH VOLUME)            10 units/line

Checks the unit balance BEFORE spending and prints running spend, because units expire
monthly and do not roll over.

Usage:
    python -m scripts.semrush_url_probe                 # default sample of corpus urls
    python -m scripts.semrush_url_probe <url> [<url>…]  # specific urls
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
load_dotenv()

KEY = os.getenv("SEMRUSH_KEY")
API = "https://api.semrush.com/"
DB = "us"
spent = 0


def _get(params: dict, unit_cost_per_line: int, label: str):
    """One API call. Returns (header, rows) parsed from Semrush's ;-separated CSV."""
    global spent
    params = {**params, "key": KEY}
    r = requests.get(API, params=params, timeout=30)
    body = (r.text or "").strip()
    # Semrush signals errors as a plain body like "ERROR 50 :: NOTHING FOUND"
    if body.startswith("ERROR") or not body:
        print(f"  [{label}] {body or 'empty response'}")
        return None, []
    lines = body.splitlines()
    header = lines[0].split(";")
    rows = [dict(zip(header, ln.split(";"))) for ln in lines[1:]]
    spent += len(rows) * unit_cost_per_line
    return header, rows


def balance():
    r = requests.get("https://www.semrush.com/users/countapiunits.html",
                     params={"key": KEY}, timeout=20)
    return (r.text or "").strip()


def probe(url: str, months: list[str]):
    print(f"\n=== {url}")

    # 1. Absolute organic traffic for THIS url.
    _h, rows = _get({"type": "url_ranks", "url": url, "database": DB,
                     "export_columns": "Ot,Or"}, 10, "url_ranks")
    if rows:
        d = rows[0]
        print(f"  organic traffic (Ot) : {d.get('Organic Traffic', d.get('Ot', '?'))}")
        print(f"  organic keywords (Or): {d.get('Organic Keywords', d.get('Or', '?'))}")
    else:
        print("  no live overview row")

    # 2. The same url month by month — the trajectory axis.
    print("  history:")
    for ym in months:
        _h, rows = _get({"type": "url_ranks", "url": url, "database": DB,
                         "export_columns": "Ot,Or", "display_date": ym}, 50,
                        f"url_ranks {ym}")
        if rows:
            d = rows[0]
            print(f"    {ym[:6]}  Ot={d.get('Organic Traffic', d.get('Ot','?')):>10}"
                  f"  Or={d.get('Organic Keywords', d.get('Or','?')):>8}")
        else:
            print(f"    {ym[:6]}  (no data)")

    # 3. Keywords + SEARCH VOLUME — the capture denominator.
    _h, rows = _get({"type": "url_organic", "url": url, "database": DB,
                     "display_limit": 5, "export_columns": "Ph,Po,Nq,Tr"}, 10, "url_organic")
    if rows:
        print("  top keywords (Nq = search volume, Tr = traffic %):")
        for d in rows:
            print(f"    {d.get('Keyword','?')[:38]:<38} pos={d.get('Position','?'):>3}"
                  f"  Nq={d.get('Search Volume','?'):>9}  Tr={d.get('Traffic (%)','?')}")


def main() -> int:
    if not KEY:
        print("SEMRUSH_KEY not set"); return 1
    print(f"unit balance BEFORE: {balance()}")

    urls = sys.argv[1:]
    if not urls:
        import sqlite3
        conn = sqlite3.connect("file:recipes.db?mode=ro", uri=True)
        urls = [r[0] for r in conn.execute(
            "SELECT json_extract(data,'$._source.originalUrl') u FROM master_recipes "
            "WHERE source_host IN ('smittenkitchen.com','pinchofyum.com') "
            "AND u IS NOT NULL LIMIT 2")]
        urls.append("https://www.sun-sentinel.com/2026/07/31/quick-fix-summer-corn-soup/")

    # 1 month back, 6 back, 12 back — the buckets the design wants.
    months = ["20260715", "20260215", "20250815"]
    for u in urls:
        probe(u, months)

    print(f"\nunits spent this run (computed): {spent}")
    print(f"unit balance AFTER : {balance()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
