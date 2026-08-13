"""Pure contract tests for Phase 3.2 snapshot provenance identity."""

import pytest

from src.ingestion.repository import (
    InvalidSnapshotError,
    compute_snapshot_provenance_fingerprint,
)


def _fingerprint(**overrides):
    values = {
        "source_primary_key": ["customer_id"],
        "source_row_count": 3,
        "source_schema_fingerprint": "a" * 64,
        "source_dataset_fingerprint": "b" * 64,
    }
    values.update(overrides)
    return compute_snapshot_provenance_fingerprint(**values)


def test_snapshot_provenance_fingerprint_is_deterministic():
    assert _fingerprint() == (
        "7f43f729d1b5d61e28db8c20a112d9d2a41d452eb0fc0e9690c95aa68eb8733e"
    )
    assert _fingerprint() == _fingerprint()


@pytest.mark.parametrize(
    "change",
    [
        {"source_primary_key": ["external_id"]},
        {"source_primary_key": ["customer_id", "external_id"]},
        {"source_row_count": 4},
        {"source_schema_fingerprint": "c" * 64},
        {"source_dataset_fingerprint": "d" * 64},
    ],
)
def test_each_provenance_component_changes_identity(change):
    assert _fingerprint(**change) != _fingerprint()


@pytest.mark.parametrize(
    "change",
    [
        {"source_primary_key": []},
        {"source_primary_key": ["unsafe-name"]},
        {"source_primary_key": ["customer_id", "customer_id"]},
        {"source_row_count": -1},
        {"source_row_count": True},
        {"source_schema_fingerprint": "not-a-hash"},
        {"source_dataset_fingerprint": "A" * 64},
    ],
)
def test_invalid_provenance_evidence_fails_closed(change):
    with pytest.raises(InvalidSnapshotError):
        _fingerprint(**change)
