"""scripts/build_ws_taxonomy.py — mirror Williams-Sonoma's OWN product category hierarchy
into a table (3 levels), with per-category sample text + an embedding for vector matching.

WS is a 3-level tree: headline > section > subcategory
  e.g. Home Essentials > Kitchen & Home Storage > Food Storage Containers & Lunch Boxes
A 4th, curator-entered LEAF (project_equipment_standardization) hangs under any node for
finer granularity (compost bags, glass food-storage containers) and rides in the embedding;
leaves are added via the taxonomy admin page, NOT this scraper.

Faithful to WS's hierarchy (their names), not ours. Uses the paid web-unblocker
(to_markdown.fetch_via_unblocker → Oxylabs) to break through WS's bot protection. Brand,
deal, "New Arrivals", "Guides/Cookbooks", and "Shop by Material" sections are dropped — they
aren't functional categories (the Schott-Zwiesel/Le-Creuset pollution). The mega-menu groups
subcategories under a section header terminated by an "All <section>" link; that delimiter is
how we recover the middle level.

Embedding source = full path + WS's category description (+ carried-over product samples).
Table `ws_categories` in recipes.db. Run: python -m scripts.build_ws_taxonomy [--limit N]
"""
from __future__ import annotations

import argparse
import html as _H
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

HEADLINES = [
    ("Cookware", "cookware"), ("Cooks' Tools", "cooks-tools"), ("Cutlery", "cutlery"),
    ("Electrics", "electrics"), ("Bakeware", "bakeware"), ("Food", "food"),
    ("Tabletop & Bar", "tabletop-glassware-bar"), ("Home Essentials", "homekeeping"),
    ("Outdoor & Garden", "outdoor"), ("Furniture", "home-furniture"),
]

# Section headers we drop (not functional categories): brands, deals, merch, material facets.
_DROP_SECTION = re.compile(
    r"^(brands?|sale|clearance|deals?|new arrivals?|new\b|guides?|cookbooks?|seasonal|"
    r"shop by|gift|registry|monogram|featured)", re.I)
# Sub labels/slugs that are section-view / merch, not real subcategories.
_DROP_SUB = re.compile(r"^(all |shop |view all|new arrivals?|new\b)", re.I)
_DROP_SUB_SLUG = re.compile(r"(-header$|-view-all$|shop-by-category$|-new-arrivals|registry|monogram)", re.I)
_BRANDS = (
    "microplane", "staub", "le creuset", "all-clad", "all clad", "oxo", "shun", "wusthof",
    "wüsthof", "miyabi", "global", "zwilling", "henckels", "schott", "emile henry", "mauviel",
    "peugeot", "cangshan", "hestan", "greenpan", "made in", "demeyere", "nordic ware", "boos",
    "epicurean", "goldtouch", "jura", "ruffoni", "smithey", "scanpan", "anchor hocking", "pyrex",
    "kitchenaid", "simplehuman", "cuisinart", "calphalon", "vitamix", "breville", "smeg",
    "laguiole", "mosser", "yeti", "chef'n", "cole & mason", "riedel", "spiegelau", "nespresso",
)
_PROMO = ("rewards", "shipping", "pre-approved", "learn more", "join now", "credit card",
          "sign up", "unlimited flat rate", "reserve -")


def _is_brand(label: str) -> bool:
    l = label.lower()
    return any(b in l for b in _BRANDS)


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


def sections(headline_slug: str, html: str) -> list:
    """[(section_label, [(sub_label, sub_slug, sub_url), ...]), ...] — WS's 3-level nav.
    Recovers the middle 'section' level from the ordered mega-menu anchors: a section runs
    from its header to its 'All <section>' terminator. Brand/deal/merch sections + subs are
    dropped. Stops at the trailing brand tail (a header with no 'All' terminator)."""
    pat = re.compile(rf'href="(/shop/{re.escape(headline_slug)}/[a-z0-9\-]+/)"[^>]*>([^<]{{2,60}})</a>')
    ordered, seen = [], set()
    for href, txt in pat.findall(html):
        label = _H.unescape(txt).strip()
        slug = href.rstrip("/").split("/")[-1]
        key = (slug, label)
        if not label or key in seen:
            continue
        seen.add(key)
        ordered.append((label, slug, _BASE + href))

    out, i, n = [], 0, len(ordered)
    while i < n:
        sec_label = ordered[i][0]
        i += 1
        subs = []
        while i < n and not ordered[i][0].lower().startswith("all "):
            subs.append(ordered[i])
            i += 1
        terminated = i < n and ordered[i][0].lower().startswith("all ")
        if terminated:
            i += 1
        else:
            # A header with no 'All <section>' terminator = the trailing brand/merch tail.
            break
        if _DROP_SECTION.match(sec_label) or _is_brand(sec_label):
            continue
        clean = [(l, s, u) for (l, s, u) in subs
                 if not _DROP_SUB.match(l) and not _DROP_SUB_SLUG.search(s) and not _is_brand(l)]
        if clean:
            out.append((sec_label, clean))
    return out


def description(html: str) -> str:
    """WS's own category descriptive copy (SEO prose). Strip chrome, keep long prose lines."""
    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["script", "style", "nav", "header", "footer", "svg", "noscript", "form"]):
        bad.decompose()
    keep, seen = [], set()
    for line in (l.strip() for l in soup.get_text("\n").split("\n")):
        if len(line) < 80 or "." not in line or any(p in line.lower() for p in _PROMO):
            continue
        line = re.sub(r"\s+", " ", line)
        if line not in seen:
            seen.add(line); keep.append(line)
        if sum(len(x) for x in keep) > 1800:
            break
    return " ".join(keep)[:2000]


def embed_text_for(path: str, desc: str, products: str) -> bytes:
    text = f"{path}."
    if desc:
        text += f" {desc}"
    if products:
        text += f" Sample products: {products}"
    return vec_to_bytes(embed_text(text))


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ws_categories (
            id              INTEGER PRIMARY KEY,
            headline        TEXT,
            subcategory     TEXT,
            ws_path         TEXT,
            url             TEXT UNIQUE,
            description     TEXT,
            products_sample TEXT,
            embedding       BLOB,
            created_at      TEXT
        )""")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ws_categories)")}
    if "section" not in cols:
        conn.execute("ALTER TABLE ws_categories ADD COLUMN section TEXT")       # L2 (WS)
    if "leaf" not in cols:
        conn.execute("ALTER TABLE ws_categories ADD COLUMN leaf TEXT")          # L4 (curator)
    if "source" not in cols:
        conn.execute("ALTER TABLE ws_categories ADD COLUMN source TEXT DEFAULT 'ws'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_wscat_headline ON ws_categories(headline)")
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max subcats PER headline (0=all)")
    args = ap.parse_args()
    conn = sqlite3.connect(DB)
    ensure_table(conn)

    # Carry forward v2 product samples (keyed by url) + preserve any curator LEAF rows.
    prior_products = {u: p for u, p in conn.execute(
        "SELECT url, products_sample FROM ws_categories WHERE products_sample IS NOT NULL AND url IS NOT NULL")}
    conn.execute("DELETE FROM ws_categories WHERE source IS NULL OR source = 'ws'")
    conn.commit()
    print(f"carried {len(prior_products)} product-sample rows; scraping 3 levels…")

    total = 0
    for name, slug in HEADLINES:
        h = fetch(f"{_BASE}/shop/{slug}/")
        if not h:
            print(f"[skip] {name}: headline fetch failed")
            continue
        secs = sections(slug, h)
        print(f"\n=== {name}: {len(secs)} sections ===")
        for sec_label, subs in secs:
            if args.limit:
                subs = subs[:args.limit]
            for sub_label, sub_slug, sub_url in subs:
                hs = fetch(sub_url)
                desc = description(hs) if hs else ""
                path = f"{name} > {sec_label} > {sub_label}"
                products = prior_products.get(sub_url) or ""
                try:
                    emb = embed_text_for(path, desc, products)
                except Exception as e:
                    emb = None
                    print(f"    (embed failed: {e})")
                conn.execute(
                    "INSERT OR REPLACE INTO ws_categories "
                    "(headline, section, subcategory, leaf, ws_path, url, description, "
                    " products_sample, embedding, source, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?, 'ws', datetime('now'))",
                    (name, sec_label, sub_label, None, path, sub_url, desc,
                     products or None, emb))
                conn.commit()
                total += 1
                print(f"  [ok] {sec_label} > {sub_label:34} desc={len(desc):4}c "
                      f"prod={'Y' if products else '-'} emb={'Y' if emb else '-'}")
                time.sleep(0.3)
    print(f"\nDONE: {total} ws_categories rows "
          f"({conn.execute('SELECT COUNT(*) FROM ws_categories').fetchone()[0]} total incl. curator leaves).")


if __name__ == "__main__":
    main()
