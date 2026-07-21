"""
Aegis_LogicalDtypeVocabulary
Phase 2.5 -- the shared logical dtype vocabulary used to compare a
trusted-source table's actual structure against a Gold schema, kept
in its OWN sqlalchemy-free module deliberately: source_connector.py
needs sqlalchemy (it queries Postgres directly), but safety.py's
evaluate_safety_gates() and this module's own functions are meant to
be testable with zero database dependency, the same way
identifiers.py and dtype_mapping.py already are. Importing this
vocabulary FROM source_connector.py would have made safety.py
transitively require sqlalchemy too, breaking the entire pure-Python
test suite -- confirmed directly, caught before it shipped.

Why this vocabulary needs to exist at all: read_complete_source_table()
forces every column to dtype=object to protect Decimal/date/UUID
precision (see that function's docstring), which makes the raw pandas
dtype label meaningless for schema comparison -- a genuinely BIGINT
column would show "object" and never type-match a Gold column
declared "int64", silently preventing Consultant from ever detecting
a rename (confirmed directly: propose_repairs() returned zero plans
for exactly that scenario before this existed). This module provides
two ways to recover the LOGICAL dtype: from captured Postgres column
metadata (for the pre-repair source read) and from actual values (for
a post-repair working copy, where a column may be untouched from the
source read or renamed-but-not-cast by Surgeon).
"""

import decimal
import datetime
import json
import uuid as uuid_module

import pandas as pd

from src.inspector import ObservedSchema, ColumnStats


def _json_safe_unique_count(series: pd.Series) -> int:
    """
    pandas' own Series.nunique() raises TypeError: unhashable type
    'dict' the moment it encounters a JSONB column's dict/list values
    -- confirmed directly. Canonicalizes any dict/list value to a
    stable, sorted JSON string before counting (two dicts with the
    same keys/values in a different order count as the same unique
    value, matching what "unique" should mean for JSON content), so a
    JSONB column no longer crashes schema observation entirely.
    """
    seen = set()
    for v in series:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        if isinstance(v, (dict, list)):
            v = json.dumps(v, sort_keys=True, default=str)
        seen.add(v)
    return len(seen)


_POSTGRES_TO_AEGIS_LOGICAL_DTYPE = {
    "bigint": "int64",
    "integer": "int64",
    "smallint": "int64",
    "text": "object",
    "character varying": "object",
    "character": "object",
    "double precision": "float64",
    "real": "float64",
    "boolean": "bool",
    "numeric": "decimal",
    "date": "date",
    "timestamp without time zone": "datetime",
    "timestamp with time zone": "datetime_tz",
    "uuid": "uuid",
    "json": "json",
    "jsonb": "json",
}


class UnsupportedSourceTypeError(Exception):
    """A source column's Postgres type (or, in the reverse direction, a
    logical dtype with no PostgreSQL equivalent) has no explicit, safe
    mapping -- rejected before an approval ticket is ever created or
    before publication, not discovered later."""


# Phase 2.5 POLICY DECISION, stated explicitly rather than left
# implicit: publication uses CANONICAL AEGIS NORMALIZATION, not exact
# source database fidelity. A source NUMERIC(20, 9) publishes as
# unconstrained NUMERIC (no declared precision/scale); SMALLINT and
# INTEGER both publish as BIGINT; REAL publishes as DOUBLE PRECISION;
# VARCHAR(n) publishes as TEXT. This is a deliberate choice, not an
# oversight: the property this project actually cares about -- exact
# VALUE fidelity, never silently losing precision on the data itself
# -- is fully preserved by this scheme (unconstrained NUMERIC still
# holds a Decimal with its original exact digits; nothing here ever
# rounds or truncates a value). What is NOT preserved is the source's
# declared SCHEMA-LEVEL constraint (that a column may only hold up to
# 9 digits after the decimal point, or at most 255 characters) --
# publication does not re-derive or enforce those constraints in the
# published table. Exact schema-level fidelity (persisting precision,
# scale, and length, and recreating them on the published table) is a
# larger, separate piece of work than this phase attempts; if it's
# ever needed, it belongs in a dedicated follow-up, not bolted on here
# implicitly.
_AEGIS_LOGICAL_DTYPE_TO_POSTGRES_TYPE_NAME = {
    "int64": "bigint",
    "float64": "double precision",
    "object": "text",
    "bool": "boolean",
    "decimal": "numeric",
    "date": "date",
    "datetime": "timestamp without time zone",
    "datetime_tz": "timestamp with time zone",
    "uuid": "uuid",
    "json": "jsonb",
}


def aegis_dtype_to_postgres_type_name(logical_dtype: str) -> str:
    try:
        return _AEGIS_LOGICAL_DTYPE_TO_POSTGRES_TYPE_NAME[logical_dtype]
    except KeyError:
        raise UnsupportedSourceTypeError(
            f"No PostgreSQL type mapping for logical dtype {logical_dtype!r}. "
            f"Supported: {sorted(_AEGIS_LOGICAL_DTYPE_TO_POSTGRES_TYPE_NAME)}."
        )


def postgres_type_to_aegis_dtype(pg_type: str) -> str:
    try:
        return _POSTGRES_TO_AEGIS_LOGICAL_DTYPE[pg_type]
    except KeyError:
        raise UnsupportedSourceTypeError(
            f"No explicit logical dtype mapping for PostgreSQL type {pg_type!r}. "
            f"Supported: {sorted(_POSTGRES_TO_AEGIS_LOGICAL_DTYPE)}."
        )


def infer_aegis_logical_dtype_from_values(series: pd.Series) -> str:
    """
    Value-based counterpart to postgres_type_to_aegis_dtype() -- used
    when a column's pandas dtype is "object" and there is no captured
    Postgres metadata to consult (e.g. a post-repair working copy).
    Order matters: bool before int (bool IS an int subclass in
    Python), datetime.datetime before datetime.date (datetime.datetime
    IS a date subclass).
    """
    first_value = next(
        (v for v in series if v is not None and not (isinstance(v, float) and pd.isna(v))), None
    )
    if first_value is None:
        return "object"
    if isinstance(first_value, bool):
        return "bool"
    if isinstance(first_value, decimal.Decimal):
        return "decimal"
    if isinstance(first_value, datetime.datetime):
        return "datetime_tz" if first_value.tzinfo is not None else "datetime"
    if isinstance(first_value, datetime.date):
        return "date"
    if isinstance(first_value, uuid_module.UUID):
        return "uuid"
    if isinstance(first_value, (dict, list)):
        return "json"
    if isinstance(first_value, int):
        return "int64"
    if isinstance(first_value, float):
        return "float64"
    return "object"


def build_source_observed_schema(dataframe: pd.DataFrame, column_types: dict) -> ObservedSchema:
    """
    Builds an ObservedSchema for a trusted-source read using the
    CAPTURED PostgreSQL column metadata for each column's dtype, not
    DataFrame.dtypes. null_count/unique_count are still computed from
    the actual data. Raises UnsupportedSourceTypeError for any column
    type with no explicit, safe mapping, so an unsupported type is
    rejected before an approval ticket exists.
    """
    columns = {}
    for col in dataframe.columns:
        series = dataframe[col]
        columns[col] = ColumnStats(
            null_count=int(series.isna().sum()),
            unique_count=_json_safe_unique_count(series),
            dtype=postgres_type_to_aegis_dtype(column_types[col]),
        )
    return ObservedSchema(columns=columns, column_order=list(dataframe.columns))


def build_corrected_observed_schema(
    dataframe: pd.DataFrame, original_observed_schema: ObservedSchema, proposed_action: str
) -> ObservedSchema:
    """
    Builds an ObservedSchema for a POST-REPAIR working copy, using a
    METADATA-BACKED type contract rather than inferring from row
    values. Confirmed directly that value-based inference wrongly
    rejects every all-null typed column (NUMERIC, UUID, JSONB, date,
    etc.) -- there's no non-null value to infer a type from, so it
    falls back to "object", which then never matches whatever Gold
    actually declared for that column.

    For a column with a genuine native numpy dtype (Surgeon's
    CAST_COLUMN produces these directly via .astype()), that dtype is
    authoritative -- Surgeon actually changed it, so there's nothing
    to look up. For an object-dtype column (untouched by this repair,
    or renamed but not cast), its logical dtype is looked up from
    original_observed_schema instead of inferred: a RENAME_COLUMN
    repair changes a column's name but not its data or type, so the
    ORIGINAL column's already-known logical dtype (found under its OLD
    name, via proposed_action) is still correct. Value-based inference
    remains only as a last-resort fallback for a column this can't
    otherwise account for.
    """
    rename_map = {}  # new_name -> old_name
    if proposed_action.startswith("RENAME_COLUMN"):
        try:
            _, rest = proposed_action.split(" ", 1)
            old_name, new_name = (s.strip() for s in rest.split("->"))
            rename_map[new_name] = old_name
        except ValueError:
            pass  # unexpected format -- fall through to value-based inference

    columns = {}
    for col in dataframe.columns:
        series = dataframe[col]
        dtype_str = str(series.dtype)
        if dtype_str != "object":
            logical_dtype = dtype_str
        else:
            original_name = rename_map.get(col, col)
            if original_name in original_observed_schema.columns:
                logical_dtype = original_observed_schema.columns[original_name].dtype
            else:
                logical_dtype = infer_aegis_logical_dtype_from_values(series)
        columns[col] = ColumnStats(
            null_count=int(series.isna().sum()),
            unique_count=_json_safe_unique_count(series),
            dtype=logical_dtype,
        )
    return ObservedSchema(columns=columns, column_order=list(dataframe.columns))
