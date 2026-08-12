"""
Postgres integration tests for Phase 2.5's final architecture:
trusted-source ingestion (read a complete, real table server-side,
never caller-supplied sample_data) and stable-view publication
(immutable versioned physical tables behind a CREATE OR REPLACE VIEW,
replacing the earlier rename-to-backup design entirely).

Needs THREE disposable Postgres databases:
- TEST_DATABASE_URL / aegis_test -- governance (tickets, manifests, live_executions)
- LIVE_TEST_DATABASE_URL / aegis_live_test -- the publication target
  (aegis_publish / aegis_publish_data schemas)
- SOURCE_TEST_DATABASE_URL / aegis_source_test -- the trusted source
  Aegis reads complete tables from

    docker compose up -d postgres
    export TEST_DATABASE_URL=postgresql+psycopg://aegis_user:change_me@localhost:5432/aegis_test
    export LIVE_TEST_DATABASE_URL=postgresql+psycopg://aegis_user:change_me@localhost:5432/aegis_live_test
    export SOURCE_TEST_DATABASE_URL=postgresql+psycopg://aegis_user:change_me@localhost:5432/aegis_source_test
    python -m pytest tests/test_live_execution_api.py -v

If your Postgres data volume predates this phase, the init script
won't create aegis_source_test automatically -- create it manually:
    docker compose exec postgres psql -U aegis_user -d postgres \
        -c "CREATE DATABASE aegis_source_test;"

This suite is executed only against the three explicitly verified,
disposable PostgreSQL test databases listed above. Its guards refuse
to run against any differently named database.
"""

import sys
import os
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import (
    get_verified_test_database_url,
    get_verified_live_test_database_url,
    get_verified_source_test_database_url,
)

TEST_DATABASE_URL = get_verified_test_database_url()
LIVE_TEST_DATABASE_URL = get_verified_live_test_database_url()
SOURCE_TEST_DATABASE_URL = get_verified_source_test_database_url()

os.environ["AEGIS_LIVE_EXECUTION_STALE_SECONDS"] = "0"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from src.api.app import app
from src.surgeon import AegisSurgeon
from src.db.session import get_db
from src.db.live_session import get_live_engine
from src.db.source_session import get_source_engine
from src.db.models import (
    Base,
    ApprovalTicketRecord,
    HealingManifestRecord,
    GoldSchemaRecord,
    SchemaVersionRecord,
    LiveExecutionRecord,
)
from src.live_execution.repository import LiveExecutionRepository
from src.live_execution.writer import PostgresPublicationWriter, AEGIS_PUBLISH_SCHEMA, AEGIS_PUBLISH_DATA_SCHEMA


engine = create_engine(TEST_DATABASE_URL)
TestSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

live_target_engine = create_engine(LIVE_TEST_DATABASE_URL)
source_engine = create_engine(SOURCE_TEST_DATABASE_URL)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_live_engine] = lambda: live_target_engine
app.dependency_overrides[get_source_engine] = lambda: source_engine
client = TestClient(app)


def setup_module(_module):
    Base.metadata.create_all(bind=engine)


def teardown_module(_module):
    Base.metadata.drop_all(bind=engine)


def setup_function(_):
    os.environ["AEGIS_LIVE_EXECUTION_ENABLED"] = "true"
    os.environ["AEGIS_LIVE_CAST_ALLOWLIST"] = ""
    with engine.begin() as conn:
        conn.execute(LiveExecutionRecord.__table__.delete())
        conn.execute(HealingManifestRecord.__table__.delete())
        conn.execute(ApprovalTicketRecord.__table__.delete())
        conn.execute(SchemaVersionRecord.__table__.delete())
        conn.execute(GoldSchemaRecord.__table__.delete())
    with live_target_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{AEGIS_PUBLISH_SCHEMA}" CASCADE'))
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{AEGIS_PUBLISH_DATA_SCHEMA}" CASCADE'))
    with source_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS customers"))
        conn.execute(text("DROP TABLE IF EXISTS customers_no_pk"))
        conn.execute(text("DROP TABLE IF EXISTS orders_cast"))


def _create_source_table(rows=((1, "alice"), (2, "bob"), (3, "carol"))):
    """
    Creates a simple, real source table with a primary key and some
    rows in the trusted-source test database. Column name deliberately
    mismatches the Gold schema (Customer_ID vs customer_id) so a
    repair plan actually gets proposed, mirroring the rename scenario
    used throughout the rest of this project's tests.
    """
    with source_engine.begin() as conn:
        # Some publication/rollback tests intentionally create a
        # second source snapshot inside the same test. Replace the
        # first table deterministically rather than relying only on
        # setup_function(), which runs once per test function.
        conn.execute(text("DROP TABLE IF EXISTS customers"))
        conn.execute(text(
            'CREATE TABLE customers ('
            '"Customer_ID" BIGINT PRIMARY KEY, name TEXT)'
        ))
        for pk, name in rows:
            conn.execute(
                text('INSERT INTO customers VALUES (:pk, :name)'),
                {"pk": pk, "name": name},
            )


def _register_schema(schema_name="customer_master_source_test"):
    client.post(f"/schemas/{schema_name}/versions", json={
        "format_version": 1, "created_by": "mo",
        "columns": [{"name": "customer_id", "dtype": "int64"}, {"name": "name", "dtype": "object"}],
    })
    return schema_name


def _register_and_approve_source_ticket(schema_name=None, rows=None):
    schema_name = schema_name or _register_schema()
    _create_source_table(rows=rows) if rows else _create_source_table()
    body = {
        "schema_name": schema_name,
        "source_schema": "public",
        "source_table": "customers",
    }
    submit = client.post("/simulate-migration-from-source", json=body).json()
    ticket_id = submit["ticket_id"]
    client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})
    return ticket_id, submit["schema_version_id"]


def _execute_live_body(logical_target, confirm=True, operator="mo", **overrides):
    body = {"operator": operator, "logical_target": logical_target, "confirm": confirm}
    body.update(overrides)
    return body


def _view_rows(logical_target, order_col="customer_id"):
    with live_target_engine.connect() as conn:
        rows = conn.execute(text(
            f'SELECT * FROM "{AEGIS_PUBLISH_SCHEMA}"."{logical_target}" ORDER BY "{order_col}"'
        )).fetchall()
    return rows


def _create_cast_source_table(rows=((1, "10"), (2, "20"), (3, "-30"))):
    with source_engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS orders_cast"))
        conn.execute(text(
            "CREATE TABLE orders_cast (id BIGINT PRIMARY KEY, amount TEXT)"
        ))
        for pk, amount in rows:
            conn.execute(
                text("INSERT INTO orders_cast VALUES (:pk, :amount)"),
                {"pk": pk, "amount": amount},
            )


def _register_cast_schema(schema_name="orders_cast_schema"):
    response = client.post(f"/schemas/{schema_name}/versions", json={
        "format_version": 1,
        "created_by": "mo",
        "columns": [
            {"name": "id", "dtype": "int64"},
            {"name": "amount", "dtype": "int64"},
        ],
    })
    assert response.status_code == 200, response.text
    return schema_name


def _submit_cast_ticket(schema_name=None, rows=None):
    os.environ["AEGIS_LIVE_CAST_ALLOWLIST"] = "object->int64"
    schema_name = _register_cast_schema(schema_name or "orders_cast_schema")
    _create_cast_source_table(rows=rows) if rows else _create_cast_source_table()
    response = client.post("/simulate-migration-from-source", json={
        "schema_name": schema_name,
        "source_schema": "public",
        "source_table": "orders_cast",
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["live_cast_pair"] == "object->int64"
    assert body["conversion_decision"]["status"] == "SAFE"
    return body


def _submit_and_approve_cast_ticket(schema_name=None, rows=None):
    submit = _submit_cast_ticket(schema_name=schema_name, rows=rows)
    approval = client.post(
        f"/approvals/{submit['ticket_id']}/approve",
        json={"operator": "mo"},
    )
    assert approval.status_code == 200, approval.text
    assert approval.json()["conversion_outcome"] == submit["conversion_decision"]
    return submit["ticket_id"], submit["schema_version_id"]


def _cast_view_rows(logical_target):
    return _view_rows(logical_target, order_col="id")


# ---- Phase 3.1.6 controlled live CAST_COLUMN ----

def test_safe_source_cast_is_rejected_when_live_allowlist_is_empty():
    _register_cast_schema("orders_cast_empty_allowlist")
    _create_cast_source_table()

    response = client.post("/simulate-migration-from-source", json={
        "schema_name": "orders_cast_empty_allowlist",
        "source_schema": "public",
        "source_table": "orders_cast",
    })

    assert response.status_code == 422
    assert "object->int64" in response.json()["detail"]
    assert "not explicitly allowlisted" in response.json()["detail"]
    with TestSessionLocal() as db:
        assert db.query(ApprovalTicketRecord).count() == 0


def test_allowlisted_source_cast_persists_safe_decision_and_matching_manifest():
    submit = _submit_cast_ticket("orders_cast_approval")
    ticket_id = submit["ticket_id"]

    approval = client.post(
        f"/approvals/{ticket_id}/approve",
        json={"operator": "mo"},
    )
    assert approval.status_code == 200, approval.text
    body = approval.json()
    assert body["conversion_decision"] == submit["conversion_decision"]
    assert body["conversion_outcome"] == submit["conversion_decision"]

    with TestSessionLocal() as db:
        ticket = db.query(ApprovalTicketRecord).filter_by(
            ticket_id=__import__("uuid").UUID(ticket_id)
        ).one()
        manifest = db.query(HealingManifestRecord).filter_by(
            ticket_id=__import__("uuid").UUID(ticket_id)
        ).one()
        assert ticket.conversion_decision["status"] == "SAFE"
        assert manifest.conversion_outcome == ticket.conversion_decision
        assert manifest.final_row_count == manifest.original_row_count == 3


def test_allowlisted_live_cast_publishes_verified_converted_values():
    ticket_id, _ = _submit_and_approve_cast_ticket("orders_cast_publish")

    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("orders_cast_target"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert _cast_view_rows("orders_cast_target") == [
        (1, 10),
        (2, 20),
        (3, -30),
    ]

    with live_target_engine.connect() as conn:
        dtype = conn.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table "
            "AND column_name = 'amount'"
        ), {
            "schema": AEGIS_PUBLISH_DATA_SCHEMA,
            "table": body["physical_table"],
        }).scalar_one()
        assert dtype == "bigint"


def test_removing_cast_pair_after_approval_revokes_live_execution():
    ticket_id, _ = _submit_and_approve_cast_ticket("orders_cast_revoke")
    os.environ["AEGIS_LIVE_CAST_ALLOWLIST"] = ""

    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("orders_cast_revoked_target"),
    )
    assert response.status_code == 422
    assert "not explicitly allowlisted" in response.json()["detail"]
    with TestSessionLocal() as db:
        assert db.query(LiveExecutionRecord).count() == 0


def test_live_cast_republish_and_rollback_preserve_immutable_versions():
    first_ticket, _ = _submit_and_approve_cast_ticket("orders_cast_versions")
    first = client.post(
        f"/approvals/{first_ticket}/execute-live",
        json=_execute_live_body("orders_cast_versioned"),
    )
    assert first.status_code == 200, first.text
    first_body = first.json()

    second_ticket, _ = _submit_and_approve_cast_ticket(
        "orders_cast_versions_2",
        rows=((1, "100"), (2, "200"), (3, "300")),
    )
    second = client.post(
        f"/approvals/{second_ticket}/execute-live",
        json=_execute_live_body("orders_cast_versioned"),
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["previous_physical_table"] == first_body["physical_table"]
    assert _cast_view_rows("orders_cast_versioned") == [
        (1, 100), (2, 200), (3, 300)
    ]

    rollback = client.post(
        f"/live-executions/{second_body['live_execution_id']}/rollback",
        json={"operator": "mo"},
    )
    assert rollback.status_code == 200, rollback.text
    assert rollback.json()["status"] == "ROLLED_BACK"
    assert _cast_view_rows("orders_cast_versioned") == [
        (1, 10), (2, 20), (3, -30)
    ]

    with live_target_engine.connect() as conn:
        existing = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name IN (:first, :second)"
        ), {
            "schema": AEGIS_PUBLISH_DATA_SCHEMA,
            "first": first_body["physical_table"],
            "second": second_body["physical_table"],
        }).scalars().all()
        assert set(existing) == {
            first_body["physical_table"],
            second_body["physical_table"],
        }


# ---- Trusted source ----

def test_source_table_without_primary_key_rejected():
    with source_engine.begin() as conn:
        conn.execute(text('CREATE TABLE customers_no_pk (customer_id BIGINT, name TEXT)'))
    schema_name = _register_schema()
    response = client.post("/simulate-migration-from-source", json={
        "schema_name": schema_name, "source_schema": "public", "source_table": "customers_no_pk",
    })
    assert response.status_code == 422


def test_cast_to_uncastable_extended_dtype_rejected_before_ticket_creation():
    """
    Issue 1 (eighth correction pass): Surgeon's CAST_COLUMN applies
    working_df[column].astype(target_type) verbatim, and pandas has no
    understanding of Aegis's own extended logical dtypes ("decimal",
    "date", "datetime_tz", "uuid", "json") -- confirmed directly that
    every such cast fails for every row. A Gold schema whose dtype
    mismatch would require Consultant to propose exactly this kind of
    cast must be rejected before a ticket is ever created, not
    discovered later as an unexplained HIGH_RISK/applied=False result.
    """
    client.post("/schemas/orders_uncastable_test/versions", json={
        "format_version": 1, "created_by": "mo",
        "columns": [{"name": "amount", "dtype": "decimal"}],
    })
    with source_engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS orders_uncastable'))
        # Same column NAME as Gold ("amount"), but wrong TYPE (TEXT
        # instead of NUMERIC) -- this forces Consultant to propose a
        # CAST_COLUMN (not a rename), targeting "decimal".
        conn.execute(text(
            'CREATE TABLE orders_uncastable (id BIGINT PRIMARY KEY, amount TEXT)'
        ))
        conn.execute(text("INSERT INTO orders_uncastable VALUES (1, 'not-a-number')"))
    response = client.post("/simulate-migration-from-source", json={
        "schema_name": "orders_uncastable_test",
        "source_schema": "public", "source_table": "orders_uncastable",
    })
    assert response.status_code == 422
    assert "cast" in response.json()["detail"].lower()


def test_unpublishable_gold_dtype_rejected_before_ticket_creation():
    """
    Issue 3 (eighth correction pass): the Schema Registry accepts any
    dtype pandas.api.types.pandas_dtype() recognizes, which is a much
    wider set than publication actually supports -- confirmed directly
    that "int32"/"Int64"/"category" etc. all pass registry validation
    but have no PostgreSQL publication mapping. A Gold schema declaring
    one of these must be rejected at simulation time for the
    live-capable path, not allowed to pass approval and only fail at
    execute-live.
    """
    client.post("/schemas/orders_unpublishable_test/versions", json={
        "format_version": 1, "created_by": "mo",
        "columns": [{"name": "small_amount", "dtype": "int32"}],
    })
    with source_engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS orders_unpublishable'))
        conn.execute(text(
            'CREATE TABLE orders_unpublishable (id BIGINT PRIMARY KEY, small_amount BIGINT)'
        ))
        conn.execute(text("INSERT INTO orders_unpublishable VALUES (1, 100)"))
    response = client.post("/simulate-migration-from-source", json={
        "schema_name": "orders_unpublishable_test",
        "source_schema": "public", "source_table": "orders_unpublishable",
    })
    assert response.status_code == 422
    assert "publication mapping" in response.json()["detail"].lower() or "publishable" in response.json()["detail"].lower()


def test_sample_data_ticket_is_not_live_eligible():
    """The core safety property of this whole redesign: a ticket from
    /simulate-migration (sample_data) must never be able to execute
    live, even if approved."""
    client.post("/schemas/customer_master_sampledata_test/versions", json={
        "format_version": 1, "created_by": "mo",
        "columns": [{"name": "customer_id", "dtype": "int64"}],
    })
    submit = client.post("/simulate-migration", json={
        "schema_name": "customer_master_sampledata_test",
        "sample_data": {"Customer_ID": [1, 2, 3]},
    }).json()
    ticket_id = submit["ticket_id"]
    client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})

    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_sampledata_target"),
    )
    assert response.status_code == 422
    assert "live-eligible" in response.json()["detail"] or "sample_data" in response.json()["detail"]


def test_source_backed_simulation_records_provenance():
    ticket_id, schema_version_id = _register_and_approve_source_ticket()
    with TestSessionLocal() as db:
        record = db.query(ApprovalTicketRecord).filter(
            ApprovalTicketRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()
        assert record.live_eligible is True
        assert record.source_schema == "public"
        assert record.source_table == "customers"
        assert record.source_row_count == 3
        assert record.source_dataset_fingerprint is not None


def test_execute_live_disabled_by_default_returns_403():
    os.environ["AEGIS_LIVE_EXECUTION_ENABLED"] = "false"
    ticket_id, _ = _register_and_approve_source_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_1"),
    )
    assert response.status_code == 403


def test_execute_live_requires_approved_ticket():
    schema_name = _register_schema()
    _create_source_table()
    submit = client.post("/simulate-migration-from-source", json={
        "schema_name": schema_name, "source_schema": "public", "source_table": "customers",
    }).json()
    response = client.post(
        f"/approvals/{submit['ticket_id']}/execute-live",
        json=_execute_live_body("customer_master_2"),
    )
    assert response.status_code == 409


def test_execute_live_rejects_missing_confirmation():
    ticket_id, _ = _register_and_approve_source_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_3", confirm=False),
    )
    assert response.status_code == 422


# ---- Stable-view publication ----

def test_execute_live_succeeds_and_publishes_a_stable_view():
    ticket_id, schema_version_id = _register_and_approve_source_ticket()
    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_4"),
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "COMPLETED"
    assert body["previous_physical_table"] is None
    assert body["physical_table"].startswith("customer_master_4__")

    rows = _view_rows("customer_master_4")
    assert len(rows) == 3

    fetched = client.get(f"/live-executions/{body['live_execution_id']}").json()
    assert fetched["status"] == "COMPLETED"
    assert fetched["schema_version_id"] == schema_version_id
    assert fetched["source_schema"] == "public"
    assert fetched["source_table"] == "customers"


def test_republish_creates_new_version_and_repoints_view_without_dropping_old():
    first_ticket, _ = _register_and_approve_source_ticket()
    first = client.post(
        f"/approvals/{first_ticket}/execute-live",
        json=_execute_live_body("customer_master_5"),
    ).json()
    first_physical = first["physical_table"]

    second_ticket, _ = _register_and_approve_source_ticket(
        rows=((1, "alice"), (2, "bob"), (3, "carol"), (4, "dave"))
    )
    second = client.post(
        f"/approvals/{second_ticket}/execute-live",
        json=_execute_live_body("customer_master_5"),
    ).json()
    assert second["previous_physical_table"] == first_physical
    assert second["physical_table"] != first_physical

    rows = _view_rows("customer_master_5")
    assert len(rows) == 4, "the view must show the NEW version's data"

    with live_target_engine.connect() as conn:
        old_still_exists = conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :table)"
        ), {"schema": AEGIS_PUBLISH_DATA_SCHEMA, "table": first_physical}).scalar()
        assert old_still_exists, "immutable physical versions must never be dropped by publication"


def test_execute_live_rejects_repeat_execution_of_same_ticket():
    ticket_id, _ = _register_and_approve_source_ticket()
    first = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_6"),
    )
    assert first.status_code == 200
    second = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_6b"),
    )
    assert second.status_code == 409


def test_rollback_repoints_view_to_previous_version_without_dropping_current():
    first_ticket, _ = _register_and_approve_source_ticket()
    first = client.post(
        f"/approvals/{first_ticket}/execute-live",
        json=_execute_live_body("customer_master_7"),
    ).json()
    first_physical = first["physical_table"]

    second_ticket, _ = _register_and_approve_source_ticket(
        rows=((1, "alice"), (2, "bob"), (3, "carol"), (4, "dave"))
    )
    second = client.post(
        f"/approvals/{second_ticket}/execute-live",
        json=_execute_live_body("customer_master_7"),
    ).json()
    second_physical = second["physical_table"]

    rollback = client.post(
        f"/live-executions/{second['live_execution_id']}/rollback",
        json={"operator": "mo"},
    )
    assert rollback.status_code == 200
    assert rollback.json()["status"] == "ROLLED_BACK"

    rows = _view_rows("customer_master_7")
    assert len(rows) == 3, "view must be repointed back to the first (3-row) version"

    with live_target_engine.connect() as conn:
        second_still_exists = conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = :schema AND table_name = :table)"
        ), {"schema": AEGIS_PUBLISH_DATA_SCHEMA, "table": second_physical}).scalar()
        assert second_still_exists, "rollback must never drop the physical version it's moving away from"


def test_rollback_of_first_ever_publish_removes_the_view():
    ticket_id, _ = _register_and_approve_source_ticket()
    executed = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_8"),
    ).json()

    rollback = client.post(
        f"/live-executions/{executed['live_execution_id']}/rollback",
        json={"operator": "mo"},
    )
    assert rollback.status_code == 200

    with live_target_engine.connect() as conn:
        view_exists = conn.execute(text(
            "SELECT EXISTS (SELECT 1 FROM information_schema.views "
            "WHERE table_schema = :schema AND table_name = :view)"
        ), {"schema": AEGIS_PUBLISH_SCHEMA, "view": "customer_master_8"}).scalar()
        assert not view_exists, "rolling back the only-ever publish removes the view (nothing to repoint to)"


def test_rollback_of_unknown_execution_returns_404():
    response = client.post(
        "/live-executions/00000000-0000-0000-0000-000000000000/rollback",
        json={"operator": "mo"},
    )
    assert response.status_code == 404


def test_rollback_twice_returns_409():
    ticket_id, _ = _register_and_approve_source_ticket()
    executed = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_9"),
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
    ticket_id, _ = _register_and_approve_source_ticket()
    executed = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_10"),
    ).json()
    client.post(
        f"/live-executions/{executed['live_execution_id']}/rollback", json={"operator": "mo"},
    )
    retry = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_10b"),
    )
    assert retry.status_code == 409


def test_stale_rollback_refused_when_a_newer_execution_supersedes():
    first_ticket, _ = _register_and_approve_source_ticket()
    first_executed = client.post(
        f"/approvals/{first_ticket}/execute-live",
        json=_execute_live_body("customer_master_11"),
    ).json()

    second_ticket, _ = _register_and_approve_source_ticket(
        rows=((1, "alice"), (2, "bob"), (3, "carol"), (4, "dave"))
    )
    client.post(
        f"/approvals/{second_ticket}/execute-live",
        json=_execute_live_body("customer_master_11"),
    )

    stale_rollback = client.post(
        f"/live-executions/{first_executed['live_execution_id']}/rollback",
        json={"operator": "mo"},
    )
    assert stale_rollback.status_code == 409

    rows = _view_rows("customer_master_11")
    assert len(rows) == 4, "the newer execution's data must survive untouched"


def test_incompatible_view_schema_rejected():
    """
    Phase 2.5 requires an exact publication signature. A structurally
    incompatible republish to the SAME logical target must be
    refused, not silently break the view (or succeed and leave
    rollback broken later).
    """
    first_ticket, _ = _register_and_approve_source_ticket()
    client.post(
        f"/approvals/{first_ticket}/execute-live",
        json=_execute_live_body("customer_master_incompatible_target"),
    )

    # A genuinely different repair scenario, guaranteed to produce a
    # repair plan (Order_ID -> order_id, the same proven rename
    # pattern _register_and_approve_source_ticket uses), whose
    # corrected output has a completely different column signature
    # than the first ticket's (customer_id, name).
    client.post("/schemas/orders_incompatible_test/versions", json={
        "format_version": 1, "created_by": "mo",
        "columns": [{"name": "order_id", "dtype": "int64"}, {"name": "amount", "dtype": "object"}],
    })
    with source_engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS orders_incompatible'))
        conn.execute(text(
            'CREATE TABLE orders_incompatible ("Order_ID" BIGINT PRIMARY KEY, amount TEXT)'
        ))
        conn.execute(text("INSERT INTO orders_incompatible VALUES (1, '100.00'), (2, '200.00')"))
    submit = client.post("/simulate-migration-from-source", json={
        "schema_name": "orders_incompatible_test",
        "source_schema": "public", "source_table": "orders_incompatible",
    }).json()
    assert "ticket_id" in submit, (
        f"test setup must guarantee a repair plan is proposed -- got: {submit}"
    )
    second_ticket = submit["ticket_id"]
    client.post(f"/approvals/{second_ticket}/approve", json={"operator": "mo"})

    response = client.post(
        f"/approvals/{second_ticket}/execute-live",
        json=_execute_live_body("customer_master_incompatible_target"),
    )
    assert response.status_code == 422


def test_appended_column_rejected_not_just_removed_column():
    """
    Issue 5: the earlier design allowed appending columns (Postgres's
    CREATE OR REPLACE VIEW permits it going forward), but rollback
    repoints to the PREVIOUS, narrower physical table, which Postgres
    will not allow via CREATE OR REPLACE VIEW (it cannot remove
    existing output columns). Phase 2.5 requires an EXACT signature in
    both directions -- appending is refused too, not just removing.
    """
    first_ticket, _ = _register_and_approve_source_ticket()
    client.post(
        f"/approvals/{first_ticket}/execute-live",
        json=_execute_live_body("customer_master_append_test"),
    )

    client.post("/schemas/customer_master_append_test_schema/versions", json={
        "format_version": 1, "created_by": "mo",
        "columns": [
            {"name": "customer_id", "dtype": "int64"},
            {"name": "name", "dtype": "object"},
            {"name": "extra_column", "dtype": "int64"},
        ],
    })
    with source_engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS customers'))
        conn.execute(text(
            'CREATE TABLE customers ("Customer_ID" BIGINT PRIMARY KEY, name TEXT, extra_column BIGINT)'
        ))
        conn.execute(text("INSERT INTO customers VALUES (1, 'alice', 100), (2, 'bob', 200)"))
    submit = client.post("/simulate-migration-from-source", json={
        "schema_name": "customer_master_append_test_schema",
        "source_schema": "public", "source_table": "customers",
    }).json()
    assert "ticket_id" in submit, f"test setup must guarantee a repair plan -- got: {submit}"
    second_ticket = submit["ticket_id"]
    client.post(f"/approvals/{second_ticket}/approve", json={"operator": "mo"})

    response = client.post(
        f"/approvals/{second_ticket}/execute-live",
        json=_execute_live_body("customer_master_append_test"),
    )
    assert response.status_code == 422, "appending a column must be refused, not silently accepted"


def test_primary_key_lookup_does_not_mix_columns_from_a_same_named_constraint_on_another_table():
    """
    Issue 2: the primary-key lookup originally joined
    information_schema.key_column_usage to table_constraints on
    constraint_name/constraint_schema alone -- any DIFFERENT
    constraint (of any type) in the same schema sharing that exact
    name could have its key_column_usage rows cross-matched in.

    A prior version of this test tried to construct two PRIMARY KEY
    constraints sharing one name in the same schema -- confirmed via
    PostgreSQL's own documented behavior (each PRIMARY KEY/UNIQUE
    constraint requires a backing index, and index names are unique
    per SCHEMA, not per table) that this specific scenario is not
    legally constructible at all: the second CREATE TABLE would fail
    with "relation already exists" before ever reaching the lookup
    logic being tested. FOREIGN KEY constraints have no such
    restriction (they need no backing index), so this uses one of
    those instead to legally reproduce the actual class of collision
    the fix addresses.
    """
    from src.live_execution.source_connector import get_primary_key_columns

    with source_engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS table_b'))
        conn.execute(text('DROP TABLE IF EXISTS table_a'))
        conn.execute(text('DROP TABLE IF EXISTS table_c'))
        conn.execute(text(
            'CREATE TABLE table_a (a_id BIGINT, CONSTRAINT shared_name PRIMARY KEY (a_id))'
        ))
        conn.execute(text(
            'CREATE TABLE table_c (c_id BIGINT PRIMARY KEY)'
        ))
        # table_b's FOREIGN KEY deliberately shares table_a's PRIMARY
        # KEY constraint name -- legal, since a foreign key needs no
        # backing index and so isn't subject to the schema-wide unique
        # index-name restriction PRIMARY KEY/UNIQUE constraints have.
        conn.execute(text(
            'CREATE TABLE table_b (b_id BIGINT, ref_id BIGINT, '
            'CONSTRAINT shared_name FOREIGN KEY (ref_id) REFERENCES table_c(c_id))'
        ))

    pk_a = get_primary_key_columns(source_engine, "public", "table_a")
    assert pk_a == ["a_id"], (
        f"table_a's PK lookup must not pick up table_b's foreign-key column "
        f"just because the constraint names collide, got {pk_a}"
    )

    with source_engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS table_b'))
        conn.execute(text('DROP TABLE IF EXISTS table_a'))
        conn.execute(text('DROP TABLE IF EXISTS table_c'))

    with source_engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS table_a'))
        conn.execute(text('DROP TABLE IF EXISTS table_b'))


def test_high_precision_numeric_and_temporal_types_survive_publication():
    """
    Issue 4: precise PostgreSQL NUMERIC values must not be silently
    coerced to float before fingerprinting or publication, and
    DATE/TIMESTAMPTZ/UUID/JSONB must be preserved as their real types,
    not collapsed to TEXT. Issue 5: insertion must go through the
    writer's explicitly-typed table object, not DataFrame.to_sql()
    (which would silently fall back to TEXT for these columns
    regardless of what the physical table actually declared). This
    test previously only read the source and checked Python values --
    it never called PostgresPublicationWriter or queried a physical
    publication table or the stable view; fixed to do all three,
    including an all-null NUMERIC column specifically (which
    value-based type inference had no way to type correctly, since
    there's no value to inspect -- the Gold-schema-declared type is
    what makes this work now).
    """
    import decimal
    import datetime
    import uuid as uuid_module

    from src.live_execution.source_connector import read_complete_source_table
    from src.live_execution.writer import PostgresPublicationWriter, AEGIS_PUBLISH_DATA_SCHEMA
    from src.inspector import ObservedSchema, ColumnStats

    with source_engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS financial_precision_test'))
        conn.execute(text(
            'CREATE TABLE financial_precision_test ('
            'id BIGINT PRIMARY KEY, '
            'amount NUMERIC(20, 9), '
            'nullable_amount NUMERIC(20, 9), '
            'effective_date DATE, '
            'recorded_at TIMESTAMPTZ, '
            'record_id UUID, '
            'metadata JSONB)'
        ))
        conn.execute(text(
            "INSERT INTO financial_precision_test VALUES "
            "(1, 12345678901.123456789, NULL, '2026-01-15', '2026-01-15 10:30:00+00', "
            "'12345678-1234-5678-1234-567812345678', '{\"key\": \"value\"}')"
        ))

    result = read_complete_source_table(source_engine, "public", "financial_precision_test")
    df = result["dataframe"]

    assert [item["column_name"] for item in result["column_metadata"]] == list(
        df.columns
    )
    amount_metadata = next(
        item
        for item in result["column_metadata"]
        if item["column_name"] == "amount"
    )
    assert amount_metadata["numeric_precision"] == 20
    assert amount_metadata["numeric_scale"] == 9

    amount = df["amount"].iloc[0]
    assert isinstance(amount, decimal.Decimal), f"amount should be Decimal, got {type(amount)}"
    assert str(amount) == "12345678901.123456789", f"NUMERIC precision was not preserved: {amount}"

    effective_date = df["effective_date"].iloc[0]
    assert isinstance(effective_date, datetime.date)

    recorded_at = df["recorded_at"].iloc[0]
    assert isinstance(recorded_at, datetime.datetime)
    assert recorded_at.tzinfo is not None, "TIMESTAMPTZ must preserve timezone awareness"

    record_id = df["record_id"].iloc[0]
    assert isinstance(record_id, uuid_module.UUID)

    metadata = df["metadata"].iloc[0]
    assert isinstance(metadata, dict) and metadata == {"key": "value"}

    # Now actually publish it -- this is the part the original test
    # never did.
    gold_schema = ObservedSchema(
        columns={
            "id": ColumnStats(0, 0, "int64"),
            "amount": ColumnStats(0, 0, "decimal"),
            "nullable_amount": ColumnStats(1, 0, "decimal"),
            "effective_date": ColumnStats(0, 0, "date"),
            "recorded_at": ColumnStats(0, 0, "datetime_tz"),
            "record_id": ColumnStats(0, 0, "uuid"),
            "metadata": ColumnStats(0, 0, "json"),
        },
        column_order=list(df.columns),
    )

    writer = PostgresPublicationWriter(live_target_engine)
    with writer.hold_target_lock("financial_precision_target") as locked_conn:
        publish_result = writer.publish(
            locked_conn,
            logical_target="financial_precision_target",
            dataframe=df,
            execution_id=__import__("uuid").uuid4(),
            expected_row_count=1,
            gold_schema=gold_schema,
        )

    # Verify the PHYSICAL TABLE's declared types -- confirms the
    # all-null NUMERIC column kept its declared type rather than
    # falling back to TEXT (which value-based inference could never
    # have avoided, since there's no value in that column to infer
    # from).
    with live_target_engine.connect() as conn:
        columns = conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table ORDER BY ordinal_position"
        ), {"schema": AEGIS_PUBLISH_DATA_SCHEMA, "table": publish_result["physical_table"]}).fetchall()
        types_by_column = {row[0]: row[1] for row in columns}

    assert types_by_column["amount"] == "numeric"
    assert types_by_column["nullable_amount"] == "numeric", (
        "an all-null NUMERIC column must keep its Gold-declared type, not fall back to text"
    )
    assert types_by_column["effective_date"] == "date"
    assert types_by_column["recorded_at"] == "timestamp with time zone"
    assert types_by_column["record_id"] == "uuid"
    assert types_by_column["metadata"] == "jsonb"

    # Verify the actual published VALUES survive through the real
    # insert path (writer.publish()'s Table.insert(), not to_sql()).
    with live_target_engine.connect() as conn:
        published_row = conn.execute(text(
            'SELECT amount, nullable_amount, effective_date, recorded_at, record_id, metadata '
            f'FROM "aegis_publish"."financial_precision_target"'
        )).fetchone()

    assert str(published_row[0]) == "12345678901.123456789"
    assert published_row[1] is None
    assert published_row[4] is not None  # UUID round-tripped without erroring

    with source_engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS financial_precision_test'))


def test_source_changed_since_approval_blocks_execution():
    ticket_id, _ = _register_and_approve_source_ticket()

    # Mutate the source AFTER approval -- the stored fingerprint on
    # the ticket no longer matches.
    with source_engine.begin() as conn:
        conn.execute(text('INSERT INTO customers VALUES (999, \'zed\')'))

    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_13"),
    )
    assert response.status_code == 409
    assert "changed" in response.json()["detail"].lower()


def test_surgeon_failure_during_execute_live_marks_failed_not_stuck_running():
    ticket_id, _ = _register_and_approve_source_ticket()
    with patch("src.api.app.AegisSurgeon.execute", side_effect=RuntimeError("boom")):
        response = client.post(
            f"/approvals/{ticket_id}/execute-live",
            json=_execute_live_body("customer_master_14"),
        )
    assert response.status_code == 500

    with TestSessionLocal() as db:
        record = db.query(LiveExecutionRecord).filter(
            LiveExecutionRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()
        assert record.status == "FAILED"


def test_target_write_failure_marks_execution_failed():
    ticket_id, _ = _register_and_approve_source_ticket()
    with patch("src.api.app.PostgresPublicationWriter.publish", side_effect=RuntimeError("target db exploded")):
        response = client.post(
            f"/approvals/{ticket_id}/execute-live",
            json=_execute_live_body("customer_master_15"),
        )
    assert response.status_code == 500

    with TestSessionLocal() as db:
        record = db.query(LiveExecutionRecord).filter(
            LiveExecutionRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()
        assert record.status == "FAILED"


def test_ambiguous_commit_recovers_via_target_side_marker():
    """
    Simulates a connection drop AFTER the target transaction actually
    committed -- the PUBLISH marker genuinely exists, but the call
    site still sees an exception. Must resolve to COMPLETED via the
    target-side marker, not incorrectly marked FAILED.
    """
    ticket_id, _ = _register_and_approve_source_ticket()

    real_publish = PostgresPublicationWriter.publish

    def publish_then_raise(self, *args, **kwargs):
        result = real_publish(self, *args, **kwargs)
        raise RuntimeError("simulated connection drop after commit")

    with patch("src.api.app.PostgresPublicationWriter.publish", publish_then_raise):
        response = client.post(
            f"/approvals/{ticket_id}/execute-live",
            json=_execute_live_body("customer_master_16"),
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "COMPLETED"
    assert "note" in body

    rows = _view_rows("customer_master_16")
    assert len(rows) == 3


def test_reconciliation_heals_a_stuck_running_record():
    """
    Simulates the crash window directly: a live execution whose
    target-side PUBLISH actually committed, but whose governance
    record never got updated past RUNNING.
    """
    ticket_id, schema_version_id = _register_and_approve_source_ticket()

    with TestSessionLocal() as db:
        from src.governance.approval_repository import PostgresApprovalRepository
        ticket = PostgresApprovalRepository(db).get(ticket_id)
        manifest = db.query(HealingManifestRecord).filter(
            HealingManifestRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()

        live_repo = LiveExecutionRepository(db)
        live_record = live_repo.create_running(
            ticket_id=ticket_id,
            sandbox_manifest_id=str(manifest.manifest_id),
            schema_version_id=schema_version_id,
            logical_target="customer_master_17",
            requested_by="mo",
            original_row_count=manifest.original_row_count,
            final_row_count=manifest.final_row_count,
            risk_level=manifest.risk_level,
            integrity_status=manifest.integrity_status,
            source_schema=ticket.source_schema,
            source_table=ticket.source_table,
            source_primary_key=ticket.source_primary_key,
            source_row_count=ticket.source_row_count,
            source_schema_fingerprint=ticket.source_schema_fingerprint,
            source_dataset_fingerprint=ticket.source_dataset_fingerprint,
        )

        # The persisted ticket dataset is the trusted source snapshot
        # before repair (Customer_ID). Reproduce the live Surgeon step
        # so the simulated committed publication contains the same
        # corrected dataset (customer_id) that execute_live() would
        # have written before the governance process supposedly
        # crashed.
        corrected_dataset = ticket.target_dataset.copy()
        execution_result, _ = AegisSurgeon().execute(
            repair_plan=ticket.repair_plan,
            observed_schema=ticket.observed_schema,
            gold_schema=ticket.gold_schema,
            target_dataset=corrected_dataset,
            operator="mo",
            execution_mode="live",
            allowed_modes=["sandbox", "live"],
        )
        assert execution_result.applied
        assert execution_result.validation.success

        writer = PostgresPublicationWriter(live_target_engine)
        with writer.hold_target_lock("customer_master_17") as locked_conn:
            writer.publish(
                locked_conn,
                logical_target="customer_master_17",
                dataframe=corrected_dataset,
                execution_id=live_record.live_execution_id,
                expected_row_count=3,
                gold_schema=ticket.gold_schema,
            )
        stuck_id = str(live_record.live_execution_id)

    fetched = client.get(f"/live-executions/{stuck_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "COMPLETED"


def test_reconciliation_heals_a_stuck_rolling_back_record():
    ticket_id, _ = _register_and_approve_source_ticket()
    executed = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_18"),
    ).json()

    with TestSessionLocal() as db:
        live_repo = LiveExecutionRepository(db)
        record = live_repo.mark_rolling_back(executed["live_execution_id"], operator="mo")

        writer = PostgresPublicationWriter(live_target_engine)
        with writer.hold_target_lock("customer_master_18") as locked_conn:
            writer.rollback_to_previous(
                locked_conn,
                logical_target="customer_master_18",
                execution_id=record.live_execution_id,
                previous_physical_table=record.previous_physical_table,
            )
        stuck_id = str(record.live_execution_id)

    fetched = client.get(f"/live-executions/{stuck_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "ROLLED_BACK"


def test_staleness_gate_prevents_premature_reconciliation():
    ticket_id, schema_version_id = _register_and_approve_source_ticket()

    with TestSessionLocal() as db:
        from src.governance.approval_repository import PostgresApprovalRepository
        ticket = PostgresApprovalRepository(db).get(ticket_id)
        manifest = db.query(HealingManifestRecord).filter(
            HealingManifestRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()
        live_repo = LiveExecutionRepository(db)
        live_record = live_repo.create_running(
            ticket_id=ticket_id,
            sandbox_manifest_id=str(manifest.manifest_id),
            schema_version_id=schema_version_id,
            logical_target="customer_master_19",
            requested_by="mo",
            original_row_count=manifest.original_row_count,
            final_row_count=manifest.final_row_count,
            risk_level=manifest.risk_level,
            integrity_status=manifest.integrity_status,
            source_schema=ticket.source_schema,
            source_table=ticket.source_table,
            source_primary_key=ticket.source_primary_key,
            source_row_count=ticket.source_row_count,
            source_schema_fingerprint=ticket.source_schema_fingerprint,
            source_dataset_fingerprint=ticket.source_dataset_fingerprint,
        )
        stuck_id = str(live_record.live_execution_id)

    old_threshold = os.environ.get("AEGIS_LIVE_EXECUTION_STALE_SECONDS")
    os.environ["AEGIS_LIVE_EXECUTION_STALE_SECONDS"] = "3600"
    try:
        fetched = client.get(f"/live-executions/{stuck_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "RUNNING"
    finally:
        if old_threshold is not None:
            os.environ["AEGIS_LIVE_EXECUTION_STALE_SECONDS"] = old_threshold
        else:
            os.environ.pop("AEGIS_LIVE_EXECUTION_STALE_SECONDS", None)


def test_reconciliation_respects_lock_held_before_publish_even_starts():
    """
    The session lock is held for the WHOLE flow (before create_running,
    not just inside publish()'s DDL transaction). Simulates a slow
    Surgeon call by directly holding the lock on a separate connection
    while a RUNNING record with no marker exists yet.
    """
    ticket_id, schema_version_id = _register_and_approve_source_ticket()

    with TestSessionLocal() as db:
        from src.governance.approval_repository import PostgresApprovalRepository
        ticket = PostgresApprovalRepository(db).get(ticket_id)
        manifest = db.query(HealingManifestRecord).filter(
            HealingManifestRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()
        live_repo = LiveExecutionRepository(db)
        live_record = live_repo.create_running(
            ticket_id=ticket_id,
            sandbox_manifest_id=str(manifest.manifest_id),
            schema_version_id=schema_version_id,
            logical_target="customer_master_20",
            requested_by="mo",
            original_row_count=manifest.original_row_count,
            final_row_count=manifest.final_row_count,
            risk_level=manifest.risk_level,
            integrity_status=manifest.integrity_status,
            source_schema=ticket.source_schema,
            source_table=ticket.source_table,
            source_primary_key=ticket.source_primary_key,
            source_row_count=ticket.source_row_count,
            source_schema_fingerprint=ticket.source_schema_fingerprint,
            source_dataset_fingerprint=ticket.source_dataset_fingerprint,
        )
        stuck_id = str(live_record.live_execution_id)

    writer = PostgresPublicationWriter(live_target_engine)
    with writer.hold_target_lock("customer_master_20"):
        fetched = client.get(f"/live-executions/{stuck_id}")
        assert fetched.status_code == 200
        assert fetched.json()["status"] == "RUNNING"

    fetched_after = client.get(f"/live-executions/{stuck_id}")
    assert fetched_after.status_code == 200
    assert fetched_after.json()["status"] == "FAILED"


def test_concurrent_rollback_only_one_succeeds():
    ticket_id, _ = _register_and_approve_source_ticket()
    executed = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_21"),
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


def test_manifest_execution_mode_mismatch_is_refused():
    ticket_id, _ = _register_and_approve_source_ticket()

    with TestSessionLocal() as db:
        manifest = db.query(HealingManifestRecord).filter(
            HealingManifestRecord.ticket_id == __import__("uuid").UUID(ticket_id)
        ).one()
        manifest.execution_mode = "corrupted"
        db.commit()

    response = client.post(
        f"/approvals/{ticket_id}/execute-live",
        json=_execute_live_body("customer_master_22"),
    )
    assert response.status_code == 422


def test_migration_0003_creates_expected_tables_and_columns():
    from alembic.config import Config
    from alembic import command

    project_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)

    # Base.metadata.drop_all() does not own Alembic's version table.
    # Leaving an old 0002/0003 row behind while dropping the actual
    # application tables makes Alembic skip migrations whose tables no
    # longer exist. Reset both schema objects and revision state so the
    # test genuinely exercises 0001 -> 0002 -> 0003 from an empty,
    # disposable database.
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

    try:
        command.upgrade(cfg, "head")

        inspector = sa_inspect(engine)
        assert "live_executions" in inspector.get_table_names()

        columns = {c["name"] for c in inspector.get_columns("live_executions")}
        expected = {
            "live_execution_id", "ticket_id", "sandbox_manifest_id", "schema_version_id",
            "logical_target", "physical_table", "previous_physical_table",
            "status", "requested_by", "started_at", "completed_at",
            "rollback_started_at", "rolled_back_by", "rolled_back_at",
            "failure_reason", "original_row_count", "final_row_count", "risk_level",
            "integrity_status", "source_schema", "source_table", "source_primary_key",
            "source_row_count", "source_schema_fingerprint", "source_dataset_fingerprint",
        }
        assert expected.issubset(columns)

        ticket_columns = {c["name"] for c in inspector.get_columns("approval_tickets")}
        assert {
            "source_schema", "source_table", "source_primary_key", "source_row_count",
            "source_schema_fingerprint", "source_dataset_fingerprint", "live_eligible",
        }.issubset(ticket_columns)

        manifest_columns = {c["name"] for c in inspector.get_columns("healing_manifests")}
        assert "corrected_output_fingerprint" in manifest_columns
        assert "source_dataset_fingerprint" in manifest_columns

        index_names = {idx["name"] for idx in inspector.get_indexes("live_executions")}
        assert "uq_live_executions_ticket_active_or_done" in index_names
        assert "uq_live_executions_target_in_flight" in index_names

        command.downgrade(cfg, "0002")
        tables_after = sa_inspect(engine).get_table_names()
        assert "live_executions" not in tables_after
    finally:
        # Restore the module's normal Base.metadata-managed test schema
        # and remove Alembic bookkeeping so this migration test neither
        # depends on nor contaminates other integration-test modules.
        Base.metadata.drop_all(bind=engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        Base.metadata.create_all(bind=engine)
