"""Curation — rank a product class from the expert reviews, with our own evidence attached.

The second acquisition technique for products. The first (`collections_store`) starts from a
saved Amazon search URL and screens the cohort it returns on owner ratings; this one starts
from a product CLASS NAME and the reviews the named authorities published about it.

    prompt    the research prompt (the curator's, grounded in documents we fetched)
    verify    shape validation + enrichment that only BCC can do (ASIN identity, owner
              histograms, buy links, our own review corpus)
    render    the written brief
    pipeline  the four stages as one tracked run

Lifted out of `experiments/curate/` once it had run two classes end to end. The experimental
CLI still imports from here rather than keeping a copy — one canonical path, so a fix reaches
both surfaces.

The package puts the repo root and `docs/RealRank` on `sys.path` itself. Each module used to
do its own, which meant `to_products` only found `realrank_research` when `pipeline` happened
to have been imported first — and its reviewer lookup failed CLOSED, returning "no reviewers"
rather than raising. A dependency that resolves by import order is one that works until it is
used on its own.
"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "..", ".."))
for _p in (_ROOT, os.path.join(_ROOT, "docs", "RealRank")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
