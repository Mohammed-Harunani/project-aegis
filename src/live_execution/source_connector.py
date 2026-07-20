"""
Aegis_TrustedSourceConnector
Phase 2.5 final architecture -- reads a COMPLETE, trusted dataset from
a real PostgreSQL source table, for live-capable simulation
(/simulate-migration-from-source) and for re-verification immediately
before a live execution publishes.

This exists because the prior design -- caller-supplied `sample_data`
in the request body -- had no connection to any real, complete
dataset at all. This module is what actually closes that gap: it
reads the real table itself, server-side, deterministically, and
produces the fingerprints that prove later reads match what was
approved.

Correction pass: table existence, primary-key metadata, column
metadata, and the complete ordered read now all happen inside ONE
connection and ONE REPEATABLE READ transaction. Originally these were
three separate connections/queries -- a concurrent schema or
constraint change between them could produce a ticket whose primary
key, row data, and schema fingerprint were captured from three
different states of the table. Also switched the read away from
pd.read_sql's own type inference, which cannot be trusted not to
coerce precise NUMERIC values (returned by the driver as Python
Decimal) into float64 -- exactly the kind of silent precision loss
this project exists to catch elsewhere. Rows are now read directly
and the DataFrame is built with dtype=object forced throughout,
keeping whatever native Python type the driver already produced per
cell (Decimal, date, timezone-aware datetime, UUID, dict/list for
JSONB) completely untouched.
"""

import hashlib
import json

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.live_execution.identifiers import validate_identifier, InvalidIdentifierError
from src.live_execution.output_fingerprint import compute_dataframe_fingerprint


class SourceValidationError(Exception):
    """The source table doesn't meet what's required to be read as a
    trusted, live-capable dataset (bad identifiers, no primary key,
    doesn't exist)."""


class SourceChangedError(Exception):
    """The source table's dataset fingerprint no longer matches what
    was approved -- something changed between simulation and live
    execution. Raised at execute-live time, never silently ignored."""


def _get_primary_key_columns(conn, source_schema: str, source_table: str) -> list:
    """
    Connection-scoped version -- used INSIDE the one atomic snapshot
    transaction in read_complete_source_table(), so the PK lookup
    shares the exact same REPEATABLE READ view of the catalog as
    everything else.

    Joins key_column_usage to table_constraints on table_schema AND
    table_name as well as constraint_name/constraint_schema --
    originally joined on constraint identity alone, which meant two
    DIFFERENT tables in the same schema sharing an identically-named
    PRIMARY KEY constraint could have their key_column_usage rows
    cross-matched, silently mixing columns from the wrong table into
    the result.
    """
    rows = conn.execute(
        text(
            "SELECT kcu.column_name FROM information_schema.table_constraints tco "
            "JOIN information_schema.key_column_usage kcu "
            "ON kcu.constraint_name = tco.constraint_name "
            "AND kcu.constraint_schema = tco.constraint_schema "
            "AND kcu.table_schema = tco.table_schema "
            "AND kcu.table_name = tco.table_name "
            "WHERE tco.constraint_type = 'PRIMARY KEY' "
            "AND tco.table_schema = :schema AND tco.table_name = :table "
            "ORDER BY kcu.ordinal_position"
        ),
        {"schema": source_schema, "table": source_table},
    ).fetchall()
    return [row[0] for row in rows]


def get_primary_key_columns(engine: Engine, source_schema: str, source_table: str) -> list:
    """
    Standalone convenience wrapper (its own connection) for callers
    that just want a quick PK check outside the atomic snapshot flow.
    read_complete_source_table() does NOT call this -- it uses
    _get_primary_key_columns() directly inside its own transaction, so
    the PK lookup, the column metadata, and the data read all come
    from exactly the same snapshot.
    """
    try:
        validate_identifier(source_schema, "source schema")
        validate_identifier(source_table, "source table")
    except InvalidIdentifierError as e:
        raise SourceValidationError(str(e))
    with engine.connect() as conn:
        return _get_primary_key_columns(conn, source_schema, source_table)


def _get_column_metadata(conn, source_schema: str, source_table: str) -> list:
    """[(column_name, data_type), ...] in ordinal order, from the SAME
    connection/transaction as everything else in the snapshot."""
    rows = conn.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table "
            "ORDER BY ordinal_position"
        ),
        {"schema": source_schema, "table": source_table},
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _schema_fingerprint_from_metadata(column_metadata: list) -> str:
    canonical = json.dumps([[name, dtype] for name, dtype in column_metadata], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_source_schema_fingerprint(engine: Engine, source_schema: str, source_table: str) -> str:
    """Standalone convenience wrapper (own connection). Not used by
    read_complete_source_table(), which computes this from the same
    transaction's column metadata instead -- kept for any caller that
    wants a schema fingerprint without a full table read."""
    with engine.connect() as conn:
        column_metadata = _get_column_metadata(conn, source_schema, source_table)
    return _schema_fingerprint_from_metadata(column_metadata)


def read_complete_source_table(engine: Engine, source_schema: str, source_table: str) -> dict:
    """
    Reads the ENTIRE source table in ONE read-only, REPEATABLE READ
    transaction and ONE connection -- table existence, primary-key
    metadata, column metadata, and the complete ordered read all come
    from the exact same snapshot. Returns a dict: dataframe,
    primary_key, row_count, schema_fingerprint, column_types (dict of
    column_name -> Postgres data_type, as this exact snapshot saw it),
    dataset_fingerprint.

    Raises SourceValidationError if the table doesn't exist or has no
    primary key.
    """
    try:
        validate_identifier(source_schema, "source schema")
        validate_identifier(source_table, "source table")
    except InvalidIdentifierError as e:
        raise SourceValidationError(str(e))

    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))

            exists = conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :schema AND table_name = :table)"
                ),
                {"schema": source_schema, "table": source_table},
            ).scalar()
            if not exists:
                raise SourceValidationError(f'"{source_schema}"."{source_table}" does not exist.')

            primary_key = _get_primary_key_columns(conn, source_schema, source_table)
            if not primary_key:
                raise SourceValidationError(
                    f'"{source_schema}"."{source_table}" has no primary key -- a '
                    f"live-capable source table must have one, so the complete read "
                    f"can be ordered deterministically and the dataset fingerprint "
                    f"is reproducible across reads."
                )
            for col in primary_key:
                validate_identifier(col, "primary key column")

            column_metadata = _get_column_metadata(conn, source_schema, source_table)
            schema_fingerprint = _schema_fingerprint_from_metadata(column_metadata)

            order_clause = ", ".join(f'"{col}"' for col in primary_key)
            result = conn.execute(
                text(f'SELECT * FROM "{source_schema}"."{source_table}" ORDER BY {order_clause}')
            )
            column_names = list(result.keys())
            rows = [tuple(r) for r in result.fetchall()]

    # dtype=object forced throughout -- NOT pd.read_sql's own type
    # inference, which cannot be trusted not to coerce precise NUMERIC
    # values (returned by the driver as Python Decimal) into float64.
    # This keeps whatever native Python type the driver already gave
    # each cell (Decimal, datetime.date, timezone-aware
    # datetime.datetime, uuid.UUID, dict/list for JSONB, bool, int,
    # str) completely untouched all the way through to publication --
    # see writer.py's value-based type inference for the other half of
    # this fix.
    dataframe = pd.DataFrame(rows, columns=column_names, dtype=object)

    dataset_fingerprint = compute_dataframe_fingerprint(dataframe)

    return {
        "dataframe": dataframe,
        "primary_key": primary_key,
        "row_count": len(dataframe),
        "schema_fingerprint": schema_fingerprint,
        "column_types": {name: pg_type for name, pg_type in column_metadata},
        "dataset_fingerprint": dataset_fingerprint,
    }


def verify_source_unchanged(
    engine: Engine, source_schema: str, source_table: str, expected_dataset_fingerprint: str
) -> dict:
    """
    Re-reads the source table and confirms its dataset fingerprint
    still matches what was approved. Called immediately before a live
    execution publishes -- if the source has changed since the ticket
    was simulated and approved, this raises SourceChangedError rather
    than silently publishing against stale provenance. Returns the
    fresh read (same shape as read_complete_source_table) on success,
    so the caller doesn't need to read the source twice.
    """
    fresh = read_complete_source_table(engine, source_schema, source_table)
    if fresh["dataset_fingerprint"] != expected_dataset_fingerprint:
        raise SourceChangedError(
            f'"{source_schema}"."{source_table}" has changed since this ticket '
            f"was simulated and approved -- the source dataset fingerprint no "
            f"longer matches. Refusing to publish against a source that no "
            f"longer matches what was actually reviewed. A new simulation and "
            f"approval are required."
        )
    return fresh
