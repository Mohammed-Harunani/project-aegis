"""persist redacted verified conversion decisions

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-30

Phase 3.1.4 stores only redacted summary metadata. Raw values, row labels,
converted series, and diagnostic messages are deliberately excluded.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approval_tickets",
        sa.Column("conversion_decision", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "healing_manifests",
        sa.Column("conversion_outcome", postgresql.JSONB, nullable=True),
    )

    # JSONB is intentionally nullable for RENAME_COLUMN and historical rows.
    # When present, it must be an object; field-level validation remains in the
    # immutable Python model so one serializer defines the exact shape.
    op.create_check_constraint(
        "ck_approval_tickets_conversion_decision_object",
        "approval_tickets",
        "conversion_decision IS NULL OR jsonb_typeof(conversion_decision) = 'object'",
    )
    op.create_check_constraint(
        "ck_healing_manifests_conversion_outcome_object",
        "healing_manifests",
        "conversion_outcome IS NULL OR jsonb_typeof(conversion_outcome) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_healing_manifests_conversion_outcome_object",
        "healing_manifests",
    )
    op.drop_constraint(
        "ck_approval_tickets_conversion_decision_object",
        "approval_tickets",
    )
    op.drop_column("healing_manifests", "conversion_outcome")
    op.drop_column("approval_tickets", "conversion_decision")
