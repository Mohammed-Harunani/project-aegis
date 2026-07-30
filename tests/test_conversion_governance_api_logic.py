"""Database-free API orchestration tests for Phase 3.1.4 governance."""

import os
from datetime import UTC, datetime

import pandas as pd
import pytest
from fastapi import HTTPException

os.environ.setdefault(
    "DATABASE_URL",
    "sqlite+pysqlite:///:memory:",
)

import src.api.app as api_module
from src.consultant.consultant import RepairPlan
from src.governance.approval import ApprovalTicket
from src.governance.conversion_safety import analyze_cast_plan
from src.inspector import AegisInspector


class FakeDB:
    def __init__(self):
        self.rollback_calls = 0
        self.commit_calls = 0

    def rollback(self):
        self.rollback_calls += 1

    def commit(self):
        self.commit_calls += 1


def _schemas(df, gold_df):
    inspector = AegisInspector()
    return (
        inspector.generate_observed_schema(df),
        inspector.generate_observed_schema(gold_df),
    )


def _approved_cast_ticket(df, decision):
    observed, gold = _schemas(df, pd.DataFrame({"amount": [1, 2, 3]}))
    plan = RepairPlan(
        proposed_action="CAST_COLUMN amount TO int64",
        confidence=0.85,
        explanation="verified cast",
    )
    now = datetime.now(UTC).isoformat()
    return ApprovalTicket(
        ticket_id="11111111-1111-1111-1111-111111111111",
        repair_plan=plan,
        confidence=plan.confidence,
        status="APPROVED",
        created_at=now,
        observed_schema=observed,
        gold_schema=gold,
        target_dataset=df,
        decided_by="mo",
        decided_at=now,
        decision_note="approved",
        conversion_decision=decision,
    )


def test_simulate_unsafe_cast_returns_redacted_block_without_ticket(monkeypatch):
    class Repo:
        def __init__(self, db):
            raise AssertionError("unsafe conversion must not construct approval repository")

    # Repository is constructed at endpoint start in legacy flow, so use a
    # passive fake and assert submit is never called instead.
    class PassiveRepo:
        submitted = False

        def __init__(self, db):
            pass

        def submit(self, **kwargs):
            type(self).submitted = True
            raise AssertionError("unsafe conversion must not create a ticket")

    monkeypatch.setattr(api_module, "PostgresApprovalRepository", PassiveRepo)

    request = api_module.MigrationRequest(
        gold_schema={"amount": "int64"},
        sample_data={"amount": [1.5, 2.0]},
    )
    result = api_module.simulate_migration(request, db=FakeDB())

    assert result["status"] == "BLOCKED_BY_CONVERSION_SAFETY"
    assert result["conversion_decision"]["status"] == "UNSAFE"
    assert "FRACTIONAL_VALUE" in result["conversion_decision"]["reason_codes"]
    assert PassiveRepo.submitted is False


def test_simulate_safe_cast_persists_decision_on_pending_ticket(monkeypatch):
    captured = {}

    class Repo:
        def __init__(self, db):
            pass

        def submit(self, **kwargs):
            captured.update(kwargs)
            plan = kwargs["repair_plan"]
            return ApprovalTicket(
                ticket_id="22222222-2222-2222-2222-222222222222",
                repair_plan=plan,
                confidence=plan.confidence,
                status="PENDING",
                created_at=datetime.now(UTC).isoformat(),
                observed_schema=kwargs["observed_schema"],
                gold_schema=kwargs["gold_schema"],
                target_dataset=kwargs["target_dataset"],
                schema_version_id=kwargs.get("schema_version_id"),
                live_eligible=kwargs.get("live_eligible", False),
                conversion_decision=kwargs.get("conversion_decision"),
            )

    monkeypatch.setattr(api_module, "PostgresApprovalRepository", Repo)

    request = api_module.MigrationRequest(
        gold_schema={"amount": "int64"},
        sample_data={"amount": ["1", "2", "-3"]},
    )
    result = api_module.simulate_migration(request, db=FakeDB())

    assert result["status"] == "PENDING_APPROVAL"
    assert result["governance_decision"] == "REQUIRES_HUMAN_APPROVAL"
    assert captured["conversion_decision"].status == "SAFE"
    assert result["conversion_decision"] == captured["conversion_decision"].to_dict()


def test_approve_unsafe_cast_is_blocked_before_surgeon(monkeypatch):
    df = pd.DataFrame({"amount": [1.5]})
    plan = RepairPlan("CAST_COLUMN amount TO int64", 0.85, "cast")
    unsafe = analyze_cast_plan(plan, df)
    ticket = _approved_cast_ticket(df, unsafe)

    class Repo:
        def __init__(self, db):
            pass

        def approve(self, ticket_id, operator, note):
            return ticket

    monkeypatch.setattr(api_module, "PostgresApprovalRepository", Repo)

    class SurgeonMustNotRun:
        def execute(self, **kwargs):
            raise AssertionError("unsafe approval must be blocked before Surgeon")

    monkeypatch.setattr(api_module, "AegisSurgeon", SurgeonMustNotRun)

    db = FakeDB()
    with pytest.raises(HTTPException) as exc:
        api_module.approve_ticket(
            ticket.ticket_id,
            api_module.ApprovalDecisionRequest(operator="mo"),
            db=db,
        )

    assert exc.value.status_code == 422
    assert db.rollback_calls == 1
    assert db.commit_calls == 0


def test_approve_safe_cast_requires_matching_execution_outcome(monkeypatch):
    df = pd.DataFrame({"amount": ["1", "2", "-3"]})
    plan = RepairPlan("CAST_COLUMN amount TO int64", 0.85, "cast")
    safe = analyze_cast_plan(plan, df)
    ticket = _approved_cast_ticket(df, safe)
    saved = {}

    class Repo:
        def __init__(self, db):
            pass

        def approve(self, ticket_id, operator, note):
            return ticket

    monkeypatch.setattr(api_module, "PostgresApprovalRepository", Repo)

    def fake_save_manifest(db, manifest, **kwargs):
        saved["manifest"] = manifest
        saved["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(api_module, "save_manifest", fake_save_manifest)

    db = FakeDB()
    result = api_module.approve_ticket(
        ticket.ticket_id,
        api_module.ApprovalDecisionRequest(operator="mo", note="safe"),
        db=db,
    )

    assert result["status"] == "APPROVED"
    assert result["conversion_decision"] == safe.to_dict()
    assert result["conversion_outcome"] == safe.to_dict()
    assert saved["manifest"].conversion_outcome == safe
    assert saved["kwargs"]["commit"] is False
    assert db.rollback_calls == 0
    assert db.commit_calls == 1


def test_approve_stale_safe_decision_is_blocked(monkeypatch):
    original = pd.DataFrame({"amount": ["1", "2", "-3"]})
    plan = RepairPlan("CAST_COLUMN amount TO int64", 0.85, "cast")
    safe = analyze_cast_plan(plan, original)
    changed = pd.DataFrame({"amount": ["1", "bad", "-3"]})
    ticket = _approved_cast_ticket(changed, safe)

    class Repo:
        def __init__(self, db):
            pass

        def approve(self, ticket_id, operator, note):
            return ticket

    monkeypatch.setattr(api_module, "PostgresApprovalRepository", Repo)

    db = FakeDB()
    with pytest.raises(HTTPException) as exc:
        api_module.approve_ticket(
            ticket.ticket_id,
            api_module.ApprovalDecisionRequest(operator="mo"),
            db=db,
        )

    assert exc.value.status_code == 422
    assert "remains PENDING" in exc.value.detail
    assert db.rollback_calls == 1
    assert db.commit_calls == 0


def test_live_cast_remains_blocked_before_manifest_or_database_work(monkeypatch):
    df = pd.DataFrame({"amount": ["1", "2", "-3"]})
    plan = RepairPlan("CAST_COLUMN amount TO int64", 0.85, "cast")
    safe = analyze_cast_plan(plan, df)
    ticket = _approved_cast_ticket(df, safe)

    class Repo:
        def __init__(self, db):
            pass

        def lock_for_live_execution(self, ticket_id):
            return ticket

    monkeypatch.setattr(api_module, "PostgresApprovalRepository", Repo)
    monkeypatch.setattr(api_module, "live_execution_globally_enabled", lambda: True)

    class QueryMustNotRunDB(FakeDB):
        def query(self, *args, **kwargs):
            raise AssertionError("live CAST_COLUMN must stop before manifest query")

    with pytest.raises(HTTPException) as exc:
        api_module.execute_live(
            ticket.ticket_id,
            api_module.ExecuteLiveRequest(
                operator="mo",
                logical_target="customer_master",
                confirm=True,
            ),
            db=QueryMustNotRunDB(),
            live_engine=object(),
            source_engine=object(),
        )

    assert exc.value.status_code == 422
    assert "Phase 3.1.6" in exc.value.detail
