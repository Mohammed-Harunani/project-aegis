"""
Aegis_DtypeVocabulary
The single, authoritative set of dtype strings Aegis accepts anywhere
a "dtype" is declared or compared: Schema Registry validation
(registry/schema_definition.py), the PostgreSQL<->Aegis logical dtype
mapping (live_execution/logical_dtype.py), and publication type
mapping (live_execution/writer.py). Kept in its own module, at the
top level rather than under live_execution/, specifically so Phase
2.4's Schema Registry (which predates Phase 2.5 entirely) doesn't have
to import from a later phase's package to know what a valid dtype is
-- both import from here instead.

Confirmed directly that pandas.api.types.pandas_dtype() rejects every
one of Aegis's own logical dtypes (decimal/date/datetime/datetime_tz/
uuid/json) before this existed -- meaning no Gold schema could ever be
registered declaring a column NUMERIC, DATE, TIMESTAMPTZ, UUID, or
JSONB, making the trusted-source datatype-fidelity work from prior
correction passes unusable for exactly the financial types this
project cares most about. A valid Aegis dtype is now a standard pandas
dtype (via pandas_dtype()) OR one of these extended logical dtypes.
"""

import pandas as pd

# Types with no direct pandas dtype equivalent -- pandas has no native
# way to represent "this object-dtype column holds Decimal" (or date,
# or UUID, or JSONB) distinctly from a plain string/object column, so
# Aegis declares these itself.
AEGIS_EXTENDED_LOGICAL_DTYPES = frozenset({
    "decimal", "date", "datetime", "datetime_tz", "uuid", "json",
})


def is_valid_aegis_dtype(dtype: str) -> bool:
    """
    True if dtype is either a standard pandas dtype pandas itself
    recognizes, or one of Aegis's own extended logical dtypes.
    """
    if dtype in AEGIS_EXTENDED_LOGICAL_DTYPES:
        return True
    try:
        pd.api.types.pandas_dtype(dtype)
        return True
    except TypeError:
        return False
