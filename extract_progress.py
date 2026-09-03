"""extract_progress.py — live phase/progress registry for extraction requests.

The bookmarklet → form extract is a single long POST (~25-40s on a no-JSON-LD
page, dominated by the streamed markdown_to_recipe call). The form used to fill
that wait with rotating novelty messages only; the curator asked for the real
thing: "real status updates… maybe even a progress bar" (2026-09-03).

In-memory + process-local by design: the extract runs INSIDE the server process
(threadpool), so the registry never needs to cross processes. The client mints a
token, passes it as `progress_token` on the extract POST, and polls
GET /extract-progress/{token} (~1/s) while the POST is in flight.

Phases are machine names; the form owns the human wording. `pct` is overall
0..1 across the whole request — the extract phase maps the LLM's streamed
output chars onto its slice, so the bar moves smoothly through the long call
instead of sitting at a phase boundary.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

_LOCK = threading.Lock()
_STATE: dict = {}          # token -> {phase, pct, detail, updated_at, done}
_TTL_S = 15 * 60           # forgotten tokens evaporate (client crashed mid-poll)


def _prune_locked(now: float) -> None:
    dead = [t for t, s in _STATE.items() if now - s["updated_at"] > _TTL_S]
    for t in dead:
        _STATE.pop(t, None)


def update(token: str, phase: str, pct: float,
           detail: Optional[str] = None, *, done: bool = False) -> None:
    """Stamp the current phase. No-op on a blank token so call sites don't
    need to guard (an extract without a polling client passes '')."""
    if not token:
        return
    now = time.time()
    with _LOCK:
        _prune_locked(now)
        prev = _STATE.get(token)
        # pct never moves backwards — a repair/retry inside a phase should not
        # visibly rewind the bar.
        if prev and not done:
            pct = max(pct, prev["pct"])
        _STATE[token] = {
            "phase": phase,
            "pct": round(min(max(pct, 0.0), 1.0), 4),
            "detail": detail,
            "updated_at": now,
            "done": done,
        }


def finish(token: str, *, ok: bool = True) -> None:
    update(token, "done" if ok else "error", 1.0, done=True)


def get(token: str) -> Optional[dict]:
    with _LOCK:
        s = _STATE.get(token)
        return dict(s) if s else None
