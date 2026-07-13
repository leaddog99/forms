"""scripts/build_ws_taxonomy.py — mirror Williams-Sonoma's OWN product category hierarchy
into a table, with per-category sample text + an embedding for vector matching.

Faithful to WS's hierarchy (headline > subcategory, THEIR names), not our taxonomy. Uses
the paid web-unblocker (to_markdown.fetch_via_unblocker → Oxylabs) to break through WS's
bot protection — the pages are a hard React SPA that plain fetches 403.

v1 (this script): headline + subcategory (+ url) + WS's own category descriptive copy →
embedding. Product tiles are client-rendered via a JS product API the unblocker doesn't
wait for; real per-product samples are a v2 browser pass (the `products_sample` column is
left for it). See docs/equipment-product-linking.md + memory/project_equipment_standardization.

Table `ws_categories` in recipes.db:
    headline, subcategory, ws_path, url, description, products_sample, embedding (BLOB), ...

Run:  python -m scripts.build_ws_taxonomy            # full scrape (resumable)
      python -m scripts.build_ws_taxonomy --limit 5  # a few subcats per headline (smoke)
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time

sys.path.insert(0, ".")
from bs4 import BeautifulSoup

from to_markdown.html_to_markdown import fetch_via_unblocker
from input.pipeline.embeddings import embed_text, vec_to_bytes

DB = "recipes.db"
_BASE = "https://www.williams-sonoma.com"

# Authoritative WS headline categories (from the site mega-menu, read 2026-07-12).
# Merchandising buckets (New / Sale / Gifts / Holidays) are intentionally excluded —
# they aren't product categories.
HEADLINES = [
    ("Cookware", "cookware"),
    ("Cooks' Tools", "cooks-tools"),
    ("Cutlery", "cutlery"),
    ("Electrics", "electrics"),
    ("Bakeware", "bakeware"),
    ("Food", "food"),
    ("Tabletop & Bar", "tabletop-glassware-bar"),
    ("Home Essentials", "homekeeping"),
    ("Outdoor & Garden", "outdoor"),
    ("Furniture", "home-furniture"),
]

# Nav labels that are NOT real subcategories (section headers, "view all", brand/merch).
_SKIP_LABEL = re.compile(
    r"^(all\b|shop\b|view all|new arrivals?|new\b|sale|clearance|deals?|gift|guides?|"
    r"brands?|registry|seasonal|shop by)", re.I)
_SKIP_SLUG = re.compile(r"(-header$|-view-all$|shop-by-category$|-new-arrivals|registry|monogram)", re.I)
_PROMO = ("rewards", "shipping", "pre-approved", "learn more", "join now", "credit card",
          "sign up", "unlimited flat rate", "reserve -")


def fetch(url: str, tries: int = 4):
    for _ in range(tries):
        try:
            r = fetch_via_unblocker(url, render=False)
            if r and r[0].status_code == 200 and len(r[0].text) > 50000:
                return r[0].text
        except Exception:
            pass
        time.sleep(2)
    return None


def subcategories(headline_slug: str, html: str) -> list[tuple]:
    """[(label, slug, url)] — WS's own subcategory names under a headline, from the nav."""
    pat = re.compile(rf'href="(/shop/{re.escape(headline_slug)}/[a-z0-9\-]+/)"[^>]*>([^<]{{2,60}})</a>')
    out, seen = [], set()
    for href, txt in pat.findall(html):
        import html as _H
        label = _H.unescape(txt).strip()
        slug = href.rstrip("/").split("/")[-1]
        if not label or href in seen or _SKIP_LABEL.match(label) or _SKIP_SLUG.search(slug):
            continue
        seen.add(href)
        out.append((label, slug, _BASE + href))
    return out


def description(html: str) -> str:
    """WS's own category descriptive copy — the SEO prose ('A fry pan is a shallow,
    flat-bottomed pan…'). Strip chrome, keep long prose lines, drop promo lines."""
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["script", "style", "nav", "header", "footer", "svg", "noscript", "form"]):
        bad.decompose()
    text = soup.get_text("\n")
    keep = []
    for line in (l.strip() for l in text.split("\n")):
        if len(line) < 80 or "." not in line:
            continue
        low = line.lower()
        if any(p in low for p in _PROMO):
            continue
        keep.append(re.sub(r"\s+", " ", line))
        if sum(len(x) for x in keep) > 1800:
            break
    # dedupe preserving order
    seen, out = set(), []
    for l in keep:
        if l not in seen:
            seen.add(l); out.append(l)
    return " ".join(out)[:2000]


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ws_categories (
            id              INTEGER PRIMARY KEY,
            headline        TEXT,
            subcategory     TEXT,
            ws_path         TEXT,          -- "Cookware > Fry Pans & Skillets"
            url             TEXT UNIQUE,
            description     TEXT,          -- WS's own category copy (embedding source v1)
            products_sample TEXT,          -- real product names (v2 browser pass)
            embedding       BLOB,
            created_at      TEXT
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wscat_headline ON ws_categories(headline)")
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max subcats PER headline (0=all)")
    args = ap.parse_args()
    conn = sqlite3.connect(DB)
    ensure_table(conn)
    done_urls = {r[0] for r in conn.execute("SELECT url FROM ws_categories")}
    total = 0
    for name, slug in HEADLINES:
        h = fetch(f"{_BASE}/shop/{slug}/")
        if not h:
            print(f"[skip] {name}: headline fetch failed")
            continue
        subs = subcategories(slug, h)
        if args.limit:
            subs = subs[:args.limit]
        print(f"\n=== {name}: {len(subs)} subcategories ===")
        for label, sslug, surl in subs:
            if surl in done_urls:
                print(f"  [have] {label}")
                continue
            hs = fetch(surl)
            if not hs:
                print(f"  [fail] {label} (fetch)")
                continue
            desc = description(hs)
            path = f"{name} > {label}"
            emb_text = f"{path}. {desc}" if desc else path
            try:
                emb = vec_to_bytes(embed_text(emb_text))
            except Exception as e:
                emb = None
                print(f"    (embed failed: {e})")
            conn.execute(
                "INSERT OR REPLACE INTO ws_categories "
                "(headline, subcategory, ws_path, url, description, embedding, created_at) "
                "VALUES (?,?,?,?,?,?, datetime('now'))",
                (name, label, path, surl, desc, emb))
            conn.commit()
            total += 1
            print(f"  [ok] {label:34} desc={len(desc):4}c emb={'Y' if emb else '-'}")
            time.sleep(0.4)
    print(f"\nDONE: {total} ws_categories rows written "
          f"({conn.execute('SELECT COUNT(*) FROM ws_categories').fetchone()[0]} total).")


if __name__ == "__main__":
    main()
