from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any
import pandas as pd

from src.inspector import AegisInspector, ObservedSchema, ColumnStats
from src.consultant import AegisConsultant
from src.surgeon import AegisSurgeon
from src.governance.policy import GovernancePolicy
from src.governance.selector import RepairSelector
from src.governance.approval import (
    ApprovalQueue,
    TicketNotFoundError,
    TicketNotPendingError,
)


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

# Shared across requests, unlike Inspector/Consultant/Surgeon below
# (which are stateless and cheap to build per-call). This holds
# PENDING tickets between a /simulate-migration call and whichever
# later call approves or rejects it -- in-memory only for now; see
# approval.py for why that's a deliberate Phase 2.2 scope limit, not
# an oversight.
approval_queue = ApprovalQueue()


class MigrationRequest(BaseModel):
    gold_schema: Dict[str, str]
    sample_data: Dict[str, list]


class ApprovalDecisionRequest(BaseModel):
    operator: str
    note: str = ""


def _build_gold_schema(gold_schema: Dict[str, str]) -> ObservedSchema:
    """
    Builds the Gold ObservedSchema directly from the declared
    name -> dtype mapping in the request.

    Deliberately does NOT go through Inspector.generate_observed_schema()
    on an empty DataFrame: pd.DataFrame(columns=...) with no rows gives
    every column dtype 'object', silently discarding the dtypes the
    caller actually specified. There's no real data behind a schema
    *definition* (as opposed to an observed sample), so null_count and
    unique_count are set to 0 rather than inferred from anything.
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
def simulate_migration(request: MigrationRequest):

    inspector = AegisInspector()
    consultant = AegisConsultant()
    surgeon = AegisSurgeon()
    governance = GovernancePolicy()

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
        ticket = approval_queue.submit(
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
def list_pending_approvals():
    tickets = approval_queue.list_pending()
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
def get_approval(ticket_id: str):
    try:
        t = approval_queue.get(ticket_id)
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
def approve_ticket(ticket_id: str, request: ApprovalDecisionRequest):
    try:
        ticket = approval_queue.approve(ticket_id, operator=request.operator, note=request.note)
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

    return {
        "ticket_id": ticket.ticket_id,
        "status": ticket.status,
        "decided_by": ticket.decided_by,
        "decided_at": ticket.decided_at,
        "execution_result": str(execution_result),
        "manifest": str(manifest),
    }


@app.post("/approvals/{ticket_id}/reject")
def reject_ticket(ticket_id: str, request: ApprovalDecisionRequest):
    try:
        ticket = approval_queue.reject(ticket_id, operator=request.operator, note=request.note)
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