"""How often did the _pick tie-break throw away a usable Moz answer?

THE BUG (fixed 2026-08-06, commit 226635a): `_pick` sorted variants into
`crawled` (http_code 200/301/302) and `estimated` (402) and dropped everything
else into a generic pool ranked by PA. Moz also returns 1, 3 and 5 with real
metrics. When no variant carried a standard code, the pool was ALL variants and
`max(..., key=PA)` broke ties by list order — so it could return a code-0 row,
which then failed the usable gate and the URL was reported as having no data.

WHY THIS NEEDS A CAREFUL SAMPLE. The obvious population — rows already in the
corpus — is useless: they scored under the old code, so by construction the old
`_pick` succeeded for every one of them. Sampling survivors would report zero
damage no matter how bad the bug was. And the genuinely affected population
(candidates dropped mid-harvest as "moz-unavailable") is never persisted:
`_moz_score` drops the entry and `metabase_url` keeps no failure record.

So this samples `dish_rejects` rows rejected for FETCH reasons — real recipe
URLs, rejected for something unrelated to Moz, therefore unbiased with respect
to the tie-break. A corpus sample runs alongside purely as the survivor-biased
control; expect ~0 failures there, and treat any as a red flag rather than a
result.

Replays BOTH algorithms against ONE Moz response per URL, so the comparison
costs nothing extra and cannot drift between them.

Usage:
    python -m scripts.sample_tiebreak_damage --limit 120
"""
from __future__ import annotations

import argparse
import base64
import os
import random
import sqlite3
import sys
from pathlib import Path

os.environ.setdefault("BCC_SKIP_JOB_RESET", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from input.pipeline import url_scoring as us  # noqa: E402

DB = str(Path(__file__).resolve().parents[1] / "recipes.db")
COST_PER_ROW = 250 / 120_000


def _pa(r):
    return r.get("page_authority") or 0


def pick_old(paired):
    """The pre-2026-08-06 tier logic, verbatim."""
    crawled = [(u, r) for u, r in paired if r.get("http_code") in (200, 301, 302)]
    estimated = [(u, r) for u, r in paired if r.get("http_code") == 402]
    pool = crawled or estimated or paired
    if not pool:
        return None, False
    cu, cr = max(pool, key=lambda ur: _pa(ur[1]))
    return cr, bool(cr.get("http_code"))


def pick_new(paired):
    """With the has_data tier."""
    crawled = [(u, r) for u, r in paired if r.get("http_code") in (200, 301, 302)]
    estimated = [(u, r) for u, r in paired if r.get("http_code") == 402]
    has_data = [(u, r) for u, r in paired if r.get("http_code")]
    pool = crawled or estimated or has_data or paired
    if not pool:
        return None, False
    cu, cr = max(pool, key=lambda ur: _pa(ur[1]))
    return cr, bool(cr.get("http_code"))


def run(label, urls, auth):
    print(f"\n=== {label}  (n={len(urls)}) ===")
    recovered, both_ok, both_fail, nonstandard = [], 0, 0, 0
    for i, u in enumerate(urls, 1):
        cands = us._url_variants(u)
        res = us._moz_url_metrics(cands, auth)
        if res is None:
            continue
        paired = list(zip(cands, res)) if len(res) == len(cands) else [(None, r) for r in res]
        if not any(r.get("http_code") in (200, 301, 302, 402) for _, r in paired) \
           and any(r.get("http_code") for _, r in paired):
            nonstandard += 1
        o_row, o_ok = pick_old(paired)
        n_row, n_ok = pick_new(paired)
        if o_ok and n_ok:
            both_ok += 1
        elif not o_ok and n_ok:
            recovered.append((u, n_row.get("http_code"), _pa(n_row)))
            print(f"  [{i:>3}] RECOVERED code={n_row.get('http_code')} pa={_pa(n_row)}  {u[:66]}")
        elif not o_ok and not n_ok:
            both_fail += 1
    n = len(urls)
    print(f"\n  old OK, new OK          : {both_ok:>4}")
    print(f"  old FAILED, new OK      : {len(recovered):>4}   <- tie-break damage"
          f"   ({len(recovered)/n*100:.1f}% of sample)" if n else "")
    print(f"  both failed (no data)   : {both_fail:>4}")
    print(f"  responses with NO standard code but real data: {nonstandard}"
          f"   (the at-risk condition)")
    return len(recovered), n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    if not us.MOZ_ACCESS_ID:
        print("no Moz creds"); return 1
    random.seed(a.seed)
    auth = base64.b64encode(f"{us.MOZ_ACCESS_ID}:{us.MOZ_SECRET_KEY}".encode()).decode()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    rejects = [r[0] for r in con.execute(
        "SELECT DISTINCT url FROM dish_rejects WHERE reason LIKE 'fetch-failed%' AND url LIKE 'http%'")]
    corpus = [r[0] for r in con.execute(
        "SELECT DISTINCT json_extract(data,'$._source.originalUrl') u FROM master_recipes "
        "WHERE u LIKE 'http%'")]
    rej_s = random.sample(rejects, min(a.limit, len(rejects)))
    cor_s = random.sample(corpus, min(a.limit // 3, len(corpus)))

    us.reset_moz_row_stats()
    d1, n1 = run("UNBIASED — fetch-failed rejects", rej_s, auth)
    d2, n2 = run("CONTROL — corpus survivors (expect ~0 by construction)", cor_s, auth)
    rows = us.moz_row_stats()["rows"]
    print(f"\nmoz rows spent: {rows}  (~${rows * COST_PER_ROW:.2f})")
    if n1:
        print(f"\nESTIMATED TIE-BREAK DAMAGE RATE: {d1/n1*100:.1f}% of scoring attempts "
              f"(unbiased sample, n={n1})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
