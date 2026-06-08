"""Enrich's own database — its reference-data home.

Enrich is stateless about CALLER content: it never persists a recipe, a corpus,
or any user data (BYOK, seal-at-emit). But as a standalone product it owns the
curated reference knowledge it needs to do its job — ingredient densities and
aliases today; prompt catalogs, unit tables, locale phrase tables tomorrow. That
data lives HERE, in Enrich's own database, separate from BCC's recipes.db and
TBOTB's corpus, so when the API splits out to its own process the data travels
with it. See docs/split-decisions-log.md (the "stateless about caller content,
owns its own reference DBs" decision).

One bootstrap pointer: ENRICH_DB (env) overrides the default location; the file
is created on first use. Default: enrich/data/enrich.db (git-ignored).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

_DEFAULT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
ENRICH_DB_PATH = os.environ.get("ENRICH_DB", os.path.join(_DEFAULT_DIR, "enrich.db"))


@contextmanager
def connect():
    """Yield a connection to Enrich's DB, committing on clean exit. Creates the
    parent directory and file on first use."""
    os.makedirs(os.path.dirname(ENRICH_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(ENRICH_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
