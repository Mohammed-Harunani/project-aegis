"""
Aegis_LiveExecution_OutputFingerprint
Phase 2.5 correction -- proves the live recomputation produces the
exact same corrected dataset as the sandbox execution that was
actually approved, not just the same row count.

Between sandbox approval and a later live-execution request, code,
component versions, or repair behavior could have changed -- the live
path recomputes via Surgeon rather than persisting a second copy of
the corrected data (see the spec's reasoning: avoid storing the
correction twice, stay consistent with "every repair must be
deterministic"). That determinism claim is exactly what this
fingerprint verifies rather than assumes.
"""

import hashlib
import json

import pandas as pd

# Phase 3.2 moved the already-proven scalar codec to a shared module because
# immutable snapshots and approval replay now require the exact same encoder.
# Fingerprinting continues to reuse that single implementation.
from src.governance.dataset_codec import (
    sanitize_scalar as _sanitize_value_for_fingerprint,
)


def compute_dataframe_fingerprint(df: pd.DataFrame) -> str:
    """
    Deterministic SHA-256 over column names+order, dtypes, index
    (name, dtype, values), and every row's values -- reuses the same
    tagged scalar sanitizer already proven correct for Decimal/
    Timestamp/date/infinity in Phase 2.3, so two DataFrames with
    identical logical content produce the identical fingerprint
    regardless of incidental Python object identity.
    """
    clean = df.astype(object).where(pd.notnull(df), None)

    canonical = {
        "columns": list(df.columns),
        "dtypes": [str(df[col].dtype) for col in df.columns],
        "index": {
            "name": df.index.name,
            "dtype": str(df.index.dtype),
            "values": [
                None if (isinstance(v, float) and pd.isna(v)) else _sanitize_value_for_fingerprint(v)
                for v in df.index.tolist()
            ],
        },
        "rows": [
            [_sanitize_value_for_fingerprint(v) for v in clean[col].tolist()]
            for col in df.columns
        ],
    }
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
