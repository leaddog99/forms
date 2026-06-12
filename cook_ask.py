"""cook_ask.py — "Claudette", the resident cooking assistant for the cook view.

Answers an open cooking question GROUNDED in the specific recipe the cook is
making (its `_cook` block — ingredients, mise, steps, put-asides) while still
drawing on the model's general cooking knowledge. Powers `POST /cook/ask`.

This is the Tier-3 keystone of the hands-free cook experience
(recipe_anchor/voice-cook-spec.md): today a typed question; tomorrow the voice
loop transcribes the cook's speech (STT), routes non-command utterances here,
and speaks the reply (TTS) — SAME entry point, no rework. The reply is written
to be HEARD: short, plain, conversational prose (no markdown/lists).

Plain conversational completion (no forced tool) — the value is a warm, correct,
concise answer about THIS dish. Sonnet tier: judgment-light Q&A, grounded by the
recipe context so "is it done?" / "can I swap panko?" answer about the real
recipe in front of the cook, not in the abstract. Token-journaled; never the
source of a 500 to the caller (the endpoint degrades to a friendly message).
"""
from __future__ import annotations

import json
import re
from typing import Optional

import anthropic

from input.pipeline.token_journal import build_usage_entry

_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

MODEL = "claude-sonnet-4-6"      # warm, accurate cooking Q&A grounded by recipe context
_MAX_TOKENS = 700
ASK_PROMPT_VERSION = "cook-ask-v1-2026-06-11"

CLAUDETTE_SYSTEM = (
    "You are Claudette, a warm, sharp cooking companion talking to someone who is "
    "cooking RIGHT NOW — hands busy, maybe messy. Your reply will be READ ALOUD, so:\n"
    "- Be brief and conversational: usually one to three sentences. No lists, no "
    "markdown, no headings, no emoji — just natural spoken prose.\n"
    "- Answer the actual question first and directly; add at most one short reason or tip.\n"
    "- You are grounded in THIS recipe (given below). When the question is about this "
    "dish — an amount, a substitution, timing, doneness, what's next, why a step matters "
    "— answer from the recipe's real details. When it's general cooking knowledge, draw "
    "on your full expertise.\n"
    "- Never invent specifics about this recipe that aren't in the context. If something "
    "genuinely isn't specified, say so briefly and give your best general guidance.\n"
    "- Be calm and encouraging. On anything touching food safety (doneness temperatures, "
    "raw meat/eggs/seafood, preserving), be accurate and do not hand-wave.\n"
    "- The cook can ask you anything at any moment; the app itself handles step "
    "navigation, so you don't need to recite the whole recipe."
)


# --------------------------------------------------------------------------- #
# Recipe grounding context — compact, readable, built from `_cook`
# --------------------------------------------------------------------------- #
def _amt(a) -> str:
    """Imperial face is the source of truth; fall back to metric/string."""
    if not a:
        return ""
    if isinstance(a, str):
        return a
    return a.get("imperial") or a.get("metric") or ""


def _expand_instruction(step: dict) -> str:
    """Expand the renderer tokens into plain text for grounding — {ingN} ->
    'amount label', {amt:IMP|MET} -> imperial face, {bundle:ID} stripped to a
    readable phrase isn't available here so left as the id's tail. Mirrors the
    cook.html expander (imperial), so Claudette reads exactly what the cook sees."""
    text = str(step.get("instruction") or "")
    ings = step.get("ingredients") or []

    def ing_sub(m):
        i = int(m.group(1)) - 1
        if 0 <= i < len(ings):
            si = ings[i]
            a = _amt(si.get("amount"))
            return (a + " " if a else "") + (si.get("label") or "")
        return m.group(0)

    text = re.sub(r"\{ing(\d+)\}", ing_sub, text)
    text = re.sub(r"\{amt:([^}]*)\}", lambda m: m.group(1).split("|")[0].strip(), text)
    text = re.sub(r"\{bundle:([^}]*)\}", lambda m: m.group(1).strip().replace("bndl_", "").replace("_", " "), text)
    return text.strip()


def build_context(recipe: dict, current_step: Optional[int]) -> str:
    """A compact, model-readable snapshot of the recipe being cooked. Prefers the
    `_cook` block (the reworked, step-anchored truth); falls back to the faithful
    schema fields when a recipe somehow lacks one."""
    ck = recipe.get("_cook") or {}
    ctx: dict = {
        "name": recipe.get("name"),
        "description": recipe.get("description"),
        "serves": ck.get("recipe_yield") or recipe.get("recipeYield"),
        "total_time_minutes": ck.get("total_time_minutes"),
        "headnote": ck.get("headnote"),
    }
    if ck:
        ctx["ingredients"] = [
            {
                "name": i.get("name"),
                "aisle": i.get("aisle"),
                "to_taste": i.get("to_taste") or None,
                "buy": i.get("shopping_hint") or None,
            }
            for i in (ck.get("ingredients") or [])
        ]
        ctx["mise_bundles"] = [
            {
                "label": b.get("label"),
                "members": [f"{_amt(m.get('amount'))} {m.get('label')}".strip() for m in (b.get("members") or [])],
                "why_combined": b.get("combine_note") or b.get("excluded_reason"),
            }
            for b in (ck.get("bundles") or [])
        ]
        ctx["put_asides"] = [
            {"label": r.get("label"), "from_step": r.get("created_step"),
             "used_step": r.get("consumed_step"), "note": r.get("note")}
            for r in (ck.get("reserved") or [])
        ]
        ctx["steps"] = [
            {
                "number": s.get("number"),
                "name": s.get("name"),
                "do": _expand_instruction(s),
                "minutes": s.get("duration_minutes"),
                "tips": [a.get("text") for a in (s.get("attachments") or [])],
            }
            for s in (ck.get("steps") or [])
        ]
        ctx["finish"] = ck.get("finish")
        ctx["cooks_note"] = ck.get("cooks_note")
        ctx["good_to_know"] = [a.get("text") for a in (ck.get("tips") or [])]
    else:
        # Fallback: faithful capture (recipe never reworked — still answerable).
        ctx["ingredients"] = recipe.get("recipeIngredient")
        instrs = recipe.get("recipeInstructions") or []
        ctx["steps"] = [
            (s.get("text") if isinstance(s, dict) else str(s)) for s in instrs
        ]

    if current_step is not None:
        ctx["cook_is_currently_on_step"] = current_step

    # Drop empty keys to keep the prompt tight.
    ctx = {k: v for k, v in ctx.items() if v not in (None, [], "", {})}
    return json.dumps(ctx, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #
def ask(recipe: dict, question: str, *, current_step: Optional[int] = None,
        usage_log: Optional[list] = None) -> str:
    """Answer `question` as Claudette, grounded in `recipe`. Returns plain text
    suitable for TTS. Appends a token-journal entry to `usage_log` if provided.
    Raises on a hard API failure — the caller (endpoint) maps that to a friendly
    503 rather than leaking a stack trace to the cook."""
    question = (question or "").strip()
    if not question:
        return "What would you like to know?"

    context = build_context(recipe, current_step)
    user = (
        "Here is the recipe I'm cooking right now, as structured context:\n\n"
        f"{context}\n\n"
        f"My question: {question}"
    )
    resp = _client.messages.create(
        model=MODEL,
        max_tokens=_MAX_TOKENS,
        system=CLAUDETTE_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    if usage_log is not None:
        try:
            usage_log.append(build_usage_entry("cook_ask", MODEL, resp))
        except Exception:
            pass
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return ("".join(parts)).strip() or "I'm not sure how to answer that — could you rephrase?"
