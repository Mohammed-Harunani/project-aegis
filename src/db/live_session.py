"""
Aegis_LiveDBSession
Phase 2.5 -- engine/session for LIVE_DATABASE_URL, kept entirely
separate from src/db/session.py's DATABASE_URL (governance data).

Deliberately lazy: unlike src/db/session.py (where DATABASE_URL is
validated the moment the module is imported), LIVE_DATABASE_URL is
only validated when get_live_engine()/get_live_db() is actually
called. Validating eagerly at import time would make LIVE_DATABASE_URL
a requirement just to import api.app at all -- breaking every
existing test (test_api.py, test_schema_registry_api.py) that has
nothing to do with live execution. Only code that actually attempts
a live write needs this to be configured.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

_live_engine = None
_LiveSessionLocal = None


def _get_live_database_url() -> str:
    url = os.environ.get("LIVE_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "LIVE_DATABASE_URL is not set. Live execution requires an "
            "explicitly configured external PostgreSQL target -- Aegis "
            "will not guess at or default to any connection."
        )
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql"):
        raise RuntimeError(
            f"LIVE_DATABASE_URL must be a PostgreSQL connection string "
            f"(got drivername {parsed.drivername!r}) -- the live writer "
            f"depends on PostgreSQL-specific transactional DDL and SQL "
            f"that will not work against any other database."
        )
    if not parsed.database:
        raise RuntimeError("LIVE_DATABASE_URL must specify a database name.")
    if parsed.database in ("aegis", "aegis_test"):
        raise RuntimeError(
            f"LIVE_DATABASE_URL must not point at Aegis's own governance "
            f"database ({parsed.database!r}). Configure a genuinely "
            f"external target."
        )
    return url


def get_live_engine():
    """Lazily creates (once) and returns the live-target engine."""
    global _live_engine
    if _live_engine is None:
        _live_engine = create_engine(_get_live_database_url(), pool_pre_ping=True)
    return _live_engine


def get_live_db():
    """FastAPI dependency: one Session per request against the live target."""
    global _LiveSessionLocal
    if _LiveSessionLocal is None:
        _LiveSessionLocal = sessionmaker(bind=get_live_engine(), autoflush=False, autocommit=False)
    db = _LiveSessionLocal()
    try:
        yield db
    finally:
        db.close()
