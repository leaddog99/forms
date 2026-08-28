# Postgres migration — SQLite-surface inventory

*2026-08-27. Step 0 (every runtime connection through `input/pipeline/db.connect`,
`e78ae66`) is done. This doc is the promised inventory of everything
SQLite-specific, measured from the live DBs and a runtime-code census
(scripts/, experiments/, backup_db.py, enrich/ excluded — enrich/db.py stays
self-contained by split design). It ends with the decisions the curator owns.*

## The estate: four database files, not one

| file | size | contents | migrate? |
|---|---|---|---|
| recipes.db | 557 MB | 76 tables — the system of record | **yes — this is the migration** |
| page_cache.db | 1.17 GB | page_fetch_cache, 12,237 rows | recommend **stays SQLite** (append-mostly cache, one writer, disposable) |
| training.db | 674 MB | is_recipe_samples, 51,561 rows | recommend **stays SQLite** (ML corpus, bulk-write, rebuildable) |
| media.db | 405 MB | page_screenshots 9,340 + tts_audio 825 (blobs) | recommend **stays SQLite** (blob store; Postgres bytea buys nothing) |

Scoping the migration to recipes.db alone cuts the moved bytes from 2.8 GB to
557 MB and leaves every sidecar's single-writer pattern — which is fine for a
cache — untouched.

## What DISSOLVES in Postgres (the wins)

These are the pain points that motivated the exploration; each one stops
existing rather than getting ported.

1. **The single-writer story.** WAL mode, the 30 s busy_timeout in the factory,
   the "publisher refresh saves every ~7 s vs server save" contention that
   birthed it — MVCC makes concurrent writers the normal case. The factory
   docstring's whole reason to exist evaporates.
2. **The disabled job runner.** The 2 s poll was turned off because blocking
   sqlite stalled requests. Postgres gives real concurrency plus
   LISTEN/NOTIFY, so the jobs table (1,060 rows) can go event-driven instead
   of polled — the runner can come back on.
3. **vec0 sidecar tables + cleanup triggers.** Three vec0 virtual tables
   (dishes_vec, recipes_master_vec, products_vec, all float[1536]) shadow BLOB
   source-of-truth columns (7,579 / 222 / 56 embeddings), kept honest by three
   AFTER DELETE triggers and `enable_vec` discipline on every delete path.
   pgvector puts a `vector(1536)` column with an HNSW index directly ON the
   base table — no sidecar, no triggers, no drift class, the
   check_embeddings.py regression check shrinks.
4. **The source_host generated column.** A ~5 KB nested CASE/instr/substr
   expression (SQLite has no split_part). In Postgres:
   `split_part(split_part(data->'_source'->>'originalUrl','://',2),'/',1)`
   plus a www-strip — one line.
5. **FTS trigger plumbing.** Six triggers currently re-derive
   name/dish/ingredients/cuisine into two fts5 tables on every
   insert/update/delete. A generated `tsvector` STORED column + GIN index is
   declarative — triggers deleted, no ad/ai/au triplets to keep in sync.
6. **The ANALYZE/PRAGMA-optimize wound.** The dish top-10 94 ms→9 ms mis-plan
   (reference_sqlite_analyze) — autovacuum/autoanalyze does this for a living.

## What is a REWRITE (the costs)

1. **Driver + placeholder sweep.** `?` → `%s`, sqlite3.Row → dict_row,
   `lastrowid` (3 files) → `RETURNING id`, `executescript` (3 runtime files).
   Step 0 means the *connection* swaps in one file, but every SQL string still
   crosses ~30 runtime modules. This is the bulk of the diff — mechanical but
   wide.
2. **json_extract → jsonb.** 106 occurrences across 6 runtime files (68 in
   save_recipe_api.py alone), plus `json_each` (ingredient explode), plus
   `group_concat` → `string_agg`. `data TEXT` becomes `data jsonb`;
   `json_extract(data,'$.a.b')` becomes `data->'a'->>'b'`. The 24 VIRTUAL
   generated columns on master_recipes and the 5 json_extract expression
   indexes translate cleanly (Postgres generated columns are STORED — fine,
   arguably better per feedback_persist_derived_values).
3. **Upsert dialect.** `INSERT OR REPLACE/IGNORE` in 15 files (~22 sites) →
   `ON CONFLICT DO UPDATE/NOTHING`. (13 files already use ON CONFLICT, which
   ports as-is — SQLite adopted the Postgres form.)
4. **bcc_sortkey.** A Python-registered deterministic function used in ORDER BY
   (`bcc_sortkey(recipe_name)`, `bcc_sortkey(chapter)`). Postgres can't call
   into the app process. Options: (a) port `_sort_key_fold` to an IMMUTABLE
   SQL/plpgsql function, or (b) materialize a `sort_key` column stamped on
   write — (b) is the house style (persist derived values) and lets the sort
   use a plain index.
5. **FTS semantics shift.** fts5 `porter unicode61 remove_diacritics` MATCH →
   `to_tsquery` over `to_tsvector('english', ...)` + unaccent extension.
   Ranking (bm25 → ts_rank) changes result ORDER, not just syntax — the
   two-engine dish search (vector + FTS) needs a behavior re-check, not just a
   compile check. MATCH sites: dish_match.py, save_recipe_api.py,
   equipment_match.py, catalog_store.py (some grep hits are non-SQL).
6. **Backup/DR chain, end to end.** backup_db.py, bcc_backup.bat,
   bcc_backup_scheduled.bat (nightly), the git-side recipes.sql dump, ADAM
   disk mirror, bcc_sync_bailey.ps1 (file-copy + size-verify — meaningless for
   a Postgres cluster; becomes pg_dump/pg_restore or a standby), and
   docs/disaster-recovery.md §5. The "copy the .db file" mental model dies;
   this is the piece with the most operational muscle memory to retrain.
7. **PRAGMA census.** 12+ files issue PRAGMAs (foreign_keys, optimize, etc.) —
   each becomes a no-op, a session SET, or gets deleted.

## Infrastructure reality

Nothing Postgres-shaped exists yet: no psql/pg_dump on PATH, no
psycopg/psycopg2 in the venv (Python 3.13). First concrete step of any pilot
is install: Postgres 17 + pgvector (Windows service, same box) and
`pip install psycopg[binary]`.

## Migration shape (recommendation)

**Big-bang restore with BAILEY as the rehearsal host — not dual-write.**
Dual-write buys safety for systems that can't tolerate a failed cutover window;
this is a single-curator system with a nightly-verified backup chain and a
standby box. The rehearsal IS the safety:

1. ETL script: recipes.db → Postgres (jsonb cast, embeddings BLOB → vector,
   generated cols re-declared, tsvector built). Idempotent, re-runnable.
2. Run the app against Postgres on BAILEY behind a `DB_BACKEND` switch in the
   step-0 factory; drive the real surfaces (save, dish batch, cook, search).
3. Diff the two-engine search results and the list-endpoint orderings
   (bcc_sortkey, ts_rank) against SQLite ground truth.
4. Cutover on MARLEY when the diffs are boring. Keep recipes.db frozen as the
   rollback for a week.

## Decisions the curator owns

1. **Go / no-go.** The wins are real (concurrency, job runner back, vec/FTS
   plumbing dissolves) but the pains have all been *worked around* already —
   nothing is on fire. This is an investment call, not a rescue.
2. **Portable-package stance** (the strategic one): Postgres-only raises the
   self-host bar for the product; SQLite-default + Postgres-optional means
   maintaining the dual-backend factory forever (every new query written
   twice-compatible). A third option: SQLite stays the *product* story,
   Postgres is the *TBOTB corpus* story — which rhymes with the split
   (corpus/local/API are already three entities).
3. **Sidecar DBs** — accept the stay-SQLite recommendation above, or move all
   four for operational uniformity.
4. **Hosting** — native Windows service vs Docker on MARLEY; affects the
   backup chain design and the NSSM/restart playbook.
