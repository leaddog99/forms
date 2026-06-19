# Voice-Enabled Instruction Component — Architecture Deep Dive

> **Status:** Reference (knowledge transfer). Written 2026-06-18 on branch `split/enrichment-api`.
> **Audience:** an engineer who will maintain and extend the hands-free cook view ("Claudette").
> **Scope:** the whole voice loop — client (`forms/cook.html`), the three server endpoints, the
> `_cook` data substrate it renders, the cognitive-science rationale, portability/cross-domain
> design, and the competitive moats. Where this doc and the code disagree, the code wins; prefer
> referring to **function/constant names** over line numbers (the single-file `cook.html` shifts).

---

## 0. How to read this

The voice component is a **cascade** of cheap, swappable parts (mic → VAD → STT → router → {deterministic command | grounded LLM} → TTS), driving a **structured, pre-validated recipe** (`_cook`). Two ideas explain almost every decision in here:

1. **The instruction is data, not prose.** An LLM produces a validated `_cook` block offline; the
   voice layer only *renders and navigates* it. The hard thinking (technique, scheduling, mise,
   anchoring) is done once, gated by a deterministic validator gauntlet, and cached.
2. **The delivery is shaped by working-memory limits.** Every pacing/segmentation/voice-vs-screen
   choice traces to a named cognitive-load finding (see §7), not taste.

If you only read three sections: §2 (end-to-end flow), §6 (latency/conversation-flow optimizations),
§10 (maintenance playbook).

---

## 1. Component map

| Layer | File(s) | Responsibility |
|---|---|---|
| **Client voice loop** | `forms/cook.html` (single-file HTML+JS, ~1.9k lines) | Mic capture, VAD, endpointing, STT round-trip, command routing, TTS playback, sub-step/mise pacing, ask-Chef streaming, no-look touch, diagnostics. Reference implementation of the loop. |
| **STT endpoint** | `cook_stt.py` + `POST /cook/listen` (`save_recipe_api.py`) | faster-whisper transcription of one utterance WAV. |
| **TTS endpoint** | `cook_tts.py` + `POST /cook/speak` | OpenAI TTS, content-addressed cache in `media.db`. |
| **Grounded Q&A (generic)** | `voice_agent.py` | Domain-agnostic Anthropic streaming engine: sentence chunking, tool/action detection, SSE. |
| **Grounded Q&A (cooking adapter)** | `cook_ask.py` + `POST /cook/ask`, `POST /cook/ask-stream` | Chef persona, recipe grounding, `navigate` tool. |
| **Data substrate** | `cook_model.py` (`CookMetadata`/`_cook`) | The validated, step-anchored recipe the voice renders. |
| **Generation** | `cook_rework.py`, `cook_validators.py`, `cook_augment.py`, `cook_tips_kb.py` | Offline rework that produces + gates + augments `_cook`. |

Design docs that back this: `docs/voice-agent-architecture.md`, `docs/procedural-instruction-research.md`,
`docs/procedural-instruction-research-deep.md`, `docs/cook-substeps-v2.1-design.md`, `docs/cook-kb-as-product.md`.
Relevant memory: `project_cook_voice`, `project_voice_redesign`, `project_recipe_anchor`, `project_voice_pack`.

---

## 2. End-to-end flow

### 2.1 The hands-free turn

```
                 ┌─────────────────────── browser (cook.html) ───────────────────────┐
 mic ─► getUserMedia(echoCancel+noiseSupp+AGC)
        │
        ▼
   ScriptProcessor onAudioFrame  ── per-frame RMS vs adaptive noise floor
        │   (capture starts when rms>onT; ends after an ADAPTIVE silence hang)
        ▼
   encodeWAV(samples) ──HTTP POST──►  /cook/listen ──► cook_stt.py (faster-whisper base.en)
        │                                                   returns {text}
        ▼
   routeUtterance(text)  ── decision tree (see §5.3):
        ├─ armed?            → go-word starts step 1
        ├─ tip offer answer? → yes/no
        ├─ awaiting a Q?     → send to Chef
        ├─ deterministic command (next/back/tip/mise/restart/stop…)? → act, INSTANT, no LLM
        ├─ wake word ("hey chef …")? → askChef() OR bare-wake ack + await
        ├─ in the follow-up window? → askChef() (no wake word needed)
        └─ else → ignored (ambient)
        │
   speak(text) ──► getSpeechUrl(text):
        ├─ cache hit (blob URL, padded WAV) → play instantly
        └─ miss → POST /cook/speak → MP3 → decode → prepend 180ms silence → WAV → cache → play
                                       (server: media.db content-addressed cache)
        ▼
   <audio> element (routed through a GainNode on touch devices) → speaker
                 └────────────────────────────────────────────────────────────────────┘

   askChef(q) path adds: POST /cook/ask-stream (SSE) → voice_agent.stream_grounded
        → claude-sonnet-4-6 → {sentence|action|done} events
        → sentences are TTS'd + queued + played back-to-back as they arrive
```

### 2.2 The data the loop renders

The browser is handed a `_cook` block (`window.CK`) — the recipe already reworked into a
step-anchored, fully-measured, validated structure (§3). The loop never reasons about cooking; it
walks `CK.steps`, `CK.bundles` (the mise), and each step's `substeps` (voice-pacing chunks).

---

## 3. The data substrate — `_cook` (what voice renders)

The voice component is only as good as the structure beneath it. `cook_model.py`'s `CookMetadata`
(stored on a recipe as `_cook`) enforces three binding invariants that make hands-free delivery
*possible*:

1. **No mid-cook measuring.** 100% of measuring lives in the mise (`Bundle`s, including a
   "Measured & ready" catch-all). Method steps only *reference back*.
2. **No lookback.** Every step is self-contained; nothing says "see the list above."
3. **Decisions already made.** Unit system, ingredient form, yield are baked into prose, not
   deferred to UI controls.

### 3.1 Key types (see `cook_model.py`)

- **`CookAmount`** `{imperial, metric, convertible}` — two faces, display-ready. `convertible=False`
  exempts counts/pinches/back-refs (shown identically both faces). The model never computes
  conversions; code supplies them (§4.x).
- **`CookIngredient`** — shopping-grain entity, `form_variants[]`, and `first_step` (the
  appearance-order index, **computed by code**).
- **`Bundle`** / **`BundleMember`** — the mise: pre-combined groups with `combine_note` (why
  combined) or `excluded_reason` (why kept separate), `make_ahead`, and `first_step` (deploy order).
- **`CookStep`** — `instruction` with inline tokens `{ingN}`, `{amt:IMP|MET}`, `{bundle:ID}`;
  `ingredients[]` as **back-references** (`StepIngredient.definiteness ∈ {the,your,reserved,from-step,bundle}`);
  `substeps[]` (voice pacing); `attention` (active/passive), `depends_on`, `resource` (for scheduling).
- **`CookSubStep`** `{voice, screen, ingredients[]}` — the heart of voice pacing: a **terse spoken
  `voice` line** + a **fuller on-screen `screen` fragment** carrying the numbers + 1-based indices
  into the parent step's ingredients (coverage-gated). This is the cognitive-load split made data (§7.3).
- **`ReservedItem`** — put-asides held `created_step → consumed_step` (living progress indicator).
- **`Attachment`** `{kb_id, kind:tip|check, text, media[]}` — KB-sourced tips/checks with provenance.
- **`CookMetadata`** — top-level: `ingredients/bundles/equipment/reserved/steps` **all sorted by
  first appearance**, plus `headnote`, `finish`, `cooks_note`, `tips[]`, `technique_changes[]`, and
  the `validators` report + `rework_prompt_version` + `reworked_at` stamps.

**Appearance-order invariant:** ingredients, bundles, equipment, reserved are stored in the order the
cook will reach for them (each carries a step index). The Shopping/mise/gear/put-aside views lay out
left-to-right and are consumed in sequence — they double as progress indicators.

### 3.2 Generation: `cook_rework.py` (offline, not on the hot path)

The rework is where the **cost/quality architecture** is made real — a strict division of labor:

- **CODE** does the deterministic work for $0 and full reliability: pulls amounts from
  `_measurements` (`build_rework_input`, with `_clean_metric_face()` stripping stranded imperial
  approximations), computes `first_step` appearance order (`_stamp_first_step`), runs the gauntlet,
  drives repair loops, and stamps cost/version.
- **The LLM does ONLY judgment** (`claude-opus-4-8`, forced `emit_cook` tool, `_MAX_TOKENS=16000`
  non-streaming): technique audit (`technique_changes`), scheduling, bundling/mise design, anchoring
  prose, and the `substeps` split. It is told to **use the provided conversions, never compute** units.
- Prompt version `cook-rework-v2.2-2026-06-16` (v2 = tips come from the KB augment pass with
  provenance, not invented; v2.2 = model-authored voice `substeps`). A v2.1 design (`mise` vs
  `method` separation) is pending → will bump to v2.3.

Pipeline: `build_rework_input` → Opus `emit_cook` → `_assemble_with_repair` (pydantic schema repair)
→ `run_all` gauntlet → one Opus **repair loop** if any gate fails → `augment_cook` (attach KB
tips/checks **only if gates pass**) → dedupe → persist with `validators` report. **A rework that
fails any gate does not ship.**

### 3.3 The validator gauntlet (`cook_validators.py`) — 10 hard gates

`run_all()` → `CookValidatorReport{passed, failures[], ran[]}`. All are **hard** (block persist):

| Gate | Enforces |
|---|---|
| `has-steps` | A rework with zero steps is a failed/truncated emit, not a cook. |
| `unit-consistency` | No amount face mixes systems (imperial face carries no metric token, etc.). |
| `definite-article` | Every step ingredient is a back-reference (`definiteness` set) → measuring already happened. |
| `every-measure-numeric` | A *convertible* amount must show a number (imprecise measures are `convertible=False`, exempt). |
| `no-lookback` | No "see the list / as above / picker above" prose. |
| `mise-complete` | Every ingredient is pre-measured in some bundle; no step introduces an undeclared ingredient. |
| `appearance-order` | The four lists sorted by first appearance; reuse points *backward* (`reused_from_step`); `consumed_step ≥ created_step`. |
| `reuse-referenced` | Re-used tools/ingredients reference their origin step — except layered staples (salt/pepper/oil/water/`to_taste`). |
| `bundling` | Every bundle states `combine_note` or `excluded_reason`. |
| `substeps` | When present: every sub-step has non-empty `voice`; indices valid; **coverage** — every step ingredient appears in some sub-step. |

Advisory (not gated): `technique_changes` content, the *quality* of the sub-step split, and KB
attachments (added post-gate; non-fatal if augment fails).

### 3.4 Owned knowledge: `cook_tips_kb.py`

A hand-authored KB (~18 entries) of success-tips + failure-mode checks keyed by technique
(`saute_garlic`, `reserve_pasta_water`, `discard_unopened_shellfish`, …). The augment pass **SELECTs
a `kb_id` and rewords it** for the recipe; it never invents advice, and **media is curated on the
entry, never model-emitted** (a hallucinated URL can never reach a user). This is a moat (§9).

---

## 4. Client architecture (`forms/cook.html`)

### 4.1 Audio capture + VAD

- `getUserMedia({echoCancellation:true, noiseSuppression:true, autoGainControl:true})` — AGC ON
  because a desktop mic at arm's length sat below threshold without it.
- **Self-contained Web Audio RMS-energy VAD.** Primary path is an **AudioWorklet**
  (`forms/vad-worklet.js`, `vad-processor`) running on the dedicated audio-rendering thread, so
  main-thread/GC jank can't stall capture (an earlier main-thread pre-roll allocating per-frame in
  the callback tanked iPad hands-free — moving to the worklet + pre-allocated buffers is the fix).
  The worklet posts only EVENTS to the main thread (`start`/`end`/`drop`/`hb`/`barge`); the main
  thread tells it when Chef is `speaking`/`awaiting` (via `_setSpeaking`/`setAwaiting`). The old
  main-thread **`ScriptProcessor` `onAudioFrame` is kept as a fallback** (no AudioWorklet support, or
  any failure; force it with `USE_AUDIO_WORKLET=false`). Both compute per-frame RMS against an
  **adaptive noise floor** (`noiseFloor = 0.95·floor + 0.05·rms` while quiet). Thresholds:
  `onT = max(0.022, floor·3.5)`, `offT = max(0.015, floor·2.2)` (more eager when `awaitingQ`). The
  worklet also does an **allocation-free onset pre-roll** (a pre-allocated ring) so word onsets
  aren't clipped. Config (hang/threshold/pre-roll tunables) ships from cook.html via `_vadCfg()`. *We deliberately do NOT use Silero/`vad-web`* — it needed `window.ort`; the
  self-contained energy VAD works offline and always triggers the mic prompt (portability fit). A
  stale server comment still mentions vad-web; ignore it.
- **Mic heartbeat** (every ~50 frames / ~4s): logs peak/floor/threshold/state and, when idle, shows
  a live on-screen level so a cook can *see* the mic working. This was the key diagnostic for the
  "goes to listening but doesn't hear" class of bugs.

### 4.2 Endpointing — the adaptive silence hang

Capture ends after a silence hang. The hang is **adaptive** (this is the single biggest "next"
latency win — see §6):

| Constant | Value | Meaning |
|---|---|---|
| `SILENCE_HANG_MS` | 800 | Full hang — long utterances, questions, and the wake word. |
| `SILENCE_HANG_SHORT_MS` | 420 | Short hang — a crisp one-word command. |
| `SHORT_CMD_MAX_VOICE_MS` | 360 | Voiced ≤ this ⇒ treat as a short command (use the short hang). |
| `MIN_VOICE_MS` | 170 | Reject clicks/coughs below this. |

The 360 gate is load-bearing: "hey chef" voices ~450ms, so it stays on the **full** hang and does not
endpoint early on the natural pause before a question (an earlier 600 value caused exactly that — the
wake split off and the ack's deaf window clipped the question's front). The chosen hang is logged on
the `stt` line (`· Nms hang`) for tuning.

### 4.3 STT round-trip (`/cook/listen` → `cook_stt.py`)

- faster-whisper **`base.en`**, `device=cpu`, `compute_type=int8`, **`beam_size=1`** (greedy, fast
  on short utterances), `language=en`, **`vad_filter=False`** (browser already endpointed),
  `condition_on_previous_text=False`, `temperature=0.0`.
- Lazy thread-safe singleton kept warm in-process; optional `warm()` at startup. ~140MB first load.
- Stateless; returns `{text}`. **Audio never leaves the box for transcription** (privacy moat §9).

### 4.4 Command routing — `routeUtterance(text)` decision tree

Order matters; the first match wins. (`REQUIRE_WAKE_FOR_COMMANDS=false` → commands work without a
wake word; flip for strict Alexa mode.)

1. **Armed-but-not-started** (`_armedWaiting`, see §4.8): a go-word (`go/start/begin/ready/cook/
   let's cook/…`) reads step 1; `stop/cancel` aborts; anything else keeps waiting.
2. **Tip-offer answer** (if a proactive "Read the tip?" is pending — currently disabled,
   `TIP_OFFER_ENABLED=false`): yes → read, no → drop.
3. **Awaiting a question** (`awaitingQ`, after a bare "hey chef"): a command still wins; else send to
   Chef. Bare-wake residue (mis-hears like "he's chef") keeps waiting *quietly*.
4. **Deterministic command** (`_CMD_RULES`, `matchCommand`): `where/mise/tip/repeat/restart/cancel/
   pause/back/next/stop`. **Instant, no LLM, cannot be misrouted.** `_Q_BLOCK` vetoes nav when the
   utterance is a question ("how long until the next flip" asks, not advances). Forgiving matching
   (a keyword can sit inside filler) but echo-prone words (done/continue/got it) stay out.
5. **Wake word** ("hey chef …"): substantive remainder → `askChef`; bare wake → short ack + `await`.
6. **Follow-up window** (`_now() < followUpUntil`, `FOLLOWUP_MS=25000`): a substantive utterance
   shortly after an answer goes to Chef with **no wake word needed** (natural conversation).
7. **Ambient** → ignored silently (never nag the cook).

### 4.5 TTS playback pipeline

- **Premaking + cache (two tiers):** `prefetchAllSteps()` warms every step/sub-step (and the acks)
  into an in-memory per-text cache (`_ttsCache`, blob URLs); the server keeps a **content-addressed
  cache in `media.db`** keyed by `sha256(text+model+voice+instructions)` so even a cold first-open is
  a hit on subsequent loads. On "next", playback is from cache — **no OpenAI round-trip between
  command and speech**.
- **Lead-in silence** (`LEAD_SILENCE_MS=180`): `getSpeechUrl` decodes each MP3, prepends 180ms of
  silence, re-encodes to WAV (`encodeWAV`), caches that. The output-device/decoder cold-start then
  swallows silence, not the first syllable. Best-effort; any decode failure falls back to the raw MP3.
- **Loudness:** on touch devices only, the `<audio>` element is routed through a Web Audio `GainNode`
  (`TTS_GAIN=2.0`) because iPad speakers play the MP3 quietly even at full volume. Desktop skips this
  (double-path would echo).
- **iOS unlock:** `unlockAudio()` resumes the AudioContext inside a user gesture (Start/Read/Ask/tap)
  — iOS blocks programmatic audio otherwise.
- **Single reusable `<audio>` element** so a new clip *replaces* the current one (rapid taps can't
  stack). `_speakSeq` invalidates superseded `speak()` calls. `_onSpeakEnd` sets a 700ms
  `_speakGuard` echo tail (mic deaf just after she stops).

### 4.6 Sub-steps + mise (voice pacing)

- **Sub-steps:** `subsFor(step)` extracts `step.substeps[].voice` (de-abbreviated/tidied); `_subs`/
  `_subIdx` track position. `next/back/repeat` operate at **sub-step granularity** when Talk is on
  (one chunk per "next"; back/repeat re-scan). `highlightSub(i)` lights the matching on-screen
  `.substep.active` and scrolls it into view. Talk OFF → whole-step, `_subs=[]`.
- **Mise:** `miseLines()` builds spoken cluster lines from `CK.bundles`; `enterMise()`/`speakMiseAt`
  walk one cluster per "next", then fall into step 1. Opt-in via the `mise` command today (auto-mise
  was disabled because it duplicated early "combine" steps — the v2.1 mise/method design fixes the
  root cause and will re-enable it).
- **Place-keeping:** `whereWasI()` re-speaks the current cluster or "step N of M, part i/m" + the
  current chunk (speech leaves no re-scannable trace — §7.3).
- **Text pipeline:** `spokenText(step)` expands `{ingN}/{amt:…}/{bundle:…}` tokens; `deAbbrev()`
  turns "½"→"one half", "Tbsp"→"tablespoons", "°F"→"degrees Fahrenheit", etc. for natural TTS.

### 4.7 Ask-Chef streaming

- `askChef(q)` POSTs `/cook/ask-stream` and reads the SSE stream. `makeAnswerPlayer()` TTSs each
  `sentence` event as it arrives and plays sentences **back-to-back, pipelined** (sentence N+1
  fetches while N plays), so audio starts ~1s into generation rather than after the full answer.
- `_streamSeq` cancels an in-flight answer (new ask / stop / nav). An `action` event routes through
  `doChefAction` (conversational navigation). `finish()` mirrors `_onSpeakEnd` (opens the follow-up
  window). Falls back to non-streaming `askChefOnce()` (`/cook/ask`) if SSE is unavailable.

### 4.8 Acks + arm-then-go

- Short spoken acks, all prefetched: `_WAKE_ACKS=["Yes?","Here!"]` (bare wake),
  `_STOP_ACKS=["All done!","See you!","Stopping."]` (a *voice* stop only — a button/tap stop is
  silent because you see it reset), `_READY_PROMPTS=["Ready when you are.", "All set — say go when
  you're ready."]`.
- **Arm-then-go** (`START_ON_GO=true`): the Start button arms the mic, cues "ready," and waits for a
  go-word before reading step 1 — Chef doesn't launch into the recipe before the cook is positioned.
  Flip `START_ON_GO=false` for instant start.

### 4.9 No-look touch mode (`setTouchMode`/`wireTouchPad`)

A three-zone gesture pad for "almost hands-free" (gloves/wet hands): single tap **L**=smart-back,
**M**=play/pause, **R**=next; long-press (`HOLD=500ms`) **L**=restart, **R**=end. `smartBack` is a
music-player "previous" pattern (`BACK_RESTART_MS=3000`: tap early → previous step, tap mid-step →
restart this step). Works with mouse for desktop testing. Zone flash gives visual feedback.

### 4.10 Robustness: diagnostics, watchdogs, failure modes

- **`vlog(kind,text)`** ring buffer (400) → on-screen panel; kinds: `session/stt/heard/mic/vad/
  command/wake/ask/followup/answer/ignored/error`. This is the primary field-debug tool — ask the
  user to read an `stt · Nms · …hang` line.
- **VAD-drop counter** (`noteVadDrop`/`flushVadDrops`): coalesces too-short captures so the log isn't
  buried; flushes at 12 or on stop.
- **Watchdogs** (the loop can never wedge): `setAwaiting` clears `awaitingQ` after `AWAIT_MS=12000`;
  `setAsking` clears `_asking` after `ASK_WATCHDOG_MS=30000` (guards a skipped `_onSpeakEnd`).
- **Degradation:** every server endpoint maps errors to a friendly 503 (never a stack trace); the
  client falls back (SSE→non-stream→browser `speechSynthesis`).
- **Barge-in is OFF** (`BARGE_IN=false`): without real AEC, Chef's own voice off the speakers
  self-triggered the cutoff. Re-enable only with a headset, or once a WebRTC-loopback AEC lands (P1).

---

## 5. Server architecture

### 5.1 `/cook/listen` — STT
`POST` multipart WAV → `{text}` (503 on failure). Backed by `cook_stt.py` (§4.3). No caching
(stateless); no server-side VAD.

### 5.2 `/cook/speak` — TTS
`POST {text}` → MP3 bytes + `X-TTS-Cache: hit|miss`. `cook_tts.py`: `gpt-4o-mini-tts`, voice
**`coral`**, steerable `instructions` (warm/calm/encouraging persona — shapes tone, not words),
`TTS_MAX_CHARS=1400` guardrail. Content-addressed `tts_audio` table in `media.db`; key self-
invalidates if voice/model/instructions change. ~250ms to first audio, ~$0.015/1k chars.

### 5.3 `/cook/ask` and `/cook/ask-stream` — grounded Q&A
The clean **generic-engine / domain-adapter** split:

- **`voice_agent.py` (generic, reuse unchanged):** `stream_grounded(*, system, context, question,
  model, max_tokens, tools, operation, usage_log)` → a generator of event dicts. Owns Anthropic
  streaming, **sentence chunking** (`_SENTENCE_BOUNDARY = (?<=[.!?])\s+`, preserves decimals),
  tool/action detection, token journaling, and **answer-XOR-navigate** (if the model emits answer
  text, a trailing tool call is dropped). `grounded(...)` is the non-streaming twin; `sse(events)`
  serializes to `data:` frames.
- **`cook_ask.py` (cooking adapter):** supplies `CHEF_SYSTEM` (warm, brief, **spoken prose — no
  markdown/lists/emoji**, 1–3 sentences, food-safety-accurate, never invents recipe facts),
  `build_context(recipe, current_step)` (compact JSON from the `_cook` block: ingredients, mise
  bundles, put-asides, token-expanded steps, "cook is currently on step N"), the **`navigate` tool**
  (`enum: next|back|repeat|restart|pause|resume|stop|goto` + `step`), and `ask`/`ask_or_act`/
  `ask_or_act_stream`. Model: **`claude-sonnet-4-6`**, `max_tokens=700`.
- **Endpoints:** `/cook/ask` (sync, used by the typed-box path + fallback) returns `{answer}` or
  `{action,step}`. `/cook/ask-stream` returns SSE `{sentence}*`, optional terminal `{action}`,
  `{done}`, `{error}`. Usage journaled in the generator's `finally`.

---

## 6. Efficiency, conversation flow, and latency optimization

This is the section to internalize before "improving" anything — most obvious tweaks have already
been made for a reason.

**Command latency (the "say *next* → hear next step" loop):**
- **Deterministic commands bypass the LLM entirely** (§4.4). The common case is a local regex match +
  a cached-clip play. No network reasoning on the hot path.
- **Adaptive endpointing** (§4.2) is the biggest lever: a one-word command endpoints at 420ms, not
  800ms. We *measured* that TTS was already cached, so the hang + STT round-trip were the real cost.
- **STT tuned for short utterances:** `beam_size=1` greedy, `int8` CPU, warm singleton, server-side
  VAD off (the client already endpointed). Verify the split with the `stt · Nms · …hang` log line
  before optimizing further.
- **TTS premaking + dual cache + lead-in silence** (§4.5): "next" plays a cached, pre-padded clip
  immediately — no synthesis, no cold-start syllable clip.

**Conversational latency (ask-Chef):**
- **Sentence-streamed TTS** (§4.7): the player speaks sentence 1 while the model is still writing
  sentence 3, and prefetches N+1 while N plays. First audio ~1s in, not after the full answer.
- **Answer-XOR-navigate** prevents a double response (speak *and* jump).
- **Follow-up window** (`FOLLOWUP_MS=25000`): no "hey chef" needed for the natural next question —
  removes a wake-word round trip from multi-turn exchanges.

**Cost efficiency:**
- The **cascade is per-event** (cents/session), not a per-minute managed voice platform that bills
  idle session time — wrong for a recipe you leave running on the counter (`project_voice_redesign`).
- The expensive reasoning (`_cook` rework, Opus) is **offline and cached**, never on the voice path.
- Q&A is Sonnet (cheap) + tightly-scoped context + `max_tokens=700`.

**Flow/UX guarantees:**
- **Watchdogs** make the loop un-wedgeable (§4.10).
- **Place-keeping** + **check-to-advance** keep a hands-busy, interruptible cook oriented (§7).
- **Short acks** minimize the no-AEC deaf window; **arm-then-go** prevents Chef talking before the
  cook is ready.

---

## 7. Cognitive-science grounding

The pacing model is not taste; it implements named findings. The design docs separate
**[VERIFIED]** (a 3-vote adversarial verification run, 2026-06-16) from **[LITERATURE]**
(well-established but not independently re-verified). Preserve that distinction when citing.

| Design decision (shipped) | Principle | Evidence |
|---|---|---|
| Split a step on **independence**, keep **coupled** actions together ("whisk *while* pouring") | **Element interactivity** (load = elements processed *simultaneously because coupled*, not word count); split-attention | [VERIFIED] Sweller 2010 |
| **≤ ~3 interacting elements** per spoken sub-step | Working-memory capacity ~**4 chunks** (Cowan), *fewer* under divided attention; **not** Miller's 7±2 (explicitly refuted) | [VERIFIED] Cowan 2001 |
| **`voice` = terse action; `screen` = the numbers** | **Modality principle** *and its boundary* — audio+visual beats visual (d≈0.76), but the advantage reverses for long/dense/numeric material (a recipe). Use each modality for what it's best at. | [VERIFIED] Baddeley/Hitch, Paivio; Mayer |
| **Aggressively short** voice; "if it feels short, split again" | **Transient information effect** — speech vanishes; long narration overruns WM. Designers systematically *under*-cut; it took *two* rounds of shortening to restore the benefit. | [VERIFIED] Leahy & Sweller |
| **One sub-step per "next"** (check-to-advance, user-paced) | **Segmenting principle** — the active ingredient is *user control*: the executor signals "next" when *their* WM has cleared. We didn't retrofit a justification; the principle prescribes it. | [VERIFIED] Mayer |
| **Coverage gate** — every ingredient is spoken in some sub-step | faithful segmentation; no silent omissions | (engineering invariant, `v_substeps`) |
| **Mise / bundles** — pre-measure + pre-group before cooking | **Pre-training + extraneous-load removal made physical** | [VERIFIED] CLT |
| **"No lookback"** — each step self-contained | **Split-attention effect** | [VERIFIED] |
| **"Where was I?"** re-speaks the current chunk | transience leaves no re-scannable artifact | [VERIFIED transient; place-keeping itself flagged un-verified] |
| **Verbosity / expertise-fade** (PLANNED) | **Expertise reversal** — detail that scaffolds a novice is redundant load to an expert; there is *no single correct granularity*. | [VERIFIED] Kalyuga 2007 |

**Convergent applied traditions** [LITERATURE, low evidence — convergent, not independently
verified in our run]: Toyota **TWI Job Instruction** ("Important Steps" + "Key Points" = our
action-line / critical-detail split, found by industrial trainers decades before CLT); the
**surgical/aviation checklist** (segmented pause-points under stress); **mise en place** (pre-staging
= pre-training); Carroll's **minimalist instruction** (action-first); **IKEA wordless diagrams** (one
action per frame). They converge on the same structure the lab science predicts.

**Honestly-flagged gaps** (`procedural-instruction-research-deep.md §9`): the exact element cap
(≤3–4 is a heuristic, not a cooking-specific optimum); the transient crossover *length* (direction
robust, threshold material-dependent); a validated in-app **skill signal** (so expertise-fade is a
manual setting for now); and **interruption/place-keeping** as procedural prospective memory — *no
primary source survived verification*, so our check-to-advance + highlight + re-speak answer is
*reasoned design*, flagged for a dedicated research run.

---

## 8. Sub-steps v2.1 — mise vs method (DESIGN, pending)

`docs/cook-substeps-v2.1-design.md`. Observed problem (Chicken Milanese): the spoken mise walk and an
early "stir the spices together" method step say the same thing twice. **Root cause:** the rework
conflates *cold pre-combining* (mise) with *combining-as-cooking* (method).

**Fix (prompt + two optional fields, no schema overhaul):**
- `Bundle.mise_action` (e.g. "whisk until emulsified") so the mise *line* carries the combining verb;
  `Bundle.kind ∈ {gather,combine,catchall}`.
- Rework classifies each source instruction as mise or method; a pure "stir these already-bundled
  things together" becomes a bundle, **not** a step.
- New lenient gate `v_no_redundant_combine` (fires only on the tight case: a combine-verb step that
  deploys exactly one bundle, no duration/heat). Salad-toss-at-serve stays a real step.
- Renderer: the **mise walk speaks the combining once**; auto-mise at hands-free start gets
  re-enabled (its duplication reason is gone). Bump `REWORK_PROMPT_VERSION` → v2.3 + re-rework via
  `scripts/rerework_cooks.py`. Old blocks render unchanged (fields optional).

---

## 9. Moats — why this is hard to copy

These are durable advantages a competitor can't trivially replicate, roughly in order of depth.

1. **The validated `_cook` substrate.** The differentiator isn't TTS — it's that the instruction is a
   *structured, machine-checked artifact*. The 10-gate gauntlet guarantees measure-once, no-lookback,
   appearance-order, mise-completeness, and sub-step coverage. A competitor piping raw recipe text
   into TTS can't deliver hands-free pacing because raw text has no clean seams. Reproducing this
   means rebuilding the rework engine *and* the validators *and* the prompt discipline.
2. **Cognitively-grounded pacing.** The voice/screen split, element-interactivity chunking, and
   check-to-advance are backed by an adversarially-verified research base (`procedural-instruction-
   research-deep.md`). It's not a UI skin; it's why the delivery actually reduces cognitive load. The
   research itself (the [VERIFIED] vs [LITERATURE] discipline) is an asset.
3. **Owned, anti-hallucination knowledge base.** `cook_tips_kb` is curated, in-our-words technique
   knowledge; the augment pass *selects* entries by `kb_id` (mechanically validated provenance) and
   **never invents**, and **media is curated, never model-emitted**. The moat grows with curation and
   can't be scraped; the guardrail (`source='curated'` gates every shared projection) keeps it clean.
4. **Cost architecture.** Expensive reasoning is offline + cached (Opus once per recipe);
   the live loop is deterministic commands + cached TTS + cheap Sonnet Q&A + a per-event cascade. A
   competitor on a per-minute managed voice platform bills idle counter-time and can't match unit
   economics at catalog scale.
5. **Portability / self-hostable product.** The cascade is BYOK and offline-capable (self-contained
   VAD, local STT). The north star is a shippable product, not a SaaS tenant (`project_portable_
   package`) — a different go-to-market a hosted competitor can't easily mirror.
6. **Cross-domain generalization** (§10.4). The same engine is a *procedural-guidance platform*, not
   a cooking app. The breadth (cooking-first, then any step-wise domain) is a strategic moat.

---

## 10. Maintenance & extension playbook

### 10.1 Operational gotchas
- **`cook.html` and `cook_*.py` are served as static/loaded assets** — a browser reload picks up
  client changes; **no server restart needed** for `cook.html`. The cook-rework job runs
  out-of-process and picks up Python changes without a restart.
- **Restart correctly:** `bcc_restart.bat` HANGS non-interactively and leaves the stale server
  serving. Kill the `:8009` listener and `Start-Process python -m uvicorn` directly, then **verify
  new code is live** (curl the changed endpoint) before trusting a screenshot. (`project_restart_
  zombie_port`.)

### 10.2 Tuning latency / recognition (start here)
1. Have the user read an `stt · Nms · …hang` line mid-cook → splits hang vs whisper round-trip.
2. Too-slow commands: lower `SILENCE_HANG_SHORT_MS`, but watch `SHORT_CMD_MAX_VOICE_MS` (keep "hey
   chef" on the full hang).
3. First-syllable clip: raise `LEAD_SILENCE_MS` (220–250); sluggish start: lower it (~120).
4. Missed short words: `MIN_VOICE_MS` floor and the `onT/offT` multipliers in `onAudioFrame`.
5. Quiet on iPad: `TTS_GAIN`.

### 10.3 Adding/changing a voice command
- Add a rule to `_CMD_RULES` `{name, re, act}`. **Anchor risky short words** to the whole utterance
  (`^word$`) so they don't fire mid-sentence (see how `check` was added to `next`). Add a `_Q_BLOCK`
  pattern if the word collides with a question. Spoken phrases/acks are prefetched in
  `prefetchAllSteps` — add new ones there.
- **Caveat / planned refactor:** command keywords + spoken phrases are currently **literal English in
  code**. The planned **voice pack** (`project_voice_pack`, `docs/voice-pack.md` TBD) externalizes the
  *data* (wake list, per-intent synonym lists, phrases) to a per-language JSON; code keeps the *logic*
  (regex assembly + routing) and builds regexes from the lists at load. Prefer pushing new language
  data toward that shape. (True non-English voice also needs a multilingual STT model — `base.en` is
  English-only — and a per-language TTS persona.)

### 10.4 Adding a new domain instance (the reusability checklist)
The architecture is designed so a new step-wise domain (plumbing repair, furniture assembly,
appliance setup, lab protocol) reuses the engine:
1. Write `<domain>_ask.py`: a persona system prompt, `build_context(entity, position)`, a nav tool +
   action enum, and an `ask_or_act_stream` adapter over `voice_agent.stream_grounded`.
2. Add `POST /<domain>/ask-stream` (+ non-stream) that loads the entity.
3. Client: instantiate the loop config — wake word, command regexes → nav, `onAction(action, step)`,
   proactive offers, context-number provider.
4. TTS/STT are shared (`/cook/speak`, `/cook/listen`).

The **planned extraction** is `VoiceAgent.mount(config)` (factor the `cook.html` loop into a reusable
module taking `{wakeWords, persona, commands, endpoints, onAction, proactiveOffer, contextNumber}`).
`cook.html` is the reference implementation until then. The `_cook` schema is, structurally, a general
**procedure** schema (ingredients≈parts/materials, equipment≈tools, steps+sub-steps, tips/checks,
put-asides≈sub-assemblies set aside) — "recipe = procedure." A plumbing job has materials to stage
(mise), tools in order of need, coupled vs independent actions, doneness checks, and parts set aside
mid-job; the same pacing engine applies unchanged.

### 10.5 Key constants (single reference)

| Constant | Value | File | Purpose |
|---|---|---|---|
| `SILENCE_HANG_MS` | 800 | cook.html | full endpoint hang |
| `SILENCE_HANG_SHORT_MS` | 420 | cook.html | short-command hang |
| `SHORT_CMD_MAX_VOICE_MS` | 360 | cook.html | short-command gate |
| `MIN_VOICE_MS` | 170 | cook.html | reject clicks/coughs |
| `LEAD_SILENCE_MS` | 180 | cook.html | anti first-syllable-clip pad |
| `TTS_GAIN` | 2.0 | cook.html | iPad loudness (touch only) |
| `FOLLOWUP_MS` | 25000 | cook.html | wake-free follow-up window |
| `AWAIT_MS` / `ASK_WATCHDOG_MS` | 12000 / 30000 | cook.html | loop-unwedge watchdogs |
| `BACK_RESTART_MS` | 3000 | cook.html | smart-back threshold |
| `HOLD` | 500 | cook.html | touch long-press threshold |
| `START_ON_GO` | true | cook.html | arm-then-go start |
| `BARGE_IN` | false | cook.html | barge-in (needs AEC) |
| `REQUIRE_WAKE_FOR_COMMANDS` | false | cook.html | strict Alexa mode |
| `TIP_OFFER_ENABLED` | false | cook.html | proactive tip offer |
| TTS model / voice | `gpt-4o-mini-tts` / `coral` | cook_tts.py | |
| STT model | faster-whisper `base.en`, int8, beam=1 | cook_stt.py | |
| Q&A model | `claude-sonnet-4-6`, 700 tok | cook_ask.py | |
| Rework model / prompt | `claude-opus-4-8` / `cook-rework-v2.2-2026-06-16`, 16000 tok | cook_rework.py | |

### 10.6 Roadmap (carried)
- **Barge-in / AEC** (Voice P1) — the gating blocker for talk-over; WebRTC-loopback AEC, then
  re-enable `BARGE_IN` and the proactive tip offer.
- **Sub-steps v2.1** (mise/method split, §8) → re-enable auto-mise.
- **Expertise fade** — verbosity setting first, behavioral inference later.
- **Voice pack** (§10.3) — externalize language; prerequisite for non-English voice (needs
  multilingual STT + TTS persona too).
- **Local command spotter** — "measure first": confirm the `stt` split before building a Web Speech
  API fast-path vs in-browser whisper vs cheaper server tuning.
- **`VoiceAgent.mount` extraction** + a second domain instance.
- **Interruption/place-keeping research run** (the one [un-verified] design assumption).

---

## 11. Glossary
- **`_cook`** — the validated, step-anchored recipe block (`CookMetadata`) the voice renders.
- **mise / bundle** — pre-measured, often pre-combined ingredient group; holds 100% of measuring.
- **put-aside / `ReservedItem`** — something set aside mid-cook and used later.
- **sub-step** — a memory-sized chunk of a step; `voice` (spoken) + `screen` (numbers).
- **the gauntlet** — the 10 hard validators that gate a rework.
- **the cascade** — mic → VAD → STT → router → {command | LLM} → TTS, per-event, BYOK.
- **deterministic command** — a voice command matched by local regex, executed with no LLM.
- **arm-then-go** — Start arms the mic and waits for a spoken go-word before reading step 1.
