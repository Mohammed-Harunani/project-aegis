"""
Phase 3.3.4 governance validation for verified column-order repair.

The approval boundary independently proves that a REORDER_COLUMNS ticket is
canonical, reorder-only, bound to its persisted Gold order, and replayable
from a dataset with the exact required unique column membership.  Only
structural metadata crosses this boundary; row values are never inspected or
reported.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

import pandas as pd

from src.column_order import (
    InvalidColumnOrderActionError,
    analyze_reorder_only_eligibility,
    parse_column_order_action,
    serialize_column_order_action,
)
from src.consultant.consultant import RepairPlan
from src.governance.manifest import ConversionOutcomeMetadata
from src.inspector import SchemaDelta


class ColumnOrderGovernanceError(Exception):
    """Base class for deterministic REORDER_COLUMNS governance failures."""


class InvalidColumnOrderEvidenceError(ColumnOrderGovernanceError):
    """Persisted or submitted evidence does not prove a safe reorder."""


class ColumnOrderApprovalBlockedError(ColumnOrderGovernanceError):
    """Approval cannot proceed under the verified order contract."""


@dataclass(frozen=True)
class ColumnOrderGovernanceEvidence:
    """Redacted structural evidence derived from a validated ticket."""

    canonical_action: str
    observed_order: Tuple[str, ...]
    gold_order: Tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "canonical_action": self.canonical_action,
            "observed_order": list(self.observed_order),
            "gold_order": list(self.gold_order),
            "column_count": len(self.gold_order),
            "reorder_only": True,
        }


def _is_reorder_candidate(action: object) -> bool:
    return type(action) is str and action.startswith("REORDER_COLUMNS")


def _derive_delta(observed_schema, gold_schema) -> SchemaDelta:
    """Derive the complete structural delta without trusting stored claims."""

    try:
        observed_order = observed_schema.column_order
        gold_order = gold_schema.column_order
        observed_columns = observed_schema.columns
        gold_columns = gold_schema.columns
    except (AttributeError, TypeError) as exc:
        raise InvalidColumnOrderEvidenceError(
            "Column-order ticket contains invalid schema evidence."
        ) from exc

    if (
        type(observed_order) not in (list, tuple)
        or type(gold_order) not in (list, tuple)
        or type(observed_columns) is not dict
        or type(gold_columns) is not dict
    ):
        raise InvalidColumnOrderEvidenceError(
            "Column-order ticket contains invalid schema evidence."
        )

    # Let the pure eligibility policy assign the stable duplicate/invalid
    # order reason where possible.  A neutral delta avoids unsafe set work.
    if (
        any(type(name) is not str for name in observed_order)
        or any(type(name) is not str for name in gold_order)
        or len(set(observed_order)) != len(observed_order)
        or len(set(gold_order)) != len(gold_order)
    ):
        return SchemaDelta([], [], {}, True)

    observed_names = set(observed_order)
    gold_names = set(gold_order)
    type_mismatches = {}
    try:
        for name in observed_names.intersection(gold_names):
            observed_dtype = observed_columns[name].dtype
            gold_dtype = gold_columns[name].dtype
            if observed_dtype != gold_dtype:
                type_mismatches[name] = {
                    "observed": observed_dtype,
                    "gold": gold_dtype,
                }
    except Exception as exc:
        raise InvalidColumnOrderEvidenceError(
            "Column-order ticket contains inconsistent schema metadata."
        ) from exc

    return SchemaDelta(
        missing_columns=sorted(gold_names - observed_names),
        new_columns=sorted(observed_names - gold_names),
        type_mismatches=type_mismatches,
        reorder_event=tuple(observed_order) != tuple(gold_order),
    )


def require_valid_column_order_evidence(
    repair_plan: RepairPlan,
    observed_schema,
    gold_schema,
    target_dataset: pd.DataFrame,
    conversion_decision: Optional[ConversionOutcomeMetadata],
) -> Optional[ColumnOrderGovernanceEvidence]:
    """
    Validate one submitted or persisted ticket's reorder evidence.

    Non-order plans pass through unchanged.  Anything beginning with the
    reserved REORDER_COLUMNS token is treated as an order candidate so a
    malformed prefix or payload cannot fall back to legacy governance.
    """

    try:
        action = repair_plan.proposed_action
    except AttributeError as exc:
        raise InvalidColumnOrderEvidenceError(
            "Repair plan does not contain an action."
        ) from exc

    if not _is_reorder_candidate(action):
        return None

    try:
        parsed = parse_column_order_action(action)
    except InvalidColumnOrderActionError as exc:
        raise InvalidColumnOrderEvidenceError(
            "REORDER_COLUMNS action is malformed."
        ) from exc

    canonical_action = serialize_column_order_action(parsed.target_order)
    if action != canonical_action:
        raise InvalidColumnOrderEvidenceError(
            "REORDER_COLUMNS action is not canonical."
        )
    if conversion_decision is not None:
        raise ColumnOrderApprovalBlockedError(
            "REORDER_COLUMNS ticket cannot carry conversion metadata."
        )

    eligibility = analyze_reorder_only_eligibility(
        _derive_delta(observed_schema, gold_schema),
        observed_schema,
        gold_schema,
    )
    if not eligibility.eligible:
        raise InvalidColumnOrderEvidenceError(
            "Ticket does not prove reorder-only drift: "
            f"{eligibility.reason_code.value}."
        )
    if parsed.target_order != eligibility.gold_order:
        raise InvalidColumnOrderEvidenceError(
            "REORDER_COLUMNS target does not match the persisted Gold order."
        )
    if type(target_dataset) is not pd.DataFrame:
        raise InvalidColumnOrderEvidenceError(
            "Column-order replay payload must be a pandas DataFrame."
        )

    replay_order = tuple(target_dataset.columns)
    if (
        any(type(name) is not str for name in replay_order)
        or len(set(replay_order)) != len(replay_order)
    ):
        raise InvalidColumnOrderEvidenceError(
            "Column-order replay payload has invalid or duplicate columns."
        )
    if replay_order != eligibility.observed_order:
        raise InvalidColumnOrderEvidenceError(
            "Column-order replay payload does not match the persisted observed order."
        )
    if (
        len(replay_order) != len(parsed.target_order)
        or set(replay_order) != set(parsed.target_order)
    ):
        raise InvalidColumnOrderEvidenceError(
            "Column-order replay payload does not contain the required membership."
        )

    return ColumnOrderGovernanceEvidence(
        canonical_action=canonical_action,
        observed_order=eligibility.observed_order,
        gold_order=eligibility.gold_order,
    )
