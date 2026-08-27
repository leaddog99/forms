"""llm.py — the single gateway every LLM call routes through.

Mirrors the Anthropic Messages SDK (`create` / `stream`) but:
  * stamps each call with an AMBIENT context (recipe_id, user_id, api_key) set once
    at a request/job boundary via `with llm.context(...)`, and
  * AUTO-JOURNALS token usage to `bcc_token_journal` (reusing token_journal.py),
so accounting is structural — not opt-in per call-site (the gap that let cook-rework
slip the ledger). See docs/llm-gateway.md.

Usage:

    with llm.context(recipe_id=rid, user_id=uid):          # set once at the boundary
        resp = llm.create(operation="cook_rework", model=OPUS,
                          max_tokens=16000, system=..., messages=..., tools=...)
        with llm.stream(operation="markdown_to_recipe", model=..., **kw) as s:
            for ev in s: ...
            final = s.get_final_message()

Calls made OUTSIDE any context still work and still journal (user_id=placeholder,
recipe_id=None) — best-effort, immediate. Journaling NEVER breaks a call: a journal
failure is logged and swallowed; the model response is always returned.

Phase 1 covers Anthropic `.messages` text calls. OpenAI text/embeddings/TTS/image
(different cost units) land in Phase 2 on the same gateway.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
import sqlite3
from input.pipeline.db import connect as db_connect
import threading
from typing import Any, Iterator, Optional

import anthropic

from input.pipeline.token_journal import build_usage_entry, write_usage_entries

# Same recipes.db the rest of the app journals into (DB_PATH in save_recipe_api).
# Overridable for tests via BCC_DB.
_DB_PATH = os.environ.get("BCC_DB", "recipes.db")
PLACEHOLDER_USER_ID = 1  # matches save_recipe_api.PLACEHOLDER_USER_ID until identity is wired


# --------------------------------------------------------------------------- #
# Ambient call context (contextvars — async- and thread-safe; survives the
# Starlette threadpool hop). Usage entries buffer here and flush in ONE
# transaction when the OWNING context exits.
# --------------------------------------------------------------------------- #
class _Ctx:
    __slots__ = ("recipe_id", "user_id", "api_key", "buffer")

    def __init__(self, recipe_id, user_id, api_key):
        self.recipe_id = recipe_id
        self.user_id = user_id
        self.api_key = api_key
        self.buffer: list[dict] = []


_current: "contextvars.ContextVar[Optional[_Ctx]]" = contextvars.ContextVar("llm_ctx", default=None)


@contextlib.contextmanager
def context(*, recipe_id: Optional[str] = None, user_id: Optional[int] = None,
            api_key: Optional[str] = None) -> Iterator[_Ctx]:
    """Stamp every LLM call inside this block with (recipe_id, user_id, api_key).
    Unspecified fields INHERIT from an enclosing context — so a batch can set
    user_id=0 once and each item override just recipe_id. Buffered usage flushes
    to the journal when this (owning) context exits."""
    parent = _current.get()
    ctx = _Ctx(
        recipe_id=recipe_id if recipe_id is not None else (parent.recipe_id if parent else None),
        user_id=user_id if user_id is not None else (parent.user_id if parent else None),
        api_key=api_key if api_key is not None else (parent.api_key if parent else None),
    )
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
        _flush(ctx)


def enter(*, recipe_id: Optional[str] = None, user_id: Optional[int] = None,
          api_key: Optional[str] = None) -> None:
    """Begin an ambient context WITHOUT a with-block — for large request handlers
    where wrapping the whole body is impractical. Inherits unspecified fields from
    any enclosing context. Pair with flush() at the handler's exit points (the
    existing _journal_usage sites already are those). No explicit reset needed:
    each request runs in its OWN copied contextvars context (Starlette task /
    anyio threadpool copy), so the set is discarded when the request ends and never
    leaks across requests. Calls inside still buffer + attribute correctly; the
    contextvar is shared by reference into asyncio.to_thread workers, so usage from
    a worker buffers onto the same context and flush() (back in the handler) writes
    it with this attribution."""
    parent = _current.get()
    ctx = _Ctx(
        recipe_id=recipe_id if recipe_id is not None else (parent.recipe_id if parent else None),
        user_id=user_id if user_id is not None else (parent.user_id if parent else None),
        api_key=api_key if api_key is not None else (parent.api_key if parent else None),
    )
    _current.set(ctx)


def flush() -> None:
    """Flush the active context's buffered entries mid-run — for long batch loops
    that would otherwise hold many entries until the outer context closes, and for
    enter()-style handlers that flush at their existing exit points."""
    ctx = _current.get()
    if ctx is not None:
        _flush(ctx)


def _flush(ctx: _Ctx) -> None:
    if not ctx.buffer:
        return
    entries, ctx.buffer = ctx.buffer, []
    uid = ctx.user_id if ctx.user_id is not None else PLACEHOLDER_USER_ID
    try:
        with db_connect(_DB_PATH) as conn:
            write_usage_entries(conn, user_id=uid, recipe_id=ctx.recipe_id, entries=entries)
    except Exception as e:  # noqa: BLE001 — journaling must never break the caller
        print(f"[llm] journal flush failed: {e}")


def _journal(operation: str, model: str, response: Any) -> None:
    """Buffer one usage entry on the active context, else write it immediately."""
    try:
        entry = build_usage_entry(operation, model, response)
    except Exception as e:  # noqa: BLE001
        print(f"[llm] build_usage_entry failed: {e}")
        return
    ctx = _current.get()
    if ctx is not None:
        ctx.buffer.append(entry)
        return
    try:
        with db_connect(_DB_PATH) as conn:
            write_usage_entries(conn, user_id=PLACEHOLDER_USER_ID, recipe_id=None, entries=[entry])
    except Exception as e:  # noqa: BLE001
        print(f"[llm] immediate journal failed: {e}")


# --------------------------------------------------------------------------- #
# Client cache — one Anthropic client per distinct api_key (incl. the ambient
# env key under "__env__"). Keeps BYOK per-tenant keys working without churning
# a new client per call.
# --------------------------------------------------------------------------- #
_clients: dict[str, anthropic.Anthropic] = {}
_clients_lock = threading.Lock()


def _resolve_key(api_key: Optional[str]) -> Optional[str]:
    if api_key is not None:
        return api_key
    ctx = _current.get()
    return ctx.api_key if ctx is not None else None


def _client(api_key: Optional[str]) -> anthropic.Anthropic:
    key = api_key or "__env__"
    cli = _clients.get(key)
    if cli is None:
        with _clients_lock:
            cli = _clients.get(key)
            if cli is None:
                cli = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
                _clients[key] = cli
    return cli


# --------------------------------------------------------------------------- #
# The wrapped calls — pass SDK kwargs through verbatim, return the SAME objects.
# --------------------------------------------------------------------------- #
def create(*, operation: str, model: str, api_key: Optional[str] = None, **kwargs) -> Any:
    """Wrap `client.messages.create`; journal usage; return the SAME response.
    `operation` is the journal label; all other kwargs (system/messages/max_tokens/
    tools/tool_choice/temperature/...) pass through unchanged."""
    resp = _client(_resolve_key(api_key)).messages.create(model=model, **kwargs)
    _journal(operation, model, resp)
    return resp


@contextlib.contextmanager
def stream(*, operation: str, model: str, api_key: Optional[str] = None, **kwargs) -> Iterator[Any]:
    """Wrap `client.messages.stream`; yield the SDK stream object (use .text_stream
    / iterate / .get_final_message()). Journals the FINAL message's usage on exit —
    so callers don't have to. Best-effort on aborted/partial streams."""
    with _client(_resolve_key(api_key)).messages.stream(model=model, **kwargs) as s:
        try:
            yield s
        finally:
            final = None
            try:
                final = s.get_final_message()
            except Exception:  # noqa: BLE001 — aborted/partial stream
                final = None
            if final is not None:
                _journal(operation, model, final)
