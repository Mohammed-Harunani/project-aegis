from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pandas as pd
import pytest

from consultant.consultant import RepairPlan
from inspector import AegisInspector, ColumnStats, ObservedSchema
from src.column_order import serialize_column_order_action
from src.live_execution.output_fingerprint import compute_dataframe_fingerprint
from surgeon.surgeon import AegisSurgeon


def _schema(dataframe):
    return AegisInspector().generate_observed_schema(dataframe)


def _plan(order):
    return RepairPlan(
        proposed_action=serialize_column_order_action(order),
        confidence=0.90,
        explanation="test order repair",
    )


def _execute(source, gold, *, action=None, observed_schema=None, mode="sandbox"):
    return AegisSurgeon().execute(
        repair_plan=(
            RepairPlan(action, 0.90, "test order repair")
            if action is not None
            else _plan(list(gold.columns))
        ),
        observed_schema=observed_schema or _schema(source),
        gold_schema=_schema(gold),
        target_dataset=source,
        operator="test_user",
        execution_mode=mode,
        allowed_modes=["sandbox", "live"],
    )


def test_sandbox_reorder_uses_exact_gold_order_and_preserves_source():
    source = pd.DataFrame({"b": [2, 3], "a": [0, 1]})
    pristine = source.copy(deep=True)
    gold = source.loc[:, ["a", "b"]]

    result, manifest = _execute(source, gold)

    assert result.applied is True
    assert result.validation.success is True
    assert list(manifest.corrected_dataset.columns) == ["a", "b"]
    pd.testing.assert_frame_equal(source, pristine)
    pd.testing.assert_frame_equal(
        manifest.corrected_dataset.loc[:, ["b", "a"]],
        pristine,
    )


def test_manifest_is_accurate_and_contains_no_conversion_metadata():
    source = pd.DataFrame({"b": [2], "a": [1]})
    gold = source.loc[:, ["a", "b"]]

    result, manifest = _execute(source, gold)

    assert result.applied is True
    assert manifest.original_row_count == 1
    assert manifest.final_row_count == 1
    assert manifest.integrity_status == "NO_VOLUME_CHANGE"
    assert manifest.risk_level == "LOW_RISK"
    assert manifest.conversion_outcome is None
    assert manifest.component_versions["surgeon"] == "2.1"
    assert manifest.component_versions["column_order"] == "phase3.3-policy-v1"
    assert "type_repair" not in manifest.component_versions
    assert result.validation.message == (
        "Verified column-order repair preserved rows, index, dtypes, "
        "values, and nulls."
    )


def test_reorder_preserves_index_values_order_name_dtype_and_rows():
    index = pd.Index([9, 3, 7], dtype="int64", name="record_id")
    source = pd.DataFrame(
        {"b": ["z", "x", "y"], "a": [3, 1, 2]},
        index=index,
    )
    gold = source.loc[:, ["a", "b"]]

    result, manifest = _execute(source, gold)

    assert result.validation.success is True
    corrected = manifest.corrected_dataset
    assert corrected.index.equals(index)
    assert corrected.index.name == "record_id"
    assert str(corrected.index.dtype) == "int64"
    assert corrected["a"].tolist() == [3, 1, 2]
    assert corrected["b"].tolist() == ["z", "x", "y"]


def test_reorder_preserves_dtypes_values_null_positions_and_null_kinds():
    source = pd.DataFrame(
        {
            "objects": pd.Series([None, "x"], dtype=object),
            "numbers": pd.Series([float("nan"), 2.5], dtype="float64"),
            "nullable": pd.Series([pd.NA, 4], dtype="Int64"),
        }
    )
    pristine = source.copy(deep=True)
    gold = source.loc[:, ["nullable", "objects", "numbers"]]

    result, manifest = _execute(source, gold)

    assert result.validation.success is True
    corrected = manifest.corrected_dataset
    assert [str(dtype) for dtype in corrected.dtypes] == [
        "Int64",
        "object",
        "float64",
    ]
    assert corrected["objects"].iloc[0] is None
    assert pd.isna(corrected["numbers"].iloc[0])
    assert corrected["nullable"].iloc[0] is pd.NA
    pd.testing.assert_frame_equal(corrected.loc[:, list(source.columns)], pristine)


def test_reorder_preserves_extended_object_values():
    nested = {"items": [1, {"ok": True}]}
    source = pd.DataFrame(
        {
            "payload": pd.Series(
                [
                    nested,
                    UUID("12345678-1234-5678-1234-567812345678"),
                    date(2026, 8, 13),
                    datetime(2026, 8, 13, 12, 30, tzinfo=timezone.utc),
                ],
                dtype=object,
            ),
            "amount": pd.Series(
                [Decimal("1.2300"), float("inf"), float("-inf"), None],
                dtype=object,
            ),
        }
    )
    pristine_payload = deepcopy(source["payload"].tolist())
    pristine_amount = deepcopy(source["amount"].tolist())
    gold = source.loc[:, ["amount", "payload"]]

    observed_schema = AegisSurgeon._dataset_schema(source)
    gold_schema = AegisSurgeon._dataset_schema(gold)
    result, manifest = AegisSurgeon().execute(
        repair_plan=_plan(["amount", "payload"]),
        observed_schema=observed_schema,
        gold_schema=gold_schema,
        target_dataset=source,
        operator="test_user",
    )

    assert result.validation.success is True
    corrected = manifest.corrected_dataset
    assert corrected["payload"].tolist() == pristine_payload
    assert corrected["amount"].tolist() == pristine_amount
    assert corrected["payload"].iloc[0] is nested
    assert compute_dataframe_fingerprint(corrected) == (
        compute_dataframe_fingerprint(gold)
    )


def test_empty_rows_with_declared_columns_reorder_successfully():
    source = pd.DataFrame(
        {
            "b": pd.Series(dtype="object"),
            "a": pd.Series(dtype="int64"),
        }
    )
    gold = source.loc[:, ["a", "b"]]

    result, manifest = _execute(source, gold)

    assert result.applied is True
    assert result.validation.success is True
    assert list(manifest.corrected_dataset.columns) == ["a", "b"]
    assert len(manifest.corrected_dataset) == 0


def test_already_ordered_single_column_is_rejected_as_ineligible():
    source = pd.DataFrame({"only": [1]})

    result, manifest = _execute(source, source.copy(deep=True))

    assert result.applied is False
    assert result.validation.success is False
    assert result.validation.message == (
        "Column-order repair is ineligible: NOT_REORDER_EVENT."
    )
    pd.testing.assert_frame_equal(manifest.corrected_dataset, source)


@pytest.mark.parametrize(
    "action",
    [
        "REORDER_COLUMNS",
        "REORDER_COLUMNS TO",
        "REORDER_COLUMNS TO ",
        'REORDER_COLUMNS TO ["a","b"] trailing',
        "REORDER_COLUMNS TO not-json",
        'REORDER_COLUMNS TO {"a":1}',
        'REORDER_COLUMNS TO ["a",1]',
    ],
)
def test_malformed_order_actions_fail_atomically(action):
    source = pd.DataFrame({"b": [2], "a": [1]})
    pristine = source.copy(deep=True)
    gold = source.loc[:, ["a", "b"]]

    result, manifest = _execute(source, gold, action=action)

    assert result.applied is False
    assert result.validation.success is False
    assert result.validation.message == "Invalid REORDER_COLUMNS action."
    assert manifest.conversion_outcome is None
    pd.testing.assert_frame_equal(source, pristine)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, pristine)


def test_stale_action_target_order_is_rejected():
    source = pd.DataFrame({"c": [3], "b": [2], "a": [1]})
    gold = source.loc[:, ["a", "b", "c"]]

    result, manifest = _execute(
        source,
        gold,
        action=serialize_column_order_action(["b", "a", "c"]),
    )

    assert result.applied is False
    assert result.validation.message == (
        "Column-order action does not match the Gold schema."
    )
    assert list(manifest.corrected_dataset.columns) == ["c", "b", "a"]


def test_duplicate_action_names_are_rejected_by_strict_parser():
    source = pd.DataFrame({"b": [2], "a": [1]})
    gold = source.loc[:, ["a", "b"]]

    result, manifest = _execute(
        source,
        gold,
        action='REORDER_COLUMNS TO ["a","a"]',
    )

    assert result.applied is False
    assert result.validation.message == "Invalid REORDER_COLUMNS action."
    pd.testing.assert_frame_equal(manifest.corrected_dataset, source)


def test_duplicate_dataset_labels_are_rejected_without_mutation():
    source = pd.DataFrame([[1, 2]], columns=["dup", "dup"])
    pristine = source.copy(deep=True)
    gold_schema = SimpleNamespace(
        column_order=["dup", "other"],
        columns={
            "dup": SimpleNamespace(dtype="int64"),
            "other": SimpleNamespace(dtype="int64"),
        },
    )
    observed_schema = SimpleNamespace(
        column_order=["other", "dup"],
        columns={
            "dup": SimpleNamespace(dtype="int64"),
            "other": SimpleNamespace(dtype="int64"),
        },
    )

    result, manifest = AegisSurgeon().execute(
        repair_plan=_plan(["dup", "other"]),
        observed_schema=observed_schema,
        gold_schema=gold_schema,
        target_dataset=source,
        operator="test_user",
    )

    assert result.applied is False
    assert result.validation.message == (
        "Target dataset has invalid or duplicate columns."
    )
    pd.testing.assert_frame_equal(source, pristine)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, pristine)


@pytest.mark.parametrize(
    ("source", "gold", "reason"),
    [
        (
            pd.DataFrame({"b": [2], "extra": [1]}),
            pd.DataFrame({"a": [1], "b": [2]}),
            "MISSING_COLUMNS",
        ),
        (
            pd.DataFrame({"b": [2], "a": [1], "extra": [3]}),
            pd.DataFrame({"a": [1], "b": [2]}),
            "NEW_COLUMNS",
        ),
        (
            pd.DataFrame(
                {"b": pd.Series([2], dtype="int64"), "a": ["1"]}
            ),
            pd.DataFrame(
                {"a": pd.Series([1], dtype="int64"), "b": [2]}
            ),
            "TYPE_MISMATCHES",
        ),
    ],
)
def test_compound_drift_is_rejected(source, gold, reason):
    pristine = source.copy(deep=True)

    result, manifest = _execute(source, gold)

    assert result.applied is False
    assert result.validation.message == (
        f"Column-order repair is ineligible: {reason}."
    )
    pd.testing.assert_frame_equal(source, pristine)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, pristine)


def test_stale_observed_schema_is_rejected_against_actual_dataset():
    source = pd.DataFrame({"c": [3], "b": [2], "a": [1]})
    stale = pd.DataFrame({"b": [2], "c": [3], "a": [999]})
    gold = source.loc[:, ["a", "b", "c"]]

    result, manifest = _execute(
        source,
        gold,
        observed_schema=_schema(stale),
    )

    assert result.applied is False
    assert result.validation.message == (
        "Observed schema does not match the target dataset."
    )
    pd.testing.assert_frame_equal(manifest.corrected_dataset, source)


def test_unexpected_preservation_exception_restores_pristine_snapshot():
    source = pd.DataFrame({"b": [2], "a": [1]})
    pristine = source.copy(deep=True)
    gold = source.loc[:, ["a", "b"]]

    with patch.object(
        AegisSurgeon,
        "_validate_reorder_candidate",
        side_effect=RuntimeError("controlled preservation failure"),
    ):
        result, manifest = _execute(source, gold)

    assert result.applied is False
    assert result.validation.success is False
    assert result.validation.message == "Execution error: RuntimeError."
    pd.testing.assert_frame_equal(source, pristine)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, pristine)


def test_reported_preservation_failure_is_atomic_and_redacted():
    source = pd.DataFrame({"b": [2], "a": [1]})
    pristine = source.copy(deep=True)
    gold = source.loc[:, ["a", "b"]]

    with patch.object(
        AegisSurgeon,
        "_validate_reorder_candidate",
        return_value="CONTROLLED_PROOF_FAILURE",
    ):
        result, manifest = _execute(source, gold)

    assert result.applied is False
    assert result.validation.success is False
    assert result.validation.message == (
        "Column-order preservation failed: CONTROLLED_PROOF_FAILURE."
    )
    pd.testing.assert_frame_equal(source, pristine)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, pristine)


def test_reorder_fingerprint_is_deterministic_and_order_sensitive():
    source = pd.DataFrame({"b": [2, 3], "a": [0, 1]})
    gold = source.loc[:, ["a", "b"]]

    first_result, first_manifest = _execute(source, gold)
    second_result, second_manifest = _execute(source, gold)
    expected = compute_dataframe_fingerprint(gold)

    assert first_result.validation.success is True
    assert second_result.validation.success is True
    assert (
        compute_dataframe_fingerprint(first_manifest.corrected_dataset)
        == expected
    )
    assert (
        compute_dataframe_fingerprint(second_manifest.corrected_dataset)
        == expected
    )
    assert compute_dataframe_fingerprint(source) != expected


def test_live_reorder_is_blocked_until_controlled_live_stage():
    source = pd.DataFrame({"b": [2], "a": [1]})
    pristine = source.copy(deep=True)
    gold = source.loc[:, ["a", "b"]]

    result, manifest = _execute(source, gold, mode="live")

    assert result.applied is False
    assert result.validation.success is False
    assert result.validation.message == (
        "REORDER_COLUMNS is not enabled for live execution."
    )
    assert manifest.conversion_outcome is None
    pd.testing.assert_frame_equal(source, pristine)
    pd.testing.assert_frame_equal(manifest.corrected_dataset, pristine)


def _source_logical_schema(order, *, null_count=0):
    return ObservedSchema(
        columns={
            name: ColumnStats(
                null_count=(null_count if name == "customer_id" else 0),
                unique_count=2,
                dtype="int64",
            )
            for name in order
        },
        column_order=list(order),
    )


def test_verified_source_logical_dtypes_allow_object_replay_reorder():
    source = pd.DataFrame(
        {"balance": [100, 200], "customer_id": [1, 2]},
        dtype=object,
    )
    observed = _source_logical_schema(("balance", "customer_id"))
    gold = _source_logical_schema(("customer_id", "balance"))

    result, manifest = AegisSurgeon().execute(
        repair_plan=_plan(["customer_id", "balance"]),
        observed_schema=observed,
        gold_schema=gold,
        target_dataset=source,
        operator="test_user",
        trusted_observed_schema=True,
    )

    assert result.applied is True
    assert result.validation.success is True
    assert list(manifest.corrected_dataset.columns) == [
        "customer_id",
        "balance",
    ]
    assert [str(dtype) for dtype in manifest.corrected_dataset.dtypes] == [
        "object",
        "object",
    ]


def test_unverified_logical_dtypes_do_not_override_replay_dtypes():
    source = pd.DataFrame(
        {"balance": [100, 200], "customer_id": [1, 2]},
        dtype=object,
    )

    result, manifest = AegisSurgeon().execute(
        repair_plan=_plan(["customer_id", "balance"]),
        observed_schema=_source_logical_schema(("balance", "customer_id")),
        gold_schema=_source_logical_schema(("customer_id", "balance")),
        target_dataset=source,
        operator="test_user",
    )

    assert result.applied is False
    assert result.validation.message == (
        "Observed schema does not match the target dataset."
    )
    pd.testing.assert_frame_equal(manifest.corrected_dataset, source)


def test_trusted_logical_schema_still_rejects_snapshot_null_mismatch():
    source = pd.DataFrame(
        {"balance": [100, 200], "customer_id": [1, 2]},
        dtype=object,
    )

    result, manifest = AegisSurgeon().execute(
        repair_plan=_plan(["customer_id", "balance"]),
        observed_schema=_source_logical_schema(
            ("balance", "customer_id"),
            null_count=1,
        ),
        gold_schema=_source_logical_schema(("customer_id", "balance")),
        target_dataset=source,
        operator="test_user",
        trusted_observed_schema=True,
    )

    assert result.applied is False
    assert result.validation.message == (
        "Observed schema does not match the target dataset."
    )
    pd.testing.assert_frame_equal(manifest.corrected_dataset, source)
