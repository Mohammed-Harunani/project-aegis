from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict
from sqlalchemy.orm import Session
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


class MigrationRequest(BaseModel):
    gold_schema: Dict[str, str]
    sample_data: Dict[str, list]


class ApprovalDecisionRequest(BaseModel):
    operator: str
    note: str = ""


def _build_gold_schema(gold_schema: Dict[str, str]) -> ObservedSchema:
    """
    Builds the Gold ObservedSchema directly from the declared
    name -> dtype mapping in the request. Does NOT go through
    Inspector.generate_observed_schema() on an empty DataFrame --
    see Phase 2.2 notes: that silently discarded every declared dtype.
    No real data backs a schema *definition*, so null_count and
    unique_count are 0 rather than inferred from anything.
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

    df_observed = pd.DataFrame(request.sample_data)

    observed_schema_obj = inspector.generate_observed_schema(df_observed)
    gold_schema_obj = _build_gold_schema(request.gold_schema)

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
            "message": "No repair plans proposed."
        }

    selector = RepairSelector(governance_policy=governance)
    selected_plan = selector.choose_best(repair_plans)

    if selected_plan is None:
        return {
            "schema_delta": str(delta),
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
        save_manifest(db, manifest, ticket_id=None)
        return {
            "schema_delta": str(delta),
            "proposed_repair": str(selected_plan),
            "confidence": selected_plan.confidence,
            "governance_decision": decision,
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
        )
        return {
            "schema_delta": str(delta),
            "proposed_repair": str(selected_plan),
            "confidence": selected_plan.confidence,
            "governance_decision": decision,
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
    }


@app.post("/approvals/{ticket_id}/approve")
def approve_ticket(ticket_id: str, request: ApprovalDecisionRequest, db: Session = Depends(get_db)):
    approvals = PostgresApprovalRepository(db)
    try:
        ticket = approvals.approve(ticket_id, operator=request.operator, note=request.note)
    except TicketNotFoundError:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    except TicketNotPendingError as e:
        raise HTTPException(status_code=409, detail=str(e))

    surgeon = AegisSurgeon()
    execution_result, manifest = surgeon.execute(
        repair_plan=ticket.repair_plan,
        observed_schema=ticket.observed_schema,
        gold_schema=ticket.gold_schema,
        target_dataset=ticket.target_dataset,
        operator=request.operator,
        execution_mode="sandbox",
    )
    save_manifest(db, manifest, ticket_id=ticket.ticket_id)

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
