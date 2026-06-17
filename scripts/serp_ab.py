"""serp_ab.py — fidelity A/B: SerpApi vs Scale SERP on the same verbatim queries.

The GATE before flipping the `serp_provider` config (memory project_serp_provider):
our rankings trust Google's exact result SET + ORDER, so a new vendor must return
substantially the same organic results in the same order — the precise bar
DataForSEO FAILED (Spearman -0.41, 0/10 top-10 overlap). This runs real queries
through both providers and reports, per query: result counts, URL-set overlap
(Jaccard), top-10 overlap, and Spearman rank correlation on the shared URLs.

Run AFTER dropping SCALE_SERP_API_KEY into .env:
  python scripts/serp_ab.py                       # built-in sample queries
  python scripts/serp_ab.py "banana bread recipe" "site:milkstreet.com/recipes"

Reads-only; makes a handful of SERP calls per provider. No config change — pass
provider explicitly, so the live default is untouched.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")

from input.pipeline.serp_search import serp_search, has_key

DEFAULT_QUERIES = [
    "banana bread recipe",
    "chicken milanese recipe",
    'site:milkstreet.com/recipes',
]
PAGES = 5  # ~50 results — enough to see set + order agreement


def _spearman(rank_a: dict, rank_b: dict) -> float | None:
    """Spearman rho over URLs present in BOTH (ranks are 1-based ints)."""
    common = [u for u in rank_a if u in rank_b]
    n = len(common)
    if n < 3:
        return None
    d2 = sum((rank_a[u] - rank_b[u]) ** 2 for u in common)
    return 1 - (6 * d2) / (n * (n * n - 1))


def _run(query: str) -> None:
    a = serp_search(query, pages=PAGES, provider="serpapi")
    b = serp_search(query, pages=PAGES, provider="scaleserp")
    sa = {r["link"] for r in a}
    sb = {r["link"] for r in b}
    inter, union = sa & sb, sa | sb
    jac = (len(inter) / len(union)) if union else 0.0
    top10a = [r["link"] for r in a[:10]]
    top10b = [r["link"] for r in b[:10]]
    top10_overlap = len(set(top10a) & set(top10b))
    rho = _spearman({r["link"]: r["rank"] for r in a}, {r["link"]: r["rank"] for r in b})

    print(f"\nQ: {query!r}")
    print(f"   serpapi={len(a)}  scaleserp={len(b)}  shared={len(inter)}  Jaccard={jac:.2f}"
          f"  top10_overlap={top10_overlap}/10  Spearman={'n/a' if rho is None else f'{rho:+.2f}'}")
    if len(b) == 0:
        print("   (scaleserp returned 0 — key missing or request shape wrong)")
    # Eyeball the top 5 side by side.
    for i in range(5):
        ua = top10a[i] if i < len(top10a) else "—"
        ub = top10b[i] if i < len(top10b) else "—"
        mark = "=" if ua == ub else " "
        print(f"   {i+1} {mark} serp:{ua[:58]:<58} scale:{ub[:58]}")


def main() -> int:
    queries = sys.argv[1:] or DEFAULT_QUERIES
    print("=== SERP fidelity A/B: SerpApi vs Scale SERP ===")
    print(f"serpapi key: {has_key('serpapi')} | scaleserp key: {has_key('scaleserp')}")
    if not has_key("scaleserp"):
        print("\n!! SCALE_SERP_API_KEY not set — drop it in .env and re-run. "
              "(SerpApi side will still print so you can sanity-check the harness.)")
    print("\nPASS bar (vs DataForSEO's -0.41 / 0-overlap failure): high Jaccard, "
          "top10_overlap >= ~7/10, Spearman strongly positive (>~0.7).")
    for q in queries:
        try:
            _run(q)
        except Exception as e:  # noqa: BLE001
            print(f"\nQ: {q!r}  ERROR: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
