"""Application service for durable complete-source observations."""

from datetime import UTC, datetime

from src.ingestion.models import CapturedSimulation
from src.ingestion.repository import IngestionRepository, InvalidSnapshotError


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
        required = {
            "dataframe",
            "primary_key",
            "row_count",
            "schema_fingerprint",
            "dataset_fingerprint",
            "column_metadata",
        }
        if not isinstance(source_read, dict) or not required.issubset(source_read):
            raise InvalidSnapshotError(
                "Complete source read is missing required snapshot evidence."
            )
        snapshot = self.repository.resolve_snapshot(
            source_dataset_id=source_dataset_id,
            dataframe=source_read["dataframe"],
            source_primary_key=source_read["primary_key"],
            source_row_count=source_read["row_count"],
            source_schema_fingerprint=source_read["schema_fingerprint"],
            source_dataset_fingerprint=source_read["dataset_fingerprint"],
            column_metadata=source_read["column_metadata"],
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
