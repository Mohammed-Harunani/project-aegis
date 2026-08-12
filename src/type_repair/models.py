"""
Aegis_TypeRepairModels
Phase 3.1.2 -- immutable result models for the verified type-conversion
engine defined by Docs/phase3_1_verified_type_repair_spec.md.

Deliberately pure: only the standard library and pandas (for the
Series type used in ColumnConversionResult.converted_series) are
imported. No FastAPI, SQLAlchemy, repositories, or PostgreSQL
connectors -- checked directly here, since the module-boundary
requirement (spec Section 14) is meant to be enforced by what this
package can even import, not just by convention.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

import pandas as pd


class ConversionStatus(str, Enum):
    """
    Exactly the four outcomes the spec defines (Section 5). A str
    subclass so status comparisons and serialization (e.g. in a later
    stage's manifest persistence) don't require a separate mapping
    step -- ConversionStatus.SAFE == "SAFE" is True.
    """

    SAFE = "SAFE"
    UNSAFE = "UNSAFE"
    UNSUPPORTED = "UNSUPPORTED"
    ERROR = "ERROR"


class ReasonCode(str, Enum):
    """
    The stable, mandatory reason codes (spec Section 7). Tests assert
    against these values directly, not against free-form message
    text -- the message field on ConversionDiagnostic is for human
    readability only and must never be the thing a test depends on.
    """

    PARSE_ERROR = "PARSE_ERROR"
    FRACTIONAL_VALUE = "FRACTIONAL_VALUE"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    PRECISION_LOSS = "PRECISION_LOSS"
    AMBIGUOUS_BOOLEAN = "AMBIGUOUS_BOOLEAN"
    NON_FINITE_NUMBER = "NON_FINITE_NUMBER"
    NULL_TARGET_INCOMPATIBLE = "NULL_TARGET_INCOMPATIBLE"
    TIMEZONE_LOSS = "TIMEZONE_LOSS"
    UNSUPPORTED_SOURCE_VALUE = "UNSUPPORTED_SOURCE_VALUE"
    UNSUPPORTED_TARGET_DTYPE = "UNSUPPORTED_TARGET_DTYPE"
    ROUND_TRIP_MISMATCH = "ROUND_TRIP_MISMATCH"
    INTERNAL_CONVERSION_ERROR = "INTERNAL_CONVERSION_ERROR"


@dataclass(frozen=True)
class ConversionDiagnostic:
    """
    One row-level (or, with row_index=None, column-level) diagnostic.
    message is a human-readable summary and must never embed the raw
    source value -- confirmed throughout Phase 2.5 that this project
    treats "don't leak the actual data into logs/messages" as a real
    requirement, not a nice-to-have, and this engine follows the same
    discipline from its first line of code.
    """

    row_index: Optional[object]
    source_value_type: str
    target_dtype: str
    reason_code: ReasonCode
    message: str


@dataclass(frozen=True)
class ColumnConversionResult:
    """
    The complete outcome of analyzing one column against one target
    dtype. converted_series is populated only when status is SAFE;
    every other status carries converted_series=None, since "no
    partially converted column may be returned" (spec Section 4.2) --
    there is no such thing as a partially-successful result object.

    Counting semantics (the spec doesn't pin down a value-level example
    for every mixed-outcome case, so this is made explicit and applied
    consistently). total_count = the complete series length, always.
    null_count = the original null-position count, always, regardless
    of outcome. converted_count and failed_count are computed per
    non-null-that-was-analyzed row, based on whether that row
    individually passes its own conversion check -- this is true even
    when the overall status is not SAFE, since it's meaningfully more
    informative than collapsing everything to zero (e.g. "3 of 100
    rows failed" rather than just "failed").

    IMPORTANT: total_count = null_count + converted_count + failed_count
    does NOT hold in general -- confirmed directly, and it is a real
    overlap, not a bug to paper over with a false equation. For a
    null-incompatible target (int64, bool), a null row is counted in
    BOTH null_count (it is a null in the original input) AND
    failed_count (it is also a rejected row, carrying its own
    NULL_TARGET_INCOMPATIBLE diagnostic). Concretely, for
    pandas.Series([1, None]) -> int64: total_count=2, null_count=1,
    converted_count=1, failed_count=1 -- the sum (3) exceeds
    total_count (2) because the null row is double-counted by design,
    not by accident. For a null-preserving target (float64, object), a
    null row is counted ONLY in null_count -- neither converted_count
    nor failed_count include it, since it is preserved as-is rather
    than converted or rejected, and in that case the equation happens
    to hold. This distinction is covered directly by
    test_count_semantics_for_null_incompatible_target and
    test_count_semantics_for_null_preserving_target in the test suite.
    """

    status: ConversionStatus
    source_dtype: str
    target_dtype: str
    total_count: int
    null_count: int
    converted_count: int
    failed_count: int
    diagnostics: Tuple[ConversionDiagnostic, ...]
    converted_series: Optional[pd.Series]
    policy_version: str

    def __post_init__(self):
        # tuple(self.diagnostics) defensively copies whatever
        # sequence was supplied (a list, a generator, an actual tuple)
        # into a genuine tuple -- confirmed directly that passing a
        # list let a caller mutate it after construction and change
        # the supposedly-frozen result's diagnostics, since frozen
        # only prevents field REASSIGNMENT, not mutation of a mutable
        # object a field happens to point at.
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))

        if self.status == ConversionStatus.SAFE:
            if self.converted_series is None:
                raise ValueError(
                    "A SAFE ColumnConversionResult must have a converted_series; "
                    "got None."
                )
        else:
            if self.converted_series is not None:
                raise ValueError(
                    f"A {self.status} ColumnConversionResult must not have a "
                    f"converted_series -- there is no such thing as a partially "
                    f"successful result. Got a non-None converted_series for "
                    f"status {self.status}."
                )
