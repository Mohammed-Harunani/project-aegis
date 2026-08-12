import pytest

from src.consultant.consultant import RepairPlan
from src.inspector import ColumnStats, ObservedSchema
from src.live_execution.cast_allowlist import (
    LiveCastAllowlistError,
    LiveCastPair,
    LiveCastPairNotAllowedError,
    configured_live_cast_allowlist,
    parse_live_cast_allowlist,
    require_live_cast_pair_allowed,
    resolve_live_cast_pair,
)


def _schema(dtype="object"):
    return ObservedSchema(
        columns={"amount": ColumnStats(null_count=0, unique_count=3, dtype=dtype)},
        column_order=["amount"],
    )


def _plan(target="int64", suffix=""):
    return RepairPlan(
        proposed_action=f"CAST_COLUMN amount TO {target}{suffix}",
        confidence=0.85,
        explanation="cast",
    )


def test_unset_or_blank_configuration_is_empty_and_fail_closed(monkeypatch):
    monkeypatch.delenv("AEGIS_LIVE_CAST_ALLOWLIST", raising=False)
    assert configured_live_cast_allowlist() == frozenset()
    assert parse_live_cast_allowlist("   ") == frozenset()

    with pytest.raises(LiveCastPairNotAllowedError, match="object->int64"):
        require_live_cast_pair_allowed(_plan(), _schema())


def test_parser_accepts_only_explicit_unique_supported_pairs(monkeypatch):
    monkeypatch.setenv(
        "AEGIS_LIVE_CAST_ALLOWLIST",
        "object->int64,int64->float64,bool->object",
    )
    assert configured_live_cast_allowlist() == frozenset(
        {
            LiveCastPair("object", "int64"),
            LiveCastPair("int64", "float64"),
            LiveCastPair("bool", "object"),
        }
    )


@pytest.mark.parametrize(
    "raw",
    [
        "object-int64",
        "object->int64,",
        "object ->int64",
        "object-> int64",
        "object->decimal",
        "uuid->object",
        "object->int64,object->int64",
    ],
)
def test_malformed_or_unsupported_configuration_is_rejected(raw):
    with pytest.raises(LiveCastAllowlistError):
        parse_live_cast_allowlist(raw)


def test_pair_resolution_uses_trusted_observed_logical_dtype():
    pair = resolve_live_cast_pair(_plan("int64"), _schema("object"))
    assert pair == LiveCastPair("object", "int64")
    assert pair.token == "object->int64"


def test_allowlisted_pair_is_permitted_and_other_pair_is_refused():
    allowed = {LiveCastPair("object", "int64")}
    assert require_live_cast_pair_allowed(_plan(), _schema(), allowed) == LiveCastPair(
        "object", "int64"
    )

    with pytest.raises(LiveCastPairNotAllowedError, match="object->bool"):
        require_live_cast_pair_allowed(_plan("bool"), _schema(), allowed)


def test_drop_invalid_can_never_be_live_allowlisted():
    with pytest.raises(LiveCastAllowlistError, match="row dropping"):
        resolve_live_cast_pair(
            _plan("int64", " WITH_DROP_INVALID"),
            _schema(),
        )


def test_missing_observed_column_is_rejected():
    empty = ObservedSchema(columns={}, column_order=[])
    with pytest.raises(LiveCastAllowlistError, match="source dtype"):
        resolve_live_cast_pair(_plan(), empty)
