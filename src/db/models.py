"""
Aegis_DB_Models
Phase 2.3 -- SQLAlchemy ORM models for durable approval and manifest
storage.

Postgres-specific types (UUID, JSONB) are used deliberately -- Phase
2.3 targets PostgreSQL only, not a portable/SQLite-compatible schema.
That was an explicit decision, not an oversight: it also means these
models cannot be exercised against SQLite as a stand-in for testing.
"""

import uuid

from sqlalchemy import Column, Text, Numeric, DateTime, Integer, ForeignKey, UniqueConstraint
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
