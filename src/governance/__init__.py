from .policy import GovernancePolicy
from .selector import RepairSelector
from .manifest import ConversionOutcomeMetadata, HealingManifest
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
