"""
Aegis_PostgresApprovalRepository
Phase 2.3 -- Postgres-backed replacement for the in-memory ApprovalQueue
from Phase 2.2.

Deliberately mirrors ApprovalQueue's method surface (submit / get /
list_pending / approve / reject) so callers change which class they
instantiate, not how they call it. Reuses ApprovalTicket,
TicketNotFoundError, and TicketNotPendingError from approval.py rather
than redefining them -- those are transport-agnostic; only the storage
underneath changes in this phase.
"""

import uuid
from datetime import datetime, UTC
from typing import List

import pandas as pd
from sqlalchemy.orm import Session

from src.consultant.consultant import RepairPlan
from src.inspector import ObservedSchema, ColumnStats
from src.db.models import ApprovalTicketRecord
from src.governance.approval import (
    ApprovalTicket,
    TicketNotFoundError,
    TicketNotPendingError,
)


def _schema_to_json(schema: ObservedSchema) -> dict:
    return {
        "columns": {
            name: {
                "null_count": stats.null_count,
                "unique_count": stats.unique_count,
                "dtype": stats.dtype,
            }
            for name, stats in schema.columns.items()
        },
        "column_order": schema.column_order,
    }


def _schema_from_json(data: dict) -> ObservedSchema:
    columns = {name: ColumnStats(**stats) for name, stats in data["columns"].items()}
    return ObservedSchema(columns=columns, column_order=data["column_order"])


def _dataset_to_json(df: pd.DataFrame) -> dict:
    # Matches the shape of MigrationRequest.sample_data (column -> list
    # of values), so round-tripping through pd.DataFrame(...) on read
    # reconstructs the same DataFrame shape Surgeon originally saw.
    return df.to_dict(orient="list")


def _dataset_from_json(data: dict) -> pd.DataFrame:
    return pd.DataFrame(data)


def _record_to_ticket(record: ApprovalTicketRecord) -> ApprovalTicket:
    return ApprovalTicket(
        ticket_id=str(record.ticket_id),
        repair_plan=RepairPlan(
            proposed_action=record.proposed_action,
            confidence=float(record.confidence),
            explanation=record.explanation,
        ),
        confidence=float(record.confidence),
        status=record.status,
        created_at=record.created_at.isoformat(),
        observed_schema=_schema_from_json(record.observed_schema),
        gold_schema=_schema_from_json(record.gold_schema),
        target_dataset=_dataset_from_json(record.target_dataset),
        decided_by=record.decided_by,
        decided_at=record.decided_at.isoformat() if record.decided_at else None,
        decision_note=record.decision_note,
    )


class PostgresApprovalRepository:
    """
    Aegis_PostgresApprovalRepository
    One instance per request, built from a request-scoped Session
    (see db/session.py's get_db()).
    """

    def __init__(self, db: Session):
        self.db = db

    def submit(
        self,
        repair_plan: RepairPlan,
        observed_schema: ObservedSchema,
        gold_schema: ObservedSchema,
        target_dataset: pd.DataFrame,
    ) -> ApprovalTicket:
        record = ApprovalTicketRecord(
            ticket_id=uuid.uuid4(),
            proposed_action=repair_plan.proposed_action,
            confidence=repair_plan.confidence,
            explanation=repair_plan.explanation,
            status="PENDING",
            created_at=datetime.now(UTC),
            observed_schema=_schema_to_json(observed_schema),
            gold_schema=_schema_to_json(gold_schema),
            target_dataset=_dataset_to_json(target_dataset),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return _record_to_ticket(record)

    def _get_record(self, ticket_id: str) -> ApprovalTicketRecord:
        try:
            ticket_uuid = uuid.UUID(ticket_id)
        except ValueError:
            raise TicketNotFoundError(ticket_id)

        record = (
            self.db.query(ApprovalTicketRecord)
            .filter(ApprovalTicketRecord.ticket_id == ticket_uuid)
            .one_or_none()
        )
        if record is None:
            raise TicketNotFoundError(ticket_id)
        return record

    def get(self, ticket_id: str) -> ApprovalTicket:
        return _record_to_ticket(self._get_record(ticket_id))

    def list_pending(self) -> List[ApprovalTicket]:
        records = (
            self.db.query(ApprovalTicketRecord)
            .filter(ApprovalTicketRecord.status == "PENDING")
            .all()
        )
        return [_record_to_ticket(r) for r in records]

    def approve(self, ticket_id: str, operator: str, note: str = "") -> ApprovalTicket:
        record = self._get_record(ticket_id)
        if record.status != "PENDING":
            raise TicketNotPendingError(f"Ticket {ticket_id} is {record.status}, not PENDING.")
        record.status = "APPROVED"
        record.decided_by = operator
        record.decided_at = datetime.now(UTC)
        record.decision_note = note
        self.db.commit()
        self.db.refresh(record)
        return _record_to_ticket(record)

    def reject(self, ticket_id: str, operator: str, note: str = "") -> ApprovalTicket:
        record = self._get_record(ticket_id)
        if record.status != "PENDING":
            raise TicketNotPendingError(f"Ticket {ticket_id} is {record.status}, not PENDING.")
        record.status = "REJECTED"
        record.decided_by = operator
        record.decided_at = datetime.now(UTC)
        record.decision_note = note
        self.db.commit()
        self.db.refresh(record)
        return _record_to_ticket(record)
