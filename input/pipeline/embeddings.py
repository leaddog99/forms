"""Embedding-based dish cohort matching.

Each dish carries a vector embedding of its name + query phrases. When
a new recipe is saved outside the batch path (harvest, personal save,
legacy backfill), we embed the recipe (name + cuisine + top
ingredients) and find the nearest dish by cosine similarity. Above a
confidence threshold the recipe is graded against that dish's stored
OU-fit (see input.pipeline.grading.compute_exceptionalism); below
threshold the row stays ungraded (em-dash in UI).

Why embeddings rather than rules: cuisines / chapters / ingredient
overlap drift across recipe sites — "Bolognese", "Spaghetti with meat
sauce", "Spaghetti and meat sauce" are the same dish under different
phrasings. Semantic embeddings catch this without per-dish rule
maintenance. The same embedding cache feeds the deferred "recipes
like this" recommender, so the spend amortizes across features.

Model: OpenAI text-embedding-3-small (1536 dims, ~$0.02/1M tokens,
output already L2-normalized so cosine == dot product). Cheap enough
to compute on every save; cached on the dish row so we only embed each
dish once (and re-embed when the query list changes).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from openai import OpenAI


EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536

# Cosine similarity floor for a "confident enough to grade" match. Tuned
# conservatively on first pass — embedding-3-small typically places
# same-dish recipe name pairs at 0.55-0.85, and unrelated pairs below
# 0.3. 0.55 keeps obvious near-misses out while still catching most
# legitimate matches. Recalibrate after the first batch of saves
# surfaces the false-positive rate.
DEFAULT_MATCH_THRESHOLD = 0.55

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    """Lazy-init OpenAI client — keeps import-time cheap and lets the
    .env load before construction (same pattern the Anthropic clients
    use in save_recipe_api.py)."""
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def embed_text(text: str) -> np.ndarray:
    """Return a 1536-dim float32 vector for a single text. Output is
    already L2-normalized by the API; cosine similarity reduces to dot
    product. Raises on API errors — callers decide whether to swallow.

    Empty / whitespace-only input → zero vector (so cosine against any
    real vector is 0, which falls below any sane threshold). Avoids the
    API call entirely on the dead-input case.
    """
    if not text or not text.strip():
        return np.zeros(EMBED_DIM, dtype="float32")
    resp = _get_client().embeddings.create(model=EMBED_MODEL, input=[text])
    return np.array(resp.data[0].embedding, dtype="float32")


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity. Returns 0.0 when either vector is zero-norm
    (avoids a NaN that would muddy threshold comparisons)."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na <= 0 or nb <= 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def compose_identity_text(card: dict, *, title: str = "") -> str:
    """Build the embedding input from a dish identity card.

    Lead with the canonical `likelyDish` phrase (highest-leverage
    single token sequence for text-embedding-3-small), then the
    optional original title, then structured fact lines. The result
    is short (~60-80 tokens), signal-dense, and symmetric across
    recipe and dish sides — both produce embed text from the same
    composer, so cosine reflects semantic similarity rather than
    format-similarity.

    `title` defaults to "" for dishes (where the card IS the
    canonical name); recipes pass their `name` field so the literal
    title still reinforces the embedding.
    """
    if not isinstance(card, dict):
        return ""
    parts: list[str] = []
    likely = (card.get("likelyDish") or "").strip()
    if likely:
        parts.append(likely)
    if title and title.strip() and title.strip().lower() != likely.lower():
        parts.append(title.strip())
    cuisine = (card.get("cuisine") or "").strip()
    ethnicity = (card.get("ethnicity") or "").strip()
    if cuisine:
        parts.append(f"cuisine: {cuisine}")
    if ethnicity and ethnicity.lower() != cuisine.lower():
        parts.append(f"ethnicity: {ethnicity}")
    primary = card.get("primaryIngredients") or []
    if primary:
        parts.append("primary: " + ", ".join(p for p in primary if p))
    technique = (card.get("technique") or "").strip()
    if technique:
        parts.append(f"technique: {technique}")
    return ". ".join(parts)


def compose_dish_text(dish_row: dict) -> str:
    """Build the embedding input for a dish.

    Card-first: when the dish has a populated `identity_card`, we
    derive the embed text from it via `compose_identity_text`. The
    fallback path (no card yet) uses name + description + queries —
    same composition the pre-card architecture used so behavior is
    backward-compatible during the rollout.
    """
    card = dish_row.get("identity_card")
    if isinstance(card, dict) and (card.get("likelyDish") or "").strip():
        return compose_identity_text(card, title=dish_row.get("name") or "")

    name = (dish_row.get("name") or "").strip()
    description = (dish_row.get("description") or "").strip()
    queries = dish_row.get("queries") or []
    parts = [name]
    if description:
        parts.append(description)
    parts.extend(str(q).strip() for q in queries if str(q).strip())
    seen, deduped = set(), []
    for p in parts:
        key = p.lower()
        if p and key not in seen:
            seen.add(key)
            deduped.append(p)
    return ". ".join(deduped)


def compose_recipe_text(recipe: dict, *, max_ingredients: int = 8) -> str:
    """Build the embedding input for a recipe.

    Card-first architecture (2026-05-28): when `_identity` is present
    (a structured identity card produced by extract.identity_card),
    derive embed text from the card via `compose_identity_text`. The
    card's `likelyDish` is the highest-leverage single field; its
    structured cuisine + primaryIngredients + technique fill out the
    vector with dense, signal-bearing tokens.

    Fallback path for un-carded rows (or extract paths that haven't
    stamped a card yet): the prior composition — dishSignal + name +
    cuisine + chapter + first N raw ingredients. Kept until the
    backfill has covered every row.
    """
    card = recipe.get("_identity")
    if isinstance(card, dict) and (card.get("likelyDish") or "").strip():
        return compose_identity_text(card, title=recipe.get("name") or "")

    name = (recipe.get("name") or "").strip()
    cls = recipe.get("classification") or {}
    prov = recipe.get("provenance") or {}
    cuisine = (recipe.get("recipeCuisine") or "").strip()
    chapter = (cls.get("chapter") or "").strip()
    ethnicity = (prov.get("ethnicity") or "").strip()
    dish_signal = (cls.get("dishSignal") or "").strip()

    parts: list[str] = []
    if dish_signal:
        parts.append(dish_signal)
    if name:
        parts.append(name)
    if cuisine:
        parts.append(f"cuisine: {cuisine}")
    elif ethnicity:
        parts.append(f"cuisine: {ethnicity}")
    if chapter:
        parts.append(f"chapter: {chapter}")

    ings = recipe.get("recipeIngredient") or []
    if ings:
        # RecipeIngredient items can be strings or dicts depending on
        # source; coerce to text and clip.
        cleaned: list[str] = []
        for ing in ings[:max_ingredients]:
            if isinstance(ing, str):
                cleaned.append(ing.strip())
            elif isinstance(ing, dict):
                cleaned.append(str(ing.get("text") or ing.get("name") or "").strip())
        cleaned = [c for c in cleaned if c]
        if cleaned:
            parts.append("ingredients: " + ", ".join(cleaned))

    return ". ".join(p for p in parts if p)


# === Storage helpers =========================================================
# Embeddings stored as raw float32 bytes in a BLOB column — 1536 floats
# = 6144 bytes per dish. SQLite BLOBs handle this without ceremony; no
# need for a separate vector store at this scale.


def vec_to_bytes(v: np.ndarray) -> bytes:
    """Serialize a float32 vector to bytes for BLOB storage."""
    arr = np.asarray(v, dtype="float32")
    return arr.tobytes()


def bytes_to_vec(b: Optional[bytes]) -> Optional[np.ndarray]:
    """Deserialize a BLOB back into a numpy vector. None / empty → None."""
    if not b:
        return None
    return np.frombuffer(b, dtype="float32")


# === Dish embedding cache ====================================================


def ensure_dish_description(conn: sqlite3.Connection, dish_row: dict) -> Optional[str]:
    """Make sure the dish has a description + chapter + identity card,
    auto-generating any missing piece via Haiku. Returns the
    description value (or None when name is missing). Mutates
    dish_row in place so the caller's downstream composition uses the
    fresh values without a round-trip.

    Three artifacts maintained in one path:

      1. description — one-line prose dish summary, used by UI display
         and as the fallback embed source when no card is present.
      2. chapter — cookbook chapter (from chapter_classifier), used as
         the SQL pre-filter on KNN.
      3. identity_card — structured fact card (extract.identity_card
         output) used by compose_dish_text + compose_recipe_text to
         build the embed input symmetrically.

    Default-on per user 2026-05-28: missing artifacts are auto-filled
    on first read; curator may override description through the
    dishes form. The card itself is LLM-only for now — exposed
    read-only in the UI.
    """
    name = (dish_row.get("name") or "").strip()
    if not name:
        return None

    existing_desc = (dish_row.get("description") or "").strip()
    existing_chapter = (dish_row.get("chapter") or "").strip()
    existing_card = dish_row.get("identity_card")
    has_card = isinstance(existing_card, dict) and (existing_card.get("likelyDish") or "").strip()

    if existing_desc and existing_chapter and has_card:
        return existing_desc

    try:
        from extract.dish_signal import generate_dish_signal_for_dish
        from extract.chapter_classifier import classify_chapter
        from extract.identity_card import generate_identity_card_for_dish
    except Exception as e:
        print(f"[EMBED] dish-helper imports failed: {e}")
        return existing_desc or None

    signal = existing_desc or generate_dish_signal_for_dish(name, dish_row.get("queries"))
    chapter = existing_chapter or classify_chapter(name)
    if not has_card:
        # Pass the freshly-derived description in so the card LLM has
        # the curator's framing available even before persist.
        feed = dict(dish_row)
        feed["description"] = signal or existing_desc
        card = generate_identity_card_for_dish(feed)
    else:
        card = existing_card

    if not signal:
        return None

    import json as _json
    conn.execute(
        "UPDATE dishes SET description = ?, chapter = ?, identity_card = ?, "
        "updated_at = ? WHERE name = ?",
        (
            signal, chapter,
            _json.dumps(card) if card else None,
            datetime.now(timezone.utc).isoformat(), name,
        ),
    )
    conn.commit()
    dish_row["description"] = signal
    dish_row["chapter"] = chapter
    if card:
        dish_row["identity_card"] = card
    if not existing_desc or not has_card:
        likely = (card or {}).get("likelyDish", "?")
        print(f"[EMBED] dish {name!r} card+desc ready (likelyDish={likely!r}, "
              f"chapter={chapter!r})")
    return signal


def ensure_dish_embedding(conn: sqlite3.Connection, dish_row: dict,
                          *, force: bool = False,
                          auto_describe: bool = True) -> Optional[np.ndarray]:
    """Compute + cache the dish's embedding if missing or stale, return
    the vector. Returns None on API failure (best-effort; cohort
    matching falls back to the dishes that DO have embeddings cached).

    Staleness rule: re-embed when `embedding_text` in the cache differs
    from `compose_dish_text(dish_row)` — i.e. queries / name /
    description changed. `force=True` re-embeds unconditionally.

    `auto_describe=True` (default) auto-generates a description via
    Haiku when blank. Set False to keep behavior pure (no LLM side
    effect) — used by tests + backfill dry-runs.
    """
    name = dish_row.get("name")
    if not name:
        return None

    row = conn.execute(
        "SELECT embedding, embedding_text FROM dishes WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None

    if auto_describe:
        ensure_dish_description(conn, dish_row)

    cached_blob, cached_text = row
    target_text = compose_dish_text(dish_row)

    if not force and cached_blob and cached_text == target_text:
        return bytes_to_vec(cached_blob)

    try:
        vec = embed_text(target_text)
    except Exception as e:
        print(f"[EMBED] dish {name!r} failed: {type(e).__name__}: {e}")
        return None

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE dishes SET embedding = ?, embedding_text = ?, "
        "embedding_model = ?, embedding_updated_at = ? WHERE name = ?",
        (vec_to_bytes(vec), target_text, EMBED_MODEL, now, name),
    )
    conn.commit()

    # Keep dishes_vec in lockstep with the BLOB column so the KNN
    # path (find_best_dish_match → vector_store.find_similar_dishes)
    # sees the fresh embedding immediately. Best-effort: if vec0
    # tables aren't created yet (startup race) or sqlite-vec is
    # missing, skip — the in-memory dish embedding still works as
    # the cache for this call.
    try:
        from input.pipeline import vector_store
        vector_store.enable_vec(conn)
        vector_store.upsert_dish_vector(conn, name, vec)
    except Exception as e:
        print(f"[EMBED] dishes_vec upsert failed for {name!r}: {e}")
    return vec


def _l2_to_cosine_sim(l2_dist: float) -> float:
    """Convert L2 distance to cosine similarity for L2-normalized
    vectors. For unit vectors: dist² = 2 - 2·cos → cos = 1 - dist²/2.
    Used so the public API of find_best_dish_match keeps returning
    cosine similarity, even though the vec0 store reports L2."""
    return 1.0 - (l2_dist * l2_dist) / 2.0


def find_best_dish_match(conn: sqlite3.Connection, recipe: dict, *,
                         threshold: float = DEFAULT_MATCH_THRESHOLD
                         ) -> Optional[dict]:
    """Embed the recipe, KNN-search dishes via sqlite-vec, return the
    best match above threshold. None when no dish clears the bar.

    Backed by `input.pipeline.vector_store.find_similar_dishes` (vec0
    virtual table). Chapter filter applied in SQL — vec0 supports the
    JOIN against dishes.chapter natively. Fallback to wide scan if
    chapter-filtered KNN comes up empty or below-threshold.

    Distance comes back as L2 from vec0; we convert to cosine
    similarity (the unit-sphere identity `cos = 1 - dist²/2`) so the
    public threshold semantics stay unchanged.

    Return shape: {dish_name, confidence, ou_fit, chapter_filtered}.
    """
    from input.pipeline import vector_store  # local import — keeps
                                              # embeddings.py importable
                                              # without sqlite-vec for tests

    text = compose_recipe_text(recipe)
    if not text.strip():
        return None
    try:
        recipe_vec = embed_text(text)
    except Exception as e:
        print(f"[EMBED] recipe embed failed: {type(e).__name__}: {e}")
        return None

    # vec0 needs the extension loaded on this connection. Idempotent.
    try:
        vector_store.enable_vec(conn)
    except Exception as e:
        print(f"[EMBED] sqlite-vec load failed: {e}")
        return None

    chapter = ((recipe.get("classification") or {}).get("chapter") or "").strip()
    chapter_filtered = False

    # Tier 1: chapter-filtered KNN. K=1 is enough because the threshold
    # gate runs on the top result; if the closest dish doesn't clear
    # the bar, no further-away one will.
    results: list[dict] = []
    if chapter:
        results = vector_store.find_similar_dishes(conn, recipe_vec, k=1, chapter=chapter)
        chapter_filtered = bool(results)

    # Tier 2: wide scan as fallback (no in-chapter dish at all, or
    # closest in-chapter dish below threshold).
    needs_widen = (
        not results
        or _l2_to_cosine_sim(results[0]["distance"]) < threshold
    )
    if needs_widen:
        wide = vector_store.find_similar_dishes(conn, recipe_vec, k=1)
        if wide:
            results = wide
        chapter_filtered = False

    if not results:
        return None

    top = results[0]
    confidence = _l2_to_cosine_sim(top["distance"])
    if confidence < threshold:
        return None

    return {
        "dish_name": top["name"],
        "confidence": confidence,
        "ou_fit": top.get("ou_fit"),
        "chapter_filtered": chapter_filtered,
    }
