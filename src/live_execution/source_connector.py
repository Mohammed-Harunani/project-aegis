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
connection and ONE REPEATABLE READ transaction. Also switched the
read away from pd.read_sql's own type inference (dtype=object forced
throughout instead, to protect Decimal/date/UUID precision). The
logical-dtype vocabulary this module needs (to compare against Gold
schemas -- see logical_dtype.py's own docstring for why that's a
separate concern from the raw pandas dtype label) lives in
logical_dtype.py specifically because THIS module needs sqlalchemy
(it queries Postgres directly) and that one must not: safety.py's
evaluate_safety_gates() needs to stay importable with zero database
dependency for the pure-Python test suite, and it needs the same
vocabulary for verify_complete_schema_match(). Importing it from here
would have made safety.py transitively require sqlalchemy -- caught
directly (the entire pure-Python suite failed to import) before it
shipped.

Second correction pass: verify_source_unchanged() now revalidates ALL
FOUR persisted provenance values (primary key, row count, schema
fingerprint, dataset fingerprint), not just the dataset fingerprint --
a source change that alters structure (a column's type, nullability,
or precision/scale) without changing any current row's VALUES could
otherwise pass unnoticed. The schema fingerprint itself is also richer
now: ordinal position, concrete type, numeric precision/scale,
datetime precision, and nullability, not just column name + data_type.
"""

import hashlib
import json

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.live_execution.identifiers import validate_identifier, InvalidIdentifierError
from src.live_execution.output_fingerprint import compute_dataframe_fingerprint
from src.live_execution.logical_dtype import (
    postgres_type_to_aegis_dtype,
    build_source_observed_schema,
    build_corrected_observed_schema,
    UnsupportedSourceTypeError,
)


class SourceValidationError(Exception):
    """The source table doesn't meet what's required to be read as a
    trusted, live-capable dataset (bad identifiers, no primary key,
    doesn't exist)."""


class SourceChangedError(Exception):
    """The source's provenance no longer matches what was approved --
    primary key, row count, schema fingerprint, or dataset fingerprint
    has changed since simulation. Raised at execute-live time, never
    silently ignored."""


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
    """
    Full column metadata in ordinal order, from the SAME connection/
    transaction as everything else in the snapshot. Returns dicts with
    name, data_type, numeric_precision, numeric_scale,
    datetime_precision, is_nullable, character_maximum_length,
    udt_schema, udt_name, domain_schema, domain_name, collation_name --
    a source change that alters precision/scale/length, nullability,
    domain, UDT identity, or collation, without changing any current
    row's values, would otherwise be invisible to the schema
    fingerprint. Confirmed directly this was a real gap: a column
    widened from VARCHAR(20) to VARCHAR(200) produced no fingerprint
    change at all under the narrower metadata set.
    """
    rows = conn.execute(
        text(
            "SELECT column_name, data_type, numeric_precision, numeric_scale, "
            "datetime_precision, is_nullable, character_maximum_length, "
            "udt_schema, udt_name, domain_schema, domain_name, collation_name "
            "FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table "
            "ORDER BY ordinal_position"
        ),
        {"schema": source_schema, "table": source_table},
    ).mappings().fetchall()
    return [dict(r) for r in rows]


def _schema_fingerprint_from_metadata(column_metadata: list) -> str:
    canonical = json.dumps(
        [
            [
                m["column_name"], m["data_type"], m["numeric_precision"],
                m["numeric_scale"], m["datetime_precision"], m["is_nullable"],
                m["character_maximum_length"], m["udt_schema"], m["udt_name"],
                m["domain_schema"], m["domain_name"], m["collation_name"],
            ]
            for m in column_metadata
        ],
        separators=(",", ":"),
    )
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
    # str) completely untouched all the way through to publication.
    dataframe = pd.DataFrame(rows, columns=column_names, dtype=object)

    dataset_fingerprint = compute_dataframe_fingerprint(dataframe)

    return {
        "dataframe": dataframe,
        "primary_key": primary_key,
        "row_count": len(dataframe),
        "schema_fingerprint": schema_fingerprint,
        "column_types": {m["column_name"]: m["data_type"] for m in column_metadata},
        "dataset_fingerprint": dataset_fingerprint,
    }


def verify_source_unchanged(
    engine: Engine,
    source_schema: str,
    source_table: str,
    expected_primary_key: list,
    expected_row_count: int,
    expected_schema_fingerprint: str,
    expected_dataset_fingerprint: str,
) -> dict:
    """
    Re-reads the source table and confirms ALL FOUR persisted
    provenance values still match what was approved: primary key
    columns, row count, schema fingerprint, and dataset fingerprint.
    Checking the dataset fingerprint alone would miss a structural
    change that doesn't happen to alter any current row's values --
    e.g. a column's declared precision/scale changing, or its
    nullability changing, or the primary key definition itself
    changing. Raises SourceChangedError on any mismatch rather than
    silently publishing against stale provenance. Returns the fresh
    read (same shape as read_complete_source_table) on success, so the
    caller doesn't need to read the source twice.
    """
    fresh = read_complete_source_table(engine, source_schema, source_table)

    mismatches = []
    if fresh["primary_key"] != expected_primary_key:
        mismatches.append(
            f"primary key changed (was {expected_primary_key}, now {fresh['primary_key']})"
        )
    if fresh["row_count"] != expected_row_count:
        mismatches.append(
            f"row count changed (was {expected_row_count}, now {fresh['row_count']})"
        )
    if fresh["schema_fingerprint"] != expected_schema_fingerprint:
        mismatches.append("schema fingerprint changed (column types, precision, scale, or nullability)")
    if fresh["dataset_fingerprint"] != expected_dataset_fingerprint:
        mismatches.append("dataset fingerprint changed (row values)")

    if mismatches:
        raise SourceChangedError(
            f'"{source_schema}"."{source_table}" has changed since this ticket was '
            f"simulated and approved: {'; '.join(mismatches)}. Refusing to publish "
            f"against a source that no longer matches what was actually reviewed. "
            f"A new simulation and approval are required."
        )
    return fresh
