"""Leave balance query detection — especially during expense wizard."""

import pytest

from chat.constants import INTENT_LEAVE_BALANCE
from chat.services.intent_detector import IntentDetector, looks_like_wizard_side_question
from chat.services.leave_balance_intent import is_leave_balance_query
from chat.services.orchestrator import (
    ChatOrchestrator,
    _detect_intent_during_expense_workflow,
    _leave_balance_probe,
)

COMPANY_ID = "company-a"


@pytest.mark.parametrize(
    "message",
    [
        "amar koy ta leave ache?",
        "amar koyta leave ache",
        "kotodin chuti ache",
        "koy din leave baki",
        "how many leave days do I have",
        "আমার কয়টা ছুটি আছে",
        "leave balance koto",
    ],
)
def test_is_leave_balance_query_positive(message):
    assert is_leave_balance_query(message)
    assert _leave_balance_probe(message)


@pytest.mark.parametrize(
    "message",
    [
        "ami kalke sick leave nite chai",
        "amar ajke expense hoyeche 100 taka bus e",
        "lunch 100 taka",
        "travel cost remove koro",
    ],
)
def test_is_leave_balance_query_negative(message):
    assert not is_leave_balance_query(message)


def test_leave_balance_is_wizard_side_question():
    assert looks_like_wizard_side_question("amar koy ta leave ache?")


def test_leave_balance_during_expense_workflow_gate():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"category": "Bus", "amount": 100}],
        }
    }
    out = _detect_intent_during_expense_workflow(
        "amar koy ta leave ache?",
        wf,
        balance_probe=True,
    )
    assert out["intent"] == INTENT_LEAVE_BALANCE
    assert out["source"] == "expense_workflow_gate+balance"


@pytest.mark.django_db
def test_leave_balance_during_active_expense_answers_balance_not_bus_prompt(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )

    orch = ChatOrchestrator()
    emp = "leave-bal-exp-int"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="bus 100 mirpur to motijheel, lunch 50",
        session_id=None,
        employee_id=emp,
        trace_id="lb-exp-1",
    )
    sid = r1["_session_id"]

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="amar koy ta leave ache?",
        session_id=sid,
        employee_id=emp,
        trace_id="lb-exp-2",
    )

    assert r2["intent"] == INTENT_LEAVE_BALANCE
    msg = r2["response"]["message"] or ""
    assert "leave balance" in msg.lower() or "ছুটি" in msg or "day" in msg.lower()
    assert "From ও To" not in msg
    assert "Bus খরচ" not in msg

    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    session.refresh_from_db()
    block = session.workflow_state.get("expense_request") or {}
    assert block.get("paused") is True
    assert list(block.get("items") or [])


def test_intent_detector_rules_leave_balance():
    det = IntentDetector()
    out = det.detect("amar koy ta leave ache?", trace_id="lb-det")
    assert out["intent"] == INTENT_LEAVE_BALANCE
