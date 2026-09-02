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


def _doc_title(md: str) -> str:
    """The fetched page's own first heading — what the page says IT is about. A byte count
    tells the curator a source ANSWERED; the title is what shows an off-topic answer (an
    ATK pressure-COOKER roundup fetched for a pressure-CANNER run reads identically in
    bytes)."""
    for m in re.finditer(r"^#+\s*(.+?)\s*$", md or "", re.M):
        t = m.group(1).strip()
        if t and t.upper() != "PAGE CONTENT":
            return t[:90]
    return (md or "").strip().split("\n", 1)[0][:90]


def cache_path(product_class: str, suffix: str = "docs.json") -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{_slug(product_class)}.{suffix}")


def fetch_docs(product_class: str, *, refresh: bool = False, terms: list | None = None,
               should_cancel: Callable[[], bool] | None = None) -> list:
    """The named sources, CACHED on disk by class. `terms` = the curator's fallback
    search ladder (curated_collections.search_terms); NOTE the cache is keyed by class
    alone, so after editing the terms pass refresh to make them matter."""
    path = cache_path(product_class)
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            docs = json.load(f)
        got = sum(1 for d in docs if d.get("markdown"))
        print(f"[CURATE] {got}/{len(docs)} sources from cache ({os.path.basename(path)}) "
              f"— pass refresh to re-fetch")
        for d in docs:
            state = (f"{len(d['markdown']):>6} chars | {_doc_title(d['markdown'])}"
                     if d.get("markdown") else f"FAILED — {d.get('error')}")
            print(f"[CURATE]   {d['label']:<24} {state}")
        return docs

    import realrank_research as rr
    print(f"[CURATE] fetching named sources for: {product_class}"
          + (f" (fallback terms: {', '.join(terms)})" if terms else ""))
    docs = rr.fetch_source_docs(product_class, terms=terms)
    for d in docs:
        state = (f"{len(d['markdown']):>6} chars via {d['via']} "
                 f"({d.get('class_hits', '?')} on-class) | {_doc_title(d['markdown'])}"
                 if d.get("markdown") else f"FAILED — {d.get('error')}")
        print(f"[CURATE]   {d['label']:<24} {state}")
    if should_cancel and should_cancel():
        raise KeyboardInterrupt("cancelled after fetching sources")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(docs, f)
    return docs


def research(product_class: str, categories=None, *, docs: list | None = None,
             refresh: bool = False, editors_choice: str = "",
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

    if docs is None:
        docs = fetch_docs(product_class, refresh=refresh, should_cancel=should_cancel)
    got = sum(1 for d in docs if d.get("markdown"))
    print(f"[CURATE] {got}/{len(docs)} sources retrieved; curating…")

    if (editors_choice or "").strip():
        print(f"[CURATE] editor's choice pinned: {editors_choice.strip()}")
    text = P.build_prompt(product_class, categories, docs, editors_choice=editors_choice)
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
    # Same idea for the pinned pick: stamping the REQUEST makes its absence checkable.
    if (editors_choice or "").strip():
        data["editors_choice_requested"] = editors_choice.strip()
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
        use_network: bool = True, terms: list | None = None, editors_choice: str = "",
        should_cancel: Callable[[], bool] | None = None) -> dict:
    """The whole pass. Returns {record, report, brief_text, sources}.

    Fetches here rather than inside `research` so the caller can report WHICH authorities
    answered — a run grounded in three publishers and one grounded in eight are not the same
    artifact, and the summary is what a curator reads without opening the log.
    """
    docs = fetch_docs(product_class, refresh=refresh, terms=terms,
                      should_cancel=should_cancel)
    sources = {"retrieved": [d["label"] for d in docs if d.get("markdown")],
               "missing": [d["label"] for d in docs if not d.get("markdown")]}
    data = research(product_class, categories, docs=docs, editors_choice=editors_choice,
                    should_cancel=should_cancel)
    # Per-source ACCOUNTING: fetch facts (ours) merged with the model's reading of each
    # document (source_report in the reply — what the page actually covers, whether it
    # was usable). One row per named authority, failures included, so "Serious Eats got
    # cited again" is answerable from the summary: the others answered off-topic, or
    # didn't answer at all.
    model_rep = {(r.get("source") or "").strip(): r
                 for r in (data.get("source_report") or []) if isinstance(r, dict)}
    sources["report"] = []
    for d in docs:
        row = {"source": d["label"], "via": d.get("via") or "",
               "chars": len(d.get("markdown") or ""),
               "class_hits": d.get("class_hits", 0),
               "page_title": _doc_title(d["markdown"]) if d.get("markdown") else "",
               "error": d.get("error") or ""}
        m = model_rep.get(d["label"])
        if m:
            row.update({"page_covers": (m.get("page_covers") or "").strip(),
                        "relevance": (m.get("relevance") or "").strip(),
                        "used_in_ranking": bool(m.get("used_in_ranking"))})
        elif not d.get("markdown"):
            row["relevance"] = "not-retrieved"
        sources["report"].append(row)
    # Stash on the record: the renderer prints it in the brief, and set_run_result
    # stores the record, so the accounting survives into result_json for the editor.
    data["_source_accounting"] = sources["report"]
    if should_cancel and should_cancel():
        raise KeyboardInterrupt("cancelled before verification")
    report, brief_text = verify_and_render(data, use_network=use_network)
    return {"record": data, "report": report, "brief_text": brief_text, "sources": sources}


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
    # The curator-pinned pick, in its own labeled section — provenance stays visible
    # all the way into the picks table.
    ec = data.get("editors_choice")
    if isinstance(ec, dict):
        out.append(dict(ec, place=1, _section="Editor's Choice", _slot="editors-choice.1"))
    return out
