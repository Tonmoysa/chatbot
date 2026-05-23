"""Leave submission path, schema, and idempotency."""

import pytest

from chat.services.crm.mock_crm import MockCRMAdapter
from chat.services.leave_fsm import (
    KEY_SUBMISSION_ID,
    KEY_STATUS,
    STATUS_SUBMITTED,
    is_leave_submission_locked,
    mark_submitted,
    normalize_workflow_state,
    read_leave_state,
)
from chat.services.leave_submission_service import LeaveSubmissionService


@pytest.mark.django_db
def test_single_submit_path_via_submission_service():
    crm = MockCRMAdapter()
    svc = LeaveSubmissionService(crm)
    entities = {
        "leave_type": "sick",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "start_date": "2026-05-22",
        "end_date": "2026-05-22",
        "reason": "fever",
    }
    r1 = svc.submit_confirmed_leave(
        workflow_state={},
        company_id="co",
        employee_id="E1",
        session_id="sess-1",
        entities=entities,
        decision={"outcome": "SUBMITTED", "leave_status": "pending"},
        trace_id="t1",
        idempotency_key="idem-abc",
    )
    assert r1.ok
    assert r1.submission_id.startswith("PHP-LEAVE-")
    assert is_leave_submission_locked(r1.workflow_state)
    assert read_leave_state(r1.workflow_state)["submission_id"] == r1.submission_id

    r2 = svc.submit_confirmed_leave(
        workflow_state=r1.workflow_state,
        company_id="co",
        employee_id="E1",
        session_id="sess-1",
        entities=entities,
        decision={"outcome": "SUBMITTED"},
        trace_id="t2",
        idempotency_key="idem-abc",
    )
    assert r2.deduped
    assert r2.submission_id == r1.submission_id


def test_normalize_legacy_leave_request_block():
    legacy = {
        "leave_request": {
            "active": True,
            "stage": "awaiting_confirmation",
            "draft": {"leave_type": "casual"},
            "pending_slot": None,
        }
    }
    norm = normalize_workflow_state(legacy)
    assert "leave_request" not in norm
    assert norm.get("active_flow") == "leave"
    assert norm.get("review_pending") is True
    assert norm["draft"]["leave_type"] == "casual"


def test_mark_submitted_terminal_fields():
    wf = mark_submitted(
        {"active_flow": "leave", "status": "active", "draft": {"leave_type": "sick"}},
        draft={"leave_type": "sick"},
        submission_id="PHP-LEAVE-TEST99",
        idempotency_key="key-1",
    )
    assert wf[KEY_STATUS] == STATUS_SUBMITTED
    assert wf[KEY_SUBMISSION_ID] == "PHP-LEAVE-TEST99"
    assert is_leave_submission_locked(wf)
