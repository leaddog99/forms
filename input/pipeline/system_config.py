"""System config — the DB-resident "config file" (the system record).

The apex of the portable-package model (memory/project_system_config.md,
project_portable_package): every customizable knob reaches the app via the DB,
not a source file, so a recipient of the package never edits code. Today's
`bcc_config.json` becomes a bootstrap SEED (memory/feedback_no_data_in_code) —
the table is canonical once seeded.

Shape: key/value rows, one per setting. `value` is JSON-encoded so a setting
keeps its type (bool/int/float/str/list). `type`/`category`/`label`/
`description` drive how the admin editor renders + groups it.

Reads go through `get_setting(key, default)` — cached process-wide and
invalidated on any write, mirroring domains_lib.get_blocked_root_domains. The
cache makes hot-path reads (e.g. a scheduler gate) free.

This first slice ships only the scheduler settings; the rest of bcc_config.json
migrates in incrementally as code touches those keys.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional


# ============================================================
#  Bootstrap seed (DEFAULTS) — code constant is seed-only.
#  Each row: key, default value, type, category, label, description.
#  `type` is a UI/rendering hint; the value is always JSON-encoded.
# ============================================================

SYSTEM_DEFAULTS: list[dict] = [
    {
        "key": "scheduler_enabled",
        "value": True,
        "type": "bool",
        "category": "Scheduler",
        "label": "Auto-refresh scheduler enabled",
        "description": "Master on/off for unattended dish refreshes. When off, "
                       "`python -m jobs schedule` does nothing (manual Run still works).",
    },
    {
        "key": "scheduler_interval_hours",
        "value": 6,
        "type": "int",
        "category": "Scheduler",
        "label": "Scheduler interval (hours)",
        "description": "Minimum hours between scheduler passes. The OS heartbeat "
                       "fires hourly; a pass is skipped until this many hours have "
                       "elapsed since the last one. Per-dish timing still obeys each "
                       "dish's Refresh TTL.",
    },
    {
        "key": "scheduler_last_tick_at",
        "value": None,
        "type": "string",
        "category": "Scheduler",
        "label": "Last scheduler pass (UTC)",
        "description": "Stamped by the scheduler after each real pass. Read-only; "
                       "drives the interval gate.",
    },
    {
        "key": "rollups_last_run_at",
        "value": None,
        "type": "string",
        "category": "Scheduler",
        "label": "Last chapter-rollups pass (UTC)",
        "description": "Stamped after the nightly cross-dish rollup (dish "
                       "competitiveness). Read-only; gates the ~daily rollup that "
                       "rides the hourly scheduler heartbeat.",
    },
    # --- Images: standardization knobs for the coopt/upload pipeline ---
    {
        "key": "image_jpeg_quality",
        "value": 85,
        "type": "int",
        "category": "Images",
        "label": "JPEG quality",
        "description": "Quality (1–95) for standardized hero/cooped images. "
                       "Higher = sharper + bigger. 85 ≈ cookbook-grade.",
    },
    {
        "key": "image_hero_max_px",
        "value": 1600,
        "type": "int",
        "category": "Images",
        "label": "Hero image max edge (px)",
        "description": "A user's hero image is resized PRESERVING its whole "
                       "frame + orientation (no crop), scaled down so the longest "
                       "edge is at most this many pixels. Small images are never "
                       "upscaled. (The corpish-thumbnail crop below is separate.)",
    },
    {
        "key": "image_landscape_target",
        "value": "1500x1000",
        "type": "string",
        "category": "Images",
        "label": "Landscape target (WxH)",
        "description": "Exact pixel size landscape-ish images are center-cropped + "
                       "scaled to. 3:2 is the cookbook standard.",
    },
    {
        "key": "image_portrait_target",
        "value": "1000x1500",
        "type": "string",
        "category": "Images",
        "label": "Portrait target (WxH)",
        "description": "Exact pixel size portrait images are center-cropped + scaled to.",
    },
    # --- Limits: hard caps that protect against fat-finger / runaway cost ---
    {
        "key": "dish_max_serpapi",
        "value": 200,
        "type": "int",
        "category": "Limits",
        "label": "Max SerpAPI candidates per query",
        "description": "Hard cap on a dish's top_n_serpapi. Creating/editing a dish "
                       "above this is rejected, and a refresh clamps to it — guards "
                       "against e.g. an accidental 2550 asking SerpAPI for 2550 rows.",
    },
    {
        "key": "dish_max_final",
        "value": 200,
        "type": "int",
        "category": "Limits",
        "label": "Max kept winners per dish",
        "description": "Hard cap on a dish's top_n_final (selected rows). Same "
                       "enforcement as the SerpAPI cap.",
    },
    # --- Matching: recipe -> canonical-dish vector NN at save time ---
    {
        "key": "dish_match_max_distance",
        "value": 0.85,
        "type": "float",
        "category": "Matching",
        "label": "Dish match cutoff (L2 distance)",
        "description": "A user recipe is a CONFIDENT match to its nearest dish "
                       "when their embeddings are within this L2 distance. Lower = "
                       "stricter. 0.85 (≈ cosine 0.64) catches compound names like "
                       "'Shrimp Risotto' (0.82) while excluding true non-matches "
                       "(≥0.98). Validated: real matches ≤0.25 or ~0.82, non-matches ≥0.98.",
    },
]

_SEED_BY_KEY = {d["key"]: d for d in SYSTEM_DEFAULTS}


# ============================================================
#  Schema + seed
# ============================================================

def ensure_system_config_table(conn: sqlite3.Connection) -> None:
    """Create the system_config table if absent and seed any missing default
    rows. Idempotent — safe on every startup. Seeding only INSERTs keys that
    don't exist yet, so it never clobbers a curator's edited value."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_config (
            key         TEXT PRIMARY KEY,
            value       TEXT,                         -- JSON-encoded (typed)
            type        TEXT NOT NULL DEFAULT 'string',
            category    TEXT NOT NULL DEFAULT 'General',
            label       TEXT,
            description TEXT,
            updated_at  TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_system_config_category "
        "ON system_config(category)"
    )
    now = datetime.now(timezone.utc).isoformat()
    existing = {r[0] for r in conn.execute("SELECT key FROM system_config")}
    for d in SYSTEM_DEFAULTS:
        if d["key"] in existing:
            # Backfill metadata (label/desc/type/category) so doc edits to the
            # seed reach already-seeded rows, WITHOUT touching the value.
            conn.execute(
                "UPDATE system_config SET type=?, category=?, label=?, description=? "
                "WHERE key=?",
                (d["type"], d["category"], d.get("label"), d.get("description"), d["key"]),
            )
            continue
        conn.execute(
            "INSERT INTO system_config (key, value, type, category, label, description, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d["key"], json.dumps(d["value"]), d["type"], d["category"],
             d.get("label"), d.get("description"), now),
        )
    conn.commit()
    _invalidate_cache()


# ============================================================
#  Cached read / write
# ============================================================

# Process-wide cache: key -> decoded value. Populated lazily, dropped on any
# write. None means "not loaded yet" (distinct from an empty dict).
_cache: Optional[dict[str, Any]] = None


def _invalidate_cache() -> None:
    global _cache
    _cache = None


def _load_cache(db_path: str) -> dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    out: dict[str, Any] = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for key, value_json in conn.execute("SELECT key, value FROM system_config"):
                try:
                    out[key] = json.loads(value_json) if value_json is not None else None
                except Exception:
                    out[key] = value_json  # tolerate a non-JSON legacy value
    except Exception as e:
        # Table may not exist yet (very early boot) — fall back to seed defaults.
        print(f"[SYSCONFIG] cache load failed ({e}); using seed defaults")
        return {k: d["value"] for k, d in _SEED_BY_KEY.items()}
    _cache = out
    return out


def get_setting(key: str, default: Any = None, *, db_path: Optional[str] = None) -> Any:
    """Read a setting (cached). Falls back to the seed default, then `default`.
    db_path defaults to save_recipe_api.DB_PATH so callers don't have to pass it."""
    if db_path is None:
        import save_recipe_api as _api  # lazy to avoid import cycle at module load
        db_path = _api.DB_PATH
    cache = _load_cache(db_path)
    if key in cache:
        return cache[key]
    if key in _SEED_BY_KEY:
        return _SEED_BY_KEY[key]["value"]
    return default


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Upsert a setting's value (JSON-encoded) and invalidate the cache. Carries
    the seed's type/category/label/description for a brand-new key so the admin
    UI can still render it."""
    now = datetime.now(timezone.utc).isoformat()
    seed = _SEED_BY_KEY.get(key, {})
    conn.execute(
        """
        INSERT INTO system_config (key, value, type, category, label, description, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, json.dumps(value), seed.get("type", "string"),
         seed.get("category", "General"), seed.get("label"),
         seed.get("description"), now),
    )
    conn.commit()
    _invalidate_cache()


def list_settings(conn: sqlite3.Connection,
                  category: Optional[str] = None) -> list[dict]:
    """All settings (optionally one category), decoded, for the admin editor.
    Ordered by category then key for stable grouping."""
    sql = "SELECT key, value, type, category, label, description, updated_at FROM system_config"
    args: list = []
    if category is not None:
        sql += " WHERE category = ?"
        args.append(category)
    sql += " ORDER BY category, key"
    out: list[dict] = []
    for key, value_json, type_, cat, label, desc, updated in conn.execute(sql, args):
        try:
            value = json.loads(value_json) if value_json is not None else None
        except Exception:
            value = value_json
        out.append({
            "key": key, "value": value, "type": type_, "category": cat,
            "label": label, "description": desc, "updated_at": updated,
        })
    return out
