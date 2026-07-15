"""
Aegis_LiveExecutionRepository
Phase 2.5 -- governance-database persistence for live_executions.
Tracks the STATE of a live-execution attempt; the actual target-table
mutation happens in a separate database via PostgresLiveWriter (see
Docs/phase2_5_live_execution_spec.md for why these are two connections,
not one literal atomic transaction).
"""

import uuid
from datetime import datetime, UTC
from typing import Optional

from sqlalchemy.orm import Session

from src.db.models import LiveExecutionRecord


class LiveExecutionNotFoundError(Exception):
    pass


class LiveExecutionInvalidStateError(Exception):
    """Raised when an operation doesn't match the execution's current status --
    e.g. rolling back something that isn't COMPLETED (only one rollback
    is allowed per live execution, and only of a successful one)."""


class LiveExecutionRepository:
    def __init__(self, db: Session):
        self.db = db

    def has_completed_execution(self, ticket_id: str) -> bool:
        return (
            self.db.query(LiveExecutionRecord)
            .filter(
                LiveExecutionRecord.ticket_id == uuid.UUID(ticket_id),
                LiveExecutionRecord.status == "COMPLETED",
            )
            .first()
            is not None
        )

    def has_unresolved_execution_for_target(self, target_schema: str, target_table: str) -> bool:
        return (
            self.db.query(LiveExecutionRecord)
            .filter(
                LiveExecutionRecord.target_schema == target_schema,
                LiveExecutionRecord.target_table == target_table,
                LiveExecutionRecord.status.in_(["PENDING", "RUNNING"]),
            )
            .first()
            is not None
        )

    def create_pending(
        self,
        ticket_id: str,
        sandbox_manifest_id: str,
        schema_version_id: str,
        target_schema: str,
        target_table: str,
        requested_by: str,
        original_row_count: int,
        final_row_count: int,
        risk_level: str,
        integrity_status: str,
    ) -> LiveExecutionRecord:
        record = LiveExecutionRecord(
            live_execution_id=uuid.uuid4(),
            ticket_id=uuid.UUID(ticket_id),
            sandbox_manifest_id=uuid.UUID(sandbox_manifest_id),
            schema_version_id=uuid.UUID(schema_version_id),
            target_schema=target_schema,
            target_table=target_table,
            backup_table=None,
            status="PENDING",
            requested_by=requested_by,
            started_at=datetime.now(UTC),
            original_row_count=original_row_count,
            final_row_count=final_row_count,
            risk_level=risk_level,
            integrity_status=integrity_status,
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def _get(self, live_execution_id) -> LiveExecutionRecord:
        if isinstance(live_execution_id, str):
            try:
                live_execution_id = uuid.UUID(live_execution_id)
            except ValueError:
                raise LiveExecutionNotFoundError(live_execution_id)
        record = (
            self.db.query(LiveExecutionRecord)
            .filter(LiveExecutionRecord.live_execution_id == live_execution_id)
            .one_or_none()
        )
        if record is None:
            raise LiveExecutionNotFoundError(str(live_execution_id))
        return record

    def get(self, live_execution_id: str) -> LiveExecutionRecord:
        return self._get(live_execution_id)

    def mark_running(self, live_execution_id) -> None:
        record = self._get(live_execution_id)
        record.status = "RUNNING"
        self.db.commit()

    def mark_completed(
        self, live_execution_id, backup_table: Optional[str], final_row_count: int
    ) -> None:
        record = self._get(live_execution_id)
        record.status = "COMPLETED"
        record.backup_table = backup_table
        record.final_row_count = final_row_count
        record.completed_at = datetime.now(UTC)
        self.db.commit()

    def mark_failed(self, live_execution_id, failure_reason: str) -> None:
        record = self._get(live_execution_id)
        record.status = "FAILED"
        record.failure_reason = failure_reason
        record.completed_at = datetime.now(UTC)
        self.db.commit()

    def mark_rolled_back(self, live_execution_id, rolled_back_by: str) -> LiveExecutionRecord:
        record = self._get(live_execution_id)
        if record.status != "COMPLETED":
            raise LiveExecutionInvalidStateError(
                f"Cannot roll back a live execution with status "
                f"{record.status!r} -- only a COMPLETED execution can be "
                f"rolled back, and only once."
            )
        record.status = "ROLLED_BACK"
        record.rolled_back_by = rolled_back_by
        record.rolled_back_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(record)
        return record
