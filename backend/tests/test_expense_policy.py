"""Expense daily cap / limit policy answers."""

import datetime as dt

import pytest

from chat.constants import INTENT_HR_POLICY
from chat.services.expense.expense_policy import (
    build_daily_cap_response,
    format_expense_daily_cap_message,
    is_expense_daily_cap_query,
)
from chat.services.intent_detector import IntentDetector
from chat.services.orchestrator import ChatOrchestrator
from chat.services.policy_intent_helpers import is_expense_entitlement_query

COMPANY_ID = "company-a"


@pytest.mark.parametrize(
    "message",
    [
        "amar total koto cost limit?",
        "daily expense limit koto?",
        "expense cost limit",
        "ajker expense cap koto?",
    ],
)
def test_daily_cap_query_detected(message: str) -> None:
    assert is_expense_daily_cap_query(message)
    assert is_expense_entitlement_query(message)


@pytest.mark.parametrize(
    "message",
    [
        "lunch 100, bus 50",
        "350 taka cost hoyeche",
        "amar total cost koto hoyeche",
    ],
)
def test_daily_cap_not_spend_summary(message: str) -> None:
    assert not is_expense_daily_cap_query(message)


def test_format_daily_cap_message_includes_300():
    msg = format_expense_daily_cap_message(daily_cap=300)
    assert "300" in msg
    assert "cap" in msg.lower() or "Cap" in msg


def test_build_daily_cap_response_with_pending_draft():
    wf = {
        "expense_request": {
            "active": True,
            "incurred_date_iso": "2026-06-07",
            "items": [{"category": "Lunch", "amount": 200}],
        }
    }
    msg = build_daily_cap_response(wf, incurred_date_iso="2026-06-07", daily_cap=300)
    assert "300" in msg
    assert "200" in msg
    assert "Pending" in msg or "pending" in msg


def test_cost_limit_intent_is_hr_policy(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    r = det.detect("amar total koto cost limit?", "tid-cap")
    assert r["intent"] == INTENT_HR_POLICY


@pytest.mark.django_db
def test_cost_limit_during_expense_wizard(monkeypatch):
    fixed = dt.date(2026, 6, 7)
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))

    orch = ChatOrchestrator()
    emp = "cap-limit-wizard"
    pack = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 200 taka",
        session_id=None,
        employee_id=emp,
        trace_id="cap-1",
    )
    sid = pack["_session_id"]
    resp = orch.run_chat(
        company_id=COMPANY_ID,
        message="amar total koto cost limit?",
        session_id=sid,
        employee_id=emp,
        trace_id="cap-2",
    )
    assert resp["intent"] == INTENT_HR_POLICY
    body = resp["response"]["message"] or ""
    assert "300" in body
    assert "জমা পাওয়া যায়নি" not in body
    assert "আজকের খরচ বলুন" not in body
