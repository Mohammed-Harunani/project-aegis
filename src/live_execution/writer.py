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

from contextlib import contextmanager
import logging
import uuid as uuid_module

import pandas as pd
from sqlalchemy import MetaData, Table, Column, text
from sqlalchemy import BigInteger, Float, Text, Boolean, TIMESTAMP, Date, Numeric
from sqlalchemy.dialects.postgresql import UUID as SA_UUID, JSONB as SA_JSONB
from sqlalchemy.engine import Engine

from src.live_execution.identifiers import validate_identifier, physical_version_table_name
from src.live_execution.logical_dtype import aegis_dtype_to_postgres_type_name, UnsupportedSourceTypeError

_logger = logging.getLogger(__name__)

AEGIS_PUBLISH_SCHEMA = "aegis_publish"
AEGIS_PUBLISH_DATA_SCHEMA = "aegis_publish_data"

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

        Yields the connection itself -- publish()/rollback_to_previous()
        must be given THIS SAME connection (not open their own via
        self.engine.begin()). Originally they opened a separate
        connection: reconciliation acquiring the lock later would then
        only prove the LOCK-holding connection's session had ended, not
        that the publication transaction on the OTHER connection had
        actually resolved. Both connections are drawn from the same
        pool but are otherwise independent -- something like an
        intermediate proxy's idle timeout could in principle affect one
        without affecting the other at the same moment, letting the
        lock release while the publish transaction is still genuinely
        in flight elsewhere. Sharing one connection removes that gap
        entirely: there is no "part of it" that can die while another
        part stays alive.
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
                yield conn
            finally:
                # Best-effort cleanup -- must NEVER mask whatever the
                # caller's code already decided (a return value, or a
                # deliberately-raised HTTPException) by raising a NEW,
                # unrelated exception here. Confirmed this was a real
                # risk: if the connection is already dead or in a
                # failed-transaction state (e.g. the publish
                # transaction itself hit a connection error), a bare
                # unlock attempt on it would itself raise, and that
                # new exception would silently REPLACE whatever the
                # inner code had already carefully determined (a clean
                # 503, or a confirmed COMPLETED result). Any failure
                # here is logged, not propagated -- PostgreSQL
                # releases a backend's session-level advisory locks
                # automatically when its connection terminates, so a
                # failed unlock doesn't leave the lock stuck forever.
                try:
                    conn.rollback()
                except Exception:
                    pass
                try:
                    conn.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:target))"),
                        {"target": logical_target},
                    )
                    conn.commit()
                except Exception as unlock_error:
                    _logger.warning(
                        "Failed to release advisory lock for logical_target=%r "
                        "(connection likely already invalid) -- relying on "
                        "PostgreSQL's automatic session-level lock release on "
                        "connection termination instead. Reason: %s",
                        logical_target, unlock_error,
                    )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def marker_matches_publication_target(marker, publication_target_id) -> bool:
        marker_target_id = marker.get("publication_target_id")
        if marker_target_id is None or publication_target_id is None:
            return marker_target_id is None and publication_target_id is None
        return str(marker_target_id) == str(publication_target_id)

    @classmethod
    def _operation_marker_outcome(
        cls, markers: list, operation: str, publication_target_id
    ) -> str:
        operation_markers = [m for m in markers if m["operation"] == operation]
        if any(
            cls.marker_matches_publication_target(marker, publication_target_id)
            for marker in operation_markers
        ):
            return "completed"
        if operation_markers:
            return "identity_mismatch"
        return "not_committed"

    def check_operation_outcome(
        self, execution_id, operation: str, publication_target_id=None
    ) -> str:
        """
        Marker-only check, with NO lock acquisition -- for use when the
        CALLER already holds the target's session-level lock. Returns
        "completed" if a matching marker exists, "identity_mismatch"
        if the execution/operation marker names different target lineage,
        "not_committed" if the marker table is reachable and has no such
        operation, or "unknown" if the marker table couldn't be checked.
        """
        try:
            markers = self.get_marker_for_execution(execution_id)
        except Exception:
            return "unknown"
        return self._operation_marker_outcome(
            markers, operation, publication_target_id
        )

    def determine_outcome_under_lock(
        self,
        logical_target: str,
        execution_id,
        operation: str,
        publication_target_id=None,
    ) -> str:
        """
        The definitive way to resolve an ambiguous or stale execution's
        true outcome -- for reconciliation from a SEPARATE, later
        request that does not already hold the lock. Tries to acquire
        the same session-level lock genuine operations hold:
        "active" (lock unavailable -- something else genuinely still
        holds it), "not_committed" (lock acquired, no matching marker
        -- provably safe, since nothing else can be running against
        this target while we hold the lock), "completed" (lock
        acquired, matching marker exists), "identity_mismatch" (the
        operation marker exists for different target lineage), or
        "unknown" (lock acquired, marker table itself unreachable). The
        marker lookup happens
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
                return self._operation_marker_outcome(
                    markers, operation, publication_target_id
                )
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:target))"),
                    {"target": logical_target},
                )
                conn.commit()

    # ---- Internals ----

    def _sa_type_for_gold_dtype(self, logical_dtype: str):
        """
        Derives the SQLAlchemy column type from the Gold schema's OWN
        declared logical dtype -- NOT from inspecting the dataframe's
        actual row values. verify_complete_schema_match() (called
        before publish() ever runs, in app.py's execute_live) already
        guarantees every column's post-repair logical dtype exactly
        matches what Gold declares, so this is a reliable, explicit
        publication-type contract rather than a guess: an all-null
        NUMERIC column keeps its declared NUMERIC type instead of
        falling back to TEXT (which value-inspection had no way to
        avoid, since there's no value to inspect), and an unsupported
        logical dtype is rejected outright rather than silently
        defaulting to anything.
        """
        pg_type_name = aegis_dtype_to_postgres_type_name(logical_dtype)
        return self._sa_type_for_postgres_type_name(pg_type_name)

    def _sa_type_for_postgres_type_name(self, pg_type_name: str):
        if pg_type_name == "timestamp without time zone":
            return TIMESTAMP(timezone=False)
        if pg_type_name == "timestamp with time zone":
            return TIMESTAMP(timezone=True)
        try:
            return {
                "bigint": BigInteger, "double precision": Float, "text": Text,
                "boolean": Boolean, "numeric": Numeric, "date": Date,
                "uuid": SA_UUID, "jsonb": SA_JSONB,
            }[pg_type_name]()
        except KeyError:
            raise UnsupportedColumnValueError(
                f"No SQLAlchemy type mapping for PostgreSQL type name {pg_type_name!r}."
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
        # Additive target-side evolution. Existing Phase 2.5/3.1 marker
        # tables and their rows remain intact; historical rows read as
        # NULL, while every new lineage-aware operation records the
        # immutable governance-side publication target identity.
        conn.execute(text(
            f'ALTER TABLE "{AEGIS_PUBLISH_DATA_SCHEMA}"."{_EXECUTION_LOG_TABLE}" '
            f'ADD COLUMN IF NOT EXISTS publication_target_id UUID'
        ))

    def _execution_log_has_publication_target_id(self, conn) -> bool:
        return bool(conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table "
                "AND column_name = 'publication_target_id')"
            ),
            {"schema": AEGIS_PUBLISH_DATA_SCHEMA, "table": _EXECUTION_LOG_TABLE},
        ).scalar())

    def _publication_target_select_expression(self, conn) -> str:
        if self._execution_log_has_publication_target_id(conn):
            return "publication_target_id"
        # Read-only compatibility with a pre-Phase-3.2 target database.
        return "NULL::uuid AS publication_target_id"

    def _record_marker(
        self,
        conn,
        execution_id,
        logical_target,
        physical_table,
        previous_physical_table,
        operation,
        publication_target_id=None,
    ) -> None:
        conn.execute(
            text(
                f'INSERT INTO "{AEGIS_PUBLISH_DATA_SCHEMA}"."{_EXECUTION_LOG_TABLE}" '
                f'(marker_id, live_execution_id, logical_target, physical_table, '
                f'previous_physical_table, operation, publication_target_id) '
                f'VALUES (:marker_id, :execution_id, :target, :physical, :previous, '
                f':operation, :publication_target_id)'
            ),
            {
                "marker_id": str(uuid_module.uuid4()),
                "execution_id": str(execution_id),
                "target": logical_target,
                "physical": physical_table,
                "previous": previous_physical_table,
                "operation": operation,
                "publication_target_id": (
                    str(publication_target_id)
                    if publication_target_id is not None
                    else None
                ),
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
            publication_target_expression = (
                self._publication_target_select_expression(conn)
            )
            rows = conn.execute(
                text(
                    f'SELECT logical_target, physical_table, previous_physical_table, '
                    f'operation, recorded_at, {publication_target_expression} '
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
            publication_target_expression = (
                self._publication_target_select_expression(conn)
            )
            row = conn.execute(
                text(
                    f'SELECT live_execution_id, physical_table, previous_physical_table, '
                    f'operation, recorded_at, {publication_target_expression} '
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
        self, conn, previous_physical_table: str, new_columns: list, gold_schema
    ) -> None:
        """
        Phase 2.5 requires an EXACT publication signature: same column
        names, same order, same count, same Postgres types -- derived
        from the Gold schema's OWN declared logical dtype for each
        column, not inferred from row values. Appending columns is NOT
        allowed, even though Postgres's CREATE OR REPLACE VIEW would
        accept it going forward -- the earlier design allowed
        appending, but rollback repoints the view back to the
        PREVIOUS (narrower) physical table, and Postgres does not
        allow CREATE OR REPLACE VIEW to remove existing output
        columns. That meant a publish-then-rollback sequence with an
        appended column would fail specifically when rolling back,
        which is exactly the operation this system exists to make
        safe. Requiring an exact match in both directions removes that
        asymmetry entirely.
        """
        existing_signature = self._get_physical_table_column_signature(conn, previous_physical_table)
        new_signature = [
            (col, aegis_dtype_to_postgres_type_name(gold_schema.columns[col].dtype))
            for col in new_columns
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

    # ---- Publication ----

    def publish(
        self,
        conn,
        logical_target: str,
        dataframe: pd.DataFrame,
        execution_id,
        expected_row_count: int,
        gold_schema,
        publication_target_id=None,
    ) -> dict:
        """
        Creates a new immutable physical version table and repoints the
        stable view to it, all in one transaction. Returns
        {"physical_table", "previous_physical_table", "final_row_count"}.

        REQUIRES conn to be the SAME connection currently yielded by
        this logical target's hold_target_lock() -- not a separate one
        opened here. Publication and the session-level lock must share
        one Postgres backend: reconciliation acquiring the lock later
        only proves something about whichever connection held it, so if
        that were a different connection than the one running this
        transaction, a dead lock-holding connection wouldn't prove
        anything about whether THIS transaction had resolved.

        gold_schema is the ticket's own ObservedSchema for the Gold
        target -- app.py's execute_live already calls
        verify_complete_schema_match() before this, which guarantees
        every column's post-repair logical dtype exactly matches what
        Gold declares, so this is the authoritative, explicit
        publication-type contract, not an inference from row values.
        """
        validate_identifier(logical_target, "logical target")
        for col in dataframe.columns:
            validate_identifier(col, "column")

        physical_table = physical_version_table_name(logical_target, execution_id)

        with conn.begin():
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
                    conn, previous_physical_table, list(dataframe.columns), gold_schema
                )

            # 1. Create the new immutable physical version table,
            # typed from the Gold schema's own declared dtype for each
            # column -- not inferred from row values, so an all-null
            # NUMERIC/UUID/JSONB column keeps its declared type
            # instead of falling back to TEXT.
            metadata = MetaData(schema=AEGIS_PUBLISH_DATA_SCHEMA)
            sa_columns = [
                Column(col, self._sa_type_for_gold_dtype(gold_schema.columns[col].dtype))
                for col in dataframe.columns
            ]
            physical_table_obj = Table(physical_table, metadata, *sa_columns)
            physical_table_obj.create(bind=conn)

            # 2. Write the corrected dataset into it -- through the
            # explicitly-typed table object's own insert(), not
            # DataFrame.to_sql(). to_sql() builds its OWN SQLAlchemy
            # metadata from the DataFrame directly, and its normal
            # fallback for an object-dtype column is Text, regardless
            # of what type the physical table actually declared --
            # meaning the JSONB/UUID/Numeric/Date/TIMESTAMP bind
            # processors selected above would never actually be used.
            # Inserting through the real table object guarantees they
            # are.
            records = dataframe.to_dict(orient="records")
            if records:
                conn.execute(physical_table_obj.insert(), records)

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
                previous_physical_table, "PUBLISH", publication_target_id,
            )

        return {
            "physical_table": physical_table,
            "previous_physical_table": previous_physical_table,
            "final_row_count": expected_row_count,
        }

    def rollback_to_previous(
        self,
        conn,
        logical_target: str,
        execution_id,
        previous_physical_table,
        publication_target_id=None,
    ) -> None:
        """
        Repoints the stable view back to the execution's own recorded
        previous_physical_table -- or removes the view entirely if
        this was the first-ever publish for this logical target (no
        previous version to repoint to). Never drops or recreates any
        physical version table; they remain immutable for audit and
        for any later rollback.

        REQUIRES conn to be the SAME connection currently yielded by
        this logical target's hold_target_lock() -- same reasoning as
        publish(): the rollback transaction and the session-level lock
        must share one Postgres backend for the lock's later
        availability to mean anything about this transaction's fate.
        """
        validate_identifier(logical_target, "logical target")

        with conn.begin():
            self._ensure_publish_schemas_and_log(conn)

            latest_marker = conn.execute(
                text(
                    f'SELECT live_execution_id, physical_table, operation, '
                    f'publication_target_id '
                    f'FROM "{AEGIS_PUBLISH_DATA_SCHEMA}"."{_EXECUTION_LOG_TABLE}" '
                    f'WHERE logical_target = :target ORDER BY recorded_at DESC LIMIT 1'
                ),
                {"target": logical_target},
            ).mappings().first()

            stale = (
                latest_marker is None
                or str(latest_marker["live_execution_id"]) != str(execution_id)
                or latest_marker["operation"] != "PUBLISH"
                or not self.marker_matches_publication_target(
                    latest_marker, publication_target_id
                )
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
                current_physical_table, "ROLLBACK", publication_target_id,
            )
