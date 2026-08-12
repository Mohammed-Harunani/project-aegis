"""
Aegis_ManifestRepository
Phase 2.3 -- persists every HealingManifest Surgeon produces.

ticket_id is nullable and left None for AUTO_APPROVE executions,
since those never go through the approval queue -- there is no
ticket to link back to. Both AUTO_APPROVE and manually-approved
executions call this; see app.py.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from src.db.models import HealingManifestRecord
from src.governance.manifest import HealingManifest


def save_manifest(
    db: Session,
    manifest: HealingManifest,
    ticket_id: Optional[str] = None,
    schema_version_id: Optional[str] = None,
    corrected_output_fingerprint: Optional[str] = None,
    source_schema: Optional[str] = None,
    source_table: Optional[str] = None,
    source_primary_key: Optional[list] = None,
    source_row_count: Optional[int] = None,
    source_schema_fingerprint: Optional[str] = None,
    source_dataset_fingerprint: Optional[str] = None,
    commit: bool = True,
) -> HealingManifestRecord:
    """
    commit=True (default): standalone call, e.g. the AUTO_APPROVE path
    in app.py, where this is the only DB write in the request.
    commit=False: the caller (e.g. approve_ticket in app.py) is
    bundling this into a larger transaction alongside the ticket's
    APPROVED status change, and will commit or roll back both together.

    corrected_output_fingerprint: the caller computes this from
    manifest.corrected_dataset (populated by Surgeon itself). This
    function only persists the fingerprint string; the DataFrame
    itself is never written here or anywhere in the database.

    source_* fields (Phase 2.5 final architecture): mirror the
    ticket's own source provenance at manifest-creation time,
    independent of whatever the ticket looks like later. None for
    sample_data-derived manifests.
    """
    record = HealingManifestRecord(
        manifest_id=uuid.uuid4(),
        ticket_id=uuid.UUID(ticket_id) if ticket_id else None,
        timestamp=datetime.fromisoformat(manifest.timestamp),
        repair_plan={
            "proposed_action": manifest.repair_plan.proposed_action,
            "confidence": manifest.repair_plan.confidence,
            "explanation": manifest.repair_plan.explanation,
        },
        execution_result={
            "applied": manifest.execution_result.applied,
            "validation": {
                "success": manifest.execution_result.validation.success,
                "message": manifest.execution_result.validation.message,
            },
        },
        execution_mode=manifest.execution_mode,
        operator=manifest.operator,
        component_versions=manifest.component_versions,
        original_row_count=manifest.original_row_count,
        final_row_count=manifest.final_row_count,
        integrity_status=manifest.integrity_status,
        risk_level=manifest.risk_level,
        schema_version_id=uuid.UUID(schema_version_id) if schema_version_id else None,
        corrected_output_fingerprint=corrected_output_fingerprint,
        source_schema=source_schema,
        source_table=source_table,
        source_primary_key=source_primary_key,
        source_row_count=source_row_count,
        source_schema_fingerprint=source_schema_fingerprint,
        source_dataset_fingerprint=source_dataset_fingerprint,
        conversion_outcome=(
            manifest.conversion_outcome.to_dict()
            if manifest.conversion_outcome is not None
            else None
        ),
    )
    db.add(record)
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record
