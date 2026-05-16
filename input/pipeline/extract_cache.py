"""LLM extract cache — keyed by (URL, model, prompt), invalidated by TTL.

Stage B (markdown -> recipe via LLM) is expensive (~25 s, ~$0.0015 per call)
and stable for the same source URL: even if per-capture noise (Captured:
timestamps, view counters, JSON-LD dateModified flipping daily) changes the
markdown bytes, the underlying recipe usually hasn't moved. A content-hash
key was too fragile — every site has its own per-visit cruft, and the cost
of a false miss is real. We accept bounded staleness (TTL default 30 days)
and pair it with a *semantic fingerprint* for drift detection.

Cache key:
    url_normalized   canonical URL form (normalize_url())
    model            model id, e.g. 'gpt-4o-mini'
    prompt_version   sha256(SYSTEM_PROMPT)[:12] — auto, no manual bumping

Each row also stores:
    semantic_fingerprint   sha256 of {name, ingredients[], instruction-texts[]}
                           NOT used as cache key (you'd have to call the LLM
                           to compute it). Used only on TTL-expired refresh:
                           if the new fingerprint differs from the cached
                           one, the source page meaningfully changed and
                           callers stamp `recipes.source_changed_at` so the
                           UI can flag "review and re-save."

Cache hits journal as 'cache_hit_markdown_to_recipe' with zero tokens so
future per-user usage queries can total tokens saved alongside actual spend.
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
    # Detect and drop the legacy schema (markdown_hash in PK). The cache
    # contents are recomputable; the only thing we lose is one or two test
    # rows that were on disk when the schema changed.
    cols = {c[1] for c in conn.execute("PRAGMA table_info(llm_extract_cache)").fetchall()}
    if cols and "markdown_hash" in cols:
        print("[CACHE] Dropping legacy llm_extract_cache schema (markdown_hash in PK)")
        conn.execute("DROP TABLE IF EXISTS llm_extract_cache")
        conn.commit()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_extract_cache (
            url_normalized       TEXT NOT NULL,
            model                TEXT NOT NULL,
            prompt_version       TEXT NOT NULL,
            recipe_json          TEXT NOT NULL,
            semantic_fingerprint TEXT NOT NULL DEFAULT '',
            created_at           TEXT NOT NULL,
            last_used_at         TEXT NOT NULL,
            hit_count            INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (url_normalized, model, prompt_version)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_extract_cache_last_used ON llm_extract_cache(last_used_at)")
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
    """Return the cached row for this key, with TTL/freshness info.

    Returns a dict with:
        llm_output            cached recipe JSON
        cached_at             created_at timestamp
        semantic_fingerprint  fingerprint stored on the cached row (may be '')
        is_stale              True when past TTL — caller should re-run the
                              LLM AND compare the new fingerprint with the
                              cached one to detect source drift.

    Returns None when no row exists. Fresh hits bump last_used_at + hit_count;
    stale reads leave usage stats alone so they don't get confused with hits.
    Never raises — returns None on any error so callers fall through to LLM.
    """
    if not url_normalized:
        return None
    try:
        with sqlite3.connect(db_path) as conn:
            ensure_llm_extract_cache_table(conn)
            row = conn.execute(
                """SELECT recipe_json, semantic_fingerprint, created_at
                   FROM llm_extract_cache
                   WHERE url_normalized = ? AND model = ? AND prompt_version = ?""",
                (url_normalized, model, prompt_version),
            ).fetchone()
            if not row:
                return None
            recipe_json, fingerprint, created_at = row
            is_stale = not _is_within_ttl(created_at, ttl_days)
            if not is_stale:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """UPDATE llm_extract_cache
                       SET last_used_at = ?, hit_count = hit_count + 1
                       WHERE url_normalized = ? AND model = ? AND prompt_version = ?""",
                    (now, url_normalized, model, prompt_version),
                )
                conn.commit()
            return {
                "llm_output": json.loads(recipe_json),
                "cached_at": created_at,
                "semantic_fingerprint": fingerprint or "",
                "is_stale": is_stale,
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
    """Store the LLM's raw JSON output and its semantic fingerprint under this
    cache key. created_at is reset on every write (TTL clock restarts on
    refresh) and hit_count zeroes. Never raises."""
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
                   ON CONFLICT(url_normalized, model, prompt_version)
                   DO UPDATE SET
                     recipe_json = excluded.recipe_json,
                     semantic_fingerprint = excluded.semantic_fingerprint,
                     created_at = excluded.created_at,
                     last_used_at = excluded.last_used_at,
                     hit_count = 0""",
                (url_normalized, model, prompt_version,
                 json.dumps(llm_output), semantic_fingerprint,
                 now, now),
            )
            conn.commit()
    except Exception as e:
        print(f"[WARN] llm_extract_cache write failed: {e}")
