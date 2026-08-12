from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple


_ALLOWED_CONVERSION_STATUSES = frozenset({"SAFE", "UNSAFE", "UNSUPPORTED", "ERROR"})


@dataclass(frozen=True)
class ConversionOutcomeMetadata:
    """
    Redacted, immutable summary of one verified CAST_COLUMN analysis.

    Phase 3.1.4 uses the same value object for the approval ticket's persisted
    preflight decision and the HealingManifest's persisted execution outcome.
    It never contains raw source values, row indexes, or free-form diagnostic
    messages. JSON conversion is explicit and version-stable.
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
        # Defensive normalization keeps the frozen model genuinely immutable
        # even if a caller supplies a mutable sequence.
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

        if self.status not in _ALLOWED_CONVERSION_STATUSES:
            raise ValueError(f"Invalid conversion status: {self.status!r}.")
        for field_name in (
            "column_name",
            "source_dtype",
            "target_dtype",
            "policy_version",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value:
                raise ValueError(f"{field_name} must be a non-empty string.")
        for field_name in (
            "total_count",
            "null_count",
            "converted_count",
            "failed_count",
            "diagnostic_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer.")
        if any(type(code) is not str or not code for code in self.reason_codes):
            raise ValueError("reason_codes must contain non-empty strings only.")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("reason_codes must not contain duplicates.")
        if self.diagnostic_count < len(self.reason_codes):
            raise ValueError(
                "diagnostic_count cannot be smaller than the unique reason-code count."
            )
        if self.status == "SAFE":
            if self.failed_count != 0 or self.diagnostic_count != 0 or self.reason_codes:
                raise ValueError(
                    "SAFE conversion metadata cannot contain failures or diagnostics."
                )
        elif self.diagnostic_count == 0:
            raise ValueError(
                f"{self.status} conversion metadata must contain at least one diagnostic."
            )

    @property
    def is_safe(self) -> bool:
        return self.status == "SAFE"

    def to_dict(self) -> Dict[str, Any]:
        """Return the only JSON shape permitted for persistence and APIs."""
        return {
            "column_name": self.column_name,
            "status": self.status,
            "source_dtype": self.source_dtype,
            "target_dtype": self.target_dtype,
            "policy_version": self.policy_version,
            "total_count": self.total_count,
            "null_count": self.null_count,
            "converted_count": self.converted_count,
            "failed_count": self.failed_count,
            "diagnostic_count": self.diagnostic_count,
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ConversionOutcomeMetadata":
        """Strictly reconstruct persisted metadata; reject unknown fields."""
        if not isinstance(value, Mapping):
            raise ValueError("Conversion metadata must be a mapping.")

        required = {
            "column_name",
            "status",
            "source_dtype",
            "target_dtype",
            "policy_version",
            "total_count",
            "null_count",
            "converted_count",
            "failed_count",
            "diagnostic_count",
            "reason_codes",
        }
        actual = set(value.keys())
        missing = required - actual
        unknown = actual - required
        if missing:
            raise ValueError(
                f"Conversion metadata is missing fields: {sorted(missing)}."
            )
        if unknown:
            raise ValueError(
                f"Conversion metadata contains unknown fields: {sorted(unknown)}."
            )

        reason_codes = value["reason_codes"]
        if not isinstance(reason_codes, (list, tuple)):
            raise ValueError("reason_codes must be a list or tuple.")

        return cls(
            column_name=value["column_name"],
            status=value["status"],
            source_dtype=value["source_dtype"],
            target_dtype=value["target_dtype"],
            policy_version=value["policy_version"],
            total_count=value["total_count"],
            null_count=value["null_count"],
            converted_count=value["converted_count"],
            failed_count=value["failed_count"],
            diagnostic_count=value["diagnostic_count"],
            reason_codes=tuple(reason_codes),
        )


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
    # Never persisted as a DataFrame. compare=False because DataFrame
    # equality is element-wise; repr=False prevents data leakage.
    corrected_dataset: Any = field(default=None, compare=False, repr=False)

    # Phase 3.1.4 -- redacted CAST_COLUMN execution outcome. This is now
    # persisted to healing_manifests.conversion_outcome. RENAME_COLUMN
    # manifests leave it None.
    conversion_outcome: Optional[ConversionOutcomeMetadata] = None
