"""Paywall DA-adjustment — `pa_gap_v1`.

WHAT THIS CORRECTS
------------------
A gated publisher's recipe pages earn less PAGE authority than free pages do,
because links accrue to what people can read. But DA is measured across the
WHOLE domain — bostonglobe.com is DA 91 on the strength of its free news — and
the recipes sit behind the wall. OU is `PA - bar(DA)`, so a gated recipe is
judged against an expectations bar set by an ungated domain. It loses on the
paywall, not on quality: measured 2026-08-12, the median Boston Globe recipe
scored OU -2.04 against free peers at the same DA scoring +10.65.

WHY NOT THE OLD REMAP (pa_cal_*, shift-and-scale)
-------------------------------------------------
The superseded method rewrote PA — the only thing we actually measured — as
`free_mean + (PA - paid_mean) * (free_std / paid_std)`. Two faults:

  1. `free_std` was pooled across a DA+-8 window, so it carried BETWEEN-site
     variance, while `paid_std` was a single publisher's WITHIN-site spread.
     Dividing one by the other is apples-to-oranges. It pinned the slope at its
     2.0 cap for 3 of 5 publishers and DOUBLED each page's distance from its
     publisher's mean.
  2. Unbounded. It produced a Boston Globe OU of +31.70 when the highest OU
     across 4,896 master rows is +25.38, and would emit PA > 100 (an impossible
     Moz value) at raw PA >= 70 on a DA-91 site.

The premise that the within-site spread was too tight to rank on also failed on
its own data: the Globe's PA sigma is 2.75 over a 16-point range, which is
mid-pack for this corpus — free cohorts at DA 84, 71 and 69 discriminate LESS.
The spread was never the problem.

THE METHOD
----------
Measure the penalty where it is actually observable — the PA gap against free
publishers at matched DA — then move the BAR by exactly that much and leave PA
alone:

    gap        = mean_free_PA(DA) - mean_publisher_PA
    adjusted_DA = ou_bar_inverse(ou_bar(DA) - gap)

One measured quantity, one bounded transform, nothing extrapolated. `gap <= 0`
means the publisher is NOT starved and earns no adjustment at all.

GATED IS NOT THE SAME AS PENALIZED
----------------------------------
cooking.nytimes.com is hard-gated (it cannot even be fetched without the
unblocker) yet its pages average PA 59.8 against free DA-95 peers at 58.9 — a
gap of -0.8. It is linked heavily enough to overcome its own wall. So the
`paywall` FLAG (is it gated) and the DA discount (is it penalized, and by how
much) are separate facts and neither is derived from the other.
"""

from __future__ import annotations

import json
import statistics as st
from typing import Optional

from input.pipeline import domains_lib
from input.pipeline.url_scoring import ou_bar, ou_bar_inverse

METHOD = "pa_gap_v1"

# A publisher needs this many scored recipes before its gap is trustworthy.
# Below it we still COMPUTE and record the number (so the curator can see it
# forming) but mark it low-confidence and do not apply it.
MIN_N = 12

# How large the gap must be relative to the ORDINARY page-to-page spread of the
# free peer cohort (a Cohen's-d style effect size) before it counts as a measured
# tax. 1.0 = the publisher's pages run a full peer-sigma below their DA cohort.
# Deliberately NOT a standard-error test: SE shrinks with sqrt(n), so a large
# sample would wave through a 2-point gap that means nothing.
MIN_EFFECT = 1.0

# Free-peer window over DA, widened only until the reference is big enough.
_PEER_WINDOWS = (0, 2, 3, 5, 8)
_MIN_PEERS = 25


def _peer_pa(free_rows: list[dict], da: float):
    """(mean, sd, n, window) of FREE publishers' PA at matched DA.

    Starts at an exact-DA match and widens only as needed: the whole point is a
    like-for-like comparison, and every extra DA point of window mixes in sites
    with a different expectations bar."""
    vals: list[float] = []
    for w in _PEER_WINDOWS:
        vals = [r["pa"] for r in free_rows if abs(r["da"] - da) <= w]
        if len(vals) >= _MIN_PEERS:
            return st.mean(vals), st.pstdev(vals), len(vals), w
    if not vals:
        return None, None, 0, None
    return st.mean(vals), st.pstdev(vals), len(vals), _PEER_WINDOWS[-1]


def _sign_stable(free_rows: list[dict], da: float, avg_pa: float) -> bool:
    """Does the publisher read as starved at EVERY usable peer window?

    The window is a judgment call (how far in DA a 'comparable' site is), so any
    verdict that depends on it is an artifact. cooking.nytimes.com is the case
    this exists for: starved at exact-DA, not starved at DA±2."""
    seen = 0
    for w in _PEER_WINDOWS:
        vals = [r["pa"] for r in free_rows if abs(r["da"] - da) <= w]
        if len(vals) < _MIN_PEERS:
            continue
        seen += 1
        if st.mean(vals) - avg_pa <= 0:
            return False
    return seen > 0


def compute_gap(rows: list[dict], free_rows: list[dict]) -> dict:
    """Gap + DA haircut for ONE publisher.

    `rows` = that publisher's scored pages [{pa, da}]; `free_rows` = every
    scored page belonging to a publisher that is NOT flagged gated. Returns a
    dict carrying the verdict AND every input behind it, which is what gets
    persisted — a bare percentage with no provenance can't be audited later or
    re-derived when the method changes."""
    if not rows:
        return {"status": "no_rows", "n": 0}

    da = st.median([r["da"] for r in rows])
    avg_pa = st.mean([r["pa"] for r in rows])
    pub_sd = st.pstdev([r["pa"] for r in rows]) if len(rows) > 1 else 0.0
    peer_avg, peer_sd, peer_n, window = _peer_pa(free_rows, da)
    if peer_avg is None:
        return {"status": "no_free_reference", "n": len(rows), "da_measured": da}

    gap = peer_avg - avg_pa
    # EFFECT SIZE, not statistical significance. A standard-error test is the
    # wrong instrument here: SE shrinks with sqrt(n), so with 89 rows even a
    # 2.2-point gap clears 3 SE and "passes" while being visibly meaningless.
    # What matters is the gap against how much individual pages vary anyway —
    # measured 2026-08-12, cooking.nytimes.com's gap of 2.21 is under half the
    # peer spread (σ 4.57 at DA 95), and its verdict flipped SIGN between an
    # exact-DA window (+2.21, starved) and DA±2 (-0.80, not starved).
    effect = gap / peer_sd if peer_sd else 0.0
    # ...and require the sign to survive the window choice, which is what
    # actually caught NYT. A tax that exists only at one peer window is an
    # artifact of who happened to land in the comparison set.
    stable = _sign_stable(free_rows, da, avg_pa)

    base = {
        "n": len(rows), "da_measured": round(da, 1), "avg_pa": round(avg_pa, 2),
        "pub_sd": round(pub_sd, 2), "peer_avg_pa": round(peer_avg, 2),
        "peer_sd": round(peer_sd, 2), "peer_n": peer_n, "peer_window": window,
        "pa_gap": round(gap, 2), "effect": round(effect, 2),
        "window_stable": stable, "method": METHOD,
    }

    # Not starved: the publisher already matches or beats its ungated peers, so
    # there is no paywall tax to refund. NOT a zero discount — no discount.
    if gap <= 0:
        return {**base, "status": "no_penalty"}
    if len(rows) < MIN_N:
        return {**base, "status": "low_confidence", "min_n": MIN_N}
    # Gap is real in sign but small against the ordinary page-to-page spread, or
    # it evaporates at a different peer window. Treat as NO evidence of a tax
    # rather than as a small tax: adjusting on a sub-threshold gap is how a
    # publisher sitting at parity quietly acquires a permanent bonus.
    if effect < MIN_EFFECT or not stable:
        return {**base, "status": "inconclusive",
                "min_effect": MIN_EFFECT,
                "why": ("window-unstable" if not stable else
                        f"effect {effect:.2f} < {MIN_EFFECT}")}

    adjusted = ou_bar_inverse(ou_bar(da) - gap)
    discount = 100.0 * (1.0 - adjusted / da)
    return {**base, "status": "adjusted",
            "da_adjusted": round(adjusted, 1), "discount_pct": round(discount, 1)}


def _explain(res: dict) -> str:
    """One line a curator can act on, for the domain record and the job log.

    Every 'no adjustment' outcome has a different remedy — harvest the
    publisher, wait for more rows, or accept that it isn't penalized — and a
    blank field tells you none of them."""
    s = res.get("status")
    n, gap = res.get("n"), res.get("pa_gap")
    if s == "adjusted":
        return (f"Pages run {gap:.1f} PA below free publishers at DA "
                f"{res['da_measured']:.0f} (effect {res['effect']:.2f} of the peer "
                f"spread, n={n}); DA discounted {res['discount_pct']:.1f}%.")
    if s == "no_rows":
        return ("No scored recipes for this publisher yet, so the paywall flag has "
                "never been tested. Run a publisher refresh to gather evidence.")
    if s == "no_penalty":
        return (f"Not starved: its pages match or beat free publishers at the same DA "
                f"(gap {gap:+.1f}). Gated, but earning normal authority — no discount "
                f"is warranted.")
    if s == "low_confidence":
        return (f"Only {n} scored recipes (need {res.get('min_n', MIN_N)}). A gap of "
                f"{gap:+.1f} is showing but the sample is too thin to act on.")
    if s == "inconclusive":
        why = res.get("why", "")
        if "window-unstable" in why:
            return (f"Gap of {gap:+.1f} reverses sign depending on which free peers it "
                    f"is compared against, so it is an artifact of the comparison set, "
                    f"not a measured tax.")
        return (f"Gap of {gap:+.1f} is small against the ordinary page-to-page spread "
                f"(effect {res.get('effect', 0):.2f}, needs {MIN_EFFECT}). Too close to "
                f"noise to act on.")
    if s == "no_free_reference":
        return ("No free publishers at a comparable DA to measure against.")
    return s or "unknown"


def calibrate(conn, *, persist: bool = True) -> dict:
    """Recompute every flagged publisher's DA adjustment from current corpus data.

    Reads master_recipes directly: the corpus IS the sample, so unlike the old
    method there is no SERP harvest, no Moz spend, and nothing to keep in sync."""
    domains_lib.ensure_domains_table(conn)
    flagged = [r[0] for r in conn.execute(
        "SELECT domain FROM domains WHERE paywall = 1 ORDER BY domain")]
    if not flagged:
        return {"method": METHOD, "flagged": 0, "results": []}

    # SAMPLE SOURCE: master_recipes.
    #
    # These rows ARE selected — a URL only lands here by surviving a run's cut,
    # and that cut ranks on OU. But the requirement is not that the sample be
    # unselected; it is that the gated publisher and its free reference pass
    # through the SAME selection regime, so the filtering cancels in the
    # difference. master_recipes satisfies that: both sides are run survivors.
    #
    # The candidate ledger was tried as a "pre-selection" alternative and is
    # WORSE, measured 2026-08-12. It is ~75% rejects, so it compares a gated
    # publisher's fresh top-traffic harvest against other runs' DISCARDS. Its
    # free-peer pools were n=38 and n=23 averaging PA 45.3/50.3, against
    # master's n=268/n=114 at 54.6/62.2 — and it reported cooking.nytimes.com
    # OUTSCORING its peers by 12.7 points, which is composition, not signal.
    # Revisit only once the ledger holds comparable harvests on BOTH sides.
    #
    # Residual known bias, not corrected: truncating two distributions at one
    # OU threshold lifts the lower one more, so a survivors-only gap is
    # COMPRESSED. The number below is therefore a conservative floor on the
    # real paywall tax — which is the safe direction to err for a correction
    # whose failure mode is manufacturing record-breaking scores.
    rows = []
    for url, pa, da in conn.execute(
        "SELECT url_normalized, "
        "       json_extract(data,'$._scoring.pageAuthority'), "
        "       json_extract(data,'$._scoring.domainAuthority') "
        "FROM master_recipes "
        "WHERE json_extract(data,'$._scoring.pageAuthority') IS NOT NULL "
        "  AND json_extract(data,'$._scoring.domainAuthority') IS NOT NULL"
    ):
        # Match on the URL's real host, not the stored apex — see
        # domains_lib.adjustment_for_url for why the apex cannot be trusted.
        rows.append({"host": domains_lib._canon_host(url or ""),
                     "pa": float(pa), "da": float(da)})

    flagged_set = set(flagged)

    def _owner(r):
        """The flagged publisher this row belongs to, walking up the host labels."""
        labels = r["host"].split(".")
        for i in range(len(labels) - 1):
            cand = ".".join(labels[i:])
            if cand in flagged_set:
                return cand
        return None

    for r in rows:
        r["owner"] = _owner(r)
    free_rows = [r for r in rows if r["owner"] is None]

    results = []
    for dom in flagged:
        # A curator who set this discount by hand owns it. Skip the row
        # entirely — recomputing and overwriting would revert their decision
        # silently on a schedule, which is the failure mode that makes people
        # stop trusting an override.
        if persist and domains_lib.paywall_adjustment_is_manual(conn, dom):
            results.append({"domain": dom, "status": "manual",
                            "note": "curator-owned; calibration skipped"})
            continue

        res = compute_gap([r for r in rows if r["owner"] == dom], free_rows)
        res["domain"] = dom
        res["note"] = _explain(res)
        results.append(res)
        if not persist:
            continue
        if res["status"] == "adjusted":
            domains_lib.set_paywall_da_adjustment(
                conn, dom, discount_pct=res["discount_pct"],
                da_adjusted=res["da_adjusted"], method=METHOD,
                inputs={**{k: res[k] for k in
                           ("da_measured", "avg_pa", "peer_avg_pa", "peer_n",
                            "peer_window", "pa_gap", "n")},
                        # Which pool the gap was measured on. Recorded because
                        # the answer moves with it (ledger vs master differed by
                        # 7-15 PA points on the two publishers holding both), so
                        # a stored discount is uninterpretable without it.
                        "sample_source": "master_recipes"},
                n=res["n"], note=res["note"])
        else:
            # no_rows / no_penalty / no_free_reference / low_confidence /
            # inconclusive all mean "we are not adjusting this publisher" — clear
            # the discount so it can never outlive its evidence, but KEEP the
            # status and reason so the domain record says which of the five it is.
            domains_lib.clear_paywall_da_adjustment(
                conn, dom, status=res["status"], note=res["note"],
                n=res.get("n"))

    restamped = restamp_recipes(conn) if persist else {}

    return {"method": METHOD, "flagged": len(flagged),
            "adjusted": sum(1 for r in results if r["status"] == "adjusted"),
            "corpus_rows": len(rows), "free_rows": len(free_rows),
            "restamped": restamped, "results": results}


def restamp_recipes(conn, tables=("master_recipes", "recipes")) -> dict:
    """Refresh the stored paywall adjustment on every affected recipe row.

    This is what makes persisting the adjustment safe: stored derived values go
    stale when the calibration moves, and the answer is to RE-STAMP on
    recalibration rather than to refuse to store (which would put the number out
    of SQL's reach entirely). Same treatment `ou`/`power`/`rank_score` already
    get from their rescore jobs.

    Rows whose publisher LOST its adjustment are re-stamped too — the stamper
    clears the keys — so a publisher that falls below the evidence bar does not
    keep an unearned lift."""
    from input.pipeline.url_scoring import stamp_paywall_adjustment, reset_paywall_cache
    reset_paywall_cache()   # the calibration just moved; drop the cached copy

    out = {}
    for tbl in tables:
        try:
            rows = conn.execute(
                f"SELECT id, data FROM {tbl} "
                f"WHERE json_extract(data,'$._scoring.pageAuthority') IS NOT NULL "
                f"  AND json_extract(data,'$._scoring.domainAuthority') IS NOT NULL"
            ).fetchall()
        except Exception:
            continue
        changed = 0
        for rid, blob in rows:
            # Narrow: catch a genuinely malformed row, NOT programmer error. A
            # bare `except Exception` here swallowed a NameError on all 4,898
            # rows and reported "0 re-stamped" as though the data were simply
            # unchanged — a silent no-op that looked exactly like success.
            try:
                data = json.loads(blob)
            except (ValueError, TypeError):
                continue
            sc = data.get("_scoring") or {}
            if not sc:
                continue
            before = (sc.get("adjustedDomainAuthority"), sc.get("adjustedOuScore"))
            url = (data.get("_source") or {}).get("originalUrl") or ""
            stamp_paywall_adjustment(sc, url)
            if (sc.get("adjustedDomainAuthority"), sc.get("adjustedOuScore")) == before:
                continue
            data["_scoring"] = sc
            conn.execute(f"UPDATE {tbl} SET data = ? WHERE id = ?",
                         (json.dumps(data, ensure_ascii=False), rid))
            changed += 1
        out[tbl] = changed
    conn.commit()
    return out
