"""PostgreSQL upgrade/downgrade verification for Phase 3.2 migration 0005."""

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import get_verified_test_database_url


TEST_DATABASE_URL = get_verified_test_database_url()

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect as sa_inspect, text

from src.db.models import Base


engine = create_engine(TEST_DATABASE_URL)


def _alembic_config() -> Config:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config


def _reset_database() -> None:
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))


def test_0005_upgrade_preserves_history_and_downgrade_restores_replay_payload():
    config = _alembic_config()
    legacy_ticket_id = uuid.uuid4()
    phase_3_2_ticket_id = uuid.uuid4()
    source_system_id = uuid.uuid4()
    source_dataset_id = uuid.uuid4()
    snapshot_id = uuid.uuid4()
    ingestion_run_id = uuid.uuid4()
    now = datetime.now(UTC)
    legacy_payload = {"format_version": 2, "data": {"id": [7]}}
    snapshot_payload = {"format_version": 2, "data": {"id": [11]}}

    _reset_database()
    try:
        command.upgrade(config, "0004")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO approval_tickets (
                        ticket_id, proposed_action, confidence, explanation,
                        status, created_at, observed_schema, gold_schema,
                        target_dataset, live_eligible
                    ) VALUES (
                        :ticket_id, 'RENAME_COLUMN:id->customer_id', 0.95,
                        'legacy ticket', 'PENDING', :created_at,
                        CAST(:observed_schema AS jsonb),
                        CAST(:gold_schema AS jsonb),
                        CAST(:target_dataset AS jsonb), false
                    )
                    """
                ),
                {
                    "ticket_id": legacy_ticket_id,
                    "created_at": now,
                    "observed_schema": json.dumps({}),
                    "gold_schema": json.dumps({}),
                    "target_dataset": json.dumps(legacy_payload),
                },
            )

        command.upgrade(config, "head")

        inspector = sa_inspect(engine)
        assert {
            "source_systems",
            "source_datasets",
            "dataset_snapshots",
            "ingestion_runs",
            "publication_systems",
            "publication_targets",
        }.issubset(inspector.get_table_names())
        approval_columns = {
            column["name"]: column
            for column in inspector.get_columns("approval_tickets")
        }
        assert approval_columns["source_ingestion_run_id"]["nullable"] is True
        assert approval_columns["target_dataset"]["nullable"] is True
        approval_checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("approval_tickets")
        }
        assert "ck_approval_tickets_replay_source" in approval_checks

        with engine.begin() as connection:
            historical_lineage = connection.execute(
                text(
                    "SELECT source_ingestion_run_id FROM approval_tickets "
                    "WHERE ticket_id = :ticket_id"
                ),
                {"ticket_id": legacy_ticket_id},
            ).scalar_one()
            assert historical_lineage is None

            connection.execute(
                text(
                    """
                    INSERT INTO source_systems (
                        source_system_id, system_key, platform, binding_version,
                        endpoint_binding_fingerprint, created_at
                    ) VALUES (
                        :id, 'erp-source', 'POSTGRESQL', 1, :fingerprint, :created_at
                    )
                    """
                ),
                {
                    "id": source_system_id,
                    "fingerprint": "a" * 64,
                    "created_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO source_datasets (
                        source_dataset_id, source_system_id, source_schema,
                        source_table, created_at
                    ) VALUES (
                        :id, :system_id, 'public', 'customers', :created_at
                    )
                    """
                ),
                {
                    "id": source_dataset_id,
                    "system_id": source_system_id,
                    "created_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO dataset_snapshots (
                        dataset_snapshot_id, source_dataset_id,
                        provenance_fingerprint, source_primary_key,
                        source_row_count, source_schema_fingerprint,
                        source_dataset_fingerprint, column_metadata,
                        payload_format_version, snapshot_payload, created_at
                    ) VALUES (
                        :id, :dataset_id, :provenance,
                        CAST(:primary_key AS jsonb), 1, :schema_fingerprint,
                        :dataset_fingerprint, CAST(:column_metadata AS jsonb),
                        2, CAST(:snapshot_payload AS jsonb), :created_at
                    )
                    """
                ),
                {
                    "id": snapshot_id,
                    "dataset_id": source_dataset_id,
                    "provenance": "b" * 64,
                    "primary_key": json.dumps(["id"]),
                    "schema_fingerprint": "c" * 64,
                    "dataset_fingerprint": "d" * 64,
                    "column_metadata": json.dumps([{"name": "id"}]),
                    "snapshot_payload": json.dumps(snapshot_payload),
                    "created_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO ingestion_runs (
                        ingestion_run_id, source_dataset_id, dataset_snapshot_id,
                        purpose, outcome, started_at, completed_at
                    ) VALUES (
                        :id, :dataset_id, :snapshot_id,
                        'SIMULATION', 'CAPTURED', :started_at, :completed_at
                    )
                    """
                ),
                {
                    "id": ingestion_run_id,
                    "dataset_id": source_dataset_id,
                    "snapshot_id": snapshot_id,
                    "started_at": now,
                    "completed_at": now,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO approval_tickets (
                        ticket_id, proposed_action, confidence, explanation,
                        status, created_at, observed_schema, gold_schema,
                        target_dataset, live_eligible, source_ingestion_run_id
                    ) VALUES (
                        :ticket_id, 'RENAME_COLUMN:id->customer_id', 0.95,
                        'Phase 3.2 ticket', 'PENDING', :created_at,
                        CAST(:observed_schema AS jsonb),
                        CAST(:gold_schema AS jsonb), NULL, false, :run_id
                    )
                    """
                ),
                {
                    "ticket_id": phase_3_2_ticket_id,
                    "created_at": now,
                    "observed_schema": json.dumps({}),
                    "gold_schema": json.dumps({}),
                    "run_id": ingestion_run_id,
                },
            )

        command.downgrade(config, "0004")

        inspector = sa_inspect(engine)
        assert "ingestion_runs" not in inspector.get_table_names()
        approval_columns = {
            column["name"]: column
            for column in inspector.get_columns("approval_tickets")
        }
        assert "source_ingestion_run_id" not in approval_columns
        assert approval_columns["target_dataset"]["nullable"] is False
        with engine.connect() as connection:
            restored_payload = connection.execute(
                text(
                    "SELECT target_dataset FROM approval_tickets "
                    "WHERE ticket_id = :ticket_id"
                ),
                {"ticket_id": phase_3_2_ticket_id},
            ).scalar_one()
            unchanged_payload = connection.execute(
                text(
                    "SELECT target_dataset FROM approval_tickets "
                    "WHERE ticket_id = :ticket_id"
                ),
                {"ticket_id": legacy_ticket_id},
            ).scalar_one()
        assert restored_payload == snapshot_payload
        assert unchanged_payload == legacy_payload
    finally:
        _reset_database()
        Base.metadata.create_all(bind=engine)
