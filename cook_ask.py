"""cook_ask.py — "Chef", the resident cooking companion for the cook view.

Answers the cook's question ("Hey Chef …") GROUNDED in the specific recipe being
made (its `_cook` block — ingredients, mise, steps, put-asides), while drawing on
general cooking knowledge when the question is about technique rather than this
dish. It is a COOKING companion, NOT a general-purpose assistant: off-topic, non-
cooking questions get a brief, friendly redirect back to the kitchen. Powers
`POST /cook/ask`.

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

import llm  # central LLM gateway — auto-journals usage to bcc_token_journal
_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

MODEL = "claude-sonnet-4-6"      # warm, accurate cooking Q&A grounded by recipe context
_MAX_TOKENS = 700
ASK_PROMPT_VERSION = "cook-ask-v1-2026-06-11"

# The voice loop hands wake-gated utterances here. Most are questions, but some
# are navigation phrased conversationally ("okay, I'm done — let's move on",
# "take me back a step"). Rather than a brittle keyword regex OR a second LLM
# round-trip, we give Chef ONE optional tool: if the cook clearly wants to move
# through the recipe, she calls `navigate` instead of answering — same single
# call, no added latency for real questions. Deterministic keyword commands
# ("next", "back") never reach here; the client runs those instantly client-side.
_NAV_ACTIONS = ("next", "back", "repeat", "restart", "pause", "resume", "stop", "goto")
_NAVIGATE_TOOL = {
    "name": "navigate",
    "description": (
        "Move through the recipe on the cook's behalf. Call this ONLY when the cook "
        "clearly wants to change position or playback — advance, go back, start over, "
        "pause/resume the reading, stop the session, or jump to a specific step number. "
        "'repeat' means RE-READ THE CURRENT STEP'S INSTRUCTION aloud — NOTHING else. "
        "Do NOT call this for requests to read or hear a TIP, a hint, an ingredient, an "
        "amount, or any explanation: 'read the tip', \"what's the tip\", 'is there a "
        "tip', 'read the ingredients', and every how/why/what/can-I/is-it-done/how-long/"
        "substitution question are ANSWERED IN WORDS, never navigation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_NAV_ACTIONS),
                       "description": "The navigation/playback action the cook intends."},
            "step": {"type": "integer",
                     "description": "1-based step number, only when action is 'goto'."},
        },
        "required": ["action"],
    },
}

CHEF_SYSTEM = (
    "You are Chef, a warm, sharp cooking companion talking to someone who is "
    "cooking RIGHT NOW — hands busy, maybe messy. Your reply will be READ ALOUD, so:\n"
    "- Be brief and conversational: usually one to three sentences. No lists, no "
    "markdown, no headings, no emoji — just natural spoken prose.\n"
    "- Answer the actual question first and directly; add at most one short reason or tip.\n"
    "- You are grounded in THIS recipe (given below). When the question is about this "
    "dish — an amount, a substitution, timing, doneness, what's next, why a step matters "
    "— answer from the recipe's real details. General COOKING questions (technique, "
    "ingredients, equipment, why something works) are fair game — draw on your full "
    "culinary expertise.\n"
    "- You are a COOKING companion, NOT a general-purpose assistant. If asked something "
    "unrelated to cooking, food, or this recipe, warmly redirect in one short sentence "
    "(e.g. 'Let's keep our heads in the kitchen — what can I help you cook?') and don't "
    "answer the off-topic question.\n"
    "- Never invent specifics about this recipe that aren't in the context. If something "
    "genuinely isn't specified, say so briefly and give your best general guidance.\n"
    "- Be calm and encouraging. On anything touching food safety (doneness temperatures, "
    "raw meat/eggs/seafood, preserving), be accurate and do not hand-wave.\n"
    "- The cook can ask you anything at any moment; the app itself handles step "
    "navigation, so you don't need to recite the whole recipe.\n"
    "- If the cook ONLY greets you, says your name, or says something with no clear "
    "question or request (e.g. just 'hey Chef'), do NOT describe the current step or "
    "what to do next — simply reply with a short 'I'm here — what can I do for you?' "
    "and wait. Only describe a step when they actually ask about it."
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
    cook.html expander (imperial), so Chef reads exactly what the cook sees."""
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
        usage_log: Optional[list] = None, operation: str = "cook_ask") -> str:
    """Answer `question` as Chef, grounded in `recipe`. Returns plain text
    suitable for TTS. `operation` is the billing/journal label — the cook view
    uses the default "cook_ask"; the Chef's-Notes chat passes "notes_chat" so its
    spend is legible per-feature in bcc_token_journal. Appends a token-journal
    entry to `usage_log` if provided. Raises on a hard API failure — the caller
    (endpoint) maps that to a friendly 503 rather than leaking a stack trace."""
    question = (question or "").strip()
    if not question:
        return "What would you like to know?"

    context = build_context(recipe, current_step)
    user = (
        "Here is the recipe I'm cooking right now, as structured context:\n\n"
        f"{context}\n\n"
        f"My question: {question}"
    )
    resp = llm.create(
        operation=operation, model=MODEL,
        max_tokens=_MAX_TOKENS,
        system=CHEF_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    # usage auto-journaled by the gateway (operation="cook_ask").
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return ("".join(parts)).strip() or "I'm not sure how to answer that — could you rephrase?"


def ask_or_act(recipe: dict, question: str, *, current_step: Optional[int] = None,
               usage_log: Optional[list] = None) -> dict:
    """Voice-loop entry: ONE Sonnet call that either ANSWERS the cook's question
    or, when the utterance is clearly navigation phrased conversationally
    ("okay I'm done, move on" / "take me back one"), returns a structured
    `navigate` action instead. Deterministic keyword commands ("next", "back")
    never reach here — the client runs those instantly. Returns one of:
      {"kind": "action", "action": <enum>, "step": <int|None>}
      {"kind": "answer", "text": <str>}
    Raises on a hard API failure (caller maps to a friendly 503)."""
    question = (question or "").strip()
    if not question:
        return {"kind": "answer", "text": "What would you like to know?"}

    context = build_context(recipe, current_step)
    user = (
        "Here is the recipe I'm cooking right now, as structured context:\n\n"
        f"{context}\n\n"
        f"What I just said: {question}"
    )
    resp = llm.create(
        operation="cook_ask", model=MODEL,
        max_tokens=_MAX_TOKENS,
        system=CHEF_SYSTEM,
        tools=[_NAVIGATE_TOOL],
        messages=[{"role": "user", "content": user}],
    )
    # usage auto-journaled by the gateway (operation="cook_ask").

    for b in resp.content:
        if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == "navigate":
            inp = b.input or {}
            action = inp.get("action")
            if action in _NAV_ACTIONS:
                out = {"kind": "action", "action": action}
                if action == "goto":
                    try:
                        out["step"] = int(inp.get("step"))
                    except (TypeError, ValueError):
                        # 'goto' with no usable number — degrade to an answer.
                        return {"kind": "answer",
                                "text": "Which step would you like to jump to?"}
                return out
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    text = ("".join(parts)).strip() or "I'm not sure how to answer that — could you rephrase?"
    return {"kind": "answer", "text": text}


def _grounding_context(recipe: dict, current_step: Optional[int]) -> str:
    """The recipe-domain grounding string handed to the generic voice engine."""
    return (
        "Here is the recipe I'm cooking right now, as structured context:\n\n"
        + build_context(recipe, current_step)
        + "\n\nThe cook just said:"
    )


def ask_or_act_stream(recipe: dict, question: str, *, current_step: Optional[int] = None,
                      usage_log: Optional[list] = None):
    """STREAMING voice variant — delegates the LLM/streaming/tool/journaling
    mechanics to the generic `voice_agent` engine, supplying only the recipe
    domain specifics (Chef persona, recipe grounding, step-navigation tool). A
    GENERATOR yielding recipe-flavored events so the client can speak the first
    sentence while Chef composes the rest:
        {"type": "sentence", "text": ...}
        {"type": "action", "action": <nav>, "step": <int|None>}   (terminal)
        {"type": "done"}
    This is the recipe ADAPTER over voice_agent.stream_grounded; a future
    person/product voice app writes its own adapter the same way."""
    import voice_agent
    ctx = _grounding_context(recipe, current_step)
    for ev in voice_agent.stream_grounded(
        system=CHEF_SYSTEM, context=ctx, question=question, model=MODEL,
        max_tokens=_MAX_TOKENS, tools=[_NAVIGATE_TOOL], operation="cook_ask",
        usage_log=usage_log,
    ):
        if ev["type"] == "action" and ev.get("name") == "navigate":
            inp = ev.get("input") or {}
            action = inp.get("action")
            if action in _NAV_ACTIONS:
                step = None
                if action == "goto":
                    try:
                        step = int(inp.get("step"))
                    except (TypeError, ValueError):
                        yield {"type": "sentence", "text": "Which step would you like to jump to?"}
                        yield {"type": "done"}
                        return
                yield {"type": "action", "action": action, "step": step}
                return
            # unrecognized action — ignore; let the stream finish normally
        else:
            yield ev
