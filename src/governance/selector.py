"""
Aegis_RepairSelector
Phase 2.1 -- Repair Plan Selection Engine

Replaces naive first-plan selection (`repair_plans[0]`) with a
governance-aware choice.

Selection rules (per roadmap):
1. Remove plans that would be QUARANTINE-bound under the current
   GovernancePolicy -- they are not viable candidates regardless of
   how they compare to the rest.
2. Among what's left, prefer non-destructive repairs over destructive
   ones, regardless of confidence.
3. Within the same safety tier, prefer higher confidence.
4. Return the top result, or None if nothing survives step 1.

Destructiveness is inferred from Consultant's `proposed_action`
string: any plan containing the WITH_DROP_INVALID marker drops rows
to force a cast and is treated as destructive. Everything else
(RENAME_COLUMN, plain CAST_COLUMN) is non-destructive.

NOTE: this is a string-based signal because Consultant (V1) encodes
the action as free text, not a typed enum/flag. If Consultant ever
grows repair types that are destructive without using this marker,
this check needs revisiting -- flagged in the project handoff notes.
"""

from typing import List, Optional

from src.consultant.consultant import RepairPlan
from src.governance.policy import GovernancePolicy


DESTRUCTIVE_MARKER = "WITH_DROP_INVALID"


class RepairSelector:
    """
    Aegis_RepairSelector
    Chooses the safest, highest-confidence viable repair plan
    from the Consultant's proposals.
    """

    def __init__(self, governance_policy: Optional[GovernancePolicy] = None):
        self.governance_policy = governance_policy or GovernancePolicy()

    def _is_destructive(self, plan: RepairPlan) -> bool:
        return DESTRUCTIVE_MARKER in plan.proposed_action

    def _passes_governance_floor(self, plan: RepairPlan) -> bool:
        # Anything the policy would quarantine is not a viable pick,
        # no matter how it compares to the other candidates.
        return self.governance_policy.evaluate(plan.confidence) != "QUARANTINE"

    def choose_best(self, repair_plans: List[RepairPlan]) -> Optional[RepairPlan]:
        viable = [p for p in repair_plans if self._passes_governance_floor(p)]

        if not viable:
            return None

        # False (non-destructive) sorts before True (destructive);
        # negative confidence gives descending order within each tier.
        viable.sort(key=lambda p: (self._is_destructive(p), -p.confidence))

        return viable[0]
