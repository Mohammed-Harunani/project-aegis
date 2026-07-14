"""
API-level tests for the Postgres-backed /simulate-migration and
/approvals endpoints (Phase 2.3).

REQUIRES A LIVE POSTGRES DATABASE -- these are integration tests, not
unit tests. Set TEST_DATABASE_URL (falls back to DATABASE_URL) before
running:

    pip install -r requirements.txt -r requirements-dev.txt
    docker compose up -d postgres
    export TEST_DATABASE_URL=postgresql+psycopg://aegis_user:change_me@localhost:5432/aegis
    python -m pytest tests/test_api.py -v

These were written and syntax-checked, but NOT executed: the sandbox
they were written in has no Docker, no Postgres, no network to install
sqlalchemy/psycopg/httpx. Treat every assertion here as a draft until
you've actually run it once.
"""

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.app import app
from db.session import get_db
from db.models import Base, ApprovalTicketRecord, HealingManifestRecord
from consultant.consultant import RepairPlan


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")

if not TEST_DATABASE_URL:
    pytest.skip(
        "TEST_DATABASE_URL (or DATABASE_URL) is not set -- these tests "
        "need a live Postgres instance, see module docstring.",
        allow_module_level=True,
    )

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
    # Clean slate per test -- truncate rather than drop/recreate tables,
    # cheaper and doesn't fight Alembic's migration history.
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


def test_pending_ticket_is_readable_from_a_fresh_session():
    """
    The actual point of Phase 2.3: fetching through a brand-new Session
    (not the one that created the ticket) proves it's durable in
    Postgres, not just held in the request that created it. This is
    the closest practical proxy for "survives an API restart" without
    literally killing and restarting the uvicorn process mid-test.
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


def test_auto_approve_executes_immediately_and_persists_manifest_without_ticket():
    """
    Consultant still can't produce confidence >=0.92 on its own (see
    Phase 2.2 finding) -- this patches it to prove the AUTO_APPROVE
    branch itself persists correctly, including the nullable ticket_id.
    """
    fake_plan = RepairPlan(
        proposed_action="RENAME_COLUMN Customer_ID -> customer_id",
        confidence=0.95,
        explanation="forced high-confidence plan for this test",
    )
    with patch("api.app.AegisConsultant.propose_repairs", return_value=[fake_plan]):
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
    with patch("api.app.AegisConsultant.propose_repairs", return_value=[fake_plan]):
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
