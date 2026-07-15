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
    evaluate_safety_gates,
)
from src.live_execution.repository import (
    LiveExecutionRepository,
    LiveExecutionNotFoundError,
    LiveExecutionInvalidStateError,
)
from src.live_execution.writer import PostgresLiveWriter, LiveWriteValidationError


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


class ExecuteLiveRequest(BaseModel):
    operator: str
    target_schema: str
    target_table: str
    # Optional: see Docs/phase2_5_live_execution_spec.md -- nothing in
    # Aegis today tracks a live source-table reference to compare
    # against otherwise, so this is opt-in provenance, not inferred.
    source_schema: Optional[str] = None
    source_table: Optional[str] = None
    # Explicit "yes I mean it" -- gate 14. No default of True; the
    # caller must say so.
    confirm: bool = False


class LiveRollbackRequest(BaseModel):
    operator: str


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
        # No approval ticket for an auto-approved repair (ticket_id
        # stays null on the manifest -- see healing_manifests schema).
        # schema_version_id is still recorded independently, since an
        # auto-approved execution can still have registry lineage.
        save_manifest(db, manifest, ticket_id=None, schema_version_id=schema_version_id)
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
        # Manifest inherits the ticket's own lineage -- whatever
        # schema version the ticket was validated against is what it
        # was actually executed against too.
        save_manifest(
            db, manifest, ticket_id=ticket.ticket_id,
            schema_version_id=ticket.schema_version_id, commit=False,
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
        ticket = approvals.get(ticket_id)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    if ticket.status != "APPROVED":
        raise HTTPException(
            status_code=409,
            detail=f"Ticket must be APPROVED to execute live, currently {ticket.status}.",
        )

    manifest_record = (
        db.query(HealingManifestRecord)
        .filter(HealingManifestRecord.ticket_id == uuid.UUID(ticket_id))
        .order_by(HealingManifestRecord.timestamp.desc())
        .first()
    )

    live_repo = LiveExecutionRepository(db)

    try:
        evaluate_safety_gates(
            schema_version_id=ticket.schema_version_id,
            sandbox_manifest_applied=bool(
                manifest_record and manifest_record.execution_result.get("applied", False)
            ),
            sandbox_risk_level=manifest_record.risk_level if manifest_record else "",
            sandbox_integrity_status=manifest_record.integrity_status if manifest_record else "",
            original_row_count=manifest_record.original_row_count if manifest_record else -1,
            final_row_count=manifest_record.final_row_count if manifest_record else -2,
            proposed_action=ticket.repair_plan.proposed_action,
            target_schema=request.target_schema,
            target_table=request.target_table,
            source_schema=request.source_schema,
            source_table=request.source_table,
            confirm=request.confirm,
            schema_allowlist=get_target_schema_allowlist(),
            already_executed_live=live_repo.has_completed_execution(ticket_id),
            unresolved_execution_exists_for_target=live_repo.has_unresolved_execution_for_target(
                request.target_schema, request.target_table
            ),
        )
    except LiveExecutionConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LiveExecutionNotAllowedError as e:
        raise HTTPException(status_code=422, detail=str(e))

    live_record = live_repo.create_pending(
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
    live_repo.mark_running(live_record.live_execution_id)

    # Deterministically recompute the correction rather than persist a
    # second copy of it anywhere -- reuses Surgeon's dormant "live"
    # mode from Phase 1 (confirmed directly: mutates target_dataset in
    # place instead of returning a new one). Operates on a copy of the
    # ticket's own stored snapshot so that snapshot itself is untouched.
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

    writer = PostgresLiveWriter(live_engine)
    try:
        result = writer.promote(
            target_schema=request.target_schema,
            target_table=request.target_table,
            dataframe=working_copy,
            execution_id=live_record.live_execution_id,
            expected_row_count=manifest_record.final_row_count,
        )
    except Exception as e:
        live_repo.mark_failed(live_record.live_execution_id, failure_reason=str(e))
        raise HTTPException(
            status_code=500,
            detail=(
                f"Live execution failed and was rolled back at the target "
                f"database -- the target table is unchanged. Reason: {e}"
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
def get_live_execution(live_execution_id: str, db: Session = Depends(get_db)):
    live_repo = LiveExecutionRepository(db)
    try:
        record = live_repo.get(live_execution_id)
    except LiveExecutionNotFoundError:
        raise HTTPException(status_code=404, detail="Live execution not found.")

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

    writer = PostgresLiveWriter(live_engine)
    try:
        writer.rollback(
            target_schema=record.target_schema,
            target_table=record.target_table,
            backup_table=record.backup_table,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {e}")

    try:
        updated = live_repo.mark_rolled_back(live_execution_id, rolled_back_by=request.operator)
    except LiveExecutionInvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {
        "live_execution_id": str(updated.live_execution_id),
        "status": updated.status,
        "rolled_back_by": updated.rolled_back_by,
        "rolled_back_at": updated.rolled_back_at.isoformat(),
    }
