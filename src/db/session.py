"""
Aegis_DB_Session
Phase 2.3 -- engine/session setup, driven entirely by DATABASE_URL.

No credentials or connection details are hardcoded here or anywhere
else in the codebase -- see .env.example for the expected shape.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Copy .env.example to .env and fill it "
        "in (or export it directly). Aegis will not guess at database "
        "credentials or fall back to a default connection."
    )

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """FastAPI dependency: one Session per request, always closed after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
