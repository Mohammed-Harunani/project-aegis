from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from consultant.consultant import AegisConsultant, RepairPlan
from governance.policy import GovernancePolicy
from governance.selector import RepairSelector
from src.column_order import (
    COLUMN_ORDER_ACTION_PREFIX,
    ColumnOrderEligibility,
    ColumnOrderReason,
    InvalidColumnOrderActionError,
    ParsedColumnOrderAction,
    analyze_reorder_only_eligibility,
    is_column_order_action,
    parse_column_order_action,
    serialize_column_order_action,
)
from src.inspector import ColumnStats, ObservedSchema, SchemaDelta


def _schema(order, dtype="object"):
    return ObservedSchema(
        columns={
            name: ColumnStats(null_count=0, unique_count=1, dtype=dtype)
            for name in dict.fromkeys(order)
            if type(name) is str
        },
        column_order=list(order),
    )


def _delta(**overrides):
    values = {
        "missing_columns": [],
        "new_columns": [],
        "type_mismatches": {},
        "reorder_event": True,
    }
    values.update(overrides)
    return SchemaDelta(**values)


def _analyze(observed=("b", "a"), gold=("a", "b"), delta=None):
    return analyze_reorder_only_eligibility(
        delta or _delta(),
        _schema(observed),
        _schema(gold),
    )


def _propose(observed_data, gold_data):
    from inspector import AegisInspector

    inspector = AegisInspector()
    consultant = AegisConsultant()
    observed_schema = inspector.generate_observed_schema(pd.DataFrame(observed_data))
    gold_schema = inspector.generate_observed_schema(pd.DataFrame(gold_data))
    delta = inspector.detect_delta(observed_schema, gold_schema)
    return consultant.propose_repairs(delta, observed_schema, gold_schema)


def _governance_policy():
    return GovernancePolicy(auto_approve_threshold=0.92, approval_threshold=0.80)


def test_canonical_serialization_and_round_trip():
    action = serialize_column_order_action(("customer_id", "email"))
    assert action == 'REORDER_COLUMNS TO ["customer_id","email"]'
    assert parse_column_order_action(action) == ParsedColumnOrderAction(
        ("customer_id", "email")
    )


def test_unicode_serialization_is_deterministic_and_not_ascii_escaped():
    assert serialize_column_order_action(["café", "識別子"]) == (
        'REORDER_COLUMNS TO ["café","識別子"]'
    )


def test_noncanonical_valid_json_parses_then_serializes_canonically():
    parsed = parse_column_order_action('REORDER_COLUMNS TO [ "a", "b" ]')
    assert serialize_column_order_action(parsed.target_order) == (
        'REORDER_COLUMNS TO ["a","b"]'
    )


def test_parsed_action_defensively_copies_and_freezes_target_order():
    supplied = ["a", "b"]
    parsed = ParsedColumnOrderAction(supplied)
    supplied.reverse()
    assert parsed.target_order == ("a", "b")
    with pytest.raises(FrozenInstanceError):
        parsed.target_order = ("b", "a")


@pytest.mark.parametrize(
    "action",
    [
        None,
        7,
        "",
        "REORDER_COLUMNS",
        "REORDER_COLUMNS TO",
        "REORDER_COLUMNS TO ",
        "REORDER_COLUMN TO [\"a\"]",
        "REORDER_COLUMNS TO not-json",
        "REORDER_COLUMNS TO {\"a\":1}",
        "REORDER_COLUMNS TO []",
        "REORDER_COLUMNS TO [1]",
        "REORDER_COLUMNS TO [\"a\",null]",
        "REORDER_COLUMNS TO [\"a\",\"a\"]",
        "REORDER_COLUMNS TO [\"a\"] trailing",
        "RENAME_COLUMN b -> a",
        "CAST_COLUMN a TO object",
    ],
)
def test_invalid_actions_are_rejected(action):
    with pytest.raises(InvalidColumnOrderActionError):
        parse_column_order_action(action)


@pytest.mark.parametrize("target", [None, "a", [], ["a", "a"], ["a", 1]])
def test_invalid_serialization_targets_are_rejected(target):
    with pytest.raises(InvalidColumnOrderActionError):
        serialize_column_order_action(target)


def test_action_discriminator_is_exact_type_and_prefix_only():
    action = 'REORDER_COLUMNS TO ["a"]'
    assert is_column_order_action(action) is True
    assert is_column_order_action("RENAME_COLUMN b -> a") is False
    assert is_column_order_action(None) is False
    assert COLUMN_ORDER_ACTION_PREFIX == "REORDER_COLUMNS TO "


def test_pure_reorder_is_eligible_with_immutable_orders():
    result = _analyze()
    assert result == ColumnOrderEligibility(
        eligible=True,
        reason_code=ColumnOrderReason.ELIGIBLE,
        observed_order=("b", "a"),
        gold_order=("a", "b"),
    )
    with pytest.raises(FrozenInstanceError):
        result.eligible = False


@pytest.mark.parametrize(
    ("delta", "reason"),
    [
        (_delta(reorder_event=False), ColumnOrderReason.NOT_REORDER_EVENT),
        (_delta(missing_columns=["a"]), ColumnOrderReason.MISSING_COLUMNS),
        (_delta(new_columns=["extra"]), ColumnOrderReason.NEW_COLUMNS),
        (
            _delta(type_mismatches={"a": {"observed": "x", "gold": "y"}}),
            ColumnOrderReason.TYPE_MISMATCHES,
        ),
    ],
)
def test_compound_or_absent_reorder_is_ineligible(delta, reason):
    result = _analyze(delta=delta)
    assert result.eligible is False
    assert result.reason_code is reason


def test_already_ordered_is_not_a_repair_even_if_delta_claims_reorder():
    result = _analyze(observed=("a", "b"), gold=("a", "b"))
    assert result.reason_code is ColumnOrderReason.ALREADY_ORDERED


def test_unequal_order_counts_fail_closed():
    result = _analyze(observed=("a", "b", "c"), gold=("a", "b"))
    assert result.reason_code is ColumnOrderReason.COLUMN_COUNT_MISMATCH


def test_unequal_order_membership_fails_closed():
    result = _analyze(observed=("a", "x"), gold=("a", "b"))
    assert result.reason_code is ColumnOrderReason.COLUMN_MEMBERSHIP_MISMATCH


def test_duplicate_observed_order_fails_closed():
    result = _analyze(observed=("b", "a", "a"), gold=("a", "b"))
    assert result.reason_code is ColumnOrderReason.DUPLICATE_OBSERVED_COLUMN


def test_duplicate_gold_order_fails_closed():
    result = _analyze(observed=("b", "a"), gold=("a", "b", "b"))
    assert result.reason_code is ColumnOrderReason.DUPLICATE_GOLD_COLUMN


def test_non_string_order_name_fails_closed():
    observed = SimpleNamespace(columns={"a": object(), 1: object()}, column_order=[1, "a"])
    result = analyze_reorder_only_eligibility(_delta(), observed, _schema(("a", "b")))
    assert result.reason_code is ColumnOrderReason.INVALID_OBSERVED_ORDER


def test_schema_mapping_must_match_its_declared_order():
    observed = _schema(("b", "a"))
    observed.columns.pop("b")
    result = analyze_reorder_only_eligibility(
        _delta(), observed, _schema(("a", "b"))
    )
    assert result.reason_code is ColumnOrderReason.OBSERVED_SCHEMA_INCONSISTENT


def test_gold_mapping_must_match_its_declared_order():
    gold = _schema(("a", "b"))
    gold.columns.pop("b")
    result = analyze_reorder_only_eligibility(_delta(), _schema(("b", "a")), gold)
    assert result.reason_code is ColumnOrderReason.GOLD_SCHEMA_INCONSISTENT


def test_dtype_is_rechecked_even_when_delta_claims_no_mismatch():
    observed = _schema(("b", "a"), dtype="object")
    gold = _schema(("a", "b"), dtype="object")
    gold.columns["a"] = ColumnStats(0, 1, "int64")
    result = analyze_reorder_only_eligibility(_delta(), observed, gold)
    assert result.reason_code is ColumnOrderReason.DTYPE_MISMATCH


def test_faulty_dtype_metadata_fails_closed_instead_of_escaping():
    class FaultyStats:
        @property
        def dtype(self):
            raise RuntimeError("metadata fault")

    observed = _schema(("b", "a"))
    observed.columns["a"] = FaultyStats()
    result = analyze_reorder_only_eligibility(
        _delta(), observed, _schema(("a", "b"))
    )
    assert result.reason_code is ColumnOrderReason.DTYPE_MISMATCH


@pytest.mark.parametrize(
    "malformed_delta",
    [
        object(),
        SimpleNamespace(
            reorder_event=1,
            missing_columns=[],
            new_columns=[],
            type_mismatches={},
        ),
        SimpleNamespace(
            reorder_event=True,
            missing_columns=(),
            new_columns=[],
            type_mismatches={},
        ),
    ],
)
def test_malformed_delta_fails_closed(malformed_delta):
    result = analyze_reorder_only_eligibility(
        malformed_delta,
        _schema(("b", "a")),
        _schema(("a", "b")),
    )
    assert result.reason_code is ColumnOrderReason.INVALID_DELTA


def test_column_order_package_has_no_forbidden_framework_imports():
    package_dir = Path(__file__).parents[1] / "src" / "column_order"
    source = "\n".join(path.read_text() for path in package_dir.glob("*.py"))
    for forbidden in ("pandas", "fastapi", "sqlalchemy", "src.db", "repository"):
        assert f"import {forbidden}" not in source.lower()
        assert f"from {forbidden}" not in source.lower()


def test_consultant_proposes_one_canonical_plan_for_pure_reorder():
    plans = _propose(
        {"email": ["a"], "id": [1], "active": [True]},
        {"id": [1], "email": ["a"], "active": [True]},
    )

    assert len(plans) == 1
    assert plans[0].proposed_action == (
        'REORDER_COLUMNS TO ["id","email","active"]'
    )
    assert plans[0].confidence == 0.90
    assert plans[0].explanation == (
        "Identical columns and dtypes detected in a different order. "
        "Exact Gold schema order proposed."
    )


def test_consultant_proposes_no_order_plan_when_already_ordered():
    assert _propose({"id": [1], "email": ["a"]}, {"id": [1], "email": ["a"]}) == []


def test_consultant_rejects_missing_column_as_order_repair():
    plans = _propose({"email": ["a"]}, {"id": [1], "email": ["a"]})
    assert not any(p.proposed_action.startswith("REORDER_COLUMNS") for p in plans)


def test_consultant_rejects_new_column_as_order_repair():
    plans = _propose(
        {"email": ["a"], "id": [1], "extra": [9]},
        {"id": [1], "email": ["a"]},
    )
    assert not any(p.proposed_action.startswith("REORDER_COLUMNS") for p in plans)


def test_consultant_rejects_reorder_with_type_mismatch_as_order_repair():
    plans = _propose(
        {"email": ["a"], "id": ["1"]},
        {"id": [1], "email": ["a"]},
    )
    assert not any(p.proposed_action.startswith("REORDER_COLUMNS") for p in plans)
    assert any(p.proposed_action.startswith("CAST_COLUMN id") for p in plans)


def test_consultant_does_not_add_order_plan_to_rename_shaped_delta():
    plans = _propose({"new_name": [1]}, {"old_name": [1]})
    assert [p.proposed_action for p in plans] == [
        "RENAME_COLUMN new_name -> old_name"
    ]


def test_consultant_reorder_proposal_is_deterministic():
    observed = {"β": [2], "α": [1]}
    gold = {"α": [1], "β": [2]}
    first = _propose(observed, gold)[0]
    second = _propose(observed, gold)[0]
    assert first == second
    assert first.proposed_action == 'REORDER_COLUMNS TO ["α","β"]'


def test_consultant_duplicate_order_metadata_fails_closed():
    stats = ColumnStats(null_count=0, unique_count=1, dtype="int64")
    observed = ObservedSchema(
        columns={"a": stats, "b": stats},
        column_order=["b", "a", "a"],
    )
    gold = ObservedSchema(
        columns={"a": stats, "b": stats},
        column_order=["a", "b"],
    )
    delta = SchemaDelta([], [], {}, True)

    assert AegisConsultant().propose_repairs(delta, observed, gold) == []


def test_selector_treats_verified_reorder_as_non_destructive():
    reorder = RepairPlan(
        proposed_action='REORDER_COLUMNS TO ["id","email"]',
        confidence=0.90,
        explanation="verified reorder",
    )
    destructive = RepairPlan(
        proposed_action="CAST_COLUMN id TO int64 WITH_DROP_INVALID",
        confidence=0.99,
        explanation="drops rows",
    )

    best = RepairSelector(governance_policy=_governance_policy()).choose_best(
        [destructive, reorder]
    )

    assert best is reorder


def test_verified_reorder_confidence_requires_human_approval_by_default():
    assert _governance_policy().evaluate(0.90) == "REQUIRES_HUMAN_APPROVAL"
