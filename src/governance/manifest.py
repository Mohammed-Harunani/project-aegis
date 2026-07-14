from dataclasses import dataclass
from typing import Dict, Any


@dataclass(frozen=True)
class HealingManifest:
    """
    Immutable audit record of a healing execution.
    """

    timestamp: str
    repair_plan: Any
    execution_result: Any
    execution_mode: str
    operator: str
    component_versions: Dict[str, str]

    # Phase 4 – Integrity
    original_row_count: int
    final_row_count: int
    integrity_status: str

    # Phase 5 – Risk Layer
    risk_level: str