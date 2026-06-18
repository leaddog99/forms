# LLM Gateway — a single choke point for every model call

> **Status:** DESIGN (2026-06-18). Approved direction: build after this doc is reviewed.
> **Goal:** route *all* LLM requests through one module so token/cost journaling is
> structural (automatic) rather than opt-in per call-site — trapping current and all
> future usage — and so retries/caching/rate-limiting/model-routing later have one home.
> **Principle:** this is `feedback_single_path` applied to model calls.

---

## 1. Why

Journaling today is **opt-in per call-site**: each module builds a `usage_log` by hand
(`build_usage_entry(...)`) and someone must remember to persist it (`_journal_usage(...)`).
That works for the extract/enrich/voice paths but **silently misses** any call that forgets —
which is exactly how **cook-rework + cook-augment** (our single most expensive op, Opus
~$0.45/recipe) ended up outside the central ledger, accounted only by a parallel `cook_costs`
summary stamped on `_cook.rework_cost`.

A choke point makes "every model call is journaled" an invariant of the architecture, not a
discipline. It also becomes the natural home for cross-cutting concerns we'll want later.

### Current surface (Phase-1 targets — Anthropic `.messages` text calls)
Each of these instantiates its **own** `anthropic.Anthropic()` and calls `.messages.create` or
`.messages.stream` directly:

`voice_agent.py` (stream) · `cook_ask.py` (create ×2) · `cook_rework.py` (create) ·
`cook_augment.py` (create) · `extract/markdown_to_recipe.py` (stream) ·
`extract/enrich_recipe.py` (stream) · `to_markdown/image_to_markdown.py` (stream) ·
`to_markdown/pdf_to_markdown.py` (stream) · `extract/dish_signal.py` (create) ·
`extract/chapter_classifier.py` (create) · `extract/domain_enrich.py` (create) ·
`extract/identity_card.py` (create) · `intake/translate.py` (create ×2) ·
`enrich/measurement/estimate.py` (create, **BYOK key**) ·
`enrich/measurement/recipe_pass.py` (create) · `recipe_anchor/pipeline.py` (create, likely legacy).

OpenAI (Phase 2): `cook_tts.py` (TTS) · `input/pipeline/embeddings.py` (embeddings) ·
`image_gen_openai.py` (image gen).

### Existing infra we reuse (do NOT reinvent)
- **`bcc_token_journal`** table + `write_usage_entries(conn, *, user_id, recipe_id, entries)` +
  `build_usage_entry(operation, model, response)` in `input/pipeline/token_journal.py`. This stays
  the persistence layer and schema; the gateway just always feeds it.
- **`_journal_usage(usage_log, *, recipe_id, user_id)`** in `save_recipe_api.py` (best-effort,
  own connection). The gateway absorbs this responsibility; the helper can remain as a thin shim
  during migration, then retire.
- **Cleanup to fold in:** there are **two** `build_usage_entry` implementations
  (`input/pipeline/token_journal.py` and `enrich/journal.py`). The gateway consolidates on the
  `token_journal` one; `enrich/journal.py`'s copy is deprecated.

---

## 2. Goals / non-goals

**Goals**
- One module every LLM call goes through; journaling is automatic and complete.
- Minimal call-site churn (mirror the SDK; no re-plumbing of deep signatures).
- Preserve everything that works today: streaming, forced tools, prompt caching, BYOK keys,
  per-operation attribution, batch (`user_id=0`) vs personal cost separation.
- Journal = single source of truth; `cook_costs`/`rework_cost` become *derived views*.

**Non-goals (now)**
- Retries / rate-limiting / caching / model-routing — the gateway is *designed to host* these
  later, but Phase 1 ships none of them (behavior-preserving).
- Replacing provider SDKs or changing models.
- Async rewrite (the codebase is sync; gateway is sync, threadpooled by Starlette as today).

---

## 3. The gateway

New module **`llm.py`** at the repo root (next to `cook_*.py`), provider-agnostic.

### 3.1 Public API (mirrors the SDK)

```python
# Non-streaming — wraps client.messages.create, returns the SAME response object.
resp = llm.create(operation="cook_rework", model=OPUS, max_tokens=16000,
                  system=..., messages=..., tools=..., tool_choice=...)

# Streaming — context manager wrapping client.messages.stream; captures the
# final message's usage on __exit__ and journals it (same trick voice_agent uses).
with llm.stream(operation="markdown_to_recipe", model=..., **kw) as s:
    for ev in s: ...
    final = s.get_final_message()
```

`create`/`stream` accept the full Anthropic kwargs verbatim (`system`, `messages`, `tools`,
`tool_choice`, `max_tokens`, `temperature`, …) and pass them through unchanged, so migrating a
call site is: `_client.messages.create(...)` → `llm.create(operation="…", …)`. Two required
additions per call: `operation=` (the journal label) and nothing else — context comes ambiently.

### 3.2 Context model — `contextvars`, not hand-threaded params

The journal needs `recipe_id` + `user_id`. Threading those through every function signature is the
current pain (and the source of the gap). Instead, set them **once at the request/job boundary**:

```python
with llm.context(recipe_id=new_recipe_id, user_id=user_id):
    recipe = markdown_to_recipe(md)        # any LLM calls inside are stamped + journaled
    _attach_identity_card(recipe)          # no usage_log plumbing needed
```

- Implemented with `contextvars.ContextVar` (async- and thread-safe; survives the Starlette
  threadpool hop). Nested `with llm.context(...)` overrides inner fields and restores on exit.
- A call made **outside** any context still works and still journals, with
  `user_id = PLACEHOLDER_USER_ID` (1) and `recipe_id = None` (matches today's defaults). Scripts/
  batch wrap their work in `llm.context(user_id=0, recipe_id=…)`.
- `operation` is **per-call** (one request issues many differently-labeled calls), not part of the
  ambient context.

### 3.3 Automatic journaling

- After `create` returns (or at `stream` exit), the gateway calls `build_usage_entry(operation,
  model, response)` and **buffers** the entry in the active context.
- On `llm.context(...)` **exit**, buffered entries are flushed via `write_usage_entries(conn, …)`
  in one transaction (one DB connection per request, not per call). A call with no surrounding
  context flushes immediately, best-effort (mirrors `_journal_usage` today).
- **Best-effort, never breaks the call:** a journal failure is logged and swallowed — the model
  response is always returned. (Same contract as `write_usage_entries`/`_journal_usage` today.)
- `meta` already captures cache-read/write + finish/stop reason via `usage.model_dump()`, so
  prompt-cache accounting is preserved for free.

### 3.4 Forced tools, streaming, BYOK

- **Forced tools** (cook_rework `emit_cook`, measurement `estimate_cup_weight`): just kwargs
  (`tools=`, `tool_choice=`) passed through. Usage is on the response regardless of tool use.
- **Streaming**: the `stream` context manager owns `get_final_message()` and journals its usage on
  exit; partial/aborted streams journal what the final message reports (or skip if none).
- **BYOK / per-tenant key**: `llm.create(..., api_key=...)` (and a context-level
  `llm.context(api_key=...)`) override the ambient env key. The gateway caches one client per
  distinct key. Keeps `enrich/measurement/estimate.py` and the portable BYOK story working.

### 3.5 `cook_costs` reconciliation

`bcc_token_journal` becomes the source of truth. `cook_costs.record()` (the in-memory accumulator)
retires for *journaling*; the human-facing rollup (`estimate`/`format_summary` → the rework log +
the new form metadata panel + `_cook.rework_cost`) is rebuilt as a **derivation over the journal
rows for that `recipe_id`** (or kept computing from the same per-call usage the gateway already
captures). Net: the rework cost panel keeps showing the same numbers, now backed by the ledger.

---

## 4. Scope phasing

- **Phase 1 — all Anthropic `.messages` text calls** (the ~16 modules above). Closes the gap,
  unifies the ledger, retires per-call-site `usage_log` plumbing. Behavior-preserving.
- **Phase 2 — OpenAI text + non-text** (`embeddings`, `cook_tts`, `image_gen_openai`). Different
  cost units (embedding tokens / characters / audio-seconds / images); same gateway, richer `meta`
  and a `units`/`cost_basis` field so the ledger can price them. TTS already has its own
  content-addressed cache (`media.db`) — the gateway journals the *synthesis* calls (cache misses).
- **Phase 3 (future, not scoped)** — retries, prompt-cache control, rate-limiting, model-routing,
  a spend dashboard / quota enforcement off the ledger.

---

## 5. Migration plan (incremental, gateway coexists with direct calls)

1. **Build `llm.py`** (`create`, `stream`, `context`, contextvars, buffered flush, BYOK client
   cache) + unit coverage on a stub/fake client.
2. **Migrate the gap first** — `cook_rework.py` + `cook_augment.py` → `llm.create(...)`, and wrap
   the cook-rework **job handler** in `llm.context(recipe_id, user_id)`. Immediately closes the
   accounting hole; proves the design end-to-end. Make `cook_costs` derive from the captured usage.
3. **Migrate request endpoints** — wrap each extract/enrich/voice endpoint body in
   `llm.context(...)`, swap its inner `_client.messages.*` for `llm.*`, and **delete the
   hand-threaded `usage_log` + `_journal_usage` call** for that path. Order by spend/risk:
   markdown_to_recipe → enrich_recipe → identity_card → measurement → translate → image/pdf →
   dish_signal/chapter_classifier/domain_enrich → cook_ask/voice_agent (streaming last).
4. **Retire shims** — once all paths are migrated, remove `_journal_usage`, the duplicate
   `enrich/journal.py:build_usage_entry`, and the per-module `anthropic.Anthropic()` singletons.
5. **Verify** — after each migration, confirm rows land in `bcc_token_journal` with the right
   `operation`/`model`/`recipe_id`/`user_id` (a quick `SELECT … ORDER BY id DESC` check), and that
   the rework cost panel + log still match.

---

## 6. Open questions / decisions to confirm during build

- **Module name/location:** `llm.py` at root (proposed) vs `input/pipeline/llm_gateway.py`
  (next to `token_journal.py`). Root is more discoverable for the many top-level `cook_*`/`extract`
  callers; pick at build time.
- **Flush granularity:** per-context (proposed, one txn/request) vs per-call immediate. Per-context
  is cheaper and atomic; the only risk is a long-running job buffering many entries before flush —
  mitigate with an explicit `llm.flush()` callable mid-job for the big batch loops.
- **Nested context semantics:** confirm field-level override + restore (recipe_id changes per item
  inside a batch that set user_id=0 once).
- **Calls with no usable `usage`** (provider hiccup): journal a zero-token row tagged in `meta`, or
  skip? Proposed: skip (don't pollute the ledger), but log at WARN.
- **`recipe_anchor/pipeline.py`**: confirm it's live or legacy before migrating (may be a
  prototype superseded by `cook_rework`).
- **Async future:** if any path goes async later, `contextvars` already propagates correctly;
  the gateway just needs `acreate`/`astream` twins.

---

## 7. Acceptance criteria (Phase 1)

- Every Anthropic text call in the listed modules flows through `llm.create`/`llm.stream`.
- No call site builds/threads `usage_log` or calls `_journal_usage` anymore.
- A cook-rework run produces rows in `bcc_token_journal` (operation `cook_rework` + `cook_augment`)
  attributable to the right recipe/user — verified by query.
- The rework cost log line, the form metadata panel, and `_cook.rework_cost` are unchanged to the
  user, now backed by the ledger.
- No regression in extract/enrich/voice behavior (streaming, forced tools, BYOK all intact).
