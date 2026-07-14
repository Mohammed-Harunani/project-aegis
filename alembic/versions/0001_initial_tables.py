"""initial tables: approval_tickets, healing_manifests

Revision ID: 0001
Revises:
Create Date: 2026-07-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_tickets",
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("proposed_action", sa.Text, nullable=False),
        sa.Column("confidence", sa.Numeric, nullable=False),
        sa.Column("explanation", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_by", sa.Text, nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text, nullable=True),
        sa.Column("observed_schema", postgresql.JSONB, nullable=False),
        sa.Column("gold_schema", postgresql.JSONB, nullable=False),
        sa.Column("target_dataset", postgresql.JSONB, nullable=False),
    )

    op.create_table(
        "healing_manifests",
        sa.Column("manifest_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approval_tickets.ticket_id"),
            nullable=True,
        ),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("repair_plan", postgresql.JSONB, nullable=False),
        sa.Column("execution_result", postgresql.JSONB, nullable=False),
        sa.Column("execution_mode", sa.Text, nullable=False),
        sa.Column("operator", sa.Text, nullable=False),
        sa.Column("component_versions", postgresql.JSONB, nullable=False),
        sa.Column("original_row_count", sa.Integer, nullable=False),
        sa.Column("final_row_count", sa.Integer, nullable=False),
        sa.Column("integrity_status", sa.Text, nullable=False),
        sa.Column("risk_level", sa.Text, nullable=False),
        sa.UniqueConstraint("ticket_id", name="uq_healing_manifests_ticket_id"),
    )


def downgrade() -> None:
    op.drop_table("healing_manifests")
    op.drop_table("approval_tickets")
