"""
Aegis_ColumnOrder
Phase 3.3.2 -- pure, database-independent action and eligibility
model for verified column-order repair.

The package deliberately imports no pandas, FastAPI, SQLAlchemy,
repositories, or database connectors. It reasons only about schema
metadata and canonical action text; execution belongs to later
Phase 3.3 stages.
"""

from .models import (
    ColumnOrderEligibility,
    ColumnOrderReason,
    ParsedColumnOrderAction,
)
from .policy import (
    COLUMN_ORDER_ACTION_PREFIX,
    COLUMN_ORDER_CONFIDENCE,
    InvalidColumnOrderActionError,
    analyze_reorder_only_eligibility,
    is_column_order_action,
    parse_column_order_action,
    serialize_column_order_action,
)

__all__ = [
    "COLUMN_ORDER_ACTION_PREFIX",
    "COLUMN_ORDER_CONFIDENCE",
    "ColumnOrderEligibility",
    "ColumnOrderReason",
    "InvalidColumnOrderActionError",
    "ParsedColumnOrderAction",
    "analyze_reorder_only_eligibility",
    "is_column_order_action",
    "parse_column_order_action",
    "serialize_column_order_action",
]
