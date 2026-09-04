"""Render a validated curation result as a WRITTEN BRIEF.

EXPERIMENTAL. The curator asked for text rather than a spreadsheet — which is the better
target anyway: a workbook is a container, and what we actually want to publish is prose a
reader can follow, with the evidence attached to each claim.

The three worksheets become three sections (Top Three Overall / By Category / How We Ranked),
and two things the spreadsheet could not carry come with them:

  * the OWNER EVIDENCE line — real star histogram, count, and the computed index, so the
    reader sees arithmetic beside the judgment;
  * the SOURCES line — which publications were actually read for that row, so a claim is
    traceable rather than merely confident.

Deliberately keeps `important_tradeoff` prominent. One required "here is the catch" per pick
is what separates a brief from an advertisement.
"""
from __future__ import annotations

import re


def _money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _price_line(r: dict) -> str:
    """Typical price leads; today's price is mentioned only when it differs and is a sale —
    a discount is news, not a ranking input."""
    typical = r.get("typical_price", r.get("price"))
    out = f"{_money(typical)} typical"
    cur = r.get("current_price")
    try:
        if cur and float(cur) < float(typical) * 0.98:
            out += f" · on sale at {_money(cur)} as of {r.get('_date','today')}"
    except (TypeError, ValueError):
        pass
    return out


def _evidence(r: dict) -> list:
    lines = []
    if r.get("owner_count"):
        hist = r.get("owner_histogram") or []
        bars = " ".join(f"{5-i}★{p:g}%" for i, p in enumerate(hist)) if hist else ""
        shape = f" · {r['rating_shape']}" if r.get("rating_shape") else ""
        lines.append(f"    Owners: {r.get('owner_rating')}★ across "
                     f"{int(r['owner_count']):,} ratings{shape}")
        if bars:
            lines.append(f"            {bars}")
        if r.get("realrank_score") is not None:
            # Under the floor the shrinkage IS the score — say so instead of
            # printing a number that reads as a verdict (realrank_index
            # .MIN_RATINGS_FOR_SCORE; the one-review bread oven case).
            if int(r.get("owner_count") or 0) < 20:
                lines.append(f"    RealRank withheld — only "
                             f"{int(r.get('owner_count') or 0)} rating(s), too few to score")
            else:
                lines.append(f"    RealRank {r['realrank_score']} "
                             f"(promoters minus detractors, discounted for sample size)")
    if r.get("identity_warning"):
        lines.append(f"    ⚠ {r['identity_warning']}")
    if r.get("amazon_asin"):
        src = f" [{r['asin_source']}]" if r.get("asin_source") else ""
        lines.append(f"    ASIN {r['amazon_asin']}{src} · {r.get('amazon_link','')}")
    # WHERE TO BUY — every retailer we can offer, because a single-retailer pick earns
    # nothing from a reader who shops elsewhere. Links are clean destinations: the affiliate
    # code is applied at CLICK time, so nothing here is pre-tagged (and nothing here carries
    # the reviewer's tag, which would pay them instead of us).
    offers = r.get("offers") or []
    if offers:
        # Show the link AS A READER WOULD GET IT — Amazon minted with our tag and a
        # placement subtag, everything else clean. The stored row keeps the bare
        # destination; this is the click-time form, previewed.
        try:
            from intake.products.buy_links import affiliate_url
        except Exception:
            affiliate_url = None
        slot = r.get("_slot") or "brief"
        lines.append(f"    Buy at ({len(offers)}):")
        for o in offers[:6]:
            shown = affiliate_url(o["url"], subtag=slot) if affiliate_url else o["url"]
            seen = f"  [linked by {', '.join(o['seen_in'][:2])}]" if o.get("seen_in") else ""
            lines.append(f"      {o['retailer']:<18} {shown}{seen}")
    srcs = [s for s in (r.get("source_links") or []) if str(s).strip()]
    if srcs:
        lines.append("    Sources: " + " · ".join(str(s) for s in srcs[:4]))
    return lines


def _name(r: dict) -> str:
    """Manufacturer + title, without repeating the brand — stored titles usually lead with
    it, so a naive join gives "Le Creuset Le Creuset Signature..."."""
    brand = (r.get("manufacturer") or "").strip()
    title = (r.get("product_title") or "").strip()
    if brand and title.lower().startswith(brand.lower().split()[0]):
        return title
    return f"{brand} {title}".strip()


def _row(r: dict, *, show_place: bool = True) -> str:
    head = f"{r.get('place','')}. " if show_place else ""
    out = [f"  {head}{_name(r)} "
           f"({r.get('capacity','')}) — {_price_line(r)}"]
    if r.get("best_for"):
        out.append(f"     Best for: {r['best_for']}")
    if r.get("why_it_ranks_here"):
        out.append(f"     {r['why_it_ranks_here']}")
    # Why it beat the one below it. `why_it_ranks_here` justifies a pick in isolation, which
    # left the ORDER unexplained — nothing ever said why #1 outranked #2. Ranking here is a
    # model judgment, not arithmetic (no weighted score is computed), so the reasoning IS the
    # audit trail; without it a rank can't be challenged, only accepted.
    if r.get("important_tradeoff"):
        out.append(f"     The catch: {r['important_tradeoff']}")
    if r.get("edge_over_next"):
        out.append(f"     Ahead of the next: {r['edge_over_next']}")
    out += _evidence(r)
    return "\n".join(out)


def render(data: dict, report: dict | None = None) -> str:
    pc = data.get("product_class") or "products"
    lines = [f"BEST {pc.upper()}", "=" * (5 + len(pc)),
             f"Researched {data.get('date_checked','')} · prices in "
             f"{data.get('currency','USD')} · ranked on TYPICAL price, not sale price", ""]

    lines += ["TOP THREE OVERALL", "-" * 17, ""]
    slug = re.sub(r"[^a-z0-9]+", "-", pc.lower()).strip("-")[:32]
    for r in sorted(data.get("overall_top_three") or [], key=lambda x: int(x.get("place", 9))):
        r["_slot"] = f"{slug}.overall.{r.get('place','')}"
        lines += [_row(r), ""]
    # THE HONEST GAP — a declared-empty slot is content, not absence: the reader
    # learns the class ran out of qualifying products, instead of being handed a
    # padded off-class #3 (the Water-Bath-Canner lesson, 2026-09-03).
    for s in (data.get("omitted_slots") or []):
        if not str(s.get("section") or "").strip():
            lines += [f"  #{s.get('place')} — left open: {s.get('reason','')}", ""]

    cats: dict[str, list] = {}
    for r in data.get("category_rankings") or []:
        cats.setdefault(r.get("category", "?"), []).append(r)
    if cats:
        lines += ["", "TOP THREE BY CATEGORY", "-" * 21, ""]
        for cat, rows in cats.items():
            lines += [f"{cat}", ""]
            cslug = re.sub(r"[^a-z0-9]+", "-", cat.lower()).strip("-")[:24]
            for r in sorted(rows, key=lambda x: int(x.get("place", 9))):
                r["_slot"] = f"{slug}.{cslug}.{r.get('place','')}"
                lines += [_row(r), ""]
            for s in (data.get("omitted_slots") or []):
                if str(s.get("section") or "").strip().lower() == cat.strip().lower():
                    lines += [f"  #{s.get('place')} — left open: {s.get('reason','')}", ""]

    # Curator-pinned pick — full analysis, separate slot, provenance stated. Placed after
    # the rankings so the review-sourced order reads first, never diluted.
    ec = data.get("editors_choice")
    if isinstance(ec, dict):
        lines += ["", "EDITOR'S CHOICE", "-" * 15,
                  "  Curator-selected — analyzed with the same evidence and rigor;",
                  "  not chosen by the supplied review sources.", ""]
        ec["_slot"] = f"{slug}.editors-choice.1"
        lines += [_row(ec, show_place=False), ""]

    crit = data.get("ranking_criteria") or []
    if crit:
        lines += ["", "HOW WE RANKED", "-" * 13, ""]
        for c in crit:
            lines.append(f"  {int(float(c.get('weight',0))*100):>3}%  {c.get('criterion','')}")
            if c.get("what_was_considered"):
                lines.append(f"        {c['what_was_considered']}")
        lines.append("")

    if data.get("methodology_note"):
        lines += ["", "NOTE", "-" * 4, "  " + data["methodology_note"], ""]

    # What each named authority's page ACTUALLY contained — the answer to "why does one
    # publisher get all the citations". An authority that never reviewed this class shows
    # up here as off-topic or not-retrieved instead of silently missing from the sources.
    srep = data.get("_source_accounting") or data.get("source_report") or []
    if srep:
        lines += ["", "SOURCES CONSULTED", "-" * 17]
        for s in srep:
            label = s.get("source") or "?"
            if s.get("error"):
                lines.append(f"  {label:<24} could not be retrieved — {s['error']}")
                continue
            rel = s.get("relevance") or "?"
            used = ("used" if s.get("used_in_ranking") else "not used") \
                if "used_in_ranking" in s else ""
            covers = s.get("page_covers") or s.get("page_title") or ""
            lines.append(f"  {label:<24} {rel:<13} {used:<9} {covers}")
        lines.append("")

    lines += ["", "AFFILIATE NOTE", "-" * 14,
              "  Amazon links carry our tag plus an ascsubtag naming the placement, so the",
              "  Associates report says WHICH surface earned the sale. `tag` is the only",
              "  parameter Amazon requires; SiteStripe's linkCode/linkId/language/ref_ are",
              "  reporting residue and are dropped.",
              "  Stored rows keep CLEAN destinations. Codes are applied at",
              "  click time so attribution can be tracked per placement, and so a change of",
              "  network never requires rewriting stored links. No link here carries anyone",
              "  else's tag — a review's own buy link does, and republishing it would pay",
              "  that publisher on our sale.", ""]

    if report:
        lines += ["", "VERIFICATION", "-" * 12, ""]
        for key, head in (("verified", "Identity confirmed"), ("filled", "ASIN recovered"),
                          ("offers", "Buy links"), ("scored", "Owner evidence"),
                          ("rejected", "REJECTED"), ("notes", "Notes")):
            for item in report.get(key) or []:
                lines.append(f"  [{head}] {item}")
        lines.append("")
    return "\n".join(lines)
