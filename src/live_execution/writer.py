"""
Aegis_PostgresLiveWriter
Phase 2.5 -- the dedicated write component for live execution. Not an
extension of the read-only connector (src/db/connector.py) -- a
separate, purpose-built component, per the locked architecture.

Correction-pass additions on top of the original create/write/
validate/backup/promote sequence:

- A durable execution-log marker table lives INSIDE the target
  database, in the same schema as the tables it tracks, written as
  part of the SAME transaction as the shadow/backup/promote sequence.
  This is what makes crash recovery possible: the governance database
  and the target database are two separate connections that cannot
  share one literal commit (see Docs/phase2_5_live_execution_spec.md).
  If the API crashes between the target transaction committing and
  the governance record being updated, this marker is the only
  reliable evidence of what actually happened on the target side.
- Only tables Aegis itself created/manages (proven via a prior PROMOTE
  marker for that exact target) can be replaced. An existing table
  with no such marker is refused outright -- Aegis's simplified
  shadow-table schema (column names + basic types only) would
  silently drop primary keys, foreign keys, indexes, defaults,
  triggers, grants, and everything else a real production table might
  depend on, and Postgres's object-identity-based dependency tracking
  means anything referencing the old table by its renamed-backup
  identity wouldn't automatically follow the rename anyway.
- Rollback refuses to proceed unless the target-side marker confirms
  this execution is still the most recent PROMOTE for that exact
  table -- otherwise a newer execution may have since superseded it,
  and rolling back the older one would destroy valid newer data.
"""

import uuid as uuid_module

import pandas as pd
from sqlalchemy import MetaData, Table, Column, text
from sqlalchemy import BigInteger, Integer, Float, Text, Boolean, TIMESTAMP
from sqlalchemy.engine import Engine

from src.live_execution.identifiers import (
    validate_identifier,
    shadow_table_name,
    backup_table_name,
)
from src.live_execution.dtype_mapping import pandas_dtype_to_postgres_type


_POSTGRES_TYPE_TO_SA = {
    "BIGINT": BigInteger,
    "INTEGER": Integer,
    "DOUBLE PRECISION": Float,
    "REAL": Float,
    "TEXT": Text,
    "BOOLEAN": Boolean,
    "TIMESTAMP": TIMESTAMP,
}

_EXECUTION_LOG_TABLE = "_aegis_execution_log"


class LiveWriteValidationError(Exception):
    """Post-write validation failed inside the transaction; it has been rolled back."""


class UnmanagedTargetTableError(Exception):
    """The target table already exists with no Aegis PROMOTE marker proving
    Aegis created/manages it -- refusing to silently take it over."""


class StaleRollbackError(Exception):
    """This execution is not the most recent PROMOTE for its target --
    something newer has superseded it, so rolling back would destroy
    valid, newer data."""


class PostgresLiveWriter:
    def __init__(self, engine: Engine):
        self.engine = engine

    def _sa_type_for(self, pandas_dtype: str):
        postgres_type_name = pandas_dtype_to_postgres_type(pandas_dtype)
        return _POSTGRES_TYPE_TO_SA[postgres_type_name]()

    def _table_exists(self, conn, schema: str, table: str) -> bool:
        return bool(conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = :table)"
            ),
            {"schema": schema, "table": table},
        ).scalar())

    def _ensure_execution_log_table(self, conn, target_schema: str) -> None:
        conn.execute(text(
            f'CREATE TABLE IF NOT EXISTS "{target_schema}"."{_EXECUTION_LOG_TABLE}" ('
            f'marker_id UUID PRIMARY KEY, '
            f'live_execution_id UUID NOT NULL, '
            f'target_table TEXT NOT NULL, '
            f'backup_table TEXT, '
            f'operation TEXT NOT NULL, '
            f'recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()'
            f')'
        ))

    def _record_marker(self, conn, target_schema, execution_id, target_table, backup_table, operation) -> None:
        conn.execute(
            text(
                f'INSERT INTO "{target_schema}"."{_EXECUTION_LOG_TABLE}" '
                f'(marker_id, live_execution_id, target_table, backup_table, operation) '
                f'VALUES (:marker_id, :execution_id, :table, :backup, :operation)'
            ),
            {
                "marker_id": str(uuid_module.uuid4()),
                "execution_id": str(execution_id),
                "table": target_table,
                "backup": backup_table,
                "operation": operation,
            },
        )

    def get_marker_for_execution(self, target_schema: str, live_execution_id) -> list:
        """
        Every marker row recorded for a specific execution_id (could be
        just a PROMOTE, or a PROMOTE followed later by a ROLLBACK).
        Used for crash-recovery reconciliation: durable target-side
        evidence of what actually happened, independent of whether the
        governance-database update after it succeeded.
        """
        validate_identifier(target_schema, "target schema")
        with self.engine.connect() as conn:
            if not self._table_exists(conn, target_schema, _EXECUTION_LOG_TABLE):
                return []
            rows = conn.execute(
                text(
                    f'SELECT target_table, backup_table, operation, recorded_at '
                    f'FROM "{target_schema}"."{_EXECUTION_LOG_TABLE}" '
                    f'WHERE live_execution_id = :id ORDER BY recorded_at'
                ),
                {"id": str(live_execution_id)},
            ).mappings().all()
            return [dict(r) for r in rows]

    def get_latest_promote_marker(self, target_schema: str, target_table: str):
        """
        The most recent PROMOTE-operation marker for this exact table
        name, or None. Used both for the managed-target check (an
        existing table with no PROMOTE marker at all is not provably
        Aegis-managed) and rollback's staleness check (is this
        execution still the most recent PROMOTE for this target).
        """
        validate_identifier(target_schema, "target schema")
        validate_identifier(target_table, "target table")
        with self.engine.connect() as conn:
            if not self._table_exists(conn, target_schema, _EXECUTION_LOG_TABLE):
                return None
            row = conn.execute(
                text(
                    f'SELECT live_execution_id, backup_table, recorded_at '
                    f'FROM "{target_schema}"."{_EXECUTION_LOG_TABLE}" '
                    f"WHERE target_table = :table AND operation = 'PROMOTE' "
                    f'ORDER BY recorded_at DESC LIMIT 1'
                ),
                {"table": target_table},
            ).mappings().first()
            return dict(row) if row else None

    def promote(
        self,
        target_schema: str,
        target_table: str,
        dataframe: pd.DataFrame,
        execution_id,
        expected_row_count: int,
    ) -> dict:
        """
        Returns {"backup_table": Optional[str], "final_row_count": int}
        on success. Raises on any failure -- by the time an exception
        propagates, the target transaction has already rolled back to
        its pre-execution state.
        """
        validate_identifier(target_schema, "target schema")
        validate_identifier(target_table, "target table")
        for col in dataframe.columns:
            validate_identifier(col, "column")

        shadow_name = shadow_table_name(target_table, execution_id)
        backup_name = backup_table_name(target_table, execution_id)

        with self.engine.begin() as conn:
            self._ensure_execution_log_table(conn, target_schema)

            target_exists = self._table_exists(conn, target_schema, target_table)

            if target_exists:
                has_prior_promote = conn.execute(
                    text(
                        f'SELECT EXISTS (SELECT 1 FROM "{target_schema}"."{_EXECUTION_LOG_TABLE}" '
                        f"WHERE target_table = :table AND operation = 'PROMOTE')"
                    ),
                    {"table": target_table},
                ).scalar()
                if not has_prior_promote:
                    raise UnmanagedTargetTableError(
                        f'"{target_schema}"."{target_table}" already exists with no '
                        f"Aegis PROMOTE marker proving Aegis created it. Refusing to "
                        f"replace a table that might be a real production table with "
                        f"constraints, indexes, triggers, or grants Aegis's simplified "
                        f"shadow-table schema would silently drop."
                    )

            # 1. Create the shadow table with explicit, validated column types.
            metadata = MetaData(schema=target_schema)
            columns = [
                Column(col, self._sa_type_for(str(dataframe[col].dtype)))
                for col in dataframe.columns
            ]
            Table(shadow_name, metadata, *columns).create(bind=conn)

            # 2. Write the corrected dataset into it.
            dataframe.to_sql(
                shadow_name, con=conn, schema=target_schema,
                if_exists="append", index=False,
            )

            # 3. Validate (gate 15): row count actually written matches
            # what sandbox execution already found.
            actual_count = conn.execute(
                text(f'SELECT COUNT(*) FROM "{target_schema}"."{shadow_name}"')
            ).scalar()
            if actual_count != expected_row_count:
                raise LiveWriteValidationError(
                    f"Shadow table row count {actual_count} does not match "
                    f"expected {expected_row_count} -- refusing to promote."
                )

            # 4. Back up the existing target, if one exists (already
            # proven Aegis-managed above, if it exists at all).
            if target_exists:
                conn.execute(text(
                    f'ALTER TABLE "{target_schema}"."{target_table}" '
                    f'RENAME TO "{backup_name}"'
                ))
            else:
                backup_name = None

            # 5. Promote: shadow becomes the target, in the same transaction.
            conn.execute(text(
                f'ALTER TABLE "{target_schema}"."{shadow_name}" '
                f'RENAME TO "{target_table}"'
            ))

            # 6. Durable target-side evidence, in the SAME transaction --
            # exists if and only if the promotion above actually committed.
            self._record_marker(conn, target_schema, execution_id, target_table, backup_name, "PROMOTE")

        return {"backup_table": backup_name, "final_row_count": expected_row_count}

    def rollback(self, target_schema: str, target_table: str, backup_table, execution_id) -> None:
        """
        Restores the previous target table from its backup. Refuses
        (StaleRollbackError) unless this execution is still the most
        recent PROMOTE marker for this exact target -- if a newer
        execution has since replaced it, rolling back this older one
        would destroy valid, newer data.
        """
        validate_identifier(target_schema, "target schema")
        validate_identifier(target_table, "target table")

        latest = self.get_latest_promote_marker(target_schema, target_table)
        if latest is None or str(latest["live_execution_id"]) != str(execution_id):
            raise StaleRollbackError(
                f'"{target_schema}"."{target_table}" has since been replaced by a '
                f"different live execution (or has no recorded Aegis history at all) "
                f"-- refusing to roll back an execution that is not the latest "
                f"PROMOTE for this target."
            )

        with self.engine.begin() as conn:
            self._ensure_execution_log_table(conn, target_schema)
            conn.execute(text(f'DROP TABLE IF EXISTS "{target_schema}"."{target_table}"'))
            if backup_table:
                validate_identifier(backup_table, "backup table")
                conn.execute(text(
                    f'ALTER TABLE "{target_schema}"."{backup_table}" '
                    f'RENAME TO "{target_table}"'
                ))
            self._record_marker(conn, target_schema, execution_id, target_table, None, "ROLLBACK")
