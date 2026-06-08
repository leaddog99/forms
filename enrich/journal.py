"""Enrich's own token journal — LLM cost accounting for the enrichment product,
kept in Enrich's DB (enrich.db), separate from BCC's bcc_token_journal.

Enrich's LLM calls (measurement estimates today; the measure fallback and future
enrich blocks next) are the *product's* costs — they belong with Enrich, not in a
customer's ledger. When the API splits to its own process the journal travels with
it. Self-contained: own usage-entry builder (Anthropic + defensive OpenAI), own
table, own writer. Never raises — journaling must not break an LLM feature.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from .db import connect

_DDL = """
CREATE TABLE IF NOT EXISTS enrich_token_journal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    operation     TEXT NOT NULL,          -- measure_estimate | measure_fallback | ...
    model         TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    context       TEXT,                    -- free-form: what was measured/estimated
    meta          TEXT,                    -- JSON: raw usage, response id, finish reason
    created_at    TEXT NOT NULL
);
"""


def ensure_schema() -> None:
    with connect() as conn:
        conn.executescript(_DDL)


def build_usage_entry(operation: str, model: str, response: Any) -> dict:
    """Token counts + extras off a provider response. Anthropic shape
    (usage.input_tokens/output_tokens, stop_reason); defensively handles the
    OpenAI shape too. Returns zeros rather than raising on missing fields."""
    usage = getattr(response, "usage", None)
    prompt = getattr(usage, "prompt_tokens", None) if usage else None
    completion = getattr(usage, "completion_tokens", None) if usage else None
    if prompt is None and usage is not None:
        prompt = getattr(usage, "input_tokens", 0)
    if completion is None and usage is not None:
        completion = getattr(usage, "output_tokens", 0)
    prompt = prompt or 0
    completion = completion or 0

    meta: dict[str, Any] = {}
    if usage is not None:
        try:
            meta["usage"] = usage.model_dump()
        except Exception:
            meta["usage"] = {"prompt_tokens": prompt, "completion_tokens": completion}
    rid = getattr(response, "id", None)
    if rid:
        meta["response_id"] = rid
    stop = getattr(response, "stop_reason", None)
    if stop:
        meta["finish_reason"] = stop
    return {"operation": operation, "model": model,
            "input_tokens": prompt, "output_tokens": completion, "meta": meta}


def write_usage(entries: list[dict], *, context: Optional[str] = None) -> int:
    """Persist usage entries to Enrich's journal. Returns rows written."""
    if not entries:
        return 0
    ensure_schema()
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    with connect() as conn:
        for e in entries:
            try:
                conn.execute(
                    "INSERT INTO enrich_token_journal "
                    "(operation, model, input_tokens, output_tokens, context, meta, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (e.get("operation"), e.get("model"),
                     int(e.get("input_tokens", 0) or 0), int(e.get("output_tokens", 0) or 0),
                     context, json.dumps(e.get("meta") or {}), now),
                )
                written += 1
            except Exception as ex:
                print(f"[WARN] enrich journal write failed: {ex}")
    return written


def totals() -> dict:
    """Roll-up for a quick admin view: rows + tokens per operation."""
    ensure_schema()
    with connect() as conn:
        rows = conn.execute(
            "SELECT operation, COUNT(*) n, "
            "SUM(input_tokens) inp, SUM(output_tokens) outp "
            "FROM enrich_token_journal GROUP BY operation ORDER BY n DESC"
        ).fetchall()
    return {"by_operation": [dict(r) for r in rows]}
