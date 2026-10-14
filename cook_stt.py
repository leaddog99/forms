"""cook_stt.py — Chef's ears (faster-whisper, base.en, CPU/int8).

Transcribes a short utterance captured in the cook view's hands-free loop and
returns text the browser routes (a command like "next"/"back", or a Chef
question). Recognition runs server-side, so the mic audio stays on the host —
which for this app is the user's own machine (portable-package privacy stance,
recipe_anchor/voice-cook-spec.md "only a deliberate question leaves the device";
here not even that, the LLM call is separate).

faster-whisper = the CTranslate2 reimplementation of Whisper ("the faster
variant"): int8 on CPU turns a ~3s utterance around in ~1-2s with base.en — fast
enough for a live loop on a machine with no GPU. The model is lazy-loaded on the
first call and kept warm for the session (first load also downloads ~140MB).

Backs POST /cook/listen. The browser already does VAD/endpointing and sends one
trimmed utterance as WAV, so server-side VAD is off.

NOISE ROBUSTNESS (2026-07-10): base.en HALLUCINATES on non-speech — the
2026-06-18 noisy session was 83/93 utterances ignored, dominated by phantom
"Okay. Okay. Okay." / "Bye." / repetitive filler that the VAD fed in from
background noise. We now reject those with the standard Whisper heuristics
(no_speech_prob / avg_logprob / compression_ratio) BEFORE the text reaches the
client, lightly bias decoding toward the kitchen command set, and log every
decision to logs/cook_stt.log so tuning is data-driven, not guessed.
"""
from __future__ import annotations

import io
import os
import re
import threading
from datetime import datetime

MODEL_SIZE = "base.en"          # English-only: faster + sharper for kitchen use
_DEVICE = "cpu"
_COMPUTE = "int8"               # the CPU sweet spot for faster-whisper

# Bias decoding toward the WAKE WORD + kitchen command vocabulary — improves recall
# of a quietly/quickly spoken command. "hey" leads the list on purpose: base.en kept
# decoding the soft /h/ onset of "hey chef" as pay/lay/today/it (voice log 2026-07-10),
# so biasing toward "hey" pulls the wake word back. Double-edged: biasing can also turn
# a noise clip INTO a command (a false advance), so keep the confidence gates below
# strict, and WATCH logs/cook_stt.log for it. Set to "" to disable the bias.
_HOTWORDS = "hey chef next back previous repeat again step stop start begin pause resume tip done"

# Hallucination gates (the canonical Whisper trio). A segment is dropped if ANY
# trips. Tuned to the 2026-06-18 failure shapes; revisit against cook_stt.log.
_NO_SPEECH_MAX = 0.6       # no_speech_prob above this  -> not real speech
_AVG_LOGPROB_MIN = -1.1    # avg_logprob below this     -> low-confidence garbage
_COMPRESSION_MAX = 2.4     # compression_ratio above this -> repetition hallucination

# Known Whisper phantoms on silence/noise that SLIP the statistical gates with
# deceptively decent confidence ('you' verified nsp=0.33 on pure silence, 'Bye.'
# and 'Boom.' in the 2026-06-18 log). NONE is a valid kitchen command, so dropping
# an utterance that is ENTIRELY one of these is safe — and it removes the biggest
# false-advance risk now that decoding is biased toward the command set.
# Command words/phrases that PASS the statistical gates when the whole utterance IS
# one of them (2026-10-13: the cook said "stop" four times, base.en heard 'stop' each
# time at nsp≈0.45 / alp≈-1.3, and the gates dropped it as noise — nothing ever reached
# the router). A one-word command is SHORT, so its no_speech_prob runs high and its
# avg_logprob low by nature; the gates were tuned on chatter, not commands. Bounded by
# _CMD_NSP_MAX / _CMD_ALP_MIN so a pure-noise clip the hotword bias turned into "stop"
# still has to clear a looser bar, and the repetition backstop still applies.
_COMMANDS = {
    "stop", "halt", "quit", "exit", "next", "back", "previous", "repeat", "again", "pause",
    "resume", "skip", "go", "start", "begin", "cancel", "tip", "tips", "done", "wait", "hold",
    "hey chef", "chef", "next step", "go back", "stop listening", "stop cooking", "that's all",
    "that's it", "all done", "i'm done", "we're done", "goodbye", "bye chef", "thanks chef",
    "thank you chef", "stand down", "start over", "where was i", "read it", "never mind",
}
_CMD_NSP_MAX = 0.8
_CMD_ALP_MIN = -1.8
# 'stop stop stop stop ...' (compression-ratio hallucination on a SHORT clip) is what
# base.en made of the cook's actual 'stop' nine times on 2026-10-13, and the repetition
# backstop threw every one away. When EVERY token of the clip is the same SAFE command
# word, that word is the utterance. Navigation words (next/back) stay excluded: a false
# 'stop' costs one tap, a false 'next' loses the cook's place.
_STANDDOWN_REP = {"stop", "halt", "quit", "exit", "cancel", "pause", "wait"}


def _repeated_standdown(raw: str):
    toks = re.findall(r"[a-z']+", (raw or "").lower())
    if len(toks) >= 2 and len(set(toks)) == 1 and toks[0] in _STANDDOWN_REP:
        return toks[0]
    return None

_PHANTOMS = {
    "you", "thank you", "thanks for watching", "please subscribe", "bye", "boom",
    "okay", "ok", "oh", "so", "the", "uh", "um", "hmm", "mm", "yeah", "you know",
    "i'm", "outro", ".", "-",
}


def _norm(text: str) -> str:
    """Lowercased, punctuation/space-stripped form for phantom matching."""
    return re.sub(r"[^a-z0-9' ]", "", text.lower()).strip()

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "cook_stt.log")

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


def _log(line: str) -> None:
    """Append one diagnostic line to logs/cook_stt.log (best-effort). This is what
    'look at the logs' should read: per-utterance transcript + confidence + verdict."""
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _looks_repetitive(text: str) -> bool:
    """Backstop for the repetition hallucination that slips past compression_ratio:
    many tokens with few distinct ('Okay Okay Okay…') OR a dominant repeated phrase
    ('a little bit of a little bit of…')."""
    toks = re.findall(r"[a-z0-9']+", text.lower())
    if len(toks) < 6:
        return False
    if len(set(toks)) <= max(2, round(len(toks) * 0.30)):
        return True
    bigrams = list(zip(toks, toks[1:]))
    if bigrams:
        from collections import Counter
        top = Counter(bigrams).most_common(1)[0][1]
        if top >= 3 and top / len(bigrams) >= 0.20:
            return True
    return False


def transcribe(audio_bytes: bytes) -> str:
    """Transcribe one utterance (WAV/webm/whatever PyAV can decode) -> text, with
    noise/hallucination rejection. Returns '' on empty input OR when the audio is
    judged non-speech (so the client loop ignores it cleanly). beam_size=1 (greedy)
    keeps the live loop snappy; the gates below do the quality work."""
    if not audio_bytes:
        return ""
    model = _get_model()
    kwargs = dict(
        language="en", beam_size=1, vad_filter=False,
        condition_on_previous_text=False, temperature=0.0,
    )
    if _HOTWORDS:
        kwargs["hotwords"] = _HOTWORDS
    segments, info = model.transcribe(io.BytesIO(audio_bytes), **kwargs)

    segs = list(segments)   # materialize the generator once
    raw = " ".join(s.text.strip() for s in segs).strip()

    kept = []
    worst_nsp, worst_alp, worst_cr = 0.0, 0.0, 0.0
    for s in segs:
        nsp = float(getattr(s, "no_speech_prob", 0.0) or 0.0)
        alp = float(getattr(s, "avg_logprob", 0.0) or 0.0)
        cr = float(getattr(s, "compression_ratio", 0.0) or 0.0)
        worst_nsp = max(worst_nsp, nsp)
        worst_alp = min(worst_alp, alp)
        worst_cr = max(worst_cr, cr)
        if nsp > _NO_SPEECH_MAX or alp < _AVG_LOGPROB_MIN or cr > _COMPRESSION_MAX:
            continue
        kept.append(s.text.strip())

    out = " ".join(kept).strip()
    reason = "ok"
    if not out and raw and _norm(raw) in _COMMANDS \
            and worst_nsp <= _CMD_NSP_MAX and worst_alp >= _CMD_ALP_MIN:
        out, reason = raw, "command-pass"     # the whole clip IS a command word — let it through
    elif not out and raw and _repeated_standdown(raw) \
            and worst_nsp <= _CMD_NSP_MAX and worst_alp >= _CMD_ALP_MIN:
        out, reason = _repeated_standdown(raw), "command-pass-rep"   # 'stop stop stop…' -> 'stop'
    if not out:
        reason = "gated" if raw else "empty"
    elif _looks_repetitive(out):
        out, reason = "", "repetition"
    elif _norm(out) in _PHANTOMS:
        out, reason = "", "phantom"

    dur_ms = (getattr(info, "duration", 0.0) or 0.0) * 1000
    _log(f"{datetime.now().astimezone().isoformat()} · audio={dur_ms:.0f}ms · nsp={worst_nsp:.2f} "
         f"· alp={worst_alp:.2f} · cr={worst_cr:.2f} · raw={raw!r} · out={out!r} · {reason}")
    return out
