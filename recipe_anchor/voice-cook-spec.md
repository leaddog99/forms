# Spec addendum — Voice / hands-free cook view ("Chef")

**Why:** while cooking your hands are full and messy. The step-anchored cook view is
the ideal surface for hands-free use — speak the step, say "next" to advance, and ask
the resident expert a question without touching the screen.

**Terminology (get this right):**
- **TTS** (text-to-speech) = *speaking* the steps to the cook. Whisper does NOT do this.
- **STT** (speech-to-text) = *hearing* the cook (commands + questions). **Whisper** is an STT model.
- So the feature needs BOTH directions, from different engines.

This rides entirely on top of the rendered cook view (Phase 4 renderer). It needs almost
no new `_cook` data — steps are already discrete, ordered, navigable units. Build the
renderer **voice-aware** (discrete step units, a JS-drivable "advance/check/repeat" API,
each step's instruction available as plain spoken text) so voice is not a retrofit.

## THE KEY: real-time conversational loop + silence detection (endpointing)
(User has built this before; this is the make-or-break part — not push-to-talk.)

The experience is a continuous loop, hands never touch the screen:
1. **Always listening** — mic open, low-latency.
2. **VAD detects speech start** (energy rises) → begin capturing the utterance; show live
   interim transcript.
3. **VAD detects SILENCE** — energy below threshold for ~500–800 ms = the user stopped →
   **endpoint** the utterance (finalize). *This silence-trigger is the crux.* No button.
4. **Route** the finalized text: a command keyword (next/back/repeat…) executes instantly;
   anything else is a Chef question → LLM → streamed answer → TTS.
5. **Barge-in** — while Chef/TTS is speaking, keep the mic live; a new utterance (or
   "stop") interrupts playback immediately. A real conversation, not turn-locked.

**Silence-detection options (pick per accuracy/latency):**
- **Browser `SpeechRecognition`** fires `onresult` with `isFinal` using its own built-in
  endpointing — cheapest path, decent silence handling, no extra code. Good for v1.
- **Web Audio API energy/RMS threshold** — roll your own VAD: track short-window energy,
  declare end-of-utterance after N ms below threshold. Full control of the silence window
  + barge-in; tune the threshold to kitchen noise (sizzle, fan, running water).
- **Silero VAD in-browser** (e.g. `@ricky0123/vad-web`, ONNX) — model-based VAD, robust to
  background noise; best quality, a bit more weight. The upgrade path.
Tunables that matter in a kitchen: silence-window length, energy threshold (noisy room),
min-utterance length (ignore a clatter), and a debounce so one pause mid-sentence doesn't
endpoint early.

## Three tiers (increasing cost + complexity)

### Tier 1 — Speak the steps (TTS)
Read the current step aloud; advance reads the next.
- **v1: browser `SpeechSynthesis`** (Web Speech API) — free, on-device, zero latency, no
  backend. Good enough to ship.
- **upgrade: OpenAI TTS** (`gpt-4o-mini-tts` / `tts-1`) — a branded, warmer **"Chef"**
  voice; costs + latency + a backend audio route. Do later.
- Spoken text is **derived at render time** from `step.instruction`: expand the
  `{ingN}`/`{amt}`/`{bundle}` tokens and de-abbreviate for the ear ("1½ lb" → "one and a
  half pounds", "tsp" → "teaspoon"). Render-time transform; no stored field needed unless a
  step's spoken form must diverge from its display (add `CookStep.spoken` only then — model
  is the bug only if a phase can't hold the data).

### Tier 2 — Voice commands (keyword navigation)
Continuous listening for a small command vocabulary, mapped to the existing cook-view
controls (the left checkboxes + autoscroll):
- **next / next step** → check current + advance (the user's example).
- **back / previous · repeat (re-speak) · pause / stop · start over.**
- **v1: browser `SpeechRecognition`** — free, on-device, continuous, keyword match.
- Routing: transcribe → if the text matches a command keyword, execute it; **else it's a
  question** → Tier 3.

### Tier 3 — Ask Chef (open cooking Q&A)
Non-keyword utterances are questions for a resident cooking expert, grounded in THIS recipe.
- Flow: capture audio → **STT** (browser SpeechRecognition for v1; **Whisper** upgrade for
  accuracy/other languages) → call an LLM with **{question + recipe context (`_cook` +
  ingredients/steps) + a "Chef" system prompt}** → **TTS** the answer.
- Example: *"what does blond vs brown mean in sautéing garlic?"* → grounded explanation.
- **Backend:** new `POST /cook/ask {recipe_id, question}` → Anthropic SDK (a cheaper tier —
  Sonnet/Haiku — is fine for Q&A; recipe context grounds it) → text (+ optional TTS audio).
- **Wake / cost control:** always-listen for commands (cheap, local); gate question-mode
  behind a push-to-talk button or a "Chef…" wake word so not every utterance hits the
  LLM. Decide before wiring Tier 3.

## Build order (recommendation)
The full interactive loop is a primary goal, NOT parked — but it has to attach to something,
so the renderer comes first and is **designed for the loop from the start**.
1. Phases 1–4 first (model → validators → pipeline → **voice-aware renderer**): discrete
   step units, a JS advance/check/repeat API, plain spoken text per step, a current-step
   pointer the loop drives.
2. **Voice phase (the conversational loop)** — built as ONE coherent piece, because the
   value is in the loop, not the parts: VAD/silence-endpointing + interim transcript +
   command routing (next/back/repeat) + Chef Q&A + TTS + barge-in. Start with the
   browser-only stack (SpeechRecognition + SpeechSynthesis) end-to-end to prove the loop +
   tune the silence window; then upgrade engines (Whisper STT, OpenAI "Chef" TTS,
   Silero VAD) without changing the loop.
3. Wake/cost control inside that phase: commands are local + free; gate the LLM call so noise
   and mid-sentence pauses don't fire Chef (min-utterance length + the silence window;
   a "Chef…" prefix optional once VAD is good).

Privacy note (portable-package): continuous VAD + SpeechRecognition stay on-device; only a
deliberate Chef question leaves the machine. Make that boundary explicit in the UI.

## State of the art (researched June 2026) + component picks

**The big shift — silence alone is now the WEAK approach.** Rule-based energy VAD
("silence for N ms = done") is considered broken for natural turn-taking in 2026: it cuts
off hesitant/accented speakers and misfires on mid-sentence pauses. The frontier is
**semantic / model-based endpointing** — a model reads the partial transcript in real time
and predicts whether the user is *semantically done*, not just whether they paused. Several
streaming STT models now fold end-of-utterance INTO the ASR (NVIDIA Parakeet Realtime EOU,
AssemblyAI Universal-Streaming, Deepgram Nova-3 + Flux). So: VAD for v1, but plan the
semantic upgrade — don't over-invest in hand-tuning silence thresholds.

**VAD / turn detection**
- v1: **`@ricky0123/vad-web`** — Silero VAD in the browser via ONNX Runtime Web; simple API,
  robust to background noise (Silero trained on 6000+ languages/domains — good for kitchen
  sizzle/fan). The de-facto browser VAD.
- upgrade: semantic turn detection — Pipecat **Smart Turn** (open), **LiveKit turn-detector**,
  or let the streaming STT do it (AssemblyAI/Deepgram built-in EOU).

**STT (hearing)**
- v1 cheap: **Web Speech API** (`SpeechRecognition`) — free, built-in endpointing, but
  Chrome-centric and routes audio to Google (privacy ✗).
- v1 on-device (privacy ✓, portable-package fit): **Moonshine** — very low latency, tiny
  (down to 26 MB), runs in-browser + everywhere, beats Whisper Large-v3 at the top end;
  or whisper-web (transformers.js) / faster-whisper (Python).
- cloud upgrade: **Deepgram Nova-3 + Flux** (leads voice-agent latency + EOS), **AssemblyAI
  Universal-Streaming** (~307 ms, native turn detection), **gpt-4o-transcribe** (best WER
  ~8.9%), **ElevenLabs Scribe v2 Realtime** (~150 ms, 90+ langs).

**TTS (speaking — the "Chef" voice)**
- v1: **browser `SpeechSynthesis`** — free, on-device, zero latency.
- branded/low-latency upgrade: **Cartesia Sonic 3** (~40 ms TTFA, fastest), **ElevenLabs
  Flash 2.5** (sub-100 ms), **OpenAI gpt-4o-mini-tts** (~250 ms, *steerable character* — good
  for a warm "Chef" persona).
- open/self-host (portable-package): **Kokoro-82M** (tiny, fast, runs on consumer hardware,
  best open low-latency English), Fish Speech S2, Piper.

**Orchestration (the loop)**
- Claude has **no native speech-to-speech realtime API**, so a Claude-reasoned expert is the
  classic **STT → Claude → TTS** pipeline (NOT OpenAI's Realtime API, which uses OpenAI's
  brain — wrong for a Claude app).
- v1: **no framework** — browser VAD + browser STT + a `/cook/ask` Claude endpoint + browser
  TTS. Proves the loop with zero new infra.
- upgrade: **Pipecat** (Python, v1.0 Apr-2026; pipeline of swappable STT/LLM/TTS processors,
  JS/React "Voice UI Kit", works with Claude) for the full conversational agent (barge-in,
  interruptions handled), or **LiveKit Agents** if you want WebRTC/turn-detection/noise-
  cancellation built in.

**Recommended path for THIS app (browser cook view, Claude brain, kitchen, privacy-aware):**
- **Stage 1 (prove the loop, zero infra):** `vad-web` (Silero) for endpointing · Web Speech
  *or* Moonshine for STT · keyword routing (next/back/repeat) local · `/cook/ask` → Claude
  (Sonnet/Haiku, grounded in `_cook`) · `SpeechSynthesis` TTS. Tune the silence window here.
- **Stage 2 (make it feel natural):** swap to **semantic turn detection** + a streaming STT
  with built-in EOU, and a branded TTS (gpt-4o-mini-tts / Cartesia / Kokoro). Adopt Pipecat
  only if the loop earns the full agent treatment.
