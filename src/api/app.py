from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator, field_validator
from typing import Dict, List, Optional
from sqlalchemy.orm import Session
from sqlalchemy.exc import DBAPIError
from datetime import UTC, datetime
import os
import uuid
import pandas as pd

from src.inspector import AegisInspector, ObservedSchema, ColumnStats
from src.consultant import AegisConsultant
from src.surgeon import AegisSurgeon
from src.governance.policy import GovernancePolicy
from src.governance.selector import RepairSelector
from src.governance.approval import TicketNotFoundError, TicketNotPendingError
from src.governance.approval_repository import (
    ApprovalReplayError,
    PostgresApprovalRepository,
)
from src.governance.manifest_repository import save_manifest
from src.governance.conversion_safety import (
    ConversionApprovalBlockedError,
    ConversionGovernanceError,
    StaleConversionDecisionError,
    analyze_cast_plan,
    is_cast_action,
    require_safe_conversion_decision,
)
from src.db.session import get_db
from src.db.models import HealingManifestRecord
from src.governance.manifest import ConversionOutcomeMetadata
from src.db.live_session import get_live_engine
from src.db.source_session import get_source_engine
from src.registry.schema_definition import (
    InvalidSchemaDefinitionError,
    validate_and_normalize_created_by,
    to_gold_schema_dict,
)
from src.registry.repository import (
    SchemaRegistryRepository,
    SchemaNotFoundError,
    SchemaVersionNotFoundError,
    DuplicateSchemaDefinitionError,
)
from src.live_execution.safety import (
    LiveExecutionNotAllowedError,
    LiveExecutionConflictError,
    live_execution_globally_enabled,
    validate_and_normalize_operator,
    verify_output_fingerprint_match,
    verify_complete_schema_match,
    evaluate_safety_gates,
)
from src.live_execution.repository import (
    LiveExecutionRepository,
    LiveExecutionNotFoundError,
    LiveExecutionInvalidStateError,
)
from src.live_execution.writer import (
    PostgresPublicationWriter,
    LiveWriteValidationError,
    IncompatibleViewSchemaError,
    StaleRollbackError,
    TargetLockUnavailableError,
)
from src.live_execution.output_fingerprint import compute_dataframe_fingerprint
from src.live_execution.source_connector import (
    read_complete_source_table,
    verify_source_unchanged,
    build_source_observed_schema,
    postgres_type_to_aegis_dtype,
    SourceValidationError,
    SourceChangedError,
    UnsupportedSourceTypeError,
)
from src.live_execution.logical_dtype import (
    is_publishable_dtype,
    get_publishable_dtypes,
)
from src.live_execution.cast_allowlist import (
    LiveCastAllowlistError,
    configured_live_cast_allowlist,
    require_live_cast_pair_allowed,
)
from src.identity.binding import (
    IdentityConfigurationError,
    UnsafeDatabaseTopologyError,
    configured_binding,
    ensure_distinct_bindings,
)
from src.identity.repository import (
    EndpointBindingAlreadyRegisteredError,
    IdentityNotFoundError,
    IdentityRepository,
    SystemBindingConflictError,
)
from src.ingestion.repository import (
    IngestionRepository,
    IngestionRepositoryError,
)
from src.ingestion.service import IngestionService
from src.live_execution.identifiers import InvalidIdentifierError


app = FastAPI(
    title="Aegis Financial Data Integrity Guardian",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SchemaColumnDef(BaseModel):
    name: str
    dtype: str


class MigrationRequest(BaseModel):
    # Exactly one of these two must be provided -- see
    # _exactly_one_schema_source below. gold_schema is the original
    # Phase 2.1-2.3 direct payload (kept for backward compatibility);
    # schema_name (+ optional schema_version) is the Phase 2.4
    # registry-backed path. New usage should prefer the latter.
    gold_schema: Optional[Dict[str, str]] = None
    schema_name: Optional[str] = None
    schema_version: Optional[int] = Field(default=None, gt=0)
    sample_data: Dict[str, list]

    @model_validator(mode="after")
    def _exactly_one_schema_source(self):
        has_gold_schema = self.gold_schema is not None
        has_schema_name = self.schema_name is not None
        if has_gold_schema and has_schema_name:
            raise ValueError("Provide either gold_schema or schema_name, never both.")
        if not has_gold_schema and not has_schema_name:
            raise ValueError("Provide either gold_schema or schema_name.")
        if self.schema_version is not None and not has_schema_name:
            raise ValueError("schema_version requires schema_name -- it has no meaning without it.")
        return self


class SimulateFromSourceRequest(BaseModel):
    """
    The live-capable simulation path -- reads a COMPLETE, trusted
    dataset from a real PostgreSQL source table, unlike
    MigrationRequest's caller-supplied sample_data. Tickets from this
    endpoint are live_eligible; tickets from /simulate-migration never
    are.
    """
    schema_name: str
    schema_version: Optional[int] = Field(default=None, gt=0)
    source_schema: str
    source_table: str


class ApprovalDecisionRequest(BaseModel):
    operator: str
    note: str = ""

    @field_validator("operator")
    @classmethod
    def _operator_must_be_meaningful(cls, value: str) -> str:
        try:
            return validate_and_normalize_operator(value)
        except LiveExecutionNotAllowedError as e:
            raise ValueError(str(e))


class ExecuteLiveRequest(BaseModel):
    operator: str
    logical_target: str
    # Explicit "yes I mean it" -- gate 14. No default of True; the
    # caller must say so.
    confirm: bool = False

    # Deliberately no source_schema/source_table, and no
    # target_schema/target_table -- source identity comes only from
    # the approved ticket's own persisted provenance now, and
    # publication always happens in the fixed aegis_publish /
    # aegis_publish_data schemas. A caller-supplied source was never
    # verified against anything real under the old design; a
    # caller-supplied target schema was pure trust. Neither is
    # accepted here anymore.

    @field_validator("operator")
    @classmethod
    def _operator_must_be_meaningful(cls, value: str) -> str:
        try:
            return validate_and_normalize_operator(value)
        except LiveExecutionNotAllowedError as e:
            raise ValueError(str(e))


class LiveRollbackRequest(BaseModel):
    operator: str

    @field_validator("operator")
    @classmethod
    def _operator_must_be_meaningful(cls, value: str) -> str:
        try:
            return validate_and_normalize_operator(value)
        except LiveExecutionNotAllowedError as e:
            raise ValueError(str(e))


class RegisterSchemaVersionRequest(BaseModel):
    format_version: int = 1
    columns: List[SchemaColumnDef]
    created_by: str = Field(min_length=1)
    description: Optional[str] = None
    change_summary: Optional[str] = None

    @field_validator("created_by")
    @classmethod
    def _created_by_must_be_meaningful(cls, value: str) -> str:
        # Field(min_length=1) alone lets "   " through -- it's a plain
        # character-count check, not a meaningful-content check.
        # Reuses the same strip-and-check logic the repository applies
        # independently, so "  mohammed  " is stored as "mohammed" and
        # "   " is rejected here rather than reaching the database.
        try:
            return validate_and_normalize_created_by(value)
        except InvalidSchemaDefinitionError as e:
            raise ValueError(str(e))


def _build_gold_schema(gold_schema: Dict[str, str]) -> ObservedSchema:
    """
    Builds the Gold ObservedSchema directly from a flat name -> dtype
    mapping -- whether that mapping came from the legacy direct
    gold_schema payload or was converted from a registry schema
    version's ordered columns array (see to_gold_schema_dict()). Does
    NOT go through Inspector.generate_observed_schema() on an empty
    DataFrame -- see Phase 2.2 notes: that silently discarded every
    declared dtype. No real data backs a schema *definition*, so
    null_count and unique_count are 0 rather than inferred from
    anything.
    """
    columns = {
        name: ColumnStats(null_count=0, unique_count=0, dtype=dtype)
        for name, dtype in gold_schema.items()
    }
    return ObservedSchema(columns=columns, column_order=list(gold_schema.keys()))


def _public_conversion_metadata(metadata):
    """Expose only the reviewed redacted conversion metadata shape."""
    return metadata.to_dict() if metadata is not None else None


def _configured_source_binding():
    return configured_binding(
        key_env="AEGIS_SOURCE_SYSTEM_KEY",
        url_env="SOURCE_DATABASE_URL",
    )


def _verify_optional_source_publication_separation(source_binding) -> None:
    # Publication configuration stays lazy: source-only simulation does not
    # require it, but when both server-controlled values exist the unsafe
    # same-database topology is rejected before source data is captured.
    if (
        os.environ.get("AEGIS_PUBLICATION_SYSTEM_KEY") is not None
        and os.environ.get("LIVE_DATABASE_URL") is not None
    ):
        _, publication_binding = configured_binding(
            key_env="AEGIS_PUBLICATION_SYSTEM_KEY",
            url_env="LIVE_DATABASE_URL",
        )
        ensure_distinct_bindings(source_binding, publication_binding)


def _simulation_lineage(source_system, source_dataset, captured) -> dict:
    return {
        "source_system_id": str(source_system.source_system_id),
        "source_dataset_id": str(source_dataset.source_dataset_id),
        "dataset_snapshot_id": str(captured.snapshot.dataset_snapshot_id),
        "ingestion_run_id": str(captured.run.ingestion_run_id),
    }


def _public_ingestion_run(db: Session, run) -> dict:
    snapshot = None
    if run.dataset_snapshot_id is not None:
        snapshot = IngestionRepository(db).get_snapshot(run.dataset_snapshot_id)
    return {
        "ingestion_run_id": str(run.ingestion_run_id),
        "source_dataset_id": str(run.source_dataset_id),
        "dataset_snapshot_id": (
            str(run.dataset_snapshot_id)
            if run.dataset_snapshot_id is not None
            else None
        ),
        "purpose": run.purpose,
        "outcome": run.outcome,
        "baseline_ingestion_run_id": (
            str(run.baseline_ingestion_run_id)
            if run.baseline_ingestion_run_id is not None
            else None
        ),
        "requested_by": run.requested_by,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat(),
        "failure_code": run.failure_code,
        "failure_reason": run.failure_reason,
        "source_row_count": snapshot.source_row_count if snapshot else None,
        "source_schema_fingerprint": (
            snapshot.source_schema_fingerprint if snapshot else None
        ),
        "source_dataset_fingerprint": (
            snapshot.source_dataset_fingerprint if snapshot else None
        ),
    }


@app.get("/")
def root():
    return {"status": "Aegis API running"}


@app.post("/simulate-migration")
def simulate_migration(request: MigrationRequest, db: Session = Depends(get_db)):

    inspector = AegisInspector()
    consultant = AegisConsultant()
    surgeon = AegisSurgeon()
    governance = GovernancePolicy()
    approvals = PostgresApprovalRepository(db)

    # Resolve the Gold schema -- either from the registry (schema_name,
    # optionally pinned to a specific schema_version) or from the
    # legacy direct gold_schema payload. Exactly one is guaranteed to
    # be set by MigrationRequest's model_validator.
    schema_version_id = None
    if request.schema_name is not None:
        registry = SchemaRegistryRepository(db)
        try:
            if request.schema_version is not None:
                version = registry.get_version(request.schema_name, request.schema_version)
            else:
                version = registry.get_latest(request.schema_name)
        except SchemaNotFoundError:
            raise HTTPException(status_code=404, detail=f"Schema {request.schema_name!r} not found.")
        except SchemaVersionNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

        schema_version_id = str(version.schema_version_id)
        gold_schema_dict = to_gold_schema_dict(version.schema_definition["columns"])
    else:
        gold_schema_dict = request.gold_schema

    df_observed = pd.DataFrame(request.sample_data)

    observed_schema_obj = inspector.generate_observed_schema(df_observed)
    gold_schema_obj = _build_gold_schema(gold_schema_dict)

    delta = inspector.detect_delta(
        observed_schema_obj,
        gold_schema_obj
    )

    repair_plans = consultant.propose_repairs(
        delta,
        observed_schema_obj,
        gold_schema_obj
    )

    if not repair_plans:
        return {
            "schema_delta": str(delta),
            "schema_version_id": schema_version_id,
            "message": "No repair plans proposed."
        }

    selector = RepairSelector(governance_policy=governance)
    selected_plan = selector.choose_best(repair_plans)

    if selected_plan is None:
        return {
            "schema_delta": str(delta),
            "schema_version_id": schema_version_id,
            "message": "No repair plan survived governance review; all candidates quarantined.",
        }

    conversion_decision = None
    if is_cast_action(selected_plan.proposed_action):
        try:
            conversion_decision = analyze_cast_plan(selected_plan, df_observed)
        except ConversionGovernanceError as e:
            raise HTTPException(status_code=422, detail=str(e))

        # CAST_COLUMN is never auto-approved. A non-SAFE result is exposed as
        # redacted governance evidence but no approval ticket is created, so a
        # human decision cannot override conversion safety.
        if not conversion_decision.is_safe:
            return {
                "schema_delta": str(delta),
                "proposed_repair": str(selected_plan),
                "confidence": selected_plan.confidence,
                "governance_decision": "QUARANTINE",
                "schema_version_id": schema_version_id,
                "status": "BLOCKED_BY_CONVERSION_SAFETY",
                "conversion_decision": _public_conversion_metadata(
                    conversion_decision
                ),
                "message": (
                    "CAST_COLUMN was not submitted for approval because the "
                    f"verified conversion decision is {conversion_decision.status}."
                ),
            }

        decision = "REQUIRES_HUMAN_APPROVAL"
    else:
        decision = governance.evaluate(selected_plan.confidence)

    if decision == "AUTO_APPROVE":
        execution_result, manifest = surgeon.execute(
            repair_plan=selected_plan,
            observed_schema=observed_schema_obj,
            gold_schema=gold_schema_obj,
            target_dataset=df_observed,
            operator="api_user",
            execution_mode="sandbox"
        )
        # Phase 2.5 correction: fingerprint the ACTUAL corrected output
        # this exact sandbox execution produced (manifest.corrected_dataset),
        # not a separate recomputation. A redundant second Surgeon call
        # only proves two invocations agree with each other, not that
        # either one matches what this manifest actually represents.
        corrected_output_fingerprint = compute_dataframe_fingerprint(manifest.corrected_dataset)

        # No approval ticket for an auto-approved repair (ticket_id
        # stays null on the manifest -- see healing_manifests schema).
        # schema_version_id is still recorded independently, since an
        # auto-approved execution can still have registry lineage.
        save_manifest(
            db, manifest, ticket_id=None, schema_version_id=schema_version_id,
            corrected_output_fingerprint=corrected_output_fingerprint,
        )
        return {
            "schema_delta": str(delta),
            "proposed_repair": str(selected_plan),
            "confidence": selected_plan.confidence,
            "governance_decision": decision,
            "schema_version_id": schema_version_id,
            "status": "EXECUTED",
            "execution_result": str(execution_result),
            "manifest": str(manifest),
        }

    if decision == "REQUIRES_HUMAN_APPROVAL":
        ticket = approvals.submit(
            repair_plan=selected_plan,
            observed_schema=observed_schema_obj,
            gold_schema=gold_schema_obj,
            target_dataset=df_observed,
            schema_version_id=schema_version_id,
            live_eligible=False,
            conversion_decision=conversion_decision,
        )
        return {
            "schema_delta": str(delta),
            "proposed_repair": str(selected_plan),
            "confidence": selected_plan.confidence,
            "governance_decision": decision,
            "schema_version_id": schema_version_id,
            "status": "PENDING_APPROVAL",
            "ticket_id": ticket.ticket_id,
            "conversion_decision": _public_conversion_metadata(
                ticket.conversion_decision
            ),
            "message": (
                f"Repair requires human approval before execution. "
                f"POST /approvals/{ticket.ticket_id}/approve to proceed, "
                f"or /reject to discard it."
            ),
        }

    # QUARANTINE -- defensive only. RepairSelector already filters
    # anything below the governance floor out of selected_plan, so
    # this branch should be unreachable in practice; kept in case
    # that filtering logic ever changes.
    return {
        "schema_delta": str(delta),
        "proposed_repair": str(selected_plan),
        "confidence": selected_plan.confidence,
        "governance_decision": decision,
        "schema_version_id": schema_version_id,
        "status": "QUARANTINED",
    }


@app.post("/simulate-migration-from-source")
def simulate_migration_from_source(
    request: SimulateFromSourceRequest,
    db: Session = Depends(get_db),
    source_engine=Depends(get_source_engine),
):
    """
    The live-capable simulation path. Unlike /simulate-migration
    (caller-supplied sample_data, never live-eligible), this reads the
    COMPLETE source table server-side, requires it to have a primary
    key, and records enough provenance (schema + dataset fingerprints)
    for execute-live to later prove the source hasn't changed.
    """
    inspector = AegisInspector()
    consultant = AegisConsultant()
    governance = GovernancePolicy()
    approvals = PostgresApprovalRepository(db)

    identities = IdentityRepository(db)
    try:
        source_system_key, source_binding = _configured_source_binding()
        _verify_optional_source_publication_separation(source_binding)
        source_system = identities.resolve_source_system(
            source_system_key, source_binding
        )
        source_dataset = identities.resolve_source_dataset(
            source_system.source_system_id,
            request.source_schema,
            request.source_table,
        )
    except IdentityConfigurationError as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    except (
        EndpointBindingAlreadyRegisteredError,
        SystemBindingConflictError,
    ) as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except UnsafeDatabaseTopologyError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))
    except InvalidIdentifierError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))

    registry = SchemaRegistryRepository(db)
    try:
        if request.schema_version is not None:
            version = registry.get_version(request.schema_name, request.schema_version)
        else:
            version = registry.get_latest(request.schema_name)
    except SchemaNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Schema {request.schema_name!r} not found.",
        )
    except SchemaVersionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    schema_version_id = str(version.schema_version_id)
    gold_schema_dict = to_gold_schema_dict(version.schema_definition["columns"])

    ingestion = IngestionService(IngestionRepository(db))
    observation_started_at = datetime.now(UTC)

    def persist_failed_simulation(failure_code: str) -> None:
        try:
            ingestion.record_failed_run(
                source_dataset_id=source_dataset.source_dataset_id,
                purpose="SIMULATION",
                started_at=observation_started_at,
                failure_code=failure_code,
            )
            db.commit()
        except Exception as persistence_error:
            db.rollback()
            raise HTTPException(
                status_code=500,
                detail=(
                    "Source observation failed and its redacted terminal ingestion "
                    "record could not be persisted. No approval ticket was created."
                ),
            ) from persistence_error

    try:
        source_read = read_complete_source_table(
            source_engine, request.source_schema, request.source_table
        )
    except SourceValidationError as e:
        persist_failed_simulation("SOURCE_VALIDATION_FAILED")
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        persist_failed_simulation("SOURCE_READ_FAILED")
        raise HTTPException(
            status_code=500,
            detail=(
                "The trusted source could not be read. A redacted FAILED "
                "ingestion run was recorded and no approval ticket was created."
            ),
        ) from e

    try:
        captured = ingestion.record_captured_simulation(
            source_dataset_id=source_dataset.source_dataset_id,
            source_read=source_read,
            started_at=observation_started_at,
        )
    except IngestionRepositoryError as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=(
                "The complete source observation could not be persisted. "
                "No approval ticket was created."
            ),
        ) from e

    lineage = _simulation_lineage(source_system, source_dataset, captured)

    df_observed = source_read["dataframe"]

    # Built from the CAPTURED PostgreSQL column metadata, not
    # DataFrame.dtypes -- every column from read_complete_source_table()
    # is forced to dtype=object to protect Decimal/date/UUID precision,
    # which makes the pandas dtype label meaningless for schema
    # comparison. Consultant.propose_repairs()'s rename detection
    # requires an exact dtype match; without this, a genuinely BIGINT
    # source column would never match a Gold column declared "int64",
    # and no rename would ever be detected. Also rejects any column
    # whose Postgres type has no explicit, safe logical mapping BEFORE
    # a ticket is ever created.
    try:
        observed_schema_obj = build_source_observed_schema(df_observed, source_read["column_types"])
    except UnsupportedSourceTypeError as e:
        db.commit()
        raise HTTPException(status_code=422, detail=str(e))

    gold_schema_obj = _build_gold_schema(gold_schema_dict)

    # Confirmed directly: the Schema Registry accepts a much wider set
    # of dtypes (anything pandas.api.types.pandas_dtype() recognizes,
    # e.g. "int32", "Int64", "category") than publication actually
    # supports. Without this check, such a Gold schema could pass
    # simulation and approval and only fail at live-execution time.
    # Checked here, for the live-capable path specifically, rather than
    # at registration time -- a Gold schema is reusable across both
    # /simulate-migration (sandbox-only, no publication concern at all)
    # and this endpoint, so publishability is a live-path requirement,
    # not a blanket registry-wide one.
    unpublishable = [
        (name, stats.dtype) for name, stats in gold_schema_obj.columns.items()
        if not is_publishable_dtype(stats.dtype)
    ]
    if unpublishable:
        db.commit()
        raise HTTPException(
            status_code=422,
            detail=(
                f"Gold schema {request.schema_name!r} declares dtypes that have no "
                f"PostgreSQL publication mapping, so this schema cannot be used for "
                f"live-capable simulation: {unpublishable}. Supported dtypes: "
                f"{get_publishable_dtypes()}."
            ),
        )

    delta = inspector.detect_delta(observed_schema_obj, gold_schema_obj)

    repair_plans = consultant.propose_repairs(delta, observed_schema_obj, gold_schema_obj)

    if not repair_plans:
        db.commit()
        return {
            "schema_delta": str(delta),
            "schema_version_id": schema_version_id,
            "source_row_count": source_read["row_count"],
            **lineage,
            "message": "No repair plans proposed.",
        }

    selector = RepairSelector(governance_policy=governance)
    selected_plan = selector.choose_best(repair_plans)

    if selected_plan is None:
        db.commit()
        return {
            "schema_delta": str(delta),
            "schema_version_id": schema_version_id,
            "source_row_count": source_read["row_count"],
            **lineage,
            "message": "No repair plan survived governance review; all candidates quarantined.",
        }

    conversion_decision = None
    live_cast_pair = None
    if is_cast_action(selected_plan.proposed_action):
        try:
            conversion_decision = analyze_cast_plan(selected_plan, df_observed)
            if not conversion_decision.is_safe:
                raise ConversionApprovalBlockedError(
                    f"Verified conversion decision is {conversion_decision.status}, not SAFE."
                )
            live_cast_pair = require_live_cast_pair_allowed(
                selected_plan,
                observed_schema_obj,
            )
        except (ConversionGovernanceError, LiveCastAllowlistError) as e:
            db.commit()
            raise HTTPException(
                status_code=422,
                detail=(
                    "Source-backed CAST_COLUMN is not live-eligible. "
                    f"{e}"
                ),
            )

    # Live-capable simulation always goes through human approval,
    # regardless of confidence -- not just as an extra safety margin
    # for anything source-backed, but because AUTO_APPROVE never
    # creates a ticket at all (see the /simulate-migration branch
    # above), and execute-live requires one to reference. Without
    # forcing this, a high-confidence repair from this endpoint would
    # sandbox-execute and then have no path to live execution at all.
    decision = "REQUIRES_HUMAN_APPROVAL"

    ticket = approvals.submit(
        repair_plan=selected_plan,
        observed_schema=observed_schema_obj,
        gold_schema=gold_schema_obj,
        target_dataset=None,
        schema_version_id=schema_version_id,
        source_schema=request.source_schema,
        source_table=request.source_table,
        source_primary_key=source_read["primary_key"],
        source_row_count=source_read["row_count"],
        source_schema_fingerprint=source_read["schema_fingerprint"],
        source_dataset_fingerprint=source_read["dataset_fingerprint"],
        live_eligible=True,
        conversion_decision=conversion_decision,
        source_ingestion_run_id=str(captured.run.ingestion_run_id),
    )
    return {
        "schema_delta": str(delta),
        "proposed_repair": str(selected_plan),
        "confidence": selected_plan.confidence,
        "governance_decision": decision,
        "schema_version_id": schema_version_id,
        "source_schema": request.source_schema,
        "source_table": request.source_table,
        "source_row_count": source_read["row_count"],
        "status": "PENDING_APPROVAL",
        "ticket_id": ticket.ticket_id,
        "live_eligible": True,
        **lineage,
        "conversion_decision": _public_conversion_metadata(
            ticket.conversion_decision
        ),
        "live_cast_pair": live_cast_pair.token if live_cast_pair else None,
        "message": (
            f"Repair requires human approval before execution. "
            f"POST /approvals/{ticket.ticket_id}/approve to proceed, "
            f"or /reject to discard it. This ticket is live-eligible."
        ),
    }


@app.get("/approvals")
def list_pending_approvals(db: Session = Depends(get_db)):
    approvals = PostgresApprovalRepository(db)
    try:
        tickets = approvals.list_pending()
    except ApprovalReplayError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "pending_count": len(tickets),
        "tickets": [
            {
                "ticket_id": t.ticket_id,
                "proposed_repair": str(t.repair_plan),
                "confidence": t.confidence,
                "status": t.status,
                "created_at": t.created_at,
                "schema_version_id": t.schema_version_id,
                "source_ingestion_run_id": t.source_ingestion_run_id,
                "conversion_decision": _public_conversion_metadata(
                    t.conversion_decision
                ),
            }
            for t in tickets
        ],
    }


@app.get("/approvals/{ticket_id}")
def get_approval(ticket_id: str, db: Session = Depends(get_db)):
    approvals = PostgresApprovalRepository(db)
    try:
        t = approvals.get(ticket_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    except ApprovalReplayError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return {
        "ticket_id": t.ticket_id,
        "proposed_repair": str(t.repair_plan),
        "confidence": t.confidence,
        "status": t.status,
        "created_at": t.created_at,
        "decided_by": t.decided_by,
        "decided_at": t.decided_at,
        "decision_note": t.decision_note,
        "schema_version_id": t.schema_version_id,
        "source_ingestion_run_id": t.source_ingestion_run_id,
        "conversion_decision": _public_conversion_metadata(
            t.conversion_decision
        ),
    }


@app.post("/approvals/{ticket_id}/approve")
def approve_ticket(ticket_id: str, request: ApprovalDecisionRequest, db: Session = Depends(get_db)):
    approvals = PostgresApprovalRepository(db)
    try:
        ticket = approvals.approve(ticket_id, operator=request.operator, note=request.note)
    except TicketNotFoundError:
        db.rollback()
        raise HTTPException(status_code=404, detail="Ticket not found.")
    except TicketNotPendingError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))
    except ConversionGovernanceError as e:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                "Approval was blocked by verified conversion governance; "
                f"the ticket remains PENDING. {e}"
            ),
        )
    except ApprovalReplayError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))

    try:
        require_safe_conversion_decision(
            ticket.repair_plan,
            ticket.target_dataset,
            ticket.conversion_decision,
        )
    except ConversionGovernanceError as e:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                "Approval was blocked by verified conversion governance; "
                f"the ticket remains PENDING. {e}"
            ),
        )

    # ticket.status is "APPROVED" here, but NOT YET COMMITTED --
    # approve() only flushed. If execution or manifest persistence
    # fails below, we roll back and the ticket goes back to PENDING
    # in the database (this in-memory `ticket` object is a detached
    # snapshot and won't reflect that rollback itself, which is why
    # the error response below doesn't claim any status for it).
    surgeon = AegisSurgeon()
    try:
        execution_result, manifest = surgeon.execute(
            repair_plan=ticket.repair_plan,
            observed_schema=ticket.observed_schema,
            gold_schema=ticket.gold_schema,
            target_dataset=ticket.target_dataset,
            operator=request.operator,
            execution_mode="sandbox",
        )

        if ticket.conversion_decision is not None:
            if not execution_result.applied or not execution_result.validation.success:
                raise ConversionApprovalBlockedError(
                    "SAFE CAST_COLUMN approval did not produce a successful sandbox execution."
                )
            if manifest.conversion_outcome != ticket.conversion_decision:
                raise StaleConversionDecisionError(
                    "Sandbox conversion outcome does not match the approved decision."
                )

        # Phase 2.5 correction: fingerprint the ACTUAL corrected output
        # this exact sandbox execution produced, not a separate
        # recomputation -- see the AUTO_APPROVE branch above for why
        # that distinction matters.
        corrected_output_fingerprint = compute_dataframe_fingerprint(manifest.corrected_dataset)

        # Manifest inherits the ticket's own lineage -- whatever
        # schema version the ticket was validated against is what it
        # was actually executed against too -- and the ticket's own
        # source provenance, for an independent audit trail on the
        # manifest itself.
        save_manifest(
            db, manifest, ticket_id=ticket.ticket_id,
            schema_version_id=ticket.schema_version_id,
            corrected_output_fingerprint=corrected_output_fingerprint,
            source_schema=ticket.source_schema,
            source_table=ticket.source_table,
            source_primary_key=ticket.source_primary_key,
            source_row_count=ticket.source_row_count,
            source_schema_fingerprint=ticket.source_schema_fingerprint,
            source_dataset_fingerprint=ticket.source_dataset_fingerprint,
            source_ingestion_run_id=ticket.source_ingestion_run_id,
            commit=False,
        )

        # Surgeon's own validation only checks column name/order --
        # it can report applied=True, validation.success=True, and
        # LOW_RISK even when a second column Consultant also flagged
        # still has the wrong type, because RepairSelector only ever
        # chose one repair. execute-live already refuses to publish
        # such a ticket, but without this, the PERSISTED sandbox
        # manifest itself would misleadingly claim full success.
        # Scoped to live-eligible tickets specifically -- a sample_data
        # ticket never carried an expectation of fully resolving the
        # schema, and this is specifically about not letting a
        # live-eligible ticket's manifest overstate what it actually
        # achieved.
        if ticket.live_eligible:
            verify_complete_schema_match(
                manifest.corrected_dataset, ticket.observed_schema,
                ticket.repair_plan.proposed_action, ticket.gold_schema,
                repair_applied=execution_result.applied,
            )
    except ConversionGovernanceError as e:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                "Approval execution was blocked by verified conversion governance; "
                f"the ticket remains PENDING. {e}"
            ),
        )
    except LiveExecutionNotAllowedError as e:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=(
                f"This ticket's repair plan does not fully resolve the schema "
                f"against Gold, so a live-eligible ticket cannot be approved as "
                f"a misleadingly 'successful' manifest -- the approval was "
                f"rolled back and the ticket remains PENDING. {e}"
            ),
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=(
                "Execution or manifest persistence failed. The approval "
                "was rolled back -- the ticket remains PENDING. "
                f"GET /approvals/{ticket_id} to confirm current status."
            ),
        )

    db.commit()

    return {
        "ticket_id": ticket.ticket_id,
        "status": ticket.status,
        "decided_by": ticket.decided_by,
        "decided_at": ticket.decided_at,
        "execution_result": str(execution_result),
        "manifest": str(manifest),
        "conversion_decision": _public_conversion_metadata(
            ticket.conversion_decision
        ),
        "conversion_outcome": _public_conversion_metadata(
            manifest.conversion_outcome
        ),
    }


@app.post("/approvals/{ticket_id}/reject")
def reject_ticket(ticket_id: str, request: ApprovalDecisionRequest, db: Session = Depends(get_db)):
    approvals = PostgresApprovalRepository(db)
    try:
        ticket = approvals.reject(ticket_id, operator=request.operator, note=request.note)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    except TicketNotPendingError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {
        "ticket_id": ticket.ticket_id,
        "status": ticket.status,
        "decided_by": ticket.decided_by,
        "decided_at": ticket.decided_at,
        "decision_note": ticket.decision_note,
    }


@app.post("/schemas/{schema_name}/versions")
def register_schema_version(
    schema_name: str, request: RegisterSchemaVersionRequest, db: Session = Depends(get_db)
):
    registry = SchemaRegistryRepository(db)
    columns = [c.model_dump() for c in request.columns]
    try:
        version = registry.register_version(
            schema_name=schema_name,
            format_version=request.format_version,
            columns=columns,
            description=request.description,
            change_summary=request.change_summary,
            created_by=request.created_by,
        )
    except InvalidSchemaDefinitionError as e:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(e))
    except DuplicateSchemaDefinitionError as e:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(e))

    return {
        "schema_name": schema_name,
        "version_number": version.version_number,
        "schema_version_id": str(version.schema_version_id),
        "fingerprint": version.fingerprint,
        "created_at": version.created_at.isoformat(),
    }


@app.get("/schemas/{schema_name}/versions/{version_number}")
def get_schema_version(schema_name: str, version_number: int, db: Session = Depends(get_db)):
    registry = SchemaRegistryRepository(db)
    try:
        version = registry.get_version(schema_name, version_number)
        family = registry.get_schema_family(schema_name)
    except SchemaNotFoundError:
        raise HTTPException(status_code=404, detail=f"Schema {schema_name!r} not found.")
    except SchemaVersionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "schema_name": schema_name,
        "description": family.description,
        "schema_created_at": family.created_at.isoformat(),
        "schema_created_by": family.created_by,
        "version_number": version.version_number,
        "schema_version_id": str(version.schema_version_id),
        "schema_definition": version.schema_definition,
        "fingerprint": version.fingerprint,
        "change_summary": version.change_summary,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
    }


@app.get("/schemas/{schema_name}/versions")
def list_schema_versions(schema_name: str, db: Session = Depends(get_db)):
    registry = SchemaRegistryRepository(db)
    try:
        versions = registry.list_history(schema_name)
        family = registry.get_schema_family(schema_name)
    except SchemaNotFoundError:
        raise HTTPException(status_code=404, detail=f"Schema {schema_name!r} not found.")

    return {
        "schema_name": schema_name,
        "description": family.description,
        "schema_created_at": family.created_at.isoformat(),
        "schema_created_by": family.created_by,
        "versions": [
            {
                "version_number": v.version_number,
                "schema_version_id": str(v.schema_version_id),
                "fingerprint": v.fingerprint,
                "change_summary": v.change_summary,
                "created_at": v.created_at.isoformat(),
                "created_by": v.created_by,
            }
            for v in versions
        ],
    }


@app.get("/schemas/{schema_name}/latest")
def get_latest_schema_version(schema_name: str, db: Session = Depends(get_db)):
    registry = SchemaRegistryRepository(db)
    try:
        version = registry.get_latest(schema_name)
        family = registry.get_schema_family(schema_name)
    except SchemaNotFoundError:
        raise HTTPException(status_code=404, detail=f"Schema {schema_name!r} not found.")
    except SchemaVersionNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "schema_name": schema_name,
        "description": family.description,
        "schema_created_at": family.created_at.isoformat(),
        "schema_created_by": family.created_by,
        "version_number": version.version_number,
        "schema_version_id": str(version.schema_version_id),
        "schema_definition": version.schema_definition,
        "fingerprint": version.fingerprint,
        "created_at": version.created_at.isoformat(),
    }


@app.get("/source-datasets/{source_dataset_id}")
def get_source_dataset(source_dataset_id: str, db: Session = Depends(get_db)):
    try:
        dataset = IdentityRepository(db).get_source_dataset(source_dataset_id)
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail="Source dataset not found.")
    return {
        "source_dataset_id": str(dataset.source_dataset_id),
        "source_system_id": str(dataset.source_system_id),
        "source_schema": dataset.source_schema,
        "source_table": dataset.source_table,
        "created_at": dataset.created_at.isoformat(),
    }


@app.get("/source-datasets/{source_dataset_id}/ingestion-runs")
def list_source_dataset_ingestion_runs(
    source_dataset_id: str, db: Session = Depends(get_db)
):
    try:
        IdentityRepository(db).get_source_dataset(source_dataset_id)
        runs = IngestionRepository(db).list_runs(source_dataset_id)
        public_runs = [_public_ingestion_run(db, run) for run in runs]
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail="Source dataset not found.")
    except IngestionRepositoryError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {
        "source_dataset_id": source_dataset_id,
        "run_count": len(public_runs),
        "ingestion_runs": public_runs,
    }


@app.get("/ingestion-runs/{ingestion_run_id}")
def get_ingestion_run(ingestion_run_id: str, db: Session = Depends(get_db)):
    try:
        run = IngestionRepository(db).get_run(ingestion_run_id)
        return _public_ingestion_run(db, run)
    except IngestionRepositoryError:
        raise HTTPException(status_code=404, detail="Ingestion run not found.")


@app.post("/approvals/{ticket_id}/execute-live")
def execute_live(
    ticket_id: str,
    request: ExecuteLiveRequest,
    db: Session = Depends(get_db),
    live_engine=Depends(get_live_engine),
    source_engine=Depends(get_source_engine),
):
    # Environment kill switch checked first and separately -- this is
    # a configuration/authorization question (403), not a request-
    # validity one (422).
    if not live_execution_globally_enabled():
        raise HTTPException(
            status_code=403,
            detail="Live execution is disabled (AEGIS_LIVE_EXECUTION_ENABLED is not 'true').",
        )

    approvals = PostgresApprovalRepository(db)
    try:
        # Row-locks the ticket for the rest of this transaction -- two
        # concurrent requests for the same ticket no longer both pass
        # an unlocked check before either commits. The second blocks
        # here until the first's transaction ends, then sees the
        # now-existing live_executions row and is correctly rejected.
        ticket = approvals.lock_for_live_execution(ticket_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    except ApprovalReplayError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if ticket.status != "APPROVED":
        raise HTTPException(
            status_code=409,
            detail=f"Ticket must be APPROVED to execute live, currently {ticket.status}.",
        )

    # Status alone doesn't prove a meaningful human identity approved
    # this -- ApprovalDecisionRequest.operator is validated at the API
    # layer, but this defends against any ticket that predates that
    # validation, or reached APPROVED some other way.
    if not (ticket.decided_by or "").strip() or not ticket.decided_at:
        raise HTTPException(
            status_code=422,
            detail="Ticket has no recorded approval identity or timestamp -- refusing to execute live.",
        )

    is_live_cast = is_cast_action(ticket.repair_plan.proposed_action)
    allowed_live_cast_pairs = frozenset()
    live_cast_pair = None
    try:
        if is_live_cast:
            allowed_live_cast_pairs = configured_live_cast_allowlist()
            require_safe_conversion_decision(
                ticket.repair_plan,
                ticket.target_dataset,
                ticket.conversion_decision,
            )
            live_cast_pair = require_live_cast_pair_allowed(
                ticket.repair_plan,
                ticket.observed_schema,
                allowed_live_cast_pairs,
            )
        elif ticket.conversion_decision is not None:
            raise ConversionApprovalBlockedError(
                "Non-CAST live ticket unexpectedly carries conversion metadata."
            )
    except (ConversionGovernanceError, LiveCastAllowlistError) as e:
        raise HTTPException(status_code=422, detail=str(e))

    manifest_record = (
        db.query(HealingManifestRecord)
        .filter(HealingManifestRecord.ticket_id == uuid.UUID(ticket_id))
        .order_by(HealingManifestRecord.timestamp.desc())
        .first()
    )

    # Manifest-to-ticket consistency -- cheap, DB-only checks, done
    # before anything else touches Surgeon, the source, or creates a
    # record.
    if manifest_record is None:
        raise HTTPException(
            status_code=422, detail="No sandbox Healing Manifest found for this ticket."
        )
    if manifest_record.execution_mode != "sandbox":
        raise HTTPException(
            status_code=422,
            detail=f"Sandbox manifest has unexpected execution_mode {manifest_record.execution_mode!r}.",
        )
    sandbox_conversion_outcome = None
    if is_live_cast:
        if manifest_record.conversion_outcome is None:
            raise HTTPException(
                status_code=422,
                detail="Sandbox manifest has no verified CAST_COLUMN outcome.",
            )
        try:
            sandbox_conversion_outcome = ConversionOutcomeMetadata.from_dict(
                manifest_record.conversion_outcome
            )
        except ValueError as e:
            raise HTTPException(
                status_code=422,
                detail=f"Sandbox CAST_COLUMN outcome is invalid: {e}",
            )
        if sandbox_conversion_outcome != ticket.conversion_decision:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Sandbox CAST_COLUMN outcome does not match the approved "
                    "conversion decision."
                ),
            )
    elif manifest_record.conversion_outcome is not None:
        raise HTTPException(
            status_code=422,
            detail="Non-CAST sandbox manifest unexpectedly carries conversion metadata.",
        )
    if str(manifest_record.schema_version_id) != str(ticket.schema_version_id):
        raise HTTPException(
            status_code=422,
            detail="Manifest schema_version_id does not match the ticket's own lineage.",
        )
    if (
        manifest_record.repair_plan.get("proposed_action") != ticket.repair_plan.proposed_action
        or manifest_record.repair_plan.get("confidence") != ticket.repair_plan.confidence
    ):
        raise HTTPException(
            status_code=422,
            detail="Manifest repair plan does not match the ticket's repair plan.",
        )
    if str(manifest_record.ticket_id) != str(ticket.ticket_id):
        raise HTTPException(
            status_code=422, detail="Manifest ticket_id does not match (should be unreachable)."
        )
    if (
        str(manifest_record.source_ingestion_run_id)
        if manifest_record.source_ingestion_run_id is not None
        else None
    ) != ticket.source_ingestion_run_id:
        raise HTTPException(
            status_code=422,
            detail=(
                "Manifest simulation ingestion lineage does not match the ticket."
            ),
        )

    simulation_replay = None
    if ticket.source_ingestion_run_id is not None:
        try:
            simulation_replay = IngestionRepository(db).load_simulation_replay(
                ticket.source_ingestion_run_id
            )
        except IngestionRepositoryError as e:
            raise HTTPException(
                status_code=422,
                detail="Ticket simulation ingestion lineage is invalid.",
            ) from e
        try:
            source_system_key, source_binding = _configured_source_binding()
            _verify_optional_source_publication_separation(source_binding)
            IdentityRepository(db).verify_source_system_binding(
                simulation_replay.source_system_id,
                source_system_key,
                source_binding,
            )
        except IdentityConfigurationError as e:
            raise HTTPException(status_code=500, detail=str(e))
        except SystemBindingConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except UnsafeDatabaseTopologyError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except IdentityNotFoundError as e:
            raise HTTPException(
                status_code=422,
                detail="Ticket source-system lineage is invalid.",
            ) from e

    live_repo = LiveExecutionRepository(db)

    # Every gate that doesn't require actually reading the source or
    # running Surgeon. live_eligible is the gate that actually closes
    # the sample_data gap -- source_schema/source_table come from the
    # TICKET's own persisted provenance now, never from the caller.
    try:
        evaluate_safety_gates(
            schema_version_id=ticket.schema_version_id,
            sandbox_manifest_applied=bool(manifest_record.execution_result.get("applied", False)),
            sandbox_risk_level=manifest_record.risk_level,
            sandbox_integrity_status=manifest_record.integrity_status,
            original_row_count=manifest_record.original_row_count,
            final_row_count=manifest_record.final_row_count,
            proposed_action=ticket.repair_plan.proposed_action,
            logical_target=request.logical_target,
            live_eligible=ticket.live_eligible,
            source_schema=ticket.source_schema,
            source_table=ticket.source_table,
            confirm=request.confirm,
            already_executed_live=live_repo.has_executed_live(ticket_id),
            unresolved_execution_exists_for_target=live_repo.has_unresolved_execution_for_target(
                request.logical_target
            ),
        )
    except LiveExecutionConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LiveExecutionNotAllowedError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Session-level lock held for the ENTIRE flow below -- source
    # re-verification, Surgeon recomputation, fingerprint
    # verification, AND the target transaction.
    writer = PostgresPublicationWriter(live_engine)
    try:
        with writer.hold_target_lock(request.logical_target) as locked_conn:
            # Created already RUNNING, in one committed insert. The
            # database-enforced backstop against the same race the
            # ticket lock above already guards against.
            try:
                live_record = live_repo.create_running(
                    ticket_id=ticket_id,
                    sandbox_manifest_id=str(manifest_record.manifest_id),
                    schema_version_id=ticket.schema_version_id,
                    logical_target=request.logical_target,
                    requested_by=request.operator,
                    original_row_count=manifest_record.original_row_count,
                    final_row_count=manifest_record.final_row_count,
                    risk_level=manifest_record.risk_level,
                    integrity_status=manifest_record.integrity_status,
                    source_schema=ticket.source_schema,
                    source_table=ticket.source_table,
                    source_primary_key=ticket.source_primary_key,
                    source_row_count=ticket.source_row_count,
                    source_schema_fingerprint=ticket.source_schema_fingerprint,
                    source_dataset_fingerprint=ticket.source_dataset_fingerprint,
                    simulation_ingestion_run_id=ticket.source_ingestion_run_id,
                )
            except LiveExecutionConflictError as e:
                raise HTTPException(status_code=409, detail=str(e))

            # Everything from here to mark_completed() is protected in
            # one failure-handling block: source re-verification,
            # Surgeon recomputation, fingerprint verification, and
            # publication. A failure at ANY point must leave an honest
            # governance state: FAILED when publication was never
            # attempted or is proven not committed, RUNNING only when
            # the target outcome genuinely cannot yet be proven.
            publication_attempted = False
            try:
                # Re-read the source and require the SAME dataset
                # fingerprint as what was approved -- a source change
                # between approval and execution blocks the execution
                # rather than silently publishing against stale
                # provenance. Uses the FRESH read (not the ticket's
                # stored snapshot) for the Surgeon recomputation below,
                # so "verified unchanged" and "what actually gets
                # corrected" are provably the same data, not two
                # things that merely happen to share a fingerprint.
                if simulation_replay is not None:
                    revalidation_started_at = datetime.now(UTC)
                    ingestion_service = IngestionService(IngestionRepository(db))
                    try:
                        fresh_source = read_complete_source_table(
                            source_engine,
                            ticket.source_schema,
                            ticket.source_table,
                        )
                    except SourceValidationError:
                        try:
                            failed_run = ingestion_service.record_failed_run(
                                source_dataset_id=(
                                    simulation_replay.run.source_dataset_id
                                ),
                                purpose="LIVE_REVALIDATION",
                                baseline_ingestion_run_id=(
                                    ticket.source_ingestion_run_id
                                ),
                                requested_by=request.operator,
                                started_at=revalidation_started_at,
                                failure_code="SOURCE_VALIDATION_FAILED",
                            )
                            live_repo.attach_revalidation(
                                live_record.live_execution_id,
                                failed_run.ingestion_run_id,
                            )
                        except Exception as persistence_error:
                            db.rollback()
                            raise RuntimeError(
                                "Live source validation failed and its redacted "
                                "terminal ingestion record could not be persisted."
                            ) from persistence_error
                        raise
                    except Exception as source_error:
                        try:
                            failed_run = ingestion_service.record_failed_run(
                                source_dataset_id=(
                                    simulation_replay.run.source_dataset_id
                                ),
                                purpose="LIVE_REVALIDATION",
                                baseline_ingestion_run_id=(
                                    ticket.source_ingestion_run_id
                                ),
                                requested_by=request.operator,
                                started_at=revalidation_started_at,
                                failure_code="SOURCE_READ_FAILED",
                            )
                            live_repo.attach_revalidation(
                                live_record.live_execution_id,
                                failed_run.ingestion_run_id,
                            )
                        except Exception as persistence_error:
                            db.rollback()
                            raise RuntimeError(
                                "Live source read failed and its redacted terminal "
                                "ingestion record could not be persisted."
                            ) from persistence_error
                        raise RuntimeError(
                            "Trusted source read failed during live revalidation."
                        ) from source_error

                    revalidation = ingestion_service.record_live_revalidation(
                        source_dataset_id=simulation_replay.run.source_dataset_id,
                        baseline_ingestion_run_id=ticket.source_ingestion_run_id,
                        source_read=fresh_source,
                        started_at=revalidation_started_at,
                        requested_by=request.operator,
                    )
                    live_repo.attach_revalidation(
                        live_record.live_execution_id,
                        revalidation.run.ingestion_run_id,
                    )
                    if not revalidation.matched:
                        categories = ", ".join(
                            revalidation.mismatch_categories
                        )
                        raise SourceChangedError(
                            "Trusted source provenance changed after approval "
                            f"({categories}). A new simulation and approval are required."
                        )
                else:
                    # Historical live-eligible tickets retain their existing
                    # copied-provenance verification path; no lineage is
                    # fabricated for rows created before Phase 3.2.
                    fresh_source = verify_source_unchanged(
                        source_engine, ticket.source_schema, ticket.source_table,
                        expected_primary_key=ticket.source_primary_key,
                        expected_row_count=ticket.source_row_count,
                        expected_schema_fingerprint=ticket.source_schema_fingerprint,
                        expected_dataset_fingerprint=ticket.source_dataset_fingerprint,
                    )

                surgeon = AegisSurgeon()
                working_copy = fresh_source["dataframe"].copy()
                live_execution_result, _live_manifest = surgeon.execute(
                    repair_plan=ticket.repair_plan,
                    observed_schema=ticket.observed_schema,
                    gold_schema=ticket.gold_schema,
                    target_dataset=working_copy,
                    operator=request.operator,
                    execution_mode="live",
                    allowed_modes=["sandbox", "live"],
                    allowed_live_cast_pairs=allowed_live_cast_pairs,
                )

                if not live_execution_result.applied or not live_execution_result.validation.success:
                    raise LiveExecutionNotAllowedError(
                        "Fresh live Surgeon execution did not produce a successful repair. "
                        f"{live_execution_result.validation.message}"
                    )

                if is_live_cast:
                    if _live_manifest.conversion_outcome is None:
                        raise LiveExecutionNotAllowedError(
                            "Fresh live CAST_COLUMN execution produced no conversion outcome."
                        )
                    if _live_manifest.conversion_outcome != ticket.conversion_decision:
                        raise LiveExecutionNotAllowedError(
                            "Fresh live CAST_COLUMN outcome does not match the approved decision."
                        )
                    if _live_manifest.conversion_outcome != sandbox_conversion_outcome:
                        raise LiveExecutionNotAllowedError(
                            "Fresh live CAST_COLUMN outcome does not match the sandbox manifest."
                        )

                # For CAST_COLUMN, Surgeon applies to a candidate copy and returns
                # the verified corrected dataset in its manifest. For RENAME_COLUMN
                # the same assignment is equivalent to the legacy in-place result.
                working_copy = _live_manifest.corrected_dataset

                # Surgeon's own validation only checks column name/
                # order against Gold -- it can pass even when a SECOND
                # column still has the wrong type, because
                # RepairSelector only ever picks one of the repairs
                # Consultant proposed. Require a completely empty
                # delta before anything gets published. repair_applied
                # records the fresh Surgeon result for both legacy live
                # RENAME_COLUMN and explicitly allowlisted live CAST_COLUMN.
                verify_complete_schema_match(
                    working_copy, ticket.observed_schema, ticket.repair_plan.proposed_action,
                    ticket.gold_schema, repair_applied=live_execution_result.applied,
                )

                live_recomputed_fingerprint = compute_dataframe_fingerprint(working_copy)
                verify_output_fingerprint_match(
                    sandbox_fingerprint=manifest_record.corrected_output_fingerprint,
                    live_fingerprint=live_recomputed_fingerprint,
                )

                publication_attempted = True
                result = writer.publish(
                    locked_conn,
                    logical_target=request.logical_target,
                    dataframe=working_copy,
                    execution_id=live_record.live_execution_id,
                    expected_row_count=manifest_record.final_row_count,
                    gold_schema=ticket.gold_schema,
                )
            except SourceChangedError as e:
                # The world changed out from under the approval --
                # a state conflict, not a validity failure.
                live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
                raise HTTPException(status_code=409, detail=str(e))
            except SourceValidationError as e:
                live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
                raise HTTPException(status_code=422, detail=str(e))
            except LiveExecutionNotAllowedError as e:
                live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
                raise HTTPException(status_code=422, detail=str(e))
            except IncompatibleViewSchemaError as e:
                # Provably nothing happened -- raised before any DDL,
                # still inside the same transaction.
                live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
                raise HTTPException(status_code=422, detail=str(e))
            except UnsupportedSourceTypeError as e:
                # Should be unreachable in practice -- an unsupported
                # logical dtype is already rejected at simulation time
                # (build_source_observed_schema). Kept as a defensive
                # backstop; raised before any DDL, so provably nothing
                # happened yet.
                live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
                raise HTTPException(status_code=422, detail=str(e))
            except LiveWriteValidationError as e:
                # Raised inside publish()'s own transaction before any
                # commit -- provably rolled back, safe to mark FAILED
                # directly.
                live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Live execution failed and was rolled back at the target "
                        f"database -- the previously published version is unchanged. "
                        f"Reason: {e}"
                    ),
                )
            except Exception as e:
                # Unexpected failures before writer.publish() begins
                # cannot have changed the target database. Record them
                # as FAILED immediately rather than misclassifying a
                # Surgeon/source/application error as an ambiguous
                # publication outcome.
                if not publication_attempted:
                    db.rollback()
                    live_repo.mark_failed(
                        live_record.live_execution_id,
                        failure_reason=(
                            "Live execution failed before target publication began."
                        ),
                    )
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "Live execution failed before target publication began. "
                            "No target transaction was attempted and the execution "
                            "has been recorded as FAILED."
                        ),
                    )

                # Publication was attempted. A durable target-side
                # marker proves the transaction committed, including
                # the case where the response/acknowledgement was lost
                # after commit.
                outcome = writer.check_operation_outcome(
                    live_record.live_execution_id, "PUBLISH"
                )
                if outcome == "completed":
                    marker = writer.get_latest_marker(request.logical_target)
                    physical_table = marker["physical_table"] if marker else None
                    previous_physical_table = marker["previous_physical_table"] if marker else None
                    live_repo.mark_completed(
                        live_record.live_execution_id,
                        physical_table=physical_table,
                        previous_physical_table=previous_physical_table,
                        final_row_count=manifest_record.final_row_count,
                    )
                    return {
                        "live_execution_id": str(live_record.live_execution_id),
                        "status": "COMPLETED",
                        "logical_target": request.logical_target,
                        "physical_table": physical_table,
                        "previous_physical_table": previous_physical_table,
                        "final_row_count": manifest_record.final_row_count,
                        "live_cast_pair": live_cast_pair.token if live_cast_pair else None,
                        "simulation_ingestion_run_id": (
                            str(live_record.simulation_ingestion_run_id)
                            if live_record.simulation_ingestion_run_id is not None
                            else None
                        ),
                        "revalidation_ingestion_run_id": (
                            str(live_record.revalidation_ingestion_run_id)
                            if live_record.revalidation_ingestion_run_id is not None
                            else None
                        ),
                        "publication_target_id": (
                            str(live_record.publication_target_id)
                            if live_record.publication_target_id is not None
                            else None
                        ),
                        "note": (
                            "The original response to this request was lost, but "
                            "the target-side marker confirms the publication "
                            "committed. This response reflects that completed "
                            "operation and does not start a new attempt."
                        ),
                    }

                # A database/connection exception may represent a lost
                # commit acknowledgement. Likewise, outcome ==
                # "unknown" means the target-side marker could not be
                # checked at all. In either case it would be unsafe to
                # mark FAILED and permit a retry, because the original
                # publication may still have committed.
                if isinstance(e, DBAPIError) or outcome == "unknown":
                    live_repo.mark_outcome_unknown(
                        live_record.live_execution_id,
                        reason=(
                            f"Publication raised {type(e).__name__} and its target "
                            f"outcome is not yet provable ({outcome}). Left RUNNING; "
                            f"GET /live-executions/{{id}} will reconcile it after "
                            f"the staleness threshold and target-lock check. "
                            f"Reason: {e}"
                        ),
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "Live execution outcome is not yet provable. The "
                            "execution remains RUNNING and must be reconciled via "
                            "GET /live-executions/{live_execution_id}; retrying "
                            f"blindly risks a duplicate publication. Reason: {e}"
                        ),
                    )

                # The marker table was reachable and contains no
                # PUBLISH marker, while the raised error was an
                # ordinary application exception rather than a DBAPI
                # connection/transaction exception. Publication is
                # therefore not committed and this attempt is safely
                # recorded as FAILED.
                live_repo.mark_failed(
                    live_record.live_execution_id,
                    failure_reason=str(e),
                )
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "Live execution failed and no committed target publication "
                        "was found. The previously published version is unchanged "
                        f"and this attempt has been recorded as FAILED. Reason: {e}"
                    ),
                )


            live_repo.mark_completed(
                live_record.live_execution_id,
                physical_table=result["physical_table"],
                previous_physical_table=result["previous_physical_table"],
                final_row_count=result["final_row_count"],
            )

            return {
                "live_execution_id": str(live_record.live_execution_id),
                "status": "COMPLETED",
                "logical_target": request.logical_target,
                "physical_table": result["physical_table"],
                "previous_physical_table": result["previous_physical_table"],
                "final_row_count": result["final_row_count"],
                "live_cast_pair": live_cast_pair.token if live_cast_pair else None,
                "simulation_ingestion_run_id": (
                    str(live_record.simulation_ingestion_run_id)
                    if live_record.simulation_ingestion_run_id is not None
                    else None
                ),
                "revalidation_ingestion_run_id": (
                    str(live_record.revalidation_ingestion_run_id)
                    if live_record.revalidation_ingestion_run_id is not None
                    else None
                ),
                "publication_target_id": (
                    str(live_record.publication_target_id)
                    if live_record.publication_target_id is not None
                    else None
                ),
            }
    except TargetLockUnavailableError as e:
        # Raised entering hold_target_lock() itself, before
        # create_running() ever runs -- nothing to mark FAILED,
        # nothing has happened yet.
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/live-executions/{live_execution_id}")
def get_live_execution(
    live_execution_id: str, db: Session = Depends(get_db), live_engine=Depends(get_live_engine)
):
    live_repo = LiveExecutionRepository(db)
    try:
        record = live_repo.get(live_execution_id)
    except LiveExecutionNotFoundError:
        raise HTTPException(status_code=404, detail="Live execution not found.")

    # Crash-recovery reconciliation: a record stuck RUNNING or
    # ROLLING_BACK means the API may have crashed between the target
    # transaction committing and the governance update running. The
    # target-side marker is the only reliable evidence of what
    # actually happened. Staleness-gated: only acts after
    # AEGIS_LIVE_EXECUTION_STALE_SECONDS have passed, since a fresh
    # RUNNING/ROLLING_BACK record might simply still be legitimately
    # in progress.
    if record.status == "RUNNING":
        writer = PostgresPublicationWriter(live_engine)
        record = live_repo.reconcile_running(live_execution_id, writer)
    elif record.status == "ROLLING_BACK":
        writer = PostgresPublicationWriter(live_engine)
        record = live_repo.reconcile_rolling_back(live_execution_id, writer)

    return {
        "live_execution_id": str(record.live_execution_id),
        "ticket_id": str(record.ticket_id),
        "schema_version_id": str(record.schema_version_id),
        "status": record.status,
        "logical_target": record.logical_target,
        "physical_table": record.physical_table,
        "previous_physical_table": record.previous_physical_table,
        "requested_by": record.requested_by,
        "started_at": record.started_at.isoformat(),
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "rolled_back_by": record.rolled_back_by,
        "rolled_back_at": record.rolled_back_at.isoformat() if record.rolled_back_at else None,
        "failure_reason": record.failure_reason,
        "original_row_count": record.original_row_count,
        "final_row_count": record.final_row_count,
        "risk_level": record.risk_level,
        "integrity_status": record.integrity_status,
        "source_schema": record.source_schema,
        "source_table": record.source_table,
        "source_primary_key": record.source_primary_key,
        "source_row_count": record.source_row_count,
        "source_schema_fingerprint": record.source_schema_fingerprint,
        "source_dataset_fingerprint": record.source_dataset_fingerprint,
        "simulation_ingestion_run_id": (
            str(record.simulation_ingestion_run_id)
            if record.simulation_ingestion_run_id is not None
            else None
        ),
        "revalidation_ingestion_run_id": (
            str(record.revalidation_ingestion_run_id)
            if record.revalidation_ingestion_run_id is not None
            else None
        ),
        "publication_target_id": (
            str(record.publication_target_id)
            if record.publication_target_id is not None
            else None
        ),
    }


@app.post("/live-executions/{live_execution_id}/rollback")
def rollback_live_execution(
    live_execution_id: str,
    request: LiveRollbackRequest,
    db: Session = Depends(get_db),
    live_engine=Depends(get_live_engine),
):
    live_repo = LiveExecutionRepository(db)
    try:
        record = live_repo.get(live_execution_id)
    except LiveExecutionNotFoundError:
        raise HTTPException(status_code=404, detail="Live execution not found.")

    if record.status != "COMPLETED":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot roll back a live execution with status {record.status!r}.",
        )

    # Governance-side staleness check: has a NEWER execution completed
    # against this same logical target since? The target-side marker
    # check inside writer.rollback_to_previous() below is the other
    # half -- this one catches supersession the governance database
    # itself already knows about; that one catches the published view
    # having been changed by something the governance database
    # doesn't.
    if not live_repo.is_latest_completed_execution_for_target(
        live_execution_id, record.logical_target
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "A newer live execution has since replaced this target -- "
                "refusing to roll back an execution that is not the latest "
                "for it."
            ),
        )

    writer = PostgresPublicationWriter(live_engine)
    try:
        with writer.hold_target_lock(record.logical_target) as locked_conn:
            try:
                record = live_repo.mark_rolling_back(live_execution_id, operator=request.operator)
            except LiveExecutionInvalidStateError as e:
                raise HTTPException(status_code=409, detail=str(e))

            try:
                writer.rollback_to_previous(
                    locked_conn,
                    logical_target=record.logical_target,
                    execution_id=record.live_execution_id,
                    previous_physical_table=record.previous_physical_table,
                )
            except StaleRollbackError as e:
                # Left in ROLLING_BACK rather than reverted -- the
                # target wasn't touched (the check ran before any
                # write), but reverting the governance status back to
                # COMPLETED would be dishonest about having attempted
                # this at all.
                live_repo.mark_rollback_failed(live_execution_id, failure_reason=str(e))
                raise HTTPException(status_code=409, detail=str(e))
            except Exception as e:
                # Genuinely ambiguous, same reasoning as execute_live's
                # generic handler: this exception alone doesn't prove
                # whether the rollback transaction committed. Check the
                # target-side marker before concluding anything -- a
                # plain marker check, not a lock-testing one, since we
                # already hold this target's lock ourselves right now.
                outcome = writer.check_operation_outcome(
                    record.live_execution_id, "ROLLBACK"
                )
                if outcome == "completed":
                    updated = live_repo.mark_rolled_back(live_execution_id)
                    return {
                        "live_execution_id": str(updated.live_execution_id),
                        "status": updated.status,
                        "rolled_back_by": updated.rolled_back_by,
                        "rolled_back_at": updated.rolled_back_at.isoformat(),
                        "note": (
                            "The original response to this request was lost to a "
                            "connection error, but the target-side marker confirms "
                            "the rollback actually committed -- this reflects that, "
                            "not a new attempt."
                        ),
                    }
                elif outcome == "not_committed":
                    # A missing marker at this instant does not prove
                    # the rollback transaction has finished failing --
                    # same reasoning as execute_live's generic handler.
                    # Leave it in ROLLING_BACK (mark_rollback_failed
                    # only annotates the reason, it does not change
                    # status) for reconcile_rolling_back to resolve
                    # once it can additionally prove the target lock
                    # is free.
                    live_repo.mark_rollback_failed(
                        live_execution_id,
                        failure_reason=(
                            f"Connection error during rollback -- outcome not yet "
                            f"provable (marker absent). Left in ROLLING_BACK; GET "
                            f"/live-executions/{{id}} will reconcile it once the "
                            f"staleness threshold passes and the target lock can be "
                            f"proven free. Reason: {e}"
                        ),
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            f"Rollback outcome is not yet provable -- a connection error "
                            f"occurred and the target-side marker was absent, but that "
                            f"alone doesn't prove the rollback failed. This execution "
                            f"remains in ROLLING_BACK; poll GET "
                            f"/live-executions/{{live_execution_id}} for the reconciled "
                            f"outcome once the staleness threshold passes. Reason: {e}"
                        ),
                    )
                else:
                    live_repo.mark_outcome_unknown(
                        live_execution_id,
                        reason=(
                            f"Connection error during rollback, and the target-side "
                            f"marker could not be checked either -- outcome unknown. "
                            f"Reason: {e}"
                        ),
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            f"Rollback outcome is UNKNOWN -- a connection error occurred "
                            f"and the target-side marker could not be checked either. "
                            f"This execution remains in ROLLING_BACK and needs manual "
                            f"investigation before any retry. Reason: {e}"
                        ),
                    )

            updated = live_repo.mark_rolled_back(live_execution_id)

            return {
                "live_execution_id": str(updated.live_execution_id),
                "status": updated.status,
                "rolled_back_by": updated.rolled_back_by,
                "rolled_back_at": updated.rolled_back_at.isoformat(),
            }
    except TargetLockUnavailableError as e:
        # Raised entering hold_target_lock() itself, before
        # mark_rolling_back() ever runs.
        raise HTTPException(status_code=409, detail=str(e))
