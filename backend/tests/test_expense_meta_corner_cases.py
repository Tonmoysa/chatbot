"""Corner-case meta questions during expense wizard (submit later, draft save, typos)."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_STATUS
from chat.services.expense.session_action_memory import (
    format_meta_question_answer,
    wants_expense_meta_question,
    wants_expense_submit_timing_question,
)
from chat.services.expense.wizard_commands import wants_expense_submit_command
from chat.services.intent_detector import IntentDetector
from chat.services.orchestrator import ChatOrchestrator
from chat.services.response_formatter import build_user_message

COMPANY_ID = "company-a"


@pytest.mark.parametrize(
    "message",
    [
        "can i submit it later?",
        "can i submit it letter?",
        "can I submit later",
        "do i need to submit now?",
        "pore submit korbo?",
        "submit korbo pore?",
        "will my draft be saved?",
    ],
)
def test_meta_corner_cases_detected(message):
    assert wants_expense_meta_question(message), message
    assert not wants_expense_submit_command(message), message


def test_submit_timing_answer_pending_category():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "incurred_date_iso": "2026-06-09",
            "pending_step": "category",
            "pending_line": {"amount": 200, "category": ""},
            "items": [],
        }
    }
    ans = format_meta_question_answer(wf, "can i submit it later?", lang="en")
    assert ans
    assert "later" in ans.lower() or "submit" in ans.lower()
    assert "200" in ans
    assert "category" in ans.lower()
    assert "reference" not in ans.lower()


def test_submit_timing_answer_typo_letter():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "pending_step": "category",
            "pending_line": {"amount": 200, "category": ""},
            "items": [],
        }
    }
    ans = format_meta_question_answer(wf, "can i submit it letter?", lang="en")
    assert ans
    assert "later" in ans.lower() or "submit" in ans.lower()
    assert "200" in ans


def test_response_formatter_no_reference_id_on_wizard_detail():
    msg, status = build_user_message(
        intent=INTENT_EXPENSE_STATUS,
        entities={},
        decision={"outcome": "INFORMATIONAL", "reason": ""},
        crm_payload={
            "detail": "Missing request_id for status lookup.",
            "expense_wizard_active": True,
            "expense_wizard_stage": "collecting",
        },
    )
    assert "reference" not in msg.lower()
    assert "re-check" not in msg.lower()
    low = msg.lower()
    assert (
        "draft" in low
        or "submit" in low
        or "জমা" in msg
        or "খরচ" in msg
        or "confirm" in low
    )
    assert status == "needs_input"


def test_intent_detector_submit_later_is_status_not_claim(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    out = det.detect("can i submit it later?", trace_id="t-later")
    assert out["intent"] == INTENT_EXPENSE_STATUS


@pytest.mark.django_db
def test_orchestrator_submit_later_during_category_prompt(monkeypatch):
    fixed = dt.date(2026, 6, 9)
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))

    orch = ChatOrchestrator()
    emp = "submit-later-emp"
    start = orch.run_chat(
        company_id=COMPANY_ID,
        message="amar ajker expense 200 taka",
        session_id=None,
        employee_id=emp,
        trace_id="sl-start",
    )
    sid = start["_session_id"]
    resp = orch.run_chat(
        company_id=COMPANY_ID,
        message="can i submit it letter?",
        session_id=sid,
        employee_id=emp,
        trace_id="sl-ask",
    )
    msg = resp["response"]["message"] or ""
    assert resp.get("intent") == INTENT_EXPENSE_STATUS
    assert "reference" not in msg.lower()
    assert "re-check" not in msg.lower()
    assert "later" in msg.lower() or "submit" in msg.lower() or "পরে" in msg or "হ্যাঁ" in msg
