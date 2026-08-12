"""
Aegis_DB_Models
Phase 2.3 -- SQLAlchemy ORM models for durable approval and manifest
storage. Later phases add the schema registry, live execution,
verified conversion evidence, and Phase 3.2's source-ingestion and
dataset-identity lineage.

Postgres-specific types (UUID, JSONB) are used deliberately -- this
targets PostgreSQL only, not a portable/SQLite-compatible schema.
That was an explicit decision, not an oversight: it also means these
models cannot be exercised against SQLite as a stand-in for testing.
"""

import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class SourceSystemRecord(Base):
    """Immutable server-controlled identity for one trusted source binding."""

    __tablename__ = "source_systems"
    __table_args__ = (
        UniqueConstraint("system_key", name="uq_source_systems_system_key"),
        UniqueConstraint(
            "endpoint_binding_fingerprint",
            name="uq_source_systems_endpoint_binding_fingerprint",
        ),
        CheckConstraint("platform = 'POSTGRESQL'", name="ck_source_systems_platform"),
        CheckConstraint("binding_version = 1", name="ck_source_systems_binding_version"),
        CheckConstraint(
            "system_key ~ '^[a-z][a-z0-9_-]{2,63}$'",
            name="ck_source_systems_system_key",
        ),
        CheckConstraint(
            "endpoint_binding_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_source_systems_endpoint_binding_fingerprint",
        ),
    )

    source_system_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    system_key = Column(Text, nullable=False)
    platform = Column(Text, nullable=False)
    binding_version = Column(Integer, nullable=False)
    endpoint_binding_fingerprint = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class SourceDatasetRecord(Base):
    """Stable identity for one source-system/schema/table combination."""

    __tablename__ = "source_datasets"
    __table_args__ = (
        UniqueConstraint(
            "source_system_id",
            "source_schema",
            "source_table",
            name="uq_source_datasets_system_relation",
        ),
        CheckConstraint(
            "source_schema ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'",
            name="ck_source_datasets_source_schema",
        ),
        CheckConstraint(
            "source_table ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'",
            name="ck_source_datasets_source_table",
        ),
    )

    source_dataset_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_system_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "source_systems.source_system_id",
            name="fk_source_datasets_source_system_id",
        ),
        nullable=False,
    )
    source_schema = Column(Text, nullable=False)
    source_table = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DatasetSnapshotRecord(Base):
    """Dataset-scoped, deduplicated, replayable complete source snapshot."""

    __tablename__ = "dataset_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source_dataset_id",
            "provenance_fingerprint",
            name="uq_dataset_snapshots_dataset_provenance",
        ),
        CheckConstraint(
            "provenance_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_dataset_snapshots_provenance_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(source_primary_key) = 'array' "
            "AND jsonb_array_length(source_primary_key) > 0",
            name="ck_dataset_snapshots_primary_key_array",
        ),
        CheckConstraint("source_row_count >= 0", name="ck_dataset_snapshots_row_count"),
        CheckConstraint(
            "source_schema_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_dataset_snapshots_schema_fingerprint",
        ),
        CheckConstraint(
            "source_dataset_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_dataset_snapshots_dataset_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(column_metadata) = 'array' "
            "AND jsonb_array_length(column_metadata) > 0",
            name="ck_dataset_snapshots_column_metadata_array",
        ),
        CheckConstraint(
            "payload_format_version > 0",
            name="ck_dataset_snapshots_payload_format_version",
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot_payload) = 'object'",
            name="ck_dataset_snapshots_payload_object",
        ),
        Index("ix_dataset_snapshots_dataset_created", "source_dataset_id", "created_at"),
    )

    dataset_snapshot_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_dataset_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "source_datasets.source_dataset_id",
            name="fk_dataset_snapshots_source_dataset_id",
        ),
        nullable=False,
    )
    provenance_fingerprint = Column(Text, nullable=False)
    source_primary_key = Column(JSONB, nullable=False)
    source_row_count = Column(Integer, nullable=False)
    source_schema_fingerprint = Column(Text, nullable=False)
    source_dataset_fingerprint = Column(Text, nullable=False)
    column_metadata = Column(JSONB, nullable=False)
    payload_format_version = Column(Integer, nullable=False)
    snapshot_payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class IngestionRunRecord(Base):
    """Append-only terminal record of one trusted-source observation."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('SIMULATION', 'LIVE_REVALIDATION')",
            name="ck_ingestion_runs_purpose",
        ),
        CheckConstraint(
            "outcome IN ('CAPTURED', 'MATCHED', 'DRIFTED', 'FAILED')",
            name="ck_ingestion_runs_outcome",
        ),
        CheckConstraint(
            "completed_at >= started_at",
            name="ck_ingestion_runs_timestamp_order",
        ),
        CheckConstraint(
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
        CheckConstraint(
            "outcome <> 'FAILED' OR failure_code IS NOT NULL",
            name="ck_ingestion_runs_failed_code",
        ),
        Index("ix_ingestion_runs_dataset_completed", "source_dataset_id", "completed_at"),
        Index("ix_ingestion_runs_baseline", "baseline_ingestion_run_id"),
    )

    ingestion_run_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_dataset_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "source_datasets.source_dataset_id",
            name="fk_ingestion_runs_source_dataset_id",
        ),
        nullable=False,
    )
    dataset_snapshot_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "dataset_snapshots.dataset_snapshot_id",
            name="fk_ingestion_runs_dataset_snapshot_id",
        ),
        nullable=True,
        index=True,
    )
    purpose = Column(Text, nullable=False)
    outcome = Column(Text, nullable=False)
    baseline_ingestion_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "ingestion_runs.ingestion_run_id",
            name="fk_ingestion_runs_baseline_ingestion_run_id",
        ),
        nullable=True,
    )
    requested_by = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=False)
    failure_code = Column(Text, nullable=True)
    failure_reason = Column(Text, nullable=True)


class PublicationSystemRecord(Base):
    """Immutable server-controlled identity for one publication binding."""

    __tablename__ = "publication_systems"
    __table_args__ = (
        UniqueConstraint("system_key", name="uq_publication_systems_system_key"),
        UniqueConstraint(
            "endpoint_binding_fingerprint",
            name="uq_publication_systems_endpoint_binding_fingerprint",
        ),
        CheckConstraint("platform = 'POSTGRESQL'", name="ck_publication_systems_platform"),
        CheckConstraint("binding_version = 1", name="ck_publication_systems_binding_version"),
        CheckConstraint(
            "system_key ~ '^[a-z][a-z0-9_-]{2,63}$'",
            name="ck_publication_systems_system_key",
        ),
        CheckConstraint(
            "endpoint_binding_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_publication_systems_endpoint_binding_fingerprint",
        ),
    )

    publication_system_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    system_key = Column(Text, nullable=False)
    platform = Column(Text, nullable=False)
    binding_version = Column(Integer, nullable=False)
    endpoint_binding_fingerprint = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class PublicationTargetRecord(Base):
    """Stable identity for one logical target within a publication system."""

    __tablename__ = "publication_targets"
    __table_args__ = (
        UniqueConstraint(
            "publication_system_id",
            "logical_target",
            name="uq_publication_targets_system_logical_target",
        ),
        CheckConstraint(
            "logical_target ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'",
            name="ck_publication_targets_logical_target",
        ),
    )

    publication_target_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publication_system_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "publication_systems.publication_system_id",
            name="fk_publication_targets_publication_system_id",
        ),
        nullable=False,
    )
    logical_target = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


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
            "source_primary_key IS NOT NULL AND jsonb_array_length(source_primary_key) > 0 AND "
            "source_row_count IS NOT NULL AND source_schema_fingerprint IS NOT NULL AND "
            "source_dataset_fingerprint IS NOT NULL"
            ")",
            name="ck_approval_tickets_live_eligible_requires_provenance",
        ),
        CheckConstraint(
            "conversion_decision IS NULL OR jsonb_typeof(conversion_decision) = 'object'",
            name="ck_approval_tickets_conversion_decision_object",
        ),
        CheckConstraint(
            "target_dataset IS NOT NULL OR source_ingestion_run_id IS NOT NULL",
            name="ck_approval_tickets_replay_source",
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
    # none_as_null=True is required for new snapshot-backed tickets: Python
    # None must become SQL NULL, not a JSONB literal null that would satisfy
    # `target_dataset IS NOT NULL` while containing no replayable payload.
    target_dataset = Column(JSONB(none_as_null=True), nullable=True)

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

    # Phase 3.2 -- nullable for historical and sample-data tickets.
    # New source-backed tickets will use this lineage and load their
    # replayable DataFrame from the linked immutable snapshot.
    source_ingestion_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "ingestion_runs.ingestion_run_id",
            name="fk_approval_tickets_source_ingestion_run_id",
        ),
        nullable=True,
        index=True,
    )

    # Phase 3.1.4 -- redacted verified CAST_COLUMN preflight decision.
    # None for RENAME_COLUMN tickets and all pre-Phase-3.1.4 rows.
    conversion_decision = Column(JSONB(none_as_null=True), nullable=True)


class HealingManifestRecord(Base):
    __tablename__ = "healing_manifests"
    __table_args__ = (
        # Postgres treats NULL != NULL, so this permits unlimited
        # NULL ticket_id rows (auto-approved executions) while still
        # blocking a second manifest for the same manually-approved
        # ticket -- defense-in-depth alongside the row lock in
        # approval_repository.py's approve().
        UniqueConstraint("ticket_id", name="uq_healing_manifests_ticket_id"),
        CheckConstraint(
            "conversion_outcome IS NULL OR jsonb_typeof(conversion_outcome) = 'object'",
            name="ck_healing_manifests_conversion_outcome_object",
        ),
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

    # Phase 3.1.4 -- persisted redacted CAST_COLUMN execution outcome.
    # The corrected DataFrame itself remains ephemeral and is never stored.
    conversion_outcome = Column(JSONB(none_as_null=True), nullable=True)

    # Phase 3.2 -- exact simulation observation used by the ticket.
    # Nullable for historical, auto-approved, and sample-data rows.
    source_ingestion_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "ingestion_runs.ingestion_run_id",
            name="fk_healing_manifests_source_ingestion_run_id",
        ),
        nullable=True,
        index=True,
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
    #
    # Originally only source_schema/source_table/source_dataset_fingerprint
    # were copied here -- source_primary_key/source_row_count/
    # source_schema_fingerprint were recoverable via the ticket_id
    # foreign key, but the design intent was for this record to stand
    # on its own as a complete, independent audit trail, not one that
    # requires following a join to fully account for what was
    # revalidated immediately before publication.
    source_schema = Column(Text, nullable=False)
    source_table = Column(Text, nullable=False)
    source_primary_key = Column(JSONB, nullable=False)
    source_row_count = Column(Integer, nullable=False)
    source_schema_fingerprint = Column(Text, nullable=False)
    source_dataset_fingerprint = Column(Text, nullable=False)

    # Phase 3.2 lineage. Nullable at the schema level for historical
    # rows and, for revalidation, the short RUNNING interval before
    # the fresh observation becomes terminal and durable.
    simulation_ingestion_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "ingestion_runs.ingestion_run_id",
            name="fk_live_executions_simulation_ingestion_run_id",
        ),
        nullable=True,
        index=True,
    )
    revalidation_ingestion_run_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "ingestion_runs.ingestion_run_id",
            name="fk_live_executions_revalidation_ingestion_run_id",
        ),
        nullable=True,
        index=True,
    )
    publication_target_id = Column(
        UUID(as_uuid=True),
        ForeignKey(
            "publication_targets.publication_target_id",
            name="fk_live_executions_publication_target_id",
        ),
        nullable=True,
        index=True,
    )
