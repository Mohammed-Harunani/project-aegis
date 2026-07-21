"""
Pure-Python tests for the dataset codec (approval_repository.py's
_dataset_to_json/_dataset_from_json and their scalar sanitize/restore
helpers). These functions themselves need no database -- the only
barrier to testing them without one is that they live inside a module
which ALSO imports sqlalchemy at the top level for its Postgres-backed
repository class. This file stubs sqlalchemy only if it isn't already
installed, so it runs correctly both in a minimal sandbox (no
sqlalchemy) and a real environment (sqlalchemy actually present) --
try the normal import first, fall back to stubbing only on failure.
"""

import sys
import decimal
import uuid
import datetime

import pandas as pd

try:
    import sqlalchemy  # noqa: F401
except ImportError:
    import types

    def _stub_callable(*a, **k):
        class _Stub:
            def __init__(self, *a, **k):
                pass

            def __getattr__(self, name):
                return _stub_callable

        return _Stub()

    _sqlalchemy_mod = types.ModuleType("sqlalchemy")
    for _name in [
        "Column", "Text", "Numeric", "DateTime", "Integer", "ForeignKey",
        "UniqueConstraint", "Index", "text", "CheckConstraint", "Boolean",
    ]:
        setattr(_sqlalchemy_mod, _name, _stub_callable)
    _pg_mod = types.ModuleType("sqlalchemy.dialects.postgresql")
    _pg_mod.UUID = _stub_callable
    _pg_mod.JSONB = _stub_callable
    _dialects_mod = types.ModuleType("sqlalchemy.dialects")
    _dialects_mod.postgresql = _pg_mod
    _orm_mod = types.ModuleType("sqlalchemy.orm")
    _orm_mod.Session = object
    _orm_mod.declarative_base = lambda: type("Base", (), {})
    sys.modules["sqlalchemy"] = _sqlalchemy_mod
    sys.modules["sqlalchemy.dialects"] = _dialects_mod
    sys.modules["sqlalchemy.dialects.postgresql"] = _pg_mod
    sys.modules["sqlalchemy.orm"] = _orm_mod

from governance.approval_repository import (
    DATASET_FORMAT_VERSION,
    _dataset_to_json,
    _dataset_from_json,
)


def test_current_format_version_is_2():
    assert DATASET_FORMAT_VERSION == 2


def test_v2_round_trips_decimal_exactly():
    df = pd.DataFrame({"amount": [decimal.Decimal("12345678901234567890.123456789")]})
    payload = _dataset_to_json(df)
    assert payload["format_version"] == 2
    restored = _dataset_from_json(payload)
    value = restored["amount"].iloc[0]
    assert isinstance(value, decimal.Decimal)
    assert str(value) == "12345678901234567890.123456789"


def test_v2_round_trips_uuid():
    original = uuid.UUID("12345678-1234-5678-1234-567812345678")
    df = pd.DataFrame({"record_id": [original]})
    restored = _dataset_from_json(_dataset_to_json(df))
    value = restored["record_id"].iloc[0]
    assert isinstance(value, uuid.UUID)
    assert value == original


def test_v2_round_trips_nested_dict_and_list():
    original = {"key": "value", "nested": [1, 2, {"deep": True}]}
    df = pd.DataFrame({"metadata": [original]})
    restored = _dataset_from_json(_dataset_to_json(df))
    assert restored["metadata"].iloc[0] == original


def test_v2_round_trips_date_and_datetime():
    d = datetime.date(2026, 1, 15)
    dt = datetime.datetime(2026, 1, 15, 10, 30, tzinfo=datetime.timezone.utc)
    df = pd.DataFrame({"d": [d], "dt": [dt]})
    restored = _dataset_from_json(_dataset_to_json(df))
    assert restored["d"].iloc[0] == d
    assert restored["dt"].iloc[0] == dt


def test_v2_round_trips_legitimate_dict_containing_reserved_key():
    """The exact scenario a prior review confirmed was broken: a
    legitimate JSONB value containing "__aegis_type__" as its own
    data, not as an Aegis-internal tag."""
    original = {"__aegis_type__": "decimal", "value": "10.50"}
    df = pd.DataFrame({"data": [original]})
    restored = _dataset_from_json(_dataset_to_json(df))
    assert restored["data"].iloc[0] == original


def test_v2_round_trips_positive_and_negative_infinity():
    df = pd.DataFrame({"x": [float("inf"), float("-inf"), 1.5]})
    restored = _dataset_from_json(_dataset_to_json(df))
    assert restored["x"].iloc[0] == float("inf")
    assert restored["x"].iloc[1] == float("-inf")
    assert restored["x"].iloc[2] == 1.5


def test_legacy_v1_payload_decodes_normal_decimal_correctly():
    """A v1 payload (the original encoding -- no raw_json wrapper, no
    UUID tag) with a normal, unambiguous Decimal value must still
    decode correctly via the legacy path."""
    v1_payload = {
        "format_version": 1,
        "column_order": ["amount"],
        "dtypes": {"amount": "object"},
        "index": {"name": None, "dtype": "int64", "values": [0]},
        "data": {"amount": [{"__aegis_type__": "decimal", "value": "99.99"}]},
    }
    restored = _dataset_from_json(v1_payload)
    assert restored["amount"].iloc[0] == decimal.Decimal("99.99")


def test_legacy_v1_payload_has_documented_ambiguity_for_reserved_key_collision():
    """A v1 payload's inherent, unresolvable ambiguity: since v1 never
    wrapped dict/list values, a legitimate user dict happening to
    contain "__aegis_type__" is indistinguishable from an actual v1
    tag. The legacy decoder resolves this the same way v1's own code
    always did (treat it as the tag) -- this test documents that
    known limitation rather than asserting it's "correct", since there
    is no way to retroactively recover certainty for v1 data."""
    v1_payload = {
        "format_version": 1,
        "column_order": ["data"],
        "dtypes": {"data": "object"},
        "index": {"name": None, "dtype": "int64", "values": [0]},
        "data": {"data": [{"__aegis_type__": "decimal", "value": "10.50"}]},
    }
    restored = _dataset_from_json(v1_payload)
    # Documented behavior: resolved as a Decimal (v1's own
    # interpretation), NOT recoverable as the original dict.
    assert restored["data"].iloc[0] == decimal.Decimal("10.50")


def test_unsupported_format_version_rejected():
    bad_payload = {
        "format_version": 3,
        "column_order": ["x"],
        "dtypes": {"x": "object"},
        "index": {"name": None, "dtype": "int64", "values": [0]},
        "data": {"x": [1]},
    }
    try:
        _dataset_from_json(bad_payload)
        assert False, "expected ValueError for unsupported format_version"
    except ValueError as e:
        assert "format_version" in str(e)


def test_missing_format_version_rejected():
    bad_payload = {
        "column_order": ["x"],
        "dtypes": {"x": "object"},
        "index": {"name": None, "dtype": "int64", "values": [0]},
        "data": {"x": [1]},
    }
    try:
        _dataset_from_json(bad_payload)
        assert False, "expected ValueError for missing format_version"
    except ValueError:
        pass
