"""Application service for durable complete-source observations."""

from datetime import UTC, datetime

from src.ingestion.models import CapturedSimulation, RevalidationObservation
from src.ingestion.repository import (
    IngestionLineageError,
    IngestionRepository,
    InvalidIngestionRunError,
    InvalidSnapshotError,
)


_REQUIRED_SOURCE_EVIDENCE = {
    "dataframe",
    "primary_key",
    "row_count",
    "schema_fingerprint",
    "dataset_fingerprint",
    "column_metadata",
}
_REDACTED_FAILURE_REASONS = {
    "SOURCE_VALIDATION_FAILED": "Complete source observation failed validation.",
    "SOURCE_READ_FAILED": "Complete source observation could not be read.",
}


class IngestionService:
    def __init__(self, repository: IngestionRepository):
        self.repository = repository

    def record_captured_simulation(
        self,
        *,
        source_dataset_id,
        source_read: dict,
        started_at: datetime,
        requested_by: str | None = None,
    ) -> CapturedSimulation:
        """Persist one complete read as a deduplicated snapshot plus new run."""
        snapshot = self._resolve_complete_snapshot(
            source_dataset_id=source_dataset_id,
            source_read=source_read,
        )
        run = self.repository.append_run(
            source_dataset_id=source_dataset_id,
            dataset_snapshot_id=snapshot.dataset_snapshot_id,
            purpose="SIMULATION",
            outcome="CAPTURED",
            requested_by=requested_by,
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )
        return CapturedSimulation(snapshot=snapshot, run=run)

    def record_live_revalidation(
        self,
        *,
        source_dataset_id,
        baseline_ingestion_run_id,
        source_read: dict,
        started_at: datetime,
        requested_by: str,
    ) -> RevalidationObservation:
        """Persist a complete fresh read before reporting MATCHED or DRIFTED."""
        baseline = self.repository.load_simulation_replay(
            baseline_ingestion_run_id
        )
        if str(baseline.run.source_dataset_id) != str(source_dataset_id):
            raise IngestionLineageError(
                "Live revalidation dataset does not match its simulation baseline."
            )
        snapshot = self._resolve_complete_snapshot(
            source_dataset_id=source_dataset_id,
            source_read=source_read,
        )
        mismatch_categories = tuple(
            category
            for category, baseline_value, fresh_value in (
                (
                    "PRIMARY_KEY",
                    baseline.snapshot.source_primary_key,
                    snapshot.source_primary_key,
                ),
                (
                    "ROW_COUNT",
                    baseline.snapshot.source_row_count,
                    snapshot.source_row_count,
                ),
                (
                    "SCHEMA_FINGERPRINT",
                    baseline.snapshot.source_schema_fingerprint,
                    snapshot.source_schema_fingerprint,
                ),
                (
                    "DATASET_FINGERPRINT",
                    baseline.snapshot.source_dataset_fingerprint,
                    snapshot.source_dataset_fingerprint,
                ),
            )
            if baseline_value != fresh_value
        )
        outcome = "DRIFTED" if mismatch_categories else "MATCHED"
        run = self.repository.append_run(
            source_dataset_id=source_dataset_id,
            dataset_snapshot_id=snapshot.dataset_snapshot_id,
            purpose="LIVE_REVALIDATION",
            outcome=outcome,
            baseline_ingestion_run_id=baseline_ingestion_run_id,
            requested_by=requested_by,
            started_at=started_at,
            completed_at=datetime.now(UTC),
        )
        return RevalidationObservation(
            snapshot=snapshot,
            run=run,
            mismatch_categories=mismatch_categories,
        )

    def record_failed_run(
        self,
        *,
        source_dataset_id,
        purpose: str,
        started_at: datetime,
        failure_code: str,
        baseline_ingestion_run_id=None,
        requested_by: str | None = None,
    ):
        """Append a terminal failure using only a locked redacted diagnostic."""
        failure_reason = _REDACTED_FAILURE_REASONS.get(failure_code)
        if failure_reason is None:
            raise InvalidIngestionRunError(
                "Unsupported ingestion failure category."
            )
        return self.repository.append_run(
            source_dataset_id=source_dataset_id,
            purpose=purpose,
            outcome="FAILED",
            baseline_ingestion_run_id=baseline_ingestion_run_id,
            requested_by=requested_by,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            failure_code=failure_code,
            failure_reason=failure_reason,
        )

    def _resolve_complete_snapshot(self, *, source_dataset_id, source_read: dict):
        if (
            not isinstance(source_read, dict)
            or not _REQUIRED_SOURCE_EVIDENCE.issubset(source_read)
        ):
            raise InvalidSnapshotError(
                "Complete source read is missing required snapshot evidence."
            )
        return self.repository.resolve_snapshot(
            source_dataset_id=source_dataset_id,
            dataframe=source_read["dataframe"],
            source_primary_key=source_read["primary_key"],
            source_row_count=source_read["row_count"],
            source_schema_fingerprint=source_read["schema_fingerprint"],
            source_dataset_fingerprint=source_read["dataset_fingerprint"],
            column_metadata=source_read["column_metadata"],
        )
