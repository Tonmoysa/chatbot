"""Cross-workflow navigation phrases (leave ↔ expense) and session gates."""

import pytest

from chat.constants import INTENT_LEAVE_REQUEST, INTENT_UNKNOWN
from chat.services.leave.reason_value import extract_reason_value
from chat.services.leave_workflow import is_leave_in_progress
from chat.services.orchestrator import (
    ChatOrchestrator,
    _detect_intent_during_expense_workflow,
)
from chat.services.workflow_navigation import (
    format_no_active_leave_session_message,
    is_leave_application_message,
    is_leave_navigation_phrase,
)
from chat.services.workflow_suspend import wants_resume_suspended_leave


@pytest.mark.parametrize(
    "message",
    [
        "leave e back koro",
        "leave e asho",
        "leave request e back koro",
        "leave request e back koor",
        "chuti e back koro",
        "ছুটি তে ফিরে যাও",
        "back to leave",
    ],
)
def test_resume_suspended_leave_phrases(message: str) -> None:
    assert wants_resume_suspended_leave(message)
    assert is_leave_navigation_phrase(message)


@pytest.mark.parametrize(
    "message",
    [
        "expense e back koro",
        "summery",
        "snack 70 taka",
    ],
)
def test_resume_leave_not_triggered_by_expense_slot_work(message: str) -> None:
    assert not wants_resume_suspended_leave(message)
    assert not is_leave_navigation_phrase(message)


@pytest.mark.parametrize(
    "message",
    [
        "leave request e back koro",
        "leave request e back koor",
        "leave e back koro",
        "chuti e back koro",
    ],
)
def test_navigation_is_not_leave_application(message: str) -> None:
    assert is_leave_navigation_phrase(message)
    assert not is_leave_application_message(message)


@pytest.mark.parametrize(
    "message",
    [
        "ami kalke sick leave nite chai",
        "amar kalke leave lagbe",
        "apply for leave tomorrow",
    ],
)
def test_leave_application_still_detected(message: str) -> None:
    assert is_leave_application_message(message)


def test_extract_reason_skips_navigation_phrase() -> None:
    assert extract_reason_value("leave request e back koor") is None
    assert extract_reason_value("leave request e back koro") is None


def test_expense_gate_leave_nav_without_session() -> None:
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [],
            "pending_line": {},
        },
    }
    out = _detect_intent_during_expense_workflow(
        "leave request e back koor",
        wf,
        balance_probe=False,
    )
    assert out["intent"] == INTENT_UNKNOWN
    assert "leave_nav_no_session" in out.get("source", "")


def test_expense_gate_leave_nav_with_suspended_still_resumes() -> None:
    wf = {
        "expense_request": {"active": True, "stage": "collecting", "items": []},
        "suspended_leave": {
            "draft": {"start_date": "2026-06-04", "reason": "family"},
            "step": "payment",
        },
    }
    out = _detect_intent_during_expense_workflow(
        "leave request e back koro",
        wf,
        balance_probe=False,
    )
    assert out["intent"] == INTENT_LEAVE_REQUEST
    assert "resume_leave_nav" in out.get("source", "")


def test_no_session_message_mentions_expense_when_active() -> None:
    msg = format_no_active_leave_session_message(expense_active=True)
    assert "চালু নেই" in msg
    assert "Expense form" in msg


@pytest.mark.django_db
def test_expense_active_leave_nav_no_session_does_not_start_leave(monkeypatch):
    """Regression: navigation must not become leave reason or open leave wizard."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "nav-no-leave-session"
    r1 = orch.run_chat(
        company_id="company-a",
        message="lunch 100 taka, bus 50 office to badda",
        session_id=None,
        employee_id=emp,
        trace_id="nav-exp-start",
    )
    sid = r1["_session_id"]
    pack = orch.run_chat(
        company_id="company-a",
        message="leave request e back koor",
        session_id=sid,
        employee_id=emp,
        trace_id="nav-leave-back",
    )
    assert pack["intent"] == INTENT_UNKNOWN
    assert pack["decision"]["outcome"] == "INFORMATIONAL"
    assert "LEAVE_NAV_NO_SESSION" in pack["decision"].get("rules_applied", [])
    body = pack["response"]["message"] or ""
    assert "চালু নেই" in body
    assert "e back koor" not in body
    assert "কারণ:" not in body

    session = orch.memory.get_or_create_session(
        company_id="company-a",
        session_id=sid,
        employee_id=emp,
    )
    session.refresh_from_db()
    assert not is_leave_in_progress(session.workflow_state)
