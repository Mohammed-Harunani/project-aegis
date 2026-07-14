from dataclasses import dataclass
from typing import Literal


Decision = Literal["AUTO_APPROVE", "REQUIRES_HUMAN_APPROVAL", "QUARANTINE"]


@dataclass
class GovernancePolicy:
    """
    Governance decision engine for Aegis.

    Determines whether a repair plan:
    - Can be auto-approved
    - Requires human approval
    - Must be quarantined
    """

    auto_approve_threshold: float = 0.92
    approval_threshold: float = 0.80

    def evaluate(self, confidence: float) -> Decision:
        """
        Evaluate confidence score and return governance decision.
        """

        if confidence >= self.auto_approve_threshold:
            return "AUTO_APPROVE"

        if confidence >= self.approval_threshold:
            return "REQUIRES_HUMAN_APPROVAL"

        return "QUARANTINE"