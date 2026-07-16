"""live_executions: governance audit table for Phase 2.5

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
        sa.Column("target_schema", sa.Text, nullable=False),
        sa.Column("target_table", sa.Text, nullable=False),
        sa.Column("backup_table", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("requested_by", sa.Text, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_by", sa.Text, nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("original_row_count", sa.Integer, nullable=False),
        sa.Column("final_row_count", sa.Integer, nullable=False),
        sa.Column("risk_level", sa.Text, nullable=False),
        sa.Column("integrity_status", sa.Text, nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'ROLLING_BACK', 'ROLLED_BACK')",
            name="ck_live_executions_valid_status",
        ),
    )

    # A ticket may have AT MOST ONE non-FAILED live execution ever --
    # FAILED attempts are retryable; anything else (in-flight or
    # executed-in-some-form) is one-shot. Originally this covered only
    # COMPLETED, which let a rolled-back ticket execute live again.
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
    # against the same target table at once -- this is what actually
    # stops a second concurrent promote() at the database level,
    # rather than relying solely on an application-level check.
    op.create_index(
        "uq_live_executions_target_in_flight",
        "live_executions",
        ["target_schema", "target_table"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"),
    )

    # Phase 2.5 correction -- proves a later live-execution
    # recomputation produces the exact same corrected output as what
    # was actually validated in sandbox, not just the same row count.
    op.add_column(
        "healing_manifests",
        sa.Column("corrected_output_fingerprint", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("healing_manifests", "corrected_output_fingerprint")
    op.drop_index("uq_live_executions_target_in_flight", table_name="live_executions")
    op.drop_index("uq_live_executions_ticket_active_or_done", table_name="live_executions")
    op.drop_table("live_executions")
