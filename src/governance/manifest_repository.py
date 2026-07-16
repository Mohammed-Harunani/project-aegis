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
    commit: bool = True,
) -> HealingManifestRecord:
    """
    commit=True (default): standalone call, e.g. the AUTO_APPROVE path
    in app.py, where this is the only DB write in the request.
    commit=False: the caller (e.g. approve_ticket in app.py) is
    bundling this into a larger transaction alongside the ticket's
    APPROVED status change, and will commit or roll back both together.

    corrected_output_fingerprint (Phase 2.5 correction): the caller
    computes this from the actual corrected DataFrame -- this function
    only persists it, since it's purely a storage concern and Surgeon
    itself isn't touched to produce it (see app.py for how it's
    obtained without changing Surgeon's sandbox-mode behavior).
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
    )
    db.add(record)
    if commit:
        db.commit()
        db.refresh(record)
    else:
        db.flush()
    return record
