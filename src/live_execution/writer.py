"""
Aegis_PostgresLiveWriter
Phase 2.5 -- the dedicated write component for live execution. Not an
extension of the read-only connector (src/db/connector.py) -- a
separate, purpose-built component, per the locked architecture.

Runs the full sequence in ONE transaction against the live target
database: create shadow table -> write corrected data -> validate row
count -> back up the existing target (if any) -> promote shadow to
the target name. If anything fails before commit, Postgres rolls the
target database back to exactly its pre-execution state -- this
relies on Postgres's transactional DDL support (CREATE/ALTER TABLE
inside a transaction), which is specifically why this phase is scoped
to PostgreSQL only and not portable to e.g. MySQL, where DDL isn't
transactional the same way.
"""

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


class LiveWriteValidationError(Exception):
    """Post-write validation failed inside the transaction; it has been rolled back."""


class PostgresLiveWriter:
    def __init__(self, engine: Engine):
        self.engine = engine

    def _sa_type_for(self, pandas_dtype: str):
        postgres_type_name = pandas_dtype_to_postgres_type(pandas_dtype)
        return _POSTGRES_TYPE_TO_SA[postgres_type_name]()

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
        propagates out of this method, the `with engine.begin()` block
        has already rolled the target database back to its
        pre-execution state.
        """
        validate_identifier(target_schema, "target schema")
        validate_identifier(target_table, "target table")
        for col in dataframe.columns:
            validate_identifier(col, "column")

        shadow_name = shadow_table_name(target_table, execution_id)
        backup_name = backup_table_name(target_table, execution_id)

        with self.engine.begin() as conn:
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
            # what sandbox execution already found. Refusing to
            # promote here rolls back everything above, including the
            # CREATE TABLE.
            actual_count = conn.execute(
                text(f'SELECT COUNT(*) FROM "{target_schema}"."{shadow_name}"')
            ).scalar()
            if actual_count != expected_row_count:
                raise LiveWriteValidationError(
                    f"Shadow table row count {actual_count} does not match "
                    f"expected {expected_row_count} -- refusing to promote."
                )

            # 4. Back up the existing target, if one exists.
            target_exists = conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :schema AND table_name = :table)"
                ),
                {"schema": target_schema, "table": target_table},
            ).scalar()

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

        return {"backup_table": backup_name, "final_row_count": expected_row_count}

    def rollback(self, target_schema: str, target_table: str, backup_table: str) -> None:
        """
        Restores the previous target table from its backup, in one
        transaction. If backup_table is None, the target didn't exist
        before execution -- rollback means dropping the promoted table
        instead, restoring the "no table" state.
        """
        validate_identifier(target_schema, "target schema")
        validate_identifier(target_table, "target table")

        with self.engine.begin() as conn:
            conn.execute(text(f'DROP TABLE IF EXISTS "{target_schema}"."{target_table}"'))
            if backup_table:
                validate_identifier(backup_table, "backup table")
                conn.execute(text(
                    f'ALTER TABLE "{target_schema}"."{backup_table}" '
                    f'RENAME TO "{target_table}"'
                ))
