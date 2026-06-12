"""cook_tts.py — Chef's voice (OpenAI gpt-4o-mini-tts).

The warm, steerable spoken voice for the hands-free cook view: speaks each step
and Chef's answers (voice-cook-spec.md, Tier 1 upgrade from the robotic
browser SpeechSynthesis, which stays in cook.html as an offline/failure
fallback). Backs POST /cook/speak — returns MP3 bytes the page plays via <audio>.

gpt-4o-mini-tts is the *steerable* TTS: the `instructions` field shapes the
persona (warm, calm, encouraging) without changing the words, so "Chef"
sounds like a friend in the kitchen rather than a screen reader. ~250ms to first
audio; ~$0.015 / 1K characters — pennies per step/answer. Reuses the project's
existing OpenAI client (same key as image_gen_openai.py).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Tuple

import openai

client = openai.OpenAI()  # reads OPENAI_API_KEY (same as image_gen_openai.py)

# Persistent TTS cache lives in the git-ignored media.db (same BLOB store as page
# screenshots). Caller may override; defaults to the project's media.db.
MEDIA_DB_PATH = os.environ.get("BCC_MEDIA_DB", "media.db")

TTS_MODEL = "gpt-4o-mini-tts"
# Warm, friendly voice. gpt-4o-mini-tts voices: alloy/ash/ballad/coral/echo/
# fable/nova/onyx/sage/shimmer/verse — "coral" reads warm + encouraging.
TTS_VOICE = "coral"
TTS_INSTRUCTIONS = (
    "You are Chef, a warm, calm, encouraging cooking companion speaking to "
    "someone who is cooking right now, hands busy. Speak naturally and unhurried, "
    "with genuine warmth — like a trusted friend standing beside them at the stove. "
    "Keep it clear and easy to follow; don't rush."
)
# Guardrail: a step or a Q&A answer is short. Anything longer is truncated rather
# than billed for a runaway synthesis.
TTS_MAX_CHARS = 1400


def synthesize(text: str) -> bytes:
    """Render `text` to MP3 bytes in Chef's voice. '' → empty bytes."""
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


# --------------------------------------------------------------------------- #
# Persistent, content-addressed TTS cache (media.db)
# --------------------------------------------------------------------------- #
# A step's spoken text is stable once a recipe is reworked, so synthesizing it
# every play (and every session/device/user) is wasted latency + spend. We store
# the MP3 keyed by a hash of (text + voice + model + instructions): a HIT serves
# instantly and free; changing the voice or re-wording a step changes the key, so
# it self-invalidates with no bookkeeping (same trick as the KB prompt-cache).
def tts_key(text: str) -> str:
    """Content hash of the spoken text + the exact voice parameters."""
    h = hashlib.sha256()
    h.update((text or "").strip().encode("utf-8"))
    h.update(b"\x00"); h.update(TTS_MODEL.encode("utf-8"))
    h.update(b"\x00"); h.update(TTS_VOICE.encode("utf-8"))
    h.update(b"\x00"); h.update(TTS_INSTRUCTIONS.encode("utf-8"))
    return h.hexdigest()[:24]


def ensure_tts_audio_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tts_audio (
               tts_id     TEXT PRIMARY KEY,   -- hash(text+voice+model+instructions)
               text       TEXT NOT NULL,      -- spoken text (debug/inspection)
               voice      TEXT NOT NULL,
               model      TEXT NOT NULL,
               mp3        BLOB NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    conn.commit()


def get_cached_tts(db_path: str, key: str) -> Optional[bytes]:
    """Stored MP3 for this key, or None on miss / error (never raises)."""
    try:
        with sqlite3.connect(db_path) as conn:
            ensure_tts_audio_table(conn)
            row = conn.execute(
                "SELECT mp3 FROM tts_audio WHERE tts_id = ?", (key,)).fetchone()
            return row[0] if row else None
    except Exception as e:  # noqa: BLE001 — cache must never break playback
        print(f"[tts-cache] read failed: {e}")
        return None


def store_tts(db_path: str, key: str, text: str, mp3: bytes) -> None:
    """Persist an MP3 BLOB. Best-effort; a failure never blocks the response."""
    if not mp3:
        return
    try:
        with sqlite3.connect(db_path) as conn:
            ensure_tts_audio_table(conn)
            conn.execute(
                """INSERT INTO tts_audio (tts_id, text, voice, model, mp3, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(tts_id) DO NOTHING""",
                (key, (text or "")[:2000], TTS_VOICE, TTS_MODEL, mp3,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[tts-cache] store failed: {e}")


def synthesize_cached(text: str, db_path: Optional[str] = None) -> Tuple[bytes, bool]:
    """Cache-first synthesis. Returns (mp3_bytes, cache_hit). A miss synthesizes
    once and stores. '' text → (b'', False)."""
    text = (text or "").strip()
    if not text:
        return b"", False
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS]
    db_path = db_path or MEDIA_DB_PATH
    key = tts_key(text)
    cached = get_cached_tts(db_path, key)
    if cached:
        return cached, True
    mp3 = synthesize(text)
    store_tts(db_path, key, text, mp3)
    return mp3, False
