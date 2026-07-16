"""
Pure-Python tests for Phase 2.5's identifier validation and dtype
mapping. No database dependency -- these run in any environment,
unlike the rest of Phase 2.5's tests.
"""

import uuid

from live_execution.identifiers import (
    InvalidIdentifierError,
    validate_identifier,
    shadow_table_name,
    backup_table_name,
)
from live_execution.dtype_mapping import (
    UnsupportedDtypeError,
    pandas_dtype_to_postgres_type,
)
from live_execution.safety import (
    LiveExecutionNotAllowedError,
    LiveExecutionConflictError,
    evaluate_safety_gates,
    get_target_schema_allowlist,
    verify_output_fingerprint_match,
)


def test_valid_identifiers_pass():
    for name in ["customer_master", "_leading_underscore", "a", "table_123"]:
        assert validate_identifier(name, "test") == name


def test_invalid_identifiers_rejected():
    bad = [
        "1_leading_digit",
        "has-a-dash",
        "has a space",
        "has.a.dot",
        "DROP TABLE x;--",
        "",
        "a" * 64,  # 64 chars, over the 63-byte Postgres limit
    ]
    for name in bad:
        try:
            validate_identifier(name, "test")
            assert False, f"expected rejection for {name!r}"
        except InvalidIdentifierError:
            pass


def test_sql_injection_attempt_rejected():
    malicious = 'customer_master"; DROP TABLE approval_tickets; --'
    try:
        validate_identifier(malicious, "target table")
        assert False, "expected rejection"
    except InvalidIdentifierError:
        pass


def test_shadow_and_backup_table_names_are_distinct_and_deterministic():
    execution_id = uuid.uuid4()
    shadow = shadow_table_name("customer_master", execution_id)
    backup = backup_table_name("customer_master", execution_id)

    assert shadow != backup
    assert shadow.startswith("customer_master__aegis_shadow_")
    assert backup.startswith("customer_master__aegis_backup_")
    # Deterministic given the same execution_id -- calling again must match.
    assert shadow_table_name("customer_master", execution_id) == shadow


def test_generated_names_never_exceed_postgres_identifier_limit():
    """
    The specific bug this replaced: a 63-char target_table name caused
    the naive version to silently truncate away the ENTIRE suffix,
    including the shadow/backup marker itself -- making shadow and
    backup names identical and colliding across different executions.
    """
    from live_execution.identifiers import MAX_IDENTIFIER_LENGTH

    long_name = "a" * 63
    exec_id_1 = uuid.uuid4()
    exec_id_2 = uuid.uuid4()

    shadow_1 = shadow_table_name(long_name, exec_id_1)
    backup_1 = backup_table_name(long_name, exec_id_1)
    shadow_2 = shadow_table_name(long_name, exec_id_2)

    assert len(shadow_1) <= MAX_IDENTIFIER_LENGTH
    assert len(backup_1) <= MAX_IDENTIFIER_LENGTH
    assert shadow_1 != backup_1, "shadow and backup must differ even when truncated"
    assert shadow_1 != shadow_2, "different executions must not collide even when truncated"


def test_dtype_mapping_known_types():
    assert pandas_dtype_to_postgres_type("int64") == "BIGINT"
    assert pandas_dtype_to_postgres_type("float64") == "DOUBLE PRECISION"
    assert pandas_dtype_to_postgres_type("object") == "TEXT"
    assert pandas_dtype_to_postgres_type("bool") == "BOOLEAN"
    assert pandas_dtype_to_postgres_type("Int64") == "BIGINT"


def test_unmapped_dtype_raises_rather_than_silently_falling_back():
    for bad_dtype in ["category", "complex128", "timedelta64[ns]", "not_a_real_dtype"]:
        try:
            pandas_dtype_to_postgres_type(bad_dtype)
            assert False, f"expected UnsupportedDtypeError for {bad_dtype!r}"
        except UnsupportedDtypeError:
            pass


def _valid_gate_kwargs(**overrides):
    base = dict(
        schema_version_id="some-uuid",
        sandbox_manifest_applied=True,
        sandbox_risk_level="LOW_RISK",
        sandbox_integrity_status="NO_VOLUME_CHANGE",
        original_row_count=10,
        final_row_count=10,
        proposed_action="RENAME_COLUMN Customer_ID -> customer_id",
        target_schema="warehouse",
        target_table="customer_master_live",
        source_schema="raw",
        source_table="customer_master_source",
        confirm=True,
        schema_allowlist=["warehouse"],
        already_executed_live=False,
        unresolved_execution_exists_for_target=False,
    )
    base.update(overrides)
    return base


def test_all_gates_pass_for_a_valid_request():
    evaluate_safety_gates(**_valid_gate_kwargs())  # must not raise


def test_missing_schema_version_id_rejected():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(schema_version_id=None))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_no_sandbox_manifest_rejected():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(sandbox_manifest_applied=False))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_wrong_risk_level_rejected():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(sandbox_risk_level="MEDIUM_RISK"))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_wrong_integrity_status_rejected():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(sandbox_integrity_status="DATA_LOSS_EVENT"))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_row_count_mismatch_rejected():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(original_row_count=10, final_row_count=8))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_destructive_repair_rejected():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(
            proposed_action="CAST_COLUMN amount TO float64 WITH_DROP_INVALID"
        ))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_non_allowlisted_schema_rejected():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(target_schema="not_allowlisted"))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_unsafe_target_identifier_rejected():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(target_table='x"; DROP TABLE approval_tickets; --'))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_target_same_as_source_rejected():
    """source_schema/source_table are now mandatory -- originally
    optional, which converted a mandatory safety gate into one a
    caller could simply omit to bypass."""
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(
            source_schema="warehouse", source_table="customer_master_live",
        ))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_target_different_from_source_passes():
    evaluate_safety_gates(**_valid_gate_kwargs(
        source_schema="warehouse", source_table="customer_master_source",
    ))  # must not raise


def test_missing_confirmation_rejected():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(confirm=False))
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_already_executed_live_is_a_conflict_not_a_422():
    """State conflicts (already executed, in-flight elsewhere) map to
    409 in app.py, distinct from validity failures (422). This now
    also covers a rolled-back ticket -- originally only COMPLETED
    blocked re-execution, letting a rolled-back ticket run again."""
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(already_executed_live=True))
        assert False
    except LiveExecutionConflictError:
        pass


def test_unresolved_execution_against_target_is_a_conflict():
    try:
        evaluate_safety_gates(**_valid_gate_kwargs(unresolved_execution_exists_for_target=True))
        assert False
    except LiveExecutionConflictError:
        pass


def test_missing_sandbox_fingerprint_rejected():
    try:
        verify_output_fingerprint_match(sandbox_fingerprint=None, live_fingerprint="anything")
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_fingerprint_mismatch_rejected():
    """The core of issue 8: if the live recomputation doesn't match
    what was actually approved in sandbox, refuse to publish --
    something changed since approval."""
    try:
        verify_output_fingerprint_match(sandbox_fingerprint="abc123", live_fingerprint="different456")
        assert False
    except LiveExecutionNotAllowedError:
        pass


def test_matching_fingerprint_passes():
    verify_output_fingerprint_match(sandbox_fingerprint="xyz789", live_fingerprint="xyz789")  # must not raise


def test_schema_allowlist_parsing():
    import os
    os.environ["AEGIS_LIVE_TARGET_SCHEMA_ALLOWLIST"] = " warehouse , reporting ,, "
    assert get_target_schema_allowlist() == ["warehouse", "reporting"]
    del os.environ["AEGIS_LIVE_TARGET_SCHEMA_ALLOWLIST"]
    assert get_target_schema_allowlist() == []


def test_operator_validation_strips_and_rejects_empty():
    from live_execution.safety import validate_and_normalize_operator

    assert validate_and_normalize_operator("  mo  ") == "mo"
    for bad in ["", "   ", None]:
        try:
            validate_and_normalize_operator(bad)
            assert False, f"expected rejection for {bad!r}"
        except LiveExecutionNotAllowedError:
            pass
