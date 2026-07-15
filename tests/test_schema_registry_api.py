"""
Postgres integration tests for the Phase 2.4 Schema Registry:
registration, versioning, retrieval, concurrency, and migration
lineage on approval tickets / healing manifests.

Same requirements as test_api.py: a dedicated aegis_test database,
TEST_DATABASE_URL exported, never falls back to DATABASE_URL. Uses
the shared guard in conftest.py (get_verified_test_database_url())
rather than duplicating that logic a third time.

Written and syntax-checked but NOT executed -- no Docker, Postgres,
sqlalchemy, or network access in the environment these were written
in. Treat every assertion here as a draft until run for real.
"""

import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import get_verified_test_database_url

TEST_DATABASE_URL = get_verified_test_database_url()

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.app import app
from src.db.session import get_db
from src.db.models import (
    Base,
    ApprovalTicketRecord,
    HealingManifestRecord,
    GoldSchemaRecord,
    SchemaVersionRecord,
)
from src.consultant.consultant import RepairPlan
from src.registry.repository import SchemaRegistryRepository


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
        conn.execute(SchemaVersionRecord.__table__.delete())
        conn.execute(GoldSchemaRecord.__table__.delete())


CUSTOMER_MASTER_V1 = {
    "format_version": 1,
    "columns": [
        {"name": "customer_id", "dtype": "int64"},
        {"name": "customer_name", "dtype": "object"},
    ],
    "description": "Canonical customer master schema",
    "change_summary": "Initial registration",
    "created_by": "mo",
}

CUSTOMER_MASTER_V2 = dict(
    CUSTOMER_MASTER_V1,
    columns=[
        {"name": "customer_id", "dtype": "int64"},
        {"name": "customer_name", "dtype": "object"},
        {"name": "balance", "dtype": "float64"},
    ],
    change_summary="Added balance column",
)


# --- Registration, versioning, retrieval -----------------------------

def test_first_registration_creates_version_1():
    response = client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    assert response.status_code == 200
    assert response.json()["version_number"] == 1


def test_changed_definition_creates_version_2():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    response = client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V2)
    assert response.status_code == 200
    assert response.json()["version_number"] == 2


def test_identical_definition_is_rejected():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    response = client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    assert response.status_code == 409


def test_exact_version_retrieval():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    response = client.get("/schemas/customer_master/versions/1")
    body = response.json()
    assert response.status_code == 200
    assert body["version_number"] == 1
    assert body["schema_definition"]["columns"][0]["name"] == "customer_id"


def test_latest_version_retrieval():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V2)
    response = client.get("/schemas/customer_master/latest")
    assert response.status_code == 200
    assert response.json()["version_number"] == 2


def test_history_returned_newest_first_without_full_definitions():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V2)
    response = client.get("/schemas/customer_master/versions")
    body = response.json()
    assert response.status_code == 200
    assert [v["version_number"] for v in body["versions"]] == [2, 1]
    assert "schema_definition" not in body["versions"][0]


def test_column_order_survives_real_postgres_jsonb_storage():
    """
    The specific lesson from Phase 2.3: Postgres JSONB does not
    guarantee key/array order the way plain json does -- this has to
    go through a real round trip, not just Python-level json.dumps.
    """
    non_alphabetical = {
        "format_version": 1,
        "columns": [
            {"name": "zebra_col", "dtype": "int64"},
            {"name": "apple_col", "dtype": "object"},
            {"name": "mango_col", "dtype": "float64"},
        ],
    }
    client.post("/schemas/customer_master/versions", json=non_alphabetical)
    response = client.get("/schemas/customer_master/versions/1")
    names = [c["name"] for c in response.json()["schema_definition"]["columns"]]
    assert names == ["zebra_col", "apple_col", "mango_col"]


def test_invalid_dtype_rejected_by_api():
    bad = dict(CUSTOMER_MASTER_V1, columns=[{"name": "x", "dtype": "not_a_real_dtype"}])
    assert client.post("/schemas/customer_master/versions", json=bad).status_code == 422


def test_duplicate_column_names_rejected_by_api():
    bad = dict(
        CUSTOMER_MASTER_V1,
        columns=[{"name": "x", "dtype": "int64"}, {"name": "x", "dtype": "float64"}],
    )
    assert client.post("/schemas/customer_master/versions", json=bad).status_code == 422


def test_unknown_schema_returns_404():
    assert client.get("/schemas/does_not_exist/latest").status_code == 404


def test_unknown_version_returns_404():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    assert client.get("/schemas/customer_master/versions/99").status_code == 404


def test_concurrent_version_creation_produces_unique_sequential_versions():
    """
    Two threads, two independent Sessions, both registering a
    different definition against the same brand-new schema name at
    once -- tests the repository directly rather than through
    TestClient/HTTP, same reasoning as Phase 2.3's concurrent-approval
    test: TestClient's concurrency model isn't a reliable way to prove
    a database-level row lock.
    """
    outcomes = []
    lock = threading.Lock()

    def register(col_name):
        session = TestSessionLocal()
        try:
            version = SchemaRegistryRepository(session).register_version(
                schema_name="concurrent_test_schema",
                format_version=1,
                columns=[{"name": col_name, "dtype": "int64"}],
            )
            with lock:
                outcomes.append(version.version_number)
        finally:
            session.close()

    threads = [threading.Thread(target=register, args=(f"col_{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == [1, 2]


# --- Migration API integration ----------------------------------------

def test_both_gold_schema_and_schema_name_returns_422():
    response = client.post(
        "/simulate-migration",
        json={
            "gold_schema": {"customer_id": "int64"},
            "schema_name": "customer_master",
            "sample_data": {"customer_id": [1]},
        },
    )
    assert response.status_code == 422


def test_neither_gold_schema_nor_schema_name_returns_422():
    response = client.post("/simulate-migration", json={"sample_data": {"customer_id": [1]}})
    assert response.status_code == 422


def test_registered_schema_simulation_uses_correct_version():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V2)

    response = client.post(
        "/simulate-migration",
        json={
            "schema_name": "customer_master",
            "schema_version": 1,
            "sample_data": {"Customer_ID": [1, 2], "customer_name": ["A", "B"]},
        },
    )
    v1 = client.get("/schemas/customer_master/versions/1").json()

    assert response.status_code == 200
    assert response.json()["schema_version_id"] == v1["schema_version_id"]


def test_omitted_version_resolves_latest():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V2)

    response = client.post(
        "/simulate-migration",
        json={
            "schema_name": "customer_master",
            "sample_data": {"customer_id": [1], "customer_name": ["A"]},
        },
    )
    latest = client.get("/schemas/customer_master/latest").json()

    assert response.json()["schema_version_id"] == latest["schema_version_id"]


def test_legacy_direct_schema_request_still_works_with_null_lineage():
    response = client.post(
        "/simulate-migration",
        json={"gold_schema": {"customer_id": "int64"}, "sample_data": {"Customer_ID": [1, 2]}},
    )
    assert response.status_code == 200
    assert response.json()["schema_version_id"] is None


# --- Lineage on tickets and manifests ----------------------------------

def test_pending_approval_stores_schema_version_id():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)

    response = client.post(
        "/simulate-migration",
        json={
            "schema_name": "customer_master",
            "sample_data": {"Customer_ID": [1, 2], "customer_name": ["A", "B"]},
        },
    )
    body = response.json()
    assert body["status"] == "PENDING_APPROVAL"

    ticket = client.get(f"/approvals/{body['ticket_id']}").json()
    assert ticket["schema_version_id"] == body["schema_version_id"]
    assert ticket["schema_version_id"] is not None


def test_manually_approved_manifest_retains_same_schema_version_id():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)

    submit = client.post(
        "/simulate-migration",
        json={
            "schema_name": "customer_master",
            "sample_data": {"Customer_ID": [1, 2], "customer_name": ["A", "B"]},
        },
    )
    ticket_id = submit.json()["ticket_id"]
    schema_version_id = submit.json()["schema_version_id"]

    client.post(f"/approvals/{ticket_id}/approve", json={"operator": "mo"})

    with TestSessionLocal() as db:
        manifest = (
            db.query(HealingManifestRecord)
            .filter(HealingManifestRecord.ticket_id == ticket_id)
            .one()
        )
        assert str(manifest.schema_version_id) == schema_version_id


def test_auto_approved_manifest_stores_schema_version_id_without_ticket():
    client.post("/schemas/customer_master/versions", json=CUSTOMER_MASTER_V1)

    fake_plan = RepairPlan(
        proposed_action="RENAME_COLUMN Customer_ID -> customer_id",
        confidence=0.95,
        explanation="forced high-confidence plan for this test",
    )
    with patch("src.api.app.AegisConsultant.propose_repairs", return_value=[fake_plan]):
        response = client.post(
            "/simulate-migration",
            json={
                "schema_name": "customer_master",
                "sample_data": {"Customer_ID": [1, 2], "customer_name": ["A", "B"]},
            },
        )
    body = response.json()
    assert body["status"] == "EXECUTED"

    with TestSessionLocal() as db:
        manifest = (
            db.query(HealingManifestRecord)
            .filter(HealingManifestRecord.ticket_id.is_(None))
            .one()
        )
        assert str(manifest.schema_version_id) == body["schema_version_id"]
