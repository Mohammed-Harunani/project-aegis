"""Pure metadata-contract tests for Phase 3.2 ORM and Alembic lineage."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from src.db.models import Base


def _column_names(table_name):
    return set(Base.metadata.tables[table_name].columns.keys())


def _constraint_names(table_name):
    return {
        constraint.name
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name
    }


def _index_names(table_name):
    return {index.name for index in Base.metadata.tables[table_name].indexes}


def test_phase_3_2_tables_are_present_in_metadata():
    assert {
        "source_systems",
        "source_datasets",
        "dataset_snapshots",
        "ingestion_runs",
        "publication_systems",
        "publication_targets",
    }.issubset(Base.metadata.tables)


def test_source_identity_columns_and_constraints_are_locked():
    assert _column_names("source_systems") == {
        "source_system_id",
        "system_key",
        "platform",
        "binding_version",
        "endpoint_binding_fingerprint",
        "created_at",
    }
    assert {
        "uq_source_systems_system_key",
        "uq_source_systems_endpoint_binding_fingerprint",
        "ck_source_systems_platform",
        "ck_source_systems_binding_version",
        "ck_source_systems_system_key",
        "ck_source_systems_endpoint_binding_fingerprint",
    }.issubset(_constraint_names("source_systems"))

    assert {
        "ck_source_datasets_source_schema",
        "ck_source_datasets_source_table",
    }.issubset(_constraint_names("source_datasets"))


def test_dataset_snapshot_contract_is_present():
    assert {
        "dataset_snapshot_id",
        "source_dataset_id",
        "provenance_fingerprint",
        "source_primary_key",
        "source_row_count",
        "source_schema_fingerprint",
        "source_dataset_fingerprint",
        "column_metadata",
        "payload_format_version",
        "snapshot_payload",
        "created_at",
    } == _column_names("dataset_snapshots")
    assert "uq_dataset_snapshots_dataset_provenance" in _constraint_names(
        "dataset_snapshots"
    )
    assert "ix_dataset_snapshots_dataset_created" in _index_names(
        "dataset_snapshots"
    )


def test_ingestion_run_terminal_contract_is_present():
    assert {
        "ck_ingestion_runs_purpose",
        "ck_ingestion_runs_outcome",
        "ck_ingestion_runs_timestamp_order",
        "ck_ingestion_runs_valid_combination",
        "ck_ingestion_runs_failed_code",
    }.issubset(_constraint_names("ingestion_runs"))
    assert {
        "ix_ingestion_runs_dataset_completed",
        "ix_ingestion_runs_baseline",
        "ix_ingestion_runs_dataset_snapshot_id",
    }.issubset(_index_names("ingestion_runs"))


def test_publication_identity_contract_is_present():
    assert _column_names("publication_targets") == {
        "publication_target_id",
        "publication_system_id",
        "logical_target",
        "created_at",
    }
    assert "uq_publication_targets_system_logical_target" in _constraint_names(
        "publication_targets"
    )
    assert "ck_publication_targets_logical_target" in _constraint_names(
        "publication_targets"
    )
    assert "ck_publication_systems_system_key" in _constraint_names(
        "publication_systems"
    )


def test_existing_tables_have_nullable_historical_lineage():
    approval = Base.metadata.tables["approval_tickets"]
    manifest = Base.metadata.tables["healing_manifests"]
    live = Base.metadata.tables["live_executions"]

    assert approval.c.source_ingestion_run_id.nullable is True
    assert approval.c.target_dataset.nullable is True
    assert "ck_approval_tickets_replay_source" in _constraint_names(
        "approval_tickets"
    )
    assert manifest.c.source_ingestion_run_id.nullable is True
    assert live.c.simulation_ingestion_run_id.nullable is True
    assert live.c.revalidation_ingestion_run_id.nullable is True
    assert live.c.publication_target_id.nullable is True


def test_existing_lineage_foreign_key_indexes_are_present():
    assert "ix_approval_tickets_source_ingestion_run_id" in _index_names(
        "approval_tickets"
    )
    assert "ix_healing_manifests_source_ingestion_run_id" in _index_names(
        "healing_manifests"
    )
    assert {
        "ix_live_executions_simulation_ingestion_run_id",
        "ix_live_executions_revalidation_ingestion_run_id",
        "ix_live_executions_publication_target_id",
    }.issubset(_index_names("live_executions"))


def test_alembic_head_is_0005_with_linear_parent_0004():
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_current_head() == "0005"
    assert scripts.get_revision("0005").down_revision == "0004"
