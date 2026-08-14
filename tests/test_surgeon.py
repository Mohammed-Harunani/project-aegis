from dataclasses import FrozenInstanceError
from inspect import getsource
from unittest.mock import patch

import pandas as pd
import pytest

from consultant.consultant import AegisConsultant, RepairPlan
from inspector import AegisInspector
from surgeon.surgeon import AegisSurgeon
from src.live_execution.cast_allowlist import LiveCastPair
from type_repair import (
    ColumnConversionResult,
    ConversionDiagnostic,
    ConversionStatus,
    ReasonCode,
)


def _schema(dataframe: pd.DataFrame):
    return AegisInspector().generate_observed_schema(dataframe)


def _plan(action: str, confidence: float = 0.85) -> RepairPlan:
    return RepairPlan(
        proposed_action=action,
        confidence=confidence,
        explanation="test repair plan",
    )


def _execute(
    target: pd.DataFrame,
    gold: pd.DataFrame,
    action: str,
    *,
    execution_mode: str = "sandbox",
    allowed_modes=None,
    allowed_live_cast_pairs=None,
):
    return AegisSurgeon().execute(
        repair_plan=_plan(action),
        observed_schema=_schema(target),
        gold_schema=_schema(gold),
        target_dataset=target,
        operator="test_user",
        execution_mode=execution_mode,
        allowed_modes=allowed_modes,
        allowed_live_cast_pairs=allowed_live_cast_pairs,
    )


def test_surgeon_emits_healing_manifest_for_rename_unchanged():
    df_gold = pd.DataFrame({"old_name": [1, 2, 3]})
    df_new = pd.DataFrame({"new_name": [1, 2, 3]})

    inspector = AegisInspector()
    consultant = AegisConsultant()
    surgeon = AegisSurgeon()

    gold_schema = inspector.generate_observed_schema(df_gold)
    observed_schema = inspector.generate_observed_schema(df_new)
    delta = inspector.detect_delta(observed_schema, gold_schema)
    plans = consultant.propose_repairs(
        schema_delta=delta,
        observed_schema=observed_schema,
        gold_schema=gold_schema,
    )

    assert len(plans) == 1

    result, manifest = surgeon.execute(
        repair_plan=plans[0],
        observed_schema=observed_schema,
        gold_schema=gold_schema,
        target_dataset=df_new,
        operator="test_user",
    )

    assert result.applied is True
    assert result.validation.success is True
    assert manifest.repair_plan.proposed_action == "RENAME_COLUMN new_name -> old_name"
    assert manifest.execution_mode == "sandbox"
    assert manifest.operator == "test_user"
    assert manifest.component_versions["inspector"] == "1.0"
    assert manifest.component_versions["consultant"] == "1.2"
    assert manifest.component_versions["surgeon"] == "2.2"
    assert "type_repair" not in manifest.component_versions
    assert manifest.conversion_outcome is None
    assert list(manifest.corrected_dataset.columns) == ["old_name"]
    # Sandbox rename must not mutate the caller's source DataFrame.
    assert list(df_new.columns) == ["new_name"]


def test_rename_schema_mismatch_reports_failure_not_success_message():
    source = pd.DataFrame({"old": [1], "extra": [2]})
    gold = pd.DataFrame({"new": [1], "different": [2]})

    result, manifest = _execute(source, gold, "RENAME_COLUMN old -> new")

    assert result.applied is True
    assert result.validation.success is False
    assert result.validation.message == "Operation applied but schema mismatch."
    assert list(manifest.corrected_dataset.columns) == ["new", "extra"]


def test_safe_cast_delegates_to_verified_engine_and_preserves_source_dataframe():
    source = pd.DataFrame(
        {
            "amount": pd.Series(["1", "2"], dtype=object),
            "label": ["a", "b"],
        }
    )
    original = source.copy(deep=True)
    gold = pd.DataFrame(
        {
            "amount": pd.Series([1, 2], dtype="int64"),
            "label": ["a", "b"],
        }
    )

    result, manifest = _execute(source, gold, "CAST_COLUMN amount TO int64")

    assert result.applied is True
    assert result.validation.success is True
    pd.testing.assert_frame_equal(source, original)
    assert str(manifest.corrected_dataset["amount"].dtype) == "int64"
    assert manifest.corrected_dataset["amount"].tolist() == [1, 2]
    assert manifest.corrected_dataset["label"].tolist() == ["a", "b"]

    outcome = manifest.conversion_outcome
    assert outcome is not None
    assert outcome.column_name == "amount"
    assert outcome.status == "SAFE"
    assert outcome.source_dtype == "object"
    assert outcome.target_dtype == "int64"
    assert outcome.policy_version == "phase3.1-policy-v1"
    assert outcome.total_count == 2
    assert outcome.null_count == 0
    assert outcome.converted_count == 2
    assert outcome.failed_count == 0
    assert outcome.diagnostic_count == 0
    assert outcome.reason_codes == ()
    assert manifest.component_versions["type_repair"] == "phase3.1-policy-v1"
    assert "Verified cast SAFE" in result.validation.message
    assert "Schema matches gold" in result.validation.message


def test_unsafe_fractional_cast_is_atomic_and_redacted():
    source = pd.DataFrame({"amount": [1.5, 2.0]})
    original = source.copy(deep=True)
    gold = pd.DataFrame({"amount": pd.Series([1, 2], dtype="int64")})

    result, manifest = _execute(source, gold, "CAST_COLUMN amount TO int64")

    assert result.applied is False
    assert result.validation.success is False
    pd.testing.assert_frame_equal(source, original)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, original)

    outcome = manifest.conversion_outcome
    assert outcome.status == "UNSAFE"
    assert outcome.converted_count == 1
    assert outcome.failed_count == 1
    assert outcome.reason_codes == ("FRACTIONAL_VALUE",)
    assert "FRACTIONAL_VALUE" in result.validation.message
    assert "1.5" not in result.validation.message
    assert "1.5" not in str(manifest)
    assert manifest.risk_level == "HIGH_RISK"


def test_strict_boolean_tokens_are_converted_without_python_truthiness():
    source = pd.DataFrame(
        {"flag": pd.Series(["false", "0", "true", "1"], dtype=object)}
    )
    gold = pd.DataFrame({"flag": pd.Series([False, False, True, True], dtype="bool")})

    result, manifest = _execute(source, gold, "CAST_COLUMN flag TO bool")

    assert result.applied is True
    assert result.validation.success is True
    assert manifest.corrected_dataset["flag"].tolist() == [False, False, True, True]
    assert str(manifest.corrected_dataset["flag"].dtype) == "bool"
    assert manifest.conversion_outcome.status == "SAFE"


def test_ambiguous_boolean_is_rejected_without_mutation():
    source = pd.DataFrame({"flag": pd.Series(["yes", "false"], dtype=object)})
    original = source.copy(deep=True)
    gold = pd.DataFrame({"flag": pd.Series([True, False], dtype="bool")})

    result, manifest = _execute(source, gold, "CAST_COLUMN flag TO bool")

    assert result.applied is False
    assert result.validation.success is False
    pd.testing.assert_frame_equal(manifest.corrected_dataset, original)
    assert manifest.conversion_outcome.status == "UNSAFE"
    assert manifest.conversion_outcome.reason_codes == ("AMBIGUOUS_BOOLEAN",)


def test_integer_precision_loss_to_float64_is_rejected():
    source = pd.DataFrame({"amount": pd.Series([2**53 + 1], dtype="int64")})
    gold = pd.DataFrame({"amount": pd.Series([float(2**53)], dtype="float64")})

    result, manifest = _execute(source, gold, "CAST_COLUMN amount TO float64")

    assert result.applied is False
    assert manifest.conversion_outcome.status == "UNSAFE"
    assert manifest.conversion_outcome.reason_codes == ("PRECISION_LOSS",)


def test_unsupported_target_is_rejected_deterministically():
    source = pd.DataFrame({"amount": pd.Series(["1"], dtype=object)})
    gold = pd.DataFrame({"amount": pd.Series(["1"], dtype=object)})

    result, manifest = _execute(source, gold, "CAST_COLUMN amount TO decimal")

    assert result.applied is False
    assert result.validation.success is False
    assert manifest.conversion_outcome.status == "UNSUPPORTED"
    assert manifest.conversion_outcome.reason_codes == (
        "UNSUPPORTED_TARGET_DTYPE",
    )
    assert "UNSUPPORTED_TARGET_DTYPE" in result.validation.message


def test_with_drop_invalid_is_forbidden_and_never_calls_converter():
    source = pd.DataFrame({"amount": pd.Series(["1", "bad"], dtype=object)})
    original = source.copy(deep=True)
    gold = pd.DataFrame({"amount": pd.Series([1, 2], dtype="int64")})

    with patch(
        "surgeon.surgeon.analyze_and_convert",
        side_effect=AssertionError("converter must not be called"),
    ):
        result, manifest = _execute(
            source,
            gold,
            "CAST_COLUMN amount TO int64 WITH_DROP_INVALID",
        )

    assert result.applied is False
    assert result.validation.success is False
    assert "WITH_DROP_INVALID is forbidden" in result.validation.message
    assert manifest.conversion_outcome is None
    assert manifest.original_row_count == 2
    assert manifest.final_row_count == 2
    assert manifest.integrity_status == "NO_VOLUME_CHANGE"
    pd.testing.assert_frame_equal(source, original)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, original)


def test_live_cast_is_blocked_inside_surgeon_when_allowlist_is_empty():
    source = pd.DataFrame({"amount": pd.Series(["1"], dtype=object)})
    original = source.copy(deep=True)
    gold = pd.DataFrame({"amount": pd.Series([1], dtype="int64")})

    result, manifest = _execute(
        source,
        gold,
        "CAST_COLUMN amount TO int64",
        execution_mode="live",
        allowed_modes=["sandbox", "live"],
        allowed_live_cast_pairs=frozenset(),
    )

    assert result.applied is False
    assert result.validation.success is False
    assert "object->int64" in result.validation.message
    assert "not explicitly allowlisted" in result.validation.message
    assert manifest.conversion_outcome is None
    pd.testing.assert_frame_equal(source, original)


def test_allowlisted_safe_live_cast_returns_verified_corrected_manifest():
    source = pd.DataFrame({"amount": pd.Series(["1", "2", "-3"], dtype=object)})
    original = source.copy(deep=True)
    gold = pd.DataFrame({"amount": pd.Series([1, 2, -3], dtype="int64")})

    result, manifest = _execute(
        source,
        gold,
        "CAST_COLUMN amount TO int64",
        execution_mode="live",
        allowed_modes=["sandbox", "live"],
        allowed_live_cast_pairs={LiveCastPair("object", "int64")},
    )

    assert result.applied is True
    assert result.validation.success is True
    assert manifest.conversion_outcome.status == "SAFE"
    assert manifest.conversion_outcome.failed_count == 0
    assert manifest.corrected_dataset["amount"].tolist() == [1, 2, -3]
    assert str(manifest.corrected_dataset["amount"].dtype) == "int64"
    # Candidate-copy semantics prevent a partial mutation of the caller-owned
    # source frame; the API publishes only manifest.corrected_dataset.
    pd.testing.assert_frame_equal(source, original)


def test_allowlisted_but_unsafe_live_cast_remains_atomic_and_unapplied():
    source = pd.DataFrame({"amount": pd.Series(["1", "bad"], dtype=object)})
    original = source.copy(deep=True)
    gold = pd.DataFrame({"amount": pd.Series([1, 2], dtype="int64")})

    result, manifest = _execute(
        source,
        gold,
        "CAST_COLUMN amount TO int64",
        execution_mode="live",
        allowed_modes=["sandbox", "live"],
        allowed_live_cast_pairs={LiveCastPair("object", "int64")},
    )

    assert result.applied is False
    assert result.validation.success is False
    assert manifest.conversion_outcome.status == "UNSAFE"
    assert manifest.conversion_outcome.reason_codes == ("PARSE_ERROR",)
    pd.testing.assert_frame_equal(source, original)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, original)


def test_live_rename_behavior_remains_available_and_unchanged():
    source = pd.DataFrame({"old": [1, 2]})
    gold = pd.DataFrame({"new": [1, 2]})

    result, manifest = _execute(
        source,
        gold,
        "RENAME_COLUMN old -> new",
        execution_mode="live",
        allowed_modes=["sandbox", "live"],
    )

    assert result.applied is True
    assert result.validation.success is True
    assert list(source.columns) == ["new"]
    assert list(manifest.corrected_dataset.columns) == ["new"]
    assert manifest.conversion_outcome is None


def test_safe_float_cast_preserves_index_order_name_and_null_positions():
    index = pd.Index(["row-b", "row-a"], name="record_id")
    source = pd.DataFrame(
        {
            "amount": pd.Series(["0.5", None], index=index, name="amount", dtype=object),
            "other": pd.Series([10, 20], index=index),
        },
        index=index,
    )
    gold = pd.DataFrame(
        {
            "amount": pd.Series([0.5, float("nan")], index=index, dtype="float64"),
            "other": pd.Series([10, 20], index=index),
        },
        index=index,
    )

    result, manifest = _execute(source, gold, "CAST_COLUMN amount TO float64")

    assert result.applied is True
    assert result.validation.success is True
    converted = manifest.corrected_dataset
    assert converted.index.equals(index)
    assert list(converted.columns) == ["amount", "other"]
    assert converted["amount"].name == "amount"
    assert converted["amount"].iloc[0] == 0.5
    assert pd.isna(converted["amount"].iloc[1])
    assert manifest.conversion_outcome.null_count == 1
    assert manifest.conversion_outcome.converted_count == 1


def test_safe_cast_replaces_only_named_column():
    marker = object()
    source = pd.DataFrame(
        {
            "amount": pd.Series(["1"], dtype=object),
            "payload": pd.Series([marker], dtype=object),
        }
    )
    gold = pd.DataFrame(
        {
            "amount": pd.Series([1], dtype="int64"),
            "payload": pd.Series([marker], dtype=object),
        }
    )

    result, manifest = _execute(source, gold, "CAST_COLUMN amount TO int64")

    assert result.applied is True
    assert manifest.corrected_dataset["payload"].iloc[0] is marker
    assert source["payload"].iloc[0] is marker


def test_object_target_widens_safely_and_preserves_identity_and_nulls():
    marker = object()
    source = pd.DataFrame(
        {"payload": pd.Series([marker, None], dtype=object)}
    )
    gold = source.copy(deep=True)

    result, manifest = _execute(source, gold, "CAST_COLUMN payload TO object")

    assert result.applied is True
    assert result.validation.success is True
    converted = manifest.corrected_dataset["payload"]
    assert str(converted.dtype) == "object"
    assert converted.iloc[0] is marker
    assert converted.iloc[1] is None
    assert manifest.conversion_outcome.status == "SAFE"
    assert manifest.conversion_outcome.null_count == 1


def test_null_incompatible_int64_cast_is_rejected_atomically():
    source = pd.DataFrame(
        {"amount": pd.Series(["1", None], dtype=object)}
    )
    original = source.copy(deep=True)
    gold = pd.DataFrame(
        {"amount": pd.Series([1, 2], dtype="int64")}
    )

    result, manifest = _execute(source, gold, "CAST_COLUMN amount TO int64")

    assert result.applied is False
    assert result.validation.success is False
    assert manifest.conversion_outcome.status == "UNSAFE"
    assert manifest.conversion_outcome.null_count == 1
    assert manifest.conversion_outcome.failed_count == 1
    assert manifest.conversion_outcome.reason_codes == (
        "NULL_TARGET_INCOMPATIBLE",
    )
    pd.testing.assert_frame_equal(manifest.corrected_dataset, original)


def test_malformed_safe_engine_result_cannot_be_assigned():
    source = pd.DataFrame(
        {"amount": pd.Series(["1"], index=pd.Index([10]), dtype=object)}
    )
    original = source.copy(deep=True)
    gold = pd.DataFrame(
        {"amount": pd.Series([1], index=pd.Index([10]), dtype="int64")}
    )
    malformed_safe = ColumnConversionResult(
        status=ConversionStatus.SAFE,
        source_dtype="object",
        target_dtype="int64",
        total_count=1,
        null_count=0,
        converted_count=1,
        failed_count=0,
        diagnostics=(),
        converted_series=pd.Series([1], index=pd.Index([999]), name="amount", dtype="int64"),
        policy_version="phase3.1-policy-v1",
    )

    with patch("surgeon.surgeon.analyze_and_convert", return_value=malformed_safe):
        result, manifest = _execute(source, gold, "CAST_COLUMN amount TO int64")

    assert result.applied is False
    assert result.validation.success is False
    assert result.validation.message == (
        "Execution error: invalid SAFE conversion result."
    )
    pd.testing.assert_frame_equal(source, original)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, original)


def test_post_operation_schema_validation_still_runs_after_safe_cast():
    source = pd.DataFrame(
        {
            "amount": pd.Series(["1"], dtype=object),
            "other": [10],
        }
    )
    # Same names and target dtype, deliberately different Gold order.
    gold = pd.DataFrame(
        {
            "other": [10],
            "amount": pd.Series([1], dtype="int64"),
        }
    )

    result, manifest = _execute(source, gold, "CAST_COLUMN amount TO int64")

    assert result.applied is True
    assert result.validation.success is False
    assert "Operation applied but schema mismatch" in result.validation.message
    assert str(manifest.corrected_dataset["amount"].dtype) == "int64"


def test_missing_cast_column_is_rejected_without_conversion_metadata():
    source = pd.DataFrame({"present": [1]})
    gold = source.copy(deep=True)

    result, manifest = _execute(source, gold, "CAST_COLUMN missing TO int64")

    assert result.applied is False
    assert result.validation.success is False
    assert result.validation.message == "Cast source column not found: missing."
    assert manifest.conversion_outcome is None


@pytest.mark.parametrize(
    "action",
    [
        "CAST_COLUMN",
        "CAST_COLUMN amount AS int64",
        "CAST_COLUMN amount TO int64 EXTRA",
        "RENAME_COLUMN bad",
        "UNKNOWN_ACTION amount",
    ],
)
def test_malformed_or_unknown_actions_are_rejected_deterministically(action):
    source = pd.DataFrame({"amount": [1]})
    gold = source.copy(deep=True)

    result, manifest = _execute(source, gold, action)

    assert result.applied is False
    assert result.validation.success is False
    assert manifest.conversion_outcome is None


def test_engine_error_result_is_not_applied_and_is_recorded_in_manifest():
    source = pd.DataFrame({"amount": pd.Series(["1"], dtype=object)})
    gold = pd.DataFrame({"amount": pd.Series([1], dtype="int64")})
    engine_error = ColumnConversionResult(
        status=ConversionStatus.ERROR,
        source_dtype="object",
        target_dtype="int64",
        total_count=1,
        null_count=0,
        converted_count=0,
        failed_count=1,
        diagnostics=(
            ConversionDiagnostic(
                row_index=None,
                source_value_type="object",
                target_dtype="int64",
                reason_code=ReasonCode.INTERNAL_CONVERSION_ERROR,
                message="Internal conversion failure.",
            ),
        ),
        converted_series=None,
        policy_version="phase3.1-policy-v1",
    )

    with patch("surgeon.surgeon.analyze_and_convert", return_value=engine_error):
        result, manifest = _execute(source, gold, "CAST_COLUMN amount TO int64")

    assert result.applied is False
    assert result.validation.success is False
    assert manifest.conversion_outcome.status == "ERROR"
    assert manifest.conversion_outcome.reason_codes == (
        "INTERNAL_CONVERSION_ERROR",
    )
    assert manifest.corrected_dataset["amount"].tolist() == ["1"]


def test_conversion_summary_and_metadata_are_deterministic():
    source = pd.DataFrame({"amount": [1.5, 2.5]})
    gold = pd.DataFrame({"amount": pd.Series([1, 2], dtype="int64")})

    first_result, first_manifest = _execute(
        source,
        gold,
        "CAST_COLUMN amount TO int64",
    )
    second_result, second_manifest = _execute(
        source,
        gold,
        "CAST_COLUMN amount TO int64",
    )

    assert first_result.validation.message == second_result.validation.message
    assert first_manifest.conversion_outcome == second_manifest.conversion_outcome
    assert first_manifest.conversion_outcome.reason_codes == (
        "FRACTIONAL_VALUE",
    )
    assert first_manifest.conversion_outcome.diagnostic_count == 2
    # Both the human summary and compact audit metadata de-duplicate reason
    # codes, while diagnostic_count preserves the complete failure volume.
    assert first_result.validation.message.count("FRACTIONAL_VALUE") == 1


def test_conversion_metadata_is_frozen_and_reason_codes_are_immutable():
    source = pd.DataFrame({"amount": [1.5]})
    gold = pd.DataFrame({"amount": pd.Series([1], dtype="int64")})

    _, manifest = _execute(source, gold, "CAST_COLUMN amount TO int64")
    outcome = manifest.conversion_outcome

    assert type(outcome.reason_codes) is tuple
    with pytest.raises(FrozenInstanceError):
        outcome.status = "SAFE"
    with pytest.raises(AttributeError):
        outcome.reason_codes.append("PARSE_ERROR")


def test_unexpected_sandbox_exception_restores_pristine_working_copy():
    source = pd.DataFrame({"amount": pd.Series(["1"], dtype=object)})
    original = source.copy(deep=True)

    class ExplodingGoldSchema:
        @property
        def column_order(self):
            raise RuntimeError("controlled schema validation failure")

    result, manifest = AegisSurgeon().execute(
        repair_plan=_plan("CAST_COLUMN amount TO int64"),
        observed_schema=_schema(source),
        gold_schema=ExplodingGoldSchema(),
        target_dataset=source,
        operator="test_user",
        execution_mode="sandbox",
    )

    assert result.applied is False
    assert result.validation.success is False
    assert result.validation.message == "Execution error: RuntimeError."
    pd.testing.assert_frame_equal(source, original)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, original)
    # The conversion analysis itself was safe and remains auditable even
    # though the later sandbox schema-validation step failed.
    assert manifest.conversion_outcome.status == "SAFE"


def test_surgeon_source_contains_no_direct_astype_casting():
    source = getsource(AegisSurgeon)
    assert ".astype(" not in source
    assert "analyze_and_convert" in source
