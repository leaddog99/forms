"""Curation runner — research, validate, enrich, render.

EXPERIMENTAL. Registered nowhere; touches no production table.

    python -m experiments.curate.run research "Dutch ovens"      # grounded research -> JSON
    python -m experiments.curate.run brief results.json          # validate + enrich -> text

Split deliberately. `research` costs money and minutes; `brief` is cheap and re-runnable, so
a result can be re-verified and re-rendered as often as you like without paying again. It
also means a JSON produced ANYWHERE — ChatGPT, Claude, by hand — can be run through our
verification and come out the far side as a grounded brief.
"""
from __future__ import annotations

import json
import os
import re
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for p in (_ROOT, os.path.join(_ROOT, "docs", "RealRank")):
    if p not in sys.path:
        sys.path.insert(0, p)

from experiments.curate import prompt as P, render as R, verify as V  # noqa: E402

# Seven categories x 3 rows + 3 overall = 24 rich rows, and the first live run truncated
# at 32k output. Four is a comfortable ask; pass more explicitly when a class warrants it.
DEFAULT_CATEGORIES = [
    "Classic all-purpose", "Best value", "Best for braising", "Bread baking",
]


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_ROOT, ".env"))
    except ImportError:
        pass


def _doc_cache_path(product_class: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", product_class.lower()).strip("-")
    os.makedirs(os.path.join(_ROOT, "experiments", "curate", "cache"), exist_ok=True)
    return os.path.join(_ROOT, "experiments", "curate", "cache", f"{slug}.docs.json")


def fetch_docs(product_class: str, *, refresh: bool = False) -> list:
    """The named sources, CACHED.

    Fetching eight publishers through the unblocker costs ~2 minutes. Without a cache every
    retry — a deprecated parameter, a truncated reply, a schema tweak — re-pays it in full.
    Six minutes of a failed session went on re-fetching identical pages.
    """
    path = _doc_cache_path(product_class)
    if not refresh and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            docs = json.load(f)
        got = sum(1 for d in docs if d.get("markdown"))
        print(f"[curate] {got}/{len(docs)} sources from cache ({os.path.basename(path)}) "
              f"— delete it or pass --refresh to re-fetch")
        return docs

    import realrank_research as rr
    print(f"[curate] fetching named sources for: {product_class}")
    docs = rr.fetch_source_docs(product_class)
    for d in docs:
        state = f"{len(d['markdown']):>6} chars via {d['via']}" if d["markdown"] \
            else f"FAILED — {d['error']}"
        print(f"  {d['label']:<24} {state}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(docs, f)
    return docs


def research(product_class: str, categories=None, *, refresh: bool = False) -> dict:
    """Fetch the named authorities ourselves, then ask the model to curate from them."""
    _load_env()
    import realrank_research as rr

    docs = fetch_docs(product_class, refresh=refresh)
    got = sum(1 for d in docs if d["markdown"])
    print(f"[curate] {got}/{len(docs)} sources; curating…")

    text = P.build_prompt(product_class, categories or DEFAULT_CATEGORIES, docs)
    import llm
    # No `temperature`: deprecated on current Sonnet, and passing it is a hard 400.
    with llm.stream(operation="curate_research", model=rr.MODEL, max_tokens=32000,
                    messages=[{"role": "user", "content": text}]) as s:
        msg = s.get_final_message()
    raw = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    # SAVE BEFORE PARSING. A research call costs minutes and money; losing the reply to a
    # parse error means paying for it twice. (Learned in realrank_research, then promptly
    # not carried into this file — which is how the first two runs left nothing behind.)
    rawpath = _doc_cache_path(product_class).replace(".docs.json", ".raw.txt")
    with open(rawpath, "w", encoding="utf-8") as f:
        f.write(raw)
    print(f"[curate] raw reply saved to {os.path.basename(rawpath)} ({len(raw)} chars)")

    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError(
            f"model hit max_tokens — the JSON is TRUNCATED, not malformed. {len(raw)} chars "
            f"written to {rawpath}. Ask for fewer categories, or raise max_tokens.")

    m = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.S) or re.search(r"(\{.*\})", raw, re.S)
    if not m:
        raise ValueError("no JSON in the model reply:\n" + raw[:400])
    data = json.loads(m.group(1))
    data.setdefault("product_class", product_class)
    return data


def brief(path: str, *, use_network: bool = True) -> str:
    """Validate SHAPE, then enrich with identity + owner evidence, then render."""
    _load_env()          # enrichment needs RAINFOREST_KEY; without it every row silently
                         # degraded to "listing lookup failed" and the brief lost its evidence
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    errs = V.validate_shape(data)
    if errs:
        print("SHAPE ERRORS — refusing to build:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)

    print("[curate] shape OK; verifying identity + owner evidence…", file=sys.stderr)
    report = V.enrich(data, use_network=use_network)
    return R.render(data, report)


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "research":
        if len(argv) < 2:
            print("usage: research \"<product class>\" [out.json]")
            return 1
        data = research(argv[1], refresh="--refresh" in argv)
        out = argv[2] if len(argv) > 2 else "curate_results.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"wrote {out}")
        return 0
    if cmd == "brief":
        if len(argv) < 2:
            print("usage: brief <results.json> [out.txt] [--offline]")
            return 1
        text = brief(argv[1], use_network="--offline" not in argv)
        outs = [a for a in argv[2:] if not a.startswith("--")]
        if outs:
            with open(outs[0], "w", encoding="utf-8") as f:
                f.write(text)
            print(f"wrote {outs[0]}", file=sys.stderr)
        print(text)
        return 0
    print(f"unknown command {cmd!r}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
