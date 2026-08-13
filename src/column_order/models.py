"""
Aegis_ColumnOrderModels
Phase 3.3.2 -- immutable models for canonical order actions and the
reorder-only eligibility proof.

Only schema column names and stable reason codes are represented here.
No dataset values enter these models.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Tuple


class ColumnOrderReason(str, Enum):
    """Stable outcomes for the reorder-only eligibility proof."""

    ELIGIBLE = "ELIGIBLE"
    INVALID_DELTA = "INVALID_DELTA"
    NOT_REORDER_EVENT = "NOT_REORDER_EVENT"
    MISSING_COLUMNS = "MISSING_COLUMNS"
    NEW_COLUMNS = "NEW_COLUMNS"
    TYPE_MISMATCHES = "TYPE_MISMATCHES"
    INVALID_OBSERVED_ORDER = "INVALID_OBSERVED_ORDER"
    INVALID_GOLD_ORDER = "INVALID_GOLD_ORDER"
    DUPLICATE_OBSERVED_COLUMN = "DUPLICATE_OBSERVED_COLUMN"
    DUPLICATE_GOLD_COLUMN = "DUPLICATE_GOLD_COLUMN"
    COLUMN_COUNT_MISMATCH = "COLUMN_COUNT_MISMATCH"
    COLUMN_MEMBERSHIP_MISMATCH = "COLUMN_MEMBERSHIP_MISMATCH"
    OBSERVED_SCHEMA_INCONSISTENT = "OBSERVED_SCHEMA_INCONSISTENT"
    GOLD_SCHEMA_INCONSISTENT = "GOLD_SCHEMA_INCONSISTENT"
    DTYPE_MISMATCH = "DTYPE_MISMATCH"
    ALREADY_ORDERED = "ALREADY_ORDERED"


@dataclass(frozen=True)
class ParsedColumnOrderAction:
    """A fully parsed, immutable target-order action."""

    target_order: Tuple[str, ...]

    def __post_init__(self):
        target_order = tuple(self.target_order)
        object.__setattr__(self, "target_order", target_order)

        if not target_order:
            raise ValueError("A column-order action requires a non-empty target order.")
        if any(type(name) is not str for name in target_order):
            raise ValueError(
                "A column-order action requires exact built-in string names."
            )
        if len(set(target_order)) != len(target_order):
            raise ValueError("A column-order action cannot contain duplicate names.")


@dataclass(frozen=True)
class ColumnOrderEligibility:
    """Deterministic result of independently proving reorder-only drift."""

    eligible: bool
    reason_code: ColumnOrderReason
    observed_order: Tuple[str, ...] = ()
    gold_order: Tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "observed_order", tuple(self.observed_order))
        object.__setattr__(self, "gold_order", tuple(self.gold_order))

        if type(self.eligible) is not bool:
            raise ValueError("eligible must be an exact built-in bool.")
        if not isinstance(self.reason_code, ColumnOrderReason):
            raise ValueError("reason_code must be a ColumnOrderReason.")
        if self.eligible and self.reason_code is not ColumnOrderReason.ELIGIBLE:
            raise ValueError("An eligible result must use reason code ELIGIBLE.")
        if not self.eligible and self.reason_code is ColumnOrderReason.ELIGIBLE:
            raise ValueError("An ineligible result cannot use reason code ELIGIBLE.")
