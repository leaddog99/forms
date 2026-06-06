"""Recipe Enrichment API — the contract.

Carved from the monolith via the strangler pattern (see docs/split-phase1-map.md
§0 and the rollout plan). This is the SINGLE home for the per-recipe transform:
extract → translate → sanitize → enrich → identity → embed → seal.

Customer zero is us: `save_recipe_api`'s extract paths call `enrich()` in-process
instead of running their own inline copies. Whatever that bypasses is the
empirical cut list for the split. BCC and TBOTB become customers #2/#3 — calling
the SAME entrypoint, over HTTP, only at the physical split.

THE CONTRACT (what makes this the third entity, not just a helper):

  • It TRANSFORMS already-fetched content; it does NOT fetch. The caller
    (BCC = user-session bookmarklet; TBOTB = bot crawl) fetches and passes the
    content in. Keeping fetch out of here preserves the legal actor distinction
    (SplitSpec §181).

  • BYOK. The caller passes its OWN llm key; it is used for the inference
    call(s) then discarded. NEVER persisted, NEVER logged. Inference is billed by
    the LLM vendor to the caller's account; this service charges a flat
    value-add fee, immune to token/price volatility.

  • STATELESS. Retains no content and no keys. No DB, no cache, no scoring. The
    TBOTB index/cache and the scoring/ranking moat stay OUTSIDE this service.

  • SEAL via `profile` (full | static | public) — the one canonical serializer.
    The API returns the FULL recipe to authenticated callers (BCC: the user's own
    copy; TBOTB: needs the body internally to score). The `public` seal — "what
    doesn't come back" — is enforced downstream at TBOTB's emit boundary, using
    this same serializer. BCC never emits publicly, so it never seals.

  • Generates an embedding VECTOR (a transform). It does NOT compare that vector
    to the corpus — grade/rank is TBOTB's moat, not enrichment.

In-process now (a plain function call, no network) so the monolith pays no HTTP
cost during build-up. The identical logic is exposed over HTTP in `service.py`
(the real network surface BCC/TBOTB call at the split). See SplitSpec Phase 2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# Output-shaping profiles. The seal lives in one canonical serializer:
#   full   — everything (internal processing; the body included)
#   static — body kept, per-user/claim/owner fields dropped (BCC's saved copy)
#   public — body SEALED; work-product + rich-result envelope + link only
#            (TBOTB's public/subscriber emit). "What doesn't come back."
Profile = Literal["full", "static", "public"]


class EnrichmentError(Exception):
    """A hard failure of the transform (e.g. extraction produced nothing
    usable). Best-effort steps (enrich/identity/embed) never raise — they log
    and continue — so this signals only that there is no recipe to return."""


@dataclass
class EnrichmentRequest:
    """Already-fetched content + which transforms to run. The caller did the
    fetch; we never reach out to the source."""

    # --- content (the caller fetched this) ---
    markdown: str = ""                    # cleaned page markdown
    jsonld: Optional[dict] = None         # schema.org Recipe block, if the page shipped one
    source_url: str = ""                  # canonical source URL — provenance + dedup key
    title: str = ""                       # page <title>/<h1> hint
    page_language: str = "en"             # ISO 639-1; drives the translate step

    # --- which transforms to run (callers opt in to the heavy ones) ---
    # Enrichment work-product blocks to generate, BY NAME. Each name = one
    # parallel LLM call (the cost unit), matching the ENRICHMENT_BLOCKS registry
    # in extract/enrich_recipe.py — registry-driven so a NEW block (e.g. nutrition
    # commentary, dietary tags) becomes selectable without editing this contract
    # (the "no data in code" rule). Today's blocks and what each yields:
    #   "provenance"     -> ethnicity, origin, traditional context, variations
    #   "classification" -> the multi-paragraph STORY (+ confidence/reasoning/hierarchy)
    #   "editorial"      -> opinion (the CRITIQUE) + scoreCommentary + sourcingNotes
    # Empty = no enrichment. Unknown names are rejected (validated against the
    # live registry at call time). Add-as-we-go: new registry blocks need no
    # change here.
    enrich: frozenset[str] = frozenset()
    do_identity: bool = True              # dish identity card (fingerprint for matching)
    do_embed: bool = False                # generate the embedding vector
    profile: Profile = "full"             # output shaping / seal

    # --- BYOK ---
    # The caller's OWN llm credential. Decrypted at the HTTP edge (`service.py`);
    # in-process callers pass plaintext. Used for the inference call(s), then
    # dropped. MUST NOT be persisted or logged anywhere in this package.
    llm_key: Optional[str] = None


@dataclass
class EnrichmentResult:
    recipe: dict                          # RecipeModel-shaped, shaped by request.profile
    embedding: Optional[list] = None      # present iff do_embed (a transform, NOT a score)
    meta: dict = field(default_factory=dict)  # timings + token usage, for the caller to journal


def enrich(req: EnrichmentRequest) -> EnrichmentResult:
    """Run the per-recipe transform on already-fetched content.

    Orchestration (each step delegates to the existing monolith implementation
    during build-up; those modules MOVE into this package as the strangle
    proceeds):

      1. extract   jsonld_to_recipe(req.jsonld)  OR  markdown_to_recipe(req.markdown)
      2. translate intake.translate, when req.page_language is non-English
      3. sanitize  sanitize_recipe_data(...)
      4. enrich    extract.enrich_recipe(...)        when req.do_enrich
      5. identity  extract.identity_card(...)        when req.do_identity
      6. embed     embeddings.generate(...)          when req.do_embed -> result.embedding
      7. seal      serialize.apply_profile(recipe, req.profile)

    Does NOT: fetch, cache, score/rank, screenshot, or persist — those stay with
    the caller. Raises EnrichmentError only when step 1 yields no usable recipe;
    the best-effort steps (4-6) log and continue.

    STRANGLER STATUS (DL-4): v1 implements step 1 (extract) + step 7 (seal) by
    delegating to the monolith's existing functions. Steps 2-6 (translate,
    sanitize, enrich blocks, identity, embed) are carved in later, one at a time
    — they are currently no-ops here and remain in the caller. `req.enrich`,
    `req.do_identity`, `req.do_embed`, `req.page_language` are accepted now but
    not yet acted on (documented so callers can already pass them).
    """
    timings: dict = {}
    prompts: dict = {}
    usage_log: list = []

    # --- 1. Extract: JSON-LD fast lane, else the markdown LLM call. Exact same
    #        selection logic the monolith used inline (delegate, don't reimpl). ---
    recipe: Optional[dict] = None
    if req.jsonld:
        try:
            from extract.jsonld_to_recipe import jsonld_to_recipe
            recipe = jsonld_to_recipe(
                req.jsonld, source_url=req.source_url, title=req.title,
                timings=timings,
            )
        except Exception as e:  # parity with the monolith's fall-back-to-LLM
            print(f"[enrich] jsonld_to_recipe raised, falling back to LLM: {e}")
            recipe = None

    if recipe is None:
        from extract.markdown_to_recipe import markdown_to_recipe
        recipe = markdown_to_recipe(
            req.markdown, source_name="", source_url=req.source_url,
            title=req.title, timings=timings, prompts=prompts,
            usage_log=usage_log,
        )

    if recipe is None:
        raise EnrichmentError("extraction produced no usable recipe")

    # --- steps 2-6: deferred carves (see DL-4); currently handled by the caller ---

    # --- 7. Seal / shape by profile (full = identity). ---
    from .serialize import apply_profile
    recipe = apply_profile(recipe, req.profile)

    return EnrichmentResult(
        recipe=recipe,
        embedding=None,  # do_embed deferred (DL-4)
        meta={"timings": timings, "prompts": prompts, "usage": usage_log},
    )
