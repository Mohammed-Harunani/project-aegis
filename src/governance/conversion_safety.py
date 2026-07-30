"""
Phase 3.1.4 governance integration for verified CAST_COLUMN decisions.

This module sits between the pure type-repair engine and approval/API
layers. It deliberately stores and compares only redacted summary metadata:
no raw source values, row labels, or free-form diagnostic messages cross the
governance boundary.
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from src.consultant.consultant import RepairPlan
from src.governance.manifest import ConversionOutcomeMetadata
from src.type_repair import ColumnConversionResult, ConversionStatus, analyze_and_convert


class ConversionGovernanceError(Exception):
    """Base class for deterministic CAST_COLUMN governance failures."""


class InvalidCastActionError(ConversionGovernanceError):
    """Raised when a CAST_COLUMN action cannot be parsed safely."""


class ConversionApprovalBlockedError(ConversionGovernanceError):
    """Raised when a conversion decision does not permit approval."""


class StaleConversionDecisionError(ConversionGovernanceError):
    """Raised when a persisted decision no longer matches a fresh analysis."""


@dataclass(frozen=True)
class ParsedCastAction:
    column: str
    target_dtype: str
    drop_invalid_requested: bool


def is_cast_action(action: object) -> bool:
    return type(action) is str and action.startswith("CAST_COLUMN")


def parse_cast_action(action: object) -> ParsedCastAction:
    """Parse the V1 free-text CAST_COLUMN action deterministically."""
    if type(action) is not str:
        raise InvalidCastActionError("CAST_COLUMN action must be a string.")

    parts = action.split()
    if len(parts) not in (4, 5):
        raise InvalidCastActionError("Invalid CAST_COLUMN action shape.")
    if parts[0] != "CAST_COLUMN" or parts[2] != "TO":
        raise InvalidCastActionError("Invalid CAST_COLUMN action syntax.")
    if len(parts) == 5 and parts[4] != "WITH_DROP_INVALID":
        raise InvalidCastActionError("Invalid CAST_COLUMN action suffix.")

    column = parts[1]
    target_dtype = parts[3]
    if not column or not target_dtype:
        raise InvalidCastActionError("CAST_COLUMN requires a column and target dtype.")

    return ParsedCastAction(
        column=column,
        target_dtype=target_dtype,
        drop_invalid_requested=len(parts) == 5,
    )


def build_conversion_metadata(
    column: str,
    result: ColumnConversionResult,
) -> ConversionOutcomeMetadata:
    """Build the immutable, redacted persistence/API representation."""
    return ConversionOutcomeMetadata(
        column_name=column,
        status=result.status.value,
        source_dtype=result.source_dtype,
        target_dtype=result.target_dtype,
        policy_version=result.policy_version,
        total_count=result.total_count,
        null_count=result.null_count,
        converted_count=result.converted_count,
        failed_count=result.failed_count,
        diagnostic_count=len(result.diagnostics),
        reason_codes=tuple(
            dict.fromkeys(
                diagnostic.reason_code.value
                for diagnostic in result.diagnostics
            )
        ),
    )


def analyze_cast_plan(
    repair_plan: RepairPlan,
    target_dataset: pd.DataFrame,
) -> ConversionOutcomeMetadata:
    """
    Analyze a CAST_COLUMN plan without mutating the supplied dataset.

    WITH_DROP_INVALID is rejected before engine invocation because row dropping
    is outside the verified conversion contract. Missing columns and malformed
    actions are governance errors rather than fabricated conversion outcomes.
    """
    if type(target_dataset) is not pd.DataFrame:
        raise ConversionGovernanceError("Target dataset must be a pandas DataFrame.")

    parsed = parse_cast_action(repair_plan.proposed_action)
    if parsed.drop_invalid_requested:
        raise ConversionApprovalBlockedError(
            "WITH_DROP_INVALID cannot be submitted for approval; row dropping is forbidden."
        )
    if parsed.column not in target_dataset.columns:
        raise ConversionGovernanceError(
            f"CAST_COLUMN source column is missing: {parsed.column}."
        )

    result = analyze_and_convert(
        target_dataset[parsed.column],
        parsed.target_dtype,
    )
    return build_conversion_metadata(parsed.column, result)


def require_safe_conversion_decision(
    repair_plan: RepairPlan,
    target_dataset: pd.DataFrame,
    persisted_decision: Optional[ConversionOutcomeMetadata],
) -> Optional[ConversionOutcomeMetadata]:
    """
    Enforce approval plus conversion safety.

    Non-cast plans must not carry conversion metadata. CAST_COLUMN plans must
    carry a persisted SAFE decision, and a fresh deterministic analysis of the
    persisted dataset snapshot must exactly match that decision. Human approval
    therefore cannot override UNSAFE, UNSUPPORTED, ERROR, destructive, missing,
    malformed, or stale conversion states.
    """
    action = repair_plan.proposed_action
    if not is_cast_action(action):
        if persisted_decision is not None:
            raise ConversionApprovalBlockedError(
                "Non-CAST repair plan unexpectedly carries conversion metadata."
            )
        return None

    if persisted_decision is None:
        raise ConversionApprovalBlockedError(
            "CAST_COLUMN ticket has no persisted conversion decision."
        )
    if persisted_decision.status != ConversionStatus.SAFE.value:
        raise ConversionApprovalBlockedError(
            f"CAST_COLUMN decision is {persisted_decision.status}, not SAFE."
        )

    fresh_decision = analyze_cast_plan(repair_plan, target_dataset)
    if fresh_decision != persisted_decision:
        raise StaleConversionDecisionError(
            "Persisted conversion decision does not match fresh deterministic analysis."
        )
    if fresh_decision.status != ConversionStatus.SAFE.value:
        # Defensive duplication: equality above should make this unreachable,
        # but the safety invariant is important enough to state explicitly.
        raise ConversionApprovalBlockedError(
            f"Fresh CAST_COLUMN decision is {fresh_decision.status}, not SAFE."
        )

    return fresh_decision
