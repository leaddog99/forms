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

import json as _json
import re as _re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

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
    page_language: str = "en"             # ISO 639-1 of the SOURCE page
    # The canonical language to land in — resolved by the caller as
    # per-user-preference -> instance-default -> "en". Translate IFF
    # page_language != target_language. Default "en" keeps today's behavior
    # exactly (so a Greek instance + Greek recipe simply never translates).
    target_language: str = "en"

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
    # Ingredient-context-aware measurement conversion (recipe_enrichment.measurement).
    # DETERMINISTIC engine (KA densities); a per-line LLM fallback fires ONLY for
    # ingredient strings the free parser/resolver can't handle. Writes a
    # `_measurements` block aligned 1:1 with recipeIngredient — the metric/weight
    # layer beside the source's own strings. Off by default (opt-in cost).
    do_measurements: bool = False
    profile: Profile = "full"             # output shaping / seal

    # --- authority scores supplied BY THE CALLER (TBOTB), not fetched here ---
    # Moz PA/DA/OU live in TBOTB's metabase_url store and are its moat; this API
    # is stateless / no-DB / no-scoring, so it never fetches them. But the
    # editorial `scoreCommentary` block INTERPRETS them, so the caller may pass
    # them in and we stamp recipe._scoring before enrichment. Shape (any subset):
    # {pageAuthority, domainAuthority, ouScore, rootDomain, rawTitle}.
    scoring: Optional[dict] = None

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


def _coerce_jsonld_for_fastlane(jsonld: dict) -> dict:
    """Pre-coerce known JSON-LD quirks that would otherwise fail RecipeModel
    validation and needlessly force the expensive markdown-LLM fallback.

    The fast lane is high value (≈1s vs ≈17s), so a single malformed PERIPHERAL
    field must not sink it. Currently handled: the `video` field — publishers
    (e.g. WP Recipe Maker, the Sally's case) ship a VideoObject whose
    `thumbnailUrl` is a LIST of URLs, but RecipeModel wants a string, and that
    lone field rejected the whole recipe. We normalize video to a single object
    with a string thumbnailUrl. Returns a shallow copy; never mutates the input.
    Extend here as new peripheral quirks surface.

    NB (DL): this fix lives ONLY in the enrichment API (per the user) — the
    legacy inline extract path keeps the old behavior, so the API path is now
    strictly faster on these pages.
    """
    if not isinstance(jsonld, dict):
        return jsonld
    out = dict(jsonld)
    v = out.get("video")
    if isinstance(v, list):
        v = next((x for x in v if isinstance(x, dict)), None)
    if isinstance(v, dict):
        v = dict(v)
        tu = v.get("thumbnailUrl")
        if isinstance(tu, list):
            v["thumbnailUrl"] = next((u for u in tu if isinstance(u, str)), "")
        out["video"] = v
    elif v is not None and not isinstance(v, str):
        out.pop("video", None)  # unusable shape -> drop (video is non-essential)
    return out


def _log_fastlane_validation_errors(jsonld: dict, source_url: str) -> None:
    """Surface the ACTUAL pydantic field errors when the JSON-LD fast lane
    misses — we're still discovering RecipeModel quirks, and each one silently
    costs the ~17s LLM fallback. `jsonld_to_recipe` builds the model and swallows
    the ValidationError, so we replay the same construction here (flatten +
    sanitize + model_validate) purely to capture the errors as ONE greppable
    line. Best-effort: a diagnostic must NEVER break extraction.

    Greppable marker: `PYDANTIC FAST-LANE MISS`.
    """
    try:
        from pydantic import ValidationError
        from recipe_model import RecipeModel
        from sanitize_recipe_data import sanitize_recipe_data
        from extract.jsonld_to_recipe import _flatten_instructions
    except Exception:
        return  # deps unavailable — say nothing rather than break

    # The ValidationError can originate in EITHER sanitize_recipe_data (it
    # validates) OR model_validate — one try so we catch it wherever it fires.
    try:
        payload = dict(jsonld)
        payload["recipeInstructions"] = _flatten_instructions(
            payload.get("recipeInstructions")
        )
        sanitized = sanitize_recipe_data(payload)
        RecipeModel.model_validate(sanitized)
        # No pydantic error reproduced -> the miss was structural (thin data,
        # missing name/ingredients/instructions), not a model quirk.
        print(f"[enrich] FAST-LANE MISS (non-pydantic; thin/ineligible JSON-LD) "
              f"-> ~17s LLM. url={source_url!r}")
    except ValidationError as ve:
        fields = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()))}={e.get('type')}"
            for e in ve.errors()
        )
        print(f"[enrich] PYDANTIC FAST-LANE MISS -> ~17s LLM. "
              f"url={source_url!r} fields=[{fields}]")
    except Exception as diag_err:  # diagnostics never break the extract
        print(f"[enrich] (fast-lane diagnostic skipped: {type(diag_err).__name__})")


def _extract_json_block(text: str) -> Optional[dict]:
    """Pull a JSON object out of an LLM reply that may be fenced or prefaced."""
    if not text:
        return None
    t = text.strip()
    m = _re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, _re.DOTALL)
    if m:
        t = m.group(1)
    elif not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j != -1 and j > i:
            t = t[i:j + 1]
    try:
        obj = _json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _translate_jsonld_for_fastlane(jsonld: dict, src_lang: str):
    """Translate a non-English JSON-LD Recipe's STRING VALUES to English while
    preserving structure, so the fast lane can run on it — instead of discarding
    the JSON-LD (the old non-English behavior) and paying for the slow markdown
    LLM. One Haiku call via the recipe-aware translator (its prompt rule #7
    preserves JSON shape + leaves URLs/@type/ISO durations untouched).

    Returns (translated_jsonld, translation_meta) or (None, None) on failure —
    caller then falls back to translating the markdown for the LLM path.
    """
    try:
        from intake.translate import translate_markdown, language_name
    except Exception:
        return None, None
    try:
        original_title = (jsonld.get("name") or "").strip() or None
        block = "```json\n" + _json.dumps(jsonld, ensure_ascii=False, indent=2) + "\n```"
        tr = translate_markdown(block, src_lang)
        translated = _extract_json_block(tr.translated_markdown or "")
        # Sanity: must still look like a recipe (name + ingredients), else the
        # translation mangled it — fall back to markdown.
        if not isinstance(translated, dict) or not (translated.get("name") or "").strip():
            return None, None
        if not (translated.get("recipeIngredient")):
            return None, None
        meta = {
            "originalLanguage": src_lang,
            "originalLanguageName": language_name(src_lang),
            "translated": True,
            "translatedAt": _now_iso(),
            "originalTitle": original_title,
        }
        return translated, meta
    except Exception as e:
        print(f"[enrich] JSON-LD translation failed ({e}); will try markdown path")
        return None, None


def _translate_markdown_for_llm(markdown: str, src_lang: str, meta):
    """Fallback: translate the markdown to English for the LLM extract path
    (when there's no usable JSON-LD). Reuses the existing extraction-stage
    translator + plausibility check. Returns (markdown, meta) — original markdown
    on suspect/failed translation (so we never feed garbage downstream)."""
    try:
        from intake.translate import translate_extraction_markdown, language_name
    except Exception:
        return markdown, meta
    try:
        xr = translate_extraction_markdown(markdown, src_lang)
        if not xr.plausibility_ok:
            print(f"[enrich] markdown translation suspect ({xr.plausibility_reason}); "
                  f"using original")
            return markdown, meta
        m = dict(meta or {})
        m.update({
            "originalLanguage": xr.source_language,
            "originalLanguageName": xr.source_language_name,
            "translated": True,
            "translatedAt": _now_iso(),
            "originalTitle": (m.get("originalTitle") or xr.original_title or ""),
        })
        return xr.translated_markdown, m
    except Exception as e:
        print(f"[enrich] markdown translation failed ({e}); using original")
        return markdown, meta


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

    # --- 2/3. Translate non-English content to English BEFORE extract. PREFER
    #          translating the JSON-LD (structured) so the fast lane can run on
    #          it; only fall back to translating the markdown for the LLM path.
    #          English pages: no-op. This is the "use the JSON-LD, don't discard
    #          it" change for non-English sources — jsonld-direct (~1s) instead
    #          of markdown-llm (~17s), original preserved for provenance. ---
    eff_jsonld = req.jsonld
    eff_markdown = req.markdown
    translation_meta = None
    src_lang = (req.page_language or "en").strip().lower()[:2]
    tgt_lang = (req.target_language or "en").strip().lower()[:2]
    needs_translation = bool(src_lang) and src_lang != tgt_lang
    if needs_translation and tgt_lang != "en":
        # The recipe-aware translator targets English today, so we can't yet
        # land a non-English target (e.g. en->el for a Greek instance). Keep the
        # source untranslated rather than mistranslate. NB: the common localized
        # case (Greek instance + Greek recipe) never gets here — source==target.
        print(f"[enrich] target_language={tgt_lang!r} not yet a supported "
              f"translation TARGET; keeping source ({src_lang!r}) untranslated")
        needs_translation = False
    if needs_translation:  # implies tgt_lang == 'en' and src_lang != 'en'
        try:
            if eff_jsonld:
                t_jsonld, translation_meta = _translate_jsonld_for_fastlane(
                    eff_jsonld, src_lang)
                if t_jsonld is not None:
                    eff_jsonld = t_jsonld
                    print(f"[enrich] translated JSON-LD {src_lang!r}->en "
                          f"-> fast lane (kept structured data, skipped ~17s LLM)")
                else:
                    eff_jsonld = None  # jsonld translation failed -> markdown path
            if not eff_jsonld and eff_markdown:
                eff_markdown, translation_meta = _translate_markdown_for_llm(
                    eff_markdown, src_lang, translation_meta)
        except Exception as e:
            print(f"[enrich] translation step failed ({e}); proceeding untranslated")

    # --- 1. Extract: JSON-LD fast lane, else the markdown LLM call. ---
    recipe: Optional[dict] = None
    extract_path = ""
    if eff_jsonld:
        from extract.jsonld_to_recipe import jsonld_to_recipe
        fixed = _coerce_jsonld_for_fastlane(eff_jsonld)
        try:
            recipe = jsonld_to_recipe(
                fixed, source_url=req.source_url, title=req.title,
                timings=timings,
            )
            if recipe is None and fixed.get("video") is not None:
                # A peripheral video field can still reject the recipe; drop it
                # and retry once before paying ~17s for the LLM fallback.
                no_video = dict(fixed)
                no_video.pop("video", None)
                recipe = jsonld_to_recipe(
                    no_video, source_url=req.source_url, title=req.title,
                    timings=timings,
                )
                if recipe is not None:
                    print("[enrich] fast lane RECOVERED by dropping the video field "
                          "(peripheral field was failing validation)")
            if recipe is not None:
                extract_path = "jsonld-direct"
            else:
                # We HAD structured data but couldn't use it — a silent ~17s tax.
                # Surface the actual field-level pydantic errors so the quirk is
                # findable and fixable (we're still discovering them).
                _log_fastlane_validation_errors(fixed, req.source_url)
        except Exception as e:  # parity with the monolith's fall-back-to-LLM
            print(f"[enrich] WARNING: jsonld_to_recipe raised ({e}) -> markdown LLM "
                  f"fallback (~17s). url={req.source_url!r}")
            recipe = None

    if recipe is None:
        from extract.markdown_to_recipe import markdown_to_recipe
        recipe = markdown_to_recipe(
            eff_markdown, source_name="", source_url=req.source_url,
            title=req.title, timings=timings, prompts=prompts,
            usage_log=usage_log,
        )
        extract_path = "markdown-llm"

    if recipe is None:
        raise EnrichmentError("extraction produced no usable recipe")

    # Translation provenance -> _source (so the UI can show "Translated from X").
    if translation_meta:
        src = recipe.get("_source") or {}
        src["originalLanguage"] = translation_meta.get("originalLanguage", "")
        src["translated"] = True
        src["translatedAt"] = translation_meta.get("translatedAt", "")
        if translation_meta.get("originalTitle"):
            src["originalTitle"] = translation_meta["originalTitle"]
        recipe["_source"] = src

    # Caller-supplied authority scores (from TBOTB) -> stamp onto _scoring so the
    # editorial scoreCommentary block can interpret them. The API never fetches
    # Moz itself (stateless / no DB / no scoring).
    if req.scoring:
        recipe["_scoring"] = {**(recipe.get("_scoring") or {}), **req.scoring}

    # --- Measurement conversion (deterministic engine + LLM fallback on misses).
    #     Writes recipe['_measurements'] (1:1 with recipeIngredient). Runs on the
    #     post-extract recipe (translated ingredients are already English here, so
    #     the parser sees parseable text). Best-effort: a failure leaves the
    #     recipe without the block rather than aborting the transform. ---
    if req.do_measurements:
        try:
            from .measurement import convert_recipe_measurements
            m_meta = convert_recipe_measurements(recipe, llm_key=req.llm_key)
            timings.update(m_meta.get("timings") or {})
            usage_log.extend(m_meta.get("usage") or [])
            timings["measure_counts"] = m_meta.get("counts") or {}
        except Exception as e:
            print(f"[enrich] measurement pass failed ({e}); skipping _measurements")

    # --- 4. Enrichment blocks — INDIVIDUALLY selectable via req.enrich (a set
    #        of block names off the live registry). identity/translate/embed
    #        remain deferred carves (DL-4). ---
    if req.enrich:
        e_meta = run_enrichment_blocks(recipe, req.enrich, llm_key=req.llm_key)
        timings.update(e_meta.get("timings") or {})
        usage_log.extend(e_meta.get("usage") or [])
        if e_meta.get("prompts"):
            prompts["enrich"] = e_meta["prompts"]

    # --- 7. Seal / shape by profile (full = identity). ---
    from .serialize import apply_profile
    recipe = apply_profile(recipe, req.profile)

    return EnrichmentResult(
        recipe=recipe,
        embedding=None,  # do_embed deferred (DL-4)
        meta={
            "timings": timings,
            "prompts": prompts,
            "usage": usage_log,
            "extract_path": extract_path,  # 'jsonld-direct' | 'markdown-llm'
        },
    )


def available_enrichment_blocks() -> tuple[str, ...]:
    """The enrichment block names available to request, read off the live
    ENRICHMENT_BLOCKS registry. Registry-driven so a new block (nutrition,
    dietary tags, …) becomes selectable with no change here — and the form/UI
    can enumerate the checkboxes from this. Falls back to the known three if the
    registry can't be imported."""
    try:
        from extract.enrich_recipe import ENRICHMENT_BLOCKS
        return tuple(b.name for b in ENRICHMENT_BLOCKS)
    except Exception:
        return ("provenance", "classification", "editorial")


def run_enrichment_blocks(
    recipe: dict,
    block_names=None,
    *,
    model: str = "claude-haiku-4-5",
    llm_key: Optional[str] = None,
) -> dict:
    """Run the SELECTED enrichment blocks on `recipe` in place, merging each
    block's output into recipe[block.name]. The whole point (per the user):
    every block is INDIVIDUALLY selectable, and "there will be more".

    block_names:
        None        -> all registry blocks (legacy enrich_recipe behavior)
        iterable    -> only those names (unknown names ignored with a warning)

    Registry-driven: blocks + their order come from ENRICHMENT_BLOCKS, so adding
    a block needs no change here. Each block is one parallel LLM call (the cost
    unit). Per-block failure is isolated (other blocks still land). Returns meta
    {timings, prompts, usage, blocks_run}. Best-effort: a block raising never
    aborts the others.

    Delegates to the existing block machinery (_run_block / _build_user_prompt)
    during build-up — the strangler keeps ONE implementation. BYOK (llm_key) is
    accepted for the eventual per-request key injection (DL-5); today _run_block
    uses the ambient env key.
    """
    from concurrent.futures import ThreadPoolExecutor
    from extract.enrich_recipe import (
        ENRICHMENT_BLOCKS, _build_user_prompt, _run_block,
    )
    from input.pipeline.token_journal import build_usage_entry

    timings: dict = {}
    usage: list = []

    if block_names is None:
        selected = list(ENRICHMENT_BLOCKS)
    else:
        wanted = set(block_names)
        known = {b.name for b in ENRICHMENT_BLOCKS}
        unknown = wanted - known
        if unknown:
            print(f"[enrich] ignoring unknown enrichment blocks: {sorted(unknown)} "
                  f"(known: {sorted(known)})")
        selected = [b for b in ENRICHMENT_BLOCKS if b.name in wanted]

    if not selected:
        return {"timings": timings, "prompts": {}, "usage": usage, "blocks_run": []}

    user_prompt = _build_user_prompt(recipe)
    results: dict = {}
    with ThreadPoolExecutor(max_workers=len(selected),
                            thread_name_prefix="enrich-api") as pool:
        futs = {pool.submit(_run_block, b, user_prompt, model): b for b in selected}
        for fut, b in futs.items():
            try:
                results[b.name] = (fut.result(), None)
            except Exception as e:  # isolate per-block failure
                results[b.name] = (None, e)

    for b in selected:  # registry order -> deterministic merge + usage order
        res, err = results.get(b.name, (None, None))
        if err is not None:
            print(f"[enrich] block {b.name!r} failed ({err}); leaving defaults")
            timings[f"enrich_{b.name}_ms"] = 0
            continue
        response, parsed, elapsed_ms = res
        timings[f"enrich_{b.name}_ms"] = elapsed_ms
        if response is not None:
            usage.append(build_usage_entry(b.operation, model, response))
        if isinstance(parsed, dict):
            recipe[b.name] = {**(recipe.get(b.name) or {}), **parsed}

    blocks_run = [b.name for b in selected]
    timings["enrich_blocks_ms"] = sum(
        v for k, v in timings.items() if k.startswith("enrich_") and k.endswith("_ms")
    )
    return {
        "timings": timings,
        "prompts": {"model": model, "blocks": blocks_run},
        "usage": usage,
        "blocks_run": blocks_run,
    }
