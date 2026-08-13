"""
Aegis_DatasetCodec

Shared, versioned DataFrame serialization used by approval tickets and
Phase 3.2 immutable dataset snapshots. The v2 encoder preserves column
order, dtypes, index identity, Decimal/UUID/temporal values, infinities,
and arbitrary JSON values. The legacy v1 decoder remains supported.
"""

import datetime as datetime_module
import decimal
import uuid

import pandas as pd


DATASET_FORMAT_VERSION = 2


def sanitize_scalar(value):
    """
    Tag non-JSON-native values so JSONB round trips retain exact types.

    Every dict/list is wrapped in a raw_json envelope. Without that rule,
    legitimate source JSON containing ``__aegis_type__`` could be mistaken
    for an internal codec tag and silently changed on replay.
    """
    if value is None:
        return None
    if isinstance(value, float):
        if value == float("inf"):
            return {"__aegis_type__": "positive_infinity"}
        if value == float("-inf"):
            return {"__aegis_type__": "negative_infinity"}
        return value
    if isinstance(value, decimal.Decimal):
        return {"__aegis_type__": "decimal", "value": str(value)}
    # Order matters because Timestamp subclasses datetime, which subclasses
    # date. Preserve the most specific type first.
    if isinstance(value, pd.Timestamp):
        return {
            "__aegis_type__": "pandas_timestamp",
            "value": value.isoformat(),
        }
    if isinstance(value, datetime_module.datetime):
        return {
            "__aegis_type__": "python_datetime",
            "value": value.isoformat(),
        }
    if isinstance(value, datetime_module.date):
        return {"__aegis_type__": "date", "value": value.isoformat()}
    if isinstance(value, uuid.UUID):
        return {"__aegis_type__": "uuid", "value": str(value)}
    if isinstance(value, (dict, list)):
        return {"__aegis_type__": "raw_json", "value": value}
    return value


def _restore_scalar_v2(value):
    if isinstance(value, dict) and "__aegis_type__" in value:
        kind = value["__aegis_type__"]
        if kind == "positive_infinity":
            return float("inf")
        if kind == "negative_infinity":
            return float("-inf")
        raw = value.get("value")
        if kind == "decimal":
            return decimal.Decimal(raw)
        if kind == "pandas_timestamp":
            return pd.Timestamp(raw)
        if kind == "python_datetime":
            return datetime_module.datetime.fromisoformat(raw)
        if kind == "date":
            return datetime_module.date.fromisoformat(raw)
        if kind == "uuid":
            return uuid.UUID(raw)
        if kind == "raw_json":
            return raw
    return value


def _restore_scalar_v1(value):
    """
    Decode the legacy format exactly as originally written.

    V1 did not wrap arbitrary dict/list values, so a legitimate source dict
    containing an old internal type tag is inherently ambiguous. Preserving
    the original decoder behavior is the only backward-compatible choice.
    """
    if isinstance(value, dict) and "__aegis_type__" in value:
        kind = value["__aegis_type__"]
        if kind == "positive_infinity":
            return float("inf")
        if kind == "negative_infinity":
            return float("-inf")
        raw = value.get("value")
        if kind == "decimal":
            return decimal.Decimal(raw)
        if kind == "pandas_timestamp":
            return pd.Timestamp(raw)
        if kind == "python_datetime":
            return datetime_module.datetime.fromisoformat(raw)
        if kind == "date":
            return datetime_module.date.fromisoformat(raw)
    return value


def encode_dataset(dataframe: pd.DataFrame) -> dict:
    """Encode a complete DataFrame in the current JSONB-safe format."""
    if not isinstance(dataframe, pd.DataFrame):
        raise TypeError("Dataset codec requires a pandas DataFrame.")

    clean = dataframe.astype(object).where(pd.notnull(dataframe), None)
    column_order = list(dataframe.columns)
    data = {
        column: [sanitize_scalar(value) for value in clean[column].tolist()]
        for column in column_order
    }
    index_values = [
        None
        if isinstance(value, float) and pd.isna(value)
        else sanitize_scalar(value)
        for value in dataframe.index.tolist()
    ]
    return {
        "format_version": DATASET_FORMAT_VERSION,
        "column_order": column_order,
        "dtypes": {
            column: str(dataframe[column].dtype) for column in column_order
        },
        "index": {
            "name": dataframe.index.name,
            "dtype": str(dataframe.index.dtype),
            "values": index_values,
        },
        "data": data,
    }


def decode_dataset(payload: dict) -> pd.DataFrame:
    """Decode supported v1/v2 payloads into replay-equivalent DataFrames."""
    if not isinstance(payload, dict):
        raise ValueError("Dataset payload must be a JSON object.")

    format_version = payload.get("format_version")
    if format_version == 1:
        restore_scalar = _restore_scalar_v1
    elif format_version == 2:
        restore_scalar = _restore_scalar_v2
    else:
        raise ValueError(
            f"Unsupported dataset format_version: {format_version!r}"
        )

    column_order = payload["column_order"]
    dtypes = payload["dtypes"]
    data = payload["data"]
    index_info = payload["index"]

    # Build each column independently as object dtype before reapplying its
    # recorded dtype. A whole-frame inference pass can silently collapse a
    # plain datetime.datetime into pandas.Timestamp before the cast occurs.
    columns = {}
    for column in column_order:
        restored_values = [restore_scalar(value) for value in data[column]]
        series = pd.Series(restored_values, dtype=object)
        target_dtype = dtypes.get(column)
        if target_dtype and target_dtype != "object":
            try:
                series = series.astype(target_dtype)
            except (TypeError, ValueError):
                # Values remain exact even for dtype labels pandas cannot
                # reapply directly.
                pass
        columns[column] = series

    dataframe = pd.DataFrame(columns, columns=column_order)

    index_values = [restore_scalar(value) for value in index_info["values"]]
    index = pd.Index(index_values, name=index_info["name"], dtype=object)
    index_dtype = index_info.get("dtype")
    if index_dtype and index_dtype != "object":
        try:
            index = index.astype(index_dtype)
        except (TypeError, ValueError):
            pass
    dataframe.index = index

    return dataframe


# Backward-compatible private names retained for already-closed callers and
# tests. New code should use the public encode/decode/sanitize names above.
_sanitize_scalar = sanitize_scalar
_dataset_to_json = encode_dataset
_dataset_from_json = decode_dataset


__all__ = [
    "DATASET_FORMAT_VERSION",
    "decode_dataset",
    "encode_dataset",
    "sanitize_scalar",
]
