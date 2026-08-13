"""
Aegis_ApprovalQueue
Phase 2.2 -- Human Approval Workflow

When GovernancePolicy marks a repair REQUIRES_HUMAN_APPROVAL, Surgeon
must not execute it automatically. Instead an ApprovalTicket is
created and held here until a human operator approves or rejects it
via the API.

Scope note: storage is in-memory (a plain dict), on purpose. Durable
storage is Phase 2.3 (Manifest Persistence), not this phase -- so
restarting the API process currently loses pending tickets. That is
a known limitation of 2.2, not an oversight, and it's why this needs
a decision from you before I build 2.3: which database, hosted
where, with what connection details.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Dict, List, Literal, Optional

import pandas as pd

from src.consultant.consultant import RepairPlan
from src.governance.manifest import ConversionOutcomeMetadata


ApprovalStatus = Literal["PENDING", "APPROVED", "REJECTED"]


class TicketNotFoundError(Exception):
    """Raised when a ticket_id does not exist in the queue."""


class TicketNotPendingError(Exception):
    """Raised when approve/reject is called on a ticket that has already been decided."""


@dataclass
class ApprovalTicket:
    ticket_id: str
    repair_plan: RepairPlan
    confidence: float
    status: ApprovalStatus
    created_at: str

    # Execution context, held so approval can trigger Surgeon later
    # without re-running Inspector/Consultant from scratch.
    observed_schema: object
    gold_schema: object
    target_dataset: pd.DataFrame

    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    decision_note: Optional[str] = None

    # Phase 2.4 -- which immutable registry schema version this ticket
    # was validated against, if any. None for legacy direct-gold_schema
    # requests, which never reference the registry at all.
    schema_version_id: Optional[str] = None

    # Phase 2.5 final architecture -- trusted-source provenance. None
    # for sample_data tickets (/simulate-migration); populated for
    # /simulate-migration-from-source tickets. live_eligible is the
    # enforced gate: a sample_data ticket can be approved but can
    # never execute live.
    source_schema: Optional[str] = None
    source_table: Optional[str] = None
    source_primary_key: Optional[List[str]] = None
    source_row_count: Optional[int] = None
    source_schema_fingerprint: Optional[str] = None
    source_dataset_fingerprint: Optional[str] = None
    live_eligible: bool = False

    # Phase 3.2 -- new source-backed tickets replay from the immutable
    # snapshot linked through this captured simulation run. Historical and
    # sample-data tickets retain their embedded target_dataset payload.
    source_ingestion_run_id: Optional[str] = None

    # Phase 3.1.4 -- persisted, redacted CAST_COLUMN preflight decision.
    # None for non-cast tickets. Human approval may proceed only when this
    # decision exists, is SAFE, and matches a fresh deterministic analysis.
    conversion_decision: Optional[ConversionOutcomeMetadata] = None


class ApprovalQueue:
    """
    Aegis_ApprovalQueue
    Holds repair plans that require human sign-off before Surgeon
    is allowed to execute them, and records who decided what, when.
    """

    def __init__(self):
        self._tickets: Dict[str, ApprovalTicket] = {}

    def submit(
        self,
        repair_plan: RepairPlan,
        observed_schema,
        gold_schema,
        target_dataset: pd.DataFrame,
        conversion_decision: Optional[ConversionOutcomeMetadata] = None,
    ) -> ApprovalTicket:
        ticket = ApprovalTicket(
            ticket_id=str(uuid.uuid4()),
            repair_plan=repair_plan,
            confidence=repair_plan.confidence,
            status="PENDING",
            created_at=datetime.now(UTC).isoformat(),
            observed_schema=observed_schema,
            gold_schema=gold_schema,
            target_dataset=target_dataset,
            conversion_decision=conversion_decision,
        )
        self._tickets[ticket.ticket_id] = ticket
        return ticket

    def get(self, ticket_id: str) -> ApprovalTicket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise TicketNotFoundError(ticket_id)
        return ticket

    def list_pending(self) -> List[ApprovalTicket]:
        return [t for t in self._tickets.values() if t.status == "PENDING"]

    def approve(self, ticket_id: str, operator: str, note: str = "") -> ApprovalTicket:
        ticket = self.get(ticket_id)
        if ticket.status != "PENDING":
            raise TicketNotPendingError(f"Ticket {ticket_id} is {ticket.status}, not PENDING.")

        # Phase 3.1.4: approval itself enforces conversion safety, not only
        # the API orchestrator. The local import avoids a module cycle while
        # keeping the legacy in-memory queue governed by the same invariant.
        from src.governance.conversion_safety import require_safe_conversion_decision

        require_safe_conversion_decision(
            ticket.repair_plan,
            ticket.target_dataset,
            ticket.conversion_decision,
        )

        ticket.status = "APPROVED"
        ticket.decided_by = operator
        ticket.decided_at = datetime.now(UTC).isoformat()
        ticket.decision_note = note
        return ticket

    def reject(self, ticket_id: str, operator: str, note: str = "") -> ApprovalTicket:
        ticket = self.get(ticket_id)
        if ticket.status != "PENDING":
            raise TicketNotPendingError(f"Ticket {ticket_id} is {ticket.status}, not PENDING.")
        ticket.status = "REJECTED"
        ticket.decided_by = operator
        ticket.decided_at = datetime.now(UTC).isoformat()
        ticket.decision_note = note
        return ticket
