"""cook_kb.py — the cooking tips/checks KNOWLEDGE BASE (the durable, curated asset).

The moat. A DB-resident, human-curated set of cooking success-TIPS and failure-mode
CHECKS, each written in OUR words (cooking technique is uncopyrightable fact; only the
expression is — so every entry is authored from scratch, never reproduced from a
source). At rework time the augment pass (3c-b) may only SELECT from PUBLISHED entries
and reword them for the recipe — it can never invent advice; every attachment carries
the entry's `id` (provenance), mechanically validated. See
recipe_anchor/phase3-pipeline-design.md + the augment design.

Curated/runtime split (same as system_config, the ingredient optimizer): editors write
DRAFTS here; you PUBLISH a byte-stable snapshot; the runtime model only ever sees the
projected published set. `editor_note` / `confidence` are server-side bookkeeping —
projected OUT before the prompt, never shown to the model or the cook.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Optional


# Controlled technique vocab (the coarse selection key). Extensible — log additions
# rather than silently inventing tags, then fold them in here.
TECHNIQUE_VOCAB = [
    # dry heat
    "sear", "saute", "pan_fry", "deep_fry", "stir_fry", "roast", "broil", "grill",
    "toast", "bloom_spices", "caramelize", "sweat", "reduce", "deglaze", "pan_sauce",
    # wet heat
    "boil", "simmer", "poach", "blanch", "steam", "braise", "stew", "pickle",
    # transform
    "knead", "fold", "cream", "whip", "emulsify", "temper_eggs", "temper_chocolate",
    "brine", "salt_ahead", "marinate", "proof", "rest_dough", "mix_gently",
    # finish
    "rest_meat", "carryover", "thicken", "mount_butter", "season_to_taste", "finish",
    # situational
    "mise_en_place", "doneness", "preheat", "pan_temp", "salting", "salt_vegetables",
]

KINDS = ("tip", "check")
SCOPES = ("step", "recipe", "either")
CONFIDENCES = ("high", "medium", "low")
STATUSES = ("draft", "published")

# JSON-encoded list/array columns.
_LIST_COLS = ("technique_tags", "trigger_signals", "ingredient_classes",
              "equipment", "variants")
_ALL_COLS = ("id, kind, title, technique_tags, trigger_signals, ingredient_classes, "
             "equipment, scope, claim, action, signal, failure_mode, variants, "
             "mechanism, render_hint, confidence, editor_note, status, "
             "created_at, updated_at")


# A few exemplar entries in the FULL schema — they seed the table + serve as the
# format reference for the bulk-authored batch. Real volume comes via import +
# curation, NOT this constant (no-data-in-code: this is bootstrap only).
SEED_ENTRIES: list[dict] = [
    {
        "id": "sear_dont_crowd", "kind": "check",
        "title": "Crowding the pan steams instead of sears",
        "technique_tags": ["sear", "saute", "pan_fry"],
        "trigger_signals": ["add to hot pan", "brown", "in batches", "sear"],
        "ingredient_classes": ["meat", "poultry", "vegetables"], "equipment": ["skillet", "saute_pan"],
        "scope": "step",
        "claim": "A crowded pan drops in temperature and the food releases water faster than it can evaporate, so it steams instead of browning.",
        "action": "Leave space between pieces; cook in batches and don't move them until they release.",
        "signal": "Liquid pooling in the pan, no active sizzle, a pale surface.",
        "failure_mode": "Food turns gray and grainy instead of forming a crust.",
        "variants": [], "mechanism": "A hot surface evaporates surface moisture fast enough for the Maillard browning reactions to start; too much cold food at once stalls that.",
        "render_hint": "Name the actual protein and pan from the recipe.",
        "confidence": "high", "editor_note": "interpreted from general searing technique",
    },
    {
        "id": "bloom_spices_in_fat", "kind": "check",
        "title": "Bloom spices in fat or they taste raw",
        "technique_tags": ["bloom_spices", "saute"],
        "trigger_signals": ["add spices", "curry powder", "spice paste", "cook until fragrant", "stir in spices"],
        "ingredient_classes": ["spices"], "equipment": ["skillet", "saucepan"], "scope": "step",
        "claim": "Stirring ground spices or a spice paste into hot fat before any liquid unlocks their full flavor; skip it and the spice character stays shallow and powdery.",
        "action": "Cook the spices in the fat until fragrant before adding liquid, watching the clock so they don't scorch.",
        "signal": "No aroma when the spices hit the fat means it's too cool; wisps of smoke or fast-darkening specks mean it's about to burn.",
        "failure_mode": "Either flat, raw, powdery spice flavor (under-bloomed) or an acrid, scorched note (over-bloomed).",
        "variants": [
            {"case": "dry spice blend", "note": "Stir into the fat with the sauteed aromatics; usually fragrant in 1-2 minutes."},
            {"case": "spice paste", "note": "Add to shimmering fat and cook longer than a dry blend; ready when fragrant and visibly darkened."},
        ],
        "mechanism": "Many spice flavor compounds are fat-soluble; warming them in oil or butter shifts those compounds into a state where they disperse through the dish.",
        "render_hint": "Name the actual spices and fat, and pick the dry-blend or paste variant to match.",
        "confidence": "high", "editor_note": "interpreted from general spice-blooming technique",
    },
    {
        "id": "salt_pasta_water", "kind": "tip",
        "title": "Salt the pasta water like the sea",
        "technique_tags": ["boil", "salting"], "trigger_signals": ["boil the pasta", "salt the water", "bring to a boil"],
        "ingredient_classes": ["pasta"], "equipment": ["pot"], "scope": "step",
        "claim": "The pasta's only chance to be seasoned through is the cooking water; under-salted water gives bland pasta no surface sauce can fix.",
        "action": "Salt the water until it tastes distinctly of the sea before the pasta goes in.",
        "signal": None, "failure_mode": None, "variants": [],
        "mechanism": "Pasta absorbs water (and dissolved salt) as it cooks, seasoning it from the inside.",
        "render_hint": "Name the pasta in the recipe.",
        "confidence": "high", "editor_note": "settled technique",
    },
    {
        "id": "rest_meat", "kind": "check",
        "title": "Rest meat or the juices run out",
        "technique_tags": ["rest_meat", "roast", "grill"], "trigger_signals": ["rest", "let it rest", "before slicing", "before carving"],
        "ingredient_classes": ["meat", "poultry"], "equipment": ["board"], "scope": "step",
        "claim": "Cutting straight out of the heat lets the juices spill onto the board instead of redistributing through the meat.",
        "action": "Let it rest before slicing — a few minutes for steaks/chops, longer for large roasts.",
        "signal": "A puddle on the board and a dry interior when you cut too soon.",
        "failure_mode": "Dry meat and a wet cutting board.",
        "variants": [], "mechanism": "Resting lets the muscle fibers reabsorb moisture forced out by heat.",
        "render_hint": "Name the cut and a rest time scaled to its size.",
        "confidence": "high", "editor_note": "settled technique",
    },
    {
        "id": "reserve_pasta_water", "kind": "tip",
        "title": "Reserve starchy pasta water for the sauce",
        "technique_tags": ["boil", "emulsify", "pan_sauce"], "trigger_signals": ["before draining", "reserve", "pasta water", "drain"],
        "ingredient_classes": ["pasta"], "equipment": ["pot"], "scope": "step",
        "claim": "The starchy cooking water emulsifies fat and loosens a sauce so it clings to the pasta instead of pooling.",
        "action": "Scoop out a cup of the water before you drain, and add it to the sauce a splash at a time.",
        "signal": None, "failure_mode": None, "variants": [],
        "mechanism": "Dissolved starch acts as an emulsifier, binding fat and water into a glossy sauce.",
        "render_hint": "Reference the sauce in this recipe.",
        "confidence": "high", "editor_note": "settled technique",
    },
    {
        "id": "dont_overmix_batter", "kind": "check",
        "title": "Overmixing batter bakes up tough",
        "technique_tags": ["fold", "mix_gently"], "trigger_signals": ["mix until just combined", "fold in", "stir in the flour", "do not overmix"],
        "ingredient_classes": ["flour", "batter"], "equipment": ["bowl"], "scope": "step",
        "claim": "Mixing flour-based batters past 'just combined' develops gluten and knocks out air, baking up dense and tough.",
        "action": "Stop as soon as the dry streaks disappear; a few lumps are fine.",
        "signal": "A smooth, elastic, glossy batter that's been worked too long.",
        "failure_mode": "Dense, tough, rubbery crumb.",
        "variants": [], "mechanism": "Agitating hydrated flour builds gluten strands; muffins/quick breads want minimal development.",
        "render_hint": "Name the batter (muffin, pancake, cake) in the recipe.",
        "confidence": "high", "editor_note": "settled technique",
    },
]


def ensure_cook_kb_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cook_tips_kb (
            id                 TEXT PRIMARY KEY,
            kind               TEXT NOT NULL DEFAULT 'tip',     -- tip | check
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
    now = datetime.now(timezone.utc).isoformat()
    existing = {r[0] for r in conn.execute("SELECT id FROM cook_tips_kb")}
    for e in SEED_ENTRIES:
        if e["id"] in existing:
            continue
        # Seed entries publish immediately — they're hand-vetted exemplars.
        _insert(conn, {**e, "status": "published"}, now)
    conn.commit()


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


def import_drafts(conn: sqlite3.Connection, entries: list[dict]) -> dict:
    """Bulk-import authored entries as DRAFTS (never auto-publish a batch — the KB
    is the moat; entries get reviewed in the editor before publish). Existing ids
    are skipped (idempotent). Returns {imported, skipped}."""
    now = datetime.now(timezone.utc).isoformat()
    existing = {r[0] for r in conn.execute("SELECT id FROM cook_tips_kb")}
    imported = skipped = 0
    for e in entries:
        if not e.get("id"):
            skipped += 1; continue
        if e["id"] in existing:
            skipped += 1; continue
        # Accept desktop-Claude's JSON unedited: it used `source_note` (we renamed
        # to editor_note); map it so existing batches import without hand-editing.
        e = {**e, "editor_note": e.get("editor_note") or e.get("source_note"),
             "status": "draft"}
        _insert(conn, e, now)
        existing.add(e["id"]); imported += 1
    conn.commit()
    return {"imported": imported, "skipped": skipped}


def project_published(conn: sqlite3.Connection) -> list[dict]:
    """The runtime view for the augment pass (3c-b): PUBLISHED entries only, with
    just the selection + generation fields — editor_note and confidence are
    DROPPED before the model ever sees them (rejoined server-side by id). Stable,
    key-ordered output so it can be a cacheable prompt prefix."""
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
