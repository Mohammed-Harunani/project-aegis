import pandas as pd

from consultant.consultant import RepairPlan
from governance.approval import (
    ApprovalQueue,
    TicketNotFoundError,
    TicketNotPendingError,
)


def make_plan(confidence=0.85):
    return RepairPlan(
        proposed_action="RENAME_COLUMN new_name -> old_name",
        confidence=confidence,
        explanation="test plan",
    )


def make_context():
    df = pd.DataFrame({"new_name": [1, 2, 3]})
    return df, df  # stand-in for observed_schema/gold_schema objects


def test_submit_creates_pending_ticket():
    queue = ApprovalQueue()
    df, _ = make_context()

    ticket = queue.submit(make_plan(), observed_schema=None, gold_schema=None, target_dataset=df)

    assert ticket.status == "PENDING"
    assert ticket.decided_by is None
    assert ticket.ticket_id in queue._tickets


def test_list_pending_only_returns_pending_tickets():
    queue = ApprovalQueue()
    df, _ = make_context()

    t1 = queue.submit(make_plan(), observed_schema=None, gold_schema=None, target_dataset=df)
    t2 = queue.submit(make_plan(), observed_schema=None, gold_schema=None, target_dataset=df)
    queue.approve(t1.ticket_id, operator="mo")

    pending = queue.list_pending()

    assert len(pending) == 1
    assert pending[0].ticket_id == t2.ticket_id


def test_approve_transitions_status_and_records_operator():
    queue = ApprovalQueue()
    df, _ = make_context()
    ticket = queue.submit(make_plan(), observed_schema=None, gold_schema=None, target_dataset=df)

    approved = queue.approve(ticket.ticket_id, operator="mo", note="looks fine")

    assert approved.status == "APPROVED"
    assert approved.decided_by == "mo"
    assert approved.decision_note == "looks fine"
    assert approved.decided_at is not None


def test_reject_transitions_status_and_records_operator():
    queue = ApprovalQueue()
    df, _ = make_context()
    ticket = queue.submit(make_plan(), observed_schema=None, gold_schema=None, target_dataset=df)

    rejected = queue.reject(ticket.ticket_id, operator="mo", note="not safe")

    assert rejected.status == "REJECTED"
    assert rejected.decided_by == "mo"
    assert rejected.decision_note == "not safe"


def test_approve_unknown_ticket_raises_not_found():
    queue = ApprovalQueue()
    try:
        queue.approve("does-not-exist", operator="mo")
        assert False, "expected TicketNotFoundError"
    except TicketNotFoundError:
        pass


def test_approve_already_decided_ticket_raises_not_pending():
    queue = ApprovalQueue()
    df, _ = make_context()
    ticket = queue.submit(make_plan(), observed_schema=None, gold_schema=None, target_dataset=df)
    queue.approve(ticket.ticket_id, operator="mo")

    try:
        queue.approve(ticket.ticket_id, operator="mo")
        assert False, "expected TicketNotPendingError"
    except TicketNotPendingError:
        pass


def test_reject_already_approved_ticket_raises_not_pending():
    queue = ApprovalQueue()
    df, _ = make_context()
    ticket = queue.submit(make_plan(), observed_schema=None, gold_schema=None, target_dataset=df)
    queue.approve(ticket.ticket_id, operator="mo")

    try:
        queue.reject(ticket.ticket_id, operator="mo")
        assert False, "expected TicketNotPendingError"
    except TicketNotPendingError:
        pass
