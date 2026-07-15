"""
Aegis_SchemaRegistryRepository
Phase 2.4 -- Postgres-backed schema registry: register and retrieve
immutable, versioned Gold schema definitions.
"""

import uuid
from datetime import datetime, UTC
from typing import List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import GoldSchemaRecord, SchemaVersionRecord
from src.registry.schema_definition import (
    validate_schema_name,
    validate_schema_definition,
    compute_fingerprint,
)


class SchemaNotFoundError(Exception):
    """Raised when a schema_name has no gold_schemas row."""


class SchemaVersionNotFoundError(Exception):
    """Raised when a specific version_number doesn't exist for a schema."""


class DuplicateSchemaDefinitionError(Exception):
    """Raised when a submitted definition's fingerprint already exists for this schema."""


class SchemaRegistryRepository:
    def __init__(self, db: Session):
        self.db = db

    def _get_or_create_schema_locked(
        self, schema_name: str, description: Optional[str], created_by: str
    ) -> GoldSchemaRecord:
        """
        Returns the gold_schemas row for schema_name, row-locked for
        the rest of the caller's transaction -- creating it first if
        this is a brand new name. Locking (not just reading) is what
        makes version-number assignment in register_version() safe
        against two concurrent registrations for the same schema: both
        block here until the first transaction commits or rolls back.
        """
        record = (
            self.db.query(GoldSchemaRecord)
            .filter(GoldSchemaRecord.name == schema_name)
            .with_for_update()
            .one_or_none()
        )
        if record is not None:
            return record

        # Doesn't exist yet -- create it. Two concurrent requests for
        # the same brand-new name can both reach here; the UNIQUE
        # constraint on `name` means only one INSERT succeeds. A
        # SAVEPOINT (begin_nested) means the loser only rolls back
        # this insert attempt, not the whole transaction, then
        # re-queries -- now row-locked, since the winner's row exists.
        try:
            with self.db.begin_nested():
                record = GoldSchemaRecord(
                    schema_id=uuid.uuid4(),
                    name=schema_name,
                    description=description,
                    created_at=datetime.now(UTC),
                    created_by=created_by,
                )
                self.db.add(record)
                self.db.flush()
        except IntegrityError:
            record = (
                self.db.query(GoldSchemaRecord)
                .filter(GoldSchemaRecord.name == schema_name)
                .with_for_update()
                .one()
            )
        return record

    def register_version(
        self,
        schema_name: str,
        format_version: int,
        columns: List[dict],
        created_by: str,
        description: Optional[str] = None,
        change_summary: Optional[str] = None,
    ) -> SchemaVersionRecord:
        validate_schema_name(schema_name)
        validate_schema_definition(format_version, columns)
        fingerprint = compute_fingerprint(format_version, columns)

        schema = self._get_or_create_schema_locked(schema_name, description, created_by)

        existing = (
            self.db.query(SchemaVersionRecord)
            .filter(
                SchemaVersionRecord.schema_id == schema.schema_id,
                SchemaVersionRecord.fingerprint == fingerprint,
            )
            .one_or_none()
        )
        if existing is not None:
            raise DuplicateSchemaDefinitionError(
                f"An identical definition already exists for {schema_name!r} "
                f"as version {existing.version_number}."
            )

        current_max = (
            self.db.query(SchemaVersionRecord)
            .filter(SchemaVersionRecord.schema_id == schema.schema_id)
            .order_by(SchemaVersionRecord.version_number.desc())
            .first()
        )
        next_version = (current_max.version_number + 1) if current_max else 1

        version = SchemaVersionRecord(
            schema_version_id=uuid.uuid4(),
            schema_id=schema.schema_id,
            version_number=next_version,
            schema_definition={"format_version": format_version, "columns": columns},
            fingerprint=fingerprint,
            change_summary=change_summary,
            created_at=datetime.now(UTC),
            created_by=created_by,
        )
        self.db.add(version)
        self.db.commit()
        self.db.refresh(version)
        return version

    def get_schema_family(self, schema_name: str) -> GoldSchemaRecord:
        record = (
            self.db.query(GoldSchemaRecord)
            .filter(GoldSchemaRecord.name == schema_name)
            .one_or_none()
        )
        if record is None:
            raise SchemaNotFoundError(schema_name)
        return record

    def get_version(self, schema_name: str, version_number: int) -> SchemaVersionRecord:
        schema = self.get_schema_family(schema_name)
        version = (
            self.db.query(SchemaVersionRecord)
            .filter(
                SchemaVersionRecord.schema_id == schema.schema_id,
                SchemaVersionRecord.version_number == version_number,
            )
            .one_or_none()
        )
        if version is None:
            raise SchemaVersionNotFoundError(f"{schema_name} has no version {version_number}.")
        return version

    def get_latest(self, schema_name: str) -> SchemaVersionRecord:
        schema = self.get_schema_family(schema_name)
        version = (
            self.db.query(SchemaVersionRecord)
            .filter(SchemaVersionRecord.schema_id == schema.schema_id)
            .order_by(SchemaVersionRecord.version_number.desc())
            .first()
        )
        if version is None:
            raise SchemaVersionNotFoundError(f"{schema_name} has no registered versions.")
        return version

    def list_history(self, schema_name: str) -> List[SchemaVersionRecord]:
        schema = self.get_schema_family(schema_name)
        return (
            self.db.query(SchemaVersionRecord)
            .filter(SchemaVersionRecord.schema_id == schema.schema_id)
            .order_by(SchemaVersionRecord.version_number.desc())
            .all()
        )
