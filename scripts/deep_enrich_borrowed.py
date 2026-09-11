"""Deep-enrich every BORROWED, never-harvested domain (the orange dot on the
domains list) and size each by the DA rule — one in-process pass, journaled.

Curator 2026-09-11: "do a deep enrich on all the sites that currently have an
orange dot and also do the formula for determining the sample size, n, and the
180 ttl." The 59 punchfork-borrowed publishers were imported 09-10 with a DA but
no profile/story/known_for, and their keep/records/ttl still carry the pre-rule
table defaults (20/100/90).

Per domain:
  1. extract.domain_enrich.deep_enrich_domain  (Moz V3 facts + one Sonnet call,
     grounded on the homepage) — the SAME call and the SAME persisted field set
     as the create-path auto-enrich in save_recipe_api.create_domain_endpoint.
  2. domains_lib.rule_defaults_for_da(DA)  → keep_top_n / harvest_records /
     harvest_ttl_days. Unlike the create path this OVERWRITES the three sizing
     columns — the curator asked for the formula to be applied to these rows —
     and prints before → after for every row it touches.

Usage:
  python scripts/deep_enrich_borrowed.py            # dry run: list + rule preview
  python scripts/deep_enrich_borrowed.py --apply    # enrich + persist + size
  python scripts/deep_enrich_borrowed.py --apply --only host1,host2
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from input.pipeline import domains_lib  # noqa: E402

PERSIST_SCALARS = ("profile", "story", "language", "country", "cuisine_focus",
                   "ethnicity", "logo_url", "brand_authority", "referring_domains",
                   "domain_authority")
PERSIST_JSON = ("ranking_keywords", "known_for")
SIZING = ("keep_top_n", "harvest_records", "harvest_ttl_days")


def orange_dot_domains(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM domains WHERE discovery_source IS NOT NULL AND discovery_source <> '' "
        "AND last_harvested_at IS NULL AND harvestable = 1 ORDER BY domain_authority DESC, domain")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="", help="restrict the orange-dot set to these hosts")
    ap.add_argument("--hosts", default="", help="explicit hosts, orange dot or not (same per-domain path)")
    ap.add_argument("--no-size", action="store_true", help="enrich only; leave keep/records/ttl as stored")
    args = ap.parse_args()
    only = {h.strip().lower() for h in args.only.split(",") if h.strip()}
    hosts = [h.strip().lower() for h in args.hosts.split(",") if h.strip()]

    conn = domains_lib._connect(domains_lib._DEFAULT_DB)
    conn.row_factory = __import__("sqlite3").Row
    if hosts:
        rows = [dict(r) for h in hosts for r in conn.execute("SELECT * FROM domains WHERE domain = ?", (h,))]
    else:
        rows = orange_dot_domains(conn)
    if only:
        rows = [r for r in rows if r["domain"].lower() in only]
    print(f"{len(rows)} orange-dot domain(s) {'— APPLY' if args.apply else '— DRY RUN'}")

    if args.apply:
        import llm
        from extract.domain_enrich import deep_enrich_domain

    n_ok = n_fail = 0
    t0 = time.time()
    for i, row in enumerate(rows, 1):
        host = row["domain"]
        before = {k: row[k] for k in SIZING}
        sets: dict = {}
        if args.apply:
            llm.enter(recipe_id=f"domain:{host}", user_id=0)
            try:
                result = deep_enrich_domain(host, display_name=row["display_name"] or "")
            except Exception as e:
                result = None
                print(f"[{i:>2}/{len(rows)}] {host}: deep enrich raised {type(e).__name__}: {e}")
            finally:
                try:
                    llm.flush()
                except Exception:
                    pass
            if result:
                for f in PERSIST_SCALARS:
                    v = result.get(f)
                    if v not in (None, "", []):
                        sets[f] = v
                for f in PERSIST_JSON:
                    v = result.get(f)
                    if v:
                        sets[f] = json.dumps(v)
                sets["enriched_at"] = datetime.now(timezone.utc).isoformat()
                n_ok += 1
            else:
                n_fail += 1
        da = sets.get("domain_authority", row["domain_authority"])
        rule = domains_lib.rule_defaults_for_da(da)
        # Mixed-media publishers (realsimple, bhg) carry a DA their recipe section
        # did not earn — the curator sized them by hand on 09-10. Keep that keep;
        # only the multiplier and the TTL follow the rule.
        if row["mixed_media"] and before["keep_top_n"]:
            rule["keep_top_n"] = before["keep_top_n"]
            from input.pipeline.system_config import get_setting
            rule["harvest_records"] = before["keep_top_n"] * int(get_setting("domain_records_per_keep", 6) or 6)
        after = {k: rule.get(k, before[k]) for k in SIZING}
        changed = {k: (before[k], after[k]) for k in SIZING if before[k] != after[k]}
        if args.no_size:
            rule = {}
            after = dict(before)
            changed = {}
        if args.apply:
            sets.update(rule)
            if sets:
                domains_lib.update_domain(conn, host, sets)
                conn.commit()
        prof = (sets.get("profile") or "")
        print(f"[{i:>2}/{len(rows)}] {host:34} DA {da!s:>5}  "
              f"keep/records/ttl {before['keep_top_n']}/{before['harvest_records']}/{before['harvest_ttl_days']}"
              f" → {after['keep_top_n']}/{after['harvest_records']}/{after['harvest_ttl_days']}"
              f"{'  (unchanged)' if not changed else ''}"
              f"{'  profile ' + str(len(prof)) + ' chars' if prof else ''}")
    conn.close()
    print(f"done in {time.time() - t0:.0f}s — enriched {n_ok}, failed {n_fail}, sized {len(rows)}")
    return 0 if not n_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
