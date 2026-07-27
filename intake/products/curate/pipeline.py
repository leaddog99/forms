"""The curation run: named class -> expert reviews -> ranked, evidenced picks.

Four stages, in one tracked pass:

  1. FETCH    the named authorities' own pages, through BCC's tiered ladder (SERP -> UA ->
              unblocker -> Wayback). Cached on disk: fetching eight publishers costs ~2
              minutes and every retry — a schema tweak, a truncated reply — would re-pay it.
  2. RESEARCH one grounded LLM call over those documents. The raw reply is written BEFORE
              parsing: the call costs minutes and money, and losing it to a parse error means
              paying twice. Truncation is reported AS truncation, never as malformed JSON.
  3. VERIFY   shape, then the enrichment only we can do — ASIN identity against the listing,
              blank ASINs recovered from our own review corpus, real owner histograms, and
              buy links stripped of the reviewer's affiliate tag.
  4. RENDER   the written brief.

Extracted from `experiments/curate/run.py` unchanged in behaviour. What production adds is a
cancel checkpoint between sources, progress printed for the job log, and no `chdir` (a direct
run used to create a second empty recipes.db beside the real one).
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Callable

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "..", ".."))
_REALRANK = os.path.join(_ROOT, "docs", "RealRank")
for _p in (_ROOT, _REALRANK):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from intake.products.curate import prompt as P, render as R, verify as V  # noqa: E402

CACHE_DIR = os.path.join(_ROOT, "cache", "curate")
MAX_TOKENS = 32000


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "class"


def cache_path(product_class: str, suffix: str = "docs.json") -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{_slug(product_class)}.{suffix}")


def fetch_docs(product_class: str, *, refresh: bool = False,
               should_cancel: Callable[[], bool] | None = None) -> list:
    """The named sources, CACHED on disk by class."""
    path = cache_path(product_class)
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            docs = json.load(f)
        got = sum(1 for d in docs if d.get("markdown"))
        print(f"[CURATE] {got}/{len(docs)} sources from cache ({os.path.basename(path)}) "
              f"— pass refresh to re-fetch")
        return docs

    import realrank_research as rr
    print(f"[CURATE] fetching named sources for: {product_class}")
    docs = rr.fetch_source_docs(product_class)
    for d in docs:
        state = f"{len(d['markdown']):>6} chars via {d['via']}" if d.get("markdown") \
            else f"FAILED — {d.get('error')}"
        print(f"[CURATE]   {d['label']:<24} {state}")
    if should_cancel and should_cancel():
        raise KeyboardInterrupt("cancelled after fetching sources")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(docs, f)
    return docs


def research(product_class: str, categories=None, *, refresh: bool = False,
             should_cancel: Callable[[], bool] | None = None) -> dict:
    """Fetch the authorities ourselves, then ask the model to curate FROM THEM.

    No `categories` means the WHOLE CLASS — the overall three and nothing else. It never
    means "pick some": a category is a subset a human chose to carve out, and inventing one
    decides what gets recommended at all.
    """
    categories = P.normalize_categories(categories)
    import realrank_research as rr

    rows = 3 + 3 * len(categories)
    print(f"[CURATE] class: {product_class}")
    print(f"[CURATE] categories: " + (", ".join(categories) if categories
                                      else "NONE — ranking the whole class"))
    if rows >= 24:
        print(f"[CURATE] WARNING: {rows} rich rows; 24 truncated a run at {MAX_TOKENS} output. "
              f"If the reply comes back TRUNCATED, ask for fewer categories.")

    docs = fetch_docs(product_class, refresh=refresh, should_cancel=should_cancel)
    got = sum(1 for d in docs if d.get("markdown"))
    print(f"[CURATE] {got}/{len(docs)} sources retrieved; curating…")

    text = P.build_prompt(product_class, categories, docs)
    import llm
    # No `temperature`: deprecated on current Sonnet, and passing it is a hard 400.
    with llm.stream(operation="curate_research", model=rr.MODEL, max_tokens=MAX_TOKENS,
                    messages=[{"role": "user", "content": text}]) as s:
        msg = s.get_final_message()
    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    # SAVE BEFORE PARSING — the reply is the expensive artifact.
    rawpath = cache_path(product_class, "raw.txt")
    with open(rawpath, "w", encoding="utf-8") as f:
        f.write(raw)
    print(f"[CURATE] raw reply saved to {os.path.basename(rawpath)} ({len(raw)} chars)")

    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError(
            f"model hit max_tokens — the JSON is TRUNCATED, not malformed. {len(raw)} chars "
            f"written to {rawpath}. Ask for fewer categories, or raise max_tokens.")

    m = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.S) or re.search(r"(\{.*\})", raw, re.S)
    if not m:
        raise ValueError("no JSON in the model reply:\n" + raw[:400])
    data = json.loads(m.group(1))
    data.setdefault("product_class", product_class)
    # What was ASKED FOR, not merely what came back: without it a renamed, dropped or invented
    # category is indistinguishable from a requested one.
    data["categories_requested"] = categories
    return data


def verify_and_render(data: dict, *, use_network: bool = True) -> tuple:
    """Shape -> enrichment -> brief. Returns (report, brief_text).

    Raises on a shape error: refusing to build beats emitting a plausible-but-broken artifact
    that a curator would have to disprove rather than merely read.
    """
    errs = V.validate_shape(data)
    if errs:
        raise ValueError("shape errors — refusing to build:\n  - " + "\n  - ".join(errs))
    print("[CURATE] shape OK; verifying identity + owner evidence…")
    report = V.enrich(data, use_network=use_network)
    for key in ("verified", "filled", "offers", "scored", "rejected", "notes"):
        for item in report.get(key) or []:
            print(f"[CURATE]   [{key}] {item}")
    return report, R.render(data, report)


def run(product_class: str, categories=None, *, refresh: bool = False,
        use_network: bool = True,
        should_cancel: Callable[[], bool] | None = None) -> dict:
    """The whole pass. Returns {record, report, brief_text}."""
    data = research(product_class, categories, refresh=refresh, should_cancel=should_cancel)
    if should_cancel and should_cancel():
        raise KeyboardInterrupt("cancelled before verification")
    report, brief_text = verify_and_render(data, use_network=use_network)
    return {"record": data, "report": report, "brief_text": brief_text}


def picks_from(data: dict) -> list:
    """Flatten a validated record into one row per PLACEMENT, in publication order.

    `slot` is the stable key — "overall.1", "best-value.2". It is not the ASIN, because a pick
    with no verifiable ASIN is a correct answer ("a blank is a correct answer; a guess is
    not"), and it is not the product name, which the model may reword between runs.
    """
    out = []
    for r in sorted(data.get("overall_top_three") or [],
                    key=lambda x: int(x.get("place", 9))):
        out.append(dict(r, _section="", _slot=f"overall.{r.get('place', '')}"))
    bycat: dict[str, list] = {}
    for r in data.get("category_rankings") or []:
        bycat.setdefault(str(r.get("category") or "?"), []).append(r)
    for cat, rows in bycat.items():
        for r in sorted(rows, key=lambda x: int(x.get("place", 9))):
            out.append(dict(r, _section=cat,
                            _slot=f"{_slug(cat)}.{r.get('place', '')}"))
    return out
