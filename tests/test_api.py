"""
API-level tests for the /simulate-migration and /approvals endpoints.

NOTE: fastapi is not installed in the environment these were written in
(no network access to install it), so these could not be executed there.
Everything else in this test suite has been run and verified directly;
these have only been syntax-checked and reasoned through carefully.
Run `pip install httpx` if it's not already present -- FastAPI's
TestClient depends on it -- then `python -m pytest tests/test_api.py -v`
to confirm before treating Phase 2.2 as closed.
"""

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import app, approval_queue
from consultant.consultant import RepairPlan


client = TestClient(app)


def setup_function(_):
    # approval_queue is a module-level singleton in api.app (on purpose --
    # it has to survive between the /simulate-migration call and the later
    # /approvals call). That means it leaks state between tests unless
    # each test starts from a clean slate.
    approval_queue._tickets.clear()


def test_requires_human_approval_creates_pending_ticket_not_execution():
    # Rename-only scenario -> Consultant emits confidence 0.85 ->
    # REQUIRES_HUMAN_APPROVAL band (0.80-0.92).
    response = client.post(
        "/simulate-migration",
        json={
            "gold_schema": {"customer_id": "int64"},
            "sample_data": {"Customer_ID": [1, 2, 3]},
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "PENDING_APPROVAL"
    assert body["governance_decision"] == "REQUIRES_HUMAN_APPROVAL"
    assert "ticket_id" in body
    assert len(approval_queue.list_pending()) == 1


def test_approve_executes_and_clears_from_pending():
    submit = client.post(
        "/simulate-migration",
        json={
            "gold_schema": {"customer_id": "int64"},
            "sample_data": {"Customer_ID": [1, 2, 3]},
        },
    )
    ticket_id = submit.json()["ticket_id"]

    response = client.post(
        f"/approvals/{ticket_id}/approve",
        json={"operator": "mo", "note": "looks right"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "APPROVED"
    assert body["decided_by"] == "mo"
    assert "execution_result" in body
    assert len(approval_queue.list_pending()) == 0


def test_reject_discards_without_executing():
    submit = client.post(
        "/simulate-migration",
        json={
            "gold_schema": {"customer_id": "int64"},
            "sample_data": {"Customer_ID": [1, 2, 3]},
        },
    )
    ticket_id = submit.json()["ticket_id"]

    response = client.post(
        f"/approvals/{ticket_id}/reject",
        json={"operator": "mo", "note": "not yet"},
    )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "REJECTED"
    assert "execution_result" not in body


def test_unknown_ticket_returns_404():
    response = client.post("/approvals/does-not-exist/approve", json={"operator": "mo"})
    assert response.status_code == 404


def test_repeated_approval_returns_409():
    submit = client.post(
        "/simulate-migration",
        json={
            "gold_schema": {"customer_id": "int64"},
            "sample_data": {"Customer_ID": [1, 2, 3]},
        },
    )
    ticket_id = submit.json()["ticket_id"]
    client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})

    second = client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})
    assert second.status_code == 409


def test_auto_approve_executes_immediately():
    """
    Consultant currently only ever emits confidence 0.85 or 0.70 -- it
    cannot produce an AUTO_APPROVE-tier (>=0.92) plan on its own, so a
    real request can never reach this branch yet. Patches
    Consultant.propose_repairs to prove the branch itself is correct,
    independent of that gap. Worth flagging to whoever owns Consultant
    calibration next -- AUTO_APPROVE is currently dead code.
    """
    fake_plan = RepairPlan(
        proposed_action="RENAME_COLUMN Customer_ID -> customer_id",
        confidence=0.95,
        explanation="forced high-confidence plan for this test",
    )
    with patch("api.app.AegisConsultant.propose_repairs", return_value=[fake_plan]):
        response = client.post(
            "/simulate-migration",
            json={
                "gold_schema": {"customer_id": "int64"},
                "sample_data": {"Customer_ID": [1, 2, 3]},
            },
        )
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "EXECUTED"
    assert body["governance_decision"] == "AUTO_APPROVE"
    assert len(approval_queue.list_pending()) == 0


def test_quarantine_when_only_low_confidence_plans_exist():
    """
    Mirrors the gap above: Consultant always pairs its 0.70
    WITH_DROP_INVALID plan with an 0.85 sibling for the same column,
    so a real request can never land with ONLY quarantine-tier plans
    on the table. Patches Consultant to prove RepairSelector +
    the endpoint correctly refuse to execute anything in that case.
    """
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
    assert len(approval_queue.list_pending()) == 0
