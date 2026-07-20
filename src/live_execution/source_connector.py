"""
Aegis_TrustedSourceConnector
Phase 2.5 final architecture -- reads a COMPLETE, trusted dataset from
a real PostgreSQL source table, for live-capable simulation
(/simulate-migration-from-source) and for re-verification immediately
before a live execution publishes.

This exists because the prior design -- caller-supplied `sample_data`
in the request body -- had no connection to any real, complete
dataset at all. A three-row simulation sample could be approved and
then published live as if it were the whole table. This module is
what actually closes that gap: it reads the real table itself,
server-side, deterministically, and produces the fingerprints that
prove later reads match what was approved.
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


def get_primary_key_columns(engine: Engine, source_schema: str, source_table: str) -> list:
    """
    Returns the primary key column names, in their defined order, for
    the given table. Empty list if the table has no primary key --
    the caller (read_complete_source_table) turns that into a hard
    SourceValidationError; a source table without a primary key has no
    way to be ordered deterministically, which is required for the
    dataset fingerprint to be meaningful (two reads of an unordered
    table with unstable output order would fingerprint differently
    even with identical data).

    Uses information_schema (ANSI-standard, not a Postgres catalog
    internal) specifically because key_column_usage.ordinal_position
    is explicitly documented as the column's position within the
    constraint -- more certain to be correct than decoding pg_index's
    internal int2vector representation by hand.
    """
    try:
        validate_identifier(source_schema, "source schema")
        validate_identifier(source_table, "source table")
    except InvalidIdentifierError as e:
        raise SourceValidationError(str(e))

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT kcu.column_name FROM information_schema.table_constraints tco "
                "JOIN information_schema.key_column_usage kcu "
                "ON kcu.constraint_name = tco.constraint_name "
                "AND kcu.constraint_schema = tco.constraint_schema "
                "WHERE tco.constraint_type = 'PRIMARY KEY' "
                "AND tco.table_schema = :schema AND tco.table_name = :table "
                "ORDER BY kcu.ordinal_position"
            ),
            {"schema": source_schema, "table": source_table},
        ).fetchall()
    return [row[0] for row in rows]


def compute_source_schema_fingerprint(engine: Engine, source_schema: str, source_table: str) -> str:
    """
    SHA-256 over the source table's (column_name, data_type) pairs, in
    ordinal position order, as Postgres's own catalog reports them
    right now. Distinct from output_fingerprint's DATA fingerprint --
    this one is about the table's STRUCTURE, checked once at
    simulation time and stored for audit; the DATASET fingerprint
    below is what's actually re-verified before live execution.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table "
                "ORDER BY ordinal_position"
            ),
            {"schema": source_schema, "table": source_table},
        ).fetchall()
    canonical = json.dumps([[r[0], r[1]] for r in rows], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_complete_source_table(engine: Engine, source_schema: str, source_table: str) -> dict:
    """
    Reads the ENTIRE source table in one read-only, REPEATABLE READ
    transaction, ordered deterministically by its primary key.
    Returns a dict: dataframe, primary_key (list of column names),
    row_count, schema_fingerprint, dataset_fingerprint.

    REPEATABLE READ + READ ONLY: the whole read sees one consistent
    snapshot of the table (no phantom rows from concurrent writes
    partway through a large read) and can't itself write anything.
    Ordering by the primary key is what makes the dataset fingerprint
    reproducible -- an unordered read of the same data could return
    rows in a different physical order between two reads even with no
    actual changes, which would fingerprint as a false mismatch.

    Raises SourceValidationError if the table has no primary key.
    """
    try:
        validate_identifier(source_schema, "source schema")
        validate_identifier(source_table, "source table")
    except InvalidIdentifierError as e:
        raise SourceValidationError(str(e))

    primary_key = get_primary_key_columns(engine, source_schema, source_table)
    if not primary_key:
        raise SourceValidationError(
            f'"{source_schema}"."{source_table}" has no primary key -- a '
            f"live-capable source table must have one, so the complete read "
            f"can be ordered deterministically and the dataset fingerprint "
            f"is reproducible across reads."
        )
    for col in primary_key:
        validate_identifier(col, "primary key column")

    order_clause = ", ".join(f'"{col}"' for col in primary_key)

    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            dataframe = pd.read_sql(
                text(f'SELECT * FROM "{source_schema}"."{source_table}" ORDER BY {order_clause}'),
                conn,
            )

    schema_fingerprint = compute_source_schema_fingerprint(engine, source_schema, source_table)
    dataset_fingerprint = compute_dataframe_fingerprint(dataframe)

    return {
        "dataframe": dataframe,
        "primary_key": primary_key,
        "row_count": len(dataframe),
        "schema_fingerprint": schema_fingerprint,
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
