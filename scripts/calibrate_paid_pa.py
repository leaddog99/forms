"""Calibrate the paywall PA-tax per premium publisher + surface their best recipes.

Premium/gated publishers earn far less PAGE authority than free sites at the same
DA, so OU penalizes them. For each publisher we estimate its recipe-page PA
distribution (μ, σ) and the matched-DA FREE reference, giving a shift-and-scale
remap to free-equivalent PA:
    adjusted_PA = μ_free(DA) + (PA - μ_paid) * (σ_free(DA)/σ_paid)
Calibration is PERSISTED to the domains master (paywall=1 + pa_cal_*), where the
scorer reads it. The same Moz-scored URL set, sorted by PA, also yields each
publisher's TOP-N most-notable recipes (an ingestion / Editor's-Choice queue).

NYT/ATK calibrate free from the corpus; absent publishers (Milk Street) are
harvested live via SerpAPI site: search (recipe-URL filtered) + Moz scoring.

Run:  python -m scripts.calibrate_paid_pa            (report + persist)
      python -m scripts.calibrate_paid_pa --dry-run  (no DB writes)
"""
import argparse, json, os, sys, statistics as st
from urllib.parse import urlparse

import requests
import sqlite3

sys.path.insert(0, ".")
from input.pipeline.url_scoring import score_url_via_moz  # loads .env on import
from input.pipeline import domains_lib

DB = "recipes.db"
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
TOP_N = 10


def _host(u):
    return (urlparse(u).hostname or "").replace("www.", "") if u else ""


def _is_recipe_url(u):
    """Recipe-page filter: the FIRST path segment is recipe(s) and a slug
    follows — i.e. /recipes/<slug>. Excludes /stories, /discussion, /collections,
    /recipes (bare index) etc. that merely contain the word 'recipe'. Works for
    NYT/ATK/Milk Street, which all use a /recipes/<slug> shape."""
    segs = [s for s in urlparse(u).path.lower().strip("/").split("/") if s]
    return len(segs) >= 2 and segs[0] in ("recipe", "recipes")


def corpus_da_pa(conn):
    """All (url, host, da, pa) already scored in the corpus — free."""
    out = []
    for tbl in ("master_recipes", "recipes"):
        for (dj,) in conn.execute(f"SELECT data FROM {tbl}"):
            try:
                d = json.loads(dj)
            except Exception:
                continue
            sc = d.get("_scoring") or {}
            da, pa = sc.get("domainAuthority"), sc.get("pageAuthority")
            u = (d.get("_source") or {}).get("originalUrl") or ""
            h = _host(u)
            if isinstance(da, (int, float)) and isinstance(pa, (int, float)) and h:
                out.append((u, h, float(da), float(pa)))
    return out


def free_ref(rows, da, window=8):
    nbr = [pa for (u, h, d, pa) in rows if abs(d - da) <= window]
    if len(nbr) < 4:
        return None
    return st.mean(nbr), st.pstdev(nbr), len(nbr)


def serpapi_recipe_urls(domain, want=100, recipe_path="recipes"):
    """Discover a publisher's recipe URLs via SerpAPI. KEY DETAILS: scope the
    query to the recipes PATH (`site:domain/recipes`) and pass filter=0 so Google
    doesn't omit "similar" results — that's the difference between ~5 and ~90 hits.
    Google's site: ranking surfaces the most-notable pages naturally. Restrict to
    the canonical host (domain / www.domain) so the calibration matches what the
    scorer remaps (archive.* / prod.* subdomains excluded)."""
    from input.pipeline.serp_search import serp_search, has_key
    if not has_key():
        print("  [skip] no SERP key (SERPAPI_KEY / SCALESERP_KEY)"); return []
    urls, seen = [], set()
    pages = (want + 20) // 10 + 1
    for r in serp_search(f"site:{domain}/{recipe_path}", pages=pages, want=want * 3):
        link = r.get("link") or ""
        if _host(link) == domain and link not in seen and _is_recipe_url(link):
            seen.add(link); urls.append(link)
        if len(urls) >= want:
            break
    return urls[:want]


def harvest_moz(urls):
    """(url, da, pa) for each URL Moz can score."""
    out = []
    for u in urls:
        s = score_url_via_moz(u)
        if s and s.get("page_authority"):
            out.append((u, float(s["domain_authority"]), float(s["page_authority"])))
    return out


def calibrate(name, domain, samples, free_rows, conn, persist):
    """samples = list of (url, da, pa). Report + (optionally) persist."""
    if not samples:
        print(f"\n{name}: no data"); return
    das = [d for _, d, _ in samples]; pas = [pa for _, _, pa in samples]
    da = st.mean(das); mu = st.mean(pas); sd = st.pstdev(pas) or 0.01
    ref = free_ref(free_rows, da)
    print(f"\n{name}  ({domain})  n={len(pas)}  DA~{da:.0f}  PA μ={mu:.1f} σ={sd:.1f} range {min(pas):.0f}-{max(pas):.0f}")
    if not ref:
        print("  (no free DA-neighbors — cannot calibrate)"); return
    fmu, fsd, fn = ref
    slope = fsd / sd
    print(f"  free@DA±8 (n={fn}): μ={fmu:.1f} σ={fsd:.1f}  ->  shift={fmu-mu:+.1f}  slope={slope:.2f}")
    def adj_ou(pa): return (pa - mu) * slope
    print(f"  adjusted-OU  top={adj_ou(max(pas)):+.1f}  median={adj_ou(st.median(pas)):+.1f}  (winner bar ~+1.5..+7)")
    # top-N most-notable (by PA) — the ingestion / Editor's-Choice queue
    top = sorted(samples, key=lambda x: -x[2])[:TOP_N]
    print(f"  TOP {len(top)} by PA (most-notable → ingestion queue):")
    for u, _, pa in top:
        print(f"     PA {pa:4.0f}  {u[:78]}")
    if persist:
        domains_lib.set_paywall_calibration(
            conn, domain, da=da, pa_mean=mu, pa_std=sd, pa_n=len(pas),
            free_mean=fmu, free_std=fsd, paywall=True)
        print(f"  [persisted] domains.paywall=1 + calibration for {domain}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, no DB writes")
    args = ap.parse_args()
    persist = not args.dry_run
    conn = sqlite3.connect(DB)
    rows = corpus_da_pa(conn)
    paid_substr = ["cooking.nytimes.com", "americastestkitchen.com"]
    free_rows = [r for r in rows if not any(p in r[1] for p in paid_substr)]
    print(f"corpus: {len(rows)} scored pages ({len(free_rows)} free){'  [DRY RUN]' if args.dry_run else ''}")

    calibrate("NYT Cooking", "cooking.nytimes.com",
              [(u, d, pa) for (u, h, d, pa) in rows if "cooking.nytimes.com" in h],
              free_rows, conn, persist)
    calibrate("America's Test Kitchen", "americastestkitchen.com",
              [(u, d, pa) for (u, h, d, pa) in rows if "americastestkitchen.com" in h],
              free_rows, conn, persist)

    print("\n[harvest] Milk Street via SerpAPI (recipe-filtered) + Moz …")
    urls = serpapi_recipe_urls("177milkstreet.com", want=80)
    print(f"  {len(urls)} recipe URLs; Moz-scoring …")
    calibrate("Milk Street", "177milkstreet.com", harvest_moz(urls), free_rows, conn, persist)


if __name__ == "__main__":
    main()
