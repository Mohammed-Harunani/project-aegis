"""
Aegis_Phase3_2_IdentityRepository

PostgreSQL-backed, fail-closed registration for immutable source systems,
source datasets, publication systems, and publication targets.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.models import (
    PublicationSystemRecord,
    PublicationTargetRecord,
    SourceDatasetRecord,
    SourceSystemRecord,
)
from src.identity.binding import (
    EndpointBinding,
    UnsafeDatabaseTopologyError,
    validate_endpoint_binding,
    validate_system_key,
)
from src.identity.models import (
    PublicationSystemIdentity,
    PublicationTargetIdentity,
    SourceDatasetIdentity,
    SourceSystemIdentity,
)
from src.live_execution.identifiers import validate_identifier


class IdentityNotFoundError(Exception):
    """A referenced immutable identity does not exist."""


class SystemBindingConflictError(Exception):
    """A known stable key was presented with a different endpoint binding."""


class EndpointBindingAlreadyRegisteredError(Exception):
    """The endpoint is already registered under a different stable key."""


class IdentityRepository:
    def __init__(self, db: Session):
        self.db = db

    def _transaction_lock(self, lock_key: str) -> None:
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": lock_key},
        )

    @staticmethod
    def _identity_uuid(value, label: str) -> uuid.UUID:
        try:
            return uuid.UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise IdentityNotFoundError(f"Unknown {label} identity.") from exc

    @staticmethod
    def _source_system(record: SourceSystemRecord) -> SourceSystemIdentity:
        return SourceSystemIdentity(
            source_system_id=record.source_system_id,
            system_key=record.system_key,
            platform=record.platform,
            binding_version=record.binding_version,
            endpoint_binding_fingerprint=record.endpoint_binding_fingerprint,
            created_at=record.created_at,
        )

    @staticmethod
    def _source_dataset(record: SourceDatasetRecord) -> SourceDatasetIdentity:
        return SourceDatasetIdentity(
            source_dataset_id=record.source_dataset_id,
            source_system_id=record.source_system_id,
            source_schema=record.source_schema,
            source_table=record.source_table,
            created_at=record.created_at,
        )

    @staticmethod
    def _publication_system(record: PublicationSystemRecord) -> PublicationSystemIdentity:
        return PublicationSystemIdentity(
            publication_system_id=record.publication_system_id,
            system_key=record.system_key,
            platform=record.platform,
            binding_version=record.binding_version,
            endpoint_binding_fingerprint=record.endpoint_binding_fingerprint,
            created_at=record.created_at,
        )

    @staticmethod
    def _publication_target(record: PublicationTargetRecord) -> PublicationTargetIdentity:
        return PublicationTargetIdentity(
            publication_target_id=record.publication_target_id,
            publication_system_id=record.publication_system_id,
            logical_target=record.logical_target,
            created_at=record.created_at,
        )

    def resolve_source_system(
        self, system_key: str, binding: EndpointBinding
    ) -> SourceSystemIdentity:
        system_key = validate_system_key(system_key)
        binding = validate_endpoint_binding(binding)
        try:
            self._transaction_lock(f"aegis:identity:source-key:{system_key}")
            self._transaction_lock(
                f"aegis:identity:endpoint:{binding.fingerprint}"
            )
            existing = (
                self.db.query(SourceSystemRecord)
                .filter(SourceSystemRecord.system_key == system_key)
                .with_for_update()
                .one_or_none()
            )
            if existing is not None:
                if existing.endpoint_binding_fingerprint != binding.fingerprint:
                    raise SystemBindingConflictError(
                        f"Source system key {system_key!r} is already bound "
                        "to a different endpoint."
                    )
                self.db.commit()
                return self._source_system(existing)

            duplicate = (
                self.db.query(SourceSystemRecord)
                .filter(
                    SourceSystemRecord.endpoint_binding_fingerprint == binding.fingerprint
                )
                .one_or_none()
            )
            if duplicate is not None:
                raise EndpointBindingAlreadyRegisteredError(
                    "Source endpoint binding is already registered under another system key."
                )
            publication = (
                self.db.query(PublicationSystemRecord)
                .filter(
                    PublicationSystemRecord.endpoint_binding_fingerprint
                    == binding.fingerprint
                )
                .one_or_none()
            )
            if publication is not None:
                raise UnsafeDatabaseTopologyError(
                    "Trusted source and publication target resolve to the same "
                    "database binding."
                )

            record = SourceSystemRecord(
                source_system_id=uuid.uuid4(),
                system_key=system_key,
                platform="POSTGRESQL",
                binding_version=binding.version,
                endpoint_binding_fingerprint=binding.fingerprint,
                created_at=datetime.now(UTC),
            )
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            return self._source_system(record)
        except Exception:
            self.db.rollback()
            raise

    def get_source_system(self, source_system_id) -> SourceSystemIdentity:
        source_system_uuid = self._identity_uuid(
            source_system_id, "source system"
        )
        record = self.db.get(SourceSystemRecord, source_system_uuid)
        if record is None:
            raise IdentityNotFoundError("Unknown source system identity.")
        return self._source_system(record)

    def verify_source_system_binding(
        self,
        source_system_id,
        system_key: str,
        binding: EndpointBinding,
    ) -> SourceSystemIdentity:
        """Verify live configuration still names the exact captured source."""
        system_key = validate_system_key(
            system_key, setting_name="AEGIS_SOURCE_SYSTEM_KEY"
        )
        binding = validate_endpoint_binding(binding)
        system = self.get_source_system(source_system_id)
        if system.system_key != system_key:
            raise SystemBindingConflictError(
                "Configured source system key does not match the approved "
                "simulation lineage."
            )
        if system.endpoint_binding_fingerprint != binding.fingerprint:
            raise SystemBindingConflictError(
                "Configured source endpoint binding does not match the approved "
                "simulation lineage."
            )
        return system

    def resolve_publication_system(
        self, system_key: str, binding: EndpointBinding
    ) -> PublicationSystemIdentity:
        system_key = validate_system_key(system_key)
        binding = validate_endpoint_binding(binding)
        try:
            self._transaction_lock(f"aegis:identity:publication-key:{system_key}")
            self._transaction_lock(
                f"aegis:identity:endpoint:{binding.fingerprint}"
            )
            existing = (
                self.db.query(PublicationSystemRecord)
                .filter(PublicationSystemRecord.system_key == system_key)
                .with_for_update()
                .one_or_none()
            )
            if existing is not None:
                if existing.endpoint_binding_fingerprint != binding.fingerprint:
                    raise SystemBindingConflictError(
                        f"Publication system key {system_key!r} is already bound "
                        "to a different endpoint."
                    )
                self.db.commit()
                return self._publication_system(existing)

            duplicate = (
                self.db.query(PublicationSystemRecord)
                .filter(
                    PublicationSystemRecord.endpoint_binding_fingerprint
                    == binding.fingerprint
                )
                .one_or_none()
            )
            if duplicate is not None:
                raise EndpointBindingAlreadyRegisteredError(
                    "Publication endpoint binding is already registered under another system key."
                )
            source = (
                self.db.query(SourceSystemRecord)
                .filter(SourceSystemRecord.endpoint_binding_fingerprint == binding.fingerprint)
                .one_or_none()
            )
            if source is not None:
                raise UnsafeDatabaseTopologyError(
                    "Trusted source and publication target resolve to the same "
                    "database binding."
                )

            record = PublicationSystemRecord(
                publication_system_id=uuid.uuid4(),
                system_key=system_key,
                platform="POSTGRESQL",
                binding_version=binding.version,
                endpoint_binding_fingerprint=binding.fingerprint,
                created_at=datetime.now(UTC),
            )
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            return self._publication_system(record)
        except Exception:
            self.db.rollback()
            raise

    def resolve_source_dataset(
        self, source_system_id, source_schema: str, source_table: str
    ) -> SourceDatasetIdentity:
        validate_identifier(source_schema, "source schema")
        validate_identifier(source_table, "source table")
        try:
            source_system_uuid = uuid.UUID(str(source_system_id))
        except (TypeError, ValueError) as exc:
            raise IdentityNotFoundError(str(source_system_id)) from exc

        try:
            self._transaction_lock(
                "aegis:identity:dataset:"
                f"{source_system_uuid}:{source_schema}:{source_table}"
            )
            source_system = self.db.get(SourceSystemRecord, source_system_uuid)
            if source_system is None:
                raise IdentityNotFoundError(str(source_system_id))
            existing = (
                self.db.query(SourceDatasetRecord)
                .filter(
                    SourceDatasetRecord.source_system_id == source_system_uuid,
                    SourceDatasetRecord.source_schema == source_schema,
                    SourceDatasetRecord.source_table == source_table,
                )
                .one_or_none()
            )
            if existing is not None:
                self.db.commit()
                return self._source_dataset(existing)

            record = SourceDatasetRecord(
                source_dataset_id=uuid.uuid4(),
                source_system_id=source_system_uuid,
                source_schema=source_schema,
                source_table=source_table,
                created_at=datetime.now(UTC),
            )
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            return self._source_dataset(record)
        except Exception:
            self.db.rollback()
            raise

    def get_source_dataset(self, source_dataset_id) -> SourceDatasetIdentity:
        source_dataset_uuid = self._identity_uuid(
            source_dataset_id, "source dataset"
        )
        record = self.db.get(SourceDatasetRecord, source_dataset_uuid)
        if record is None:
            raise IdentityNotFoundError("Unknown source dataset identity.")
        return self._source_dataset(record)

    def resolve_publication_target(
        self, publication_system_id, logical_target: str
    ) -> PublicationTargetIdentity:
        validate_identifier(logical_target, "logical target")
        try:
            publication_system_uuid = uuid.UUID(str(publication_system_id))
        except (TypeError, ValueError) as exc:
            raise IdentityNotFoundError(str(publication_system_id)) from exc

        try:
            self._transaction_lock(
                "aegis:identity:publication-target:"
                f"{publication_system_uuid}:{logical_target}"
            )
            publication_system = self.db.get(
                PublicationSystemRecord, publication_system_uuid
            )
            if publication_system is None:
                raise IdentityNotFoundError(str(publication_system_id))
            existing = (
                self.db.query(PublicationTargetRecord)
                .filter(
                    PublicationTargetRecord.publication_system_id
                    == publication_system_uuid,
                    PublicationTargetRecord.logical_target == logical_target,
                )
                .one_or_none()
            )
            if existing is not None:
                self.db.commit()
                return self._publication_target(existing)

            record = PublicationTargetRecord(
                publication_target_id=uuid.uuid4(),
                publication_system_id=publication_system_uuid,
                logical_target=logical_target,
                created_at=datetime.now(UTC),
            )
            self.db.add(record)
            self.db.commit()
            self.db.refresh(record)
            return self._publication_target(record)
        except Exception:
            self.db.rollback()
            raise
