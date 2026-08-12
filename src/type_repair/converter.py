"""
Aegis_TypeRepairConverter
Phase 3.1.2 -- the pure, deterministic conversion analyser defined by
Docs/phase3_1_verified_type_repair_spec.md. Replaces the question
"does astype() raise" with "does every value provably preserve its
meaning" -- confirmed directly, repeatedly, in Phase 2.5 that the
former lets float64->int64 truncate, object->bool corrupt via Python
string-truthiness, and int64->float64 silently lose precision beyond
2**53, all while reporting success.

Pure Python + pandas + NumPy only. No FastAPI, SQLAlchemy, database
connectors, or Surgeon import -- this module must be safely testable
with nothing but Python and pandas installed.
"""

import math
from decimal import Decimal, InvalidOperation

import numpy as np
import pandas as pd

from .models import ColumnConversionResult, ConversionDiagnostic, ConversionStatus, ReasonCode
from .policy import ConversionPolicy, DEFAULT_POLICY, INT64_MIN, INT64_MAX


_NON_FINITE_STRING_TOKENS = {
    "nan", "-nan", "+nan",
    "inf", "-inf", "+inf",
    "infinity", "-infinity", "+infinity",
}


def _is_null(value) -> bool:
    """Return whether *value* is a supported scalar null sentinel.

    ``pandas.isna`` probes several object protocols. Unsupported user
    objects are allowed to raise ordinary exceptions from those protocols;
    that must not convert a simple unsupported value (or a valid object
    widening) into an internal engine error. Both the probe and the scalar
    truth conversion therefore live inside one ordinary-``Exception``
    boundary. ``BaseException`` subclasses such as ``KeyboardInterrupt``
    and ``SystemExit`` are deliberately not swallowed.
    """
    try:
        result = pd.isna(value)
        # Array-like results do not describe one scalar cell. Treat them as
        # non-null and let the source classifier decide what the value is.
        if isinstance(result, (list, tuple, np.ndarray, pd.Series)):
            return False
        return bool(result)
    except Exception:
        return False


def _discover_numpy_scalar_types(abstract_type) -> frozenset:
    """Discover genuine platform NumPy scalar classes by exact identity.

    NumPy aliases are platform-dependent: for example ``longlong`` and
    ``ulonglong`` may be distinct from ``int64`` and ``uint64``. Building
    from NumPy's scalar registry includes every genuine class available on
    the running platform while exact ``type(value)`` membership still rejects
    user-defined subclasses.
    """
    return frozenset(
        scalar_type
        for scalar_type in np.sctypeDict.values()
        if isinstance(scalar_type, type) and issubclass(scalar_type, abstract_type)
    )


# The only trusted scalar source classes. Exact type identity prevents
# method-overriding subclasses from changing the value observed by int(),
# float(), strip(), lower(), or comparison operations.
_APPROVED_NUMPY_INTEGER_TYPES = frozenset(
    scalar_type
    for scalar_type in _discover_numpy_scalar_types(np.integer)
    if scalar_type not in {np.bool_, np.timedelta64}
)
_APPROVED_NUMPY_FLOAT_TYPES = _discover_numpy_scalar_types(np.floating)
_APPROVED_INTEGER_TYPES = frozenset({int}) | _APPROVED_NUMPY_INTEGER_TYPES
_APPROVED_FLOAT_TYPES = frozenset({float}) | _APPROVED_NUMPY_FLOAT_TYPES
_APPROVED_BOOL_TYPES = frozenset({bool, np.bool_})


def _is_bool_like(value) -> bool:
    return type(value) in _APPROVED_BOOL_TYPES


def _is_timedelta_like(value) -> bool:
    """
    numpy.timedelta64 surprisingly inherits from numpy.integer --
    confirmed directly (isinstance(np.timedelta64(1, "D"), np.integer)
    is True). Retained as a standalone, explicit check (even though
    exact-type checking in _is_integer_like already excludes it
    naturally, since np.timedelta64 is not in _APPROVED_INTEGER_TYPES)
    because it documents this specific, easy-to-miss NumPy quirk by
    name rather than relying on readers to notice its absence from a
    set.
    """
    return type(value) is np.timedelta64


def _is_integer_like(value) -> bool:
    return (
        type(value) in _APPROVED_INTEGER_TYPES
        and not _is_timedelta_like(value)
    )


def _is_float_like(value) -> bool:
    return type(value) in _APPROVED_FLOAT_TYPES


def _is_string_like(value) -> bool:
    return type(value) is str


def _value_type_name(value) -> str:
    return type(value).__name__


def _is_finite_native(value) -> bool:
    """
    Checks finiteness at the value's own precision. For a plain Python
    float or numpy.float64 this is equivalent to math.isfinite(); for
    an extended-precision numpy type (e.g. numpy.longdouble) np.isfinite
    evaluates at that type's own precision rather than silently
    reducing to float64 first.
    """
    return bool(np.isfinite(value))


def _is_integral_native(value) -> bool:
    """
    Checks whether value has zero fractional component using the
    value's OWN native precision (np.floor preserves the input's
    dtype). Confirmed directly this matters: reducing an
    extended-precision numpy.longdouble to Python float BEFORE this
    check can make a genuinely non-integral value (e.g.
    numpy.longdouble(1) + numpy.longdouble(2) ** numpy.longdouble(-63),
    whose fractional part is far smaller than float64 can represent)
    look exactly integral after the reduction, wrongly passing this
    check.
    """
    return bool(value == np.floor(value))


def _float64_roundtrip_preserves_value(value) -> bool:
    """
    True if converting value to float64 and back to its own original
    type reproduces the exact original value. Trivially true for a
    plain Python float or numpy.float64, since those are already their
    own exact float64 representation. Confirmed directly this can
    genuinely be False for numpy.longdouble (or any other
    wider-than-float64 numpy floating type a platform might provide) --
    without this check, such a value would be silently reduced to
    float64 and treated as exact, even when it demonstrably is not.
    """
    original_type = type(value)
    as_float64 = float(value)
    reconstructed = original_type(as_float64)
    return bool(reconstructed == value)


# ---- int64 ----

# Boundary digit strings for int64 range checking without unrestricted
# int() parsing -- see _int64_from_canonical_string. These match the
# LOCKED INT64_MIN/INT64_MAX exactly (ConversionPolicy's own
# validation guarantees policy.int64_min/int64_max can never differ
# from these), so they're safe to hardcode rather than stringify on
# every call.
_INT64_MAX_MAGNITUDE_DIGITS = "9223372036854775807"  # str(2**63 - 1), 19 digits
_INT64_MIN_MAGNITUDE_DIGITS = "9223372036854775808"  # str(2**63), 19 digits
_INT64_BOUNDARY_DIGIT_COUNT = len(_INT64_MAX_MAGNITUDE_DIGITS)


def _int64_from_canonical_string(s: str):
    """
    Determines whether a canonical integer string (already confirmed
    to match the strict grammar: optional sign, no leading zeros
    except a bare "0") fits within int64 range purely from its digit
    length and, only when exactly at the boundary length, a
    lexicographic comparison against the known boundary digit
    strings -- WITHOUT ever calling int() on a string whose length
    hasn't already been proven small enough.

    Confirmed directly this matters: Python 3.11+ limits unrestricted
    int(str) conversion via sys.get_int_max_str_digits() (4300 by
    default, but process-wide and caller-configurable down to a
    minimum of 640) -- calling int() on an unbounded-length string can
    itself raise ValueError for a value that is simply out of int64
    range, not an internal engine failure, and the exact threshold at
    which this happens depends on process-wide state outside this
    engine's control, which is not acceptable for a conversion that
    must be deterministic. This function never calls int() on a
    string longer than 20 characters (an optional sign plus 19
    digits) -- comfortably below even the smallest value Python
    permits for that limit (640).

    A plain string comparison correctly determines numeric ordering
    here because both operands are the same length and contain only
    ASCII digit characters (guaranteed by the grammar match that
    happens before this is ever called).
    """
    negative = s.startswith("-")
    digits = s[1:] if s and s[0] in "+-" else s

    if len(digits) < _INT64_BOUNDARY_DIGIT_COUNT:
        # Provably in range regardless of exact value -- at most 18
        # digits, nowhere near any digit-count limit.
        return int(s)
    if len(digits) > _INT64_BOUNDARY_DIGIT_COUNT:
        # Provably out of range regardless of exact value -- never
        # call int() on this, no matter how long it is.
        return ReasonCode.OUT_OF_RANGE

    boundary = _INT64_MIN_MAGNITUDE_DIGITS if negative else _INT64_MAX_MAGNITUDE_DIGITS
    if digits > boundary:
        return ReasonCode.OUT_OF_RANGE
    return int(s)


def _is_within_int64_range(int_value: int, policy: ConversionPolicy) -> bool:
    """
    Independently enforces the canonical int64 boundaries using the
    hardcoded INT64_MIN/INT64_MAX constants, in addition to (not
    instead of) policy.int64_min/int64_max. Confirmed directly this
    matters: a custom object with its own comparison dunder methods
    (e.g. one whose __ge__/__le__ always return an attacker-chosen
    result) could previously be passed as int64_max and defeat
    range validation entirely, letting INT64_MAX + 1 report SAFE with
    the value silently wrapped to INT64_MIN once materialized into a
    NumPy int64 array. ConversionPolicy's own validation now requires
    int64_min/int64_max to be exact built-in int values, which closes
    that specific hole at construction time -- this check is the
    converter's OWN independent enforcement, so its safety does not
    rest on trusting the policy object alone.
    """
    return (
        INT64_MIN <= int_value <= INT64_MAX
        and policy.int64_min <= int_value <= policy.int64_max
    )


def _analyze_value_for_int64(value, policy: ConversionPolicy):
    """Returns an int on success, or a ReasonCode on rejection."""
    if _is_bool_like(value):
        return ReasonCode.UNSUPPORTED_SOURCE_VALUE
    if _is_integer_like(value):
        int_value = int(value)
        if _is_within_int64_range(int_value, policy):
            return int_value
        return ReasonCode.OUT_OF_RANGE
    if _is_float_like(value):
        if not _is_finite_native(value):
            return ReasonCode.NON_FINITE_NUMBER
        if not _is_integral_native(value):
            return ReasonCode.FRACTIONAL_VALUE
        # Compute the integer DIRECTLY from the value's own native
        # precision -- int64 has no dependency on float64
        # representability at all. Confirmed directly: int() on a
        # numpy floating scalar, including an extended-precision type
        # like numpy.longdouble, preserves full native precision with
        # no lossy float64 intermediate (int(np.longdouble(2**53+1))
        # gives the exact 9007199254740993, not the float64-rounded
        # 9007199254740992). Requiring a float64 round-trip here, as
        # an earlier version of this code did, was itself a real bug:
        # it wrongly rejected exact, in-range integers like 2**53+1 or
        # INT64_MAX represented as numpy.longdouble, neither of which
        # ever needed to pass through float64 to produce the correct
        # int64 value. The float64 round-trip check remains correct
        # and necessary for the float64 TARGET below, where the
        # output genuinely must be an exact float64 value -- it was
        # simply never applicable to int64's own correctness.
        try:
            int_value = int(value)
        except (OverflowError, ValueError):
            return ReasonCode.OUT_OF_RANGE
        if _is_within_int64_range(int_value, policy):
            return int_value
        return ReasonCode.OUT_OF_RANGE
    if _is_string_like(value):
        if value.lower() in _NON_FINITE_STRING_TOKENS:
            return ReasonCode.NON_FINITE_NUMBER
        if not policy.match_integer_string(value):
            return ReasonCode.PARSE_ERROR
        return _int64_from_canonical_string(value)
    return ReasonCode.UNSUPPORTED_SOURCE_VALUE


# ---- float64 ----

def _analyze_value_for_float64(value, policy: ConversionPolicy):
    """Returns a float on success, or a ReasonCode on rejection."""
    if _is_bool_like(value):
        return ReasonCode.UNSUPPORTED_SOURCE_VALUE
    if _is_integer_like(value):
        int_value = int(value)
        try:
            float_value = float(int_value)
        except OverflowError:
            return ReasonCode.PRECISION_LOSS
        if not math.isfinite(float_value):
            return ReasonCode.PRECISION_LOSS
        # Round-trip: float -> int must reproduce the exact integer
        # (spec 9.2) -- protects integers beyond binary64's exact
        # integer range (2**53), confirmed directly in Phase 2.5 that
        # naive astype() silently loses this precision.
        if int(float_value) != int_value:
            return ReasonCode.PRECISION_LOSS
        return float_value
    if _is_float_like(value):
        if not _is_finite_native(value):
            return ReasonCode.NON_FINITE_NUMBER
        if not _float64_roundtrip_preserves_value(value):
            # Confirmed directly: an extended-precision numpy type
            # (e.g. numpy.longdouble) can hold a value that is NOT
            # exactly representable in float64, even when it looks
            # like an ordinary float from the outside. A plain Python
            # float or numpy.float64 always passes this trivially,
            # since it is already its own exact float64 representation.
            return ReasonCode.PRECISION_LOSS
        return float(value)
    if _is_string_like(value):
        if value.lower() in _NON_FINITE_STRING_TOKENS:
            return ReasonCode.NON_FINITE_NUMBER
        if not policy.match_decimal_string(value):
            return ReasonCode.PARSE_ERROR
        try:
            exact_decimal = Decimal(value)
        except InvalidOperation:
            return ReasonCode.PARSE_ERROR
        float_value = float(value)
        if not math.isfinite(float_value):
            return ReasonCode.NON_FINITE_NUMBER
        # Round-trip: does the float's OWN exact binary value equal
        # the original decimal exactly? Decimal(a_float) gives the
        # exact value that float actually holds (e.g. Decimal(0.1) is
        # 0.1000000000000000055511151231257827021181583404541015625,
        # not 0.1) -- this is precisely what makes "0.1" a rejection
        # under this policy: it cannot be represented exactly in
        # binary64, only approximated.
        float_as_exact_decimal = Decimal(float_value)
        if float_as_exact_decimal != exact_decimal:
            return ReasonCode.PRECISION_LOSS
        return float_value
    return ReasonCode.UNSUPPORTED_SOURCE_VALUE


# ---- bool ----

def _analyze_value_for_bool(value, policy: ConversionPolicy):
    """Returns a bool on success, or a ReasonCode on rejection."""
    if _is_bool_like(value):
        return bool(value)
    if _is_integer_like(value):
        int_value = int(value)
        if int_value == 0:
            return False
        if int_value == 1:
            return True
        return ReasonCode.AMBIGUOUS_BOOLEAN
    if _is_float_like(value):
        # ALL floats rejected, including 0.0/1.0 -- spec 9.3 lists
        # "float 0.0/1.0 rejection" explicitly. Only native bool and
        # native int 0/1 are accepted directly; a float is never
        # treated as an integer here even when mathematically equal.
        return ReasonCode.AMBIGUOUS_BOOLEAN
    if _is_string_like(value):
        token = policy.lookup_bool_token(value)
        if token is None:
            return ReasonCode.AMBIGUOUS_BOOLEAN
        return token
    return ReasonCode.UNSUPPORTED_SOURCE_VALUE


# ---- object ----

def _widen_to_object(series: pd.Series) -> pd.Series:
    """
    Non-transforming widening: every value keeps its own Python
    identity where pandas' own object-dtype storage permits (an
    already-object-dtype series' elements are preserved as the same
    references; a native-dtype series' elements are boxed into their
    equivalent Python objects, which is the standard, expected
    behavior of astype(object) and not a transformation of VALUE).
    Returns a NEW Series -- the input is never mutated.
    """
    return series.astype(object)


def _get_source_dtype_str(series: pd.Series) -> str:
    return str(series.dtype)


def _safe_policy_version(policy) -> str:
    """Read a policy version without trusting user-controlled conversion."""
    try:
        version = policy.version
    except Exception:
        return "unknown"
    return version if type(version) is str else "unknown"


def _safe_null_count(series: pd.Series) -> int:
    """Count scalar null positions without invoking ``Series.isna()``.

    The vectorized pandas path may probe arbitrary object protocols and can
    fail for an otherwise preservable object value. The scalar helper is
    deliberately non-throwing for ordinary exceptions.
    """
    return sum(1 for value in series if _is_null(value))


def _build_unsupported_target_result(
    series: pd.Series,
    source_dtype_str: str,
    target_label: str,
    policy,
) -> ColumnConversionResult:
    """Build the deterministic unsupported-target outcome."""
    total_count = len(series)
    null_count = _safe_null_count(series)
    return ColumnConversionResult(
        status=ConversionStatus.UNSUPPORTED,
        source_dtype=source_dtype_str,
        target_dtype=target_label,
        total_count=total_count,
        null_count=null_count,
        converted_count=0,
        failed_count=total_count - null_count,
        diagnostics=(
            ConversionDiagnostic(
                row_index=None,
                source_value_type=source_dtype_str,
                target_dtype=target_label,
                reason_code=ReasonCode.UNSUPPORTED_TARGET_DTYPE,
                message=_message_for(ReasonCode.UNSUPPORTED_TARGET_DTYPE, target_label),
            ),
        ),
        converted_series=None,
        policy_version=_safe_policy_version(policy),
    )


def _build_internal_error_result(
    source_dtype_str: str,
    target_dtype: str,
    total_count: int,
    null_count: int,
    policy,
) -> ColumnConversionResult:
    """Build a contained internal-error outcome with no partial data."""
    return ColumnConversionResult(
        status=ConversionStatus.ERROR,
        source_dtype=source_dtype_str,
        target_dtype=target_dtype,
        total_count=total_count,
        null_count=null_count,
        converted_count=0,
        failed_count=total_count - null_count,
        diagnostics=(
            ConversionDiagnostic(
                row_index=None,
                source_value_type=source_dtype_str,
                target_dtype=target_dtype,
                reason_code=ReasonCode.INTERNAL_CONVERSION_ERROR,
                message=_message_for(ReasonCode.INTERNAL_CONVERSION_ERROR, target_dtype),
            ),
        ),
        converted_series=None,
        policy_version=_safe_policy_version(policy),
    )


def _build_diagnostic(row_index, value, target_dtype: str, reason_code: ReasonCode) -> ConversionDiagnostic:
    return ConversionDiagnostic(
        row_index=row_index,
        source_value_type=_value_type_name(value),
        target_dtype=target_dtype,
        reason_code=reason_code,
        message=_message_for(reason_code, target_dtype),
    )


def _message_for(reason_code: ReasonCode, target_dtype: str) -> str:
    """
    Human-readable summary only -- deliberately generic and never
    includes the actual source value (spec Section 6: "Normal
    diagnostic messages must not embed raw source values"). Tests
    assert against reason_code, not this text.
    """
    return {
        ReasonCode.PARSE_ERROR: f"Value could not be parsed as {target_dtype}.",
        ReasonCode.FRACTIONAL_VALUE: f"Value has a non-zero fractional component and cannot convert to {target_dtype}.",
        ReasonCode.OUT_OF_RANGE: f"Value is outside the representable range for {target_dtype}.",
        ReasonCode.PRECISION_LOSS: f"Value cannot be represented in {target_dtype} without losing precision.",
        ReasonCode.AMBIGUOUS_BOOLEAN: "Value is not one of the recognized boolean tokens.",
        ReasonCode.NON_FINITE_NUMBER: f"Value is not finite and cannot convert to {target_dtype}.",
        ReasonCode.NULL_TARGET_INCOMPATIBLE: f"{target_dtype} cannot represent a null value.",
        ReasonCode.TIMEZONE_LOSS: "Converting this value would lose timezone information.",
        ReasonCode.UNSUPPORTED_SOURCE_VALUE: f"This value's type has no approved converter to {target_dtype}.",
        ReasonCode.UNSUPPORTED_TARGET_DTYPE: f"Target dtype {target_dtype!r} is not in the supported conversion matrix.",
        ReasonCode.ROUND_TRIP_MISMATCH: f"Value did not survive an exact round-trip check for {target_dtype}.",
        ReasonCode.INTERNAL_CONVERSION_ERROR: "An internal error occurred during conversion analysis.",
    }[reason_code]


_NULL_INCOMPATIBLE_TARGETS = frozenset({"int64", "bool"})
# This module's OWN hardcoded set of targets it actually has analyzers
# for -- checked independently of, and in addition to,
# policy.is_supported_target(). A policy object claiming some other
# target is "supported" (whether through a future bug, a malformed
# construction path, or any other means) must never be trusted alone;
# confirmed directly that trusting it alone let an unregistered target
# reach a raw KeyError on _VALUE_ANALYZERS, surfacing as
# ERROR/INTERNAL_CONVERSION_ERROR instead of the correct
# UNSUPPORTED/UNSUPPORTED_TARGET_DTYPE.
_CONVERTER_KNOWN_TARGETS = frozenset({"int64", "float64", "bool", "object"})

_VALUE_ANALYZERS = {
    "int64": _analyze_value_for_int64,
    "float64": _analyze_value_for_float64,
    "bool": _analyze_value_for_bool,
}


def _analyze_and_convert_impl(
    series: pd.Series, target_dtype: str, policy: ConversionPolicy
) -> ColumnConversionResult:
    source_dtype_str = _get_source_dtype_str(series)

    # Public target boundary: never invoke virtual string methods on an
    # untrusted target object. Exact built-in strings are the only accepted
    # target inputs. A str subclass may override strip()/lower() and silently
    # redirect one requested conversion into another.
    if type(target_dtype) is not str:
        return _build_unsupported_target_result(
            series, source_dtype_str, "unknown", policy
        )

    # Only the exact, validated ConversionPolicy model may authorize an
    # executable conversion. Duck-typed or subclassed policy objects can
    # override parsers, token lookups, bounds, or target support. For an
    # unknown target we can still return the precise UNSUPPORTED outcome; for
    # a known executable target, an invalid policy is a contained internal
    # configuration error and no analysis is attempted.
    if type(policy) is not ConversionPolicy:
        trusted_target = DEFAULT_POLICY.normalize_target_dtype(target_dtype)
        if trusted_target not in _CONVERTER_KNOWN_TARGETS:
            return _build_unsupported_target_result(
                series, source_dtype_str, trusted_target, policy
            )
        return _build_internal_error_result(
            source_dtype_str,
            trusted_target,
            len(series),
            _safe_null_count(series),
            policy,
        )

    normalized_target = policy.normalize_target_dtype(target_dtype)
    if type(normalized_target) is not str:
        return _build_unsupported_target_result(
            series, source_dtype_str, "unknown", policy
        )

    # Independent converter registry check: policy metadata alone cannot make
    # an unimplemented target executable.
    if (
        not policy.is_supported_target(normalized_target)
        or normalized_target not in _CONVERTER_KNOWN_TARGETS
    ):
        return _build_unsupported_target_result(
            series, source_dtype_str, normalized_target, policy
        )

    total_count = len(series)
    if normalized_target == "object":
        return _analyze_object(
            series, policy, source_dtype_str, normalized_target, total_count
        )

    return _analyze_scalar_target(
        series, policy, source_dtype_str, normalized_target, total_count
    )


def _analyze_scalar_target(
    series: pd.Series, policy: ConversionPolicy, source_dtype_str: str,
    normalized_target: str, total_count: int,
) -> ColumnConversionResult:
    # Second, explicit defensive check immediately before indexing the
    # registry -- belt and suspenders with the check in
    # _analyze_and_convert_impl above, since this function could in
    # principle be reached some other way in the future. Never index a
    # dict with a value whose membership hasn't just been confirmed,
    # regardless of how many callers already believe it's safe.
    if normalized_target not in _VALUE_ANALYZERS:
        return _build_unsupported_target_result(
            series, source_dtype_str, normalized_target, policy
        )

    analyzer = _VALUE_ANALYZERS[normalized_target]
    null_incompatible = normalized_target in _NULL_INCOMPATIBLE_TARGETS

    diagnostics = []
    converted_values = []  # only meaningful if the overall result ends up SAFE
    null_count = 0
    converted_count = 0
    failed_count = 0
    has_unsupported_source = False

    for row_index, value in series.items():
        if _is_null(value):
            null_count += 1
            if null_incompatible:
                failed_count += 1
                diagnostics.append(
                    _build_diagnostic(row_index, value, normalized_target, ReasonCode.NULL_TARGET_INCOMPATIBLE)
                )
            else:
                # float64 preserves the null position; nothing to
                # convert for this row, and it is not a failure.
                converted_values.append(None)
            continue

        outcome = analyzer(value, policy)
        if isinstance(outcome, ReasonCode):
            failed_count += 1
            if outcome == ReasonCode.UNSUPPORTED_SOURCE_VALUE:
                has_unsupported_source = True
            diagnostics.append(_build_diagnostic(row_index, value, normalized_target, outcome))
        else:
            converted_count += 1
            converted_values.append(outcome)

    if diagnostics:
        status = ConversionStatus.UNSUPPORTED if has_unsupported_source else ConversionStatus.UNSAFE
        return ColumnConversionResult(
            status=status,
            source_dtype=source_dtype_str,
            target_dtype=normalized_target,
            total_count=total_count,
            null_count=null_count,
            converted_count=converted_count,
            failed_count=failed_count,
            diagnostics=tuple(diagnostics),
            converted_series=None,
            policy_version=policy.version,
        )

    # Every row is null-compatible-preserved or successfully converted.
    numpy_dtype = "float64" if normalized_target == "float64" else "int64" if normalized_target == "int64" else "bool"

    # Final, independent validation immediately before materialization.
    # This boundary does not trust any upstream analyzer: a future internal
    # fault must not be able to coerce, round, wrap, or truth-cast a produced
    # value while the public result reports SAFE.
    produced_null_count = sum(value is None for value in converted_values)
    if produced_null_count != null_count:
        return _build_internal_error_result(
            source_dtype_str, normalized_target, total_count, null_count, policy
        )
    if normalized_target != "float64" and produced_null_count:
        return _build_internal_error_result(
            source_dtype_str, normalized_target, total_count, null_count, policy
        )

    for produced_value in converted_values:
        if produced_value is None:
            continue

        value_is_valid = (
            normalized_target == "int64"
            and type(produced_value) is int
            and INT64_MIN <= produced_value <= INT64_MAX
        ) or (
            normalized_target == "float64"
            and type(produced_value) is float
            and math.isfinite(produced_value)
        ) or (
            normalized_target == "bool"
            and type(produced_value) is bool
        )

        if not value_is_valid:
            return _build_internal_error_result(
                source_dtype_str, normalized_target, total_count, null_count, policy
            )

    converted_series = pd.Series(converted_values, index=series.index, name=series.name)
    converted_series = converted_series.astype(numpy_dtype)

    expected_dtype = numpy_dtype
    metadata_ok = (
        type(converted_series) is pd.Series
        and len(converted_series) == total_count
        and converted_series.index.equals(series.index)
        and converted_series.name == series.name
        and str(converted_series.dtype) == expected_dtype
    )
    if not metadata_ok:
        return _build_internal_error_result(
            source_dtype_str, normalized_target, total_count, null_count, policy
        )

    for produced_value, materialized_value in zip(
        converted_values, converted_series.tolist()
    ):
        if produced_value is None:
            if not _is_null(materialized_value):
                return _build_internal_error_result(
                    source_dtype_str, normalized_target, total_count, null_count, policy
                )
            continue
        if normalized_target == "int64":
            values_match = int(materialized_value) == produced_value
        elif normalized_target == "bool":
            values_match = bool(materialized_value) is produced_value
        else:
            values_match = (
                np.float64(materialized_value).view(np.uint64)
                == np.float64(produced_value).view(np.uint64)
            )
        if not bool(values_match):
            return _build_internal_error_result(
                source_dtype_str, normalized_target, total_count, null_count, policy
            )

    return ColumnConversionResult(
        status=ConversionStatus.SAFE,
        source_dtype=source_dtype_str,
        target_dtype=normalized_target,
        total_count=total_count,
        null_count=null_count,
        converted_count=converted_count,
        failed_count=failed_count,
        diagnostics=tuple(),
        converted_series=converted_series,
        policy_version=policy.version,
    )


def _analyze_object(
    series: pd.Series, policy: ConversionPolicy, source_dtype_str: str,
    normalized_target: str, total_count: int,
) -> ColumnConversionResult:
    null_count = _safe_null_count(series)
    non_null_count = total_count - null_count

    try:
        converted_series = _widen_to_object(series)
        # Structural invariants are part of the object-widening contract,
        # not optional metadata: the result must be a new ordinary Series
        # with identical row count, index, name, order and object dtype.
        structure_matches = (
            type(converted_series) is pd.Series
            and converted_series is not series
            and len(converted_series) == total_count
            and converted_series.index.equals(series.index)
            and converted_series.name == series.name
            and str(converted_series.dtype) == "object"
        )
        if not structure_matches:
            return ColumnConversionResult(
                status=ConversionStatus.UNSAFE,
                source_dtype=source_dtype_str,
                target_dtype=normalized_target,
                total_count=total_count,
                null_count=null_count,
                converted_count=0,
                failed_count=non_null_count,
                diagnostics=(
                    ConversionDiagnostic(
                        row_index=None,
                        source_value_type=source_dtype_str,
                        target_dtype=normalized_target,
                        reason_code=ReasonCode.ROUND_TRIP_MISMATCH,
                        message=_message_for(ReasonCode.ROUND_TRIP_MISMATCH, normalized_target),
                    ),
                ),
                converted_series=None,
                policy_version=policy.version,
            )

        # Defensive round-trip check: every position must compare equal
        # (including null-to-null) between input and output.
        mismatch_row_index = None
        for row_index, (original, widened) in enumerate(zip(series, converted_series)):
            original_is_null = _is_null(original)
            widened_is_null = _is_null(widened)
            if original_is_null != widened_is_null:
                mismatch_row_index = series.index[row_index]
                break
            if not original_is_null and original is not widened and original != widened:
                mismatch_row_index = series.index[row_index]
                break
    except Exception:
        return ColumnConversionResult(
            status=ConversionStatus.ERROR,
            source_dtype=source_dtype_str,
            target_dtype=normalized_target,
            total_count=total_count,
            null_count=null_count,
            converted_count=0,
            failed_count=non_null_count,
            diagnostics=(
                ConversionDiagnostic(
                    row_index=None,
                    source_value_type=source_dtype_str,
                    target_dtype=normalized_target,
                    reason_code=ReasonCode.INTERNAL_CONVERSION_ERROR,
                    message=_message_for(ReasonCode.INTERNAL_CONVERSION_ERROR, normalized_target),
                ),
            ),
            converted_series=None,
            policy_version=policy.version,
        )

    if mismatch_row_index is not None:
        return ColumnConversionResult(
            status=ConversionStatus.UNSAFE,
            source_dtype=source_dtype_str,
            target_dtype=normalized_target,
            total_count=total_count,
            null_count=null_count,
            converted_count=0,
            failed_count=non_null_count,
            diagnostics=(
                ConversionDiagnostic(
                    row_index=mismatch_row_index,
                    source_value_type=source_dtype_str,
                    target_dtype=normalized_target,
                    reason_code=ReasonCode.ROUND_TRIP_MISMATCH,
                    message=_message_for(ReasonCode.ROUND_TRIP_MISMATCH, normalized_target),
                ),
            ),
            converted_series=None,
            policy_version=policy.version,
        )

    return ColumnConversionResult(
        status=ConversionStatus.SAFE,
        source_dtype=source_dtype_str,
        target_dtype=normalized_target,
        total_count=total_count,
        null_count=null_count,
        converted_count=non_null_count,
        failed_count=0,
        diagnostics=tuple(),
        converted_series=converted_series,
        policy_version=policy.version,
    )


def analyze_and_convert(
    series: pd.Series, target_dtype: str, *, policy: ConversionPolicy = DEFAULT_POLICY
) -> ColumnConversionResult:
    """
    The primary, public operation. Analyzes the complete series before
    any value is written anywhere (spec 4.1) and never mutates the
    input series (spec 4.3, 10). Any unexpected internal failure is
    contained and returned as ERROR / INTERNAL_CONVERSION_ERROR rather
    than propagating -- the internal implementation
    (_analyze_and_convert_impl and its helpers) is the intended seam
    for a test to monkeypatch in order to exercise this path
    deterministically.
    """
    if type(series) is not pd.Series:
        safe_target = (
            DEFAULT_POLICY.normalize_target_dtype(target_dtype)
            if type(target_dtype) is str
            else "unknown"
        )
        return _build_internal_error_result(
            "unknown", safe_target, 0, 0, policy
        )

    try:
        return _analyze_and_convert_impl(series, target_dtype, policy)
    except Exception:
        try:
            source_dtype_str = _get_source_dtype_str(series)
        except Exception:
            source_dtype_str = "unknown"
        try:
            total_count = len(series)
        except Exception:
            total_count = 0
        normalized_target = "unknown"
        if type(target_dtype) is str:
            try:
                candidate_target = policy.normalize_target_dtype(target_dtype)
                if type(candidate_target) is str:
                    normalized_target = candidate_target
            except Exception:
                pass
        # Never call str()/repr() or virtual string methods on an untrusted
        # target object while constructing the error fallback.
        policy_version = _safe_policy_version(policy)
        return ColumnConversionResult(
            status=ConversionStatus.ERROR,
            source_dtype=source_dtype_str,
            target_dtype=normalized_target,
            total_count=total_count,
            null_count=0,
            converted_count=0,
            failed_count=total_count,
            diagnostics=(
                ConversionDiagnostic(
                    row_index=None,
                    source_value_type="unknown",
                    target_dtype=normalized_target,
                    reason_code=ReasonCode.INTERNAL_CONVERSION_ERROR,
                    message=_message_for(ReasonCode.INTERNAL_CONVERSION_ERROR, normalized_target),
                ),
            ),
            converted_series=None,
            policy_version=policy_version,
        )
