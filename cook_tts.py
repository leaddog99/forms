"""cook_tts.py — Claudette's voice (OpenAI gpt-4o-mini-tts).

The warm, steerable spoken voice for the hands-free cook view: speaks each step
and Claudette's answers (voice-cook-spec.md, Tier 1 upgrade from the robotic
browser SpeechSynthesis, which stays in cook.html as an offline/failure
fallback). Backs POST /cook/speak — returns MP3 bytes the page plays via <audio>.

gpt-4o-mini-tts is the *steerable* TTS: the `instructions` field shapes the
persona (warm, calm, encouraging) without changing the words, so "Claudette"
sounds like a friend in the kitchen rather than a screen reader. ~250ms to first
audio; ~$0.015 / 1K characters — pennies per step/answer. Reuses the project's
existing OpenAI client (same key as image_gen_openai.py).
"""
from __future__ import annotations

import openai

client = openai.OpenAI()  # reads OPENAI_API_KEY (same as image_gen_openai.py)

TTS_MODEL = "gpt-4o-mini-tts"
# Warm, friendly voice. gpt-4o-mini-tts voices: alloy/ash/ballad/coral/echo/
# fable/nova/onyx/sage/shimmer/verse — "coral" reads warm + encouraging.
TTS_VOICE = "coral"
TTS_INSTRUCTIONS = (
    "You are Claudette, a warm, calm, encouraging cooking companion speaking to "
    "someone who is cooking right now, hands busy. Speak naturally and unhurried, "
    "with genuine warmth — like a trusted friend standing beside them at the stove. "
    "Keep it clear and easy to follow; don't rush."
)
# Guardrail: a step or a Q&A answer is short. Anything longer is truncated rather
# than billed for a runaway synthesis.
TTS_MAX_CHARS = 1400


def synthesize(text: str) -> bytes:
    """Render `text` to MP3 bytes in Claudette's voice. '' → empty bytes."""
    text = (text or "").strip()
    if not text:
        return b""
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS]
    # Streaming response avoids the direct-.create deprecation path and reads the
    # whole clip into memory (clips are a few seconds — small).
    with client.audio.speech.with_streaming_response.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        instructions=TTS_INSTRUCTIONS,
        response_format="mp3",
    ) as response:
        return response.read()
