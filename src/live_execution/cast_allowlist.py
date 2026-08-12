"""Explicit, fail-closed allowlist for Phase 3.1.6 live CAST_COLUMN pairs.

The allowlist is configuration, not approval. Human approval and the verified
conversion decision remain mandatory. The configured pair only determines
whether a previously verified source logical dtype may be published as the
requested target dtype through the live execution path.
"""

from dataclasses import dataclass
import os
from typing import FrozenSet, Iterable, Optional

from src.governance.conversion_safety import parse_cast_action


LIVE_CAST_ALLOWLIST_ENV = "AEGIS_LIVE_CAST_ALLOWLIST"
SUPPORTED_LIVE_CAST_DTYPES = frozenset({"object", "int64", "float64", "bool"})


class LiveCastAllowlistError(ValueError):
    """Raised when live CAST_COLUMN configuration or pair resolution is invalid."""


class LiveCastPairNotAllowedError(LiveCastAllowlistError):
    """Raised when a valid CAST_COLUMN pair is absent from the allowlist."""


@dataclass(frozen=True, order=True)
class LiveCastPair:
    source_dtype: str
    target_dtype: str

    def __post_init__(self) -> None:
        source = _normalize_dtype(self.source_dtype, "source")
        target = _normalize_dtype(self.target_dtype, "target")
        object.__setattr__(self, "source_dtype", source)
        object.__setattr__(self, "target_dtype", target)

    @property
    def token(self) -> str:
        return f"{self.source_dtype}->{self.target_dtype}"


def _normalize_dtype(value: object, role: str) -> str:
    if type(value) is not str or not value.strip():
        raise LiveCastAllowlistError(
            f"Live CAST_COLUMN {role} dtype must be a non-empty string."
        )
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_LIVE_CAST_DTYPES:
        supported = ", ".join(sorted(SUPPORTED_LIVE_CAST_DTYPES))
        raise LiveCastAllowlistError(
            f"Unsupported live CAST_COLUMN {role} dtype {value!r}; "
            f"supported values: {supported}."
        )
    return normalized


def parse_live_cast_allowlist(raw_value: Optional[str]) -> FrozenSet[LiveCastPair]:
    """Parse the strict comma-separated ``source->target`` configuration.

    Unset or whitespace-only configuration is an intentionally empty allowlist.
    Empty entries, malformed arrows, whitespace inside tokens, and duplicate
    pairs are rejected rather than silently normalized.
    """
    if raw_value is None or not raw_value.strip():
        return frozenset()

    pairs = []
    seen = set()
    for raw_token in raw_value.split(","):
        token = raw_token.strip()
        if not token:
            raise LiveCastAllowlistError(
                "AEGIS_LIVE_CAST_ALLOWLIST contains an empty pair."
            )
        if token.count("->") != 1:
            raise LiveCastAllowlistError(
                f"Invalid live CAST_COLUMN pair {token!r}; expected source->target."
            )
        source, target = token.split("->", 1)
        if source != source.strip() or target != target.strip():
            raise LiveCastAllowlistError(
                f"Invalid live CAST_COLUMN pair {token!r}; whitespace around dtypes is forbidden."
            )
        pair = LiveCastPair(source, target)
        if pair in seen:
            raise LiveCastAllowlistError(
                f"Duplicate live CAST_COLUMN pair {pair.token!r}."
            )
        seen.add(pair)
        pairs.append(pair)

    return frozenset(pairs)


def configured_live_cast_allowlist() -> FrozenSet[LiveCastPair]:
    return parse_live_cast_allowlist(os.environ.get(LIVE_CAST_ALLOWLIST_ENV))


def resolve_live_cast_pair(repair_plan, observed_schema) -> LiveCastPair:
    """Resolve the pair from trusted logical schema metadata, never pandas dtype."""
    parsed = parse_cast_action(repair_plan.proposed_action)
    if parsed.drop_invalid_requested:
        raise LiveCastAllowlistError(
            "WITH_DROP_INVALID cannot be live-enabled; row dropping is forbidden."
        )

    columns = getattr(observed_schema, "columns", None)
    if not isinstance(columns, dict) or parsed.column not in columns:
        raise LiveCastAllowlistError(
            f"Cannot resolve live CAST_COLUMN source dtype for {parsed.column!r}."
        )

    source_dtype = getattr(columns[parsed.column], "dtype", None)
    return LiveCastPair(source_dtype, parsed.target_dtype)


def require_live_cast_pair_allowed(
    repair_plan,
    observed_schema,
    allowed_pairs: Optional[Iterable[LiveCastPair]] = None,
) -> LiveCastPair:
    pair = resolve_live_cast_pair(repair_plan, observed_schema)
    configured = (
        frozenset(allowed_pairs)
        if allowed_pairs is not None
        else configured_live_cast_allowlist()
    )
    if pair not in configured:
        raise LiveCastPairNotAllowedError(
            f"Live CAST_COLUMN pair {pair.token!r} is not explicitly allowlisted "
            f"by {LIVE_CAST_ALLOWLIST_ENV}."
        )
    return pair
