"""Immutable domain projections for Phase 3.2 dataset ingestion."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional
from uuid import UUID

import pandas as pd


@dataclass(frozen=True)
class DatasetSnapshot:
    dataset_snapshot_id: UUID
    source_dataset_id: UUID
    provenance_fingerprint: str
    source_primary_key: tuple[str, ...]
    source_row_count: int
    source_schema_fingerprint: str
    source_dataset_fingerprint: str
    column_metadata: tuple[Mapping[str, Any], ...]
    payload_format_version: int
    created_at: datetime


@dataclass(frozen=True)
class IngestionRun:
    ingestion_run_id: UUID
    source_dataset_id: UUID
    dataset_snapshot_id: Optional[UUID]
    purpose: str
    outcome: str
    baseline_ingestion_run_id: Optional[UUID]
    requested_by: Optional[str]
    started_at: datetime
    completed_at: datetime
    failure_code: Optional[str]
    failure_reason: Optional[str]


@dataclass(frozen=True)
class SimulationReplay:
    run: IngestionRun
    snapshot: DatasetSnapshot
    source_system_id: UUID
    source_schema: str
    source_table: str
    dataframe: pd.DataFrame


@dataclass(frozen=True)
class CapturedSimulation:
    snapshot: DatasetSnapshot
    run: IngestionRun


@dataclass(frozen=True)
class RevalidationObservation:
    snapshot: DatasetSnapshot
    run: IngestionRun
    mismatch_categories: tuple[str, ...]

    @property
    def matched(self) -> bool:
        return self.run.outcome == "MATCHED"
