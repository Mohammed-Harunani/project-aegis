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
        # Only one successful live execution per ticket -- partial
        # unique index so PENDING/RUNNING/FAILED/ROLLED_BACK attempts
        # for the same ticket don't collide with each other, only a
        # second COMPLETED one would.
    )
    op.create_index(
        "uq_live_executions_ticket_completed",
        "live_executions",
        ["ticket_id"],
        unique=True,
        postgresql_where=sa.text("status = 'COMPLETED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_live_executions_ticket_completed", table_name="live_executions")
    op.drop_table("live_executions")
