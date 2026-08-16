"""HTTP surface for the Recipe Enrichment API — the real network boundary that
BCC and TBOTB call AFTER the split.

During build-up the monolith (customer zero) calls `enrich()` in-process and does
NOT use this module; it exists so the boundary is real, runnable, and testable
now. At the split, BCC/TBOTB call `POST /enrich` over the network with their own
(encrypted) LLM key.

Run standalone:  uvicorn enrich.service:app --port 8011

Contract reminders (enforced by enrich(), surfaced here):
  • no fetch — the caller passes already-fetched content in
  • BYOK — the caller's key is decrypted at THIS edge, used once, discarded;
    never persisted, never logged
  • stateless — no request input is retained
  • the `public` profile is the seal; the body never crosses on a public read
"""
from __future__ import annotations

from typing import Optional

from pathlib import Path

# Load .env so the standalone service has LLM keys for its calls. Harmless when
# mounted in the main server (which already loaded .env) — env is process-wide.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .api import (
    EnrichmentError, EnrichmentRequest, available_enrichment_blocks, enrich,
)
from .measurement import convert as convert_measure, resolve_canonical
from .measurement import store as measures_store
from .serialize import SealError

app = FastAPI(title="Recipe Enrichment API", version="0.1.0")

_TEST_FORM = Path(__file__).resolve().parent / "test_form.html"
_MEASURES_ADMIN = Path(__file__).resolve().parent / "measures_admin.html"


@app.on_event("startup")
def _seed_and_sync_reference_data() -> None:
    """Seed Enrich's reference table on first boot and point the engine at the
    live (editable) table, so admin edits are authoritative at runtime."""
    try:
        n = measures_store.sync_engine()
        print(f"[OK] Enrich measurement table synced ({n} engine entries)")
    except Exception as e:
        print(f"[WARN] measurement table sync failed ({e}); "
              f"engine stays on the bundled JSON seed")


@app.get("/")
def test_form():
    """Self-contained test harness for the API (same-origin, no CORS). Open this
    in a browser to exercise extract + profiles + per-block selection."""
    return FileResponse(str(_TEST_FORM), media_type="text/html")


_ENRICH_NAV = Path(__file__).resolve().parent / "enrich-nav.js"


@app.get("/enrich-nav.js")
def enrich_nav_js():
    """Enrich's own cross-page hamburger nav, shared by every Enrich page."""
    return FileResponse(str(_ENRICH_NAV), media_type="application/javascript")


@app.get("/blocks")
def list_blocks() -> dict:
    """The enrichment block names available to request — read off the live
    registry so the test form (and any caller) can enumerate them."""
    return {"blocks": list(available_enrichment_blocks())}


class ConvertBody(BaseModel):
    """One ingredient-aware unit conversion. Deterministic — no LLM, no fetch."""
    amount: float
    from_unit: str
    to_unit: str
    ingredient: Optional[str] = None          # canonical/alias name -> KA density
    density_g_per_ml: Optional[float] = None   # override / supply for volume<->mass
    grams_per_item: Optional[float] = None     # override / supply for count<->mass


@app.post("/convert")
def convert_endpoint(body: ConvertBody) -> dict:
    """Single ingredient-context-aware conversion (cup<->g<->ml<->oz<->each…).
    Cross-domain needs a density/per-item bridge: pass `ingredient=` to look one
    up in the King Arthur table (alias/fuzzy/unit-aware, same resolver the recipe
    pass uses), or supply it explicitly. Returns the converted amount, the matched
    canonical ingredient, and the resolution `path` for debugging."""
    density, per_item = body.density_g_per_ml, body.grams_per_item
    canonical = None
    ing_arg = None
    if body.ingredient and density is None and per_item is None:
        # Smart-resolve the name (try both units for count context like "clove").
        ing = (resolve_canonical(body.ingredient, body.from_unit)
               or resolve_canonical(body.ingredient, body.to_unit))
        if ing is not None:
            canonical, density, per_item = ing.name, ing.g_per_ml, ing.grams_per_item
        else:
            ing_arg = body.ingredient  # let the engine raise a clear 404
    try:
        r = convert_measure(
            body.amount, body.from_unit, body.to_unit, ing_arg,
            density_g_per_ml=density, grams_per_item=per_item,
        )
    except KeyError as e:  # unknown ingredient
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:  # unknown unit / missing bridge constant
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "amount": r.amount, "to_unit": r.to_unit, "grams": r.grams,
        "canonical": canonical, "path": r.path,
    }


class EnrichBody(BaseModel):
    """Already-fetched content + transform options. Mirrors EnrichmentRequest."""
    markdown: str = ""
    jsonld: Optional[dict] = None
    source_url: str = ""
    title: str = ""
    page_language: str = "en"             # source language
    target_language: str = "en"           # canonical language to land in
    enrich: list[str] = Field(default_factory=list)   # enrichment block names
    do_identity: bool = True
    do_embed: bool = False
    do_measurements: bool = False                      # ingredient-aware unit conversion
    profile: str = "full"                              # full | static | public
    # Authority scores from TBOTB (not fetched here) — interpreted by the
    # editorial scoreCommentary block. {pageAuthority, domainAuthority, ouScore,
    # rootDomain, ...}
    scoring: Optional[dict] = None
    # BYOK: the caller's own LLM key, encrypted to THIS service's public key.
    # Decrypted at this edge (see _decrypt_byok), used for the inference call,
    # then discarded. NEVER persisted, NEVER logged.
    llm_key_encrypted: Optional[str] = None


class EnrichResponse(BaseModel):
    recipe: dict
    embedding: Optional[list] = None
    meta: dict = Field(default_factory=dict)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "recipe-enrichment", "version": "0.1.0"}


# --------------------------------------------------------------------------- #
# Measurement reference admin (a/c/d) — Enrich's OWN curated ingredient table.
# Admin-only surface (gate at deployment, like the other a/c/d editors). The
# page and its fetches are served from this same mount, so Enrich stays a
# self-contained product with its own admin chrome.
# --------------------------------------------------------------------------- #

class MeasureBody(BaseModel):
    """One ingredient_measures row from the editor. Numbers may arrive as null
    for the bridge that doesn't apply to the row's kind."""
    name: str
    kind: str = "density"                       # density | count
    g_per_ml: Optional[float] = None
    grams_per_item: Optional[float] = None
    grams_per_cup: Optional[float] = None
    aliases: list[str] = Field(default_factory=list)
    provenance: str = "curated_reference"       # king_arthur|curated_reference|llm_derived|measured
    source_note: Optional[str] = None
    notes: Optional[str] = None
    description: Optional[str] = None            # free text; also feeds the LLM estimate


@app.get("/measures")
def measures_admin_page():
    """The a/c/d editor for the ingredient measurement table (HTML)."""
    return FileResponse(str(_MEASURES_ADMIN), media_type="text/html")


@app.get("/measures/rows")
def measures_list(search: Optional[str] = None) -> dict:
    """All ingredient rows (optionally substring-filtered by name/alias)."""
    return {"rows": measures_store.list_rows(search)}


@app.get("/measures/rows/{row_id}")
def measures_get(row_id: int) -> dict:
    row = measures_store.get_row(row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="ingredient not found")
    return row


@app.post("/measures/rows")
def measures_create(body: MeasureBody) -> dict:
    try:
        new_id = measures_store.create_row(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return measures_store.get_row(new_id)


@app.put("/measures/rows/{row_id}")
def measures_update(row_id: int, body: MeasureBody) -> dict:
    if measures_store.get_row(row_id) is None:
        raise HTTPException(status_code=404, detail="ingredient not found")
    measures_store.update_row(row_id, body.model_dump())
    return measures_store.get_row(row_id)


@app.delete("/measures/rows/{row_id}")
def measures_delete(row_id: int) -> dict:
    if not measures_store.delete_row(row_id):
        raise HTTPException(status_code=404, detail="ingredient not found")
    return {"deleted": row_id}


@app.post("/measures/export")
def measures_export() -> dict:
    """Dump the live table to a JSON snapshot a fresh Enrich node can boot from
    (the 'produce the file on demand' path)."""
    out = Path(__file__).resolve().parent / "data" / "ingredient_weights.generated.json"
    n = measures_store.export_snapshot(str(out))
    return {"exported": n, "path": str(out)}


class EstimateBody(BaseModel):
    """Ask the LLM for a grams-per-cup estimate for a food item we have no
    measured/reference value for. Name + aliases + description are the context."""
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    kind: str = "density"


@app.post("/measures/estimate")
def measures_estimate(body: EstimateBody) -> dict:
    """One-time, curator-assisted estimate: the model proposes grams_per_cup
    (helped by aliases + description); the curator reviews and saves it into the
    row. After that the deterministic engine resolves it for free forever. The
    LLM cost is logged to Enrich's OWN token journal (enrich.db), not BCC's."""
    from .measurement.estimate import estimate_cup_weight, EstimateError
    from . import journal
    if body.kind == "count":
        raise HTTPException(status_code=400,
                            detail="estimate is for per-cup weight (density); "
                                   "count items use grams-per-item")
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    try:
        out = estimate_cup_weight(body.name, aliases=tuple(body.aliases),
                                  description=body.description)
    except EstimateError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"estimate failed: {e}") from e
    # Log the LLM cost to Enrich's own journal (best-effort, never breaks).
    usage = out.pop("usage", [])
    try:
        journal.write_usage(usage, context=f"estimate: {body.name.strip()}")
    except Exception as e:
        print(f"[WARN] enrich journal write skipped: {e}")
    out["provenance"] = "llm_derived"   # suggested provenance for the saved row
    return out


@app.post("/enrich", response_model=EnrichResponse)
def enrich_endpoint(body: EnrichBody) -> EnrichResponse:
    """Transform already-fetched content into a structured recipe, shaped by
    `profile`. The caller did the fetch; this never reaches out to the source."""
    llm_key = _decrypt_byok(body.llm_key_encrypted)  # used-then-discarded
    req = EnrichmentRequest(
        markdown=body.markdown,
        jsonld=body.jsonld,
        source_url=body.source_url,
        title=body.title,
        page_language=body.page_language,
        target_language=body.target_language,
        enrich=frozenset(body.enrich),
        do_identity=body.do_identity,
        do_embed=body.do_embed,
        do_measurements=body.do_measurements,
        profile=body.profile,        # type: ignore[arg-type]
        scoring=body.scoring,
        llm_key=llm_key,
    )
    try:
        result = enrich(req)
    except EnrichmentError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except SealError as e:
        # public profile asked for, but the record has no source link to point at
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e  # bad profile, etc.
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"enrichment failed: {e}") from e
    return EnrichResponse(
        recipe=result.recipe, embedding=result.embedding, meta=result.meta,
    )


def _decrypt_byok(token: Optional[str]) -> Optional[str]:
    """Decrypt the caller's BYOK key (encrypted to this service's public key)
    and return the plaintext for one-time use. Never logs the token.

    STUB (DL-5): asymmetric decryption + per-request injection into the LLM
    clients is deferred — the existing extract functions read ANTHROPIC_API_KEY
    from env, so during build-up we use that. Returning None here means "use the
    ambient env key." Wiring real per-request BYOK is a tracked next step.
    """
    if not token:
        return None
    # TODO(BYOK): asymmetric-decrypt `token` with the service private key, then
    # inject the plaintext into the LLM client for this call only.
    return None
