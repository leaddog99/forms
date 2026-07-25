"""realrank_posting.py — render a RealRank record as the POSTING page.

The record (from realrank_research) is data; this is the surface a reader sees. Three
placements off the same facts, exactly as prototyped in realrank-loafpan.html:

  * Editorial note  — the in-recipe aside ("the pan for this")
  * Full card       — teaser + expandable breakdown
  * Compact         — a "you might also like" rail row

The CSS and markup are lifted from that prototype rather than reinvented, so the generated
page IS the mockup with real data in it.

Two honesty rules carried over from the prototype, because they are the product:
  * the cheaper near-identical alternative gets its own block — naming it is the feature;
  * the score badge reads "—" and says so when no real histogram was found, instead of
    showing a number we can't stand behind.

Everything user-facing is escaped: the text comes from a language model reading arbitrary
web pages, so it is never trusted as markup.
"""
from __future__ import annotations

from html import escape as _esc

CSS = """
  :root{
    --ink:#12232e; --ink-soft:#3d4f59; --muted:#6b7a83;
    --line:#e7ecef; --bg:#eef1f2; --card:#ffffff;
    --brand:#0f6e6a; --brand-dark:#0a4f4c;
    --accent:#d9772f; --accent-dark:#bd6021;
    --pro:#1f8a54; --pro-bg:#eaf6ef;
    --con:#b4552f; --con-bg:#fbefe8;
    --amber:#c98a12; --amber-bg:#fbf3df;
    --gold:#e6a817;
    --shadow:0 1px 2px rgba(18,35,46,.05),0 10px 34px rgba(18,35,46,.09);
    --radius:16px;
    --serif:Georgia,"Iowan Old Style","Times New Roman",serif;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    line-height:1.55;-webkit-font-smoothing:antialiased;padding:34px 18px 70px}
  .wrap{max-width:700px;margin:0 auto}
  .switch-wrap{margin:0 0 22px}
  .switch-title{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:700;margin:0 0 10px 2px}
  .switch{display:inline-flex;background:#fff;border:1px solid var(--line);border-radius:12px;padding:4px;gap:3px;box-shadow:var(--shadow);flex-wrap:wrap}
  .switch button{appearance:none;border:none;background:transparent;cursor:pointer;font:inherit;font-weight:700;font-size:13.5px;color:var(--ink-soft);padding:8px 15px;border-radius:9px;transition:.15s}
  .switch button:hover{color:var(--ink)}
  .switch button.active{background:var(--brand);color:#fff}
  .stage > section{display:none}
  .stage > section.active{display:block;animation:fade .25s ease}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:none}}
  .ctx{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:700;margin:0 0 10px 4px}
  .photo{position:relative;border-radius:12px;overflow:hidden;background:#f3f5f6;border:1px solid var(--line)}
  .photo img{display:block;width:100%;height:100%;object-fit:cover}
  .photo .ph{display:none;position:absolute;inset:0;background:linear-gradient(160deg,#d9a441,#b9822c)}
  .photo.failed .ph{display:block}
  .photo.failed img{display:none}
  .score-badge{display:inline-flex;flex-direction:column;align-items:center;justify-content:center;
    width:52px;height:52px;border-radius:12px;background:linear-gradient(135deg,var(--brand),var(--brand-dark));color:#fff;flex:0 0 auto}
  .score-badge.pending{background:linear-gradient(135deg,#8fa3ab,#6b7a83)}
  .score-badge .num{font-size:20px;font-weight:900;line-height:1}
  .score-badge .lab{font-size:8px;letter-spacing:.1em;text-transform:uppercase;font-weight:700;opacity:.9;margin-top:2px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden}
  .teaser{padding:20px 22px}
  .teaser-top{display:flex;align-items:center;gap:10px;margin-bottom:15px}
  .rr-logo{display:inline-flex;align-items:center;gap:7px;font-weight:800;letter-spacing:-.01em;color:var(--brand);font-size:15px}
  .rr-mark{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,var(--brand),var(--brand-dark));display:inline-flex;align-items:center;justify-content:center;color:#fff;font-size:13px;font-weight:900}
  .verdict-pill{margin-left:auto;background:var(--brand);color:#fff;font-size:12px;font-weight:800;padding:5px 11px;border-radius:999px}
  .teaser-body{display:flex;gap:18px;align-items:flex-start}
  .teaser-body .photo{flex:0 0 108px;height:108px}
  .teaser-main{min-width:0}
  .teaser-main h2{margin:0 0 4px;font-size:18px;line-height:1.25;font-weight:800;letter-spacing:-.01em}
  .teaser-main .sub{margin:0 0 10px;color:var(--muted);font-size:13px}
  .teaser-line{font-size:14px;color:var(--ink-soft);margin:0 0 12px}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
  .chip{font-size:11.5px;font-weight:700;padding:4px 9px;border-radius:999px;display:inline-flex;align-items:center;gap:5px}
  .chip.good{background:var(--pro-bg);color:var(--pro);border:1px solid #cfe9da}
  .chip.mid{background:var(--amber-bg);color:var(--amber);border:1px solid #eeddb4}
  .chip.poor{background:var(--con-bg);color:var(--con);border:1px solid #f0d8cb}
  .chip .dot{width:6px;height:6px;border-radius:50%;background:currentColor}
  .rating-inline{display:flex;align-items:center;gap:9px;font-size:13px;color:var(--ink-soft);font-weight:600}
  .g{color:var(--gold);letter-spacing:1px}
  .prox{font-size:11px;color:var(--muted);font-weight:600}
  .award-badges{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
  .abadge{font-size:11.5px;font-weight:700;color:var(--brand-dark);background:#e9f3f2;border:1px solid #d3e7e5;padding:4px 9px;border-radius:8px}
  .abadge .n{color:var(--muted);font-weight:600}
  .teaser-cta{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:17px;padding-top:16px;border-top:1px solid var(--line)}
  .btn{appearance:none;border:none;cursor:pointer;font:inherit;font-weight:800;font-size:14px;border-radius:10px;padding:11px 16px;transition:.12s;text-decoration:none;display:inline-block}
  .btn:active{transform:translateY(1px)}
  .btn-primary{background:var(--accent);color:#fff}.btn-primary:hover{background:var(--accent-dark)}
  .btn-ghost{background:transparent;color:var(--brand);padding-left:2px}.btn-ghost:hover{color:var(--brand-dark);text-decoration:underline}
  .chev{display:inline-block;transition:transform .2s;margin-left:3px}
  .card.open .chev{transform:rotate(90deg)}
  .aff-note{font-size:11px;color:var(--muted);margin-left:auto}
  .detail{display:none;border-top:1px solid var(--line);background:linear-gradient(180deg,#fbfcfc,#fff);padding:4px 22px 24px}
  .detail.show{display:block;animation:fade .25s ease}
  .sec-label{font-size:11.5px;letter-spacing:.13em;text-transform:uppercase;font-weight:800;color:var(--brand);margin:22px 0 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .sec-label .tag{font-size:10px;letter-spacing:.02em;text-transform:none;font-weight:700;color:var(--muted);background:#eef1f2;border:1px solid var(--line);padding:2px 7px;border-radius:6px}
  .prclist{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  @media(max-width:540px){.prclist{grid-template-columns:1fr}}
  .pcbox{border-radius:12px;padding:13px 14px;font-size:13.5px}
  .pcbox h4{margin:0 0 8px;font-size:12px;letter-spacing:.04em;text-transform:uppercase;font-weight:800}
  .pcbox ul{margin:0;padding:0;list-style:none}
  .pcbox li{position:relative;padding-left:18px;margin:0 0 7px;color:var(--ink-soft)}
  .pcbox li:last-child{margin-bottom:0}
  .pros{background:var(--pro-bg);border:1px solid #cfe9da}.pros h4{color:var(--pro)}
  .pros li::before{content:"+";position:absolute;left:2px;top:-1px;font-weight:900;color:var(--pro)}
  .cons{background:var(--con-bg);border:1px solid #f0d8cb}.cons h4{color:var(--con)}
  .cons li::before{content:"\\2013";position:absolute;left:2px;top:-1px;font-weight:900;color:var(--con)}
  .alt{background:#f7f9f9;border:1px dashed #cfdcdc;border-radius:12px;padding:14px 16px;font-size:13.5px;color:var(--ink-soft)}
  .alt b{color:var(--ink)}
  .src{display:flex;gap:13px;padding:13px 0;border-bottom:1px solid var(--line)}
  .src:last-child{border-bottom:none}
  .src-badge{flex:0 0 auto;align-self:flex-start;font-size:11px;font-weight:800;color:#fff;background:var(--ink);padding:4px 9px;border-radius:7px;min-width:104px;text-align:center}
  .src-badge.award{background:var(--brand)}
  .src p{margin:0;font-size:13.5px;color:var(--ink-soft)}
  .src p .who{font-weight:800;color:var(--ink)}
  .src blockquote{margin:6px 0 0;padding-left:10px;border-left:2px solid var(--line);font-style:italic;color:var(--muted);font-size:13px}
  .owner{display:flex;gap:16px;flex-wrap:wrap;align-items:center;background:#fff;border:1px solid var(--line);border-radius:12px;padding:15px 16px}
  .owner .big{font-size:30px;font-weight:900;line-height:1}
  .owner .note{font-size:12px;color:var(--muted);flex:1;min-width:190px}
  .hist{flex:1;min-width:210px}
  .hrow{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--muted);margin-bottom:3px}
  .hrow .lab{width:26px;text-align:right;font-weight:700}
  .hbar{flex:1;height:8px;background:#eef1f2;border-radius:99px;overflow:hidden}
  .hbar span{display:block;height:100%;background:var(--gold)}
  .hrow .pct{width:34px;text-align:right;font-weight:700}
  .scorecalc{margin-top:10px;font-size:12px;color:var(--muted);line-height:1.6;background:#f7f9f9;border:1px solid var(--line);border-radius:10px;padding:11px 13px}
  .scorecalc b{color:var(--brand-dark);font-size:13.5px}
  .footer{margin-top:22px;padding-top:16px;border-top:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  .retailers{font-size:12px;color:var(--muted)}
  .retailers a{color:var(--brand);text-decoration:none;font-weight:700}.retailers a:hover{text-decoration:underline}
  .disclosure{font-size:11px;color:var(--muted);margin-top:14px;line-height:1.5;background:#f7f9f9;border:1px dashed var(--line);border-radius:8px;padding:9px 11px}
  .disclosure b{color:var(--ink-soft)}
  .coverage{font-size:11px;color:var(--muted);margin-top:10px;line-height:1.6}
  .coverage .ok{color:var(--pro);font-weight:700}
  .coverage .no{color:var(--con);font-weight:700}
  .compact{background:#fff;border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);padding:14px;display:flex;gap:14px;align-items:center}
  .compact .photo{flex:0 0 64px;height:64px}
  .compact .cmain{min-width:0;flex:1}
  .compact .kick{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--brand);font-weight:800;margin:0 0 3px}
  .compact h3{margin:0 0 3px;font-size:15px;font-weight:800;letter-spacing:-.01em}
  .compact p{margin:0 0 6px;font-size:12.5px;color:var(--muted)}
  .compact .mini{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--ink-soft);font-weight:600}
  .compact .go{flex:0 0 auto;color:var(--brand);font-weight:800;font-size:13px;text-decoration:none;white-space:nowrap}
  .recipe{font-family:var(--serif);color:#26333b;background:#fdfcfa;border:1px solid #eae4d9;border-radius:14px;padding:26px 28px;box-shadow:var(--shadow)}
  .note{margin:0;padding:16px 18px 15px 20px;border-left:3px solid var(--brand);background:#f6f8f8;border-radius:0 10px 10px 0}
  .note .nhead{font-family:-apple-system,Segoe UI,sans-serif;display:flex;align-items:center;gap:7px;font-size:11px;letter-spacing:.12em;text-transform:uppercase;font-weight:800;color:var(--brand);margin:0 0 8px}
  .note .nhead .rr-mark{width:17px;height:17px;font-size:11px;border-radius:5px}
  .note p{font-size:15.5px;line-height:1.65;margin:0 0 9px}
  .note p:last-of-type{margin-bottom:0}
  .note .expert{color:#2b3a42}
  .note .evidence{font-family:-apple-system,Segoe UI,sans-serif;font-size:13px;color:var(--muted);line-height:1.55}
  .note .evidence .src-mini{color:var(--ink-soft);font-weight:700}
  .note .fineprint{font-family:-apple-system,Segoe UI,sans-serif;font-size:11px;color:#9aa6ac;font-style:italic;margin-top:10px}
  .caption{font-size:12.5px;color:var(--muted);margin:12px 4px 0;line-height:1.5}
"""

_SENTIMENT_CLASS = {"good": "good", "mixed": "mid", "poor": "poor"}
_DISCLOSURE = ("<b>How RealRank works:</b> verdicts and specs are reported as facts with "
               "attribution; Pros/Cons are AI-assisted and grounded in the cited sources. "
               "<b>Disclosure:</b> RealRank may earn a commission on purchases through these "
               "links — it never affects our ranking.")


def _stars(rating):
    """Five glyphs for a 0-5 rating, rounded to the nearest whole star."""
    try:
        n = int(round(float(rating)))
    except (TypeError, ValueError):
        return ""
    return "★" * max(0, min(5, n)) + "☆" * (5 - max(0, min(5, n)))


def _score_badge(record, cls=""):
    s = record.get("realrank_score")
    if s is None:
        return (f'<div class="score-badge pending{cls}" title="No star histogram found — '
                f'score pending"><span class="num">—</span><span class="lab">Score</span></div>')
    return (f'<div class="score-badge{cls}"><span class="num">{_esc(str(s))}</span>'
            f'<span class="lab">Score</span></div>')


def _histogram_rows(owner):
    """The 5..1 bars. Percentages come from the feed; when only counts exist we derive
    them, because a bar chart needs shares and the counts are exact."""
    counts = owner.get("distribution_counts")
    if not counts or len(counts) != 5:
        return ""
    total = sum(counts) or 1
    rows = []
    for i, c in enumerate(counts):
        pct = 100.0 * c / total
        rows.append(
            f'<div class="hrow"><span class="lab">{5 - i}★</span>'
            f'<span class="hbar"><span style="width:{pct:.0f}%"></span></span>'
            f'<span class="pct">{pct:.0f}%</span></div>')
    return f'<div class="hist">{"".join(rows)}</div>'


def _photo(listing, alt):
    img = (listing or {}).get("image") or ""
    inner = f'<img alt="{_esc(alt)}" src="{_esc(img)}">' if img else ""
    cls = "photo" if img else "photo failed"
    return f'<div class="{cls}">{inner}<span class="ph"></span></div>'


def _buy(listing, label, cls="btn btn-primary"):
    """Buy links carry rel="sponsored nofollow" — these become affiliate links."""
    href = (listing or {}).get("link") or ""
    if not href:
        return ""
    return (f'<a class="{cls}" href="{_esc(href)}" target="_blank" '
            f'rel="sponsored nofollow noopener">{_esc(label)}</a>')


def _sub_line(record):
    """brand · price — whatever we actually know."""
    l = record.get("listing") or {}
    bits = [b for b in (l.get("brand"), l.get("price")) if b]
    return " · ".join(bits)


def _editorial(record):
    l = record.get("listing") or {}
    o = record.get("owner_sentiment") or {}
    srcs = record.get("sources") or []
    experts = [s for s in srcs if (s.get("type") or "expert") == "expert"][:2]
    ev = "; ".join(f'<span class="src-mini">{_esc(s.get("name",""))}</span> — '
                   f'{_esc(s.get("verdict_or_award",""))}' for s in experts if s.get("name"))
    if o.get("avg_rating"):
        ev += (f'. Owners rate it {_esc(str(o["avg_rating"]))}★ across '
               f'{_esc(f"{o.get('review_count'):,}" if o.get("review_count") else "?")} ratings')
    alt = record.get("cheaper_alternative") or {}
    alt_html = ""
    if alt.get("name"):
        alt_html = (f' The honest catch: <b>{_esc(alt["name"])}</b> '
                    f'({_esc(alt.get("approx_price",""))}) — {_esc(alt.get("why",""))}')
    return f"""
    <section data-s="ed" class="active">
      <div class="recipe">
        <div class="note">
          <p class="nhead"><span class="rr-mark">R</span>RealRank · the one to get</p>
          <p class="expert">{_esc(record.get('summary','')[:600])}{alt_html}</p>
          <p class="evidence">{ev}. {_buy(l, 'Compare current prices →', 'inline') or ''}</p>
          <p class="fineprint">RealRank is reader-funded; we may earn a commission if you
            buy through this link. It never changes our ranking.</p>
        </div>
      </div>
      <p class="caption"><b>Editorial note</b> — the in-context placement: an expert aside
        inside a recipe or guide. Naming the cheaper near-identical option is the trust move.</p>
    </section>"""


def _card(record):
    l = record.get("listing") or {}
    o = record.get("owner_sentiment") or {}
    chips = "".join(
        f'<span class="chip {_SENTIMENT_CLASS.get(a.get("sentiment"), "mid")}">'
        f'<span class="dot"></span>{_esc(a.get("name",""))}</span>'
        for a in (record.get("aspects") or [])[:6])
    awards = "".join(
        f'<span class="abadge">{_esc(s.get("name",""))} '
        f'<span class="n">{_esc(s.get("verdict_or_award",""))}</span></span>'
        for s in (record.get("sources") or [])[:4] if s.get("name"))
    pros = "".join(f"<li>{_esc(p)}</li>" for p in (record.get("pros") or []))
    cons = "".join(f"<li>{_esc(c)}</li>" for c in (record.get("cons") or []))

    src_rows = []
    for s in (record.get("sources") or []):
        q = (f'<blockquote>“{_esc(s["short_quote"])}” — {_esc(s.get("name",""))}</blockquote>'
             if s.get("short_quote") else "")
        facts = "; ".join(s.get("key_facts") or [])
        badge_cls = "src-badge award" if (s.get("type") or "expert") == "expert" else "src-badge"
        src_rows.append(
            f'<div class="src"><span class="{badge_cls}">'
            f'{_esc((s.get("verdict_or_award") or "Reviewed")[:28])}</span>'
            f'<p><span class="who">{_esc(s.get("name",""))}</span> — {_esc(facts)}{q}</p></div>')

    alt = record.get("cheaper_alternative") or {}
    alt_block = ""
    if alt.get("name"):
        alt_block = (
            '<div class="sec-label">Cheaper alternative <span class="tag">we name it on purpose</span></div>'
            f'<div class="alt"><b>{_esc(alt["name"])}</b> '
            f'{_esc(alt.get("approx_price",""))} — {_esc(alt.get("why",""))}</div>')

    owner_block = ""
    if o.get("avg_rating"):
        n = o.get("review_count")
        score = record.get("realrank_score")
        # Show the score WHERE ITS EVIDENCE IS. The index is computed from exactly this
        # histogram, so the number belongs next to the bars that produced it — otherwise
        # the badge at the top reads as an opinion instead of an arithmetic result.
        score_line = ""
        if score is not None:
            score_line = (
                f'<div class="scorecalc"><b>RealRank {_esc(str(score))}</b> — '
                f'NPS-from-stars with a Wilson-style confidence penalty: '
                f'promoters (5★) minus detractors (3★ and below), 4★ passive, then '
                f'discounted for sample size across these {_esc(f"{n:,}") if n else "?"} '
                f'ratings. A 5.0 from nine reviews cannot outrank a 4.6 from '
                f'{_esc(f"{n:,}") if n else "many"}.</div>')
        owner_block = (
            '<div class="sec-label">Owner sentiment <span class="tag">structured feed, not scraped prose</span></div>'
            f'<div class="owner"><div><div class="big">{_esc(str(o["avg_rating"]))}</div>'
            f'<div class="g">{_stars(o["avg_rating"])}</div></div>'
            f'{_histogram_rows(o)}'
            f'<div class="note" style="border:none;background:none;padding:0">'
            f'{_esc(f"{n:,}") if n else "?"} ratings'
            f'{" · Amazon " + _esc(o.get("asin","")) if o.get("asin") else ""}</div></div>'
            f'{score_line}')

    cov = record.get("source_coverage") or []
    cov_html = ""
    if cov:
        marks = {"fetched": '<span class="ok">✔</span>', "searched": "·",
                 "unavailable": '<span class="no">✕</span>'}
        cov_html = ('<p class="coverage"><b>Sources we read:</b> '
                    + " &nbsp; ".join(f'{marks.get(c.get("status"), "·")} {_esc(c.get("name",""))}'
                                      for c in cov) + "</p>")

    score_note = ""
    if record.get("realrank_score") is None:
        score_note = ("<b>Score pending</b> — no star histogram was available for this "
                      "product, and we don't publish a number we can't compute. ")

    return f"""
    <section data-s="card">
      <p class="ctx">Equipment for this recipe</p>
      <div class="card" id="card">
        <div class="teaser">
          <div class="teaser-top">
            <span class="rr-logo"><span class="rr-mark">R</span>RealRank</span>
            <span class="verdict-pill">{_esc(record.get('verdict','—'))}</span>
          </div>
          <div class="teaser-body">
            {_photo(l, record.get('product',''))}
            <div class="teaser-main">
              <h2>{_esc(record.get('product',''))}</h2>
              <p class="sub">{_esc(_sub_line(record))}</p>
              <p class="teaser-line">{_esc(record.get('one_liner',''))}</p>
              <div class="chips">{chips}</div>
              <span class="rating-inline"><span class="g">{_stars(o.get('avg_rating'))}</span>
                {_esc(str(o.get('avg_rating') or '—'))}
                <span class="prox">· {_esc(f"{o.get('review_count'):,}") if o.get('review_count') else 'no ratings found'}</span></span>
              <div class="award-badges">{awards}</div>
            </div>
          </div>
          <div class="teaser-cta">
            {_score_badge(record)}
            <button class="btn btn-ghost" id="toggle" aria-expanded="false">See the full breakdown <span class="chev">›</span></button>
            {_buy(l, 'Check price')}
            <span class="aff-note">Affiliate link</span>
          </div>
        </div>
        <div class="detail" id="detail">
          <div class="sec-label">RealRank's take <span class="tag">AI-assisted · grounded in cited sources</span></div>
          <div class="prclist">
            <div class="pcbox pros"><h4>Pros</h4><ul>{pros}</ul></div>
            <div class="pcbox cons"><h4>Cons</h4><ul>{cons}</ul></div>
          </div>
          {alt_block}
          <div class="sec-label">What the review sites say <span class="tag">facts, attributed</span></div>
          {''.join(src_rows)}
          {owner_block}
          <div class="footer">
            {_score_badge(record)}
            {_buy(l, 'Check current price')}
          </div>
          <p class="disclosure">{score_note}{_DISCLOSURE}</p>
          {cov_html}
        </div>
      </div>
      <p class="caption"><b>Full card</b> — the full placement. Score, verdict, attributed
        findings, the honest cheaper option, and which sources we actually read.</p>
    </section>"""


def _compact(record):
    l = record.get("listing") or {}
    o = record.get("owner_sentiment") or {}
    top = next((s for s in (record.get("sources") or []) if s.get("name")), {})
    proof = f"{top.get('name','')} {top.get('verdict_or_award','')}".strip()
    return f"""
    <section data-s="cmp">
      <p class="ctx">You might also like</p>
      <div class="compact">
        {_photo(l, record.get('product',''))}
        <div class="cmain">
          <p class="kick">RealRank · {_esc(record.get('verdict',''))}</p>
          <h3>{_esc(record.get('product',''))}</h3>
          <p>{_esc(record.get('one_liner','')[:110])}</p>
          <span class="mini"><span class="g">{_stars(o.get('avg_rating'))}</span>
            {_esc(str(o.get('avg_rating') or '—'))} · {_esc(proof[:44])}</span>
        </div>
        <a class="go" href="#" onclick="return false">See why →</a>
      </div>
      <p class="caption"><b>Compact</b> — for a “you might like” rail. One verdict line,
        one proof point, a quiet link into the full breakdown.</p>
    </section>"""


def to_html(record):
    """The full posting page — three placements off one record."""
    title = record.get("product", "RealRank")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>RealRank — {_esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="switch-wrap">
    <p class="switch-title">RealRank — same facts, three placements</p>
    <div class="switch">
      <button data-t="ed" class="active">Editorial note</button>
      <button data-t="card">Full card</button>
      <button data-t="cmp">Compact “you might like”</button>
    </div>
  </div>
  <div class="stage">
{_editorial(record)}
{_card(record)}
{_compact(record)}
  </div>
</div>
<script>
  document.querySelectorAll('.photo img').forEach(function(img){{
    img.addEventListener('error', function(){{ img.closest('.photo').classList.add('failed'); }});
  }});
  var btns=document.querySelectorAll('.switch button');
  var secs=document.querySelectorAll('.stage > section');
  btns.forEach(function(b){{b.addEventListener('click',function(){{
    btns.forEach(function(x){{x.classList.remove('active')}});
    b.classList.add('active');
    var t=b.getAttribute('data-t');
    secs.forEach(function(s){{ s.classList.toggle('active', s.getAttribute('data-s')===t); }});
  }});}});
  var toggle=document.getElementById('toggle');
  var detail=document.getElementById('detail');
  var card=document.getElementById('card');
  if(toggle){{toggle.addEventListener('click',function(){{
    var open=detail.classList.toggle('show');
    card.classList.toggle('open',open);
    toggle.setAttribute('aria-expanded',open?'true':'false');
    toggle.firstChild.textContent=open?'Hide the full breakdown ':'See the full breakdown ';
  }});}}
</script>
</body>
</html>"""
