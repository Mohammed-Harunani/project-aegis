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
from typing import List, Optional

import pandas as pd
from sqlalchemy.orm import Session

from src.consultant.consultant import RepairPlan
from src.inspector import ObservedSchema, ColumnStats
from src.db.models import ApprovalTicketRecord
from src.governance.manifest import ConversionOutcomeMetadata
from src.governance.conversion_safety import require_safe_conversion_decision
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

DATASET_FORMAT_VERSION = 2


def _sanitize_scalar(value):
    """
    Tags non-JSON-native types so they survive the round trip through
    JSONB and come back as the exact original Python type -- not a
    lossy string, and never a Decimal silently turned into a float
    (imprecision on money values is exactly the kind of silent
    corruption a "Financial Data Integrity Guardian" shouldn't
    introduce itself). None already covers NaN/NaT/pd.NA by the time
    this runs -- see _dataset_to_json.

    Always writes in the CURRENT (v2) format -- there is no v1
    encoder, only a v1 DECODER (see _restore_scalar_v1), since nothing
    is ever newly written in the old format.

    Every dict/list value is wrapped in an explicit "raw_json"
    envelope, not just ones that happen to collide with the
    "__aegis_type__" key -- confirmed directly that a legitimate JSONB
    value containing that key as its own data (e.g.
    {"__aegis_type__": "decimal", "value": "10.50"} as genuine user
    content, not an Aegis tag) would otherwise be silently
    misinterpreted as an internal type tag on restore and corrupted
    into a Decimal. Wrapping every dict/list unconditionally removes
    the ambiguity entirely rather than trying to detect collisions.
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
    # Getting this wrong is exactly how an earlier version tagged
    # both pd.Timestamp and plain datetime.datetime as "timestamp",
    # silently collapsing the distinction between them.
    if isinstance(value, pd.Timestamp):
        return {"__aegis_type__": "pandas_timestamp", "value": value.isoformat()}
    if isinstance(value, datetime_module.datetime):
        return {"__aegis_type__": "python_datetime", "value": value.isoformat()}
    if isinstance(value, datetime_module.date):
        return {"__aegis_type__": "date", "value": value.isoformat()}
    if isinstance(value, uuid.UUID):
        return {"__aegis_type__": "uuid", "value": str(value)}
    if isinstance(value, (dict, list)):
        return {"__aegis_type__": "raw_json", "value": value}
    return value


def _restore_scalar_v2(value):
    """
    Current (format_version 2) decoder. Handles UUID and wraps every
    dict/list in an explicit "raw_json" envelope -- see
    _sanitize_scalar's docstring for why the wrapping is
    unconditional, not collision-detected.
    """
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
        if kind == "uuid":
            return uuid.UUID(raw)
        if kind == "raw_json":
            return raw
    return value


def _restore_scalar_v1(value):
    """
    LEGACY decoder for format_version 1 payloads only -- reproduces
    the ORIGINAL behavior exactly (no UUID tag, no raw_json wrapper),
    since that is genuinely what was written under that version and a
    v2-aware decoder would silently misinterpret it. Any dict/list
    written under v1 was NEVER wrapped, so it passes through here
    unchanged UNLESS it happens to be a dict containing a
    "__aegis_type__" key matching one of the tags v1 actually used
    (positive_infinity, negative_infinity, decimal, pandas_timestamp,
    python_datetime, date). That specific collision is a genuine,
    UNRESOLVABLE ambiguity inherent to the v1 format itself: a v1
    payload cannot distinguish "this is really an Aegis-tagged
    Decimal" from "this is a legitimate user JSONB object that happens
    to have those same keys" -- v1 never recorded which one it was.
    This decoder resolves that ambiguity the same way v1's own code
    always did (treat it as the tag), which is the closest available
    approximation, not a guarantee of correctness. There is no way to
    retroactively recover certainty for data written before this
    distinction existed; this is a known, documented limitation of
    reading v1 data, not something v2 can silently paper over.
    """
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
    format_version = payload.get("format_version")
    if format_version == 1:
        restore_scalar = _restore_scalar_v1
    elif format_version == 2:
        restore_scalar = _restore_scalar_v2
    else:
        raise ValueError(f"Unsupported dataset format_version: {format_version!r}")

    column_order = payload["column_order"]
    dtypes = payload["dtypes"]
    data = payload["data"]
    index_info = payload["index"]

    # Each column is built as its own object-dtype Series first (same
    # default 0..n-1 index for every one of them, so no alignment
    # mismatch when combined below), and ONLY THEN cast to its recorded
    # dtype. Doing this in one shot via pd.DataFrame(dict_of_lists)
    # instead -- as an earlier version did -- lets pandas re-infer
    # types across the whole frame at once, which silently promotes a
    # plain datetime.datetime column back to Timestamp before the
    # dtype ever gets reapplied. Verified directly: that was exactly
    # how "python_datetime" and "pandas_timestamp" collapsed together.
    columns = {}
    for col in column_order:
        restored_values = [restore_scalar(v) for v in data[col]]
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

    index_values = [restore_scalar(v) for v in index_info["values"]]
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
        source_schema=record.source_schema,
        source_table=record.source_table,
        source_primary_key=record.source_primary_key,
        source_row_count=record.source_row_count,
        source_schema_fingerprint=record.source_schema_fingerprint,
        source_dataset_fingerprint=record.source_dataset_fingerprint,
        live_eligible=bool(record.live_eligible),
        conversion_decision=(
            ConversionOutcomeMetadata.from_dict(record.conversion_decision)
            if record.conversion_decision is not None
            else None
        ),
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
        source_schema: str = None,
        source_table: str = None,
        source_primary_key: list = None,
        source_row_count: int = None,
        source_schema_fingerprint: str = None,
        source_dataset_fingerprint: str = None,
        live_eligible: bool = False,
        conversion_decision: Optional[ConversionOutcomeMetadata] = None,
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
            source_schema=source_schema,
            source_table=source_table,
            source_primary_key=source_primary_key,
            source_row_count=source_row_count,
            source_schema_fingerprint=source_schema_fingerprint,
            source_dataset_fingerprint=source_dataset_fingerprint,
            live_eligible=live_eligible,
            conversion_decision=(
                conversion_decision.to_dict()
                if conversion_decision is not None
                else None
            ),
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

    def lock_for_live_execution(self, ticket_id: str) -> ApprovalTicket:
        """
        Phase 2.5 -- row-locks the ticket for the remainder of the
        caller's transaction, reusing the same SELECT ... FOR UPDATE
        mechanism as approve()/reject(). This is what makes the
        check-current-state-then-create-a-live-execution-row sequence
        in app.py's execute_live() safe against two concurrent
        requests for the same ticket: the second blocks here until the
        first's transaction ends, then sees the now-existing PENDING/
        RUNNING/COMPLETED live_executions row and correctly gets
        rejected, instead of both racing past an unlocked check.
        """
        return _record_to_ticket(self._get_record_for_update(ticket_id))

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

        ticket = _record_to_ticket(record)
        require_safe_conversion_decision(
            ticket.repair_plan,
            ticket.target_dataset,
            ticket.conversion_decision,
        )

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
