"""cook_chat.py — the persistent Chef conversation ledger.

Every Ask-Chef exchange (cook view typed/voice, Chef's-Notes chat) is one pair of
rows here: the cook's question and Chef's answer, keyed by recipe + user. Two jobs:

1. MEMORY — `recent_history()` returns the last N exchanges for a recipe+user so
   `cook_ask` passes them as real conversation turns. Before this, every ask was
   an isolated single-turn call ("the fact that it has no memory of previous
   chats is problematic" — curator, 2026-09-03, the John's Pesto incident).
2. AUDIT — when Chef gives a wrong or disputed answer, the exchange is on record
   (the pesto "insisted the recipe said shredded" answer was unrecoverable: the
   token journal had the counts, nobody had the words).

Personal, on-device content (the split's local 'possess & use' side) — never
flows to the corpus. Callers own the connection (the server's _db() or a job's);
this module is pure SQL helpers, no path knowledge.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import List

# How many prior exchanges ride along on each ask. Curator-set (2026-09-03):
# "include the last 5 chats… token cost i'm aware of."
HISTORY_EXCHANGES = 5


def ensure_table(conn: sqlite3.Connection) -> None:
    """Idempotent DDL — called from init_db() alongside the other tables."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cook_chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            surface TEXT NOT NULL,            -- 'cook' | 'cook-voice' | 'notes'
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cook_chat_recipe_user
        ON cook_chat(recipe_id, user_id, id);
    """)


def record_exchange(conn: sqlite3.Connection, *, recipe_id: str, user_id: int,
                    surface: str, question: str, answer: str) -> None:
    """Persist one Q/A pair. Caller commits (or relies on its context manager)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO cook_chat (recipe_id, user_id, surface, role, text, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(recipe_id, user_id, surface, "user", question, now),
         (recipe_id, user_id, surface, "assistant", answer, now)],
    )


def recent_history(conn: sqlite3.Connection, *, recipe_id: str, user_id: int,
                   exchanges: int = HISTORY_EXCHANGES) -> List[dict]:
    """The last `exchanges` Q/A pairs for this recipe+user, OLDEST FIRST, as
    [{'role': 'user'|'assistant', 'content': text}, …] ready for the messages
    array. Pulls across surfaces on purpose — a question asked from the notes
    chat is context the cook view should remember too."""
    rows = conn.execute(
        "SELECT role, text FROM cook_chat WHERE recipe_id = ? AND user_id = ? "
        "ORDER BY id DESC LIMIT ?",
        (recipe_id, user_id, exchanges * 2),
    ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
