"""
Aegis_DB_Models
Phase 2.3 -- SQLAlchemy ORM models for durable approval and manifest
storage. Phase 2.4 adds the schema registry (gold_schemas,
schema_versions) and nullable lineage FKs on the two existing tables.

Postgres-specific types (UUID, JSONB) are used deliberately -- this
targets PostgreSQL only, not a portable/SQLite-compatible schema.
That was an explicit decision, not an oversight: it also means these
models cannot be exercised against SQLite as a stand-in for testing.
"""

import uuid

from sqlalchemy import Column, Text, Numeric, DateTime, Integer, ForeignKey, UniqueConstraint, Index, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ApprovalTicketRecord(Base):
    __tablename__ = "approval_tickets"

    ticket_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposed_action = Column(Text, nullable=False)
    confidence = Column(Numeric, nullable=False)
    explanation = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    decided_by = Column(Text, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_note = Column(Text, nullable=True)

    # Stored as structured JSONB, never pickled/repr'd Python objects.
    observed_schema = Column(JSONB, nullable=False)
    gold_schema = Column(JSONB, nullable=False)
    target_dataset = Column(JSONB, nullable=False)

    # Phase 2.4 -- nullable so existing rows and legacy direct-schema
    # requests (which never reference the registry) are unaffected.
    # The gold_schema JSONB snapshot above is kept regardless: this FK
    # establishes lineage, the snapshot preserves the exact execution
    # input for deterministic replay independent of the registry.
    schema_version_id = Column(
        UUID(as_uuid=True), ForeignKey("schema_versions.schema_version_id"), nullable=True
    )


class HealingManifestRecord(Base):
    __tablename__ = "healing_manifests"
    __table_args__ = (
        # Postgres treats NULL != NULL, so this permits unlimited
        # NULL ticket_id rows (auto-approved executions) while still
        # blocking a second manifest for the same manually-approved
        # ticket -- defense-in-depth alongside the row lock in
        # approval_repository.py's approve().
        UniqueConstraint("ticket_id", name="uq_healing_manifests_ticket_id"),
    )

    manifest_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Nullable: an AUTO_APPROVE execution never creates an approval
    # ticket, so it has nothing to link back to here.
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("approval_tickets.ticket_id"), nullable=True)

    timestamp = Column(DateTime(timezone=True), nullable=False)
    repair_plan = Column(JSONB, nullable=False)
    execution_result = Column(JSONB, nullable=False)
    execution_mode = Column(Text, nullable=False)
    operator = Column(Text, nullable=False)
    component_versions = Column(JSONB, nullable=False)
    original_row_count = Column(Integer, nullable=False)
    final_row_count = Column(Integer, nullable=False)
    integrity_status = Column(Text, nullable=False)
    risk_level = Column(Text, nullable=False)

    # Phase 2.4 -- nullable independently of ticket_id: an
    # auto-approved execution has no ticket AT ALL, but may still have
    # been validated against a registry schema version.
    schema_version_id = Column(
        UUID(as_uuid=True), ForeignKey("schema_versions.schema_version_id"), nullable=True
    )


class GoldSchemaRecord(Base):
    __tablename__ = "gold_schemas"

    schema_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Text, unique=True, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    created_by = Column(Text, nullable=False)


class SchemaVersionRecord(Base):
    __tablename__ = "schema_versions"
    __table_args__ = (
        UniqueConstraint("schema_id", "version_number", name="uq_schema_versions_schema_version_number"),
        UniqueConstraint("schema_id", "fingerprint", name="uq_schema_versions_schema_fingerprint"),
    )

    schema_version_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    schema_id = Column(UUID(as_uuid=True), ForeignKey("gold_schemas.schema_id"), nullable=False)
    version_number = Column(Integer, nullable=False)
    schema_definition = Column(JSONB, nullable=False)
    fingerprint = Column(Text, nullable=False)
    change_summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    created_by = Column(Text, nullable=False)


class LiveExecutionRecord(Base):
    __tablename__ = "live_executions"
    __table_args__ = (
        # Only one COMPLETED execution per ticket -- a failed attempt
        # doesn't block a later retry, only a second success would.
        Index(
            "uq_live_executions_ticket_completed",
            "ticket_id",
            unique=True,
            postgresql_where=text("status = 'COMPLETED'"),
        ),
    )

    live_execution_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_id = Column(UUID(as_uuid=True), ForeignKey("approval_tickets.ticket_id"), nullable=False)
    sandbox_manifest_id = Column(
        UUID(as_uuid=True), ForeignKey("healing_manifests.manifest_id"), nullable=False
    )
    schema_version_id = Column(
        UUID(as_uuid=True), ForeignKey("schema_versions.schema_version_id"), nullable=False
    )
    target_schema = Column(Text, nullable=False)
    target_table = Column(Text, nullable=False)
    backup_table = Column(Text, nullable=True)
    status = Column(Text, nullable=False)
    requested_by = Column(Text, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    rolled_back_by = Column(Text, nullable=True)
    rolled_back_at = Column(DateTime(timezone=True), nullable=True)
    failure_reason = Column(Text, nullable=True)
    original_row_count = Column(Integer, nullable=False)
    final_row_count = Column(Integer, nullable=False)
    risk_level = Column(Text, nullable=False)
    integrity_status = Column(Text, nullable=False)
