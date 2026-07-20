"""
Aegis_PostgresPublicationWriter
Phase 2.5 final architecture -- publishes corrected datasets via
immutable versioned physical tables behind a stable, consumer-facing
view. Replaces the earlier rename-to-backup design entirely (not an
extension of it): a rename-swap left anything referencing the target
by Postgres object identity (views, foreign keys) still pointing at
the OLD table, now the backup, not the newly promoted one. A stable
view repointed via CREATE OR REPLACE VIEW solves this structurally --
Postgres preserves a view's OID across CREATE OR REPLACE VIEW as long
as the output column signature stays compatible, so anything
referencing the view by identity keeps working across republication,
because the view's identity never changes, only what it selects from
does.

Two fixed schemas, never caller-supplied:
- aegis_publish_data: immutable physical version tables (one per
  execution, named "<logical_target>__<execution_suffix>", never
  renamed or reused) and the target-side marker table.
- aegis_publish: the stable views consumers actually query.

Locking: session-level (pg_advisory_lock/pg_advisory_unlock), held by
the CALLER (app.py) via hold_target_lock() for the entire execute-live
or rollback flow -- this design carries forward unchanged from the
rename-based writer; how publication works underneath is orthogonal
to how concurrent operations on the same logical_target are
serialized.
"""

import decimal
import datetime
import uuid as uuid_module
from contextlib import contextmanager

import pandas as pd
from sqlalchemy import MetaData, Table, Column, text
from sqlalchemy import BigInteger, Integer, Float, Text, Boolean, TIMESTAMP, Date, Numeric
from sqlalchemy.dialects.postgresql import UUID as SA_UUID, JSONB as SA_JSONB
from sqlalchemy.engine import Engine

from src.live_execution.identifiers import validate_identifier, physical_version_table_name
from src.live_execution.dtype_mapping import pandas_dtype_to_postgres_type, UnsupportedDtypeError


AEGIS_PUBLISH_SCHEMA = "aegis_publish"
AEGIS_PUBLISH_DATA_SCHEMA = "aegis_publish_data"

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


class UnsupportedColumnValueError(Exception):
    """A column's actual values (not just its pandas dtype label) have
    no safe, explicit Postgres type mapping -- refusing to silently
    fall back to TEXT for something we can't identify."""


class LiveWriteValidationError(Exception):
    """Post-write validation failed inside the transaction; it has been rolled back."""


class IncompatibleViewSchemaError(Exception):
    """The new publication's columns aren't compatible with the
    currently-published version -- Postgres requires a replacement
    view to retain the same output column names, order, and types
    (columns may be added at the end)."""


class StaleRollbackError(Exception):
    """This execution's own publish is not the current one for its
    logical target -- something newer has superseded it, so rolling
    back would repoint the view away from valid, newer data."""


class TargetLockUnavailableError(Exception):
    """Another operation currently holds the session-level advisory lock
    for this logical target -- something is genuinely still active
    against it."""


class PostgresPublicationWriter:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ---- Locking (unchanged design from the rename-based writer) ----

    @contextmanager
    def hold_target_lock(self, logical_target: str):
        """
        Session-level advisory lock, held on a dedicated connection for
        the FULL duration of the caller's `with` block -- the entire
        execute-live or rollback flow, not just the target-database
        transaction. Non-blocking acquire: raises
        TargetLockUnavailableError immediately if something else
        already holds it. Always released and the connection closed on
        the way out, success or failure. If the process crashes
        instead of exiting normally, Postgres releases all of a
        backend's session-level advisory locks when its connection
        terminates, which is what makes this a reliable liveness
        signal for reconciliation even across a hard crash.
        """
        conn = self.engine.connect()
        try:
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:target))"),
                {"target": logical_target},
            ).scalar()
            conn.commit()
            if not acquired:
                raise TargetLockUnavailableError(
                    f"Another operation is currently active against logical "
                    f'target "{logical_target}" -- refusing to proceed '
                    f"concurrently rather than racing it."
                )
            try:
                yield
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:target))"),
                    {"target": logical_target},
                )
                conn.commit()
        finally:
            conn.close()

    def check_operation_outcome(self, execution_id, operation: str) -> str:
        """
        Marker-only check, with NO lock acquisition -- for use when the
        CALLER already holds the target's session-level lock. Returns
        "completed" if a matching marker exists, "not_committed" if
        the marker table is reachable and doesn't, "unknown" if the
        marker table itself couldn't be checked.
        """
        try:
            markers = self.get_marker_for_execution(execution_id)
        except Exception:
            return "unknown"
        has_marker = any(m["operation"] == operation for m in markers)
        return "completed" if has_marker else "not_committed"

    def determine_outcome_under_lock(self, logical_target: str, execution_id, operation: str) -> str:
        """
        The definitive way to resolve an ambiguous or stale execution's
        true outcome -- for reconciliation from a SEPARATE, later
        request that does not already hold the lock. Tries to acquire
        the same session-level lock genuine operations hold:
        "active" (lock unavailable -- something else genuinely still
        holds it), "not_committed" (lock acquired, no matching marker
        -- provably safe, since nothing else can be running against
        this target while we hold the lock), "completed" (lock
        acquired, matching marker exists), "unknown" (lock acquired,
        marker table itself unreachable). The marker lookup happens
        WHILE holding the lock, closing the window where a different
        operation could start between testing the lock and checking
        the marker.
        """
        with self.engine.connect() as conn:
            acquired = conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:target))"),
                {"target": logical_target},
            ).scalar()
            conn.commit()
            if not acquired:
                return "active"
            try:
                try:
                    markers = self.get_marker_for_execution(execution_id)
                except Exception:
                    return "unknown"
                has_marker = any(m["operation"] == operation for m in markers)
                return "completed" if has_marker else "not_committed"
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:target))"),
                    {"target": logical_target},
                )
                conn.commit()

    # ---- Internals ----

    def _sa_type_for_column(self, series: pd.Series):
        """
        Two paths, deliberately: a column with a genuine native numpy
        dtype (e.g. Surgeon cast it via .astype("int64")) uses the
        existing, proven pandas-dtype mapping -- unambiguous. A column
        with dtype=object (which is EVERY column coming out of the
        trusted-source read, forced there specifically to protect
        Decimal/date/datetime/UUID precision -- see source_connector.py)
        has a meaningless dtype label, so its Postgres type is decided
        by inspecting the actual Python values instead.
        """
        dtype_str = str(series.dtype)
        if dtype_str != "object":
            postgres_type_name = pandas_dtype_to_postgres_type(dtype_str)
            return _POSTGRES_TYPE_TO_SA[postgres_type_name]()
        return self._infer_sa_type_from_values(series)

    def _infer_sa_type_from_values(self, series: pd.Series):
        """
        Looks at the first non-null value in an object-dtype column to
        decide its Postgres column type. Order matters: bool is
        checked before int (bool IS an int subclass in Python --
        isinstance(True, int) is True), and datetime.datetime is
        checked before datetime.date (datetime.datetime IS a
        datetime.date subclass).
        """
        first_value = next((v for v in series if v is not None and not (isinstance(v, float) and pd.isna(v))), None)
        if first_value is None:
            # Fully-null column -- nothing to infer a more specific
            # type from; TEXT is the honest fallback, not a guess.
            return Text()
        if isinstance(first_value, bool):
            return Boolean()
        if isinstance(first_value, decimal.Decimal):
            # Unconstrained precision/scale -- preserves whatever the
            # source had exactly, rather than guessing a fixed
            # precision that could truncate it.
            return Numeric()
        if isinstance(first_value, datetime.datetime):
            return TIMESTAMP(timezone=first_value.tzinfo is not None)
        if isinstance(first_value, datetime.date):
            return Date()
        if isinstance(first_value, uuid_module.UUID):
            return SA_UUID()
        if isinstance(first_value, (dict, list)):
            return SA_JSONB()
        if isinstance(first_value, int):
            return BigInteger()
        if isinstance(first_value, float):
            return Float()
        if isinstance(first_value, str):
            return Text()
        raise UnsupportedColumnValueError(
            f"No safe Postgres type mapping for column values of Python type "
            f"{type(first_value).__name__!r} -- refusing to silently fall back to TEXT."
        )

    def _table_exists(self, conn, schema: str, table: str) -> bool:
        return bool(conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = :schema AND table_name = :table)"
            ),
            {"schema": schema, "table": table},
        ).scalar())

    def _ensure_publish_schemas_and_log(self, conn) -> None:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {AEGIS_PUBLISH_DATA_SCHEMA}"))
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {AEGIS_PUBLISH_SCHEMA}"))
        conn.execute(text(
            f'CREATE TABLE IF NOT EXISTS "{AEGIS_PUBLISH_DATA_SCHEMA}"."{_EXECUTION_LOG_TABLE}" ('
            f'marker_id UUID PRIMARY KEY, '
            f'live_execution_id UUID NOT NULL, '
            f'logical_target TEXT NOT NULL, '
            f'physical_table TEXT, '
            f'previous_physical_table TEXT, '
            f'operation TEXT NOT NULL, '
            f'recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()'
            f')'
        ))

    def _record_marker(
        self, conn, execution_id, logical_target, physical_table, previous_physical_table, operation
    ) -> None:
        conn.execute(
            text(
                f'INSERT INTO "{AEGIS_PUBLISH_DATA_SCHEMA}"."{_EXECUTION_LOG_TABLE}" '
                f'(marker_id, live_execution_id, logical_target, physical_table, '
                f'previous_physical_table, operation) '
                f'VALUES (:marker_id, :execution_id, :target, :physical, :previous, :operation)'
            ),
            {
                "marker_id": str(uuid_module.uuid4()),
                "execution_id": str(execution_id),
                "target": logical_target,
                "physical": physical_table,
                "previous": previous_physical_table,
                "operation": operation,
            },
        )

    def get_marker_for_execution(self, live_execution_id) -> list:
        """
        Every marker row recorded for a specific execution_id. Used for
        crash-recovery reconciliation: durable target-side evidence of
        what actually happened, independent of whether the
        governance-database update after it succeeded.
        """
        with self.engine.connect() as conn:
            if not self._table_exists(conn, AEGIS_PUBLISH_DATA_SCHEMA, _EXECUTION_LOG_TABLE):
                return []
            rows = conn.execute(
                text(
                    f'SELECT logical_target, physical_table, previous_physical_table, '
                    f'operation, recorded_at '
                    f'FROM "{AEGIS_PUBLISH_DATA_SCHEMA}"."{_EXECUTION_LOG_TABLE}" '
                    f'WHERE live_execution_id = :id ORDER BY recorded_at'
                ),
                {"id": str(live_execution_id)},
            ).mappings().all()
            return [dict(r) for r in rows]

    def get_latest_marker(self, logical_target: str):
        """
        The most recent marker (PUBLISH or ROLLBACK) for this logical
        target -- every marker's physical_table field records what the
        stable view points to AFTER that operation, so this is always
        "what should currently be published," regardless of whether
        the most recent event was a publish or a rollback.
        """
        validate_identifier(logical_target, "logical target")
        with self.engine.connect() as conn:
            if not self._table_exists(conn, AEGIS_PUBLISH_DATA_SCHEMA, _EXECUTION_LOG_TABLE):
                return None
            row = conn.execute(
                text(
                    f'SELECT live_execution_id, physical_table, previous_physical_table, '
                    f'operation, recorded_at '
                    f'FROM "{AEGIS_PUBLISH_DATA_SCHEMA}"."{_EXECUTION_LOG_TABLE}" '
                    f'WHERE logical_target = :target ORDER BY recorded_at DESC LIMIT 1'
                ),
                {"target": logical_target},
            ).mappings().first()
            return dict(row) if row else None

    def _get_physical_table_column_signature(self, conn, physical_table: str) -> list:
        """[(column_name, data_type), ...] in ordinal order."""
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table "
                "ORDER BY ordinal_position"
            ),
            {"schema": AEGIS_PUBLISH_DATA_SCHEMA, "table": physical_table},
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def _check_structural_compatibility(
        self, conn, previous_physical_table: str, new_dataframe: pd.DataFrame
    ) -> None:
        """
        Phase 2.5 requires an EXACT publication signature: same column
        names, same order, same count, same Postgres types. Appending
        columns is NOT allowed, even though Postgres's CREATE OR
        REPLACE VIEW would accept it going forward -- the earlier
        design allowed appending, but rollback repoints the view back
        to the PREVIOUS (narrower) physical table, and Postgres does
        not allow CREATE OR REPLACE VIEW to remove existing output
        columns. That meant a publish-then-rollback sequence with an
        appended column would fail specifically when rolling back,
        which is exactly the operation this system exists to make
        safe. Requiring an exact match in both directions removes that
        asymmetry entirely.
        """
        existing_signature = self._get_physical_table_column_signature(conn, previous_physical_table)
        new_signature = [
            (col, self._postgres_type_name_for_column(new_dataframe[col]))
            for col in new_dataframe.columns
        ]
        if new_signature != existing_signature:
            raise IncompatibleViewSchemaError(
                f"New publication's column signature {new_signature} does not "
                f"exactly match the currently published signature "
                f"{existing_signature} -- Phase 2.5 requires an exact match "
                f"(same names, same order, same types, same count). Appending "
                f"columns is not permitted: Postgres will not allow rollback to "
                f"repoint the view back to a physical table with fewer columns, "
                f"so allowing the append in one direction but not the other "
                f"would leave rollback broken for exactly this case."
            )

    def _postgres_type_name_for_column(self, series: pd.Series) -> str:
        """
        The Postgres type NAME (not the SQLAlchemy type object) for a
        column, used only for signature comparison against
        information_schema's own data_type strings. Must stay
        consistent with _sa_type_for_column()/_infer_sa_type_from_values()
        -- same decision, described in Postgres's own vocabulary.

        Uses exact type() equality rather than isinstance() --
        deliberately sidesteps needing to know SQLAlchemy's exact
        subclass hierarchy (e.g. whether BigInteger extends Integer,
        or Float extends Numeric), which could not be verified in this
        environment (no working SQLAlchemy install to introspect).
        Since these objects are only ever constructed by
        _infer_sa_type_from_values()/_sa_type_for_column() immediately
        above, exact-type matching is sufficient and removes any
        dependency on an inheritance assumption that couldn't be
        checked directly.
        """
        sa_type = self._sa_type_for_column(series)
        exact_type = type(sa_type)
        if exact_type is BigInteger:
            return "bigint"
        if exact_type is Integer:
            return "integer"
        if exact_type is Float:
            return "double precision"
        if exact_type is Boolean:
            return "boolean"
        if exact_type is Numeric:
            return "numeric"
        if exact_type is TIMESTAMP:
            return "timestamp with time zone" if sa_type.timezone else "timestamp without time zone"
        if exact_type is Date:
            return "date"
        if exact_type is SA_UUID:
            return "uuid"
        if exact_type is SA_JSONB:
            return "jsonb"
        if exact_type is Text:
            return "text"
        return str(sa_type).lower()

    # ---- Publication ----

    def publish(
        self,
        logical_target: str,
        dataframe: pd.DataFrame,
        execution_id,
        expected_row_count: int,
    ) -> dict:
        """
        Creates a new immutable physical version table and repoints the
        stable view to it, all in one transaction. Returns
        {"physical_table", "previous_physical_table", "final_row_count"}.
        REQUIRES the caller to already be holding this logical target's
        session-level lock (via hold_target_lock()).
        """
        validate_identifier(logical_target, "logical target")
        for col in dataframe.columns:
            validate_identifier(col, "column")

        physical_table = physical_version_table_name(logical_target, execution_id)

        with self.engine.begin() as conn:
            self._ensure_publish_schemas_and_log(conn)

            latest_marker = conn.execute(
                text(
                    f'SELECT physical_table FROM "{AEGIS_PUBLISH_DATA_SCHEMA}"."{_EXECUTION_LOG_TABLE}" '
                    f'WHERE logical_target = :target ORDER BY recorded_at DESC LIMIT 1'
                ),
                {"target": logical_target},
            ).mappings().first()
            previous_physical_table = latest_marker["physical_table"] if latest_marker else None

            if previous_physical_table:
                self._check_structural_compatibility(
                    conn, previous_physical_table, dataframe
                )

            # 1. Create the new immutable physical version table.
            metadata = MetaData(schema=AEGIS_PUBLISH_DATA_SCHEMA)
            columns = [
                Column(col, self._sa_type_for_column(dataframe[col]))
                for col in dataframe.columns
            ]
            Table(physical_table, metadata, *columns).create(bind=conn)

            # 2. Write the corrected dataset into it.
            dataframe.to_sql(
                physical_table, con=conn, schema=AEGIS_PUBLISH_DATA_SCHEMA,
                if_exists="append", index=False,
            )

            # 3. Validate (gate 15): row count actually written matches
            # what sandbox execution already found.
            actual_count = conn.execute(
                text(f'SELECT COUNT(*) FROM "{AEGIS_PUBLISH_DATA_SCHEMA}"."{physical_table}"')
            ).scalar()
            if actual_count != expected_row_count:
                raise LiveWriteValidationError(
                    f"Physical version table row count {actual_count} does not "
                    f"match expected {expected_row_count} -- refusing to publish."
                )

            # 4. Repoint (or create) the stable view. Explicit column
            # list, not SELECT * -- makes the "same columns, same
            # order, may append at the end" contract concrete rather
            # than implicit.
            column_list = ", ".join(f'"{c}"' for c in dataframe.columns)
            conn.execute(text(
                f'CREATE OR REPLACE VIEW "{AEGIS_PUBLISH_SCHEMA}"."{logical_target}" AS '
                f'SELECT {column_list} FROM "{AEGIS_PUBLISH_DATA_SCHEMA}"."{physical_table}"'
            ))

            # 5. Durable target-side evidence, in the SAME transaction
            # -- exists if and only if the publish above actually
            # committed.
            self._record_marker(
                conn, execution_id, logical_target, physical_table,
                previous_physical_table, "PUBLISH",
            )

        return {
            "physical_table": physical_table,
            "previous_physical_table": previous_physical_table,
            "final_row_count": expected_row_count,
        }

    def rollback_to_previous(self, logical_target: str, execution_id, previous_physical_table) -> None:
        """
        Repoints the stable view back to the execution's own recorded
        previous_physical_table -- or removes the view entirely if
        this was the first-ever publish for this logical target (no
        previous version to repoint to). Never drops or recreates any
        physical version table; they remain immutable for audit and
        for any later rollback. REQUIRES the caller to already be
        holding this logical target's session-level lock.
        """
        validate_identifier(logical_target, "logical target")

        with self.engine.begin() as conn:
            self._ensure_publish_schemas_and_log(conn)

            latest_marker = conn.execute(
                text(
                    f'SELECT live_execution_id, physical_table, operation '
                    f'FROM "{AEGIS_PUBLISH_DATA_SCHEMA}"."{_EXECUTION_LOG_TABLE}" '
                    f'WHERE logical_target = :target ORDER BY recorded_at DESC LIMIT 1'
                ),
                {"target": logical_target},
            ).mappings().first()

            stale = (
                latest_marker is None
                or str(latest_marker["live_execution_id"]) != str(execution_id)
                or latest_marker["operation"] != "PUBLISH"
            )
            if stale:
                raise StaleRollbackError(
                    f'Logical target "{logical_target}" has since been republished '
                    f"by a different execution, or already rolled back -- refusing "
                    f"to roll back an execution that is not the current publish "
                    f"for this target."
                )

            current_physical_table = latest_marker["physical_table"]

            if previous_physical_table is None:
                conn.execute(text(
                    f'DROP VIEW IF EXISTS "{AEGIS_PUBLISH_SCHEMA}"."{logical_target}"'
                ))
            else:
                signature = self._get_physical_table_column_signature(conn, previous_physical_table)
                column_list = ", ".join(f'"{name}"' for name, _pg_type in signature)
                conn.execute(text(
                    f'CREATE OR REPLACE VIEW "{AEGIS_PUBLISH_SCHEMA}"."{logical_target}" AS '
                    f'SELECT {column_list} FROM "{AEGIS_PUBLISH_DATA_SCHEMA}"."{previous_physical_table}"'
                ))

            self._record_marker(
                conn, execution_id, logical_target, previous_physical_table,
                current_physical_table, "ROLLBACK",
            )
