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

from sqlalchemy import Column, Text, Numeric, DateTime, Integer, ForeignKey, UniqueConstraint, Index, text, CheckConstraint, Boolean
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ApprovalTicketRecord(Base):
    __tablename__ = "approval_tickets"
    __table_args__ = (
        # Prevents a malformed or manually altered ticket from being
        # live_eligible=true with no actual source provenance -- the
        # API already enforces this at creation time, but a database-
        # level constraint means it can never be true regardless of
        # how a row got there.
        CheckConstraint(
            "NOT live_eligible OR ("
            "source_schema IS NOT NULL AND source_table IS NOT NULL AND "
            "source_primary_key IS NOT NULL AND source_dataset_fingerprint IS NOT NULL"
            ")",
            name="ck_approval_tickets_live_eligible_requires_provenance",
        ),
    )

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

    # Phase 2.5 final architecture -- trusted-source provenance.
    # NULL for sample_data tickets (/simulate-migration); populated
    # for /simulate-migration-from-source tickets. live_eligible is
    # the enforced gate: a sample_data ticket can be approved (still
    # useful for sandbox analysis) but can never execute live,
    # checked at execute-live time, not just at approval time.
    source_schema = Column(Text, nullable=True)
    source_table = Column(Text, nullable=True)
    source_primary_key = Column(JSONB, nullable=True)
    source_row_count = Column(Integer, nullable=True)
    source_schema_fingerprint = Column(Text, nullable=True)
    source_dataset_fingerprint = Column(Text, nullable=True)
    live_eligible = Column(Boolean, nullable=False, default=False)


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
    # Phase 2.5 correction -- proves a later live-execution
    # recomputation produces the exact same corrected output as what
    # was actually validated here in sandbox, not just the same row
    # count. See src/live_execution/output_fingerprint.py.
    corrected_output_fingerprint = Column(Text, nullable=True)

    # Phase 2.5 final architecture -- mirrors the ticket's own source
    # provenance at manifest-creation time, independent of whatever
    # the ticket looks like later.
    source_schema = Column(Text, nullable=True)
    source_table = Column(Text, nullable=True)
    source_primary_key = Column(JSONB, nullable=True)
    source_row_count = Column(Integer, nullable=True)
    source_schema_fingerprint = Column(Text, nullable=True)
    source_dataset_fingerprint = Column(Text, nullable=True)

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
        # A ticket may have AT MOST ONE non-FAILED live execution ever.
        # FAILED attempts are retryable; anything else (in-flight or
        # executed-in-some-form) is one-shot.
        Index(
            "uq_live_executions_ticket_active_or_done",
            "ticket_id",
            unique=True,
            postgresql_where=text(
                "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'ROLLING_BACK', 'ROLLED_BACK')"
            ),
        ),
        # No two different tickets can have an unresolved (in-flight)
        # live execution against the same logical target at once.
        # Keyed on logical_target alone now -- schemas are fixed
        # (aegis_publish / aegis_publish_data), not caller-supplied, so
        # there's nothing else to disambiguate on. ROLLING_BACK is
        # included -- a target actively being rolled back is still
        # busy.
        Index(
            "uq_live_executions_target_in_flight",
            "logical_target",
            unique=True,
            postgresql_where=text("status IN ('PENDING', 'RUNNING', 'ROLLING_BACK')"),
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'ROLLING_BACK', 'ROLLED_BACK')",
            name="ck_live_executions_valid_status",
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

    # Phase 2.5 final architecture -- replaces target_schema/
    # target_table/backup_table entirely. There is no caller-supplied
    # schema anymore: publication always happens in the fixed
    # aegis_publish (stable views) / aegis_publish_data (immutable
    # physical versions) schemas. logical_target is the single name
    # used for both. physical_table is the NEW immutable version this
    # execution created (e.g. "customer_master__a1b2c3d4"), never
    # renamed or reused. previous_physical_table is whatever the
    # stable view pointed to immediately before this execution, so
    # rollback knows what to repoint to -- rollback never drops or
    # recreates anything, only repoints the view.
    logical_target = Column(Text, nullable=False)
    physical_table = Column(Text, nullable=True)
    previous_physical_table = Column(Text, nullable=True)

    status = Column(Text, nullable=False)
    requested_by = Column(Text, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # Phase 2.5 correction: set when mark_rolling_back() transitions the
    # record, BEFORE the risky target-side operation begins -- this is
    # what lets staleness-based reconciliation of a stuck ROLLING_BACK
    # record work without needing a fresh "who requested this" input
    # (the original HTTP requester may never get a response if the
    # process crashes mid-rollback).
    rollback_started_at = Column(DateTime(timezone=True), nullable=True)
    rolled_back_by = Column(Text, nullable=True)
    rolled_back_at = Column(DateTime(timezone=True), nullable=True)
    failure_reason = Column(Text, nullable=True)
    original_row_count = Column(Integer, nullable=False)
    final_row_count = Column(Integer, nullable=False)
    risk_level = Column(Text, nullable=False)
    integrity_status = Column(Text, nullable=False)

    # Phase 2.5 final architecture -- copied from the ticket at
    # execution time for an independent audit trail, and re-verified
    # (source re-read, fingerprint compared) immediately before
    # publishing -- a source change between approval and execution
    # blocks the execution rather than silently publishing stale data.
    # NOT NULL: every row here is guaranteed to come from a
    # live_eligible ticket (evaluate_safety_gates checks this before
    # create_running() is ever called), so a null here could only mean
    # a bug, never a legitimate case.
    source_schema = Column(Text, nullable=False)
    source_table = Column(Text, nullable=False)
    source_dataset_fingerprint = Column(Text, nullable=False)
