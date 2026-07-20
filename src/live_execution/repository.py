"""
Aegis_LiveExecutionRepository
Phase 2.5 final architecture -- governance-database persistence for
live_executions. Tracks the STATE of a live-execution attempt; the
actual publication happens in a separate database via
PostgresPublicationWriter (see Docs/phase2_5_live_execution_spec.md
for why these are two connections, not one literal atomic
transaction).

Design carried forward unchanged from the rename-based writer:
- create_running() creates the record ALREADY in RUNNING status, in
  one committed insert -- not a separate PENDING-then-RUNNING pair of
  commits, which left a crash window where the record could be
  stranded at PENDING forever.
- has_executed_live() covers ROLLED_BACK, not just COMPLETED --
  a rolled-back ticket must not execute live again.
- Reconciliation (reconcile_running / reconcile_rolling_back) is
  staleness-gated AND lock-proven: a timeout alone can only ever prove
  "a while has passed," never "nothing is still active" -- the
  session-level advisory lock (held by the caller for the whole
  execute-live/rollback flow) is what actually proves exclusivity.
- mark_rolling_back() records who requested the rollback and when
  BEFORE the risky target-side operation begins, so a crashed/orphaned
  rollback can still be reconciled without needing that information
  again.

Renamed to match the stable-view model: target_schema/target_table
are gone (replaced by logical_target); backup_table is gone (replaced
by physical_table/previous_physical_table).
"""

import os
import uuid
from datetime import datetime, UTC
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import LiveExecutionRecord
from src.live_execution.safety import LiveExecutionConflictError


class LiveExecutionNotFoundError(Exception):
    pass


class LiveExecutionInvalidStateError(Exception):
    """Raised when an operation doesn't match the execution's current status --
    e.g. rolling back something that isn't COMPLETED (only one rollback
    is allowed per live execution, and only of a successful one)."""


# Statuses that mean "this ticket has executed live in some form" --
# FAILED is deliberately excluded (retryable); everything else is
# one-shot per ticket, matching the partial unique index in the model.
_TICKET_ACTIVE_OR_DONE_STATUSES = ("PENDING", "RUNNING", "COMPLETED", "ROLLING_BACK", "ROLLED_BACK")

# Statuses that mean "this logical target is currently busy" --
# ROLLING_BACK is included: the target database is actively being
# mutated by that operation too.
_TARGET_IN_FLIGHT_STATUSES = ("PENDING", "RUNNING", "ROLLING_BACK")


def _stale_threshold_seconds() -> int:
    """
    How long a RUNNING or ROLLING_BACK record must sit unchanged
    before reconciliation is willing to act on it. Below this, a
    markerless record could just be a legitimately still-in-progress
    operation -- reconciling immediately would incorrectly fail
    something that might commit successfully moments later.
    """
    return int(os.environ.get("AEGIS_LIVE_EXECUTION_STALE_SECONDS", "300"))


class LiveExecutionRepository:
    def __init__(self, db: Session):
        self.db = db

    def has_executed_live(self, ticket_id: str) -> bool:
        return (
            self.db.query(LiveExecutionRecord)
            .filter(
                LiveExecutionRecord.ticket_id == uuid.UUID(ticket_id),
                LiveExecutionRecord.status.in_(_TICKET_ACTIVE_OR_DONE_STATUSES),
            )
            .first()
            is not None
        )

    def has_unresolved_execution_for_target(self, logical_target: str) -> bool:
        return (
            self.db.query(LiveExecutionRecord)
            .filter(
                LiveExecutionRecord.logical_target == logical_target,
                LiveExecutionRecord.status.in_(_TARGET_IN_FLIGHT_STATUSES),
            )
            .first()
            is not None
        )

    def is_latest_completed_execution_for_target(
        self, live_execution_id: str, logical_target: str
    ) -> bool:
        """
        Governance-side counterpart to the writer's own target-side
        marker check -- confirms no COMPLETED execution newer than
        this one exists for the same logical target. Both checks are
        required: this one guards against governance-recorded
        supersession, the target-side marker check guards against the
        published view having been changed by something the
        governance database doesn't know about.
        """
        latest = (
            self.db.query(LiveExecutionRecord)
            .filter(
                LiveExecutionRecord.logical_target == logical_target,
                LiveExecutionRecord.status.in_(["COMPLETED", "ROLLING_BACK", "ROLLED_BACK"]),
            )
            .order_by(LiveExecutionRecord.completed_at.desc())
            .first()
        )
        return latest is not None and str(latest.live_execution_id) == str(live_execution_id)

    def create_running(
        self,
        ticket_id: str,
        sandbox_manifest_id: str,
        schema_version_id: str,
        logical_target: str,
        requested_by: str,
        original_row_count: int,
        final_row_count: int,
        risk_level: str,
        integrity_status: str,
        source_schema: Optional[str] = None,
        source_table: Optional[str] = None,
        source_dataset_fingerprint: Optional[str] = None,
    ) -> LiveExecutionRecord:
        """
        Creates the record already RUNNING, in one committed insert.
        source_* fields are copied from the ticket's own persisted
        provenance for an independent audit trail on this specific
        execution.
        """
        record = LiveExecutionRecord(
            live_execution_id=uuid.uuid4(),
            ticket_id=uuid.UUID(ticket_id),
            sandbox_manifest_id=uuid.UUID(sandbox_manifest_id),
            schema_version_id=uuid.UUID(schema_version_id),
            logical_target=logical_target,
            physical_table=None,
            previous_physical_table=None,
            status="RUNNING",
            requested_by=requested_by,
            started_at=datetime.now(UTC),
            original_row_count=original_row_count,
            final_row_count=final_row_count,
            risk_level=risk_level,
            integrity_status=integrity_status,
            source_schema=source_schema,
            source_table=source_table,
            source_dataset_fingerprint=source_dataset_fingerprint,
        )
        self.db.add(record)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            raise LiveExecutionConflictError(
                "A live execution for this ticket or target is already active or "
                "has already run -- this is the database-enforced backstop against "
                "a race between two concurrent requests, not just the pre-flight check."
            )
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

    def _get_for_update(self, live_execution_id) -> LiveExecutionRecord:
        """Row-locked read, used by mark_rolling_back() -- without this,
        two concurrent rollback requests could both see COMPLETED
        before either commits the ROLLING_BACK transition."""
        if isinstance(live_execution_id, str):
            try:
                live_execution_id = uuid.UUID(live_execution_id)
            except ValueError:
                raise LiveExecutionNotFoundError(live_execution_id)
        record = (
            self.db.query(LiveExecutionRecord)
            .filter(LiveExecutionRecord.live_execution_id == live_execution_id)
            .with_for_update()
            .one_or_none()
        )
        if record is None:
            raise LiveExecutionNotFoundError(str(live_execution_id))
        return record

    def get(self, live_execution_id: str) -> LiveExecutionRecord:
        return self._get(live_execution_id)

    def mark_completed(
        self,
        live_execution_id,
        physical_table: Optional[str],
        previous_physical_table: Optional[str],
        final_row_count: int,
    ) -> None:
        record = self._get(live_execution_id)
        record.status = "COMPLETED"
        record.physical_table = physical_table
        record.previous_physical_table = previous_physical_table
        record.final_row_count = final_row_count
        record.completed_at = datetime.now(UTC)
        self.db.commit()

    def mark_failed(self, live_execution_id, failure_reason: str) -> None:
        record = self._get(live_execution_id)
        record.status = "FAILED"
        record.failure_reason = failure_reason
        record.completed_at = datetime.now(UTC)
        self.db.commit()

    def mark_rolling_back(self, live_execution_id, operator: str) -> LiveExecutionRecord:
        record = self._get_for_update(live_execution_id)
        if record.status != "COMPLETED":
            raise LiveExecutionInvalidStateError(
                f"Cannot begin rollback for a live execution with status "
                f"{record.status!r} -- only a COMPLETED execution can be rolled back."
            )
        record.status = "ROLLING_BACK"
        record.rolled_back_by = operator
        record.rollback_started_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(record)
        return record

    def mark_rolled_back(self, live_execution_id) -> LiveExecutionRecord:
        record = self._get(live_execution_id)
        if record.status != "ROLLING_BACK":
            raise LiveExecutionInvalidStateError(
                f"Cannot complete rollback for a live execution with status "
                f"{record.status!r} -- expected ROLLING_BACK."
            )
        record.status = "ROLLED_BACK"
        record.rolled_back_at = datetime.now(UTC)
        self.db.commit()
        self.db.refresh(record)
        return record

    def mark_outcome_unknown(self, live_execution_id, reason: str) -> None:
        """
        Records that an ambiguous exception occurred and the target-
        side marker couldn't even be checked (e.g. the connection
        itself is down) -- status is deliberately left unchanged
        (RUNNING or ROLLING_BACK). Neither FAILED (which is retryable
        and could create a duplicate publication if it actually did
        commit) nor COMPLETED (which would hide a real failure if it
        didn't) would be honest here.
        """
        record = self._get(live_execution_id)
        record.failure_reason = reason
        self.db.commit()

    def mark_rollback_failed(self, live_execution_id, failure_reason: str) -> None:
        """
        Left in ROLLING_BACK on failure rather than reverted to
        COMPLETED or forced to FAILED -- neither would be honest about
        a rollback that started but didn't finish cleanly. Stays there
        until reconciled (via the target-side marker) or manually
        investigated.
        """
        record = self._get(live_execution_id)
        record.failure_reason = failure_reason
        self.db.commit()

    def reconcile_running(self, live_execution_id, writer) -> LiveExecutionRecord:
        """
        Recovers a stuck RUNNING record's true state. Staleness is a
        cheap first filter; the actual proof comes from
        writer.determine_outcome_under_lock(), which acquires the same
        session-level lock genuine operations hold and checks the
        marker WHILE holding it.
        """
        record = self._get(live_execution_id)
        if record.status != "RUNNING":
            return record

        age_seconds = (datetime.now(UTC) - record.started_at).total_seconds()
        if age_seconds < _stale_threshold_seconds():
            return record

        outcome = writer.determine_outcome_under_lock(
            record.logical_target, record.live_execution_id, "PUBLISH"
        )

        if outcome == "active":
            return record
        if outcome == "unknown":
            record.failure_reason = (
                f"Reconciliation attempted after {age_seconds:.0f}s but the target-side "
                f"marker could not be checked -- left RUNNING for a later attempt."
            )
            self.db.commit()
            self.db.refresh(record)
            return record
        if outcome == "completed":
            markers = writer.get_marker_for_execution(record.live_execution_id)
            publish_marker = next((m for m in markers if m["operation"] == "PUBLISH"), None)
            record.status = "COMPLETED"
            record.physical_table = publish_marker["physical_table"] if publish_marker else None
            record.previous_physical_table = (
                publish_marker["previous_physical_table"] if publish_marker else None
            )
            record.completed_at = datetime.now(UTC)
        else:  # "not_committed"
            record.status = "FAILED"
            record.failure_reason = (
                f"Reconciled after {age_seconds:.0f}s: the target lock was free (proving "
                f"nothing is active) and no target-side PUBLISH marker exists -- treating "
                f"as failed rather than left stuck RUNNING."
            )
            record.completed_at = datetime.now(UTC)

        self.db.commit()
        self.db.refresh(record)
        return record

    def reconcile_rolling_back(self, live_execution_id, writer) -> LiveExecutionRecord:
        """
        Rollback counterpart to reconcile_running() -- same staleness
        pre-filter, same lock-proven outcome determination.
        """
        record = self._get(live_execution_id)
        if record.status != "ROLLING_BACK":
            return record

        reference_time = record.rollback_started_at or record.started_at
        age_seconds = (datetime.now(UTC) - reference_time).total_seconds()
        if age_seconds < _stale_threshold_seconds():
            return record

        outcome = writer.determine_outcome_under_lock(
            record.logical_target, record.live_execution_id, "ROLLBACK"
        )

        if outcome == "active":
            return record
        if outcome == "unknown":
            record.failure_reason = (
                f"Reconciliation attempted after {age_seconds:.0f}s but the target-side "
                f"marker could not be checked -- left ROLLING_BACK for a later attempt."
            )
            self.db.commit()
            self.db.refresh(record)
            return record
        if outcome == "completed":
            record.status = "ROLLED_BACK"
            record.rolled_back_at = datetime.now(UTC)
            self.db.commit()
            self.db.refresh(record)
        # "not_committed": leave it in ROLLING_BACK -- this needs
        # manual investigation, same as the synchronous failure path;
        # guessing wrong here (e.g. reverting to COMPLETED) could be
        # worse than leaving it for a human.

        return record
