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
import decimal
import datetime as datetime_module
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


def _sanitize_value(value):
    """
    Tags non-JSON-native types so they survive the round trip through
    JSONB and back out as the exact original Python type, rather than
    a lossy string or, worse, a Decimal silently turned into a float
    (float imprecision is exactly the kind of silent corruption a
    'Financial Data Integrity Guardian' shouldn't introduce itself).
    None already covers NaN/NaT/pd.NA by the time this runs -- see
    _dataset_to_json.
    """
    if value is None:
        return None
    if isinstance(value, decimal.Decimal):
        return {"__type__": "decimal", "value": str(value)}
    # Order matters: pd.Timestamp and datetime.datetime are both
    # subclasses of datetime.date, so the more specific checks must
    # come first or they'd never be reached.
    if isinstance(value, pd.Timestamp):
        return {"__type__": "timestamp", "value": value.isoformat()}
    if isinstance(value, datetime_module.datetime):
        return {"__type__": "timestamp", "value": value.isoformat()}
    if isinstance(value, datetime_module.date):
        return {"__type__": "date", "value": value.isoformat()}
    return value


def _restore_value(value):
    if isinstance(value, dict) and "__type__" in value:
        kind, raw = value["__type__"], value["value"]
        if kind == "decimal":
            return decimal.Decimal(raw)
        if kind == "timestamp":
            return pd.Timestamp(raw)
        if kind == "date":
            return datetime_module.date.fromisoformat(raw)
    return value


def _dataset_to_json(df: pd.DataFrame) -> dict:
    """
    Converts NaN/NaT/pd.NA to JSON null, boxes numpy scalar types
    (int64, float64, ...) as native Python types, and tags Decimal /
    Timestamp / datetime / date so _dataset_from_json can reconstruct
    them exactly. Verified directly (see conversation history): the
    unsanitized version fails on nulls (NaN is not valid JSON) AND
    separately on any of these four types (none of them are JSON-
    serializable at all, with or without nulls present).
    """
    clean = df.astype(object).where(pd.notnull(df), None)
    data = clean.to_dict(orient="list")
    return {col: [_sanitize_value(v) for v in values] for col, values in data.items()}


def _dataset_from_json(data: dict) -> pd.DataFrame:
    restored = {col: [_restore_value(v) for v in values] for col, values in data.items()}
    return pd.DataFrame(restored)


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
        """Plain read, no lock -- used by get() and list_pending()."""
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

    def _get_record_for_update(self, ticket_id: str) -> ApprovalTicketRecord:
        """
        Row-locked read (SELECT ... FOR UPDATE), used by approve() and
        reject(). Two concurrent decisions on the same ticket can no
        longer both read PENDING and both proceed: the second request
        blocks until the first transaction commits or rolls back, then
        sees the updated status and raises TicketNotPendingError
        instead of double-processing.
        """
        try:
            ticket_uuid = uuid.UUID(ticket_id)
        except ValueError:
            raise TicketNotFoundError(ticket_id)

        record = (
            self.db.query(ApprovalTicketRecord)
            .filter(ApprovalTicketRecord.ticket_id == ticket_uuid)
            .with_for_update()
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
        """
        Marks the ticket APPROVED but does NOT commit. This is meant
        to run inside the same transaction as Surgeon execution and
        manifest persistence (see app.py's approve_ticket endpoint) --
        if either of those fails, the caller rolls back and this
        status change is discarded along with them, leaving the
        ticket PENDING rather than stuck APPROVED with no manifest.
        The row lock from _get_record_for_update() is held until that
        transaction commits or rolls back.
        """
        record = self._get_record_for_update(ticket_id)
        if record.status != "PENDING":
            raise TicketNotPendingError(f"Ticket {ticket_id} is {record.status}, not PENDING.")
        record.status = "APPROVED"
        record.decided_by = operator
        record.decided_at = datetime.now(UTC)
        record.decision_note = note
        self.db.flush()
        return _record_to_ticket(record)

    def reject(self, ticket_id: str, operator: str, note: str = "") -> ApprovalTicket:
        record = self._get_record_for_update(ticket_id)
        if record.status != "PENDING":
            raise TicketNotPendingError(f"Ticket {ticket_id} is {record.status}, not PENDING.")
        record.status = "REJECTED"
        record.decided_by = operator
        record.decided_at = datetime.now(UTC)
        record.decision_note = note
        self.db.commit()
        self.db.refresh(record)
        return _record_to_ticket(record)
