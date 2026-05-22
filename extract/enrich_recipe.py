"""Enrichment LLM calls — independent blocks fired in parallel.

Each `EnrichmentBlock` describes one focused LLM call: a tool schema, a
job-specific system prompt, and which key it merges into on the recipe
dict. The blocks in `ENRICHMENT_BLOCKS` fan out via a ThreadPoolExecutor
so wall time is roughly the slowest block, not the sum of all blocks.

Adding a new block later (e.g. nutritional commentary, dietary tags,
controlled-vocab ethnicity enum) is a one-place change: define another
EnrichmentBlock and append it to ENRICHMENT_BLOCKS. The orchestrator
discovers it automatically — it picks up the tool, threads the same
user_prompt through, journals usage under the block's `operation`
label, and merges the tool input into `recipe[block.name]`.

Failure isolation: a single block raising leaves OTHER blocks' results
in place. The old monolithic call had all-or-nothing semantics — a
truncated story would void provenance and editorial too.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

import anthropic


_anthropic_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env


# Shared preamble for every enrichment block. Per-block job instructions
# get appended below. Kept compact — each block already gets a focused
# input_schema that hard-constrains the output shape, so the prompt
# doesn't need to re-spell the JSON envelope.
_BASE_SYSTEM = """
You are a culinary historian AND an opinionated food editor. Given a
dish's name, ingredients, and any cuisine hints, you produce a focused
piece of structured output as directed by the YOUR TASK section below.

Make a best-effort inference from ANY signal: dish name, cooking
technique (e.g. "au gratin" → French, "tagine" → North African,
"carbonara" → Roman), key ingredients, naming convention. Leaving a
field empty signals "no signal at all" — reserve that for genuinely
unidentifiable dishes.

Hedge with "likely", "tradition holds", "commonly attributed to" when
inferring rather than quoting a fact. DO NOT invent specific chefs,
restaurants, dates, brand names, shop names, or URLs — omit rather
than guess.
""".strip()


_PROVENANCE_JOB = """
== YOUR TASK ==
Call submit_provenance with the dish's cultural/historical metadata.
Brief inference beats empty fields, but genuinely unknown should stay
empty (or null for firstDocumented). `sources` should stay an empty
list — the field exists for future curated citations, not for
fabricated ones.
""".strip()


_CLASSIFICATION_JOB = """
== YOUR TASK ==
Call submit_classification with classification fields PLUS a
multi-paragraph story about the dish.

confidence: integer 0-100. 70+ for unambiguous technique markers,
50-70 for technique + corroborating ingredients, 30-50 for a single
weak cue, <30 only for genuinely unidentifiable.

reasoning: one or two sentences explaining your classification call.

hierarchyPath: slash-separated taxonomy like 'side/gratin/vegetable',
'main/braise/stew'.

story: 150 to 300 words split into 3 to 5 short paragraphs, separated
by literal "\\n\\n" (two newline characters inside the JSON string).
Cover, in roughly this order:
  1. Origin & history — when/where the dish emerged, its lineage.
  2. Geography & culture — region, local ingredients/foodways that
     shaped it, why it belongs to that place.
  3. Traditional usage — meal type, season, occasion, who cooks it.
  4. Modern usage & spread — diaspora variations if any.
  5. Notable variations or widely-recognized renditions.

A 3-sentence single-paragraph story is WRONG. For genuinely obscure
dishes, write a shorter honest paragraph saying so rather than padding.

== EXAMPLE STORY (for length and tone calibration) ==
For "Asparagus au Gratin", a CORRECT story looks like:

>>>BEGIN EXAMPLE STORY>>>
Gratins are a defining technique of French home cooking, traceable to
the 18th-century kitchens of the Dauphiné region where cooks layered
sliced vegetables with cream and breadcrumbs to make humble produce
keep longer and taste richer. The word itself comes from the French
verb gratter — to scrape — referring to the browned crust that forms
on top.

Asparagus arrived in this tradition somewhat later, prized as a
seasonal luxury in the Loire Valley and around Paris, where white
asparagus in particular became a springtime ritual. The combination of
the vegetable's sweetness with a creamy béchamel and a crisp gruyère
topping is now classical, but it began as a way to stretch a fleeting
crop into something celebratory.

Traditionally the dish is served as a starter or vegetable course at
Sunday lunch in spring, often alongside roast lamb or veal. Outside
France it has become a standard of bistro menus and home dinner
parties, sometimes lightened with crème fraîche, sometimes enriched
with ham or shaved truffle for a holiday version.

Modern variants run the gamut from purist (asparagus, butter,
breadcrumbs) to elaborate (multiple cheeses, leeks, pancetta), and the
dish translates easily to other tender vegetables — fennel, leeks,
endive — using the same gratin grammar.
<<<END EXAMPLE STORY<<<

That story is roughly 230 words across four paragraphs. Match that
density and structure.
""".strip()


_EDITORIAL_JOB = """
== YOUR TASK ==
Call submit_editorial with three fields:

opinion: 2-3 short paragraphs (separate with \\n\\n), roughly 100-200
words. Editorial take on THIS specific recipe — technique choices,
ingredient ratios, what makes it work or wobble, who would love it.
Concrete and opinionated. Not 'this is a classic dish' filler —
comment on what the cook in front of this recipe is actually being
asked to do.

scoreCommentary: 1-2 short paragraphs interpreting the PA/DA/OU scores
in plain language. PA is the page's Moz authority (0-100); DA is the
domain's; OU = -3.0273 * DA^0.6034 + PA, positive when the page
outperforms its domain baseline, negative when it underperforms.
Translate the numbers into a reader-facing observation. If scores are
missing/zero (the recipe wasn't scored), say so and keep it short —
don't fabricate authority claims.

sourcingNotes: Markdown bullet list. Pick 2-5 ingredients where
quality dominates outcome (raw oils, fresh herbs, aged cheeses,
anchovies, vanilla, etc.). For each: '- **Ingredient name**: why
quality matters here, what to look for, descriptive sourcing guidance
(origin, style, hallmarks of the good stuff).' DO NOT invent brand
names, shop names, or URLs — those will be layered in later from a
curated affiliate database.
""".strip()


@dataclass(frozen=True)
class EnrichmentBlock:
    """One parallel-callable unit of enrichment.

    To add a new block: define another instance and append it to
    ENRICHMENT_BLOCKS. The orchestrator picks it up automatically —
    fires it alongside the others, journals usage under `operation`,
    and merges the tool's `input` dict into `recipe[name]`.
    """
    name: str               # 'provenance' — also the recipe-dict key the result merges into
    tool_name: str          # 'submit_provenance'
    tool_description: str
    input_schema: dict      # JSON Schema for the tool input
    job_prompt: str         # The "== YOUR TASK ==" section, appended to _BASE_SYSTEM
    operation: str          # journal label, e.g. 'enrich_provenance'
    max_tokens: int = 1024  # generation cap; classification (with story) needs more

    def tool(self) -> dict:
        return {
            "name": self.tool_name,
            "description": self.tool_description,
            "input_schema": self.input_schema,
        }

    def system_prompt(self) -> str:
        return f"{_BASE_SYSTEM}\n\n{self.job_prompt}"


PROVENANCE_BLOCK = EnrichmentBlock(
    name="provenance",
    tool_name="submit_provenance",
    tool_description="Submit the dish's cultural/historical provenance metadata.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": [
            "ethnicity", "originRegion", "firstDocumented",
            "traditionalContext", "notableVariations",
            "relatedDishes", "sources",
        ],
        "properties": {
            "ethnicity": {"type": "string"},
            "originRegion": {"type": "string"},
            "firstDocumented": {"type": ["string", "null"]},
            "traditionalContext": {"type": "string"},
            "notableVariations": {"type": "array", "items": {"type": "string"}},
            "relatedDishes": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
    },
    job_prompt=_PROVENANCE_JOB,
    operation="enrich_provenance",
    max_tokens=800,
)


CLASSIFICATION_BLOCK = EnrichmentBlock(
    name="classification",
    tool_name="submit_classification",
    tool_description="Submit classification fields plus a multi-paragraph dish story.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["confidence", "reasoning", "hierarchyPath", "story"],
        "properties": {
            "confidence": {"type": "integer"},
            "reasoning": {"type": "string"},
            "hierarchyPath": {"type": "string"},
            "story": {"type": "string"},
        },
    },
    job_prompt=_CLASSIFICATION_JOB,
    operation="enrich_classification",
    max_tokens=1600,  # story alone is ~400 tokens; headroom for reasoning + structural
)


EDITORIAL_BLOCK = EnrichmentBlock(
    name="editorial",
    tool_name="submit_editorial",
    tool_description="Submit editorial opinion, score commentary, and sourcing notes for this recipe.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["opinion", "scoreCommentary", "sourcingNotes"],
        "properties": {
            "opinion": {"type": "string"},
            "scoreCommentary": {"type": "string"},
            "sourcingNotes": {"type": "string"},
        },
    },
    job_prompt=_EDITORIAL_JOB,
    operation="enrich_editorial",
    max_tokens=1200,
)


# Ordered registry. Order drives:
#   1) the deterministic walk-and-merge after futures complete (so
#      usage_log entries land in a consistent order regardless of which
#      block finishes first), and
#   2) the worker pool size (one worker per block).
# Append new blocks to extend enrichment.
ENRICHMENT_BLOCKS: list[EnrichmentBlock] = [
    PROVENANCE_BLOCK,
    CLASSIFICATION_BLOCK,
    EDITORIAL_BLOCK,
]


# Concatenated system prompt for cache-key versioning. `save_recipe_api.py`
# hashes this string into `EXTRACT_PROMPT_VERSION`; any change to any
# block's prompt naturally invalidates the cache without needing the
# caller to know about the block split. Order matches ENRICHMENT_BLOCKS.
SYSTEM_PROMPT = "\n\n---\n\n".join(
    f"[{block.name}]\n{block.system_prompt()}" for block in ENRICHMENT_BLOCKS
)


def _build_user_prompt(recipe: dict) -> str:
    """Compact context for the model. Avoids dumping anything we don't need."""
    name = (recipe.get("name") or "").strip()
    description = (recipe.get("description") or "").strip()
    cuisine = (recipe.get("recipeCuisine") or "").strip()
    category = (recipe.get("recipeCategory") or "").strip()
    ingredients = recipe.get("recipeIngredient") or []
    # Cap at 12 to keep the prompt small; ingredients are all the signal the
    # model needs for cultural attribution.
    ing_sample = ingredients[:12]

    lines = [f"Dish name: {name}"]
    if description:
        lines.append(f"Description: {description}")
    if cuisine:
        lines.append(f"Cuisine label from source: {cuisine}")
    if category:
        lines.append(f"Category label from source: {category}")
    if ing_sample:
        lines.append("Ingredients:")
        for ing in ing_sample:
            lines.append(f"  - {ing}")
        if len(ingredients) > len(ing_sample):
            lines.append(f"  (+ {len(ingredients) - len(ing_sample)} more)")

    # Moz / page-authority scores for the editorial.scoreCommentary field.
    # Only include scores with a real (non-zero) value so the model doesn't
    # fabricate commentary on a missing measurement. Always included even
    # though only the editorial block uses them — keeps user_prompt
    # identical across blocks (one less thing to vary).
    scoring = recipe.get("_scoring") or recipe.get("scoring") or {}
    pa = scoring.get("pageAuthority")
    da = scoring.get("domainAuthority")
    ou = scoring.get("ouScore")
    root_domain = scoring.get("rootDomain") or ""
    score_lines = []
    if pa is not None and float(pa) > 0:
        score_lines.append(f"  PA (page authority, 0-100): {float(pa):.1f}")
    if da is not None and float(da) > 0:
        score_lines.append(f"  DA (domain authority, 0-100): {float(da):.1f}")
    if ou is not None:
        score_lines.append(f"  OU (page-vs-domain over/under-performance, +/-): {float(ou):.1f}")
    if root_domain:
        score_lines.append(f"  Root domain: {root_domain}")
    if score_lines:
        lines.append("Page authority scores (for editorial.scoreCommentary):")
        lines.extend(score_lines)
    else:
        lines.append(
            "Page authority scores: NOT AVAILABLE — keep editorial.scoreCommentary brief and "
            "do not fabricate authority claims."
        )

    return "\n".join(lines)


def _run_block(
    block: EnrichmentBlock,
    user_prompt: str,
    model: str,
) -> tuple[Any, Optional[dict], int]:
    """Execute a single enrichment block via Anthropic streamed messages.

    Returns (response, parsed_input_dict_or_None, elapsed_ms). Raises on
    network/SDK errors — caller catches and isolates per-block failure.
    """
    t_start = time.perf_counter()
    with _anthropic_client.messages.stream(
        model=model,
        max_tokens=block.max_tokens,
        temperature=0.4,
        system=block.system_prompt(),
        messages=[{"role": "user", "content": user_prompt}],
        tools=[block.tool()],
        tool_choice={"type": "tool", "name": block.tool_name},
    ) as stream:
        response = stream.get_final_message()
    elapsed_ms = int((time.perf_counter() - t_start) * 1000)
    parsed = next(
        (b.input for b in response.content
         if b.type == "tool_use" and b.name == block.tool_name),
        None,
    )
    return response, (parsed if isinstance(parsed, dict) else None), elapsed_ms


def enrich_recipe(
    recipe: dict,
    *,
    model: str = "claude-haiku-4-5",
    timings: Optional[dict] = None,
    prompts: Optional[dict] = None,
    usage_log: Optional[list] = None,
) -> dict:
    """Mutate `recipe` in place with provenance + classification + editorial.

    Fires each block in ENRICHMENT_BLOCKS in parallel. Per-block failures
    (LLM error, missing tool_use block, schema mismatch) leave that block's
    existing recipe values untouched but DO NOT block other blocks from
    landing. Wall time is bounded by the slowest block, not the sum.

    Timings populated:
      enrich_prep_ms              — user prompt build time
      enrich_llm_ms               — total wall time across all blocks (max of per-block)
      enrich_<block>_ms           — per-block wall time, one row per block

    Prompts populated (preserves the original top-level shape so the
    existing trace panel still renders; adds `blocks` for per-block detail):
      model                       — same string for every block today
      system_prompt, user_prompt  — copied from the classification block
                                    (the block carrying the user-visible story)
      blocks[<name>]              — { model, system_prompt, user_prompt, operation }

    Usage log: one entry per block, appended in ENRICHMENT_BLOCKS order
    regardless of completion order.
    """
    from input.pipeline.token_journal import build_usage_entry

    t0 = time.perf_counter()
    user_prompt = _build_user_prompt(recipe)

    if prompts is not None:
        # Per-block detail; top-level keys filled below from the
        # classification block so the existing trace panel keeps working.
        blocks_trace: dict = {}
        for block in ENRICHMENT_BLOCKS:
            blocks_trace[block.name] = {
                "model": model,
                "system_prompt": block.system_prompt(),
                "user_prompt": user_prompt,
                "operation": block.operation,
            }
        prompts["model"] = model
        prompts["blocks"] = blocks_trace
        # Pick the classification block for the top-level fields — it's
        # the one that produces the user-visible story. Falls back to
        # the first block if classification is somehow missing.
        primary = next(
            (b for b in ENRICHMENT_BLOCKS if b.name == "classification"),
            ENRICHMENT_BLOCKS[0] if ENRICHMENT_BLOCKS else None,
        )
        if primary is not None:
            prompts["system_prompt"] = primary.system_prompt()
            prompts["user_prompt"] = user_prompt

    t_prep = time.perf_counter()
    if timings is not None:
        timings["enrich_prep_ms"] = int((t_prep - t0) * 1000)

    # Fan out. Each block runs in its own thread; the synchronous
    # Anthropic SDK is thread-safe (httpx underneath). Worker count
    # scales with the registry, so adding a 4th block doesn't require
    # touching this code.
    results: dict = {}  # block.name -> (response_or_None, parsed_or_None, elapsed_ms, error_or_None)
    if ENRICHMENT_BLOCKS:
        with ThreadPoolExecutor(
            max_workers=len(ENRICHMENT_BLOCKS),
            thread_name_prefix="enrich",
        ) as pool:
            future_to_block = {
                pool.submit(_run_block, block, user_prompt, model): block
                for block in ENRICHMENT_BLOCKS
            }
            for future, block in future_to_block.items():
                try:
                    response, parsed, elapsed_ms = future.result()
                    results[block.name] = (response, parsed, elapsed_ms, None)
                except Exception as e:
                    elapsed_ms = int((time.perf_counter() - t_prep) * 1000)
                    results[block.name] = (None, None, elapsed_ms, e)

    if timings is not None:
        timings["enrich_llm_ms"] = int((time.perf_counter() - t_prep) * 1000)

    # Walk blocks in registry order — deterministic usage_log + merge order.
    for block in ENRICHMENT_BLOCKS:
        response, parsed, elapsed_ms, err = results.get(
            block.name, (None, None, 0, None)
        )

        if timings is not None:
            timings[f"enrich_{block.name}_ms"] = elapsed_ms

        if err is not None:
            print(f"     ENRICH[{block.name}]: failed ({err}); leaving defaults")
            continue

        if usage_log is not None and response is not None:
            usage_log.append(build_usage_entry(block.operation, model, response))

        if not isinstance(parsed, dict):
            # tool_choice forced the tool, so missing it means an API
            # anomaly (e.g. max_tokens hit before the tool_use block
            # completed). Leave existing defaults intact.
            print(f"     ENRICH[{block.name}]: no {block.tool_name} tool_use in response; "
                  f"leaving defaults (stop_reason={getattr(response, 'stop_reason', None)})")
            continue

        # Merge, don't replace. The LLM doesn't populate every sub-field
        # of `classification` — `chapter` is set separately by
        # `_attach_chapter` (the keyword/LLM chapter classifier). A
        # wholesale `recipe[block.name] = parsed` wipes that. Same merge
        # discipline applies to provenance/editorial so any other code
        # path that stamps fields there survives.
        existing = recipe.get(block.name) or {}
        merged = dict(existing)
        merged.update(parsed)
        recipe[block.name] = merged

    return recipe


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python -m extract.enrich_recipe <recipe.json>")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        recipe = json.load(f)
    timings: dict = {}
    enriched = enrich_recipe(recipe, timings=timings)
    print("timings:", timings)
    print(json.dumps({"provenance": enriched.get("provenance"),
                      "classification": enriched.get("classification"),
                      "editorial": enriched.get("editorial")}, indent=2))
