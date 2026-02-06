from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class HealingManifest:
    """
    HealingManifest (V1)
    Immutable audit record for a repair execution attempt.
    """

    timestamp: str
    repair_plan: str
    execution_result: str
    validation_result: str

    inspector_version: str
    consultant_version: str
    surgeon_version: str

    execution_mode: str  # sandbox | commit
    operator: Optional[str]
