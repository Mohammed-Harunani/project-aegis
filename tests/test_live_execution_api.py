"""
Postgres integration tests for Phase 2.5 live execution: safety gates
enforced end-to-end, actual promotion to a real (disposable) target
database, and rollback.

Needs BOTH TEST_DATABASE_URL (governance -- aegis_test) and
LIVE_TEST_DATABASE_URL (the live target -- aegis_live_test, a THIRD
database, since the writer itself refuses aegis/aegis_test as targets).

    docker compose up -d postgres   # creates aegis_test AND aegis_live_test
    export TEST_DATABASE_URL=postgresql+psycopg://aegis_user:change_me@localhost:5432/aegis_test
    export LIVE_TEST_DATABASE_URL=postgresql+psycopg://aegis_user:change_me@localhost:5432/aegis_live_test
    python -m pytest tests/test_live_execution_api.py -v

Written and syntax-checked but NOT executed -- same constraint as
every Postgres-dependent piece of this project, and the stakes on
that gap are higher here than anywhere else: this is the one place
Aegis writes to something other than its own governance database.
Treat every assertion here as a draft until run against a real,
disposable Postgres target.
"""

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import get_verified_test_database_url, get_verified_live_test_database_url

TEST_DATABASE_URL = get_verified_test_database_url()
LIVE_TEST_DATABASE_URL = get_verified_live_test_database_url()

os.environ["AEGIS_LIVE_TARGET_SCHEMA_ALLOWLIST"] = "public"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
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
    # Clean the live target database of anything a previous test left behind.
    with live_target_engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename LIKE 'customer_master_live%'"
        )).fetchall()
        for (table_name,) in rows:
            conn.execute(text(f'DROP TABLE IF EXISTS "public"."{table_name}"'))


def _register_and_approve_registry_ticket(schema_name="customer_master_live_test"):
    """Registers a schema, submits a rename migration against it, approves
    the resulting ticket. Returns (ticket_id, schema_version_id)."""
    client.post(f"/schemas/{schema_name}/versions", json={
        "format_version": 1,
        "created_by": "mo",
        "columns": [{"name": "customer_id", "dtype": "int64"}],
    })
    submit = client.post("/simulate-migration", json={
        "schema_name": schema_name,
        "sample_data": {"Customer_ID": [1, 2, 3]},
    }).json()
    ticket_id = submit["ticket_id"]
    client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})
    return ticket_id, submit["schema_version_id"]


def test_execute_live_disabled_by_default_returns_403():
    os.environ["AEGIS_LIVE_EXECUTION_ENABLED"] = "false"
    ticket_id, _ = _register_and_approve_registry_ticket()

    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json={
            "operator": "mo", "target_schema": "public",
            "target_table": "customer_master_live_1", "confirm": True,
        },
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
    # Not approved yet -- still PENDING.
    response = client.post(
        f"/approvals/{submit['ticket_id']}/execute-live",
        json={
            "operator": "mo", "target_schema": "public",
            "target_table": "customer_master_live_2", "confirm": True,
        },
    )
    assert response.status_code == 409


def test_execute_live_rejects_legacy_gold_schema_ticket():
    """Legacy direct-gold_schema requests always have schema_version_id
    null -- gate 1/2 must reject them for live execution."""
    submit = client.post("/simulate-migration", json={
        "gold_schema": {"customer_id": "int64"},
        "sample_data": {"Customer_ID": [1, 2, 3]},
    }).json()
    client.post(f"/approvals/{submit['ticket_id']}/approve", json={"operator": "mo"})

    response = client.post(
        f"/approvals/{submit['ticket_id']}/execute-live",
        json={
            "operator": "mo", "target_schema": "public",
            "target_table": "customer_master_live_3", "confirm": True,
        },
    )
    assert response.status_code == 422


def test_execute_live_rejects_non_allowlisted_schema():
    ticket_id, _ = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json={
            "operator": "mo", "target_schema": "not_allowlisted",
            "target_table": "customer_master_live_4", "confirm": True,
        },
    )
    assert response.status_code == 422


def test_execute_live_rejects_missing_confirmation():
    ticket_id, _ = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json={"operator": "mo", "target_schema": "public", "target_table": "customer_master_live_5"},
    )
    assert response.status_code == 422


def test_execute_live_succeeds_and_actually_writes_the_target_table():
    ticket_id, schema_version_id = _register_and_approve_registry_ticket()

    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json={
            "operator": "mo", "target_schema": "public",
            "target_table": "customer_master_live_6", "confirm": True,
        },
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "COMPLETED"
    assert body["backup_table"] is None  # table didn't exist before
    assert body["final_row_count"] == 3

    # Verify against the REAL live target database, not just the API's
    # own report of what it did.
    with live_target_engine.connect() as conn:
        rows = conn.execute(text('SELECT customer_id FROM "public"."customer_master_live_6" ORDER BY customer_id')).fetchall()
        assert [r[0] for r in rows] == [1, 2, 3]

    fetched = client.get(f"/live-executions/{body['live_execution_id']}").json()
    assert fetched["status"] == "COMPLETED"
    assert fetched["schema_version_id"] == schema_version_id


def test_execute_live_rejects_repeat_execution_of_same_ticket():
    ticket_id, _ = _register_and_approve_registry_ticket()
    first = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json={
            "operator": "mo", "target_schema": "public",
            "target_table": "customer_master_live_7", "confirm": True,
        },
    )
    assert first.status_code == 200

    second = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json={
            "operator": "mo", "target_schema": "public",
            "target_table": "customer_master_live_7b", "confirm": True,
        },
    )
    assert second.status_code == 409


def test_execute_live_backs_up_and_replaces_an_existing_target():
    """When the target table already exists, it must be renamed to a
    backup, not dropped or overwritten in place."""
    with live_target_engine.begin() as conn:
        conn.execute(text('CREATE TABLE "public"."customer_master_live_8" (customer_id BIGINT)'))
        conn.execute(text('INSERT INTO "public"."customer_master_live_8" VALUES (999)'))

    ticket_id, _ = _register_and_approve_registry_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json={
            "operator": "mo", "target_schema": "public",
            "target_table": "customer_master_live_8", "confirm": True,
        },
    )
    body = response.json()
    assert response.status_code == 200
    assert body["backup_table"] is not None

    with live_target_engine.connect() as conn:
        current = conn.execute(text('SELECT customer_id FROM "public"."customer_master_live_8" ORDER BY customer_id')).fetchall()
        assert [r[0] for r in current] == [1, 2, 3]

        backed_up = conn.execute(text(f'SELECT customer_id FROM "public"."{body["backup_table"]}"')).fetchall()
        assert [r[0] for r in backed_up] == [999]

    return body["live_execution_id"], body["backup_table"]


def test_rollback_restores_the_previous_target_table():
    with live_target_engine.begin() as conn:
        conn.execute(text('CREATE TABLE "public"."customer_master_live_9" (customer_id BIGINT)'))
        conn.execute(text('INSERT INTO "public"."customer_master_live_9" VALUES (999)'))

    ticket_id, _ = _register_and_approve_registry_ticket()
    executed = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json={
            "operator": "mo", "target_schema": "public",
            "target_table": "customer_master_live_9", "confirm": True,
        },
    ).json()

    rollback = client.post(
        f"/live-executions/{executed['live_execution_id']}/rollback",
        json={"operator": "mo"},
    )
    rollback_body = rollback.json()
    assert rollback.status_code == 200
    assert rollback_body["status"] == "ROLLED_BACK"

    with live_target_engine.connect() as conn:
        restored = conn.execute(text('SELECT customer_id FROM "public"."customer_master_live_9"')).fetchall()
        assert [r[0] for r in restored] == [999]


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
        json={
            "operator": "mo", "target_schema": "public",
            "target_table": "customer_master_live_10", "confirm": True,
        },
    ).json()

    first = client.post(
        f"/live-executions/{executed['live_execution_id']}/rollback",
        json={"operator": "mo"},
    )
    assert first.status_code == 200

    second = client.post(
        f"/live-executions/{executed['live_execution_id']}/rollback",
        json={"operator": "mo"},
    )
    assert second.status_code == 409
