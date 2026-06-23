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
MIN_N = 15               # calibration sample floor (matches get_paywall_calibrations)
SIGMA_FLOOR_FRAC = 0.5   # floor σ_paid at this × σ_free → slope cap = 1/this = 2.0


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


def collection_members_da_pa(conn, domain):
    """(url, da, pa) samples for a publisher from its already-harvested collection
    members (Moz-scored at harvest). The reuse source for gated publishers whose
    recipe URLs DON'T live under a clean /recipe(s)/ path — e.g. Boston Globe
    (/YYYY/.../slug) — so serpapi_recipe_urls' path filter can't find them, but the
    publisher harvest already scored them. Free, no SERP/Moz spend."""
    rows = conn.execute(
        "SELECT url_normalized, da, pa FROM collection_members "
        "WHERE collection_type='publisher' AND collection_key=? "
        "AND da IS NOT NULL AND pa IS NOT NULL",
        (domain,),
    ).fetchall()
    return [(u, float(da), float(pa)) for u, da, pa in rows]


def calibrate(name, domain, samples, free_rows, conn, persist):
    """samples = list of (url, da, pa). Report + (optionally) persist."""
    if not samples:
        print(f"\n{name}: no data"); return
    das = [d for _, d, _ in samples]; pas = [pa for _, _, pa in samples]
    da = st.mean(das); mu = st.mean(pas); sd_raw = st.pstdev(pas) or 0.01
    ref = free_ref(free_rows, da)
    print(f"\n{name}  ({domain})  n={len(pas)}  DA~{da:.0f}  PA μ={mu:.1f} σ={sd_raw:.1f} range {min(pas):.0f}-{max(pas):.0f}")
    if len(pas) < MIN_N:
        print(f"  (only {len(pas)} samples — need ≥{MIN_N}; not calibrated)"); return
    if not ref:
        print("  (no free DA-neighbors — cannot calibrate)"); return
    fmu, fsd, fn = ref
    # σ-floor: a tiny, tightly-clustered paid σ makes the shift-and-scale slope
    # (σ_free/σ_paid) explode and over-rewards the single highest page — Boston Globe
    # at raw σ 1.8 → slope 3.24 → #1 of 249, an over-correction. Floor σ_paid at
    # SIGMA_FLOOR_FRAC × σ_free so the slope can't exceed 1/SIGMA_FLOOR_FRAC. We STORE
    # the floored σ, so every consumer (domain_scoring + the dish SQL scorer, both of
    # which recompute slope = σ_free/pa_std at scoring time) gets the bounded slope.
    # Analogous to the dish OU fit's EXC_SIGMA_FLOOR.
    sd = max(sd_raw, SIGMA_FLOOR_FRAC * fsd)
    slope = fsd / sd
    floored = f"  (σ floored {sd_raw:.1f}→{sd:.1f})" if sd > sd_raw + 1e-9 else ""
    print(f"  free@DA±8 (n={fn}): μ={fmu:.1f} σ={fsd:.1f}  ->  shift={fmu-mu:+.1f}  slope={slope:.2f}{floored}")
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
        print(f"  [persisted] calibration for {domain} (pa_std={sd:.2f})")


def _paywall_domains(conn):
    """The calibration worklist — every curator-flagged premium publisher, read from
    the domains master (DATA, not a hardcoded list). [(domain, display_name)]. A
    publisher is marked paywalled in the domains editor; this program picks the flag
    up. See memory/feedback_no_data_in_code + project_domain_master."""
    domains_lib.ensure_domains_table(conn)
    rows = conn.execute(
        "SELECT domain, COALESCE(NULLIF(display_name, ''), domain) "
        "FROM domains WHERE paywall = 1 ORDER BY domain"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _resolve_samples(conn, domain, corpus_rows, spend_ok=True):
    """Auto-find a publisher's (url, da, pa) PA samples with NO per-site config:
    prefer ingested corpus rows, then the already-harvested collection members
    (free), and only fall back to a live SERP site: harvest when nothing local
    clears the n≥MIN_N bar. `spend_ok=False` (the default — no --harvest-missing)
    skips the paid SERP fallback so a run never surprise-spends. Returns
    (source_label, samples)."""
    host = domain.replace("www.", "")
    corpus = [(u, d, pa) for (u, h, d, pa) in corpus_rows if host in h]
    members = collection_members_da_pa(conn, domain)
    best = max((("corpus", corpus), ("harvest", members)), key=lambda c: len(c[1]))
    if len(best[1]) >= MIN_N or not spend_ok:
        if len(best[1]) < MIN_N and not spend_ok:
            print(f"  [{domain}] local thin (corpus {len(corpus)}, harvest {len(members)}) "
                  f"→ would SERP-harvest with --harvest-missing (skipped)")
        return best
    # Nothing local is enough → SERP harvest (costs credits) as a last resort.
    print(f"  [{domain}] local samples thin (corpus {len(corpus)}, harvest {len(members)}) "
          f"→ live SERP harvest")
    serp = harvest_moz(serpapi_recipe_urls(domain, want=80))
    return max((("corpus", corpus), ("harvest", members), ("serp", serp)),
               key=lambda c: len(c[1]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, no DB writes")
    ap.add_argument("--only", default=None, help="calibrate ONLY this domain")
    ap.add_argument("--harvest-missing", action="store_true",
                    help="SERP-harvest publishers with too few LOCAL samples (costs credits); "
                         "off by default so a run never surprise-spends")
    args = ap.parse_args()
    persist = not args.dry_run
    conn = sqlite3.connect(DB)
    rows = corpus_da_pa(conn)
    flagged = _paywall_domains(conn)
    # Free reference excludes EVERY paywall-flagged host (read from the DB, not a code
    # constant) so a gated publisher's starved PA never poisons the free baseline.
    paid_hosts = [d for d, _ in flagged]
    free_rows = [r for r in rows if not any(p in r[1] for p in paid_hosts)]
    print(f"corpus: {len(rows)} scored pages ({len(free_rows)} free) | "
          f"{len(flagged)} paywall-flagged domain(s){'  [DRY RUN]' if args.dry_run else ''}")
    if not flagged:
        print("No paywall=1 domains in the master — flag a publisher in the domains "
              "editor first, then re-run.")
        return
    for domain, name in flagged:
        if args.only and args.only.replace("www.", "") != domain:
            continue
        src, samples = _resolve_samples(conn, domain, rows, spend_ok=args.harvest_missing)
        print(f"\n[{domain}] samples: {src} ({len(samples)})")
        calibrate(name, domain, samples, free_rows, conn, persist)


if __name__ == "__main__":
    main()
