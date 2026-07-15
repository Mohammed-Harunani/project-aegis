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


# --- Dataset serialization -------------------------------------------
#
# A versioned envelope rather than a plain column->list dict, because
# the plain dict was verified to lose four separate things: (1) column
# order -- relies on JSON key order, which Postgres JSONB does NOT
# guarantee survives a round trip, it's a decomposed binary format, not
# text; (2) per-column dtype -- e.g. a nullable Int64 column with a
# null silently becomes float64/NaN through a plain dict round-trip;
# (3) the DataFrame's index -- name, dtype, and values all discarded,
# which matters because Surgeon's diagnostics reference rows by index
# label; (4) datetime.datetime and pandas.Timestamp were tagged
# identically, so a plain datetime.datetime silently came back as a
# Timestamp. All four confirmed directly and fixed here.

DATASET_FORMAT_VERSION = 1


def _sanitize_scalar(value):
    """
    Tags non-JSON-native types so they survive the round trip through
    JSONB and come back as the exact original Python type -- not a
    lossy string, and never a Decimal silently turned into a float
    (imprecision on money values is exactly the kind of silent
    corruption a "Financial Data Integrity Guardian" shouldn't
    introduce itself). None already covers NaN/NaT/pd.NA by the time
    this runs -- see _dataset_to_json.
    """
    if value is None:
        return None
    if isinstance(value, float):
        if value == float("inf"):
            return {"__aegis_type__": "positive_infinity"}
        if value == float("-inf"):
            return {"__aegis_type__": "negative_infinity"}
        return value
    if isinstance(value, decimal.Decimal):
        return {"__aegis_type__": "decimal", "value": str(value)}
    # Order matters: pd.Timestamp is a subclass of datetime.datetime,
    # which is a subclass of datetime.date -- most specific first.
    # Getting this wrong is exactly how the previous version tagged
    # both pd.Timestamp and plain datetime.datetime as "timestamp",
    # silently collapsing the distinction between them.
    if isinstance(value, pd.Timestamp):
        return {"__aegis_type__": "pandas_timestamp", "value": value.isoformat()}
    if isinstance(value, datetime_module.datetime):
        return {"__aegis_type__": "python_datetime", "value": value.isoformat()}
    if isinstance(value, datetime_module.date):
        return {"__aegis_type__": "date", "value": value.isoformat()}
    return value


def _restore_scalar(value):
    if isinstance(value, dict) and "__aegis_type__" in value:
        kind = value["__aegis_type__"]
        if kind == "positive_infinity":
            return float("inf")
        if kind == "negative_infinity":
            return float("-inf")
        raw = value.get("value")
        if kind == "decimal":
            return decimal.Decimal(raw)
        if kind == "pandas_timestamp":
            return pd.Timestamp(raw)
        if kind == "python_datetime":
            return datetime_module.datetime.fromisoformat(raw)
        if kind == "date":
            return datetime_module.date.fromisoformat(raw)
    return value


def _dataset_to_json(df: pd.DataFrame) -> dict:
    clean = df.astype(object).where(pd.notnull(df), None)
    column_order = list(df.columns)
    data = {col: [_sanitize_scalar(v) for v in clean[col].tolist()] for col in column_order}
    index_values = [
        None if (isinstance(v, float) and pd.isna(v)) else _sanitize_scalar(v)
        for v in df.index.tolist()
    ]
    return {
        "format_version": DATASET_FORMAT_VERSION,
        "column_order": column_order,
        "dtypes": {col: str(df[col].dtype) for col in column_order},
        "index": {
            "name": df.index.name,
            "dtype": str(df.index.dtype),
            "values": index_values,
        },
        "data": data,
    }


def _dataset_from_json(payload: dict) -> pd.DataFrame:
    if payload.get("format_version") != DATASET_FORMAT_VERSION:
        raise ValueError(f"Unsupported dataset format_version: {payload.get('format_version')!r}")

    column_order = payload["column_order"]
    dtypes = payload["dtypes"]
    data = payload["data"]
    index_info = payload["index"]

    # Each column is built as its own object-dtype Series first (same
    # default 0..n-1 index for every one of them, so no alignment
    # mismatch when combined below), and ONLY THEN cast to its recorded
    # dtype. Doing this in one shot via pd.DataFrame(dict_of_lists)
    # instead -- as the previous version did -- lets pandas re-infer
    # types across the whole frame at once, which silently promotes a
    # plain datetime.datetime column back to Timestamp before the
    # dtype ever gets reapplied. Verified directly: that was exactly
    # how "python_datetime" and "pandas_timestamp" collapsed together.
    columns = {}
    for col in column_order:
        restored_values = [_restore_scalar(v) for v in data[col]]
        series = pd.Series(restored_values, dtype=object)
        target_dtype = dtypes.get(col)
        if target_dtype and target_dtype != "object":
            try:
                series = series.astype(target_dtype)
            except (TypeError, ValueError):
                # A handful of dtype strings round-trip awkwardly
                # through astype() directly; leave the column as
                # reconstructed rather than raise -- the values
                # themselves are still correct even if the dtype
                # label isn't perfectly reapplied.
                pass
        columns[col] = series

    df = pd.DataFrame(columns, columns=column_order)

    index_values = [_restore_scalar(v) for v in index_info["values"]]
    idx = pd.Index(index_values, name=index_info["name"], dtype=object)
    index_dtype = index_info.get("dtype")
    if index_dtype and index_dtype != "object":
        try:
            idx = idx.astype(index_dtype)
        except (TypeError, ValueError):
            pass
    df.index = idx

    return df


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
        schema_version_id=str(record.schema_version_id) if record.schema_version_id else None,
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
        schema_version_id: str = None,
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
            schema_version_id=uuid.UUID(schema_version_id) if schema_version_id else None,
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
