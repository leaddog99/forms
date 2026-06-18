"""voice_agent.py — a GENERIC grounded voice-agent engine (reusable core).

The reusable pattern behind the cooking voice loop, factored out so future
domain-specific voice apps share it. A "grounded voice agent" is:

  - a PERSONA with general expertise in a DOMAIN (Chef / cooking),
  - grounded in ONE SPECIFIC ENTITY's structured data (the recipe being cooked),
  - that either ANSWERS a question (spoken, STREAMED sentence-by-sentence so the
    client can start talking ~1s in instead of waiting for the whole reply), or
  - performs a domain NAVIGATION action (next/back/repeat… via a tool).

The domain specifics — system prompt (persona + expertise), the grounding
context string, and the navigation tool/actions — are supplied by the CALLER
(an adapter like `cook_ask.py`). Everything domain-agnostic lives here once:
Anthropic streaming, sentence chunking, tool/action detection, token journaling,
and SSE serialization. A second app (e.g. a person + their bio) writes its own
small adapter + endpoint and reuses this engine unchanged.

Event protocol (dicts yielded by `stream_grounded`):
    {"type": "sentence", "text": "<a complete sentence ready to speak>"}
    {"type": "action",   "name": "<tool name>", "input": {<tool args>}}   # terminal
    {"type": "done"}
The caller/adapter maps a generic `action` event to its domain's action shape.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterator, Optional

import anthropic

from input.pipeline.token_journal import build_usage_entry

_client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

# Flush a sentence when a terminator is followed by whitespace. The lookbehind
# keeps decimals ("165.5") and unit dots without a trailing space intact, so we
# don't split "3 min." mid-number — only at real sentence gaps.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def stream_grounded(
    *,
    system: str,
    context: str,
    question: str,
    model: str,
    max_tokens: int = 700,
    tools: Optional[list] = None,
    operation: str = "voice_ask",
    usage_log: Optional[list] = None,
) -> Iterator[dict]:
    """Stream a grounded answer (or a navigation action) as event dicts.

    `system` is the persona+domain prompt; `context` is the entity grounding (the
    question is appended after it); `tools` (optional) is the domain navigation
    tool(s). If the model calls a tool, a single terminal `action` event is
    emitted (no sentences); otherwise sentences flush at boundaries as they form.
    Usage is journaled onto `usage_log` from the final streamed message.
    Raises on a hard API failure (the endpoint maps that to a friendly error)."""
    question = (question or "").strip()
    if not question:
        yield {"type": "sentence", "text": "What would you like to know?"}
        yield {"type": "done"}
        return

    user = f"{context}\n\n{question}" if context else question
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if tools:
        kwargs["tools"] = tools

    buf = ""
    saw_tool = False
    emitted_text = False   # any answer text seen? if so, ANSWER WINS — ignore a trailing tool call
    final = None
    with _client.messages.stream(**kwargs) as stream:
        for event in stream:
            et = getattr(event, "type", None)
            if et == "content_block_start":
                cb = getattr(event, "content_block", None)
                if getattr(cb, "type", None) == "tool_use":
                    saw_tool = True  # navigation chosen — suppress text sentences
            elif et == "content_block_delta" and not saw_tool:
                d = getattr(event, "delta", None)
                if getattr(d, "type", None) == "text_delta":
                    txt = d.text or ""
                    if txt.strip():
                        emitted_text = True
                    buf += txt
                    while True:
                        parts = _SENTENCE_BOUNDARY.split(buf, 1)
                        if len(parts) < 2:
                            break
                        sentence = parts[0].strip()
                        buf = parts[1]
                        if sentence:
                            yield {"type": "sentence", "text": sentence}
        final = stream.get_final_message()

    # Streaming SSE journaling stays on the plain usage_log (a shared list), NOT the
    # llm.py contextvar gateway: Starlette drives the SSE generator across separate
    # threadpool contexts per iteration, so a contextvar set at the top is discarded
    # before the stream's flush — the entry would be lost. The list is immune (shared
    # by reference). Caller (endpoint) journals usage_log at stream end. See llm-gateway.md.
    if usage_log is not None and final is not None:
        try:
            usage_log.append(build_usage_entry(operation, model, final))
        except Exception:
            pass

    # Action ONLY when the model navigated WITHOUT answering. A tool call appended
    # after answer text is spurious (the model occasionally does both) — the spoken
    # answer wins, so we drop the stray action rather than navigate over the reply.
    if saw_tool and not emitted_text and final is not None:
        for b in final.content:
            if getattr(b, "type", None) == "tool_use":
                yield {"type": "action", "name": b.name, "input": (b.input or {})}
                yield {"type": "done"}
                return
        # tool flagged but no parseable block — fall through to any buffered text

    tail = (buf or "").strip()
    if tail:
        yield {"type": "sentence", "text": tail}
    yield {"type": "done"}


def grounded(
    *,
    system: str,
    context: str,
    question: str,
    model: str,
    max_tokens: int = 700,
    tools: Optional[list] = None,
    operation: str = "voice_ask",
    usage_log: Optional[list] = None,
) -> dict:
    """Non-streaming convenience over the same engine. Returns one of:
        {"kind": "action", "name": <tool>, "input": {...}}
        {"kind": "answer", "text": <full text>}
    Useful for typed (non-voice) callers that want the whole reply at once."""
    parts: list[str] = []
    for ev in stream_grounded(
        system=system, context=context, question=question, model=model,
        max_tokens=max_tokens, tools=tools, operation=operation, usage_log=usage_log,
    ):
        if ev["type"] == "action":
            return {"kind": "action", "name": ev["name"], "input": ev["input"]}
        if ev["type"] == "sentence":
            parts.append(ev["text"])
    text = " ".join(parts).strip()
    return {"kind": "answer", "text": text or "I'm not sure how to answer that — could you rephrase?"}


def sse(events: Iterator[dict]) -> Iterator[str]:
    """Serialize event dicts to Server-Sent-Events `data:` frames for a
    StreamingResponse(media_type='text/event-stream')."""
    for ev in events:
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
