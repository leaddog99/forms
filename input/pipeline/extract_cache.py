"""LLM extract cache — keyed by (URL, markdown content hash, model, prompt).

Stage B (markdown -> recipe via LLM) is expensive (~25 s, ~$0.0015 per call)
and stable: the same input markdown + system prompt + model produces the
same output. We cache the LLM's JSON output keyed by:

    url_normalized   canonical URL form (normalize_url())
    markdown_hash    sha256 of cleaned_markdown (the actual LLM input bytes)
    model            model id, e.g. 'gpt-4o-mini'
    prompt_version   sha256(SYSTEM_PROMPT)[:12] — auto, no manual bumping

Hash-based invalidation, not TTL. If the page changes, the markdown hash
changes. If the prompt is tweaked, prompt_version changes. Either way the
old entry naturally misses; no policy required.

Cache hits should get journaled as 'cache_hit_markdown_to_recipe' with
zero tokens so future per-user usage queries can surface a "tokens
saved" number alongside actual spend.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


def ensure_llm_extract_cache_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_extract_cache (
            url_normalized   TEXT NOT NULL,
            markdown_hash    TEXT NOT NULL,
            model            TEXT NOT NULL,
            prompt_version   TEXT NOT NULL,
            recipe_json      TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            last_used_at     TEXT NOT NULL,
            hit_count        INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (url_normalized, markdown_hash, model, prompt_version)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_extract_cache_url ON llm_extract_cache(url_normalized)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_extract_cache_last_used ON llm_extract_cache(last_used_at)")
    conn.commit()


def hash_text(text: str) -> str:
    """sha256 hex of UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prompt_version_for(prompt: str) -> str:
    """12-char prefix of sha256(prompt). Caller never has to remember to bump
    a version constant when the prompt changes — the hash does it."""
    return hash_text(prompt)[:12]


def get_cached_extract(
    db_path: str,
    *,
    url_normalized: str,
    markdown_hash: str,
    model: str,
    prompt_version: str,
) -> Optional[dict]:
    """Return cached LLM JSON for this cache key, or None on miss. Updates
    last_used_at + hit_count on hit. Never raises — returns None on any
    error so callers fall through to the LLM."""
    if not url_normalized:
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            ensure_llm_extract_cache_table(conn)
            row = conn.execute(
                """SELECT recipe_json, created_at FROM llm_extract_cache
                   WHERE url_normalized = ? AND markdown_hash = ?
                   AND model = ? AND prompt_version = ?""",
                (url_normalized, markdown_hash, model, prompt_version),
            ).fetchone()
            if not row:
                return None
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """UPDATE llm_extract_cache
                   SET last_used_at = ?, hit_count = hit_count + 1
                   WHERE url_normalized = ? AND markdown_hash = ?
                   AND model = ? AND prompt_version = ?""",
                (now, url_normalized, markdown_hash, model, prompt_version),
            )
            conn.commit()
            return {
                "llm_output": json.loads(row[0]),
                "cached_at": row[1],
            }
    except Exception as e:
        print(f"[WARN] llm_extract_cache lookup failed: {e}")
        return None


def set_cached_extract(
    db_path: str,
    *,
    url_normalized: str,
    markdown_hash: str,
    model: str,
    prompt_version: str,
    llm_output: dict,
) -> None:
    """Store the LLM's raw JSON output (post json.loads, pre-sanitize) under
    this cache key. Never raises."""
    if not url_normalized:
        return
    try:
        with sqlite3.connect(db_path) as conn:
            ensure_llm_extract_cache_table(conn)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO llm_extract_cache
                     (url_normalized, markdown_hash, model, prompt_version,
                      recipe_json, created_at, last_used_at, hit_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(url_normalized, markdown_hash, model, prompt_version)
                   DO UPDATE SET
                     recipe_json = excluded.recipe_json,
                     last_used_at = excluded.last_used_at""",
                (url_normalized, markdown_hash, model, prompt_version,
                 json.dumps(llm_output), now, now),
            )
            conn.commit()
    except Exception as e:
        print(f"[WARN] llm_extract_cache write failed: {e}")