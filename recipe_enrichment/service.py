"""HTTP surface for the Recipe Enrichment API — the real network boundary that
BCC and TBOTB call AFTER the split.

During build-up the monolith (customer zero) calls `enrich()` in-process and does
NOT use this module; it exists so the boundary is real, runnable, and testable
now. At the split, BCC/TBOTB call `POST /enrich` over the network with their own
(encrypted) LLM key.

Run standalone:  uvicorn recipe_enrichment.service:app --port 8011

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
from .serialize import SealError

app = FastAPI(title="Recipe Enrichment API", version="0.1.0")

_TEST_FORM = Path(__file__).resolve().parent / "test_form.html"


@app.get("/")
def test_form():
    """Self-contained test harness for the API (same-origin, no CORS). Open this
    in a browser to exercise extract + profiles + per-block selection."""
    return FileResponse(str(_TEST_FORM), media_type="text/html")


@app.get("/blocks")
def list_blocks() -> dict:
    """The enrichment block names available to request — read off the live
    registry so the test form (and any caller) can enumerate them."""
    return {"blocks": list(available_enrichment_blocks())}


class EnrichBody(BaseModel):
    """Already-fetched content + transform options. Mirrors EnrichmentRequest."""
    markdown: str = ""
    jsonld: Optional[dict] = None
    source_url: str = ""
    title: str = ""
    page_language: str = "en"
    enrich: list[str] = Field(default_factory=list)   # enrichment block names
    do_identity: bool = True
    do_embed: bool = False
    profile: str = "full"                              # full | static | public
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
        enrich=frozenset(body.enrich),
        do_identity=body.do_identity,
        do_embed=body.do_embed,
        profile=body.profile,        # type: ignore[arg-type]
        llm_key=llm_key,
    )
    try:
        result = enrich(req)
    except EnrichmentError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except SealError as e:
        # public profile asked for, but the record has no source link to point at
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))  # bad profile, etc.
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"enrichment failed: {e}")
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
