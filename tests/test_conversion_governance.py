"""Pure tests for Phase 3.1.4 conversion governance and serialization."""

import pandas as pd
import pytest

from src.consultant.consultant import RepairPlan
from src.governance.approval import ApprovalQueue
from src.governance.conversion_safety import (
    ConversionApprovalBlockedError,
    ConversionGovernanceError,
    InvalidCastActionError,
    StaleConversionDecisionError,
    analyze_cast_plan,
    is_cast_action,
    parse_cast_action,
    require_safe_conversion_decision,
)
from src.governance.manifest import ConversionOutcomeMetadata


def cast_plan(target="int64", suffix=""):
    action = f"CAST_COLUMN amount TO {target}"
    if suffix:
        action = f"{action} {suffix}"
    return RepairPlan(action, 0.99, "verified cast")


def rename_plan():
    return RepairPlan("RENAME_COLUMN amount_raw -> amount", 0.85, "rename")


def safe_df():
    return pd.DataFrame({"amount": ["1", "2", "-3"]}, index=[10, 20, 30])


def safe_metadata():
    return analyze_cast_plan(cast_plan(), safe_df())


def test_is_cast_action_is_strict_and_non_cast_is_false():
    assert is_cast_action("CAST_COLUMN amount TO int64") is True
    assert is_cast_action("RENAME_COLUMN amount -> total") is False
    assert is_cast_action(None) is False


def test_parse_cast_action_returns_typed_fields():
    parsed = parse_cast_action("CAST_COLUMN amount TO float64")
    assert parsed.column == "amount"
    assert parsed.target_dtype == "float64"
    assert parsed.drop_invalid_requested is False


def test_parse_cast_action_detects_destructive_suffix():
    parsed = parse_cast_action(
        "CAST_COLUMN amount TO int64 WITH_DROP_INVALID"
    )
    assert parsed.drop_invalid_requested is True


@pytest.mark.parametrize(
    "action",
    [
        None,
        "CAST_COLUMN",
        "CAST_COLUMN amount int64",
        "CAST amount TO int64",
        "CAST_COLUMN amount AS int64",
        "CAST_COLUMN amount TO int64 EXTRA",
        "cast_column amount TO int64",
    ],
)
def test_invalid_cast_actions_are_rejected(action):
    with pytest.raises(InvalidCastActionError):
        parse_cast_action(action)


def test_safe_cast_analysis_returns_redacted_safe_decision():
    decision = analyze_cast_plan(cast_plan(), safe_df())
    assert decision.status == "SAFE"
    assert decision.is_safe is True
    assert decision.column_name == "amount"
    assert decision.target_dtype == "int64"
    assert decision.total_count == 3
    assert decision.converted_count == 3
    assert decision.failed_count == 0
    assert decision.diagnostic_count == 0
    assert decision.reason_codes == ()


def test_unsafe_cast_analysis_returns_reasons_without_raw_values():
    df = pd.DataFrame({"amount": [1.5, 2.0]})
    decision = analyze_cast_plan(cast_plan(), df)
    payload = decision.to_dict()

    assert decision.status == "UNSAFE"
    assert "FRACTIONAL_VALUE" in decision.reason_codes
    assert set(payload) == {
        "column_name",
        "status",
        "source_dtype",
        "target_dtype",
        "policy_version",
        "total_count",
        "null_count",
        "converted_count",
        "failed_count",
        "diagnostic_count",
        "reason_codes",
    }
    text = repr(payload)
    assert "1.5" not in text
    assert "row_index" not in text
    assert "message" not in text


def test_unsupported_target_is_exposed_as_redacted_decision():
    decision = analyze_cast_plan(cast_plan(target="decimal"), safe_df())
    assert decision.status == "UNSUPPORTED"
    assert decision.reason_codes == ("UNSUPPORTED_TARGET_DTYPE",)


def test_analysis_never_mutates_input_dataframe():
    df = safe_df()
    before = df.copy(deep=True)
    analyze_cast_plan(cast_plan(), df)
    pd.testing.assert_frame_equal(df, before)


def test_missing_source_column_is_governance_error():
    with pytest.raises(ConversionGovernanceError):
        analyze_cast_plan(cast_plan(), pd.DataFrame({"other": [1]}))


def test_with_drop_invalid_is_blocked_before_analysis():
    with pytest.raises(ConversionApprovalBlockedError):
        analyze_cast_plan(
            cast_plan(suffix="WITH_DROP_INVALID"),
            safe_df(),
        )


def test_metadata_json_round_trip_is_exact_and_immutable():
    original = safe_metadata()
    payload = original.to_dict()
    restored = ConversionOutcomeMetadata.from_dict(payload)

    assert restored == original
    assert isinstance(restored.reason_codes, tuple)
    payload["reason_codes"].append("TAMPERED")
    assert restored.reason_codes == ()


def test_metadata_rejects_unknown_and_missing_fields():
    payload = safe_metadata().to_dict()
    payload["raw_value"] = "secret"
    with pytest.raises(ValueError, match="unknown"):
        ConversionOutcomeMetadata.from_dict(payload)

    payload = safe_metadata().to_dict()
    del payload["policy_version"]
    with pytest.raises(ValueError, match="missing"):
        ConversionOutcomeMetadata.from_dict(payload)


def test_safe_metadata_rejects_failures_or_diagnostics():
    payload = safe_metadata().to_dict()
    payload["failed_count"] = 1
    with pytest.raises(ValueError, match="SAFE"):
        ConversionOutcomeMetadata.from_dict(payload)


def test_non_safe_metadata_requires_a_diagnostic():
    payload = safe_metadata().to_dict()
    payload["status"] = "UNSAFE"
    with pytest.raises(ValueError, match="diagnostic"):
        ConversionOutcomeMetadata.from_dict(payload)


def test_duplicate_reason_codes_are_rejected():
    unsafe = analyze_cast_plan(
        cast_plan(),
        pd.DataFrame({"amount": [1.5]}),
    ).to_dict()
    unsafe["reason_codes"] = ["FRACTIONAL_VALUE", "FRACTIONAL_VALUE"]
    unsafe["diagnostic_count"] = 2
    with pytest.raises(ValueError, match="duplicates"):
        ConversionOutcomeMetadata.from_dict(unsafe)


def test_safe_persisted_decision_allows_approval_after_fresh_match():
    df = safe_df()
    decision = analyze_cast_plan(cast_plan(), df)
    fresh = require_safe_conversion_decision(cast_plan(), df, decision)
    assert fresh == decision


def test_non_cast_plan_with_no_conversion_decision_remains_allowed():
    assert require_safe_conversion_decision(rename_plan(), safe_df(), None) is None


def test_non_cast_plan_with_conversion_decision_is_blocked():
    with pytest.raises(ConversionApprovalBlockedError):
        require_safe_conversion_decision(
            rename_plan(),
            safe_df(),
            safe_metadata(),
        )


def test_cast_ticket_without_persisted_decision_is_blocked():
    with pytest.raises(ConversionApprovalBlockedError, match="no persisted"):
        require_safe_conversion_decision(cast_plan(), safe_df(), None)


def test_unsafe_persisted_decision_cannot_be_human_overridden():
    df = pd.DataFrame({"amount": [1.5]})
    unsafe = analyze_cast_plan(cast_plan(), df)
    with pytest.raises(ConversionApprovalBlockedError, match="not SAFE"):
        require_safe_conversion_decision(cast_plan(), df, unsafe)


def test_changed_dataset_makes_persisted_decision_stale():
    decision = safe_metadata()
    changed = safe_df()
    changed.loc[20, "amount"] = "not-an-int"
    with pytest.raises(StaleConversionDecisionError):
        require_safe_conversion_decision(cast_plan(), changed, decision)


def test_changed_plan_makes_persisted_decision_stale():
    with pytest.raises(StaleConversionDecisionError):
        require_safe_conversion_decision(
            cast_plan(target="float64"),
            safe_df(),
            safe_metadata(),
        )


def test_repeated_analysis_is_deterministic():
    first = analyze_cast_plan(cast_plan(), safe_df())
    second = analyze_cast_plan(cast_plan(), safe_df())
    assert first == second
    assert first.to_dict() == second.to_dict()


def test_in_memory_approval_ticket_retains_conversion_decision():
    queue = ApprovalQueue()
    df = safe_df()
    decision = analyze_cast_plan(cast_plan(), df)

    ticket = queue.submit(
        cast_plan(),
        observed_schema=None,
        gold_schema=None,
        target_dataset=df,
        conversion_decision=decision,
    )

    assert ticket.conversion_decision == decision
    assert ticket.conversion_decision.is_safe is True


def test_postgres_repository_submit_serializes_and_restores_decision_without_database():
    from src.governance.approval_repository import PostgresApprovalRepository

    class FakeDB:
        def __init__(self):
            self.added = None
            self.commits = 0

        def add(self, record):
            self.added = record

        def commit(self):
            self.commits += 1

        def refresh(self, record):
            pass

    df = safe_df()
    inspector = __import__("src.inspector", fromlist=["AegisInspector"]).AegisInspector()
    observed = inspector.generate_observed_schema(df)
    gold = inspector.generate_observed_schema(pd.DataFrame({"amount": [1, 2, 3]}))
    decision = safe_metadata()
    db = FakeDB()

    ticket = PostgresApprovalRepository(db).submit(
        repair_plan=cast_plan(),
        observed_schema=observed,
        gold_schema=gold,
        target_dataset=df,
        conversion_decision=decision,
    )

    assert db.commits == 1
    assert db.added.conversion_decision == decision.to_dict()
    assert ticket.conversion_decision == decision
    pd.testing.assert_frame_equal(ticket.target_dataset, df)


def test_manifest_repository_serializes_conversion_outcome_without_database():
    from src.governance.manifest_repository import save_manifest
    from src.inspector import AegisInspector
    from src.surgeon import AegisSurgeon

    class FakeDB:
        def __init__(self):
            self.added = None
            self.commits = 0
            self.flushes = 0

        def add(self, record):
            self.added = record

        def commit(self):
            self.commits += 1

        def refresh(self, record):
            pass

        def flush(self):
            self.flushes += 1

    df = safe_df()
    inspector = AegisInspector()
    observed = inspector.generate_observed_schema(df)
    gold = inspector.generate_observed_schema(pd.DataFrame({"amount": [1, 2, 3]}))
    result, manifest = AegisSurgeon().execute(
        repair_plan=cast_plan(),
        observed_schema=observed,
        gold_schema=gold,
        target_dataset=df,
        operator="mo",
    )
    assert result.applied is True

    db = FakeDB()
    record = save_manifest(db, manifest)

    assert record is db.added
    assert db.commits == 1
    assert db.added.conversion_outcome == manifest.conversion_outcome.to_dict()
    assert "raw_value" not in db.added.conversion_outcome
    assert "row_index" not in db.added.conversion_outcome


def test_in_memory_queue_cannot_approve_unsafe_cast():
    queue = ApprovalQueue()
    df = pd.DataFrame({"amount": [1.5]})
    plan = cast_plan()
    unsafe = analyze_cast_plan(plan, df)
    ticket = queue.submit(
        plan,
        observed_schema=None,
        gold_schema=None,
        target_dataset=df,
        conversion_decision=unsafe,
    )

    with pytest.raises(ConversionApprovalBlockedError):
        queue.approve(ticket.ticket_id, operator="mo")

    assert ticket.status == "PENDING"
    assert ticket.decided_by is None


def test_postgres_repository_approve_enforces_conversion_before_status_mutation():
    from src.governance.approval_repository import PostgresApprovalRepository
    from src.inspector import AegisInspector

    class FakeDB:
        def __init__(self):
            self.added = None
            self.flushes = 0

        def add(self, record):
            self.added = record

        def commit(self):
            pass

        def refresh(self, record):
            pass

        def flush(self):
            self.flushes += 1

    df = pd.DataFrame({"amount": [1.5]})
    inspector = AegisInspector()
    observed = inspector.generate_observed_schema(df)
    gold = inspector.generate_observed_schema(pd.DataFrame({"amount": [1]}))
    plan = cast_plan()
    unsafe = analyze_cast_plan(plan, df)
    db = FakeDB()
    repo = PostgresApprovalRepository(db)
    repo.submit(
        repair_plan=plan,
        observed_schema=observed,
        gold_schema=gold,
        target_dataset=df,
        conversion_decision=unsafe,
    )
    repo._get_record_for_update = lambda ticket_id: db.added

    with pytest.raises(ConversionApprovalBlockedError):
        repo.approve(str(db.added.ticket_id), operator="mo")

    assert db.added.status == "PENDING"
    assert db.added.decided_by is None
    assert db.flushes == 0
