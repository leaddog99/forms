"""cook_kb.py — the cooking tips/checks KNOWLEDGE BASE (the durable, curated asset).

The moat. A DB-resident, human-curated set of cooking success-TIPS and failure-mode
CHECKS, each written in OUR words (cooking technique is uncopyrightable fact; only the
expression is — so every entry is authored from scratch, never reproduced from a
source). At rework time the augment pass (3c-b) may only SELECT from PUBLISHED entries
and reword them for the recipe — it can never invent advice; every attachment carries
the entry's `id` (provenance), mechanically validated. See
recipe_anchor/phase3-pipeline-design.md + the augment design.

Curated/runtime split (same as system_config, the ingredient optimizer): editors write
DRAFTS here; you PUBLISH; the runtime model only ever sees the projected published set.
`editor_note` / `confidence` / `exemplar_recipes` are server-side bookkeeping — projected
OUT before the prompt, never shown to the model or the cook.

SEED: the canonical entries live in the git-tracked `cook_kb_seed.json` beside this module
(NOT a Python constant — no-data-in-code). That file both bootstraps a FRESH install and
is the KB's version-controlled backup. It seeds only an EMPTY table, so curation
(publish/edit/delete) is never clobbered or resurrected on restart.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional


# Controlled technique vocab (the coarse selection key). Extensible — log additions
# rather than silently inventing tags, then fold them in here.
TECHNIQUE_VOCAB = [
    # dry heat
    "sear", "saute", "pan_fry", "deep_fry", "stir_fry", "roast", "broil", "grill", "smoke", "flambe",
    "toast", "bloom_spices", "grind_spices", "grind_meat", "caramelize", "sweat", "reduce", "deglaze", "pan_sauce", "glaze",
    # wet heat
    "boil", "simmer", "poach", "blanch", "steam", "braise", "stew", "pickle", "ferment",
    # transform
    "knead", "fold", "cream", "whip", "emulsify", "temper_eggs", "temper_chocolate",
    "brine", "salt_ahead", "marinate", "proof", "rest_dough", "mix_gently", "bread_coat",
    "infuse", "curdle",
    # baking
    "bake", "leaven", "laminate",
    # preserve / specialty
    "dry_age", "cure", "confit", "freeze",
    # finish
    "rest_meat", "carryover", "thicken", "mount_butter", "season_to_taste", "finish",
    # situational
    "mise_en_place", "doneness", "preheat", "pan_temp", "salting", "salt_vegetables", "knife_skills", "shave",
]

KINDS = ("tip", "check")
SCOPES = ("step", "recipe", "either")
CONFIDENCES = ("high", "medium", "low")
STATUSES = ("draft", "published")

# JSON-encoded list/array columns.
_LIST_COLS = ("technique_tags", "trigger_signals", "ingredient_classes",
              "equipment", "variants", "exemplar_recipes")
_ALL_COLS = ("id, kind, title, technique_tags, trigger_signals, ingredient_classes, "
             "equipment, scope, claim, action, signal, failure_mode, variants, "
             "exemplar_recipes, mechanism, render_hint, confidence, editor_note, status, "
             "created_at, updated_at")

# Canonical seed — git-tracked, the backup + fresh-install bootstrap.
_SEED_FILE = os.path.join(os.path.dirname(__file__), "cook_kb_seed.json")


def ensure_cook_kb_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cook_tips_kb (
            id                 TEXT PRIMARY KEY,
            kind               TEXT NOT NULL DEFAULT 'tip',      -- tip | check
            title              TEXT,
            technique_tags     TEXT,                            -- JSON array
            trigger_signals    TEXT,                            -- JSON array
            ingredient_classes TEXT,                            -- JSON array
            equipment          TEXT,                            -- JSON array
            scope              TEXT NOT NULL DEFAULT 'step',     -- step | recipe | either
            claim              TEXT,
            action             TEXT,
            signal             TEXT,                            -- check-only
            failure_mode       TEXT,                            -- check-only
            variants           TEXT,                            -- JSON [{case, note}]
            exemplar_recipes   TEXT,                            -- JSON; internal QA, never sent
            mechanism          TEXT,                            -- grounding only, not rendered
            render_hint        TEXT,
            confidence         TEXT NOT NULL DEFAULT 'high',     -- high | medium | low
            editor_note        TEXT,                            -- internal; never sent to LLM
            status             TEXT NOT NULL DEFAULT 'draft',    -- draft | published
            created_at         TEXT,
            updated_at         TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cook_kb_status ON cook_tips_kb(status)")
    # Migration for tables created before exemplar_recipes existed.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cook_tips_kb)")}
    if "exemplar_recipes" not in cols:
        conn.execute("ALTER TABLE cook_tips_kb ADD COLUMN exemplar_recipes TEXT")
    # Seed a FRESH (empty) table from the git-tracked seed file — only on empty, so
    # curation is never clobbered or resurrected.
    if conn.execute("SELECT COUNT(*) FROM cook_tips_kb").fetchone()[0] == 0:
        _seed_from_file(conn)
    conn.commit()


def _seed_from_file(conn: sqlite3.Connection) -> int:
    """Import cook_kb_seed.json into a fresh table. Each entry seeds with its own
    `status` (default 'draft'); source_note maps to editor_note. Missing/empty -> 0."""
    try:
        with open(_SEED_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (FileNotFoundError, ValueError):
        return 0
    if not isinstance(entries, list):
        return 0
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for e in entries:
        if not e.get("id"):
            continue
        e = {**e, "editor_note": e.get("editor_note") or e.get("source_note"),
             "status": e.get("status", "draft")}
        _insert(conn, e, now)
        n += 1
    return n


def _insert(conn: sqlite3.Connection, e: dict, now: str) -> None:
    conn.execute(
        f"INSERT INTO cook_tips_kb ({_ALL_COLS}) VALUES "
        f"({','.join(['?'] * len(_ALL_COLS.split(',')))})",
        (
            e["id"], e.get("kind", "tip"), e.get("title"),
            json.dumps(e.get("technique_tags") or []),
            json.dumps(e.get("trigger_signals") or []),
            json.dumps(e.get("ingredient_classes") or []),
            json.dumps(e.get("equipment") or []),
            e.get("scope", "step"), e.get("claim"), e.get("action"),
            e.get("signal"), e.get("failure_mode"),
            json.dumps(e.get("variants") or []),
            json.dumps(e.get("exemplar_recipes") or []),
            e.get("mechanism"), e.get("render_hint"),
            e.get("confidence", "high"), e.get("editor_note"),
            e.get("status", "draft"), now, now,
        ),
    )


def _row_to_dict(row: tuple) -> dict:
    d = dict(zip([c.strip() for c in _ALL_COLS.split(",")], row))
    for c in _LIST_COLS:
        try:
            d[c] = json.loads(d[c]) if d[c] else []
        except Exception:
            d[c] = []
    return d


def list_kb(conn: sqlite3.Connection, status: Optional[str] = None) -> list[dict]:
    if status:
        rows = conn.execute(f"SELECT {_ALL_COLS} FROM cook_tips_kb WHERE status = ? "
                            f"ORDER BY kind, id", (status,)).fetchall()
    else:
        rows = conn.execute(f"SELECT {_ALL_COLS} FROM cook_tips_kb ORDER BY status, kind, id").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_kb(conn: sqlite3.Connection, kb_id: str) -> Optional[dict]:
    row = conn.execute(f"SELECT {_ALL_COLS} FROM cook_tips_kb WHERE id = ?", (kb_id,)).fetchone()
    return _row_to_dict(row) if row else None


def upsert_kb(conn: sqlite3.Connection, kb_id: str, fields: dict) -> dict:
    if (fields.get("kind") or "tip") not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if (fields.get("scope") or "step") not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    now = datetime.now(timezone.utc).isoformat()
    existing = get_kb(conn, kb_id)
    merged = {**(existing or {}), **fields, "id": kb_id}
    if existing:
        sets, params = [], []
        cols = ("kind", "title", "scope", "claim", "action", "signal", "failure_mode",
                "mechanism", "render_hint", "confidence", "editor_note", "status")
        for c in cols:
            if c in fields:
                sets.append(f"{c} = ?"); params.append(fields[c])
        for c in _LIST_COLS:
            if c in fields:
                sets.append(f"{c} = ?"); params.append(json.dumps(fields[c] or []))
        sets.append("updated_at = ?"); params.append(now)
        params.append(kb_id)
        conn.execute(f"UPDATE cook_tips_kb SET {', '.join(sets)} WHERE id = ?", params)
    else:
        _insert(conn, merged, now)
    conn.commit()
    return get_kb(conn, kb_id)


def delete_kb(conn: sqlite3.Connection, kb_id: str) -> bool:
    cur = conn.execute("DELETE FROM cook_tips_kb WHERE id = ?", (kb_id,))
    conn.commit()
    return cur.rowcount > 0


def set_status(conn: sqlite3.Connection, kb_id: str, status: str) -> Optional[dict]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    conn.execute("UPDATE cook_tips_kb SET status = ?, updated_at = ? WHERE id = ?",
                 (status, datetime.now(timezone.utc).isoformat(), kb_id))
    conn.commit()
    return get_kb(conn, kb_id)


def import_drafts(conn: sqlite3.Connection, entries: list[dict],
                  overwrite: bool = False) -> dict:
    """Bulk-import authored entries as DRAFTS (the KB is the moat; entries get
    reviewed before publish). Default is IDEMPOTENT: an existing id is SKIPPED.
    With overwrite=True an existing id is UPDATED in place (content replaced) while
    its existing STATUS is PRESERVED — so a published entry isn't silently
    un-reviewed; use to backfill a corrected source file (e.g. add exemplars).
    Accepts desktop's `source_note` (mapped to editor_note). Returns
    {imported, updated, skipped}."""
    now = datetime.now(timezone.utc).isoformat()
    existing = {r[0] for r in conn.execute("SELECT id FROM cook_tips_kb")}
    imported = updated = skipped = 0
    for e in entries:
        if not e.get("id"):
            skipped += 1; continue
        e = {**e, "editor_note": e.get("editor_note") or e.get("source_note")}
        if e["id"] in existing:
            if overwrite:
                fields = {k: v for k, v in e.items() if k not in ("id", "status", "source_note")}
                upsert_kb(conn, e["id"], fields)   # content updated, status kept
                updated += 1
            else:
                skipped += 1
            continue
        _insert(conn, {**e, "status": "draft"}, now)
        existing.add(e["id"]); imported += 1
    conn.commit()
    return {"imported": imported, "updated": updated, "skipped": skipped}


def project_published(conn: sqlite3.Connection) -> list[dict]:
    """The runtime view for the augment pass (3c-b): PUBLISHED entries only, with
    just the selection + generation fields — editor_note / confidence /
    exemplar_recipes are DROPPED before the model ever sees them (rejoined
    server-side by id). Stable, key-ordered output so it can be a cacheable prefix."""
    out = []
    for e in list_kb(conn, status="published"):
        out.append({
            "id": e["id"], "kind": e["kind"], "title": e["title"],
            "applies_when": {
                "technique_tags": e["technique_tags"], "trigger_signals": e["trigger_signals"],
                "ingredient_classes": e["ingredient_classes"], "equipment": e["equipment"],
                "scope": e["scope"],
            },
            "knowledge": {
                "mechanism": e.get("mechanism"), "claim": e.get("claim"),
                "action": e.get("action"), "signal": e.get("signal"),
                "failure_mode": e.get("failure_mode"), "variants": e.get("variants"),
            },
            "render_hint": e.get("render_hint"),
        })
    return out
