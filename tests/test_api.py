"""
Postgres integration tests for /simulate-migration and /approvals.

REQUIRES a dedicated test database. Set TEST_DATABASE_URL explicitly --
this file will NOT fall back to DATABASE_URL under any circumstances,
because setup/teardown here creates and drops tables, and doing that
against the real application database would be destructive.

    pip install -r requirements.txt -r requirements-dev.txt
    docker compose up -d postgres      # also creates aegis_test, see
                                        # docker/init-test-db.sql
    export TEST_DATABASE_URL=postgresql+psycopg://aegis_user:change_me@localhost:5432/aegis_test
    python -m pytest tests/test_api.py -v

Written and syntax-checked but NOT executed: no Docker, Postgres,
sqlalchemy, or network access in the environment these were written
in. Treat every assertion here as a draft until you've run it.
"""

import os

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

if not TEST_DATABASE_URL:
    pytest.skip(
        "TEST_DATABASE_URL is not set. These are Postgres integration "
        "tests against a dedicated test database and must never fall "
        "back to DATABASE_URL -- that would point at the real "
        "application database, which this file's setup/teardown would "
        "then create and drop tables against. See the module docstring.",
        allow_module_level=True,
    )

# Everything below is only imported once TEST_DATABASE_URL is confirmed
# set. Importing src.api.app (and therefore src.db.session) BEFORE that
# check would raise RuntimeError immediately -- session.py refuses to
# start without DATABASE_URL. That was the bug in the first draft of
# this file: the skip check existed, but ran too late to matter.
#
# Also note every import below is src.-qualified, matching app.py
# exactly. The first draft imported `from db.session import get_db`
# (unqualified) while app.py uses `from src.db.session import get_db` --
# those are two different module objects, so
# `app.dependency_overrides[get_db] = ...` silently overrode the wrong
# key and tests could have hit the real DATABASE_URL instead of this
# test database.
import threading
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from src.api.app import app
from src.db.session import get_db
from src.db.models import Base, ApprovalTicketRecord, HealingManifestRecord
from src.consultant.consultant import RepairPlan
from src.governance.approval import TicketNotPendingError
from src.governance.approval_repository import PostgresApprovalRepository


engine = create_engine(TEST_DATABASE_URL)
TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def setup_module(_module):
    Base.metadata.create_all(bind=engine)


def teardown_module(_module):
    Base.metadata.drop_all(bind=engine)


def setup_function(_):
    with engine.begin() as conn:
        conn.execute(HealingManifestRecord.__table__.delete())
        conn.execute(ApprovalTicketRecord.__table__.delete())


def _submit_rename_scenario():
    return client.post(
        "/simulate-migration",
        json={
            "gold_schema": {"customer_id": "int64"},
            "sample_data": {"Customer_ID": [1, 2, 3]},
        },
    )


def test_requires_human_approval_creates_pending_ticket_not_execution():
    response = _submit_rename_scenario()
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "PENDING_APPROVAL"
    assert body["governance_decision"] == "REQUIRES_HUMAN_APPROVAL"
    assert "ticket_id" in body


def test_pending_ticket_survives_a_fresh_session():
    """
    Closest practical proxy for "survives an API restart" without
    literally killing and restarting the uvicorn process mid-test:
    fetches through a brand-new Session, not the one that created it.
    """
    ticket_id = _submit_rename_scenario().json()["ticket_id"]

    fetched = client.get(f"/approvals/{ticket_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "PENDING"


def test_approve_executes_persists_manifest_and_clears_from_pending():
    ticket_id = _submit_rename_scenario().json()["ticket_id"]

    response = client.post(
        f"/approvals/{ticket_id}/approve",
        json={"operator": "mo", "note": "looks right"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "APPROVED"
    assert body["decided_by"] == "mo"
    assert "execution_result" in body
    assert client.get("/approvals").json()["pending_count"] == 0

    with TestSessionLocal() as db:
        count = (
            db.query(HealingManifestRecord)
            .filter(HealingManifestRecord.ticket_id == ticket_id)
            .count()
        )
        assert count == 1


def test_reject_discards_without_executing_or_persisting_manifest():
    ticket_id = _submit_rename_scenario().json()["ticket_id"]

    response = client.post(
        f"/approvals/{ticket_id}/reject",
        json={"operator": "mo", "note": "not yet"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "REJECTED"
    assert "execution_result" not in body

    with TestSessionLocal() as db:
        assert db.query(HealingManifestRecord).count() == 0


def test_unknown_ticket_returns_404():
    response = client.post("/approvals/does-not-exist/approve", json={"operator": "mo"})
    assert response.status_code == 404


def test_repeated_approval_returns_409():
    ticket_id = _submit_rename_scenario().json()["ticket_id"]
    client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})

    second = client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})
    assert second.status_code == 409


def test_execution_failure_rolls_back_approval():
    """Ticket must stay PENDING, not get stranded as APPROVED with no manifest."""
    ticket_id = _submit_rename_scenario().json()["ticket_id"]

    with patch("src.api.app.AegisSurgeon.execute", side_effect=RuntimeError("boom")):
        response = client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})

    assert response.status_code == 500
    assert client.get(f"/approvals/{ticket_id}").json()["status"] == "PENDING"

    with TestSessionLocal() as db:
        count = (
            db.query(HealingManifestRecord)
            .filter(HealingManifestRecord.ticket_id == ticket_id)
            .count()
        )
        assert count == 0


def test_manifest_persistence_failure_rolls_back_approval():
    ticket_id = _submit_rename_scenario().json()["ticket_id"]

    with patch("src.api.app.save_manifest", side_effect=RuntimeError("boom")):
        response = client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})

    assert response.status_code == 500
    assert client.get(f"/approvals/{ticket_id}").json()["status"] == "PENDING"


def test_concurrent_approval_executes_only_once():
    """
    Two threads, two independent Sessions, both calling approve()
    directly against the repository -- NOT through TestClient/HTTP.
    TestClient's concurrency model isn't reliable enough to depend on
    for proving a database-level row lock; calling the repository
    directly with real separate Sessions tests the actual mechanism
    (with_for_update()) rather than however TestClient happens to
    schedule things internally.
    """
    ticket_id = _submit_rename_scenario().json()["ticket_id"]

    outcomes = []
    lock = threading.Lock()

    def attempt():
        session = TestSessionLocal()
        try:
            PostgresApprovalRepository(session).approve(ticket_id, operator="concurrent-test")
            session.commit()
            with lock:
                outcomes.append("approved")
        except TicketNotPendingError:
            session.rollback()
            with lock:
                outcomes.append("rejected")
        finally:
            session.close()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["approved", "rejected"]


def test_dataset_with_null_values_persists_successfully():
    response = client.post(
        "/simulate-migration",
        json={
            "gold_schema": {"customer_id": "int64"},
            "sample_data": {"Customer_ID": [1, None, 3]},
        },
    )
    body = response.json()
    assert response.status_code == 200

    fetched = client.get(f"/approvals/{body['ticket_id']}")
    assert fetched.status_code == 200


def test_auto_approve_executes_immediately_and_persists_manifest_without_ticket():
    """
    Consultant still can't produce confidence >=0.92 on its own (see
    Phase 2.2 finding). Patches it to prove the AUTO_APPROVE branch
    persists correctly, including the nullable ticket_id.
    """
    fake_plan = RepairPlan(
        proposed_action="RENAME_COLUMN Customer_ID -> customer_id",
        confidence=0.95,
        explanation="forced high-confidence plan for this test",
    )
    with patch("src.api.app.AegisConsultant.propose_repairs", return_value=[fake_plan]):
        response = _submit_rename_scenario()
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "EXECUTED"
    assert body["governance_decision"] == "AUTO_APPROVE"

    with TestSessionLocal() as db:
        manifests = db.query(HealingManifestRecord).all()
        assert len(manifests) == 1
        assert manifests[0].ticket_id is None


def test_quarantine_when_only_low_confidence_plans_exist():
    fake_plan = RepairPlan(
        proposed_action="CAST_COLUMN amount TO float64 WITH_DROP_INVALID",
        confidence=0.50,
        explanation="forced low-confidence plan for this test",
    )
    with patch("src.api.app.AegisConsultant.propose_repairs", return_value=[fake_plan]):
        response = client.post(
            "/simulate-migration",
            json={
                "gold_schema": {"amount": "float64"},
                "sample_data": {"amount": ["10.5", "20.25"]},
            },
        )
    body = response.json()

    assert response.status_code == 200
    assert body["message"] == "No repair plan survived governance review; all candidates quarantined."


def test_alembic_upgrade_and_downgrade():
    """
    Runs last on purpose -- drops and recreates the schema via Alembic
    itself rather than Base.metadata, then restores it afterward so
    any test running after this one still finds tables in place.
    """
    from alembic.config import Config
    from alembic import command

    project_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)

    Base.metadata.drop_all(bind=engine)

    command.upgrade(cfg, "head")
    tables = sa_inspect(engine).get_table_names()
    assert "approval_tickets" in tables
    assert "healing_manifests" in tables

    command.downgrade(cfg, "base")
    tables_after = sa_inspect(engine).get_table_names()
    assert "approval_tickets" not in tables_after
    assert "healing_manifests" not in tables_after

    Base.metadata.create_all(bind=engine)
