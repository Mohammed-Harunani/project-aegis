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
- Table IDENTITY, not just name, proves Aegis manages a target. A
  historical PROMOTE marker matching the table NAME is not enough --
  if the table were dropped and something unrelated recreated with
  the same name, a name-only check would wrongly treat it as
  Aegis-managed. Each PROMOTE marker records the table's Postgres OID
  (via to_regclass()) at the moment of promotion; both the
  managed-target check and rollback's staleness check compare the
  CURRENT table's OID against what's recorded, not just its name.
- Rollback refuses to proceed unless the target-side marker confirms
  this execution is still the most recent PROMOTE for that exact
  table (by OID, not just name) -- otherwise a newer execution (or an
  external change) may have since superseded it.
"""

import hashlib
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


def _advisory_lock_key(target_schema: str, target_table: str) -> int:
    """
    Deterministic signed 64-bit key for Postgres advisory locks (bigint
    is signed 64-bit), derived from the fully-qualified target name.
    Confirmed via Postgres docs: pg_advisory_xact_lock is transaction-
    scoped and releases automatically on commit OR rollback, which is
    exactly what makes this usable as a liveness proof -- if a process
    crashes mid-operation, its connection drops, its transaction never
    commits, and the lock is released, so a LATER attempt to acquire it
    succeeding proves nothing is currently active for this target.
    """
    digest = hashlib.sha256(f"{target_schema}.{target_table}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class LiveWriteValidationError(Exception):
    """Post-write validation failed inside the transaction; it has been rolled back."""


class UnmanagedTargetTableError(Exception):
    """The target table already exists with no matching Aegis PROMOTE
    marker (by current table identity, not just name) proving Aegis
    created/manages it -- refusing to silently take it over."""


class StaleRollbackError(Exception):
    """This execution is not the most recent PROMOTE for its target (by
    current table identity) -- something newer has superseded it, so
    rolling back would destroy valid, newer data."""


class TargetLockUnavailableError(Exception):
    """Another operation currently holds the advisory lock for this
    target -- something is genuinely still active against it."""


class PostgresLiveWriter:
    def __init__(self, engine: Engine):
        self.engine = engine

    def _try_acquire_target_lock(self, conn, target_schema: str, target_table: str) -> bool:
        """
        Transaction-scoped Postgres advisory lock unique to this
        (schema, table) pair -- automatically released when the
        transaction commits or rolls back, no manual unlock needed.
        This is what actually proves mutual exclusion, unlike a
        timeout alone: a timeout can only ever prove "a while has
        passed," never "nothing is still active." Used to serialize
        promote()/rollback() against each other for the same target,
        and by reconciliation to prove a markerless RUNNING/
        ROLLING_BACK record's target transaction is genuinely no
        longer active (if the lock CAN be acquired, nothing else
        holds it).
        """
        return bool(conn.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:schema), hashtext(:table))"),
            {"schema": target_schema, "table": target_table},
        ).scalar())

    def is_target_currently_locked(self, target_schema: str, target_table: str) -> bool:
        """
        True if a DIFFERENT active transaction currently holds the
        lock for this target (a promote/rollback is genuinely still
        in progress there right now) -- False if the lock is free.
        Opens its own short-lived transaction purely to test the lock,
        which releases it immediately upon exit if acquired.
        """
        with self.engine.connect() as conn:
            with conn.begin():
                acquired = self._try_acquire_target_lock(conn, target_schema, target_table)
        return not acquired

    def check_operation_outcome(self, target_schema: str, execution_id, operation: str) -> str:
        """
        Used after an AMBIGUOUS exception (e.g. the target transaction
        may have committed but the connection dropped before
        acknowledging it) to determine what actually happened,
        target-side. `operation` is "PROMOTE" or "ROLLBACK". Returns:
        "completed" -- a matching marker exists, so it committed
        despite the exception; "not_committed" -- the marker table was
        reachable and has no such marker, so it's provable this did
        NOT commit; "unknown" -- the marker table itself couldn't even
        be checked (e.g. the connection is still down), so nothing can
        be concluded and the caller must not guess either way.
        """
        try:
            markers = self.get_marker_for_execution(target_schema, execution_id)
        except Exception:
            return "unknown"
        has_marker = any(m["operation"] == operation for m in markers)
        return "completed" if has_marker else "not_committed"

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

    def _current_oid(self, conn, schema: str, table: str):
        """
        The table's current Postgres object identifier, or None if it
        doesn't exist. Identifiers are pre-validated against a strict
        allowlist pattern elsewhere, so passing them unquoted into
        to_regclass()'s string argument (itself a bind parameter, not
        string-interpolated SQL) is safe.
        """
        return conn.execute(
            text("SELECT to_regclass(:qualified)::oid"),
            {"qualified": f"{schema}.{table}"},
        ).scalar()

    def _ensure_execution_log_table(self, conn, target_schema: str) -> None:
        conn.execute(text(
            f'CREATE TABLE IF NOT EXISTS "{target_schema}"."{_EXECUTION_LOG_TABLE}" ('
            f'marker_id UUID PRIMARY KEY, '
            f'live_execution_id UUID NOT NULL, '
            f'target_table TEXT NOT NULL, '
            f'table_oid OID, '
            f'backup_table TEXT, '
            f'operation TEXT NOT NULL, '
            f'recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()'
            f')'
        ))

    def _record_marker(
        self, conn, target_schema, execution_id, target_table, table_oid, backup_table, operation
    ) -> None:
        conn.execute(
            text(
                f'INSERT INTO "{target_schema}"."{_EXECUTION_LOG_TABLE}" '
                f'(marker_id, live_execution_id, target_table, table_oid, backup_table, operation) '
                f'VALUES (:marker_id, :execution_id, :table, :table_oid, :backup, :operation)'
            ),
            {
                "marker_id": str(uuid_module.uuid4()),
                "execution_id": str(execution_id),
                "table": target_table,
                "table_oid": table_oid,
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
                    f'SELECT target_table, table_oid, backup_table, operation, recorded_at '
                    f'FROM "{target_schema}"."{_EXECUTION_LOG_TABLE}" '
                    f'WHERE live_execution_id = :id ORDER BY recorded_at'
                ),
                {"id": str(live_execution_id)},
            ).mappings().all()
            return [dict(r) for r in rows]

    def get_latest_management_marker(self, target_schema: str, target_table: str):
        """
        The most recent marker of EITHER operation (PROMOTE or
        ROLLBACK) for this exact table name, or None. A ROLLBACK is
        also a legitimate management event: it restores a table to a
        known OID, and that restoration must become the new
        authoritative "current identity" for future checks. Confirmed
        this was a real bug when it only considered PROMOTE markers:
        publish A (oid 100) -> publish B (renames A's table to backup,
        oid 100, promotes a new table, oid 200) -> roll back B
        (restores oid 100 as the target, records a ROLLBACK marker
        with oid 100) -> publish C. Looking only at the latest PROMOTE
        marker would still see B's oid 200, mismatching the restored
        table's actual oid 100, and incorrectly refuse C as targeting
        an "unmanaged" table -- even though it's the same table A
        originally created, correctly restored by B's rollback.
        """
        validate_identifier(target_schema, "target schema")
        validate_identifier(target_table, "target table")
        with self.engine.connect() as conn:
            if not self._table_exists(conn, target_schema, _EXECUTION_LOG_TABLE):
                return None
            row = conn.execute(
                text(
                    f'SELECT live_execution_id, table_oid, backup_table, operation, recorded_at '
                    f'FROM "{target_schema}"."{_EXECUTION_LOG_TABLE}" '
                    f"WHERE target_table = :table AND operation IN ('PROMOTE', 'ROLLBACK') "
                    f'ORDER BY recorded_at DESC LIMIT 1'
                ),
                {"table": target_table},
            ).mappings().first()
            return dict(row) if row else None

    def is_target_aegis_managed(self, target_schema: str, target_table: str) -> bool:
        """
        True only if the table currently at this name and schema is
        the SAME physical table (by OID) that Aegis's most recent
        management event (promote OR rollback-restore) for this name
        refers to -- not just that some table with this name was
        promoted at some point in the past.
        """
        marker = self.get_latest_management_marker(target_schema, target_table)
        if marker is None:
            return False
        with self.engine.connect() as conn:
            current_oid = self._current_oid(conn, target_schema, target_table)
        return current_oid is not None and current_oid == marker["table_oid"]

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
            if not self._try_acquire_target_lock(conn, target_schema, target_table):
                raise TargetLockUnavailableError(
                    f"Another operation is currently active against "
                    f'"{target_schema}"."{target_table}" -- refusing to proceed '
                    f"concurrently rather than racing it."
                )

            self._ensure_execution_log_table(conn, target_schema)

            target_exists = self._table_exists(conn, target_schema, target_table)

            if target_exists:
                marker = conn.execute(
                    text(
                        f'SELECT table_oid FROM "{target_schema}"."{_EXECUTION_LOG_TABLE}" '
                        f"WHERE target_table = :table AND operation IN ('PROMOTE', 'ROLLBACK') "
                        f'ORDER BY recorded_at DESC LIMIT 1'
                    ),
                    {"table": target_table},
                ).mappings().first()
                current_oid = self._current_oid(conn, target_schema, target_table)
                if marker is None or marker["table_oid"] != current_oid:
                    raise UnmanagedTargetTableError(
                        f'"{target_schema}"."{target_table}" already exists and is not '
                        f"provably the same table Aegis last promoted under that name "
                        f"(no matching marker, or the table's identity has changed since -- "
                        f"e.g. dropped and recreated by something else). Refusing to "
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
            # exists if and only if the promotion above actually
            # committed. Records the NEW table's OID, so future checks
            # compare physical identity, not just name.
            new_oid = self._current_oid(conn, target_schema, target_table)
            self._record_marker(
                conn, target_schema, execution_id, target_table, new_oid, backup_name, "PROMOTE"
            )

        return {"backup_table": backup_name, "final_row_count": expected_row_count}

    def rollback(self, target_schema: str, target_table: str, backup_table, execution_id) -> None:
        """
        Restores the previous target table from its backup. Refuses
        (StaleRollbackError) unless this execution's own PROMOTE is
        still the most recent management event (PROMOTE or ROLLBACK)
        for this exact table BY IDENTITY (OID), not just name -- if a
        newer execution has since promoted over it, or something else
        has already rolled it back, rolling back this older one would
        destroy valid, newer data (or touch nothing meaningful at all).
        """
        validate_identifier(target_schema, "target schema")
        validate_identifier(target_table, "target table")

        marker = self.get_latest_management_marker(target_schema, target_table)
        with self.engine.connect() as conn:
            current_oid = self._current_oid(conn, target_schema, target_table)

        stale = (
            marker is None
            or str(marker["live_execution_id"]) != str(execution_id)
            or marker["table_oid"] != current_oid
        )
        if stale:
            raise StaleRollbackError(
                f'"{target_schema}"."{target_table}" has since been replaced by a '
                f"different live execution, externally modified, or has no recorded "
                f"Aegis history at all -- refusing to roll back an execution that is "
                f"not provably the latest PROMOTE for this target."
            )

        with self.engine.begin() as conn:
            if not self._try_acquire_target_lock(conn, target_schema, target_table):
                raise TargetLockUnavailableError(
                    f"Another operation is currently active against "
                    f'"{target_schema}"."{target_table}" -- refusing to roll back '
                    f"concurrently rather than racing it."
                )
            self._ensure_execution_log_table(conn, target_schema)
            conn.execute(text(f'DROP TABLE IF EXISTS "{target_schema}"."{target_table}"'))
            if backup_table:
                validate_identifier(backup_table, "backup table")
                conn.execute(text(
                    f'ALTER TABLE "{target_schema}"."{backup_table}" '
                    f'RENAME TO "{target_table}"'
                ))
            restored_oid = self._current_oid(conn, target_schema, target_table)
            self._record_marker(
                conn, target_schema, execution_id, target_table, restored_oid, None, "ROLLBACK"
            )
