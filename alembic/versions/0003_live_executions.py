"""live_executions: governance audit table for Phase 2.5, final
stable-view / trusted-source architecture

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-15
Amended: 2026-07-20 -- replaced the rename-to-backup design with
immutable versioned physical tables behind stable views, and added
trusted-source provenance to approval_tickets/healing_manifests.
Amended directly rather than as migration 0004 since this has not
been applied to a live database yet.

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOTE: aegis_publish / aegis_publish_data are NOT created here.
    # This migration runs against DATABASE_URL (the governance
    # database) -- but those two schemas belong in LIVE_DATABASE_URL
    # (the actual publication target), a different database entirely.
    # Creating them here would put two permanently-empty, unused
    # schemas in governance while the real ones the writer actually
    # publishes into live somewhere else. PostgresPublicationWriter's
    # own _ensure_publish_schemas_and_log() already creates both,
    # idempotently (CREATE SCHEMA IF NOT EXISTS), in the correct
    # database, the first time anything is ever published -- that is
    # the only place these schemas are initialized.

    # Trusted-source provenance on approval_tickets -- NULL for
    # sample_data tickets, populated for /simulate-migration-from-source
    # tickets. live_eligible is the enforced gate.
    op.add_column("approval_tickets", sa.Column("source_schema", sa.Text, nullable=True))
    op.add_column("approval_tickets", sa.Column("source_table", sa.Text, nullable=True))
    op.add_column(
        "approval_tickets",
        sa.Column("source_primary_key", postgresql.JSONB, nullable=True),
    )
    op.add_column("approval_tickets", sa.Column("source_row_count", sa.Integer, nullable=True))
    op.add_column(
        "approval_tickets", sa.Column("source_schema_fingerprint", sa.Text, nullable=True)
    )
    op.add_column(
        "approval_tickets", sa.Column("source_dataset_fingerprint", sa.Text, nullable=True)
    )
    op.add_column(
        "approval_tickets",
        sa.Column("live_eligible", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    # Prevents a malformed or manually altered ticket from being
    # live_eligible=true with no actual source provenance -- a
    # database-level guarantee, not just an API-layer one.
    op.create_check_constraint(
        "ck_approval_tickets_live_eligible_requires_provenance",
        "approval_tickets",
        "NOT live_eligible OR ("
        "source_schema IS NOT NULL AND source_table IS NOT NULL AND "
        "source_primary_key IS NOT NULL AND source_dataset_fingerprint IS NOT NULL"
        ")",
    )

    # Phase 2.5 correction -- proves a later live-execution
    # recomputation produces the exact same corrected output as what
    # was actually validated in sandbox, not just the same row count.
    op.add_column(
        "healing_manifests",
        sa.Column("corrected_output_fingerprint", sa.Text, nullable=True),
    )
    # Mirrors the ticket's own source provenance at manifest-creation
    # time.
    op.add_column("healing_manifests", sa.Column("source_schema", sa.Text, nullable=True))
    op.add_column("healing_manifests", sa.Column("source_table", sa.Text, nullable=True))
    op.add_column(
        "healing_manifests",
        sa.Column("source_primary_key", postgresql.JSONB, nullable=True),
    )
    op.add_column("healing_manifests", sa.Column("source_row_count", sa.Integer, nullable=True))
    op.add_column(
        "healing_manifests", sa.Column("source_schema_fingerprint", sa.Text, nullable=True)
    )
    op.add_column(
        "healing_manifests", sa.Column("source_dataset_fingerprint", sa.Text, nullable=True)
    )

    op.create_table(
        "live_executions",
        sa.Column("live_execution_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_tickets.ticket_id"),
            nullable=False,
        ),
        sa.Column(
            "sandbox_manifest_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("healing_manifests.manifest_id"),
            nullable=False,
        ),
        sa.Column(
            "schema_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("schema_versions.schema_version_id"),
            nullable=False,
        ),
        # Replaces target_schema/target_table/backup_table entirely --
        # publication always happens in the fixed aegis_publish /
        # aegis_publish_data schemas now, so there's no caller-supplied
        # schema to store. physical_table is the new immutable version
        # this execution created; previous_physical_table is what the
        # stable view pointed to immediately before, so rollback knows
        # what to repoint to.
        sa.Column("logical_target", sa.Text, nullable=False),
        sa.Column("physical_table", sa.Text, nullable=True),
        sa.Column("previous_physical_table", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("requested_by", sa.Text, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_by", sa.Text, nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("original_row_count", sa.Integer, nullable=False),
        sa.Column("final_row_count", sa.Integer, nullable=False),
        sa.Column("risk_level", sa.Text, nullable=False),
        sa.Column("integrity_status", sa.Text, nullable=False),
        # Copied from the ticket at execution time for an independent
        # audit trail, and re-verified (source re-read, fingerprint
        # compared) immediately before publishing.
        sa.Column("source_schema", sa.Text, nullable=False),
        sa.Column("source_table", sa.Text, nullable=False),
        sa.Column("source_dataset_fingerprint", sa.Text, nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'ROLLING_BACK', 'ROLLED_BACK')",
            name="ck_live_executions_valid_status",
        ),
    )

    # A ticket may have AT MOST ONE non-FAILED live execution ever --
    # FAILED attempts are retryable; anything else (in-flight or
    # executed-in-some-form) is one-shot.
    op.create_index(
        "uq_live_executions_ticket_active_or_done",
        "live_executions",
        ["ticket_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'ROLLING_BACK', 'ROLLED_BACK')"
        ),
    )
    # No two different tickets can have an unresolved live execution
    # against the same logical target at once. Keyed on logical_target
    # alone now -- schemas are fixed, not caller-supplied. ROLLING_BACK
    # is included: a target actively being rolled back is still busy.
    op.create_index(
        "uq_live_executions_target_in_flight",
        "live_executions",
        ["logical_target"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING', 'ROLLING_BACK')"),
    )


def downgrade() -> None:
    op.drop_index("uq_live_executions_target_in_flight", table_name="live_executions")
    op.drop_index("uq_live_executions_ticket_active_or_done", table_name="live_executions")
    op.drop_table("live_executions")

    op.drop_column("healing_manifests", "source_dataset_fingerprint")
    op.drop_column("healing_manifests", "source_schema_fingerprint")
    op.drop_column("healing_manifests", "source_row_count")
    op.drop_column("healing_manifests", "source_primary_key")
    op.drop_column("healing_manifests", "source_table")
    op.drop_column("healing_manifests", "source_schema")
    op.drop_column("healing_manifests", "corrected_output_fingerprint")

    op.drop_constraint("ck_approval_tickets_live_eligible_requires_provenance", "approval_tickets")
    op.drop_column("approval_tickets", "live_eligible")
    op.drop_column("approval_tickets", "source_dataset_fingerprint")
    op.drop_column("approval_tickets", "source_schema_fingerprint")
    op.drop_column("approval_tickets", "source_row_count")
    op.drop_column("approval_tickets", "source_primary_key")
    op.drop_column("approval_tickets", "source_table")
    op.drop_column("approval_tickets", "source_schema")
    # No DROP SCHEMA here -- upgrade() never creates aegis_publish /
    # aegis_publish_data in this (governance) database; see the note
    # in upgrade().
