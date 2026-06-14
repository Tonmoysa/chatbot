"""Leave switch from expense, intent buffer, date grounding, submit locks."""

from __future__ import annotations

from chat.services.leave.intent_buffer import (
    capture_leave_intent_buffer,
    consume_leave_intent_buffer,
    extract_leave_intent_patch,
)
from chat.services.leave_meta_queries import block_duplicate_submitted_leave_dates
from chat.services.leave_submission_service import LeaveSubmissionService
from chat.services.leave_workflow import process_leave_turn
from chat.services.workflow_navigation import (
    is_leave_application_message,
    is_leave_navigation_phrase,
)
from chat.services.workflow_suspend import wants_resume_suspended_leave


def test_reason_in_message_not_leave_navigation():
    msg = "agami 15 august leave chai.reason personal work."
    assert wants_resume_suspended_leave(msg) is False
    assert is_leave_navigation_phrase(msg) is False
    assert is_leave_application_message(msg) is True


def test_leave_nav_phrase_still_works():
    assert wants_resume_suspended_leave("leave request e back koro") is True
    assert is_leave_application_message("leave request e back koro") is False


def test_extract_intent_patch_august_and_reason():
    patch = extract_leave_intent_patch(
        "agami 15 august leave chai. reason personal work."
    )
    assert patch.get("start_date") == "2026-08-15"
    assert "personal" in str(patch.get("reason") or "").lower()


def test_intent_buffer_two_turn_flow():
    wf: dict = {}
    wf = capture_leave_intent_buffer(
        wf, "agami 15 august leave chai. reason personal work."
    )
    assert wf.get("leave_intent_buffer", {}).get("start_date") == "2026-08-15"

    draft: dict = {}
    wf, draft = consume_leave_intent_buffer(wf, draft)
    assert draft.get("start_date") == "2026-08-15"
    assert "personal" in str(draft.get("reason") or "").lower()
    assert "leave_intent_buffer" not in wf


def test_vague_leave_does_not_inject_tomorrow(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    pack = process_leave_turn(
        workflow_state={},
        message="ami leave nite chai",
        entities={"start_date": "2026-06-14", "end_date": "2026-06-14"},
        company_id="company-a",
    )
    draft = pack["workflow_state"].get("draft") or {}
    assert draft.get("start_date") in (None, "")


def test_compound_leave_during_expense_starts_with_date(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"category": "Lunch", "amount": 150}],
            "pending_line": {"category": "Bus", "amount": 100},
            "pending_step": "from_to",
        }
    }
    wf = capture_leave_intent_buffer(
        wf, "agami 15 august leave chai. reason personal work."
    )
    pack = process_leave_turn(
        workflow_state=wf,
        message="agami 15 august leave chai. reason personal work.",
        entities={},
        company_id="company-a",
    )
    draft = pack["workflow_state"].get("draft") or {}
    assert draft.get("start_date") == "2026-08-15"
    assert "personal" in str(draft.get("reason") or "").lower()


def test_block_duplicate_submitted_leave_dates():
    wf = {
        "leave_last_submission": {
            "submission_id": "PHP-LEAVE-1",
            "draft": {"start_date": "2026-08-15", "end_date": "2026-08-15"},
        }
    }
    msg = block_duplicate_submitted_leave_dates(
        wf,
        {"start_date": "2026-08-15", "end_date": "2026-08-15"},
    )
    assert msg is not None
    assert "2026-08-15" in msg


def test_submission_service_blocks_same_date_resubmit():
    from chat.services.crm.mock_crm import MockCRMAdapter

    svc = LeaveSubmissionService(MockCRMAdapter())
    entities = {
        "leave_type": "annual",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "start_date": "2026-08-15",
        "end_date": "2026-08-15",
        "reason": "personal work",
    }
    r1 = svc.submit_confirmed_leave(
        workflow_state={},
        company_id="co",
        employee_id="E1",
        session_id="s1",
        entities=entities,
        decision={"outcome": "SUBMITTED"},
        trace_id="t1",
    )
    assert r1.ok
    wf = dict(r1.workflow_state)
    wf.pop("active_flow", None)
    wf.pop("status", None)
    wf.pop("locked", None)
    wf["draft"] = {}
    wf["leave_last_submission"] = {
        "submission_id": r1.submission_id,
        "draft": entities,
    }
    r2 = svc.submit_confirmed_leave(
        workflow_state=wf,
        company_id="co",
        employee_id="E1",
        session_id="s1",
        entities=entities,
        decision={"outcome": "SUBMITTED"},
        trace_id="t2",
    )
    assert not r2.ok
    assert "2026-08-15" in (r2.detail or "")
