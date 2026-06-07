"""P3 — expense total check / dispute (deterministic recount)."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_STATUS
from chat.services.expense.expense_total_dispute import (
    format_expense_total_check_message,
    is_expense_total_check_query,
    is_expense_total_dispute_query,
    is_expense_total_verify_query,
    parse_user_stated_total,
)
from chat.services.expense.session_action_memory import (
    read_last_bot_action,
    wants_expense_meta_question,
)
from chat.services.expense_workflow import process_expense_turn
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("total mony hoy vul hoise, check", True),
        ("mot vul hoise check koro", True),
        ("total thik ache ki", True),
        ("mot koto hobe", True),
        ("hisab check koro", True),
        ("amar total koto cost limit?", False),
        ("ki add korcho?", False),
    ],
)
def test_is_expense_total_check_query(message, expected):
    assert is_expense_total_check_query(message) is expected


def test_verify_vs_dispute():
    assert is_expense_total_dispute_query("total mony vul hoise")
    assert is_expense_total_verify_query("total thik ache ki")
    assert not is_expense_total_dispute_query("total thik ache ki")


def test_parse_user_stated_total():
    assert parse_user_stated_total("total hoy 120 tk vul") == 120.0
    assert parse_user_stated_total("150 tk wrong") == 150.0
    assert parse_user_stated_total("total thik ache ki") is None


def test_total_check_not_meta_overlap():
    assert not wants_expense_meta_question("total thik ache ki")
    assert wants_expense_meta_question("ki add korcho?")


def test_format_expense_total_check_with_stated_total():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "incurred_date_iso": "2026-06-07",
            "items": [
                {"category": "Bus", "amount": 100},
                {"category": "Lunch", "amount": 50},
            ],
        }
    }
    msg = format_expense_total_check_message(
        wf,
        incurred_date_iso="2026-06-07",
        user_message="total hoy 120 tk vul",
    )
    assert msg
    assert "150" in msg
    assert "120" in msg
    assert "Next steps" in msg or "পরবর্তী" in msg


@pytest.mark.django_db
def test_total_check_via_workflow_review(monkeypatch):
    fixed = dt.date(2026, 6, 7)
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))

    wf: dict = {}
    p1 = process_expense_turn(workflow_state=wf, message="bus 100, lunch 50")
    wf = p1["workflow_state"]
    p2 = process_expense_turn(workflow_state=wf, message="total thik ache ki")
    assert "total check" in (p2.get("question") or "").lower() or "গণনা" in (p2.get("question") or "")
    action = read_last_bot_action(p2["workflow_state"])
    assert action.get("action_type") == "expense_total_check"


@pytest.mark.django_db
def test_total_verify_orchestrator_not_llm(monkeypatch):
    fixed = dt.date(2026, 6, 7)
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )

    orch = ChatOrchestrator()
    emp = "total-verify-p3"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="bus 100 mirpur to motijheel, lunch 50",
        session_id=None,
        employee_id=emp,
        trace_id="tv-1",
    )
    sid = r1["_session_id"]
    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="total thik ache ki",
        session_id=sid,
        employee_id=emp,
        trace_id="tv-2",
    )
    assert r2["intent"] == INTENT_EXPENSE_STATUS
    msg = r2["response"]["message"] or ""
    assert "total check" in msg.lower() or "লাইন" in msg
    assert "150" in msg
    assert "আমি শুধু" not in msg
