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
from typing import List, Optional

import pandas as pd
from sqlalchemy.orm import Session

from src.consultant.consultant import RepairPlan
from src.inspector import ObservedSchema, ColumnStats
from src.db.models import ApprovalTicketRecord
from src.governance.manifest import ConversionOutcomeMetadata
from src.governance.conversion_safety import require_safe_conversion_decision
from src.governance.dataset_codec import (
    DATASET_FORMAT_VERSION,
    decode_dataset as _dataset_from_json,
    encode_dataset as _dataset_to_json,
    sanitize_scalar as _sanitize_scalar,
)
from src.governance.approval import (
    ApprovalTicket,
    TicketNotFoundError,
    TicketNotPendingError,
)
from src.ingestion.repository import IngestionRepository, IngestionRepositoryError


class ApprovalReplayError(Exception):
    """A ticket has no single, internally consistent replay source."""


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


def _snapshot_backed_dataset(
    db: Session,
    *,
    source_ingestion_run_id,
    source_schema,
    source_table,
    source_primary_key,
    source_row_count,
    source_schema_fingerprint,
    source_dataset_fingerprint,
) -> pd.DataFrame:
    try:
        replay = IngestionRepository(db).load_simulation_replay(
            source_ingestion_run_id
        )
    except IngestionRepositoryError as exc:
        raise ApprovalReplayError(
            "Ticket ingestion lineage cannot produce a verified simulation replay."
        ) from exc

    mismatches = []
    expected_pairs = (
        ("source_schema", source_schema, replay.source_schema),
        ("source_table", source_table, replay.source_table),
        (
            "source_primary_key",
            source_primary_key,
            list(replay.snapshot.source_primary_key),
        ),
        ("source_row_count", source_row_count, replay.snapshot.source_row_count),
        (
            "source_schema_fingerprint",
            source_schema_fingerprint,
            replay.snapshot.source_schema_fingerprint,
        ),
        (
            "source_dataset_fingerprint",
            source_dataset_fingerprint,
            replay.snapshot.source_dataset_fingerprint,
        ),
    )
    for field, ticket_value, snapshot_value in expected_pairs:
        if ticket_value != snapshot_value:
            mismatches.append(field)
    if mismatches:
        raise ApprovalReplayError(
            "Ticket source provenance disagrees with its linked immutable snapshot: "
            + ", ".join(mismatches)
        )
    return replay.dataframe


def _record_to_ticket(record: ApprovalTicketRecord, db: Session) -> ApprovalTicket:
    has_embedded_dataset = record.target_dataset is not None
    has_ingestion_lineage = record.source_ingestion_run_id is not None
    if has_embedded_dataset == has_ingestion_lineage:
        raise ApprovalReplayError(
            "Ticket must have exactly one replay source: embedded dataset or "
            "captured simulation lineage."
        )
    target_dataset = (
        _dataset_from_json(record.target_dataset)
        if has_embedded_dataset
        else _snapshot_backed_dataset(
            db,
            source_ingestion_run_id=record.source_ingestion_run_id,
            source_schema=record.source_schema,
            source_table=record.source_table,
            source_primary_key=record.source_primary_key,
            source_row_count=record.source_row_count,
            source_schema_fingerprint=record.source_schema_fingerprint,
            source_dataset_fingerprint=record.source_dataset_fingerprint,
        )
    )
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
        target_dataset=target_dataset,
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
        source_ingestion_run_id=(
            str(record.source_ingestion_run_id)
            if record.source_ingestion_run_id is not None
            else None
        ),
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
        target_dataset: Optional[pd.DataFrame] = None,
        schema_version_id: str = None,
        source_schema: str = None,
        source_table: str = None,
        source_primary_key: list = None,
        source_row_count: int = None,
        source_schema_fingerprint: str = None,
        source_dataset_fingerprint: str = None,
        live_eligible: bool = False,
        conversion_decision: Optional[ConversionOutcomeMetadata] = None,
        source_ingestion_run_id: str = None,
    ) -> ApprovalTicket:
        has_embedded_dataset = target_dataset is not None
        has_ingestion_lineage = source_ingestion_run_id is not None
        if has_embedded_dataset == has_ingestion_lineage:
            raise ApprovalReplayError(
                "Submit exactly one replay source: target_dataset or "
                "source_ingestion_run_id."
            )
        if has_embedded_dataset:
            if not isinstance(target_dataset, pd.DataFrame):
                raise ApprovalReplayError("target_dataset must be a pandas DataFrame.")
            target_payload = _dataset_to_json(target_dataset)
            run_uuid = None
        else:
            try:
                run_uuid = uuid.UUID(str(source_ingestion_run_id))
            except (TypeError, ValueError) as exc:
                raise ApprovalReplayError(
                    "source_ingestion_run_id is not a valid UUID."
                ) from exc
            # Validate the run, snapshot payload, dataset identity, and copied
            # provenance before the ticket is allowed to reference them.
            _snapshot_backed_dataset(
                self.db,
                source_ingestion_run_id=run_uuid,
                source_schema=source_schema,
                source_table=source_table,
                source_primary_key=source_primary_key,
                source_row_count=source_row_count,
                source_schema_fingerprint=source_schema_fingerprint,
                source_dataset_fingerprint=source_dataset_fingerprint,
            )
            target_payload = None

        record = ApprovalTicketRecord(
            ticket_id=uuid.uuid4(),
            proposed_action=repair_plan.proposed_action,
            confidence=repair_plan.confidence,
            explanation=repair_plan.explanation,
            status="PENDING",
            created_at=datetime.now(UTC),
            observed_schema=_schema_to_json(observed_schema),
            gold_schema=_schema_to_json(gold_schema),
            target_dataset=target_payload,
            schema_version_id=uuid.UUID(schema_version_id) if schema_version_id else None,
            source_schema=source_schema,
            source_table=source_table,
            source_primary_key=source_primary_key,
            source_row_count=source_row_count,
            source_schema_fingerprint=source_schema_fingerprint,
            source_dataset_fingerprint=source_dataset_fingerprint,
            live_eligible=live_eligible,
            source_ingestion_run_id=run_uuid,
            conversion_decision=(
                conversion_decision.to_dict()
                if conversion_decision is not None
                else None
            ),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return _record_to_ticket(record, self.db)

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
        return _record_to_ticket(self._get_record(ticket_id), self.db)

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
        return _record_to_ticket(self._get_record_for_update(ticket_id), self.db)

    def list_pending(self) -> List[ApprovalTicket]:
        records = (
            self.db.query(ApprovalTicketRecord)
            .filter(ApprovalTicketRecord.status == "PENDING")
            .all()
        )
        return [_record_to_ticket(r, self.db) for r in records]

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

        ticket = _record_to_ticket(record, self.db)
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
        return _record_to_ticket(record, self.db)

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
        return _record_to_ticket(record, self.db)
