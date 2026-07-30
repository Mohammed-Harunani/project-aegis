"""
Pure-Python tests for src/type_repair -- the verified type-conversion
engine (Phase 3.1.2). No database, no FastAPI, no Surgeon dependency:
these tests require only Python, pandas, and NumPy, matching the
module-boundary requirement in Docs/phase3_1_verified_type_repair_spec.md
Section 14.
"""

import dataclasses
import math
from decimal import Decimal

import numpy as np
import pandas as pd

from type_repair import (
    analyze_and_convert,
    ColumnConversionResult,
    ConversionDiagnostic,
    ConversionStatus,
    ConversionPolicy,
    DEFAULT_POLICY,
    ReasonCode,
)
from type_repair import converter as converter_module


# ---------------------------------------------------------------------------
# Model and policy
# ---------------------------------------------------------------------------

def test_column_conversion_result_is_frozen():
    result = analyze_and_convert(pd.Series([1]), "int64")
    try:
        result.status = ConversionStatus.ERROR
        assert False, "expected FrozenInstanceError"
    except dataclasses.FrozenInstanceError:
        pass


def test_conversion_diagnostic_is_frozen():
    diagnostic = ConversionDiagnostic(
        row_index=0, source_value_type="str", target_dtype="int64",
        reason_code=ReasonCode.PARSE_ERROR, message="test",
    )
    try:
        diagnostic.reason_code = ReasonCode.OUT_OF_RANGE
        assert False, "expected FrozenInstanceError"
    except dataclasses.FrozenInstanceError:
        pass


def test_conversion_policy_is_frozen():
    try:
        DEFAULT_POLICY.version = "tampered"
        assert False, "expected FrozenInstanceError"
    except dataclasses.FrozenInstanceError:
        pass


def test_conversion_status_values_are_stable():
    assert ConversionStatus.SAFE == "SAFE"
    assert ConversionStatus.UNSAFE == "UNSAFE"
    assert ConversionStatus.UNSUPPORTED == "UNSUPPORTED"
    assert ConversionStatus.ERROR == "ERROR"


def test_reason_code_values_are_stable():
    expected = {
        "PARSE_ERROR", "FRACTIONAL_VALUE", "OUT_OF_RANGE", "PRECISION_LOSS",
        "AMBIGUOUS_BOOLEAN", "NON_FINITE_NUMBER", "NULL_TARGET_INCOMPATIBLE",
        "TIMEZONE_LOSS", "UNSUPPORTED_SOURCE_VALUE", "UNSUPPORTED_TARGET_DTYPE",
        "ROUND_TRIP_MISMATCH", "INTERNAL_CONVERSION_ERROR",
    }
    actual = {code.value for code in ReasonCode}
    assert expected.issubset(actual)


def test_default_policy_version_is_exact():
    assert DEFAULT_POLICY.version == "phase3.1-policy-v1"


def test_target_normalization_is_deterministic():
    assert DEFAULT_POLICY.normalize_target_dtype("  INT64  ") == "int64"
    assert DEFAULT_POLICY.normalize_target_dtype("OBJECT") == "object"
    assert DEFAULT_POLICY.normalize_target_dtype("Bool") == "bool"
    assert DEFAULT_POLICY.normalize_target_dtype("FLOAT64") == "float64"


def test_extension_dtype_spellings_never_fold_into_supported_targets():
    """
    "Int64" (pandas' nullable extension type) must never be treated as
    equivalent to "int64" (numpy dtype) just because it matches after
    lowercasing -- confirmed as a real bug in this engine's own first
    draft, caught by this test, before it shipped.
    """
    for blocked in ["Int64", "Float64", "Boolean"]:
        normalized = DEFAULT_POLICY.normalize_target_dtype(blocked)
        assert not DEFAULT_POLICY.is_supported_target(normalized), blocked
        result = analyze_and_convert(pd.Series([1]), blocked)
        assert result.status == ConversionStatus.UNSUPPORTED, blocked


def test_supported_targets_are_exactly_four():
    assert DEFAULT_POLICY.supported_targets == frozenset({"int64", "float64", "bool", "object"})


def test_unsupported_targets_remain_unsupported():
    for target in ["decimal", "date", "datetime", "datetime_tz", "uuid", "json",
                    "Int64", "Float64", "boolean", "string", "str", "category"]:
        result = analyze_and_convert(pd.Series([1]), target)
        assert result.status == ConversionStatus.UNSUPPORTED, f"{target} should be UNSUPPORTED"
        assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_TARGET_DTYPE
        assert result.converted_series is None


# ---------------------------------------------------------------------------
# int64
# ---------------------------------------------------------------------------

def test_int64_python_and_numpy_integer_success():
    result = analyze_and_convert(pd.Series([1, 2, np.int64(3)], dtype=object), "int64")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [1, 2, 3]
    assert str(result.converted_series.dtype) == "int64"


def test_int64_signed_boundaries_success():
    result = analyze_and_convert(
        pd.Series([-9223372036854775808, 9223372036854775807], dtype=object), "int64"
    )
    assert result.status == ConversionStatus.SAFE


def test_int64_overflow_rejection():
    result = analyze_and_convert(pd.Series([9223372036854775808], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.OUT_OF_RANGE
    assert result.converted_series is None


def test_int64_underflow_rejection():
    result = analyze_and_convert(pd.Series([-9223372036854775809], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.OUT_OF_RANGE


def test_int64_integral_finite_float_success():
    result = analyze_and_convert(pd.Series([5.0], dtype=object), "int64")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [5]


def test_int64_fractional_float_rejection():
    result = analyze_and_convert(pd.Series([1.2], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.FRACTIONAL_VALUE


def test_int64_canonical_signed_string_success():
    result = analyze_and_convert(pd.Series(["-42", "+7", "0"], dtype=object), "int64")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [-42, 7, 0]


def test_int64_leading_zero_rejection():
    result = analyze_and_convert(pd.Series(["01"], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.PARSE_ERROR


def test_int64_decimal_and_exponent_string_rejection():
    for value in ["1.0", "1e3"]:
        result = analyze_and_convert(pd.Series([value], dtype=object), "int64")
        assert result.status == ConversionStatus.UNSAFE, value
        assert result.diagnostics[0].reason_code == ReasonCode.PARSE_ERROR, value


def test_int64_whitespace_string_rejection():
    for value in [" 1", "1 ", " 1 "]:
        result = analyze_and_convert(pd.Series([value], dtype=object), "int64")
        assert result.status == ConversionStatus.UNSAFE, repr(value)
        assert result.diagnostics[0].reason_code == ReasonCode.PARSE_ERROR, repr(value)


def test_int64_comma_string_rejection():
    result = analyze_and_convert(pd.Series(["1,000"], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.PARSE_ERROR


def test_int64_boolean_rejection():
    result = analyze_and_convert(pd.Series([True, False], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSUPPORTED
    assert all(d.reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE for d in result.diagnostics)
    assert result.converted_series is None


def test_int64_non_finite_rejection():
    result = analyze_and_convert(pd.Series([float("inf"), float("-inf")], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSAFE
    assert all(d.reason_code == ReasonCode.NON_FINITE_NUMBER for d in result.diagnostics)


def test_int64_null_incompatibility():
    result = analyze_and_convert(pd.Series([1, None, 3]), "int64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.NULL_TARGET_INCOMPATIBLE
    assert result.converted_series is None
    assert result.null_count == 1


# ---------------------------------------------------------------------------
# float64
# ---------------------------------------------------------------------------

def test_float64_ordinary_finite_float_success():
    result = analyze_and_convert(pd.Series([3.14, -2.5], dtype=object), "float64")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [3.14, -2.5]


def test_float64_exactly_representable_integer_success():
    result = analyze_and_convert(pd.Series([5, -100], dtype=object), "float64")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [5.0, -100.0]


def test_float64_large_integer_precision_loss_rejection():
    result = analyze_and_convert(pd.Series([9007199254740993], dtype=object), "float64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.PRECISION_LOSS


def test_float64_exact_decimal_string_success():
    result = analyze_and_convert(pd.Series(["2.5", "100.25"], dtype=object), "float64")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [2.5, 100.25]


def test_float64_zero_point_one_exactness_rejection():
    result = analyze_and_convert(pd.Series(["0.1"], dtype=object), "float64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.PRECISION_LOSS
    # Confirm the underlying reasoning directly, not just trust the code path
    assert Decimal(0.1) != Decimal("0.1")


def test_float64_exponent_whitespace_comma_underscore_rejection():
    for value in ["1e3", " 1.0", "1.0 ", "1,000.5", "1_000.5"]:
        result = analyze_and_convert(pd.Series([value], dtype=object), "float64")
        assert result.status == ConversionStatus.UNSAFE, repr(value)
        assert result.diagnostics[0].reason_code == ReasonCode.PARSE_ERROR, repr(value)


def test_float64_non_finite_rejection():
    result = analyze_and_convert(pd.Series([float("inf"), float("nan")], dtype=object), "float64")
    # NaN is treated as null (preserved), not as a non-finite failure --
    # only inf/-inf as an actual VALUE reach the non-finite check.
    # Isolate inf alone to test the rejection path cleanly:
    inf_only = analyze_and_convert(pd.Series([float("inf")], dtype=object), "float64")
    assert inf_only.status == ConversionStatus.UNSAFE
    assert inf_only.diagnostics[0].reason_code == ReasonCode.NON_FINITE_NUMBER


def test_float64_null_position_preservation():
    result = analyze_and_convert(pd.Series([1.0, None, 3.0]), "float64")
    assert result.status == ConversionStatus.SAFE
    assert result.null_count == 1
    assert pd.isna(result.converted_series.iloc[1])
    assert result.converted_series.iloc[0] == 1.0
    assert result.converted_series.iloc[2] == 3.0


# ---------------------------------------------------------------------------
# bool
# ---------------------------------------------------------------------------

def test_bool_native_boolean_success():
    result = analyze_and_convert(pd.Series([True, False], dtype=object), "bool")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [True, False]


def test_bool_integer_zero_one_success():
    result = analyze_and_convert(pd.Series([1, 0], dtype=object), "bool")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [True, False]


def test_bool_strict_string_token_success():
    result = analyze_and_convert(pd.Series(["true", "false", "1", "0"], dtype=object), "bool")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [True, False, True, False]


def test_bool_case_insensitive_token_success():
    result = analyze_and_convert(pd.Series(["TRUE", "False", "tRuE"], dtype=object), "bool")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [True, False, True]


def test_bool_ascii_whitespace_token_success():
    result = analyze_and_convert(pd.Series([" true", "false\t", "\ntrue\r"], dtype=object), "bool")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [True, False, True]


def test_bool_other_integer_rejection():
    result = analyze_and_convert(pd.Series([2, -1], dtype=object), "bool")
    assert result.status == ConversionStatus.UNSAFE
    assert all(d.reason_code == ReasonCode.AMBIGUOUS_BOOLEAN for d in result.diagnostics)


def test_bool_float_zero_one_rejection():
    result = analyze_and_convert(pd.Series([1.0, 0.0], dtype=object), "bool")
    assert result.status == ConversionStatus.UNSAFE
    assert all(d.reason_code == ReasonCode.AMBIGUOUS_BOOLEAN for d in result.diagnostics)


def test_bool_yes_no_on_off_rejection():
    result = analyze_and_convert(pd.Series(["yes", "no", "on", "off"], dtype=object), "bool")
    assert result.status == ConversionStatus.UNSAFE
    assert all(d.reason_code == ReasonCode.AMBIGUOUS_BOOLEAN for d in result.diagnostics)


def test_bool_arbitrary_string_rejection():
    result = analyze_and_convert(pd.Series(["anything", ""], dtype=object), "bool")
    assert result.status == ConversionStatus.UNSAFE
    assert all(d.reason_code == ReasonCode.AMBIGUOUS_BOOLEAN for d in result.diagnostics)


def test_bool_null_incompatibility():
    result = analyze_and_convert(pd.Series([True, None], dtype=object), "bool")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.NULL_TARGET_INCOMPATIBLE
    assert result.converted_series is None


def test_bool_never_uses_python_truthiness():
    # The exact bug confirmed in Phase 2.5: "false" and "0" must NOT
    # become True via non-empty-string truthiness.
    result = analyze_and_convert(pd.Series(["false", "0", ""], dtype=object), "bool")
    if result.status == ConversionStatus.SAFE:
        assert list(result.converted_series) == [False, False, False]
    else:
        # "" is rejected (AMBIGUOUS_BOOLEAN), so the overall column is
        # UNSAFE -- but confirm "false" and "0" were NOT the failures.
        failing_reasons = {d.row_index: d.reason_code for d in result.diagnostics}
        assert 0 not in failing_reasons and 1 not in failing_reasons


# ---------------------------------------------------------------------------
# object
# ---------------------------------------------------------------------------

def test_object_dtype_becomes_object():
    result = analyze_and_convert(pd.Series([1, 2, 3]), "object")
    assert result.status == ConversionStatus.SAFE
    assert str(result.converted_series.dtype) == "object"


def test_object_values_not_stringified():
    result = analyze_and_convert(pd.Series([1, 2.5, "text"], dtype=object), "object")
    assert result.status == ConversionStatus.SAFE
    assert result.converted_series.iloc[0] == 1 and isinstance(result.converted_series.iloc[0], int)
    assert result.converted_series.iloc[1] == 2.5 and isinstance(result.converted_series.iloc[1], float)
    assert result.converted_series.iloc[2] == "text"


def test_object_mutable_reference_preservation():
    shared_list = [1, 2, 3]
    result = analyze_and_convert(pd.Series([shared_list, "other"], dtype=object), "object")
    assert result.status == ConversionStatus.SAFE
    assert result.converted_series.iloc[0] is shared_list


def test_object_null_position_preservation():
    result = analyze_and_convert(pd.Series([1, None, 3], dtype=object), "object")
    assert result.status == ConversionStatus.SAFE
    assert pd.isna(result.converted_series.iloc[1])


def test_object_input_series_unchanged():
    original = pd.Series([1, 2, 3], name="unchanged_test")
    before_dtype = str(original.dtype)
    analyze_and_convert(original, "object")
    assert str(original.dtype) == before_dtype
    assert list(original) == [1, 2, 3]


# ---------------------------------------------------------------------------
# Global invariants
# ---------------------------------------------------------------------------

def test_row_count_preservation():
    series = pd.Series([1, 2, 3, 4, 5])
    result = analyze_and_convert(series, "float64")
    assert result.total_count == 5
    assert len(result.converted_series) == 5


def test_order_preservation():
    series = pd.Series([3, 1, 2])
    result = analyze_and_convert(series, "float64")
    assert list(result.converted_series) == [3.0, 1.0, 2.0]


def test_index_preservation_including_duplicates_and_non_numeric():
    series = pd.Series([1, 2, 3], index=["b", "a", "a"])
    result = analyze_and_convert(series, "float64")
    assert list(result.converted_series.index) == ["b", "a", "a"]


def test_series_name_preservation():
    series = pd.Series([1, 2], name="my_column")
    result = analyze_and_convert(series, "float64")
    assert result.converted_series.name == "my_column"


def test_atomic_rejection_leaves_no_converted_series():
    series = pd.Series([1, 2, "not a number", 4])
    result = analyze_and_convert(series, "int64")
    assert result.status != ConversionStatus.SAFE
    assert result.converted_series is None


def test_atomic_rejection_input_unchanged():
    series = pd.Series([1, "bad", 3], dtype=object)
    before = list(series)
    analyze_and_convert(series, "int64")
    assert list(series) == before


def test_deterministic_diagnostic_order():
    series = pd.Series(["bad1", "bad2", "bad3"], dtype=object)
    result = analyze_and_convert(series, "int64")
    assert [d.row_index for d in result.diagnostics] == [0, 1, 2]


def test_deterministic_repeated_results():
    series = pd.Series(["1", "bad", "3"], dtype=object)
    r1 = analyze_and_convert(series, "int64")
    r2 = analyze_and_convert(series, "int64")
    assert r1.status == r2.status
    assert r1.diagnostics == r2.diagnostics
    assert r1.converted_count == r2.converted_count
    assert r1.failed_count == r2.failed_count


def test_non_safe_result_has_no_converted_series():
    for target, series in [
        ("int64", pd.Series(["bad"], dtype=object)),
        ("bool", pd.Series(["bad"], dtype=object)),
        ("decimal", pd.Series([1])),
    ]:
        result = analyze_and_convert(series, target)
        assert result.status != ConversionStatus.SAFE
        assert result.converted_series is None, target


def test_raw_values_absent_from_diagnostic_messages():
    secret = "UNIQUE_SENTINEL_VALUE_998877"
    series = pd.Series([secret], dtype=object)
    result = analyze_and_convert(series, "int64")
    for d in result.diagnostics:
        assert secret not in d.message


def test_internal_error_containment_via_test_seam():
    """
    Controlled test seam: patch the internal per-value analyzer to
    raise, and confirm analyze_and_convert() contains it as
    ERROR / INTERNAL_CONVERSION_ERROR rather than propagating or
    returning a partial result. Implemented as manual patch/restore
    (not the pytest `monkeypatch` fixture) so this test is runnable
    both under real pytest and under a plain function-calling runner.
    """
    def _boom(value, policy):
        raise RuntimeError("simulated internal failure")

    original_analyzer = converter_module._VALUE_ANALYZERS["int64"]
    converter_module._VALUE_ANALYZERS["int64"] = _boom
    try:
        result = analyze_and_convert(pd.Series([1, 2, 3]), "int64")
    finally:
        converter_module._VALUE_ANALYZERS["int64"] = original_analyzer

    assert result.status == ConversionStatus.ERROR
    assert result.diagnostics[0].reason_code == ReasonCode.INTERNAL_CONVERSION_ERROR
    assert result.converted_series is None


def test_no_third_party_dependency_imports():
    """
    Confirms the module-boundary requirement directly (spec Section
    14): src/type_repair must not import FastAPI, SQLAlchemy, or any
    database connector. Checked by inspecting the actual source files
    for forbidden import statements, not just what happens to already
    be loaded in sys.modules (which could pass or fail depending on
    what other tests ran first).
    """
    import os
    import type_repair as package

    package_dir = os.path.dirname(package.__file__)
    forbidden_terms = ("fastapi", "sqlalchemy", "psycopg", "asyncpg")
    for filename in os.listdir(package_dir):
        if not filename.endswith(".py"):
            continue
        filepath = os.path.join(package_dir, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            source = f.read()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                lowered = stripped.lower()
                for forbidden in forbidden_terms:
                    assert forbidden not in lowered, f"{filename} imports {forbidden}: {stripped!r}"


# ---------------------------------------------------------------------------
# External review corrections
# ---------------------------------------------------------------------------

def test_trailing_newline_rejected_for_int64():
    for value in ["1\n", "1\r", "1\r\n"]:
        result = analyze_and_convert(pd.Series([value], dtype=object), "int64")
        assert result.status == ConversionStatus.UNSAFE, repr(value)
        assert result.diagnostics[0].reason_code == ReasonCode.PARSE_ERROR, repr(value)


def test_trailing_newline_rejected_for_float64():
    for value in ["1.0\n", "1.0\r", "1.0\r\n"]:
        result = analyze_and_convert(pd.Series([value], dtype=object), "float64")
        assert result.status == ConversionStatus.UNSAFE, repr(value)
        assert result.diagnostics[0].reason_code == ReasonCode.PARSE_ERROR, repr(value)


def test_all_ascii_whitespace_variants_rejected_for_int64_grammar():
    # Confirmed directly: Python's re $ matches before a trailing \n
    # even without MULTILINE -- this specifically exercises every
    # ASCII whitespace character at both ends, not just \n.
    for ws in [" ", "\t", "\r", "\n", "\f", "\v"]:
        for value in [f"{ws}1", f"1{ws}", f"{ws}1{ws}"]:
            result = analyze_and_convert(pd.Series([value], dtype=object), "int64")
            assert result.status == ConversionStatus.UNSAFE, repr(value)
            assert result.diagnostics[0].reason_code == ReasonCode.PARSE_ERROR, repr(value)


def test_all_ascii_whitespace_variants_rejected_for_float64_grammar():
    for ws in [" ", "\t", "\r", "\n", "\f", "\v"]:
        for value in [f"{ws}1.0", f"1.0{ws}"]:
            result = analyze_and_convert(pd.Series([value], dtype=object), "float64")
            assert result.status == ConversionStatus.UNSAFE, repr(value)
            assert result.diagnostics[0].reason_code == ReasonCode.PARSE_ERROR, repr(value)


def test_fullmatch_used_not_bare_match():
    """Directly exercises the regex distinction the bug hinged on."""
    from type_repair.policy import DEFAULT_POLICY
    assert DEFAULT_POLICY.match_integer_string("1\n") is False
    assert DEFAULT_POLICY.match_integer_string("1") is True
    assert DEFAULT_POLICY.match_decimal_string("1.0\n") is False
    assert DEFAULT_POLICY.match_decimal_string("1.0") is True


def test_extended_precision_numpy_float_rejected_for_int64():
    """
    Where numpy.longdouble has more precision than float64, this value
    is NOT mathematically an integer, but naively
    reducing to Python float before checking would make it look
    exactly 1.0. Must be rejected as FRACTIONAL_VALUE, not silently
    accepted as 1. On platforms where longdouble is only float64
    precision (including standard Windows builds), the extra fraction
    cannot exist and the platform-specific assertion is a no-op.
    """
    if not (
        np.finfo(np.longdouble).nmant > np.finfo(np.float64).nmant
    ):
        return

    value = np.longdouble(1) + np.longdouble(2) ** np.longdouble(-63)
    result = analyze_and_convert(pd.Series([value], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.FRACTIONAL_VALUE
    assert result.converted_series is None


def test_extended_precision_numpy_float_rejected_for_float64():
    """Where longdouble is wider than float64, the same value cannot
    be exactly represented in float64 and must be rejected as
    PRECISION_LOSS. On standard Windows builds longdouble is float64,
    so the platform-specific assertion is a no-op."""
    if not (
        np.finfo(np.longdouble).nmant > np.finfo(np.float64).nmant
    ):
        return

    value = np.longdouble(1) + np.longdouble(2) ** np.longdouble(-63)
    result = analyze_and_convert(pd.Series([value], dtype=object), "float64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.PRECISION_LOSS
    assert result.converted_series is None


def test_extended_precision_numpy_float_that_is_genuinely_exact_still_succeeds():
    """A genuinely exact extended-precision value (a plain whole
    number with no hidden fractional component) must still convert
    successfully -- the fix must not become overly conservative and
    reject values that really are exact."""
    value = np.longdouble(42)
    int_result = analyze_and_convert(pd.Series([value], dtype=object), "int64")
    assert int_result.status == ConversionStatus.SAFE
    assert list(int_result.converted_series) == [42]

    float_result = analyze_and_convert(pd.Series([value], dtype=object), "float64")
    assert float_result.status == ConversionStatus.SAFE
    assert list(float_result.converted_series) == [42.0]


def test_ordinary_float64_and_python_float_unaffected_by_native_precision_checks():
    """Regression guard: the native-precision fix must not change
    behavior for ordinary numpy.float64 or plain Python float values,
    which are already their own exact float64 representation."""
    result = analyze_and_convert(pd.Series([3.14, np.float64(2.5)], dtype=object), "float64")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [3.14, 2.5]


def test_conversion_policy_bool_tokens_not_externally_mutable():
    """
    Confirmed directly: a caller-supplied dict was retained by
    reference, so mutating it after construction silently changed the
    policy's behavior. Must be defensively copied in __post_init__.
    """
    tokens = {"true": True, "false": False, "1": True, "0": False}
    policy = ConversionPolicy(bool_tokens=tokens)
    tokens["yes"] = True  # mutate the ORIGINAL dict after construction

    result = analyze_and_convert(pd.Series(["yes"], dtype=object), "bool", policy=policy)
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.AMBIGUOUS_BOOLEAN

    # The policy's own bool_tokens must also not reflect the mutation
    assert "yes" not in policy.bool_tokens


def test_conversion_policy_supported_targets_not_externally_mutable():
    targets = {"int64", "float64", "bool", "object"}  # the complete, valid set
    policy = ConversionPolicy(supported_targets=targets)
    targets.add("decimal")  # mutate the ORIGINAL set after construction

    assert "decimal" not in policy.supported_targets
    result = analyze_and_convert(pd.Series([1]), "decimal", policy=policy)
    assert result.status == ConversionStatus.UNSUPPORTED


def test_conversion_policy_returned_mapping_cannot_be_mutated_directly():
    policy = ConversionPolicy()
    try:
        policy.bool_tokens["yes"] = True
        assert False, "expected the returned mapping to be read-only"
    except TypeError:
        pass


def test_count_semantics_for_null_incompatible_target():
    """
    Documents the real, intentional overlap: for int64 (null-
    incompatible), a null row counts in BOTH null_count and
    failed_count. total_count = null_count + converted_count +
    failed_count does NOT hold here -- confirmed directly, and this
    is by design, not a bug.
    """
    result = analyze_and_convert(pd.Series([1, None]), "int64")
    assert result.total_count == 2
    assert result.null_count == 1
    assert result.converted_count == 1
    assert result.failed_count == 1
    assert result.null_count + result.converted_count + result.failed_count == 3
    assert result.total_count != result.null_count + result.converted_count + result.failed_count


def test_count_semantics_for_null_preserving_target():
    """For float64 (null-preserving), the equation DOES hold: a null
    row counts only in null_count, not also in failed_count."""
    result = analyze_and_convert(pd.Series([1.0, None, 3.0]), "float64")
    assert result.total_count == 3
    assert result.null_count == 1
    assert result.converted_count == 2
    assert result.failed_count == 0
    assert result.total_count == result.null_count + result.converted_count + result.failed_count


# ---------------------------------------------------------------------------
# Final correction pass: int64 over-rejection fix, policy validation,
# portability, and additional mandatory tests
# ---------------------------------------------------------------------------

_LONGDOUBLE_IS_WIDER_THAN_FLOAT64 = (
    np.finfo(np.longdouble).nmant > np.finfo(np.float64).nmant
)


def test_exact_large_longdouble_integer_succeeds_for_int64():
    """
    Confirmed directly: int64 conversion does not require float64
    representability at all. An earlier version of this fix wrongly
    required a float64 round-trip before accepting an integral float,
    which rejected exact, in-range integers like 2**53+1 represented
    as numpy.longdouble -- values that never needed to pass through
    float64 to produce the correct int64 result. Guarded by the
    portability check: this only exercises anything meaningful where
    longdouble is genuinely wider than float64 on this platform.
    """
    if not _LONGDOUBLE_IS_WIDER_THAN_FLOAT64:
        return
    for python_int in [2**53 + 1, 9223372036854775807]:
        value = np.longdouble(python_int)
        result = analyze_and_convert(pd.Series([value], dtype=object), "int64")
        assert result.status == ConversionStatus.SAFE, python_int
        assert result.converted_series.iloc[0] == python_int, python_int


def test_non_finite_longdouble_rejected():
    """Non-finite values must be caught at native precision
    regardless of platform-specific longdouble width."""
    result = analyze_and_convert(pd.Series([np.longdouble("inf")], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSAFE
    assert result.diagnostics[0].reason_code == ReasonCode.NON_FINITE_NUMBER

    result2 = analyze_and_convert(pd.Series([np.longdouble("inf")], dtype=object), "float64")
    assert result2.status == ConversionStatus.UNSAFE
    assert result2.diagnostics[0].reason_code == ReasonCode.NON_FINITE_NUMBER


def test_numpy_float16_safe_conversion():
    result = analyze_and_convert(pd.Series([np.float16(3.5)], dtype=object), "float64")
    assert result.status == ConversionStatus.SAFE
    assert result.converted_series.iloc[0] == 3.5

    result_int = analyze_and_convert(pd.Series([np.float16(5.0)], dtype=object), "int64")
    assert result_int.status == ConversionStatus.SAFE
    assert result_int.converted_series.iloc[0] == 5


def test_numpy_float32_safe_conversion():
    result = analyze_and_convert(pd.Series([np.float32(2.5)], dtype=object), "float64")
    assert result.status == ConversionStatus.SAFE
    assert result.converted_series.iloc[0] == 2.5

    result_int = analyze_and_convert(pd.Series([np.float32(7.0)], dtype=object), "int64")
    assert result_int.status == ConversionStatus.SAFE
    assert result_int.converted_series.iloc[0] == 7


def test_invalid_boolean_token_value_rejected_at_construction():
    """Confirmed directly: constructing a policy with a non-bool token
    VALUE (e.g. 1 instead of True) previously succeeded silently and
    let the engine return SAFE with a non-bool-derived result."""
    try:
        ConversionPolicy(bool_tokens={"true": 1, "false": False, "1": True, "0": False})
        assert False, "expected ValueError"
    except ValueError as e:
        assert "bool" in str(e).lower()


def test_expanded_or_inverted_boolean_tokens_rejected_at_construction():
    """Confirmed directly: a caller could previously expand the token
    table (adding "yes") or invert it (mapping "true" -> False),
    silently violating the locked boolean mapping."""
    try:
        ConversionPolicy(bool_tokens={"yes": True, "true": False, "false": True, "1": True, "0": False})
        assert False, "expected ValueError for expanded/inverted tokens"
    except ValueError:
        pass

    try:
        ConversionPolicy(bool_tokens={"true": True, "false": False, "1": True})  # missing "0"
        assert False, "expected ValueError for incomplete token table"
    except ValueError:
        pass


def test_unsupported_target_added_to_policy_rejected_at_construction():
    """
    Confirmed directly: a custom policy could previously add e.g.
    "decimal" to supported_targets, and the converter would then
    reach ERROR / INTERNAL_CONVERSION_ERROR (no registered analyzer)
    instead of correctly reporting UNSUPPORTED. Now rejected at
    construction time instead.
    """
    try:
        ConversionPolicy(supported_targets=frozenset({"int64", "float64", "bool", "object", "decimal"}))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "unsupported" in str(e).lower() or "must be exactly" in str(e).lower()

    try:
        ConversionPolicy(supported_targets=frozenset({"int64", "float64"}))  # incomplete
        assert False, "expected ValueError for incomplete supported_targets"
    except ValueError:
        pass


def test_default_policy_stable_across_repeated_construction():
    """A freshly-constructed ConversionPolicy() with no overrides must
    always produce the exact same bool_tokens/supported_targets as
    DEFAULT_POLICY -- policy construction itself must be deterministic."""
    fresh = ConversionPolicy()
    assert dict(fresh.bool_tokens) == dict(DEFAULT_POLICY.bool_tokens)
    assert fresh.supported_targets == DEFAULT_POLICY.supported_targets
    assert fresh.version == DEFAULT_POLICY.version

    fresh2 = ConversionPolicy()
    assert dict(fresh.bool_tokens) == dict(fresh2.bool_tokens)
    assert fresh.supported_targets == fresh2.supported_targets


def test_count_scenario_all_converted_no_failures():
    """Scenario 1 of 4: every row converts successfully, no nulls, no
    failures -- converted_count == total_count, failed_count == 0,
    null_count == 0."""
    result = analyze_and_convert(pd.Series([1, 2, 3]), "float64")
    assert result.total_count == 3
    assert result.null_count == 0
    assert result.converted_count == 3
    assert result.failed_count == 0


def test_count_scenario_mixed_converted_and_failed_no_nulls():
    """Scenario 2 of 4: some rows convert, some fail their own check,
    no nulls involved at all."""
    result = analyze_and_convert(pd.Series([1, "bad", 3, "also bad"], dtype=object), "int64")
    assert result.status != ConversionStatus.SAFE
    assert result.total_count == 4
    assert result.null_count == 0
    assert result.converted_count == 2
    assert result.failed_count == 2


def test_count_scenario_null_incompatible_target():
    """Scenario 3 of 4 (already covered above, restated here for the
    explicit 'all four scenarios' requirement): int64/bool count a
    null in BOTH null_count and failed_count."""
    result = analyze_and_convert(pd.Series([1, None]), "int64")
    assert result.total_count == 2
    assert result.null_count == 1
    assert result.converted_count == 1
    assert result.failed_count == 1


def test_count_scenario_null_preserving_target():
    """Scenario 4 of 4: float64/object count a null only in
    null_count, not also in failed_count."""
    result = analyze_and_convert(pd.Series([1.0, None, 3.0]), "float64")
    assert result.total_count == 3
    assert result.null_count == 1
    assert result.converted_count == 2
    assert result.failed_count == 0


# ---------------------------------------------------------------------------
# Closure corrections: policy-lock bypass (critical) and immutable-model gaps
# ---------------------------------------------------------------------------

def test_version_string_cannot_bypass_policy_validation():
    """
    CRITICAL, confirmed directly: gating all policy validation behind
    `if self.version == POLICY_VERSION` let a caller bypass every
    safety rule just by passing a different version string. There is
    exactly one policy version this engine implements; validation is
    now unconditional and the version itself is locked.
    """
    try:
        ConversionPolicy(version="other", bool_tokens={"yes": True}, supported_targets={"bool"})
        assert False, "expected ValueError for unsupported version"
    except ValueError as e:
        assert "version" in str(e).lower()


def test_version_bypass_cannot_reach_unsupported_target_via_nonexistent_analyzer():
    try:
        ConversionPolicy(version="other", supported_targets={"decimal"})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_version_bypass_cannot_replace_integer_grammar():
    import re
    try:
        ConversionPolicy(version="other", integer_string_pattern=re.compile(r"^\d+$"))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_version_bypass_cannot_replace_decimal_grammar():
    import re
    try:
        ConversionPolicy(version="other", decimal_string_pattern=re.compile(r"^.*$"))
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_int64_range_cannot_be_widened_even_by_one():
    """
    CRITICAL, confirmed directly: widening int64_max by exactly one
    let INT64_MAX + 1 (9223372036854775808) report SAFE with the
    silently wrapped output -9223372036854775808 -- exactly the class
    of corruption this engine exists to prevent. int64_min/int64_max
    must be locked, not merely defaulted.
    """
    try:
        ConversionPolicy(int64_max=9223372036854775808)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "int64" in str(e).lower()

    try:
        ConversionPolicy(int64_min=-9223372036854775809)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_int64_wraparound_scenario_no_longer_constructible_end_to_end():
    """End-to-end confirmation the exact reported scenario is now
    impossible to construct at all, not just that the policy field
    is rejected in isolation."""
    try:
        policy = ConversionPolicy(int64_max=9223372036854775808)
        result = analyze_and_convert(
            pd.Series([9223372036854775808], dtype=object), "int64", policy=policy
        )
        assert False, "the tampered policy must never have been constructible"
    except ValueError:
        pass


def test_integer_string_pattern_locked_with_correct_version():
    """Confirms the grammar lock is enforced independent of the
    version check -- not merely a side effect of the version being
    wrong in other tests."""
    import re
    try:
        ConversionPolicy(integer_string_pattern=re.compile(r"^\d+$"))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "integer_string_pattern" in str(e)


def test_decimal_string_pattern_locked_with_correct_version():
    import re
    try:
        ConversionPolicy(decimal_string_pattern=re.compile(r"^.*$"))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "decimal_string_pattern" in str(e)


def test_diagnostics_defensively_copied_into_tuple():
    """
    Confirmed directly: passing a list for diagnostics let a caller
    mutate the original list after construction and change the
    supposedly-frozen result. Must be copied into a genuine tuple.
    """
    diag_list = [ConversionDiagnostic(
        row_index=0, source_value_type="str", target_dtype="int64",
        reason_code=ReasonCode.PARSE_ERROR, message="test",
    )]
    result = ColumnConversionResult(
        status=ConversionStatus.UNSAFE, source_dtype="object", target_dtype="int64",
        total_count=1, null_count=0, converted_count=0, failed_count=1,
        diagnostics=diag_list, converted_series=None, policy_version="phase3.1-policy-v1",
    )
    assert isinstance(result.diagnostics, tuple)
    diag_list.append("INJECTED")
    assert "INJECTED" not in result.diagnostics
    assert len(result.diagnostics) == 1


def test_safe_result_requires_converted_series():
    try:
        ColumnConversionResult(
            status=ConversionStatus.SAFE, source_dtype="object", target_dtype="int64",
            total_count=1, null_count=0, converted_count=1, failed_count=0,
            diagnostics=(), converted_series=None, policy_version="phase3.1-policy-v1",
        )
        assert False, "expected ValueError -- SAFE requires a converted_series"
    except ValueError:
        pass


def test_non_safe_result_forbids_converted_series():
    for status in [ConversionStatus.UNSAFE, ConversionStatus.UNSUPPORTED, ConversionStatus.ERROR]:
        try:
            ColumnConversionResult(
                status=status, source_dtype="object", target_dtype="int64",
                total_count=1, null_count=0, converted_count=0, failed_count=1,
                diagnostics=(), converted_series=pd.Series([1]), policy_version="phase3.1-policy-v1",
            )
            assert False, f"expected ValueError -- {status} must forbid a converted_series"
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Final-gate corrections: regex type/flags locking, defensive dispatch
# ---------------------------------------------------------------------------

def test_regex_flags_must_match_exactly():
    """
    Confirmed directly: matching pattern TEXT alone let a caller add
    re.MULTILINE (or any other flag) without detection -- same
    pattern string, different actual matching behavior.
    """
    import re
    from type_repair.policy import INTEGER_STRING_PATTERN, DECIMAL_STRING_PATTERN

    try:
        ConversionPolicy(integer_string_pattern=re.compile(INTEGER_STRING_PATTERN.pattern, re.MULTILINE))
        assert False, "expected ValueError for flag mismatch"
    except ValueError as e:
        assert "flag" in str(e).lower()

    try:
        ConversionPolicy(decimal_string_pattern=re.compile(DECIMAL_STRING_PATTERN.pattern, re.IGNORECASE))
        assert False, "expected ValueError for flag mismatch"
    except ValueError as e:
        assert "flag" in str(e).lower()


def test_regex_must_be_genuine_pattern_instance():
    """
    CRITICAL, confirmed directly: a duck-typed object exposing
    matching .pattern/.flags attributes but a fake fullmatch() that
    always returns True completely bypassed the grammar -- "01"
    reported SAFE for int64. The matcher type itself must be checked,
    not just its declared attributes.
    """
    from type_repair.policy import INTEGER_STRING_PATTERN, DECIMAL_STRING_PATTERN

    class UnsafePattern:
        pattern = INTEGER_STRING_PATTERN.pattern
        flags = INTEGER_STRING_PATTERN.flags
        def fullmatch(self, value):
            return True

    try:
        ConversionPolicy(integer_string_pattern=UnsafePattern())
        assert False, "expected ValueError for non-Pattern matcher"
    except ValueError as e:
        assert "re.Pattern" in str(e)

    class UnsafeDecimalPattern:
        pattern = DECIMAL_STRING_PATTERN.pattern
        flags = DECIMAL_STRING_PATTERN.flags
        def fullmatch(self, value):
            return True

    try:
        ConversionPolicy(decimal_string_pattern=UnsafeDecimalPattern())
        assert False, "expected ValueError for non-Pattern matcher"
    except ValueError as e:
        assert "re.Pattern" in str(e)


def test_grammar_bypass_scenario_no_longer_constructible_end_to_end():
    """End-to-end confirmation the exact reported "01" bypass scenario
    is now impossible to construct at all."""
    from type_repair.policy import INTEGER_STRING_PATTERN

    class UnsafePattern:
        pattern = INTEGER_STRING_PATTERN.pattern
        flags = INTEGER_STRING_PATTERN.flags
        def fullmatch(self, value):
            return True

    try:
        policy = ConversionPolicy(integer_string_pattern=UnsafePattern())
        analyze_and_convert(pd.Series(["01"], dtype=object), "int64", policy=policy)
        assert False, "the unsafe policy must never have been constructible"
    except ValueError:
        pass


def test_converter_does_not_trust_policy_supported_target_alone():
    """
    Confirmed directly: a policy-like object claiming an unregistered
    target (e.g. "decimal") was supported reached
    ERROR/INTERNAL_CONVERSION_ERROR via a raw KeyError on the analyzer
    registry, instead of the correct UNSUPPORTED/UNSUPPORTED_TARGET_DTYPE.
    The converter must independently verify against its own known
    targets before ever dispatching or indexing the registry.
    """
    class FakeMalformedPolicy:
        version = DEFAULT_POLICY.version
        def normalize_target_dtype(self, raw):
            return raw.strip().lower()
        def is_supported_target(self, normalized):
            return normalized == "decimal"

    result = analyze_and_convert(pd.Series([1]), "decimal", policy=FakeMalformedPolicy())
    assert result.status == ConversionStatus.UNSUPPORTED
    assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_TARGET_DTYPE
    assert result.converted_series is None


def test_converter_defensive_check_covers_all_four_real_targets_normally():
    """Regression guard: the new defensive check must not break normal
    operation for any of the four genuinely supported targets."""
    for target, series, expect_safe in [
        ("int64", pd.Series([1, 2, 3]), True),
        ("float64", pd.Series([1.0, 2.0]), True),
        ("bool", pd.Series([True, False]), True),
        ("object", pd.Series([1, "a"], dtype=object), True),
    ]:
        result = analyze_and_convert(series, target)
        assert result.status == ConversionStatus.SAFE, target


# ---------------------------------------------------------------------------
# Owner-verification corrections: int64 digit-limit determinism,
# error-containment gap
# ---------------------------------------------------------------------------

def test_long_integer_strings_beyond_python_digit_limit_are_out_of_range_not_error():
    """
    Confirmed directly: Python 3.11+ limits unrestricted int(str)
    conversion via sys.get_int_max_str_digits() (4300 by default). A
    canonical integer string beyond that length was raising ValueError
    internally, surfacing as ERROR/INTERNAL_CONVERSION_ERROR instead
    of the correct UNSAFE/OUT_OF_RANGE -- an out-of-range value is not
    an internal engine failure.
    """
    for digits in [4299, 4300, 4301, 5000]:
        value = "9" * digits
        result = analyze_and_convert(pd.Series([value], dtype=object), "int64")
        assert result.status == ConversionStatus.UNSAFE, digits
        assert result.diagnostics[0].reason_code == ReasonCode.OUT_OF_RANGE, digits


def test_int64_range_determination_independent_of_process_wide_digit_limit():
    """
    Confirmed directly: the result changed depending on
    sys.get_int_max_str_digits(), which is process-wide, mutable
    state -- violating the determinism requirement (spec 4.6). Setting
    the limit to its minimum allowed value (640) must not change the
    outcome for any digit count.
    """
    import sys as sys_module
    original_limit = sys_module.get_int_max_str_digits()
    try:
        sys_module.set_int_max_str_digits(640)
        for digits in [640, 641]:
            value = "9" * digits
            result = analyze_and_convert(pd.Series([value], dtype=object), "int64")
            assert result.status == ConversionStatus.UNSAFE, digits
            assert result.diagnostics[0].reason_code == ReasonCode.OUT_OF_RANGE, digits
    finally:
        sys_module.set_int_max_str_digits(original_limit)


def test_int64_boundary_strings_still_succeed_after_digit_limit_fix():
    """Regression guard: the digit-length pre-check must not change
    behavior for the genuine boundary values themselves."""
    result_max = analyze_and_convert(pd.Series(["9223372036854775807"], dtype=object), "int64")
    assert result_max.status == ConversionStatus.SAFE
    assert result_max.converted_series.iloc[0] == 9223372036854775807

    result_min = analyze_and_convert(pd.Series(["-9223372036854775808"], dtype=object), "int64")
    assert result_min.status == ConversionStatus.SAFE
    assert result_min.converted_series.iloc[0] == -9223372036854775808

    result_over = analyze_and_convert(pd.Series(["9223372036854775808"], dtype=object), "int64")
    assert result_over.status == ConversionStatus.UNSAFE
    assert result_over.diagnostics[0].reason_code == ReasonCode.OUT_OF_RANGE

    result_under = analyze_and_convert(pd.Series(["-9223372036854775809"], dtype=object), "int64")
    assert result_under.status == ConversionStatus.UNSAFE
    assert result_under.diagnostics[0].reason_code == ReasonCode.OUT_OF_RANGE


def test_analyze_and_convert_never_raises_even_with_malformed_policy_missing_version():
    """
    CRITICAL for the "never raises to the caller" guarantee. Confirmed
    directly: a policy object lacking .version caused
    analyze_and_convert() itself to raise AttributeError, completely
    defeating the documented error-containment contract.
    """
    class MalformedPolicyNoVersion:
        def normalize_target_dtype(self, raw):
            raise RuntimeError("simulated failure to reach the except block")

    result = analyze_and_convert(pd.Series([1]), "int64", policy=MalformedPolicyNoVersion())
    assert result.status == ConversionStatus.ERROR
    assert result.policy_version == "unknown"
    assert result.diagnostics[0].reason_code == ReasonCode.INTERNAL_CONVERSION_ERROR


# ---------------------------------------------------------------------------
# Local-verification corrections: timedelta64 misclassification,
# comparison-object policy bypass
# ---------------------------------------------------------------------------

def test_timedelta64_rejected_as_unsupported_not_misclassified_as_integer():
    """
    Confirmed directly: numpy.timedelta64 surprisingly inherits from
    numpy.integer, so it was silently misclassified as an approved
    integer and then failed unpredictably deeper in the pipeline,
    surfacing as ERROR/INTERNAL_CONVERSION_ERROR. A time duration is
    not an approved numeric integer and must be rejected as
    UNSUPPORTED/UNSUPPORTED_SOURCE_VALUE.
    """
    value = np.timedelta64(1, "D")
    for target in ["int64", "float64", "bool"]:
        result = analyze_and_convert(pd.Series([value], dtype=object), target)
        assert result.status == ConversionStatus.UNSUPPORTED, target
        assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE, target
        assert result.converted_series is None, target


def test_genuine_numpy_integers_still_work_after_timedelta64_exclusion():
    """Regression guard: excluding timedelta64 must not affect genuine
    numpy integer types."""
    result = analyze_and_convert(pd.Series([np.int64(5), np.int32(10)], dtype=object), "int64")
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [5, 10]


def test_policy_fields_must_be_exact_builtin_types_not_comparison_objects():
    """
    CRITICAL, confirmed directly: a custom object with its own
    __eq__/__ne__/__ge__ (always returning attacker-chosen results)
    defeated comparison-based validation entirely --
    ConversionPolicy(int64_max=EvilMax()) constructed successfully,
    and INT64_MAX + 1 then reported SAFE with the value silently
    wrapped to INT64_MIN. version/int64_min/int64_max/bool_tokens
    keys+values/supported_targets entries must all be exact built-in
    types, checked via type() is, not isinstance() or comparison.
    """
    class EvilComparator:
        def __ne__(self, other): return False
        def __eq__(self, other): return True
        def __ge__(self, other): return True
        def __le__(self, other): return True

    try:
        ConversionPolicy(int64_max=EvilComparator())
        assert False, "expected ValueError"
    except ValueError as e:
        assert "exact built-in int" in str(e)

    try:
        ConversionPolicy(int64_min=EvilComparator())
        assert False, "expected ValueError"
    except ValueError as e:
        assert "exact built-in int" in str(e)

    try:
        ConversionPolicy(version=EvilComparator())
        assert False, "expected ValueError"
    except ValueError as e:
        assert "exact built-in str" in str(e)


def test_int64_wraparound_via_comparison_object_no_longer_reachable_end_to_end():
    """End-to-end confirmation the exact reported EvilMax scenario is
    now impossible to construct at all."""
    class EvilMax:
        def __ne__(self, other): return False
        def __eq__(self, other): return True
        def __ge__(self, other): return True
        def __le__(self, other): return True

    try:
        policy = ConversionPolicy(int64_max=EvilMax())
        analyze_and_convert(pd.Series([9223372036854775808], dtype=object), "int64", policy=policy)
        assert False, "the tampered policy must never have been constructible"
    except ValueError:
        pass


def test_bool_tokens_keys_and_supported_targets_entries_must_be_exact_str():
    class WeirdKey:
        def __eq__(self, other): return other == "true"
        def __hash__(self): return hash("true")

    try:
        ConversionPolicy(bool_tokens={WeirdKey(): True, "false": False, "1": True, "0": False})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_defensive_int64_range_check_uses_hardcoded_constants():
    """Directly exercises the converter's own independent range check,
    not just that policy construction is now locked down."""
    from type_repair.converter import _is_within_int64_range
    from type_repair import DEFAULT_POLICY
    assert _is_within_int64_range(9223372036854775807, DEFAULT_POLICY) is True
    assert _is_within_int64_range(9223372036854775808, DEFAULT_POLICY) is False
    assert _is_within_int64_range(-9223372036854775808, DEFAULT_POLICY) is True
    assert _is_within_int64_range(-9223372036854775809, DEFAULT_POLICY) is False


# ---------------------------------------------------------------------------
# Canonical-ready corrections: primitive subclass rejection, regex
# pattern-text subclass bypass, final int64 materialisation guard,
# and expanded timedelta64 coverage
# ---------------------------------------------------------------------------

def test_int_subclass_overriding_dunder_int_rejected():
    """
    CRITICAL, confirmed directly: a subclass of int overriding __int__
    to return a different value than the one it actually holds
    silently corrupted the result -- EvilInt(1) reported SAFE with
    output 0 for int64, 0.0 for float64, and False for bool. Only
    exact built-in int (and exact NumPy integer types) are approved
    sources; a subclass, however it behaves, is not.
    """
    class EvilInt(int):
        def __int__(self):
            return 0

    value = EvilInt(1)
    for target in ["int64", "float64", "bool"]:
        result = analyze_and_convert(pd.Series([value], dtype=object), target)
        assert result.status == ConversionStatus.UNSUPPORTED, target
        assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE, target
        assert result.converted_series is None, target


def test_float_subclass_overriding_dunder_int_rejected():
    """Confirmed directly: EvilFloat(1.0) with __int__ returning 2
    reported SAFE with output 2 for int64."""
    class EvilFloat(float):
        def __int__(self):
            return 2

    result = analyze_and_convert(pd.Series([EvilFloat(1.0)], dtype=object), "int64")
    assert result.status == ConversionStatus.UNSUPPORTED
    assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE


def test_str_subclass_overriding_strip_and_lower_rejected():
    """Confirmed directly: a str subclass lying via strip()/lower()
    made "yes" pass as if it were "true", reporting SAFE/True."""
    class EvilString(str):
        def strip(self, *args):
            return "true"
        def lower(self):
            return "true"

    result = analyze_and_convert(pd.Series([EvilString("yes")], dtype=object), "bool")
    assert result.status == ConversionStatus.UNSUPPORTED
    assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE


def test_genuine_scalar_types_unaffected_by_exact_type_checking():
    """Regression guard: exact-type checking must not reject any
    genuinely approved scalar type."""
    result_int = analyze_and_convert(
        pd.Series([1, np.int8(2), np.int16(3), np.int32(4), np.int64(5),
                   np.uint8(6), np.uint16(7), np.uint32(8), np.uint64(9)], dtype=object),
        "int64",
    )
    assert result_int.status == ConversionStatus.SAFE
    assert list(result_int.converted_series) == [1, 2, 3, 4, 5, 6, 7, 8, 9]

    result_float = analyze_and_convert(
        pd.Series([1.0, np.float16(2.0), np.float32(3.0), np.float64(4.0)], dtype=object),
        "float64",
    )
    assert result_float.status == ConversionStatus.SAFE

    result_str = analyze_and_convert(pd.Series(["42"], dtype=object), "int64")
    assert result_str.status == ConversionStatus.SAFE


def test_regex_pattern_text_must_be_exact_str_not_lying_subclass():
    """
    CRITICAL, confirmed directly: a str subclass with __eq__/__ne__
    always returning attacker-chosen results, used as a compiled
    Pattern's .pattern, defeated the pattern-text comparison entirely
    even though the Pattern object itself was genuine -- "01" reported
    SAFE for int64, "1e3" reported SAFE for float64.
    """
    import re
    from type_repair.policy import INTEGER_STRING_PATTERN, DECIMAL_STRING_PATTERN

    class EvilPatternText(str):
        __hash__ = str.__hash__
        def __eq__(self, other):
            return True
        def __ne__(self, other):
            return False

    evil_int_text = EvilPatternText(INTEGER_STRING_PATTERN.pattern)
    try:
        ConversionPolicy(integer_string_pattern=re.compile(evil_int_text))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "exact built-in str" in str(e)

    evil_decimal_text = EvilPatternText(DECIMAL_STRING_PATTERN.pattern)
    try:
        ConversionPolicy(decimal_string_pattern=re.compile(evil_decimal_text))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "exact built-in str" in str(e)


def test_regex_bypass_scenario_no_longer_constructible_end_to_end():
    """End-to-end confirmation the exact reported "01"/"1e3" bypass
    scenarios are now impossible to construct at all."""
    import re
    from type_repair.policy import INTEGER_STRING_PATTERN

    class EvilPatternText(str):
        __hash__ = str.__hash__
        def __eq__(self, other):
            return True
        def __ne__(self, other):
            return False

    try:
        policy = ConversionPolicy(integer_string_pattern=re.compile(EvilPatternText(INTEGER_STRING_PATTERN.pattern)))
        analyze_and_convert(pd.Series(["01"], dtype=object), "int64", policy=policy)
        assert False, "the unsafe policy must never have been constructible"
    except ValueError:
        pass


def test_final_int64_materialisation_guard_via_controlled_analyzer_fault():
    """
    CRITICAL, confirmed directly: a controlled internal analyzer fault
    returning an out-of-range value (INT64_MAX + 1) reached
    .astype("int64") completely unchecked and silently wrapped to
    INT64_MIN, reporting SAFE. A final, independent guard immediately
    before materialisation must catch this and resolve to ERROR
    (an internal inconsistency, not a normal per-value rejection).
    """
    def faulty_analyzer(value, policy):
        return 9223372036854775808  # INT64_MAX + 1

    original = converter_module._VALUE_ANALYZERS["int64"]
    converter_module._VALUE_ANALYZERS["int64"] = faulty_analyzer
    try:
        result = analyze_and_convert(pd.Series([1, 2, 3]), "int64")
        assert result.status == ConversionStatus.ERROR
        assert result.converted_series is None
        assert result.diagnostics[0].reason_code == ReasonCode.INTERNAL_CONVERSION_ERROR
    finally:
        converter_module._VALUE_ANALYZERS["int64"] = original


def test_final_materialisation_guard_also_catches_wrong_type_not_just_range():
    """The guard must reject a produced value that isn't an exact int
    at all, not just one that's merely out of range."""
    def faulty_analyzer(value, policy):
        return 5.0  # wrong type: a float where an int is required

    original = converter_module._VALUE_ANALYZERS["int64"]
    converter_module._VALUE_ANALYZERS["int64"] = faulty_analyzer
    try:
        result = analyze_and_convert(pd.Series([1]), "int64")
        assert result.status == ConversionStatus.ERROR
        assert result.converted_series is None
    finally:
        converter_module._VALUE_ANALYZERS["int64"] = original


def test_materialisation_guard_does_not_disrupt_genuine_boundary_values():
    """Regression guard: the new final check must not reject the
    genuine INT64_MIN/INT64_MAX values themselves."""
    result = analyze_and_convert(
        pd.Series([9223372036854775807, -9223372036854775808], dtype=object), "int64"
    )
    assert result.status == ConversionStatus.SAFE
    assert list(result.converted_series) == [9223372036854775807, -9223372036854775808]


# ---- Expanded timedelta64 coverage, per explicit requirement ----

def test_timedelta64_nanoseconds_unit_rejected():
    value = np.timedelta64(500, "ns")
    for target in ["int64", "float64", "bool"]:
        result = analyze_and_convert(pd.Series([value], dtype=object), target)
        assert result.status == ConversionStatus.UNSUPPORTED, target
        assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE, target


def test_timedelta64_seconds_unit_rejected():
    value = np.timedelta64(30, "s")
    for target in ["int64", "float64", "bool"]:
        result = analyze_and_convert(pd.Series([value], dtype=object), target)
        assert result.status == ConversionStatus.UNSUPPORTED, target
        assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE, target


def test_timedelta64_days_unit_rejected():
    value = np.timedelta64(1, "D")
    for target in ["int64", "float64", "bool"]:
        result = analyze_and_convert(pd.Series([value], dtype=object), target)
        assert result.status == ConversionStatus.UNSUPPORTED, target
        assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE, target


def test_timedelta64_nat_treated_as_null_not_misclassified_as_unsupported_value():
    """
    numpy.timedelta64("NaT") specifically. Confirmed directly:
    pd.isna(np.timedelta64("NaT")) is True, so it is correctly caught
    by null detection before ever reaching the type classifiers --
    this matches the spec's own null policy (Section 8), which lists
    NaT alongside None/pandas.NA/NaN as a recognized null. It must
    therefore resolve the same way any other null does: incompatible
    for a null-incompatible target (int64/bool), preserved for a
    null-preserving one (float64/object) -- not classified as an
    "unsupported source value" in its own right.
    """
    value = np.timedelta64("NaT")

    result_int = analyze_and_convert(pd.Series([value], dtype=object), "int64")
    assert result_int.status == ConversionStatus.UNSAFE
    assert result_int.diagnostics[0].reason_code == ReasonCode.NULL_TARGET_INCOMPATIBLE
    assert result_int.null_count == 1

    result_float = analyze_and_convert(pd.Series([value], dtype=object), "float64")
    assert result_float.status == ConversionStatus.SAFE
    assert result_float.null_count == 1
    assert pd.isna(result_float.converted_series.iloc[0])


def test_mixed_valid_numeric_and_timedelta_rows_atomic_rejection_with_counts():
    """Mixed column: some genuinely valid numeric values alongside
    timedelta64 values. Must be atomically rejected (no partial
    conversion), with accurate counts, and classified UNSUPPORTED
    since at least one UNSUPPORTED_SOURCE_VALUE failure is present."""
    series = pd.Series([1, np.timedelta64(1, "D"), 3, np.timedelta64(2, "s")], dtype=object)
    result = analyze_and_convert(series, "int64")
    assert result.status == ConversionStatus.UNSUPPORTED
    assert result.converted_series is None
    assert result.total_count == 4
    assert result.null_count == 0
    assert result.converted_count == 2  # the two genuine integers
    assert result.failed_count == 2     # the two timedelta64 values
    timedelta_diagnostics = [d for d in result.diagnostics
                              if d.reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE]
    assert len(timedelta_diagnostics) == 2


def test_signed_and_unsigned_numpy_integer_types_all_still_approved():
    """Explicit regression coverage: every signed and unsigned NumPy
    integer width must remain approved after the exact-type-checking
    change, individually."""
    for numpy_type, value in [
        (np.int8, 5), (np.int16, 5), (np.int32, 5), (np.int64, 5),
        (np.uint8, 5), (np.uint16, 5), (np.uint32, 5), (np.uint64, 5),
    ]:
        result = analyze_and_convert(pd.Series([numpy_type(value)], dtype=object), "int64")
        assert result.status == ConversionStatus.SAFE, numpy_type
        assert result.converted_series.iloc[0] == value, numpy_type

# ---------------------------------------------------------------------------
# Canonical-ready hardening: target boundary, platform NumPy integers,
# null-probe containment, and complete scalar materialisation guards
# ---------------------------------------------------------------------------


def test_target_str_subclass_cannot_redirect_unsupported_to_int64():
    class RedirectingTarget(str):
        def strip(self, *args, **kwargs):
            return self

        def lower(self):
            return "int64"

    result = analyze_and_convert(
        pd.Series([1]), RedirectingTarget("decimal")
    )
    assert result.status == ConversionStatus.UNSUPPORTED
    assert result.target_dtype == "unknown"
    assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_TARGET_DTYPE
    assert result.converted_series is None


def test_target_str_subclass_cannot_redirect_supported_target():
    class RedirectingTarget(str):
        def strip(self, *args, **kwargs):
            return self

        def lower(self):
            return "int64"

    result = analyze_and_convert(
        pd.Series([1]), RedirectingTarget("float64")
    )
    assert result.status == ConversionStatus.UNSUPPORTED
    assert result.converted_series is None


def test_target_subclass_with_exploding_strip_is_contained():
    class ExplodingTarget(str):
        def strip(self, *args, **kwargs):
            raise RuntimeError("strip must not be called")

    result = analyze_and_convert(pd.Series([1]), ExplodingTarget("int64"))
    assert result.status == ConversionStatus.UNSUPPORTED
    assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_TARGET_DTYPE


def test_target_subclass_with_exploding_str_is_contained():
    class ExplodingTarget(str):
        def __str__(self):
            raise RuntimeError("str must not be called")

    result = analyze_and_convert(pd.Series([1]), ExplodingTarget("int64"))
    assert result.status == ConversionStatus.UNSUPPORTED
    assert result.target_dtype == "unknown"


def test_non_string_target_returns_unsupported_not_error():
    for target in (None, 1, object()):
        result = analyze_and_convert(pd.Series([1]), target)
        assert result.status == ConversionStatus.UNSUPPORTED
        assert result.target_dtype == "unknown"
        assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_TARGET_DTYPE


def test_exact_string_target_normalization_remains_supported():
    cases = {
        "  INT64  ": "int64",
        " FLOAT64 ": "float64",
        " Bool ": "bool",
        " OBJECT ": "object",
    }
    for raw_target, canonical in cases.items():
        result = analyze_and_convert(pd.Series([1]), raw_target)
        assert result.status == ConversionStatus.SAFE, raw_target
        assert result.target_dtype == canonical


def _platform_numpy_integer_scalar_types():
    return frozenset(
        scalar_type
        for scalar_type in np.sctypeDict.values()
        if isinstance(scalar_type, type)
        and issubclass(scalar_type, np.integer)
        and scalar_type not in {np.bool_, np.timedelta64}
    )


def test_every_genuine_platform_numpy_integer_scalar_type_is_supported():
    scalar_types = _platform_numpy_integer_scalar_types()
    assert scalar_types

    for scalar_type in scalar_types:
        value = scalar_type(1)
        for target, expected in (
            ("int64", 1),
            ("float64", 1.0),
            ("bool", True),
        ):
            result = analyze_and_convert(
                pd.Series([value], dtype=object), target
            )
            assert result.status == ConversionStatus.SAFE, (
                scalar_type,
                target,
                result.diagnostics,
            )
            assert result.converted_series.iloc[0] == expected


def test_longlong_and_ulonglong_are_supported_when_distinct():
    for scalar_type in {np.longlong, np.ulonglong}:
        value = scalar_type(1)
        result = analyze_and_convert(
            pd.Series([value], dtype=object), "int64"
        )
        assert result.status == ConversionStatus.SAFE
        assert int(result.converted_series.iloc[0]) == 1


def test_numpy_integer_scalar_subclasses_remain_rejected():
    candidate_bases = {np.int64, np.uint64, np.longlong, np.ulonglong}
    for base_type in candidate_bases:
        subclass = type(
            f"Untrusted{base_type.__name__}",
            (base_type,),
            {},
        )
        value = subclass(1)
        for target in ("int64", "float64", "bool"):
            result = analyze_and_convert(
                pd.Series([value], dtype=object), target
            )
            assert result.status == ConversionStatus.UNSUPPORTED
            assert (
                result.diagnostics[0].reason_code
                == ReasonCode.UNSUPPORTED_SOURCE_VALUE
            )


def _series_with_single_object(value):
    values = np.empty(1, dtype=object)
    values[0] = value
    return pd.Series(values)


def test_null_probe_exception_becomes_unsupported_for_scalar_targets():
    class ExplodingArray:
        def __array__(self, *args, **kwargs):
            raise RuntimeError("array conversion is intentionally unsupported")

    series = _series_with_single_object(ExplodingArray())
    for target in ("int64", "float64", "bool"):
        result = analyze_and_convert(series, target)
        assert result.status == ConversionStatus.UNSUPPORTED
        assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE
        assert result.converted_series is None


def test_null_probe_exception_object_widening_is_safe_and_preserves_identity():
    class ExplodingArray:
        def __array__(self, *args, **kwargs):
            raise RuntimeError("array conversion is intentionally unsupported")

    original = ExplodingArray()
    result = analyze_and_convert(
        _series_with_single_object(original), "object"
    )
    assert result.status == ConversionStatus.SAFE
    assert result.converted_series.iloc[0] is original


def test_null_probe_truth_conversion_exception_is_treated_as_non_null():
    class ExplodingTruth:
        def __bool__(self):
            raise RuntimeError("truth conversion is intentionally unsupported")

    class UnsupportedValue:
        pass

    original_isna = converter_module.pd.isna
    converter_module.pd.isna = lambda value: ExplodingTruth()
    try:
        result = analyze_and_convert(
            _series_with_single_object(UnsupportedValue()), "int64"
        )
        assert result.status == ConversionStatus.UNSUPPORTED
        assert result.diagnostics[0].reason_code == ReasonCode.UNSUPPORTED_SOURCE_VALUE
    finally:
        converter_module.pd.isna = original_isna


def test_standard_null_sentinels_remain_unchanged_after_safe_probe():
    values = [None, pd.NA, np.nan, pd.NaT, np.timedelta64("NaT")]
    result = analyze_and_convert(pd.Series(values, dtype=object), "float64")
    assert result.status == ConversionStatus.SAFE
    assert result.null_count == len(values)
    assert result.converted_count == 0
    assert result.failed_count == 0
    assert result.converted_series.isna().all()


def test_final_bool_materialisation_guard_rejects_internal_wrong_type():
    original = converter_module._VALUE_ANALYZERS["bool"]
    converter_module._VALUE_ANALYZERS["bool"] = lambda value, policy: 1
    try:
        result = analyze_and_convert(pd.Series([1]), "bool")
        assert result.status == ConversionStatus.ERROR
        assert result.converted_series is None
        assert result.diagnostics[0].reason_code == ReasonCode.INTERNAL_CONVERSION_ERROR
    finally:
        converter_module._VALUE_ANALYZERS["bool"] = original


def test_final_float64_materialisation_guard_rejects_internal_wrong_type():
    original = converter_module._VALUE_ANALYZERS["float64"]
    converter_module._VALUE_ANALYZERS["float64"] = lambda value, policy: 1
    try:
        result = analyze_and_convert(pd.Series([1]), "float64")
        assert result.status == ConversionStatus.ERROR
        assert result.converted_series is None
        assert result.diagnostics[0].reason_code == ReasonCode.INTERNAL_CONVERSION_ERROR
    finally:
        converter_module._VALUE_ANALYZERS["float64"] = original


def test_policy_public_string_methods_reject_untrusted_subclasses():
    class RedirectingString(str):
        def strip(self, *args, **kwargs):
            return "true"

        def lower(self):
            return "true"

    value = RedirectingString("yes")
    try:
        DEFAULT_POLICY.normalize_target_dtype(value)
        assert False, "expected exact-string TypeError"
    except TypeError:
        pass
    assert DEFAULT_POLICY.is_supported_target(value) is False
    assert DEFAULT_POLICY.match_integer_string(value) is False
    assert DEFAULT_POLICY.match_decimal_string(value) is False
    assert DEFAULT_POLICY.lookup_bool_token(value) is None


def test_malformed_policy_cannot_execute_known_target_conversion():
    class PermissivePolicy:
        version = DEFAULT_POLICY.version
        int64_min = -10**100
        int64_max = 10**100

        def normalize_target_dtype(self, raw):
            return "int64"

        def is_supported_target(self, normalized):
            return True

        def match_integer_string(self, value):
            return True

    result = analyze_and_convert(
        pd.Series(["01"], dtype=object), "int64", policy=PermissivePolicy()
    )
    assert result.status == ConversionStatus.ERROR
    assert result.converted_series is None
    assert result.diagnostics[0].reason_code == ReasonCode.INTERNAL_CONVERSION_ERROR


def test_conversion_policy_subclass_cannot_override_executable_behavior():
    class PermissivePolicy(ConversionPolicy):
        def match_integer_string(self, value):
            return True

    result = analyze_and_convert(
        pd.Series(["01"], dtype=object), "int64", policy=PermissivePolicy()
    )
    assert result.status == ConversionStatus.ERROR
    assert result.converted_series is None


def test_pandas_series_subclass_is_contained_not_executed():
    class UntrustedSeries(pd.Series):
        @property
        def _constructor(self):
            return UntrustedSeries

        def items(self):
            raise RuntimeError("items must not be called")

    result = analyze_and_convert(UntrustedSeries([1]), "int64")
    assert result.status == ConversionStatus.ERROR
    assert result.source_dtype == "unknown"
    assert result.converted_series is None


def test_internal_analyzer_cannot_smuggle_none_for_non_null_float_row():
    original = converter_module._VALUE_ANALYZERS["float64"]
    converter_module._VALUE_ANALYZERS["float64"] = lambda value, policy: None
    try:
        result = analyze_and_convert(pd.Series([1]), "float64")
        assert result.status == ConversionStatus.ERROR
        assert result.converted_series is None
    finally:
        converter_module._VALUE_ANALYZERS["float64"] = original


def test_object_widening_rejects_internal_same_series_fault():
    original = converter_module._widen_to_object
    source = pd.Series([1, 2], name="source")
    converter_module._widen_to_object = lambda series: series
    try:
        result = analyze_and_convert(source, "object")
        assert result.status == ConversionStatus.UNSAFE
        assert result.converted_series is None
        assert result.diagnostics[0].reason_code == ReasonCode.ROUND_TRIP_MISMATCH
    finally:
        converter_module._widen_to_object = original


def test_object_widening_rejects_internal_length_fault():
    original = converter_module._widen_to_object
    converter_module._widen_to_object = lambda series: pd.Series([], dtype=object)
    try:
        result = analyze_and_convert(pd.Series([1, 2]), "object")
        assert result.status == ConversionStatus.UNSAFE
        assert result.converted_series is None
        assert result.diagnostics[0].reason_code == ReasonCode.ROUND_TRIP_MISMATCH
    finally:
        converter_module._widen_to_object = original
