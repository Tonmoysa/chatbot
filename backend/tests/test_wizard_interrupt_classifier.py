"""Wizard interrupt classifier — Bengali leave during expense, rules + LLM."""

import pytest

from chat.constants import INTENT_LEAVE_REQUEST, INTENT_UNKNOWN
from chat.services.expense_workflow import is_expense_in_progress
from chat.services.leave_workflow import is_leave_in_progress
from chat.services.orchestrator import (
    ChatOrchestrator,
    _detect_intent_during_expense_workflow,
)
from chat.services.turn_classifier import TURN_NEW_WORKFLOW, classify_workflow_turn
from chat.services.workflow_navigation import is_leave_application_message
from chat.services.workflow_suspend import has_suspended_expense
from chat.services.wizard_interrupt_classifier import (
    INTERRUPT_NEW_LEAVE,
    WizardInterruptContext,
    classify_wizard_interrupt,
    rules_wizard_interrupt,
)

COMPANY_ID = "company-a"

BN_SICK_LEAVE_VOICE = (
    "আমার শরীর খারাপ তাই কালকে লিভ লাগবে ফুল্লিপেড এন্ড ফোল্ডে ফুল ডে"
)
BN_SICK_LEAVE_SHORT = "আমার শরীর খারাপ তাই কালকে লিভ লাগবে"
BANGLISH_SICK_LEAVE = "amar soril kharap tai leave lagbe"


@pytest.mark.parametrize(
    "message",
    [
        BN_SICK_LEAVE_VOICE,
        BN_SICK_LEAVE_SHORT,
        BANGLISH_SICK_LEAVE,
        "ami kalke sick leave nite chai paid full day",
    ],
)
def test_leave_application_detects_bengali_and_banglish(message: str) -> None:
    assert is_leave_application_message(message)


def test_rules_interrupt_new_leave_during_expense() -> None:
    ctx = WizardInterruptContext(expense_active=True, expense_stage="review")
    decision = rules_wizard_interrupt(BN_SICK_LEAVE_SHORT, context=ctx)
    assert decision.interrupt_type == INTERRUPT_NEW_LEAVE
    assert decision.maps_to_intent == INTENT_LEAVE_REQUEST


def test_expense_gate_bengali_leave_intent_without_llm() -> None:
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [
                {"category": "Bus", "amount": 100},
                {"category": "Lunch", "amount": 100},
            ],
        },
    }
    out = _detect_intent_during_expense_workflow(
        BN_SICK_LEAVE_VOICE,
        wf,
        balance_probe=False,
        trace_id="test-bn-leave",
    )
    assert out["intent"] == INTENT_LEAVE_REQUEST
    assert "leave_apply" in out.get("source", "") or "new_leave" in out.get("source", "")


def test_turn_classifier_new_workflow_for_bengali_leave() -> None:
    turn = classify_workflow_turn(
        BN_SICK_LEAVE_SHORT,
        leave_active=False,
        expense_active=True,
        expense_review_pending=True,
    )
    assert turn == TURN_NEW_WORKFLOW


def test_llm_fallback_new_leave_when_rules_miss(monkeypatch) -> None:
    monkeypatch.setattr(
        "chat.services.wizard_interrupt_classifier.LLMClient.is_configured",
        lambda self: True,
    )
    monkeypatch.setattr(
        "chat.services.wizard_interrupt_classifier.LLMClient.chat_json",
        lambda self, **kwargs: {
            "interrupt_type": "new_leave_request",
            "confidence": 0.91,
        },
    )
    ctx = WizardInterruptContext(expense_active=True, expense_stage="review")
    decision = classify_wizard_interrupt(
        "bodhoy kal off thakbo, ma er shathe doctor",
        context=ctx,
        trace_id="llm-leave",
        use_llm=True,
    )
    assert decision.interrupt_type == INTERRUPT_NEW_LEAVE
    assert decision.source == "llm_interrupt"


@pytest.mark.django_db
def test_bengali_leave_during_expense_review_suspends_expense(monkeypatch):
    """Regression: Bengali voice leave must not show expense help hint."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.wizard_interrupt_classifier.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "exp-bn-leave-switch"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message=(
            "আমার আজকে খরচ হয়েছে বাসে মিরপুর টু মতিঝিল একশো টাকা "
            "তারপরে খরচ হয়েছে মতিঝিল টু মিরপুর বাইকে ২০০ টাকা এবং লাঞ্ছ ১০০ টাকা"
        ),
        session_id=None,
        employee_id=emp,
        trace_id="exp-bn-leave-1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    assert is_expense_in_progress(session.workflow_state)

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message=BN_SICK_LEAVE_VOICE,
        session_id=sid,
        employee_id=emp,
        trace_id="exp-bn-leave-2",
    )
    session.refresh_from_db()
    msg = r2["response"]["message"] or ""
    assert r2["intent"] == INTENT_LEAVE_REQUEST
    assert "খরচ ফর্ম চলছে" not in msg
    assert has_suspended_expense(session.workflow_state) or not is_expense_in_progress(
        session.workflow_state
    )
    assert is_leave_in_progress(session.workflow_state)
