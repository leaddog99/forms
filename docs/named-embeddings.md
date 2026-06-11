# Named embeddings + hybrid (filtered) vector retrieval — design note

**Status:** design (2026-06-11). Grounded in research, not invention — see
"Prior art" below. First consumer: the cook tips/checks KB
([[project_cook_kb]] / docs/cook-kb-as-product.md). Pattern is reusable across
entities (recipes, dishes).

## The decision

1. An entity may carry **multiple NAMED embeddings** ("aspects"), each built from a
   different slice of its fields, because different retrieval tasks want different
   "document text." For `cook_tips_kb`:
   - **`browse`**  = `title + claim + action` — matches a natural-language question
     ("how do I keep things crisp?"). For the reader library.
   - **`applies`** = `title + trigger_signals + technique_tags + ingredient_classes`
     — matches a *recipe step* ("cut the carrots on the bias"). For authoring
     dedup now, augment retrieval pre-filter later.
   A single blended vector is a compromise that's mediocre at both; two specialized
   vectors are each strong at one.

2. **Hybrid (filtered) retrieval is first-class via in-index metadata, NOT a JOIN
   after KNN.** This is the crux (see "The trap" below).

## Prior art (this is a known method)

- **Multiple named/aspect vectors per record** is established: multi-vector / late
  interaction (ColBERT), multi-aspect fusion of content + metadata vectors.
  (Zilliz: how multi-vector embedding approaches work.)
- **Filtered vector search** is the canonical hard problem — *pre-filter vs
  post-filter* (apxml; "The Achilles Heel of Vector Search: Filters").
- **sqlite-vec metadata columns + partition keys** (Alex Garcia, 2024 release) are
  the supported mechanism for in-index filtering. Verified on our installed
  **sqlite-vec 0.1.9**: one vec0 table with two vector columns + a partition key +
  metadata columns, KNN filtered by `status`/`source` in the WHERE clause, works.

## The trap (why "embedding only linked to the record" causes heartburn)

If vectors live in a vec0 index keyed only by `rowid`/`id`, and you try to filter by
JOINing the base table and adding `WHERE base.status='published'` **after** the KNN:
the planner runs the **KNN first**, returns the top-k nearest, *then* applies the
filter — so a restrictive filter can drop all k and you get few/zero results
(post-filter recall collapse). This is exactly sqlite-vec issue #196. **Do not
filter by post-KNN JOIN.**

## The fix (what we build)

Put the **hot filter columns into the vec0 index** as metadata/partition columns, so
filtering runs *during* the KNN (sqlite-vec intersects a metadata bitmap with the
index before computing distances — correct AND faster).

```sql
-- Retrieval index (DERIVED; rebuilt from the BLOBs; excluded from recipes.sql).
CREATE VIRTUAL TABLE cook_tips_vec USING vec0(
  tip_rowid   integer primary key,   -- maps back to cook_tips_kb
  browse_emb  float[768],            -- named embedding 1
  applies_emb float[768],            -- named embedding 2
  status      text,                  -- HOT filter (metadata column)
  source      text                   -- HOT filter (curated | user)
  -- add a partition key here if/when the table grows enough to shard
);

-- Filtered KNN, fully in-index, no post-JOIN filter:
SELECT tip_rowid, distance
FROM cook_tips_vec
WHERE applies_emb MATCH :step_vec
  AND k = 12
  AND status = 'published'
  AND source = 'curated';
-- THEN join cook_tips_kb on rowid ONLY to fetch display fields (never to filter).
```

sqlite-vec metadata columns support `= != < <= > >= IN` (INTEGER/TEXT); partition
keys (TEXT/INTEGER) shard the index for fast pre-filter. Limitations to respect:
no NULL, no LIKE/GLOB/REGEXP, no BLOB/date filter columns (yet). So the in-index
filter set must be small, low-cardinality, non-null scalars.

## Source of truth vs index

- **Source of truth = real SQL table** holds the BLOBs + all scalar fields:
  `cook_tips_kb.emb_browse BLOB`, `emb_applies BLOB` (inline columns — same shape
  as `recipes.embedding`, keeps the record a first-class SQL citizen, FK-able,
  dumpable). The record is canonical; vectors hang off it.
- **Retrieval index = vec0** (above), carrying the named embeddings + hot filter
  columns, **rebuilt from the BLOBs** via `vector_store.py` (existing
  rebuild-from-blobs pattern, [[project_vec_delete_triggers]]).
- **Backup** unchanged: BLOB columns are dumped in `recipes.sql`; the vec0 index is
  excluded (derived) and rebuilt on load — same rule as `recipes_master_vec` today
  ([[project_db_backup]]).

This *extends* the inline-BLOB + vec0-rebuild path already in use for
recipes/dishes — single canonical path ([[feedback_single_path]]), not a parallel
store. (A generalized `entity_embeddings(entity_type, entity_id, purpose, ...)`
table was considered and set aside: it adds indirection and, more importantly, the
in-index-filter fix means the vec0 index — not the BLOB store — is what retrieval
hits, so a fancier BLOB store buys little. Revisit only if an entity needs many
aspects.)

## The reusable convention (the part to "get accustomed to")

A small registry in `vector_store.py` — the durable artifact:

```
EMBEDDING_ASPECTS = {
  ("cook_tips_kb", "browse"):  lambda e: f"{e.title}\n{e.claim}\n{e.action}",
  ("cook_tips_kb", "applies"): lambda e: f"{e.title}\n{' '.join(e.trigger_signals)}"
                                          f"\n{' '.join(e.technique_tags)}"
                                          f"\n{' '.join(e.ingredient_classes)}",
}
HOT_FILTERS = { "cook_tips_kb": ["status", "source"] }   # -> vec0 metadata columns
```

A new aspect or entity = a registry entry (+ vec0 column), not a migration. The
registry defines *what text* each named embedding is built from — the thing worth
getting right and reusing.

## When / how

- **Embed at publish** (and re-embed when an aspect's source fields change). Backfill
  the existing 101 entries once.
- **Delete** cascades the entity's vec0 rows via the AFTER-DELETE trigger pattern
  ([[project_vec_delete_triggers]]).
- Model: the same embedder used elsewhere (768-dim, matches `recipes.embedding`).

## Sequencing (with content_to_tip)

- **`applies`** → powers **authoring dedup** in `content_to_tip` (KNN a new draft vs
  existing published entries, filtered to `source='curated'`, warn on near-match
  before publish) — earns its keep the day transcript drafting starts adding entries.
- **`browse`** → powers the reader-library semantic search.
- **Augment retrieval pre-filter** (replace whole-KB-in-prompt with top-K `applies`
  candidates) is the LATER cutover — only when the KB outgrows the cached prompt; at
  ~100 entries the cached full-KB selection is still better.

## Open questions

- Embedding model/dim confirm (reuse the recipe embedder = 768).
- Add a `purpose`/`entity` partition key only if a shared vec0 ever holds multiple
  entity types; for a per-entity `cook_tips_vec`, status/source metadata suffice.
- Whether `browse` and `applies` can share one model with different source-text
  (yes to start) or eventually want different models.
