"""
Aegis_LiveExecution_DtypeMapping
Phase 2.5 -- explicit pandas dtype -> Postgres column type mapping for
building the shadow table's DDL.

An unmapped dtype is a hard error, not a silent fallback to TEXT --
silently widening a numeric column to text on a real external table
is exactly the kind of quiet corruption this project exists to catch
elsewhere (see the Phase 2.3 NaN/JSONB and Decimal/Timestamp fixes).
"""

_PANDAS_TO_POSTGRES = {
    "int64": "BIGINT",
    "int32": "INTEGER",
    "Int64": "BIGINT",   # pandas nullable integer
    "Int32": "INTEGER",
    "float64": "DOUBLE PRECISION",
    "float32": "REAL",
    "object": "TEXT",
    "string": "TEXT",    # pandas StringDtype
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",  # pandas nullable boolean
    "datetime64[ns]": "TIMESTAMP",
    "datetime64[us]": "TIMESTAMP",
}


class UnsupportedDtypeError(Exception):
    """Raised when a column's dtype has no explicit, safe Postgres mapping."""


def pandas_dtype_to_postgres_type(dtype: str) -> str:
    try:
        return _PANDAS_TO_POSTGRES[dtype]
    except KeyError:
        raise UnsupportedDtypeError(
            f"No explicit Postgres type mapping for pandas dtype {dtype!r}. "
            f"Supported: {sorted(_PANDAS_TO_POSTGRES)}."
        )
