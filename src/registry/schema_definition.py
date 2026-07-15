"""
Aegis_SchemaDefinition
Phase 2.4 -- versioned Gold schema definition format, validation, and
fingerprinting. Pure Python/pandas -- no database dependency, which is
exactly why this module is fully tested by direct execution rather
than reasoned through blind, unlike most of the rest of Phase 2.4.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Dict, List

import pandas as pd


SUPPORTED_FORMAT_VERSIONS = {1}

# Schema *names* (gold_schemas.name), not column names -- lowercase
# identifier format, e.g. "customer_master".
SCHEMA_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class InvalidSchemaDefinitionError(Exception):
    """Raised for a structurally or semantically invalid schema definition."""


@dataclass(frozen=True)
class SchemaColumn:
    name: str
    dtype: str


def validate_schema_name(name: str) -> None:
    if not SCHEMA_NAME_PATTERN.match(name or ""):
        raise InvalidSchemaDefinitionError(
            f"Schema name must be lowercase identifier format "
            f"(letters, digits, underscores, starting with a letter): {name!r}"
        )


def validate_schema_definition(format_version: int, columns: List[dict]) -> None:
    """
    Raises InvalidSchemaDefinitionError on the first problem found:
    unsupported format_version, no columns, an empty/duplicate column
    name, or a dtype pandas.api.types.pandas_dtype() doesn't accept.
    """
    if format_version not in SUPPORTED_FORMAT_VERSIONS:
        raise InvalidSchemaDefinitionError(f"Unsupported format_version: {format_version!r}")

    if not columns:
        raise InvalidSchemaDefinitionError("At least one column is required.")

    seen_names = set()
    for col in columns:
        name = col.get("name", "")
        if not name or not name.strip():
            raise InvalidSchemaDefinitionError("Column names must be non-empty.")
        if name in seen_names:
            raise InvalidSchemaDefinitionError(f"Duplicate column name: {name!r}")
        seen_names.add(name)

        dtype = col.get("dtype", "")
        try:
            pd.api.types.pandas_dtype(dtype)
        except TypeError:
            raise InvalidSchemaDefinitionError(f"Unsupported dtype for column {name!r}: {dtype!r}")


def compute_fingerprint(format_version: int, columns: List[dict]) -> str:
    """
    Deterministic SHA-256 over column names, order, and dtypes, plus
    format_version. created_by / timestamps / change_summary never
    affect this -- confirmed directly: identical definitions produce
    identical fingerprints, reordering columns changes it, changing a
    dtype changes it, and an unexpected extra key on a column dict
    does not change it (only name/dtype are pulled out explicitly).
    """
    canonical = {
        "format_version": format_version,
        "columns": [{"name": c["name"], "dtype": c["dtype"]} for c in columns],
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def to_gold_schema_dict(columns: List[dict]) -> Dict[str, str]:
    """
    Converts the registry's ordered columns array to the flat
    {name: dtype} shape _build_gold_schema() in app.py already expects
    -- no separate Gold-schema construction path for registry-backed
    requests.
    """
    return {c["name"]: c["dtype"] for c in columns}
