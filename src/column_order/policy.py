"""
Aegis_ColumnOrderPolicy
Phase 3.3.2 -- strict action parsing, canonical serialization, and
independent proof that a schema delta is reorder-only.

This module is deliberately pure and uses only the standard library.
"""

import json
from typing import Optional, Tuple

from .models import (
    ColumnOrderEligibility,
    ColumnOrderReason,
    ParsedColumnOrderAction,
)


COLUMN_ORDER_ACTION_PREFIX = "REORDER_COLUMNS TO "
COLUMN_ORDER_CONFIDENCE = 0.90


class InvalidColumnOrderActionError(ValueError):
    """Raised when untrusted order-action text fails strict parsing."""


def _validated_order(
    raw_order,
    *,
    invalid_reason: ColumnOrderReason,
    duplicate_reason: ColumnOrderReason,
) -> Tuple[Optional[Tuple[str, ...]], Optional[ColumnOrderReason]]:
    if type(raw_order) not in (list, tuple):
        return None, invalid_reason

    order = tuple(raw_order)
    if not order or any(type(name) is not str for name in order):
        return None, invalid_reason
    if len(set(order)) != len(order):
        return None, duplicate_reason
    return order, None


def serialize_column_order_action(target_order) -> str:
    """Return the one canonical JSON-backed action representation."""

    if type(target_order) not in (list, tuple):
        raise InvalidColumnOrderActionError(
            "Column-order target must be a list or tuple of strings."
        )
    try:
        parsed = ParsedColumnOrderAction(tuple(target_order))
    except (TypeError, ValueError) as exc:
        raise InvalidColumnOrderActionError(str(exc)) from exc

    payload = json.dumps(
        list(parsed.target_order),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{COLUMN_ORDER_ACTION_PREFIX}{payload}"


def is_column_order_action(action) -> bool:
    return type(action) is str and action.startswith(COLUMN_ORDER_ACTION_PREFIX)


def parse_column_order_action(action) -> ParsedColumnOrderAction:
    """Strictly parse untrusted action text without evaluating it as code."""

    if type(action) is not str:
        raise InvalidColumnOrderActionError(
            "Column-order action must be an exact built-in string."
        )
    if not action.startswith(COLUMN_ORDER_ACTION_PREFIX):
        raise InvalidColumnOrderActionError("Invalid column-order action prefix.")

    payload = action[len(COLUMN_ORDER_ACTION_PREFIX):]
    if not payload:
        raise InvalidColumnOrderActionError("Column-order action payload is missing.")

    try:
        decoded = json.loads(payload)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise InvalidColumnOrderActionError(
            "Column-order action payload must be one valid JSON array."
        ) from exc

    if type(decoded) is not list:
        raise InvalidColumnOrderActionError(
            "Column-order action payload must be a JSON array."
        )

    try:
        return ParsedColumnOrderAction(tuple(decoded))
    except (TypeError, ValueError) as exc:
        raise InvalidColumnOrderActionError(str(exc)) from exc


def _result(
    eligible: bool,
    reason_code: ColumnOrderReason,
    observed_order: Tuple[str, ...] = (),
    gold_order: Tuple[str, ...] = (),
) -> ColumnOrderEligibility:
    return ColumnOrderEligibility(
        eligible=eligible,
        reason_code=reason_code,
        observed_order=observed_order,
        gold_order=gold_order,
    )


def analyze_reorder_only_eligibility(
    schema_delta,
    observed_schema,
    gold_schema,
) -> ColumnOrderEligibility:
    """
    Independently prove that the complete delta is one safe permutation.

    The proof deliberately rechecks order membership and dtypes instead of
    trusting SchemaDelta's summary fields alone.
    """

    try:
        reorder_event = schema_delta.reorder_event
        missing_columns = schema_delta.missing_columns
        new_columns = schema_delta.new_columns
        type_mismatches = schema_delta.type_mismatches
    except (AttributeError, TypeError):
        return _result(False, ColumnOrderReason.INVALID_DELTA)

    if type(reorder_event) is not bool:
        return _result(False, ColumnOrderReason.INVALID_DELTA)
    if type(missing_columns) is not list:
        return _result(False, ColumnOrderReason.INVALID_DELTA)
    if type(new_columns) is not list:
        return _result(False, ColumnOrderReason.INVALID_DELTA)
    if type(type_mismatches) is not dict:
        return _result(False, ColumnOrderReason.INVALID_DELTA)
    if reorder_event is not True:
        return _result(False, ColumnOrderReason.NOT_REORDER_EVENT)
    if missing_columns:
        return _result(False, ColumnOrderReason.MISSING_COLUMNS)
    if new_columns:
        return _result(False, ColumnOrderReason.NEW_COLUMNS)
    if type_mismatches:
        return _result(False, ColumnOrderReason.TYPE_MISMATCHES)

    try:
        raw_observed_order = observed_schema.column_order
        raw_gold_order = gold_schema.column_order
        observed_columns = observed_schema.columns
        gold_columns = gold_schema.columns
    except (AttributeError, TypeError):
        return _result(False, ColumnOrderReason.INVALID_OBSERVED_ORDER)

    observed_order, observed_error = _validated_order(
        raw_observed_order,
        invalid_reason=ColumnOrderReason.INVALID_OBSERVED_ORDER,
        duplicate_reason=ColumnOrderReason.DUPLICATE_OBSERVED_COLUMN,
    )
    if observed_error is not None:
        return _result(False, observed_error)

    gold_order, gold_error = _validated_order(
        raw_gold_order,
        invalid_reason=ColumnOrderReason.INVALID_GOLD_ORDER,
        duplicate_reason=ColumnOrderReason.DUPLICATE_GOLD_COLUMN,
    )
    if gold_error is not None:
        return _result(False, gold_error, observed_order)

    if observed_order == gold_order:
        return _result(
            False,
            ColumnOrderReason.ALREADY_ORDERED,
            observed_order,
            gold_order,
        )
    if len(observed_order) != len(gold_order):
        return _result(
            False,
            ColumnOrderReason.COLUMN_COUNT_MISMATCH,
            observed_order,
            gold_order,
        )
    if set(observed_order) != set(gold_order):
        return _result(
            False,
            ColumnOrderReason.COLUMN_MEMBERSHIP_MISMATCH,
            observed_order,
            gold_order,
        )

    if type(observed_columns) is not dict:
        return _result(
            False,
            ColumnOrderReason.OBSERVED_SCHEMA_INCONSISTENT,
            observed_order,
            gold_order,
        )
    if type(gold_columns) is not dict:
        return _result(
            False,
            ColumnOrderReason.GOLD_SCHEMA_INCONSISTENT,
            observed_order,
            gold_order,
        )
    if set(observed_columns.keys()) != set(observed_order):
        return _result(
            False,
            ColumnOrderReason.OBSERVED_SCHEMA_INCONSISTENT,
            observed_order,
            gold_order,
        )
    if set(gold_columns.keys()) != set(gold_order):
        return _result(
            False,
            ColumnOrderReason.GOLD_SCHEMA_INCONSISTENT,
            observed_order,
            gold_order,
        )

    for name in gold_order:
        try:
            observed_dtype = observed_columns[name].dtype
            gold_dtype = gold_columns[name].dtype
        except Exception:
            return _result(
                False,
                ColumnOrderReason.DTYPE_MISMATCH,
                observed_order,
                gold_order,
            )
        if (
            type(observed_dtype) is not str
            or type(gold_dtype) is not str
            or observed_dtype != gold_dtype
        ):
            return _result(
                False,
                ColumnOrderReason.DTYPE_MISMATCH,
                observed_order,
                gold_order,
            )

    return _result(
        True,
        ColumnOrderReason.ELIGIBLE,
        observed_order,
        gold_order,
    )
