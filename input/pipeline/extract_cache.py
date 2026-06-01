"""LLM extract cache — ONE row per URL, refreshed in place.

Stage B (markdown -> recipe via LLM) is expensive (~25 s, ~$/call) and stable
for the same source URL. We cache the extraction keyed solely by the
normalized URL and decide whether to *reuse* it from columns on the row.

KEY DESIGN (2026-06-01): the primary key is `url_normalized` ALONE. `model`
and `prompt_version` are stored as COLUMNS, not part of the key, and used only
in the freshness decision:

    HIT  iff  row.model == current  AND  row.prompt_version == current
             AND  created_at within TTL
    else re-extract and OVERWRITE the row (one row per URL, always latest).

Why not put model/prompt_version in the key (the old design)? Because a prompt
edit then created a *parallel* row instead of replacing the old one — the cache
fragmented into N orphaned versions per URL and went cold after every prompt
change. As a freshness *column* instead, a prompt/model change still forces a
fresh extraction (the comparison fails) but the re-extract OVERWRITES, so there
is exactly one live row per URL and improvements propagate on next touch. Same
correctness, no fragmentation, and the table doubles as a clean "latest
extraction per URL" library.

Each row also stores:
    semantic_fingerprint   sha256 of {name, ingredients[], instruction-texts[]}.
                           On a SAME-pipeline TTL-expired refresh we compare the
                           new fingerprint with this one; a difference means the
                           source page meaningfully changed and callers stamp
                           `recipes.source_changed_at`. A model/prompt change is
                           NOT source drift, so the fingerprint is suppressed in
                           that case to avoid a false "source changed" flag.

Cache hits journal as 'cache_hit_markdown_to_recipe' with zero tokens so future
per-user usage queries can total tokens saved alongside actual spend.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional


# Tunable per call via the `ttl_days` kwarg on get_cached_extract.
# 30 days matches MOZ_REFRESH_TTL_DAYS so manual and automatic refresh
# cadences agree on what "stale" means.
EXTRACT_CACHE_TTL_DAYS = 30


def ensure_llm_extract_cache_table(conn: sqlite3.Connection) -> None:
    """Create the url-only-PK cache table; rebuild from any legacy schema.

    The legacy schemas (composite PK on (url, model, prompt_version), or the
    even older markdown_hash key) are dropped wholesale — the cache is fully
    recomputable, and the old fragmented rows can never hit under the new
    single-row-per-URL design anyway.
    """
    info = conn.execute("PRAGMA table_info(llm_extract_cache)").fetchall()
    if info:
        cols = {c[1] for c in info}
        pk_cols = [c[1] for c in info if c[5]]          # c[5] = pk position (>0 if in PK)
        legacy = ("markdown_hash" in cols) or (pk_cols != ["url_normalized"])
        if legacy:
            print(f"[CACHE] Rebuilding llm_extract_cache under url-only PK "
                  f"(was {pk_cols or 'legacy'}) — prior rows discarded")
            conn.execute("DROP TABLE IF EXISTS llm_extract_cache")
            conn.commit()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_extract_cache (
            url_normalized       TEXT PRIMARY KEY,
            model                TEXT NOT NULL,
            prompt_version       TEXT NOT NULL,
            recipe_json          TEXT NOT NULL,
            semantic_fingerprint TEXT NOT NULL DEFAULT '',
            created_at           TEXT NOT NULL,
            last_used_at         TEXT NOT NULL,
            hit_count            INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()


def prompt_version_for(prompt: str) -> str:
    """12-char prefix of sha256(prompt). Caller never has to remember to bump
    a version constant when the prompt changes — the hash does it."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]


def compute_recipe_fingerprint(recipe: dict) -> str:
    """Bit-stable fingerprint of the recipe's load-bearing semantic content.

    Includes name + ingredients[] + instruction-texts[]. Excludes
    description/dateModified/image/etc. because those flip on the source page
    without the actual recipe changing — including them would yield spurious
    drift signals on every TTL-expired re-extract. Used only for drift
    detection; never as a cache key.
    """
    name = (recipe.get("name") or "").strip().lower()

    ingredients = []
    for ing in (recipe.get("recipeIngredient") or []):
        if isinstance(ing, str):
            ingredients.append(ing.strip().lower())

    instr_texts = []
    for item in (recipe.get("recipeInstructions") or []):
        if isinstance(item, dict):
            text = item.get("text") or ""
        else:
            text = item
        instr_texts.append(str(text).strip().lower())

    payload = "\n".join([name, *ingredients, *instr_texts])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_within_ttl(created_at_iso: str, ttl_days: int) -> bool:
    """True when created_at is within ttl_days of now (UTC). False on null,
    unparseable, or any error — caller treats those as stale and re-runs."""
    if not created_at_iso or ttl_days <= 0:
        return False
    try:
        created = datetime.fromisoformat(created_at_iso)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created) < timedelta(days=ttl_days)
    except Exception:
        return False


def get_cached_extract(
    db_path: str,
    *,
    url_normalized: str,
    model: str,
    prompt_version: str,
    ttl_days: int = EXTRACT_CACHE_TTL_DAYS,
) -> Optional[dict]:
    """Look up the single cached row for this URL and decide freshness.

    Returns a dict with:
        llm_output            cached recipe JSON (only on a fresh HIT; None when stale)
        cached_at             created_at timestamp
        semantic_fingerprint  fingerprint to compare for drift — populated only
                              when the row is SAME-pipeline (so a model/prompt
                              change doesn't masquerade as source drift); '' otherwise
        is_stale              True when the row exists but can't be reused
                              (model/prompt changed, or past TTL) — caller
                              re-extracts and overwrites.

    Returns None when no row exists for the URL. Never raises — returns None on
    any error so callers fall through to the LLM.
    """
    if not url_normalized:
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            ensure_llm_extract_cache_table(conn)
            row = conn.execute(
                """SELECT model, prompt_version, recipe_json,
                          semantic_fingerprint, created_at
                   FROM llm_extract_cache
                   WHERE url_normalized = ?""",
                (url_normalized,),
            ).fetchone()
            if not row:
                return None
            row_model, row_pv, recipe_json, fingerprint, created_at = row
            same_pipeline = (row_model == model and row_pv == prompt_version)
            fresh = same_pipeline and _is_within_ttl(created_at, ttl_days)
            if fresh:
                # Fresh hit. (last_used_at/hit_count intentionally not bumped —
                # nothing reads them; bcc_token_journal records the hit instead,
                # and a write on every hit would defeat the read-only fast path.)
                return {
                    "llm_output": json.loads(recipe_json),
                    "cached_at": created_at,
                    "semantic_fingerprint": fingerprint or "",
                    "is_stale": False,
                }
            # Stale: re-extract + overwrite. Carry the prior fingerprint for
            # drift detection ONLY when the pipeline matched (true TTL expiry);
            # a model/prompt change is not source drift.
            return {
                "llm_output": None,
                "cached_at": created_at,
                "semantic_fingerprint": (fingerprint or "") if same_pipeline else "",
                "is_stale": True,
            }
    except Exception as e:
        print(f"[WARN] llm_extract_cache lookup failed: {e}")
        return None


def set_cached_extract(
    db_path: str,
    *,
    url_normalized: str,
    model: str,
    prompt_version: str,
    llm_output: dict,
    semantic_fingerprint: str,
) -> None:
    """Store/overwrite the extraction for this URL. ONE row per URL: a re-extract
    (new prompt/model, or TTL refresh) replaces the row in place, stamping the
    current model + prompt_version and resetting the TTL clock. Never raises."""
    if not url_normalized:
        return
    try:
        with sqlite3.connect(db_path) as conn:
            ensure_llm_extract_cache_table(conn)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO llm_extract_cache
                     (url_normalized, model, prompt_version,
                      recipe_json, semantic_fingerprint,
                      created_at, last_used_at, hit_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(url_normalized) DO UPDATE SET
                     model                = excluded.model,
                     prompt_version       = excluded.prompt_version,
                     recipe_json          = excluded.recipe_json,
                     semantic_fingerprint = excluded.semantic_fingerprint,
                     created_at           = excluded.created_at""",
                (url_normalized, model, prompt_version,
                 json.dumps(llm_output), semantic_fingerprint,
                 now, now),
            )
            conn.commit()
    except Exception as e:
        print(f"[WARN] llm_extract_cache write failed: {e}")
