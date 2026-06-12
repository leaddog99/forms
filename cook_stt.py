"""cook_stt.py — Claudette's ears (faster-whisper, base.en, CPU/int8).

Transcribes a short utterance captured in the cook view's hands-free loop and
returns text the browser routes (a command like "next"/"back", or a Claudette
question). Recognition runs server-side, so the mic audio stays on the host —
which for this app is the user's own machine (portable-package privacy stance,
recipe_anchor/voice-cook-spec.md "only a deliberate question leaves the device";
here not even that, the LLM call is separate).

faster-whisper = the CTranslate2 reimplementation of Whisper ("the faster
variant"): int8 on CPU turns a ~3s utterance around in ~1-2s with base.en — fast
enough for a live loop on a machine with no GPU. The model is lazy-loaded on the
first call and kept warm for the session (first load also downloads ~140MB).

Backs POST /cook/listen. The browser already does VAD/endpointing (Silero
vad-web) and sends one trimmed utterance as WAV, so server-side VAD is off.
"""
from __future__ import annotations

import io
import threading
from typing import Optional

MODEL_SIZE = "base.en"          # English-only: faster + sharper for kitchen use
_DEVICE = "cpu"
_COMPUTE = "int8"               # the CPU sweet spot for faster-whisper

_model = None                   # lazy singleton
_lock = threading.Lock()


def _get_model():
    """Load the WhisperModel once, thread-safely. First call downloads base.en."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                from faster_whisper import WhisperModel
                _model = WhisperModel(MODEL_SIZE, device=_DEVICE, compute_type=_COMPUTE)
    return _model


def warm() -> None:
    """Optional: pre-load the model (e.g. at server boot) so the first real
    utterance doesn't eat the load latency. Best-effort."""
    try:
        _get_model()
    except Exception as e:
        print(f"[cook_stt] warm failed: {e}")


def transcribe(audio_bytes: bytes) -> str:
    """Transcribe one utterance (WAV/webm/whatever PyAV can decode) → text.
    Returns '' on empty input. beam_size=1 (greedy) — commands and short
    questions don't need beam search, and it shaves latency."""
    if not audio_bytes:
        return ""
    model = _get_model()
    segments, _info = model.transcribe(
        io.BytesIO(audio_bytes),
        language="en",
        beam_size=1,
        vad_filter=False,            # browser already endpointed the utterance
        condition_on_previous_text=False,
        temperature=0.0,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()
