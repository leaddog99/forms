"""Audit every curated pick's listing identity with the shared scorer.

    python scripts/audit_pick_identity.py            # score stored picks (free)
    python scripts/audit_pick_identity.py --fix      # re-resolve rejects via Google + Traject

Scores the research model's product name against the listing title we stored
(verified_title, or the listing quoted in an ASIN-mismatch warning) and prints
the band counts plus every weak/reject row, flagging disagreements with the
curator's acks. --fix runs the Google candidate finder on each reject, verifies
the best candidate with the Traject listing lookup, and writes it onto the
pick through the same path the editor's fix-ASIN control uses.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

from input.pipeline.db import connect as db_connect  # noqa: E402
from intake.products.curate.identity import identity_score  # noqa: E402

DB = os.path.join(ROOT, "recipes.db")
_WARN_TITLE = re.compile(r"looks like a different product \(.*?\): (.*)$")


def listing_title_of(row: dict) -> str:
    if row.get("verified_title"):
        return row["verified_title"]
    m = _WARN_TITLE.search(row.get("identity_warning") or "")
    return m.group(1).strip() if m else ""


def audit(conn, verbose: bool = True) -> dict:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM curated_collection_picks WHERE COALESCE(excluded,0)=0 "
        "ORDER BY collection, section, place")]
    bands = {"verified": 0, "weak": 0, "reject": 0, "no-listing": 0}
    flagged = []
    for r in rows:
        lt = listing_title_of(r)
        if not lt:
            bands["no-listing"] += 1
            continue
        info = identity_score(r, lt)
        bands[info["verdict"]] += 1
        acked = bool(r.get("warning_ack"))
        if info["verdict"] != "verified" or (acked and info["verdict"] == "reject"):
            flagged.append((info["verdict"], r["collection"], r["place"], r["asin"] or "",
                            r["product_title"][:48], lt[:60], info["score"], info["why"],
                            "ACKED" if acked else ""))
    if verbose:
        print("picks:", len(rows), " bands:", bands)
        for v in ("reject", "weak"):
            print(f"\n--- {v.upper()} ---")
            for f in flagged:
                if f[0] == v:
                    print(f"[{f[1]} #{f[2]}] {f[3]:10s} {f[8]:5s} score={f[6]}  {f[7]}")
                    print(f"     pick:    {f[4]}")
                    print(f"     listing: {f[5]}")
    return {"bands": bands, "flagged": flagged, "rows": rows}


def fix(conn, rows: list, dry: bool = False) -> None:
    """Re-resolve every reject the curator has NOT acknowledged. `dry` stops
    after the candidate search (SERP credit only): prints what would be
    chosen, verifies and writes nothing."""
    from intake.products.curate import verify as V
    from intake.products import curated_collections as ccs
    for r in rows:
        lt = listing_title_of(r)
        if not lt or identity_score(r, lt)["verdict"] != "reject" or r.get("warning_ack"):
            continue
        label = f"{r['collection']}/{r['slot']}"
        pick = {k: r.get(k) or "" for k in ("manufacturer", "product_title", "capacity",
                                             "model_number", "buy_link")}
        found = V.resolve_asin(conn, pick, label=label, prior_asin=r.get("asin") or "")
        if not found:
            print(f"[fix] {label}: no listing matched — leaving as is")
            continue
        if dry:
            print(f"[dry] {label}: {r.get('asin') or '(blank)'} -> {pick.pop('_candidate_note')}")
            continue
        report = V.enrich_one(conn, pick, label=label)
        if not pick.get("verified_title"):
            # Search said yes, the live listing said no. Blank beats a second
            # wrong ASIN; the warning records what was tried.
            tried = pick.get("amazon_asin")
            pick["amazon_asin"] = ""
            pick["amazon_link"] = ""
            pick["asin_source"] = f"blank ({r.get('asin') or 'none'} and google's {tried} both rejected)"
        ok = ccs.apply_pick_asin(conn, r["collection"], r["slot"], pick)
        print(f"[fix] {label}: {r.get('asin') or '(blank)'} -> {pick.get('amazon_asin')} "
              f"[{pick.get('asin_source')}] {'verified' if pick.get('verified_title') else pick.get('identity_warning')} "
              f"score={pick.get('identity_score')} {'written' if ok else 'NOT written'}")
        for n in report.get("rejected") or []:
            print("      ", n)


if __name__ == "__main__":
    conn = db_connect(DB)
    conn.row_factory = __import__("sqlite3").Row
    res = audit(conn, verbose="--quiet" not in sys.argv)
    if "--dry" in sys.argv:
        print("\n=== DRY: candidates for un-acked rejects (SERP only, no writes) ===")
        fix(conn, res["rows"], dry=True)
    elif "--fix" in sys.argv:
        print("\n=== FIX rejects via Google + Traject ===")
        fix(conn, res["rows"])
        print("\n=== AFTER ===")
        audit(conn)
    conn.close()
