"""Generic cancel must dismiss the foreground wizard (expense vs leave)."""

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_LEAVE_REQUEST
from chat.services.expense_workflow import is_expense_in_progress
from chat.services.leave_workflow import is_leave_in_progress
from chat.services.orchestrator import ChatOrchestrator
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn
from chat.services.workflow_priority import (
    expense_workflow_is_foreground,
    leave_workflow_is_foreground,
    resolve_generic_cancel_target,
)

COMPANY_ID = "company-a"


def _paused_leave_plus_expense_review_wf() -> dict:
    return {
        "active_flow": "leave",
        "status": "paused",
        "draft": {"reason": "family program"},
        "step": "reason",
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [
                {"category": "Bus", "amount": 100.0, "from_to": "mirpur → motijheel"},
                {"category": "Lunch", "amount": 50.0},
            ],
        },
    }


def test_resolve_generic_cancel_prefers_foreground_expense_over_paused_leave():
    wf = _paused_leave_plus_expense_review_wf()
    assert expense_workflow_is_foreground(wf)
    assert not leave_workflow_is_foreground(wf)
    assert resolve_generic_cancel_target(wf) == "expense"


def test_resolve_generic_cancel_leave_when_only_leave_active():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {"reason": "sick"},
        "step": "reason",
        "review_pending": False,
    }
    assert resolve_generic_cancel_target(wf) == "leave"


def test_p00_cancel_routes_to_expense_during_expense_review():
    wf = _paused_leave_plus_expense_review_wf()
    snap = build_session_snapshot("cancel it", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf, utterance=None)
    assert decision.turn_kind == TurnKind.CANCEL
    assert decision.intent == INTENT_EXPENSE_CLAIM
    assert decision.target_workflow == "expense"
    assert decision.reason == "P00_cancel_expense"


def test_expense_review_prompt_domain_is_expense():
    wf = _paused_leave_plus_expense_review_wf()
    snap = build_session_snapshot("yes", workflow_state=wf)
    assert snap.expense_review_pending
    assert snap.active_prompt_domain == "expense"
    assert snap.active_prompt_slot == "review_confirm"


@pytest.mark.django_db
def test_orchestrator_cancel_it_during_expense_review_not_leave(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "cancel-exp-review"
    sid = "cancel-exp-review-session"
    orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        employee_id=emp,
        session_id=sid,
    )
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        employee_id=emp,
        session_id=sid,
    )
    session.workflow_state = _paused_leave_plus_expense_review_wf()
    session.save(update_fields=["workflow_state", "updated_at"])

    out = orch.run_chat(
        company_id=COMPANY_ID,
        message="cancel it",
        session_id=sid,
        employee_id=emp,
        trace_id="cancel-exp-review",
    )
    msg = out["response"]["message"] or ""
    session.refresh_from_db()
    assert not is_expense_in_progress(session.workflow_state)
    assert is_leave_in_progress(session.workflow_state)
    assert "expense" in msg.lower() or "খরচ" in msg
    assert "leave form" not in msg.lower()
