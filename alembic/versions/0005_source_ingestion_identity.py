"""source ingestion and dataset identity foundation

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-12

Creates Phase 3.2's immutable identity, snapshot, ingestion-run, and
publication-target schema. Existing workflow rows receive nullable lineage
columns only; no historical identity is fabricated.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_systems",
        sa.Column("source_system_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("system_key", sa.Text, nullable=False),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("binding_version", sa.Integer, nullable=False),
        sa.Column("endpoint_binding_fingerprint", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("system_key", name="uq_source_systems_system_key"),
        sa.UniqueConstraint(
            "endpoint_binding_fingerprint",
            name="uq_source_systems_endpoint_binding_fingerprint",
        ),
        sa.CheckConstraint("platform = 'POSTGRESQL'", name="ck_source_systems_platform"),
        sa.CheckConstraint("binding_version = 1", name="ck_source_systems_binding_version"),
        sa.CheckConstraint(
            "system_key ~ '^[a-z][a-z0-9_-]{2,63}$'",
            name="ck_source_systems_system_key",
        ),
        sa.CheckConstraint(
            "endpoint_binding_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_source_systems_endpoint_binding_fingerprint",
        ),
    )

    op.create_table(
        "source_datasets",
        sa.Column("source_dataset_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_system_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_schema", sa.Text, nullable=False),
        sa.Column("source_table", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_system_id"],
            ["source_systems.source_system_id"],
            name="fk_source_datasets_source_system_id",
        ),
        sa.UniqueConstraint(
            "source_system_id",
            "source_schema",
            "source_table",
            name="uq_source_datasets_system_relation",
        ),
        sa.CheckConstraint(
            "source_schema ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'",
            name="ck_source_datasets_source_schema",
        ),
        sa.CheckConstraint(
            "source_table ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'",
            name="ck_source_datasets_source_table",
        ),
    )

    op.create_table(
        "dataset_snapshots",
        sa.Column("dataset_snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provenance_fingerprint", sa.Text, nullable=False),
        sa.Column("source_primary_key", postgresql.JSONB, nullable=False),
        sa.Column("source_row_count", sa.Integer, nullable=False),
        sa.Column("source_schema_fingerprint", sa.Text, nullable=False),
        sa.Column("source_dataset_fingerprint", sa.Text, nullable=False),
        sa.Column("column_metadata", postgresql.JSONB, nullable=False),
        sa.Column("payload_format_version", sa.Integer, nullable=False),
        sa.Column("snapshot_payload", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_dataset_id"],
            ["source_datasets.source_dataset_id"],
            name="fk_dataset_snapshots_source_dataset_id",
        ),
        sa.UniqueConstraint(
            "source_dataset_id",
            "provenance_fingerprint",
            name="uq_dataset_snapshots_dataset_provenance",
        ),
        sa.CheckConstraint(
            "provenance_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_dataset_snapshots_provenance_fingerprint",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_primary_key) = 'array' "
            "AND jsonb_array_length(source_primary_key) > 0",
            name="ck_dataset_snapshots_primary_key_array",
        ),
        sa.CheckConstraint("source_row_count >= 0", name="ck_dataset_snapshots_row_count"),
        sa.CheckConstraint(
            "source_schema_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_dataset_snapshots_schema_fingerprint",
        ),
        sa.CheckConstraint(
            "source_dataset_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_dataset_snapshots_dataset_fingerprint",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(column_metadata) = 'array' "
            "AND jsonb_array_length(column_metadata) > 0",
            name="ck_dataset_snapshots_column_metadata_array",
        ),
        sa.CheckConstraint(
            "payload_format_version > 0",
            name="ck_dataset_snapshots_payload_format_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot_payload) = 'object'",
            name="ck_dataset_snapshots_payload_object",
        ),
    )
    op.create_index(
        "ix_dataset_snapshots_dataset_created",
        "dataset_snapshots",
        ["source_dataset_id", "created_at"],
    )

    op.create_table(
        "ingestion_runs",
        sa.Column("ingestion_run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("baseline_ingestion_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failure_code", sa.Text, nullable=True),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.ForeignKeyConstraint(
            ["source_dataset_id"],
            ["source_datasets.source_dataset_id"],
            name="fk_ingestion_runs_source_dataset_id",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_snapshot_id"],
            ["dataset_snapshots.dataset_snapshot_id"],
            name="fk_ingestion_runs_dataset_snapshot_id",
        ),
        sa.ForeignKeyConstraint(
            ["baseline_ingestion_run_id"],
            ["ingestion_runs.ingestion_run_id"],
            name="fk_ingestion_runs_baseline_ingestion_run_id",
        ),
        sa.CheckConstraint(
            "purpose IN ('SIMULATION', 'LIVE_REVALIDATION')",
            name="ck_ingestion_runs_purpose",
        ),
        sa.CheckConstraint(
            "outcome IN ('CAPTURED', 'MATCHED', 'DRIFTED', 'FAILED')",
            name="ck_ingestion_runs_outcome",
        ),
        sa.CheckConstraint(
            "completed_at >= started_at",
            name="ck_ingestion_runs_timestamp_order",
        ),
        sa.CheckConstraint(
            "(purpose = 'SIMULATION' AND outcome = 'CAPTURED' "
            "AND dataset_snapshot_id IS NOT NULL AND baseline_ingestion_run_id IS NULL) OR "
            "(purpose = 'SIMULATION' AND outcome = 'FAILED' "
            "AND baseline_ingestion_run_id IS NULL) OR "
            "(purpose = 'LIVE_REVALIDATION' AND outcome IN ('MATCHED', 'DRIFTED') "
            "AND dataset_snapshot_id IS NOT NULL AND baseline_ingestion_run_id IS NOT NULL) OR "
            "(purpose = 'LIVE_REVALIDATION' AND outcome = 'FAILED' "
            "AND baseline_ingestion_run_id IS NOT NULL)",
            name="ck_ingestion_runs_valid_combination",
        ),
        sa.CheckConstraint(
            "outcome <> 'FAILED' OR failure_code IS NOT NULL",
            name="ck_ingestion_runs_failed_code",
        ),
    )
    op.create_index(
        "ix_ingestion_runs_dataset_completed",
        "ingestion_runs",
        ["source_dataset_id", "completed_at"],
    )
    op.create_index(
        "ix_ingestion_runs_baseline",
        "ingestion_runs",
        ["baseline_ingestion_run_id"],
    )
    op.create_index(
        "ix_ingestion_runs_dataset_snapshot_id",
        "ingestion_runs",
        ["dataset_snapshot_id"],
    )

    op.create_table(
        "publication_systems",
        sa.Column("publication_system_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("system_key", sa.Text, nullable=False),
        sa.Column("platform", sa.Text, nullable=False),
        sa.Column("binding_version", sa.Integer, nullable=False),
        sa.Column("endpoint_binding_fingerprint", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("system_key", name="uq_publication_systems_system_key"),
        sa.UniqueConstraint(
            "endpoint_binding_fingerprint",
            name="uq_publication_systems_endpoint_binding_fingerprint",
        ),
        sa.CheckConstraint(
            "platform = 'POSTGRESQL'",
            name="ck_publication_systems_platform",
        ),
        sa.CheckConstraint(
            "binding_version = 1",
            name="ck_publication_systems_binding_version",
        ),
        sa.CheckConstraint(
            "system_key ~ '^[a-z][a-z0-9_-]{2,63}$'",
            name="ck_publication_systems_system_key",
        ),
        sa.CheckConstraint(
            "endpoint_binding_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_publication_systems_endpoint_binding_fingerprint",
        ),
    )

    op.create_table(
        "publication_targets",
        sa.Column("publication_target_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("publication_system_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("logical_target", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["publication_system_id"],
            ["publication_systems.publication_system_id"],
            name="fk_publication_targets_publication_system_id",
        ),
        sa.UniqueConstraint(
            "publication_system_id",
            "logical_target",
            name="uq_publication_targets_system_logical_target",
        ),
        sa.CheckConstraint(
            "logical_target ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'",
            name="ck_publication_targets_logical_target",
        ),
    )

    op.add_column(
        "approval_tickets",
        sa.Column("source_ingestion_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_approval_tickets_source_ingestion_run_id",
        "approval_tickets",
        "ingestion_runs",
        ["source_ingestion_run_id"],
        ["ingestion_run_id"],
    )
    op.create_index(
        "ix_approval_tickets_source_ingestion_run_id",
        "approval_tickets",
        ["source_ingestion_run_id"],
    )
    op.alter_column(
        "approval_tickets",
        "target_dataset",
        existing_type=postgresql.JSONB,
        nullable=True,
    )
    op.create_check_constraint(
        "ck_approval_tickets_replay_source",
        "approval_tickets",
        "target_dataset IS NOT NULL OR source_ingestion_run_id IS NOT NULL",
    )
    # NOT VALID preserves historical live-eligible rows without inventing
    # lineage, while PostgreSQL still enforces the check for every new row.
    op.execute(
        "ALTER TABLE approval_tickets "
        "ADD CONSTRAINT ck_approval_tickets_live_eligible_ingestion_lineage "
        "CHECK (NOT live_eligible OR source_ingestion_run_id IS NOT NULL) NOT VALID"
    )
    op.add_column(
        "healing_manifests",
        sa.Column("source_ingestion_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_healing_manifests_source_ingestion_run_id",
        "healing_manifests",
        "ingestion_runs",
        ["source_ingestion_run_id"],
        ["ingestion_run_id"],
    )
    op.create_index(
        "ix_healing_manifests_source_ingestion_run_id",
        "healing_manifests",
        ["source_ingestion_run_id"],
    )

    for column_name in (
        "simulation_ingestion_run_id",
        "revalidation_ingestion_run_id",
    ):
        op.add_column(
            "live_executions",
            sa.Column(column_name, postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_live_executions_{column_name}",
            "live_executions",
            "ingestion_runs",
            [column_name],
            ["ingestion_run_id"],
        )
        op.create_index(
            f"ix_live_executions_{column_name}",
            "live_executions",
            [column_name],
        )

    op.add_column(
        "live_executions",
        sa.Column("publication_target_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_live_executions_publication_target_id",
        "live_executions",
        "publication_targets",
        ["publication_target_id"],
        ["publication_target_id"],
    )
    op.create_index(
        "ix_live_executions_publication_target_id",
        "live_executions",
        ["publication_target_id"],
    )


def downgrade() -> None:
    # Phase 3.2 source tickets may store their replay payload only in
    # dataset_snapshots. Restore that payload before removing lineage
    # and making approval_tickets.target_dataset non-null again.
    op.execute(
        """
        UPDATE approval_tickets AS ticket
        SET target_dataset = snapshot.snapshot_payload
        FROM ingestion_runs AS run
        JOIN dataset_snapshots AS snapshot
          ON snapshot.dataset_snapshot_id = run.dataset_snapshot_id
        WHERE ticket.source_ingestion_run_id = run.ingestion_run_id
          AND ticket.target_dataset IS NULL
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM approval_tickets WHERE target_dataset IS NULL
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade 0005: an approval ticket has no replayable dataset payload';
            END IF;
        END
        $$
        """
    )

    op.drop_index("ix_live_executions_publication_target_id", table_name="live_executions")
    op.drop_constraint(
        "fk_live_executions_publication_target_id",
        "live_executions",
        type_="foreignkey",
    )
    op.drop_column("live_executions", "publication_target_id")

    for column_name in (
        "revalidation_ingestion_run_id",
        "simulation_ingestion_run_id",
    ):
        op.drop_index(f"ix_live_executions_{column_name}", table_name="live_executions")
        op.drop_constraint(
            f"fk_live_executions_{column_name}",
            "live_executions",
            type_="foreignkey",
        )
        op.drop_column("live_executions", column_name)

    op.drop_index(
        "ix_healing_manifests_source_ingestion_run_id",
        table_name="healing_manifests",
    )
    op.drop_constraint(
        "fk_healing_manifests_source_ingestion_run_id",
        "healing_manifests",
        type_="foreignkey",
    )
    op.drop_column("healing_manifests", "source_ingestion_run_id")

    op.drop_constraint(
        "ck_approval_tickets_live_eligible_ingestion_lineage",
        "approval_tickets",
        type_="check",
    )
    op.drop_constraint("ck_approval_tickets_replay_source", "approval_tickets")
    op.alter_column(
        "approval_tickets",
        "target_dataset",
        existing_type=postgresql.JSONB,
        nullable=False,
    )
    op.drop_index(
        "ix_approval_tickets_source_ingestion_run_id",
        table_name="approval_tickets",
    )
    op.drop_constraint(
        "fk_approval_tickets_source_ingestion_run_id",
        "approval_tickets",
        type_="foreignkey",
    )
    op.drop_column("approval_tickets", "source_ingestion_run_id")

    op.drop_table("publication_targets")
    op.drop_table("publication_systems")
    op.drop_index(
        "ix_ingestion_runs_dataset_snapshot_id",
        table_name="ingestion_runs",
    )
    op.drop_index("ix_ingestion_runs_baseline", table_name="ingestion_runs")
    op.drop_index("ix_ingestion_runs_dataset_completed", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
    op.drop_index(
        "ix_dataset_snapshots_dataset_created",
        table_name="dataset_snapshots",
    )
    op.drop_table("dataset_snapshots")
    op.drop_table("source_datasets")
    op.drop_table("source_systems")
