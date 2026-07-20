"""
Aegis_SourceDBSession
Phase 2.5 final architecture -- engine for SOURCE_DATABASE_URL, the
trusted PostgreSQL source Aegis reads complete datasets from for
live-capable simulation. Entirely separate from src/db/session.py
(governance data, DATABASE_URL) and src/db/live_session.py (the live
publication target, LIVE_DATABASE_URL) -- three distinct databases
Aegis knows about, none of them inferred from another.

Deliberately lazy, same reasoning as live_session.py: validating
SOURCE_DATABASE_URL only when get_source_engine() is actually called
(not at module import time) means importing api.app doesn't require
it to be configured -- only code that actually reads from the source
does.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

_source_engine = None


def _get_source_database_url() -> str:
    url = os.environ.get("SOURCE_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "SOURCE_DATABASE_URL is not set. Live-capable simulation requires "
            "an explicitly configured trusted PostgreSQL source -- Aegis will "
            "not guess at or default to any connection."
        )
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        raise RuntimeError(
            f"SOURCE_DATABASE_URL must be a PostgreSQL connection string "
            f"(got drivername {parsed.drivername!r}) -- the source connector "
            f"depends on PostgreSQL-specific catalog queries (pg_index, "
            f"information_schema) that will not work against any other "
            f"database."
        )
    if not parsed.database:
        raise RuntimeError("SOURCE_DATABASE_URL must specify a database name.")
    if parsed.database in ("aegis", "aegis_test"):
        raise RuntimeError(
            f"SOURCE_DATABASE_URL must not point at Aegis's own governance "
            f"database ({parsed.database!r}). Configure a genuinely external "
            f"source."
        )
    return url


def get_source_engine():
    """Lazily creates (once) and returns the trusted-source engine."""
    global _source_engine
    if _source_engine is None:
        _source_engine = create_engine(_get_source_database_url(), pool_pre_ping=True)
    return _source_engine
