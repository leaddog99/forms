"""Deterministic tests for the Recipe Enrichment API.

All tests use the JSON-LD fast lane (no LLM call) so they are deterministic,
free, and offline-safe. Run from the repo root:

    python -m pytest recipe_enrichment/tests/ -q          # if pytest is installed
    python -m recipe_enrichment.tests.test_enrichment     # no-pytest fallback runner
"""
from contextlib import contextmanager


@contextmanager
def _raises(exc):
    """Minimal pytest.raises stand-in so these tests run with OR without pytest
    installed (the venv currently has no pytest)."""
    try:
        yield
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__} to be raised")


from recipe_enrichment import EnrichmentRequest, enrich
from recipe_enrichment.serialize import (
    SealError,
    apply_profile,
    corpus_public_view,
)


JSONLD = {
    "@context": "https://schema.org",
    "@type": "Recipe",
    "name": "Test Banana Bread",
    "recipeIngredient": ["2 ripe bananas", "1 cup flour", "1 egg", "1/2 cup sugar"],
    "recipeInstructions": [
        {"@type": "HowToStep", "text": "Mash the bananas in a bowl."},
        {"@type": "HowToStep", "text": "Mix in flour, egg, sugar; bake 60 min."},
    ],
}


def _req(**kw):
    base = dict(jsonld=JSONLD, source_url="https://example.com/bb", title="BB")
    base.update(kw)
    return EnrichmentRequest(**base)


# --- enrich() -------------------------------------------------------------

def test_enrich_jsonld_path_returns_valid_recipe():
    res = enrich(_req(profile="full"))
    assert res.meta["extract_path"] == "jsonld-direct"
    assert res.recipe.get("name")
    assert len(res.recipe.get("recipeIngredient") or []) == 4
    assert len(res.recipe.get("recipeInstructions") or []) == 2


def test_enrich_full_profile_is_unsealed():
    res = enrich(_req(profile="full"))
    assert "recipeIngredient" in res.recipe
    assert "recipeInstructions" in res.recipe


def test_enrich_public_profile_seals_the_body():
    res = enrich(_req(profile="public"))
    assert "recipeIngredient" not in res.recipe
    assert "recipeInstructions" not in res.recipe
    assert res.recipe.get("sourceLink") == "https://example.com/bb"


# --- the seal serializer --------------------------------------------------

def test_full_profile_is_identity():
    rec = {"name": "x"}
    assert apply_profile(rec, "full") is rec


def test_public_seals_body_keeps_work_product_and_envelope():
    rec = {
        "name": "Banana Bread",
        "recipeIngredient": ["2 bananas"],            # sealed
        "recipeInstructions": [{"text": "bake"}],     # sealed
        "description": "publisher prose",             # sealed
        "aggregateRating": {"ratingValue": 4.8},      # envelope -> kept
        "totalTime": "PT1H",                          # envelope -> kept
        "editorial": {"opinion": "our critique"},     # work-product -> kept
        "_scoring": {"ouScore": 12.3},                # work-product -> kept
        "_identity": {"likelyDish": "Banana Bread"},  # work-product -> kept
        "_source": {
            "originalUrl": "https://example.com/bb",
            "siteName": "Example",
            "previewImage": "/screenshot/x",          # our cooped copy -> sealed
        },
    }
    out = corpus_public_view(rec)
    assert "recipeIngredient" not in out and "recipeInstructions" not in out
    assert "description" not in out
    assert out["editorial"] and out["_scoring"] and out["_identity"]
    assert out["aggregateRating"] and out["totalTime"]
    assert out["sourceLink"] == "https://example.com/bb"
    assert "previewImage" not in out.get("_source", {})


def test_public_requires_a_source_link():
    with _raises(SealError):
        corpus_public_view({"name": "x", "_source": {}})


def test_unknown_profile_raises():
    with _raises(ValueError):
        apply_profile({"name": "x"}, "bogus")


# --- JSON-LD fast-lane coercion (the video-quirk fix) ---------------------

def test_coerce_video_thumbnail_list_to_string():
    from recipe_enrichment.api import _coerce_jsonld_for_fastlane
    src = {"name": "x", "video": {"@type": "VideoObject",
                                  "thumbnailUrl": ["https://a/1.jpg", "https://a/2.jpg"]}}
    out = _coerce_jsonld_for_fastlane(src)
    assert out["video"]["thumbnailUrl"] == "https://a/1.jpg"
    assert isinstance(src["video"]["thumbnailUrl"], list)  # input not mutated


def test_coerce_video_list_to_single_dict():
    from recipe_enrichment.api import _coerce_jsonld_for_fastlane
    out = _coerce_jsonld_for_fastlane(
        {"name": "x", "video": [{"@type": "VideoObject", "thumbnailUrl": ["https://a/1.jpg"]}]}
    )
    assert isinstance(out["video"], dict)
    assert out["video"]["thumbnailUrl"] == "https://a/1.jpg"


def test_coerce_drops_unusable_video():
    from recipe_enrichment.api import _coerce_jsonld_for_fastlane
    out = _coerce_jsonld_for_fastlane({"name": "x", "video": 12345})
    assert "video" not in out


def test_fastlane_diagnostic_never_raises():
    # Best-effort diagnostic: must never throw, even on junk.
    from recipe_enrichment.api import _log_fastlane_validation_errors
    _log_fastlane_validation_errors({"name": "x"}, "https://example.com/x")
    _log_fastlane_validation_errors({}, "")


# --- enrichment block selection (individually selectable) -----------------

def test_available_enrichment_blocks_lists_registry():
    from recipe_enrichment import available_enrichment_blocks
    blocks = available_enrichment_blocks()
    assert "provenance" in blocks
    assert "classification" in blocks
    assert "editorial" in blocks


def test_run_enrichment_blocks_empty_and_unknown_skip_llm():
    # Empty or all-unknown selection must NOT call the LLM — returns no blocks.
    from recipe_enrichment import run_enrichment_blocks
    r = {"name": "X", "recipeIngredient": ["a"], "recipeInstructions": [{"text": "b"}]}
    assert run_enrichment_blocks(r, [])["blocks_run"] == []
    assert run_enrichment_blocks(r, ["bogus", "nope"])["blocks_run"] == []
    assert "provenance" not in r and "editorial" not in r  # nothing mutated


# --- HTTP surface ---------------------------------------------------------

def test_http_health_and_enrich_and_seal():
    from fastapi.testclient import TestClient
    from recipe_enrichment.service import app

    c = TestClient(app)
    assert c.get("/health").json()["status"] == "ok"

    body = {"jsonld": JSONLD, "source_url": "https://example.com/bb", "title": "BB"}

    full = c.post("/enrich", json={**body, "profile": "full"})
    assert full.status_code == 200
    assert full.json()["recipe"].get("recipeIngredient")

    pub = c.post("/enrich", json={**body, "profile": "public"})
    assert pub.status_code == 200
    assert "recipeIngredient" not in pub.json()["recipe"]
    assert pub.json()["recipe"]["sourceLink"] == "https://example.com/bb"

    bad = c.post("/enrich", json={**body, "profile": "nope"})
    assert bad.status_code == 400


if __name__ == "__main__":
    import sys
    fns = sorted(
        (v for k, v in dict(globals()).items()
         if k.startswith("test_") and callable(v)),
        key=lambda f: f.__name__,
    )
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
            passed += 1
        except Exception as e:  # noqa: BLE001
            print("FAIL", fn.__name__, "->", repr(e))
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
