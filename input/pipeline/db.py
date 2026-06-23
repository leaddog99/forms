"""db.py — the single SQLite connection factory for the whole app.

WAL journal mode is set ON THE FILE once (save_recipe_api.init_db), so every
connection inherits it. The per-connection knob that does NOT persist is the
busy_timeout: without it a writer that can't immediately get the lock fails with
"database is locked" instead of waiting. The out-of-process harvest/cook jobs and
the server write concurrently (one WAL writer at a time), so a healthy default
busy-wait is essential — a publisher refresh saves a recipe every ~7s, and a
server-side save (add a domain, edit a recipe) that overlapped it used to die on
Python's stingy 5s default.

Route ALL connections through `connect()` so the timeout lives in ONE place
(was 120+ call sites each carrying their own `timeout=`). Same signature as
`sqlite3.connect` — a drop-in replacement.
"""
from __future__ import annotations

import sqlite3

DEFAULT_DB = "recipes.db"
# 30s: comfortably longer than any single write transaction (the slow part of a
# save — fetch + LLM — happens OUTSIDE the transaction), so concurrent writers
# wait-and-succeed rather than erroring. Bump here, everywhere, if contention grows.
BUSY_TIMEOUT_S = 30.0


def connect(db_path: str = DEFAULT_DB, *, timeout: float = BUSY_TIMEOUT_S,
            **kwargs) -> sqlite3.Connection:
    """`sqlite3.connect` with our standard busy_timeout. Python's `timeout` arg sets
    the SQLite busy handler (≡ `PRAGMA busy_timeout`), so a blocked writer waits up
    to `timeout` seconds for the lock instead of failing immediately. Extra kwargs
    pass straight through."""
    return sqlite3.connect(db_path, timeout=timeout, **kwargs)
