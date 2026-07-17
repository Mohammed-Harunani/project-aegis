from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator, field_validator
from typing import Dict, List, Optional
from sqlalchemy.orm import Session
import uuid
import pandas as pd

from src.inspector import AegisInspector, ObservedSchema, ColumnStats
from src.consultant import AegisConsultant
from src.surgeon import AegisSurgeon
from src.governance.policy import GovernancePolicy
from src.governance.selector import RepairSelector
from src.governance.approval import TicketNotFoundError, TicketNotPendingError
from src.governance.approval_repository import PostgresApprovalRepository
from src.governance.manifest_repository import save_manifest
from src.db.session import get_db
from src.db.models import HealingManifestRecord
from src.db.live_session import get_live_engine
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
    get_target_schema_allowlist,
    validate_and_normalize_operator,
    verify_output_fingerprint_match,
    evaluate_safety_gates,
)
from src.live_execution.repository import (
    LiveExecutionRepository,
    LiveExecutionNotFoundError,
    LiveExecutionInvalidStateError,
)
from src.live_execution.writer import (
    PostgresLiveWriter,
    LiveWriteValidationError,
    UnmanagedTargetTableError,
    StaleRollbackError,
    TargetLockUnavailableError,
)
from src.live_execution.output_fingerprint import compute_dataframe_fingerprint


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
    target_schema: str
    target_table: str
    # Mandatory, not optional -- originally optional, which converted
    # gate 11 (target must not be the source table) into something a
    # caller could simply omit to bypass. See
    # Docs/phase2_5_live_execution_spec.md.
    source_schema: str
    source_table: str
    # Explicit "yes I mean it" -- gate 14. No default of True; the
    # caller must say so.
    confirm: bool = False

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
        )
        return {
            "schema_delta": str(delta),
            "proposed_repair": str(selected_plan),
            "confidence": selected_plan.confidence,
            "governance_decision": decision,
            "schema_version_id": schema_version_id,
            "status": "PENDING_APPROVAL",
            "ticket_id": ticket.ticket_id,
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


@app.get("/approvals")
def list_pending_approvals(db: Session = Depends(get_db)):
    approvals = PostgresApprovalRepository(db)
    tickets = approvals.list_pending()
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
        # Phase 2.5 correction: fingerprint the ACTUAL corrected output
        # this exact sandbox execution produced, not a separate
        # recomputation -- see the AUTO_APPROVE branch above for why
        # that distinction matters.
        corrected_output_fingerprint = compute_dataframe_fingerprint(manifest.corrected_dataset)

        # Manifest inherits the ticket's own lineage -- whatever
        # schema version the ticket was validated against is what it
        # was actually executed against too.
        save_manifest(
            db, manifest, ticket_id=ticket.ticket_id,
            schema_version_id=ticket.schema_version_id,
            corrected_output_fingerprint=corrected_output_fingerprint,
            commit=False,
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


@app.post("/approvals/{ticket_id}/execute-live")
def execute_live(
    ticket_id: str,
    request: ExecuteLiveRequest,
    db: Session = Depends(get_db),
    live_engine=Depends(get_live_engine),
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
        # Row-locks the ticket for the rest of this transaction --
        # correction for issue 2: two concurrent requests for the same
        # ticket no longer both pass an unlocked check before either
        # commits. The second blocks here until the first's
        # transaction ends (success or failure), then sees the
        # now-existing live_executions row and is correctly rejected.
        ticket = approvals.lock_for_live_execution(ticket_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    if ticket.status != "APPROVED":
        raise HTTPException(
            status_code=409,
            detail=f"Ticket must be APPROVED to execute live, currently {ticket.status}.",
        )

    # Status alone doesn't prove a meaningful human identity approved
    # this -- ApprovalDecisionRequest.operator is now validated at the
    # API layer, but this defends against any ticket that predates
    # that validation, or reached APPROVED some other way.
    if not (ticket.decided_by or "").strip() or not ticket.decided_at:
        raise HTTPException(
            status_code=422,
            detail="Ticket has no recorded approval identity or timestamp -- refusing to execute live.",
        )

    manifest_record = (
        db.query(HealingManifestRecord)
        .filter(HealingManifestRecord.ticket_id == uuid.UUID(ticket_id))
        .order_by(HealingManifestRecord.timestamp.desc())
        .first()
    )

    # Manifest-to-ticket consistency (issue 7) -- cheap, DB-only checks,
    # done before anything else touches Surgeon or creates a record.
    if manifest_record is None:
        raise HTTPException(
            status_code=422, detail="No sandbox Healing Manifest found for this ticket."
        )
    if manifest_record.execution_mode != "sandbox":
        raise HTTPException(
            status_code=422,
            detail=f"Sandbox manifest has unexpected execution_mode {manifest_record.execution_mode!r}.",
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
    # Implicit in the query filter above (manifest_record can only be
    # found via ticket_id == this ticket) -- asserted anyway as
    # defense against a future query change.
    if str(manifest_record.ticket_id) != str(ticket.ticket_id):
        raise HTTPException(
            status_code=422, detail="Manifest ticket_id does not match (should be unreachable)."
        )

    live_repo = LiveExecutionRepository(db)

    # Every gate that doesn't require actually running Surgeon --
    # fingerprint comparison happens separately, below, inside the
    # protected block, since it can only be checked after Surgeon
    # recomputes (issue 1: Surgeon must not run before a durable
    # execution record exists, or a failure has nothing to mark
    # FAILED and leaves nothing for a caller to even look up).
    try:
        evaluate_safety_gates(
            schema_version_id=ticket.schema_version_id,
            sandbox_manifest_applied=bool(manifest_record.execution_result.get("applied", False)),
            sandbox_risk_level=manifest_record.risk_level,
            sandbox_integrity_status=manifest_record.integrity_status,
            original_row_count=manifest_record.original_row_count,
            final_row_count=manifest_record.final_row_count,
            proposed_action=ticket.repair_plan.proposed_action,
            target_schema=request.target_schema,
            target_table=request.target_table,
            source_schema=request.source_schema,
            source_table=request.source_table,
            confirm=request.confirm,
            schema_allowlist=get_target_schema_allowlist(),
            already_executed_live=live_repo.has_executed_live(ticket_id),
            unresolved_execution_exists_for_target=live_repo.has_unresolved_execution_for_target(
                request.target_schema, request.target_table
            ),
        )
    except LiveExecutionConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LiveExecutionNotAllowedError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Created already RUNNING, in one committed insert (issue 5 fix --
    # a separate PENDING-then-RUNNING pair of commits left a crash
    # window where the record could be stranded at PENDING forever).
    # The database-enforced backstop against the same race the ticket
    # lock above already guards against -- if this still hits a
    # partial-unique-index violation (e.g. a concurrent request
    # against the same TARGET from a DIFFERENT ticket, which the
    # ticket lock alone wouldn't catch), it's converted to 409 here.
    try:
        live_record = live_repo.create_running(
            ticket_id=ticket_id,
            sandbox_manifest_id=str(manifest_record.manifest_id),
            schema_version_id=ticket.schema_version_id,
            target_schema=request.target_schema,
            target_table=request.target_table,
            requested_by=request.operator,
            original_row_count=manifest_record.original_row_count,
            final_row_count=manifest_record.final_row_count,
            risk_level=manifest_record.risk_level,
            integrity_status=manifest_record.integrity_status,
        )
    except LiveExecutionConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Everything from here to mark_completed() is protected in one
    # failure-handling block (issue 1 fix): Surgeon recomputation,
    # fingerprint verification, and target publication. A failure at
    # ANY point marks the (already-durable) record FAILED rather than
    # leaving it stuck RUNNING or, worse, never having existed at all.
    writer = PostgresLiveWriter(live_engine)
    try:
        # Deterministically recompute the correction rather than
        # persist a second copy of it anywhere -- reuses Surgeon's
        # dormant "live" mode from Phase 1 (confirmed directly:
        # mutates target_dataset in place instead of returning a new
        # one). Operates on a copy of the ticket's own stored snapshot
        # so that snapshot itself is untouched.
        surgeon = AegisSurgeon()
        working_copy = ticket.target_dataset.copy()
        surgeon.execute(
            repair_plan=ticket.repair_plan,
            observed_schema=ticket.observed_schema,
            gold_schema=ticket.gold_schema,
            target_dataset=working_copy,
            operator=request.operator,
            execution_mode="live",
            allowed_modes=["sandbox", "live"],
        )

        live_recomputed_fingerprint = compute_dataframe_fingerprint(working_copy)
        verify_output_fingerprint_match(
            sandbox_fingerprint=manifest_record.corrected_output_fingerprint,
            live_fingerprint=live_recomputed_fingerprint,
        )

        result = writer.promote(
            target_schema=request.target_schema,
            target_table=request.target_table,
            dataframe=working_copy,
            execution_id=live_record.live_execution_id,
            expected_row_count=manifest_record.final_row_count,
        )
    except TargetLockUnavailableError as e:
        # Provably nothing happened -- raised before any DDL even
        # starts inside promote()'s transaction.
        live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except LiveExecutionNotAllowedError as e:
        live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
        raise HTTPException(status_code=422, detail=str(e))
    except UnmanagedTargetTableError as e:
        # Provably nothing happened -- raised before any DDL, still
        # inside the same transaction.
        live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
        raise HTTPException(status_code=422, detail=str(e))
    except LiveWriteValidationError as e:
        # Raised inside promote()'s own transaction before any commit
        # -- provably rolled back, safe to mark FAILED directly.
        live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
        raise HTTPException(
            status_code=500,
            detail=(
                f"Live execution failed and was rolled back at the target "
                f"database -- the target table is unchanged. Reason: {e}"
            ),
        )
    except Exception as e:
        # Genuinely ambiguous: this exception alone doesn't prove
        # whether the target transaction committed -- e.g. the
        # connection could have dropped after a successful commit but
        # before acknowledging it back to us. Guessing wrong in either
        # direction is dangerous: marking FAILED (retryable) when it
        # actually committed risks a duplicate publication; marking
        # COMPLETED when it didn't would hide a real failure. Check the
        # target-side marker before concluding anything.
        outcome = writer.check_operation_outcome(
            request.target_schema, live_record.live_execution_id, "PROMOTE"
        )
        if outcome == "completed":
            marker = writer.get_latest_management_marker(
                request.target_schema, request.target_table
            )
            backup_table = marker["backup_table"] if marker else None
            live_repo.mark_completed(
                live_record.live_execution_id,
                backup_table=backup_table,
                final_row_count=manifest_record.final_row_count,
            )
            return {
                "live_execution_id": str(live_record.live_execution_id),
                "status": "COMPLETED",
                "target_schema": request.target_schema,
                "target_table": request.target_table,
                "backup_table": backup_table,
                "final_row_count": manifest_record.final_row_count,
                "note": (
                    "The original response to this request was lost to a "
                    "connection error, but the target-side marker confirms "
                    "the publication actually committed -- this reflects "
                    "that, not a new attempt."
                ),
            }
        elif outcome == "not_committed":
            live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Live execution failed and was rolled back at the target "
                    f"database -- the target table is unchanged. Reason: {e}"
                ),
            )
        else:
            live_repo.mark_outcome_unknown(
                live_record.live_execution_id,
                reason=(
                    f"Connection error during promotion, and the target-side "
                    f"marker could not be checked either -- outcome unknown. "
                    f"Reason: {e}"
                ),
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Live execution outcome is UNKNOWN -- a connection error "
                    f"occurred and the target-side marker could not be checked "
                    f"either. This execution remains RUNNING and needs manual "
                    f"investigation before any retry; retrying blindly risks a "
                    f"duplicate publication. Reason: {e}"
                ),
            )

    live_repo.mark_completed(
        live_record.live_execution_id,
        backup_table=result["backup_table"],
        final_row_count=result["final_row_count"],
    )

    return {
        "live_execution_id": str(live_record.live_execution_id),
        "status": "COMPLETED",
        "target_schema": request.target_schema,
        "target_table": request.target_table,
        "backup_table": result["backup_table"],
        "final_row_count": result["final_row_count"],
    }


@app.get("/live-executions/{live_execution_id}")
def get_live_execution(
    live_execution_id: str, db: Session = Depends(get_db), live_engine=Depends(get_live_engine)
):
    live_repo = LiveExecutionRepository(db)
    try:
        record = live_repo.get(live_execution_id)
    except LiveExecutionNotFoundError:
        raise HTTPException(status_code=404, detail="Live execution not found.")

    # Crash-recovery reconciliation (issues 3 & 4): a record stuck
    # RUNNING or ROLLING_BACK means the API may have crashed between
    # the target transaction committing and the governance update
    # running. The target-side marker is the only reliable evidence of
    # what actually happened. Staleness-gated (issue 4): only acts
    # after AEGIS_LIVE_EXECUTION_STALE_SECONDS have passed, since a
    # fresh RUNNING/ROLLING_BACK record might simply still be
    # legitimately in progress -- reconciling immediately would
    # incorrectly fail an operation that could still succeed.
    if record.status == "RUNNING":
        writer = PostgresLiveWriter(live_engine)
        record = live_repo.reconcile_running(live_execution_id, writer)
    elif record.status == "ROLLING_BACK":
        writer = PostgresLiveWriter(live_engine)
        record = live_repo.reconcile_rolling_back(live_execution_id, writer)

    return {
        "live_execution_id": str(record.live_execution_id),
        "ticket_id": str(record.ticket_id),
        "schema_version_id": str(record.schema_version_id),
        "status": record.status,
        "target_schema": record.target_schema,
        "target_table": record.target_table,
        "backup_table": record.backup_table,
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

    # Governance-side staleness check (issue 5): has a NEWER execution
    # completed against this same target since? The target-side
    # marker check inside writer.rollback() below is the other half --
    # this one catches supersession the governance database itself
    # already knows about; that one catches the target table having
    # been changed by something the governance database doesn't.
    if not live_repo.is_latest_completed_execution_for_target(
        live_execution_id, record.target_schema, record.target_table
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "A newer live execution has since replaced this target -- "
                "refusing to roll back an execution that is not the latest "
                "for it."
            ),
        )

    try:
        record = live_repo.mark_rolling_back(live_execution_id, operator=request.operator)
    except LiveExecutionInvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))

    writer = PostgresLiveWriter(live_engine)
    try:
        writer.rollback(
            target_schema=record.target_schema,
            target_table=record.target_table,
            backup_table=record.backup_table,
            execution_id=record.live_execution_id,
        )
    except StaleRollbackError as e:
        # Left in ROLLING_BACK rather than reverted -- the target
        # wasn't touched (the check ran before any write), but
        # reverting the governance status back to COMPLETED would be
        # dishonest about having attempted this at all.
        live_repo.mark_rollback_failed(live_execution_id, failure_reason=str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except TargetLockUnavailableError as e:
        # Provably nothing happened -- raised before any DDL even
        # starts inside rollback()'s transaction.
        live_repo.mark_rollback_failed(live_execution_id, failure_reason=str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        # Genuinely ambiguous, same reasoning as execute_live's
        # generic handler: this exception alone doesn't prove whether
        # the rollback transaction committed. Check the target-side
        # marker before concluding anything.
        outcome = writer.check_operation_outcome(
            record.target_schema, record.live_execution_id, "ROLLBACK"
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
            live_repo.mark_rollback_failed(live_execution_id, failure_reason=str(e))
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Rollback failed and was rolled back at the target "
                    f"database -- the target table is unchanged from before "
                    f"this rollback attempt. Reason: {e}"
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
                status_code=500,
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
