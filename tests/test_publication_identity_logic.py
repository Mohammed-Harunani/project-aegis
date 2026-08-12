"""Pure target-marker identity checks for Phase 3.2.5."""

import uuid

from src.live_execution.writer import PostgresPublicationWriter


def _marker(operation="PUBLISH", publication_target_id=None):
    return {
        "operation": operation,
        "publication_target_id": publication_target_id,
    }


def test_lineage_aware_marker_requires_exact_publication_target():
    target_id = uuid.uuid4()
    assert PostgresPublicationWriter.marker_matches_publication_target(
        _marker(publication_target_id=target_id), str(target_id)
    )
    assert not PostgresPublicationWriter.marker_matches_publication_target(
        _marker(publication_target_id=uuid.uuid4()), target_id
    )


def test_historical_null_lineage_only_matches_historical_null_marker():
    assert PostgresPublicationWriter.marker_matches_publication_target(
        _marker(), None
    )
    assert not PostgresPublicationWriter.marker_matches_publication_target(
        _marker(), uuid.uuid4()
    )
    assert not PostgresPublicationWriter.marker_matches_publication_target(
        _marker(publication_target_id=uuid.uuid4()), None
    )


def test_operation_outcome_distinguishes_absence_from_identity_mismatch():
    target_id = uuid.uuid4()
    assert PostgresPublicationWriter._operation_marker_outcome(
        [_marker(publication_target_id=target_id)], "PUBLISH", target_id
    ) == "completed"
    assert PostgresPublicationWriter._operation_marker_outcome(
        [_marker(publication_target_id=uuid.uuid4())], "PUBLISH", target_id
    ) == "identity_mismatch"
    assert PostgresPublicationWriter._operation_marker_outcome(
        [_marker(operation="ROLLBACK", publication_target_id=target_id)],
        "PUBLISH",
        target_id,
    ) == "not_committed"
