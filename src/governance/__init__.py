from .policy import GovernancePolicy
from .selector import RepairSelector
from .manifest import ConversionOutcomeMetadata, HealingManifest
from .conversion_safety import (
    ConversionApprovalBlockedError,
    ConversionGovernanceError,
    InvalidCastActionError,
    ParsedCastAction,
    StaleConversionDecisionError,
    analyze_cast_plan,
    build_conversion_metadata,
    is_cast_action,
    parse_cast_action,
    require_safe_conversion_decision,
)
from .approval import (
    ApprovalQueue,
    ApprovalTicket,
    TicketNotFoundError,
    TicketNotPendingError,
)

__all__ = [
    "GovernancePolicy",
    "RepairSelector",
    "HealingManifest",
    "ConversionOutcomeMetadata",
    "ConversionGovernanceError",
    "ConversionApprovalBlockedError",
    "InvalidCastActionError",
    "StaleConversionDecisionError",
    "ParsedCastAction",
    "is_cast_action",
    "parse_cast_action",
    "build_conversion_metadata",
    "analyze_cast_plan",
    "require_safe_conversion_decision",
    "ApprovalQueue",
    "ApprovalTicket",
    "TicketNotFoundError",
    "TicketNotPendingError",
]

# PostgresApprovalRepository and save_manifest are intentionally NOT
# re-exported here. Doing so would make `governance/__init__.py` --
# and therefore every plain `from governance.x import y`, including
# ones that have nothing to do with persistence -- import sqlalchemy
# transitively. Import them directly where needed instead:
#   from src.governance.approval_repository import PostgresApprovalRepository
#   from src.governance.manifest_repository import save_manifest
