"""Bengali voice expense meta + recent-day recall queries."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_DAY_SUMMARY
from chat.services.expense.session_action_memory import wants_expense_meta_question
from chat.services.expense.session_ledger import (
    infer_session_expense_summary_date,
    wants_recent_expense_recall_query,
    wants_session_expense_ledger_query,
)
from chat.services.expense_incurred_date import infer_expense_incurred_date_iso
from chat.services.intent_detector import _strong_expense_day_summary
from chat.services.orchestrator import ChatOrchestrator
from chat.services.policy_intent_helpers import (
    is_hr_assistant_in_scope,
    is_off_topic_for_hr_assistant,
)

COST_ADD_BN = "কত কস্ট এড করছি আমি"
RECALL_BN = "লাস্ট দিনে কোন এক্সপেন্স দিছিলাম আমি"


def test_bengali_cost_add_is_ledger_query() -> None:
    assert wants_session_expense_ledger_query(COST_ADD_BN)
    assert _strong_expense_day_summary(COST_ADD_BN)
    assert wants_expense_meta_question(COST_ADD_BN)


def test_bengali_last_day_recall_is_ledger_query() -> None:
    assert wants_recent_expense_recall_query(RECALL_BN)
    assert wants_session_expense_ledger_query(RECALL_BN)
    assert _strong_expense_day_summary(RECALL_BN)


def test_bengali_recall_not_out_of_scope() -> None:
    assert is_hr_assistant_in_scope(RECALL_BN)
    assert not is_off_topic_for_hr_assistant(RECALL_BN)


def test_last_day_maps_to_yesterday() -> None:
    fixed = dt.date(2026, 6, 8)
    iso = infer_expense_incurred_date_iso(
        message=RECALL_BN,
        hints={},
        today=fixed,
    )
    assert iso == "2026-06-07"
    summary_iso = infer_session_expense_summary_date(
        {},
        message=RECALL_BN,
        today=fixed,
    )
    assert summary_iso == "2026-06-07"


@pytest.mark.django_db
def test_orchestrator_recall_returns_ledger_not_oos(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.expense.session_ledger.infer_session_expense_summary_date",
        lambda *a, **k: "2026-06-07",
    )

    orch = ChatOrchestrator()
    emp = "exp-recall-bn"
    r = orch.run_chat(
        company_id="company-a",
        message=RECALL_BN,
        session_id=None,
        employee_id=emp,
        trace_id="exp-recall-bn-1",
    )
    msg = r["response"]["message"] or ""
    assert r["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    assert "HR বটের কাজ নয়" not in msg
    assert "স্কোপের বাইরে" not in msg
    assert "expense draft" in msg.lower() or "খরচ" in msg or "সারাংশ" in msg
