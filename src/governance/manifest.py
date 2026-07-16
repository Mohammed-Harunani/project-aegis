from dataclasses import dataclass, field
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

    # Phase 2.5 correction -- the exact corrected DataFrame this
    # execution produced, for the caller to fingerprint immediately.
    # Never persisted (manifest_repository.py doesn't reference this
    # field, so it's simply not written to the database -- ephemeral,
    # in-memory only). compare=False because DataFrame equality isn't
    # a plain bool (it's element-wise), which would break this
    # dataclass's auto-generated __eq__ the moment two manifests both
    # carrying a DataFrame here were ever compared. repr=False keeps
    # a full DataFrame dump out of str(manifest), which app.py already
    # embeds directly in API responses.
    corrected_dataset: Any = field(default=None, compare=False, repr=False)