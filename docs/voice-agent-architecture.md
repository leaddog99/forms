# Grounded Voice Agent — reusable architecture

A persona with **general expertise in a DOMAIN**, grounded in **one SPECIFIC
ENTITY's structured data**, that either **answers** a spoken question (streamed
sentence-by-sentence for low latency) or performs a **domain navigation action**.
Built first for the cook view; designed to be reused across apps.

## Planned instances

| Entity (specific) | Domain (general) | Persona | Nav actions |
|---|---|---|---|
| this **recipe** (`_cook`) | cooking | "Chef" | next / back / repeat / restart / pause / stop / goto step |
| this **person** | their bio / field | the person | section / topic nav |
| this **product** | its product category | (brand voice) | spec / compare / variant nav |

The person and product apps are future; the architecture below is what they reuse.

## Three interaction tiers (per `recipe_anchor/voice-cook-spec.md`)
1. **Deterministic commands** — instant, client-side, no LLM (the domain nav verbs). Reliable + free; can't be misrouted.
2. **Grounded Q&A** — streamed LLM answer, entity data + general domain expertise.
3. **Proactive offers** — short spoken yes/no prompts (e.g. "Read the tip?").

## Server layers

### Generic engine — `voice_agent.py` (domain-agnostic, reused unchanged)
- `stream_grounded(*, system, context, question, model, max_tokens, tools, operation, usage_log)` → **generator of event dicts**. Owns: Anthropic streaming, sentence chunking (`_SENTENCE_BOUNDARY`), tool/action detection, token journaling. **Answer XOR navigate**: if answer text is emitted, a trailing tool call is dropped (`emitted_text` guard) — models occasionally do both.
- `grounded(...)` → non-streaming `{"kind":"answer"|"action", ...}` over the same path.
- `sse(events)` → serialize event dicts to SSE `data:` frames.

**Event protocol:** `{"type":"sentence","text":...}` · `{"type":"action","name":<tool>,"input":{...}}` (terminal) · `{"type":"done"}`.

### Domain adapter — e.g. `cook_ask.py` (small, per-app)
Supplies ONLY the domain specifics and maps the generic action to the domain's shape:
- `CHEF_SYSTEM` (persona + expertise + "answer briefly, spoken prose" rules),
- `build_context(entity, pos)` → the grounding string,
- `_NAVIGATE_TOOL` + `_NAV_ACTIONS` (the nav vocabulary; `repeat` = re-read current unit, **never** "read the tip"),
- `ask_or_act_stream(entity, question, ...)` → wraps `voice_agent.stream_grounded`, re-emits `{"type":"action","action":...,"step":...}`.
- `ask()` / `ask_or_act()` non-streaming kept for the typed box.

### Endpoint — e.g. `POST /cook/ask-stream` (per-app)
Loads the entity, then `StreamingResponse(voice_agent.sse(adapter.ask_or_act_stream(...)), media_type="text/event-stream")`. Journals usage in the generator's `finally`. (Sync generator → Starlette runs it in a threadpool, so it won't block the event loop.) `POST /cook/ask` (non-stream, `allow_actions`) stays as the typed-box path + fallback.

## Client layer (cook.html today; extract to `voice-agent.js` next)
The hands-free loop is generic except for config. The streamed-answer player is already factored:
- `askChef(q)` → consumes the SSE stream; **`makeAnswerPlayer()`** plays sentences back-to-back as they arrive (each synthesized via the shared TTS cache, pipelined; bypasses `speak()` so sentences don't cut each other). `_streamSeq` cancels a sequence (bumped by `stopSpeaking`/new ask/nav). `finish()` mirrors `_onSpeakEnd` (opens the follow-up window, clears `_asking`).
- Falls back to non-streaming `askChefOnce()` if SSE is unavailable.

**Reuse follow-up (not yet done):** extract the whole loop into `VoiceAgent.mount(config)` where config = `{ wakeWords, persona, commands:[{name,re,act}], endpoints:{listen,speak,askStream,voiceLog}, onAction(action,step), proactiveOffer, contextNumber }`. Cooking passes recipe config; person/product pass theirs. Until then, cook.html is the reference implementation.

## Adding a new instance (checklist)
1. Write `<domain>_ask.py`: persona system prompt, `build_context(entity)`, nav tool + actions, `ask_or_act_stream` adapter over `voice_agent`.
2. Add `POST /<domain>/ask-stream` (+ non-stream) loading the entity.
3. Client: instantiate the voice loop config (wake word, command regexes → nav, `onAction`, proactive offers).
4. TTS/STT are shared (`/cook/speak`, `/cook/listen` today; per-event BYOK — see [[project_voice_redesign]]).

## Why cascaded + BYOK, not a managed platform
See `memory/project_voice_redesign.md`: per-minute platforms bill connected-session time incl. silence (wrong for long-idle sessions); our cascade pays per-event (cents/session). This engine keeps the transcript → deterministic nav + entity grounding. Murf is the planned per-char streaming TTS upgrade.
