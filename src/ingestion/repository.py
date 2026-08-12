"""
Aegis_Phase3_2_IngestionRepository

Dataset-scoped immutable snapshot deduplication, append-only terminal
ingestion runs, deterministic inspection, and validated replay loading.
The caller owns the surrounding transaction; repository writes flush but
never commit, so snapshot/run/ticket lineage can become durable atomically.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.models import (
    DatasetSnapshotRecord,
    IngestionRunRecord,
    SourceDatasetRecord,
)
from src.governance.dataset_codec import (
    DATASET_FORMAT_VERSION,
    decode_dataset,
    encode_dataset,
)
from src.ingestion.models import DatasetSnapshot, IngestionRun, SimulationReplay
from src.live_execution.identifiers import InvalidIdentifierError, validate_identifier
from src.live_execution.output_fingerprint import compute_dataframe_fingerprint


SNAPSHOT_PROVENANCE_FORMAT_VERSION = 1
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FAILURE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class IngestionRepositoryError(Exception):
    """Base class for persistence-boundary ingestion failures."""


class InvalidSnapshotError(IngestionRepositoryError):
    """Snapshot evidence is incomplete, inconsistent, or malformed."""


class SnapshotConflictError(IngestionRepositoryError):
    """One provenance key resolved to non-identical persisted evidence."""


class DatasetSnapshotNotFoundError(IngestionRepositoryError):
    """The requested immutable snapshot does not exist."""


class InvalidIngestionRunError(IngestionRepositoryError):
    """A terminal ingestion run violates its purpose/outcome contract."""


class IngestionRunNotFoundError(IngestionRepositoryError):
    """The requested ingestion run does not exist."""


class IngestionLineageError(IngestionRepositoryError):
    """Persisted run, snapshot, dataset, or replay evidence disagrees."""


def _validated_uuid(value, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise IngestionRepositoryError(f"{label} is not a valid UUID.") from exc


def _validated_fingerprint(value: str, label: str) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_PATTERN.fullmatch(value):
        raise InvalidSnapshotError(f"{label} must be a lowercase SHA-256 value.")
    return value


def _validated_primary_key(value) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise InvalidSnapshotError(
            "source_primary_key must be a non-empty ordered list."
        )
    primary_key = list(value)
    if len(set(primary_key)) != len(primary_key):
        raise InvalidSnapshotError("source_primary_key contains duplicate columns.")
    try:
        for column in primary_key:
            validate_identifier(column, "primary key column")
    except InvalidIdentifierError as exc:
        raise InvalidSnapshotError(str(exc)) from exc
    return primary_key


def compute_snapshot_provenance_fingerprint(
    *,
    source_primary_key,
    source_row_count: int,
    source_schema_fingerprint: str,
    source_dataset_fingerprint: str,
) -> str:
    """Compute the approved dataset-scoped snapshot provenance key."""
    primary_key = _validated_primary_key(source_primary_key)
    if (
        isinstance(source_row_count, bool)
        or not isinstance(source_row_count, int)
        or source_row_count < 0
    ):
        raise InvalidSnapshotError(
            "source_row_count must be a non-negative integer."
        )
    schema_fingerprint = _validated_fingerprint(
        source_schema_fingerprint, "source_schema_fingerprint"
    )
    dataset_fingerprint = _validated_fingerprint(
        source_dataset_fingerprint, "source_dataset_fingerprint"
    )
    canonical = {
        "format_version": SNAPSHOT_PROVENANCE_FORMAT_VERSION,
        "source_dataset_fingerprint": dataset_fingerprint,
        "source_primary_key": primary_key,
        "source_row_count": source_row_count,
        "source_schema_fingerprint": schema_fingerprint,
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class IngestionRepository:
    def __init__(self, db: Session):
        self.db = db

    def _transaction_lock(self, lock_key: str) -> None:
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _snapshot(record: DatasetSnapshotRecord) -> DatasetSnapshot:
        return DatasetSnapshot(
            dataset_snapshot_id=record.dataset_snapshot_id,
            source_dataset_id=record.source_dataset_id,
            provenance_fingerprint=record.provenance_fingerprint,
            source_primary_key=tuple(record.source_primary_key),
            source_row_count=record.source_row_count,
            source_schema_fingerprint=record.source_schema_fingerprint,
            source_dataset_fingerprint=record.source_dataset_fingerprint,
            column_metadata=tuple(copy.deepcopy(record.column_metadata)),
            payload_format_version=record.payload_format_version,
            created_at=record.created_at,
        )

    @staticmethod
    def _run(record: IngestionRunRecord) -> IngestionRun:
        return IngestionRun(
            ingestion_run_id=record.ingestion_run_id,
            source_dataset_id=record.source_dataset_id,
            dataset_snapshot_id=record.dataset_snapshot_id,
            purpose=record.purpose,
            outcome=record.outcome,
            baseline_ingestion_run_id=record.baseline_ingestion_run_id,
            requested_by=record.requested_by,
            started_at=record.started_at,
            completed_at=record.completed_at,
            failure_code=record.failure_code,
            failure_reason=record.failure_reason,
        )

    def _source_dataset_record(self, source_dataset_id) -> SourceDatasetRecord:
        dataset_uuid = _validated_uuid(source_dataset_id, "source_dataset_id")
        record = self.db.get(SourceDatasetRecord, dataset_uuid)
        if record is None:
            raise IngestionLineageError("Referenced source dataset does not exist.")
        return record

    def _snapshot_record(self, dataset_snapshot_id) -> DatasetSnapshotRecord:
        snapshot_uuid = _validated_uuid(
            dataset_snapshot_id, "dataset_snapshot_id"
        )
        record = self.db.get(DatasetSnapshotRecord, snapshot_uuid)
        if record is None:
            raise DatasetSnapshotNotFoundError(str(snapshot_uuid))
        return record

    def _run_record(self, ingestion_run_id) -> IngestionRunRecord:
        run_uuid = _validated_uuid(ingestion_run_id, "ingestion_run_id")
        record = self.db.get(IngestionRunRecord, run_uuid)
        if record is None:
            raise IngestionRunNotFoundError(str(run_uuid))
        return record

    def resolve_snapshot(
        self,
        *,
        source_dataset_id,
        dataframe: pd.DataFrame,
        source_primary_key,
        source_row_count: int,
        source_schema_fingerprint: str,
        source_dataset_fingerprint: str,
        column_metadata: list[dict],
    ) -> DatasetSnapshot:
        """Reuse an identical dataset-scoped snapshot or append a new one."""
        dataset = self._source_dataset_record(source_dataset_id)
        if not isinstance(dataframe, pd.DataFrame):
            raise InvalidSnapshotError("Snapshot payload must be a pandas DataFrame.")
        if source_row_count != len(dataframe):
            raise InvalidSnapshotError(
                "source_row_count does not match the complete snapshot payload."
            )
        primary_key = _validated_primary_key(source_primary_key)
        missing_keys = [
            column for column in primary_key if column not in dataframe.columns
        ]
        if missing_keys:
            raise InvalidSnapshotError(
                "Snapshot primary-key columns are absent from the payload."
            )
        if not isinstance(column_metadata, list) or not column_metadata:
            raise InvalidSnapshotError(
                "column_metadata must be a non-empty ordered list."
            )
        if not all(isinstance(item, dict) for item in column_metadata):
            raise InvalidSnapshotError("Every column_metadata item must be an object.")
        metadata_names = [item.get("column_name") for item in column_metadata]
        if metadata_names != list(dataframe.columns):
            raise InvalidSnapshotError(
                "Ordered column_metadata does not match the snapshot columns."
            )

        schema_fingerprint = _validated_fingerprint(
            source_schema_fingerprint, "source_schema_fingerprint"
        )
        dataset_fingerprint = _validated_fingerprint(
            source_dataset_fingerprint, "source_dataset_fingerprint"
        )
        if compute_dataframe_fingerprint(dataframe) != dataset_fingerprint:
            raise InvalidSnapshotError(
                "source_dataset_fingerprint does not match the snapshot payload."
            )
        provenance_fingerprint = compute_snapshot_provenance_fingerprint(
            source_primary_key=primary_key,
            source_row_count=source_row_count,
            source_schema_fingerprint=schema_fingerprint,
            source_dataset_fingerprint=dataset_fingerprint,
        )
        payload = encode_dataset(dataframe)

        self._transaction_lock(
            "aegis:ingestion:snapshot:"
            f"{dataset.source_dataset_id}:{provenance_fingerprint}"
        )
        existing = (
            self.db.query(DatasetSnapshotRecord)
            .filter(
                DatasetSnapshotRecord.source_dataset_id
                == dataset.source_dataset_id,
                DatasetSnapshotRecord.provenance_fingerprint
                == provenance_fingerprint,
            )
            .one_or_none()
        )
        if existing is not None:
            if not (
                existing.source_primary_key == primary_key
                and existing.source_row_count == source_row_count
                and existing.source_schema_fingerprint == schema_fingerprint
                and existing.source_dataset_fingerprint == dataset_fingerprint
                and existing.column_metadata == column_metadata
                and existing.payload_format_version == DATASET_FORMAT_VERSION
                and existing.snapshot_payload == payload
            ):
                raise SnapshotConflictError(
                    "Existing snapshot provenance resolves to different evidence."
                )
            return self._snapshot(existing)

        record = DatasetSnapshotRecord(
            dataset_snapshot_id=uuid.uuid4(),
            source_dataset_id=dataset.source_dataset_id,
            provenance_fingerprint=provenance_fingerprint,
            source_primary_key=primary_key,
            source_row_count=source_row_count,
            source_schema_fingerprint=schema_fingerprint,
            source_dataset_fingerprint=dataset_fingerprint,
            column_metadata=copy.deepcopy(column_metadata),
            payload_format_version=DATASET_FORMAT_VERSION,
            snapshot_payload=payload,
            created_at=datetime.now(UTC),
        )
        self.db.add(record)
        self.db.flush()
        return self._snapshot(record)

    def append_run(
        self,
        *,
        source_dataset_id,
        purpose: str,
        outcome: str,
        started_at: datetime,
        completed_at: datetime,
        dataset_snapshot_id=None,
        baseline_ingestion_run_id=None,
        requested_by: Optional[str] = None,
        failure_code: Optional[str] = None,
        failure_reason: Optional[str] = None,
    ) -> IngestionRun:
        """Append one already-terminal observation; runs are never deduplicated."""
        dataset = self._source_dataset_record(source_dataset_id)
        if purpose not in {"SIMULATION", "LIVE_REVALIDATION"}:
            raise InvalidIngestionRunError("Unsupported ingestion-run purpose.")
        allowed_outcomes = {
            "SIMULATION": {"CAPTURED", "FAILED"},
            "LIVE_REVALIDATION": {"MATCHED", "DRIFTED", "FAILED"},
        }
        if outcome not in allowed_outcomes[purpose]:
            raise InvalidIngestionRunError(
                "Outcome is not valid for the ingestion-run purpose."
            )
        for value, label in (
            (started_at, "started_at"),
            (completed_at, "completed_at"),
        ):
            if not isinstance(value, datetime) or value.utcoffset() is None:
                raise InvalidIngestionRunError(
                    f"{label} must be a timezone-aware datetime."
                )
        if completed_at < started_at:
            raise InvalidIngestionRunError("completed_at cannot precede started_at.")

        snapshot = None
        if dataset_snapshot_id is not None:
            snapshot = self._snapshot_record(dataset_snapshot_id)
            if snapshot.source_dataset_id != dataset.source_dataset_id:
                raise IngestionLineageError(
                    "Ingestion run and snapshot belong to different datasets."
                )

        baseline = None
        if baseline_ingestion_run_id is not None:
            baseline = self._run_record(baseline_ingestion_run_id)
            if baseline.source_dataset_id != dataset.source_dataset_id:
                raise IngestionLineageError(
                    "Revalidation and baseline belong to different datasets."
                )
            if baseline.purpose != "SIMULATION" or baseline.outcome != "CAPTURED":
                raise IngestionLineageError(
                    "Revalidation baseline must be a captured simulation run."
                )

        if purpose == "SIMULATION":
            if baseline is not None:
                raise InvalidIngestionRunError(
                    "Simulation runs cannot reference a baseline."
                )
            if outcome == "CAPTURED" and snapshot is None:
                raise InvalidIngestionRunError(
                    "Captured simulation runs require a snapshot."
                )
        else:
            if baseline is None:
                raise InvalidIngestionRunError(
                    "Live revalidation runs require a simulation baseline."
                )
            if outcome in {"MATCHED", "DRIFTED"} and snapshot is None:
                raise InvalidIngestionRunError(
                    "Completed revalidation runs require a snapshot."
                )

        if outcome == "FAILED":
            if (
                not isinstance(failure_code, str)
                or not _FAILURE_CODE_PATTERN.fullmatch(failure_code)
            ):
                raise InvalidIngestionRunError(
                    "Failed runs require a stable uppercase failure_code."
                )
            if failure_reason is not None and (
                not isinstance(failure_reason, str)
                or not failure_reason
                or len(failure_reason) > 1000
            ):
                raise InvalidIngestionRunError(
                    "failure_reason must be a non-empty redacted string up to "
                    "1000 characters."
                )
        elif failure_code is not None or failure_reason is not None:
            raise InvalidIngestionRunError(
                "Successful runs cannot persist failure diagnostics."
            )

        if requested_by is not None:
            if not isinstance(requested_by, str) or not requested_by.strip():
                raise InvalidIngestionRunError(
                    "requested_by must be meaningful when set."
                )
            requested_by = requested_by.strip()

        record = IngestionRunRecord(
            ingestion_run_id=uuid.uuid4(),
            source_dataset_id=dataset.source_dataset_id,
            dataset_snapshot_id=(
                snapshot.dataset_snapshot_id if snapshot is not None else None
            ),
            purpose=purpose,
            outcome=outcome,
            baseline_ingestion_run_id=(
                baseline.ingestion_run_id if baseline is not None else None
            ),
            requested_by=requested_by,
            started_at=started_at,
            completed_at=completed_at,
            failure_code=failure_code,
            failure_reason=failure_reason,
        )
        self.db.add(record)
        self.db.flush()
        return self._run(record)

    def get_snapshot(self, dataset_snapshot_id) -> DatasetSnapshot:
        return self._snapshot(self._snapshot_record(dataset_snapshot_id))

    def load_snapshot_dataframe(self, dataset_snapshot_id) -> pd.DataFrame:
        record = self._snapshot_record(dataset_snapshot_id)
        if record.payload_format_version != record.snapshot_payload.get(
            "format_version"
        ):
            raise IngestionLineageError(
                "Snapshot payload format disagrees with its persisted metadata."
            )
        dataframe = decode_dataset(record.snapshot_payload)
        if len(dataframe) != record.source_row_count:
            raise IngestionLineageError(
                "Snapshot replay row count disagrees with persisted provenance."
            )
        if (
            compute_dataframe_fingerprint(dataframe)
            != record.source_dataset_fingerprint
        ):
            raise IngestionLineageError(
                "Snapshot replay fingerprint disagrees with persisted provenance."
            )
        return dataframe

    def get_run(self, ingestion_run_id) -> IngestionRun:
        return self._run(self._run_record(ingestion_run_id))

    def list_runs(self, source_dataset_id) -> list[IngestionRun]:
        dataset = self._source_dataset_record(source_dataset_id)
        records = (
            self.db.query(IngestionRunRecord)
            .filter(
                IngestionRunRecord.source_dataset_id == dataset.source_dataset_id
            )
            .order_by(
                IngestionRunRecord.completed_at.asc(),
                IngestionRunRecord.ingestion_run_id.asc(),
            )
            .all()
        )
        return [self._run(record) for record in records]

    def load_simulation_replay(self, ingestion_run_id) -> SimulationReplay:
        run_record = self._run_record(ingestion_run_id)
        if run_record.purpose != "SIMULATION" or run_record.outcome != "CAPTURED":
            raise IngestionLineageError(
                "Approval replay requires a captured simulation run."
            )
        if run_record.dataset_snapshot_id is None:
            raise IngestionLineageError("Captured simulation run has no snapshot.")
        snapshot_record = self._snapshot_record(run_record.dataset_snapshot_id)
        if snapshot_record.source_dataset_id != run_record.source_dataset_id:
            raise IngestionLineageError(
                "Simulation run and snapshot belong to different datasets."
            )
        dataset_record = self._source_dataset_record(run_record.source_dataset_id)
        dataframe = self.load_snapshot_dataframe(snapshot_record.dataset_snapshot_id)
        return SimulationReplay(
            run=self._run(run_record),
            snapshot=self._snapshot(snapshot_record),
            source_system_id=dataset_record.source_system_id,
            source_schema=dataset_record.source_schema,
            source_table=dataset_record.source_table,
            dataframe=dataframe,
        )
