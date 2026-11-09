"""pgmigrate — the recipes.db → PostgreSQL ETL (docs/postgres-etl.md).

One program, three stages, re-runnable:

    python -m pgmigrate plan   --sqlite recipes.db                 # what would be built
    python -m pgmigrate run    --sqlite recipes.db --dsn ... --drop # build schema, copy, index, verify
    python -m pgmigrate verify --sqlite recipes.db --dsn ...        # compare the two engines

Scope is recipes.db ONLY (docs/postgres-migration-inventory.md): page_cache.db,
training.db and media.db stay SQLite and move as files. The target DSN comes
from --dsn or the BCC_PG_DSN environment variable.
"""
