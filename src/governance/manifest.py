from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class ConversionOutcomeMetadata:
    """
    Redacted, immutable summary of one verified CAST_COLUMN analysis.

    Phase 3.1.3 records this on the in-memory HealingManifest so sandbox
    callers can audit exactly why a cast was or was not applied. It does
    not contain raw source values or row indexes. Persistence of this
    metadata is intentionally deferred to Phase 3.1.4, where the database
    schema and API exposure will be reviewed together.
    """

    column_name: str
    status: str
    source_dtype: str
    target_dtype: str
    policy_version: str
    total_count: int
    null_count: int
    converted_count: int
    failed_count: int
    diagnostic_count: int
    reason_codes: Tuple[str, ...]

    def __post_init__(self):
        # Defensive normalization keeps the frozen model genuinely
        # immutable even if a caller supplies a mutable sequence.
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))


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

    # Phase 3.1.3 -- redacted sandbox CAST_COLUMN outcome metadata.
    # This remains in-memory only until Phase 3.1.4 formally extends
    # persistence and API contracts. RENAME_COLUMN manifests leave it None.
    conversion_outcome: Optional[ConversionOutcomeMetadata] = None
