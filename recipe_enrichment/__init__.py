"""Recipe Enrichment API — the third entity (see docs/split-phase1-map.md §0).

The single home for the per-recipe transform. Being carved out of the monolith
via the strangler pattern: the existing system is "customer zero" and calls
`enrich()` in-process instead of running its own inline copies. Whatever that
bypasses becomes the empirical cut list for the split.
"""
from .api import (
    EnrichmentRequest,
    EnrichmentResult,
    EnrichmentError,
    Profile,
    enrich,
)

__all__ = [
    "EnrichmentRequest",
    "EnrichmentResult",
    "EnrichmentError",
    "Profile",
    "enrich",
]
