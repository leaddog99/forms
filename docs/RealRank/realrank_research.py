"""
realrank_research.py — the "initial search + document" step for RealRank.

Given a product name, this:
  0. FETCHES the named expert reviews ourselves, through BCC's unblocker,
  1. runs a web search for whatever we couldn't fetch (+ owner reviews),
  2. extracts SALIENT FACTS with attribution (short quotes only, Fodor's style),
  3. returns a structured JSON record + a human-readable Markdown brief,
  4. computes the RealRank score if an owner star-histogram was found.

Step 0 is why this lives inside the BCC repo. The model's own `web_search` browses from an
egress that America's Test Kitchen, Serious Eats and Wirecutter all block — and when a named
source is unreachable the model doesn't stop, it QUIETLY BACKFILLS with whatever it can read.
Measured on the 2026-07-25 KitchenAid run: all three of those sources were missing from the
record and Forbes Vetted / Foodal / Woman & Home appeared in their place, with nothing in the
output saying so. BCC already fetches those sites daily for the recipe harvest, so we do it
here: SERP for the source's own page, pull it through `fetch_via_unblocker`, and hand the
model OUR copy. What we fetched, what came from search, and what is genuinely unavailable are
all reported in `source_coverage` — a source we can't reach is ABSENT, never substituted.

Setup:
    pip install anthropic          (keys come from the repo-root .env)
Run:
    python realrank_research.py "KitchenAid Artisan KSM150PS stand mixer"
"""

import os
import re
import sys
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat()

import anthropic
from realrank_index import realrank_index   # the scoring subroutine you built

# Import BCC's fetch stack (repo root is two levels up from docs/RealRank/).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Keys live in the REPO-ROOT .env (ANTHROPIC_API_KEY, REALRANK_MODEL), same as the rest of
# the project (moz_v3 / url_scoring / enrich.service all call load_dotenv). run_realrank.bat
# only looks for a .env beside itself, which doesn't exist — so load the root one here and the
# script works from either entry point. Never overrides a var already set in the environment.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env"))
except ImportError:
    pass

MODEL = os.environ.get("REALRANK_MODEL", "claude-sonnet-4-5")  # set to your current model

DEFAULT_SOURCES = [
    "America's Test Kitchen", "Serious Eats", "Wirecutter", "The Kitchn",
    "Reviewed", "Tom's Guide", "TechGearLab", "Consumer Reports",
    "and owner reviews on Amazon / Best Buy / Walmart / the retailer's own page",
]

# Named expert sources we FETCH OURSELVES (label -> the site: filter that finds their page).
# These are the authorities worth insisting on; the model's own search silently drops the
# blocked ones. Order = the order they appear in the brief.
SOURCE_SITES = [
    ("America's Test Kitchen", "americastestkitchen.com"),
    ("Serious Eats",           "seriouseats.com"),
    ("Wirecutter",             "nytimes.com/wirecutter"),
    ("The Kitchn",             "thekitchn.com"),
    ("Reviewed",               "reviewed.com"),
    ("Tom's Guide",            "tomsguide.com"),
    ("TechGearLab",            "techgearlab.com"),
    ("Consumer Reports",       "consumerreports.org"),
]

# Per-document budget handed to the model. ATK's stand-mixer page is ~100k chars; the part
# about ANY ONE product is a few paragraphs, so we send a relevance-trimmed slice.
DOC_CHAR_BUDGET = 18000


def _host_ok(url, site):
    """Is `url` really ON the requested source's site?

    NON-NEGOTIABLE, and learned the hard way: a `site:` query that matches nothing does not
    come back empty — the SERP quietly returns unrestricted results. On the first run of this
    script, `site:americastestkitchen.com …KSM150PS…` matched no page and the top hit was
    kitchenaid.com's HOMEPAGE, which we then fetched and labelled "America's Test Kitchen"
    for three different publishers. A brand's marketing page wearing a reviewer's name is
    the worst possible failure here, so every hit is checked against the host it claims.

    `site` may carry a path prefix ("nytimes.com/wirecutter"), which must also match.
    """
    from urllib.parse import urlparse
    want_host, _, want_path = site.partition("/")
    try:
        u = urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    if not (host == want_host or host.endswith("." + want_host)):
        return False
    return (u.path or "/").lstrip("/").lower().startswith(want_path.lower()) if want_path else True


def _keywords(product):
    """Distinctive tokens from the product name — model numbers first, then words worth
    matching. Drives the relevance trim so a 100k roundup contributes the right paragraphs."""
    toks = re.findall(r"[A-Za-z0-9\-]+", product)
    model_nums = [t for t in toks if any(c.isdigit() for c in t) and len(t) > 2]
    words = [t.lower() for t in toks if len(t) > 3 and not any(c.isdigit() for c in t)]
    return model_nums + words


def _trim_for_prompt(md, keys, budget=DOC_CHAR_BUDGET):
    """Keep the passages that actually discuss this product. A roundup review covers a dozen
    products; sending the whole page wastes tokens and buries the relevant verdict."""
    if len(md) <= budget:
        return md
    lines = md.split("\n")
    keys_l = [k.lower() for k in keys]
    hits = [i for i, ln in enumerate(lines) if any(k in ln.lower() for k in keys_l)]
    if not hits:
        return md[:budget]
    keep, WINDOW = set(), 12
    for i in hits:
        keep.update(range(max(0, i - WINDOW), min(len(lines), i + WINDOW + 1)))
    out, last = [], -1
    for i in sorted(keep):
        if last >= 0 and i > last + 1:
            out.append("\n[…]\n")
        out.append(lines[i])
        last = i
    trimmed = "\n".join(out)
    return trimmed[:budget] if len(trimmed) > budget else trimmed


def fetch_source_docs(product, sites=SOURCE_SITES, max_workers=4, should_cancel=None):
    """Fetch each named source's own page through BCC's fetch stack.

    One SERP call per source to locate its page (`site:` filter), then the SAME tiered
    ladder the recipe pipeline climbs — UA chain -> unblocker -> Wayback snapshot
    (`fetch_with_full_fallback`, reached via `html_to_markdown(unblocker=True)`).

    Returns [{label, url, markdown, via, error}]. `via` records which rung actually served
    the page (direct / unblocker / wayback), so a source read from a years-old snapshot is
    visible rather than passing as current. `markdown` empty means every rung failed — and
    `url` still carries the page we found, because the LAST rung is human: capture it with
    the review bookmarklet (forms/reviews.html -> /extract-review).
    """
    from input.pipeline.serp_search import serp_search
    from to_markdown.html_to_markdown import html_to_markdown

    keys = _keywords(product)

    # A model number pins the query too tightly for a roundup review ("The Best Stand Mixers"
    # never says KSM150PS in its title), and a `site:` miss falls back to unrestricted results.
    # So: precise query first, then the same query without model numbers.
    broad = " ".join(t for t in product.split() if not any(c.isdigit() for c in t))

    def one(entry):
        label, site = entry
        rec = {"label": label, "url": "", "markdown": "", "via": "", "error": ""}
        # Cancel checkpoint per source: the fetch phase is the long one (a SERP call plus an
        # unblocker fetch each), so a cancel shouldn't have to wait for all eight.
        if should_cancel and should_cancel():
            rec["error"] = "cancelled"
            return rec
        hit = None
        for q in (f"site:{site} {product} review", f"site:{site} {broad} review"):
            try:
                hits = serp_search(q, pages=1, want=5)
            except Exception as e:
                rec["error"] = f"search failed: {e}"
                return rec
            # Keep ONLY results actually on that publisher's site (see _host_ok).
            hit = next((h for h in hits if _host_ok(h.get("link", ""), site)), None)
            if hit:
                break
        if not hit:
            rec["error"] = "no page found on this site"
            return rec
        rec["url"] = hit.get("link", "")
        timings = {}
        try:
            doc = html_to_markdown(rec["url"], timings, unblocker=True, render=True)
        except Exception as e:
            rec["error"] = f"fetch failed: {e}"
            return rec
        rec["via"] = timings.get("fetch_source") or "direct"
        if timings.get("wayback_timestamp"):
            rec["via"] += f":{timings['wayback_timestamp']}"
        md = (doc or {}).get("markdown") or ""
        if not md.strip():
            rec["error"] = "fetched but empty (blocked or JS-only)"
            return rec
        rec["markdown"] = _trim_for_prompt(md, keys)
        return rec

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(one, sites))

SYSTEM_PROMPT = """\
You are RealRank's product-research analyst. Your job is to survey what the top \
review sites and real owners say about ONE product, then distill it into an \
honest, attributed record.

RULES — these are the point of RealRank, follow them exactly:
- THE TEST FOR WHAT YOU MAY USE: would a person who read this page be able to write \
  this down afterwards, in their own words, from what they learned? If yes, it is a \
  fact you may report — a score, an award, a measurement, a test result, a stated \
  drawback, a price. If instead you are carrying over the author's PHRASING, their \
  jokes, their metaphors, their structure, or the flow of their argument, you are \
  copying, not reporting. Read, understand, then write it yourself.
- Report FACTS with attribution (scores, awards, ratings, specs, findings). \
  Facts are not copyrightable; prefer them.
- Use verbatim quotes ONLY when a phrasing is essential, keep them under ~15 \
  words, and always attribute them (Fodor's style). Never reproduce a source's \
  prose at length.
- Be BALANCED: surface the real drawbacks, not just praise. A product that only \
  has pros is a research failure.
- When a near-identical product is clearly cheaper or better value, say so and \
  name it. Naming the cheaper option is a feature, not a leak.
- For owner sentiment, capture the average rating, the review COUNT, and the \
  full 5/4/3/2/1 star breakdown if you can find it (we need the histogram to \
  score the product). If you can only find a proxy (e.g. the same pan sold under \
  another brand), label it clearly as a proxy with its source.
- Do not invent numbers. If a figure isn't found, use null.
- SUPPLIED DOCUMENTS come first. Any source given to you under "FETCHED SOURCE \
  DOCUMENTS" was retrieved by us from that publisher's own page — treat it as the \
  authoritative text for that source and draw its facts from there, not from search.
- NEVER SUBSTITUTE. If a named source is unavailable, say so in `source_coverage` \
  and move on. Do not quietly fill its slot with an easier-to-reach site, and never \
  attribute a finding to a publisher whose page you did not actually read — a mirror, \
  aggregator or reprint is NOT that publisher. Extra sources you find are welcome as \
  ADDITIONS, never as replacements for a named one.
- Every entry in `sources` must carry the URL of the page the facts actually came from.

OUTPUT: after researching, respond with EXACTLY ONE fenced ```json block and \
nothing else, matching this schema:

{
  "product": str,
  "verdict": "Top Pick" | "Highly Recommended" | "Recommended" | "Mixed" | "Not Recommended",
  "one_liner": str,
  "aspects": [ {"name": str, "sentiment": "good" | "mixed" | "poor"} ],
  "sources": [
    {
      "name": str,
      "url": str,
      "type": "expert" | "owner",
      "verdict_or_award": str,
      "key_facts": [str, ...],
      "short_quote": str | null
    }
  ],
  "owner_sentiment": {
    "avg_rating": number | null,
    "review_count": integer | null,
    "distribution_pct": {"5": num, "4": num, "3": num, "2": num, "1": num} | null,
    "is_proxy": boolean,
    "proxy_note": str | null,
    "source_url": str | null
  },
  "pros": [str, ...],
  "cons": [str, ...],
  "cheaper_alternative": {"name": str, "why": str, "approx_price": str} | null,
  "summary": str,
  "source_coverage": [
    {
      "name": str,
      "status": "fetched" | "searched" | "unavailable",
      "note": str | null
    }
  ]
}

`source_coverage` must list EVERY named source, including the ones you could not use — \
"fetched" = it was supplied to you under FETCHED SOURCE DOCUMENTS, "searched" = you found \
it yourself via web search, "unavailable" = you could not read that publisher at all. An \
honest gap is a correct answer; a silent substitution is not.
"""


def _verify_asin(asin, product):
    """Is this listing actually the product we're researching? -> (ok, reason).

    Checks the product TYPE via the shared vocabulary (intake/products/product_types), which
    knows that a cocotte IS a dutch oven while a bread oven is not — the first version got
    the bread oven right and then falsely rejected Staub's "Dutch Oven 5.5-qt Round Cocotte".
    Fail-open by design: a lookup failure or an ambiguous name lets the candidate through
    rather than dropping a legitimate product out of a ranking.
    """
    try:
        from intake.products import amazon_rainforest as az
        from intake.products.product_types import same_type
        title = (az.product_ratings(asin).get("title") or "")
    except Exception as e:
        return True, f"could not verify ({e})"
    if not title:
        return True, "no title to verify against"
    ok, why = same_type(product, title)
    return (True, "ok") if ok else (False, f"listing is a {why} — {title[:60]}")


def fetch_owner_data(product, asin="", brand=""):
    """Amazon owner ratings + listing facts for the ENHANCEMENT stage (histogram, average,
    total, photo, price, top review bodies) — one structured call per product.

    Deliberately NOT the rating-popover widget: that is a SELECTION-stage tool used to
    screen a shortlist before any product is opened (intake/products/amazon_widget). By the
    time a product reaches this function it has already been chosen, and we want the listing
    facts the widget doesn't carry, in one call. Best-effort — any failure leaves the score
    pending rather than sinking the run.
    """
    try:
        from intake.products import amazon_rainforest as az
        return az.owner_sentiment(product, asin=asin, brand=brand)
    except Exception as e:
        print(f"[realrank] Amazon owner data unavailable: {e}")
        return None


def build_user_prompt(product, sources, docs=None, owner=None):
    parts = [f"Product: {product}\n"]
    fetched = [d for d in (docs or []) if d.get("markdown")]
    missed = [d for d in (docs or []) if not d.get("markdown")]

    if fetched:
        parts.append(
            "FETCHED SOURCE DOCUMENTS — we retrieved these from the publishers' own pages. "
            "Use them as the authoritative text for those sources; do not search for them "
            "again. Long roundups are trimmed to the passages about this product.\n")
        for d in fetched:
            parts.append(
                f"\n===== {d['label']} =====\nURL: {d['url']}\nRetrieved via: {d['via']}\n"
                f"-----\n{d['markdown']}\n===== end {d['label']} =====\n")
    if missed:
        parts.append(
            "\nCOULD NOT RETRIEVE — every fetch tier failed for these. Try web search; if "
            "that also fails, mark them \"unavailable\" in source_coverage. Do NOT replace "
            "them with a different publisher, and do NOT cite a mirror or reprint as though "
            "it were them:\n"
            + "\n".join(f"- {d['label']} ({d['error']}) — page found: {d['url'] or 'none'}"
                        for d in missed))

    if owner and owner.get("ratings_total"):
        revs = "\n".join(
            f"  [{r.get('rating')}★] {r.get('title','')}\n      {(r.get('body') or '')[:500]}"
            for r in (owner.get("top_reviews") or []))
        parts.append(
            f"\n===== AMAZON OWNER DATA (structured — authoritative) =====\n"
            f"Listing: {owner.get('title','')}\nASIN: {owner.get('asin')}\n"
            f"URL: {owner.get('link','')}\n"
            f"Average: {owner.get('rating')} stars across {owner.get('ratings_total')} ratings\n"
            f"Star breakdown (counts, 5→1): {owner.get('histogram')}\n"
            f"\nRepresentative owner reviews:\n{revs}\n"
            f"===== end Amazon owner data =====\n"
            f"\nThese figures come from a structured product feed, not from reading a page. "
            f"Use them for `owner_sentiment` EXACTLY as given — do not search for different "
            f"numbers and do not round them. Draw owner THEMES (what buyers repeatedly praise "
            f"or complain about) from the review bodies above, in your own words.")
    else:
        parts.append(
            f"\nAlso cover owner reviews (Amazon / Best Buy / Walmart / the retailer's own "
            f"page) via search — especially the 5/4/3/2/1 star histogram, needed for scoring.")

    parts.append(
        f"\nOther reputable sources you find are welcome as ADDITIONS.\n"
        f"\nNow output the single JSON record per the schema.")
    return "\n".join(parts)


def run_research(product, sources=DEFAULT_SOURCES, max_searches=8, docs=None, owner=None):
    # Through the gateway, not a raw client: llm.create journals token usage to
    # bcc_token_journal automatically (memory/project_llm_gateway, docs/llm-gateway.md).
    # This is the most expensive call we make — a big cached prefix plus up to 8 web
    # searches — so it is exactly the kind that must not be invisible to the ledger.
    # Falls back to the bare SDK only if the gateway can't be imported (standalone use).
    # STREAM, don't create: at a 32k budget the SDK refuses a non-streaming call outright
    # ("Streaming is required for operations that may take longer than 10 minutes"), which is
    # how job #619 died after doing all the expensive fetching. markdown_to_review already
    # streams for the same reason. llm.stream journals the final message's usage for us.
    try:
        import llm

        def _run(**kw):
            with llm.stream(operation="realrank_research", **kw) as s:
                return s.get_final_message()
    except ImportError:
        def _run(**kw):
            with anthropic.Anthropic().messages.stream(**kw) as s:
                return s.get_final_message()

    resp = _run(
        model=MODEL,
        # A full record for 6-8 sources (key_facts + quotes + histogram) overran 4000, then
        # overran 12000 on a rich product (job #618, Le Creuset — cut off mid-word). The
        # web_search tool's own turns share this budget, so headroom matters more than it
        # looks from the size of the finished record.
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search",
                "max_uses": max_searches}],
        messages=[{"role": "user",
                   "content": build_user_prompt(product, sources, docs, owner)}],
    )
    # Say it out loud at the moment it happens, rather than leaving a JSONDecodeError to be
    # reverse-engineered from the raw file later.
    if getattr(resp, "stop_reason", None) == "max_tokens":
        print(f"[realrank] WARNING: hit max_tokens — the record is truncated and will not "
              f"parse. Raise max_tokens (currently 32000) or send fewer/shorter documents.")
    return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


def parse_record(raw):
    """Pull the fenced JSON record out of the reply.

    TRUNCATION IS THE COMMON FAILURE, so detect it before parsing. When the model runs out
    of room the reply has an OPENING ``` and no closing one, and the greedy fallback below
    would then match from the first `{` to whatever `}` happens to be last — handing
    json.loads a fragment that fails with a bewildering "Expecting ',' delimiter" deep inside
    a perfectly valid line. That's how job #618 read as a syntax error when it was simply
    cut off (the raw ended mid-word: "Consumer Reports' most recent l").
    """
    fenced = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))

    if "```" in raw and raw.count("```") == 1:
        raise ValueError(
            f"Model reply was TRUNCATED — opening ``` with no closing fence, "
            f"{len(raw)} chars, ends: ...{raw[-90:]!r}. Raise max_tokens or trim the "
            f"source documents; the JSON is incomplete, not malformed.")

    m = re.search(r"(\{.*\})", raw, re.DOTALL)
    if not m:
        raise ValueError("No JSON record found in model output:\n" + raw[:500])
    return json.loads(m.group(1))


def apply_owner_data(record, owner, extra_sources=None):
    """Overwrite the model's owner_sentiment with structured retailer figures.

    The model sees these numbers in its prompt, but the RECORD takes them from the feed
    directly — a rating average and a 144,676-count histogram are data, and nothing that
    drives a published score should be able to drift through a paraphrase.

    `extra_sources` pools OTHER retailers' histograms with Amazon's:
    [{"source": "bestbuy", "histogram": [5,4,3,2,1 counts], "total": n, "url": …}].
    Same 5-point scale on the same product = more evidence, so the counts are SUMMED and
    scored once (see realrank_index.pool_histograms — averaging two scores would weigh a
    3,642-review retailer equally against a 145,000-review one). Best Buy is not reachable
    through our fetch stack today, so this is how its numbers get in: another feed when we
    have one, or the curator entering the histogram from a bookmarklet capture.
    """
    from realrank_index import pool_histograms, polarization

    sources = []
    if owner and owner.get("histogram") and owner.get("ratings_total"):
        sources.append({"source": "amazon", "histogram": owner["histogram"],
                        "total": owner["ratings_total"]})
    for s in (extra_sources or []):
        if s.get("histogram"):
            sources.append(s)

    if not sources:
        return record
    pooled = pool_histograms(sources)

    record["owner_sentiment"] = {
        "avg_rating": (owner or {}).get("rating"),
        "review_count": pooled["total"],
        "distribution_pct": (owner or {}).get("distribution_pct"),
        "distribution_counts": pooled["histogram"],      # POOLED exact counts, 5..1
        "is_proxy": False,
        "proxy_note": None,
        "source_url": (owner or {}).get("link") or "",
        "asin": (owner or {}).get("asin"),
        "source": "+".join(s["source"] for s in pooled["sources"]),
        # Where the breakdown came from, and whether its counts are exact or derived from
        # Amazon's whole-percent bars — so the score's basis line can say so honestly.
        "histogram_source": (owner or {}).get("histogram_source", "rainforest"),
        "counts_derived": bool((owner or {}).get("counts_derived")),
        "pooled_from": pooled["sources"],                # per-retailer split stays visible
    }
    # Shape, not just level: a 4.6 average can be a gentle taper or a barbell, and the
    # barbell (1★ outnumbering 2★) is a different product story. See realrank_index.
    record["polarization"] = polarization(pooled["histogram"])
    # The listing facts the posting card needs (photo, asking price, buy link).
    if owner:
        record["listing"] = {k: owner.get(k) for k in
                             ("image", "price", "link", "brand", "title")}
    return record


def attach_score(record):
    os_ = record.get("owner_sentiment") or {}
    n = os_.get("review_count")
    # Prefer exact counts (Rainforest) over percentages (which are rounded to whole points
    # by Amazon, so 82/10/4/1/3 doesn't even sum to 100).
    counts = os_.get("distribution_counts")
    dist = os_.get("distribution_pct")
    d = None
    if counts and len(counts) == 5:
        d = list(counts)
        src = os_.get("histogram_source") or "rainforest"
        basis = ("computed from owner histogram "
                 + (f"(counts derived from {src} whole-percent bars)"
                    if os_.get("counts_derived") else f"(exact counts, {src})"))
    elif dist and any(v for v in dist.values() if v):
        d = [dist.get("5", 0) or 0, dist.get("4", 0) or 0, dist.get("3", 0) or 0,
             dist.get("2", 0) or 0, dist.get("1", 0) or 0]
        basis = "computed from owner histogram (percentages)"
    if d and n:
        record["realrank_score"] = round(realrank_index(d, n), 1)
        record["realrank_score_basis"] = basis
    else:
        record["realrank_score"] = None
        record["realrank_score_basis"] = "histogram not found — score pending feed"
    return record


def to_markdown(r):
    lines = [f"# {r['product']}", ""]
    score = r.get("realrank_score")
    lines.append(f"**Verdict:** {r.get('verdict','?')}"
                 + (f" · **RealRank score {score}**" if score is not None else
                    " · *score pending histogram*"))
    lines += ["", f"*{r.get('one_liner','')}*", "", "## What the review sites say", ""]
    for s in r.get("sources", []):
        q = f'  \n  > “{s["short_quote"]}” — {s["name"]}' if s.get("short_quote") else ""
        facts = "; ".join(s.get("key_facts", []))
        lines.append(f"- **{s['name']}** ({s.get('verdict_or_award','')}): {facts}{q}")
    osd = r.get("owner_sentiment") or {}
    if osd.get("avg_rating"):
        proxy = " *(proxy: " + (osd.get("proxy_note") or "") + ")*" if osd.get("is_proxy") else ""
        lines += ["", "## Owner sentiment",
                  f"{osd['avg_rating']}★ across {osd.get('review_count','?')} reviews{proxy}"]
    lines += ["", "## Pros", *[f"- {p}" for p in r.get("pros", [])],
              "", "## Cons", *[f"- {c}" for c in r.get("cons", [])]]
    if r.get("cheaper_alternative"):
        ca = r["cheaper_alternative"]
        lines += ["", "## Cheaper alternative",
                  f"**{ca['name']}** ({ca.get('approx_price','')}) — {ca.get('why','')}"]
    lines += ["", "## Summary", r.get("summary", "")]
    cov = r.get("source_coverage") or []
    if cov:
        # Printed in the brief on purpose: the whole point is that a missing authority is
        # visible instead of being papered over by an easier-to-reach site.
        mark = {"fetched": "✔ fetched", "searched": "· via search", "unavailable": "✕ unavailable"}
        lines += ["", "## Source coverage", ""]
        lines += [f"- **{c.get('name','?')}** — {mark.get(c.get('status'), c.get('status',''))}"
                  + (f" ({c['note']})" if c.get("note") else "") for c in cov]
    return "\n".join(lines)


def slugify(product):
    return re.sub(r"[^a-z0-9]+", "-", product.lower()).strip("-")[:60] or "product"


OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def research_product(product, out_stem=None, should_cancel=None, extra_owner_sources=None,
                     brand="", asin=""):
    """Full run: fetch the named sources ourselves, distill, score, write the three files.

    `out_stem` defaults to out/<product-slug> so concurrent/repeat runs don't overwrite one
    another (the old default wrote realrank_out.* into the CWD). `should_cancel` is polled
    between work units so this can run as a cancellable job.
    """
    if out_stem is None:
        os.makedirs(OUT_DIR, exist_ok=True)
        out_stem = os.path.join(OUT_DIR, slugify(product))

    print(f"[realrank] fetching named sources for: {product}")
    docs = fetch_source_docs(product, should_cancel=should_cancel)
    for d in docs:
        status = f"{len(d['markdown']):>6} chars via {d['via']}" if d["markdown"] else f"FAILED — {d['error']}"
        print(f"  {d['label']:<24} {status}")
    got = sum(1 for d in docs if d["markdown"])
    print(f"[realrank] {got}/{len(docs)} named sources retrieved")

    # IDENTITY FROM THE REVIEWERS. Every review links to the product it tested, so an ASIN
    # taken from those links is stated by someone who had the thing in their hands — better
    # than searching Amazon by name, which ranks by popularity and returns competitors (it
    # once scored Amazon Basics, 52k ratings, as the Le Creuset).
    #
    # But a roundup page links 20+ products, and proximity alone picks the wrong one: on the
    # ATK Dutch-oven page it chose a Le Creuset BREAD oven. So a candidate is only accepted
    # if the LISTING TITLE actually matches the product — the type noun has to agree.
    family, coverage_by_family = set(), []
    try:
        from intake.products.review_facts import consensus_asin, asins_in
        from intake.products import amazon_rainforest as az

        if asin:
            # We already know the product. Pull its whole variation FAMILY (all colours and
            # sizes) so a reviewer who tested the White 4.5qt still counts as covering our
            # Cerise 5.5qt — one ASIN would miss them.
            fam = az.variant_asins(asin)
            family = fam["family"]
            print(f"[realrank] variant family: {len(family)} ASINs (parent {fam['parent'] or '—'})")
        else:
            cand = (consensus_asin(docs, _keywords(product)
                                   + ([brand] if brand else [])) or {}).get("asin")
            if cand:
                fam = az.variant_asins(cand)
                ok, why = _verify_asin(cand, product)
                if ok:
                    asin, family = cand, fam["family"]
                    print(f"[realrank] ASIN {asin} from the reviewers' own links "
                          f"(family of {len(family)})")
                else:
                    print(f"[realrank] reviewer-link ASIN {cand} REJECTED — {why}; "
                          f"falling back to a brand-matched search")

        # Which sources actually reviewed THIS product, as opposed to merely mentioning the
        # brand? A link into the family is proof; it's the same test that rejects the bread
        # oven, but stated as evidence rather than a veto.
        if family:
            for d in docs:
                if d.get("markdown") and (set(asins_in(d["markdown"])) & family):
                    coverage_by_family.append(d["label"])
            if coverage_by_family:
                print(f"[realrank] linked this exact product: {', '.join(coverage_by_family)}")
    except Exception as e:
        print(f"[realrank] variant-family identity skipped: {e}")

    owner = fetch_owner_data(product, asin=asin, brand=brand)
    if owner:
        print(f"[realrank] amazon {owner.get('asin')}: {owner.get('rating')}★ × "
              f"{owner.get('ratings_total')} ratings, histogram {owner.get('histogram')}")
    print("[realrank] distilling…")

    # Second checkpoint: don't pay for the model call if a cancel arrived during the fetches.
    if should_cancel and should_cancel():
        raise KeyboardInterrupt("cancelled before the research call")
    raw = run_research(product, docs=docs, owner=owner)
    # Always keep the raw reply BEFORE parsing — a research run costs real search calls, so a
    # parse failure must be diagnosable without paying for the whole run again.
    with open(f"{out_stem}.raw.txt", "w", encoding="utf-8") as f:
        f.write(raw)
    record = attach_score(apply_owner_data(parse_record(raw), owner, extra_owner_sources))
    pol = record.get("polarization") or {}
    if pol.get("label"):
        print(f"[realrank] rating shape: {pol['label']} "
              f"(1★ {pol.get('one_star_pct')}% vs detractors {pol.get('detractor_pct')}%"
              f"{', J-shaped' if pol.get('j_shaped') else ''})")
    # Record what WE actually retrieved, independent of the model's own account of it — the
    # audit trail for every attributed fact, incl. the URL to bookmarklet if a fetch failed.
    record["fetch_log"] = [{k: d[k] for k in ("label", "url", "via", "error")} for d in docs]
    # encoding="utf-8" is required, not cosmetic: the brief carries ★ and curly quotes, and
    # Windows' default cp1252 raises UnicodeEncodeError on them after the run is already paid for.
    with open(f"{out_stem}.json", "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    with open(f"{out_stem}.md", "w", encoding="utf-8") as f:
        f.write(to_markdown(record))
    # The POSTING — the surface a reader actually sees (editorial note / full card /
    # compact), rendered from the same record. See realrank_posting.
    from realrank_posting import to_html
    with open(f"{out_stem}.html", "w", encoding="utf-8") as f:
        f.write(to_html(record))
    record["_files"] = {"json": f"{out_stem}.json", "md": f"{out_stem}.md",
                        "html": f"{out_stem}.html", "raw": f"{out_stem}.raw.txt"}
    print(f"[realrank] {record.get('verdict')} · score {record.get('realrank_score')} "
          f"· wrote {out_stem}.json/.md/.html")
    return record


def to_product_blocks(record, job_id=None):
    """Map a run record onto the two product_model blocks: (realrank, realstory).

    A translation, not a copy. The run calls them `sources`, the product model calls them
    `findings` (to keep them distinct from curator-ingested `verdicts`), and the per-source
    fetch rung from `fetch_log` is joined in so a finding read from a Wayback snapshot is
    visibly not current.

    The split is by TRUST BASIS and LIFETIME, not by tidiness: RealRank is arithmetic that
    goes stale as ratings move and needs no sign-off; RealStory is our prose, barely ages,
    and must be read by a human before it earns.
    """
    o = record.get("owner_sentiment") or {}
    via_by_name = {f.get("label"): f.get("via") or ("failed" if f.get("error") else "")
                   for f in (record.get("fetch_log") or [])}
    now = _now_iso()

    findings = []
    for s in (record.get("sources") or []):
        name = s.get("name", "")
        # fetch_log keys on our source LABEL; the model may report a longer name
        # ("Reviewed (USA Today)"), so match on prefix before falling back to 'search'.
        via = next((v for k, v in via_by_name.items() if k and name.startswith(k)), "search")
        findings.append({
            "name": name, "url": s.get("url", ""), "type": s.get("type", "expert"),
            "verdict_or_award": s.get("verdict_or_award", ""),
            "key_facts": s.get("key_facts") or [],
            "short_quote": s.get("short_quote") or "",
            "via": via, "fetched_at": now,
        })

    rating_sources = []
    for ps in (o.get("pooled_from") or []):
        rating_sources.append({
            "source": ps.get("source", ""),
            "listing_id": o.get("asin", "") if ps.get("source") == "amazon" else "",
            "url": o.get("source_url", "") if ps.get("source") == "amazon" else "",
            "avg_rating": o.get("avg_rating") if ps.get("source") == "amazon" else None,
            "count": ps.get("total"), "histogram": [], "fetched_at": now,
        })

    realrank = {
        "score": record.get("realrank_score"),
        "score_basis": record.get("realrank_score_basis", ""),
        "owner": {
            "avg_rating": o.get("avg_rating"),
            "review_count": o.get("review_count"),
            "histogram": o.get("distribution_counts") or [],
            "sources": rating_sources,
            "polarization": record.get("polarization") or {},
        },
        "computed_at": now,
        "job_id": job_id,
    }
    realstory = {
        "verdict": record.get("verdict", ""),
        "one_liner": record.get("one_liner", ""),
        "summary": record.get("summary", ""),
        "aspects": record.get("aspects") or [],
        "pros": record.get("pros") or [],
        "cons": record.get("cons") or [],
        "cheaper_alternative": record.get("cheaper_alternative"),
        "findings": findings,
        "coverage": record.get("source_coverage") or [],
        "generated_at": now,
        "model": MODEL,
        "job_id": job_id,
        "files": record.get("_files") or {},
        "approved_by": "", "approved_at": "",
    }
    return realrank, realstory


def job_summary(record):
    """The compact dict a job result should carry (the full brief is in the files)."""
    fl = record.get("fetch_log") or []
    return {
        "product": record.get("product"),
        "verdict": record.get("verdict"),
        "realrank_score": record.get("realrank_score"),
        "score_basis": record.get("realrank_score_basis"),
        "sources_fetched": sum(1 for f in fl if not f.get("error")),
        "sources_failed": [f["label"] for f in fl if f.get("error")],
        "files": record.get("_files", {}),
    }


if __name__ == "__main__":
    # Run from the repo root. Several BCC modules resolve recipes.db RELATIVE to the working
    # directory, so invoking this from docs/RealRank silently created a second, empty
    # recipes.db right here and initialised it. Outputs are absolute (OUT_DIR), so the chdir
    # costs nothing. As a job we already run from the server's root.
    os.chdir(_REPO_ROOT)
    product = " ".join(sys.argv[1:]) or "KitchenAid Artisan KSM150PS stand mixer"
    rec = research_product(product)
    print(json.dumps(job_summary(rec), indent=2))