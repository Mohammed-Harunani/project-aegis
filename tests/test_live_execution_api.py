"""
Postgres integration tests for Phase 2.5 live execution: safety gates
enforced end-to-end, actual promotion to a real (disposable) target
database, crash-recovery reconciliation, and rollback.

Needs BOTH TEST_DATABASE_URL (governance -- aegis_test) and
LIVE_TEST_DATABASE_URL (the live target -- aegis_live_test, a THIRD
database, since the writer itself refuses aegis/aegis_test as targets).

    docker compose up -d postgres   # creates aegis_test AND aegis_live_test
    export TEST_DATABASE_URL=postgresql+psycopg://aegis_user:change_me@localhost:5432/aegis_test
    export LIVE_TEST_DATABASE_URL=postgresql+psycopg://aegis_user:change_me@localhost:5432/aegis_live_test
    python -m pytest tests/test_live_execution_api.py -v

NOTE: if your Postgres data volume predates Phase 2.5, the init script
won't re-run automatically (it only fires on first container
initialization) -- create aegis_live_test manually first:
    docker compose exec postgres psql -U aegis_user -d postgres \
        -c "CREATE DATABASE aegis_live_test;"

Written and syntax-checked but NOT executed -- same constraint as
every Postgres-dependent piece of this project, and the stakes on
that gap are higher here than anywhere else: this is the one place
Aegis writes to something other than its own governance database.
Treat every assertion here as a draft until run against a real,
disposable Postgres target.
"""

import sys
import os
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import get_verified_test_database_url, get_verified_live_test_database_url

TEST_DATABASE_URL = get_verified_test_database_url()
LIVE_TEST_DATABASE_URL = get_verified_live_test_database_url()

os.environ["AEGIS_LIVE_TARGET_SCHEMA_ALLOWLIST"] = "public"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from src.api.app import app
from src.db.session import get_db
from src.db.live_session import get_live_engine
from src.db.models import (
    Base,
    ApprovalTicketRecord,
    HealingManifestRecord,
    GoldSchemaRecord,
    SchemaVersionRecord,
    LiveExecutionRecord,
)
from src.live_execution.safety import live_execution_globally_enabled
from src.live_execution.repository import LiveExecutionRepository
from src.live_execution.writer import PostgresLiveWriter


engine = create_engine(TEST_DATABASE_URL)
TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

live_target_engine = create_engine(LIVE_TEST_DATABASE_URL)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


def override_get_live_engine():
    return live_target_engine


app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_live_engine] = override_get_live_engine
client = TestClient(app)


def setup_module(_module):
    Base.metadata.create_all(bind=engine)


def teardown_module(_module):
    Base.metadata.drop_all(bind=engine)


def setup_function(_):
    os.environ["AEGIS_LIVE_EXECUTION_ENABLED"] = "true"
    with engine.begin() as conn:
        conn.execute(LiveExecutionRecord.__table__.delete())
        conn.execute(HealingManifestRecord.__table__.delete())
        conn.execute(ApprovalTicketRecord.__table__.delete())
        conn.execute(SchemaVersionRecord.__table__.delete())
        conn.execute(GoldSchemaRecord.__table__.delete())
    with live_target_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND (tablename LIKE 'customer_master_live%' OR tablename = '_aegis_execution_log')"
        )).fetchall()
        for (table_name,) in rows:
            conn.execute(text(f'DROP TABLE IF EXISTS "public"."{table_name}"'))


def _register_and_approve_registry_ticket(schema_name="customer_master_live_test"):
    client.post(f"/schemas/{schema_name}/versions", json={
        "format_version": 1, "created_by": "mo",
        "columns": [{"name": "customer_id", "dtype": "int64"}],
    })
    submit = client.post("/simulate-migration", json={
        "schema_name": schema_name,
        "sample_data": {"Customer_ID": [1, 2, 3]},
    }).json()
    ticket_id = submit["ticket_id"]
    client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})
    return ticket_id, submit["schema_version_id"]


def _execute_live_body(target_table, target_schema="public", confirm=True, operator="mo", **overrides):
    body = {
        "operator": operator,
        "target_schema": target_schema,
        "target_table": target_table,
        "source_schema": "raw",
        "source_table": "customer_master_source",
        "confirm": confirm,
    }
    body.update(overrides)
    return body


def test_execute_live_disabled_by_default_returns_403():
    os.environ["AEGIS_LIVE_EXECUTION_ENABLED"] = "false"
    ticket_id, _ = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_1"),
    )
    assert response.status_code == 403


def test_execute_live_requires_approved_ticket():
    client.post("/schemas/customer_master_live_test/versions", json={
        "format_version": 1, "created_by": "mo",
        "columns": [{"name": "customer_id", "dtype": "int64"}],
    })
    submit = client.post("/simulate-migration", json={
        "schema_name": "customer_master_live_test",
        "sample_data": {"Customer_ID": [1, 2, 3]},
    }).json()
    response = client.post(
        f"/approvals/{submit['ticket_id']}/execute-live",
        json=_execute_live_body("customer_master_live_2"),
    )
    assert response.status_code == 409


def test_execute_live_rejects_legacy_gold_schema_ticket():
    submit = client.post("/simulate-migration", json={
        "gold_schema": {"customer_id": "int64"},
        "sample_data": {"Customer_ID": [1, 2, 3]},
    }).json()
    client.post(f"/approvals/{submit['ticket_id']}/approve", json={"operator": "mo"})
    response = client.post(
        f"/approvals/{submit['ticket_id']}/execute-live",
        json=_execute_live_body("customer_master_live_3"),
    )
    assert response.status_code == 422


def test_execute_live_rejects_non_allowlisted_schema():
    ticket_id, _ = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_4", target_schema="not_allowlisted"),
    )
    assert response.status_code == 422


def test_execute_live_rejects_missing_confirmation():
    ticket_id, _ = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_5", confirm=False),
    )
    assert response.status_code == 422


def test_execute_live_rejects_target_same_as_source():
    """source_schema/source_table are now mandatory -- originally
    optional, which converted a mandatory safety gate into one a
    caller could simply omit to bypass."""
    ticket_id, _ = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body(
            "customer_master_live_6", source_schema="public", source_table="customer_master_live_6",
        ),
    )
    assert response.status_code == 422


def test_execute_live_succeeds_and_actually_writes_the_target_table():
    ticket_id, schema_version_id = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_7"),
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "COMPLETED"
    assert body["backup_table"] is None
    assert body["final_row_count"] == 3

    with live_target_engine.connect() as conn:
        rows = conn.execute(text(
            'SELECT customer_id FROM "public"."customer_master_live_7" ORDER BY customer_id'
        )).fetchall()
        assert [r[0] for r in rows] == [1, 2, 3]

    fetched = client.get(f"/live-executions/{body['live_execution_id']}").json()
    assert fetched["status"] == "COMPLETED"
    assert fetched["schema_version_id"] == schema_version_id


def test_execute_live_rejects_repeat_execution_of_same_ticket():
    ticket_id, _ = _register_and_approve_registry_ticket()
    first = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_8"),
    )
    assert first.status_code == 200
    second = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_8b"),
    )
    assert second.status_code == 409


def test_execute_live_backs_up_and_replaces_an_aegis_managed_target():
    """The target must already be Aegis-managed (a prior PROMOTE marker)
    for a second execution to be allowed to replace it -- otherwise
    this would silently take over an arbitrary existing table."""
    first_ticket, _ = _register_and_approve_registry_ticket()
    client.post(
        f"/approvals/{first_ticket}/execute-live",
        json=_execute_live_body("customer_master_live_9"),
    )

    second_ticket, _ = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{second_ticket}/execute-live",
        json=_execute_live_body("customer_master_live_9"),
    )
    body = response.json()
    assert response.status_code == 200
    assert body["backup_table"] is not None

    with live_target_engine.connect() as conn:
        current = conn.execute(text(
            'SELECT customer_id FROM "public"."customer_master_live_9" ORDER BY customer_id'
        )).fetchall()
        assert [r[0] for r in current] == [1, 2, 3]


def test_execute_live_refuses_an_unmanaged_existing_table():
    """An existing table Aegis didn't create (no PROMOTE marker) must
    be refused outright -- Aegis's simplified shadow-table schema would
    silently drop whatever real constraints/indexes/triggers it had."""
    with live_target_engine.begin() as conn:
        conn.execute(text('CREATE TABLE "public"."customer_master_live_10" (customer_id BIGINT)'))
        conn.execute(text('INSERT INTO "public"."customer_master_live_10" VALUES (999)'))

    ticket_id, _ = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_10"),
    )
    assert response.status_code == 422

    with live_target_engine.connect() as conn:
        untouched = conn.execute(text(
            'SELECT customer_id FROM "public"."customer_master_live_10"'
        )).fetchall()
        assert [r[0] for r in untouched] == [999]


def test_rollback_restores_the_previous_aegis_managed_target():
    first_ticket, _ = _register_and_approve_registry_ticket()
    client.post(
        f"/approvals/{first_ticket}/execute-live",
        json=_execute_live_body("customer_master_live_11"),
    )
    second_ticket, _ = _register_and_approve_registry_ticket()
    executed = client.post(
        f"/approvals/{second_ticket}/execute-live",
        json=_execute_live_body("customer_master_live_11"),
    ).json()

    rollback = client.post(
        f"/live-executions/{executed['live_execution_id']}/rollback",
        json={"operator": "mo"},
    )
    assert rollback.status_code == 200
    assert rollback.json()["status"] == "ROLLED_BACK"

    with live_target_engine.connect() as conn:
        restored = conn.execute(text(
            'SELECT customer_id FROM "public"."customer_master_live_11" ORDER BY customer_id'
        )).fetchall()
        assert [r[0] for r in restored] == [1, 2, 3]


def test_rollback_of_unknown_execution_returns_404():
    response = client.post(
        "/live-executions/00000000-0000-0000-0000-000000000000/rollback",
        json={"operator": "mo"},
    )
    assert response.status_code == 404


def test_rollback_twice_returns_409():
    ticket_id, _ = _register_and_approve_registry_ticket()
    executed = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_12"),
    ).json()
    first = client.post(
        f"/live-executions/{executed['live_execution_id']}/rollback", json={"operator": "mo"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/live-executions/{executed['live_execution_id']}/rollback", json={"operator": "mo"},
    )
    assert second.status_code == 409


def test_rolled_back_ticket_cannot_execute_live_again():
    """Issue 1: a rolled-back ticket must not be able to re-execute --
    live execution is one-shot per ticket, not per successful attempt."""
    ticket_id, _ = _register_and_approve_registry_ticket()
    executed = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_13"),
    ).json()
    client.post(
        f"/live-executions/{executed['live_execution_id']}/rollback", json={"operator": "mo"},
    )
    retry = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_13b"),
    )
    assert retry.status_code == 409


def test_stale_rollback_refused_when_a_newer_execution_supersedes():
    """Issue 5: rolling back an OLDER execution after a NEWER one has
    already replaced the same target must be refused -- otherwise it
    would destroy the newer, valid data."""
    first_ticket, _ = _register_and_approve_registry_ticket()
    first_executed = client.post(
        f"/approvals/{first_ticket}/execute-live",
        json=_execute_live_body("customer_master_live_14"),
    ).json()

    second_ticket, _ = _register_and_approve_registry_ticket()
    client.post(
        f"/approvals/{second_ticket}/execute-live",
        json=_execute_live_body("customer_master_live_14"),
    )

    stale_rollback = client.post(
        f"/live-executions/{first_executed['live_execution_id']}/rollback",
        json={"operator": "mo"},
    )
    assert stale_rollback.status_code == 409

    with live_target_engine.connect() as conn:
        current = conn.execute(text(
            'SELECT customer_id FROM "public"."customer_master_live_14" ORDER BY customer_id'
        )).fetchall()
        assert [r[0] for r in current] == [1, 2, 3], "the newer execution's data must survive untouched"


def test_surgeon_failure_during_execute_live_marks_failed_not_stuck_running():
    """Issue 3: a Surgeon failure used to happen OUTSIDE the try/except,
    leaving the record permanently RUNNING."""
    ticket_id, _ = _register_and_approve_registry_ticket()
    with patch("src.api.app.AegisSurgeon.execute", side_effect=RuntimeError("boom")):
        response = client.post(
            f"/approvals/{ticket_id}/execute-live",
            json=_execute_live_body("customer_master_live_15"),
        )
    assert response.status_code == 500

    with TestSessionLocal() as db:
        record = db.query(LiveExecutionRecord).filter(
            LiveExecutionRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()
        assert record.status == "FAILED"


def test_target_write_failure_marks_execution_failed():
    ticket_id, _ = _register_and_approve_registry_ticket()
    with patch("src.api.app.PostgresLiveWriter.promote", side_effect=RuntimeError("target db exploded")):
        response = client.post(
            f"/approvals/{ticket_id}/execute-live",
            json=_execute_live_body("customer_master_live_16"),
        )
    assert response.status_code == 500

    with TestSessionLocal() as db:
        record = db.query(LiveExecutionRecord).filter(
            LiveExecutionRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()
        assert record.status == "FAILED"


def test_reconciliation_heals_a_stuck_running_record():
    """
    Issue 4: simulates the crash window directly -- a live execution
    whose target-side PROMOTE actually committed, but whose governance
    record never got updated past RUNNING (as if the API crashed in
    between). GET must self-heal it to COMPLETED using the target-side
    marker, not leave it stuck.
    """
    ticket_id, schema_version_id = _register_and_approve_registry_ticket()

    with TestSessionLocal() as db:
        ticket = None
        from src.governance.approval_repository import PostgresApprovalRepository
        ticket = PostgresApprovalRepository(db).get(ticket_id)
        manifest = db.query(HealingManifestRecord).filter(
            HealingManifestRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()

        live_repo = LiveExecutionRepository(db)
        live_record = live_repo.create_pending(
            ticket_id=ticket_id,
            sandbox_manifest_id=str(manifest.manifest_id),
            schema_version_id=schema_version_id,
            target_schema="public",
            target_table="customer_master_live_17",
            requested_by="mo",
            original_row_count=manifest.original_row_count,
            final_row_count=manifest.final_row_count,
            risk_level=manifest.risk_level,
            integrity_status=manifest.integrity_status,
        )
        live_repo.mark_running(live_record.live_execution_id)

        # Directly invoke the writer -- simulates the target transaction
        # committing successfully, without ever calling mark_completed().
        writer = PostgresLiveWriter(live_target_engine)
        writer.promote(
            target_schema="public",
            target_table="customer_master_live_17",
            dataframe=ticket.target_dataset,
            execution_id=live_record.live_execution_id,
            expected_row_count=3,
        )
        stuck_id = str(live_record.live_execution_id)

    fetched = client.get(f"/live-executions/{stuck_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "COMPLETED"


def test_concurrent_rollback_only_one_succeeds():
    ticket_id, _ = _register_and_approve_registry_ticket()
    executed = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_live_18"),
    ).json()

    results = []
    lock = threading.Lock()

    def attempt():
        response = client.post(
            f"/live-executions/{executed['live_execution_id']}/rollback",
            json={"operator": "concurrent-test"},
        )
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409]


def test_migration_0003_creates_expected_tables_and_columns():
    """
    Explicit verification that migration 0003 -- not just
    Base.metadata (which the rest of this file uses for speed) --
    actually creates live_executions and the healing_manifests
    fingerprint column. Runs against a throwaway schema via Alembic
    directly, separate from the main test tables above.
    """
    from alembic.config import Config
    from alembic import command

    project_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)

    Base.metadata.drop_all(bind=engine)
    command.upgrade(cfg, "head")

    inspector = sa_inspect(engine)
    assert "live_executions" in inspector.get_table_names()

    columns = {c["name"] for c in inspector.get_columns("live_executions")}
    expected = {
        "live_execution_id", "ticket_id", "sandbox_manifest_id", "schema_version_id",
        "target_schema", "target_table", "backup_table", "status", "requested_by",
        "started_at", "completed_at", "rolled_back_by", "rolled_back_at",
        "failure_reason", "original_row_count", "final_row_count", "risk_level",
        "integrity_status",
    }
    assert expected.issubset(columns)

    manifest_columns = {c["name"] for c in inspector.get_columns("healing_manifests")}
    assert "corrected_output_fingerprint" in manifest_columns

    index_names = {idx["name"] for idx in inspector.get_indexes("live_executions")}
    assert "uq_live_executions_ticket_active_or_done" in index_names
    assert "uq_live_executions_target_in_flight" in index_names

    command.downgrade(cfg, "0002")
    tables_after = sa_inspect(engine).get_table_names()
    assert "live_executions" not in tables_after

    Base.metadata.create_all(bind=engine)
