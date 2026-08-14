"""Pure governance-boundary tests for Phase 3.3.4."""

import pandas as pd
import pytest

from src.column_order import serialize_column_order_action
from src.consultant.consultant import RepairPlan
from src.governance.approval import ApprovalQueue
from src.governance.column_order_safety import (
    ColumnOrderApprovalBlockedError,
    InvalidColumnOrderEvidenceError,
    require_valid_column_order_evidence,
)
from src.governance.conversion_safety import analyze_cast_plan
from src.inspector import AegisInspector, ColumnStats, ObservedSchema


def _frame(order=("name", "customer_id")):
    data = {
        "customer_id": pd.Series([1, 2, 3], dtype="int64"),
        "name": pd.Series([10, 20, 30], dtype="int64"),
    }
    return pd.DataFrame({name: data[name] for name in order})


def _schemas(observed_order=("name", "customer_id"), gold_order=("customer_id", "name")):
    inspector = AegisInspector()
    return (
        inspector.generate_observed_schema(_frame(observed_order)),
        inspector.generate_observed_schema(_frame(gold_order)),
    )


def _plan(order=("customer_id", "name"), action=None):
    return RepairPlan(
        proposed_action=action or serialize_column_order_action(order),
        confidence=0.90,
        explanation="verified order",
    )


def _validate(
    *,
    plan=None,
    observed=None,
    gold=None,
    dataframe=None,
    conversion_decision=None,
):
    default_observed, default_gold = _schemas()
    return require_valid_column_order_evidence(
        plan or _plan(),
        observed or default_observed,
        gold or default_gold,
        dataframe if dataframe is not None else _frame(),
        conversion_decision,
    )


def test_valid_evidence_is_immutable_redacted_and_canonical():
    evidence = _validate()

    assert evidence.canonical_action == (
        'REORDER_COLUMNS TO ["customer_id","name"]'
    )
    assert evidence.observed_order == ("name", "customer_id")
    assert evidence.gold_order == ("customer_id", "name")
    assert evidence.to_dict() == {
        "canonical_action": 'REORDER_COLUMNS TO ["customer_id","name"]',
        "observed_order": ["name", "customer_id"],
        "gold_order": ["customer_id", "name"],
        "column_count": 2,
        "reorder_only": True,
    }
    assert "10" not in repr(evidence.to_dict())
    assert "20" not in repr(evidence.to_dict())


def test_non_order_plan_passes_without_order_evidence():
    observed, gold = _schemas()
    plan = RepairPlan("RENAME_COLUMN old -> new", 0.85, "rename")
    assert require_valid_column_order_evidence(
        plan, observed, gold, _frame(), None
    ) is None


@pytest.mark.parametrize(
    "action",
    [
        "REORDER_COLUMNS",
        "REORDER_COLUMNS TO",
        "REORDER_COLUMNS TO not-json",
        'REORDER_COLUMNS TO ["customer_id","customer_id"]',
    ],
)
def test_malformed_reserved_order_actions_are_rejected(action):
    with pytest.raises(InvalidColumnOrderEvidenceError, match="malformed"):
        _validate(plan=_plan(action=action))


def test_valid_but_noncanonical_action_is_rejected():
    with pytest.raises(InvalidColumnOrderEvidenceError, match="not canonical"):
        _validate(
            plan=_plan(
                action='REORDER_COLUMNS TO [ "customer_id", "name" ]'
            )
        )


def test_action_target_must_equal_persisted_gold_order():
    with pytest.raises(InvalidColumnOrderEvidenceError, match="Gold order"):
        _validate(plan=_plan(order=("name", "customer_id")))


def test_dtype_mismatch_is_compound_drift_and_rejected():
    observed, gold = _schemas()
    gold.columns["name"] = ColumnStats(0, 0, "float64")

    with pytest.raises(InvalidColumnOrderEvidenceError, match="TYPE_MISMATCHES"):
        _validate(observed=observed, gold=gold)


def test_missing_column_is_compound_drift_and_rejected():
    observed, gold = _schemas()
    observed = ObservedSchema(
        columns={"name": observed.columns["name"]},
        column_order=["name"],
    )

    with pytest.raises(InvalidColumnOrderEvidenceError, match="MISSING_COLUMNS"):
        _validate(observed=observed, gold=gold, dataframe=_frame(("name",)))


def test_already_ordered_ticket_is_rejected():
    observed, gold = _schemas(
        observed_order=("customer_id", "name"),
        gold_order=("customer_id", "name"),
    )

    with pytest.raises(InvalidColumnOrderEvidenceError, match="NOT_REORDER_EVENT"):
        _validate(
            observed=observed,
            gold=gold,
            dataframe=_frame(("customer_id", "name")),
        )


def test_replay_order_must_match_persisted_observed_order():
    with pytest.raises(InvalidColumnOrderEvidenceError, match="observed order"):
        _validate(dataframe=_frame(("customer_id", "name")))


def test_replay_membership_must_be_exact():
    with pytest.raises(InvalidColumnOrderEvidenceError, match="observed order"):
        _validate(dataframe=_frame(("name",)))


def test_duplicate_replay_columns_are_rejected():
    dataframe = pd.DataFrame([[1, 2]], columns=["name", "name"])
    with pytest.raises(InvalidColumnOrderEvidenceError, match="duplicate"):
        _validate(dataframe=dataframe)


def test_non_string_replay_columns_are_rejected():
    dataframe = pd.DataFrame([[1, 2]], columns=["name", 7])
    with pytest.raises(InvalidColumnOrderEvidenceError, match="invalid"):
        _validate(dataframe=dataframe)


def test_reorder_ticket_cannot_carry_cast_conversion_metadata():
    cast_plan = RepairPlan("CAST_COLUMN amount TO int64", 0.85, "cast")
    conversion = analyze_cast_plan(
        cast_plan,
        pd.DataFrame({"amount": ["1", "2"]}),
    )

    with pytest.raises(ColumnOrderApprovalBlockedError, match="conversion"):
        _validate(conversion_decision=conversion)


def test_in_memory_queue_validates_submission_and_approval_again():
    observed, gold = _schemas()
    queue = ApprovalQueue()
    ticket = queue.submit(_plan(), observed, gold, _frame())

    ticket.target_dataset = _frame(("customer_id", "name"))
    with pytest.raises(InvalidColumnOrderEvidenceError, match="observed order"):
        queue.approve(ticket.ticket_id, operator="mo")

    assert ticket.status == "PENDING"
    assert ticket.decided_by is None


def test_in_memory_queue_rejects_invalid_submission_without_ticket():
    observed, gold = _schemas()
    queue = ApprovalQueue()

    with pytest.raises(InvalidColumnOrderEvidenceError):
        queue.submit(
            _plan(action="REORDER_COLUMNS TO not-json"),
            observed,
            gold,
            _frame(),
        )

    assert queue.list_pending() == []
