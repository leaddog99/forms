"""Curation CLI — research, validate, enrich, render, from a terminal.

    python -m experiments.curate.run research "Dutch ovens"      # whole class -> top three
    python -m experiments.curate.run research "Dutch ovens" --categories "Classic; Best value"
    python -m experiments.curate.run brief results.json          # validate + enrich -> text

The engine has MOVED into the application: `intake/products/curate/` (prompt, verify, render,
pipeline) and `intake/products/curated_collections.py`, run as the `curated_collection_run`
job. This file is now a thin CLI over that same code rather than a second copy of it, so a
fix reaches both surfaces — and it stays, because a terminal is still the fastest way to try
a class, re-verify a JSON, or re-render a brief without creating a collection first.

What it does NOT do, deliberately: touch a table. `research` writes a JSON file and `brief`
writes text. Materializing product records is the job's business, where it is tracked and
cancellable.

Split deliberately. `research` costs money and minutes; `brief` is cheap and re-runnable, so
a result can be re-verified and re-rendered as often as you like without paying again. It
also means a JSON produced ANYWHERE — ChatGPT, Claude, by hand — can be run through our
verification and come out the far side as a grounded brief.

Categories are OPTIONAL and have no default set: supplying none means rank the WHOLE class,
not fall back to somebody else's subsets. Pass `--categories "A; B; C"` or repeat
`--category`. `--refresh` re-fetches the source documents instead of using the cache.
"""
from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for p in (_ROOT, os.path.join(_ROOT, "docs", "RealRank")):
    if p not in sys.path:
        sys.path.insert(0, p)

from intake.products.curate import prompt as P, render as R, verify as V  # noqa: E402
from intake.products.curate import pipeline  # noqa: E402


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(_ROOT, ".env"))
    except ImportError:
        pass


def research(product_class: str, categories=None, *, refresh: bool = False) -> dict:
    _load_env()
    return pipeline.research(product_class, categories, refresh=refresh)


def brief(path: str, *, use_network: bool = True) -> str:
    """Validate SHAPE, then enrich with identity + owner evidence, then render."""
    _load_env()          # enrichment needs RAINFOREST_KEY; without it every row silently
                         # degraded to "listing lookup failed" and the brief lost its evidence
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # `[]` is a record (whole class); only a MISSING key leaves nothing to check against.
    if data.get("categories_requested") is None:
        print("[curate] NOTE: this file records no categories_requested, so the categories it "
              "delivered cannot be checked against the ones asked for.", file=sys.stderr)

    errs = V.validate_shape(data)
    if errs:
        print("SHAPE ERRORS — refusing to build:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(1)

    print("[curate] shape OK; verifying identity + owner evidence…", file=sys.stderr)
    report = V.enrich(data, use_network=use_network)
    return R.render(data, report)


_CATEGORY_FLAGS = ("--category", "--categories")


def _parse_research_argv(argv: list) -> tuple:
    """-> (positionals, [raw category strings], refresh). Flags may appear in any position.

    Hand-written rather than argparse'd because a mis-read flag here is not a usage error:
    `research "Dutch ovens" --refresh` previously took "--refresh" as the OUTPUT PATH, and a
    flag swallowed silently is a paid run against the wrong input.
    """
    positional, cats, refresh = [], [], False
    i = 0
    while i < len(argv):
        a, head = argv[i], argv[i].split("=", 1)[0]
        if a == "--refresh":
            refresh = True
        elif head in _CATEGORY_FLAGS:
            if "=" in a:
                cats.append(a.split("=", 1)[1])
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                cats.append(argv[i + 1])
                i += 1
            else:
                raise ValueError(f"{head} needs a value")
        elif a.startswith("--"):
            raise ValueError(f"unknown flag {a!r} — expected --category, --categories "
                             f"or --refresh")
        else:
            positional.append(a)
        i += 1
    return positional, cats, refresh


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "research":
        try:
            positional, cats, refresh = _parse_research_argv(argv[1:])
            if not positional:
                raise ValueError("usage: research \"<product class>\" "
                                 "[--categories \"A; B; C\"] [out.json]")
            # Parsed BEFORE any fetching or spending, so a bad list costs nothing. An EMPTY
            # list is not a failure here — it means the whole class.
            cats = P.normalize_categories(cats)
        except ValueError as e:
            print(f"[curate] REFUSING TO RUN — {e}", file=sys.stderr)
            return 2
        data = research(positional[0], cats, refresh=refresh)
        out = positional[1] if len(positional) > 1 else "curate_results.json"
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
