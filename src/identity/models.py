"""Immutable domain projections for Phase 3.2 identity records."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True)
class SourceSystemIdentity:
    source_system_id: UUID
    system_key: str
    platform: str
    binding_version: int
    endpoint_binding_fingerprint: str
    created_at: datetime


@dataclass(frozen=True)
class SourceDatasetIdentity:
    source_dataset_id: UUID
    source_system_id: UUID
    source_schema: str
    source_table: str
    created_at: datetime


@dataclass(frozen=True)
class PublicationSystemIdentity:
    publication_system_id: UUID
    system_key: str
    platform: str
    binding_version: int
    endpoint_binding_fingerprint: str
    created_at: datetime


@dataclass(frozen=True)
class PublicationTargetIdentity:
    publication_target_id: UUID
    publication_system_id: UUID
    logical_target: str
    created_at: datetime
