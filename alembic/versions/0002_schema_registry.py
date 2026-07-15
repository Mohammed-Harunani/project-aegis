"""schema registry: gold_schemas, schema_versions, lineage FKs

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-15

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gold_schemas",
        sa.Column("schema_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
    )

    op.create_table(
        "schema_versions",
        sa.Column("schema_version_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "schema_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gold_schemas.schema_id"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("schema_definition", postgresql.JSONB, nullable=False),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("change_summary", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text, nullable=False),
        sa.UniqueConstraint(
            "schema_id", "version_number", name="uq_schema_versions_schema_version_number"
        ),
        sa.UniqueConstraint(
            "schema_id", "fingerprint", name="uq_schema_versions_schema_fingerprint"
        ),
    )

    # Nullable lineage FKs on the two existing tables -- nullable so
    # existing rows and legacy direct-schema requests are unaffected.
    op.add_column(
        "approval_tickets",
        sa.Column(
            "schema_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("schema_versions.schema_version_id"),
            nullable=True,
        ),
    )
    op.add_column(
        "healing_manifests",
        sa.Column(
            "schema_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("schema_versions.schema_version_id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("healing_manifests", "schema_version_id")
    op.drop_column("approval_tickets", "schema_version_id")
    op.drop_table("schema_versions")
    op.drop_table("gold_schemas")
