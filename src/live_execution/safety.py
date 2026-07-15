"""
Aegis_LiveExecutionSafetyPolicy
Phase 2.5 -- the 15 mandatory gates a repair must pass before it can
execute live. Deliberately takes plain primitives, not ORM/ticket
objects, so it's fully testable without a database at all -- the
caller (app.py) is responsible for extracting these values from the
real ticket/manifest records.

Two exception types map to two different HTTP statuses in app.py:
LiveExecutionConflictError (409) for state conflicts -- already
executed, another execution in flight against the same target.
LiveExecutionNotAllowedError (422) for everything else -- the ticket
or request doesn't meet a live-execution requirement.
"""

import os
from typing import List, Optional

from src.live_execution.identifiers import validate_identifier, InvalidIdentifierError


class LiveExecutionNotAllowedError(Exception):
    """422 -- the ticket/request doesn't meet a live-execution safety requirement."""


class LiveExecutionConflictError(Exception):
    """409 -- conflicts with existing state."""


def live_execution_globally_enabled() -> bool:
    """
    The environment kill switch, off by default. Checked separately
    from (and before) the policy gates below -- this is a
    configuration/authorization question, not a request-validity one,
    so app.py maps it to 403, not 422.
    """
    return os.environ.get("AEGIS_LIVE_EXECUTION_ENABLED", "false").strip().lower() == "true"


def get_target_schema_allowlist() -> List[str]:
    raw = os.environ.get("AEGIS_LIVE_TARGET_SCHEMA_ALLOWLIST", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def evaluate_safety_gates(
    *,
    schema_version_id: Optional[str],
    sandbox_manifest_applied: bool,
    sandbox_risk_level: str,
    sandbox_integrity_status: str,
    original_row_count: int,
    final_row_count: int,
    proposed_action: str,
    target_schema: str,
    target_table: str,
    source_schema: Optional[str],
    source_table: Optional[str],
    confirm: bool,
    schema_allowlist: List[str],
    already_executed_live: bool,
    unresolved_execution_exists_for_target: bool,
) -> None:
    """
    Raises on the first gate that fails. Returns None (silently) if
    every gate passes. Order matters only for which error message the
    caller sees first -- every gate is independently necessary.
    """
    # Gates 1-2: registry-backed lineage, not a legacy direct request.
    if not schema_version_id:
        raise LiveExecutionNotAllowedError(
            "Ticket has no schema_version_id -- live execution requires a "
            "Schema Registry-backed migration, not a legacy direct "
            "gold_schema request."
        )

    # Gate 3: a successful sandbox execution already happened.
    if not sandbox_manifest_applied:
        raise LiveExecutionNotAllowedError(
            "No successful sandbox Healing Manifest exists for this ticket."
        )

    # Gate 4: risk tier.
    if sandbox_risk_level != "LOW_RISK":
        raise LiveExecutionNotAllowedError(
            f"Sandbox risk level must be LOW_RISK for live execution, got {sandbox_risk_level!r}."
        )

    # Gate 5: integrity status.
    if sandbox_integrity_status != "NO_VOLUME_CHANGE":
        raise LiveExecutionNotAllowedError(
            f"Sandbox integrity status must be NO_VOLUME_CHANGE, got {sandbox_integrity_status!r}."
        )

    # Gate 6: row counts.
    if original_row_count != final_row_count:
        raise LiveExecutionNotAllowedError(
            f"Original and final row counts must match for live execution "
            f"({original_row_count} vs {final_row_count})."
        )

    # Gates 7-8: non-destructive only.
    if "WITH_DROP_INVALID" in proposed_action:
        raise LiveExecutionNotAllowedError(
            "Destructive repairs (WITH_DROP_INVALID) are prohibited for live execution."
        )

    # Gates 9-10: allowlisted schema, explicit table (Pydantic already
    # makes target_schema/target_table required -- this re-validates
    # the identifiers themselves are safe to put in DDL).
    try:
        validate_identifier(target_schema, "target schema")
        validate_identifier(target_table, "target table")
    except InvalidIdentifierError as e:
        raise LiveExecutionNotAllowedError(str(e))

    if target_schema not in schema_allowlist:
        raise LiveExecutionNotAllowedError(
            f"Target schema {target_schema!r} is not in the allowlisted set: {schema_allowlist}."
        )

    # Gate 11: target != source, when a source is actually given (see
    # spec: nothing in Aegis today tracks a live source-table
    # reference to compare against otherwise).
    if source_schema is not None and source_table is not None:
        if (source_schema, source_table) == (target_schema, target_table):
            raise LiveExecutionNotAllowedError(
                "Target must not be the same as the original source table."
            )

    # Gate 14: explicit confirmation (kept in gate order 14 per the
    # spec, though checked here rather than after 12-13 -- ordering
    # among independent gates doesn't change which ones are enforced).
    if not confirm:
        raise LiveExecutionNotAllowedError(
            "Live execution requires explicit confirmation (confirm=true)."
        )

    # Gates 12-13: state conflicts -- 409, not 422.
    if already_executed_live:
        raise LiveExecutionConflictError("This ticket has already been executed live.")

    if unresolved_execution_exists_for_target:
        raise LiveExecutionConflictError(
            f"An unresolved (PENDING/RUNNING) live execution already exists "
            f"for {target_schema}.{target_table}."
        )

    # Gate 15 (PostgreSQL validation before promotion) happens inside
    # the writer during the actual DDL transaction, not here.
