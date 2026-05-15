"""Token usage journal — one row per LLM call.

Captured right after each `openai.chat.completions.create` returns, before
the user saves (or even decides to save) the recipe. Source of truth for
future billing/quota work; the planned "general ledger" can aggregate
from here.

Schema (`bcc_token_journal` table):

    id            INTEGER PRIMARY KEY AUTOINCREMENT  sequential PK (cheap append)
    user_id       INTEGER NOT NULL                   placeholder 1 until identity wired
    recipe_id     TEXT                               app-minted UUID; known at extract time
    operation     TEXT NOT NULL                      e.g. 'markdown_to_recipe'
    model         TEXT                               e.g. 'gpt-4o-mini'
    input_tokens  INTEGER NOT NULL DEFAULT 0         == response.usage.prompt_tokens
    output_tokens INTEGER NOT NULL DEFAULT 0         == response.usage.completion_tokens
    created_at    TEXT NOT NULL                      ISO-8601 UTC
    meta          TEXT                               JSON: full usage dict + finish_reason etc.

Hyphenated original concept name is `bcc-token-journal`; underscored here
so the table name doesn't need quoting everywhere it's used.

The integer PK is sequential so inserts always land at the right edge of
the B-tree (cheap append). `recipe_id` is the app-minted UUID that ties
the journal row back to the eventual recipe — present *before* the
recipe is saved, so we can log token cost even for extractions the user
abandons.
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


def ensure_bcc_token_journal_table(conn: sqlite3.Connection) -> None:
    # One-time migration: if a legacy TEXT-PK version of this table exists
    # (from earlier in this dev cycle), drop it. The app didn't ship; only
    # transient dev rows live there.
    info = conn.execute("PRAGMA table_info(bcc_token_journal)").fetchall()
    if info:
        id_col = next((row for row in info if row[1] == "id"), None)
        if id_col and id_col[2].upper() != "INTEGER":
            conn.execute("DROP TABLE bcc_token_journal")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bcc_token_journal (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            recipe_id     TEXT,
            operation     TEXT NOT NULL,
            model         TEXT,
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            meta          TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bcc_token_journal_user ON bcc_token_journal(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bcc_token_journal_recipe ON bcc_token_journal(recipe_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bcc_token_journal_created ON bcc_token_journal(created_at)")
    conn.commit()


def build_usage_entry(
    operation: str,
    model: str,
    response: Any,
) -> dict:
    """Pull token counts + interesting extras off an OpenAI ChatCompletion
    response. Safe against partial/missing fields — returns zeros rather
    than raising if `response.usage` is absent."""
    usage = getattr(response, "usage", None)
    prompt_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
    completion_tokens = getattr(usage, "completion_tokens", 0) if usage else 0

    # Anything beyond raw counts goes into meta. dump() handles cached_tokens,
    # reasoning_tokens, etc. cleanly for both regular and reasoning models.
    meta: dict[str, Any] = {}
    if usage is not None:
        try:
            meta["usage"] = usage.model_dump()
        except Exception:
            meta["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
    fp = getattr(response, "system_fingerprint", None)
    if fp:
        meta["system_fingerprint"] = fp
    rid = getattr(response, "id", None)
    if rid:
        meta["response_id"] = rid
    try:
        meta["finish_reason"] = response.choices[0].finish_reason
    except Exception:
        pass

    return {
        "operation": operation,
        "model": model,
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "meta": meta,
    }


def write_usage_entries(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    recipe_id: Optional[str],
    entries: list[dict],
) -> int:
    """Insert one row per entry. The integer PK auto-assigns at write time;
    callers don't pre-mint it. Never raises — logs and continues so journal
    failures don't break extraction flow. Returns rows written."""
    if not entries:
        return 0
    ensure_bcc_token_journal_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for entry in entries:
        try:
            conn.execute(
                """
                INSERT INTO bcc_token_journal
                    (user_id, recipe_id, operation, model, input_tokens, output_tokens, created_at, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    recipe_id,
                    entry.get("operation", ""),
                    entry.get("model"),
                    int(entry.get("input_tokens") or 0),
                    int(entry.get("output_tokens") or 0),
                    now,
                    json.dumps(entry.get("meta") or {}),
                ),
            )
            written += 1
        except Exception as e:
            print(f"[WARN] bcc_token_journal write failed: {e}")
    conn.commit()
    return written