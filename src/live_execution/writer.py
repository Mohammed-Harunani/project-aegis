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
- Table IDENTITY, not just name, proves Aegis manages a target. Each
  PROMOTE marker records the table's Postgres OID (via to_regclass())
  at the moment of promotion; both the managed-target check and
  rollback's staleness check compare the CURRENT table's OID against
  the latest MANAGEMENT event (PROMOTE or ROLLBACK, whichever is more
  recent) -- a rollback's restored OID is just as authoritative as a
  promotion's.
- Locking is SESSION-level (pg_advisory_lock/pg_advisory_unlock), held
  by the CALLER (app.py) via hold_target_lock() for the ENTIRE
  operation -- Surgeon recomputation, fingerprint verification, AND
  the target-database transaction -- not just the DDL transaction
  inside promote()/rollback(). A transaction-scoped lock acquired only
  inside promote() left a real gap: a slow Surgeon call (longer than
  the staleness threshold) meant reconciliation could see "no lock
  held yet, no marker yet" and wrongly conclude FAILED while the
  original request was still legitimately working. promote()/
  rollback() no longer acquire their own lock -- they rely entirely on
  the caller already holding it, and check target identity using the
  SAME connection that performs the mutation, closing any window
  between the identity check and the write.
"""

import uuid as uuid_module
from contextlib import contextmanager

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
    """The target table already exists with no matching Aegis management
    marker (by current table identity, not just name) proving Aegis
    created/manages it -- refusing to silently take it over."""


class StaleRollbackError(Exception):
    """This execution is not the most recent management event for its
    target (by current table identity) -- something newer has
    superseded it, so rolling back would destroy valid, newer data."""


class TargetLockUnavailableError(Exception):
    """Another operation currently holds the session-level advisory lock
    for this target -- something is genuinely still active against it."""


class PostgresLiveWriter:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def hold_target_lock(self, target_schema: str, target_table: str):
        """
        Session-level advisory lock (pg_advisory_lock), held on a
        dedicated connection for the FULL duration of the caller's
        `with` block -- meant to wrap the entire execute-live or
        rollback flow, not just the target-database transaction.
        Non-blocking acquire: raises TargetLockUnavailableError
        immediately if something else already holds it, rather than
        blocking the HTTP request. Always released (pg_advisory_unlock)
        and the connection closed on the way out, success or failure --
        the `finally` blocks guarantee this even if the caller's code
        raises. If the PROCESS crashes instead of exiting normally,
        Postgres releases all of a backend's session-level advisory
        locks when its connection terminates, which is what makes this
        a reliable liveness signal for reconciliation even across a
        hard crash, not just a clean exit.
        """
        conn = self.engine.connect()
        try:
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:schema), hashtext(:table))"),
                {"schema": target_schema, "table": target_table},
            ).scalar()
            conn.commit()
            if not acquired:
                raise TargetLockUnavailableError(
                    f"Another operation is currently active against "
                    f'"{target_schema}"."{target_table}" -- refusing to proceed '
                    f"concurrently rather than racing it."
                )
            try:
                yield
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:schema), hashtext(:table))"),
                    {"schema": target_schema, "table": target_table},
                )
                conn.commit()
        finally:
            conn.close()

    def check_operation_outcome(self, target_schema: str, execution_id, operation: str) -> str:
        """
        Marker-only check, with NO lock acquisition -- for use when the
        CALLER already holds this target's session-level lock (e.g.
        execute_live's own exception handler, still inside its
        `with hold_target_lock(...)` block). Trying to acquire the
        lock again from a different connection in that situation would
        just see the caller's OWN lock and misreport "active" for the
        wrong reason. Returns "completed" if a matching marker exists,
        "not_committed" if the marker table is reachable and doesn't,
        "unknown" if the marker table itself couldn't be checked.
        Compare to determine_outcome_under_lock(), which DOES acquire
        the lock -- that one is for reconciliation, called from a
        separate, later request that does not already hold it.
        """
        try:
            markers = self.get_marker_for_execution(target_schema, execution_id)
        except Exception:
            return "unknown"
        has_marker = any(m["operation"] == operation for m in markers)
        return "completed" if has_marker else "not_committed"

    def determine_outcome_under_lock(
        self, target_schema: str, target_table: str, execution_id, operation: str
    ) -> str:
        """
        The definitive way to resolve an ambiguous or stale execution's
        true outcome -- used both by app.py's exception handlers (an
        exception from promote()/rollback() doesn't by itself prove
        the target transaction failed) and by reconciliation (a stale
        RUNNING/ROLLING_BACK record). Tries to acquire the SAME
        session-level lock genuine operations hold:

        - Lock unavailable -> "active": something else genuinely still
          holds it, regardless of how long it's been or what the
          exception said. Do not conclude anything.
        - Lock acquired, no matching marker -> "not_committed":
          provably safe, since nothing else can be running against
          this target while we hold the lock, and the marker table
          would already show a commit if one had happened.
        - Lock acquired, matching marker exists -> "completed": the
          operation actually succeeded (e.g. a lost connection
          acknowledgment), whatever exception the caller saw.
        - Lock acquired, marker table itself unreachable -> "unknown":
          can't be checked right now; don't guess either way.

        The marker lookup happens WHILE holding the lock, closing the
        window where a different operation could start between
        testing the lock and checking the marker.
        """
        with self.engine.connect() as conn:
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:schema), hashtext(:table))"),
                {"schema": target_schema, "table": target_table},
            ).scalar()
            conn.commit()
            if not acquired:
                return "active"
            try:
                try:
                    markers = self.get_marker_for_execution(target_schema, execution_id)
                except Exception:
                    return "unknown"
                has_marker = any(m["operation"] == operation for m in markers)
                return "completed" if has_marker else "not_committed"
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:schema), hashtext(:table))"),
                    {"schema": target_schema, "table": target_table},
                )
                conn.commit()

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
        authoritative "current identity" for future checks.
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

        REQUIRES the caller to already be holding this target's
        session-level lock (via hold_target_lock()) -- this method no
        longer acquires its own, since a transaction-scoped lock
        acquired only here left a gap during Surgeon recomputation
        before this was ever called. The identity check below runs
        inside THIS transaction, on this same connection, so there is
        no window between checking and mutating.
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
        for this exact table BY IDENTITY (OID), not just name.

        REQUIRES the caller to already be holding this target's
        session-level lock. The identity check (marker + current OID)
        now runs INSIDE the same transaction and on the same
        connection that performs the mutation -- previously it ran on
        a separate connection before the transaction even opened,
        leaving a window where the target could theoretically change
        between the check and the write. With the caller already
        holding the lock for the whole operation, that window is
        closed by construction, but checking on the same connection
        removes any doubt.
        """
        validate_identifier(target_schema, "target schema")
        validate_identifier(target_table, "target table")

        with self.engine.begin() as conn:
            self._ensure_execution_log_table(conn, target_schema)

            marker = conn.execute(
                text(
                    f'SELECT live_execution_id, table_oid, backup_table '
                    f'FROM "{target_schema}"."{_EXECUTION_LOG_TABLE}" '
                    f"WHERE target_table = :table AND operation IN ('PROMOTE', 'ROLLBACK') "
                    f'ORDER BY recorded_at DESC LIMIT 1'
                ),
                {"table": target_table},
            ).mappings().first()
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
