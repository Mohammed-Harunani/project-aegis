import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

sys.path.insert(0, str(SRC_DIR))
# Also needed so `api.app`'s internal `from src.x import y` imports resolve --
# without this, any test importing api.app fails at import time, before
# a single test runs. Existing tests are unaffected: they only ever import
# unqualified (`from inspector import ...`), which still resolves via SRC_DIR.
sys.path.insert(0, str(PROJECT_ROOT))


def get_verified_test_database_url():
    """
    Shared guard for any test module that needs a live Postgres test
    database. Used by tests/test_schema_registry_api.py; test_api.py
    predates this helper and keeps its own already-verified copy of
    the same logic rather than being refactored to depend on it.

    - Requires TEST_DATABASE_URL; skips the whole calling module
      (not just one test) if it isn't set, rather than falling back
      to DATABASE_URL -- that would point at the real application
      database, which callers' setup/teardown create/drop tables in.
    - Requires the target database to be named exactly 'aegis_test',
      parsed via SQLAlchemy's make_url() -- closes the gap where the
      variable is set, just to the wrong value.
    - Sets os.environ["DATABASE_URL"] to the same safe value, since
      src.db.session builds a module-level engine from DATABASE_URL
      the moment anything imports src.api.app, independently of
      TEST_DATABASE_URL, and refuses to start without it.

    Call this before importing src.api.app or anything that imports it.
    """
    import os
    import pytest

    test_url = os.environ.get("TEST_DATABASE_URL")
    if not test_url:
        pytest.skip(
            "TEST_DATABASE_URL is not set. These are Postgres integration "
            "tests against a dedicated test database and must never fall "
            "back to DATABASE_URL. See Docs/phase2_3_persistence_spec.md.",
            allow_module_level=True,
        )

    from sqlalchemy.engine import make_url

    parsed = make_url(test_url)
    if parsed.database != "aegis_test":
        raise RuntimeError(
            f"TEST_DATABASE_URL must point at the dedicated 'aegis_test' "
            f"database, not {parsed.database!r}. Refusing to run tests "
            f"that create/drop tables against anything else."
        )

    os.environ["DATABASE_URL"] = test_url
    return test_url


def get_verified_live_test_database_url():
    """
    Same shape as get_verified_test_database_url(), for Phase 2.5's
    live-execution tests. The live target must be a THIRD database,
    distinct from both aegis and aegis_test -- the live writer itself
    refuses those two as targets, so testing live writes needs
    aegis_live_test (or equivalent) instead.
    """
    import os
    import pytest

    live_test_url = os.environ.get("LIVE_TEST_DATABASE_URL")
    if not live_test_url:
        pytest.skip(
            "LIVE_TEST_DATABASE_URL is not set. Live-execution tests need "
            "a real, disposable target database distinct from aegis and "
            "aegis_test. See Docs/phase2_5_live_execution_spec.md.",
            allow_module_level=True,
        )

    from sqlalchemy.engine import make_url

    parsed = make_url(live_test_url)
    if parsed.database in ("aegis", "aegis_test"):
        raise RuntimeError(
            f"LIVE_TEST_DATABASE_URL must not be {parsed.database!r} -- "
            f"use a distinct database (e.g. aegis_live_test)."
        )

    os.environ["LIVE_DATABASE_URL"] = live_test_url
    return live_test_url
