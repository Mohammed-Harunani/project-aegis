"""
Aegis_LiveExecutionSafetyPolicy
Phase 2.5 final architecture -- the mandatory gates a repair must pass
before it can execute live. Deliberately takes plain primitives, not
ORM/ticket objects, so it's fully testable without a database at all
-- the caller (app.py) is responsible for extracting these values
from the real ticket/manifest records.

Two exception types map to two different HTTP statuses in app.py:
LiveExecutionConflictError (409) for state conflicts -- already
executed, another execution in flight against the same target.
LiveExecutionNotAllowedError (422) for everything else -- the ticket
or request doesn't meet a live-execution requirement.

The target-schema-allowlist gate from the rename-based design is gone
entirely -- publication schemas (aegis_publish / aegis_publish_data)
are fixed, not caller-configurable, so there's nothing left to
allowlist. In its place: live_eligible, the gate that actually closes
the gap the whole redesign was for -- a sample_data ticket
(/simulate-migration) is permanently ineligible for live execution,
enforced here rather than only at approval time.
"""

import os
from typing import Optional

from src.live_execution.identifiers import validate_identifier, InvalidIdentifierError
from src.inspector import AegisInspector
from src.live_execution.logical_dtype import build_corrected_observed_schema


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


def validate_and_normalize_operator(operator: str) -> str:
    stripped = (operator or "").strip()
    if not stripped:
        raise LiveExecutionNotAllowedError("operator must not be empty or whitespace-only.")
    return stripped


def verify_output_fingerprint_match(sandbox_fingerprint: Optional[str], live_fingerprint: str) -> None:
    """
    Separate from evaluate_safety_gates() on purpose: this can only be
    checked AFTER Surgeon actually recomputes the correction, which
    must happen only after a durable RUNNING execution record already
    exists -- otherwise a Surgeon failure has nothing to mark FAILED
    and the record is stuck. So this runs inside that same protected
    block, not as part of the earlier pre-flight gate evaluation.
    """
    if not sandbox_fingerprint:
        raise LiveExecutionNotAllowedError(
            "Sandbox manifest has no recorded output fingerprint to compare "
            "against -- cannot verify the live recomputation matches what was "
            "actually approved."
        )
    if sandbox_fingerprint != live_fingerprint:
        raise LiveExecutionNotAllowedError(
            "The live recomputation does not match the sandbox output that was "
            "actually approved -- something has changed since approval (code, "
            "component versions, or repair behavior). Refusing to publish."
        )


def verify_complete_schema_match(corrected_dataframe, gold_schema) -> None:
    """
    Surgeon's own validation only checks
    `list(working_df.columns) == list(gold_schema.column_order)` --
    column names and order, nothing else. That can pass even when a
    SECOND column still has the wrong type, because Consultant may
    have proposed repairs for multiple problems but RepairSelector
    only ever chooses one. A rename could succeed, leave a type
    mismatch elsewhere untouched, and still show matching column
    order -- Surgeon would report success, and nothing before this
    point would catch the remaining problem. Live publication requires
    the corrected schema to have a COMPLETELY empty delta against
    Gold: no missing columns, no new columns, no type mismatches, no
    reorder. Multi-repair tickets are not resolved automatically by
    this system -- they are refused for live execution, not silently
    published half-fixed.

    Uses build_corrected_observed_schema(), not plain
    AegisInspector.generate_observed_schema() -- the latter reads the
    raw pandas dtype label, which is always "object" for anything
    Decimal/date/UUID/JSON-holding. Gold schemas for a trusted-source
    ticket may declare logical dtypes like "decimal" or "datetime_tz"
    that never appear as real pandas dtype strings; comparing against
    the raw label would wrongly flag every such column as unresolved
    even when nothing is actually wrong.
    """
    observed = build_corrected_observed_schema(corrected_dataframe)
    delta = AegisInspector().detect_delta(observed, gold_schema)
    if delta.missing_columns or delta.new_columns or delta.type_mismatches or delta.reorder_event:
        raise LiveExecutionNotAllowedError(
            f"The corrected dataset does not fully match the Gold schema after "
            f"repair -- missing_columns={delta.missing_columns}, "
            f"new_columns={delta.new_columns}, "
            f"type_mismatches={delta.type_mismatches}, "
            f"reorder_event={delta.reorder_event}. Live publication requires a "
            f"completely resolved schema; a single applied repair leaving another "
            f"needed repair unresolved must not be published."
        )


def evaluate_safety_gates(
    *,
    schema_version_id: Optional[str],
    sandbox_manifest_applied: bool,
    sandbox_risk_level: str,
    sandbox_integrity_status: str,
    original_row_count: int,
    final_row_count: int,
    proposed_action: str,
    logical_target: str,
    live_eligible: bool,
    source_schema: Optional[str],
    source_table: Optional[str],
    confirm: bool,
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

    # Gate 9: the logical target name is safe to put in DDL. There is
    # no schema allowlist anymore -- aegis_publish/aegis_publish_data
    # are fixed, not caller-configurable, so there's nothing left to
    # allowlist against.
    try:
        validate_identifier(logical_target, "logical target")
    except InvalidIdentifierError as e:
        raise LiveExecutionNotAllowedError(str(e))

    # Gate 10 (final architecture): this is THE gate that actually
    # closes the sample_data gap -- a ticket from /simulate-migration
    # is permanently ineligible for live execution, no matter its
    # approval status. Enforced here (execute-live time), not just at
    # approval, since approval alone doesn't prevent someone from
    # attempting execute-live afterward.
    if not live_eligible:
        raise LiveExecutionNotAllowedError(
            "This ticket is not live-eligible -- it was created via "
            "/simulate-migration (sample_data), which can never execute live. "
            "Live execution requires /simulate-migration-from-source, which "
            "reads a complete, trusted dataset with verifiable provenance."
        )

    # Gate 11: the source table must not itself be inside Aegis's own
    # publication schemas -- a defensive check against a degenerate
    # self-referential loop. Structurally much less likely to collide
    # now than under the old design (source is a real, DB-verified
    # table; target is a fixed-schema logical name), but cheap to
    # check and worth keeping.
    if source_schema in ("aegis_publish", "aegis_publish_data"):
        raise LiveExecutionNotAllowedError(
            f"Source schema {source_schema!r} must not be one of Aegis's own "
            f"publication schemas."
        )

    # Gate 14: explicit confirmation.
    if not confirm:
        raise LiveExecutionNotAllowedError(
            "Live execution requires explicit confirmation (confirm=true)."
        )

    # Gates 12-13: state conflicts -- 409, not 422.
    if already_executed_live:
        raise LiveExecutionConflictError(
            "This ticket has already executed live (or been rolled back after "
            "doing so) -- live execution is one-shot per ticket."
        )

    if unresolved_execution_exists_for_target:
        raise LiveExecutionConflictError(
            f"An unresolved (PENDING/RUNNING/ROLLING_BACK) live execution already "
            f"exists for logical target {logical_target!r}."
        )

    # Gate 15 (PostgreSQL validation before publication) happens
    # inside the writer during the actual DDL transaction. The
    # sandbox/live output-fingerprint match and the source-unchanged
    # check are verified separately -- both need an actual read
    # (Surgeon recomputation, source re-read) that can't happen inside
    # this pure, DB-free gate evaluation.
