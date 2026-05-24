"""Post-submit expense day summary (Banglish / Bengali)."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_DAY_SUMMARY
from chat.services.intent_detector import IntentDetector
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"


def run_chat(orch: ChatOrchestrator, **kwargs):
    return orch.run_chat(company_id=COMPANY_ID, **kwargs)


def test_ajker_expense_list_intent(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    r = det.detect("amake ajker expense er list ta daw", "tid-list")
    assert r["intent"] == INTENT_EXPENSE_DAY_SUMMARY


@pytest.mark.django_db
def test_post_submit_expense_summary_banglish(monkeypatch):
    fixed = dt.date(2026, 5, 23)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, FixedDate)

    orch = ChatOrchestrator()
    emp = "exp-post-sum-bn"
    sid = None
    pack = run_chat(
        orch,
        message="lunch 100, train 60 uttora to mirpur, bike 50 badda to mirpur",
        session_id=sid,
        employee_id=emp,
        trace_id="eps-1",
    )
    sid = pack["_session_id"]
    for msg in ("শেষ", "হ্যাঁ", "হ্যাঁ"):
        pack = run_chat(
            orch,
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"eps-{msg}",
        )
    assert pack["decision"]["outcome"] == "SUBMITTED"

    summ = run_chat(
        orch,
        message="okay amake expense er summery ta daw toh",
        session_id=sid,
        employee_id=emp,
        trace_id="eps-sum",
    )
    assert summ["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    body = summ["response"]["message"] or ""
    assert "210" in body
    assert "Lunch" in body or "lunch" in body.lower()
    assert "Train" in body or "train" in body.lower()
    assert "সারাংশ" in body
    assert "আজকের খরচ বলুন" not in body

    list_resp = run_chat(
        orch,
        message="amake ajker expense er list ta daw",
        session_id=sid,
        employee_id=emp,
        trace_id="eps-list",
    )
    assert list_resp["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    list_body = list_resp["response"]["message"] or ""
    assert "210" in list_body
    assert "সারাংশ" in list_body
    assert "আজকের খরচ বলুন" not in list_body
