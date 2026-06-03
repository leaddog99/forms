"""Populate the domain master from existing data.

Ensures a `domains` row for every host we actually have recipes from (not
just the ~95 seeded from the JSON name map), and enriches each with what the
corpus already knows:

  - display_name      a captured _source.siteName sample, else the seed map
  - root_domain       registrable domain (for DA sharing + root-grain blocks)
  - domain_authority  max DA seen for the root (from recipes' _scoring and
                      metabase_url) — only filled when currently NULL
  - master/user recipe counts (+ counts_updated_at)

Idempotent and edit-safe: display_name is only set when blank, DA only when
NULL, the allowed flag is never touched — so curator edits and the disallowed
seed survive a re-run. Also (re)seeds the JSON names and the config disallowed
list as allowed=0 rows.

Usage:
  python -m scripts.populate_domains            # dry run (report only)
  python -m scripts.populate_domains --apply    # write changes
"""

import argparse
import json
import sqlite3
from collections import defaultdict

from input.pipeline import domains_lib
from input.pipeline.site_names import host_from_url, friendly_site_name, _SELF_HOSTS
from input.pipeline.url_utils import root_domain
from input.pipeline.config import DISALLOWED_DOMAINS

DB = "recipes.db"


def _root_of(host: str) -> str:
    return root_domain("http://" + host) or host


def main(apply: bool) -> None:
    conn = sqlite3.connect(DB)
    domains_lib.ensure_domains_table(conn)

    # 1. Bootstrap seeds (idempotent): JSON names + disallowed list.
    seeded_names = domains_lib.seed_domains(conn) if apply else 0
    seeded_block = domains_lib.seed_disallowed_domains(conn, DISALLOWED_DOMAINS) if apply else len(DISALLOWED_DOMAINS)

    # 2. Walk the corpus: per-host siteName sample + counts; per-root max DA.
    host_site: dict = defaultdict(str)
    host_master: dict = defaultdict(int)
    host_user: dict = defaultdict(int)
    root_da: dict = defaultdict(float)
    for tbl, is_master in (("master_recipes", True), ("recipes", False)):
        for url_norm, dj in conn.execute(f"SELECT url_normalized, data FROM {tbl}"):
            try:
                d = json.loads(dj)
            except Exception:
                continue
            src = d.get("_source") or {}
            host = host_from_url(url_norm or src.get("originalUrl") or "")
            if not host or host in _SELF_HOSTS:
                continue
            (host_master if is_master else host_user)[host] += 1
            sn = (src.get("siteName") or "").strip()
            if sn and not host_site[host]:
                host_site[host] = sn
            da = (d.get("_scoring") or {}).get("domainAuthority")
            if da is not None:
                try:
                    r = _root_of(host)
                    root_da[r] = max(root_da[r], float(da))
                except Exception:
                    pass

    # metabase_url carries DA per root too — fold it in.
    try:
        for r, da in conn.execute(
            "SELECT root_domain, MAX(domain_authority) FROM metabase_url "
            "WHERE domain_authority IS NOT NULL GROUP BY root_domain"
        ):
            if r and da is not None:
                root_da[r] = max(root_da.get(r, 0.0), float(da))
    except Exception:
        pass

    all_hosts = sorted(set(host_master) | set(host_user))
    now = domains_lib._now()
    existing = {r["domain"] for r in domains_lib.list_domains(conn)}
    created = updated = da_filled = 0

    for host in all_hosts:
        r = _root_of(host)
        da = root_da.get(r)
        name = host_site[host] or friendly_site_name("", "http://" + host) or ""
        is_new = host not in existing
        if is_new:
            created += 1
        else:
            updated += 1
        if da is not None and (is_new or domains_lib.get_domain(conn, host).get("domain_authority") is None):
            da_filled += 1
        if apply:
            conn.execute(
                """
                INSERT INTO domains (domain, root_domain, display_name, domain_authority,
                                     master_recipe_count, user_recipe_count,
                                     counts_updated_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    root_domain         = excluded.root_domain,
                    display_name        = CASE WHEN domains.display_name = ''
                                               THEN excluded.display_name ELSE domains.display_name END,
                    domain_authority    = COALESCE(domains.domain_authority, excluded.domain_authority),
                    master_recipe_count = excluded.master_recipe_count,
                    user_recipe_count   = excluded.user_recipe_count,
                    counts_updated_at   = excluded.counts_updated_at,
                    updated_at          = excluded.updated_at
                """,
                (host, r, name, da, host_master.get(host, 0), host_user.get(host, 0),
                 now, now, now),
            )

    if apply:
        conn.commit()
        domains_lib.invalidate_cache()
    total = conn.execute("SELECT COUNT(*) FROM domains").fetchone()[0]
    conn.close()

    print("APPLIED" if apply else "DRY RUN (use --apply to write)")
    print(f"  name seeds (JSON):        {seeded_names}")
    print(f"  disallowed seeds (block): {seeded_block}")
    print(f"  hosts from corpus:        {len(all_hosts)}  ({created} new, {updated} existing)")
    print(f"  domain_authority filled:  {da_filled}")
    print(f"  domains table total:      {total}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    main(ap.parse_args().apply)
